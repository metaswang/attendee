import copy
import json
import logging
import os
import subprocess
import tarfile
from io import BytesIO
from pathlib import Path
from textwrap import shorten

import requests
from django.http import FileResponse, HttpResponseNotAllowed, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from bots.models import (
    AudioChunk,
    Bot,
    BotChatMessageRequest,
    BotChatMessageRequestStates,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotLogEntryLevels,
    BotLogEntryTypes,
    BotLogManager,
    BotMediaRequest,
    BotMediaRequestMediaTypes,
    BotMediaRequestStates,
    BotMediaRequestManager,
    BotRuntimeLease,
    BotRuntimeProviderTypes,
    ChatMessage,
    ChatMessageToOptions,
    MediaBlob,
    MeetingTypes,
    BotChatMessageRequestManager,
    Participant,
    ParticipantEvent,
    ParticipantEventTypes,
    Recording,
    RecordingFormats,
    RecordingManager,
    RecordingStates,
    RecordingTranscriptionStates,
    Utterance,
    WebhookTriggerTypes,
)
from bots.bots_api_utils import build_site_url
from bots.runtime_providers import get_runtime_provider
from bots.meeting_url_utils import meeting_type_from_url
from bots.webhook_payloads import chat_message_webhook_payload, participant_event_webhook_payload, utterance_webhook_payload
from bots.webhook_utils import trigger_webhook
from bots.webhook_utils import sign_payload, verify_signature

logger = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ARCHIVE_EXCLUDED_DIR_NAMES = {
    ".git",
    ".hg",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
}
SOURCE_ARCHIVE_EXCLUDED_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".envrc",
}


def _repo_source_archive_paths() -> list[Path]:
    git_dir = REPO_ROOT / ".git"
    if git_dir.exists():
        try:
            tracked_files = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
                check=True,
                capture_output=True,
            ).stdout.split(b"\x00")
            untracked_files = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "ls-files", "--others", "--exclude-standard", "-z"],
                check=True,
                capture_output=True,
            ).stdout.split(b"\x00")
            paths = []
            seen_paths: set[Path] = set()
            for raw_relative_path in [*tracked_files, *untracked_files]:
                if not raw_relative_path:
                    continue
                absolute_path = REPO_ROOT / raw_relative_path.decode("utf-8")
                if absolute_path.exists() and absolute_path not in seen_paths:
                    paths.append(absolute_path)
                    seen_paths.add(absolute_path)
            return paths
        except subprocess.CalledProcessError:
            logger.warning("Falling back to filesystem walk for runtime source archive because git ls-files failed", exc_info=True)

    paths = []
    for root, dir_names, file_names in os.walk(REPO_ROOT):
        dir_names[:] = [name for name in dir_names if name not in SOURCE_ARCHIVE_EXCLUDED_DIR_NAMES]
        for file_name in file_names:
            if file_name in SOURCE_ARCHIVE_EXCLUDED_FILE_NAMES:
                continue
            if file_name.endswith((".pyc", ".pyo")):
                continue
            absolute_path = Path(root) / file_name
            paths.append(absolute_path)
    return paths


def _runtime_lease_for_request(lease_id: int, request):
    try:
        lease = BotRuntimeLease.objects.select_related("bot", "bot__project").get(id=lease_id)
    except BotRuntimeLease.DoesNotExist:
        return None, JsonResponse({"error": "Lease not found"}, status=404)

    auth_header = request.headers.get("Authorization", "")
    expected_auth_header = f"Bearer {lease.shutdown_token}"
    if auth_header != expected_auth_header:
        return None, JsonResponse({"error": "Unauthorized"}, status=401)

    return lease, None


