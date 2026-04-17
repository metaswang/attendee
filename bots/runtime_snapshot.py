from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from bots.models import (
    BotChatMessageRequestStates,
    BotMediaRequestMediaTypes,
    BotMediaRequestStates,
    RecordingFormats,
    RecordingResolutions,
    RecordingTypes,
    RecordingViews,
    SessionTypes,
    TranscriptionSettings,
)


@dataclass
class RuntimeOrganizationSnapshot:
    is_async_transcription_enabled: bool = True


@dataclass
class RuntimeCredentialSnapshot:
    credential_type: int
    credentials: dict[str, Any]

    def get_credentials(self) -> dict[str, Any]:
        return self.credentials


class RuntimeCredentialCollection:
    def __init__(self, credentials: Iterable[RuntimeCredentialSnapshot | dict[str, Any]]):
        self._credentials = [
            credential if isinstance(credential, RuntimeCredentialSnapshot) else RuntimeCredentialSnapshot(
                credential_type=credential["credential_type"],
                credentials=credential.get("credentials") or {},
            )
            for credential in credentials
        ]

    def filter(self, **kwargs):
        credential_type = kwargs.get("credential_type")
        items = self._credentials
        if credential_type is not None:
            items = [credential for credential in items if credential.credential_type == credential_type]
        return RuntimeCredentialCollection(items)

    def first(self):
        return self._credentials[0] if self._credentials else None

    def exists(self):
        return bool(self._credentials)

    def all(self):
        return list(self._credentials)


@dataclass
class RuntimeZoomOAuthAppSnapshot:
    object_id: str
    client_id: str
    credentials: dict[str, Any]
    zoom_oauth_connections: "RuntimeZoomOAuthConnectionCollection" = field(default_factory=lambda: RuntimeZoomOAuthConnectionCollection([]))
    zoom_meeting_to_zoom_oauth_connection_mappings: "RuntimeZoomMeetingToZoomOAuthConnectionMappingCollection" = field(
        default_factory=lambda: RuntimeZoomMeetingToZoomOAuthConnectionMappingCollection([])
    )

    def get_credentials(self) -> dict[str, Any]:
        return self.credentials

    @property
    def client_secret(self) -> str | None:
        return self.credentials.get("client_secret")

    @property
    def webhook_secret(self) -> str | None:
        return self.credentials.get("webhook_secret")


class RuntimeZoomOAuthAppCollection:
    def __init__(self, zoom_oauth_apps: Iterable[RuntimeZoomOAuthAppSnapshot | dict[str, Any]]):
        self._zoom_oauth_apps = [
            zoom_oauth_app
            if isinstance(zoom_oauth_app, RuntimeZoomOAuthAppSnapshot)
            else RuntimeZoomOAuthAppSnapshot(
                object_id=zoom_oauth_app["object_id"],
                client_id=zoom_oauth_app["client_id"],
                credentials=zoom_oauth_app.get("credentials") or {},
                zoom_oauth_connections=RuntimeZoomOAuthConnectionCollection(zoom_oauth_app.get("zoom_oauth_connections") or []),
                zoom_meeting_to_zoom_oauth_connection_mappings=RuntimeZoomMeetingToZoomOAuthConnectionMappingCollection(
                    zoom_oauth_app.get("zoom_meeting_to_zoom_oauth_connection_mappings") or []
                ),
            )
            for zoom_oauth_app in zoom_oauth_apps
        ]

    def filter(self, **kwargs):
        items = self._zoom_oauth_apps
        for key, value in kwargs.items():
            if key == "object_id":
                items = [zoom_oauth_app for zoom_oauth_app in items if zoom_oauth_app.object_id == value]
            elif key == "client_id":
                items = [zoom_oauth_app for zoom_oauth_app in items if zoom_oauth_app.client_id == value]
        return RuntimeZoomOAuthAppCollection(items)

    def first(self):
        return self._zoom_oauth_apps[0] if self._zoom_oauth_apps else None

    def exists(self):
        return bool(self._zoom_oauth_apps)

    def all(self):
        return list(self._zoom_oauth_apps)

    def __iter__(self):
        return iter(self._zoom_oauth_apps)


