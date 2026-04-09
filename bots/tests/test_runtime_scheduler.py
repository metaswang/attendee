from unittest import TestCase
from unittest.mock import patch

from bots.runtime_scheduler import skip_vps_enabled, vps_target_order


class RuntimeSchedulerConfigTest(TestCase):
    @patch.dict("os.environ", {"MEETBOT_VPS_TARGET_ORDER": "myvps,myvps3"}, clear=False)
    def test_vps_target_order_respects_env_filter(self):
        self.assertEqual(vps_target_order(), ["myvps", "myvps3"])

    @patch.dict("os.environ", {"MEETBOT_SCHEDULER_SKIP_VPS": "true"}, clear=False)
    def test_skip_vps_enabled_reads_env(self):
        self.assertTrue(skip_vps_enabled())
