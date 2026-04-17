from __future__ import annotations

import logging
import os
import json

from django.db import transaction
from django.utils import timezone

from bots.runtime_providers.host_runtime import runtime_command_payload, runtime_container_name, runtime_queue_key
from bots.models import BotRuntimeLease, BotRuntimeLeaseStatuses, BotRuntimeProviderTypes
from bots.runtime_scheduler import acquire_vps_slot, bot_key, lease_key, redis_client, release_vps_slot, update_slot

logger = logging.getLogger(__name__)


class VPSDockerRuntimeError(RuntimeError):
    pass


class VPSDockerRuntimeProvider:
    def get_or_create_lease(self, bot) -> BotRuntimeLease:
        lease, _ = BotRuntimeLease.objects.get_or_create(
            bot=bot,
            defaults={
                "provider": BotRuntimeProviderTypes.VPS_DOCKER,
                "region": os.getenv("MEETBOT_VPS_DEFAULT_REGION", "fixed"),
                "size_class": os.getenv("MEETBOT_BOT_SLOT_SIZE_CLASS", "bot-slot"),
                "snapshot_id": os.getenv("MEETBOT_VPS_SNAPSHOT_ID", "runtime-slot"),
            },
        )
        return lease

    def _queue_launch(self, bot, lease: BotRuntimeLease, host_name: str, slot_index: int):
        payload = runtime_command_payload(
            bot,
            lease,
            host_name=host_name,
            slot_index=slot_index,
            provider=BotRuntimeProviderTypes.VPS_DOCKER,
        )
        queue_key = runtime_queue_key(host_name)
        from bots.runtime_scheduler import redis_client

        client = redis_client()
        client.rpush(queue_key, json.dumps(payload, sort_keys=True, separators=(",", ":")))
        client.hset(
            "meetbot:scheduler:vps:hosts",
            host_name,
            json.dumps(
                {
                    "host_name": host_name,
                    "queue_key": queue_key,
                    "last_active_at": timezone.now().isoformat(),
                    "provider": BotRuntimeProviderTypes.VPS_DOCKER,
                    "slot_index": slot_index,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def provision_bot(self, bot) -> BotRuntimeLease:
        lease = self.get_or_create_lease(bot)
        if lease.provider_instance_id and lease.status in {BotRuntimeLeaseStatuses.PROVISIONING, BotRuntimeLeaseStatuses.ACTIVE}:
            logger.info("Bot %s already has VPS lease %s (%s), skipping duplicate provision", bot.object_id, lease.id, lease.provider_instance_id)
            return lease

        allocation = acquire_vps_slot(bot, lease)
        if allocation is None:
            raise VPSDockerRuntimeError("No VPS slots available")

        lease.provider = BotRuntimeProviderTypes.VPS_DOCKER
        lease.status = BotRuntimeLeaseStatuses.PROVISIONING
        lease.provider_instance_id = allocation.host_name
        lease.provider_name = allocation.host_name
        lease.region = os.getenv("MEETBOT_VPS_DEFAULT_REGION", "fixed")
        lease.size_class = os.getenv("MEETBOT_BOT_SLOT_SIZE_CLASS", "bot-slot")
        lease.snapshot_id = os.getenv("MEETBOT_VPS_SNAPSHOT_ID", "runtime-slot")
        lease.metadata = {
            "host_name": allocation.host_name,
            "queue_key": allocation.queue_key,
            "slot_index": allocation.slot_index,
            "slot_weight": allocation.slot_weight,
            "container_name": runtime_container_name(bot.id, lease.id),
            "provider": BotRuntimeProviderTypes.VPS_DOCKER,
        }
        lease.last_error = None
        try:
            lease.save()
            def publish_runtime_launch() -> None:
                update_slot(allocation.host_name, allocation.slot_index or 0, lease.id, bot.id, extra={"lease_status": lease.status})
                self._queue_launch(bot, lease, allocation.host_name, allocation.slot_index or 0)

            if transaction.get_connection().in_atomic_block:
                transaction.on_commit(publish_runtime_launch)
            else:
                publish_runtime_launch()
        except Exception as exc:
            release_vps_slot(lease)
            lease.mark_failed(str(exc))
            raise
        logger.info("Provisioned VPS runtime slot target=%s slot=%s for bot %s lease=%s", allocation.host_name, allocation.slot_index, bot.object_id, lease.id)
        return lease

    def delete_lease(self, lease: BotRuntimeLease, missing_is_deleted: bool = True) -> BotRuntimeLease:
        if not lease.provider_instance_id:
            if missing_is_deleted:
                lease.mark_deleted()
            return lease

        host_name = lease.provider_instance_id
        slot_index = int((lease.metadata or {}).get("slot_index") or 0)
        release_vps_slot(lease)
        from bots.runtime_scheduler import redis_client

        client = redis_client()
        client.rpush(
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

    def sync_lease(self, lease: BotRuntimeLease) -> BotRuntimeLease:
        host_name = lease.provider_instance_id
        slot_index = int((lease.metadata or {}).get("slot_index") or 0)
        if host_name:
            update_slot(host_name, slot_index, lease.id, lease.bot.id, extra={"lease_status": lease.status})
            client = redis_client()
            client.setex(
                lease_key(lease.id),
                24 * 60 * 60,
                json.dumps(
                    {
                        "bot_id": lease.bot.id,
                        "lease_id": lease.id,
                        "provider": BotRuntimeProviderTypes.VPS_DOCKER,
                        "host_name": host_name,
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
                        "provider": BotRuntimeProviderTypes.VPS_DOCKER,
                        "host_name": host_name,
                        "slot_index": slot_index,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            client.hset(
                "meetbot:scheduler:vps:hosts",
                host_name,
                json.dumps(
                    {
                        "host_name": host_name,
                        "queue_key": runtime_queue_key(host_name),
                        "last_active_at": timezone.now().isoformat(),
                        "provider": BotRuntimeProviderTypes.VPS_DOCKER,
                        "slot_index": slot_index,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        if lease.bot.first_heartbeat_timestamp and lease.status == BotRuntimeLeaseStatuses.PROVISIONING:
            lease.status = BotRuntimeLeaseStatuses.ACTIVE
            lease.active_at = lease.active_at or timezone.now()
            lease.save(update_fields=["status", "active_at", "updated_at"])
        return lease
