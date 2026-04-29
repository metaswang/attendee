import hashlib
import hmac
import logging

import requests
from django.db import transaction
from django.utils import timezone

from bots.meeting_url_utils import parse_zoom_join_url
from bots.models import Bot, WebhookTriggerTypes, ZoomMeetingToZoomOAuthConnectionMapping, ZoomOAuthConnection, ZoomOAuthConnectionStates
from bots.runtime_snapshot import RuntimeZoomOAuthAppSnapshot
from bots.webhook_payloads import zoom_oauth_connection_webhook_payload
from bots.webhook_utils import trigger_webhook

logger = logging.getLogger(__name__)


class ZoomAPIError(Exception):
    """Custom exception for Zoom API errors."""

    pass


class ZoomAPIAuthenticationError(ZoomAPIError):
    """Custom exception for Zoom API errors."""

    pass


def client_id_and_secret_is_valid(client_id: str, client_secret: str) -> bool:
    """
    Validate Zoom OAuth client credentials without requiring a user via client_credentials grant type

    Returns:
        True if the credentials are valid, False otherwise
    """
    try:
        response = requests.post("https://zoom.us/oauth/token", auth=(client_id, client_secret), data={"grant_type": "client_credentials"}, timeout=30)

        # If we get a 200  the credentials are valid
        if response.status_code == 200:
            return True

        return False
    except Exception:
        logger.exception("Error validating Zoom OAuth client_id and client_secret")
        return False


def _verify_zoom_webhook_signature(body: str, timestamp: str, signature: str, secret: str):
    """Verify the Zoom webhook signature."""
    hmac_hash = hmac.new(secret.encode("utf-8"), f"v0:{timestamp}:{body}".encode("utf-8"), hashlib.sha256).hexdigest()
    expected_signature = f"v0={hmac_hash}"
    return expected_signature == signature


def _get_zoom_oauth_app_object_id(zoom_oauth_app) -> str | None:
    object_id = getattr(zoom_oauth_app, "object_id", None)
    object_id = str(object_id).strip() if object_id is not None else ""
    return object_id or None


def _is_runtime_zoom_oauth_app_snapshot(zoom_oauth_app) -> bool:
    return isinstance(zoom_oauth_app, RuntimeZoomOAuthAppSnapshot)


def _get_runtime_zoom_oauth_connection_by_user_id(zoom_oauth_app, user_id: str):
    connections = getattr(zoom_oauth_app, "zoom_oauth_connections", None)
    if connections is None:
        return None
    if hasattr(connections, "filter"):
        return connections.filter(user_id=user_id).first()
    for connection in connections or []:
        if getattr(connection, "user_id", None) == user_id:
            return connection
    return None


def _get_runtime_zoom_oauth_connection_for_meeting_id(zoom_oauth_app, meeting_id: str):
    mappings = getattr(zoom_oauth_app, "zoom_meeting_to_zoom_oauth_connection_mappings", None)
    if mappings is None:
        return None
    if hasattr(mappings, "filter"):
        mapping = mappings.filter(meeting_id=str(meeting_id)).first()
    else:
        mapping = None
        for item in mappings or []:
            if str(getattr(item, "meeting_id", "")) == str(meeting_id):
                mapping = item
                break
    if not mapping:
        return None
    connection_object_id = getattr(mapping, "zoom_oauth_connection_object_id", None)
    if connection_object_id:
        connections = getattr(zoom_oauth_app, "zoom_oauth_connections", None)
        if connections is not None and hasattr(connections, "filter"):
            return connections.filter(object_id=connection_object_id).first()
        if connections is not None:
            for connection in connections or []:
                if getattr(connection, "object_id", None) == connection_object_id:
                    return connection
    return getattr(mapping, "zoom_oauth_connection", None)


def _get_cached_onbehalf_token(bot: Bot, *, meeting_id: str) -> str | None:
    cache = getattr(bot, "_zoom_onbehalf_token_cache", None)
    if not isinstance(cache, dict):
        return None
    return cache.get(str(meeting_id))


