from unittest import TestCase
from unittest.mock import MagicMock, patch

from bots.bot_controller import BotController
from bots.bot_controller.screen_and_audio_recorder import ScreenAndAudioRecorder


class TestScreenAndAudioRecorder(TestCase):
    @patch("bots.bot_controller.screen_and_audio_recorder.subprocess.Popen")
    def test_start_recording_uses_websocket_mixed_audio_pipe_when_enabled(self, mock_popen):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        recorder = ScreenAndAudioRecorder(
            file_location="/tmp/test.mp4",
            recording_dimensions=(1920, 1080),
            audio_only=False,
            use_websocket_mixed_audio_source=True,
            websocket_mixed_audio_sample_rate=48000,
            websocket_mixed_audio_channels=1,
        )

        recorder.start_recording(":0")

        ffmpeg_cmd = mock_popen.call_args.args[0]
        self.assertIn("s16le", ffmpeg_cmd)
        self.assertIn("48000", ffmpeg_cmd)
        self.assertIn("pipe:3", ffmpeg_cmd)
        self.assertNotIn("alsa", ffmpeg_cmd)
        self.assertEqual(mock_popen.call_args.kwargs["pass_fds"], (3,))

        recorder.stop_recording()

    def test_bot_controller_forwards_mixed_audio_to_screen_recorder(self):
        controller = BotController.__new__(BotController)
        controller.screen_and_audio_recorder = MagicMock()
        controller.screen_and_audio_recorder.uses_websocket_mixed_audio_source.return_value = True
        controller.gstreamer_pipeline = None
        controller.websocket_client_manager = None

        controller.add_mixed_audio_chunk_callback(b"\x01\x02")

        controller.screen_and_audio_recorder.add_mixed_audio_chunk.assert_called_once_with(b"\x01\x02")