def _serialize_bot_runtime_snapshot(bot: Bot, lease: BotRuntimeLease) -> dict:
    bot_settings = copy.deepcopy(bot.settings or {})
    recording_settings = bot_settings.get("recording_settings") or {}
    callback_settings = bot_settings.get("callback_settings") or {}

    if lease.provider == BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE and meeting_type_from_url(bot.meeting_url) in {MeetingTypes.GOOGLE_MEET, MeetingTypes.TEAMS}:
        session_id = str((bot.metadata or {}).get("session_id") or "").strip() or bot.object_id
        recording_format = recording_settings.get("format") or RecordingFormats.MP3
        recording_settings["transport"] = "r2_chunks"
        recording_settings["format"] = recording_format
        recording_settings.setdefault("audio_raw_path", f"customer_audio/{bot.project.object_id}/{session_id}/original.m4a")
        if recording_format == RecordingFormats.MP3:
            recording_settings.setdefault("audio_chunk_prefix", f"customer_audio/{bot.project.object_id}/{session_id}/chunks")
            recording_settings.pop("video_chunk_prefix", None)
        elif recording_format in (RecordingFormats.MP4, RecordingFormats.WEBM):
            recording_settings.setdefault("video_chunk_prefix", f"video/{bot.project.object_id}/{session_id}/chunks")
            recording_settings.pop("audio_chunk_prefix", None)
        recording_settings.setdefault("chunk_interval_ms", 5000)
        recording_complete = copy.deepcopy(callback_settings.get("recording_complete") or {})
        recording_complete["url"] = build_site_url(f"/internal/bot-runtime-leases/{lease.id}/recording-complete")
        recording_complete["signing_secret"] = lease.shutdown_token
        callback_settings["recording_complete"] = recording_complete

    bot_settings["recording_settings"] = recording_settings
    if callback_settings:
        bot_settings["callback_settings"] = callback_settings

    recordings = [
        {
            "id": recording.id,
            "object_id": recording.object_id,
            "is_default_recording": recording.is_default_recording,
            "state": recording.state,
            "transcription_state": recording.transcription_state,
            "recording_type": recording.recording_type,
            "transcription_type": recording.transcription_type,
            "transcription_provider": recording.transcription_provider,
            "file": recording.file.name,
        }
        for recording in bot.recordings.all().order_by("created_at")
    ]
    project_credentials = [
        {
            "credential_type": credential.credential_type,
            "credentials": credential.get_credentials(),
        }
        for credential in bot.project.credentials.all().order_by("created_at")
    ]
    return {
        "lease": {
            "id": lease.id,
            "provider": lease.provider,
            "provider_instance_id": lease.provider_instance_id,
            "provider_name": lease.provider_name,
            "region": lease.region,
            "size_class": lease.size_class,
            "snapshot_id": lease.snapshot_id,
            "status": lease.status,
        },
        "bot": {
            "id": bot.id,
            "object_id": bot.object_id,
            "name": bot.name,
            "meeting_url": bot.meeting_url,
            "meeting_uuid": bot.meeting_uuid,
            "state": bot.state,
            "settings": bot_settings,
            "runtime_settings": bot_settings.get("runtime_settings", {}),
            "recording_settings": recording_settings,
            "transcription_settings": bot_settings.get("transcription_settings", {}),
            "websocket_settings": bot_settings.get("websocket_settings", {}),
            "voice_agent_settings": bot_settings.get("voice_agent_settings", {}),
            "metadata": bot.metadata,
            "join_at": bot.join_at.isoformat() if bot.join_at else None,
            "deduplication_key": bot.deduplication_key,
            "zoom_rtms_stream_id": bot.zoom_rtms_stream_id,
            "session_type": bot.session_type,
            "created_at": bot.created_at.isoformat(),
            "updated_at": bot.updated_at.isoformat(),
            "first_heartbeat_timestamp": bot.first_heartbeat_timestamp,
            "last_heartbeat_timestamp": bot.last_heartbeat_timestamp,
        },
        "project": {
            "id": bot.project.id,
            "object_id": bot.project.object_id,
            "name": bot.project.name,
            "organization": {
                "is_async_transcription_enabled": bot.project.organization.is_async_transcription_enabled,
            },
            "credentials": project_credentials,
        },
        "recordings": recordings,
        "last_bot_event": None
        if bot.last_bot_event() is None
        else {
            "event_type": bot.last_bot_event().event_type,
            "event_sub_type": bot.last_bot_event().event_sub_type,
            "metadata": bot.last_bot_event().metadata,
            "requested_bot_action_taken_at": bot.last_bot_event().requested_bot_action_taken_at.isoformat()
            if bot.last_bot_event().requested_bot_action_taken_at
            else None,
            "old_state": bot.last_bot_event().old_state,
            "new_state": bot.last_bot_event().new_state,
        },
    }


