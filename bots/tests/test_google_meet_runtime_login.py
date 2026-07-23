import os
from unittest.mock import patch

from django.test import Client, TestCase

from accounts.models import Organization
from bots.bot_sso_utils import create_google_meet_bot_login_session_for_bot
from bots.google_workspace_sso import create_signing_certificate
from bots.internal_views import _serialize_bot_runtime_snapshot
from bots.models import (
    Bot,
    BotRuntimeLease,
    BotRuntimeProviderTypes,
    GoogleMeetBotLogin,
    GoogleMeetBotLoginGroup,
    GoogleWorkspaceSsoSigningCertificate,
    GoogleWorkspaceSsoTenant,
    Project,
)
from attendee.settings.database import django_database_config


class GoogleMeetRuntimeLoginTests(TestCase):
    def setUp(self):
        self.sso_environment = patch.dict(
            os.environ,
            {"GOOGLE_MEET_SSO_FACADE_BASE_URL": "https://api.example.test/api/v1/integrations/google-workspace/meetbot-sso"},
        )
        self.sso_environment.start()
        self.addCleanup(self.sso_environment.stop)
        organization = Organization.objects.create(name="Workspace test organization")
        self.project = Project.objects.create(name="Workspace test project", organization=organization)
        self.bot = Bot.objects.create(
            project=self.project,
            name="Workspace bot",
            meeting_url="https://meet.google.com/abc-defg-hij",
            settings={"google_meet_settings": {"use_login": True}},
        )
        self.lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.VPS_DOCKER,
        )
        group = GoogleMeetBotLoginGroup.objects.create(project=self.project)
        self.login = GoogleMeetBotLogin.objects.create(
            group=group,
            workspace_domain="example.com",
            email="meetbot@example.com",
        )
        self.tenant = GoogleWorkspaceSsoTenant.objects.create(
            project=self.project,
            workspace_domain="example.com",
            idp_entity_id="https://api.example.test/api/v1/integrations/google-workspace/meetbot-sso/tenants/example",
            google_sp_entity_id="https://accounts.google.com/o/saml2?idpid=test",
            google_acs_url="https://accounts.google.com/a/example.com/acs",
            is_active=True,
        )
        self.signing_certificate = create_signing_certificate(
            tenant=self.tenant,
            state=GoogleWorkspaceSsoSigningCertificate.States.ACTIVE,
        )

    @patch("bots.bot_sso_utils.create_google_meet_sign_in_session", return_value="runtime-sso-session")
    def test_session_allocation_returns_only_opaque_session_and_public_identity(self, create_session):
        result = create_google_meet_bot_login_session_for_bot(self.bot)

        self.assertEqual(
            result,
            {
                "session_id": "runtime-sso-session",
                "login_email": "meetbot@example.com",
                "login_domain": "example.com",
            },
        )
        create_session.assert_called_once_with(self.bot, self.login, self.tenant, self.signing_certificate)
        self.login.refresh_from_db()
        self.assertIsNotNone(self.login.last_used_at)

    @patch(
        "bots.internal_views.create_google_meet_bot_login_session_for_bot",
        return_value={
            "session_id": "runtime-sso-session",
            "login_email": "meetbot@example.com",
            "login_domain": "example.com",
        },
    )
    def test_runtime_endpoint_requires_lease_token_and_returns_login_session(self, create_session):
        client = Client()
        path = f"/internal/bot-runtime-leases/{self.lease.id}/google-meet-login-session"

        unauthorized_response = client.post(path)
        self.assertEqual(unauthorized_response.status_code, 401)

        response = client.post(path, HTTP_AUTHORIZATION=f"Bearer {self.lease.shutdown_token}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session_id"], "runtime-sso-session")
        create_session.assert_called_once_with(self.bot)

    def test_runtime_bootstrap_exposes_login_availability_without_credentials(self):
        snapshot = _serialize_bot_runtime_snapshot(self.bot, self.lease)

        bot_payload = snapshot["bot"]
        self.assertTrue(bot_payload["google_meet_bot_login_available"])
        self.assertNotIn("private_key", bot_payload)
        self.assertNotIn("cert", bot_payload)

    @patch.dict(
        "os.environ",
        {
            "DB__URL": "postgresql+asyncpg://db-user:db-password@postgres:5432/voxella_api",
            "DATABASE_URL": "postgresql://legacy-user:legacy-password@legacy-db:5432/legacy",
        },
        clear=False,
    )
    def test_service_database_url_takes_precedence_over_shared_api_database(self):
        config = django_database_config(conn_max_age=600, conn_health_checks=True, ssl_require=False)

        self.assertEqual(config["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(config["HOST"], "legacy-db")
        self.assertEqual(config["NAME"], "legacy")
        self.assertEqual(config["CONN_MAX_AGE"], 600)
