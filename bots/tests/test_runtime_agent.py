from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase


def load_runtime_agent_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts/runtime_agent.py"
    spec = spec_from_file_location("attendee_runtime_agent_for_test", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeAgentRedisCliTests(SimpleTestCase):
    def test_redis_password_is_passed_via_environment_not_argv(self):
        runtime_agent = load_runtime_agent_module()
        completed = mock.Mock(stdout="OK\n")

        with (
            mock.patch.dict(
                runtime_agent.os.environ,
                {"REDIS_URL": "rediss://:super-secret@redis.internal:6380/0"},
                clear=False,
            ),
            mock.patch.object(runtime_agent.subprocess, "run", return_value=completed) as run,
        ):
            result = runtime_agent._redis_cli("PING")

        self.assertEqual(result, "OK")
        command = run.call_args.args[0]
        child_env = run.call_args.kwargs["env"]
        self.assertNotIn("super-secret", command)
        self.assertNotIn("-a", command)
        self.assertEqual(child_env["REDISCLI_AUTH"], "super-secret")