@dataclass
class RuntimeProjectSnapshot:
    id: int
    object_id: str
    name: str
    organization: RuntimeOrganizationSnapshot
    credentials: RuntimeCredentialCollection
    zoom_oauth_apps: RuntimeZoomOAuthAppCollection


@dataclass
class RuntimeZoomOAuthConnectionSnapshot:
    object_id: str
    user_id: str
    account_id: str
    client_id: str | None = None
    client_secret: str | None = None
    is_local_recording_token_supported: bool = True
    is_onbehalf_token_supported: bool = False
    credentials: dict[str, Any] | None = None

    def get_credentials(self) -> dict[str, Any]:
        return self.credentials or {}

    def set_credentials(self, credentials: dict[str, Any]) -> None:
        self.credentials = credentials or {}


class RuntimeZoomOAuthConnectionCollection:
    def __init__(self, zoom_oauth_connections: Iterable[RuntimeZoomOAuthConnectionSnapshot | dict[str, Any]]):
        self._zoom_oauth_connections = [
            zoom_oauth_connection
            if isinstance(zoom_oauth_connection, RuntimeZoomOAuthConnectionSnapshot)
            else RuntimeZoomOAuthConnectionSnapshot(
                object_id=zoom_oauth_connection["object_id"],
                user_id=zoom_oauth_connection["user_id"],
                account_id=zoom_oauth_connection["account_id"],
                client_id=zoom_oauth_connection.get("client_id"),
                client_secret=zoom_oauth_connection.get("client_secret"),
                is_local_recording_token_supported=zoom_oauth_connection.get("is_local_recording_token_supported", True),
                is_onbehalf_token_supported=zoom_oauth_connection.get("is_onbehalf_token_supported", False),
                credentials=zoom_oauth_connection.get("credentials") or {},
            )
            for zoom_oauth_connection in zoom_oauth_connections
        ]

    def filter(self, **kwargs):
        items = self._zoom_oauth_connections
        for key, value in kwargs.items():
            if key == "object_id":
                items = [zoom_oauth_connection for zoom_oauth_connection in items if zoom_oauth_connection.object_id == value]
            elif key == "user_id":
                items = [zoom_oauth_connection for zoom_oauth_connection in items if zoom_oauth_connection.user_id == value]
        return RuntimeZoomOAuthConnectionCollection(items)

    def first(self):
        return self._zoom_oauth_connections[0] if self._zoom_oauth_connections else None

    def exists(self):
        return bool(self._zoom_oauth_connections)

    def all(self):
        return list(self._zoom_oauth_connections)

    def __iter__(self):
        return iter(self._zoom_oauth_connections)


@dataclass
class RuntimeZoomMeetingToZoomOAuthConnectionMappingSnapshot:
    meeting_id: str
    zoom_oauth_connection_object_id: str


class RuntimeZoomMeetingToZoomOAuthConnectionMappingCollection:
    def __init__(self, mappings: Iterable[RuntimeZoomMeetingToZoomOAuthConnectionMappingSnapshot | dict[str, Any]]):
        self._mappings = [
            mapping
            if isinstance(mapping, RuntimeZoomMeetingToZoomOAuthConnectionMappingSnapshot)
            else RuntimeZoomMeetingToZoomOAuthConnectionMappingSnapshot(
                meeting_id=str(mapping["meeting_id"]),
                zoom_oauth_connection_object_id=mapping["zoom_oauth_connection_object_id"],
            )
            for mapping in mappings
        ]

    def filter(self, **kwargs):
        items = self._mappings
        for key, value in kwargs.items():
            if key == "meeting_id":
                items = [mapping for mapping in items if str(mapping.meeting_id) == str(value)]
            elif key == "zoom_oauth_connection_object_id":
                items = [mapping for mapping in items if mapping.zoom_oauth_connection_object_id == value]
        return RuntimeZoomMeetingToZoomOAuthConnectionMappingCollection(items)

    def first(self):
        return self._mappings[0] if self._mappings else None

    def exists(self):
        return bool(self._mappings)

    def all(self):
        return list(self._mappings)

    def __iter__(self):
        return iter(self._mappings)


@dataclass
class RuntimeRecordingSnapshot:
    id: int
    object_id: str
    is_default_recording: bool
    state: int
    transcription_state: int
    recording_type_value: int
    transcription_type: int
    transcription_provider: Optional[int]
    file: str | None = None

    @property
    def recording_type(self):
        return self.recording_type_value


