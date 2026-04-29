#!/usr/bin/env python3
from __future__ import annotations

import argparse
import threading
import time
import uuid
from dataclasses import dataclass
from queue import Queue


@dataclass(slots=True)
class FakeLease:
    lease_id: int
    shutdown_token: str
    committed: bool = False
    deleted: bool = False
    first_heartbeat_seen: bool = False
    status: str = "provisioning"


class FakeLeaseStore:
    def __init__(self) -> None:
        self._leases: dict[int, FakeLease] = {}
        self._next_id = 1

    def create(self) -> FakeLease:
        lease = FakeLease(
            lease_id=self._next_id,
            shutdown_token=uuid.uuid4().hex,
        )
        self._leases[lease.lease_id] = lease
        self._next_id += 1
        return lease

    def commit(self, lease_id: int) -> None:
        self._leases[lease_id].committed = True

    def delete_stale_provisioning_lease(self, lease_id: int) -> bool:
        lease = self._leases[lease_id]
        if lease.status == "provisioning" and not lease.first_heartbeat_seen:
            lease.deleted = True
            lease.status = "deleted"
            return True
        return False

    def request_source_archive(self, lease_id: int, shutdown_token: str) -> int:
        lease = self._leases.get(lease_id)
        if lease is None or lease.deleted or not lease.committed:
            return 404
        if shutdown_token != lease.shutdown_token:
            return 401
        return 200


@dataclass(slots=True)
class PublishResult:
    mode: str
    lease_id: int
    response_status: int
    published_at: float
    committed_at: float
    request_started_at: float
    response_received_at: float


def run_publish_case(mode: str, store: FakeLeaseStore) -> PublishResult:
    lease = store.create()
    publish_event = threading.Event()
    result_queue: Queue[tuple[int, float, float]] = Queue(maxsize=1)

    def worker() -> None:
        publish_event.wait()
        request_started_at = time.perf_counter()
        response_status = store.request_source_archive(lease.lease_id, lease.shutdown_token)
        response_received_at = time.perf_counter()
        result_queue.put((response_status, request_started_at, response_received_at))

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

    if mode == "immediate":
        published_at = time.perf_counter()
        publish_event.set()
        response_status, request_started_at, response_received_at = result_queue.get(timeout=5)
        store.commit(lease.lease_id)
        committed_at = time.perf_counter()
    elif mode == "on_commit":
        store.commit(lease.lease_id)
        committed_at = time.perf_counter()
        published_at = time.perf_counter()
        publish_event.set()
        response_status, request_started_at, response_received_at = result_queue.get(timeout=5)
    else:
        raise ValueError(f"Unknown mode: {mode}")

    worker_thread.join(timeout=5)
    if worker_thread.is_alive():
        raise RuntimeError(f"Worker thread did not finish for mode {mode}")

    return PublishResult(
        mode=mode,
        lease_id=lease.lease_id,
        response_status=response_status,
        published_at=published_at,
        committed_at=committed_at,
        request_started_at=request_started_at,
        response_received_at=response_received_at,
    )


def run_cleanup_case() -> tuple[bool, bool]:
    store = FakeLeaseStore()
    lease = store.create()
    stale_before = lease.deleted
    cleaned = store.delete_stale_provisioning_lease(lease.lease_id)
    return stale_before, cleaned


def print_publish_result(result: PublishResult) -> None:
    relation = "before commit" if result.mode == "immediate" else "after commit"
    print(
        f"[{result.mode}] lease={result.lease_id} response={result.response_status} "
        f"published={result.published_at:.6f} committed={result.committed_at:.6f} "
        f"request_started={result.request_started_at:.6f} response_received={result.response_received_at:.6f} "
        f"({relation})"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthetic experiment for publish ordering and stale lease cleanup.")
    parser.add_argument("--repeat", type=int, default=3, help="How many times to repeat each publish case.")
    args = parser.parse_args()

    stale_before, cleaned = run_cleanup_case()
    print(f"[cleanup] stale_before={stale_before} cleaned={cleaned} expected_deleted=True")

    failures = []
    store = FakeLeaseStore()
    for index in range(args.repeat):
        immediate = run_publish_case("immediate", store)
        print_publish_result(immediate)
        if immediate.response_status != 404:
            failures.append(("immediate", index, immediate.response_status))

        on_commit = run_publish_case("on_commit", store)
        print_publish_result(on_commit)
        if on_commit.response_status != 200:
            failures.append(("on_commit", index, on_commit.response_status))

    if failures:
        print("Experiment failed:", failures)
        return 1

    print("Experiment passed: stale provisioning lease is cleaned up, and on_commit publish avoids the 404 race.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
