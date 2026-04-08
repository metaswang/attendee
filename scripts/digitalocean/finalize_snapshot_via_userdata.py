#!/usr/bin/env python3

import base64
import json
import os
import subprocess
import sys
import tempfile
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


def run(cmd: list[str], *, env: dict[str, str], check: bool = True, capture: bool = False):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, env=env, check=check, text=True, capture_output=capture)


def doctl_json(args: list[str], do_env: dict[str, str]):
    cp = subprocess.run(["doctl", *args, "--output", "json"], env=do_env, check=True, text=True, capture_output=True)
    return json.loads(cp.stdout)


def render_userdata(runner_path: Path, service_path: Path) -> str:
    runner_b64 = base64.b64encode(runner_path.read_bytes()).decode("ascii")
    service_b64 = base64.b64encode(service_path.read_bytes()).decode("ascii")
    return f"""#cloud-config
write_files:
  - path: /usr/local/bin/attendee-bot-runner
    permissions: '0755'
    encoding: b64
    content: {runner_b64}
  - path: /etc/systemd/system/attendee-bot-runner.service
    permissions: '0644'
    encoding: b64
    content: {service_b64}
runcmd:
  - [ bash, -lc, "systemctl daemon-reload" ]
  - [ bash, -lc, "systemctl disable attendee-bot-runner.service >/dev/null 2>&1 || true" ]
  - [ bash, -lc, "cloud-init clean --logs || true" ]
  - [ bash, -lc, "truncate -s 0 /etc/machine-id || true" ]
  - [ bash, -lc, "rm -f /var/lib/dbus/machine-id || true" ]
  - [ bash, -lc, "sync" ]
  - [ bash, -lc, "poweroff" ]
"""


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: finalize_snapshot_via_userdata.py /path/to/myvps.env /path/to/runner.sh /path/to/runner.service", file=sys.stderr)
        return 1

    env_file, runner_file, service_file = sys.argv[1:]
    env = load_env(env_file)
    template_name = (env.get("DO_TEMPLATE_DROPLET_NAME") or "").strip()
    if not template_name:
        print("DO_TEMPLATE_DROPLET_NAME is required in env file", file=sys.stderr)
        return 1
    do_env = os.environ.copy()
    do_env["DIGITALOCEAN_ACCESS_TOKEN"] = env["DROPLET_API_KEY"]

    for droplet in doctl_json(["compute", "droplet", "list"], do_env):
        if droplet["name"] == template_name:
            run(["doctl", "compute", "droplet", "delete", str(droplet["id"]), "--force"], env=do_env)

    user_data = render_userdata(Path(runner_file), Path(service_file))
    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write(user_data)
        user_data_path = tmp.name

    try:
        run(
            [
                "doctl",
                "compute",
                "droplet",
                "create",
                template_name,
                "--size",
                env["DO_TEMPLATE_SIZE_SLUG"],
                "--image",
                env["DO_BOT_SNAPSHOT_ID"],
                "--region",
                env["DO_BOT_REGION"],
                "--ssh-keys",
                env["DO_BOT_SSH_KEY_IDS"],
                "--tag-names",
                env.get("DO_TEMPLATE_TAGS", "attendee-template,env-prod"),
                "--user-data-file",
                user_data_path,
                "--wait",
            ],
            env=do_env,
        )

        droplets = doctl_json(["compute", "droplet", "list"], do_env)
        target = max((d for d in droplets if d["name"] == template_name), key=lambda d: d["id"])
        target_id = str(target["id"])
        print(f"TARGET_ID={target_id}")

        for _ in range(48):
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
        snapshot = max((s for s in snapshots if s["name"] == snapshot_name), key=lambda s: s["id"])
        print(f"SNAPSHOT_ID={snapshot['id']}")
        print(f"SNAPSHOT_NAME={snapshot_name}")

        run(["doctl", "compute", "droplet", "delete", target_id, "--force"], env=do_env)
        return 0
    finally:
        try:
            os.unlink(user_data_path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
