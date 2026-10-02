#!/usr/bin/env python3
"""File mailbox between external agents and Claude Code sessions: recipient-scoped,
crash-reconciling, no network.

Every path this helper touches is under one directory, the mailbox root, named
by --root or the CLAUDE_MAILBOX_ROOT environment variable. The root's
config.json lists the agents and the limits; without a valid one every command
except `init` refuses.

One rule makes multi-file changes recoverable: the state record is written
first and is the commit point; where a message body sits (new/, claimed/,
closed/) is derived from it, and `reconcile` rolls any interrupted move
forward. See README.md for what is and is not guaranteed.
"""
import argparse
import datetime
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

ROOT_ENV = "CLAUDE_MAILBOX_ROOT"
CONFIG_NAME = "config.json"
AGENT_TYPES = ("session", "reader")  # session: a Claude Code session the mod can wake; reader: reads files itself
AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
RESERVED_NAMES = ("all",)  # control/pause-all is the global switch
MAX_AGENTS = 32
BOXES = ("tmp", "new", "claimed", "closed", "rejected")
# Each limit: (default, lowest allowed, highest allowed). config.json may set any of them.
LIMITS = {"turns_per_hour": (6, 1, 60), "turns_per_day": (40, 1, 1000), "max_attempts": (3, 1, 10),
          "default_ttl_minutes": (24 * 60, 1, 7 * 24 * 60)}

# Filled from config.json by load_config(); empty means nothing is allowed.
AGENTS = ()
WAKEABLE = ()
KINDS = ("instruction", "ack", "reply", "status")
STARTED = ("submitted", "acknowledged")
TERMINAL = ("completed", "blocked", "expired", "rejected", "failed", "recorded")
PLACE = {"queued": "new", "claimed": "claimed", "submitted": "claimed", "acknowledged": "claimed"} | {
    name: "closed" for name in TERMINAL}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MAX_BYTES = 64 * 1024
MAX_STATE_BYTES = 48 * 1024
MAX_SUBJECT_BYTES = 512
MAX_NOTE_BYTES = 200
MAX_HISTORY = 40
DEFAULT_TTL_MIN = 24 * 60
MAX_TTL_MIN = 7 * 24 * 60
MAX_FUTURE_S = 300
MAX_ATTEMPTS = 3
TURNS_PER_HOUR = 6
TURNS_PER_DAY = 40
PRESENCE_TTL_S = 90
PARTIAL_GRACE_S = 5
ABANDONED_TMP_S = 600
TEST_MARKER = ".mailbox-bridge-test-root"

ROOT = None  # set by main() from --root or the environment; never guessed


class Refused(Exception):
    pass


class NotConfigured(Refused):
    """No usable root or config: nothing is read, written or allowed."""


def is_test_root():
    return ROOT is not None and (ROOT / TEST_MARKER).is_file()


def now():
    fake = os.environ.get("BRIDGE_FAKE_NOW")
    if fake and is_test_root():
        return datetime.datetime.fromisoformat(fake.replace("Z", "+00:00"))
    return datetime.datetime.now(datetime.timezone.utc)


def checkpoint(label):
    """Test-only fault injection after a persistent step: die like a killed process, or fail like a full disk."""
    wanted = os.environ.get("BRIDGE_FAIL_AT")
    if not wanted or not is_test_root():
        return
    name, _, mode = wanted.partition("!")
    if name != label:
        return
    if mode == "raise":
        raise OSError(errno.ENOSPC, "injected write failure")
    os._exit(99)


_fault_calls = {}


def fault(op):
    """Test-only: make the nth call of one primitive fail, as a disk would (BRIDGE_FAULT=op:n[:short])."""
    wanted = os.environ.get("BRIDGE_FAULT")
    if not wanted or not is_test_root():
        return None
    name, _, rest = wanted.partition(":")
    nth, _, mode = rest.partition(":")
    if name != op:
        return None
    _fault_calls[op] = _fault_calls.get(op, 0) + 1
    if _fault_calls[op] < int(nth or 1):
        return None
    if mode in ("short", "shortok"):  # short: a partial write, then failure; shortok: a partial write, then success
        if _fault_calls[op] == int(nth or 1):
            return "short"
        if mode == "shortok":
            return None
    raise OSError(errno.EIO, f"injected {op} failure")