class RuntimeRecordingCollection:
    def __init__(self, recordings: Iterable[RuntimeRecordingSnapshot | dict[str, Any]]):
        self._recordings = [
            recording if isinstance(recording, RuntimeRecordingSnapshot) else RuntimeRecordingSnapshot(
                id=recording["id"],
                object_id=recording["object_id"],
                is_default_recording=recording.get("is_default_recording", False),
                state=recording["state"],
                transcription_state=recording["transcription_state"],
                recording_type_value=recording["recording_type"],
                transcription_type=recording["transcription_type"],
                transcription_provider=recording.get("transcription_provider"),
                file=recording.get("file"),
            )
            for recording in recordings
        ]

    def filter(self, **kwargs):
        items = self._recordings
        for key, value in kwargs.items():
            if key == "is_default_recording":
                items = [recording for recording in items if recording.is_default_recording == value]
            elif key == "state":
                items = [recording for recording in items if recording.state == value]
            elif key == "state__in":
                items = [recording for recording in items if recording.state in value]
            elif key == "transcription_state":
                items = [recording for recording in items if recording.transcription_state == value]
            elif key == "transcription_state__in":
                items = [recording for recording in items if recording.transcription_state in value]
        return RuntimeRecordingCollection(items)

    def get(self, **kwargs):
        filtered = self.filter(**kwargs)
        if len(filtered._recordings) != 1:
            raise LookupError(f"Expected exactly one recording, found {len(filtered._recordings)}")
        return filtered._recordings[0]

    def first(self):
        return self._recordings[0] if self._recordings else None

    def count(self):
        return len(self._recordings)

    def all(self):
        return list(self._recordings)

    def __iter__(self):
        return iter(self._recordings)


@dataclass
class RuntimeMediaBlobSnapshot:
    blob: bytes
    duration_ms: int | None = None
    content_type: str | None = None


class RuntimeMediaRequestSnapshot:
    def __init__(self, payload: dict[str, Any], *, bot, runtime_api_client):
        self.id = payload["id"]
        self.state = payload["state"]
        self.media_type = payload["media_type"]
        self.media_url = payload.get("media_url")
        self.loop = payload.get("loop", False)
        self.text_to_speak = payload.get("text_to_speak")
        self.text_to_speech_settings = payload.get("text_to_speech_settings")
        self.created_at = RuntimeBotSnapshot._parse_datetime(payload.get("created_at"))
        self.updated_at = RuntimeBotSnapshot._parse_datetime(payload.get("updated_at"))
        self.media_blob_object_id = payload.get("media_blob_object_id")
        self.media_blob_duration_ms = payload.get("media_blob_duration_ms")
        self.bot = bot
        self._runtime_api_client = runtime_api_client
        self._media_blob_snapshot = None

    @property
    def media_blob(self):
        if self._media_blob_snapshot is not None:
            return self._media_blob_snapshot
        if not self.media_blob_object_id or not self._runtime_api_client:
            return None
        blob = self._runtime_api_client.get_media_blob(self.media_blob_object_id)
        self._media_blob_snapshot = RuntimeMediaBlobSnapshot(
            blob=blob,
            duration_ms=self.media_blob_duration_ms,
        )
        return self._media_blob_snapshot


