from django.urls import path

from . import internal_views

app_name = "bots_internal"

_LEASE_PATHS = [
    (
        "<int:lease_id>/complete",
        internal_views.BotRuntimeLeaseCompletionView.as_view(),
        "bot-runtime-lease-complete",
    ),
    (
        "<int:lease_id>/recording-complete",
        internal_views.BotRuntimeLeaseRecordingCompleteView.as_view(),
        "bot-runtime-lease-recording-complete",
    ),
    (
        "<int:lease_id>/bootstrap",
        internal_views.BotRuntimeLeaseBootstrapView.as_view(),
        "bot-runtime-lease-bootstrap",
    ),
    (
        "<int:lease_id>/control",
        internal_views.BotRuntimeLeaseControlView.as_view(),
        "bot-runtime-lease-control",
    ),
    (
        "<int:lease_id>/source-archive",
        internal_views.BotRuntimeLeaseSourceArchiveView.as_view(),
        "bot-runtime-lease-source-archive",
    ),
    (
        "<int:lease_id>/recordings/<int:recording_id>/file",
        internal_views.BotRuntimeLeaseRecordingFileView.as_view(),
        "bot-runtime-lease-recording-file",
    ),
    (
        "<int:lease_id>/bot-events",
        internal_views.BotRuntimeLeaseBotEventsView.as_view(),
        "bot-runtime-lease-bot-events",
    ),
    (
        "<int:lease_id>/participants/events",
        internal_views.BotRuntimeLeaseParticipantEventsView.as_view(),
        "bot-runtime-lease-participant-events",
    ),
    (
        "<int:lease_id>/chat-messages",
        internal_views.BotRuntimeLeaseChatMessagesView.as_view(),
        "bot-runtime-lease-chat-messages",
    ),
    (
        "<int:lease_id>/captions",
        internal_views.BotRuntimeLeaseCaptionsView.as_view(),
        "bot-runtime-lease-captions",
    ),
    (
        "<int:lease_id>/audio-chunks",
        internal_views.BotRuntimeLeaseAudioChunksView.as_view(),
        "bot-runtime-lease-audio-chunks",
    ),
    (
        "<int:lease_id>/bot-logs",
        internal_views.BotRuntimeLeaseBotLogsView.as_view(),
        "bot-runtime-lease-bot-logs",
    ),
    (
        "<int:lease_id>/resource-snapshots",
        internal_views.BotRuntimeLeaseResourceSnapshotsView.as_view(),
        "bot-runtime-lease-resource-snapshots",
    ),
    (
        "<int:lease_id>/heartbeat",
        internal_views.BotRuntimeLeaseHeartbeatView.as_view(),
        "bot-runtime-lease-heartbeat",
    ),
    (
        "<int:lease_id>/media-requests/<int:request_id>/status",
        internal_views.BotRuntimeLeaseMediaRequestStatusView.as_view(),
        "bot-runtime-lease-media-request-status",
    ),
    (
        "<int:lease_id>/chat-message-requests/<int:request_id>/status",
        internal_views.BotRuntimeLeaseChatMessageRequestStatusView.as_view(),
        "bot-runtime-lease-chat-message-request-status",
    ),
    (
        "<int:lease_id>/media-blobs/<str:object_id>",
        internal_views.BotRuntimeLeaseMediaBlobView.as_view(),
        "bot-runtime-lease-media-blob",
    ),
]


def _build_lease_paths(prefix: str, *, namespaced: bool) -> list:
    return [
        path(f"{prefix}/{suffix}", view, name=name if namespaced else None)
        for suffix, view, name in _LEASE_PATHS
    ]


urlpatterns = [
    *_build_lease_paths("bot-runtime-leases", namespaced=True),
    *_build_lease_paths("attendee-runtime-leases", namespaced=False),
]
