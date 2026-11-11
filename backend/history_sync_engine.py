#!/usr/bin/env python3
"""Append-only, offline Claude Desktop Code index sharing; Python standard library.

Does not log in, manage credentials, write transcripts, or run a background service.
Plans contain private history copies. A checksum detects accidental changes, not
malicious edits by somebody with access to the same macOS account.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid

# Account metadata is local to the current user and is never bundled in the app.
HISTORY_ROOT = Path.home() / "Library/Application Support/Claude-Desktop-Companion/history"
VALIDATED_DESKTOP_VERSIONS = ("5.1231.2",)
ALIAS = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")

UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z")
SESSION = re.compile(r"local_[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\.json\Z")
KEEP = {
    "sessionId", "cliSessionId", "priorCliSessionIds", "unarchivedCliSessionId",
    "preClearCliSessionId", "cwd", "originCwd", "createdAt", "lastActivityAt",
    "lastFocusedAt", "title", "titleSource", "previousTitles", "isArchived",
    "model", "effort", "completedTurns", "lastAssistantUuid", "lastAssistantCliSessionId", "rewindEdges",
    "transcriptModelStates", "transcriptCuts", "forkedFromSessionId", "forkedAtMessageUuid",
    "priorCliSessionLeftAt",
}
# Desktop 5.1231.2 can reference history outside the currently supported reader.
# Empty fields carry no history; nonempty or malformed values need schema review.
UNSUPPORTED_HISTORY_FIELDS = (
    "historyOnlyCliSessionId", "overflowCliSessionIds", "priorRemoteCliSessionIds",
    "overflowRemoteCliSessionIds", "givenUpCliSessionIds",
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def safe(path, kind=None):
    """Reject symlinks in every existing path component, including ancestors."""
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "An absolute, canonical path is required")
    for item in reversed([path, *path.parents]):
        if item.exists() or item.is_symlink():
            require(not item.is_symlink(), "Symlink paths are not supported")
    if kind == "file":
        require(path.is_file() and stat.S_ISREG(path.stat().st_mode), "Expected a regular file")
    elif kind == "dir":
        require(path.is_dir(), "Expected an existing directory")
    return path


def fingerprint(path):
    s = safe(path, "file").stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def digest(path):
    before = fingerprint(path)
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    require(fingerprint(path) == before, "A file changed while being read")
    return h.hexdigest()


def read_json(path):
    safe(path, "file")
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def private_json(path, value, replace=False):
    safe(path.parent, "dir")
    data = json.dumps(value, ensure_ascii=False, indent=2).encode() + b"\n"
    if not replace:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
    else:
        fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            safe(path)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def private_dirs(path):
    safe(path)
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor); cursor = cursor.parent
    safe(cursor, "dir")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)


def validate_jsonl(path):
    with path.open("rb") as stream:
        for number, line in enumerate(stream, 1):
            require(line.endswith(b"\n"), f"Truncated JSONL: line {number} has no newline")
            if line.strip():
                require(isinstance(json.loads(line), dict), "Unexpected JSONL record type")


def transcript_ids(record):
    for key in UNSUPPORTED_HISTORY_FIELDS:
        value = record.get(key)
        require(value is None or isinstance(value, (str, list, dict)) and not value,
                f"Unsupported history field {key}; review its schema before copying")
    ids = set()
    for key in ("cliSessionId", "unarchivedCliSessionId", "preClearCliSessionId", "lastAssistantCliSessionId"):
        value = record.get(key)
        if key == "lastAssistantCliSessionId" and value == "":
            continue  # Desktop uses the empty string when the transcript owner is unknown.
        if value is not None:
            require(isinstance(value, str) and UUID.fullmatch(value), "Invalid transcript identifier")
            ids.add(value)
    prior = record.get("priorCliSessionIds", [])
    require(isinstance(prior, list), "Invalid prior transcript list")
    for value in prior:
        require(isinstance(value, str) and UUID.fullmatch(value), "Invalid prior transcript identifier")
        ids.add(value)
    # Desktop 5.1231.2 index.chunk-BxBKaMlO.js filters these exit timestamps
    # against local prior/overflow IDs and uses them for deletion decisions.
    # Overflow remains unsupported. Preserve validated timestamps exactly.
    left_at = record.get("priorCliSessionLeftAt")
    if left_at is not None:
        require(isinstance(left_at, dict), "Invalid priorCliSessionLeftAt map")
        for sid, value in left_at.items():
            require(isinstance(sid, str) and UUID.fullmatch(sid) and sid in prior,
                    "priorCliSessionLeftAt references an unsupported prior transcript")
            require(type(value) in (int, float) and 0 < value <= sys.float_info.max and math.isfinite(value),
                    "Invalid priorCliSessionLeftAt timestamp")
    edges = record.get("rewindEdges", [])
    require(isinstance(edges, list), "Invalid rewind edge list")
    for edge in edges:
        require(isinstance(edge, dict), "Invalid rewind edge")
        for key in ("parent", "child"):
            value = edge.get(key)
            require(isinstance(value, str) and UUID.fullmatch(value), "Invalid rewind transcript identifier")
            ids.add(value)
    require(record.get("cliSessionId") in ids, "Current transcript identifier is missing")
    return ids


def sanitized(record, indexed_at):
    result = {key: value for key, value in record.items() if key in KEEP}
    # Keep Desktop's legacy remote-owner fallback before dropping SSH reattach metadata.
    reattach = record.get("sshReattach")
    if (record.get("lastAssistantCliSessionId") is None and isinstance(record.get("lastAssistantUuid"), str)
            and isinstance(reattach, dict) and reattach.get("namedCliSessionId") == record["cliSessionId"]):
        result["lastAssistantCliSessionId"] = ""
    result.update(permissionMode="default", sessionPermissionUpdates=[], alwaysAllowedReasons=[],
                  importedFrom="local-1p-code", resumeConfirmed=False, indexedAt=indexed_at)
    return result


def copy_name(env, source, target, record):
    accounts = env.settings()["accounts"]
    identity = ":".join(("claude-history-copy", accounts[source]["account"],
                         accounts[target]["account"], record["sessionId"], record["cliSessionId"]))
    return "local_" + str(uuid.uuid5(uuid.NAMESPACE_URL, identity)) + ".json"


def copied_record(record, name, source, indexed_at):
    result = sanitized(record, indexed_at)
    title = record.get("title", "未命名会话")
    require(isinstance(title, str), "Invalid source session title")
    result.update(sessionId=Path(name).stem, title=title + f"（{source} 后续）")
    result.pop("forkedFromSessionId", None)
    result.pop("forkedAtMessageUuid", None)
    return result


class Environment:
    def __init__(self, desktop=None, projects=None, app=None, accounts=None):
        home = Path.home()
        self.desktop = desktop or home / "Library/Application Support/Claude"
        self.projects = projects or home / ".claude/projects"
        self.app = app or Path("/Applications/Claude.app")
        self.accounts_path = accounts or HISTORY_ROOT / "accounts.json"

    def version(self):
        info = self.app / "Contents/Info.plist"
        require(info.is_file(), "请将官方 Claude Desktop 安装在 /Applications/Claude.app 后重试。")
        with safe(info, "file").open("rb") as stream:
            return plistlib.load(stream).get("CFBundleShortVersionString")

    def running(self):
        result = subprocess.run(["/usr/bin/pgrep", "-x", "Claude"], capture_output=True)
        require(result.returncode in (0, 1), "Could not determine whether Claude is running")
        return result.returncode == 0

    def settings(self):
        safe(self.accounts_path)
        data = (read_json(self.accounts_path) if self.accounts_path.exists() else
                {"validated_desktop_versions": list(VALIDATED_DESKTOP_VERSIONS), "accounts": {}})
        require(isinstance(data, dict), "Invalid account registry")
        require(isinstance(data.get("validated_desktop_versions"), list) and
                all(isinstance(value, str) for value in data["validated_desktop_versions"]),
                "Missing validated Desktop versions")
        accounts = data.get("accounts")
        require(isinstance(accounts, dict), "Expected an account mapping")
        require(all(isinstance(alias, str) and ALIAS.fullmatch(alias)
                    for alias in accounts), "Invalid configured account alias")
        for account in accounts.values():
            require(isinstance(account, dict) and set(account) == {"account", "org"}, "Unexpected account metadata")
            require(all(isinstance(v, str) and UUID.fullmatch(v) for v in account.values()), "Invalid account metadata")
        require(len({a["account"] for a in accounts.values()}) == len(accounts), "Account identifiers must differ")
        return data

    def state(self, stopped=False):
        config = self.settings()
        running = self.running()
        if stopped:
            require(not running, "Completely quit Claude before planning or applying")
        version = self.version()
        require(version in config["validated_desktop_versions"], "Desktop version has not been validated; review its schema first")
        # Credential-bearing config is only read to extract this one field, never copied or printed.
        active = read_json(self.desktop / "config.json").get("lastKnownAccountUuid")
        matches = [alias for alias, data in config["accounts"].items() if data["account"] == active]
        require(len(matches) == 1, "Current account is not registered; run history_service.py register after opening a local Code session")
        return {"version": version, "active_alias": matches[0], "running": running,
                "accounts_sha256": digest(self.accounts_path)}

    def folder(self, alias):
        record = self.settings()["accounts"].get(alias)
        require(record is not None, "Unknown account alias")
        return safe(self.desktop / "claude-code-sessions" / record["account"] / record["org"], "dir")


def directory_state(directory):
    """Guard all entries without copying unrelated files or inspecting their contents."""
    result = {}
    for path in sorted(safe(directory, "dir").iterdir()):
        safe(path)
        if path.is_file():
            result[path.name] = {"kind": "file", "sha256": digest(path)}
        elif path.is_dir():
            result[path.name] = {"kind": "dir"}
        else:
            raise RuntimeError("Unsupported account directory entry")
    return result


def records_and_markers(folder):
    records, markers = {}, set()
    for path in sorted(folder.iterdir()):
        safe(path)
        if path.name.startswith("deleted_"):
            require(path.is_file(), "Invalid deletion marker")
            marker = path.name[len("deleted_"):]
            markers.add(marker.removesuffix(".json"))
        elif path.name.startswith("local_"):
            require(SESSION.fullmatch(path.name), "Unrecognized local session filename")
            record = read_json(path)
            require(isinstance(record, dict) and record.get("sessionId") == path.stem, "Session ID/filename mismatch")
            transcript_ids(record)
            require(isinstance(record.get("isArchived"), bool), "Unknown archive state")
            require(isinstance(record.get("cwd"), str) and Path(record["cwd"]).is_absolute(), "Invalid session project path")
            records[path.name] = record
    return records, markers


def archived_hints(folder):
    path = folder / "archived-sessions.idx"
    safe(path)
    if not path.exists():
        return set()
    data = read_json(path)
    require(isinstance(data, dict) and data.get("v") == 1 and isinstance(data.get("archived"), list),
            "Unrecognized archive hint format")
    require(all(isinstance(value, str) and re.fullmatch(r"local_[A-Za-z0-9_-]+", value)
                for value in data["archived"]), "Invalid archived session identifier")
    return set(data["archived"])


def select_records(env, source, target, sessions=None):
    sources, left_markers = records_and_markers(env.folder(source))
    targets, right_markers = records_and_markers(env.folder(target))
    archived = archived_hints(env.folder(source)) | archived_hints(env.folder(target))
    if sessions:
        require(len(sessions) == len(set(sessions)), "Duplicate session selection")
        require(set(sessions) <= sources.keys(), "A requested session does not exist in the source")
    selected, skipped = {}, {"existing": [], "archived": [], "deleted": []}
    markers = left_markers | right_markers
    for name, record in sources.items():
        if sessions and name not in sessions:
            continue
        related = transcript_ids(record) | {record["sessionId"], record["sessionId"].removeprefix("local_")}
        if name in targets:
            skipped["existing"].append(name)
        elif related & markers:
            skipped["deleted"].append(name)
        elif record["isArchived"] or record["sessionId"] in archived:
            skipped["archived"].append(name)
        else:
            require(Path(record["cwd"]).is_dir(), "A selected session's project folder is unavailable")
            selected[name] = record
    return selected, skipped


def select_copies(env, source, target, sessions):
    require(sessions, "Copying existing sessions requires explicit --session selections")
    require(len(sessions) == len(set(sessions)), "Duplicate session selection")
    sources, left_markers = records_and_markers(env.folder(source))
    targets, right_markers = records_and_markers(env.folder(target))
    require(set(sessions) <= sources.keys() and set(sessions) <= targets.keys(),
            "Copy mode requires every original session in both accounts")
    archived = archived_hints(env.folder(source)) | archived_hints(env.folder(target))
    markers = left_markers | right_markers
    selected, skipped = {}, {"existing": [], "archived": [], "deleted": []}
    for name in sorted(sessions):
        record, existing = sources[name], targets[name]
        destination = copy_name(env, source, target, record)
        destination_id = Path(destination).stem
        related = transcript_ids(record) | transcript_ids(existing) | {
            record["sessionId"], record["sessionId"].removeprefix("local_"),
            destination_id, destination_id.removeprefix("local_")}
        if related & markers:
            skipped["deleted"].append(name)
        elif record["isArchived"] or existing["isArchived"] or record["sessionId"] in archived or destination_id in archived:
            skipped["archived"].append(name)
        elif destination in targets:
            skipped["existing"].append(name)
        else:
            require(Path(record["cwd"]).is_dir(), "A selected session's project folder is unavailable")
            selected[name] = record
    return selected, skipped


def collect_files(env, source, records):
    files = {Path("source-index") / name: env.folder(source) / name for name in records}
    directories = set()
    ids = set().union(*(transcript_ids(record) for record in records.values())) if records else set()
    found = set()
    if not ids:
        return files, directories, ids
    for project in sorted(safe(env.projects, "dir").iterdir()):
        safe(project)
        if not project.is_dir():
            continue
        for sid in ids:
            transcript = project / (sid + ".jsonl")
            if not transcript.exists():
                continue
            safe(transcript, "file")
            found.add(sid)
            files[Path("projects") / transcript.relative_to(env.projects)] = transcript
            companion = project / sid
            if companion.exists() or companion.is_symlink():
                safe(companion, "dir")
                directories.add(Path("projects") / companion.relative_to(env.projects))
                for base, subdirs, names in os.walk(companion, followlinks=False):
                    for name in subdirs:
                        path = safe(Path(base) / name, "dir")
                        directories.add(Path("projects") / path.relative_to(env.projects))
                    for name in names:
                        path = safe(Path(base) / name, "file")
                        files[Path("projects") / path.relative_to(env.projects)] = path
    require(found == ids, f"{len(ids - found)} referenced transcripts are unavailable")
    return files, directories, ids


def backup_files(output, files, directories):
    before = {relative: fingerprint(path) for relative, path in files.items()}
    result = []
    for directory in sorted(directories):
        private_dirs(output / "snapshot" / directory)
    for relative, path in sorted(files.items()):
        require(fingerprint(path) == before[relative], "Source changed before copying")
        destination = output / "snapshot" / relative
        private_dirs(destination.parent)
        with path.open("rb") as source, destination.open("xb") as target:
            os.chmod(destination, 0o600)
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target.write(chunk)
            target.flush(); os.fsync(target.fileno())
        expected = digest(path)
        require(digest(destination) == expected and fingerprint(path) == before[relative], "Source changed during copying")
        if destination.suffix == ".jsonl":
            validate_jsonl(destination)
        result.append({"path": relative.as_posix(), "sha256": expected,
                       "fingerprint": before[relative], "bytes": before[relative][2]})
    return result


def verify_files(env, plan, output, records):
    files, directories, ids = collect_files(env, plan["source"], records)
    require({str(p) for p in directories} == set(plan["snapshot_directories"]), "Transcript directories changed")
    entries = plan["snapshot_files"]
    require(set(files) == {Path(e["path"]) for e in entries} and len(files) == len(entries), "Transcript inventory changed")
    for entry in entries:
        relative = Path(entry["path"])
        require(not relative.is_absolute() and ".." not in relative.parts, "Invalid snapshot path")
        source = files[relative]
        require(fingerprint(source) == entry["fingerprint"] and digest(source) == entry["sha256"], "Source history changed; make a new plan")
        copied = safe(output / "snapshot" / relative, "file")
        require(digest(copied) == entry["sha256"], "Snapshot checksum mismatch")
        if copied.suffix == ".jsonl":
            validate_jsonl(copied)
    return len(ids)


def status(env):
    result = env.state()
    result.update(accounts={}, status="ok")
    aliases = list(env.settings()["accounts"])
    read_errors = (RuntimeError, OSError, ValueError, KeyError, TypeError)
    for source in aliases:
        destinations = {}
        summary = {"local_sessions": None, "destinations": destinations}
        try:
            records, _ = records_and_markers(env.folder(source))
            summary["local_sessions"] = len(records)
        except read_errors as exc:
            summary["error"] = str(exc)
            result["status"] = "partial"
        for target in aliases:
            if target == source:
                continue
            try:
                selected, skipped = select_records(env, source, target)
                destinations[target] = {"missing": len(selected),
                                        "skipped": {key: len(value) for key, value in skipped.items()}}
            except read_errors as exc:
                destinations[target] = {"missing": None, "skipped": None, "error": str(exc)}
                result["status"] = "partial"
        if len(aliases) == 2:
            # Preserve the original two-account summary where "other" is unambiguous.
            other = next(iter(destinations.values()))
            summary.update(missing_in_other=other["missing"], skipped=other["skipped"])
        result["accounts"][source] = summary
    return result


def plan_history(env, source, target, output, sessions=None, copy_existing=False):
    require(source != target, "Source and target must differ")
    output = safe(output)
    require(not output.exists() and not output.is_symlink(), "Plan output must be a new directory")
    safe(output.parent, "dir")
    for protected in (env.desktop, env.projects):
        require(output != protected and protected not in output.parents, "Plan cannot be stored inside Claude data")
    initial = env.state(stopped=True)
    left, right = env.folder(source), env.folder(target)
    left_before, right_before = directory_state(left), directory_state(right)
    records, skipped = (select_copies(env, source, target, sessions) if copy_existing
                        else select_records(env, source, target, sessions))
    # An empty plan makes no copies and creates no directory.
    if not records:
        return {"status": "no_changes", "prepared": 0, "skipped": skipped, "plan_created": False}
    files, directories, ids = collect_files(env, source, records)
    output.mkdir(mode=0o700)
    snapshot = backup_files(output, files, directories)
    indexed_at = int(time.time() * 1000)
    private_dirs(output / "indexes")
    entries = []
    for name, record in records.items():
        destination = copy_name(env, source, target, record) if copy_existing else name
        path = output / "indexes" / destination
        private_json(path, copied_record(record, destination, source, indexed_at) if copy_existing else sanitized(record, indexed_at))
        entry = {"name": destination, "sha256": digest(path), "removed_fields": sorted(set(record) - KEEP)}
        if copy_existing:
            entry.update(source_name=name, operation="copy")
            entry["removed_fields"] = sorted(set(entry["removed_fields"]) |
                                              (set(record) & {"forkedFromSessionId", "forkedAtMessageUuid"}))
        entries.append(entry)
    plan = {"format": 2 if copy_existing else 1, "source": source, "target": target, "state": initial,
            "desktop": str(env.desktop), "projects": str(env.projects), "indexed_at": indexed_at,
            "source_directory": left_before, "target_directory": right_before,
            "snapshot_files": snapshot, "snapshot_directories": sorted(str(p) for p in directories),
            "entries": entries, "skipped": skipped}
    if copy_existing:
        plan["copy_existing"] = True
    require(env.state(stopped=True) == initial, "Desktop state changed during preparation")
    require(directory_state(left) == left_before and directory_state(right) == right_before, "Account indexes changed during preparation")
    verify_files(env, plan, output, records)
    private_json(output / "plan.json", plan)
    with (output / "plan.sha256").open("x", encoding="ascii") as stream:
        os.chmod(output / "plan.sha256", 0o600)
        stream.write(digest(output / "plan.json") + "\n")
    return {"status": "prepared", "prepared": len(entries), "transcripts": len(ids),
            "snapshot_bytes": sum(e["bytes"] for e in snapshot), "output": str(output), "skipped": skipped}


def apply_history(env, output):
    output = safe(output, "dir")
    require(digest(output / "plan.json") == safe(output / "plan.sha256", "file").read_text().strip(), "Plan checksum mismatch")
    plan = read_json(output / "plan.json")
    require(plan.get("format") in (1, 2) and plan.get("source") != plan.get("target"), "Unsupported plan")
    copy_existing = plan["format"] == 2
    require(not copy_existing or plan.get("copy_existing") is True, "Unsupported copy plan")
    require(plan.get("desktop") == str(env.desktop) and plan.get("projects") == str(env.projects), "Plan is for a different machine or data root")
    require(env.state(stopped=True) == plan["state"], "Desktop version, account or mapping changed; make a new plan")
    left, right = env.folder(plan["source"]), env.folder(plan["target"])
    require(directory_state(left) == plan["source_directory"], "Source indexes changed")
    require(directory_state(right) == plan["target_directory"], "Target indexes changed; no overwrite is allowed")
    names = [entry["name"] for entry in plan["entries"]]
    require(names and len(names) == len(set(names)) and all(SESSION.fullmatch(n) for n in names), "Invalid plan selection")
    source_names = [entry.get("source_name", entry["name"]) for entry in plan["entries"]]
    require(len(source_names) == len(set(source_names)) and all(SESSION.fullmatch(n) for n in source_names), "Invalid source selection")
    require(all(entry.get("operation") == "copy" and "source_name" in entry for entry in plan["entries"]) if copy_existing
            else all("source_name" not in entry and "operation" not in entry for entry in plan["entries"]), "Invalid plan operation")
    records, _ = (select_copies(env, plan["source"], plan["target"], source_names) if copy_existing
                  else select_records(env, plan["source"], plan["target"], source_names))
    require(set(records) == set(source_names), "Selected sessions are no longer eligible")
    verify_files(env, plan, output, records)
    for entry in plan["entries"]:
        record = records[entry.get("source_name", entry["name"])]
        if copy_existing:
            require(entry["name"] == copy_name(env, plan["source"], plan["target"], record), "Copy destination identity differs")
        path = safe(output / "indexes" / entry["name"], "file")
        require(digest(path) == entry["sha256"], "Prepared index checksum mismatch")
        expected = (copied_record(record, entry["name"], plan["source"], plan["indexed_at"]) if copy_existing
                    else sanitized(record, plan["indexed_at"]))
        require(read_json(path) == expected, "Prepared index differs from the allowed schema")
    receipt = {"status": "in_progress", "added": [], "started_at_ms": int(time.time() * 1000),
               "source": plan["source"], "target": plan["target"], "writes_transcripts": False}
    receipt_path = output / ("receipt-" + str(time.time_ns()) + ".json")
    private_json(receipt_path, receipt)
    expected_target = dict(plan["target_directory"])
    try:
        for entry in plan["entries"]:
            require(env.state(stopped=True) == plan["state"], "Claude state changed during apply")
            require(directory_state(left) == plan["source_directory"] and directory_state(right) == expected_target, "Account indexes changed during apply")
            prepared = safe(output / "indexes" / entry["name"], "file")
            require(digest(prepared) == entry["sha256"], "Prepared file changed during apply")
            target = right / entry["name"]
            safe(target)
            fd, name = tempfile.mkstemp(prefix=".history-share-", dir=right)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(prepared.read_bytes()); stream.flush(); os.fsync(stream.fileno())
                # Atomic no-clobber creation. The temporary hardlink is immediately
                # removed: final account entries are separate real files, never shared links.
                os.link(temporary, target, follow_symlinks=False)
                receipt["added"].append(entry["name"])
            finally:
                temporary.unlink(missing_ok=True)
                private_json(receipt_path, receipt, replace=True)
            require(digest(target) == entry["sha256"] and target.stat().st_nlink == 1, "Written index verification failed")
            expected_target[entry["name"]] = {"kind": "file", "sha256": entry["sha256"]}
        require(env.state(stopped=True) == plan["state"], "Claude state changed after apply")
        require(directory_state(left) == plan["source_directory"] and directory_state(right) == expected_target, "Index changes detected after apply")
        verify_files(env, plan, output, records)
        receipt["status"] = "applied"
    except Exception as exc:
        receipt["status"] = "partial" if receipt["added"] else "failed"
        receipt["error"] = str(exc)
        raise RuntimeError(f"Apply stopped; {len(receipt['added'])} added. Inspect {receipt_path}") from exc
    finally:
        receipt["finished_at_ms"] = int(time.time() * 1000)
        private_json(receipt_path, receipt, replace=True)
    return {**receipt, "receipt": str(receipt_path)}


if __name__ == "__main__":
    print("Internal history reader. Use history_service.py for checkpoint-based backup and import.", file=sys.stderr)
    sys.exit(2)