class RuntimeMediaRequestCollection:
    def __init__(self, requests: Iterable[RuntimeMediaRequestSnapshot | dict[str, Any]], *, bot, runtime_api_client):
        self._requests = [
            request if isinstance(request, RuntimeMediaRequestSnapshot) else RuntimeMediaRequestSnapshot(request, bot=bot, runtime_api_client=runtime_api_client)
            for request in requests
        ]

    def filter(self, **kwargs):
        items = self._requests
        for key, value in kwargs.items():
            if key == "state":
                items = [request for request in items if request.state == value]
            elif key == "state__in":
                items = [request for request in items if request.state in value]
            elif key == "media_type":
                items = [request for request in items if request.media_type == value]
        return RuntimeMediaRequestCollection(items, bot=self._requests[0].bot if self._requests else None, runtime_api_client=self._requests[0]._runtime_api_client if self._requests else None)

    def exclude(self, **kwargs):
        items = self._requests
        for key, value in kwargs.items():
            if key == "id":
                items = [request for request in items if request.id != value]
        return RuntimeMediaRequestCollection(items, bot=self._requests[0].bot if self._requests else None, runtime_api_client=self._requests[0]._runtime_api_client if self._requests else None)

    def order_by(self, field_name: str):
        reverse = field_name.startswith("-")
        field_name = field_name.lstrip("-")
        if field_name != "created_at":
            return self
        return RuntimeMediaRequestCollection(sorted(self._requests, key=lambda request: request.created_at or datetime.min, reverse=reverse), bot=self._requests[0].bot if self._requests else None, runtime_api_client=self._requests[0]._runtime_api_client if self._requests else None)

    def first(self):
        return self._requests[0] if self._requests else None

    def last(self):
        return self._requests[-1] if self._requests else None

    def exists(self):
        return bool(self._requests)

    def all(self):
        return list(self._requests)

    def __iter__(self):
        return iter(self._requests)


class RuntimeChatMessageRequestSnapshot:
    def __init__(self, payload: dict[str, Any]):
        self.id = payload["id"]
        self.state = payload["state"]
        self.to = payload["to"]
        self.to_user_uuid = payload.get("to_user_uuid")
        self.message = payload["message"]
        self.additional_data = payload.get("additional_data") or {}
        self.created_at = RuntimeBotSnapshot._parse_datetime(payload.get("created_at"))
        self.updated_at = RuntimeBotSnapshot._parse_datetime(payload.get("updated_at"))
        self.sent_at_timestamp_ms = payload.get("sent_at_timestamp_ms")
        self.failure_data = payload.get("failure_data")


class RuntimeChatMessageRequestCollection:
    def __init__(self, requests: Iterable[RuntimeChatMessageRequestSnapshot | dict[str, Any]]):
        self._requests = [
            request if isinstance(request, RuntimeChatMessageRequestSnapshot) else RuntimeChatMessageRequestSnapshot(request)
            for request in requests
        ]

    def filter(self, **kwargs):
        items = self._requests
        for key, value in kwargs.items():
            if key == "state":
                items = [request for request in items if request.state == value]
        return RuntimeChatMessageRequestCollection(items)

    def first(self):
        return self._requests[0] if self._requests else None

    def exists(self):
        return bool(self._requests)

    def all(self):
        return list(self._requests)

    def __iter__(self):
        return iter(self._requests)


class RuntimeBotControlSnapshot:
    def __init__(self, payload: dict[str, Any], *, bot, runtime_api_client):
        self.bot_state = payload["bot_state"]
        self.join_at = RuntimeBotSnapshot._parse_datetime(payload.get("join_at"))
        self.runtime_settings = payload.get("runtime_settings") or {}
        self.recording_settings = payload.get("recording_settings") or {}
        self.transcription_settings = payload.get("transcription_settings") or {}
        self.websocket_settings = payload.get("websocket_settings") or {}
        self.voice_agent_settings = payload.get("voice_agent_settings") or {}
        self.media_requests = RuntimeMediaRequestCollection(payload.get("media_requests") or [], bot=bot, runtime_api_client=runtime_api_client)
        self.chat_message_requests = RuntimeChatMessageRequestCollection(payload.get("chat_message_requests") or [])


