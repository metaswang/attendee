from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from bots.bots_api_utils import build_site_url
from bots.models import BotRuntimeLease, BotRuntimeProviderTypes


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _runtime_api_base_url() -> str:
    return os.getenv("MEETBOT_RUNTIME_API_BASE_URL", "").strip().rstrip("/")


def _runtime_facade_or_attendee_url(lease: BotRuntimeLease, attendee_route_name: str, facade_suffix: str) -> str:
    api_base_url = _runtime_api_base_url()
    if api_base_url:
        return f"{api_base_url}/internal/attendee-runtime-leases/{lease.id}/{facade_suffix.lstrip('/')}"
    return build_site_url(reverse(attendee_route_name, args=[lease.id]))


def runtime_queue_key(host_name: str) -> str:
    return os.getenv("MEETBOT_RUNTIME_QUEUE_KEY_TEMPLATE", "meetbot:runtime:commands:{host_name}").format(host_name=host_name)


def runtime_agent_heartbeat_key(host_name: str) -> str:
    return f"meetbot:runtime:agent:{host_name}:heartbeat"


def runtime_container_name(bot_id: int | str, lease_id: int | str | None = None) -> str:
    prefix = os.getenv("BOT_RUNTIME_CONTAINER_NAME_PREFIX", "attendee-bot")
    identifier = f"lease-{lease_id}" if lease_id is not None else f"bot-{bot_id}"
    return f"{prefix}-{identifier}"


def _runtime_callback_url(lease: BotRuntimeLease) -> str:
    return _runtime_facade_or_attendee_url(lease, "bots_internal:bot-runtime-lease-complete", "complete")


def _runtime_bootstrap_url(lease: BotRuntimeLease) -> str:
    return _runtime_facade_or_attendee_url(lease, "bots_internal:bot-runtime-lease-bootstrap", "bootstrap")


def _runtime_control_url(lease: BotRuntimeLease) -> str:
    return _runtime_facade_or_attendee_url(lease, "bots_internal:bot-runtime-lease-control", "control")


def _runtime_source_archive_url(lease: BotRuntimeLease) -> str:
    return _runtime_facade_or_attendee_url(lease, "bots_internal:bot-runtime-lease-source-archive", "source-archive")


