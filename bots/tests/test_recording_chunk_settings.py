import json
import uuid
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.test.utils import override_settings

from accounts.models import Organization
from bots.bot_controller.bot_controller import BotController
from bots.bots_api_utils import BotCreationSource, create_bot
from bots.models import Bot, Project, RecordingFormats
from bots.bot_controller.recording_chunk_uploader import RecordingChunkUploader
from bots.serializers import CreateBotSerializer


class RecordingChunkSettingsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

    def test_recording_session_id_prefers_bot_metadata_session_uuid(self):
        session_id = str(uuid.uuid4())
        bot = Bot.objects.create(
            project=self.project,
            name="Metadata Bot",
            meeting_url="https://meet.google.com/abc-defg-hij",
            settings={
                "recording_settings": {
                    "audio_raw_path": "customer_audio/proj_123/bot_123/original.m4a",
                }
            },
            metadata={"session_id": session_id},
        )

        controller = BotController.__new__(BotController)
        controller.bot_in_db = bot

        self.assertEqual(controller.recording_session_id(), session_id)

    def test_create_bot_serializer_accepts_audio_only_r2_chunks(self):
        serializer = CreateBotSerializer(
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Chunk Bot",
                "recording_settings": {
                    "format": RecordingFormats.MP3,
                    "transport": "r2_chunks",
                    "audio_chunk_prefix": "customer_audio/user-1/session-1/chunks",
                    "audio_raw_path": "customer_audio/user-1/session-1/original.m4a",
                },
                "callback_settings": {
                    "recording_complete": {
                        "url": "https://api.example.com/v2/meeting/app/bot/recording/complete",
                        "signing_secret": "top-secret",
                    }
                },
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    @override_settings(REQUIRE_HTTPS_WEBHOOKS=False)
    def test_create_bot_serializer_accepts_http_callback_when_https_not_required(self):
        serializer = CreateBotSerializer(
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Chunk Bot",
                "recording_settings": {
                    "format": RecordingFormats.MP3,
                    "transport": "r2_chunks",
                    "audio_chunk_prefix": "customer_audio/user-1/session-1/chunks",
                    "audio_raw_path": "customer_audio/user-1/session-1/original.m4a",
                },
                "callback_settings": {
                    "recording_complete": {
                        "url": "http://api:8000/v2/meeting/app/bot/recording/complete",
                        "signing_secret": "top-secret",
                    }
                },
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_create_bot_serializer_accepts_muxed_webm_r2_chunks(self):
        serializer = CreateBotSerializer(
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Chunk Bot",
                "recording_settings": {
                    "format": RecordingFormats.WEBM,
                    "transport": "r2_chunks",
                    "audio_raw_path": "customer_audio/user-1/session-1/original.m4a",
                    "video_chunk_prefix": "video/user-1/session-1/chunks",
                },
                "callback_settings": {
                    "recording_complete": {
                        "url": "https://api.example.com/v2/meeting/app/bot/recording/complete",
                        "signing_secret": "top-secret",
                    }
                },
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_create_bot_serializer_rejects_audio_only_r2_chunks_with_video_prefix(self):
        serializer = CreateBotSerializer(
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Chunk Bot",
                "recording_settings": {
                    "format": RecordingFormats.MP3,
                    "transport": "r2_chunks",
                    "audio_chunk_prefix": "customer_audio/user-1/session-1/chunks",
                    "audio_raw_path": "customer_audio/user-1/session-1/original.m4a",
                    "video_chunk_prefix": "video/user-1/session-1/chunks",
                },
                "callback_settings": {
                    "recording_complete": {
                        "url": "https://api.example.com/v2/meeting/app/bot/recording/complete",
                        "signing_secret": "top-secret",
                    }
                },
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("recording_settings", serializer.errors)

    @patch.dict("os.environ", {"LAUNCH_BOT_METHOD": "gcp-compute-engine"}, clear=False)
    def test_create_bot_preserves_muxed_video_prefix_for_gcp_google_meet(self):
        bot, error = create_bot(
            {
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Chunk Bot",
                "recording_settings": {
                    "format": RecordingFormats.WEBM,
                    "transport": "r2_chunks",
                    "audio_raw_path": "customer_audio/user-1/session-1/original.m4a",
                    "video_chunk_prefix": "video/user-1/session-1/chunks",
                    "resolution": "720p",
                },
                "callback_settings": {
                    "recording_complete": {
                        "url": "https://api.example.com/v2/meeting/app/bot/recording/complete",
                        "signing_secret": "top-secret",
                    }
                },
            },
            source=BotCreationSource.API,
            project=self.project,
        )

        self.assertIsNone(error)
        assert bot is not None
        self.assertEqual(bot.settings["recording_settings"]["video_chunk_prefix"], "video/user-1/session-1/chunks")
        self.assertNotIn("audio_chunk_prefix", bot.settings["recording_settings"])
        self.assertEqual(bot.settings["recording_settings"]["resolution"], "720p")

    def test_create_bot_serializer_rejects_native_zoom_r2_chunks(self):
        serializer = CreateBotSerializer(
            data={
                "meeting_url": "https://zoom.us/j/123456789",
                "bot_name": "Chunk Bot",
                "recording_settings": {
                    "format": RecordingFormats.MP3,
                    "transport": "r2_chunks",
                    "audio_chunk_prefix": "customer_audio/user-1/session-1/chunks",
                    "audio_raw_path": "customer_audio/user-1/session-1/original.m4a",
                },
                "callback_settings": {
                    "recording_complete": {
                        "url": "https://api.example.com/v2/meeting/app/bot/recording/complete",
                        "signing_secret": "top-secret",
                    }
                },
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("recording_settings", serializer.errors)

    def test_create_bot_serializer_rejects_local_file_transport(self):
        serializer = CreateBotSerializer(
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Chunk Bot",
                "recording_settings": {
                    "format": RecordingFormats.MP4,
                    "transport": "local_file",
                },
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("recording_settings", serializer.errors)

    def test_runtime_policy_defaults_and_debug_recording_opt_in(self):
        audio_bot = Bot.objects.create(
            name="Audio Bot",
            project=self.project,
            meeting_url="https://meet.google.com/abc-defg-hij",
            settings={"recording_settings": {"format": RecordingFormats.MP3}},
        )
        av_bot = Bot.objects.create(
            name="AV Bot",
            project=self.project,
            meeting_url="https://meet.google.com/abc-defg-hij",
            settings={"recording_settings": {"format": RecordingFormats.WEBM}},
        )

        self.assertEqual(audio_bot.runtime_resource_class(), "audio_only")
        self.assertEqual(audio_bot.memory_request(), "4Gi")
        with patch("bots.models.os.getenv", side_effect=lambda key, default=None: default):
            self.assertEqual(audio_bot.runtime_size_slug(), "s-2vcpu-4gb")
        self.assertFalse(audio_bot.create_debug_recording())

        screen_sidecar_bot = Bot.objects.create(
            name="Audio Bot With Screen",
            project=self.project,
            meeting_url="https://meet.google.com/abc-defg-hij",
            settings={"recording_settings": {"format": RecordingFormats.MP3, "video_chunk_prefix": "video/user-1/session-1/chunks"}},
        )
        self.assertEqual(screen_sidecar_bot.runtime_resource_class(), "web_av_standard")

        self.assertEqual(av_bot.runtime_resource_class(), "web_av_standard")
        self.assertEqual(av_bot.memory_request(), "8Gi")
        self.assertEqual(av_bot.runtime_size_slug(), "s-4vcpu-8gb")

        with patch("bots.models.os.getenv", side_effect=lambda key, default=None: "true" if key == "SAVE_DEBUG_RECORDINGS" else default):
            self.assertTrue(av_bot.create_debug_recording())

    @patch("bots.bot_controller.recording_chunk_uploader.boto3.client")
    def test_recording_chunk_uploader_writes_manifest_after_chunks(self, mock_boto_client):
        mock_s3_client = MagicMock()
        mock_boto_client.return_value = mock_s3_client

        uploader = RecordingChunkUploader(
            chunk_prefix="customer_audio/user-1/session-1/chunks",
            chunk_ext="webm",
            chunk_mime_type="audio/webm;codecs=opus",
            raw_path="customer_audio/user-1/session-1/original.m4a",
            chunk_interval_ms=5000,
            worker_count=1,
        )

        uploader.enqueue_chunk(b"chunk-1")
        uploader.enqueue_chunk(b"chunk-2")
        result = uploader.wait_for_uploads()

        self.assertEqual(result["manifest_path"], "customer_audio/user-1/session-1/manifest.json")
        self.assertEqual(result["chunk_paths"], [
            "customer_audio/user-1/session-1/chunks/chunk_0000.webm",
            "customer_audio/user-1/session-1/chunks/chunk_0001.webm",
        ])

        self.assertEqual(mock_s3_client.put_object.call_count, 3)
        manifest_call = mock_s3_client.put_object.call_args_list[-1]
        self.assertEqual(manifest_call.kwargs["Key"], "customer_audio/user-1/session-1/manifest.json")
        manifest = json.loads(manifest_call.kwargs["Body"].decode("utf-8"))
        self.assertEqual(manifest["chunk_count"], 2)
        self.assertEqual(manifest["raw_path"], "customer_audio/user-1/session-1/original.m4a")
        self.assertEqual(manifest["chunk_paths"], result["chunk_paths"])

        second_result = uploader.wait_for_uploads()
        self.assertEqual(second_result, result)
        self.assertEqual(mock_s3_client.put_object.call_count, 3)

    @patch("bots.bot_controller.bot_controller.make_signed_callback_request")
    def test_muxed_screen_recording_callback_uses_video_as_single_source(self, mock_callback):
        bot = Bot.objects.create(
            project=self.project,
            name="Muxed Screen Bot",
            meeting_url="https://meet.google.com/abc-defg-hij",
            settings={
                "recording_settings": {
                    "format": RecordingFormats.WEBM,
                    "transport": "r2_chunks",
                    "audio_raw_path": "customer_audio/user-1/session-1/original.m4a",
                    "video_chunk_prefix": "video/user-1/session-1/chunks",
                },
                "callback_settings": {
                    "recording_complete": {
                        "url": "https://api.example.com/v2/meeting/app/bot/recording/complete",
                        "signing_secret": "top-secret",
                    }
                },
            },
            metadata={"session_id": "session-1"},
        )

        controller = BotController.__new__(BotController)
        controller.bot_in_db = bot
        controller.recording_chunk_uploader = MagicMock()
        controller.recording_chunk_uploader.wait_for_uploads.return_value = {
            "chunk_paths": [
                "video/user-1/session-1/chunks/chunk_0000.webm",
                "video/user-1/session-1/chunks/chunk_0001.webm",
            ],
            "manifest_path": "video/user-1/session-1/manifest.json",
        }
        controller.recording_chunks_started_at = 100.0
        controller.recording_file_saved = MagicMock()
        controller.recording_complete_provider = MagicMock(return_value="google")
        with patch("bots.bot_controller.bot_controller.time.time", return_value=107.0):
            controller.deliver_recording_complete_callback()

        controller.recording_file_saved.assert_called_once_with("video/user-1/session-1/manifest.json")
        mock_callback.assert_called_once()
        payload = mock_callback.call_args.kwargs["payload"]
        self.assertEqual(payload["data"]["audio"]["chunk_paths"], [
            "video/user-1/session-1/chunks/chunk_0000.webm",
            "video/user-1/session-1/chunks/chunk_0001.webm",
        ])
        self.assertEqual(payload["data"]["audio"]["chunk_mime_type"], "video/webm")
        self.assertEqual(payload["data"]["audio"]["raw_path"], "customer_audio/user-1/session-1/original.m4a")
        self.assertEqual(payload["data"]["video"]["chunk_paths"], [
            "video/user-1/session-1/chunks/chunk_0000.webm",
            "video/user-1/session-1/chunks/chunk_0001.webm",
        ])
