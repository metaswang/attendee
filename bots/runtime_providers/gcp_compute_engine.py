import json
import logging
import os
import shlex
import uuid
import time
from pathlib import Path

from django.urls import reverse
from django.utils import timezone

from bots.bots_api_utils import build_site_url
from bots.models import BotRuntimeLease, BotRuntimeLeaseStatuses, BotRuntimeProviderTypes

logger = logging.getLogger(__name__)

try:
    from google.api_core.exceptions import NotFound
    from google.cloud import compute_v1
except ImportError:  # pragma: no cover - exercised indirectly in environments without the dependency
    NotFound = None
    compute_v1 = None


class GCPComputeEngineError(RuntimeError):
    pass


class GCPComputeInstanceProvider:
    ENV_PREFIX_EXCLUDES = (
        "BOT_CONTAINER_",
        "DO_BOT_",
        "GCP_BOT_",
        "MODAL__",
        "MODAL_BOT__",
        "MODAL_EXPERIMENT__",
    )
    ENV_EXCLUDES = {
        "BOT_HOST_CODE_PATH",
        "BOT_MAX_SIMULTANEOUS_BOTS",
        "DATABASE_URL",
        "DROPLET_API_KEY",
        "GCP_APPLICATION_CREDENTIALS_JSON",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "PGDATABASE",
        "PGHOST",
        "PGPASSWORD",
        "PGPORT",
        "PGUSER",
        "PULSE_RUNTIME_PATH",
        "PULSE_SERVER",
        "XDG_RUNTIME_DIR",
    }

    def __init__(self):
        if compute_v1 is None:
            raise GCPComputeEngineError("google-cloud-compute is not installed")
        self.project_id = os.getenv("GCP_PROJECT_ID")
        if not self.project_id:
            raise GCPComputeEngineError("GCP_PROJECT_ID is not set")
        self.instances_client = compute_v1.InstancesClient()
        self.images_client = compute_v1.ImagesClient()
        self.zone_operations_client = compute_v1.ZoneOperationsClient()

    def _default_region(self) -> str:
        return os.getenv("GCP_BOT_DEFAULT_REGION") or os.getenv("GCP_BOT_REGION") or "us-central1"

    def _runtime_callback_url(self, lease: BotRuntimeLease) -> str:
        return build_site_url(reverse("bots_internal:bot-runtime-lease-complete", args=[lease.id]))

    def _runtime_bootstrap_url(self, lease: BotRuntimeLease) -> str:
        return build_site_url(reverse("bots_internal:bot-runtime-lease-bootstrap", args=[lease.id]))

    def _runtime_control_url(self, lease: BotRuntimeLease) -> str:
        return build_site_url(reverse("bots_internal:bot-runtime-lease-control", args=[lease.id]))

    def _runtime_source_archive_url(self, lease: BotRuntimeLease) -> str:
        return build_site_url(reverse("bots_internal:bot-runtime-lease-source-archive", args=[lease.id]))

    def _tags(self, bot) -> list[str]:
        base_tags = [item.strip() for item in os.getenv("GCP_BOT_TAGS", "attendee-bot").split(",") if item.strip()]
        base_tags.append(f"attendee-bot-{bot.id}")
        return list(dict.fromkeys(base_tags))

    def _labels(self, bot, lease: BotRuntimeLease) -> dict:
        labels = {
            "app": "attendee-bot",
            "bot-id": str(bot.id),
            "lease-id": str(lease.id),
            "runtime-provider": "gcp-compute-instance",
        }
        extra_labels = {}
        raw_extra_labels = os.getenv("GCP_BOT_LABELS_JSON", "").strip()
        if raw_extra_labels:
            extra_labels = json.loads(raw_extra_labels)
        labels.update(extra_labels)
        return labels

    def _serialized_runtime_env(self, bot, lease: BotRuntimeLease) -> str:
        region = bot.runtime_region(self._default_region())
        zone = ((lease.metadata or {}).get("instance") or {}).get("zone") or ""
        env_vars = {}
        for key, value in os.environ.items():
            if key in self.ENV_EXCLUDES:
                continue
            if any(key.startswith(prefix) for prefix in self.ENV_PREFIX_EXCLUDES):
                continue
            env_vars[key] = value

        env_vars.update(
            {
                "BOT_ID": str(bot.id),
                "BOT_OBJECT_ID": bot.object_id,
                "BOT_LAUNCH_REQUESTED_AT": timezone.now().isoformat(),
                "BOT_RUNTIME_PROVIDER": BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
                "BOT_RUNTIME_BOOTSTRAP_URL": self._runtime_bootstrap_url(lease),
                "BOT_RUNTIME_CONTROL_URL": self._runtime_control_url(lease),
                "BOT_RUNTIME_SOURCE_ARCHIVE_URL": self._runtime_source_archive_url(lease),
                "DJANGO_SETTINGS_MODULE": "attendee.settings.bot_runtime",
                "ATTENDEE_REPO_DIR": "/opt/attendee",
                "BOT_RUNTIME_REGION": region,
                "BOT_RUNTIME_ZONE": zone,
                "GCP_INSTANCE_NAME": bot.gcp_instance_name(),
                "IS_GCP_BOT_RUNNER": "true",
                "LEASE_CALLBACK_URL": self._runtime_callback_url(lease),
                "LEASE_ID": str(lease.id),
                "LEASE_SHUTDOWN_TOKEN": lease.shutdown_token,
            }
        )
        return "\n".join(f"export {key}={shlex.quote(str(value))}" for key, value in sorted(env_vars.items()))

    def _startup_script(self, bot, lease: BotRuntimeLease) -> str:
        env_contents = self._serialized_runtime_env(bot, lease)
        runner_script_contents = (Path(__file__).resolve().parents[2] / "scripts/digitalocean/attendee-bot-runner.sh").read_text()
        runner_service_contents = (Path(__file__).resolve().parents[2] / "scripts/digitalocean/attendee-bot-runner.service").read_text()
        return "\n".join(
            [
                "#!/bin/bash",
                "set -euo pipefail",
                "mkdir -p /etc/attendee",
                "cat >/etc/attendee/runtime.env <<'EOF'",
                env_contents,
                "EOF",
                "chmod 0644 /etc/attendee/runtime.env",
                "cat >/usr/local/bin/attendee-bot-runner <<'EOF_RUNNER'",
                runner_script_contents,
                "EOF_RUNNER",
                "chmod 0755 /usr/local/bin/attendee-bot-runner",
                "cat >/etc/systemd/system/attendee-bot-runner.service <<'EOF_SERVICE'",
                runner_service_contents,
                "EOF_SERVICE",
                "chmod 0644 /etc/systemd/system/attendee-bot-runner.service",
                "set -a",
                "# shellcheck disable=SC1091",
                "source /etc/attendee/runtime.env",
                "set +a",
                "log_startup() {",
                "  echo \"[attendee-startup] $*\"",
                "}",
                "sync_attendee_source_archive() {",
                "  local repo_dir",
                "  local source_archive_url",
                "  local temp_dir",
                "  repo_dir=\"${ATTENDEE_REPO_DIR:-/opt/attendee}\"",
                "  source_archive_url=\"${BOT_RUNTIME_SOURCE_ARCHIVE_URL:-}\"",
                "  if [[ -z \"$source_archive_url\" ]]; then",
                "    log_startup \"Skipping source archive sync: source_archive_url missing\"",
                "    return 1",
                "  fi",
                "  temp_dir=\"$(mktemp -d)\"",
                "  log_startup \"Syncing source archive into $repo_dir from $source_archive_url\"",
                "  if ! curl -fsSL --retry 3 --connect-timeout 10 --max-time 180 -H \"Authorization: Bearer ${LEASE_SHUTDOWN_TOKEN}\" \"$source_archive_url\" | tar -xzf - -C \"$temp_dir\"; then",
                "    rm -rf \"$temp_dir\"",
                "    log_startup \"Source archive sync failed\"",
                "    return 1",
                "  fi",
                "  rm -rf \"$repo_dir\"",
                "  mkdir -p \"$repo_dir\"",
                "  cp -a \"$temp_dir/.\" \"$repo_dir/\"",
                "  rm -rf \"$temp_dir\"",
                "  log_startup \"Source archive sync complete\"",
                "  return 0",
                "}",
                "sync_attendee_repo() {",
                "  local git_bin",
                "  local repo_dir",
                "  local repo_url",
                "  local git_ref",
                "  git_bin=\"$(command -v git 2>/dev/null || true)\"",
                "  repo_dir=\"${ATTENDEE_REPO_DIR:-/opt/attendee}\"",
                "  repo_url=\"${ATTENDEE_REPO_URL:-}\"",
                "  git_ref=\"${ATTENDEE_GIT_REF:-main}\"",
                "  if [[ -z \"$git_bin\" || -z \"$repo_url\" ]]; then",
                "    log_startup \"Skipping repo sync: git_bin=${git_bin:-missing} repo_url=${repo_url:-missing}\"",
                "    return 0",
                "  fi",
                "  log_startup \"Syncing repo dir=$repo_dir ref=$git_ref url=$repo_url\"",
                "  if [[ -d \"$repo_dir/.git\" ]]; then",
                "    timeout 120 \"$git_bin\" -C \"$repo_dir\" fetch --all --tags",
                "    if \"$git_bin\" -C \"$repo_dir\" show-ref --verify --quiet \"refs/remotes/origin/${git_ref}\"; then",
                "      timeout 120 \"$git_bin\" -C \"$repo_dir\" checkout -B \"$git_ref\" \"origin/${git_ref}\"",
                "    fi",
                "  else",
                "    rm -rf \"$repo_dir\"",
                "    timeout 120 \"$git_bin\" clone --depth 1 --branch \"$git_ref\" \"$repo_url\" \"$repo_dir\"",
                "  fi",
                "  log_startup \"Repo sync complete: $(timeout 30 \"$git_bin\" -C \"$repo_dir\" rev-parse --short HEAD 2>/dev/null || echo unknown)\"",
                "}",
                "if ! sync_attendee_source_archive; then",
                "  if ! sync_attendee_repo; then",
                "    if [[ -f \"${ATTENDEE_REPO_DIR:-/opt/attendee}/manage.py\" ]]; then",
                "      log_startup \"Falling back to existing repo checkout at ${ATTENDEE_REPO_DIR:-/opt/attendee}\"",
                "    else",
                "      echo \"Unable to sync bot runtime source code\" >&2",
                "      exit 1",
                "    fi",
                "  fi",
                "fi",
                "if ! docker image inspect \"$BOT_RUNTIME_IMAGE\" >/dev/null 2>&1; then",
                "  echo \"Missing pre-baked bot runtime image: $BOT_RUNTIME_IMAGE\" >&2",
                "  echo \"Bake it into the GCP golden image with scripts/gcp/prepare-golden-image.sh\" >&2",
                "  exit 1",
                "fi",
                "log_startup \"Reloading systemd units\"",
                "systemctl daemon-reload",
                "log_startup \"Restarting attendee-bot-runner.service\"",
                "systemctl restart attendee-bot-runner.service",
            ]
        )

    def _zones_for_region(self, region: str) -> list[str]:
        raw_json = os.getenv("GCP_BOT_REGION_ZONES_JSON", "").strip()
        if raw_json:
            zone_map = json.loads(raw_json)
            zones = zone_map.get(region) or []
            if zones:
                return zones
        raw_default_zones = os.getenv("GCP_BOT_ZONES", "").strip()
        if raw_default_zones:
            return [item.strip() for item in raw_default_zones.split(",") if item.strip()]
        fallback_zone = os.getenv("GCP_BOT_DEFAULT_ZONE")
        if fallback_zone:
            return [fallback_zone]
        return [f"{region}-b"]

    def _subnetwork_for_region(self, region: str) -> str | None:
        raw_json = os.getenv("GCP_BOT_REGION_SUBNETWORKS_JSON", "").strip()
        if raw_json:
            subnet_map = json.loads(raw_json)
            if subnet_map.get(region):
                return subnet_map[region]
        return os.getenv("GCP_BOT_SUBNETWORK")

    def _private_only(self) -> bool:
        return os.getenv("GCP_BOT_PRIVATE_ONLY", "false").strip().lower() in {"1", "true", "yes", "on"}

    def _source_image(self) -> str:
        source_image = os.getenv("GCP_BOT_SOURCE_IMAGE")
        if source_image:
            return source_image

        image_family = os.getenv("GCP_BOT_SOURCE_IMAGE_FAMILY")
        image_project = os.getenv("GCP_BOT_SOURCE_IMAGE_PROJECT") or self.project_id
        if image_family:
            return f"projects/{image_project}/global/images/family/{image_family}"

        raise GCPComputeEngineError("GCP_BOT_SOURCE_IMAGE or GCP_BOT_SOURCE_IMAGE_FAMILY must be set")

    def _source_image_project_and_name(self) -> tuple[str, str, bool]:
        source_image = self._source_image()
        normalized_source_image = source_image.lstrip("/")
        if normalized_source_image.startswith("projects/") and "/global/images/family/" in normalized_source_image:
            project_part, family = normalized_source_image.rsplit("/global/images/family/", 1)
            project = project_part.split("projects/", 1)[1]
            return project, family, True
        if normalized_source_image.startswith("projects/") and "/global/images/" in normalized_source_image:
            project_part, image_name = normalized_source_image.rsplit("/global/images/", 1)
            project = project_part.split("projects/", 1)[1]
            return project, image_name, False
        return os.getenv("GCP_BOT_SOURCE_IMAGE_PROJECT") or self.project_id, source_image, False

    def _source_image_min_disk_size_gb(self) -> int:
        source_project, source_name, is_family = self._source_image_project_and_name()
        try:
            if is_family:
                image = self.images_client.get_from_family(project=source_project, family=source_name)
            else:
                image = self.images_client.get(project=source_project, image=source_name)
            raw_disk_size_gb = getattr(image, "disk_size_gb", None)
            if raw_disk_size_gb is None:
                raw_disk_size_gb = getattr(image, "diskSizeGb", None)
            if raw_disk_size_gb is None:
                raise GCPComputeEngineError(f"Unable to determine disk size for source image {self._source_image()}")
            return int(raw_disk_size_gb)
        except Exception as exc:
            logger.warning("Unable to determine disk size for source image %s, using 100 GB fallback: %s", self._source_image(), exc)
            return 100

    def _build_instance(self, bot, lease: BotRuntimeLease, zone: str, region: str):
        machine_type_name = bot.gcp_machine_type()
        machine_type = f"zones/{zone}/machineTypes/{machine_type_name}"
        requested_boot_disk_size_gb = bot.gcp_boot_disk_size_gb()
        boot_disk_size_gb = max(requested_boot_disk_size_gb, self._source_image_min_disk_size_gb())
        if boot_disk_size_gb != requested_boot_disk_size_gb:
            logger.info(
                "Increasing GCP boot disk size for bot %s from %s GB to %s GB to satisfy source image requirements",
                bot.object_id,
                requested_boot_disk_size_gb,
                boot_disk_size_gb,
            )
        boot_disk = compute_v1.AttachedDisk(
            auto_delete=True,
            boot=True,
            type_=compute_v1.AttachedDisk.Type.PERSISTENT.name,
            initialize_params=compute_v1.AttachedDiskInitializeParams(
                source_image=self._source_image(),
                disk_size_gb=boot_disk_size_gb,
                disk_type=f"zones/{zone}/diskTypes/{os.getenv('GCP_BOT_DISK_TYPE', 'pd-balanced')}",
            ),
        )

        network_interface = compute_v1.NetworkInterface()
        subnetwork = self._subnetwork_for_region(region)
        if subnetwork:
            network_interface.subnetwork = subnetwork
        elif os.getenv("GCP_BOT_NETWORK"):
            network_interface.network = os.getenv("GCP_BOT_NETWORK")
        if not self._private_only():
            network_interface.access_configs = [
                compute_v1.AccessConfig(
                    name="External NAT",
                    type_="ONE_TO_ONE_NAT",
                    network_tier=os.getenv("GCP_BOT_NETWORK_TIER", "PREMIUM"),
                )
            ]

        metadata_items = [
            compute_v1.Items(key="startup-script", value=self._startup_script(bot, lease)),
        ]

        service_account_email = os.getenv("GCP_BOT_SERVICE_ACCOUNT_EMAIL")
        service_accounts = None
        if service_account_email:
            service_accounts = [
                compute_v1.ServiceAccount(
                    email=service_account_email,
                    scopes=[item.strip() for item in os.getenv("GCP_BOT_SERVICE_ACCOUNT_SCOPES", "https://www.googleapis.com/auth/cloud-platform").split(",") if item.strip()],
                )
            ]

        return compute_v1.Instance(
            name=bot.gcp_instance_name(),
            machine_type=machine_type,
            disks=[boot_disk],
            network_interfaces=[network_interface],
            metadata=compute_v1.Metadata(items=metadata_items),
            tags=compute_v1.Tags(items=self._tags(bot)),
            labels=self._labels(bot, lease),
            service_accounts=service_accounts,
            can_ip_forward=False,
            deletion_protection=False,
        )

    def _wait_for_operation(self, zone: str, operation_name: str):
        logger.info("Waiting for GCP operation %s in zone %s", operation_name, zone)
        operation = self.zone_operations_client.wait(project=self.project_id, zone=zone, operation=operation_name)
        if getattr(operation, "error", None) and getattr(operation.error, "errors", None):
            first_error = operation.error.errors[0]
            raise GCPComputeEngineError(first_error.message or f"GCP operation {operation_name} failed")
        return operation

    def _wait_for_instance_absent(self, zone: str, instance_name: str, timeout_seconds: int = 300):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                self.instances_client.get(project=self.project_id, zone=zone, instance=instance_name)
            except Exception as exc:
                if NotFound and isinstance(exc, NotFound):
                    return
                logger.warning(
                    "Transient error while waiting for GCP instance %s to disappear in zone %s: %s",
                    instance_name,
                    zone,
                    exc,
                )
            else:
                time.sleep(5)
        raise GCPComputeEngineError(f"Timed out waiting for GCP instance {instance_name} to be deleted in zone {zone}")

    def get_or_create_lease(self, bot) -> BotRuntimeLease:
        region = bot.runtime_region(self._default_region())
        lease, _ = BotRuntimeLease.objects.get_or_create(
            bot=bot,
            defaults={
                "provider": BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
                "region": region,
                "size_class": bot.gcp_machine_type(),
                "snapshot_id": self._source_image(),
            },
        )
        return lease

    def provision_bot(self, bot) -> BotRuntimeLease:
        lease = self.get_or_create_lease(bot)
        if lease.provider_instance_id and lease.status in {BotRuntimeLeaseStatuses.PROVISIONING, BotRuntimeLeaseStatuses.ACTIVE}:
            logger.info("Bot %s already has GCP lease %s (%s), skipping duplicate provision", bot.object_id, lease.id, lease.provider_instance_id)
            return lease

        region = bot.runtime_region(self._default_region())
        last_error = None
        for zone in self._zones_for_region(region):
            try:
                instance = self._build_instance(bot, lease, zone=zone, region=region)
                request_id = str(uuid.uuid4())
                logger.info(
                    "Provisioning GCP instance for bot %s via lease %s in region=%s zone=%s machine_type=%s request_id=%s",
                    bot.object_id,
                    lease.id,
                    region,
                    zone,
                    bot.gcp_machine_type(),
                    request_id,
                )
                operation = self.instances_client.insert(
                    project=self.project_id,
                    zone=zone,
                    instance_resource=instance,
                )
                self._wait_for_operation(zone, operation.name)
                created_instance = self.instances_client.get(project=self.project_id, zone=zone, instance=bot.gcp_instance_name())
                lease.provider = BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE
                lease.status = BotRuntimeLeaseStatuses.PROVISIONING
                lease.provider_instance_id = created_instance.name
                lease.provider_name = created_instance.name
                lease.region = region
                lease.size_class = bot.gcp_machine_type()
                lease.snapshot_id = self._source_image()
                lease.metadata = {
                    "instance": {
                        "id": str(created_instance.id),
                        "name": created_instance.name,
                        "zone": zone,
                        "machine_type": bot.gcp_machine_type(),
                    },
                    "request": {
                        "project_id": self.project_id,
                        "region": region,
                        "zone": zone,
                        "request_id": request_id,
                        "private_only": self._private_only(),
                    },
                }
                lease.last_error = None
                lease.save()
                logger.info("Provisioned GCP instance %s for bot %s via lease %s", lease.provider_instance_id, bot.object_id, lease.id)
                return lease
            except Exception as exc:
                last_error = str(exc)
                if "already exists" in last_error or "409" in last_error:
                    logger.info(
                        "GCP instance %s still exists in zone %s; waiting for deletion before retrying",
                        bot.gcp_instance_name(),
                        zone,
                    )
                    self._wait_for_instance_absent(zone, bot.gcp_instance_name())
                    try:
                        instance = self._build_instance(bot, lease, zone=zone, region=region)
                        operation = self.instances_client.insert(
                            project=self.project_id,
                            zone=zone,
                            instance_resource=instance,
                        )
                        self._wait_for_operation(zone, operation.name)
                        created_instance = self.instances_client.get(project=self.project_id, zone=zone, instance=bot.gcp_instance_name())
                        lease.provider = BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE
                        lease.status = BotRuntimeLeaseStatuses.PROVISIONING
                        lease.provider_instance_id = created_instance.name
                        lease.provider_name = created_instance.name
                        lease.region = region
                        lease.size_class = bot.gcp_machine_type()
                        lease.snapshot_id = self._source_image()
                        lease.metadata = {
                            "instance": {
                                "id": str(created_instance.id),
                                "name": created_instance.name,
                                "zone": zone,
                                "machine_type": bot.gcp_machine_type(),
                            },
                            "request": {
                                "project_id": self.project_id,
                                "region": region,
                                "zone": zone,
                                "request_id": request_id,
                                "private_only": self._private_only(),
                            },
                        }
                        lease.last_error = None
                        lease.save()
                        logger.info("Provisioned GCP instance %s for bot %s via lease %s", lease.provider_instance_id, bot.object_id, lease.id)
                        return lease
                    except Exception as retry_exc:
                        last_error = str(retry_exc)
                logger.warning("Failed provisioning bot %s in zone %s: %s", bot.object_id, zone, last_error)

        raise GCPComputeEngineError(last_error or "No zones available for provisioning")

    def delete_lease(self, lease: BotRuntimeLease, missing_is_deleted: bool = True) -> BotRuntimeLease:
        if not lease.provider_instance_id:
            if missing_is_deleted:
                lease.mark_deleted()
            return lease

        zone = ((lease.metadata or {}).get("instance") or {}).get("zone")
        if not zone:
            raise GCPComputeEngineError(f"Missing zone for lease {lease.id}")

        request_id = str(uuid.uuid4())
        try:
            logger.info("Deleting GCP instance %s for lease %s in zone %s request_id=%s", lease.provider_instance_id, lease.id, zone, request_id)
            operation = self.instances_client.delete(project=self.project_id, zone=zone, instance=lease.provider_instance_id)
            lease.mark_delete_requested()
            self._wait_for_operation(zone, operation.name)
            self._wait_for_instance_absent(zone, lease.provider_instance_id)
        except Exception as exc:
            if NotFound and isinstance(exc, NotFound):
                if missing_is_deleted:
                    lease.mark_deleted()
                return lease
            raise

        if missing_is_deleted:
            lease.mark_deleted()
        return lease

    def fetch_lease_state(self, lease: BotRuntimeLease):
        if not lease.provider_instance_id:
            return None
        zone = ((lease.metadata or {}).get("instance") or {}).get("zone")
        if not zone:
            return None
        try:
            instance = self.instances_client.get(project=self.project_id, zone=zone, instance=lease.provider_instance_id)
        except Exception as exc:
            if NotFound and isinstance(exc, NotFound):
                return None
            raise
        return {
            "id": str(instance.id),
            "name": instance.name,
            "status": instance.status,
            "zone": zone,
        }

    def sync_lease(self, lease: BotRuntimeLease) -> BotRuntimeLease:
        instance = self.fetch_lease_state(lease)
        if instance is None:
            if lease.status != BotRuntimeLeaseStatuses.DELETED:
                lease.mark_deleted()
            return lease

        lease.provider_name = instance.get("name") or lease.provider_name
        lease.metadata = {**(lease.metadata or {}), "instance": instance}
        if lease.bot.first_heartbeat_timestamp and lease.status == BotRuntimeLeaseStatuses.PROVISIONING:
            lease.status = BotRuntimeLeaseStatuses.ACTIVE
            lease.active_at = lease.active_at or timezone.now()
        lease.save(update_fields=["provider_name", "metadata", "status", "active_at", "updated_at"])
        logger.info(
            "Synced GCP lease %s for bot %s status=%s zone=%s",
            lease.id,
            lease.bot.object_id,
            lease.status,
            instance.get("zone"),
        )
        return lease
