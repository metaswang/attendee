# First DigitalOcean Snapshot Runbook

This is the command-by-command sequence for building the first bot snapshot and wiring `myvps`.

## 1. Prepare the `myvps` env file

```bash
cd /path/to/attendee
cp scripts/digitalocean/myvps.env.example scripts/digitalocean/myvps.env
vim scripts/digitalocean/myvps.env
```

Minimum fields to fill before the first run:

- `SITE_DOMAIN`
- `DJANGO_SECRET_KEY`
- `CREDENTIALS_ENCRYPTION_KEY`
- `POSTGRES_*`
- `REDIS_URL`
- `AWS_*`
- `DROPLET_API_KEY`
- `DO_BOT_SSH_KEY_IDS`
- `ATTENDEE_REPO_URL`
- `ATTENDEE_GIT_REF`

## 2. Create the template droplet from `myvps`

Requires `doctl` already installed and authenticated through `DROPLET_API_KEY`.

```bash
cd /path/to/attendee
bash scripts/digitalocean/create-template-droplet.sh scripts/digitalocean/myvps.env
```

The script prints the template Droplet IP.

## 3. Prepare the template droplet over SSH

Replace `<template-ip>` with the IP printed in step 2:

```bash
ssh root@<template-ip>
exit
```

Then from `myvps` run:

```bash
cd /path/to/attendee
ATTENDEE_REPO_URL="$(grep '^ATTENDEE_REPO_URL=' scripts/digitalocean/myvps.env | cut -d= -f2-)" \
ATTENDEE_GIT_REF="$(grep '^ATTENDEE_GIT_REF=' scripts/digitalocean/myvps.env | cut -d= -f2-)" \
BOT_RUNTIME_IMAGE="$(grep '^BOT_RUNTIME_IMAGE=' scripts/digitalocean/myvps.env | cut -d= -f2-)" \
ssh root@<template-ip> 'bash -s' < scripts/digitalocean/prepare-template-droplet.sh
```

What this does:

- installs Docker
- pulls or updates the repo
- builds the bot runtime image
- installs `attendee-bot-runner` and its `systemd` service
- cleans `cloud-init` and machine identity so the snapshot can be reused

## 4. Create the first snapshot from `myvps`

Use the template droplet name from your env file, or the droplet ID:

```bash
cd /path/to/attendee
bash scripts/digitalocean/create-snapshot.sh scripts/digitalocean/myvps.env attendee-bot-template
```

The script prints the new snapshot ID.

## 5. Promote the snapshot on `myvps`

Edit `scripts/digitalocean/myvps.env` and set:

```bash
DO_BOT_SNAPSHOT_ID=<snapshot-id-from-step-4>
LAUNCH_BOT_METHOD=digitalocean-droplet
```

Then copy the same values into your real production `.env` on `myvps`.

## 6. Restart the control plane

Use the restart command that matches how `myvps` runs Attendee. Example for systemd-managed containers or services:

```bash
systemctl restart attendee-app
systemctl restart attendee-worker
systemctl restart attendee-scheduler
```

If you run with Docker Compose instead:

```bash
docker compose up -d --force-recreate attendee-app attendee-worker attendee-scheduler
```

## 7. Smoke test

Create one Google Meet bot and confirm:

- a `BotRuntimeLease` row appears in Django admin
- one DO Droplet is created from the snapshot
- the bot joins the meeting
- after the bot exits, the Droplet is deleted automatically

## 8. Snapshot refresh flow

For every future release:

1. Re-run `prepare-template-droplet.sh` on the template droplet with the new git ref.
2. Re-run `create-snapshot.sh`.
3. Update `DO_BOT_SNAPSHOT_ID` on `myvps`.
4. Restart app, worker, and scheduler.
