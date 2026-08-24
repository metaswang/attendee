import json
import logging
import os
import re
import time
from urllib.parse import parse_qsl, urljoin, urlparse

import requests
from selenium.common.exceptions import ElementNotInteractableException, NoSuchElementException, StaleElementReferenceException, TimeoutException, WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from bots.bot_sso_utils import get_google_meet_set_cookie_url, get_google_meet_sign_in_url
from bots.models import RecordingViews
from bots.web_bot_adapter.ui_methods import UiCouldNotClickElementException, UiCouldNotJoinMeetingWaitingForHostException, UiCouldNotJoinMeetingWaitingRoomTimeoutException, UiCouldNotLocateElementException, UiLoginAttemptFailedException, UiLoginRequiredException, UiMeetingNotFoundException, UiRequestToJoinDeniedException, UiRetryableExpectedException

logger = logging.getLogger(__name__)


class UiGoogleBlockingUsException(UiRetryableExpectedException):
    def __init__(self, message, step=None, inner_exception=None):
        super().__init__(message, step, inner_exception)


class GoogleMeetUIMethods:
    # This spelling is Google's documented SSO identity-confirmation bypass.
    # In particular, ``AllowedDomains`` is one token: a hyphenated variant is
    # silently ignored by Google and leaves an ephemeral bot profile stuck on
    # the interactive confirmation page after ACS.
    _GOOGLE_ALLOWED_DOMAINS_HEADER = "X-GoogApps-AllowedDomains"
    _GOOGLE_SSO_REDIRECT_HOSTS = frozenset({"www.google.com", "accounts.google.com"})
    _GOOGLE_SSO_MAX_REDIRECTS = 5
    _GOOGLE_SSO_HANDOFF_TIMEOUT_SECONDS = 60
    _GOOGLE_SSO_MEET_STABILITY_SECONDS = 2.0
    _GOOGLE_SSO_INTERSTITIAL_RETRY_SECONDS = 2.0
    _GOOGLE_SSO_DIAGNOSTIC_HOSTS = frozenset({"www.google.com", "accounts.google.com", "mail.google.com"})
    _GOOGLE_SSO_NETWORK_DIAGNOSTIC_LIMIT = 20
    _COOKIE_ATTRIBUTE_NAMES = frozenset({"domain", "expires", "httponly", "max-age", "partitioned", "path", "priority", "samesite", "secure"})
    _GOOGLE_SSO_ENTRYPOINT_MEET = "meet"
    _GOOGLE_SSO_ENTRYPOINT_DOMAIN_SERVICE_LOGIN = "domain_service_login"
    # The slash root redirects to the public Meet landing page when the
    # profile is unauthenticated; ``/home`` jumps straight into the app and
    # can land on a generic account identifier form without a Meet SSO link.
    _GOOGLE_MEET_HOME_URL = "https://meet.google.com/"
    _GOOGLE_MEET_SSO_SESSION_COOKIE = "google_meet_sign_in_session_id"
    _GOOGLE_MEET_HOSTS = frozenset({"meet.google.com", "www.meet.google.com"})
    _GOOGLE_MEET_GREEN_ROOM_LOADING_SELECTOR = "div[jsname='OQ2Y6']"
    _GOOGLE_MEET_GREEN_ROOM_LOADING_MARKERS = (
        "getting ready",
        "you'll be able to join in just a moment",
    )
    _GOOGLE_PRODUCT_SIGN_IN_LINK_SELECTORS = (
        "a[href*='accounts.google.com/ServiceLogin']",
        "a[href*='accounts.google.com/v3/signin']",
        "a[href*='www.google.com/ServiceLogin']",
        "a[href*='www.google.com/v3/signin']",
    )
    _GOOGLE_IDENTIFIER_CONTINUE_SELECTORS = (
        "#identifierNext",
        "button[jsname='LgbsSe']",
        "[role='button'][jsname='LgbsSe']",
        "button[type='submit']",
        "input[type='submit']",
    )
    _GOOGLE_SAML_CONFIRMATION_PATH_SUFFIX = "/samlconfirmaccount"
    _GOOGLE_SSO_CONTINUE_SELECTORS = (
        "#confirm",
        "button[jsname='LgbsSe']",
        "[role='button'][jsname='LgbsSe']",
        "button[type='submit']",
        "input[type='submit']",
    )
    _GOOGLE_SSO_SKIP_SELECTORS = (
        "#skip",
        "[data-action='skip']",
        "[data-value='skip']",
        "[id*='skip']",
        "[href*='skip']",
    )
    # These strings are a fallback only. Google normally exposes the stable
    # jsname/data attributes above; the fallback keeps the flow usable when a
    # localized account page omits them. It is intentionally scoped to the
    # identity-confirmation/passkey pages, never used as a general Meet locator.
    _GOOGLE_SSO_CONTINUE_LABELS = frozenset(
        {
            "continue",
            "weiter",
            "continuer",
            "continuar",
            "continua",
            "avançar",
            "doorgaan",
            "продолжить",
            "继续",
            "继续操作",
            "下一步",
        }
    )
    _GOOGLE_SSO_SKIP_LABELS = frozenset(
        {
            "not now",
            "jetzt nicht",
            "pas maintenant",
            "ahora no",
            "agora não",
            "non ora",
            "überspringen",
            "skip",
            "暂时不要",
            "暂不",
            "以后再说",
            "稍后",
        }
    )
    _GOOGLE_SSO_IDENTITY_MARKERS = (
        "verify it's you",
        "verify it’s you",
        "verify that it's you",
        "verify that it’s you",
        "verify your identity",
        "bestätige, dass du es bist",
        "bestätigen sie, dass sie es sind",
        "vérifiez que c'est vous",
        "verifica que eres tú",
        "验证是你",
    )
    _GOOGLE_SSO_PASSKEY_MARKERS = (
        "passkey",
        "pass-key",
        "webauthn",
        "simplify your sign-in",
        "simplify your sign in",
        "set up a passkey",
        "use a passkey",
    )

    @staticmethod
    def _normalized_workspace_domain(value):
        """Return a header-safe Workspace domain, or ``None`` when invalid."""
        if not isinstance(value, str):
            return None

        domain = value.strip().lower().rstrip(".")
        if not domain or any(character in domain for character in (",", "\r", "\n", "/", "\\", ":")):
            return None
        return domain

    def configure_google_workspace_sso_allowed_domains_header(self) -> bool:
        """Restrict this browser's Google sign-in flow to the assigned Workspace domain.

        Google displays an identity-confirmation interstitial once per account and
        Chrome device for SAML SSO sign-ins. Bot Chrome instances intentionally run
        in isolated ephemeral profiles, so they do not retain that per-device approval.
        Google's documented ``X-GoogApps-AllowedDomains`` mechanism both limits
        the sign-in to the verified Workspace domain and lets a managed caller opt
        out of that otherwise interactive interstitial.

        The assigned domain comes from the server-side SSO session. Operators can
        disable the behavior, or require an explicit allowlist, with environment
        variables without baking a tenant-specific domain into the bot runtime.
        """
        # Every bot gets a new Chrome profile, so Google's otherwise once-per-
        # device SAML identity confirmation is not suitable for this unattended
        # flow. Google documents this header as the organization-level way to
        # suppress that confirmation. The value is still restricted to the
        # server-validated Workspace domain and operators retain a fail-closed
        # rollback switch.
        # This header is an opt-in experiment/rollback only. In the current
        # Google account flow it can contaminate the post-identifier SAML
        # navigation and Meet's cross-origin assets, so production defaults to
        # the normal browser flow with no extra header.
        enabled = os.getenv("GOOGLE_MEET_SSO_ALLOWED_DOMAINS_HEADER_ENABLED", "false").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            logger.info("Google Workspace allowed-domains header is disabled by configuration")
            return False

        session = self.google_meet_bot_login_session or {}
        login_domain = self._normalized_workspace_domain(session.get("login_domain"))
        if login_domain is None:
            logger.warning("Skipping Google Workspace allowed-domains header because the allocated login domain is invalid")
            return False

        configured_domains = os.getenv("GOOGLE_MEET_SSO_ALLOWED_DOMAINS", "").strip()
        if configured_domains:
            allowed_domains = []
            for configured_domain in configured_domains.split(","):
                normalized_domain = self._normalized_workspace_domain(configured_domain)
                if normalized_domain is None:
                    logger.warning("Skipping Google Workspace allowed-domains header because its configured allowlist is invalid")
                    return False
                if normalized_domain not in allowed_domains:
                    allowed_domains.append(normalized_domain)
            if login_domain not in allowed_domains:
                logger.warning("Skipping Google Workspace allowed-domains header because the allocated login domain is not allowlisted")
                return False
        else:
            # The SSO allocator has already verified this domain against an active
            # tenant, so the narrowest safe default is the account's own domain.
            allowed_domains = [login_domain]

        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
            self.driver.execute_cdp_cmd(
                "Network.setExtraHTTPHeaders",
                {"headers": {self._GOOGLE_ALLOWED_DOMAINS_HEADER: ",".join(allowed_domains)}},
            )
        except Exception as exc:
            raise UiLoginAttemptFailedException(
                "Could not configure the Google Workspace SSO allowed-domains header",
                "configure_google_workspace_sso_allowed_domains_header",
                exc,
            ) from exc

        logger.info("Configured Google Workspace allowed-domains header for the allocated SSO login")
        return True

    def _safe_browser_location_for_log(self) -> str:
        """Return only the origin and path, never query parameters or fragments."""
        try:
            location = urlparse(self.driver.current_url)
            if location.scheme and location.netloc:
                return f"{location.scheme}://{location.netloc}{location.path}"
        except Exception:
            pass
        return "<unavailable>"

    @staticmethod
    def _safe_parameter_names(raw_keys):
        names = []
        for key in raw_keys:
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key):
                names.append(key)
            else:
                names.append("<redacted>")
        return sorted(set(names))[:12]

    @classmethod
    def _safe_network_url_metadata(cls, value):
        """Return URL metadata suitable for logs, without query values or fragments."""
        try:
            parsed = urlparse(str(value or ""))
        except Exception:
            return None

        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None

        try:
            raw_query_keys = [key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)]
        except ValueError:
            raw_query_keys = []

        return {
            "host": parsed.hostname.lower(),
            "path": parsed.path or "/",
            "query_key_count": len(raw_query_keys),
            "query_keys": cls._safe_parameter_names(raw_query_keys),
        }

    @classmethod
    def _safe_network_request_metadata(cls, request):
        if not isinstance(request, dict):
            return None
        url = cls._safe_network_url_metadata(request.get("url"))
        if url is None:
            return None

        method = str(request.get("method") or "").upper()[:12]
        metadata = {"method": method, "url": url}
        if method == "POST" and isinstance(request.get("postData"), str):
            try:
                raw_post_data_keys = [key for key, _ in parse_qsl(request["postData"], keep_blank_values=True)]
            except ValueError:
                raw_post_data_keys = []
            metadata["post_data_key_count"] = len(raw_post_data_keys)
            metadata["post_data_keys"] = cls._safe_parameter_names(raw_post_data_keys)
        return metadata

    @staticmethod
    def _header_value(headers, name):
        if not isinstance(headers, dict):
            return None
        for header_name, header_value in headers.items():
            if str(header_name).lower() == name.lower():
                return header_value
        return None

    @classmethod
    def _set_cookie_names(cls, value):
        if isinstance(value, (list, tuple)):
            raw_value = ", ".join(str(item) for item in value)
        else:
            raw_value = str(value or "")

        names = re.findall(r"(?:^|[,;]\\s*)([!#$%&'*+.^_`|~0-9A-Za-z-]+)=", raw_value)
        return sorted({name for name in names if name.lower() not in cls._COOKIE_ATTRIBUTE_NAMES})[:12]

    def _is_google_workspace_sso_diagnostic_url(self, value) -> bool:
        metadata = self._safe_network_url_metadata(value)
        if metadata is None:
            return False
        if metadata["host"] in self._GOOGLE_SSO_DIAGNOSTIC_HOSTS:
            return True

        try:
            configured_idp_host = urlparse(get_google_meet_sign_in_url()).hostname
        except Exception:
            configured_idp_host = None
        return bool(configured_idp_host and metadata["host"] == configured_idp_host.lower())

    def log_google_workspace_sso_network_diagnostics(self) -> None:
        """Log a bounded SSO response summary for an explicitly requested debug run.

        Chrome performance logs can contain request URLs, redirect locations, and
        Set-Cookie headers. This method intentionally keeps only response status,
        host/path, query *key names*, redirect metadata, and cookie *names*.
        Values such as SAML assertions, cookies, state, request headers, and
        email addresses never enter application logs. For the allowed-domains
        mitigation we additionally record only whether Chrome reported the
        documented header as present on a request, never its value.
        """
        if not getattr(self, "google_workspace_sso_network_diagnostics_enabled", False):
            return

        try:
            raw_entries = self.driver.get_log("performance")
        except Exception as exc:
            logger.warning("Google Workspace SSO network diagnostics were unavailable: %s", exc)
            return

        responses_by_request_id = {}
        headers_by_request_id = {}
        latest_requests_by_request_id = {}
        request_header_observed_ids = set()
        allowed_domains_header_present_by_request_id = {}
        for raw_entry in raw_entries:
            try:
                message = json.loads(raw_entry.get("message", "{}"))["message"]
                method = message.get("method")
                params = message.get("params", {})
            except (AttributeError, KeyError, TypeError, ValueError):
                continue

            request_id = params.get("requestId")
            if method == "Network.responseReceived":
                response = params.get("response", {})
                if request_id and self._is_google_workspace_sso_diagnostic_url(response.get("url")):
                    responses_by_request_id.setdefault(request_id, []).append(
                        {
                            "status": int(response.get("status", 0)),
                            "url": self._safe_network_url_metadata(response.get("url")),
                            "headers": response.get("headers") or {},
                            "request": latest_requests_by_request_id.get(request_id),
                        }
                    )
            elif method == "Network.requestWillBeSent":
                redirect_response = params.get("redirectResponse") or {}
                if request_id and self._is_google_workspace_sso_diagnostic_url(redirect_response.get("url")):
                    responses_by_request_id.setdefault(request_id, []).append(
                        {
                            "status": int(redirect_response.get("status", 0)),
                            "url": self._safe_network_url_metadata(redirect_response.get("url")),
                            "headers": redirect_response.get("headers") or {},
                            "request": latest_requests_by_request_id.get(request_id),
                        }
                    )
                request = self._safe_network_request_metadata(params.get("request"))
                if request_id and request and self._is_google_workspace_sso_diagnostic_url(params.get("request", {}).get("url")):
                    latest_requests_by_request_id[request_id] = request
            elif method == "Network.responseReceivedExtraInfo" and request_id:
                headers_by_request_id.setdefault(request_id, []).append(params.get("headers") or {})
            elif method == "Network.requestWillBeSentExtraInfo" and request_id:
                request_header_observed_ids.add(request_id)
                allowed_domains_header_present_by_request_id[request_id] = (
                    self._header_value(params.get("headers") or {}, self._GOOGLE_ALLOWED_DOMAINS_HEADER) is not None
                )

        summaries = []
        for request_id, responses in responses_by_request_id.items():
            extra_headers = headers_by_request_id.get(request_id, [])
            for response_index, response in enumerate(responses):
                headers = response["headers"] or (extra_headers[response_index] if response_index < len(extra_headers) else {})
                summary = {"status": response["status"], "url": response["url"]}
                if response["request"]:
                    request_metadata = dict(response["request"])
                    if request_id in request_header_observed_ids:
                        request_metadata["allowed_domains_header_present"] = allowed_domains_header_present_by_request_id.get(request_id, False)
                    summary["request"] = request_metadata
                location = self._header_value(headers, "location")
                if location:
                    summary["redirect"] = self._safe_network_url_metadata(location)
                cookie_names = self._set_cookie_names(self._header_value(headers, "set-cookie"))
                if cookie_names:
                    summary["set_cookie_names"] = cookie_names
                summaries.append(summary)
                if len(summaries) >= self._GOOGLE_SSO_NETWORK_DIAGNOSTIC_LIMIT:
                    break
            if len(summaries) >= self._GOOGLE_SSO_NETWORK_DIAGNOSTIC_LIMIT:
                break

        if summaries:
            logger.warning("Google Workspace SSO network diagnostics: %s", json.dumps(summaries, sort_keys=True, separators=(",", ":")))
        else:
            logger.warning("Google Workspace SSO network diagnostics captured no matching Google or configured-IdP responses")

    def log_google_login_timeout_diagnostics(self) -> None:
        """Log bounded, privacy-safe page state to diagnose non-interactive SSO failures."""
        try:
            title = self.driver.title or ""
            visible_text = self.driver.find_element(By.TAG_NAME, "body").text or ""
            visible_text = " ".join(visible_text.split())
            visible_text = re.sub(r"\b[\w.+-]+@[\w.-]+\.\w+\b", "<redacted-email>", visible_text)
            visible_text = visible_text[:500]
        except Exception as exc:
            logger.warning(
                "Google login timeout page diagnostics could not be collected (location=%s error=%s)",
                self._safe_browser_location_for_log(),
                exc,
            )
        else:
            logger.warning(
                "Google login timeout page diagnostics (location=%s title=%r visible_text=%r)",
                self._safe_browser_location_for_log(),
                title[:200],
                visible_text,
            )
        finally:
            self.log_google_workspace_sso_network_diagnostics()

    def locate_element(self, step, condition, wait_time_seconds=60):
        try:
            element = WebDriverWait(self.driver, wait_time_seconds).until(condition)
            return element
        except Exception as e:
            # Take screenshot when any exception occurs
            logger.warning(f"Exception raised in locate_element for {step}. Exception type = {type(e)}")
            raise UiCouldNotLocateElementException(f"Exception raised in locate_element for {step}", step, e)

    def find_element_by_selector(self, selector_type, selector):
        try:
            return self.driver.find_element(selector_type, selector)
        except NoSuchElementException:
            return None
        except Exception as e:
            logger.warning(f"Unknown error occurred in find_element_by_selector. Exception type = {type(e)}")
            return None

    def click_element_and_handle_blocking_elements(self, element, step):
        num_attempts = 30

        for attempt_index in range(num_attempts):
            try:
                self.click_element(element, step)
                return
            except UiCouldNotClickElementException as e:
                logger.warning(f"Error occurred when clicking element for step {step}, will click any blocking elements and retry the click")
                self.click_others_may_see_your_meeting_differently_button(step)
                last_attempt = attempt_index == num_attempts - 1
                if last_attempt:
                    raise e

    # Do it via javascript to avoid the element not being interactable exception
    def click_element_forcefully(self, element, step):
        try:
            self.driver.execute_script("arguments[0].click();", element)
        except Exception as e:
            logger.warning(f"Error occurred when forcefully clicking element for step {step}, will retry")
            raise UiCouldNotClickElementException("Error occurred when forcefully clicking element", step, e)

    def click_element(self, element, step):
        try:
            element.click()
        except Exception as e:
            logger.warning(f"Error occurred when clicking element for step {step}, will retry. Exception class name was {e.__class__.__name__}")
            raise UiCouldNotClickElementException("Error occurred when clicking element", step, e)

    # If the meeting you're about to join is being recorded, gmeet makes you click an additional button after you're admitted to the meeting
    def click_this_meeting_is_being_recorded_join_now_button(self, step):
        this_meeting_is_being_recorded_join_now_button = self.find_element_by_selector(By.XPATH, '//button[.//span[text()="Join now"]]')
        if this_meeting_is_being_recorded_join_now_button:
            logger.info("Clicking this_meeting_is_being_recorded_join_now_button")
            self.click_element(this_meeting_is_being_recorded_join_now_button, step)

    # Some modal that google put up
    def click_others_may_see_your_meeting_differently_button(self, step):
        others_may_see_your_meeting_differently_button = self.find_element_by_selector(By.XPATH, '//button[.//span[text()="Got it"]]')
        if others_may_see_your_meeting_differently_button:
            logger.info("Clicking others_may_see_your_meeting_differently_button")
            self.click_element_forcefully(others_may_see_your_meeting_differently_button, step)

    def look_for_blocked_element(self, step):
        cannot_join_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "You can\'t join this video call") or contains(text(), "There is a problem connecting to this video call")]')
        if cannot_join_element:
            # This means google is blocking us for whatever reason, but we can retry
            element_text = cannot_join_element.text

            # We need to track how many times this has happened so far.
            self.number_of_times_blocked_by_google += 1

            # If we have the ability to login, but we aren't using it, then we should raise an error that login is required.
            # Logging in will get us unblocked.
            if self.google_meet_bot_login_is_available and not self.google_meet_bot_login_should_be_used:
                if self.number_of_times_blocked_by_google > 1:
                    logger.warning("Google is blocking us for whatever reason and we have the ability to login but we aren't using it, so we should raise a UiLoginRequiredException. Logging in will get us unblocked.")
                    raise UiLoginRequiredException("Login required to get around blocking", step)
                logger.warning(f"Google is blocking us for whatever reason and we have the ability to login. So far it has only happened {self.number_of_times_blocked_by_google} times, so we will simply retry.")

            logger.warning(f"Google is blocking us for whatever reason, but we can retry. Element text: '{element_text}'. Raising UiGoogleBlockingUsException")
            raise UiGoogleBlockingUsException("You can't join this video call", step)

    def look_for_login_required_element(self, step):
        login_required_element = self.find_element_by_selector(By.XPATH, '//h1[contains(., "Sign in")]/parent::*[.//*[contains(text(), "your Google Account")]]')
        if login_required_element:
            logger.warning("Login required. Raising UiLoginRequiredException")
            raise UiLoginRequiredException("Login required", step)

    def look_for_denied_your_request_element(self, step):
        denied_your_request_element = self.find_element_by_selector(
            By.XPATH,
            '//*[contains(text(), "Someone in the call denied your request to join") or contains(text(), "No one responded to your request to join the call") or contains(text(), "You left the meeting")]',
        )
        if not denied_your_request_element:
            return

        element_text = denied_your_request_element.text

        if "Someone in the call denied your request to join" in element_text:
            logger.warning("Someone in the call actively denied our request to join. Raising UiRequestToJoinDeniedException")
            raise UiRequestToJoinDeniedException("Someone in the call denied your request to join", step)
        elif "No one responded to your request to join the call" in element_text:
            logger.warning("No one responded to our request to join (timeout). Raising UiRequestToJoinDeniedException")
            raise UiRequestToJoinDeniedException("No one responded to your request to join the call", step)
        else:  # "You left the meeting"
            logger.warning("Saw 'You left the meeting' element. Happens if someone actively denied our request to join. Raising UiRequestToJoinDeniedException")
            raise UiRequestToJoinDeniedException("You left the meeting", step)

    def look_for_asking_to_be_let_in_element_after_waiting_period_expired(self, step):
        asking_to_be_let_in_element = self.find_element_by_selector(
            By.XPATH,
            '//*[contains(text(), "Asking to be let in")]',
        )
        if asking_to_be_let_in_element:
            logger.warning("Bot was not let in after waiting period expired. Raising UiRequestToJoinDeniedException")
            raise UiRequestToJoinDeniedException("Bot was not let in after waiting period expired", step)

    def check_if_waiting_room_timeout_exceeded(self, waiting_room_timeout_started_at, step):
        waiting_room_timeout_exceeded = time.time() - waiting_room_timeout_started_at > self.automatic_leave_configuration.waiting_room_timeout_seconds
        if waiting_room_timeout_exceeded:
            # If there is more than one participant in the meeting, then the bot was just let in and we should not timeout
            if len(self.participants_info) > 1:
                logger.warning("Waiting room timeout exceeded, but there is more than one participant in the meeting. Not aborting join attempt.")
                return
            self.abort_join_attempt()
            logger.warning("Waiting room timeout exceeded. Raising UiCouldNotJoinMeetingWaitingRoomTimeoutException")
            raise UiCouldNotJoinMeetingWaitingRoomTimeoutException("Waiting room timeout exceeded", step)

    def turn_off_media_inputs(self):
        logger.info("Waiting for the microphone button...")
        MICROPHONE_BUTTON_SELECTOR = 'div[aria-label="Turn off microphone"], button[aria-label="Turn off microphone"]'
        MICROPHONE_BUTTON_ON_SELECTOR = 'div[aria-label="Turn on microphone"], button[aria-label="Turn on microphone"]'

        CAMERA_BUTTON_SELECTOR = 'div[aria-label="Turn off camera"], button[aria-label="Turn off camera"]'
        CAMERA_BUTTON_ON_SELECTOR = 'div[aria-label="Turn on camera"], button[aria-label="Turn on camera"]'

        for attempt in range(5):
            microphone_button = self.locate_element(
                step="turn_off_microphone_button",
                condition=EC.element_to_be_clickable((By.CSS_SELECTOR, MICROPHONE_BUTTON_SELECTOR)),
                wait_time_seconds=6,
            )
            logger.info("Clicking the microphone button...")
            self.click_element(microphone_button, "turn_off_microphone_button")

            # Wait for confirmation that microphone is off
            try:
                self.locate_element(
                    step="wait_for_microphone_to_be_off",
                    condition=EC.element_to_be_clickable((By.CSS_SELECTOR, MICROPHONE_BUTTON_ON_SELECTOR)),
                    wait_time_seconds=2,
                )
                break
            except:
                logger.warning("Microphone button did not seem to be turned off. Retrying...")

        for attempt in range(5):
            logger.info("Waiting for the camera button...")
            camera_button = self.locate_element(
                step="turn_off_camera_button",
                condition=EC.element_to_be_clickable((By.CSS_SELECTOR, CAMERA_BUTTON_SELECTOR)),
                wait_time_seconds=6,
            )
            logger.info("Clicking the camera button...")
            self.click_element(camera_button, "turn_off_camera_button")

            # Wait for confirmation that camera is off
            try:
                self.locate_element(
                    step="wait_for_camera_to_be_off",
                    condition=EC.element_to_be_clickable((By.CSS_SELECTOR, CAMERA_BUTTON_ON_SELECTOR)),
                    wait_time_seconds=2,
                )
                break
            except:
                logger.warning("Camera button did not seem to be turned off. Retrying...")

    def join_now_button_selector(self):
        return '//button[.//span[text()="Ask to join" or text()="Join now" or text()="Join the call now"]]'

    def check_for_failed_logged_in_bot_attempt(self):
        if not self.google_meet_bot_login_session:
            return
        logger.warning("Bot attempted to login, but name input is present, so the bot was not logged in. Raising UiLoginAttemptFailedException")
        raise UiLoginAttemptFailedException("Bot attempted to login, but name input is present, so the bot was not logged in.", "name_input")

    def join_now_button_is_present(self):
        join_button = self.find_element_by_selector(By.XPATH, self.join_now_button_selector())
        if join_button:
            return True
        return False

    def google_meet_green_room_is_loading(self) -> bool:
        """Return whether Meet is still initializing its pre-join preview.

        ``OQ2Y6`` is a Meet Green Room loading layer. It is deliberately kept
        out of the Google SSO control flow: while it is active, the correct
        action is to wait for Meet initialization rather than remove the layer
        or activate an underlying control through the keyboard/DOM.
        """
        try:
            loading_elements = self.driver.find_elements(
                By.CSS_SELECTOR,
                self._GOOGLE_MEET_GREEN_ROOM_LOADING_SELECTOR,
            )
            for element in loading_elements:
                if not element.is_displayed():
                    continue
                try:
                    active_state = element.get_attribute("data-active")
                except (StaleElementReferenceException, WebDriverException):
                    active_state = None
                if active_state != "false":
                    return True

            body_text = self.driver.find_element(By.TAG_NAME, "body").text or ""
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
            return False

        normalized_text = self._normalized_google_control_text(body_text)
        return all(marker in normalized_text for marker in self._GOOGLE_MEET_GREEN_ROOM_LOADING_MARKERS)

    def wait_for_google_meet_preview_initialization(self, wait_time_seconds=60):
        """Wait for Meet's Green Room to finish before touching join controls."""
        logger.info("Waiting for Google Meet pre-join preview initialization...")
        try:
            WebDriverWait(self.driver, wait_time_seconds).until(
                lambda driver: not self.google_meet_green_room_is_loading()
            )
        except TimeoutException as exc:
            try:
                body_text = " ".join((self.driver.find_element(By.TAG_NAME, "body").text or "").split())[:500]
            except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
                body_text = ""
            logger.warning(
                "Google Meet pre-join preview remained in its loading state (location=%s body=%r)",
                self._safe_browser_location_for_log(),
                body_text,
            )
            raise UiCouldNotLocateElementException(
                "Google Meet pre-join preview did not finish initializing",
                "google_meet_preview_initialization",
                exc,
            ) from exc

        logger.info("Google Meet pre-join preview initialization completed")

    def retrieve_name_input_element(self):
        return WebDriverWait(self.driver, 1).until(EC.presence_of_element_located((By.CSS_SELECTOR, 'input[type="text"][aria-label="Your name"]')))

    def fill_out_name_input(self):
        num_attempts_to_look_for_name_input = 30
        logger.info("Waiting for the name input field...")
        for attempt_to_look_for_name_input_index in range(num_attempts_to_look_for_name_input):
            try:
                name_input = self.retrieve_name_input_element()
                self.check_for_failed_logged_in_bot_attempt()
                logger.info("name input found")
                name_input.send_keys(self.display_name)
                return
            except TimeoutException as e:
                self.look_for_blocked_element("name_input")
                self.look_for_login_required_element("name_input")

                if self.google_meet_bot_login_session and self.join_now_button_is_present():
                    logger.info("This is a signed in bot and name input is not present but the join now button is present. Assuming name input is not present because we don't need to fill it out, so returning.")
                    return

                last_check_timed_out = attempt_to_look_for_name_input_index == num_attempts_to_look_for_name_input - 1
                if last_check_timed_out:
                    logger.warning("Could not find name input. Timed out. Raising UiCouldNotLocateElementException")
                    raise UiCouldNotLocateElementException("Could not find name input. Timed out.", "name_input", e)

            except ElementNotInteractableException as e:
                logger.warning("Name input is not interactable. Going to try again.")
                last_check_non_interactable = attempt_to_look_for_name_input_index == num_attempts_to_look_for_name_input - 1
                if last_check_non_interactable:
                    logger.warning("Could not find name input. Non interactable. Raising UiCouldNotLocateElementException")
                    raise UiCouldNotLocateElementException("Could not find name input. Non interactable.", "name_input", e)

            except UiLoginAttemptFailedException as e:
                raise e

            except Exception as e:
                logger.warning(f"Could not find name input. Unknown error {e} of type {type(e)}. Raising UiCouldNotLocateElementException")
                raise UiCouldNotLocateElementException("Could not find name input. Unknown error.", "name_input", e)

    def click_captions_button(self):
        num_attempts_to_look_for_captions_button = 600
        logger.info("Waiting for captions button...")
        waiting_room_timeout_started_at = time.time()
        for attempt_to_look_for_captions_button_index in range(num_attempts_to_look_for_captions_button):
            try:
                captions_button = WebDriverWait(self.driver, 1).until(EC.presence_of_element_located((By.CSS_SELECTOR, 'button[aria-label="Turn on captions"]')))
                logger.info("Captions button found")
                self.click_element(captions_button, "click_captions_button")
                logger.info("Waiting for captions to be enabled...")
                WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, 'button[aria-label="Turn off captions"]')))
                logger.info("Confirmed captions were enabled")
                return
            except UiCouldNotClickElementException as e:
                self.click_this_meeting_is_being_recorded_join_now_button("click_captions_button")
                self.click_others_may_see_your_meeting_differently_button("click_captions_button")
                last_check_could_not_click_element = attempt_to_look_for_captions_button_index == num_attempts_to_look_for_captions_button - 1
                if last_check_could_not_click_element:
                    logger.warning("Could not click captions button. Raising UiCouldNotClickElementException")
                    raise e
            except TimeoutException as e:
                self.look_for_blocked_element("click_captions_button")
                self.look_for_denied_your_request_element("click_captions_button")
                self.click_this_meeting_is_being_recorded_join_now_button("click_captions_button")
                self.click_others_may_see_your_meeting_differently_button("click_captions_button")
                self.check_if_waiting_room_timeout_exceeded(waiting_room_timeout_started_at, "click_captions_button")

                last_check_timed_out = attempt_to_look_for_captions_button_index == num_attempts_to_look_for_captions_button - 1
                if last_check_timed_out:
                    self.look_for_asking_to_be_let_in_element_after_waiting_period_expired("click_captions_button")

                    logger.warning("Could not find captions button. Timed out. Raising UiCouldNotLocateElementException")
                    raise UiCouldNotLocateElementException(
                        "Could not find captions button. Timed out.",
                        "click_captions_button",
                        e,
                    )

            except Exception as e:
                logger.warning(f"Could not find captions button. Unknown error {e} of type {type(e)}. Raising UiCouldNotLocateElementException")
                raise UiCouldNotLocateElementException(
                    "Could not find captions button. Unknown error.",
                    "click_captions_button",
                    e,
                )

    def check_if_meeting_is_found(self):
        meeting_not_found_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "Check your meeting code") or contains(text(), "Invalid video call name") or contains(text(), "Your meeting code has expired")]')
        if meeting_not_found_element:
            logger.warning("Meeting not found. Raising UiMeetingNotFoundException")
            raise UiMeetingNotFoundException("Meeting not found", "check_if_meeting_is_found")

    def wait_for_host_if_needed(self):
        host_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "Waiting for the host to join")]')
        if host_element:
            # Wait for up to n seconds for the host to join
            wait_time_seconds = self.automatic_leave_configuration.wait_for_host_to_start_meeting_timeout_seconds
            logger.info(f"We must wait for the host to join before we can join the meeting. Waiting for {wait_time_seconds} seconds...")
            try:
                WebDriverWait(self.driver, wait_time_seconds).until(EC.invisibility_of_element_located((By.XPATH, '//*[contains(text(), "Waiting for the host to join")]')))
            except TimeoutException:
                logger.warning("Host did not join the meeting in time. Raising UiCouldNotJoinMeetingWaitingForHostException")
                raise UiCouldNotJoinMeetingWaitingForHostException("Host did not join the meeting in time", "wait_for_host_if_needed")

    def get_layout_to_select(self):
        if self.recording_view == RecordingViews.SPEAKER_VIEW:
            return "sidebar"
        elif self.recording_view == RecordingViews.GALLERY_VIEW:
            return "tiled"
        elif self.recording_view == RecordingViews.SPEAKER_VIEW_NO_SIDEBAR:
            return "spotlight"
        else:
            return "sidebar"

    def turn_off_reactions(self):
        try:
            self.attempt_to_turn_off_reactions()
        except Exception as e:
            logger.warning(f"Error turning off reactions: {e}")

    def attempt_to_turn_off_reactions(self):
        logger.info("Attempting to turn off reactions")
        logger.info("Waiting for the more options button...")
        MORE_OPTIONS_BUTTON_SELECTOR = 'button[jsname="NakZHc"][aria-label="More options"]'
        more_options_button = self.locate_element(
            step="more_options_button_for_language_selection",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, MORE_OPTIONS_BUTTON_SELECTOR)),
            wait_time_seconds=6,
        )
        logger.info("Clicking the more options button...")
        self.click_element(more_options_button, "more_options_button")

        logger.info("Waiting for the settings list item...")
        settings_list_item = self.locate_element(
            step="settings_list_item",
            condition=EC.presence_of_element_located((By.XPATH, '//li[.//span[text()="Settings"]]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the settings list item...")
        self.click_element(settings_list_item, "settings_list_item")

        logger.info("Waiting for the reactions tab...")
        self.locate_element(
            step="reactions_tab",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, 'button[aria-label="Reactions"]')),
            wait_time_seconds=6,
        )

        # Use javascript to click the reactions button
        self.driver.execute_script("document.querySelector('button[aria-label=\"Show reactions from others\"]').click();")

        logger.info("Waiting for the close button")
        close_button = self.locate_element(
            step="close_button_for_language_selection",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, 'button[aria-label="Close dialog"]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the close button")
        self.click_element(close_button, "close_button")

    def disable_incoming_video_in_ui(self):
        logger.info("Disabling incoming video")
        logger.info("Waiting for the more options button...")
        MORE_OPTIONS_BUTTON_SELECTOR = 'button[jsname="NakZHc"][aria-label="More options"]'
        more_options_button = self.locate_element(
            step="more_options_button_for_language_selection",
            condition=EC.element_to_be_clickable((By.CSS_SELECTOR, MORE_OPTIONS_BUTTON_SELECTOR)),
            wait_time_seconds=6,
        )
        logger.info("Clicking the more options button...")
        self.click_element(more_options_button, "disable_incoming_video:more_options_button")

        logger.info("Waiting for the settings list item...")
        settings_list_item = self.locate_element(
            step="settings_list_item",
            condition=EC.element_to_be_clickable((By.XPATH, '//li[.//span[text()="Settings"]]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the settings list item...")
        self.click_element(settings_list_item, "disable_incoming_video:settings_list_item")

        logger.info("Waiting for the video button...")
        video_button = self.locate_element(
            step="video_button",
            condition=EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[aria-label="Video"]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the video button...")
        self.click_element(video_button, "disable_incoming_video:video_button")

        # After clicking the video button, select "Audio only" option
        logger.info("Waiting for the Audio only option...")
        audio_only_option = self.locate_element(
            step="audio_only_option",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, 'li[aria-label="Audio only"]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the Audio only option...")
        # Click the option using javascript
        self.driver.execute_script("arguments[0].click();", audio_only_option)

        logger.info("Waiting for the close button")
        close_button = self.locate_element(
            step="close_button",
            condition=EC.element_to_be_clickable((By.CSS_SELECTOR, '[aria-modal="true"] button[aria-label="Close dialog"]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the close button")
        self.click_element(close_button, "disable_incoming_video:close_button")

        logger.info("Incoming video disabled")

    def set_layout(self, layout_to_select):
        num_attempts = 3
        for attempt_index in range(num_attempts):
            try:
                self.attempt_to_set_layout(layout_to_select)
                return
            except Exception as e:
                last_attempt = attempt_index == num_attempts - 1
                if last_attempt:
                    raise e
                logger.warning(f"Error setting layout: {e}. Retrying. Attempt #{attempt_index}...")

                self.reset_attempt_to_set_layout()

    def reset_attempt_to_set_layout(self):
        # Check if there is a modal with a close button. If so click it.

        logger.info("Looking for a modal with a close button")
        close_button_selector = '[aria-modal="true"] button[aria-label="Close"]'
        for attempt in range(5):
            try:
                close_button = WebDriverWait(self.driver, 1).until(EC.element_to_be_clickable((By.CSS_SELECTOR, close_button_selector)))
                logger.info("Found it. Clicking the close button")
                close_button.click()
                break
            except ElementNotInteractableException as e:
                logger.warning(f"Modal close button not interactable (attempt {attempt + 1}/5): {e}. Retrying...")
            except Exception as e:
                logger.warning(f"No modal with a close button found: {e}. Continuing...")
                break

        logger.info("Sending a click to the body element to close any menus")
        try:
            body_element = self.locate_element(
                step="body_element",
                condition=EC.presence_of_element_located((By.TAG_NAME, "body")),
                wait_time_seconds=1,
            )
            self.click_element(body_element, "body_element")
        except Exception as e:
            logger.warning(f"Error sending a click to the body element to close any menus: {e}. Continuing...")

    def attempt_to_set_layout(self, layout_to_select):
        logger.info("Begin setting layout. Waiting for the more options button...")
        MORE_OPTIONS_BUTTON_SELECTOR = 'button[jsname="NakZHc"][aria-label="More options"]'
        more_options_button = self.locate_element(
            step="more_options_button",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, MORE_OPTIONS_BUTTON_SELECTOR)),
            wait_time_seconds=6,
        )
        logger.info("Clicking the more options button....")
        self.click_element_and_handle_blocking_elements(more_options_button, "more_options_button")

        logger.info("Waiting for the 'Change layout' list item...")
        change_layout_list_item = self.locate_element(
            step="change_layout_item",
            condition=EC.presence_of_element_located((By.XPATH, '//li[.//span[text()="Change layout" or text()="Adjust view"] or @jsname="WZerud"]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the 'Change layout' list item....")
        self.click_element_and_handle_blocking_elements(change_layout_list_item, "change_layout_list_item")

        if layout_to_select == "spotlight":
            logger.info("Waiting for the 'Spotlight' label element")
            spotlight_label = self.locate_element(
                step="spotlight_label",
                condition=EC.presence_of_element_located((By.XPATH, '//label[.//span[text()="Spotlight"]]')),
                wait_time_seconds=6,
            )
            logger.info("Clicking the 'Spotlight' label element")
            self.click_element(spotlight_label, "spotlight_label")

        if layout_to_select == "sidebar":
            logger.info("Waiting for the 'Sidebar' label element")
            sidebar_label = self.locate_element(
                step="sidebar_label",
                condition=EC.element_to_be_clickable((By.XPATH, '//label[.//span[text()="Sidebar"]]')),
                wait_time_seconds=6,
            )
            logger.info("Clicking the 'Sidebar' label element")
            self.click_element(sidebar_label, "sidebar_label")

        if layout_to_select == "tiled":
            logger.info("Waiting for the 'Tiled' label element")
            tiled_label = self.locate_element(
                step="tiled_label",
                condition=EC.presence_of_element_located((By.XPATH, '//label[.//span[@class="xo15nd" and contains(text(), "Tiled")]]')),
                wait_time_seconds=6,
            )
            logger.info("Clicking the 'Tiled' label element")
            self.click_element(tiled_label, "tiled_label")

            logger.info("Waiting for the tile selector element")
            tile_selector = self.locate_element(
                step="tile_selector",
                condition=EC.presence_of_element_located((By.CSS_SELECTOR, ".ByPkaf")),
                wait_time_seconds=6,
            )

            logger.info("Finding all tile options")
            tile_options = tile_selector.find_elements(By.CSS_SELECTOR, ".gyG0mb-zD2WHb-SYOSDb-OWXEXe-mt1Mkb")

            if tile_options:
                logger.info("Clicking the last tile option (49 tiles)")
                last_tile_option = tile_options[-1]
                self.click_element(last_tile_option, "last_tile_option")
            else:
                logger.warning("No tile options found")

        logger.info("Waiting for the close button")
        close_button = self.locate_element(
            step="close_button",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, '[aria-modal="true"] button[aria-label="Close"]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the close button")
        self.click_element(close_button, "close_button")

    def wait_until_url_has_stopped_changing(self, stable_for: float = 1.0, timeout: float = 30.0, poll: float = 0.1) -> bool:
        """
        Wait until the browser URL remains unchanged for at least `stable_for` seconds.
        Returns True if stability was achieved before `timeout`, else False.
        """
        last_url = self.driver.current_url
        last_change = time.monotonic()
        deadline = last_change + timeout

        while time.monotonic() < deadline:
            current_url = self.driver.current_url
            if current_url != last_url:
                # URL changed; reset the stability timer
                last_url = current_url
                last_change = time.monotonic()

            # Has the URL been stable long enough?
            if (time.monotonic() - last_change) >= stable_for:
                logger.info("URL has not changed for %.2f seconds, returning (url=%s)", stable_for, current_url)
                return True

            time.sleep(poll)

        logger.info("Timed out waiting for URL stability (>%.2fs). Last URL: %s", stable_for, last_url)
        return False

    def login_to_google_meet_account_with_retries(self):
        # Blanket guard against transient errors on Google's side
        # An explicitly requested debug run should preserve the first failure's
        # network trace rather than retrying the same SSO transaction repeatedly.
        num_attempts = 1 if getattr(self, "google_workspace_sso_network_diagnostics_enabled", False) else 3
        for attempt_index in range(num_attempts):
            try:
                self.login_to_google_meet_account()
                return
            except UiLoginAttemptFailedException as e:
                last_attempt = attempt_index == num_attempts - 1
                if last_attempt:
                    raise e
                logger.warning(f"Error logging in to Google Meet account. Clearing cookies and retrying... Attempts remaining: {num_attempts - attempt_index - 1}")
                self.driver.delete_all_cookies()

    def google_workspace_sso_entry_url(self, login_domain: str, meeting_url: str | None = None) -> str:
        """Return the service entry point for the assigned Meet bot account.

        Free-license Meet bot accounts are provisioned for Meet only. Their
        browser transaction must therefore start at the target Meet URL, after
        the facade has written the opaque SSO session cookie in the same browser
        context. Keeping the target URL preserves Google's Meet service context
        through the account-identifier and SAML redirect steps; starting at the
        public product root can leave a fresh profile on a generic identifier
        form that does not submit the Meet SSO request. A domain ServiceLogin
        URL is retained only as an explicitly configured compatibility path for
        older deployments.
        """
        entrypoint = os.getenv(
            "GOOGLE_MEET_SSO_ENTRYPOINT",
            self._GOOGLE_SSO_ENTRYPOINT_MEET,
        ).strip().lower()
        if entrypoint == self._GOOGLE_SSO_ENTRYPOINT_MEET:
            meeting_location = urlparse(str(meeting_url or ""))
            if (
                meeting_location.scheme == "https"
                and meeting_location.hostname
                and meeting_location.hostname.lower() in self._GOOGLE_MEET_HOSTS
                and meeting_location.path not in {"", "/"}
            ):
                return meeting_url
            return "https://meet.google.com/"
        if entrypoint == self._GOOGLE_SSO_ENTRYPOINT_DOMAIN_SERVICE_LOGIN:
            login_domain = self._normalized_workspace_domain(login_domain)
            if login_domain is None:
                raise UiLoginAttemptFailedException(
                    "Allocated Google Workspace domain is invalid",
                    "google_workspace_sso_entry_url",
                )
            return f"https://www.google.com/a/{login_domain}/ServiceLogin"
        raise UiLoginAttemptFailedException(
            "Google Workspace SSO entry point is invalid",
            "google_workspace_sso_entry_url",
        )

    # This prevents the browser from navigating to an untrusted URL. Google now
    # uses an internal accounts.google.com/samlredirect hop before the configured
    # IdP, so the whole redirect chain must be validated rather than its first hop.
    def safely_navigate_to_google_workspace_sso_entry(self):
        session = self.google_meet_bot_login_session or {}
        login_domain = self._normalized_workspace_domain(session.get("login_domain"))
        if login_domain is None:
            raise UiLoginAttemptFailedException("Allocated Google Workspace domain is invalid", "safe_navigate_to_google_workspace_sso_entry")

        expected_idp_url = urlparse(get_google_meet_sign_in_url())
        if expected_idp_url.scheme != "https" or not expected_idp_url.hostname:
            raise UiLoginAttemptFailedException("Configured Google Workspace IdP URL is invalid", "safe_navigate_to_google_workspace_sso_entry")

        google_service_url = self.google_workspace_sso_entry_url(login_domain)
        current_url = google_service_url
        logger.info("Resolving the trusted Google Workspace SSO redirect chain")
        http_session = requests.Session()

        for redirect_index in range(self._GOOGLE_SSO_MAX_REDIRECTS):
            try:
                response = http_session.get(current_url, allow_redirects=False, timeout=15)
            except requests.RequestException as exc:
                raise UiLoginAttemptFailedException("Could not resolve Google Workspace SSO redirect", "safe_navigate_to_google_workspace_sso_entry", exc) from exc

            location = response.headers.get("Location")
            if response.status_code not in {301, 302, 303, 307, 308} or not location:
                logger.error(
                    "Google Workspace SSO redirect chain stopped before the configured IdP (hop=%s status=%s host=%s path=%s)",
                    redirect_index,
                    response.status_code,
                    urlparse(current_url).hostname,
                    urlparse(current_url).path,
                )
                break

            next_url = urljoin(current_url, location)
            next_url_parts = urlparse(next_url)
            if next_url_parts.scheme != "https" or not next_url_parts.hostname:
                logger.error("Google Workspace SSO redirect contained an invalid target")
                break

            if next_url_parts.hostname == expected_idp_url.hostname:
                if next_url_parts.netloc != expected_idp_url.netloc or next_url_parts.path != expected_idp_url.path:
                    logger.error("Google Workspace SSO redirect did not target the configured IdP sign-in endpoint")
                    break
                logger.info("Resolved Google Workspace SSO redirect chain to the configured IdP")
                # Use the server-side session only to validate the redirect chain.
                # Chrome starts at the same verified domain entry point so its
                # browser-owned state follows the validated path.
                self.driver.get(google_service_url)
                return

            if next_url_parts.hostname not in self._GOOGLE_SSO_REDIRECT_HOSTS:
                logger.error("Google Workspace SSO redirect left the trusted Google/IdP host set")
                break
            current_url = next_url

        raise UiLoginAttemptFailedException("Google Workspace SSO redirect did not reach the configured IdP", "safe_navigate_to_google_workspace_sso_entry")

    def navigate_to_google_workspace_sso_entry(self):
        entrypoint = os.getenv(
            "GOOGLE_MEET_SSO_ENTRYPOINT",
            self._GOOGLE_SSO_ENTRYPOINT_MEET,
        ).strip().lower()
        if entrypoint == self._GOOGLE_SSO_ENTRYPOINT_MEET:
            entry_url = self.google_workspace_sso_entry_url(None, getattr(self, "meeting_url", None))
            logger.info(
                "Navigating to the Google Meet service entry point (target=%s)",
                self._safe_network_url_metadata(entry_url),
            )
            self.driver.get(entry_url)
            logger.info(
                "Google Meet service entry navigation completed (location=%s)",
                self._safe_browser_location_for_log(),
            )
            # When no target is available (for example in a standalone SSO
            # diagnostic), the public Meet root can resolve to Google's product
            # landing page. Follow its stable account link only in that fallback
            # case; a target meeting URL already carries the Meet service
            # context and must not be replaced by the generic product route.
            current_location = urlparse(str(getattr(self.driver, "current_url", "") or ""))
            current_host = current_location.hostname
            if entry_url != "https://meet.google.com/" and current_host and current_host.lower() in self._GOOGLE_MEET_HOSTS:
                # A direct target URL preserves Meet's service context, but a
                # fresh profile still shows Meet's account sign-in link before
                # Google can issue the SAML AuthnRequest. Follow that link in
                # the same browser context; do not replace the target with the
                # generic product root.
                if self.click_google_meet_product_sign_in_if_needed():
                    return
                # An unauthenticated profile can render a meeting-specific
                # "can't join" shell without a sign-in control. In that state
                # the public Meet entry page is the stable, service-scoped
                # recovery path and exposes the same Google ServiceLogin
                # destination. Keep this fallback data-driven rather than
                # matching localized Meet copy.
                logger.info("Google Meet target did not expose a sign-in link; falling back to the public Meet entry page")
                self.driver.get(self._GOOGLE_MEET_HOME_URL)
                current_location = urlparse(str(getattr(self.driver, "current_url", "") or ""))
                current_host = current_location.hostname
                if current_host and current_host.lower() not in self._GOOGLE_MEET_HOSTS:
                    if not self.click_google_meet_product_sign_in_if_needed():
                        logger.info("Google Meet fallback page did not expose a usable product sign-in link")
            if entry_url == "https://meet.google.com/" and current_host and current_host.lower() not in self._GOOGLE_MEET_HOSTS:
                already_in_google_sso = (
                    current_host.lower() in self._GOOGLE_SSO_REDIRECT_HOSTS
                    and "/signin/" in current_location.path
                )
                if not already_in_google_sso and not self.click_google_meet_product_sign_in_if_needed():
                    logger.info("Google Meet root did not expose a usable product sign-in link; navigating to the Meet home page for SSO")
                    self.driver.get(self._GOOGLE_MEET_HOME_URL)
            return

        if entrypoint == self._GOOGLE_SSO_ENTRYPOINT_DOMAIN_SERVICE_LOGIN and os.getenv(
            "USE_SAFE_NAVIGATION_FOR_SIGNED_IN_GOOGLE_MEET_BOTS", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}:
            self.safely_navigate_to_google_workspace_sso_entry()
            return

        login_domain = self._normalized_workspace_domain((self.google_meet_bot_login_session or {}).get("login_domain"))
        if login_domain is None:
            raise UiLoginAttemptFailedException("Allocated Google Workspace domain is invalid", "navigate_to_google_workspace_sso_entry")
        google_service_url = self.google_workspace_sso_entry_url(login_domain)
        logger.info("Navigating to Google Workspace SSO entry point")
        self.driver.get(google_service_url)

    def click_google_meet_product_sign_in_if_needed(self) -> bool:
        """Follow Meet's product-page sign-in link for a fresh browser profile.

        ``https://meet.google.com/`` can resolve to the public Meet product page
        before Google has established an account session. The product page's
        sign-in link carries the Meet service context into Google's identifier
        flow. Select the link by its destination, not by rendered text, so the
        flow remains independent of browser language and region.
        """
        current_url = str(getattr(self.driver, "current_url", "") or "")
        current_location = urlparse(current_url)

        def find_product_sign_in_link(driver):
            for selector in self._GOOGLE_PRODUCT_SIGN_IN_LINK_SELECTORS:
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        if element.is_displayed() and element.is_enabled():
                            return element
                    except (StaleElementReferenceException, WebDriverException):
                        continue
            return False

        try:
            sign_in_link = WebDriverWait(self.driver, 3).until(find_product_sign_in_link)
            sign_in_link.click()
        except (TimeoutException, ElementNotInteractableException, NoSuchElementException, StaleElementReferenceException, WebDriverException) as exc:
            logger.info("Google Meet product page did not expose a stable account sign-in link: %s", exc.__class__.__name__)
            return False

        try:
            WebDriverWait(self.driver, 5).until(
                lambda driver: str(getattr(driver, "current_url", "") or "") != current_url
            )
        except TimeoutException:
            logger.info("Google Meet product sign-in link did not navigate away from the product page")
            return False

        logger.info("Followed the Google Meet sign-in link into the account SSO flow")
        return True

    def clear_google_workspace_sso_allowed_domains_header(self) -> None:
        """Stop sending the SSO-only header to Meet assets after handoff.

        ``Network.setExtraHTTPHeaders`` applies to every subsequent request in
        the profile. Keeping ``X-GoogApps-AllowedDomains`` on fonts and Meet
        RPCs causes cross-origin preflight failures, so it must be removed once
        Google has completed the account handoff.
        """
        try:
            self.driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": {}})
        except Exception as exc:
            logger.warning("Could not clear the Google Workspace SSO allowed-domains header: %s", exc.__class__.__name__)
            return
        logger.info("Cleared the Google Workspace SSO allowed-domains header after handoff")

    def verify_google_meet_sso_session_cookie(self, session_id: str) -> None:
        """Require the opaque SSO session to exist in this browser before redirecting.

        The SAML facade deliberately uses a browser-owned HttpOnly cookie. A
        failed facade response must not be mistaken for a Google login failure:
        continuing without this cookie can reach Google's account chooser and
        produce an opaque timeout with no useful indication that the first hop
        was lost.
        """
        try:
            cookie = self.driver.get_cookie(self._GOOGLE_MEET_SSO_SESSION_COOKIE)
        except Exception as exc:
            raise UiLoginAttemptFailedException(
                "Could not inspect the Google Meet SSO session cookie",
                "verify_google_meet_sso_session_cookie",
                exc,
            ) from exc

        if not isinstance(cookie, dict) or cookie.get("value") != session_id:
            logger.warning(
                "Google Meet SSO session cookie was not written by the facade (location=%s cookie_present=%s)",
                self._safe_browser_location_for_log(),
                bool(cookie),
            )
            raise UiLoginAttemptFailedException(
                "Google Meet SSO session cookie was not written",
                "verify_google_meet_sso_session_cookie",
            )

        logger.info("Google Meet SSO session cookie is present in the browser context")

    def submit_google_meet_account_identifier_if_needed(self) -> bool:
        """Submit the allocated Meet account identifier on Google's generic sign-in page.

        The Meet-only entry flow may first land on Google's account identifier
        page before it can select the configured Workspace SAML provider. The
        account is allocated server-side with the short-lived SSO session, so
        submitting only its identifier is enough; no password or Gmail service
        is involved. Stable input names are used so this remains independent of
        the page language.
        """
        session = self.google_meet_bot_login_session or {}
        login_email = session.get("login_email")
        if not isinstance(login_email, str) or not login_email.strip():
            logger.warning("Google Meet account identifier is unavailable for the Meet SSO flow")
            return False

        current_url = str(getattr(self.driver, "current_url", "") or "")
        current_location = urlparse(current_url)
        if current_location.hostname not in {"accounts.google.com", "www.google.com"} or "/v3/signin/identifier" not in current_location.path:
            return False

        def find_identifier_input(driver):
            elements = driver.find_elements(By.CSS_SELECTOR, "input[name='identifier'], input[type='email']")
            return next((element for element in elements if element.is_displayed() and element.is_enabled()), False)

        try:
            identifier_input = WebDriverWait(self.driver, 5).until(find_identifier_input)
            identifier_input.clear()
            identifier_input.send_keys(login_email.strip())
            # Keep the browser's native input event path in sync as well. Some
            # Google account-page revisions use a controlled input: WebDriver
            # can update the DOM value while the page-side validation state is
            # still empty, leaving Next visually enabled but inert.
            try:
                self.driver.execute_script(
                    """
                    const field = arguments[0];
                    const value = arguments[1];
                    const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                    if (descriptor && descriptor.set && field.value !== value) {
                        descriptor.set.call(field, value);
                    }
                    field.dispatchEvent(new Event('input', {bubbles: true}));
                    field.dispatchEvent(new Event('change', {bubbles: true}));
                    field.blur();
                    """,
                    identifier_input,
                    login_email.strip(),
                )
            except (StaleElementReferenceException, WebDriverException):
                pass

            def find_next_button(driver):
                for selector in self._GOOGLE_IDENTIFIER_CONTINUE_SELECTORS:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    for element in elements:
                        try:
                            if not element.is_displayed() or not element.is_enabled():
                                continue
                            if str(element.get_attribute("aria-disabled") or "").strip().lower() == "true":
                                continue
                            return element
                        except (StaleElementReferenceException, WebDriverException):
                            continue
                return False

            next_button = WebDriverWait(self.driver, 5).until(find_next_button)
            logger.info(
                "Google account identifier control is ready (input_value_length=%s button_id=%s button_jsname=%s button_aria_disabled=%s)",
                len(str(identifier_input.get_attribute("value") or "")),
                next_button.get_attribute("id"),
                next_button.get_attribute("jsname"),
                next_button.get_attribute("aria-disabled"),
            )
            # Google enables the control after its client-side input validation.
            # Blur the field once so the same validation path is exercised as a
            # real user tabbing from the identifier field.
            try:
                identifier_input.send_keys(Keys.TAB)
            except (StaleElementReferenceException, WebDriverException):
                pass
            next_button.click()
            # Google has changed this page between a native button and a
            # client-side continuation control.  Keep the stable button click,
            # then submit the same form from the identifier field as a
            # browser-native fallback.  If the click already navigated, the
            # element becomes stale and the fallback is naturally skipped.
            if str(getattr(self.driver, "current_url", "") or "") == current_url:
                try:
                    ActionChains(self.driver).move_to_element(next_button).click().perform()
                except (AttributeError, StaleElementReferenceException, WebDriverException):
                    pass
            if str(getattr(self.driver, "current_url", "") or "") == current_url:
                try:
                    self.driver.execute_script("arguments[0].click();", next_button)
                except (StaleElementReferenceException, WebDriverException):
                    pass
            if str(getattr(self.driver, "current_url", "") or "") == current_url:
                try:
                    identifier_input.send_keys(Keys.ENTER)
                except (StaleElementReferenceException, WebDriverException):
                    pass
            if str(getattr(self.driver, "current_url", "") or "") == current_url:
                try:
                    self.driver.execute_script(
                        """
                        const field = arguments[0];
                        const submitControl = arguments[1];
                        const form = field && field.closest ? field.closest('form') : null;
                        if (form && typeof form.requestSubmit === 'function') {
                            form.requestSubmit(submitControl || undefined);
                        } else if (submitControl) {
                            submitControl.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                        }
                        """,
                        identifier_input,
                        next_button,
                    )
                except (StaleElementReferenceException, WebDriverException):
                    pass
            if str(getattr(self.driver, "current_url", "") or "") == current_url:
                try:
                    # Last-resort browser-native submission. This preserves the
                    # hidden form fields and redirect target while avoiding a
                    # dependency on a particular Google client-side handler.
                    self.driver.execute_script(
                        """
                        const field = arguments[0];
                        const form = field && field.closest ? field.closest('form') : null;
                        if (form) {
                            HTMLFormElement.prototype.submit.call(form);
                        }
                        """,
                        identifier_input,
                    )
                except (StaleElementReferenceException, WebDriverException):
                    pass
        except (TimeoutException, ElementNotInteractableException, NoSuchElementException, StaleElementReferenceException, WebDriverException) as exc:
            logger.warning("Google Meet account identifier page did not expose a usable continuation control: %s", exc.__class__.__name__)
            return False

        self._google_meet_identifier_initial_url = current_url
        try:
            WebDriverWait(self.driver, 8).until(self.google_meet_identifier_flow_advanced)
        except TimeoutException:
            logger.warning(
                "Google Meet account identifier continuation did not advance the browser (location=%s)",
                self._safe_browser_location_for_log(),
            )
            self.log_google_login_timeout_diagnostics()
            return False

        if self.google_meet_identifier_input_is_visible():
            logger.warning(
                "Google account identifier page is still visible after continuation (location=%s)",
                self._safe_browser_location_for_log(),
            )
            self.log_google_login_timeout_diagnostics()
            return False

        logger.info("Submitted the allocated Google Meet account identifier to continue SAML sign-in")
        return True

    def google_meet_identifier_input_is_visible(self) -> bool:
        """Return whether Google is still showing the account identifier form."""
        try:
            elements = self.driver.find_elements(By.CSS_SELECTOR, "input[name='identifier'], input[type='email']")
        except (StaleElementReferenceException, WebDriverException):
            return False
        return any(
            element.is_displayed() and element.is_enabled()
            for element in elements
            if element is not None
        )

    def google_meet_identifier_flow_advanced(self, driver) -> bool:
        """Wait for a real Google sign-in state change, not only a URL change."""
        if str(getattr(driver, "current_url", "") or "") != self._google_meet_identifier_initial_url:
            return True
        if not self.google_meet_identifier_input_is_visible():
            return True
        page_text = self._google_sso_page_text()
        return any(marker in page_text for marker in self._GOOGLE_SSO_IDENTITY_MARKERS) or any(marker in page_text for marker in self._GOOGLE_SSO_PASSKEY_MARKERS)

    @staticmethod
    def _normalized_google_control_text(value) -> str:
        if not isinstance(value, str):
            return ""
        return " ".join(value.replace("’", "'").split()).casefold()

    def _google_sso_page_text(self) -> str:
        try:
            page_text = self.driver.find_element(By.TAG_NAME, "body").text or ""
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
            return ""
        return self._normalized_google_control_text(page_text)

    def _find_google_sso_control(self, selectors, labels):
        for selector in selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            except (StaleElementReferenceException, WebDriverException):
                continue
            for element in elements:
                try:
                    if (
                        element.is_displayed()
                        and element.is_enabled()
                        and element.get_attribute("aria-disabled") != "true"
                    ):
                        return element
                except (StaleElementReferenceException, WebDriverException):
                    continue

        try:
            elements = self.driver.find_elements(By.CSS_SELECTOR, "button, [role='button'], a, input[type='submit']")
        except (StaleElementReferenceException, WebDriverException):
            return None

        for element in elements:
            try:
                if (
                    not element.is_displayed()
                    or not element.is_enabled()
                    or element.get_attribute("aria-disabled") == "true"
                ):
                    continue
                control_text = self._normalized_google_control_text(
                    element.text or element.get_attribute("aria-label") or element.get_attribute("value")
                )
                if control_text in labels:
                    return element
            except (StaleElementReferenceException, WebDriverException):
                continue
        return None

    def advance_google_workspace_sso_interstitial_if_needed(self) -> bool:
        """Advance Google account confirmation pages that do not require a password.

        Workspace SSO accounts can show an identity confirmation and then a
        passkey-enrollment offer even when the OU policy skips password entry.
        These pages are part of Google's account UI, not Meet's join UI. Use
        page state plus stable control attributes first, with localized labels
        only as a narrow fallback.
        """
        current_location = urlparse(str(getattr(self.driver, "current_url", "") or ""))
        if current_location.hostname not in self._GOOGLE_SSO_REDIRECT_HOSTS:
            return False

        page_text = self._google_sso_page_text()
        passkey_page = any(marker in page_text for marker in self._GOOGLE_SSO_PASSKEY_MARKERS) or "webauthn" in current_location.path.lower()
        identity_page = current_location.path.endswith(self._GOOGLE_SAML_CONFIRMATION_PATH_SUFFIX) or any(
            marker in page_text for marker in self._GOOGLE_SSO_IDENTITY_MARKERS
        )

        if passkey_page:
            control = self._find_google_sso_control(self._GOOGLE_SSO_SKIP_SELECTORS, self._GOOGLE_SSO_SKIP_LABELS)
            if control is None:
                logger.info("Google passkey prompt is visible but no safe skip control was found yet")
                return False
            try:
                self.click_element(control, "skip_google_passkey_enrollment")
            except UiCouldNotClickElementException:
                logger.info("Google passkey skip control is not interactable yet; waiting for the account page to settle")
                return False
            logger.info("Skipped the optional Google passkey enrollment prompt")
            return True

        if identity_page:
            control = self._find_google_sso_control(self._GOOGLE_SSO_CONTINUE_SELECTORS, self._GOOGLE_SSO_CONTINUE_LABELS)
            if control is None:
                logger.info("Google identity confirmation is visible but no safe continue control was found yet")
                return False
            try:
                self.click_element(control, "continue_google_identity_confirmation")
            except UiCouldNotClickElementException:
                logger.info("Google identity confirmation control is not interactable yet; waiting for the account page to settle")
                return False
            logger.info("Continued past the Google identity confirmation page")
            return True

        return False

    def google_workspace_sso_browser_flow_completed(self) -> bool:
        """Return whether Google's SSO handoff has returned to the Meet host."""
        current_location = urlparse(str(getattr(self.driver, "current_url", "") or ""))
        return current_location.hostname in self._GOOGLE_MEET_HOSTS

    def submit_google_meet_saml_confirmation_if_needed(self) -> bool:
        """Confirm Google's account-to-Workspace SAML handoff when shown.

        Google may display an account confirmation page after the identifier is
        submitted. Its button label is localized, while the confirmation page
        exposes stable button semantics (`confirm`, `jsname`, or submit type).
        Restrict this action to Google's SAML confirmation route and use those
        semantics instead of matching translated text.
        """
        current_url = str(getattr(self.driver, "current_url", "") or "")
        current_location = urlparse(current_url)
        if current_location.hostname not in {"accounts.google.com", "www.google.com"}:
            return False
        if not current_location.path.endswith(self._GOOGLE_SAML_CONFIRMATION_PATH_SUFFIX):
            return False

        selectors = (
            "#confirm",
            "button[jsname='LgbsSe']",
            "[role='button'][jsname='LgbsSe']",
            "button[type='submit']",
            "input[type='submit']",
        )

        def find_confirmation_control(driver):
            for selector in selectors:
                for element in driver.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        if element.is_displayed() and element.is_enabled():
                            return element
                    except (StaleElementReferenceException, WebDriverException):
                        continue
            return False

        try:
            confirmation_control = WebDriverWait(self.driver, 3).until(find_confirmation_control)
            confirmation_control.click()
        except (TimeoutException, ElementNotInteractableException, NoSuchElementException, StaleElementReferenceException, WebDriverException) as exc:
            logger.warning("Google SAML account confirmation page did not expose a usable continuation control: %s", exc.__class__.__name__)
            return False

        logger.info("Confirmed the allocated Google account on Google's SAML confirmation page")
        return True

    def login_to_google_meet_account(self):
        # A login retry is a retry of the same bot/account, not an instruction to
        # rotate through the account pool. Reusing the short-lived SSO session
        # avoids turning one transient Google challenge into several account
        # sign-ins from the same runtime.
        if self.google_meet_bot_login_session is None:
            self.google_meet_bot_login_session = self.create_google_meet_bot_login_session_callback()
        if not self.google_meet_bot_login_session:
            raise UiLoginAttemptFailedException("No Google Meet bot login session was allocated", "create_google_meet_bot_login_session")
        logger.info("Logging in to Google Meet account")
        session_id = self.google_meet_bot_login_session.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise UiLoginAttemptFailedException("Google Meet SSO session id is missing", "login_to_google_meet_account")
        google_meet_set_cookie_url = get_google_meet_set_cookie_url(session_id)
        logger.info("Navigating to Google Meet set-cookie URL")
        self.driver.get(google_meet_set_cookie_url)
        self.verify_google_meet_sso_session_cookie(session_id)

        # Google evaluates the allowed-domain hint while the ServiceLogin
        # request is created. Install it before the Meet/root navigation, then
        # remove it after the SSO handoff so it never leaks into Meet RPCs.
        self.configure_google_workspace_sso_allowed_domains_header()
        self.navigate_to_google_workspace_sso_entry()
        # Once the identifier form is visible, the managed-domain hint has
        # already served its purpose. Remove it before the native Next submit
        # so Google's account/IdP navigation is not polluted by a custom
        # cross-origin request header.
        self.clear_google_workspace_sso_allowed_domains_header()
        identifier_submitted = self.submit_google_meet_account_identifier_if_needed()
        current_location = urlparse(str(getattr(self.driver, "current_url", "") or ""))
        if (
            current_location.hostname in {"accounts.google.com", "www.google.com"}
            and "/v3/signin/identifier" in current_location.path
            and not identifier_submitted
        ):
            raise UiLoginAttemptFailedException(
                "Google account identifier did not advance the SSO flow",
                "submit_google_meet_account_identifier_if_needed",
            )
        # Continue through Google's interactive confirmation pages until the
        # browser has actually returned to Meet. Auth cookies can be written
        # before the speedbump/passkey redirect finishes, so cookies alone are
        # not a safe handoff boundary.
        start_waiting_at = time.time()
        try:
            sso_handoff_timeout_seconds = max(
                30,
                int(os.getenv("GOOGLE_MEET_SSO_HANDOFF_TIMEOUT_SECONDS", str(self._GOOGLE_SSO_HANDOFF_TIMEOUT_SECONDS))),
            )
        except ValueError:
            sso_handoff_timeout_seconds = self._GOOGLE_SSO_HANDOFF_TIMEOUT_SECONDS
        auth_cookies_seen = False
        meet_handoff_observed_at = None
        next_interstitial_action_at = 0.0
        while True:
            auth_cookies_seen = self.has_google_cookies_that_indicate_logged_in(self.driver) or auth_cookies_seen
            now = time.monotonic()
            browser_is_on_meet = self.google_workspace_sso_browser_flow_completed()
            if auth_cookies_seen and browser_is_on_meet:
                if meet_handoff_observed_at is None:
                    meet_handoff_observed_at = now
                    logger.info(
                        "Google SSO handoff reached Meet; waiting for the browser location to remain stable"
                    )
                elif now - meet_handoff_observed_at >= self._GOOGLE_SSO_MEET_STABILITY_SECONDS:
                    break
            else:
                # Google can briefly expose the Meet host while an unconfirmed
                # SAML speedbump is still redirecting. Reset the stability
                # window whenever the browser leaves Meet so a transient URL
                # observation can never complete authentication.
                meet_handoff_observed_at = None
                if now >= next_interstitial_action_at:
                    interstitial_advanced = self.advance_google_workspace_sso_interstitial_if_needed()
                    if interstitial_advanced:
                        # Give a successful native click time to navigate before
                        # trying the same control again. Intercepted clicks return
                        # False and are retried on the next polling iteration.
                        next_interstitial_action_at = now + self._GOOGLE_SSO_INTERSTITIAL_RETRY_SECONDS
            logger.info(
                "Waiting for Google SSO handoff to return to Meet (auth_cookies=%s location=%s)",
                auth_cookies_seen,
                self._safe_browser_location_for_log(),
            )
            if time.time() - start_waiting_at > sso_handoff_timeout_seconds:
                logger.warning("Google SSO handoff timed out after %s seconds (auth_cookies=%s location=%s)", sso_handoff_timeout_seconds, auth_cookies_seen, self._safe_browser_location_for_log())
                self.log_google_login_timeout_diagnostics()
                raise UiLoginAttemptFailedException("Google SSO did not return to Meet after authentication", "login_to_google_meet_account")
            time.sleep(1)

        self.clear_google_workspace_sso_allowed_domains_header()
        logger.info("Google SSO handoff returned to Meet with auth cookies (location=%s)", self._safe_browser_location_for_log())

    def has_google_cookies_that_indicate_logged_in(self, driver) -> bool:
        google_auth_cookie_names = {
            "SID",
            "HSID",
            "SSID",
            "APISID",
            "SAPISID",
            "__Secure-1PSID",
            "__Secure-3PSID",
            "__Secure-1PAPISID",
            "__Secure-3PAPISID",
            "SIDCC",
        }

        cookies = driver.get_cookies()
        names = {c.get("name") for c in cookies if c.get("name")}
        any_google_auth_cookies_present = bool(names & google_auth_cookie_names)
        logger.warning(f"Cookie names: {names}. Any Google auth cookies present: {any_google_auth_cookies_present}.")
        return any_google_auth_cookies_present

    def grant_google_meet_browser_permissions(self):
        """Grant Meet media permissions before SSO and again on the target page."""
        meeting_location = urlparse(str(getattr(self, "meeting_url", "") or ""))
        if meeting_location.scheme != "https" or not meeting_location.netloc:
            raise UiLoginAttemptFailedException(
                "Google Meet meeting URL is invalid",
                "grant_google_meet_browser_permissions",
            )

        meeting_origin = f"{meeting_location.scheme}://{meeting_location.netloc}"
        self.driver.execute_cdp_cmd(
            "Browser.grantPermissions",
            {
                "origin": meeting_origin,
                "permissions": [
                    "geolocation",
                    "audioCapture",
                    "displayCapture",
                    "videoCapture",
                ],
            },
        )
        logger.info("Granted Google Meet browser media permissions origin=%s", meeting_origin)

    # returns nothing if succeeded, raises an exception if failed
    def attempt_to_join_meeting(self):
        self.grant_google_meet_browser_permissions()

        if self.google_meet_bot_login_is_available and self.google_meet_bot_login_should_be_used:
            self.login_to_google_meet_account_with_retries()

        layout_to_select = self.get_layout_to_select()

        self.driver.get(self.meeting_url)

        self.grant_google_meet_browser_permissions()

        self.check_if_meeting_is_found()

        self.wait_for_google_meet_preview_initialization()

        self.fill_out_name_input()

        self.turn_off_media_inputs()

        logger.info("Waiting for the 'Ask to join' or 'Join now' button...")
        join_button = self.locate_element(
            step="join_button",
            condition=EC.presence_of_element_located((By.XPATH, self.join_now_button_selector())),
            wait_time_seconds=60,
        )
        logger.info("Clicking the join button...")
        self.click_element(join_button, "join_button")

        self.click_captions_button()

        self.wait_for_host_if_needed()

        self.set_layout(layout_to_select)

        if self.disable_incoming_video:
            self.disable_incoming_video_in_ui()

        if self.google_meet_closed_captions_language:
            self.select_language(self.google_meet_closed_captions_language)

        if os.getenv("DO_NOT_RECORD_MEETING_REACTIONS") == "true":
            self.turn_off_reactions()

        self.ready_to_show_bot_image()

    def scroll_element_into_view(self, element, step):
        try:
            actions = ActionChains(self.driver)
            actions.move_to_element(element).perform()
            logger.info(f"Scrolled element into view for {step}")
        except Exception as e:
            logger.warning(f"Error scrolling element into view for {step}")
            raise UiCouldNotLocateElementException(
                "Error scrolling element into view",
                step,
                e,
            )

    def select_language(self, language):
        logger.info(f"Selecting language: {language}")
        logger.info("Waiting for the more options button...")
        MORE_OPTIONS_BUTTON_SELECTOR = 'button[jsname="NakZHc"][aria-label="More options"]'
        more_options_button = self.locate_element(
            step="more_options_button_for_language_selection",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, MORE_OPTIONS_BUTTON_SELECTOR)),
            wait_time_seconds=6,
        )
        logger.info("Clicking the more options button...")
        self.click_element(more_options_button, "more_options_button")

        logger.info("Waiting for the settings list item...")
        settings_list_item = self.locate_element(
            step="settings_list_item",
            condition=EC.presence_of_element_located((By.XPATH, '//li[.//span[text()="Settings"]]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the settings list item...")
        self.click_element(settings_list_item, "settings_list_item")

        logger.info("Waiting for the captions button")
        self.locate_element(
            step="captions_button",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, 'button[jsname="z4Tpl"][aria-label="Captions"]')),
            wait_time_seconds=6,
        )

        # Uses javascript to select the language, bypassing the need for the dropdown to be visible
        click_language_option_result = self.driver.execute_script("return clickLanguageOption(arguments[0]);", language)
        logger.info(f"click_language_option_result: {click_language_option_result}")
        if not click_language_option_result:
            raise UiCouldNotLocateElementException(f"Could not find language option {language}", "language_option")

        logger.info("Waiting for the close button")
        close_button = self.locate_element(
            step="close_button_for_language_selection",
            condition=EC.presence_of_element_located((By.CSS_SELECTOR, 'button[aria-label="Close dialog"]')),
            wait_time_seconds=6,
        )
        logger.info("Clicking the close button")
        self.click_element(close_button, "close_button")

    def click_leave_button(self):
        logger.info("Waiting for the leave button")
        num_attempts = 5
        for attempt_index in range(num_attempts):
            leave_button = WebDriverWait(self.driver, 16).until(
                EC.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        'button[jsname="CQylAd"][aria-label="Leave call"]',
                    )
                )
            )
            logger.info("Clicking the leave button")
            try:
                leave_button.click()
                return
            except Exception as e:
                last_attempt = attempt_index == num_attempts - 1
                if last_attempt:
                    raise e
                logger.warning("Error clicking leave button. Retrying...")
