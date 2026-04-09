import json
import tarfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
import tomllib

from django.test import Client, TestCase, override_settings

from accounts.models import Organization
from bots.bots_api_utils import BotCreationSource, create_bot
from bots.bot_controller.bot_controller import RuntimeBotEventManagerProxy
from bots.management.commands.clean_up_bots_with_heartbeat_timeout_or_that_never_launched import Command as CleanupCommand
from bots.management.commands.sync_gcp_runtime_capacity import Command as SyncGCPRuntimeCapacityCommand
from bots.models import ApiKey, AudioChunk, Bot, BotEvent, BotEventTypes, BotMediaRequest, BotMediaRequestMediaTypes, BotMediaRequestStates, BotRuntimeLease, BotRuntimeLeaseStatuses, BotRuntimeProviderTypes, BotStates, MediaBlob, Participant, ParticipantEvent, ParticipantEventTypes, Project, Recording, RecordingFormats, RecordingManager, RecordingTypes, RuntimeCapacityProviders, RuntimeCapacitySnapshot, TranscriptionTypes, Utterance
from bots.runtime_providers.gcp_compute_engine import GCPComputeInstanceProvider
from bots.runtime_providers.host_runtime import runtime_container_env
from bots.runtime_snapshot import RuntimeBotSnapshot
from bots.webhook_utils import sign_payload


