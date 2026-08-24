import json
import os
from unittest.mock import MagicMock, call, patch

from django.test import SimpleTestCase
from selenium.common.exceptions import ElementClickInterceptedException
from selenium.webdriver.common.keys import Keys

from bots.google_meet_bot_adapter.google_meet_bot_adapter import GoogleMeetBotAdapter
from bots.google_meet_bot_adapter.google_meet_ui_methods import GoogleMeetUIMethods


class GoogleWorkspaceSsoBrowserHeaderTests(SimpleTestCase):
    def setUp(self):
        self.adapter = object.__new__(GoogleMeetUIMethods)
        self.adapter.driver = MagicMock()
        self.adapter.google_meet_bot_login_session = {"login_domain": "VoxStudio.me."}

    def test_uses_the_validated_login_domain_by_default(self):
        with patch.dict(
            os.environ,
            {
                "GOOGLE_MEET_SSO_ALLOWED_DOMAINS_HEADER_ENABLED": "true",
                "GOOGLE_MEET_SSO_ALLOWED_DOMAINS": "",
            },
            clear=False,
        ):
            configured = self.adapter.configure_google_workspace_sso_allowed_domains_header()

        self.assertTrue(configured)
        self.adapter.driver.execute_cdp_cmd.assert_has_calls(
            [
                call("Network.enable", {}),
                call(
                    "Network.setExtraHTTPHeaders",
                    {"headers": {"X-GoogApps-AllowedDomains": "voxstudio.me"}},
                ),
            ]
        )

    def test_honors_an_explicit_allowlist_for_the_allocated_domain(self):
        with patch.dict(
            os.environ,
            {
                "GOOGLE_MEET_SSO_ALLOWED_DOMAINS_HEADER_ENABLED": "true",
                "GOOGLE_MEET_SSO_ALLOWED_DOMAINS": "other.example, voxstudio.me, other.example",
            },
            clear=False,
        ):
            configured = self.adapter.configure_google_workspace_sso_allowed_domains_header()

        self.assertTrue(configured)
        self.adapter.driver.execute_cdp_cmd.assert_called_with(
            "Network.setExtraHTTPHeaders",
            {"headers": {"X-GoogApps-AllowedDomains": "other.example,voxstudio.me"}},
        )

    def test_does_not_send_the_header_when_disabled(self):
        with patch.dict(
            os.environ,
            {"GOOGLE_MEET_SSO_ALLOWED_DOMAINS_HEADER_ENABLED": "false"},
            clear=False,
        ):
            configured = self.adapter.configure_google_workspace_sso_allowed_domains_header()

        self.assertFalse(configured)
        self.adapter.driver.execute_cdp_cmd.assert_not_called()

    def test_does_not_send_the_experimental_header_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            configured = self.adapter.configure_google_workspace_sso_allowed_domains_header()

        self.assertFalse(configured)
        self.adapter.driver.execute_cdp_cmd.assert_not_called()

    def test_does_not_send_the_header_when_allowlist_excludes_login_domain(self):
        with patch.dict(
            os.environ,
            {
                "GOOGLE_MEET_SSO_ALLOWED_DOMAINS_HEADER_ENABLED": "true",
                "GOOGLE_MEET_SSO_ALLOWED_DOMAINS": "other.example",
            },
            clear=False,
        ):
            configured = self.adapter.configure_google_workspace_sso_allowed_domains_header()

        self.assertFalse(configured)
        self.adapter.driver.execute_cdp_cmd.assert_not_called()

    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.get_google_meet_set_cookie_url", return_value="https://sso.example.test/set-cookie")
    def test_login_retry_reuses_its_existing_allocated_sso_session(self, _get_set_cookie_url):
        self.adapter.google_meet_bot_login_session = {
            "session_id": "existing-session",
            "login_domain": "voxstudio.me",
        }
        self.adapter.create_google_meet_bot_login_session_callback = MagicMock()
        self.adapter.driver.get_cookie.return_value = {"name": "google_meet_sign_in_session_id", "value": "existing-session"}
        self.adapter.configure_google_workspace_sso_allowed_domains_header = MagicMock()
        self.adapter.navigate_to_google_workspace_sso_entry = MagicMock()
        self.adapter.has_google_cookies_that_indicate_logged_in = MagicMock(return_value=True)
        self.adapter.driver.current_url = "https://meet.google.com/home"

        with (
            patch.object(GoogleMeetUIMethods, "_GOOGLE_SSO_MEET_STABILITY_SECONDS", 0),
            patch("bots.google_meet_bot_adapter.google_meet_ui_methods.time.sleep"),
        ):
            self.adapter.login_to_google_meet_account()

        self.adapter.create_google_meet_bot_login_session_callback.assert_not_called()
        self.adapter.driver.get.assert_called_once_with("https://sso.example.test/set-cookie")
        self.adapter.configure_google_workspace_sso_allowed_domains_header.assert_called_once_with()
        self.adapter.navigate_to_google_workspace_sso_entry.assert_called_once_with()

    def test_login_fails_before_google_redirect_when_facade_does_not_write_session_cookie(self):
        self.adapter.google_meet_bot_login_session = {
            "session_id": "existing-session",
            "login_domain": "voxstudio.me",
        }
        self.adapter.driver.get_cookie.return_value = None
        self.adapter.configure_google_workspace_sso_allowed_domains_header = MagicMock()

        with self.assertRaisesRegex(Exception, "session cookie was not written"):
            self.adapter.login_to_google_meet_account()

        self.adapter.configure_google_workspace_sso_allowed_domains_header.assert_not_called()

    def test_submits_allocated_account_identifier_on_google_meet_sign_in_page(self):
        self.adapter.google_meet_bot_login_session = {
            "session_id": "existing-session",
            "login_email": "meetbot@example.com",
            "login_domain": "voxstudio.me",
        }
        self.adapter.driver.current_url = "https://accounts.google.com/v3/signin/identifier?continue=meet"
        identifier_input = MagicMock()
        identifier_input.is_displayed.return_value = True
        identifier_input.is_enabled.return_value = True
        next_button = MagicMock()
        next_button.is_displayed.return_value = True
        next_button.is_enabled.return_value = True
        self.adapter.driver.find_elements.side_effect = [[identifier_input], [next_button], []]
        def execute_script(script, *_args):
            if script == "arguments[0].click();":
                self.adapter.driver.current_url = "https://accounts.google.com/v3/signin/samlconfirmaccount"

        self.adapter.driver.execute_script.side_effect = execute_script

        self.assertTrue(self.adapter.submit_google_meet_account_identifier_if_needed())

        identifier_input.clear.assert_called_once_with()
        self.assertEqual(identifier_input.send_keys.call_args_list[0], call("meetbot@example.com"))
        self.assertEqual(identifier_input.send_keys.call_args_list[1], call(Keys.TAB))
        self.assertEqual(self.adapter.driver.execute_script.call_args_list[-1], call("arguments[0].click();", next_button))
        next_button.click.assert_called_once_with()

    def test_reports_identifier_form_when_google_did_not_advance(self):
        self.adapter.google_meet_bot_login_session = {
            "session_id": "existing-session",
            "login_email": "meetbot@example.com",
            "login_domain": "voxstudio.me",
        }
        self.adapter.driver.current_url = "https://accounts.google.com/v3/signin/identifier?continue=meet"
        identifier_input = MagicMock()
        identifier_input.is_displayed.return_value = True
        identifier_input.is_enabled.return_value = True
        next_button = MagicMock()
        next_button.is_displayed.return_value = True
        next_button.is_enabled.return_value = True
        self.adapter.driver.find_elements.side_effect = [[identifier_input], [next_button], [identifier_input]]
        self.adapter.driver.execute_script.side_effect = lambda *_args: setattr(
            self.adapter.driver,
            "current_url",
            "https://accounts.google.com/v3/signin/identifier?continue=meet&retry=1",
        )

        self.assertFalse(self.adapter.submit_google_meet_account_identifier_if_needed())

    def test_continues_past_google_identity_confirmation(self):
        self.adapter.driver.current_url = "https://accounts.google.com/v3/signin/challenge/continue"
        self.adapter.driver.find_element.return_value.text = "Verify that it’s you"
        continue_button = MagicMock()
        continue_button.is_displayed.return_value = True
        continue_button.is_enabled.return_value = True
        self.adapter.driver.find_elements.side_effect = lambda _by, selector: [continue_button] if "jsname='LgbsSe'" in selector else []

        self.assertTrue(self.adapter.advance_google_workspace_sso_interstitial_if_needed())
        continue_button.click.assert_called_once_with()
        self.adapter.driver.execute_script.assert_not_called()

    def test_waits_for_meet_green_room_loading_layer_to_finish(self):
        loading_layer = MagicMock()
        loading_layer.is_displayed.side_effect = [True, False]
        self.adapter.driver.find_elements.return_value = [loading_layer]
        self.adapter.driver.find_element.return_value.text = ""

        with patch("bots.google_meet_bot_adapter.google_meet_ui_methods.WebDriverWait") as wait_class:
            wait = wait_class.return_value

            def wait_until(condition):
                self.assertFalse(condition(self.adapter.driver))
                self.assertTrue(condition(self.adapter.driver))
                return True

            wait.until.side_effect = wait_until

            self.adapter.wait_for_google_meet_preview_initialization()

        wait.until.assert_called_once()

    def test_green_room_loading_marker_is_not_an_sso_control(self):
        loading_layer = MagicMock()
        loading_layer.is_displayed.return_value = True
        self.adapter.driver.current_url = "https://accounts.google.com/v3/signin/challenge/continue"
        self.adapter.driver.find_elements.return_value = [loading_layer]
        self.adapter.driver.find_element.return_value.text = "Getting ready... You'll be able to join in just a moment"

        self.assertTrue(self.adapter.google_meet_green_room_is_loading())
        self.assertFalse(self.adapter.google_workspace_sso_browser_flow_completed())
        self.adapter.driver.find_elements.side_effect = lambda _by, _selector: []
        self.assertIsNone(
            self.adapter._find_google_sso_control(("#missing",), {"continue"})
        )
        self.adapter.driver.execute_script.assert_not_called()

    def test_speedbump_path_is_recognized_without_localized_page_text(self):
        self.adapter.driver.current_url = "https://accounts.google.com/speedbump/samlconfirmaccount"
        self.adapter.driver.find_element.return_value.text = ""
        continue_button = MagicMock()
        continue_button.is_displayed.return_value = True
        continue_button.is_enabled.return_value = True
        self.adapter.driver.find_elements.side_effect = lambda _by, selector: [continue_button] if selector == "#confirm" else []

        self.assertTrue(self.adapter.advance_google_workspace_sso_interstitial_if_needed())
        continue_button.click.assert_called_once_with()

    def test_speedbump_click_is_retried_after_a_loading_layer_intercepts_it(self):
        self.adapter.driver.current_url = "https://accounts.google.com/speedbump/samlconfirmaccount"
        self.adapter.driver.find_element.return_value.text = ""
        continue_button = MagicMock()
        continue_button.is_displayed.return_value = True
        continue_button.is_enabled.return_value = True
        continue_button.click.side_effect = [ElementClickInterceptedException(), None]
        self.adapter.driver.find_elements.side_effect = lambda _by, selector: [continue_button] if selector == "#confirm" else []

        self.assertFalse(self.adapter.advance_google_workspace_sso_interstitial_if_needed())
        self.assertTrue(self.adapter.advance_google_workspace_sso_interstitial_if_needed())
        self.assertEqual(continue_button.click.call_count, 2)

    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.time.sleep")
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.time.monotonic")
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.get_google_meet_set_cookie_url", return_value="https://sso.example.test/set-cookie")
    def test_login_requires_a_stable_meet_handoff_after_a_transient_meet_url(
        self,
        _get_set_cookie_url,
        monotonic,
        _sleep,
    ):
        self.adapter.google_meet_bot_login_session = {
            "session_id": "existing-session",
            "login_domain": "voxstudio.me",
        }
        self.adapter.driver.get_cookie.return_value = {
            "name": "google_meet_sign_in_session_id",
            "value": "existing-session",
        }
        self.adapter.configure_google_workspace_sso_allowed_domains_header = MagicMock()
        self.adapter.navigate_to_google_workspace_sso_entry = MagicMock()
        self.adapter.submit_google_meet_account_identifier_if_needed = MagicMock(return_value=True)
        self.adapter.has_google_cookies_that_indicate_logged_in = MagicMock(return_value=True)
        self.adapter.google_workspace_sso_browser_flow_completed = MagicMock(
            side_effect=[True, False, True, True]
        )
        self.adapter.advance_google_workspace_sso_interstitial_if_needed = MagicMock(return_value=False)
        monotonic.side_effect = [10.0, 11.0, 20.0, 22.0]

        self.adapter.login_to_google_meet_account()

        self.assertEqual(self.adapter.google_workspace_sso_browser_flow_completed.call_count, 4)
        self.assertEqual(self.adapter.advance_google_workspace_sso_interstitial_if_needed.call_count, 1)

    def test_skips_google_passkey_enrollment(self):
        self.adapter.driver.current_url = "https://accounts.google.com/v3/signin/passkeyenrollment"
        self.adapter.driver.find_element.return_value.text = "Set up a passkey"
        skip_button = MagicMock()
        skip_button.is_displayed.return_value = True
        skip_button.is_enabled.return_value = True
        self.adapter.driver.find_elements.side_effect = lambda _by, selector: [skip_button] if selector == "#skip" else []

        self.assertTrue(self.adapter.advance_google_workspace_sso_interstitial_if_needed())
        skip_button.click.assert_called_once_with()

    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.get_google_meet_sign_in_url", return_value="https://sso.example.test/sign-in")
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.requests.Session")
    def test_safe_navigation_follows_google_samlredirect_to_configured_idp(self, session_class, _get_sign_in_url):
        self.adapter.google_meet_bot_login_session = {"login_domain": "voxstudio.me"}
        google_response = MagicMock(status_code=302, headers={"Location": "https://accounts.google.com/samlredirect?state=opaque"})
        accounts_response = MagicMock(status_code=302, headers={"Location": "https://sso.example.test/sign-in?SAMLRequest=opaque"})
        session_class.return_value.get.side_effect = [google_response, accounts_response]

        with patch.dict(os.environ, {"GOOGLE_MEET_SSO_ENTRYPOINT": "domain_service_login"}, clear=False):
            self.adapter.safely_navigate_to_google_workspace_sso_entry()

        self.adapter.driver.get.assert_called_once_with("https://www.google.com/a/voxstudio.me/ServiceLogin")
        self.assertEqual(session_class.return_value.get.call_count, 2)

    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.get_google_meet_sign_in_url", return_value="https://sso.example.test/sign-in")
    @patch("bots.google_meet_bot_adapter.google_meet_ui_methods.requests.Session")
    def test_safe_navigation_rejects_redirects_outside_google_and_configured_idp(self, session_class, _get_sign_in_url):
        self.adapter.google_meet_bot_login_session = {"login_domain": "voxstudio.me"}
        session_class.return_value.get.return_value = MagicMock(status_code=302, headers={"Location": "https://untrusted.example.test/redirect"})

        with self.assertRaisesRegex(Exception, "did not reach the configured IdP"):
            self.adapter.safely_navigate_to_google_workspace_sso_entry()

        self.adapter.driver.get.assert_not_called()

    def test_navigation_uses_meet_service_by_default(self):
        self.adapter.google_meet_bot_login_session = {"login_domain": "voxstudio.me"}
        self.adapter.driver.current_url = "https://meet.google.com/home"

        with patch.dict(os.environ, {}, clear=True):
            self.adapter.navigate_to_google_workspace_sso_entry()

        self.adapter.driver.get.assert_called_once_with("https://meet.google.com/")

    def test_navigation_keeps_the_target_meeting_context_for_sso(self):
        self.adapter.google_meet_bot_login_session = {"login_domain": "voxstudio.me"}
        self.adapter.meeting_url = "https://meet.google.com/abc-defg-hij"

        with patch.dict(os.environ, {}, clear=True):
            self.adapter.navigate_to_google_workspace_sso_entry()

        self.adapter.driver.get.assert_called_once_with("https://meet.google.com/abc-defg-hij")

    def test_navigation_falls_back_to_meet_service_root_without_target(self):
        self.adapter.google_meet_bot_login_session = {"login_domain": "voxstudio.me"}

        with patch.dict(os.environ, {}, clear=True):
            self.adapter.navigate_to_google_workspace_sso_entry()

        self.adapter.driver.get.assert_called_once_with("https://meet.google.com/")

    def test_navigation_keeps_an_existing_google_sign_in_redirect(self):
        self.adapter.google_meet_bot_login_session = {"login_domain": "voxstudio.me"}
        self.adapter.driver.current_url = "https://accounts.google.com/v3/signin/identifier?continue=meet"

        with patch.dict(os.environ, {}, clear=True):
            self.adapter.navigate_to_google_workspace_sso_entry()

        self.adapter.driver.get.assert_called_once_with("https://meet.google.com/")

    def test_navigation_follows_product_page_sign_in_link_when_available(self):
        self.adapter.google_meet_bot_login_session = {"login_domain": "voxstudio.me"}
        self.adapter.driver.current_url = "https://workspace.google.com/products/meet/"
        sign_in_link = MagicMock()
        sign_in_link.is_displayed.return_value = True
        sign_in_link.is_enabled.return_value = True
        sign_in_link.click.side_effect = lambda: setattr(
            self.adapter.driver,
            "current_url",
            "https://accounts.google.com/v3/signin/identifier?continue=meet",
        )
        self.adapter.driver.find_elements.return_value = [sign_in_link]

        with patch.dict(os.environ, {}, clear=True):
            self.adapter.navigate_to_google_workspace_sso_entry()

        self.adapter.driver.get.assert_called_once_with("https://meet.google.com/")
        sign_in_link.click.assert_called_once_with()

    def test_navigation_falls_back_to_meet_root_when_product_page_has_no_sign_in_link(self):
        self.adapter.google_meet_bot_login_session = {"login_domain": "voxstudio.me"}
        self.adapter.driver.current_url = "https://workspace.google.com/products/meet/"
        self.adapter.driver.find_elements.return_value = []

        with patch.dict(os.environ, {}, clear=True):
            self.adapter.navigate_to_google_workspace_sso_entry()

        self.adapter.driver.get.assert_has_calls(
            [
                call("https://meet.google.com/"),
                call("https://meet.google.com/"),
            ]
        )

    def test_confirms_google_saml_account_without_matching_localized_button_text(self):
        self.adapter.driver.current_url = "https://accounts.google.com/v3/signin/samlconfirmaccount"
        confirmation_button = MagicMock()
        confirmation_button.is_displayed.return_value = True
        confirmation_button.is_enabled.return_value = True
        self.adapter.driver.find_elements.return_value = [confirmation_button]

        self.assertTrue(self.adapter.submit_google_meet_saml_confirmation_if_needed())

        confirmation_button.click.assert_called_once_with()

    def test_navigation_rejects_gmail_entry_point(self):
        self.adapter.google_meet_bot_login_session = {"login_domain": "voxstudio.me"}

        with patch.dict(
            os.environ,
            {
                "USE_SAFE_NAVIGATION_FOR_SIGNED_IN_GOOGLE_MEET_BOTS": "false",
                "GOOGLE_MEET_SSO_ENTRYPOINT": "legacy_gmail",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(Exception, "entry point is invalid"):
                self.adapter.navigate_to_google_workspace_sso_entry()

        self.adapter.driver.get.assert_not_called()

    def test_navigation_uses_domain_service_login_when_safe_navigation_is_disabled(self):
        self.adapter.google_meet_bot_login_session = {"login_domain": "voxstudio.me"}

        with patch.dict(
            os.environ,
            {
                "USE_SAFE_NAVIGATION_FOR_SIGNED_IN_GOOGLE_MEET_BOTS": "false",
                "GOOGLE_MEET_SSO_ENTRYPOINT": "domain_service_login",
            },
            clear=False,
        ):
            self.adapter.navigate_to_google_workspace_sso_entry()

        self.adapter.driver.get.assert_called_once_with("https://www.google.com/a/voxstudio.me/ServiceLogin")

    def test_login_timeout_diagnostics_redact_query_parameters_and_email(self):
        self.adapter.driver.current_url = "https://accounts.google.com/v3/signin/continue?opaque=secret"
        self.adapter.driver.title = "Signing In"
        self.adapter.driver.find_element.return_value.text = "Please sign in as notetaker@example.com"

        with self.assertLogs("bots.google_meet_bot_adapter.google_meet_ui_methods", level="WARNING") as captured:
            self.adapter.log_google_login_timeout_diagnostics()

        output = "\n".join(captured.output)
        self.assertIn("https://accounts.google.com/v3/signin/continue", output)
        self.assertNotIn("opaque=secret", output)
        self.assertNotIn("notetaker@example.com", output)
        self.assertIn("<redacted-email>", output)

    def test_network_diagnostics_redact_query_and_cookie_values(self):
        self.adapter.google_workspace_sso_network_diagnostics_enabled = True
        self.adapter.driver.get_log.return_value = [
            {
                "message": json.dumps(
                    {
                        "message": {
                            "method": "Network.requestWillBeSent",
                            "params": {
                                "requestId": "request-1",
                                "request": {
                                    "url": "https://accounts.google.com/samlrp/acs?opaque=secret",
                                    "method": "POST",
                                    "postData": "SAMLResponse=secret&RelayState=secret",
                                },
                            },
                        }
                    }
                )
            },
            {
                "message": json.dumps(
                    {
                        "message": {
                            "method": "Network.responseReceived",
                            "params": {
                                "requestId": "request-1",
                                "response": {
                                    "url": "https://accounts.google.com/samlrp/acs?opaque=secret",
                                    "status": 302,
                                    "headers": {
                                        "Location": "https://accounts.google.com/v3/signin/continue?state=secret",
                                        "Set-Cookie": "SID=very-secret; Path=/; Secure",
                                    },
                                },
                            },
                        }
                    }
                )
            },
            {
                "message": json.dumps(
                    {
                        "message": {
                            "method": "Network.requestWillBeSentExtraInfo",
                            "params": {
                                "requestId": "request-1",
                                "headers": {
                                    "X-GoogApps-AllowedDomains": "voxstudio.me",
                                    "Cookie": "SID=very-secret",
                                },
                            },
                        }
                    }
                )
            }
        ]

        with self.assertLogs("bots.google_meet_bot_adapter.google_meet_ui_methods", level="WARNING") as captured:
            self.adapter.log_google_workspace_sso_network_diagnostics()

        output = "\n".join(captured.output)
        self.assertIn('"status":302', output)
        self.assertIn('"path":"/samlrp/acs"', output)
        self.assertIn('"method":"POST"', output)
        self.assertIn('"post_data_keys":["RelayState","SAMLResponse"]', output)
        self.assertIn('"set_cookie_names":["SID"]', output)
        self.assertIn('"allowed_domains_header_present":true', output)
        self.assertIn('"query_keys":["opaque"]', output)
        self.assertNotIn("very-secret", output)
        self.assertNotIn("voxstudio.me", output)
        self.assertNotIn("state=secret", output)
        self.assertNotIn("opaque=secret", output)


class GoogleMeetSignedInChromeProfileTests(SimpleTestCase):
    def setUp(self):
        self.adapter = object.__new__(GoogleMeetBotAdapter)
        self.adapter.google_meet_bot_login_should_be_used = True
        self.options = MagicMock()

    def test_uses_an_isolated_regular_profile_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.adapter.add_subclass_specific_chrome_options(self.options)

        self.options.add_argument.assert_not_called()

    def test_can_restore_guest_mode_explicitly(self):
        with patch.dict(os.environ, {"GOOGLE_MEET_SIGNED_IN_BOT_GUEST_MODE": "true"}, clear=True):
            self.adapter.add_subclass_specific_chrome_options(self.options)

        self.options.add_argument.assert_called_once_with("--guest")

    def test_debug_bot_enables_privacy_safe_sso_network_diagnostics(self):
        self.adapter.google_meet_bot_login_is_available = True
        self.adapter.should_create_debug_recording = True

        self.adapter.add_subclass_specific_chrome_options(self.options)

        self.options.set_capability.assert_called_once_with("goog:loggingPrefs", {"performance": "ALL"})
        self.assertTrue(self.adapter.google_workspace_sso_network_diagnostics_enabled)
