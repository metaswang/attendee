from unittest.mock import patch

from django.test import TestCase

from accounts.models import Organization
from bots.models import Bot, Project, RecordingFormats
from bots.serializers import CreateBotSerializer


class RecordingChunkSettingsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

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

    def test_create_bot_serializer_rejects_r2_chunks_for_video_recording(self):
        serializer = CreateBotSerializer(
            data={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "bot_name": "Chunk Bot",
                "recording_settings": {
                    "format": RecordingFormats.MP4,
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
            settings={"recording_settings": {"format": RecordingFormats.MP4}},
        )

        self.assertEqual(audio_bot.runtime_resource_class(), "audio_only")
        self.assertEqual(audio_bot.memory_request(), "4Gi")
        self.assertEqual(audio_bot.runtime_size_slug(), "s-2vcpu-4gb")
        self.assertFalse(audio_bot.create_debug_recording())

        self.assertEqual(av_bot.runtime_resource_class(), "web_av_standard")
        self.assertEqual(av_bot.memory_request(), "8Gi")
        self.assertEqual(av_bot.runtime_size_slug(), "s-4vcpu-8gb")

        with patch("bots.models.os.getenv", side_effect=lambda key, default=None: "true" if key == "SAVE_DEBUG_RECORDINGS" else default):
            self.assertTrue(av_bot.create_debug_recording())
