from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from accounts.models import Organization
from bots.models import Bot, BotRuntimeLease, BotRuntimeLeaseStatuses, BotRuntimeProviderTypes, BotStates, Project
from bots.runtime_providers import DigitalOceanDropletProvider


@override_settings(SITE_DOMAIN="app.example.com")
class TestDigitalOceanRuntime(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.bot = Bot.objects.create(
            project=self.project,
            name="DO Bot",
            meeting_url="https://meet.google.com/abc-defg-hij",
        )

    @patch.dict(
        "os.environ",
        {
            "DROPLET_API_KEY": "dop_v1_test",
            "DO_BOT_REGION": "sgp1",
            "DO_BOT_SIZE_SLUG": "s-4vcpu-8gb",
            "DO_BOT_SNAPSHOT_ID": "snapshot-123",
            "DO_BOT_TAGS": "attendee-bot,env-prod",
        },
        clear=False,
    )
    @patch("bots.runtime_providers.digitalocean.requests.request")
    def test_provision_bot_creates_lease_and_calls_digitalocean_api(self, mock_request):
        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {
            "droplet": {
                "id": 987654,
                "name": "attendee-bot-test",
                "status": "new",
            }
        }
        mock_request.return_value = mock_response

        provider = DigitalOceanDropletProvider()
        lease = provider.provision_bot(self.bot)

        self.assertEqual(lease.provider, BotRuntimeProviderTypes.DIGITALOCEAN_DROPLET)
        self.assertEqual(lease.status, BotRuntimeLeaseStatuses.PROVISIONING)
        self.assertEqual(lease.provider_instance_id, "987654")
        self.assertEqual(lease.snapshot_id, "snapshot-123")

        _, kwargs = mock_request.call_args
        self.assertEqual(kwargs["method"], "POST")
        self.assertIn("/droplets", kwargs["url"])
        self.assertEqual(kwargs["json"]["image"], "snapshot-123")
        self.assertEqual(kwargs["json"]["region"], "sgp1")
        self.assertEqual(kwargs["json"]["size"], "s-4vcpu-8gb")
        self.assertIn("user_data", kwargs["json"])
        self.assertIn("LEASE_CALLBACK_URL", kwargs["json"]["user_data"])

    @patch.dict(
        "os.environ",
        {
            "GOOGLE_APPLICATION_CREDENTIALS": "/var/lib/attendee-gcloud/application_default_credentials.json",
        },
        clear=False,
    )
    def test_user_data_does_not_leak_google_application_credentials(self):
        provider = DigitalOceanDropletProvider()
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.DIGITALOCEAN_DROPLET,
        )

        user_data = provider._user_data(self.bot, lease)

        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", user_data)
        self.assertNotIn("/var/lib/attendee-gcloud/application_default_credentials.json", user_data)

    @patch("bots.internal_views.get_runtime_provider")
    def test_completion_callback_requires_token_and_requests_deletion(self, mock_get_runtime_provider):
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.DIGITALOCEAN_DROPLET,
            provider_instance_id="555",
        )
        mock_provider = MagicMock()
        mock_get_runtime_provider.return_value = mock_provider

        unauthorized = self.client.post(
            f"/internal/bot-runtime-leases/{lease.id}/complete",
            data='{"droplet_id":"555"}',
            content_type="application/json",
        )
        self.assertEqual(unauthorized.status_code, 401)

        response = self.client.post(
            f"/internal/bot-runtime-leases/{lease.id}/complete",
            data='{"droplet_id":"555","exit_code":0,"reason":"process_exit"}',
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {lease.shutdown_token}",
        )

        self.assertEqual(response.status_code, 200)
        mock_provider.delete_lease.assert_called_once()

    @patch("bots.management.commands.clean_up_bots_with_heartbeat_timeout_or_that_never_launched.get_runtime_provider")
    def test_cleanup_command_deletes_digitalocean_lease_on_heartbeat_timeout(self, mock_get_runtime_provider):
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.DIGITALOCEAN_DROPLET,
            provider_instance_id="555",
        )
        mock_provider = MagicMock()
        mock_get_runtime_provider.return_value = mock_provider

        eleven_minutes_ago = 1
        self.bot.first_heartbeat_timestamp = eleven_minutes_ago
        self.bot.last_heartbeat_timestamp = eleven_minutes_ago
        self.bot.state = BotStates.JOINED_RECORDING
        self.bot.save()

        with patch.dict("os.environ", {"LAUNCH_BOT_METHOD": "digitalocean-droplet"}):
            from bots.management.commands.clean_up_bots_with_heartbeat_timeout_or_that_never_launched import Command

            command = Command()
            command.handle()

        lease.refresh_from_db()
        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)
        mock_provider.delete_lease.assert_called_once_with(lease)
