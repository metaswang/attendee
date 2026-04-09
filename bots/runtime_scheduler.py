from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from typing import Iterable

import redis
from django.conf import settings
from django.utils import timezone

from bots.models import Bot, BotRuntimeLease, BotRuntimeLeaseStatuses, BotRuntimeProviderTypes

from .runtime_providers.host_runtime import runtime_agent_heartbeat_key

logger = logging.getLogger(__name__)


DEFAULT_VPS_TARGET_ORDER = ("myvps", "myvps3", "myvps2")
DEFAULT_VPS_CAPACITY = {"myvps": 4, "myvps2": 2, "myvps3": 4}
DEFAULT_GCP_VM_SLOT_CAPACITY = 4
DEFAULT_GCP_IDLE_SHUTDOWN_SECONDS = 300
DEFAULT_SLOT_TTL_SECONDS = 6 * 60 * 60
DEFAULT_PENDING_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class RuntimeAllocation:
    provider: str
    host_name: str
    slot_index: int | None = None
    slot_weight: int = 1
    queue_key: str | None = None
    region: str | None = None
    zone: str | None = None


class RuntimeSlotAllocationError(RuntimeError):
    pass


_redis_client = None


def redis_client():
    global _redis_client
    if _redis_client is None:
        runtime_redis_url = (
            os.getenv("BOT_RUNTIME_REDIS_URL", "").strip()
            or os.getenv("REDIS__URL", "").strip()
        )
        redis_url = runtime_redis_url or settings.REDIS_URL_WITH_PARAMS
        _redis_client = redis.from_url(redis_url)
    return _redis_client