@override_settings(SITE_DOMAIN="app.example.com", SECURE_SSL_REDIRECT=False, SECURE_PROXY_SSL_HEADER=None)
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
    def test_provision_bot_creates_lease_and_records_network_metadata(self, mock_compute_v1):
        mock_instances_client = MagicMock()
        mock_zone_operations_client = MagicMock()
        mock_compute_v1.InstancesClient.return_value = mock_instances_client
        mock_compute_v1.ZoneOperationsClient.return_value = mock_zone_operations_client
        mock_compute_v1.AccessConfig.return_value = MagicMock()

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
        self.assertTrue(lease.provider_instance_id.startswith("attendee-gcp-host-asia-southeast1-"))
        self.assertEqual(lease.region, "asia-southeast1")
        self.assertFalse(lease.metadata["request"]["private_only"])
        self.assertEqual(lease.metadata["host"]["zone"], "asia-southeast1-b")
        self.assertEqual(lease.metadata["slot"]["index"], 0)

        _, kwargs = mock_instances_client.insert.call_args
        self.assertEqual(kwargs["project"], "test-project")
        self.assertEqual(kwargs["zone"], "asia-southeast1-b")
        self.assertNotIn("request_id", kwargs)

    @patch.dict(
        "os.environ",
        {
            "GCP_PROJECT_ID": "test-project",
            "GCP_BOT_DEFAULT_REGION": "asia-southeast1",
            "GCP_BOT_SOURCE_IMAGE": "projects/test-project/global/images/attendee-bot-image",
            "GCP_BOT_PRIVATE_ONLY": "true",
        },
        clear=False,
    )
    @patch("bots.runtime_providers.gcp_compute_engine.compute_v1")
    def test_build_instance_omits_external_ip_when_private_only(self, mock_compute_v1):
        mock_compute_v1.InstancesClient.return_value = MagicMock()
        mock_images_client = MagicMock()
        mock_image = MagicMock()
        mock_image.disk_size_gb = 30
        mock_images_client.get.return_value = mock_image
        mock_compute_v1.ImagesClient.return_value = mock_images_client
        mock_compute_v1.ZoneOperationsClient.return_value = MagicMock()
        mock_compute_v1.AttachedDiskInitializeParams.return_value = MagicMock()
        mock_compute_v1.AttachedDisk.return_value = MagicMock()
        network_interface = MagicMock()
        mock_compute_v1.NetworkInterface.return_value = network_interface

        provider = GCPComputeInstanceProvider()
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        provider._build_instance(self.bot, "attendee-gcp-host-asia-southeast1-test", lease, zone="asia-southeast1-b", region="asia-southeast1")

        mock_compute_v1.AccessConfig.assert_not_called()

    @patch.dict(
        "os.environ",
        {
            "GCP_PROJECT_ID": "test-project",
            "GCP_BOT_REGIONS": "asia-southeast1",
            "GCP_BOT_DEFAULT_REGION": "asia-southeast1",
            "GCP_BOT_REGION_ZONES_JSON": '{"asia-southeast1": ["asia-southeast1-b"]}',
            "GCP_BOT_SOURCE_IMAGE": "projects/test-project/global/images/attendee-bot-image",
        },
        clear=False,
    )
    @patch("bots.runtime_providers.gcp_compute_engine.compute_v1")
    def test_provision_bot_retries_after_stale_instance_conflict(self, mock_compute_v1):
        mock_instances_client = MagicMock()
        mock_zone_operations_client = MagicMock()
        mock_compute_v1.InstancesClient.return_value = mock_instances_client
        mock_compute_v1.ZoneOperationsClient.return_value = mock_zone_operations_client

        provider = GCPComputeInstanceProvider()
        provider._build_instance = MagicMock(return_value=MagicMock())
        provider._wait_for_operation = MagicMock()

        lease = provider.provision_bot(self.bot)
        duplicate = provider.provision_bot(self.bot)

        self.assertEqual(lease.provider, BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE)
        self.assertEqual(duplicate.id, lease.id)
        self.assertEqual(mock_instances_client.insert.call_count, 1)

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
            "GCP_BOT_SOURCE_IMAGE_PROJECT": "shared-images",
        },
        clear=False,
    )
    @patch("bots.runtime_providers.gcp_compute_engine.compute_v1")
    def test_get_or_create_lease_accepts_long_source_image_family_path(self, mock_compute_v1):
        mock_compute_v1.InstancesClient.return_value = MagicMock()
        mock_compute_v1.ZoneOperationsClient.return_value = MagicMock()

        provider = GCPComputeInstanceProvider()
        lease = provider.get_or_create_lease(self.bot)

        self.assertEqual(
            lease.snapshot_id,
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

        startup_script = provider._startup_script("attendee-gcp-host-asia-southeast1-test", lease, zone="asia-southeast1-b", region="asia-southeast1")

        self.assertIn("cat >/etc/attendee/runtime-agent.env <<'EOF_AGENT_ENV'", startup_script)
        self.assertIn("systemctl enable --now attendee-runtime-agent.service", startup_script)
        self.assertIn("systemctl restart attendee-runtime-agent.service", startup_script)
        self.assertIn("systemctl daemon-reload", startup_script)
        self.assertIn("MEETBOT_RUNTIME_HOST_NAME=attendee-gcp-host-asia-southeast1-test", startup_script)
        self.assertNotIn("export MEETBOT_RUNTIME_HOST_NAME", startup_script)
        self.assertNotIn("sync_attendee_source_archive", startup_script)
        self.assertNotIn("sync_attendee_repo", startup_script)
        self.assertNotIn("attendee-bot-runner <<'EOF_RUNNER'", startup_script)

    @patch.dict(
        "os.environ",
        {
            "GCP_PROJECT_ID": "test-project",
            "GCP_BOT_SOURCE_IMAGE_FAMILY": "attendee-bot-golden",
            "GCP_BOT_ALLOW_RUNTIME_BOOTSTRAP": "true",
        },
        clear=False,
    )
    @patch("bots.runtime_providers.gcp_compute_engine.compute_v1")
    def test_startup_script_can_fallback_to_runtime_bootstrap_when_enabled(self, mock_compute_v1):
        mock_compute_v1.InstancesClient.return_value = MagicMock()
        mock_compute_v1.ZoneOperationsClient.return_value = MagicMock()

        provider = GCPComputeInstanceProvider()
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        startup_script = provider._startup_script("attendee-gcp-host-asia-southeast1-test", lease, zone="asia-southeast1-b", region="asia-southeast1")

        self.assertIn("cat >/usr/local/bin/attendee-runtime-agent <<'EOF_AGENT'", startup_script)
        self.assertIn("sync_attendee_source_archive() {", startup_script)

    @patch.dict(
        "os.environ",
        {
            "GCP_PROJECT_ID": "test-project",
            "GCP_BOT_SOURCE_IMAGE": "projects/test-project/global/images/attendee-bot-image",
            "GCP_BOT_BOOT_DISK_GB": "30",
        },
        clear=False,
    )
    @patch("bots.runtime_providers.gcp_compute_engine.compute_v1")
    def test_build_instance_uses_source_image_minimum_disk_size(self, mock_compute_v1):
        mock_instances_client = MagicMock()
        mock_images_client = MagicMock()
        mock_zone_operations_client = MagicMock()
        mock_compute_v1.InstancesClient.return_value = mock_instances_client
        mock_compute_v1.ImagesClient.return_value = mock_images_client
        mock_compute_v1.ZoneOperationsClient.return_value = mock_zone_operations_client

        mock_image = MagicMock()
        mock_image.disk_size_gb = 100
        mock_images_client.get.return_value = mock_image

        provider = GCPComputeInstanceProvider()
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        provider._build_instance(self.bot, "attendee-gcp-host-asia-southeast1-test", lease, zone="asia-southeast1-b", region="asia-southeast1")

        self.assertEqual(mock_images_client.get.call_args.kwargs["project"], "test-project")
        self.assertEqual(mock_images_client.get.call_args.kwargs["image"], "attendee-bot-image")
        self.assertEqual(mock_compute_v1.AttachedDiskInitializeParams.call_args.kwargs["disk_size_gb"], 100)

    def test_attendee_bot_runner_defines_timestamp_before_first_use(self):
        script = Path("scripts/digitalocean/attendee-bot-runner.sh").read_text()
        self.assertLess(script.index("timestamp() {"), script.index("RUNNER_STARTED_AT=\"$(timestamp)\""))
        self.assertLess(script.index("epoch_ms() {"), script.index("RUNNER_STARTED_AT_MS=\"$(epoch_ms)\""))

    @patch.dict(
        "os.environ",
        {
            "GCP_PROJECT_ID": "test-project",
            "GCP_BOT_SOURCE_IMAGE_FAMILY": "attendee-bot-golden",
            "DATABASE_URL": "postgres://user:pass@db.internal/attendee",
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

        startup_script = provider._startup_script("attendee-gcp-host-asia-southeast1-test", lease, zone="asia-southeast1-b", region="asia-southeast1")

        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", startup_script)
        self.assertNotIn("/var/lib/attendee-gcloud/application_default_credentials.json", startup_script)

    @patch.dict(
        "os.environ",
        {
            "GCP_PROJECT_ID": "test-project",
            "GCP_BOT_SOURCE_IMAGE_FAMILY": "attendee-bot-golden",
            "DATABASE_URL": "postgres://user:pass@db.internal/attendee",
        },
        clear=False,
    )
    @patch("bots.runtime_providers.gcp_compute_engine.compute_v1")
    def test_serialized_runtime_env_excludes_database_url_and_includes_runtime_urls(self, mock_compute_v1):
        mock_compute_v1.InstancesClient.return_value = MagicMock()
        mock_compute_v1.ZoneOperationsClient.return_value = MagicMock()

        provider = GCPComputeInstanceProvider()
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        runtime_env = provider._serialized_runtime_env(self.bot, lease)

        self.assertNotIn("DATABASE_URL", runtime_env)
        self.assertIn("BOT_RUNTIME_BOOTSTRAP_URL", runtime_env)
        self.assertIn("BOT_RUNTIME_CONTROL_URL", runtime_env)
        self.assertIn("DJANGO_SETTINGS_MODULE", runtime_env)

    @patch.dict(
        "os.environ",
        {
            "GCP_PROJECT_ID": "test-project",
            "GCP_BOT_SOURCE_IMAGE_FAMILY": "attendee-bot-golden",
            "BOT_RUNTIME_ALLOW_BOOTSTRAP": "true",
        },
        clear=False,
    )
    @patch("bots.runtime_providers.gcp_compute_engine.compute_v1")
    def test_serialized_runtime_env_includes_source_archive_url_only_when_bootstrap_enabled(self, mock_compute_v1):
        mock_compute_v1.InstancesClient.return_value = MagicMock()
        mock_compute_v1.ZoneOperationsClient.return_value = MagicMock()

        provider = GCPComputeInstanceProvider()
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        runtime_env = provider._serialized_runtime_env(self.bot, lease)

        self.assertIn("BOT_RUNTIME_SOURCE_ARCHIVE_URL", runtime_env)

    @patch.dict(
        "os.environ",
        {
            "GCP_PROJECT_ID": "test-project",
            "GCP_BOT_SOURCE_IMAGE_FAMILY": "attendee-bot-golden",
            "BOT_RUNTIME_REDIS_URL": "rediss://:token@ad.voxstudio.me:6363",
            "REDIS_URL": "redis://redis:6379/5",
        },
        clear=False,
    )
    @patch("bots.runtime_providers.gcp_compute_engine.compute_v1")
    def test_serialized_runtime_env_prefers_bot_runtime_redis_url(self, mock_compute_v1):
        mock_compute_v1.InstancesClient.return_value = MagicMock()
        mock_compute_v1.ZoneOperationsClient.return_value = MagicMock()

        provider = GCPComputeInstanceProvider()
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        runtime_env = provider._serialized_runtime_env(self.bot, lease)

        self.assertIn("REDIS_URL=rediss://:token@ad.voxstudio.me:6363", runtime_env)
        self.assertNotIn("REDIS_URL=redis://redis:6379/5", runtime_env)

    @patch.dict(
        "os.environ",
        {
            "BOT_RUNTIME_REDIS_URL": "rediss://:token@ad.voxstudio.me:6363",
            "ATTENDEE_REPO_DIR": "/opt/attendee",
        },
        clear=False,
    )
    def test_runtime_agent_env_file_contents_uses_plain_assignments(self):
        from bots.runtime_providers.host_runtime import runtime_agent_env_file_contents

        env_contents = runtime_agent_env_file_contents(
            "attendee-gcp-host-asia-southeast1-test",
            "meetbot:runtime:commands:attendee-gcp-host-asia-southeast1-test",
        )

        self.assertIn("MEETBOT_RUNTIME_HOST_NAME=attendee-gcp-host-asia-southeast1-test", env_contents)
        self.assertIn("REDIS_URL=rediss://:token@ad.voxstudio.me:6363", env_contents)
        self.assertNotIn("export ", env_contents)

    def test_bootstrap_view_returns_runtime_snapshot(self):
        session_id = "019d61ee-39d9-700b-80d8-1d554c5b5b70"
        self.bot.settings = {
            "runtime_settings": {"region": "asia-southeast1"},
            "callback_settings": {
                "recording_complete": {
                    "url": "http://api:8000/v2/meeting/app/bot/recording/complete",
                    "signing_secret": "api-secret",
                }
            },
        }
        self.bot.metadata = {"session_id": session_id}
        self.bot.save(update_fields=["settings", "metadata", "updated_at"])

        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        response = self.client.get(
            f"/internal/bot-runtime-leases/{lease.id}/bootstrap",
            HTTP_AUTHORIZATION=f"Bearer {lease.shutdown_token}",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["bot"]["object_id"], self.bot.object_id)
        self.assertEqual(payload["lease"]["id"], lease.id)
        self.assertEqual(payload["project"]["object_id"], self.project.object_id)
        self.assertEqual(payload["bot"]["settings"]["recording_settings"]["transport"], "r2_chunks")
        self.assertEqual(payload["bot"]["settings"]["recording_settings"]["format"], RecordingFormats.MP3)
        self.assertEqual(payload["bot"]["settings"]["recording_settings"]["audio_chunk_prefix"], f"customer_audio/{self.project.object_id}/{session_id}/chunks")
        self.assertEqual(payload["bot"]["settings"]["recording_settings"]["audio_raw_path"], f"customer_audio/{self.project.object_id}/{session_id}/original.m4a")
        self.assertTrue(
            payload["bot"]["settings"]["callback_settings"]["recording_complete"]["url"].endswith(
                f"/internal/bot-runtime-leases/{lease.id}/recording-complete"
            )
        )
        self.assertTrue(payload["bot"]["settings"]["callback_settings"]["recording_complete"]["url"].startswith("https://"))
        self.assertEqual(
            payload["bot"]["settings"]["callback_settings"]["recording_complete"]["signing_secret"],
            lease.shutdown_token,
        )
        self.assertEqual(
            payload["bot"]["settings"]["callback_settings"]["recording_complete"]["upstream_signing_secret"],
            "api-secret",
        )
        runtime_bot = RuntimeBotSnapshot(payload)
        self.assertEqual(runtime_bot.id, self.bot.id)
        self.assertEqual(runtime_bot.project.object_id, self.project.object_id)
        self.assertEqual(runtime_bot.recording_complete_signing_secret(), lease.shutdown_token)
        self.assertEqual(runtime_bot.recording_complete_upstream_signing_secret(), "api-secret")

    @patch.dict("os.environ", {"MEETBOT_RUNTIME_API_BASE_URL": "https://api.voxstudio.me"}, clear=False)
    def test_bootstrap_view_uses_runtime_api_facade_for_recording_complete_url(self):
        self.bot.settings = {
            "runtime_settings": {"region": "asia-southeast1"},
            "callback_settings": {
                "recording_complete": {
                    "url": "http://api:8000/v2/meeting/app/bot/recording/complete",
                    "signing_secret": "api-secret",
                }
            },
        }
        self.bot.metadata = {"session_id": "session-1"}
        self.bot.save(update_fields=["settings", "metadata", "updated_at"])

        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        response = self.client.get(
            f"/internal/bot-runtime-leases/{lease.id}/bootstrap",
            HTTP_AUTHORIZATION=f"Bearer {lease.shutdown_token}",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            payload["bot"]["settings"]["callback_settings"]["recording_complete"]["url"],
            f"https://api.voxstudio.me/internal/attendee-runtime-leases/{lease.id}/recording-complete",
        )

    @patch.dict("os.environ", {"ATTENDEE_INTERNAL_SERVICE_KEY": "svc-key"}, clear=False)
    def test_bootstrap_view_accepts_internal_service_key(self):
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        response = self.client.get(
            f"/internal/bot-runtime-leases/{lease.id}/bootstrap",
            HTTP_X_INTERNAL_SERVICE_KEY="svc-key",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["lease"]["id"], lease.id)

    @patch.dict("os.environ", {"MEETBOT_RUNTIME_API_BASE_URL": "https://api.voxstudio.me"}, clear=False)
    def test_runtime_container_env_points_runtime_calls_to_api_facade(self):
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.VPS_DOCKER,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        env = runtime_container_env(
            self.bot,
            lease,
            host_name="myvps",
            slot_index=1,
            provider=BotRuntimeProviderTypes.VPS_DOCKER,
        )

        self.assertEqual(
            env["BOT_RUNTIME_BOOTSTRAP_URL"],
            f"https://api.voxstudio.me/internal/attendee-runtime-leases/{lease.id}/bootstrap",
        )
        self.assertEqual(
            env["BOT_RUNTIME_CONTROL_URL"],
            f"https://api.voxstudio.me/internal/attendee-runtime-leases/{lease.id}/control",
        )
        self.assertNotIn("BOT_RUNTIME_SOURCE_ARCHIVE_URL", env)
        self.assertEqual(
            env["LEASE_CALLBACK_URL"],
            f"https://api.voxstudio.me/internal/attendee-runtime-leases/{lease.id}/complete",
        )

    @patch("bots.internal_views.requests.post")
    def test_recording_complete_view_forwards_signed_callback_to_upstream(self, mock_post):
        mock_post.return_value = SimpleNamespace(status_code=202, text="accepted")
        self.bot.settings = {
            "runtime_settings": {"region": "asia-southeast1"},
            "callback_settings": {
                "recording_complete": {
                    "url": "http://api:8000/v2/meeting/app/bot/recording/complete",
                    "signing_secret": "api-secret",
                }
            },
        }
        self.bot.save(update_fields=["settings", "updated_at"])

        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        payload = {
            "idempotency_key": "test-idempotency",
            "trigger": "recording.complete",
            "bot_id": self.bot.object_id,
            "provider": "google",
            "data": {
                "session_id": "session-1",
                "audio": {
                    "chunk_paths": ["customer_audio/test/chunks/chunk_0000.webm"],
                    "chunk_count": 1,
                    "chunk_ext": "webm",
                    "chunk_mime_type": "audio/webm",
                    "chunk_interval_ms": 5000,
                    "duration_sec": 7,
                    "raw_path": "customer_audio/test/original.m4a",
                },
            },
        }
        signature = sign_payload(payload, lease.shutdown_token)

        response = self.client.post(
            f"/internal/bot-runtime-leases/{lease.id}/recording-complete",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_WEBHOOK_SIGNATURE=signature,
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        expected_upstream_signature = sign_payload(payload, "api-secret")
        mock_post.assert_called_once_with(
            "http://api:8000/v2/meeting/app/bot/recording/complete",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Attendee-Runtime/1.0",
                "X-Webhook-Signature": expected_upstream_signature,
            },
            timeout=20,
        )

    def test_control_view_returns_control_snapshot(self):
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        response = self.client.get(
            f"/internal/bot-runtime-leases/{lease.id}/control",
            HTTP_AUTHORIZATION=f"Bearer {lease.shutdown_token}",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("media_requests", payload)
        self.assertIn("chat_message_requests", payload)

    def test_source_archive_view_returns_git_tracked_files(self):
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        response = self.client.get(
            f"/internal/bot-runtime-leases/{lease.id}/source-archive",
            HTTP_AUTHORIZATION=f"Bearer {lease.shutdown_token}",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/gzip")
        archive_bytes = b"".join(response.streaming_content)
        self.assertGreater(len(archive_bytes), 0)
        with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r:gz") as archive:
            self.assertIn("bots/runtime_providers/gcp_compute_engine.py", archive.getnames())

    @patch("bots.internal_views.subprocess.run")
    def test_source_archive_includes_git_untracked_files(self, mock_run):
        tracked_result = SimpleNamespace(stdout=b"bots/runtime_providers/gcp_compute_engine.py\x00")
        untracked_result = SimpleNamespace(stdout=b"bots/tasks/launch_joining_bot_task.py\x00")
        mock_run.side_effect = [tracked_result, untracked_result]

        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        response = self.client.get(
            f"/internal/bot-runtime-leases/{lease.id}/source-archive",
            HTTP_AUTHORIZATION=f"Bearer {lease.shutdown_token}",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        archive_bytes = b"".join(response.streaming_content)
        with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r:gz") as archive:
            self.assertIn("bots/tasks/launch_joining_bot_task.py", archive.getnames())

    def test_bot_events_view_applies_state_transition(self):
        self.bot.state = BotStates.READY
        self.bot.save(update_fields=["state", "updated_at"])
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        response = self.client.post(
            f"/internal/bot-runtime-leases/{lease.id}/bot-events",
            data='{"event_type":%d,"event_metadata":{"source":"test"}}' % BotEventTypes.JOIN_REQUESTED,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {lease.shutdown_token}",
            secure=True,
        )

        self.assertEqual(response.status_code, 201)
        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.JOINING)
        self.assertEqual(BotEvent.objects.filter(bot=self.bot, event_type=BotEventTypes.JOIN_REQUESTED).count(), 1)

    def test_participant_events_view_creates_participant_and_event(self):
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        response = self.client.post(
            f"/internal/bot-runtime-leases/{lease.id}/participants/events",
            data='{"participant_uuid":"speaker-1","participant_full_name":"Speaker One","participant_is_the_bot":false,"participant_is_host":true,"event_type":%d,"timestamp_ms":12345,"event_data":{}}' % ParticipantEventTypes.JOIN,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {lease.shutdown_token}",
            secure=True,
        )

        self.assertEqual(response.status_code, 201)
        participant = Participant.objects.get(bot=self.bot, uuid="speaker-1")
        self.assertEqual(participant.full_name, "Speaker One")
        self.assertEqual(ParticipantEvent.objects.filter(participant=participant, event_type=ParticipantEventTypes.JOIN).count(), 1)

    def test_captions_view_creates_utterance_without_object_id(self):
        recording = Recording.objects.create(
            bot=self.bot,
            recording_type=RecordingTypes.NO_RECORDING,
            transcription_type=TranscriptionTypes.NON_REALTIME,
            is_default_recording=True,
        )
        RecordingManager.set_recording_in_progress(recording)
        RecordingManager.set_recording_transcription_in_progress(recording)
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        response = self.client.post(
            f"/internal/bot-runtime-leases/{lease.id}/captions",
            data='{"participant_uuid":"speaker-1","participant_full_name":"Speaker One","participant_is_the_bot":false,"participant_is_host":true,"source_uuid_suffix":"caption-1","text":"Hello captions","timestamp_ms":12345,"duration_ms":1500}',
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {lease.shutdown_token}",
            secure=True,
        )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertIn("utterance_id", payload)
        self.assertNotIn("utterance_object_id", payload)
        self.assertEqual(Utterance.objects.filter(recording=recording, source_uuid__endswith="caption-1").count(), 1)

    def test_audio_chunks_view_creates_utterance_without_object_id(self):
        recording = Recording.objects.create(
            bot=self.bot,
            recording_type=RecordingTypes.NO_RECORDING,
            transcription_type=TranscriptionTypes.NON_REALTIME,
            is_default_recording=True,
        )
        RecordingManager.set_recording_in_progress(recording)
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        with patch("bots.tasks.process_utterance_task.process_utterance.delay") as mock_process_utterance_delay:
            response = self.client.post(
                f"/internal/bot-runtime-leases/{lease.id}/audio-chunks",
                data='{"participant_uuid":"speaker-1","participant_full_name":"Speaker One","participant_is_the_bot":false,"participant_is_host":true,"audio_blob_remote_file":"audio/chunk-1.pcm","timestamp_ms":12345,"duration_ms":1500,"sample_rate":16000}',
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {lease.shutdown_token}",
                secure=True,
            )

        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertIn("utterance_id", payload)
        self.assertNotIn("utterance_object_id", payload)
        self.assertEqual(AudioChunk.objects.filter(recording=recording, audio_blob_remote_file="audio/chunk-1.pcm").count(), 1)
        self.assertEqual(Utterance.objects.filter(recording=recording, audio_chunk__audio_blob_remote_file="audio/chunk-1.pcm").count(), 1)
        mock_process_utterance_delay.assert_called_once()

    def test_recording_file_view_updates_recording_file(self):
        recording = Recording.objects.create(
            bot=self.bot,
            recording_type=RecordingTypes.AUDIO_AND_VIDEO,
            transcription_type=TranscriptionTypes.NON_REALTIME,
            is_default_recording=True,
        )
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        response = self.client.post(
            f"/internal/bot-runtime-leases/{lease.id}/recordings/{recording.id}/file",
            data='{"file":"s3://vox-video/bot-test.mp4","first_buffer_timestamp_ms":98765}',
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {lease.shutdown_token}",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        recording.refresh_from_db()
        self.assertEqual(recording.file.name, "s3://vox-video/bot-test.mp4")
        self.assertEqual(recording.first_buffer_timestamp_ms, 98765)

    def test_runtime_bot_event_proxy_updates_local_state(self):
        controller = SimpleNamespace(
            bot_runtime_lease_id=123,
            runtime_api_client=MagicMock(),
            bot_in_db=SimpleNamespace(state=BotStates.JOINED_RECORDING),
            runtime_control=SimpleNamespace(bot_state=BotStates.JOINED_RECORDING),
        )
        controller.runtime_api_client.post_bot_event.return_value = {
            "event_id": 99,
            "old_state": BotStates.JOINED_RECORDING,
            "new_state": BotStates.POST_PROCESSING,
            "created_at": "2026-03-31T00:00:00+00:00",
        }

        proxy = RuntimeBotEventManagerProxy(controller)
        event = proxy.create_event(
            bot=controller.bot_in_db,
            event_type=BotEventTypes.MEETING_ENDED,
            event_metadata={"reason": "meeting-ended"},
        )

        self.assertEqual(controller.bot_in_db.state, BotStates.POST_PROCESSING)
        self.assertEqual(controller.runtime_control.bot_state, BotStates.POST_PROCESSING)
        self.assertEqual(event.new_state, BotStates.POST_PROCESSING)
        controller.runtime_api_client.post_bot_event.assert_called_once()

    def test_heartbeat_view_updates_bot_heartbeat(self):
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )

        response = self.client.post(
            f"/internal/bot-runtime-leases/{lease.id}/heartbeat",
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {lease.shutdown_token}",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        self.bot.refresh_from_db()
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)
        payload = response.json()
        self.assertIsNotNone(payload["first_heartbeat_at"])
        self.assertIsNotNone(payload["last_heartbeat_at"])

    def test_media_request_status_view_updates_state(self):
        lease = BotRuntimeLease.objects.create(
            bot=self.bot,
            provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE,
            status=BotRuntimeLeaseStatuses.PROVISIONING,
        )
        media_blob = MediaBlob.objects.create(project=self.project, blob=b"\x89PNG\r\n\x1a\n", content_type="image/png", duration_ms=0)
        media_request = BotMediaRequest.objects.create(
            bot=self.bot,
            media_type=BotMediaRequestMediaTypes.IMAGE,
            media_blob=media_blob,
        )

        response = self.client.post(
            f"/internal/bot-runtime-leases/{lease.id}/media-requests/{media_request.id}/status",
            data='{"state": %d}' % BotMediaRequestStates.PLAYING,
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {lease.shutdown_token}",
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        media_request.refresh_from_db()
        self.assertEqual(media_request.state, BotMediaRequestStates.PLAYING)

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
            secure=True,
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
            "GCP_PROJECT_ID": "test-project",
            "GCP_BOT_SOURCE_IMAGE": "projects/test-project/global/images/attendee-bot-image",
            "BOT_RUNTIME_CLASS_AUDIO_ONLY_GCP_MACHINE_TYPE": "e2-standard-2",
            "BOT_RUNTIME_CLASS_WEB_AV_STANDARD_GCP_MACHINE_TYPE": "e2-standard-4",
            "MEETBOT_GCP_VM_SLOT_CAPACITY_BY_MACHINE_TYPE_JSON": '{"e2-standard-2":2,"e2-standard-4":1}',
            "MEETBOT_GCP_VM_SLOT_CAPACITY_BY_RUNTIME_CLASS_FAMILY_JSON": '{"light":2,"web":1}',
        },
        clear=False,
    )
    @patch("bots.runtime_providers.gcp_compute_engine.compute_v1")
    def test_existing_gcp_host_must_match_machine_type_and_runtime_family(self, mock_compute_v1):
        mock_instances_client = MagicMock()
        mock_compute_v1.InstancesClient.return_value = mock_instances_client
        mock_compute_v1.ImagesClient.return_value = MagicMock()
        mock_compute_v1.ZoneOperationsClient.return_value = MagicMock()
        mock_instances_client.get.return_value = MagicMock()

        audio_bot = Bot.objects.create(
            project=self.project,
            name="Audio Bot",
            meeting_url="https://zoom.us/j/123456789",
            settings={"recording_settings": {"format": "mp3"}},
        )
        web_bot = Bot.objects.create(
            project=self.project,
            name="Web Bot",
            meeting_url="https://meet.google.com/abc-defg-hij",
        )
        provider = GCPComputeInstanceProvider()

        provider._register_host(
            "attendee-gcp-host-asia-southeast1-audio",
            "asia-southeast1",
            "asia-southeast1-b",
            machine_type="e2-standard-2",
            runtime_class_family="light",
            slot_capacity=2,
            state="active",
        )

        audio_lease = BotRuntimeLease.objects.create(bot=audio_bot, provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE)
        web_lease = BotRuntimeLease.objects.create(bot=web_bot, provider=BotRuntimeProviderTypes.GCP_COMPUTE_INSTANCE)

        audio_existing = provider._select_existing_host(
            "asia-southeast1",
            audio_bot,
            audio_lease,
            machine_type="e2-standard-2",
            runtime_class_family="light",
            slot_capacity=2,
        )
        web_existing = provider._select_existing_host(
            "asia-southeast1",
            web_bot,
            web_lease,
            machine_type="e2-standard-4",
            runtime_class_family="web",
            slot_capacity=1,
        )

        self.assertIsNotNone(audio_existing)
        self.assertIsNone(web_existing)

    def test_pyproject_splits_control_and_dev_dependencies_out_of_runtime_base(self):
        pyproject = tomllib.loads(Path("pyproject.toml").read_text())
        runtime_deps = set(pyproject["project"]["dependencies"])
        control_group = set(pyproject["dependency-groups"]["control"])
        dev_group = set(pyproject["dependency-groups"]["dev"])

        self.assertNotIn("stripe==11.6.0", runtime_deps)
        self.assertNotIn("google-cloud-compute==1.32.0", runtime_deps)
        self.assertNotIn("ruff==0.9.6", runtime_deps)
        self.assertIn("stripe==11.6.0", control_group)
        self.assertIn("google-cloud-compute==1.32.0", control_group)
        self.assertIn("ruff==0.9.6", dev_group)

    def test_bot_runtime_settings_profile_excludes_control_plane_apps(self):
        module_source = Path("attendee/settings/base.py").read_text()
        self.assertIn('ROOT_URLCONF = "attendee.runtime_urls" if IS_BOT_RUNTIME_SETTINGS else "attendee.urls"', module_source)
        self.assertIn('SETTINGS_PROFILE = "bot_runtime" if django_settings_module == "attendee.settings.bot_runtime" else "control"', module_source)
        self.assertIn('"allauth"', module_source)
        self.assertIn('"drf_spectacular"', module_source)

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
    def test_create_bot_persists_gcp_chunk_recording_settings(self):
        session_id = "019d61ee-39d9-700b-80d8-1d554c5b5b70"
        bot, error = create_bot(
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "GCP Chunk Bot",
                "runtime_settings": {"region": "us-central1"},
                "recording_settings": {
                    "transport": "r2_chunks",
                    "format": RecordingFormats.MP3,
                    "audio_chunk_prefix": f"customer_audio/{self.project.object_id}/{session_id}/chunks",
                    "audio_raw_path": f"customer_audio/{self.project.object_id}/{session_id}/original.m4a",
                },
                "callback_settings": {
                    "recording_complete": {
                        "url": "http://api:8000/v2/meeting/app/bot/recording/complete",
                        "signing_secret": "api-secret",
                    }
                },
                "metadata": {
                    "session_id": session_id,
                },
            },
            source=BotCreationSource.API,
            project=self.project,
        )
        self.assertIsNone(error)
        self.assertEqual(bot.settings["recording_settings"]["transport"], "r2_chunks")
        self.assertEqual(bot.settings["recording_settings"]["format"], RecordingFormats.MP3)
        self.assertEqual(bot.settings["recording_settings"]["audio_chunk_prefix"], f"customer_audio/{self.project.object_id}/{session_id}/chunks")
        self.assertEqual(bot.settings["recording_settings"]["audio_raw_path"], f"customer_audio/{self.project.object_id}/{session_id}/original.m4a")

    @patch.dict(
        "os.environ",
        {
            "LAUNCH_BOT_METHOD": "gcp-compute-engine",
            "GCP_BOT_REGIONS": "asia-southeast1,us-central1",
            "GCP_BOT_DEFAULT_REGION": "asia-southeast1",
        },
        clear=False,
    )
    def test_create_bot_persists_gcp_muxed_screen_recording_settings(self):
        session_id = "019d61ee-39d9-700b-80d8-1d554c5b5b70"
        bot, error = create_bot(
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "GCP Screen Chunk Bot",
                "runtime_settings": {"region": "us-central1"},
                "recording_settings": {
                    "transport": "r2_chunks",
                    "format": RecordingFormats.WEBM,
                    "audio_raw_path": f"customer_audio/{self.project.object_id}/{session_id}/original.m4a",
                    "video_chunk_prefix": f"video/{self.project.object_id}/{session_id}/chunks",
                },
                "callback_settings": {
                    "recording_complete": {
                        "url": "http://api:8000/v2/meeting/app/bot/recording/complete",
                        "signing_secret": "api-secret",
                    }
                },
                "metadata": {
                    "session_id": session_id,
                },
            },
            source=BotCreationSource.API,
            project=self.project,
        )
        self.assertIsNone(error)
        self.assertEqual(bot.settings["recording_settings"]["transport"], "r2_chunks")
        self.assertEqual(bot.settings["recording_settings"]["format"], RecordingFormats.WEBM)
        self.assertEqual(bot.settings["recording_settings"]["video_chunk_prefix"], f"video/{self.project.object_id}/{session_id}/chunks")
        self.assertEqual(bot.settings["recording_settings"]["audio_raw_path"], f"customer_audio/{self.project.object_id}/{session_id}/original.m4a")
        self.assertNotIn("audio_chunk_prefix", bot.settings["recording_settings"])

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

    @patch.dict(
        "os.environ",
        {
            "GCP_PROJECT_ID": "test-project",
            "GCP_BOT_REGIONS": "asia-southeast1,europe-east1",
            "GCP_BOT_QUOTA_METRIC": "CPUS",
        },
        clear=False,
    )
    @patch("bots.management.commands.sync_gcp_runtime_capacity.compute_v1")
    def test_sync_gcp_runtime_capacity_skips_invalid_regions(self, mock_compute_v1):
        mock_regions_client = MagicMock()
        mock_compute_v1.RegionsClient.return_value = mock_regions_client

        valid_region = MagicMock()
        valid_region.quotas = [MagicMock(metric="CPUS", limit=32, usage=8)]
        valid_region.status = "UP"
        valid_region.zones = ["asia-southeast1-b"]
        mock_regions_client.get.side_effect = [valid_region, Exception("400 invalid region")]

        SyncGCPRuntimeCapacityCommand().handle()

        snapshots = RuntimeCapacitySnapshot.objects.filter(provider=RuntimeCapacityProviders.GCP_COMPUTE_INSTANCE)
        self.assertEqual(snapshots.count(), 1)
        snapshot = snapshots.get(region="asia-southeast1")
        self.assertEqual(snapshot.quota_limit, 32)
        self.assertEqual(snapshot.effective_available, 24)


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
            secure=True,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["provider"], RuntimeCapacityProviders.GCP_COMPUTE_INSTANCE)
        self.assertEqual(payload[0]["region"], "asia-southeast1")
