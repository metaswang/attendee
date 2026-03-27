import os
import sys
from unittest.mock import MagicMock, patch

from django.test import TestCase

from bots.bots_api_utils import validate_external_media_storage_settings
from bots.launch_bot_utils import launch_bot
from bots.management.commands.clean_up_bots_with_heartbeat_timeout_or_that_never_launched import Command
from bots.modal_launcher import apply_modal_runtime_overrides, parse_recording_upload_uri
from bots.models import (
    Bot,
    BotEventManager,
    BotEventTypes,
    Credentials,
    Organization,
    Project,
)


class TestModalLaunch(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)
        self.bot = Bot.objects.create(
            project=self.project,
            name="Original Bot",
            meeting_url="https://meet.google.com/abc-defg-hij",
            settings={
                "recording_settings": {"format": "mp4"},
                "automatic_leave_settings": {},
            },
        )
        BotEventManager.create_event(self.bot, BotEventTypes.JOIN_REQUESTED)

    def test_parse_recording_upload_uri(self):
        bucket, key, scheme = parse_recording_upload_uri("r2://voxella-video/attendee/demo.mp4")
        self.assertEqual(bucket, "voxella-video")
        self.assertEqual(key, "attendee/demo.mp4")
        self.assertEqual(scheme, "r2")

    @patch.dict(
        os.environ,
        {
            "LAUNCH_BOT_METHOD": "modal",
            "R2__ENDPOINT": "https://example.r2.cloudflarestorage.com",
            "R2__REGION": "auto",
            "R2__ACCESS_KEY_ID": "key",
            "R2__SECRET_ACCESS_KEY": "secret",
        },
        clear=False,
    )
    def test_validate_external_media_storage_settings_accepts_modal_r2_secret(self):
        error = validate_external_media_storage_settings(
            {"recording_upload_uri": "r2://voxella-video/attendee/demo.mp4"},
            self.project,
        )
        self.assertIsNone(error)

    @patch.dict(
        os.environ,
        {
            "R2__ENDPOINT": "https://example.r2.cloudflarestorage.com",
            "R2__REGION": "auto",
            "R2__ACCESS_KEY_ID": "key",
            "R2__SECRET_ACCESS_KEY": "secret",
        },
        clear=False,
    )
    def test_apply_modal_runtime_overrides_updates_bot_and_credentials(self):
        apply_modal_runtime_overrides(
            self.bot.id,
            bot_name="New Name",
            recording_upload_uri="r2://voxella-video/attendee/demo.mp4",
            other_params={"max_uptime_seconds": 123, "recording_format": "mp4"},
        )

        self.bot.refresh_from_db()
        self.assertEqual(self.bot.name, "New Name")
        self.assertEqual(self.bot.settings["external_media_storage_settings"]["bucket_name"], "voxella-video")
        self.assertEqual(self.bot.settings["external_media_storage_settings"]["recording_file_name"], "attendee/demo.mp4")
        self.assertEqual(self.bot.settings["automatic_leave_settings"]["max_uptime_seconds"], 123)
        credentials = self.project.credentials.get(credential_type=Credentials.CredentialTypes.EXTERNAL_MEDIA_STORAGE)
        self.assertEqual(credentials.get_credentials()["region_name"], "auto")

    def test_launch_bot_uses_modal_branch(self):
        fake_call = MagicMock()
        fake_call.object_id = "fc-123"
        fake_function = MagicMock()
        fake_function.spawn.return_value = fake_call
        fake_modal = MagicMock()
        fake_modal.Function.from_name.return_value = fake_function

        with patch.dict(os.environ, {"LAUNCH_BOT_METHOD": "modal"}, clear=False):
            with patch.dict(sys.modules, {"modal": fake_modal}):
                launch_bot(self.bot)

        self.bot.refresh_from_db()
        self.assertEqual(self.bot.metadata["modal_function_call_id"], "fc-123")
        fake_function.spawn.assert_called_once()

    def test_cleanup_command_cancels_modal_call(self):
        self.bot.metadata = {"modal_function_call_id": "fc-456"}
        self.bot.save(update_fields=["metadata", "updated_at"])

        fake_function_call = MagicMock()
        fake_modal = MagicMock()
        fake_modal.FunctionCall.from_id.return_value = fake_function_call

        with patch.dict(os.environ, {"LAUNCH_BOT_METHOD": "modal"}, clear=False):
            with patch.dict(sys.modules, {"modal": fake_modal}):
                Command()._terminate_modal_call(self.bot)

        fake_modal.FunctionCall.from_id.assert_called_once_with("fc-456")
        fake_function_call.cancel.assert_called_once()
