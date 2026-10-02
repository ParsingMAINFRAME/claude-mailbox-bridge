#!/usr/bin/env python3
"""Tests for bridge.py against throwaway roots in the system temp directory; never touches a real mailbox.

Most roots carry the test marker file, which is what lets the helper honour the
fake clock (BRIDGE_FAKE_NOW) and fault injection (BRIDGE_FAIL_AT).
"""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

HELPER = Path(__file__).resolve().parent / "bridge.py"
T0 = "2026-10-02T04:00:00Z"
PLACE = {"queued": "new", "claimed": "claimed", "submitted": "claimed", "acknowledged": "claimed"}
FIXED = "m-fixed-id-0001"
MARKER = ".mailbox-bridge-test-root"
CONFIG = {"version": 1, "agents": {"builder": "session", "reviewer": "session", "bot": "reader", "spare": "session"}}


def at(minutes=0, seconds=0):
    base = datetime.datetime.fromisoformat(T0.replace("Z", "+00:00"))
    return (base + datetime.timedelta(minutes=minutes, seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / MARKER).write_text("test\n")
        (self.root / "config.json").write_text(json.dumps(CONFIG))
        self.clock = T0

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, *argv, fail_at=None):
        """(parsed answer or None, exit status). Exit 99 is an injected process death."""
        env = dict(os.environ, BRIDGE_FAKE_NOW=self.clock)
        env.pop("BRIDGE_FAIL_AT", None)
        env.pop("CLAUDE_MAILBOX_ROOT", None)
        if fail_at:
            env["BRIDGE_FAIL_AT"] = fail_at
        done = subprocess.run([sys.executable, str(HELPER), "--root", str(self.root), *argv],
                              capture_output=True, text=True, env=env)
        return (json.loads(done.stdout) if done.stdout.strip() else None), done.returncode

    def ok(self, *argv):
        value, status = self.call(*argv)
        self.assertEqual(status, 0, value)
        return value

    def refused(self, *argv):
        value, status = self.call(*argv)
        self.assertEqual((status, value["ok"]), (1, False), value)
        return value["error"]

    def send(self, to="builder", sender="bot", body="reread inbox revision 4", **extra):
        argv = ["send", "--from", sender, "--to", to, "--body", body]
        for key, value in extra.items():
            argv += [f"--{key.replace('_', '-')}", str(value)]
        return self.ok(*argv)["id"]

    def record(self, message_id):
        return json.loads((self.root / "state" / f"{message_id}.json").read_text())

    def state(self, message_id):
        return self.record(message_id)["state"]

    def claim(self, agent="builder", session="s1"):
        return self.ok("claim", "--agent", agent, "--session", session)

    def begin(self, message_id, agent="builder", session="s1", *extra):
        return self.ok("begin", "--agent", agent, "--session", session, "--id", message_id, *extra)

    def reply(self, message_id, state, agent="builder", session="s1", body="x"):
        return self.ok("reply", "--agent", agent, "--session", session, "--id", message_id, "--state", state, "--body", body)

    def deliver(self, agent="builder", session="s1"):
        claimed = self.claim(agent, session)
        if claimed.get("message"):
            self.assertTrue(self.begin(claimed["message"]["id"], agent, session)["start"])
        return claimed

    def budget(self, agent="builder"):
        return self.ok("status")["agents"][agent]["automated_turns"]

    def assert_consistent(self):
        """No invisible orphan: every body has a record that places it there, every open record has its body."""
        self.assertEqual(self.ok("fsck")["needs_reconciliation"],
                         {name: [] for name in CONFIG["agents"]})
        states = {p.stem: json.loads(p.read_text()) for p in (self.root / "state").glob("*.json")}
        for agent_dir in (self.root / "mail").iterdir():
            for place in ("claimed", "closed"):
                for body in (agent_dir / place).glob("*.json"):
                    self.assertEqual(PLACE.get(states[body.stem]["state"], "closed"), place, body)
        for message_id, record in states.items():
            if record["state"] in PLACE:
                self.assertTrue((self.root / "mail" / record["to"] / PLACE[record["state"]] / f"{message_id}.json").is_file(),
                                (message_id, record["state"]))

    # ---- baseline behaviour ------------------------------------------------

    def test_idle_delivery_and_correlated_replies(self):
        message_id = self.send()
        self.assertEqual(self.state(message_id), "queued")
        self.assertEqual(self.claim()["message"]["id"], message_id)
        self.assertEqual(self.state(message_id), "claimed")
        self.begin(message_id)
        self.assertEqual(self.state(message_id), "submitted")
        self.reply(message_id, "acknowledged", body="ack rev 4")
        self.assertEqual(self.state(message_id), "acknowledged")
        self.reply(message_id, "completed", body="done")
        self.assertEqual(self.state(message_id), "completed")
        mail = self.ok("inbox", "--agent", "bot")["messages"]
        self.assertEqual(sorted(m["reply_state"] for m in mail), ["acknowledged", "completed"])
        self.assertTrue(all(m["correlation_id"] == message_id and m["in_reply_to"] == message_id for m in mail))
        self.assert_consistent()

    def test_recipient_isolation(self):
        other = self.send(to="reviewer")
        self.assertIsNone(self.claim("builder")["message"])
        self.assertEqual(self.state(other), "queued")
        for argv in (["begin", "--agent", "builder", "--session", "s1", "--id", other],
                     ["release", "--agent", "builder", "--session", "s1", "--id", other],
                     ["reply", "--agent", "builder", "--session", "s1", "--id", other, "--state", "completed", "--body", "x"]):
            self.refused(*argv)
        envelope = json.loads((self.root / "mail/reviewer/new" / f"{other}.json").read_text())
        envelope["id"] = envelope["correlation_id"] = "m-misplaced-0001"
        (self.root / "mail/builder/new/m-misplaced-0001.json").write_text(json.dumps(envelope))
        self.assertIsNone(self.claim("builder")["message"])
        self.assertEqual(len(list((self.root / "mail/builder/rejected").glob("*.reason.json"))), 1)
        self.assertEqual(self.claim("reviewer", "s2")["message"]["id"], other)

    def test_duplicates_are_delivered_once(self):
        message_id = self.send()
        self.refused("send", "--from", "bot", "--to", "builder", "--body", "again", "--id", message_id)
        original = (self.root / "mail/builder/new" / f"{message_id}.json").read_text()
        self.deliver()
        (self.root / "mail/builder/new" / f"{message_id}.json").write_text(original)  # replayed file
        self.assertIsNone(self.claim()["message"])
        self.assertEqual(self.state(message_id), "submitted")
        self.assertFalse(self.begin(message_id)["changed"])
        self.reply(message_id, "acknowledged")
        self.assertFalse(self.reply(message_id, "acknowledged")["changed"])
        self.assertEqual(len(self.ok("inbox", "--agent", "bot")["messages"]), 1)

    def test_concurrent_claims_hand_out_one_message_and_one_charge(self):
        self.send()
        self.send()
        with ThreadPoolExecutor(8) as pool:  # overlapping callbacks of one session
            results = list(pool.map(lambda _: self.claim(), range(8)))
        self.assertEqual(len([r for r in results if r.get("message")]), 1)
        self.assertEqual(len([r for r in results if r.get("in_flight")]), 7)
        self.assertEqual(self.budget()["used_last_hour"], 1)

    def test_malformed_partial_and_expired_do_not_block_good_mail(self):
        new = self.root / "mail/builder/new"
        self.ok("fsck")
        (new / "m-truncated-0001.json").write_text('{"id": "m-truncated-0001", "to": "ro')
        os.utime(new / "m-truncated-0001.json", (1, 1))  # old: not a write in progress
        (new / "notes.txt").write_text("hello")
        (new / "m-oversize-0001.json").write_text("x" * 70000)
        expired = self.send(ttl_minutes=1)
        self.clock = at(5)
        good = self.send()
        self.assertEqual(self.claim()["message"]["id"], good)
        self.assertEqual(self.state(expired), "expired")
        rejected = self.root / "mail/builder/rejected"
        self.assertEqual(len(list(rejected.glob("*.reason.json"))), 3)
        self.assertEqual(len(list(rejected.iterdir())), 6)  # the evidence is kept beside each reason
        self.assert_consistent()

    def test_fresh_partial_write_is_left_alone(self):
        self.ok("fsck")
        partial = self.root / "mail/builder/new/m-partial-00001.json"
        partial.write_text('{"id": "m-partial-0')
        stamp = datetime.datetime.fromisoformat(T0.replace("Z", "+00:00")).timestamp()
        os.utime(partial, (stamp, stamp))
        self.assertIsNone(self.claim()["message"])
        self.assertTrue(partial.is_file())

    def test_traversal_and_symlinks_are_refused(self):
        self.ok("fsck")
        outside = Path(self.tmp.name + "-outside")
        outside.mkdir()
        try:
            (outside / "secret.json").write_text('{"secret": true}')
            os.symlink(outside / "secret.json", self.root / "mail/builder/new/m-symlink-0001.json")
            self.assertIsNone(self.claim()["message"])
            self.assertTrue((outside / "secret.json").is_file())
            for bad in ("../builder", "builder/../reviewer", "/etc"):
                self.refused("claim", "--agent", bad, "--session", "s1")
            self.refused("reply", "--agent", "builder", "--session", "s1", "--id", "../../etc/passwd",
                         "--state", "completed", "--body", "x")
            self.refused("claim", "--agent", "builder", "--session", "../s")
            (self.root / "mail/spare/new").rmdir()
            os.symlink(outside, self.root / "mail/spare/new")
            self.refused("claim", "--agent", "spare", "--session", "s1")
            self.assertEqual(sorted(p.name for p in outside.iterdir()), ["secret.json"])
        finally:
            for path in outside.iterdir():
                path.unlink()
            outside.rmdir()

    def test_status_is_read_only(self):
        before = sorted(p.name for p in self.root.iterdir())
        report = self.ok("status")
        self.assertEqual(report["agents"]["builder"]["presence"], "offline")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)

    def test_closed_session_stays_offline_and_queued(self):
        message_id = self.send()
        status = self.ok("status")["agents"]["builder"]
        self.assertEqual((status["presence"], status["queued"]), ("offline", 1))
        self.ok("heartbeat", "--agent", "builder", "--session", "s1")
        self.assertEqual(self.ok("status")["agents"]["builder"]["presence"], "online")
        self.clock = at(2)  # heartbeat went stale: a crash without a goodbye
        self.assertEqual(self.ok("status")["agents"]["builder"]["presence"], "offline")
        self.ok("heartbeat", "--agent", "builder", "--session", "s2")
        self.ok("offline", "--agent", "builder", "--session", "s2")
        self.assertEqual(self.ok("status")["agents"]["builder"]["presence"], "offline")
        self.assertEqual(self.state(message_id), "queued")

    def test_reader_mailbox_is_not_claimable(self):
        self.send(to="bot", sender="builder", kind="status")
        self.refused("claim", "--agent", "bot", "--session", "s1")
        self.assertEqual(len(self.ok("inbox", "--agent", "bot", "--archive")["messages"]), 1)
        self.assertEqual(self.ok("inbox", "--agent", "bot")["messages"], [])
        self.assert_consistent()

    # ---- root and configuration: fail closed --------------------------------

    def raw(self, *argv, root=None, env_root=None, **env_extra):
        """Run the helper with no test conveniences: (answer, exit status)."""
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_MAILBOX_ROOT", "BRIDGE_FAIL_AT", "BRIDGE_FAKE_NOW")}
        env.update(env_extra)
        if env_root:
            env["CLAUDE_MAILBOX_ROOT"] = str(env_root)
        done = subprocess.run([sys.executable, str(HELPER), *(["--root", str(root)] if root else []), *argv],
                              capture_output=True, text=True, env=env)
        return (json.loads(done.stdout) if done.stdout.strip() else None), done.returncode

    COMMANDS = (["status"], ["fsck"], ["send", "--from", "bot", "--to", "builder", "--body", "b"],
                ["claim", "--agent", "builder", "--session", "s1"], ["check", "--agent", "builder"],
                ["heartbeat", "--agent", "builder", "--session", "s1"], ["pause"], ["inbox", "--agent", "bot"])

    def test_no_root_means_nothing_runs(self):
        for argv in self.COMMANDS:
            answer, status = self.raw(*argv)
            self.assertEqual((status, answer["ok"]), (2, False), argv)
            self.assertIn("no mailbox root", answer["error"])

    def test_root_without_config_is_refused_and_left_untouched(self):
        with tempfile.TemporaryDirectory() as plain:
            missing = Path(plain) / "not-there"
            for argv in self.COMMANDS:
                self.assertEqual(self.raw(*argv, root=plain)[1], 2, argv)
                self.assertEqual(self.raw(*argv, env_root=plain)[1], 2, argv)
                self.assertEqual(self.raw(*argv, root=missing)[1], 2, argv)
            self.assertEqual(os.listdir(plain), [])

    def test_invalid_config_fails_closed(self):
        agents = CONFIG["agents"]
        bad = ("", "[]", "{not json", json.dumps({"agents": agents}), json.dumps({"version": 2, "agents": agents}),
               json.dumps({"version": True, "agents": agents}),
               json.dumps({"version": 1, "agents": {}}), json.dumps({"version": 1, "agents": ["builder"]}),
               json.dumps({"version": 1, "agents": {"builder": "admin"}}),
               json.dumps({"version": 1, "agents": {"../builder": "session"}}),
               json.dumps({"version": 1, "agents": {"Builder": "session"}}),
               json.dumps({"version": 1, "agents": {"all": "session"}}),
               json.dumps({"version": 1, "agents": {f"a{n}": "reader" for n in range(33)}}),
               json.dumps({"version": 1, "agents": agents, "extra": 1}),
               json.dumps({"version": 1, "agents": agents, "limits": {"turns_per_hour": 0}}),
               json.dumps({"version": 1, "agents": agents, "limits": {"turns_per_hour": 61}}),
               json.dumps({"version": 1, "agents": agents, "limits": {"turns_per_day": "40"}}),
               json.dumps({"version": 1, "agents": agents, "limits": {"max_attempts": True}}),
               json.dumps({"version": 1, "agents": agents, "limits": {"turns_per_week": 5}}),
               json.dumps({"version": 1, "agents": agents, "limits": []}),
               "x" * 70000)
        for text in bad:
            with self.subTest(config=text[:60]):
                (self.root / "config.json").write_text(text)
                for argv in self.COMMANDS:
                    answer, status = self.call(*argv)
                    self.assertEqual((status, answer["ok"]), (2, False), argv)
                self.assertEqual(sorted(p.name for p in self.root.iterdir()), [MARKER, "config.json"])

    def test_symlinked_config_is_refused(self):
        real = Path(self.tmp.name + "-config.json")
        real.write_text(json.dumps(CONFIG))
        try:
            (self.root / "config.json").unlink()
            os.symlink(real, self.root / "config.json")
            self.assertEqual(self.call("status")[1], 2)
            self.assertEqual(self.call("claim", "--agent", "builder", "--session", "s1")[1], 2)
        finally:
            real.unlink()

    def test_only_configured_agents_exist(self):
        (self.root / "config.json").write_text(json.dumps({"version": 1, "agents": {"solo": "session", "ext": "reader"}}))
        self.refused("send", "--from", "bot", "--to", "solo", "--body", "b")
        self.refused("send", "--from", "ext", "--to", "builder", "--body", "b")
        self.refused("claim", "--agent", "builder", "--session", "s1")
        self.refused("claim", "--agent", "ext", "--session", "s1")  # a reader is never woken
        self.refused("check", "--agent", "ext")
        self.refused("check", "--agent", "builder")
        self.refused("inbox", "--agent", "builder")
        self.refused("pause", "--agent", "builder")
        self.refused("status", "--agent", "builder")
        self.assertEqual(self.ok("check", "--agent", "solo")["agent"], "solo")
        self.assertEqual(sorted(self.ok("status")["agents"]), ["ext", "solo"])
        message_id = self.ok("send", "--from", "ext", "--to", "solo", "--body", "b")["id"]
        self.assertEqual(self.claim("solo")["message"]["id"], message_id)
        self.assertFalse((self.root / "mail/builder").exists())

    def test_check_is_read_only(self):
        before = sorted(p.name for p in self.root.iterdir())
        self.ok("check", "--agent", "builder")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)

    def test_configured_limits_are_applied(self):
        (self.root / "config.json").write_text(json.dumps(CONFIG | {"limits": {
            "turns_per_hour": 2, "turns_per_day": 3, "max_attempts": 1, "default_ttl_minutes": 5}}))
        first = self.send()
        created, expires = (datetime.datetime.fromisoformat(self.record(first)[key].replace("Z", "+00:00"))
                            for key in ("created_utc", "expires_utc"))
        self.assertEqual((expires - created).total_seconds(), 300)
        self.send(), self.send(), self.send()
        abandoned = self.claim()["message"]["id"]
        self.assertEqual(self.ok("offline", "--agent", "builder", "--session", "s1")["released"][0]["state"], "failed")
        self.assertEqual(self.state(abandoned), "failed")  # max_attempts is 1
        self.assertIsNotNone(self.deliver()["message"])
        limited = self.claim()
        self.assertTrue(limited["limited"])
        self.assertEqual((limited["budget"]["per_hour"], limited["budget"]["per_day"]), (2, 3))
        self.clock = at(61)
        self.send(ttl_minutes=60)
        self.assertIsNotNone(self.deliver()["message"])
        self.clock = at(122)
        self.send(ttl_minutes=60)
        self.assertTrue(self.claim()["limited"])  # the daily cap of 3 now holds

    def test_init_creates_a_root_once_and_the_environment_variable_selects_it(self):
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent).resolve() / "mailbox"
            answer, status = self.raw("init", "--session", "worker", "--reader", "bot", root=root)
            self.assertEqual(status, 0, answer)
            config = json.loads((root / "config.json").read_text())
            self.assertEqual(config["agents"], {"worker": "session", "bot": "reader"})
            self.assertEqual(config["limits"], {"turns_per_hour": 6, "turns_per_day": 40, "max_attempts": 3,
                                                "default_ttl_minutes": 1440})
            self.assertTrue((root / "mail/worker/tmp").is_dir() and (root / "mail/bot/new").is_dir())
            self.assertEqual(self.raw("init", "--session", "worker", root=root)[1], 1)  # never overwrites
            self.assertEqual(json.loads((root / "config.json").read_text()), config)
            sent, status = self.raw("send", "--from", "bot", "--to", "worker", "--body", "hello", env_root=root)
            self.assertEqual(status, 0, sent)
            claimed, _ = self.raw("claim", "--agent", "worker", "--session", "s1", env_root=root)
            self.assertEqual(claimed["message"]["id"], sent["id"])
            for argv in (["init"], ["init", "--session", "a", "--reader", "a"], ["init", "--session", "../x"],
                         ["init", "--session", "all"]):
                fresh = Path(parent) / "fresh"
                self.assertNotEqual(self.raw(*argv, root=fresh)[1], 0, argv)
                self.assertFalse(fresh.exists(), argv)

    def test_test_hooks_are_ignored_outside_a_marked_root(self):
        (self.root / MARKER).unlink()
        answer, status = self.raw("send", "--from", "bot", "--to", "builder", "--body", "b", root=self.root,
                                  BRIDGE_FAIL_AT="send:body", BRIDGE_FAKE_NOW="2001-01-01T00:00:00Z")
        self.assertEqual(status, 0, answer)
        self.assertNotEqual(self.record(answer["id"])["created_utc"][:4], "2001")

    # ---- pause against a pending delivery --------------------------------

    def test_pause_pause_before_claim(self):
        message_id = self.send()
        self.ok("pause", "--agent", "builder")
        claimed = self.claim()
        self.assertTrue(claimed["paused"])
        self.assertIsNone(claimed["message"])
        self.assertEqual(self.state(message_id), "queued")
        self.ok("resume", "--agent", "builder")
        self.ok("pause")
        self.assertTrue(self.claim()["paused"])
        self.ok("resume")
        self.assertEqual(self.claim()["message"]["id"], message_id)

    def test_pause_pause_after_claim_withdraws_before_the_turn_starts(self):
        message_id = self.send()
        self.claim()
        self.ok("pause", "--agent", "builder")
        gate = self.begin(message_id)
        self.assertEqual((gate["start"], gate["revocable"], gate["reason"]), (False, True, "paused"))
        self.assertEqual(self.state(message_id), "claimed")  # asking changes nothing
        self.assertEqual(self.ok("release", "--agent", "builder", "--session", "s1", "--id", message_id,
                                 "--reason", "paused")["state"], "queued")
        self.assertFalse(self.record(message_id)["turn_started"])
        self.assertIsNone(self.claim()["message"])
        self.ok("resume", "--agent", "builder")
        self.assertEqual(self.claim()["message"]["id"], message_id)
        self.assertTrue(self.begin(message_id)["start"])
        record = self.record(message_id)
        self.assertEqual([h["state"] for h in record["history"]], ["queued", "claimed", "queued", "claimed", "submitted"])
        self.assertEqual(len(record["attempts"]), 2)
        self.assert_consistent()

    def test_pause_turn_that_could_not_be_withdrawn_is_recorded_as_started(self):
        message_id = self.send()
        self.claim()
        self.ok("pause")
        self.assertFalse(self.begin(message_id)["start"])
        self.assertTrue(self.begin(message_id, "builder", "s1", "--force")["start"])
        self.assertEqual(self.state(message_id), "submitted")
        self.assertIn("could not be withdrawn", self.record(message_id)["history"][-1]["note"])

    def test_pause_pause_leaves_a_running_turn_alone(self):
        message_id = self.send()
        self.deliver()
        self.ok("pause")
        self.assertEqual(self.state(message_id), "submitted")
        self.reply(message_id, "completed")
        self.assertEqual(self.state(message_id), "completed")

    def test_pause_shutdown_during_pending_delivery_requeues(self):
        message_id = self.send()
        self.ok("heartbeat", "--agent", "builder", "--session", "s1")
        self.claim()
        self.assertEqual(self.ok("offline", "--agent", "builder", "--session", "s1")["released"][0]["state"], "queued")
        self.assertEqual(self.state(message_id), "queued")
        gate = self.begin(message_id)  # the old prompt firing late is not this session's delivery any more
        self.assertEqual((gate["start"], gate["revocable"]), (False, False))
        self.assert_consistent()

    # ---- crash recovery: every persistent boundary, killed or failing ----------------------

    FLOW = (("send", ["send", "--from", "bot", "--to", "builder", "--body", "b", "--id", FIXED]),
            ("claim", ["claim", "--agent", "builder", "--session", "s1"]),
            ("begin", ["begin", "--agent", "builder", "--session", "s1", "--id", FIXED]),
            ("ack", ["reply", "--agent", "builder", "--session", "s1", "--id", FIXED, "--state", "acknowledged", "--body", "a"]),
            ("done", ["reply", "--agent", "builder", "--session", "s1", "--id", FIXED, "--state", "completed", "--body", "c"]))
    BOUNDARIES = {"send": ("send:body", "send:state"), "claim": ("claim:state", "claim:move"),
                  "begin": ("begin:state",),
                  "ack": ("reply:body", "reply:rstate", "reply:ostate", "reply:move"),
                  "done": ("reply:body", "reply:rstate", "reply:ostate", "reply:move")}

    def finish(self):
        """The supported recovery path: reconcile, then simply retry each step."""
        self.call(*self.FLOW[0][1])  # refused when the first send landed, repaired when it half-landed
        for _ in range(4):
            if self.state(FIXED) == "queued":
                self.claim()
            if self.state(FIXED) == "claimed":
                self.begin(FIXED)
        self.call(*self.FLOW[3][1])  # refused, correctly, when the message is already completed
        self.reply(FIXED, "completed", body="c")

    def crash_matrix(self, mode, expected_status):
        for step, labels in self.BOUNDARIES.items():
            for label in labels:
                with self.subTest(step=step, label=label, mode=mode):
                    self.tearDown()
                    self.setUp()
                    for name, argv in self.FLOW:
                        if name == step:
                            answer, status = self.call(*argv, fail_at=label + mode)
                            self.assertEqual(status, expected_status, (label, answer))
                            break
                        self.ok(*argv)
                    self.assert_consistent()
                    self.finish()
                    self.assertEqual(self.state(FIXED), "completed")
                    replies = self.ok("inbox", "--agent", "bot")["messages"]
                    self.assertEqual(sorted(m["reply_state"] for m in replies), ["acknowledged", "completed"])
                    self.assertTrue(all(m["in_reply_to"] == FIXED and m["correlation_id"] == FIXED for m in replies))
                    record = self.record(FIXED)
                    self.assertTrue(record["turn_started"])
                    self.assertEqual(len(record["attempts"]), 1)
                    self.assertEqual(self.budget()["used_last_hour"], 1)
                    self.assert_consistent()

    def test_crash_process_death_at_every_boundary_is_recoverable(self):
        self.crash_matrix("", 99)

    def test_crash_write_failure_at_every_boundary_is_recoverable(self):
        self.crash_matrix("!raise", 1)

    def test_crash_release_interrupted_between_state_and_move(self):
        message_id = self.send()
        self.claim()
        _, status = self.call("release", "--agent", "builder", "--session", "s1", "--id", message_id, fail_at="release:state")
        self.assertEqual(status, 99)
        self.assertEqual(self.state(message_id), "queued")
        self.assertTrue((self.root / "mail/builder/claimed" / f"{message_id}.json").is_file())  # the move never happened
        self.assert_consistent()  # reconcile rolled it forward
        self.assertEqual(self.claim()["message"]["id"], message_id)

    def test_crash_orphans_are_made_visible_not_deleted(self):
        message_id = self.send()
        self.claim()
        (self.root / "mail/builder/claimed" / f"{message_id}.json").unlink()  # a body lost outside the helper
        problems = self.ok("fsck")["needs_reconciliation"]["builder"]
        self.assertEqual([p["id"] for p in problems], [message_id])
        self.assertEqual(self.ok("status")["agents"]["builder"]["needs_reconciliation"], [message_id])
        self.assertEqual(self.state(message_id), "claimed")  # never relabelled delivered or completed
        (self.root / "mail/builder/closed/m-stray-file-0001.json").write_text("{}")  # a body with no record
        self.ok("fsck")
        self.assertEqual(len(list((self.root / "mail/builder/rejected").glob("*m-stray-file-0001.json"))), 1)
        self.ok("resolve", "--id", message_id, "--state", "blocked", "--note", "body lost; closed by the operator")
        self.assertEqual(self.state(message_id), "blocked")
        self.assertEqual(self.ok("fsck")["needs_reconciliation"]["builder"], [])

    def test_crash_unreadable_state_record_is_set_aside_and_counted(self):
        message_id = self.send()
        self.deliver()
        (self.root / "state" / f"{message_id}.json").write_text('{"id": "trunc')
        second = self.send()
        self.assertEqual(self.claim()["message"]["id"], second)  # other mail stays operable
        self.assertEqual(len(list((self.root / "state/corrupt").iterdir())), 1)
        self.assertEqual(self.ok("status")["unreadable_state_records"], 1)
        self.assertGreaterEqual(self.budget()["used_last_hour"], 1)

    # ---- limits and accounting --------------------------------------------------------

    def test_limits_reply_before_the_started_mark_is_still_charged_once(self):
        ids = [self.send(body=f"n{index}") for index in range(7)]
        for _ in range(6):
            claimed = self.claim()["message"]["id"]
            self.reply(claimed, "acknowledged")  # the answer lands before the mod's own mark
            late = self.begin(claimed)
            self.assertEqual((late["start"], late["changed"], late["state"]), (True, False, "acknowledged"))
            self.reply(claimed, "completed")
            self.begin(claimed)
            self.reply(claimed, "completed")
            self.assertTrue(self.record(claimed)["turn_started"])
        self.assertEqual(self.budget()["used_last_hour"], 6)
        self.assertTrue(self.claim()["limited"])
        self.assertEqual(sorted(self.state(i) for i in ids), ["completed"] * 6 + ["queued"])

    def test_limits_hourly_cap_at_the_boundary(self):
        for index in range(8):
            self.send(body=f"n{index}")
        self.assertEqual(len([1 for _ in range(8) if self.deliver().get("message")]), 6)
        limited = self.claim()
        self.assertTrue(limited["limited"])
        self.assertEqual(limited["budget"]["retry_after_s"], 3601)
        self.clock = at(59, 59)  # one second short of the hour: still closed
        self.assertTrue(self.claim()["limited"])
        self.clock = at(60)  # exactly one hour: those six charges have just left the window
        self.assertIsNotNone(self.deliver()["message"])
        self.assertEqual(self.budget()["used_last_hour"], 1)

    def test_limits_daily_cap_and_status_agree_after_restart(self):
        delivered = 0
        for index in range(45):
            self.clock = at(10 * index)
            self.send(body=f"n{index}")
            delivered += 1 if self.deliver(session=f"s{index}").get("message") else 0  # a new process each time
        self.assertEqual(delivered, 40)
        self.assertEqual(self.budget()["used_last_day"], 40)
        self.assertTrue(self.claim()["limited"])
        self.clock = at(24 * 60 + 1)
        self.assertIsNotNone(self.deliver()["message"])

    def test_limits_refund_only_for_a_prompt_the_session_refused(self):
        message_id = self.send()
        self.claim()
        self.ok("release", "--agent", "builder", "--session", "s1", "--id", message_id, "--refund", "--reason", "dropped")
        self.assertEqual(self.budget()["used_last_hour"], 0)
        self.claim()
        self.ok("release", "--agent", "builder", "--session", "s1", "--id", message_id, "--reason", "withdrawn at start")
        self.assertEqual(self.budget()["used_last_hour"], 1)  # an ambiguous start stays charged

    # ---- ownership ---------------------------------------------------

    def test_ownership_quick_restart_recovers_once_the_old_holder_is_gone(self):
        message_id = self.send()
        self.ok("heartbeat", "--agent", "builder", "--session", "sA")
        self.claim(session="sA")  # A dies here without a goodbye
        self.clock = at(seconds=30)
        self.assertTrue(self.ok("heartbeat", "--agent", "builder", "--session", "sB")["conflict"])
        self.assertTrue(self.claim(session="sB")["conflict"])
        self.assertEqual(self.record(message_id)["session"], "sA")
        self.clock = at(seconds=125)  # ordinary ticks, no second restart
        beat = self.ok("heartbeat", "--agent", "builder", "--session", "sB")
        self.assertEqual(beat["released"], [{"id": message_id, "state": "queued"}])
        self.assertEqual(self.claim(session="sB")["message"]["id"], message_id)
        self.assertEqual(len(self.record(message_id)["attempts"]), 2)

    def test_ownership_live_holder_keeps_its_claim_and_its_pending_delivery(self):
        message_id = self.send()
        self.ok("heartbeat", "--agent", "builder", "--session", "sA")
        self.claim(session="sA")
        self.clock = at(seconds=80)
        self.ok("heartbeat", "--agent", "builder", "--session", "sA", "--pending", message_id)  # still waiting for idle
        self.assertEqual(self.state(message_id), "claimed")
        self.clock = at(seconds=160)  # an old claim, but its holder is alive
        self.ok("heartbeat", "--agent", "builder", "--session", "sA", "--pending", message_id)
        self.assertTrue(self.ok("heartbeat", "--agent", "builder", "--session", "sB")["conflict"])
        self.assertTrue(self.claim(session="sB")["conflict"])
        self.assertEqual((self.state(message_id), self.record(message_id)["session"]), ("claimed", "sA"))
        # Same session, no delivery pending in memory (it reloaded): its own leftover goes back to the queue.
        self.assertEqual(self.ok("heartbeat", "--agent", "builder", "--session", "sA")["released"][0]["state"], "queued")

    def test_ownership_retries_are_bounded(self):
        message_id = self.send()
        for attempt in range(3):
            self.assertEqual(self.claim(session=f"s{attempt}")["message"]["id"], message_id)
            released = self.ok("offline", "--agent", "builder", "--session", f"s{attempt}")["released"]
            self.assertEqual(released[0]["state"], "queued" if attempt < 2 else "failed")
        self.assertEqual(self.state(message_id), "failed")
        self.assertIsNone(self.claim(session="s9")["message"])
        self.assertEqual(self.budget()["used_last_hour"], 3)
        self.assert_consistent()

    def test_ownership_operations_are_bound_to_the_delivery_session(self):
        message_id = self.send()
        self.ok("heartbeat", "--agent", "builder", "--session", "sA")
        self.claim(session="sA")
        for argv in (["release", "--agent", "builder", "--session", "sB", "--id", message_id],
                     ["reply", "--agent", "builder", "--session", "sB", "--id", message_id, "--state", "completed", "--body", "x"]):
            self.assertIn("different session", self.refused(*argv))
        self.assertFalse(self.begin(message_id, "builder", "sB")["start"])
        self.assertFalse(self.begin(message_id, "builder", "sB", "--force")["start"])
        self.assertEqual((self.state(message_id), self.record(message_id)["session"]), ("claimed", "sA"))
        self.begin(message_id, "builder", "sA")
        self.refused("reply", "--agent", "builder", "--session", "sB", "--id", message_id, "--state", "acknowledged", "--body", "x")
        self.reply(message_id, "completed", session="sA")

    def test_lifecycle_session_identity_change_hands_over_cleanly(self):
        message_id = self.send()
        self.ok("heartbeat", "--agent", "builder", "--session", "old")
        self.claim(session="old")
        self.ok("offline", "--agent", "builder", "--session", "old")  # /clear or /resume: same process, new id
        self.assertFalse(self.ok("heartbeat", "--agent", "builder", "--session", "new")["conflict"])
        self.assertEqual(self.claim(session="new")["message"]["id"], message_id)
        self.assertEqual(self.ok("status")["agents"]["builder"]["presence"], "online")

    # ---- expiry --------------------------------------------------------------

    def envelope(self, message_id, **changes):
        base = {"v": 1, "id": message_id, "from": "bot", "to": "builder", "kind": "instruction",
                "correlation_id": message_id, "subject": "", "created_utc": T0, "expires_utc": at(60), "body": "b"}
        base.update(changes)
        self.ok("fsck")
        (self.root / "mail/builder/new" / f"{message_id}.json").write_text(json.dumps(base))

    def test_expiry_manual_envelopes_obey_the_same_limits(self):
        self.envelope("m-too-long-0001", expires_utc=at(7 * 24 * 60 + 1))
        self.envelope("m-bad-date-0001", expires_utc="next tuesday")
        self.envelope("m-no-zone-00001", created_utc="2026-10-02T04:00:00")
        self.envelope("m-backward-0001", expires_utc=at(-1))
        self.envelope("m-future-00001", created_utc=at(30), expires_utc=at(90))
        self.envelope("m-null-date-0001", created_utc=None)
        self.envelope("m-max-ok-00001", expires_utc=at(7 * 24 * 60))
        self.assertEqual(self.claim()["message"]["id"], "m-max-ok-00001")
        self.assertEqual(len(list((self.root / "mail/builder/rejected").glob("*.reason.json"))), 6)

    def test_expiry_expiry_is_a_delivery_cutoff_at_turn_start(self):
        message_id = self.send(ttl_minutes=10)
        self.claim()
        self.clock = at(9, 59)
        self.assertEqual(self.begin(message_id, "builder", "s1")["start"], True)
        second = self.send(ttl_minutes=10)
        self.claim()
        self.clock = at(19, 59)  # exactly its expiry instant, while it waits for idle
        gate = self.begin(second)
        self.assertEqual((gate["start"], gate["reason"]), (False, "expired"))
        self.assertEqual(self.ok("release", "--agent", "builder", "--session", "s1", "--id", second)["state"], "expired")
        self.assertFalse(self.record(second)["turn_started"])
        self.assertNotIn("submitted", [h["state"] for h in self.record(second)["history"]])
        self.assert_consistent()

    # ---- no wake loops --------------------------------------------------------

    def test_loops_replies_and_status_never_wake_in_either_direction(self):
        for kind in ("ack", "reply", "status"):
            message_id = self.send(to="builder", sender="reviewer", kind=kind)
            self.assertIsNone(self.claim()["message"])
            self.assertEqual(self.state(message_id), "recorded")
        to_builder = self.send(to="builder", sender="reviewer")
        self.deliver("builder", "s1")
        self.reply(to_builder, "acknowledged")
        self.reply(to_builder, "completed")
        self.assertIsNone(self.claim("reviewer", "s2")["message"])  # the answers are filed, not delivered
        to_reviewer = self.send(to="reviewer", sender="builder")
        self.deliver("reviewer", "s2")
        self.reply(to_reviewer, "completed", agent="reviewer", session="s2")
        self.assertIsNone(self.claim("builder", "s1")["message"])
        # A replayed reply file with the same deterministic id is quarantined, not delivered.
        replayed = next((self.root / "mail/builder/closed").glob("r-*.json"))
        (self.root / "mail/builder/new" / replayed.name).write_text(replayed.read_text())
        self.assertIsNone(self.claim("builder", "s1")["message"])
        self.assertEqual((self.budget("builder")["used_last_hour"], self.budget("reviewer")["used_last_hour"]), (1, 1))
        self.assert_consistent()


if __name__ == "__main__":
    unittest.main()
