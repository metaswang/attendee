import json
import logging
import os
import time
from typing import Callable
from urllib.parse import urlparse

from bots.google_meet_bot_adapter.google_meet_ui_methods import (
    GoogleMeetUIMethods,
)
from bots.web_bot_adapter import WebBotAdapter

logger = logging.getLogger(__name__)

GOOGLE_MEET_HOSTS = frozenset({"meet.google.com", "www.meet.google.com"})
GOOGLE_MEET_END_NAVIGATION_STABILITY_SECONDS = 2.0


class GoogleMeetBotAdapter(WebBotAdapter, GoogleMeetUIMethods):
    def __init__(
        self,
        *args,
        google_meet_closed_captions_language: str | None,
        google_meet_bot_login_is_available: bool,
        google_meet_bot_login_should_be_used: bool,
        create_google_meet_bot_login_session_callback: Callable[[], dict],
        modify_dom_for_video_recording: bool,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.google_meet_closed_captions_language = google_meet_closed_captions_language
        self.google_meet_bot_login_is_available = google_meet_bot_login_is_available
        self.google_meet_bot_login_should_be_used = google_meet_bot_login_should_be_used and google_meet_bot_login_is_available
        self.create_google_meet_bot_login_session_callback = create_google_meet_bot_login_session_callback
        self.google_meet_bot_login_session = None
        self.modify_dom_for_video_recording = modify_dom_for_video_recording
        self.number_of_times_blocked_by_google = 0
        self._meeting_end_navigation_candidate_at = None
        self._meeting_end_navigation_url = None

    def should_retry_joining_meeting_that_requires_login_by_logging_in(self):
        # If we don't have the ability to login, we can't retry
        if not self.google_meet_bot_login_is_available:
            logger.info("Meeting requires login, but Google meet bot login is not available, so we can't retry")
            return False

        # If we already tried to login, we can't retry
        if self.google_meet_bot_login_should_be_used:
            logger.info("Meeting requires login, but we already tried to login, so we can't retry")
            return False

        # Activate the flag that says, we are going to login this time and then retry
        self.google_meet_bot_login_should_be_used = True
        logger.info("Meeting requires login and Google meet bot login is available, so we will retry by logging in")
        return True

    def get_chromedriver_payload_file_name(self):
        return "google_meet_bot_adapter/google_meet_chromedriver_payload.js"

    def get_websocket_port(self):
        return 8765

    def is_sent_video_still_playing(self):
        result = self.driver.execute_script("return window.botOutputManager.isVideoPlaying();")
        logger.info(f"is_sent_video_still_playing result = {result}")
        return result

    def send_video(self, video_url, loop=False):
        logger.info(f"send_video called with video_url = {video_url}, loop = {loop}")
        self.driver.execute_script(f"window.botOutputManager.playVideo({json.dumps(video_url)}, {json.dumps(loop)})")

    def send_chat_message(self, text, to_user_uuid):
        self.driver.execute_script("window?.sendChatMessage(arguments[0]);", text)

    def update_closed_captions_language(self, language):
        if self.google_meet_closed_captions_language == language:
            logger.info(f"In update_closed_captions_language, closed captions language is already set to {language}. Doing nothing.")
            return

        if not language:
            logger.info("In update_closed_captions_language, new language is None. Doing nothing.")
            return

        self.google_meet_closed_captions_language = language
        closed_caption_set_language_result = self.driver.execute_script(
            "return setClosedCaptionsLanguage(arguments[0]);",
            self.google_meet_closed_captions_language,
        )
        if closed_caption_set_language_result:
            logger.info("In update_closed_captions_language, closed captions language set programatically")
        else:
            logger.error("In update_closed_captions_language, failed to set closed captions language programatically")

    def get_staged_bot_join_delay_seconds(self):
        return 5

    def subclass_specific_initial_data_code(self):
        return f"""
            window.googleMeetInitialData = {{
                modifyDomForVideoRecording: {"true" if self.modify_dom_for_video_recording else "false"},
            }}
        """

    def subclass_specific_after_bot_joined_meeting(self):
        self.after_bot_can_record_meeting()

    def _is_target_meeting_url(self, current_url):
        target = urlparse(self.meeting_url or "")
        current = urlparse(current_url or "")
        target_host = (target.hostname or "").lower()
        current_host = (current.hostname or "").lower()
        target_path = target.path.rstrip("/")
        current_path = current.path.rstrip("/")
        return current_host == target_host and current_path == target_path and bool(current_path)

    def _is_google_meet_home_url(self, current_url):
        current = urlparse(current_url or "")
        return (current.hostname or "").lower() in GOOGLE_MEET_HOSTS and current.path.rstrip("/") in {"", "/home"}

    def check_meeting_end_navigation(self):
        """Detect the real Meet end path when Meet navigates to its home page.

        Google Meet may replace the meeting document with ``/home`` without
        rendering the old removal banner. This check only runs after recording
        permission was granted, requires a stable landing page, and emits the
        same terminal adapter message used by the structured Meet signals.
        """
        if self.left_meeting or self.cleaned_up or self._meeting_end_signal_sent:
            return
        if self.recording_permission_granted_at is None or not self.driver:
            return

        try:
            current_url = self.driver.current_url
        except Exception as exc:
            logger.debug("Unable to read Google Meet current URL during lifecycle check: %s", exc)
            return

        if self._is_target_meeting_url(current_url) or not self._is_google_meet_home_url(current_url):
            if self._meeting_end_navigation_candidate_at is not None:
                logger.info("Google Meet navigation end candidate cleared current_url=%s", current_url)
            self._meeting_end_navigation_candidate_at = None
            self._meeting_end_navigation_url = None
            return

        now = time.monotonic()
        if self._meeting_end_navigation_candidate_at is None:
            self._meeting_end_navigation_candidate_at = now
            self._meeting_end_navigation_url = current_url
            logger.info("Google Meet end navigation candidate detected current_url=%s", current_url)
            return

        if now - self._meeting_end_navigation_candidate_at < GOOGLE_MEET_END_NAVIGATION_STABILITY_SECONDS:
            return

        referrer = None
        try:
            referrer = self.driver.execute_script("return document.referrer;")
        except Exception as exc:
            logger.debug("Unable to read Google Meet end navigation referrer: %s", exc)

        self.left_meeting = True
        self.stop_media_sending_for_meeting_end()
        self._send_meeting_ended_message(
            meeting_end_signal="browser_navigation",
            current_url=current_url,
            referrer=referrer,
        )

    def add_subclass_specific_chrome_options(self, options):
        # Performance logging is opt-in through the existing per-bot debug flag.
        # It lets the SSO layer emit a deliberately redacted HTTP summary if a
        # login fails, without collecting network data for ordinary meetings.
        if getattr(self, "should_create_debug_recording", False) and getattr(self, "google_meet_bot_login_is_available", False):
            options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
            self.google_workspace_sso_network_diagnostics_enabled = True
            logger.info("Enabled privacy-safe Google Workspace SSO network diagnostics for this debug bot")

        if not self.google_meet_bot_login_should_be_used:
            return

        # WebBotAdapter creates a new, temporary --user-data-dir for every bot and
        # deletes it at teardown. That already provides the isolation that Guest
        # mode was meant to provide, while a normal ephemeral profile is more
        # compatible with Google's interactive SAML completion flow.
        #
        # Keep the legacy behaviour available as an operator-controlled rollback,
        # rather than coupling every signed-in bot to Chrome Guest mode.
        use_guest_mode = os.getenv("GOOGLE_MEET_SIGNED_IN_BOT_GUEST_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
        if use_guest_mode:
            options.add_argument("--guest")
            logger.info("Signed-in Google Meet bot is using Chrome Guest mode by configuration")
        else:
            logger.info("Signed-in Google Meet bot is using its isolated temporary Chrome profile without Guest mode")

    def subclass_specific_before_driver_close(self):
        if self.google_meet_bot_login_session:
            logger.info("Navigating to the logout page to sign out of the Google account")
            try:
                self.driver.get("https://www.google.com/accounts/logout")
            except Exception as e:
                logger.warning(f"Error navigating to the logout page to sign out of the Google account: {e}")