def _serialize_control_snapshot(bot: Bot) -> dict:
    return {
        "bot_state": bot.state,
        "join_at": bot.join_at.isoformat() if bot.join_at else None,
        "runtime_settings": bot.settings.get("runtime_settings", {}),
        "recording_settings": bot.settings.get("recording_settings", {}),
        "transcription_settings": bot.settings.get("transcription_settings", {}),
        "websocket_settings": bot.settings.get("websocket_settings", {}),
        "voice_agent_settings": bot.settings.get("voice_agent_settings", {}),
        "media_requests": [
            {
                "id": req.id,
                "state": req.state,
                "media_type": req.media_type,
                "media_url": req.media_url,
                "loop": req.loop,
                "text_to_speak": req.text_to_speak,
                "text_to_speech_settings": req.text_to_speech_settings,
                "media_blob_object_id": req.media_blob.object_id if req.media_blob else None,
                "media_blob_duration_ms": req.media_blob.duration_ms if req.media_blob else None,
                "created_at": req.created_at.isoformat(),
            }
            for req in bot.media_requests.all().order_by("created_at")
        ],
        "chat_message_requests": [
            {
                "id": req.id,
                "state": req.state,
                "to": req.to,
                "to_user_uuid": req.to_user_uuid,
                "message": req.message,
                "additional_data": req.additional_data,
                "sent_at_timestamp_ms": req.sent_at_timestamp_ms,
                "failure_data": req.failure_data,
                "created_at": req.created_at.isoformat(),
            }
            for req in bot.chat_message_requests.all().order_by("created_at")
        ],
    }


def _require_json_body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}"), None
    except json.JSONDecodeError:
        return None, JsonResponse({"error": "Invalid JSON body"}, status=400)


