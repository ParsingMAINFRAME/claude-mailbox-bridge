#!/usr/bin/env python3
"""Reduce an authorized A/B trial's JSONL stream without retaining its text.

This does not launch Claude, send mail, read login state, or prove filesystem
delivery. The trial owner must separately verify correlated reply files.
"""
import argparse
import json
import os
import sys

PLUGIN = "session-mailbox"
REPLY_TOOL = "mcp__session-mailbox__mailbox_reply"
ALLOWED_TOOLS = {REPLY_TOOL, "ToolSearch"}
BUILTINS = {"cc-plugin-agents-md", "cc-plugin-diff", "cc-plugin-plugin-authoring",
            "cc-plugin-sec-default", "cc-plugin-telemetry", "cc-plugin-you-should-know"}
MAX_LINE = 1024 * 1024


class SessionReport:
    """Every init is checked; a later good init cannot erase an earlier failure."""

    def __init__(self, plugin_path, message_id, trial="B"):
        self.plugin_path = os.path.realpath(plugin_path)
        self.message_id = message_id
        self.trial = trial
        self.problems = set()
        self.init_count = 0
        self.mail_init_count = 0
        self.invalid_init_count = 0
        self.bootstrap_results = 0
        self.turn_results = 0
        self.calls = {}
        self.outcomes = []
        self.finished_bootstrap = False

    def check_init(self, event):
        problems = set()
        if event.get("apiKeySource") not in (None, "", "none"):
            problems.add("session reports an API-key route")
        tools = event.get("tools")
        if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
            problems.add("invalid tool report")
        elif REPLY_TOOL not in tools or set(tools) - ALLOWED_TOOLS:
            problems.add("session tools differ from the isolated reply-tool scope")
        servers = event.get("mcp_servers")
        expected = {"name": PLUGIN, "status": "connected", "source": "plugin"}
        if not isinstance(servers, list) or len(servers) != 1 or not isinstance(servers[0], dict):
            problems.add("unexpected MCP servers")
        elif {key: servers[0].get(key) for key in expected} != expected:
            problems.add("MCP server lacks the expected plugin provenance")
        plugins = event.get("plugins")
        ours = 0
        if not isinstance(plugins, list):
            problems.add("invalid plugin report")
        else:
            for plugin in plugins:
                if not isinstance(plugin, dict):
                    problems.add("invalid plugin report")
                    continue
                name, source, path = (plugin.get(k) for k in ("name", "source", "path"))
                if not all(isinstance(v, str) for v in (name, source, path)):
                    problems.add("invalid plugin report")
                    continue
                if (name == PLUGIN and source == f"{PLUGIN}@inline" and
                        isinstance(path, str) and os.path.realpath(path) == self.plugin_path):
                    ours += 1
                elif name in BUILTINS and source == f"{name}@builtin" and path == "builtin":
                    continue
                else:
                    problems.add("a plugin outside the isolated trial is loaded")
        if ours != 1:
            problems.add("expected inline plugin is not loaded exactly once")
        return problems

    def feed(self, line):
        if len(line) > MAX_LINE:
            self.problems.add("oversize stream event")
            return
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            self.problems.add("unparsed stream event")
            return
        if not isinstance(event, dict):
            self.problems.add("non-object stream event")
            return
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self.init_count += 1
            if self.finished_bootstrap:
                self.mail_init_count += 1
            found = self.check_init(event)
            if found:
                self.invalid_init_count += 1
                self.problems.update(found)
        elif kind in ("assistant", "user"):
            message = event.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            for block in blocks if isinstance(blocks, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    if not self.finished_bootstrap:
                        self.problems.add("tool call before zero-work initialization finished")
                    name = block.get("name")
                    if name == "ToolSearch":
                        continue
                    if name != REPLY_TOOL:
                        self.problems.add("model called another tool")
                        continue
                    data = block.get("input")
                    if not isinstance(data, dict) or data.get("message_id") != self.message_id:
                        self.problems.add("reply call names a different message")
                        continue
                    call_id, state = block.get("id"), data.get("state")
                    if not isinstance(call_id, str) or state not in ("acknowledged", "completed", "blocked"):
                        self.problems.add("invalid reply call")
                    elif len(self.calls) >= 16 or call_id in self.calls:
                        self.problems.add("excessive or repeated reply calls")
                    else:
                        self.calls[call_id] = state
                elif block.get("type") == "tool_result":
                    call_id = block.get("tool_use_id")
                    state = self.calls.get(call_id) if isinstance(call_id, str) else None
                    if state is None:
                        continue
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(content)
                    recorded = f'recorded "{state}" for {self.message_id}'
                    outcome = "written" if recorded in text or recorded.replace('"', '\\"') in text else "refused-or-unconfirmed"
                    if len(self.outcomes) < 16:
                        self.outcomes.append({"state": state, "outcome": outcome})
                    else:
                        self.problems.add("excessive reply results")
        elif kind == "result":
            usage = event.get("usage")
            zero = (event.get("num_turns") == 0 and event.get("duration_api_ms") == 0 and
                    event.get("total_cost_usd") == 0 and isinstance(usage, dict) and
                    all(usage.get(k, 0) == 0 for k in ("input_tokens", "output_tokens",
                        "cache_read_input_tokens", "cache_creation_input_tokens")) and
                    event.get("modelUsage") == {} and event.get("subtype") == "success" and
                    event.get("is_error") is False)
            if zero and not self.finished_bootstrap:
                self.bootstrap_results += 1
                self.finished_bootstrap = True
            else:
                self.turn_results += 1
                if event.get("is_error") is not False or event.get("subtype") != "success":
                    self.problems.add("mail turn did not report success")

    def report(self):
        problems = set(self.problems)
        if self.init_count < 2 or self.mail_init_count < 1:
            problems.add("missing initialization or mail-turn session report")
        if self.bootstrap_results != 1:
            problems.add("missing exactly one zero-work initialization result")
        if self.turn_results != 1:
            problems.add("expected exactly one mail-turn result")
        written = [o["state"] for o in self.outcomes if o["outcome"] == "written"]
        if self.trial == "B" and (written != ["acknowledged", "completed"] or
                                   len(self.calls) != 2 or len(self.outcomes) != 2):
            problems.add("expected one correlated acknowledgement followed by completion")
        if self.trial == "A" and (written or not self.calls):
            problems.add("permission observation lacks a refused reply attempt")
        return {"verdict": "failed" if problems else "stream-validated" if self.trial == "B" else "observed",
                "trial": self.trial, "init_count": self.init_count,
                "invalid_init_count": self.invalid_init_count,
                "bootstrap_results": self.bootstrap_results, "mail_turn_results": self.turn_results,
                "reply_outcomes": self.outcomes, "problems": sorted(problems),
                "filesystem_delivery_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-path", required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--trial", choices=("A", "B"), default="B")
    args = parser.parse_args()
    report = SessionReport(args.plugin_path, args.message_id, args.trial)
    while True:
        line = sys.stdin.readline(MAX_LINE + 1)
        if not line:
            break
        report.feed(line)
    result = report.report()
    print(json.dumps(result, indent=2))
    return 1 if result["verdict"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
