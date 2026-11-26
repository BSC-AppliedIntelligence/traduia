import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from whisper_live.model_selection import DEFAULT_MODEL, MODELS, ModelSelectionError, SharedModelSelection

SMALL, BSC = MODELS[1]["id"], MODELS[2]["id"]


class ModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "state/selection.json"
        self.manager = SharedModelSelection(self.path)
        self.loader = Mock(side_effect=lambda model: {"model": model})
        self.releaser = Mock()

    def acquire(self):
        return self.manager.acquire(self.loader, self.releaser)

    def test_default_and_three_canonical_choices(self):
        state = self.manager.status()
        self.assertEqual(state["selected_model"], DEFAULT_MODEL)
        self.assertEqual(len(state["models"]), 3)
        self.assertIsNone(state["loaded_model"])
        self.assertFalse(self.path.exists())

    def test_save_persists_without_loading_and_survives_restart(self):
        self.manager.select(BSC, 0)
        self.assertEqual(SharedModelSelection(self.path).status()["selected_model"], BSC)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.loader.assert_not_called()

    def test_two_broadcasts_share_model_and_pending_switch_waits_for_both(self):
        first, second = self.acquire(), self.acquire()
        self.assertIs(first.model, second.model)
        self.loader.assert_called_once_with(DEFAULT_MODEL)
        self.manager.select(SMALL, 0)
        self.assertEqual(first.model["model"], DEFAULT_MODEL)
        with self.assertRaises(ModelSelectionError) as blocked:
            self.acquire()
        self.assertEqual(blocked.exception.code, "model_busy")
        first.close()
        with self.assertRaises(ModelSelectionError):
            self.acquire()
        second.close()
        new = self.acquire()
        self.assertEqual(new.model_id, SMALL)
        self.releaser.assert_called_once()
        self.assertFalse(self.manager.status()["pending"])
        new.close()
        new.close()  # Cleanup is idempotent.

    def test_stale_save_and_invalid_model_do_not_change_persisted_choice(self):
        self.manager.select(SMALL, 0)
        before = self.path.read_bytes()
        for model, revision, code in [(BSC, 0, "selection_conflict"), ("tiny", 1, "invalid_model"),
                                       (SMALL, True, "selection_conflict")]:
            with self.assertRaises(ModelSelectionError) as failure:
                self.manager.select(model, revision)
            self.assertEqual(failure.exception.code, code)
            self.assertEqual(self.path.read_bytes(), before)

    def test_status_does_not_load_or_reserve(self):
        for _ in range(3):
            self.assertTrue(self.manager.control({"operation": "status"})["ok"])
        self.loader.assert_not_called()
        self.assertEqual(self.manager.status()["active_sessions"], 0)

    def test_load_is_serialized_and_status_stays_responsive(self):
        entered, finish = threading.Event(), threading.Event()
        acquired = []
        def slow_loader(model):
            entered.set()
            self.assertTrue(finish.wait(5))
            return object()
        worker = threading.Thread(target=lambda: acquired.append(self.manager.acquire(slow_loader, self.releaser)))
        worker.start()
        self.addCleanup(worker.join, 5)
        self.assertTrue(entered.wait(3))
        try:
            self.assertTrue(self.manager.status()["loading"])
            for action in [lambda: self.acquire(), lambda: self.manager.select(BSC, 0)]:
                with self.assertRaises(ModelSelectionError):
                    action()
        finally:
            finish.set()
            worker.join(5)
        self.assertEqual(len(acquired), 1)
        acquired[0].close()

    def test_failed_load_exposes_no_model_or_secret_and_can_retry(self):
        lease = self.acquire()
        lease.close()
        self.manager.select(BSC, 0)
        with self.assertRaises(ModelSelectionError) as failure:
            self.manager.acquire(Mock(side_effect=RuntimeError("private-provider-error")), self.releaser)
        self.assertEqual(failure.exception.code, "model_load_failed")
        state = self.manager.status()
        self.assertIsNone(state["loaded_model"])
        self.assertEqual(state["active_sessions"], 0)
        self.assertFalse(state["loading"])
        self.assertNotIn("private-provider-error", json.dumps(state))
        self.assertEqual(state["selected_model"], BSC)
        retry = self.acquire()
        self.assertEqual(retry.model_id, BSC)
        self.assertIsNone(self.manager.status()["last_error"])
        retry.close()

    def test_write_failure_keeps_previous_selection_and_no_temp_file(self):
        self.manager.select(SMALL, 0)
        with patch("whisper_live.model_selection.os.replace", side_effect=OSError("private path")):
            with self.assertRaises(ModelSelectionError):
                self.manager.select(BSC, 1)
        self.assertEqual(self.manager.status()["selected_model"], SMALL)
        self.assertEqual(SharedModelSelection(self.path).status()["selected_model"], SMALL)
        self.assertEqual(list(self.path.parent.glob('.asr-selection-*')), [])

    def test_corrupt_state_fails_startup_instead_of_silently_resetting(self):
        self.path.parent.mkdir()
        self.path.write_text('{"schema_version": 1, "selected_model": "tiny", "revision": 1}')
        with self.assertRaises(ValueError):
            SharedModelSelection(self.path)

    def test_capacity_and_explicit_cancel_of_pending_change(self):
        self.manager.max_sessions = 1
        lease = self.acquire()
        with self.assertRaises(ModelSelectionError):
            self.acquire()
        self.manager.select(SMALL, 0)
        self.manager.select(DEFAULT_MODEL, 1)
        self.assertFalse(self.manager.status()["pending"])
        lease.close()
        again = self.acquire()
        self.loader.assert_called_once()
        again.close()


if __name__ == "__main__":
    unittest.main()