def _get_participant_for_runtime_payload(bot: Bot, payload: dict):
    participant, _ = Participant.objects.get_or_create(
        bot=bot,
        uuid=payload["participant_uuid"],
        defaults={
            "user_uuid": payload.get("participant_user_uuid"),
            "full_name": payload.get("participant_full_name"),
            "is_the_bot": payload.get("participant_is_the_bot", False),
            "is_host": payload.get("participant_is_host", False),
        },
    )
    return participant


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseCompletionView(View):
    http_method_names = ["post"]

    def post(self, request, lease_id: int):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response

        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON body"}, status=400)

        provider_instance_id = str(payload.get("provider_instance_id") or payload.get("droplet_id") or "").strip()
        if provider_instance_id and lease.provider_instance_id and provider_instance_id != lease.provider_instance_id:
            return JsonResponse({"error": "provider_instance_id does not match lease"}, status=400)

        provider = get_runtime_provider(lease.provider)

        if provider_instance_id and not lease.provider_instance_id:
            lease.provider_instance_id = provider_instance_id
            lease.save(update_fields=["provider_instance_id", "updated_at"])

        try:
            logger.info(
                "Received runtime lease completion for lease=%s bot=%s provider=%s provider_instance_id=%s",
                lease.id,
                lease.bot.object_id,
                lease.provider,
                provider_instance_id or lease.provider_instance_id,
            )
            provider.delete_lease(lease)
        except Exception as exc:
            logger.exception("Failed to delete runtime lease %s for bot %s", lease.id, lease.bot.object_id)
            lease.mark_failed(str(exc))
            return JsonResponse({"error": "Failed to delete runtime instance", "details": str(exc)}, status=502)

        exit_code = payload.get("exit_code")
        final_state = payload.get("final_state")
        reason = payload.get("reason")
        log_tail = (payload.get("log_tail") or "").strip()
        if exit_code not in (None, 0) or final_state == "failed":
            summary_parts = [f"exit_code={exit_code}", f"final_state={final_state}", f"reason={reason}"]
            if log_tail:
                summary_parts.append(f"log_tail={log_tail}")
            lease.last_error = shorten(" | ".join(summary_parts), width=4000, placeholder="...")
            lease.save(update_fields=["last_error", "updated_at"])

        logger.info(
            "Lease %s completion accepted for bot %s with exit_code=%s final_state=%s reason=%s",
            lease.id,
            lease.bot.object_id,
            exit_code,
            final_state,
            reason,
        )
        return JsonResponse({"status": lease.status, "provider_instance_id": lease.provider_instance_id})

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "post":
            return HttpResponseNotAllowed(["POST"])
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseRecordingFileView(View):
    http_method_names = ["post"]

    def post(self, request, lease_id: int, recording_id: int):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response

        payload, error_response = _require_json_body(request)
        if error_response is not None:
            return error_response

        file_name = str(payload.get("file") or payload.get("file_name") or "").strip()
        if not file_name:
            return JsonResponse({"error": "file is required"}, status=400)

        try:
            recording = lease.bot.recordings.get(id=recording_id)
        except Recording.DoesNotExist:
            return JsonResponse({"error": "Recording not found"}, status=404)

        recording.file = file_name

        first_buffer_timestamp_ms = payload.get("first_buffer_timestamp_ms")
        if first_buffer_timestamp_ms is not None:
            try:
                recording.first_buffer_timestamp_ms = int(first_buffer_timestamp_ms)
            except (TypeError, ValueError):
                return JsonResponse({"error": "first_buffer_timestamp_ms must be an integer"}, status=400)

        update_fields = ["file"]
        if first_buffer_timestamp_ms is not None:
            update_fields.append("first_buffer_timestamp_ms")
        update_fields.append("updated_at")
        recording.save(update_fields=update_fields)

        logger.info(
            "Persisted runtime recording file for lease=%s bot=%s recording=%s file=%s",
            lease.id,
            lease.bot.object_id,
            recording.object_id,
            file_name,
        )
        return JsonResponse(
            {
                "recording_id": recording.id,
                "recording_object_id": recording.object_id,
                "file": recording.file.name,
                "first_buffer_timestamp_ms": recording.first_buffer_timestamp_ms,
            },
            status=200,
        )

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "post":
            return HttpResponseNotAllowed(["POST"])
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseRecordingCompleteView(View):
    http_method_names = ["post"]

    def post(self, request, lease_id: int):
        try:
            lease = BotRuntimeLease.objects.select_related("bot", "bot__project").get(id=lease_id)
        except BotRuntimeLease.DoesNotExist:
            return JsonResponse({"error": "Lease not found"}, status=404)

        payload, error_response = _require_json_body(request)
        if error_response is not None:
            return error_response

        signature = request.headers.get("X-Webhook-Signature", "")
        if not signature or not verify_signature(payload, signature, lease.shutdown_token):
            return JsonResponse({"error": "Invalid signature"}, status=401)

        if payload.get("trigger") != "recording.complete":
            return JsonResponse({"error": "Unsupported trigger"}, status=400)

        logger.info(
            "Received runtime recording complete callback for lease=%s bot=%s chunk_count=%s raw_path=%s",
            lease.id,
            lease.bot.object_id,
            (((payload.get("data") or {}).get("audio") or {}).get("chunk_count")),
            (((payload.get("data") or {}).get("audio") or {}).get("raw_path")),
        )

        callback_url = lease.bot.recording_complete_callback_url()
        callback_signing_secret = lease.bot.recording_complete_signing_secret()
        if not callback_url or not callback_signing_secret:
            logger.error(
                "Missing upstream recording complete callback settings for lease=%s bot=%s",
                lease.id,
                lease.bot.object_id,
            )
            return JsonResponse({"error": "Upstream recording complete callback is not configured"}, status=502)

        upstream_signature = sign_payload(payload, callback_signing_secret)
        try:
            response = requests.post(
                callback_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Attendee-Runtime/1.0",
                    "X-Webhook-Signature": upstream_signature,
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            logger.exception(
                "Failed to forward runtime recording complete callback for lease=%s bot=%s url=%s",
                lease.id,
                lease.bot.object_id,
                callback_url,
            )
            return JsonResponse(
                {
                    "error": "Failed to forward recording complete callback",
                    "details": str(exc),
                },
                status=502,
            )

        if not 200 <= response.status_code < 300:
            logger.error(
                "Upstream recording complete callback rejected for lease=%s bot=%s url=%s status=%s body=%s",
                lease.id,
                lease.bot.object_id,
                callback_url,
                response.status_code,
                response.text[:1000],
            )
            return JsonResponse(
                {
                    "error": "Recording complete callback rejected",
                    "status_code": response.status_code,
                    "details": response.text[:1000],
                },
                status=502,
            )

        logger.info(
            "Forwarded runtime recording complete callback for lease=%s bot=%s url=%s status=%s",
            lease.id,
            lease.bot.object_id,
            callback_url,
            response.status_code,
        )
        return JsonResponse({"status": "ok"}, status=200)

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "post":
            return HttpResponseNotAllowed(["POST"])
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseBootstrapView(View):
    http_method_names = ["get"]

    def get(self, request, lease_id: int):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response
        return JsonResponse(_serialize_bot_runtime_snapshot(lease.bot, lease))

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "get":
            return HttpResponseNotAllowed(["GET"])
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseControlView(View):
    http_method_names = ["get"]

    def get(self, request, lease_id: int):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response
        return JsonResponse(_serialize_control_snapshot(lease.bot))

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "get":
            return HttpResponseNotAllowed(["GET"])
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseSourceArchiveView(View):
    http_method_names = ["get"]

    def get(self, request, lease_id: int):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response

        archive_buffer = BytesIO()
        with tarfile.open(fileobj=archive_buffer, mode="w:gz") as tar:
            for absolute_path in _repo_source_archive_paths():
                relative_path = absolute_path.relative_to(REPO_ROOT)
                tar.add(absolute_path, arcname=relative_path, recursive=False)

        archive_buffer.seek(0)
        response = FileResponse(archive_buffer, content_type="application/gzip")
        response["Content-Disposition"] = f'attachment; filename="bot-runtime-lease-{lease.id}-source.tar.gz"'
        return response

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "get":
            return HttpResponseNotAllowed(["GET"])
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseMediaBlobView(View):
    http_method_names = ["get"]

    def get(self, request, lease_id: int, object_id: str):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response

        try:
            media_blob = MediaBlob.objects.get(object_id=object_id, project=lease.bot.project)
        except MediaBlob.DoesNotExist:
            return JsonResponse({"error": "Media blob not found"}, status=404)

        content_type = media_blob.content_type
        if content_type == "audio/mp3":
            response_content_type = "audio/mpeg"
        elif content_type == "image/png":
            response_content_type = "image/png"
        else:
            response_content_type = "application/octet-stream"

        return FileResponse(
            BytesIO(media_blob.blob),
            content_type=response_content_type,
            as_attachment=False,
            filename=f"{media_blob.object_id}",
        )

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "get":
            return HttpResponseNotAllowed(["GET"])
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseBotEventsView(View):
    http_method_names = ["post"]

    def post(self, request, lease_id: int):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response

        payload, error_response = _require_json_body(request)
        if error_response is not None:
            return error_response

        try:
            event_type = int(payload["event_type"])
        except (KeyError, TypeError, ValueError):
            return JsonResponse({"error": "event_type is required"}, status=400)

        event_sub_type = payload.get("event_sub_type")
        if event_sub_type is not None:
            try:
                event_sub_type = int(event_sub_type)
            except (TypeError, ValueError):
                return JsonResponse({"error": "event_sub_type must be an integer"}, status=400)

        event_metadata = payload.get("event_metadata") or {}

        try:
            event = BotEventManager.create_event(
                bot=lease.bot,
                event_type=event_type,
                event_sub_type=event_sub_type,
                event_metadata=event_metadata,
            )
        except Exception as exc:
            logger.exception("Failed to create bot event for lease %s", lease.id)
            return JsonResponse({"error": str(exc)}, status=400)

        return JsonResponse(
            {
                "event_id": event.id,
                "old_state": event.old_state,
                "new_state": event.new_state,
                "created_at": event.created_at.isoformat(),
            },
            status=201,
        )

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "post":
            return HttpResponseNotAllowed(["POST"])
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseParticipantEventsView(View):
    http_method_names = ["post"]

    def post(self, request, lease_id: int):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response

        payload, error_response = _require_json_body(request)
        if error_response is not None:
            return error_response

        try:
            participant = _get_participant_for_runtime_payload(lease.bot, payload)
            event_type = int(payload["event_type"])
            timestamp_ms = int(payload["timestamp_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            return JsonResponse({"error": f"Invalid participant event payload: {exc}"}, status=400)

        if event_type == ParticipantEventTypes.UPDATE:
            event_data = payload.get("event_data") or {}
            if "isHost" in event_data:
                participant.is_host = event_data["isHost"]["after"]
                participant.save(update_fields=["is_host", "updated_at"])
            return JsonResponse({"participant_id": participant.id, "updated": True}, status=200)

        participant_event = ParticipantEvent.objects.create(
            participant=participant,
            event_type=event_type,
            event_data=payload.get("event_data") or {},
            timestamp_ms=timestamp_ms,
        )

        if not participant.is_the_bot:
            if participant_event.event_type in (ParticipantEventTypes.JOIN, ParticipantEventTypes.LEAVE):
                webhook_trigger_type = WebhookTriggerTypes.PARTICIPANT_EVENTS_JOIN_LEAVE
            elif participant_event.event_type in (ParticipantEventTypes.SPEECH_START, ParticipantEventTypes.SPEECH_STOP):
                webhook_trigger_type = WebhookTriggerTypes.PARTICIPANT_EVENTS_SPEECH_START_STOP
            else:
                webhook_trigger_type = None

            if webhook_trigger_type is not None:
                trigger_webhook(
                    webhook_trigger_type=webhook_trigger_type,
                    bot=lease.bot,
                    payload=participant_event_webhook_payload(participant_event),
                )

        return JsonResponse(
            {
                "participant_id": participant.id,
                "event_id": participant_event.id,
                "event_object_id": participant_event.object_id,
            },
            status=201,
        )

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "post":
            return HttpResponseNotAllowed(["POST"])
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseChatMessagesView(View):
    http_method_names = ["post"]

    def post(self, request, lease_id: int):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response

        payload, error_response = _require_json_body(request)
        if error_response is not None:
            return error_response

        try:
            participant = _get_participant_for_runtime_payload(lease.bot, payload)
            source_uuid = payload.get("source_uuid")
            if not source_uuid:
                source_uuid = f"{payload['recording_object_id']}-{payload['message_uuid']}"
            chat_message, _ = ChatMessage.objects.update_or_create(
                bot=lease.bot,
                source_uuid=source_uuid,
                defaults={
                    "timestamp": payload["timestamp"],
                    "to": ChatMessageToOptions.ONLY_BOT if payload.get("to_bot") else ChatMessageToOptions.EVERYONE,
                    "text": payload["text"],
                    "participant": participant,
                    "additional_data": payload.get("additional_data", {}),
                },
            )
        except (KeyError, TypeError, ValueError) as exc:
            return JsonResponse({"error": f"Invalid chat message payload: {exc}"}, status=400)

        trigger_webhook(
            webhook_trigger_type=WebhookTriggerTypes.CHAT_MESSAGES_UPDATE,
            bot=lease.bot,
            payload=chat_message_webhook_payload(chat_message),
        )

        return JsonResponse({"chat_message_id": chat_message.id, "chat_message_object_id": chat_message.object_id}, status=201)

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "post":
            return HttpResponseNotAllowed(["POST"])
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseCaptionsView(View):
    http_method_names = ["post"]

    def post(self, request, lease_id: int):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response

        payload, error_response = _require_json_body(request)
        if error_response is not None:
            return error_response

        try:
            participant = _get_participant_for_runtime_payload(lease.bot, payload)
            recording = RecordingManager.get_recording_in_progress(lease.bot)
            if recording is None:
                return JsonResponse({"error": "No recording in progress"}, status=409)
            source_uuid = f"{recording.object_id}-{payload['source_uuid_suffix']}"
            utterance, _ = Utterance.objects.update_or_create(
                recording=recording,
                source_uuid=source_uuid,
                defaults={
                    "source": Utterance.Sources.CLOSED_CAPTION_FROM_PLATFORM,
                    "participant": participant,
                    "transcription": {"transcript": payload["text"]},
                    "timestamp_ms": int(payload["timestamp_ms"]),
                    "duration_ms": int(payload.get("duration_ms", 0)),
                    "sample_rate": None,
                },
            )
        except (KeyError, TypeError, ValueError) as exc:
            return JsonResponse({"error": f"Invalid caption payload: {exc}"}, status=400)

        trigger_webhook(
            webhook_trigger_type=WebhookTriggerTypes.TRANSCRIPT_UPDATE,
            bot=lease.bot,
            payload=utterance_webhook_payload(utterance),
        )

        RecordingManager.set_recording_transcription_in_progress(recording)
        return JsonResponse({"utterance_id": utterance.id}, status=201)

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "post":
            return HttpResponseNotAllowed(["POST"])
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseAudioChunksView(View):
    http_method_names = ["post"]

    def post(self, request, lease_id: int):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response

        payload, error_response = _require_json_body(request)
        if error_response is not None:
            return error_response

        try:
            participant = _get_participant_for_runtime_payload(lease.bot, payload)
            recording = RecordingManager.get_recording_in_progress(lease.bot)
            if recording is None:
                return JsonResponse({"error": "No recording in progress"}, status=409)
            audio_chunk = AudioChunk.objects.create(
                recording=recording,
                participant=participant,
                audio_blob=b"",
                is_blob_stored_remotely=True,
                audio_blob_remote_file=payload["audio_blob_remote_file"],
                audio_format=AudioChunk.AudioFormat(int(payload.get("audio_format", AudioChunk.AudioFormat.PCM))),
                timestamp_ms=int(payload["timestamp_ms"]),
                duration_ms=int(payload["duration_ms"]),
                sample_rate=int(payload["sample_rate"]),
                source=AudioChunk.Sources.PER_PARTICIPANT_AUDIO,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return JsonResponse({"error": f"Invalid audio chunk payload: {exc}"}, status=400)

        from bots.tasks.process_utterance_task import process_utterance

        utterance = Utterance.objects.create(
            source=Utterance.Sources.PER_PARTICIPANT_AUDIO,
            async_transcription=None,
            recording=recording,
            participant=participant,
            audio_chunk=audio_chunk,
            timestamp_ms=audio_chunk.timestamp_ms,
            duration_ms=audio_chunk.duration_ms,
        )
        RecordingManager.set_recording_transcription_in_progress(recording)
        process_utterance.delay(utterance.id)

        return JsonResponse(
            {
                "audio_chunk_id": audio_chunk.id,
                "audio_chunk_object_id": audio_chunk.object_id,
                "utterance_id": utterance.id,
            },
            status=201,
        )

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "post":
            return HttpResponseNotAllowed(["POST"])
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseBotLogsView(View):
    http_method_names = ["post"]

    def post(self, request, lease_id: int):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response

        payload, error_response = _require_json_body(request)
        if error_response is not None:
            return error_response

        try:
            level = int(payload["level"])
            entry_type = int(payload.get("entry_type", BotLogEntryTypes.UNCATEGORIZED))
            message = str(payload["message"])
        except (KeyError, TypeError, ValueError) as exc:
            return JsonResponse({"error": f"Invalid bot log payload: {exc}"}, status=400)

        log = BotLogManager.create_bot_log_entry(
            bot=lease.bot,
            level=level,
            entry_type=entry_type,
            message=message,
        )
        return JsonResponse({"bot_log_id": log.id, "bot_log_object_id": log.object_id}, status=201)

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "post":
            return HttpResponseNotAllowed(["POST"])
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseResourceSnapshotsView(View):
    http_method_names = ["post"]

    def post(self, request, lease_id: int):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response

        payload, error_response = _require_json_body(request)
        if error_response is not None:
            return error_response

        from bots.models import BotResourceSnapshot

        snapshot = BotResourceSnapshot.objects.create(bot=lease.bot, data=payload.get("data") or {})
        return JsonResponse({"resource_snapshot_id": snapshot.id}, status=201)

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "post":
            return HttpResponseNotAllowed(["POST"])
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseHeartbeatView(View):
    http_method_names = ["post"]

    def post(self, request, lease_id: int):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response

        lease.bot.set_heartbeat()
        lease.mark_active()
        return JsonResponse(
            {
                "first_heartbeat_timestamp": lease.bot.first_heartbeat_timestamp,
                "last_heartbeat_timestamp": lease.bot.last_heartbeat_timestamp,
                "lease_status": lease.status,
            },
            status=200,
        )

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "post":
            return HttpResponseNotAllowed(["POST"])
        return super().dispatch(request, *args, **kwargs)


def _media_request_for_request(lease: BotRuntimeLease, request_id: int):
    try:
        media_request = BotMediaRequest.objects.select_related("media_blob").get(id=request_id, bot=lease.bot)
    except BotMediaRequest.DoesNotExist:
        return None, JsonResponse({"error": "Media request not found"}, status=404)
    return media_request, None


def _chat_message_request_for_request(lease: BotRuntimeLease, request_id: int):
    try:
        chat_message_request = BotChatMessageRequest.objects.get(id=request_id, bot=lease.bot)
    except BotChatMessageRequest.DoesNotExist:
        return None, JsonResponse({"error": "Chat message request not found"}, status=404)
    return chat_message_request, None


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseMediaRequestStatusView(View):
    http_method_names = ["post"]

    def post(self, request, lease_id: int, request_id: int):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response

        media_request, error_response = _media_request_for_request(lease, request_id)
        if error_response is not None:
            return error_response

        payload, error_response = _require_json_body(request)
        if error_response is not None:
            return error_response

        state = payload.get("state")
        if state is None:
            return JsonResponse({"error": "state is required"}, status=400)

        try:
            state = int(state)
        except (TypeError, ValueError):
            return JsonResponse({"error": "state must be an integer"}, status=400)

        if state == BotMediaRequestStates.PLAYING:
            BotMediaRequestManager.set_media_request_playing(media_request)
        elif state == BotMediaRequestStates.FINISHED:
            BotMediaRequestManager.set_media_request_finished(media_request)
        elif state == BotMediaRequestStates.FAILED_TO_PLAY:
            BotMediaRequestManager.set_media_request_failed_to_play(media_request)
        elif state == BotMediaRequestStates.DROPPED:
            BotMediaRequestManager.set_media_request_dropped(media_request)
        else:
            return JsonResponse({"error": "Unsupported media request state"}, status=400)

        return JsonResponse({"request_id": media_request.id, "state": media_request.state}, status=200)

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "post":
            return HttpResponseNotAllowed(["POST"])
        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class BotRuntimeLeaseChatMessageRequestStatusView(View):
    http_method_names = ["post"]

    def post(self, request, lease_id: int, request_id: int):
        lease, error_response = _runtime_lease_for_request(lease_id, request)
        if error_response is not None:
            return error_response

        chat_message_request, error_response = _chat_message_request_for_request(lease, request_id)
        if error_response is not None:
            return error_response

        payload, error_response = _require_json_body(request)
        if error_response is not None:
            return error_response

        state = payload.get("state")
        if state is None:
            return JsonResponse({"error": "state is required"}, status=400)

        try:
            state = int(state)
        except (TypeError, ValueError):
            return JsonResponse({"error": "state must be an integer"}, status=400)

        if state == BotChatMessageRequestStates.SENT:
            BotChatMessageRequestManager.set_chat_message_request_sent(chat_message_request)
        elif state == BotChatMessageRequestStates.FAILED:
            BotChatMessageRequestManager.set_chat_message_request_failed(chat_message_request)
        else:
            return JsonResponse({"error": "Unsupported chat message request state"}, status=400)

        return JsonResponse({"request_id": chat_message_request.id, "state": chat_message_request.state}, status=200)

    def dispatch(self, request, *args, **kwargs):
        if request.method.lower() != "post":
            return HttpResponseNotAllowed(["POST"])
        return super().dispatch(request, *args, **kwargs)