def _set_cached_onbehalf_token(bot: Bot, *, meeting_id: str, token: str) -> str:
    cache = getattr(bot, "_zoom_onbehalf_token_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(bot, "_zoom_onbehalf_token_cache", cache)
    cache[str(meeting_id)] = token
    return token


def _log_onbehalf_token_unavailable(*, connection_object_id: str | None, user_id: str, reason: str):
    logger.warning("Zoom onbehalf token unavailable connection_id=%s user_id=%s reason=%s", connection_object_id, user_id, reason)


def compute_zoom_webhook_validation_response(plain_token: str, secret_token: str) -> dict:
    """
    Compute the response for a Zoom webhook validation request.

    Zoom sends a challenge-response validation request when setting up a webhook endpoint.
    This function creates the required HMAC SHA-256 hash of the plainToken using the
    webhook secret token.

    Args:
        plain_token: The plainToken value from the webhook request payload
        secret_token: The webhook secret token configured in Zoom

    Returns:
        dict: A dictionary containing 'plainToken' and 'encryptedToken' keys
        Example: {
            "plainToken": "qgg8vlvZRS6UYooatFL8Aw",
            "encryptedToken": "23a89b634c017e5364a1c8d9c8ea909b60dd5599e2bb04bb1558d9c3a121faa5"
        }
    """
    # Create HMAC SHA-256 hash with secret_token as salt and plain_token as the string to hash
    encrypted_token = hmac.new(secret_token.encode("utf-8"), plain_token.encode("utf-8"), hashlib.sha256).hexdigest()

    return {"plainToken": plain_token, "encryptedToken": encrypted_token}


def _raise_if_error_is_authentication_error(e: requests.RequestException):
    error_code = e.response.json().get("error")
    if error_code == "invalid_grant" or error_code == "invalid_client":
        raise ZoomAPIAuthenticationError(f"Zoom Authentication error: {e.response.json()}")

    return


def _get_access_token(zoom_oauth_connection) -> str:
    """
    Exchange the stored refresh token for a new access token.
    Zoom returns a new refresh_token on each successful refresh.
    Persist it so we don't lose the chain.
    """
    credentials = zoom_oauth_connection.get_credentials()
    if not credentials:
        raise ZoomAPIAuthenticationError("No credentials found for zoom oauth connection")

    refresh_token = credentials.get("refresh_token")
    zoom_oauth_app = getattr(zoom_oauth_connection, "zoom_oauth_app", None)
    client_id = getattr(zoom_oauth_app, "client_id", None) or getattr(zoom_oauth_connection, "client_id", None)
    client_secret = getattr(zoom_oauth_app, "client_secret", None) or getattr(zoom_oauth_connection, "client_secret", None)
    if not refresh_token or not client_id or not client_secret:
        raise ZoomAPIAuthenticationError("Missing refresh_token or client_secret")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }

    try:
        response = requests.post("https://zoom.us/oauth/token", data=data, timeout=30)
        response.raise_for_status()
        token_data = response.json()

        access_token = token_data.get("access_token")
        if not access_token:
            raise ZoomAPIError("No access_token in refresh response")

        # IMPORTANT: Zoom rotates refresh tokens. Save the new one if provided.
        new_refresh = token_data.get("refresh_token")
        if new_refresh and new_refresh != refresh_token:
            credentials["refresh_token"] = new_refresh
            zoom_oauth_connection.set_credentials(credentials)
            logger.info("Stored rotated Zoom refresh_token for zoom oauth connection %s", zoom_oauth_connection.object_id)

        return access_token

    except requests.RequestException as e:
        _raise_if_error_is_authentication_error(e)
        raise ZoomAPIError("Failed to refresh Zoom access token")


def _make_zoom_api_request(url: str, access_token: str, params: dict) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}

    req = requests.Request("GET", url, headers=headers, params=params).prepare()
    try:
        # Send the request
        with requests.Session() as s:
            resp = s.send(req, timeout=25)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        _raise_if_error_is_authentication_error(e)
        logger.exception("Failed to make Zoom API request")
        raise e


def _get_zoom_personal_meeting_id(access_token: str) -> str:
    base_url = "https://api.zoom.us/v2/users/me"
    response_data = _make_zoom_api_request(base_url, access_token, {})
    return response_data.get("pmi")


def _get_local_recording_token(meeting_id: str, access_token: str) -> str:
    base_url = f"https://api.zoom.us/v2/meetings/{meeting_id}/jointoken/local_recording?bypass_waiting_room=true"

    response_data = _make_zoom_api_request(base_url, access_token, {})
    return response_data.get("token")


def _get_onbehalf_token(meeting_id: str, access_token: str) -> str:
    base_url = f"https://api.zoom.us/v2/users/me/token?type=onbehalf&meeting_id={meeting_id}"
    response_data = _make_zoom_api_request(base_url, access_token, {})
    return response_data.get("token")


def _get_zoom_meetings(access_token: str) -> list[dict]:
    base_url = "https://api.zoom.us/v2/users/me/meetings"
    base_params = {
        "page_size": 300,
    }

    all_meetings = []
    next_page_token = None

    while True:
        params = dict(base_params)  # copy base params
        if next_page_token:
            params["next_page_token"] = next_page_token

        logger.info("Fetching Zoom meetings")
        response_data = _make_zoom_api_request(base_url, access_token, params)

        meetings = response_data.get("meetings", [])
        all_meetings.extend(meetings)

        next_page_token = response_data.get("next_page_token")
        if not next_page_token:
            break

    return all_meetings