class RuntimeBotSnapshot:
    def __init__(self, payload: dict[str, Any]):
        if "id" not in payload and "bot" in payload:
            bot_payload = payload.get("bot") or {}
            payload = {
                **bot_payload,
                "project": payload.get("project"),
                "recordings": payload.get("recordings"),
                "last_bot_event": payload.get("last_bot_event"),
                "media_requests": payload.get("media_requests") or bot_payload.get("media_requests"),
                "chat_message_requests": payload.get("chat_message_requests") or bot_payload.get("chat_message_requests"),
            }

        self.id = payload["id"]
        self.object_id = payload["object_id"]
        self.name = payload["name"]
        self.meeting_url = payload["meeting_url"]
        self.meeting_uuid = payload.get("meeting_uuid")
        self.state = payload["state"]
        self.settings = payload.get("settings") or {}
        self.metadata = payload.get("metadata") or {}
        self.join_at = self._parse_datetime(payload.get("join_at"))
        self.deduplication_key = payload.get("deduplication_key")
        self.zoom_rtms_stream_id = payload.get("zoom_rtms_stream_id")
        self.session_type = payload.get("session_type")
        self.created_at = self._parse_datetime(payload.get("created_at"))
        self.updated_at = self._parse_datetime(payload.get("updated_at"))
        self.first_heartbeat_timestamp = payload.get("first_heartbeat_timestamp")
        self.last_heartbeat_timestamp = payload.get("last_heartbeat_timestamp")
        self.last_bot_event_data = payload.get("last_bot_event")

        project_payload = payload.get("project") or {}
        organization_payload = project_payload.get("organization") or {}
        self.project = RuntimeProjectSnapshot(
            id=project_payload["id"],
            object_id=project_payload["object_id"],
            name=project_payload["name"],
            organization=RuntimeOrganizationSnapshot(
                is_async_transcription_enabled=organization_payload.get("is_async_transcription_enabled", True),
            ),
            credentials=RuntimeCredentialCollection(project_payload.get("credentials") or []),
            zoom_oauth_apps=RuntimeZoomOAuthAppCollection(project_payload.get("zoom_oauth_apps") or []),
        )

        self.recordings = RuntimeRecordingCollection(payload.get("recordings") or ([payload["recording"]] if payload.get("recording") else []))
        self.media_requests = payload.get("media_requests") or []
        self.chat_message_requests = payload.get("chat_message_requests") or []

    @staticmethod
    def _parse_datetime(value: Optional[str]):
        if not value:
            return None
        return datetime.fromisoformat(value)

    def refresh_from_db(self):
        return None

    def save(self, *args, **kwargs):
        return None

    def set_heartbeat(self):
        return None

    def __str__(self):
        return f"{self.object_id} - {self.project.name} in {self.meeting_url}"

    def runtime_settings(self):
        return self.settings.get("runtime_settings") or {}

    def bot_duration_seconds(self) -> int:
        if self.first_heartbeat_timestamp is None or self.last_heartbeat_timestamp is None:
            return 0
        if self.last_heartbeat_timestamp < self.first_heartbeat_timestamp:
            return 0
        seconds_active = self.last_heartbeat_timestamp - self.first_heartbeat_timestamp
        if self.last_heartbeat_timestamp == self.first_heartbeat_timestamp:
            seconds_active = 30
        return seconds_active

    def centicredits_consumed(self) -> int:
        if self.first_heartbeat_timestamp is None or self.last_heartbeat_timestamp is None:
            return 0
        if self.last_heartbeat_timestamp < self.first_heartbeat_timestamp:
            return 0
        seconds_active = self.last_heartbeat_timestamp - self.first_heartbeat_timestamp
        if self.last_heartbeat_timestamp == self.first_heartbeat_timestamp:
            seconds_active = 30
        hours_active = seconds_active / 3600
        return int(hours_active * 100 + 0.999999)

    def bot_pod_spec_type(self) -> str:
        kubernetes_settings = self.settings.get("kubernetes_settings") or {}
        custom_bot_pod_spec_type = kubernetes_settings.get("bot_pod_spec_type", None)
        if custom_bot_pod_spec_type:
            return custom_bot_pod_spec_type
        return "default"

    def runtime_region(self, default_region: str | None = None):
        region = self.runtime_settings().get("region")
        return region or default_region

    @property
    def transcription_settings(self):
        return TranscriptionSettings(self.settings.get("transcription_settings"))

    def google_meet_use_bot_login(self):
        return self.settings.get("google_meet_settings", {}).get("use_login", False)

    def google_meet_login_mode_is_always(self):
        return self.settings.get("google_meet_settings", {}).get("login_mode", "always") == "always"

    def teams_use_bot_login(self):
        return self.settings.get("teams_settings", {}).get("use_login", False)

    def teams_login_mode_is_always(self):
        return self.settings.get("teams_settings", {}).get("login_mode", "always") == "always"

    def use_zoom_web_adapter(self):
        return self.settings.get("zoom_settings", {}).get("sdk", "native") == "web"

    def zoom_meeting_settings(self):
        return self.settings.get("zoom_settings", {}).get("meeting_settings", {})

    def rtmp_destination_url(self):
        rtmp_settings = self.settings.get("rtmp_settings")
        if not rtmp_settings:
            return None
        destination_url = rtmp_settings.get("destination_url", "").rstrip("/")
        stream_key = rtmp_settings.get("stream_key", "")
        if not destination_url:
            return None
        return f"{destination_url}/{stream_key}"

    def websocket_audio_url(self):
        websocket_settings = self.settings.get("websocket_settings") or {}
        websocket_audio_settings = websocket_settings.get("audio") or {}
        return websocket_audio_settings.get("url")

    def websocket_audio_sample_rate(self):
        websocket_settings = self.settings.get("websocket_settings") or {}
        websocket_audio_settings = websocket_settings.get("audio") or {}
        return websocket_audio_settings.get("sample_rate", 16000)

    def websocket_per_participant_audio_url(self):
        websocket_settings = self.settings.get("websocket_settings") or {}
        websocket_per_participant_audio_settings = websocket_settings.get("per_participant_audio") or {}
        return websocket_per_participant_audio_settings.get("url")

    def websocket_per_participant_audio_sample_rate(self):
        websocket_settings = self.settings.get("websocket_settings") or {}
        websocket_per_participant_audio_settings = websocket_settings.get("per_participant_audio") or {}
        return websocket_per_participant_audio_settings.get("sample_rate", 16000)

    def voice_agent_url(self):
        voice_agent_settings = self.settings.get("voice_agent_settings", {}) or {}
        return voice_agent_settings.get("url", None) or voice_agent_settings.get("screenshare_url", None)

    def voice_agent_video_output_destination(self):
        voice_agent_settings = self.settings.get("voice_agent_settings", {}) or {}
        if voice_agent_settings.get("url", None):
            return "webcam"
        if voice_agent_settings.get("screenshare_url", None):
            return "screenshare"
        return None

    def should_launch_webpage_streamer(self):
        voice_agent_settings = self.settings.get("voice_agent_settings", {}) or {}
        return voice_agent_settings.get("reserve_resources", False)

    def zoom_tokens_callback_url(self):
        return (self.settings.get("callback_settings", {}) or {}).get("zoom_tokens_url")

    def recording_complete_callback_url(self):
        callback_settings = self.settings.get("callback_settings", {}) or {}
        recording_complete = callback_settings.get("recording_complete") or {}
        return recording_complete.get("url")

    def recording_complete_signing_secret(self):
        callback_settings = self.settings.get("callback_settings", {}) or {}
        recording_complete = callback_settings.get("recording_complete") or {}
        return recording_complete.get("signing_secret")

    def recording_complete_upstream_signing_secret(self):
        callback_settings = self.settings.get("callback_settings", {}) or {}
        recording_complete = callback_settings.get("recording_complete") or {}
        return recording_complete.get("upstream_signing_secret")

    def recording_format(self):
        recording_settings = self.settings.get("recording_settings", {}) or {}
        return recording_settings.get("format", RecordingFormats.MP4)

    def recording_chunk_interval_ms(self):
        recording_settings = self.settings.get("recording_settings", {}) or {}
        return int(recording_settings.get("chunk_interval_ms", 5000))

    def recording_transport(self):
        recording_settings = self.settings.get("recording_settings", {}) or {}
        return recording_settings.get("transport")

    def uses_r2_chunk_recording(self):
        return self.recording_transport() == "r2_chunks"

    def audio_chunk_prefix(self):
        return (self.settings.get("recording_settings", {}) or {}).get("audio_chunk_prefix")

    def audio_raw_path(self):
        return (self.settings.get("recording_settings", {}) or {}).get("audio_raw_path")

    def video_chunk_prefix(self):
        return (self.settings.get("recording_settings", {}) or {}).get("video_chunk_prefix")

    def should_record_sidecar_video(self):
        return bool(self.video_chunk_prefix())

    def uses_muxed_screen_recording_chunks(self):
        return (
            self.uses_r2_chunk_recording()
            and self.recording_format() in (RecordingFormats.MP4, RecordingFormats.WEBM)
            and bool(self.video_chunk_prefix())
        )

    def record_chat_messages_when_paused(self):
        return (self.settings.get("recording_settings", {}) or {}).get("record_chat_messages_when_paused", False)

    def reserve_additional_storage(self):
        return (self.settings.get("recording_settings", {}) or {}).get("reserve_additional_storage", False)

    def record_async_transcription_audio_chunks(self):
        if not self.project.organization.is_async_transcription_enabled:
            return False
        return (self.settings.get("recording_settings", {}) or {}).get("record_async_transcription_audio_chunks", False)

    def record_participant_speech_start_stop_events(self):
        return (self.settings.get("recording_settings", {}) or {}).get("record_participant_speech_start_stop_events", False)

    def recording_type(self):
        recording_format = self.recording_format()
        if recording_format in (RecordingFormats.MP4, RecordingFormats.WEBM):
            return RecordingTypes.AUDIO_AND_VIDEO
        if recording_format == RecordingFormats.MP3:
            return RecordingTypes.AUDIO_ONLY
        if recording_format == RecordingFormats.NONE:
            return RecordingTypes.NO_RECORDING
        raise ValueError(f"Invalid recording format: {recording_format}")

    def runtime_resource_class(self):
        if self.recording_type() == RecordingTypes.NO_RECORDING:
            return "web_av_heavy"
        if self.should_record_sidecar_video():
            return "web_av_standard"
        if self.recording_type() == RecordingTypes.AUDIO_ONLY:
            return "audio_only"
        return "web_av_standard"

    def recording_dimensions(self):
        recording_settings = self.settings.get("recording_settings", {}) or {}
        resolution_value = recording_settings.get("resolution", RecordingResolutions.HD_1080P)
        return RecordingResolutions.get_dimensions(resolution_value)

    def recording_view(self):
        recording_settings = self.settings.get("recording_settings", {}) or {}
        return recording_settings.get("view", RecordingViews.SPEAKER_VIEW)

    def save_resource_snapshots(self):
        return False

    def create_debug_recording(self):
        debug_settings = self.settings.get("debug_settings", {}) or {}
        return debug_settings.get("create_debug_recording", False)

    def external_media_storage_bucket_name(self):
        external_media_storage_settings = self.settings.get("external_media_storage_settings", {}) or {}
        return external_media_storage_settings.get("bucket_name")

    def external_media_storage_recording_file_name(self):
        external_media_storage_settings = self.settings.get("external_media_storage_settings", {}) or {}
        return external_media_storage_settings.get("recording_file_name")

    def zoom_onbehalf_token_zoom_oauth_connection_user_id(self):
        return self.settings.get("zoom_settings", {}).get("onbehalf_token", {}).get("zoom_oauth_connection_user_id")

    def last_bot_event(self):
        return self.last_bot_event_data

    def object_id_prefix(self):
        return "bot_" if self.session_type == SessionTypes.BOT else "app_"

    def ephemeral_container_name(self):
        return f"bot-{self.id}-{self.object_id}".lower().replace("_", "-")

    def gcp_instance_name(self):
        return f"attendee-bot-{self.id}-{self.object_id}".lower().replace("_", "-")[:63]

    def k8s_pod_name(self):
        return f"bot-pod-{self.id}-{self.object_id}".lower().replace("_", "-")

    def k8s_webpage_streamer_service_hostname(self):
        return self.k8s_pod_name() + "-webpage-streamer-service.attendee-webpage-streamer.svc.cluster.local"

    def automatic_leave_settings(self):
        return self.settings.get("automatic_leave_settings", {})

    def zoom_rtms(self):
        return self.settings.get("zoom_rtms", {})

    def recording_complete_callback_url(self):
        callback_settings = self.settings.get("callback_settings", {}) or {}
        recording_complete = callback_settings.get("recording_complete") or {}
        return recording_complete.get("url")

    def recording_complete_signing_secret(self):
        callback_settings = self.settings.get("callback_settings", {}) or {}
        recording_complete = callback_settings.get("recording_complete") or {}
        return recording_complete.get("signing_secret")

    def recording_complete_upstream_signing_secret(self):
        callback_settings = self.settings.get("callback_settings", {}) or {}
        recording_complete = callback_settings.get("recording_complete") or {}
        return recording_complete.get("upstream_signing_secret")
