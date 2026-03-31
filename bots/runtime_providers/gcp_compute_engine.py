import json
import logging
import os
import shlex
import uuid

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
        "DROPLET_API_KEY",
        "GCP_APPLICATION_CREDENTIALS_JSON",
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
        self.zone_operations_client = compute_v1.ZoneOperationsClient()

    def _default_region(self) -> str:
        return os.getenv("GCP_BOT_DEFAULT_REGION") or os.getenv("GCP_BOT_REGION") or "us-central1"

    def _runtime_callback_url(self, lease: BotRuntimeLease) -> str:
        return build_site_url(reverse("bots_internal:bot-runtime-lease-complete", args=[lease.id]))

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
                "BOT_RUNTIME_PROVIDER": BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
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
        return "\n".join(
            [
                "#!/bin/bash",
                "set -euo pipefail",
                "mkdir -p /etc/attendee",
                "cat >/etc/attendee/runtime.env <<'EOF'",
                env_contents,
                "EOF",
                "chmod 0644 /etc/attendee/runtime.env",
                "systemctl daemon-reload || true",
                "systemctl enable attendee-bot-runner.service",
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

    def _build_instance(self, bot, lease: BotRuntimeLease, zone: str, region: str):
        machine_type_name = bot.gcp_machine_type()
        machine_type = f"zones/{zone}/machineTypes/{machine_type_name}"
        boot_disk = compute_v1.AttachedDisk(
            auto_delete=True,
            boot=True,
            type_=compute_v1.AttachedDisk.Type.PERSISTENT.name,
            initialize_params=compute_v1.AttachedDiskInitializeParams(
                source_image=os.getenv("GCP_BOT_SOURCE_IMAGE"),
                disk_size_gb=bot.gcp_boot_disk_size_gb(),
                disk_type=f"zones/{zone}/diskTypes/{os.getenv('GCP_BOT_DISK_TYPE', 'pd-balanced')}",
            ),
        )
        if not os.getenv("GCP_BOT_SOURCE_IMAGE"):
            raise GCPComputeEngineError("GCP_BOT_SOURCE_IMAGE is not set")

        network_interface = compute_v1.NetworkInterface()
        subnetwork = self._subnetwork_for_region(region)
        if subnetwork:
            network_interface.subnetwork = subnetwork
        elif os.getenv("GCP_BOT_NETWORK"):
            network_interface.network = os.getenv("GCP_BOT_NETWORK")

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

    def get_or_create_lease(self, bot) -> BotRuntimeLease:
        region = bot.runtime_region(self._default_region())
        lease, _ = BotRuntimeLease.objects.get_or_create(
            bot=bot,
            defaults={
                "provider": BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
                "region": region,
                "size_class": bot.gcp_machine_type(),
                "snapshot_id": os.getenv("GCP_BOT_SOURCE_IMAGE"),
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
                    request_id=request_id,
                )
                self._wait_for_operation(zone, operation.name)
                created_instance = self.instances_client.get(project=self.project_id, zone=zone, instance=bot.gcp_instance_name())
                lease.provider = BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE
                lease.status = BotRuntimeLeaseStatuses.PROVISIONING
                lease.provider_instance_id = created_instance.name
                lease.provider_name = created_instance.name
                lease.region = region
                lease.size_class = bot.gcp_machine_type()
                lease.snapshot_id = os.getenv("GCP_BOT_SOURCE_IMAGE")
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
                        "private_only": True,
                    },
                }
                lease.last_error = None
                lease.save()
                logger.info("Provisioned GCP instance %s for bot %s via lease %s", lease.provider_instance_id, bot.object_id, lease.id)
                return lease
            except Exception as exc:
                last_error = str(exc)
                logger.warning("Failed provisioning bot %s in zone %s: %s", bot.object_id, zone, exc)

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
            operation = self.instances_client.delete(project=self.project_id, zone=zone, instance=lease.provider_instance_id, request_id=request_id)
            lease.mark_delete_requested()
            self._wait_for_operation(zone, operation.name)
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
