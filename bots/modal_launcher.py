import logging
import os
from typing import Any
from urllib.parse import urlparse

from bots.models import Bot, Credentials

logger = logging.getLogger(__name__)

SUPPORTED_EXTERNAL_MEDIA_STORAGE_SCHEMES = {"s3", "r2"}


def parse_recording_upload_uri(recording_upload_uri: str | None) -> tuple[str, str | None, str] | None:
    if not recording_upload_uri:
        return None

    parsed = urlparse(recording_upload_uri)
    scheme = (parsed.scheme or "").lower()
    if scheme not in SUPPORTED_EXTERNAL_MEDIA_STORAGE_SCHEMES:
        raise ValueError(f"Unsupported recording upload uri scheme: {scheme}. Expected one of {sorted(SUPPORTED_EXTERNAL_MEDIA_STORAGE_SCHEMES)}")

    if not parsed.netloc:
        raise ValueError("recording_upload_uri must include a bucket name")

    key = parsed.path.lstrip("/") or None
    return parsed.netloc, key, scheme


def build_recording_upload_uri_from_bot(bot: Bot) -> str | None:
    external_media_storage_settings = bot.settings.get("external_media_storage_settings") or {}
    bucket_name = bot.external_media_storage_bucket_name()
    if not bucket_name:
        return None

    file_name = bot.external_media_storage_recording_file_name()
    scheme = external_media_storage_settings.get("storage_scheme", "s3")
    if file_name:
        return f"{scheme}://{bucket_name}/{file_name}"
    return f"{scheme}://{bucket_name}"


def modal_external_media_storage_credentials_available(scheme: str | None = None) -> bool:
    if scheme in (None, "r2"):
        if os.getenv("R2__ACCESS_KEY_ID") and os.getenv("R2__SECRET_ACCESS_KEY") and (os.getenv("R2__ENDPOINT") or os.getenv("R2__API_BASE_URL")):
            return True

    if scheme in (None, "s3"):
        if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY") and os.getenv("AWS_DEFAULT_REGION"):
            return True

    return False


def get_external_media_storage_credentials_for_scheme(scheme: str) -> dict[str, str | None]:
    if scheme == "r2":
        if not modal_external_media_storage_credentials_available("r2"):
            raise ValueError("R2 credentials are not configured for Modal")

        return {
            "endpoint_url": os.getenv("R2__ENDPOINT") or os.getenv("R2__API_BASE_URL"),
            "region_name": os.getenv("R2__REGION", "auto"),
            "access_key_id": os.getenv("R2__ACCESS_KEY_ID"),
            "access_key_secret": os.getenv("R2__SECRET_ACCESS_KEY"),
        }

    if scheme == "s3":
        if not modal_external_media_storage_credentials_available("s3"):
            raise ValueError("S3 credentials are not configured for Modal")

        return {
            "endpoint_url": os.getenv("AWS_S3_ENDPOINT_URL"),
            "region_name": os.getenv("AWS_DEFAULT_REGION"),
            "access_key_id": os.getenv("AWS_ACCESS_KEY_ID"),
            "access_key_secret": os.getenv("AWS_SECRET_ACCESS_KEY"),
        }

    raise ValueError(f"Unsupported external media storage scheme: {scheme}")


def upsert_external_media_storage_credentials_for_bot(bot: Bot, scheme: str) -> None:
    credentials_dict = get_external_media_storage_credentials_for_scheme(scheme)
    credentials, _ = Credentials.objects.get_or_create(
        project=bot.project,
        credential_type=Credentials.CredentialTypes.EXTERNAL_MEDIA_STORAGE,
    )
    credentials.set_credentials(credentials_dict)


def apply_modal_runtime_overrides(
    bot_id: int,
    *,
    bot_name: str | None = None,
    meeting_url: str | None = None,
    recording_upload_uri: str | None = None,
    other_params: dict[str, Any] | None = None,
) -> Bot:
    bot = Bot.objects.get(id=bot_id)
    updated_fields = set()

    if bot_name and bot.name != bot_name:
        bot.name = bot_name
        updated_fields.add("name")

    if meeting_url and bot.meeting_url != meeting_url:
        bot.meeting_url = meeting_url
        updated_fields.add("meeting_url")

    settings = dict(bot.settings or {})
    recording_settings = dict(settings.get("recording_settings") or {})
    automatic_leave_settings = dict(settings.get("automatic_leave_settings") or {})

    if other_params:
        recording_format = other_params.get("recording_format")
        if recording_format:
            recording_settings["format"] = recording_format

        recording_resolution = other_params.get("recording_resolution")
        if recording_resolution:
            recording_settings["resolution"] = recording_resolution

        max_uptime_seconds = other_params.get("max_uptime_seconds")
        if max_uptime_seconds is not None:
            automatic_leave_settings["max_uptime_seconds"] = max_uptime_seconds

    parsed_upload = parse_recording_upload_uri(recording_upload_uri)
    if parsed_upload:
        bucket_name, recording_file_name, scheme = parsed_upload
        external_media_storage_settings = dict(settings.get("external_media_storage_settings") or {})
        external_media_storage_settings["bucket_name"] = bucket_name
        external_media_storage_settings["storage_scheme"] = scheme
        if recording_file_name:
            external_media_storage_settings["recording_file_name"] = recording_file_name
        settings["external_media_storage_settings"] = external_media_storage_settings
        upsert_external_media_storage_credentials_for_bot(bot, scheme)

    settings["recording_settings"] = recording_settings
    settings["automatic_leave_settings"] = automatic_leave_settings
    if settings != (bot.settings or {}):
        bot.settings = settings
        updated_fields.add("settings")

    metadata = dict(bot.metadata or {})
    metadata["modal_runtime"] = {
        "recording_upload_uri": recording_upload_uri,
        "other_params": other_params or {},
    }
    bot.metadata = metadata
    updated_fields.add("metadata")

    if updated_fields:
        bot.save(update_fields=sorted(updated_fields | {"updated_at"}))

    return bot


def launch_bot_via_modal(bot: Bot) -> str:
    import modal

    app_name = os.getenv("MODAL__APP_NAME", "attendee-bot-runner")
    function_name = os.getenv("MODAL__FUNCTION_NAME", "run_bot_on_modal")
    function = modal.Function.from_name(app_name, function_name)

    other_params = {
        "recording_format": bot.recording_format(),
        "recording_resolution": (bot.settings.get("recording_settings") or {}).get("resolution", os.getenv("MODAL_BOT__RECORDING_RESOLUTION", "1080p")),
        "max_uptime_seconds": bot.automatic_leave_settings().get(
            "max_uptime_seconds",
            int(os.getenv("MODAL_BOT__MAX_UPTIME_SECONDS", "10800")),
        ),
    }
    call = function.spawn(
        bot_id=bot.id,
        bot_name=bot.name,
        meeting_url=bot.meeting_url,
        recording_upload_uri=build_recording_upload_uri_from_bot(bot),
        other_params=other_params,
    )
    call.hydrate()
    call_id = call.object_id

    metadata = dict(bot.metadata or {})
    metadata["modal_function_call_id"] = call_id
    metadata["modal_function_name"] = function_name
    metadata["modal_app_name"] = app_name
    bot.metadata = metadata
    bot.save(update_fields=["metadata", "updated_at"])

    logger.info("Bot %s (%s) launched via Modal function call %s", bot.object_id, bot.id, call_id)
    return call_id


def cancel_modal_bot(function_call_id: str | None) -> None:
    if not function_call_id:
        return

    import modal

    function_call = modal.FunctionCall.from_id(function_call_id)
    function_call.cancel()
