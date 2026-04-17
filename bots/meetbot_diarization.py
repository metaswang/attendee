from __future__ import annotations

from collections import Counter, defaultdict


def normalize_meetbot_participant_name(raw):
    value = str(raw or "").strip()
    return value or None


def resolve_meetbot_identity_key(participant_user_uuid, participant_uuid):
    participant_user_uuid = str(participant_user_uuid or "").strip()
    if participant_user_uuid:
        return participant_user_uuid
    participant_uuid = str(participant_uuid or "").strip()
    if participant_uuid:
        return participant_uuid
    return None


def build_meetbot_speaker_directory(participants):
    names_by_identity = defaultdict(Counter)
    last_seen_by_identity = defaultdict(dict)
    first_seen_by_identity = {}
    participant_uuid_by_identity = {}
    participant_user_uuid_by_identity = {}

    for item in participants:
        identity = resolve_meetbot_identity_key(
            item.get("participant_user_uuid"),
            item.get("participant_uuid"),
        )
        if not identity:
            continue
        ts = int(item.get("timestamp_ms") or 0)
        if identity not in first_seen_by_identity:
            first_seen_by_identity[identity] = ts
        else:
            first_seen_by_identity[identity] = min(first_seen_by_identity[identity], ts)

        participant_uuid = str(item.get("participant_uuid") or "").strip() or None
        participant_user_uuid = str(item.get("participant_user_uuid") or "").strip() or None
        if participant_uuid:
            participant_uuid_by_identity[identity] = participant_uuid
        if participant_user_uuid:
            participant_user_uuid_by_identity[identity] = participant_user_uuid

        participant_name = normalize_meetbot_participant_name(item.get("participant_name"))
        if participant_name:
            names_by_identity[identity][participant_name] += 1
            last_seen_by_identity[identity][participant_name] = max(
                ts,
                int(last_seen_by_identity[identity].get(participant_name) or 0),
            )

    ordered_identities = sorted(first_seen_by_identity.items(), key=lambda kv: (kv[1], kv[0]))
    display_name_counts = Counter()
    directory = {}
    fallback_count = 0
    collision_count = 0

    for fallback_idx, (identity, first_seen_ms) in enumerate(ordered_identities, start=1):
        chosen_name = None
        label_source = "participant_name"
        name_counter = names_by_identity.get(identity) or Counter()
        if name_counter:
            candidates = sorted(
                name_counter.items(),
                key=lambda kv: (-int(kv[1]), -int(last_seen_by_identity[identity].get(kv[0]) or 0), kv[0]),
            )
            chosen_name = candidates[0][0]
        else:
            fallback_count += 1
            label_source = "fallback"
            chosen_name = f"Participant {fallback_idx:02d}"

        occurrence = int(display_name_counts[chosen_name])
        display_name_counts[chosen_name] += 1
        display_name = chosen_name if occurrence == 0 else f"{chosen_name} ({occurrence + 1})"
        if occurrence > 0:
            collision_count += 1

        directory[identity] = {
            "speaker": display_name,
            "speaker_id": identity,
            "participant_name": chosen_name if label_source == "participant_name" else None,
            "participant_user_uuid": participant_user_uuid_by_identity.get(identity),
            "participant_uuid": participant_uuid_by_identity.get(identity),
            "label_source": label_source,
            "first_seen_ms": int(first_seen_ms),
        }

    return directory, {
        "fallback_speaker_labels": int(fallback_count),
        "speaker_name_collisions": int(collision_count),
    }
