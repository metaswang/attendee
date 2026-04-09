import json
import logging
import os
import uuid
import time
from pathlib import Path

from django.urls import reverse
from django.utils import timezone

from bots.bots_api_utils import build_site_url
from bots.models import BotRuntimeLease, BotRuntimeLeaseStatuses, BotRuntimeProviderTypes
from bots.runtime_providers.host_runtime import runtime_agent_env, runtime_agent_env_file_contents, runtime_agent_heartbeat_key, runtime_command_payload, runtime_container_env, runtime_container_name, runtime_queue_key, serialize_runtime_env
from bots.runtime_scheduler import bot_key, gcp_idle_shutdown_seconds, gcp_vm_slot_capacity, lease_key, redis_client, runtime_capacity_summary

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
    HOST_METADATA_KEY = "meetbot:gcp:host"
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
        self.zone_instance_group_client = getattr(compute_v1, "RegionOperationsClient", None)

    def _default_region(self) -> str:
        return os.getenv("GCP_BOT_DEFAULT_REGION") or os.getenv("GCP_BOT_REGION") or "us-central1"

    def _runtime_callback_url(self, lease: BotRuntimeLease) -> str:
        runtime_api_base_url = os.getenv("MEETBOT_RUNTIME_API_BASE_URL", "").strip().rstrip("/")
        if runtime_api_base_url:
            return f"{runtime_api_base_url}/internal/attendee-runtime-leases/{lease.id}/complete"
        return build_site_url(reverse("bots_internal:bot-runtime-lease-complete", args=[lease.id]))

    def _runtime_bootstrap_url(self, lease: BotRuntimeLease) -> str:
        runtime_api_base_url = os.getenv("MEETBOT_RUNTIME_API_BASE_URL", "").strip().rstrip("/")
        if runtime_api_base_url:
            return f"{runtime_api_base_url}/internal/attendee-runtime-leases/{lease.id}/bootstrap"
        return build_site_url(reverse("bots_internal:bot-runtime-lease-bootstrap", args=[lease.id]))

    def _runtime_control_url(self, lease: BotRuntimeLease) -> str:
        runtime_api_base_url = os.getenv("MEETBOT_RUNTIME_API_BASE_URL", "").strip().rstrip("/")
        if runtime_api_base_url:
            return f"{runtime_api_base_url}/internal/attendee-runtime-leases/{lease.id}/control"
        return build_site_url(reverse("bots_internal:bot-runtime-lease-control", args=[lease.id]))

    def _runtime_source_archive_url(self, lease: BotRuntimeLease) -> str:
        runtime_api_base_url = os.getenv("MEETBOT_RUNTIME_API_BASE_URL", "").strip().rstrip("/")
        if runtime_api_base_url:
            return f"{runtime_api_base_url}/internal/attendee-runtime-leases/{lease.id}/source-archive"
        return build_site_url(reverse("bots_internal:bot-runtime-lease-source-archive", args=[lease.id]))

    def _tags(self, bot) -> list[str]:
        base_tags = [item.strip() for item in os.getenv("GCP_BOT_HOST_TAGS", "attendee-bot-host").split(",") if item.strip()]
        if bot is not None:
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
        host_name = ((lease.metadata or {}).get("host") or {}).get("name") or lease.provider_instance_id or bot.gcp_instance_name()
        slot_index = int(((lease.metadata or {}).get("slot") or {}).get("index") or 0)
        return serialize_runtime_env(
            runtime_container_env(
                bot,
                lease,
                host_name=host_name,
                slot_index=slot_index,
                provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            )
        )

    def _host_name(self, region: str) -> str:
        return f"attendee-gcp-host-{region}-{uuid.uuid4().hex[:8]}"

    def _bootstrap_startup_script(self, host_name: str, lease: BotRuntimeLease, zone: str, region: str) -> str:
        runner_script_contents = (Path(__file__).resolve().parents[2] / "scripts/digitalocean/attendee-bot-runner.sh").read_text()
        runner_service_contents = (Path(__file__).resolve().parents[2] / "scripts/digitalocean/attendee-bot-runner.service").read_text()
        runtime_agent_script_contents = (Path(__file__).resolve().parents[2] / "scripts/runtime_agent.py").read_text()
        runtime_agent_service_contents = (Path(__file__).resolve().parents[2] / "scripts/digitalocean/attendee-runtime-agent.service").read_text()
        return "\n".join(
            [
                "#!/bin/bash",
                "set -euo pipefail",
                "mkdir -p /etc/attendee /var/log/attendee",
                "cat >/etc/attendee/runtime-agent.env <<'EOF_AGENT_ENV'",
                runtime_agent_env_file_contents(host_name, runtime_queue_key(host_name)),
                "EOF_AGENT_ENV",
                "chmod 0644 /etc/attendee/runtime-agent.env",
                "cat >/usr/local/bin/attendee-runtime-agent <<'EOF_AGENT'",
                runtime_agent_script_contents,
                "EOF_AGENT",
                "chmod 0755 /usr/local/bin/attendee-runtime-agent",
                "cat >/etc/systemd/system/attendee-runtime-agent.service <<'EOF_AGENT_SERVICE'",
                runtime_agent_service_contents,
                "EOF_AGENT_SERVICE",
                "chmod 0644 /etc/systemd/system/attendee-runtime-agent.service",
                "cat >/usr/local/bin/attendee-bot-runner <<'EOF_RUNNER'",
                runner_script_contents,
                "EOF_RUNNER",
                "chmod 0755 /usr/local/bin/attendee-bot-runner",
                "cat >/etc/systemd/system/attendee-bot-runner.service <<'EOF_SERVICE'",
                runner_service_contents,
                "EOF_SERVICE",
                "chmod 0644 /etc/systemd/system/attendee-bot-runner.service",
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
                "    return 0",
                "  fi",
                "  temp_dir=\"$(mktemp -d)\"",
                "  if ! curl -fsSL --retry 3 --connect-timeout 10 --max-time 180 -H \"Authorization: Bearer ${LEASE_SHUTDOWN_TOKEN}\" \"$source_archive_url\" | tar -xzf - -C \"$temp_dir\"; then",
                "    rm -rf \"$temp_dir\"",
                "    return 1",
                "  fi",
                "  rm -rf \"$repo_dir\"",
                "  mkdir -p \"$repo_dir\"",
                "  cp -a \"$temp_dir/.\" \"$repo_dir/\"",
                "  rm -rf \"$temp_dir\"",
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
                "    return 0",
                "  fi",
                "  if [[ -d \"$repo_dir/.git\" ]]; then",
                "    timeout 120 \"$git_bin\" -C \"$repo_dir\" fetch --all --tags",
                "    if \"$git_bin\" -C \"$repo_dir\" show-ref --verify --quiet \"refs/remotes/origin/${git_ref}\"; then",
                "      timeout 120 \"$git_bin\" -C \"$repo_dir\" checkout -B \"$git_ref\" \"origin/${git_ref}\"",
                "    fi",
                "  else",
                "    rm -rf \"$repo_dir\"",
                "    timeout 120 \"$git_bin\" clone --depth 1 --branch \"$git_ref\" \"$repo_url\" \"$repo_dir\"",
                "  fi",
                "  return 0",
                "}",
                "if ! sync_attendee_source_archive; then",
                "  sync_attendee_repo || true",
                "fi",
                "systemctl daemon-reload",
                "systemctl enable --now attendee-runtime-agent.service",
                "systemctl restart attendee-runtime-agent.service",
            ]
        )

    def _startup_script(self, host_name: str, lease: BotRuntimeLease, zone: str, region: str) -> str:
        if os.getenv("GCP_BOT_ALLOW_RUNTIME_BOOTSTRAP", "false").strip().lower() in {"1", "true", "yes", "on"}:
            return self._bootstrap_startup_script(host_name, lease, zone=zone, region=region)

        runtime_agent_script_contents = (Path(__file__).resolve().parents[2] / "scripts/runtime_agent.py").read_text()
        runner_script_contents = (Path(__file__).resolve().parents[2] / "scripts/digitalocean/attendee-bot-runner.sh").read_text()
        return "\n".join(
            [
                "#!/bin/bash",
                "set -euo pipefail",
                "mkdir -p /etc/attendee /var/log/attendee",
                "cat >/etc/attendee/runtime-agent.env <<'EOF_AGENT_ENV'",
                runtime_agent_env_file_contents(host_name, runtime_queue_key(host_name)),
                "EOF_AGENT_ENV",
                "chmod 0644 /etc/attendee/runtime-agent.env",
                "cat >/usr/local/bin/attendee-runtime-agent <<'EOF_AGENT'",
                runtime_agent_script_contents,
                "EOF_AGENT",
                "chmod 0755 /usr/local/bin/attendee-runtime-agent",
                "cat >/usr/local/bin/attendee-bot-runner <<'EOF_RUNNER'",
                runner_script_contents,
                "EOF_RUNNER",
                "chmod 0755 /usr/local/bin/attendee-bot-runner",
                "systemctl daemon-reload",
                "systemctl enable --now attendee-runtime-agent.service",
                "systemctl restart attendee-runtime-agent.service",
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

    def _runtime_class_family(self, bot) -> str:
        return "light" if bot.runtime_resource_class() in {"transcription_only", "audio_only"} else "web"

    def _host_machine_type(self, bot) -> str:
        return bot.gcp_machine_type()

    def _slot_capacity_for(self, *, bot=None, machine_type: str | None = None, runtime_class_family: str | None = None) -> int:
        if bot is not None:
            machine_type = machine_type or self._host_machine_type(bot)
            runtime_class_family = runtime_class_family or self._runtime_class_family(bot)

        raw_machine_type_json = os.getenv("MEETBOT_GCP_VM_SLOT_CAPACITY_BY_MACHINE_TYPE_JSON", "").strip()
        if raw_machine_type_json and machine_type:
            machine_type_map = json.loads(raw_machine_type_json)
            if machine_type in machine_type_map:
                return int(machine_type_map[machine_type])

        raw_family_json = os.getenv("MEETBOT_GCP_VM_SLOT_CAPACITY_BY_RUNTIME_CLASS_FAMILY_JSON", "").strip()
        if raw_family_json and runtime_class_family:
            family_map = json.loads(raw_family_json)
            if runtime_class_family in family_map:
                return int(family_map[runtime_class_family])

        return gcp_vm_slot_capacity()

    def _host_name(self, region: str) -> str:
        return f"attendee-gcp-host-{region}-{uuid.uuid4().hex[:8]}"

    def _register_host(self, host_name: str, region: str, zone: str, *, machine_type: str, runtime_class_family: str, slot_capacity: int, state: str = "provisioning") -> None:
        redis_client().hset(
            "meetbot:scheduler:gcp:instances",
            host_name,
            json.dumps(
                {
                    "host_name": host_name,
                    "region": region,
                    "zone": zone,
                    "state": state,
                    "runtime_class_family": runtime_class_family,
                    "slot_capacity": slot_capacity,
                    "created_at": timezone.now().isoformat(),
                    "last_active_at": timezone.now().isoformat(),
                    "heartbeat_key": f"meetbot:runtime:agent:{host_name}:heartbeat",
                    "queue_key": runtime_queue_key(host_name),
                    "machine_type": machine_type,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def _update_host_record(self, host_name: str, **updates) -> None:
        client = redis_client()
        raw = client.hget("meetbot:scheduler:gcp:instances", host_name)
        current = json.loads(raw) if raw else {"host_name": host_name}
        current.update(updates)
        client.hset("meetbot:scheduler:gcp:instances", host_name, json.dumps(current, sort_keys=True, separators=(",", ":")))

    def _acquire_host_slot(self, host_name: str, bot, lease: BotRuntimeLease, *, capacity: int) -> int | None:
        for slot_index in range(capacity):
            if redis_client().set(
                f"meetbot:scheduler:slots:gcp:{host_name}:{slot_index}",
                json.dumps(
                    {
                        "bot_id": bot.id,
                        "lease_id": lease.id,
                        "host_name": host_name,
                        "slot_index": slot_index,
                        "reserved_at": timezone.now().isoformat(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                nx=True,
                ex=6 * 60 * 60,
                ):
                return slot_index
        return None

    def _host_instance_exists(self, host_name: str, zone: str | None) -> bool:
        if not zone:
            return False
        try:
            self.instances_client.get(project=self.project_id, zone=zone, instance=host_name)
            return True
        except Exception as exc:
            if NotFound and isinstance(exc, NotFound):
                return False
            logger.warning("Unable to verify GCP host %s in zone %s: %s", host_name, zone, exc)
            return False

    def _select_existing_host(self, region: str, bot, lease: BotRuntimeLease, *, machine_type: str, runtime_class_family: str, slot_capacity: int) -> tuple[str, dict, int] | None:
        client = redis_client()
        raw_instances = client.hgetall("meetbot:scheduler:gcp:instances")
        for raw_host_name, raw_meta in raw_instances.items():
            host_name = raw_host_name.decode("utf-8") if isinstance(raw_host_name, bytes) else raw_host_name
            meta = json.loads(raw_meta) if raw_meta else {}
            if meta.get("region") != region:
                continue
            if meta.get("machine_type") != machine_type:
                continue
            if meta.get("runtime_class_family") != runtime_class_family:
                continue
            if meta.get("state") not in {None, "provisioning", "active"}:
                continue
            if not self._host_instance_exists(host_name, meta.get("zone")):
                logger.info("Skipping stale GCP host record %s in region %s", host_name, region)
                client.hdel("meetbot:scheduler:gcp:instances", host_name)
                continue
            heartbeat_key = meta.get("heartbeat_key") or runtime_agent_heartbeat_key(host_name)
            next_state = "active" if client.exists(heartbeat_key) else meta.get("state", "provisioning")
            update_fields = {
                "last_active_at": timezone.now().isoformat(),
                "state": next_state,
                "heartbeat_key": heartbeat_key,
            }
            if client.exists(heartbeat_key) and not meta.get("runtime_agent_heartbeat_seen_at"):
                update_fields["runtime_agent_heartbeat_seen_at"] = timezone.now().isoformat()
            slot_index = self._acquire_host_slot(host_name, bot, lease, capacity=int(meta.get("slot_capacity") or slot_capacity))
            if slot_index is not None:
                self._update_host_record(host_name, **update_fields)
                return host_name, meta, slot_index
        return None

    def _build_instance(self, bot, host_name: str, lease: BotRuntimeLease, zone: str, region: str):
        machine_type_name = self._host_machine_type(bot)
        machine_type = f"zones/{zone}/machineTypes/{machine_type_name}"
        requested_boot_disk_size_gb = bot.gcp_boot_disk_size_gb()
        boot_disk_size_gb = max(requested_boot_disk_size_gb, self._source_image_min_disk_size_gb())
        if boot_disk_size_gb != requested_boot_disk_size_gb:
            logger.info(
                "Increasing GCP boot disk size for host %s from %s GB to %s GB to satisfy source image requirements",
                host_name,
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
            compute_v1.Items(key="startup-script", value=self._startup_script(host_name, lease, zone=zone, region=region)),
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
            name=host_name,
            machine_type=machine_type,
            disks=[boot_disk],
            network_interfaces=[network_interface],
            metadata=compute_v1.Metadata(items=metadata_items),
            tags=compute_v1.Tags(items=self._tags(None)),
            labels={**self._labels(lease.bot, lease), "runtime-provider": "gcp-compute-instance-host", "host-name": host_name},
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

    def _select_or_create_host(self, bot, lease: BotRuntimeLease) -> tuple[str, str, str, int, str, str, int, dict]:
        region = bot.runtime_region(self._default_region())
        machine_type = self._host_machine_type(bot)
        runtime_class_family = self._runtime_class_family(bot)
        slot_capacity = self._slot_capacity_for(bot=bot, machine_type=machine_type, runtime_class_family=runtime_class_family)
        existing = self._select_existing_host(
            region,
            bot,
            lease,
            machine_type=machine_type,
            runtime_class_family=runtime_class_family,
            slot_capacity=slot_capacity,
        )
        if existing is not None:
            host_name, meta, slot_index = existing
            return host_name, region, meta.get("zone") or self._zones_for_region(region)[0], slot_index, machine_type, runtime_class_family, int(meta.get("slot_capacity") or slot_capacity), {}

        zone = self._zones_for_region(region)[0]
        host_name = self._host_name(region)
        insert_started_at = timezone.now().isoformat()
        instance = self._build_instance(bot, host_name, lease, zone=zone, region=region)
        request_id = str(uuid.uuid4())
        logger.info("Provisioning GCP host %s region=%s zone=%s request_id=%s", host_name, region, zone, request_id)
        operation = self.instances_client.insert(project=self.project_id, zone=zone, instance_resource=instance)
        self._wait_for_operation(zone, operation.name)
        instance_running_at = timezone.now().isoformat()
        self._register_host(
            host_name,
            region,
            zone,
            machine_type=machine_type,
            runtime_class_family=runtime_class_family,
            slot_capacity=slot_capacity,
            state="provisioning",
        )
        slot_index = self._acquire_host_slot(host_name, bot, lease, capacity=slot_capacity)
        if slot_index is None:
            raise GCPComputeEngineError(f"Unable to reserve slot on newly created host {host_name}")
        return host_name, region, zone, slot_index, machine_type, runtime_class_family, slot_capacity, {
            "gcp_insert_started_at": insert_started_at,
            "gcp_instance_running_at": instance_running_at,
        }

    def get_or_create_lease(self, bot) -> BotRuntimeLease:
        region = bot.runtime_region(self._default_region())
        lease, _ = BotRuntimeLease.objects.get_or_create(
            bot=bot,
            defaults={
                "provider": BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
                "region": region,
                "size_class": self._host_machine_type(bot),
                "snapshot_id": self._source_image(),
            },
        )
        return lease

    def provision_bot(self, bot) -> BotRuntimeLease:
        lease = self.get_or_create_lease(bot)
        if lease.provider_instance_id and lease.status in {BotRuntimeLeaseStatuses.PROVISIONING, BotRuntimeLeaseStatuses.ACTIVE}:
            logger.info("Bot %s already has GCP lease %s (%s), skipping duplicate provision", bot.object_id, lease.id, lease.provider_instance_id)
            return lease

        host_name, region, zone, slot_index, machine_type, runtime_class_family, slot_capacity, timing_updates = self._select_or_create_host(bot, lease)
        lease.provider = BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE
        lease.status = BotRuntimeLeaseStatuses.PROVISIONING
        lease.provider_instance_id = host_name
        lease.provider_name = host_name
        lease.region = region
        lease.size_class = machine_type
        lease.snapshot_id = self._source_image()
        timings = {**((lease.metadata or {}).get("timings") or {}), "launch_requested_at": timezone.now().isoformat(), **timing_updates}
        lease.metadata = {
            "host": {
                "name": host_name,
                "zone": zone,
                "region": region,
                "machine_type": machine_type,
                "runtime_class_family": runtime_class_family,
                "slot_capacity": slot_capacity,
            },
            "slot": {
                "index": slot_index,
                "weight": 1,
            },
            "container_name": runtime_container_name(bot.id, lease.id),
            "timings": timings,
            "request": {
                "project_id": self.project_id,
                "region": region,
                "zone": zone,
                "machine_type": machine_type,
                "runtime_class_family": runtime_class_family,
                "private_only": self._private_only(),
            },
        }
        lease.last_error = None
        payload = runtime_command_payload(
            bot,
            lease,
            host_name=host_name,
            slot_index=slot_index,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
        )
        try:
            lease.save()
            redis_client().setex(
                lease_key(lease.id),
                24 * 60 * 60,
                json.dumps(
                    {
                        "bot_id": bot.id,
                        "bot_object_id": bot.object_id,
                        "lease_id": lease.id,
                        "provider": BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
                        "host_name": host_name,
                        "slot_index": slot_index,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            redis_client().setex(
                bot_key(bot.id),
                24 * 60 * 60,
                json.dumps(
                    {
                        "bot_id": bot.id,
                        "lease_id": lease.id,
                        "provider": BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
                        "host_name": host_name,
                        "slot_index": slot_index,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            redis_client().rpush(runtime_queue_key(host_name), json.dumps(payload, sort_keys=True, separators=(",", ":")))
            self._update_host_record(host_name, last_active_at=timezone.now().isoformat(), state="provisioning")
        except Exception as exc:
            redis_client().delete(f"meetbot:scheduler:slots:gcp:{host_name}:{slot_index}")
            redis_client().delete(lease_key(lease.id))
            redis_client().delete(bot_key(bot.id))
            lease.mark_failed(str(exc))
            raise
        logger.info("Provisioned GCP host %s for bot %s via lease %s slot=%s", host_name, bot.object_id, lease.id, slot_index)
        return lease

    def delete_lease(self, lease: BotRuntimeLease, missing_is_deleted: bool = True) -> BotRuntimeLease:
        if not lease.provider_instance_id:
            if missing_is_deleted:
                lease.mark_deleted()
            return lease

        host_name = lease.provider_instance_id
        slot_index = int(((lease.metadata or {}).get("slot") or {}).get("index") or 0)
        redis_client().delete(f"meetbot:scheduler:slots:gcp:{host_name}:{slot_index}")
        redis_client().delete(lease_key(lease.id))
        redis_client().delete(bot_key(lease.bot.id))
        redis_client().rpush(
            runtime_queue_key(host_name),
            json.dumps(
                {
                    "command_type": "stop",
                    "bot_id": lease.bot.id,
                    "lease_id": lease.id,
                    "host_name": host_name,
                    "slot_index": slot_index,
                    "container_name": lease.metadata.get("container_name") if isinstance(lease.metadata, dict) else None,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        lease.mark_delete_requested()
        if missing_is_deleted:
            lease.mark_deleted()
        return lease

    def fetch_lease_state(self, lease: BotRuntimeLease):
        if not lease.provider_instance_id:
            return None
        try:
            raw = redis_client().hget("meetbot:scheduler:gcp:instances", lease.provider_instance_id)
            if not raw:
                return None
            return json.loads(raw)
        except Exception:
            logger.exception("Failed to fetch GCP lease state for lease %s", lease.id)
            return None

    def sync_lease(self, lease: BotRuntimeLease) -> BotRuntimeLease:
        instance = self.fetch_lease_state(lease)
        if instance is None:
            if lease.status != BotRuntimeLeaseStatuses.DELETED:
                lease.mark_deleted()
            return lease

        lease.provider_name = instance.get("host_name") or lease.provider_name
        updated_metadata = lease.metadata or {}
        updated_metadata["host"] = instance
        timings = dict(updated_metadata.get("timings") or {})
        heartbeat_key = instance.get("heartbeat_key") or runtime_agent_heartbeat_key(lease.provider_instance_id)
        if redis_client().exists(heartbeat_key) and not timings.get("runtime_agent_heartbeat_seen_at"):
            timings["runtime_agent_heartbeat_seen_at"] = timezone.now().isoformat()
            self._update_host_record(lease.provider_instance_id, runtime_agent_heartbeat_seen_at=timings["runtime_agent_heartbeat_seen_at"])
        updated_metadata["timings"] = timings
        lease.metadata = updated_metadata
        slot_index = int(((lease.metadata or {}).get("slot") or {}).get("index") or 0)
        client = redis_client()
        client.setex(
            lease_key(lease.id),
            24 * 60 * 60,
            json.dumps(
                {
                    "bot_id": lease.bot.id,
                    "lease_id": lease.id,
                    "provider": BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
                    "host_name": lease.provider_instance_id,
                    "slot_index": slot_index,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        client.setex(
            bot_key(lease.bot.id),
            24 * 60 * 60,
            json.dumps(
                {
                    "bot_id": lease.bot.id,
                    "lease_id": lease.id,
                    "provider": BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
                    "host_name": lease.provider_instance_id,
                    "slot_index": slot_index,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        client.set(
            f"meetbot:scheduler:slots:gcp:{lease.provider_instance_id}:{slot_index}",
            json.dumps(
                {
                    "bot_id": lease.bot.id,
                    "lease_id": lease.id,
                    "host_name": lease.provider_instance_id,
                    "slot_index": slot_index,
                    "refreshed_at": timezone.now().isoformat(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            ex=6 * 60 * 60,
        )
        self._update_host_record(lease.provider_instance_id, last_active_at=timezone.now().isoformat(), state=instance.get("state", "active"))
        if lease.bot.first_heartbeat_timestamp and lease.status == BotRuntimeLeaseStatuses.PROVISIONING:
            lease.status = BotRuntimeLeaseStatuses.ACTIVE
            lease.active_at = lease.active_at or timezone.now()
        lease.save(update_fields=["provider_name", "metadata", "status", "active_at", "updated_at"])
        logger.info(
            "Synced GCP lease %s for bot %s status=%s host=%s",
            lease.id,
            lease.bot.object_id,
            lease.status,
            instance.get("host_name"),
        )
        return lease

    def delete_host_instance(self, host_name: str) -> None:
        raw = redis_client().hget("meetbot:scheduler:gcp:instances", host_name)
        if not raw:
            return
        instance = json.loads(raw)
        zone = instance.get("zone")
        if not zone:
            return
        logger.info("Deleting idle GCP host %s in zone %s", host_name, zone)
        try:
            operation = self.instances_client.delete(project=self.project_id, zone=zone, instance=host_name)
            self._wait_for_operation(zone, operation.name)
            self._wait_for_instance_absent(zone, host_name)
        except Exception as exc:
            if NotFound and isinstance(exc, NotFound):
                pass
            else:
                raise
        finally:
            redis_client().hdel("meetbot:scheduler:gcp:instances", host_name)
            capacity = int(instance.get("slot_capacity") or gcp_vm_slot_capacity())
            for slot_index in range(capacity):
                redis_client().delete(f"meetbot:scheduler:slots:gcp:{host_name}:{slot_index}")
