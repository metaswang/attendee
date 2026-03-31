from unittest.mock import MagicMock, patch

from django.test import Client, TestCase, override_settings

from accounts.models import Organization
from bots.bots_api_utils import BotCreationSource, create_bot
from bots.management.commands.clean_up_bots_with_heartbeat_timeout_or_that_never_launched import Command as CleanupCommand
from bots.models import ApiKey, Bot, BotRuntimeLease, BotRuntimeLeaseStatuses, BotRuntimeProviderTypes, BotStates, Project, RuntimeCapacityProviders, RuntimeCapacitySnapshot
from bots.runtime_providers.gcp_compute_engine import GCPComputeInstanceProvider


@override_settings(SITE_DOMAIN="app.example.com")
class TestGCPRuntime(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.bot = Bot.objects.create(
            project=self.project,
            name="GCP Bot",
            meeting_url="https://meet.google.com/abc-defg-hij",
            settings={"runtime_settings": {"region": "asia-southeast1"}},
        )

    @patch.dict(
        "os.environ",
        {
            "GCP_PROJECT_ID": "test-project",
            "GCP_BOT_REGIONS": "asia-southeast1,us-central1",
            "GCP_BOT_DEFAULT_REGION": "asia-southeast1",
            "GCP_BOT_REGION_ZONES_JSON": '{"asia-southeast1": ["asia-southeast1-b"]}',
            "GCP_BOT_SOURCE_IMAGE": "projects/test-project/global/images/attendee-bot-image",
        },
        clear=False,
    )
    @patch("bots.runtime_providers.gcp_compute_engine.compute_v1")
    def test_provision_bot_creates_lease_and_records_private_only_metadata(self, mock_compute_v1):
        mock_instances_client = MagicMock()
        mock_zone_operations_client = MagicMock()
        mock_compute_v1.InstancesClient.return_value = mock_instances_client
        mock_compute_v1.ZoneOperationsClient.return_value = mock_zone_operations_client

        created_instance = MagicMock()
        created_instance.id = 123456
        created_instance.name = self.bot.gcp_instance_name()
        created_instance.status = "RUNNING"
        mock_instances_client.get.return_value = created_instance
        mock_instances_client.insert.return_value = MagicMock(name="operation-1")

        provider = GCPComputeInstanceProvider()
        provider._build_instance = MagicMock(return_value=MagicMock())
        provider._wait_for_operation = MagicMock()

        lease = provider.provision_bot(self.bot)

        self.assertEqual(lease.provider, BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE)
        self.assertEqual(lease.status, BotRuntimeLeaseStatuses.PROVISIONING)
        self.assertEqual(lease.provider_instance_id, self.bot.gcp_instance_name())
        self.assertEqual(lease.region, "asia-southeast1")
        self.assertTrue(lease.metadata["request"]["private_only"])

        _, kwargs = mock_instances_client.insert.call_args
        self.assertEqual(kwargs["project"], "test-project")
        self.assertEqual(kwargs["zone"], "asia-southeast1-b")
        self.assertIn("request_id", kwargs)

    @patch.dict(
        "os.environ",
        {
            "GCP_PROJECT_ID": "test-project",
            "GCP_BOT_SOURCE_IMAGE_FAMILY": "attendee-bot-golden",
            "GCP_BOT_SOURCE_IMAGE_PROJECT": "shared-images",
        },
        clear=False,
    )
    @patch("bots.runtime_providers.gcp_compute_engine.compute_v1")
    def test_source_image_uses_family_when_configured(self, mock_compute_v1):
        mock_compute_v1.InstancesClient.return_value = MagicMock()
        mock_compute_v1.ZoneOperationsClient.return_value = MagicMock()

        provider = GCPComputeInstanceProvider()

        self.assertEqual(
            provider._source_image(),
            "projects/shared-images/global/images/family/attendee-bot-golden",
        )

    @patch.dict(
        "os.environ",
        {
            "GCP_PROJECT_ID": "test-project",
            "GCP_BOT_SOURCE_IMAGE_FAMILY": "attendee-bot-golden",
        },
        clear=False,
    )
    @patch("bots.runtime_providers.gcp_compute_engine.compute_v1")
    def test_startup_script_only_writes_runtime_env_and_restarts_service(self, mock_compute_v1):
        mock_compute_v1.InstancesClient.return_value = MagicMock()
        mock_compute_v1.ZoneOperationsClient.return_value = MagicMock()

        provider = GCPComputeInstanceProvider()
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        startup_script = provider._startup_script(self.bot, lease)

        self.assertIn("cat >/etc/attendee/runtime.env <<'EOF'", startup_script)
        self.assertIn("systemctl restart attendee-bot-runner.service", startup_script)
        self.assertNotIn("systemctl enable attendee-bot-runner.service", startup_script)
        self.assertNotIn("systemctl daemon-reload", startup_script)

    @patch.dict(
        "os.environ",
        {
            "GCP_PROJECT_ID": "test-project",
            "GCP_BOT_SOURCE_IMAGE_FAMILY": "attendee-bot-golden",
            "GOOGLE_APPLICATION_CREDENTIALS": "/var/lib/attendee-gcloud/application_default_credentials.json",
        },
        clear=False,
    )
    @patch("bots.runtime_providers.gcp_compute_engine.compute_v1")
    def test_startup_script_does_not_leak_google_application_credentials(self, mock_compute_v1):
        mock_compute_v1.InstancesClient.return_value = MagicMock()
        mock_compute_v1.ZoneOperationsClient.return_value = MagicMock()

        provider = GCPComputeInstanceProvider()
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        startup_script = provider._startup_script(self.bot, lease)

        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", startup_script)
        self.assertNotIn("/var/lib/attendee-gcloud/application_default_credentials.json", startup_script)

    @patch("bots.internal_views.get_runtime_provider")
    def test_completion_callback_accepts_provider_instance_id(self, mock_get_runtime_provider):
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            provider_instance_id="instance-123",
            metadata={"instance": {"zone": "asia-southeast1-b"}},
        )
        mock_provider = MagicMock()
        mock_get_runtime_provider.return_value = mock_provider

        response = self.client.post(
            f"/internal/bot-runtime-leases/{lease.id}/complete",
            data='{"provider_instance_id":"instance-123","exit_code":0,"reason":"process_exit"}',
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {lease.shutdown_token}",
        )

        self.assertEqual(response.status_code, 200)
        mock_provider.delete_lease.assert_called_once()

    @patch("bots.management.commands.clean_up_bots_with_heartbeat_timeout_or_that_never_launched.get_runtime_provider")
    def test_cleanup_command_deletes_gcp_lease_on_heartbeat_timeout(self, mock_get_runtime_provider):
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            provider_instance_id="instance-123",
            metadata={"instance": {"zone": "asia-southeast1-b"}},
        )
        mock_provider = MagicMock()
        mock_get_runtime_provider.return_value = mock_provider

        self.bot.first_heartbeat_timestamp = 1
        self.bot.last_heartbeat_timestamp = 1
        self.bot.state = BotStates.JOINED_RECORDING
        self.bot.save()

        with patch.dict("os.environ", {"LAUNCH_BOT_METHOD": "gcp-compute-engine"}):
            CleanupCommand().handle()

        lease.refresh_from_db()
        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)
        mock_provider.delete_lease.assert_called_once_with(lease)


