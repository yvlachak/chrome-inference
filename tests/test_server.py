import json
import os
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chrome_inference.server import Broker, load_or_create_token


class BrokerTests(unittest.TestCase):
    def test_submit_and_publish_round_trip(self):
        broker = Broker()
        state = broker.submit("prompt", {"prompt": "hello"})

        job = broker.next_job(timeout=0.01)
        self.assertIsNotNone(job)
        self.assertEqual(job["id"], state.task_id)
        self.assertEqual(job["op"], "prompt")

        accepted = broker.publish(
            {"id": state.task_id, "kind": "result", "result": {"text": "hi"}}
        )
        self.assertTrue(accepted)
        event = state.events.get(timeout=0.01)
        self.assertEqual(event["result"]["text"], "hi")

    def test_unknown_task_event_is_rejected(self):
        broker = Broker()
        self.assertFalse(broker.publish({"id": "missing", "kind": "result"}))

    def test_empty_queue_returns_none(self):
        broker = Broker()
        self.assertIsNone(broker.next_job(timeout=0.001))


class ConfigTests(unittest.TestCase):
    def test_token_is_created_and_stable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"CHROME_INFERENCE_HOME": temp_dir}, clear=False):
                token_a, path_a = load_or_create_token()
                token_b, path_b = load_or_create_token()

            self.assertEqual(token_a, token_b)
            self.assertEqual(path_a, path_b)
            config = json.loads(Path(path_a).read_text(encoding="utf-8"))
            self.assertEqual(config["token"], token_a)
            self.assertGreaterEqual(len(token_a), 32)


if __name__ == "__main__":
    unittest.main()
