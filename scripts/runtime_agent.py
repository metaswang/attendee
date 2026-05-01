#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse


LOG = logging.getLogger("attendee.runtime_agent")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"{name} is required")
    return value


def _redis_cli(*args: str, input_text: str | None = None) -> str:
    redis_url = urlparse(_env("REDIS_URL"))
    if redis_url.scheme not in {"redis", "rediss"}:
        raise RuntimeError(f"Unsupported REDIS_URL scheme: {redis_url.scheme}")

    cmd = ["redis-cli", "--raw"]
    if redis_url.scheme == "rediss":
        cmd.append("--tls")
    if redis_url.hostname:
        cmd.extend(["-h", redis_url.hostname])
    if redis_url.port:
        cmd.extend(["-p", str(redis_url.port)])
    if redis_url.path and redis_url.path != "/":
        cmd.extend(["-n", redis_url.path.lstrip("/")])
    if redis_url.username:
        cmd.extend(["--user", unquote(redis_url.username)])
    if redis_url.password:
        cmd.extend(["-a", unquote(redis_url.password)])
    cmd.extend(args)

    result = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _heartbeat_key() -> str:
    host_name = _env("MEETBOT_RUNTIME_HOST_NAME")
    return f"meetbot:runtime:agent:{host_name}:heartbeat"


def _queue_key() -> str:
    host_name = _env("MEETBOT_RUNTIME_HOST_NAME")
    return os.getenv("MEETBOT_RUNTIME_QUEUE_KEY") or f"meetbot:runtime:commands:{host_name}"


def _runtime_env_base_path() -> Path:
    return Path(os.getenv("RUNTIME_ENV_PATH", "/etc/attendee/runtime.env"))


def _payload_identifier(payload: dict[str, object]) -> str:
    lease_id = payload.get("lease_id")
    if lease_id is not None:
        return f"lease-{lease_id}"
    bot_id = payload.get("bot_id")
    if bot_id is not None:
        return f"bot-{bot_id}"
    return str(int(time.time()))


def _runtime_env_path(payload: dict[str, object] | None = None) -> Path:
    base_path = _runtime_env_base_path()
    if payload is None:
        return base_path
    return base_path.with_name(f"{base_path.stem}-{_payload_identifier(payload)}{base_path.suffix}")


def _runner_script_path() -> Path:
    baked = Path("/usr/local/bin/attendee-bot-runner")
    if baked.exists():
        return baked
    repo_dir = Path(_env("ATTENDEE_REPO_DIR"))
    return repo_dir / "scripts/digitalocean/attendee-bot-runner.sh"


