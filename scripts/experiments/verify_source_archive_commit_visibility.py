#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from queue import Queue

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "attendee.settings.development")

import django

django.setup()

from django.db import close_old_connections, transaction
from django.test import Client
from django.urls import reverse

from accounts.models import Organization
from bots.models import Bot, BotRuntimeLease, BotRuntimeLeaseStatuses, BotRuntimeProviderTypes, Project


@dataclass(slots=True)
class ProbeResult:
    mode: str
    lease_id: int
    response_status: int
    response_content_type: str
    response_size: int
    published_at: float
    committed_at: float
    request_started_at: float
    response_received_at: float


def _create_probe_bot(label: str) -> tuple[Organization, Project, Bot]:
    org = Organization.objects.create(name=f"Experiment Org {label}")
    project = Project.objects.create(name=f"Experiment Project {label}", organization=org)
    bot = Bot.objects.create(
        project=project,
        name=f"Experiment Bot {label}",
        meeting_url="https://meet.google.com/abc-defg-hij",
    )
    return org, project, bot


def _request_source_archive(lease_id: int, shutdown_token: str, outbox: Queue[tuple[int, str, int, float]]) -> None:
    close_old_connections()
    client = Client()
    started_at = time.perf_counter()
    response = client.get(
        reverse("bots_internal:bot-runtime-lease-source-archive", args=[lease_id]),
        HTTP_HOST="localhost",
        HTTP_AUTHORIZATION=f"Bearer {shutdown_token}",
    )
    outbox.put(
        (
            response.status_code,
            response.headers.get("Content-Type", ""),
            len(response.content),
            started_at,
        )
    )


def _run_case(mode: str) -> ProbeResult:
    label = f"{mode}-{uuid.uuid4().hex[:8]}"
    org, project, bot = _create_probe_bot(label)
    result_queue: Queue[tuple[int, str, int, float]] = Queue(maxsize=1)
    publish_event = threading.Event()

    try:
        lease = None
        published_at_holder: dict[str, float] = {}
        with transaction.atomic():
            lease = BotRuntimeLease.objects.create(
                bot=bot,
                provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
                status=BotRuntimeLeaseStatuses.PROVISIONING,
            )

            def worker_target() -> None:
                publish_event.wait()
                _request_source_archive(lease.id, lease.shutdown_token, result_queue)

            worker = threading.Thread(target=worker_target, daemon=True)
            worker.start()

            if mode == "immediate":
                published_at_holder["published_at"] = time.perf_counter()
                publish_event.set()
                response_status, response_content_type, response_size, request_started_at = result_queue.get(timeout=10)
            elif mode == "on_commit":
                def publish_after_commit() -> None:
                    published_at_holder["published_at"] = time.perf_counter()
                    publish_event.set()

                transaction.on_commit(publish_after_commit)
            else:
                raise ValueError(f"Unknown mode: {mode}")

        committed_at = time.perf_counter()
        if mode == "on_commit":
            response_status, response_content_type, response_size, request_started_at = result_queue.get(timeout=10)
        response_received_at = time.perf_counter()
        worker.join(timeout=10)
        if worker.is_alive():
            raise RuntimeError(f"Worker thread did not finish for mode {mode}")
        if "published_at" not in published_at_holder:
            raise RuntimeError(f"Publish callback did not run for mode {mode}")
        return ProbeResult(
            mode=mode,
            lease_id=lease.id,
            response_status=response_status,
            response_content_type=response_content_type,
            response_size=response_size,
            published_at=published_at_holder["published_at"],
            committed_at=committed_at,
            request_started_at=request_started_at,
            response_received_at=response_received_at,
        )
    finally:
        BotRuntimeLease.objects.filter(bot=bot).delete()
        bot.delete()
        project.delete()
        org.delete()


def _print_result(result: ProbeResult) -> None:
    relation = "before commit" if result.mode == "immediate" else "after commit"
    print(
        f"[{result.mode}] lease={result.lease_id} response={result.response_status} "
        f"content_type={result.response_content_type!r} size={result.response_size} "
        f"published={result.published_at:.6f} committed={result.committed_at:.6f} "
        f"request_started={result.request_started_at:.6f} response_received={result.response_received_at:.6f} ({relation})"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe the source-archive commit race by comparing immediate publish vs transaction.on_commit publish.",
    )
    parser.add_argument(
        "--mode",
        choices=("immediate", "on_commit", "both"),
        default="both",
        help="Which scenario to run.",
    )
    args = parser.parse_args()

    results: list[ProbeResult] = []
    modes = ("immediate", "on_commit") if args.mode == "both" else (args.mode,)

    for mode in modes:
        result = _run_case(mode)
        results.append(result)
        _print_result(result)

    expectations = {
        "immediate": 404,
        "on_commit": 200,
    }
    failures = [result for result in results if result.response_status != expectations[result.mode]]
    if failures:
        print("Unexpected result detected. The race may still exist or the local environment is not healthy.", file=sys.stderr)
        return 1

    print("Experiment passed: immediate publish can hit 404 before commit, on_commit publish returns 200 after commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
