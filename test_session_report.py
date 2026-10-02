"""Offline regressions for every-turn scope checks; starts no Claude process."""
import copy
import json
import unittest

from validation.session_report import PLUGIN, REPLY_TOOL, SessionReport

PATH = "/tmp/example-package/mod"
MESSAGE = "m-example-trial-001"


def init():
    return {"type": "system", "subtype": "init", "apiKeySource": "none",
            "tools": [REPLY_TOOL],
            "mcp_servers": [{"name": PLUGIN, "status": "connected", "source": "plugin"}],
            "plugins": [{"name": PLUGIN, "source": f"{PLUGIN}@inline", "path": PATH}]}


def result(zero=False):
    return {"type": "result", "subtype": "success", "is_error": False,
            "num_turns": 0 if zero else 3, "duration_api_ms": 0 if zero else 10,
            "total_cost_usd": 0, "usage": {"input_tokens": 0 if zero else 4, "output_tokens": 0 if zero else 2},
            "modelUsage": {} if zero else {"example-model": {}}}


class EveryTurnValidation(unittest.TestCase):
    def feed(self, watcher, event):
        watcher.feed(json.dumps(event))

    def bootstrap(self, trial="B"):
        watcher = SessionReport(PATH, MESSAGE, trial)
        self.feed(watcher, init())
        self.feed(watcher, result(True))
        return watcher

    def finish(self, watcher, later=None, written=True):
        self.feed(watcher, init() if later is None else later)
        for number, state in enumerate(("acknowledged", "completed")):
            self.feed(watcher, {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": f"call-{number}", "name": REPLY_TOOL,
                 "input": {"message_id": MESSAGE, "state": state, "body": "private body must never be retained"}}]}})
            text = f'recorded "{state}" for {MESSAGE}' if written else "refused: permission decision: ask"
            self.feed(watcher, {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": f"call-{number}", "content": text}]}})
        self.feed(watcher, result())
        return watcher.report()

    def test_bootstrap_then_correlated_reply_stream(self):
        report = self.finish(self.bootstrap())
        self.assertEqual(report["verdict"], "stream-validated")
        self.assertFalse(report["filesystem_delivery_verified"])

    def test_permission_observation(self):
        self.assertEqual(self.finish(self.bootstrap("A"), written=False)["verdict"], "observed")

    def test_control_ack_alone_cannot_replace_initialization(self):
        watcher = SessionReport(PATH, MESSAGE)
        self.feed(watcher, {"type": "control_response", "response": {"subtype": "success"}})
        self.assertEqual(watcher.report()["verdict"], "failed")
        self.assertEqual(watcher.init_count, 0)

    def test_later_tool_list_widening_fails_even_when_other_tool_unused(self):
        later = init(); later["tools"].append("Bash")
        report = self.finish(self.bootstrap(), later)
        self.assertEqual(report["verdict"], "failed")
        self.assertEqual(report["invalid_init_count"], 1)

    def test_later_plugin_widening_fails(self):
        later = init(); later["plugins"].append({"name": "other", "source": "other@inline", "path": "/tmp/other"})
        self.assertEqual(self.finish(self.bootstrap(), later)["verdict"], "failed")

    def test_later_auth_route_change_fails(self):
        later = init(); later["apiKeySource"] = "environment"
        self.assertEqual(self.finish(self.bootstrap(), later)["verdict"], "failed")

    def test_later_server_name_lookalike_has_no_plugin_provenance(self):
        later = init(); later["mcp_servers"][0]["source"] = "user"
        self.assertEqual(self.finish(self.bootstrap(), later)["verdict"], "failed")

    def test_later_good_init_cannot_erase_prior_violation(self):
        watcher = self.bootstrap()
        bad = init(); bad["tools"].append("Bash")
        self.feed(watcher, bad)
        self.assertEqual(self.finish(watcher)["verdict"], "failed")

    def test_nonzero_bootstrap_usage_fails(self):
        watcher = SessionReport(PATH, MESSAGE)
        self.feed(watcher, init())
        bad = result(True); bad["usage"]["cache_read_input_tokens"] = 1
        self.feed(watcher, bad)
        self.assertEqual(self.finish(watcher)["verdict"], "failed")

    def test_two_inits_before_bootstrap_do_not_prove_mail_turn_scope(self):
        watcher = SessionReport(PATH, MESSAGE)
        self.feed(watcher, init()); self.feed(watcher, init()); self.feed(watcher, result(True))
        self.feed(watcher, result())
        self.assertIn("missing initialization or mail-turn session report", watcher.report()["problems"])

    def test_builtin_provenance_is_required(self):
        later = init(); later["plugins"].append({"name": "cc-plugin-diff", "source": "cc-plugin-diff@builtin", "path": "builtin"})
        self.assertEqual(self.finish(self.bootstrap(), later)["verdict"], "stream-validated")
        later["plugins"][-1]["source"] = "cc-plugin-diff@inline"
        self.assertEqual(self.finish(self.bootstrap(), later)["verdict"], "failed")

    def test_wrong_message_reply_is_rejected(self):
        watcher = self.bootstrap()
        self.feed(watcher, {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "foreign", "name": REPLY_TOOL,
             "input": {"message_id": "m-somebody-else", "state": "completed"}}]}})
        self.assertEqual(self.finish(watcher)["verdict"], "failed")

    def test_unparsed_stream_and_missing_reply_do_not_pass(self):
        watcher = self.bootstrap(); watcher.feed("invalid-json")
        self.assertEqual(self.finish(watcher, written=False)["verdict"], "failed")

    def test_output_retains_no_private_prompt_or_plugin_path(self):
        watcher = self.bootstrap()
        later = init(); later["plugins"].append({"name": "private-name", "source": "private-source", "path": "/private/path"})
        text = json.dumps(self.finish(watcher, later))
        for private in ("private body", "/private/path", "private-name", "private-source"):
            self.assertNotIn(private, text)

    def test_malformed_plugin_fields_fail_without_retaining_values(self):
        later = init(); later["plugins"][0]["name"] = {"unexpected": "shape"}
        self.assertEqual(self.finish(self.bootstrap(), later)["verdict"], "failed")

    def test_extra_reply_attempt_does_not_pass_two_written_replies(self):
        watcher = self.bootstrap()
        self.feed(watcher, {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "extra", "name": REPLY_TOOL,
             "input": {"message_id": MESSAGE, "state": "acknowledged"}}]}})
        self.assertEqual(self.finish(watcher)["verdict"], "failed")


if __name__ == "__main__":
    unittest.main()
