from __future__ import annotations

from bots.meetbot_diarization import (
    build_meetbot_speaker_directory,
    resolve_meetbot_identity_key,
)


def _event(
    *,
    participant_uuid: str,
    participant_name: str | None,
    participant_user_uuid: str | None = None,
    timestamp_ms: int,
) -> dict[str, object]:
    return {
        "participant_uuid": participant_uuid,
        "participant_user_uuid": participant_user_uuid,
        "participant_name": participant_name,
        "timestamp_ms": timestamp_ms,
    }


def test_identity_key_prefers_participant_user_uuid() -> None:
    event = _event(
        participant_uuid="spaces/abc/devices/441",
        participant_user_uuid="user-1",
        participant_name="Alice",
        timestamp_ms=1000,
    )

    assert resolve_meetbot_identity_key(event["participant_user_uuid"], event["participant_uuid"]) == "user-1"


def test_speaker_directory_uses_display_names_and_fallback_labels() -> None:
    directory, stats = build_meetbot_speaker_directory(
        [
            _event(
                participant_uuid="spaces/abc/devices/441",
                participant_user_uuid="user-1",
                participant_name="Alex",
                timestamp_ms=1000,
            ),
            _event(
                participant_uuid="spaces/abc/devices/442",
                participant_name="Alex",
                timestamp_ms=2000,
            ),
            _event(
                participant_uuid="spaces/abc/devices/443",
                participant_name=None,
                timestamp_ms=3000,
            ),
        ]
    )

    assert directory["user-1"]["speaker"] == "Alex"
    assert directory["spaces/abc/devices/442"]["speaker"] == "Alex (2)"
    assert directory["spaces/abc/devices/443"]["speaker"] == "Participant 03"
    assert directory["spaces/abc/devices/443"]["participant_name"] is None
    assert stats["fallback_speaker_labels"] == 1
    assert stats["speaker_name_collisions"] == 1
