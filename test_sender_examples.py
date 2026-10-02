"""Exercise sender examples with the real helper and a scripted receiver, never Claude."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent


class SenderExamples(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "box"
        self.env = dict(os.environ, CLAUDE_MAILBOX_ROOT=str(self.root))
        self.helper("init", "--session", "worker", "--reader", "bot")

    def helper(self, *args):
        result = subprocess.run([sys.executable, str(HERE / "bridge.py"), *args],
                                env=self.env, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def example(self, script, wait=12):
        if script == "python":
            return [sys.executable, str(HERE / "examples/send_and_wait.py"),
                    "--from", "bot", "--to", "worker", "--body", "pong", "--wait-seconds", str(wait)]
        return ["sh", str(HERE / "examples/send_and_wait.sh"), "bot", "worker", "pong"]

    def deliver(self, script):
        process = subprocess.Popen(self.example(script), env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 6
            claimed = None
            while time.monotonic() < deadline:
                claimed = self.helper("claim", "--agent", "worker", "--session", "example-test").get("message")
                if claimed:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(claimed)
            message = claimed["id"]
            self.helper("begin", "--agent", "worker", "--session", "example-test", "--id", message)
            # An unrelated reply must never be printed by the sender example.
            self.helper("send", "--from", "worker", "--to", "bot", "--kind", "status", "--body", "UNRELATED-REPLY")
            for state in ("acknowledged", "completed"):
                self.helper("reply", "--agent", "worker", "--session", "example-test", "--id", message,
                            "--state", state, "--body", "example result")
            stdout, stderr = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 0, stderr + stdout)
            self.assertIn("acknowledged", stdout)
            self.assertIn("completed", stdout)
            self.assertNotIn("UNRELATED-REPLY", stdout)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_python_sender_success_filters_correlated_replies(self):
        self.deliver("python")

    def test_shell_sender_success_filters_correlated_replies(self):
        self.deliver("shell")

    def test_python_sender_deadline_leaves_one_pending_message_without_retry(self):
        result = subprocess.run(self.example("python", wait=0), env=self.env,
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 1)
        self.assertIn("deadline reached", result.stderr)
        self.assertEqual(len(list((self.root / "mail/worker/new").glob("*.json"))), 1)
        self.assertEqual(self.helper("inbox", "--agent", "bot")["messages"], [])


if __name__ == "__main__":
    unittest.main()