def _write_runtime_env(runtime_env: dict[str, str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"export {key}={shlex.quote(str(value))}" for key, value in sorted(runtime_env.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o644)


def _apply_host_bot_resource_overrides(runtime_env: dict[str, str]) -> None:
    """
    Per-VPS caps for docker: set on each host in /etc/attendee/runtime-agent.env via systemd.
    Overrides scheduler payload so the same bot model can run with different CPU/mem on myvps vs myvps2.
    """
    cpus = os.getenv("MEETBOT_RUNTIME_HOST_BOT_CPUS", "").strip()
    mem_limit = os.getenv("MEETBOT_RUNTIME_HOST_BOT_MEMORY_LIMIT", "").strip()
    mem_reservation = os.getenv("MEETBOT_RUNTIME_HOST_BOT_MEMORY_RESERVATION", "").strip()
    if cpus:
        runtime_env["BOT_CPUS"] = cpus
    if mem_limit:
        runtime_env["BOT_MEMORY_LIMIT"] = mem_limit
        if not mem_reservation:
            runtime_env["BOT_MEMORY_RESERVATION"] = mem_limit
    if mem_reservation:
        runtime_env["BOT_MEMORY_RESERVATION"] = mem_reservation


def _spawn_runner(payload: dict[str, object]) -> None:
    runtime_env = payload.get("runtime_env")
    if not isinstance(runtime_env, dict):
        raise RuntimeError("launch payload is missing runtime_env")

    merged_env = {str(key): str(value) for key, value in runtime_env.items()}
    _apply_host_bot_resource_overrides(merged_env)
    runtime_env_path = _runtime_env_path(payload)
    _write_runtime_env(merged_env, runtime_env_path)

    log_dir = Path(os.getenv("RUNNER_LOG_DIR", "/var/log/attendee"))
    container_env_path = log_dir / f"runtime-{_payload_identifier(payload)}.env"

    env = os.environ.copy()
    env.update(
        {
            "BOT_ID": str(payload["bot_id"]),
            "LEASE_ID": str(payload["lease_id"]),
            "RUNTIME_ENV_PATH": str(runtime_env_path),
            "CONTAINER_ENV_PATH": str(container_env_path),
            "ATTENDEE_REPO_DIR": _env("ATTENDEE_REPO_DIR"),
            "ATTENDEE_CONTAINER_WORKDIR": os.getenv("ATTENDEE_CONTAINER_WORKDIR", "/attendee"),
            "BOT_RUNTIME_IMAGE": _env("BOT_RUNTIME_IMAGE"),
            "BOT_RUNTIME_AGENT_HEARTBEAT_SEEN_AT": os.getenv("BOT_RUNTIME_AGENT_HEARTBEAT_SEEN_AT", _utc_now_iso()),
        }
    )

    runner = _runner_script_path()
    if not runner.exists():
        raise RuntimeError(f"runner script not found: {runner}")

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(os.getenv("RUNNER_LOG_PATH", str(log_dir / "runtime-agent.log")))
    with log_path.open("a", encoding="utf-8") as handle:
        subprocess.Popen(
            ["bash", str(runner)],
            env=env,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )


def _stop_runtime(payload: dict[str, object]) -> None:
    container_name = payload.get("container_name")
    if not container_name and payload.get("bot_id") is not None:
        container_name = f"{os.getenv('BOT_RUNTIME_CONTAINER_NAME_PREFIX', 'attendee-bot')}-lease-{payload.get('lease_id') or payload.get('bot_id')}"
    if not container_name:
        return
    subprocess.run(
        ["docker", "rm", "-f", str(container_name)],
        check=False,
        capture_output=True,
        text=True,
    )


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    queue_key = _queue_key()
    heartbeat_key = _heartbeat_key()
    heartbeat_ttl_seconds = int(os.getenv("MEETBOT_RUNTIME_AGENT_HEARTBEAT_TTL_SECONDS", "90"))
    blpop_timeout_seconds = int(os.getenv("MEETBOT_RUNTIME_AGENT_BLPOP_TIMEOUT_SECONDS", "5"))

    LOG.info("runtime agent starting queue=%s heartbeat_key=%s", queue_key, heartbeat_key)
    while True:
        try:
            os.environ.setdefault("BOT_RUNTIME_AGENT_HEARTBEAT_SEEN_AT", _utc_now_iso())
            _redis_cli("SETEX", heartbeat_key, str(heartbeat_ttl_seconds), str(int(time.time())))
        except Exception as exc:
            LOG.warning("failed to refresh heartbeat key: %s", exc)

        try:
            raw = _redis_cli("BLPOP", queue_key, str(blpop_timeout_seconds))
        except subprocess.CalledProcessError as exc:
            LOG.warning("redis BLPOP failed: %s", exc)
            time.sleep(2)
            continue

        if not raw:
            continue

        lines = [line for line in raw.splitlines() if line.strip()]
        if not lines:
            continue

        payload_raw = lines[-1]
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError as exc:
            LOG.error("invalid launch payload: %s (%s)", payload_raw, exc)
            continue

        command_type = str(payload.get("command_type") or payload.get("kind") or "launch")
        try:
            if command_type == "stop":
                _stop_runtime(payload)
            else:
                _spawn_runner(payload)
        except Exception as exc:
            LOG.exception("failed to handle command=%s payload=%s error=%s", command_type, payload, exc)


if __name__ == "__main__":
    raise SystemExit(main())
