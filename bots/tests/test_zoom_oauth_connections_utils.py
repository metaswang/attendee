from unittest.mock import Mock, patch

from django.test import TestCase

from accounts.models import Organization
from bots.models import (
    Bot,
    Project,
    ZoomMeetingToZoomOAuthConnectionMapping,
    ZoomOAuthApp,
    ZoomOAuthConnection,
    ZoomOAuthConnectionStates,
)
from bots.runtime_snapshot import (
    RuntimeCredentialCollection,
    RuntimeOrganizationSnapshot,
    RuntimeProjectSnapshot,
    RuntimeZoomMeetingToZoomOAuthConnectionMappingCollection,
    RuntimeZoomMeetingToZoomOAuthConnectionMappingSnapshot,
    RuntimeZoomOAuthAppCollection,
    RuntimeZoomOAuthConnectionCollection,
    RuntimeZoomOAuthConnectionSnapshot,
    RuntimeZoomOAuthAppSnapshot,
)
from bots.zoom_oauth_connections_utils import (
    ZoomAPIAuthenticationError,
    _get_access_token,
    _handle_zoom_api_authentication_error,
    compute_zoom_webhook_validation_response,
    get_zoom_tokens_via_zoom_oauth_app,
)


class TestComputeZoomWebhookValidationResponse(TestCase):
    """Test the compute_zoom_webhook_validation_response function."""

    def test_returns_correct_format(self):
        """Test that the response has correct format."""
        plain_token = "test_plain_token"
        secret_token = "test_secret_token"

        result = compute_zoom_webhook_validation_response(plain_token, secret_token)

        self.assertIn("plainToken", result)
        self.assertIn("encryptedToken", result)
        self.assertEqual(result["plainToken"], plain_token)

    def test_encrypted_token_is_correct_hmac(self):
        """Test that the encrypted token is correctly computed."""
        import hashlib
        import hmac

        plain_token = "qgg8vlvZRS6UYooatFL8Aw"
        secret_token = "my_webhook_secret"

        result = compute_zoom_webhook_validation_response(plain_token, secret_token)

        # Compute expected value
        expected_hash = hmac.new(secret_token.encode("utf-8"), plain_token.encode("utf-8"), hashlib.sha256).hexdigest()

        self.assertEqual(result["encryptedToken"], expected_hash)

    def test_different_tokens_produce_different_results(self):
        """Test that different plain tokens produce different encrypted tokens."""
        secret_token = "test_secret"

        result1 = compute_zoom_webhook_validation_response("token1", secret_token)
        result2 = compute_zoom_webhook_validation_response("token2", secret_token)

        self.assertNotEqual(result1["encryptedToken"], result2["encryptedToken"])

    def test_different_secrets_produce_different_results(self):
        """Test that different secrets produce different encrypted tokens."""
        plain_token = "test_token"

        result1 = compute_zoom_webhook_validation_response(plain_token, "secret1")
        result2 = compute_zoom_webhook_validation_response(plain_token, "secret2")

        self.assertNotEqual(result1["encryptedToken"], result2["encryptedToken"])


