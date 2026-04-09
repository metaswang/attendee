from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from bots.launch_bot_utils import launch_bot


class LaunchBotUtilsTest(TestCase):
    def setUp(self):
        self.bot = SimpleNamespace(id=123, object_id="bot-123", runtime_lease=None)

    @patch("bots.tasks.launch_meetbot_runtime_task.launch_meetbot_runtime.delay")
    def test_default_launch_uses_hybrid_runtime_task(self, mock_launch_runtime):
        launch_bot(self.bot)

        mock_launch_runtime.assert_called_once_with(self.bot.id)

    @patch.dict("os.environ", {"LAUNCH_BOT_METHOD": "hybrid"}, clear=False)
    @patch("bots.tasks.launch_meetbot_runtime_task.launch_meetbot_runtime.delay")
    def test_hybrid_launch_uses_scheduler_task_when_enabled(self, mock_launch_runtime):
        launch_bot(self.bot)

        mock_launch_runtime.assert_called_once_with(self.bot.id)

    @patch.dict(
        "os.environ",
        {"LAUNCH_BOT_METHOD": "hybrid", "MEETBOT_RUNTIME_SCHEDULER_ENABLED": "false"},
        clear=False,
    )
    @patch("bots.runtime_providers.get_runtime_provider")
    def test_hybrid_launch_uses_legacy_gcp_provider_when_scheduler_disabled(self, mock_get_runtime_provider):
        mock_provider = MagicMock()
        mock_provider.provision_bot.return_value = SimpleNamespace(id=456, provider_instance_id="host-1", region="us-central1")
        mock_get_runtime_provider.return_value = mock_provider

        launch_bot(self.bot)

        mock_get_runtime_provider.assert_called_once_with("gcp_compute_instance")
        mock_provider.provision_bot.assert_called_once_with(self.bot)