class TestRuntimeSettingsAndCapacity(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    @patch.dict(
        "os.environ",
        {
            "LAUNCH_BOT_METHOD": "gcp-compute-engine",
            "GCP_BOT_REGIONS": "asia-southeast1,us-central1",
            "GCP_BOT_DEFAULT_REGION": "asia-southeast1",
        },
        clear=False,
    )
    def test_create_bot_persists_runtime_region(self):
        bot, error = create_bot(
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Runtime Region Bot",
                "runtime_settings": {"region": "us-central1"},
            },
            source=BotCreationSource.API,
            project=self.project,
        )
        self.assertIsNone(error)
        self.assertEqual(bot.settings["runtime_settings"]["region"], "us-central1")

    @patch.dict(
        "os.environ",
        {
            "LAUNCH_BOT_METHOD": "gcp-compute-engine",
            "GCP_BOT_REGIONS": "asia-southeast1,us-central1",
            "GCP_BOT_DEFAULT_REGION": "asia-southeast1",
        },
        clear=False,
    )
    def test_create_bot_rejects_exhausted_region(self):
        RuntimeCapacitySnapshot.objects.create(
            provider=RuntimeCapacityProviders.GCP_COMPUTE_INSTANCE,
            region="us-central1",
            quota_limit=10,
            quota_usage=10,
            effective_available=0,
        )

        bot, error = create_bot(
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Exhausted Region Bot",
                "runtime_settings": {"region": "us-central1"},
            },
            source=BotCreationSource.API,
            project=self.project,
        )
        self.assertIsNone(bot)
        self.assertEqual(error, {"error": "Runtime capacity for region us-central1 is exhausted. Please choose a different region."})


class TestRuntimeCapacityView(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.api_key, self.api_key_plain = ApiKey.create(project=self.project, name="API Key")
        self.client = Client()

    def test_runtime_capacity_view_returns_cached_snapshots(self):
        RuntimeCapacitySnapshot.objects.create(
            provider=RuntimeCapacityProviders.GCP_COMPUTE_INSTANCE,
            region="asia-southeast1",
            quota_limit=32,
            quota_usage=4,
            soft_cap=24,
            effective_available=20,
            metadata={"zones": ["asia-southeast1-b"]},
        )

        response = self.client.get(
            "/api/v1/runtime_capacity",
            HTTP_AUTHORIZATION=f"Token {self.api_key_plain}",
            HTTP_CONTENT_TYPE="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["provider"], RuntimeCapacityProviders.GCP_COMPUTE_INSTANCE)
        self.assertEqual(payload[0]["region"], "asia-southeast1")