def runtime_container_env(bot, lease: BotRuntimeLease, *, host_name: str, slot_index: int, slot_weight: int = 1, provider: str) -> dict[str, str]:
    env_vars = {}
    excluded_prefixes = (
        "BOT_CONTAINER_",
        "DO_BOT_",
        "GCP_BOT_",
        "MODAL__",
        "MODAL_BOT__",
        "MODAL_EXPERIMENT__",
    )
    excluded_keys = {
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
    for key, value in os.environ.items():
        if key in excluded_keys or any(key.startswith(prefix) for prefix in excluded_prefixes):
            continue
        env_vars[key] = value

    timing_metadata = ((lease.metadata or {}).get("timings") or {}).copy()
    env_vars.update(
        {
            "BOT_ID": str(bot.id),
            "BOT_OBJECT_ID": bot.object_id,
            "BOT_LAUNCH_REQUESTED_AT": timing_metadata.get("launch_requested_at") or timezone.now().isoformat(),
            "BOT_RUNTIME_PROVIDER": provider,
            "BOT_RUNTIME_BOOTSTRAP_URL": _runtime_bootstrap_url(lease),
            "BOT_RUNTIME_CONTROL_URL": _runtime_control_url(lease),
            "BOT_RUNTIME_HOST_NAME": host_name,
            "BOT_RUNTIME_SLOT_INDEX": str(slot_index),
            "BOT_RUNTIME_SLOT_WEIGHT": str(slot_weight),
            "BOT_RUNTIME_PROVIDER_INSTANCE_ID": host_name,
            "DJANGO_SETTINGS_MODULE": "attendee.settings.bot_runtime",
            "LEASE_CALLBACK_URL": _runtime_callback_url(lease),
            "LEASE_ID": str(lease.id),
            "LEASE_SHUTDOWN_TOKEN": lease.shutdown_token,
            "MEETBOT_RUNTIME_HOST_NAME": host_name,
            "MEETBOT_RUNTIME_SLOT_INDEX": str(slot_index),
            "MEETBOT_RUNTIME_SLOT_WEIGHT": str(slot_weight),
        }
    )
    if os.getenv("BOT_RUNTIME_ALLOW_BOOTSTRAP", "false").strip().lower() in {"1", "true", "yes", "on"}:
        env_vars["BOT_RUNTIME_SOURCE_ARCHIVE_URL"] = _runtime_source_archive_url(lease)
    for env_name, metadata_key in (
        ("BOT_RUNTIME_GCP_INSERT_STARTED_AT", "gcp_insert_started_at"),
        ("BOT_RUNTIME_GCP_INSTANCE_RUNNING_AT", "gcp_instance_running_at"),
        ("BOT_RUNTIME_AGENT_HEARTBEAT_SEEN_AT", "runtime_agent_heartbeat_seen_at"),
    ):
        if timing_metadata.get(metadata_key):
            env_vars[env_name] = str(timing_metadata[metadata_key])
    runtime_redis_url = (
        os.getenv("BOT_RUNTIME_REDIS_URL", "").strip()
        or os.getenv("REDIS__URL", "").strip()
        or os.getenv("REDIS_URL", "").strip()
        or getattr(settings, "REDIS_URL_WITH_PARAMS", "")
    )
    if runtime_redis_url:
        env_vars["REDIS_URL"] = runtime_redis_url
    # Preserve per-bot sizing so the runtime runner does not fall back to tiny slot defaults.
    env_vars["BOT_MEMORY_LIMIT"] = str(bot.memory_limit())
    env_vars["BOT_MEMORY_RESERVATION"] = str(bot.memory_request())
    env_vars["BOT_CPUS"] = str(bot.cpu_request())
    return env_vars


def serialize_runtime_env(runtime_env: dict[str, str]) -> str:
    return "\n".join(f"{key}={shlex.quote(str(value))}" for key, value in sorted(runtime_env.items()))


def runtime_command_payload(bot, lease: BotRuntimeLease, *, host_name: str, slot_index: int, slot_weight: int = 1, provider: str) -> dict:
    container_name = runtime_container_name(bot.id, lease.id)
    return {
        "command_type": "launch",
        "bot_id": bot.id,
        "bot_object_id": bot.object_id,
        "lease_id": lease.id,
        "provider": provider,
        "host_name": host_name,
        "slot_index": slot_index,
        "slot_weight": slot_weight,
        "container_name": container_name,
        "runtime_env": runtime_container_env(bot, lease, host_name=host_name, slot_index=slot_index, slot_weight=slot_weight, provider=provider),
    }


def runtime_stop_payload(bot, lease: BotRuntimeLease, *, host_name: str, slot_index: int) -> dict:
    return {
        "command_type": "stop",
        "bot_id": bot.id,
        "bot_object_id": bot.object_id,
        "lease_id": lease.id,
        "host_name": host_name,
        "slot_index": slot_index,
        "container_name": runtime_container_name(bot.id, lease.id),
    }


def runtime_agent_env(host_name: str, queue_key: str) -> dict[str, str]:
    runtime_redis_url = (
        os.getenv("BOT_RUNTIME_REDIS_URL", "").strip()
        or os.getenv("REDIS__URL", "").strip()
        or os.getenv("REDIS_URL", "").strip()
        or getattr(settings, "REDIS_URL_WITH_PARAMS", "")
    )
    env = {
        "ATTENDEE_REPO_DIR": os.getenv("ATTENDEE_REPO_DIR", "/voxella/voxella-attendee"),
        "ATTENDEE_CONTAINER_WORKDIR": os.getenv("ATTENDEE_CONTAINER_WORKDIR", "/attendee"),
        "BOT_RUNTIME_IMAGE": os.getenv("BOT_RUNTIME_IMAGE", "attendee-bot-runner:latest"),
        "LOG_LEVEL": os.getenv("MEETBOT_RUNTIME_AGENT_LOG_LEVEL", "INFO"),
        "MEETBOT_RUNTIME_HOST_NAME": host_name,
        "MEETBOT_RUNTIME_QUEUE_KEY": queue_key,
        "RUNTIME_ENV_PATH": os.getenv("RUNTIME_ENV_PATH", "/etc/attendee/runtime.env"),
        "RUNNER_LOG_DIR": os.getenv("RUNNER_LOG_DIR", "/var/log/attendee"),
        "RUNNER_LOG_PATH": os.getenv("RUNNER_LOG_PATH", "/var/log/attendee/runner.log"),
    }
    if runtime_redis_url:
        env["REDIS_URL"] = runtime_redis_url
    return env


def runtime_agent_env_file_contents(host_name: str, queue_key: str) -> str:
    return serialize_runtime_env(runtime_agent_env(host_name, queue_key))


def repo_path() -> Path:
    return _REPO_ROOT
