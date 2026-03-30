import json
import logging
import os
import shlex
from typing import Iterable

import requests
from django.urls import reverse
from django.utils import timezone

from bots.bots_api_utils import build_site_url
from bots.models import BotRuntimeLease, BotRuntimeLeaseStatuses, BotRuntimeProviderTypes

logger = logging.getLogger(__name__)


class DigitalOceanAPIError(RuntimeError):
    pass


class DigitalOceanDropletProvider:
    BASE_URL = os.getenv("DO_API_BASE_URL", "https://api.digitalocean.com/v2")
    ENV_PREFIX_EXCLUDES = (
        "BOT_CONTAINER_",
        "DO_BOT_",
        "MODAL__",
        "MODAL_BOT__",
        "MODAL_EXPERIMENT__",
    )
    ENV_EXCLUDES = {
        "BOT_HOST_CODE_PATH",
        "BOT_MAX_SIMULTANEOUS_BOTS",
        "DROPLET_API_KEY",
        "PULSE_RUNTIME_PATH",
        "PULSE_SERVER",
        "XDG_RUNTIME_DIR",
    }

    def _headers(self) -> dict:
        token = os.getenv("DROPLET_API_KEY")
        if not token:
            raise DigitalOceanAPIError("DROPLET_API_KEY is not set")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, expected_statuses: Iterable[int], payload: dict | None = None) -> requests.Response:
        response = requests.request(
            method=method,
            url=f"{self.BASE_URL}{path}",
            headers=self._headers(),
            json=payload,
            timeout=30,
        )
        if response.status_code not in expected_statuses:
            raise DigitalOceanAPIError(f"DigitalOcean API {method} {path} failed with status {response.status_code}: {response.text}")
        return response

    def _ssh_keys(self) -> list[int | str]:
        raw_value = os.getenv("DO_BOT_SSH_KEY_IDS", "")
        ssh_keys: list[int | str] = []
        for raw_key in [item.strip() for item in raw_value.split(",") if item.strip()]:
            if raw_key.isdigit():
                ssh_keys.append(int(raw_key))
            else:
                ssh_keys.append(raw_key)
        return ssh_keys

    def _tags(self, bot) -> list[str]:
        base_tags = [item.strip() for item in os.getenv("DO_BOT_TAGS", "attendee-bot").split(",") if item.strip()]
        base_tags.append(f"attendee-bot-{bot.id}")
        return list(dict.fromkeys(base_tags))

    def _runtime_callback_url(self, lease: BotRuntimeLease) -> str:
        return build_site_url(reverse("bots_internal:bot-runtime-lease-complete", args=[lease.id]))

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
                "BOT_RUNTIME_PROVIDER": BotRuntimeProviderTypes.DIGITALOCEAN_DROPLET,
                "IS_DROPLET_BOT_RUNNER": "true",
                "LEASE_CALLBACK_URL": self._runtime_callback_url(lease),
                "LEASE_ID": str(lease.id),
                "LEASE_SHUTDOWN_TOKEN": lease.shutdown_token,
            }
        )
        return "\n".join(f"export {key}={shlex.quote(str(value))}" for key, value in sorted(env_vars.items()))

    def _user_data(self, bot, lease: BotRuntimeLease) -> str:
        env_contents = self._serialized_runtime_env(bot, lease).replace("\n", "\n      ")
        return (
            "#cloud-config\n"
            "bootcmd:\n"
            "  - mkdir -p /etc/attendee\n"
            "write_files:\n"
            "  - path: /etc/attendee/runtime.env\n"
            "    permissions: '0600'\n"
            "    owner: root:root\n"
            "    content: |\n"
            f"      {env_contents}\n"
            "runcmd:\n"
            "  - systemctl daemon-reload\n"
            "  - systemctl enable attendee-bot-runner.service\n"
            "  - systemctl restart attendee-bot-runner.service\n"
        )

    def get_or_create_lease(self, bot) -> BotRuntimeLease:
        lease, _ = BotRuntimeLease.objects.get_or_create(
            bot=bot,
            defaults={
                "provider": BotRuntimeProviderTypes.DIGITALOCEAN_DROPLET,
                "region": os.getenv("DO_BOT_REGION", "sgp1"),
                "size_class": os.getenv("DO_BOT_SIZE_SLUG", os.getenv("DO_BOT_SIZE_CLASS", "s-4vcpu-8gb")),
                "snapshot_id": os.getenv("DO_BOT_SNAPSHOT_ID"),
            },
        )
        return lease

    def provision_bot(self, bot) -> BotRuntimeLease:
        lease = self.get_or_create_lease(bot)
        if lease.provider_instance_id and lease.status in {BotRuntimeLeaseStatuses.PROVISIONING, BotRuntimeLeaseStatuses.ACTIVE}:
            logger.info("Bot %s already has DigitalOcean lease %s (%s), skipping duplicate provision", bot.object_id, lease.id, lease.provider_instance_id)
            return lease

        snapshot_id = os.getenv("DO_BOT_SNAPSHOT_ID")
        if not snapshot_id:
            raise DigitalOceanAPIError("DO_BOT_SNAPSHOT_ID is not set")

        payload = {
            "name": bot.digitalocean_droplet_name(),
            "region": os.getenv("DO_BOT_REGION", "sgp1"),
            "size": os.getenv("DO_BOT_SIZE_SLUG", os.getenv("DO_BOT_SIZE_CLASS", "s-4vcpu-8gb")),
            "image": snapshot_id,
            "tags": self._tags(bot),
            "user_data": self._user_data(bot, lease),
        }
        ssh_keys = self._ssh_keys()
        if ssh_keys:
            payload["ssh_keys"] = ssh_keys

        response = self._request("POST", "/droplets", expected_statuses={200, 201, 202}, payload=payload)
        response_json = response.json()
        droplet_data = response_json.get("droplet", {})
        request_metadata = {key: value for key, value in payload.items() if key != "user_data"}

        lease.provider = BotRuntimeProviderTypes.DIGITALOCEAN_DROPLET
        lease.status = BotRuntimeLeaseStatuses.PROVISIONING
        lease.provider_instance_id = str(droplet_data.get("id") or "")
        lease.provider_name = droplet_data.get("name", bot.digitalocean_droplet_name())
        lease.region = payload["region"]
        lease.size_class = payload["size"]
        lease.snapshot_id = snapshot_id
        lease.metadata = {"droplet": droplet_data, "request": request_metadata}
        lease.last_error = None
        lease.save()

        logger.info("Provisioned DigitalOcean Droplet %s for bot %s via lease %s", lease.provider_instance_id, bot.object_id, lease.id)
        return lease

    def delete_lease(self, lease: BotRuntimeLease, missing_is_deleted: bool = True) -> BotRuntimeLease:
        if not lease.provider_instance_id:
            if missing_is_deleted:
                lease.mark_deleted()
            return lease

        response = self._request("DELETE", f"/droplets/{lease.provider_instance_id}", expected_statuses={204, 404})
        if response.status_code == 404 and missing_is_deleted:
            lease.mark_deleted()
            return lease

        lease.mark_delete_requested()
        return lease

    def fetch_lease_state(self, lease: BotRuntimeLease) -> dict | None:
        if not lease.provider_instance_id:
            return None
        response = self._request("GET", f"/droplets/{lease.provider_instance_id}", expected_statuses={200, 404})
        if response.status_code == 404:
            return None
        return response.json().get("droplet")

    def sync_lease(self, lease: BotRuntimeLease) -> BotRuntimeLease:
        droplet = self.fetch_lease_state(lease)
        if droplet is None:
            if lease.status != BotRuntimeLeaseStatuses.DELETED:
                lease.mark_deleted()
            return lease

        lease.provider_name = droplet.get("name") or lease.provider_name
        lease.metadata = {"droplet": droplet}
        if lease.bot.first_heartbeat_timestamp and lease.status == BotRuntimeLeaseStatuses.PROVISIONING:
            lease.status = BotRuntimeLeaseStatuses.ACTIVE
            lease.active_at = lease.active_at or timezone.now()
        lease.save(update_fields=["provider_name", "metadata", "status", "active_at", "updated_at"])
        return lease
