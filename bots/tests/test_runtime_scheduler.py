from unittest import TestCase
from unittest.mock import patch

from bots.runtime_scheduler import skip_vps_enabled, vps_target_order, vps_slot_capacity


class RuntimeSchedulerConfigTest(TestCase):
    @patch.dict("os.environ", {}, clear=True)
    def test_missing_vps_config_cannot_allocate_a_retired_or_unconfigured_host(self):
        self.assertEqual(vps_target_order(), [])
        for host in ("myvps", "myvps2", "myvps3"):
            self.assertEqual(vps_slot_capacity(host), 0)

    @patch.dict("os.environ", {"MEETBOT_VPS_TARGET_ORDER": "myvps,myvps3"}, clear=False)
    def test_vps_target_order_respects_env_filter(self):
        self.assertEqual(vps_target_order(), ["myvps", "myvps3"])

    @patch.dict("os.environ", {"MEETBOT_SCHEDULER_SKIP_VPS": "true"}, clear=False)
    def test_skip_vps_enabled_reads_env(self):
        self.assertTrue(skip_vps_enabled())