def _upsert_zoom_meeting_to_zoom_oauth_connection_mapping(zoom_meeting_ids: list[int], zoom_oauth_connection: ZoomOAuthConnection):
    zoom_oauth_app = zoom_oauth_connection.zoom_oauth_app
    num_updated = 0
    num_created = 0

    # Iterate over the zoom meetings and upsert the zoom meeting to zoom oauth connection mapping
    for zoom_meeting_id in zoom_meeting_ids:
        if not zoom_meeting_id:
        logger.warning("Zoom meeting id is None for zoom oauth connection %s", zoom_oauth_connection.id)
            continue

        zoom_meeting_to_zoom_oauth_connection_mapping, created = ZoomMeetingToZoomOAuthConnectionMapping.objects.update_or_create(
            zoom_oauth_app=zoom_oauth_app,
            meeting_id=zoom_meeting_id,
            defaults={"zoom_oauth_connection": zoom_oauth_connection},
        )
        # If one already exists, but it has a different zoom_oauth_connection_id, update it
        if not created and zoom_meeting_to_zoom_oauth_connection_mapping.zoom_oauth_connection_id != zoom_oauth_connection.id:
            zoom_meeting_to_zoom_oauth_connection_mapping.zoom_oauth_connection = zoom_oauth_connection
            zoom_meeting_to_zoom_oauth_connection_mapping.save()
            num_updated += 1
        if created:
            num_created += 1

    logger.info(
        "Upserted %s zoom meeting ids to zoom oauth connection mappings and created %s new ones for zoom oauth connection %s",
        num_updated,
        num_created,
        zoom_oauth_connection.id,
    )


def _handle_zoom_api_authentication_error(zoom_oauth_connection: ZoomOAuthConnection, e: ZoomAPIAuthenticationError):
    if zoom_oauth_connection.state == ZoomOAuthConnectionStates.DISCONNECTED:
        logger.info(
            "Zoom OAuth connection %s is already in state DISCONNECTED, skipping authentication error handling",
            zoom_oauth_connection.id,
        )
        return

    # Update zoom oauth connection state to indicate failure
    with transaction.atomic():
        zoom_oauth_connection.state = ZoomOAuthConnectionStates.DISCONNECTED
        zoom_oauth_connection.connection_failure_data = {
            "error": "Zoom OAuth authentication error",
            "timestamp": timezone.now().isoformat(),
        }
        zoom_oauth_connection.save()

    logger.exception("Zoom OAuth connection sync failed with ZoomAPIAuthenticationError for %s", zoom_oauth_connection.id)

    # Create webhook event
    trigger_webhook(
        webhook_trigger_type=WebhookTriggerTypes.ZOOM_OAUTH_CONNECTION_STATE_CHANGE,
        zoom_oauth_connection=zoom_oauth_connection,
        payload=zoom_oauth_connection_webhook_payload(zoom_oauth_connection),
    )


def get_local_recording_token_via_zoom_oauth_app(bot: Bot) -> str | None:
    project = bot.project
    meeting_url = bot.meeting_url
    zoom_oauth_app = project.zoom_oauth_apps.first()
    if not zoom_oauth_app:
        return None

    zoom_oauth_app_object_id = _get_zoom_oauth_app_object_id(zoom_oauth_app)
    if not zoom_oauth_app_object_id:
        logger.info("Zoom oauth app missing object_id for local recording token lookup")
        return None

    meeting_id, password = parse_zoom_join_url(meeting_url)
    if not meeting_id:
        logger.info("No meeting id found in join url")
        return None

    if _is_runtime_zoom_oauth_app_snapshot(zoom_oauth_app):
        runtime_zoom_oauth_connection = _get_runtime_zoom_oauth_connection_for_meeting_id(zoom_oauth_app, str(meeting_id))
        if runtime_zoom_oauth_connection is not None:
            if not getattr(runtime_zoom_oauth_connection, "is_local_recording_token_supported", True):
                logger.info("Runtime zoom oauth connection does not support local recording tokens, skipping")
                return None
            try:
                access_token = _get_access_token(runtime_zoom_oauth_connection)
                return _get_local_recording_token(meeting_id, access_token)
            except Exception:
                logger.exception("Failed to get local recording token via runtime zoom oauth app")
                return None
        logger.info("No runtime zoom oauth mapping found for meeting id in zoom oauth app")
        return None

    mapping_for_meeting_id = ZoomMeetingToZoomOAuthConnectionMapping.objects.filter(
        zoom_oauth_app__object_id=zoom_oauth_app_object_id,
        meeting_id=str(meeting_id),
    ).first()

    if not mapping_for_meeting_id:
        logger.info("No mapping found for meeting id in zoom oauth app")
        return None

    zoom_oauth_connection = mapping_for_meeting_id.zoom_oauth_connection

    if not zoom_oauth_connection.is_local_recording_token_supported:
        logger.info("Zoom oauth connection does not support local recording tokens, skipping")
        return None

    try:
        access_token = _get_access_token(zoom_oauth_connection)
        local_recording_token = _get_local_recording_token(meeting_id, access_token)
        return local_recording_token

    except ZoomAPIAuthenticationError as e:
        _handle_zoom_api_authentication_error(zoom_oauth_connection, e)
        logger.exception("Failed to get local recording token via zoom oauth app; authentication error")
        return None

    except Exception:
        logger.exception("Failed to get local recording token via zoom oauth app")
        return None


