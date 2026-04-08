# DigitalOcean Droplet Bot Runtime

This document describes the snapshot-based DigitalOcean runtime added for Attendee bots.

## Deployment shape

- `myvps` runs the control plane: Django app, Celery worker, scheduler, and reverse proxy.
- Postgres and Redis stay on managed TLS endpoints.
- Every meeting bot gets exactly one DigitalOcean Droplet.
- The Droplet is created from a prebuilt snapshot and deleted after the bot process exits.

## Required environment variables on `myvps`

```bash
LAUNCH_BOT_METHOD=digitalocean-droplet
DROPLET_API_KEY=...
DO_BOT_REGION=sgp1
DO_BOT_SIZE_SLUG=s-4vcpu-8gb
DO_BOT_SNAPSHOT_ID=123456789
DO_BOT_SSH_KEY_IDS=12345,67890
DO_BOT_TAGS=attendee-bot,env-prod
SAVE_BOT_RESOURCE_SNAPSHOTS=true
```

Notes:

- `DROPLET_API_KEY` should exist only on `myvps`.
- The spawned bot Droplet does not need the DigitalOcean API token.
- `DO_BOT_SIZE_SLUG` is the actual DigitalOcean Droplet size slug used by the provider.

## What the provider does

When a bot is launched with `LAUNCH_BOT_METHOD=digitalocean-droplet`, Attendee now:

1. Creates a `BotRuntimeLease` row.
2. Calls the DigitalOcean Droplet API with a snapshot image ID.
3. Injects cloud-init `user_data` that writes `/etc/attendee/runtime.env`.
4. Starts `attendee-bot-runner.service` on the spawned Droplet.
5. Deletes the Droplet from the control plane once the runner posts back to `/internal/bot-runtime-leases/<lease_id>/complete`.

## Preparing the snapshot image

On the template Droplet:

1. Install Docker.
2. Clone this repo to `/opt/attendee`.
3. Build the runtime image with `docker build -t "$BOT_RUNTIME_IMAGE" .`.
4. Copy [attendee-bot-runner.sh](../scripts/digitalocean/attendee-bot-runner.sh) to `/usr/local/bin/attendee-bot-runner` and mark it executable.
5. Copy [attendee-bot-runner.service](../scripts/digitalocean/attendee-bot-runner.service) to `/etc/systemd/system/attendee-bot-runner.service`.
6. Ensure the template Droplet can reach your managed Postgres, Redis, and object storage endpoints.
7. Run `cloud-init clean --logs` before taking the snapshot.
8. Create a snapshot and set its image ID as `DO_BOT_SNAPSHOT_ID` on `myvps`.

## Runtime reconciliation

- The completion callback deletes the Droplet immediately after the bot exits.
- The scheduler also reconciles `BotRuntimeLease` rows every cycle.
- Heartbeat timeout cleanup now deletes the related DigitalOcean Droplet if one exists.
- Failed-launch recovery now checks the lease and re-launches the bot if no Droplet is actually alive.

## Current limitations

- The project now includes the control-plane logic, lease model, cleanup integration, and operator scripts.
- You still need to build the initial template Droplet and snapshot manually.
- End-to-end validation should be run in a Python 3.11 environment with Django dependencies installed.
- A command-by-command first-run procedure is documented in [digitalocean_first_snapshot_runbook.md](./digitalocean_first_snapshot_runbook.md).