def _json_dump(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_load(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not value:
        return None
    return json.loads(value)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def skip_vps_enabled() -> bool:
    return _env_bool("MEETBOT_SCHEDULER_SKIP_VPS", False)


def vps_target_order() -> list[str]:
    raw = os.getenv("MEETBOT_VPS_TARGET_ORDER")
    if not raw:
        return list(DEFAULT_VPS_TARGET_ORDER)
    return [item.strip() for item in raw.split(",") if item.strip()]


def vps_slot_capacity(target_name: str) -> int:
    raw_json = os.getenv("MEETBOT_VPS_SLOT_CAPACITY_JSON", "").strip()
    if raw_json:
        capacities = json.loads(raw_json)
        if target_name in capacities:
            return int(capacities[target_name])
    return int(DEFAULT_VPS_CAPACITY.get(target_name, 0))


def gcp_vm_slot_capacity() -> int:
    return int(os.getenv("MEETBOT_GCP_VM_SLOT_CAPACITY", DEFAULT_GCP_VM_SLOT_CAPACITY))


def gcp_idle_shutdown_seconds() -> int:
    return int(os.getenv("MEETBOT_GCP_IDLE_SHUTDOWN_SECONDS", DEFAULT_GCP_IDLE_SHUTDOWN_SECONDS))


def slot_ttl_seconds() -> int:
    return int(os.getenv("MEETBOT_RUNTIME_SLOT_TTL_SECONDS", DEFAULT_SLOT_TTL_SECONDS))


def pending_ttl_seconds() -> int:
    return int(os.getenv("MEETBOT_RUNTIME_PENDING_TTL_SECONDS", DEFAULT_PENDING_TTL_SECONDS))


def target_snapshot_key() -> str:
    return "meetbot:scheduler:targets"


def slot_key(target: str, slot_index: int) -> str:
    return f"meetbot:scheduler:slots:{target}:{slot_index}"


def lease_key(lease_id: int) -> str:
    return f"meetbot:scheduler:lease:{lease_id}"


def bot_key(bot_id: int) -> str:
    return f"meetbot:scheduler:bot:{bot_id}"


def pending_queue_key() -> str:
    return "meetbot:scheduler:queue:pending"


def pending_queue_meta_key() -> str:
    return "meetbot:scheduler:queue:pending:meta"


def gcp_instances_key() -> str:
    return "meetbot:scheduler:gcp:instances"


def gcp_instance_meta_key(instance_name: str) -> str:
    return f"meetbot:scheduler:gcp:instance:{instance_name}"


def write_pending_bot(bot: Bot, reason: str) -> None:
    payload = {
        "bot_id": bot.id,
        "bot_object_id": bot.object_id,
        "reason": reason,
        "queued_at": timezone.now().isoformat(),
    }
    client = redis_client()
    client.zadd(pending_queue_key(), {str(bot.id): timezone.now().timestamp()})
    client.hset(pending_queue_meta_key(), str(bot.id), _json_dump(payload))
    client.expire(pending_queue_key(), pending_ttl_seconds())
    client.expire(pending_queue_meta_key(), pending_ttl_seconds())
    client.setex(bot_key(bot.id), pending_ttl_seconds(), _json_dump(payload))


def pop_due_pending_bots(limit: int = 50) -> list[int]:
    client = redis_client()
    now = timezone.now().timestamp()
    raw_items = client.zrangebyscore(pending_queue_key(), min="-inf", max=now, start=0, num=limit)
    bot_ids: list[int] = []
    for raw_item in raw_items:
        bot_id = int(raw_item.decode("utf-8") if isinstance(raw_item, bytes) else raw_item)
        bot_ids.append(bot_id)
        client.zrem(pending_queue_key(), raw_item)
        client.hdel(pending_queue_meta_key(), str(bot_id))
    return bot_ids


def build_targets_snapshot() -> dict:
    snapshot = {
        "skip_vps": skip_vps_enabled(),
        "vps_target_order": vps_target_order(),
        "vps_targets": [],
        "gcp": {
            "vm_slot_capacity": gcp_vm_slot_capacity(),
            "idle_shutdown_seconds": gcp_idle_shutdown_seconds(),
            "instances": [],
        },
    }
    for target in vps_target_order():
        capacity = vps_slot_capacity(target)
        if capacity <= 0:
            continue
        occupied = 0
        client = redis_client()
        for slot_index in range(capacity):
            if client.exists(slot_key(target, slot_index)):
                occupied += 1
        snapshot["vps_targets"].append(
            {
                "name": target,
                "capacity": capacity,
                "occupied": occupied,
                "available": max(0, capacity - occupied),
                "queue_key": f"meetbot:runtime:commands:{target}",
            }
        )

    client = redis_client()
    raw_instances = client.hgetall(gcp_instances_key())
    for instance_name_raw, raw_meta in raw_instances.items():
        instance_name = instance_name_raw.decode("utf-8") if isinstance(instance_name_raw, bytes) else instance_name_raw
        meta = _json_load(raw_meta) or {}
        capacity = int(meta.get("slot_capacity") or gcp_vm_slot_capacity())
        occupied = 0
        for slot_index in range(capacity):
            if client.exists(slot_key(f"gcp:{instance_name}", slot_index)):
                occupied += 1
        snapshot["gcp"]["instances"].append(
            {
                "name": instance_name,
                "region": meta.get("region"),
                "zone": meta.get("zone"),
                "capacity": capacity,
                "occupied": occupied,
                "available": max(0, capacity - occupied),
                "last_active_at": meta.get("last_active_at"),
                "created_at": meta.get("created_at"),
                "heartbeat_key": runtime_agent_heartbeat_key(instance_name),
            }
        )
    return snapshot


def persist_targets_snapshot() -> dict:
    snapshot = build_targets_snapshot()
    redis_client().set(target_snapshot_key(), _json_dump(snapshot), ex=60)
    return snapshot


def _reserve_slot(target: str, slot_index: int, lease_id: int, bot_id: int, ttl_seconds: int | None = None) -> bool:
    client = redis_client()
    payload = {
        "bot_id": bot_id,
        "lease_id": lease_id,
        "target": target,
        "slot_index": slot_index,
        "reserved_at": timezone.now().isoformat(),
    }
    return bool(client.set(slot_key(target, slot_index), _json_dump(payload), nx=True, ex=ttl_seconds or slot_ttl_seconds()))


def update_slot(target: str, slot_index: int, lease_id: int | None, bot_id: int | None, *, ttl_seconds: int | None = None, extra: dict | None = None) -> None:
    client = redis_client()
    payload = {
        "bot_id": bot_id,
        "lease_id": lease_id,
        "target": target,
        "slot_index": slot_index,
        "updated_at": timezone.now().isoformat(),
    }
    if extra:
        payload.update(extra)
    client.set(slot_key(target, slot_index), _json_dump(payload), ex=ttl_seconds or slot_ttl_seconds())


def acquire_vps_slot(bot: Bot, lease: BotRuntimeLease) -> RuntimeAllocation | None:
    if skip_vps_enabled():
        return None

    client = redis_client()
    for target in vps_target_order():
        capacity = vps_slot_capacity(target)
        if capacity <= 0:
            continue
        for slot_index in range(capacity):
            if _reserve_slot(target, slot_index, lease.id, bot.id):
                allocation = RuntimeAllocation(
                    provider=BotRuntimeProviderTypes.VPS_DOCKER,
                    host_name=target,
                    slot_index=slot_index,
                    queue_key=f"meetbot:runtime:commands:{target}",
                )
                client.setex(lease_key(lease.id), pending_ttl_seconds(), _json_dump({
                    "bot_id": bot.id,
                    "bot_object_id": bot.object_id,
                    "lease_id": lease.id,
                    "provider": allocation.provider,
                    "host_name": target,
                    "slot_index": slot_index,
                }))
                client.setex(bot_key(bot.id), pending_ttl_seconds(), _json_dump({
                    "bot_id": bot.id,
                    "lease_id": lease.id,
                    "provider": allocation.provider,
                    "host_name": target,
                    "slot_index": slot_index,
                }))
                return allocation
    return None


def release_vps_slot(lease: BotRuntimeLease) -> None:
    allocation = lease.metadata or {}
    host_name = allocation.get("host_name") or lease.provider_instance_id
    slot_index = allocation.get("slot_index")
    if host_name is None or slot_index is None:
        return
    client = redis_client()
    key = slot_key(str(host_name), int(slot_index))
    current = _json_load(client.get(key))
    if current and int(current.get("lease_id") or 0) not in {0, lease.id}:
        return
    client.delete(key)
    client.delete(lease_key(lease.id))
    client.delete(bot_key(lease.bot.id))


def acquire_lease_lock(bot_id: int, ttl_seconds: int = 300) -> bool:
    return bool(redis_client().set(f"meetbot:scheduler:launch-lock:{bot_id}", "1", nx=True, ex=ttl_seconds))


def release_lease_lock(bot_id: int) -> None:
    redis_client().delete(f"meetbot:scheduler:launch-lock:{bot_id}")


def runtime_capacity_summary() -> list[dict]:
    snapshot = persist_targets_snapshot()
    payload: list[dict] = []
    payload.extend(
        {
            "provider": "vps_docker",
            "region": "fixed",
            "target": target["name"],
            "quota_limit": target["capacity"],
            "quota_usage": target["occupied"],
            "soft_cap": target["capacity"],
            "effective_available": target["available"],
            "metadata": target,
            "last_synced_at": timezone.now().isoformat(),
        }
        for target in snapshot["vps_targets"]
    )
    payload.extend(
        {
            "provider": "gcp_compute_instance",
            "region": instance["region"] or "unknown",
            "target": instance["name"],
            "quota_limit": instance["capacity"],
            "quota_usage": instance["occupied"],
            "soft_cap": instance["capacity"],
            "effective_available": instance["available"],
            "metadata": instance,
            "last_synced_at": timezone.now().isoformat(),
        }
        for instance in snapshot["gcp"]["instances"]
    )
    return payload


def idle_gcp_hosts() -> list[dict]:
    snapshot = persist_targets_snapshot()
    idle_hosts: list[dict] = []
    threshold = gcp_idle_shutdown_seconds()
    now = timezone.now()
    for instance in snapshot["gcp"]["instances"]:
        if instance["occupied"] > 0:
            continue
        last_active_at_raw = instance.get("last_active_at")
        if not last_active_at_raw:
            continue
        try:
            last_active_at = datetime.fromisoformat(last_active_at_raw)
            if last_active_at.tzinfo is None:
                last_active_at = last_active_at.replace(tzinfo=datetime_timezone.utc)
        except Exception:
            continue
        if (now - last_active_at).total_seconds() >= threshold and instance.get("available", 0) == instance.get("capacity", 0):
            idle_hosts.append(instance)
    return idle_hosts
