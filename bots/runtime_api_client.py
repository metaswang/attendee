from __future__ import annotations

import os
import time
from typing import Any, Optional

import requests
import logging

logger = logging.getLogger(__name__)


class BotRuntimeApiClient:
    def __init__(self, bootstrap_url: str, control_url: str, shutdown_token: str, timeout_seconds: int = 15, get_retry_attempts: int = 3):
        self.bootstrap_url = bootstrap_url.rstrip("/")
        self.control_url = control_url.rstrip("/")
        self.shutdown_token = shutdown_token
        self.timeout_seconds = timeout_seconds
        self.get_retry_attempts = max(1, get_retry_attempts)
        self._session = requests.Session()

    @classmethod
    def from_environment(cls) -> "BotRuntimeApiClient | None":
        bootstrap_url = os.getenv("BOT_RUNTIME_BOOTSTRAP_URL")
        control_url = os.getenv("BOT_RUNTIME_CONTROL_URL")
        shutdown_token = os.getenv("LEASE_SHUTDOWN_TOKEN")
        if not bootstrap_url or not control_url or not shutdown_token:
            return None
        timeout_seconds = int(os.getenv("BOT_RUNTIME_API_TIMEOUT_SECONDS", "15"))
        get_retry_attempts = int(os.getenv("BOT_RUNTIME_API_GET_RETRY_ATTEMPTS", "3"))
        return cls(
            bootstrap_url=bootstrap_url,
            control_url=control_url,
            shutdown_token=shutdown_token,
            timeout_seconds=timeout_seconds,
            get_retry_attempts=get_retry_attempts,
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.shutdown_token}"}

    def _get(self, url: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.get_retry_attempts + 1):
            try:
                request_started_at = time.monotonic()
                response = self._session.get(url, headers=self._headers(), timeout=self.timeout_seconds)
                response.raise_for_status()
                logger.info(
                    "Bot runtime GET succeeded url=%s attempt=%s elapsed_seconds=%.3f status=%s",
                    url,
                    attempt,
                    time.monotonic() - request_started_at,
                    response.status_code,
                )
                return response.json()
            except requests.exceptions.RequestException as exc:
                last_error = exc
                logger.warning(
                    "Bot runtime GET failed url=%s attempt=%s/%s timeout_seconds=%s error=%s",
                    url,
                    attempt,
                    self.get_retry_attempts,
                    self.timeout_seconds,
                    exc,
                )
                if attempt >= self.get_retry_attempts:
                    break
                time.sleep(min(5 * attempt, 15))
        if last_error is None:
            raise RuntimeError(f"GET {url} failed without an exception")
        raise last_error

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_started_at = time.monotonic()
        response = self._session.post(url, headers=self._headers(), json=payload, timeout=self.timeout_seconds)
        response.raise_for_status()
        logger.info(
            "Bot runtime POST succeeded url=%s elapsed_seconds=%.3f status=%s",
            url,
            time.monotonic() - request_started_at,
            response.status_code,
        )
        if response.content:
            return response.json()
        return {}

    def get_bootstrap(self) -> dict[str, Any]:
        return self._get(self.bootstrap_url)

    def get_control(self) -> dict[str, Any]:
        return self._get(self.control_url)

    def post_complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post_json(f"{self.bootstrap_url.rsplit('/bootstrap', 1)[0]}/complete", payload)

    def post_bot_event(self, lease_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post_json(f"{self.bootstrap_url.rsplit('/bootstrap', 1)[0]}/bot-events", payload)

    def post_participant_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post_json(f"{self.control_url.rsplit('/control', 1)[0]}/participants/events", payload)

    def post_chat_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post_json(f"{self.control_url.rsplit('/control', 1)[0]}/chat-messages", payload)

    def post_caption(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post_json(f"{self.control_url.rsplit('/control', 1)[0]}/captions", payload)

    def post_audio_chunk(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post_json(f"{self.control_url.rsplit('/control', 1)[0]}/audio-chunks", payload)

    def post_bot_log(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post_json(f"{self.control_url.rsplit('/control', 1)[0]}/bot-logs", payload)

    def post_resource_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post_json(f"{self.control_url.rsplit('/control', 1)[0]}/resource-snapshots", payload)

    def post_heartbeat(self) -> dict[str, Any]:
        return self._post_json(f"{self.control_url.rsplit('/control', 1)[0]}/heartbeat", {})

    def post_media_request_status(self, request_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post_json(f"{self.control_url.rsplit('/control', 1)[0]}/media-requests/{request_id}/status", payload)

    def post_chat_message_request_status(self, request_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post_json(f"{self.control_url.rsplit('/control', 1)[0]}/chat-message-requests/{request_id}/status", payload)

    def post_recording_file(self, recording_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post_json(f"{self.control_url.rsplit('/control', 1)[0]}/recordings/{recording_id}/file", payload)

    def get_media_blob(self, object_id: str) -> bytes:
        url = f"{self.control_url.rsplit('/control', 1)[0]}/media-blobs/{object_id}"
        last_error: Exception | None = None
        for attempt in range(1, self.get_retry_attempts + 1):
            try:
                request_started_at = time.monotonic()
                response = self._session.get(url, headers=self._headers(), timeout=self.timeout_seconds)
                response.raise_for_status()
                logger.info(
                    "Bot runtime media blob GET succeeded url=%s attempt=%s elapsed_seconds=%.3f status=%s",
                    url,
                    attempt,
                    time.monotonic() - request_started_at,
                    response.status_code,
                )
                return response.content
            except requests.exceptions.RequestException as exc:
                last_error = exc
                logger.warning(
                    "Bot runtime media blob GET failed url=%s attempt=%s/%s timeout_seconds=%s error=%s",
                    url,
                    attempt,
                    self.get_retry_attempts,
                    self.timeout_seconds,
                    exc,
                )
                if attempt >= self.get_retry_attempts:
                    break
                time.sleep(min(5 * attempt, 15))
        if last_error is None:
            raise RuntimeError(f"GET {url} failed without an exception")
        raise last_error