class TestHandleZoomApiAuthenticationError(TestCase):
    """Test the _handle_zoom_api_authentication_error function."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id")
        self.zoom_oauth_app.set_credentials({"client_secret": "test_secret"})
        self.zoom_oauth_connection = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=self.zoom_oauth_app,
            user_id="test_user_id",
            account_id="test_account_id",
            state=ZoomOAuthConnectionStates.CONNECTED,
        )

    @patch("bots.zoom_oauth_connections_utils.trigger_webhook")
    def test_sets_state_to_disconnected(self, mock_trigger_webhook):
        """Test that authentication error sets connection state to DISCONNECTED."""
        error = ZoomAPIAuthenticationError("Invalid credentials")

        _handle_zoom_api_authentication_error(self.zoom_oauth_connection, error)

        self.zoom_oauth_connection.refresh_from_db()
        self.assertEqual(self.zoom_oauth_connection.state, ZoomOAuthConnectionStates.DISCONNECTED)

    @patch("bots.zoom_oauth_connections_utils.trigger_webhook")
    def test_sets_connection_failure_data(self, mock_trigger_webhook):
        """Test that authentication error sets connection failure data."""
        error = ZoomAPIAuthenticationError("Invalid credentials")

        _handle_zoom_api_authentication_error(self.zoom_oauth_connection, error)

        self.zoom_oauth_connection.refresh_from_db()
        self.assertIsNotNone(self.zoom_oauth_connection.connection_failure_data)
        self.assertIn("error", self.zoom_oauth_connection.connection_failure_data)
        self.assertIn("Invalid credentials", self.zoom_oauth_connection.connection_failure_data["error"])
        self.assertIn("timestamp", self.zoom_oauth_connection.connection_failure_data)

    @patch("bots.zoom_oauth_connections_utils.trigger_webhook")
    def test_triggers_webhook(self, mock_trigger_webhook):
        """Test that authentication error triggers a webhook."""
        error = ZoomAPIAuthenticationError("Invalid credentials")

        _handle_zoom_api_authentication_error(self.zoom_oauth_connection, error)

        mock_trigger_webhook.assert_called_once()

    @patch("bots.zoom_oauth_connections_utils.trigger_webhook")
    def test_skips_if_already_disconnected(self, mock_trigger_webhook):
        """Test that no action is taken if connection is already disconnected."""
        self.zoom_oauth_connection.state = ZoomOAuthConnectionStates.DISCONNECTED
        self.zoom_oauth_connection.save()

        error = ZoomAPIAuthenticationError("Invalid credentials")

        _handle_zoom_api_authentication_error(self.zoom_oauth_connection, error)

        # Should not trigger webhook since already disconnected
        mock_trigger_webhook.assert_not_called()

        # State should remain disconnected
        self.zoom_oauth_connection.refresh_from_db()
        self.assertEqual(self.zoom_oauth_connection.state, ZoomOAuthConnectionStates.DISCONNECTED)


class TestGetZoomTokensViaZoomOAuthApp(TestCase):
    """Test the get_zoom_tokens_via_zoom_oauth_app function."""

    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

        # Create ZoomOAuthApp with credentials
        self.zoom_oauth_app = ZoomOAuthApp.objects.create(project=self.project, client_id="test_client_id")
        self.zoom_oauth_app.set_credentials({"client_secret": "test_secret"})

        # Create ZoomOAuthConnection with credentials
        self.zoom_oauth_connection = ZoomOAuthConnection.objects.create(
            zoom_oauth_app=self.zoom_oauth_app,
            user_id="test_user_id",
            account_id="test_account_id",
            is_local_recording_token_supported=True,
            is_onbehalf_token_supported=True,
        )
        self.zoom_oauth_connection.set_credentials({"refresh_token": "test_refresh_token"})

        # Create meeting mapping for local recording token lookup
        self.meeting_id = "1234567890"
        ZoomMeetingToZoomOAuthConnectionMapping.objects.create(
            zoom_oauth_app=self.zoom_oauth_app,
            zoom_oauth_connection=self.zoom_oauth_connection,
            meeting_id=self.meeting_id,
        )

    def _create_bot(self, use_web_adapter=False, onbehalf_user_id=None):
        """Helper to create a bot with specific settings."""
        settings = {"zoom_settings": {"sdk": "web" if use_web_adapter else "native"}}
        if onbehalf_user_id:
            settings["zoom_settings"]["onbehalf_token"] = {"zoom_oauth_connection_user_id": onbehalf_user_id}
        return Bot.objects.create(
            project=self.project,
            meeting_url=f"https://zoom.us/j/{self.meeting_id}",
            settings=settings,
        )

    def _mock_zoom_api_responses(self, mock_post, mock_session, local_recording_token=None, onbehalf_token=None):
        """Helper to set up mock responses for Zoom API calls."""
        # Mock token refresh response
        mock_token_response = Mock()
        mock_token_response.json.return_value = {"access_token": "mock_access_token"}
        mock_token_response.raise_for_status.return_value = None
        mock_post.return_value = mock_token_response

        # Mock API responses for local recording and onbehalf tokens
        def mock_send(request, **kwargs):
            response = Mock()
            response.raise_for_status.return_value = None
            if "local_recording" in request.url:
                response.json.return_value = {"token": local_recording_token}
            elif "token?type=onbehalf" in request.url:
                response.json.return_value = {"token": onbehalf_token}
            return response

        mock_session.return_value.__enter__ = Mock(return_value=mock_session.return_value)
        mock_session.return_value.__exit__ = Mock(return_value=False)
        mock_session.return_value.send = mock_send

    @patch("bots.zoom_oauth_connections_utils.requests.post")
    def test_runtime_snapshot_access_token_refresh_updates_in_memory_credentials(self, mock_post):
        runtime_zoom_oauth_connection = RuntimeZoomOAuthConnectionSnapshot(
            object_id=self.zoom_oauth_connection.object_id,
            user_id=self.zoom_oauth_connection.user_id,
            account_id=self.zoom_oauth_connection.account_id,
            client_id=self.zoom_oauth_app.client_id,
            client_secret=self.zoom_oauth_app.client_secret,
            credentials={"refresh_token": "test_refresh_token"},
        )

        mock_token_response = Mock()
        mock_token_response.json.return_value = {
            "access_token": "runtime_access_token",
            "refresh_token": "rotated_refresh_token",
        }
        mock_token_response.raise_for_status.return_value = None
        mock_post.return_value = mock_token_response

        access_token = _get_access_token(runtime_zoom_oauth_connection)

        self.assertEqual(access_token, "runtime_access_token")
        self.assertEqual(
            runtime_zoom_oauth_connection.get_credentials()["refresh_token"],
            "rotated_refresh_token",
        )

    @patch("bots.zoom_oauth_connections_utils._handle_zoom_api_authentication_error")
    @patch("bots.zoom_oauth_connections_utils._get_local_recording_token")
    @patch("bots.zoom_oauth_connections_utils._get_access_token")
    def test_db_backed_zoom_oauth_app_still_uses_auth_error_handler(
        self,
        mock_get_access_token,
        mock_get_local_recording_token,
        mock_handle_auth_error,
    ):
        bot = self._create_bot(use_web_adapter=False, onbehalf_user_id=None)
        mock_get_access_token.side_effect = ZoomAPIAuthenticationError("Invalid credentials")

        result = get_zoom_tokens_via_zoom_oauth_app(bot)

        self.assertIsNone(result["app_privilege_token"])
        self.assertIsNone(result["onbehalf_token"])
        mock_handle_auth_error.assert_called_once_with(self.zoom_oauth_connection, mock_get_access_token.side_effect)
        mock_get_local_recording_token.assert_not_called()

    @patch("bots.zoom_oauth_connections_utils.requests.Session")
    @patch("bots.zoom_oauth_connections_utils.requests.post")
    def test_returns_local_recording_token_when_no_onbehalf_configured(self, mock_post, mock_session):
        """Test that local recording token is fetched when no onbehalf token is configured."""
        bot = self._create_bot(use_web_adapter=False, onbehalf_user_id=None)
        self._mock_zoom_api_responses(mock_post, mock_session, local_recording_token="local_rec_token_123")

        result = get_zoom_tokens_via_zoom_oauth_app(bot)

        self.assertEqual(result["app_privilege_token"], "local_rec_token_123")
        self.assertIsNone(result["onbehalf_token"])

    @patch("bots.zoom_oauth_connections_utils.requests.Session")
    @patch("bots.zoom_oauth_connections_utils.requests.post")
    def test_returns_both_tokens_when_using_web_adapter(self, mock_post, mock_session):
        """Test that both tokens are fetched when using web adapter."""
        bot = self._create_bot(use_web_adapter=True, onbehalf_user_id="test_user_id")
        self._mock_zoom_api_responses(mock_post, mock_session, local_recording_token="local_rec_token_123", onbehalf_token="onbehalf_token_456")

        result = get_zoom_tokens_via_zoom_oauth_app(bot)

        self.assertEqual(result["app_privilege_token"], "local_rec_token_123")
        self.assertEqual(result["onbehalf_token"], "onbehalf_token_456")

    @patch("bots.zoom_oauth_connections_utils.requests.Session")
    @patch("bots.zoom_oauth_connections_utils.requests.post")
    def test_returns_both_tokens_when_using_native_adapter(self, mock_post, mock_session):
        """Test that both tokens are fetched when using native adapter."""
        bot = self._create_bot(use_web_adapter=False, onbehalf_user_id="test_user_id")
        self._mock_zoom_api_responses(mock_post, mock_session, local_recording_token="local_rec_token_123", onbehalf_token="onbehalf_token_456")

        result = get_zoom_tokens_via_zoom_oauth_app(bot)

        self.assertEqual(result["app_privilege_token"], "local_rec_token_123")
        self.assertEqual(result["onbehalf_token"], "onbehalf_token_456")

    @patch("bots.zoom_oauth_connections_utils.requests.Session")
    @patch("bots.zoom_oauth_connections_utils.requests.post")
    def test_works_with_runtime_snapshot_zoom_oauth_app(self, mock_post, mock_session):
        """Test that runtime snapshot without meeting mappings exits cleanly without ORM access."""
        runtime_zoom_oauth_app = RuntimeZoomOAuthAppSnapshot(
            object_id=self.zoom_oauth_app.object_id,
            client_id=self.zoom_oauth_app.client_id,
            credentials={"client_secret": "test_secret"},
        )
        runtime_project = RuntimeProjectSnapshot(
            id=self.project.id,
            object_id=self.project.object_id,
            name=self.project.name,
            organization=RuntimeOrganizationSnapshot(),
            credentials=RuntimeCredentialCollection([]),
            zoom_oauth_apps=RuntimeZoomOAuthAppCollection([runtime_zoom_oauth_app]),
        )
        bot = Mock()
        bot.project = runtime_project
        bot.meeting_url = f"https://zoom.us/j/{self.meeting_id}"
        bot.zoom_onbehalf_token_zoom_oauth_connection_user_id.return_value = None

        result = get_zoom_tokens_via_zoom_oauth_app(bot)

        self.assertIsNone(result["app_privilege_token"])
        self.assertIsNone(result["onbehalf_token"])
        mock_post.assert_not_called()

    @patch("bots.zoom_oauth_connections_utils.requests.Session")
    @patch("bots.zoom_oauth_connections_utils.requests.post")
    def test_works_with_runtime_snapshot_zoom_oauth_app_for_onbehalf(self, mock_post, mock_session):
        """Test that runtime snapshot zoom oauth apps work for onbehalf token lookup too."""
        runtime_zoom_oauth_connection = RuntimeZoomOAuthConnectionSnapshot(
            object_id=self.zoom_oauth_connection.object_id,
            user_id=self.zoom_oauth_connection.user_id,
            account_id=self.zoom_oauth_connection.account_id,
            client_id=self.zoom_oauth_app.client_id,
            client_secret=self.zoom_oauth_app.client_secret,
            is_local_recording_token_supported=True,
            is_onbehalf_token_supported=True,
            credentials={"refresh_token": "test_refresh_token"},
        )
        runtime_zoom_oauth_app = RuntimeZoomOAuthAppSnapshot(
            object_id=self.zoom_oauth_app.object_id,
            client_id=self.zoom_oauth_app.client_id,
            credentials={"client_secret": "test_secret"},
            zoom_oauth_connections=RuntimeZoomOAuthConnectionCollection([runtime_zoom_oauth_connection]),
            zoom_meeting_to_zoom_oauth_connection_mappings=RuntimeZoomMeetingToZoomOAuthConnectionMappingCollection(
                [
                    RuntimeZoomMeetingToZoomOAuthConnectionMappingSnapshot(
                        meeting_id=self.meeting_id,
                        zoom_oauth_connection_object_id=runtime_zoom_oauth_connection.object_id,
                    )
                ]
            ),
        )
        runtime_project = RuntimeProjectSnapshot(
            id=self.project.id,
            object_id=self.project.object_id,
            name=self.project.name,
            organization=RuntimeOrganizationSnapshot(),
            credentials=RuntimeCredentialCollection([]),
            zoom_oauth_apps=RuntimeZoomOAuthAppCollection([runtime_zoom_oauth_app]),
        )
        bot = Mock()
        bot.project = runtime_project
        bot.meeting_url = f"https://zoom.us/j/{self.meeting_id}"
        bot.zoom_onbehalf_token_zoom_oauth_connection_user_id.return_value = "test_user_id"

        self._mock_zoom_api_responses(
            mock_post,
            mock_session,
            local_recording_token="local_rec_token_789",
            onbehalf_token="onbehalf_token_789",
        )

        result = get_zoom_tokens_via_zoom_oauth_app(bot)

        self.assertEqual(result["app_privilege_token"], "local_rec_token_789")
        self.assertEqual(result["onbehalf_token"], "onbehalf_token_789")

    @patch("bots.zoom_oauth_connections_utils.requests.Session")
    @patch("bots.zoom_oauth_connections_utils.requests.post")
    def test_works_with_runtime_snapshot_zoom_oauth_app_for_local_recording(self, mock_post, mock_session):
        """Test that runtime snapshot zoom oauth apps can resolve local recording token without ORM."""
        runtime_zoom_oauth_connection = RuntimeZoomOAuthConnectionSnapshot(
            object_id=self.zoom_oauth_connection.object_id,
            user_id=self.zoom_oauth_connection.user_id,
            account_id=self.zoom_oauth_connection.account_id,
            client_id=self.zoom_oauth_app.client_id,
            client_secret=self.zoom_oauth_app.client_secret,
            is_local_recording_token_supported=True,
            is_onbehalf_token_supported=True,
            credentials={"refresh_token": "test_refresh_token"},
        )
        runtime_zoom_oauth_app = RuntimeZoomOAuthAppSnapshot(
            object_id=self.zoom_oauth_app.object_id,
            client_id=self.zoom_oauth_app.client_id,
            credentials={"client_secret": "test_secret"},
            zoom_oauth_connections=RuntimeZoomOAuthConnectionCollection([runtime_zoom_oauth_connection]),
            zoom_meeting_to_zoom_oauth_connection_mappings=RuntimeZoomMeetingToZoomOAuthConnectionMappingCollection(
                [
                    RuntimeZoomMeetingToZoomOAuthConnectionMappingSnapshot(
                        meeting_id=self.meeting_id,
                        zoom_oauth_connection_object_id=runtime_zoom_oauth_connection.object_id,
                    )
                ]
            ),
        )
        runtime_project = RuntimeProjectSnapshot(
            id=self.project.id,
            object_id=self.project.object_id,
            name=self.project.name,
            organization=RuntimeOrganizationSnapshot(),
            credentials=RuntimeCredentialCollection([]),
            zoom_oauth_apps=RuntimeZoomOAuthAppCollection([runtime_zoom_oauth_app]),
        )
        bot = Mock()
        bot.project = runtime_project
        bot.meeting_url = f"https://zoom.us/j/{self.meeting_id}"
        bot.zoom_onbehalf_token_zoom_oauth_connection_user_id.return_value = None

        self._mock_zoom_api_responses(mock_post, mock_session, local_recording_token="local_rec_token_runtime")

        result = get_zoom_tokens_via_zoom_oauth_app(bot)

        self.assertEqual(result["app_privilege_token"], "local_rec_token_runtime")
        self.assertIsNone(result["onbehalf_token"])

    @patch("bots.zoom_oauth_connections_utils.requests.Session")
    @patch("bots.zoom_oauth_connections_utils.requests.post")
    def test_runtime_snapshot_without_mapping_returns_none_without_db_lookup(self, mock_post, mock_session):
        runtime_zoom_oauth_connection = RuntimeZoomOAuthConnectionSnapshot(
            object_id=self.zoom_oauth_connection.object_id,
            user_id=self.zoom_oauth_connection.user_id,
            account_id=self.zoom_oauth_connection.account_id,
            client_id=self.zoom_oauth_app.client_id,
            client_secret=self.zoom_oauth_app.client_secret,
            is_local_recording_token_supported=True,
            is_onbehalf_token_supported=True,
            credentials={"refresh_token": "test_refresh_token"},
        )
        runtime_zoom_oauth_app = RuntimeZoomOAuthAppSnapshot(
            object_id=self.zoom_oauth_app.object_id,
            client_id=self.zoom_oauth_app.client_id,
            credentials={"client_secret": "test_secret"},
            zoom_oauth_connections=RuntimeZoomOAuthConnectionCollection([runtime_zoom_oauth_connection]),
            zoom_meeting_to_zoom_oauth_connection_mappings=RuntimeZoomMeetingToZoomOAuthConnectionMappingCollection([]),
        )
        runtime_project = RuntimeProjectSnapshot(
            id=self.project.id,
            object_id=self.project.object_id,
            name=self.project.name,
            organization=RuntimeOrganizationSnapshot(),
            credentials=RuntimeCredentialCollection([]),
            zoom_oauth_apps=RuntimeZoomOAuthAppCollection([runtime_zoom_oauth_app]),
        )
        bot = Mock()
        bot.project = runtime_project
        bot.meeting_url = f"https://zoom.us/j/{self.meeting_id}"
        bot.zoom_onbehalf_token_zoom_oauth_connection_user_id.return_value = None

        result = get_zoom_tokens_via_zoom_oauth_app(bot)

        self.assertIsNone(result["app_privilege_token"])
        self.assertIsNone(result["onbehalf_token"])
        mock_post.assert_not_called()