def get_onbehalf_token_via_zoom_oauth_app(bot: Bot) -> str | None:
    user_id_for_onbehalf_token = bot.zoom_onbehalf_token_zoom_oauth_connection_user_id()
    if not user_id_for_onbehalf_token:
        return None

    project = bot.project
    zoom_oauth_app = project.zoom_oauth_apps.first()
    if not zoom_oauth_app:
        return None

    zoom_oauth_app_object_id = _get_zoom_oauth_app_object_id(zoom_oauth_app)
    if not zoom_oauth_app_object_id:
        logger.info("Zoom oauth app missing object_id for onbehalf token lookup user_id=%s", user_id_for_onbehalf_token)
        return None

    meeting_url = bot.meeting_url
    meeting_id, password = parse_zoom_join_url(meeting_url)
    if not meeting_id:
        logger.info("No meeting id found in join url")
        return None
    cached_token = _get_cached_onbehalf_token(bot, meeting_id=meeting_id)
    if cached_token:
        return cached_token

    if _is_runtime_zoom_oauth_app_snapshot(zoom_oauth_app):
        runtime_zoom_oauth_connection = _get_runtime_zoom_oauth_connection_by_user_id(zoom_oauth_app, user_id_for_onbehalf_token)
        if runtime_zoom_oauth_connection is not None:
            if not getattr(runtime_zoom_oauth_connection, "is_onbehalf_token_supported", False):
                _log_onbehalf_token_unavailable(
                    connection_object_id=getattr(runtime_zoom_oauth_connection, "object_id", None),
                    user_id=user_id_for_onbehalf_token,
                    reason="onbehalf_not_supported",
                )
                return None

            try:
                access_token = _get_access_token(runtime_zoom_oauth_connection)
                return _set_cached_onbehalf_token(
                    bot,
                    meeting_id=meeting_id,
                    token=_get_onbehalf_token(meeting_id, access_token),
                )
            except Exception:
                logger.exception("Failed to get onbehalf token via runtime zoom oauth app")
                return None
        logger.info("No runtime zoom oauth connection found in zoom oauth app")
        return None

    zoom_oauth_connection = ZoomOAuthConnection.objects.filter(
        zoom_oauth_app__object_id=zoom_oauth_app_object_id,
        user_id=user_id_for_onbehalf_token,
    ).first()
    if not zoom_oauth_connection:
        return None

    if not zoom_oauth_connection.is_onbehalf_token_supported:
        _log_onbehalf_token_unavailable(
            connection_object_id=zoom_oauth_connection.object_id,
            user_id=user_id_for_onbehalf_token,
            reason="onbehalf_not_supported",
        )
        return None

    try:
        access_token = _get_access_token(zoom_oauth_connection)
        onbehalf_token = _get_onbehalf_token(meeting_id, access_token)
        return _set_cached_onbehalf_token(bot, meeting_id=meeting_id, token=onbehalf_token)

    except ZoomAPIAuthenticationError as e:
        _handle_zoom_api_authentication_error(zoom_oauth_connection, e)
        logger.exception("Failed to get onbehalf token via zoom oauth app; authentication error")
        return None

    except Exception:
        logger.exception("Failed to get onbehalf token via zoom oauth app")
        return None


def get_zoom_tokens_via_zoom_oauth_app(bot: Bot) -> dict | None:
    onbehalf_token = get_onbehalf_token_via_zoom_oauth_app(bot)
    local_recording_token = get_local_recording_token_via_zoom_oauth_app(bot)

    return {
        "zak_token": None,
        "join_token": None,
        "app_privilege_token": local_recording_token,
        "onbehalf_token": onbehalf_token,
    }
