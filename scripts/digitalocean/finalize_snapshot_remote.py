#!/usr/bin/env python3

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def run(cmd: list[str], *, env: dict[str, str] | None = None, check: bool = True, capture: bool = False):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, env=env, check=check, text=True, capture_output=capture)


def doctl_json(args: list[str], do_env: dict[str, str]):
    cp = subprocess.run(["doctl", *args, "--output", "json"], env=do_env, check=True, text=True, capture_output=True)
    return json.loads(cp.stdout)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: finalize_snapshot_remote.py /path/to/myvps.env", file=sys.stderr)
        return 1

    env = load_env(sys.argv[1])
    template_name = (env.get("DO_TEMPLATE_DROPLET_NAME") or "").strip()
    if not template_name:
        print("DO_TEMPLATE_DROPLET_NAME is required in env file", file=sys.stderr)
        return 1
    do_env = os.environ.copy()
    do_env["DIGITALOCEAN_ACCESS_TOKEN"] = env["DROPLET_API_KEY"]

    droplets = doctl_json(["compute", "droplet", "list"], do_env)
    candidates = [d for d in droplets if d["name"] == template_name]
    if not candidates:
        print(f"no droplets named {template_name!r} found", file=sys.stderr)
        return 1

    target = max(candidates, key=lambda d: d["id"])
    extras = [d for d in candidates if d["id"] != target["id"]]
    for droplet in extras:
        run(["doctl", "compute", "droplet", "delete", str(droplet["id"]), "--force"], env=do_env)

    target_ip = next(n["ip_address"] for n in target["networks"]["v4"] if n["type"] == "public")
    target_id = str(target["id"])
    print(f"TARGET_ID={target_id}")
    print(f"TARGET_IP={target_ip}")

    for _ in range(30):
        cp = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=no", f"root@{target_ip}", "true"])
        if cp.returncode == 0:
            break
        time.sleep(5)
    else:
        print("ssh to target droplet failed", file=sys.stderr)
        return 1

    repo_root = Path(__file__).resolve().parents[2]
    runner_src = repo_root / "scripts/digitalocean/attendee-bot-runner.sh"
    service_src = repo_root / "scripts/digitalocean/attendee-bot-runner.service"
    if not runner_src.is_file() or not service_src.is_file():
        print(f"runner assets not found under {repo_root}", file=sys.stderr)
        return 1
    run(
        [
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            str(runner_src),
            f"root@{target_ip}:/usr/local/bin/attendee-bot-runner",
        ]
    )
    run(
        [
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            str(service_src),
            f"root@{target_ip}:/etc/systemd/system/attendee-bot-runner.service",
        ]
    )
    remote_cmd = (
        "chmod 0755 /usr/local/bin/attendee-bot-runner && "
        "chmod 0644 /etc/systemd/system/attendee-bot-runner.service && "
        "systemctl daemon-reload && "
        "systemctl disable attendee-bot-runner.service >/dev/null 2>&1 || true && "
        "cloud-init clean --logs || true && "
        "truncate -s 0 /etc/machine-id || true && "
        "rm -f /var/lib/dbus/machine-id || true && "
        "sync && poweroff"
    )
    run(["ssh", "-o", "StrictHostKeyChecking=no", f"root@{target_ip}", "bash", "-lc", remote_cmd], check=False)

    for _ in range(36):
        droplet = doctl_json(["compute", "droplet", "get", target_id], do_env)[0]
        status = droplet["status"]
        print(f"STATUS={status}", flush=True)
        if status == "off":
            break
        time.sleep(5)
    else:
        print("droplet did not power off in time", file=sys.stderr)
        return 1

    snapshot_name = "attendee-bot-snapshot-" + time.strftime("%Y%m%d%H%M%S")
    run(
        ["doctl", "compute", "droplet-action", "snapshot", target_id, "--snapshot-name", snapshot_name, "--wait"],
        env=do_env,
    )
    snapshots = doctl_json(["compute", "snapshot", "list", "--resource", "droplet"], do_env)
    matched = [s for s in snapshots if s["name"] == snapshot_name]
    if not matched:
        print("snapshot not found after creation", file=sys.stderr)
        return 1
    snapshot = max(matched, key=lambda s: s["id"])
    print(f"SNAPSHOT_ID={snapshot['id']}")
    print(f"SNAPSHOT_NAME={snapshot_name}")

    run(["doctl", "compute", "droplet", "delete", target_id, "--force"], env=do_env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