def os_write(fd, data):
    if fault("write") == "short":
        return os.write(fd, data[:max(1, len(data) // 2)])  # the kernel took only part; the next call fails
    return os.write(fd, data)


def os_fsync(fd):
    fault("fsync")
    os.fsync(fd)


def os_link(src, dst):
    fault("link")
    os.link(src, dst)


def os_replace(src, dst):
    fault("replace")
    os.replace(src, dst)


def os_rename(src, dst):
    fault("rename")
    os.rename(src, dst)


def clip(text, limit):
    """Text cut to at most `limit` UTF-8 bytes, control characters flattened, so its stored size is bounded."""
    flat = "".join(ch if ch.isprintable() else " " for ch in str(text or ""))
    return flat.encode("utf-8")[:limit].decode("utf-8", "ignore")


def cmd_stray(args):
    """A prompt this bridge had withdrawn or lost track of started a turn anyway. Record it; change no state."""
    agent, session = wakeable(args.agent), session_of(args)
    with Lock():
        record = owned(agent, args.id)
        record.setdefault("stray_starts", []).append(
            {"at_utc": iso(now()), "session": session, "aborted": bool(args.aborted), "note": clip(args.note, MAX_NOTE_BYTES)})
        record["stray_starts"] = record["stray_starts"][-5:]
        save_state(record)
        return {"ok": True, "state": record["state"]}


def iso(moment):
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc(text):
    if not isinstance(text, str):
        raise ValueError("timestamp is not a string")
    moment = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("timestamp has no UTC offset")
    return moment


def safe_dir(*parts, create=True):
    """A real directory under ROOT, never a symlink; None when absent and not creating."""
    path = ROOT
    for part in parts:
        if part in ("", ".", "..") or "/" in part or "\\" in part or "\0" in part:
            raise Refused(f"unsafe path component: {part!r}")
        path = path / part
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            if not create:
                return None
            os.mkdir(path, 0o755)
            info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode):
            raise Refused(f"not a real directory (symlink or file): {path}")
    if os.path.realpath(path) != str(path):
        raise Refused(f"path escapes the mailbox root: {path}")
    return path


def read_small(path):
    """Bytes of a regular file, refusing symlinks and oversize files."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused("not a regular file")
        if info.st_size > MAX_BYTES:
            raise Refused(f"larger than {MAX_BYTES} bytes")
        return os.read(fd, MAX_BYTES + 1), info.st_mtime
    finally:
        os.close(fd)


def write_tmp(directory, data):
    tmp = directory / f".tmp-{uuid.uuid4().hex}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        # os.write may accept fewer bytes than asked: keep going until every byte is in, or fail.
        view = memoryview(data)
        while view:
            written = os_write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "write accepted no bytes")
            view = view[written:]
        os_fsync(fd)
        if os.fstat(fd).st_size != len(data):
            raise OSError(errno.EIO, "file is not the size that was written")
    except BaseException:
        os.close(fd)
        os.unlink(tmp)  # a partial file never replaces or becomes anything
        raise
    os.close(fd)
    return tmp


def write_new(directory, name, data):
    """Publish without ever overwriting: FileExistsError when the name is taken."""
    tmp = write_tmp(directory, data)
    try:
        os_link(tmp, directory / name)
    finally:
        os.unlink(tmp)


def write_replace(directory, name, value):
    data = (json.dumps(value, indent=2) + "\n").encode()
    if len(data) > MAX_STATE_BYTES:
        raise Refused(f"record would be {len(data)} bytes; the limit is {MAX_STATE_BYTES}")
    tmp = write_tmp(directory, data)
    try:
        os_replace(tmp, directory / name)
    except BaseException:
        os.unlink(tmp)
        raise


def read_json(path):
    try:
        raw, _ = read_small(path)
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, Refused):
        return None


class Lock:
    """One exclusive flock for every mutation; the kernel drops it when the process dies."""

    def __enter__(self):
        self.fd = os.open(ROOT / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


# ---- configuration -------------------------------------------------------

def check_config(value):
    """(agents, limits) from a parsed config.json, or raise NotConfigured naming what is wrong. Strict:
    an unknown key or an out-of-range limit refuses the whole file rather than being ignored."""
    if not isinstance(value, dict):
        raise NotConfigured("config.json is not a JSON object")
    unknown = sorted(set(value) - {"version", "agents", "limits"})
    if unknown:
        raise NotConfigured(f"config.json has unknown keys: {', '.join(unknown)}")
    if value.get("version") != 1 or isinstance(value.get("version"), bool):
        raise NotConfigured("config.json version must be 1")
    agents = value.get("agents")
    if not isinstance(agents, dict) or not agents:
        raise NotConfigured("config.json must list at least one agent")
    if len(agents) > MAX_AGENTS:
        raise NotConfigured(f"config.json lists more than {MAX_AGENTS} agents")
    for name, kind in agents.items():
        if not AGENT_RE.match(name) or name in RESERVED_NAMES:
            raise NotConfigured(f"bad agent name: {name!r}")
        if kind not in AGENT_TYPES:
            raise NotConfigured(f"agent {name!r} must be \"session\" or \"reader\"")
    given = value.get("limits", {})
    if not isinstance(given, dict):
        raise NotConfigured("config.json limits is not an object")
    unknown = sorted(set(given) - set(LIMITS))
    if unknown:
        raise NotConfigured(f"config.json has unknown limits: {', '.join(unknown)}")
    limits = {}
    for name, (default, lowest, highest) in LIMITS.items():
        number = given.get(name, default)
        if not isinstance(number, int) or isinstance(number, bool) or not lowest <= number <= highest:
            raise NotConfigured(f"limit {name} must be a whole number from {lowest} to {highest}")
        limits[name] = number
    return dict(agents), limits


def load_config():
    """Read ROOT/config.json into the module's agent lists and limits. Anything wrong: NotConfigured."""
    global AGENTS, WAKEABLE, MAX_ATTEMPTS, TURNS_PER_HOUR, TURNS_PER_DAY, DEFAULT_TTL_MIN
    if not ROOT.is_dir():
        raise NotConfigured(f"mailbox root is not a directory: {ROOT}")
    try:
        raw, _ = read_small(ROOT / CONFIG_NAME)
    except FileNotFoundError:
        raise NotConfigured(f"no {CONFIG_NAME} in the mailbox root; run `init` first")
    except (OSError, Refused) as error:
        raise NotConfigured(f"{CONFIG_NAME} cannot be read: {error}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise NotConfigured(f"{CONFIG_NAME} is not valid JSON")
    agents, limits = check_config(value)
    AGENTS = tuple(agents)
    WAKEABLE = tuple(name for name, kind in agents.items() if kind == "session")
    TURNS_PER_HOUR, TURNS_PER_DAY = limits["turns_per_hour"], limits["turns_per_day"]
    MAX_ATTEMPTS, DEFAULT_TTL_MIN = limits["max_attempts"], limits["default_ttl_minutes"]


def box(agent, name, create=True):
    if agent not in AGENTS:
        raise Refused(f"unknown agent: {agent!r}")
    return safe_dir("mail", agent, name, create=create)


def names_in(directory):
    if directory is None:
        return []
    return sorted(name for name in os.listdir(directory) if name.endswith(".json") and not name.startswith("."))


# ---- state records -------------------------------------------------------

def normalized(record):
    """A record whose attempts are not a list (edited by hand, or damaged) is read as that many charged attempts."""
    attempts = record.get("attempts")
    if not isinstance(attempts, list):
        count = attempts if isinstance(attempts, int) else 0
        stamp = record.get("claimed_utc") or record.get("created_utc")
        record["attempts"] = [{"n": index + 1, "session": record.get("session"), "claimed_utc": stamp,
                               "charged": True, "outcome": "unknown"} for index in range(count)]
    return record


def load_state(message_id):
    if not ID_RE.match(message_id or ""):
        raise Refused("bad message id")
    record = read_json(safe_dir("state") / f"{message_id}.json")
    return normalized(record) if record else None


def all_states():
    """Every readable record; an unreadable or mislabelled one is set aside, never trusted or deleted."""
    directory = safe_dir("state")
    records = []
    for name in names_in(directory):
        record = read_json(directory / name)
        if not record or record.get("id") != name[:-5] or record.get("state") not in PLACE:
            os_rename(directory / name, safe_dir("state", "corrupt") / f"{now().strftime('%Y%m%dT%H%M%S%fZ')}-{name}")
            continue
        records.append(normalized(record))
    return records


def save_state(record, new_state=None, by=None, note=None):
    if new_state:
        record["state"] = new_state
        entry = {"state": new_state, "at_utc": iso(now()), "by": by}
        if note:
            entry["note"] = clip(note, MAX_NOTE_BYTES)
        history = record.setdefault("history", [])
        history.append(entry)
        if len(history) > MAX_HISTORY:
            # Keep the first entry and the newest; say how many were dropped rather than grow without bound.
            dropped = len(history) - MAX_HISTORY
            record["history_dropped"] = record.get("history_dropped", 0) + dropped
            record["history"] = history[:1] + history[1 + dropped:]
    write_replace(safe_dir("state"), f"{record['id']}.json", record)
    return record


def record_for(envelope):
    return {key: envelope.get(key) for key in
            ("id", "from", "to", "kind", "correlation_id", "created_utc", "expires_utc")} | {
        "subject": envelope.get("subject", ""), "state": None, "attempts": [], "session": None, "turn_started": False, "history": []}


def validate(envelope, agent, filename):
    """The envelope, or raise Refused naming what is wrong with it."""
    if not isinstance(envelope, dict):
        raise Refused("envelope is not a JSON object")
    message_id = envelope.get("id")
    if not isinstance(message_id, str) or not ID_RE.match(message_id):
        raise Refused("bad or missing id")
    if filename != f"{message_id}.json":
        raise Refused("file name does not match id")
    if envelope.get("to") != agent:
        raise Refused(f"addressed to {envelope.get('to')!r}, found in {agent!r} mailbox")
    if envelope.get("from") not in AGENTS:
        raise Refused("unknown sender")
    if envelope.get("kind") not in KINDS:
        raise Refused("unknown kind")
    if not isinstance(envelope.get("body"), str):
        raise Refused("body is not a string")
    subject = envelope.get("subject", "")
    if not isinstance(subject, str):
        raise Refused("subject is not a string")
    try:
        if len(subject.encode("utf-8")) > MAX_SUBJECT_BYTES:
            raise Refused(f"subject is longer than {MAX_SUBJECT_BYTES} bytes")
    except UnicodeEncodeError:
        raise Refused("subject is not valid text")
    correlation = envelope.get("correlation_id")
    if not isinstance(correlation, str) or not ID_RE.match(correlation):
        raise Refused("bad or missing correlation_id")
    try:
        created = parse_utc(envelope.get("created_utc"))
        expires = parse_utc(envelope.get("expires_utc"))
    except (ValueError, TypeError) as error:
        raise Refused(f"bad timestamp: {error}")
    if expires <= created:
        raise Refused("expires_utc is not after created_utc")
    if (expires - created).total_seconds() > MAX_TTL_MIN * 60:
        raise Refused(f"expires more than {MAX_TTL_MIN} minutes after creation")
    if (created - now()).total_seconds() > MAX_FUTURE_S:
        raise Refused("created_utc is in the future")
    return envelope


def is_expired(record):
    try:
        return parse_utc(record.get("expires_utc")) <= now()
    except (ValueError, TypeError):
        return True


# ---- mailbox files -------------------------------------------------------

def locate(agent, name):
    return [place for place in ("new", "claimed", "closed") if os.path.lexists(box(agent, place) / name)]


def quarantine(agent, place, name, reason):
    """Set an undeliverable file aside with its reason; its content is never shown to a model."""
    target = f"{now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}-{re.sub(r'[^A-Za-z0-9._-]', '_', name)[:80]}"
    os_rename(box(agent, place) / name, box(agent, "rejected") / target)
    write_replace(box(agent, "rejected"), target + ".reason.json",
                  {"file": name, "found_in": place, "reason": reason, "at_utc": iso(now())})


def settle(record):
    """Move the body to where the record says it belongs. Idempotent; flags what it cannot place."""
    agent, name = record["to"], f"{record['id']}.json"
    wanted = PLACE[record["state"]]
    found = locate(agent, name)
    if wanted in found:
        for extra in found:
            if extra != wanted:
                quarantine(agent, extra, name, f"second copy; record says {record['state']}")
    elif found:
        os_rename(box(agent, found[0]) / name, box(agent, wanted) / name)
        for extra in found[1:]:
            quarantine(agent, extra, name, f"second copy; record says {record['state']}")
    elif record["state"] not in TERMINAL and not record.get("needs_reconciliation"):
        record["needs_reconciliation"] = "body file is missing; state kept for a person to resolve"
        save_state(record)
        return
    if found and record.get("needs_reconciliation"):
        del record["needs_reconciliation"]
        save_state(record)


def reconcile(agent):
    """Repair every interrupted change for one mailbox. Returns that agent's records."""
    records = [record for record in all_states() if record.get("to") == agent]
    known = {record["id"] for record in records}
    for record in records:
        settle(record)
    for place in ("claimed", "closed"):
        for name in names_in(box(agent, place)):
            if name[:-5] not in known:
                quarantine(agent, place, name, "no state record for this file")
    for place in ("tmp", "new"):
        for name in os.listdir(box(agent, place)):
            if place == "new" and not name.startswith(".tmp-"):
                continue
            try:
                if now().timestamp() - os.lstat(box(agent, place) / name).st_mtime > ABANDONED_TMP_S:
                    quarantine(agent, place, name, "abandoned partial write")
            except FileNotFoundError:
                pass
    return records


def sweep(agent):
    """Validate what is in new/: reject, expire or record. Returns queued envelopes, oldest first."""
    queued = []
    for name in sorted(os.listdir(box(agent, "new"))):
        if name.startswith("."):
            continue
        path = box(agent, "new") / name
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if not name.endswith(".json") or not stat.S_ISREG(info.st_mode):
            quarantine(agent, "new", name, "not a regular .json file")
            continue
        try:
            raw, mtime = read_small(path)
            try:
                envelope = json.loads(raw.decode("utf-8"))
            except ValueError:
                # A writer that skipped tmp/ may still be mid-write: leave a fresh file alone.
                if now().timestamp() - mtime < PARTIAL_GRACE_S:
                    continue
                raise Refused("not valid JSON")
            envelope = validate(envelope, agent, name)
        except (Refused, OSError) as error:
            quarantine(agent, "new", name, str(error))
            continue
        record = load_state(envelope["id"])
        if record is None:
            record = save_state(record_for(envelope), "queued", envelope["from"], "found in mailbox")
        elif record.get("state") != "queued":
            quarantine(agent, "new", name, f"duplicate id; already {record.get('state')}")
            continue
        elif any(record.get(key) != envelope.get(key) for key in ("to", "from", "kind", "correlation_id")):
            quarantine(agent, "new", name, "id reused with different addressing")
            continue
        if record.get("needs_reconciliation"):
            continue
        if is_expired(record):
            save_state(record, "expired", "bridge", "expired while queued; never delivered")
            settle(record)
            continue
        if envelope["kind"] != "instruction" and agent in WAKEABLE:
            # Acks, replies and status never start a turn: this is the loop breaker.
            save_state(record, "recorded", "bridge", "non-instruction kinds never wake a session")
            settle(record)
            continue
        queued.append(envelope)
    queued.sort(key=lambda item: (item["created_utc"], item["id"]))
    return queued


# ---- presence, pause, limits ---------------------------------------------

def paused(agent):
    control = safe_dir("control", create=False)
    if control is None:
        return False
    return os.path.lexists(control / "pause-all") or os.path.lexists(control / f"pause-{agent}")


def presence(agent):
    directory = safe_dir("presence", create=False)
    record = (read_json(directory / f"{agent}.json") if directory else None) or {}
    online = False
    if record.get("online") and record.get("heartbeat_utc"):
        try:
            online = (now() - parse_utc(record["heartbeat_utc"])).total_seconds() <= PRESENCE_TTL_S
        except ValueError:
            online = False
    return record, online


def live_holder(agent):
    record, online = presence(agent)
    return record.get("session") if online else None


def turn_budget(agent, records):
    """Charges come from the state records themselves, so there is no second file to fall out of step."""
    stamps = []
    for record in records:
        for attempt in record.get("attempts", []):
            if attempt.get("charged"):
                try:
                    stamps.append(parse_utc(attempt.get("claimed_utc")))
                except (ValueError, TypeError):
                    stamps.append(now())  # an unreadable charge counts as fresh
    corrupt = safe_dir("state", "corrupt", create=False)
    for name in (os.listdir(corrupt) if corrupt else []):
        # A record that could not be read may hide charges: count the most it could hold.
        stamp = datetime.datetime.fromtimestamp(os.lstat(corrupt / name).st_mtime, datetime.timezone.utc)
        stamps.extend([stamp] * MAX_ATTEMPTS)
    day = [stamp for stamp in stamps if stamp > now() - datetime.timedelta(days=1)]
    hour = [stamp for stamp in day if stamp > now() - datetime.timedelta(hours=1)]
    retry = 0
    if len(hour) >= TURNS_PER_HOUR:
        oldest = sorted(hour)[len(hour) - TURNS_PER_HOUR]
        retry = int((oldest + datetime.timedelta(hours=1) - now()).total_seconds()) + 1
    if len(day) >= TURNS_PER_DAY:
        oldest = sorted(day)[len(day) - TURNS_PER_DAY]
        retry = max(retry, int((oldest + datetime.timedelta(days=1) - now()).total_seconds()) + 1)
    return {"used_last_hour": len(hour), "per_hour": TURNS_PER_HOUR,
            "used_last_day": len(day), "per_day": TURNS_PER_DAY, "retry_after_s": retry}


# ---- ownership -----------------------------------------------------------

def wakeable(agent):
    if agent not in WAKEABLE:
        raise Refused(f"not a session agent in {CONFIG_NAME}: {agent!r}")
    return agent


def session_of(args):
    if not SESSION_RE.match(args.session or ""):
        raise Refused("bad or missing session id")
    return args.session


def owned(agent, message_id, session=None):
    record = load_state(message_id)
    if record is None:
        raise Refused("no such message")
    if record.get("to") != agent:
        raise Refused(f"message is addressed to {record.get('to')!r}, not {agent!r}")
    if session is not None and record.get("session") != session:
        raise Refused("this message was delivered to a different session")
    return record


def let_go(record, outcome, reason, refund=False):
    """Give a claimed, not-started message back to the queue, or close it when it cannot be retried."""
    attempt = record["attempts"][-1] if record.get("attempts") else {}
    attempt["outcome"] = outcome
    if refund:
        attempt["charged"] = False
    if is_expired(record):
        save_state(record, "expired", "bridge", f"expired before a turn started ({reason})")
    elif len(record.get("attempts", [])) >= MAX_ATTEMPTS:
        save_state(record, "failed", "bridge", f"gave up after {MAX_ATTEMPTS} attempts: {reason}")
    else:
        save_state(record, "queued", "bridge", f"released: {reason}")
    checkpoint("release:state")
    settle(record)
    return record["state"]


def recover(agent, session, records, own=False, pending=None):
    """Release claims nobody can still be holding: those of sessions that are not live and, when the
    caller says what it still has pending (`own`), this session's other leftovers."""
    holder = live_holder(agent)
    released = []
    for record in records:
        if record["state"] != "claimed":
            continue
        mine = record.get("session") == session
        if (mine and own and record["id"] != pending) or (not mine and record.get("session") != holder):
            released.append({"id": record["id"], "state": let_go(record, "abandoned", "claim lost before a turn started")})
    return released


# ---- commands ------------------------------------------------------------

def publish(agent, envelope):
    """Put an envelope in agent's new/ unless a copy is already somewhere in the mailbox."""
    name = f"{envelope['id']}.json"
    data = (json.dumps(envelope, indent=2) + "\n").encode()
    if len(data) > MAX_BYTES:
        raise Refused(f"message is larger than {MAX_BYTES} bytes")
    if locate(agent, name):
        return False
    try:
        write_new(box(agent, "new"), name, data)
    except FileExistsError:
        return False
    return True


def cmd_init(args):
    """Create a new mailbox root with its config.json. Never touches a directory that already holds anything."""
    sessions, readers = args.session or [], args.reader or []
    names = sessions + readers
    if len(set(names)) != len(names):
        raise Refused("an agent is named twice")
    config = {"version": 1,
              "agents": {name: "session" for name in sessions} | {name: "reader" for name in readers},
              "limits": {name: default for name, (default, _, _) in LIMITS.items()}}
    check_config(config)
    if os.path.lexists(ROOT):
        if not ROOT.is_dir():
            raise Refused(f"not a directory: {ROOT}")
        if os.listdir(ROOT):
            raise Refused(f"not empty: {ROOT}; init only writes into a new or empty directory")
    else:
        os.makedirs(ROOT, 0o755)
    write_new(ROOT, CONFIG_NAME, (json.dumps(config, indent=2) + "\n").encode())
    load_config()
    for agent in AGENTS:
        for place in BOXES:
            box(agent, place)
    safe_dir("state")
    safe_dir("control")
    return {"ok": True, "root": str(ROOT), "config": str(ROOT / CONFIG_NAME), "agents": config["agents"],
            "limits": config["limits"]}


def cmd_check(args):
    """Read-only: is this agent a session the mod may serve under this root? The mod asks before it does anything."""
    return {"ok": True, "agent": wakeable(args.agent), "root": str(ROOT)}


def cmd_send(args):
    for name in (args.sender, args.to):
        if name not in AGENTS:
            raise Refused(f"unknown agent: {name!r}")
    body = args.body if args.body is not None else sys.stdin.read()
    ttl = max(1, min(DEFAULT_TTL_MIN if args.ttl_minutes is None else args.ttl_minutes, MAX_TTL_MIN))
    message_id = args.id or f"m-{now().strftime('%Y%m%dt%H%M%Sz')}-{uuid.uuid4().hex[:12]}"
    created = now()
    envelope = {"v": 1, "id": message_id, "from": args.sender, "to": args.to, "kind": args.kind,
                "correlation_id": args.correlation_id or message_id, "subject": args.subject,
                "created_utc": iso(created), "expires_utc": iso(created + datetime.timedelta(minutes=ttl)),
                "body": body}
    validate(envelope, args.to, f"{message_id}.json")
    with Lock():
        reconcile(args.to)
        name = f"{message_id}.json"
        if load_state(message_id) is None and locate(args.to, name) == ["new"]:
            # A send that died after publishing: finish it rather than refuse the retry.
            found = read_json(box(args.to, "new") / name) or {}
            if all(found.get(key) == envelope[key] for key in ("from", "to", "kind", "correlation_id")):
                save_state(record_for(found), "queued", args.sender, "retry completed an interrupted send")
                return {"ok": True, "id": message_id, "state": "queued", "repaired": True}
        if load_state(message_id) is not None or locate(args.to, name):
            raise Refused("a message with this id already exists")
        # Body first: a body without a record is adopted by the next sweep; a record without a body is not sendable.
        publish(args.to, envelope)
        checkpoint("send:body")
        save_state(record_for(envelope), "queued", args.sender)
        checkpoint("send:state")
    return {"ok": True, "id": message_id, "state": "queued", "recipient_online": live_holder(args.to) is not None,
            "note": "queued is not delivered; read `status` for submitted/acknowledged/completed"}


def cmd_claim(args):
    agent, session = wakeable(args.agent), session_of(args)
    with Lock():
        holder = live_holder(agent)
        if holder is not None and holder != session:
            return {"ok": True, "conflict": True, "message": None}
        records = reconcile(agent)
        recover(agent, session, records)
        queued = sweep(agent)
        records = [record for record in all_states() if record.get("to") == agent]
        result = {"ok": True, "paused": paused(agent), "pending": len(queued), "message": None,
                  "budget": turn_budget(agent, records)}
        # One delivery at a time: a second claim never hands out more while this session's first has not started.
        waiting = [r["id"] for r in records if r["state"] == "claimed" and r.get("session") == session]
        if waiting:
            result["in_flight"] = waiting[0]
            return result
        if result["paused"] or not queued:
            return result
        if result["budget"]["retry_after_s"] > 0:
            result["limited"] = True
            return result
        envelope = queued[0]
        record = load_state(envelope["id"])
        # The claim is the charge: one per delivery attempt, whatever happens after.
        record["attempts"].append({"n": len(record["attempts"]) + 1, "session": session,
                                   "claimed_utc": iso(now()), "charged": True, "outcome": None})
        record["session"] = session
        save_state(record, "claimed", agent)
        checkpoint("claim:state")
        settle(record)
        checkpoint("claim:move")
        result["message"] = envelope
        result["pending"] = len(queued) - 1
        return result


def cmd_begin(args):
    """The acceptance boundary, asked as the turn starts: may this delivery still go ahead?"""
    agent, session = wakeable(args.agent), session_of(args)
    with Lock():
        record = owned(agent, args.id)
        if record.get("session") == session and record["state"] in STARTED + ("completed", "blocked"):
            return {"ok": True, "start": True, "state": record["state"], "changed": False}
        if record["state"] != "claimed" or record.get("session") != session:
            return {"ok": True, "start": False, "revocable": False, "reason": "not this session's pending delivery"}
        if not args.force:
            holder = live_holder(agent)
            reason = ("paused" if paused(agent) else "expired" if is_expired(record)
                      else "another session holds this mailbox" if holder not in (None, session) else None)
            if reason:
                return {"ok": True, "start": False, "revocable": True, "reason": reason}
        record["attempts"][-1]["outcome"] = "started"
        record["turn_started"] = True
        note = "a turn started with this message"
        if args.force:
            # The turn is already running and could not be withdrawn: record it as it is, with what was skipped.
            skipped = [name for name, hit in (("paused", paused(agent)), ("expired", is_expired(record))) if hit]
            record["forced_start"] = {"at_utc": iso(now()), "skipped_checks": skipped,
                                      "reason": clip(args.note, MAX_NOTE_BYTES)}
            note = "turn started; it could not be withdrawn" + (f" (should have been withheld: {', '.join(skipped)})" if skipped else "")
        save_state(record, "submitted", agent, note)
        checkpoint("begin:state")
        return {"ok": True, "start": True, "state": "submitted", "changed": True}


def cmd_release(args):
    agent, session = wakeable(args.agent), session_of(args)
    with Lock():
        record = owned(agent, args.id, session)
        if record["state"] != "claimed":
            return {"ok": True, "state": record["state"], "changed": False}
        outcome = "not accepted" if args.refund else "withdrawn"
        return {"ok": True, "changed": True,
                "state": let_go(record, outcome, args.reason or "no turn started", refund=args.refund)}


def cmd_reply(args):
    agent, session = wakeable(args.agent), session_of(args)
    body = args.body if args.body is not None else sys.stdin.read()
    with Lock():
        record = owned(agent, args.id, session)
        order = {"claimed": 0, "submitted": 0, "acknowledged": 1, "completed": 2, "blocked": 2}
        if record["state"] not in order:
            raise Refused(f"message is {record['state']}; only a delivered message can be answered")
        if order[args.state] < order[record["state"]] or (
                order[record["state"]] == 2 and record["state"] != args.state):
            raise Refused(f"message is already {record['state']}")
        sender = record["from"]
        digest = hashlib.sha256((record["id"] + ":" + args.state).encode()).hexdigest()[:24]
        reply_id = "r-" + digest
        created = now()
        envelope = {"v": 1, "id": reply_id, "from": agent, "to": sender,
                    "kind": "ack" if args.state == "acknowledged" else "reply",
                    "correlation_id": record["correlation_id"], "in_reply_to": record["id"],
                    "reply_state": args.state,
                    "subject": clip(f"{args.state}: {record.get('subject') or record['id']}", MAX_SUBJECT_BYTES),
                    "created_utc": iso(created),
                    "expires_utc": iso(created + datetime.timedelta(minutes=MAX_TTL_MIN)), "body": body}
        # Each step is skipped when already done, so repeating the call finishes an interrupted one.
        reconcile(sender)
        published = False
        if load_state(reply_id) is None:
            published = publish(sender, envelope)
            checkpoint("reply:body")
            if locate(sender, f"{reply_id}.json"):
                save_state(record_for(envelope), "queued", agent)
            checkpoint("reply:rstate")
        changed = record["state"] != args.state
        if changed:
            if record["state"] == "claimed":
                # An answer proves the turn started, even if the mod's own mark never landed.
                record["attempts"][-1]["outcome"] = "started"
                record["turn_started"] = True
            if reply_id not in record.setdefault("replies", []):
                record["replies"].append(reply_id)
            save_state(record, args.state, agent)
            checkpoint("reply:ostate")
        settle(record)
        checkpoint("reply:move")
        return {"ok": True, "state": args.state, "reply_id": reply_id, "changed": changed or published}


def cmd_heartbeat(args):
    agent, session = wakeable(args.agent), session_of(args)
    with Lock():
        holder = live_holder(agent)
        if holder is not None and holder != session:
            return {"ok": True, "conflict": True}
        write_replace(safe_dir("presence"), f"{agent}.json",
                      {"agent": agent, "session": session, "online": True, "busy": bool(args.busy),
                       "heartbeat_utc": iso(now())})
        released = recover(agent, session, reconcile(agent), own=True, pending=args.pending)
    return {"ok": True, "conflict": False, "released": released}


def cmd_offline(args):
    """This session is going away: drop its presence and give back anything it claimed but never started."""
    agent, session = wakeable(args.agent), session_of(args)
    with Lock():
        holder, _ = presence(agent)
        if holder.get("session") == session:
            holder.update({"online": False, "busy": False, "offline_utc": iso(now())})
            write_replace(safe_dir("presence"), f"{agent}.json", holder)
        released = [{"id": record["id"], "state": let_go(record, "abandoned", "session ended before a turn started")}
                    for record in reconcile(agent)
                    if record["state"] == "claimed" and record.get("session") == session]
    return {"ok": True, "released": released}


def cmd_pause(args):
    name = f"pause-{args.agent}" if args.agent else "pause-all"
    if args.agent and args.agent not in AGENTS:
        raise Refused("unknown agent")
    with Lock():
        control = safe_dir("control")
        if args.command == "pause":
            try:
                write_new(control, name, (iso(now()) + "\n").encode())
            except FileExistsError:
                pass
        else:
            try:
                os.unlink(control / name)
            except FileNotFoundError:
                pass
    return {"ok": True, "paused_all": os.path.lexists(safe_dir("control") / "pause-all"),
            "paused": {agent: paused(agent) for agent in WAKEABLE},
            "note": "pause stops new claims and withdraws a delivery that has not started its turn; "
                    "it does not stop a turn that is already running"}


def cmd_resolve(args):
    """A person closes a message nobody can finish (for example its session is gone)."""
    with Lock():
        record = load_state(args.id)
        if record is None:
            raise Refused("no such message")
        if record["state"] in TERMINAL:
            return {"ok": True, "state": record["state"], "changed": False}
        record.pop("needs_reconciliation", None)
        save_state(record, args.state, "operator", args.note)
        settle(record)
        return {"ok": True, "state": args.state, "changed": True}


def cmd_fsck(args):
    if args.agent is not None and args.agent not in AGENTS:
        raise Refused(f"unknown agent: {args.agent!r}")
    with Lock():
        report = {}
        for agent in ([args.agent] if args.agent else AGENTS):
            records = reconcile(agent)
            report[agent] = [{"id": r["id"], "state": r["state"], "problem": r["needs_reconciliation"]}
                             for r in records if r.get("needs_reconciliation")]
    return {"ok": True, "needs_reconciliation": report}


def cmd_status(args):
    """Read-only: creates nothing, moves nothing, takes no lock."""
    agents = [args.agent] if args.agent else list(AGENTS)
    state_dir = safe_dir("state", create=False)
    records = []
    for name in names_in(state_dir):
        record = read_json(state_dir / name)
        if record:
            records.append(normalized(record))
    corrupt = safe_dir("state", "corrupt", create=False)
    report = {"checked_utc": iso(now()), "agents": {}, "messages": [],
              "unreadable_state_records": len(os.listdir(corrupt)) if corrupt else 0}
    for agent in agents:
        if agent not in AGENTS:
            raise Refused("unknown agent")
        mine = [record for record in records if record.get("to") == agent]
        rejected = safe_dir("mail", agent, "rejected", create=False)
        entry = {"queued": len(names_in(safe_dir("mail", agent, "new", create=False))),
                 "in_flight": len(names_in(safe_dir("mail", agent, "claimed", create=False))),
                 "rejected": len([n for n in (os.listdir(rejected) if rejected else []) if n.endswith(".reason.json")]),
                 "needs_reconciliation": [record["id"] for record in mine if record.get("needs_reconciliation")]}
        if agent in WAKEABLE:
            holder, online = presence(agent)
            entry.update({"presence": "online" if online else "offline", "busy": bool(online and holder.get("busy")),
                          "last_heartbeat_utc": holder.get("heartbeat_utc"), "paused": paused(agent),
                          "automated_turns": turn_budget(agent, mine)})
        else:
            entry["presence"] = "no wake mechanism; reads files when its own task runs"
        report["agents"][agent] = entry
    shown = [r for r in records if not args.agent or args.agent in (r.get("to"), r.get("from"))]
    shown.sort(key=lambda item: item.get("created_utc") or "", reverse=True)
    for record in shown[:args.limit]:
        report["messages"].append({key: record.get(key) for key in
                                   ("id", "from", "to", "kind", "state", "correlation_id", "subject", "created_utc",
                                    "expires_utc", "turn_started", "replies", "needs_reconciliation", "forced_start", "stray_starts")}
                                  | {"attempts": len(record.get("attempts", []))})
    return report


def cmd_inbox(args):
    """For a reader agent (an external bot with no wake mechanism): list, and optionally archive, its mail."""
    if args.agent not in AGENTS:
        raise Refused(f"unknown agent: {args.agent!r}")
    if args.agent in WAKEABLE:
        raise Refused("a session agent's mail is delivered by the mod, not read with `inbox`")
    with Lock():
        reconcile(args.agent)
        messages = sweep(args.agent)
        if args.archive:
            for envelope in messages:
                record = load_state(envelope["id"])
                save_state(record, "recorded", args.agent, "read and archived")
                settle(record)
    return {"ok": True, "messages": messages, "archived": bool(args.archive)}


def main(argv=None):
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", help=f"the mailbox root directory (default: ${ROOT_ENV})")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, run, *options):
        command = sub.add_parser(name)
        for flags, extra in options:
            command.add_argument(*flags, **extra)
        command.set_defaults(run=run)

    agent = (("--agent",), {"required": True})
    session = (("--session",), {"required": True})
    ident = (("--id",), {"required": True})
    add("init", cmd_init,
        (("--session",), {"action": "append", "metavar": "NAME", "help": "a Claude Code session agent (repeatable)"}),
        (("--reader",), {"action": "append", "metavar": "NAME", "help": "an external agent that reads files (repeatable)"}))
    add("check", cmd_check, agent)
    add("send", cmd_send,
        (("--from",), {"dest": "sender", "required": True}),
        (("--to",), {"required": True}),
        (("--kind",), {"default": "instruction", "choices": KINDS}),
        (("--subject",), {"default": ""}), (("--body",), {}),
        (("--ttl-minutes",), {"type": int}),
        (("--correlation-id",), {}), (("--id",), {}))
    add("claim", cmd_claim, agent, session)
    add("begin", cmd_begin, agent, session, ident, (("--force",), {"action": "store_true"}), (("--note",), {}))
    add("stray", cmd_stray, agent, session, ident, (("--aborted",), {"type": int, "default": 0}), (("--note",), {}))
    add("release", cmd_release, agent, session, ident, (("--reason",), {}), (("--refund",), {"action": "store_true"}))
    add("reply", cmd_reply, agent, session, ident,
        (("--state",), {"required": True, "choices": ("acknowledged", "completed", "blocked")}), (("--body",), {}))
    add("heartbeat", cmd_heartbeat, agent, session, (("--busy",), {"type": int, "default": 0}), (("--pending",), {}))
    add("offline", cmd_offline, agent, session)
    add("pause", cmd_pause, (("--agent",), {}))
    add("resume", cmd_pause, (("--agent",), {}))
    add("resolve", cmd_resolve, ident, (("--state",), {"required": True, "choices": ("blocked", "failed")}),
        (("--note",), {"required": True}))
    add("fsck", cmd_fsck, (("--agent",), {}))
    add("status", cmd_status, (("--agent",), {}), (("--limit",), {"type": int, "default": 20}))
    add("inbox", cmd_inbox, agent, (("--archive",), {"action": "store_true"}))

    args = parser.parse_args(argv)
    try:
        where = args.root or os.environ.get(ROOT_ENV)
        if not where:
            raise NotConfigured(f"no mailbox root: pass --root or set {ROOT_ENV}")
        ROOT = Path(where).expanduser().resolve()
        if args.command != "init":
            load_config()
        result = args.run(args)
    except NotConfigured as error:
        # Fail closed: without a root and a valid config nothing is created and nothing is delivered.
        print(json.dumps({"ok": False, "error": str(error)}))
        return 2
    except Refused as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    except OSError as error:
        # Fail closed: nothing is reported as done, and the next command reconciles.
        print(json.dumps({"ok": False, "error": f"{errno.errorcode.get(error.errno, 'OSError')}: {error.strerror}"}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
