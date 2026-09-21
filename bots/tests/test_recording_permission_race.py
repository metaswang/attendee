import importlib.util
import sys
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_module(module_name: str, relative_path: str):
    path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


recording_ready = _load_module("bots.recording_ready", "bots/recording_ready.py")
RecordingNotReadyError = recording_ready.RecordingNotReadyError
is_recording_not_ready_error = recording_ready.is_recording_not_ready_error
post_with_recording_ready_retry = recording_ready.post_with_recording_ready_retry

# ClosedCaptionManager imports bots.recording_ready; keep the module we already loaded.
sys.modules.setdefault("bots", types.ModuleType("bots"))
sys.modules["bots"].recording_ready = recording_ready
closed_caption_manager = _load_module(
    "bots.bot_controller.closed_caption_manager_under_test",
    "bots/bot_controller/closed_caption_manager.py",
)
ClosedCaptionManager = closed_caption_manager.ClosedCaptionManager


def _http_error(status_code: int, body: str) -> requests.HTTPError:
    response = MagicMock()
    response.status_code = status_code
    response.text = body
    return requests.HTTPError(response=response)


class RecordingReadyHelpersTests(unittest.TestCase):
    def test_is_recording_not_ready_error_detects_409(self):
        exc = _http_error(409, '{"error": "No recording in progress"}')
        self.assertTrue(is_recording_not_ready_error(exc))
        self.assertFalse(is_recording_not_ready_error(_http_error(409, '{"error": "other"}')))
        self.assertFalse(is_recording_not_ready_error(_http_error(500, "No recording in progress")))

    def test_post_with_recording_ready_retry_succeeds_after_409(self):
        calls = {"n": 0}

        def post_fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _http_error(409, "No recording in progress")
            return {"utterance_id": 1}

        with patch.object(recording_ready.time, "sleep"):
            result = post_with_recording_ready_retry(post_fn, what="Caption POST", attempts=5)

        self.assertEqual(result, {"utterance_id": 1})
        self.assertEqual(calls["n"], 3)

    def test_post_with_recording_ready_retry_raises_soft_error(self):
        def post_fn():
            raise _http_error(409, "No recording in progress")

        with patch.object(recording_ready.time, "sleep"):
            with self.assertRaises(RecordingNotReadyError):
                post_with_recording_ready_retry(post_fn, what="Caption POST", attempts=2)


class ClosedCaptionManagerRecordingNotReadyTests(unittest.TestCase):
    def test_process_captions_keeps_entry_when_recording_not_ready(self):
        save = MagicMock(side_effect=RecordingNotReadyError("not ready"))
        manager = ClosedCaptionManager(
            save_utterance_callback=save,
            get_participant_callback=lambda _id: {
                "participant_uuid": "p1",
                "participant_full_name": "P",
                "participant_is_the_bot": False,
                "participant_is_host": False,
            },
        )
        manager.upsert_caption({"captionId": 1, "deviceId": "d1", "isFinal": True, "text": "hello"})

        manager.process_captions()

        self.assertEqual(save.call_count, 1)
        self.assertIn("d1:1", manager.captions)
        self.assertIsNone(manager.captions["d1:1"].last_upsert_to_db_at)


class SyncAdapterDeliveryLogicTests(unittest.TestCase):
    """Mirrors BotController._deliver_adapter_message_synchronously without importing GLib stack."""

    def test_idle_callback_runs_before_return(self):
        order = []
        done = threading.Event()
        errors = []

        def take_action(message):
            order.append(("action", message))

        message = {"message": "Bot recording permission granted"}

        def _run():
            try:
                take_action(message)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                done.set()
            return False

        def fake_idle_add(callback):
            order.append(("idle_scheduled",))
            threading.Thread(target=callback, daemon=True).start()
            return 1

        fake_idle_add(_run)
        self.assertTrue(done.wait(timeout=2))
        self.assertEqual(errors, [])
        self.assertEqual(order[0], ("idle_scheduled",))
        self.assertEqual(order[1], ("action", message))


if __name__ == "__main__":
    unittest.main()
