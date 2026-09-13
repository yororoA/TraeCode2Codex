import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .common import MigrationError, fingerprint, load_json, parse_json, timestamp

SQLITE_HEADER = b"SQLite format 3\x00"


@dataclass
class Session:
    source_id: str
    title: str
    cwd: str
    created_at: str
    entries: list
    raw: dict
    warnings: list = field(default_factory=list)


def discover():
    home = Path.home()
    if sys.platform == "darwin":
        roots = [home / "Library/Application Support"]
    elif os.name == "nt":
        roots = [Path(os.environ.get("APPDATA", home / "AppData/Roaming"))]
    else:
        roots = [Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))]
    names = ("Trae CN", "Trae", "TRAE", "TRAE CN", "TRAE SOLO CN", "TRAE SOLO")
    found = []
    for root in roots:
        for name in names:
            db = root / name / "ModularData/ai-agent/database.db"
            if db.is_file() and not any(db.samefile(existing) for existing in found):
                found.append(str(db))
    return found


def probe(path):
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        path = path / "ModularData/ai-agent/database.db"
    if not path.is_file():
        raise MigrationError("Source not found: " + str(path))
    with path.open("rb") as stream:
        header = stream.read(32)
    kind = "sqlite" if header.startswith(SQLITE_HEADER) else "encrypted-or-unknown"
    if path.suffix.lower() == ".json":
        kind = "json"
    return {
        "path": str(path),
        "kind": kind,
        "bytes": path.stat().st_size,
        "wal_present": Path(str(path) + "-wal").exists(),
    }


@contextmanager
def connect_readonly(path, key_env=None):
    info = probe(path)
    module = sqlite3
    key = None
    if info["kind"] == "encrypted-or-unknown":
        if not key_env or not os.environ.get(key_env):
            raise MigrationError(
                "Encrypted or unknown TRAE database. Supply an authorized plaintext export, "
                "or install a SQLCipher Python binding and use --key-env VARIABLE. "
                "No key extraction or process-memory scanning is performed."
            )
        key = os.environ[key_env]
        # Keep keys out of command lines, logs, exports, and generated reports.
        if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
            raise MigrationError(
                "--key-env must contain a raw 32-byte SQLCipher key (64 hex characters)."
            )
        try:
            import sqlcipher3 as module
        except ImportError:
            raise MigrationError(
                "SQLCipher binding unavailable. Install sqlcipher3 for your platform."
            ) from None
    connection = None
    try:
        # Do not use immutable=1: it would silently ignore recent data in the WAL.
        connection = module.connect(Path(info["path"]).as_uri() + "?mode=ro", uri=True, timeout=5)
        if key is not None:
            if not connection.execute("PRAGMA cipher_version").fetchone():
                raise MigrationError("Installed database binding has no SQLCipher support.")
            connection.execute(f"PRAGMA key = \"x'{key}'\"")
            connection.execute("PRAGMA cipher_compatibility = 4")
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        yield connection
    except module.DatabaseError:
        raise MigrationError(
            "Cannot read a consistent TRAE database snapshot. Check the format/key and WAL files; "
            "close TRAE and copy the database with its sidecars before retrying."
        ) from None
    finally:
        if connection is not None:
            connection.close()


def rows(connection, query, args=()):
    cursor = connection.execute(query, args)
    fields = [item[0] for item in cursor.description]
    return [dict(zip(fields, row)) for row in cursor]


def parse_messages(value):
    if isinstance(value, str):
        try:
            value = parse_json(value)
        except json.JSONDecodeError:
            raise MigrationError("Invalid JSON in history_v2.messages.") from None
    if isinstance(value, dict):
        value = value.get("raw_messages")
    if not isinstance(value, list) or any(not isinstance(m, dict) for m in value):
        raise MigrationError("Expected a raw_messages array of message objects.")
    return value


def make_session(raw, cwd_override=None):
    source_id = raw.get("session_id", raw.get("id"))
    if not isinstance(source_id, str) or not source_id:
        raise MigrationError("Session is missing a string session_id.")
    created = timestamp(raw.get("created_at"))
    cwd = cwd_override or raw.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise MigrationError(
            "Session cwd is absent or not local/absolute; supply --cwd /project/path."
        )
    # Do not require the project to exist on the exporting machine.
    cwd = str(Path(cwd).resolve())
    entries = []
    warnings = []
    if cwd_override is not None and cwd_override != raw.get("cwd"):
        warnings.append(
            "Session cwd overridden; historical tool paths are preserved, not relocated."
        )
    records = raw.get("records")
    if records is None:
        records = [{"messages": raw.get("messages"), "created_at": created}]
    if not isinstance(records, list):
        raise MigrationError("Session records must be an array.")
    seen_ids = {}
    previous = []
    mode = raw.get("history_mode", "increments")
    if mode not in ("increments", "snapshots"):
        raise MigrationError("history_mode must be increments or snapshots.")
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise MigrationError("History record must be an object.")
        record_id = record.get("history_v2_id")
        if record_id:
            if not isinstance(record_id, str):
                raise MigrationError("history_v2_id must be a string.")
            record_hash = fingerprint(record)
            if record_id in seen_ids:
                if seen_ids[record_id] != record_hash:
                    raise MigrationError("Conflicting history_v2_id; export a consistent snapshot.")
                warnings.append("Duplicate history_v2_id skipped: " + str(record_id))
                continue
            seen_ids[record_id] = record_hash
        messages = parse_messages(record.get("messages"))
        time = timestamp(record.get("created_at", created))
        if mode == "snapshots":
            # A divergent prefix is a branch/compaction, not a safe append.
            if messages[: len(previous)] != previous:
                raise MigrationError("Divergent history snapshots require separate branch exports.")
            offset = len(previous)
            added = messages[offset:]
            previous = messages
        else:
            if previous and len(messages) > len(previous) and messages[: len(previous)] == previous:
                raise MigrationError(
                    "Cumulative history detected; specify history_mode=snapshots in JSON."
                )
            added = messages
            offset = 0
            previous = messages
        for message_index, message in enumerate(added):
            message_time = timestamp(message.get("timestamp", message.get("created_at", time)))
            if entries and message_time < entries[-1]["timestamp"]:
                raise MigrationError(
                    "History timestamps are not chronological; export an ordered session."
                )
            entries.append(
                {
                    "message": message,
                    "timestamp": message_time,
                    "source_ref": f"{record_id or index}:{offset + message_index}",
                }
            )
    if not entries:
        raise MigrationError("Selected session contains no messages.")
    if entries[0]["timestamp"] < created:
        raise MigrationError("Session creation timestamp is after its first message.")
    return Session(
        source_id,
        str(raw.get("session_title", raw.get("title", source_id))),
        cwd,
        created,
        entries,
        raw,
        warnings,
    )


def load_source(path, session_ids=None, cwd=None, key_env=None, history_mode=None):
    info = probe(path)
    selected = set(session_ids or [])
    if info["kind"] == "json":
        document = load_json(info["path"])
        if not isinstance(document, dict) or document.get("format") != "trae2codex.source.v1":
            raise MigrationError(
                "Unsupported JSON format. Expected trae2codex.source.v1; see docs/source-format.md."
            )
        raw_sessions = document.get("sessions")
        if not isinstance(raw_sessions, list) or any(not isinstance(s, dict) for s in raw_sessions):
            raise MigrationError("Source sessions must be an array of objects.")
        candidates = [
            s for s in raw_sessions if not selected or s.get("session_id", s.get("id")) in selected
        ]
    else:
        with connect_readonly(info["path"], key_env) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"chat_session", "history_v2"}.issubset(tables):
                raise MigrationError(
                    "Unsupported database schema; chat_session and history_v2 are required."
                )
            for table, required in [
                ("chat_session", {"session_id", "created_at"}),
                ("history_v2", {"session_id", "messages", "created_at", "id"}),
            ]:
                columns = {r[1] for r in connection.execute("PRAGMA table_info(" + table + ")")}
                if not required.issubset(columns):
                    raise MigrationError("Unsupported TRAE columns in " + table)
            metadata = rows(
                connection, "SELECT * FROM chat_session ORDER BY created_at, session_id"
            )
            candidates = []
            for session in metadata:
                if selected and session["session_id"] not in selected:
                    continue
                records = rows(
                    connection,
                    "SELECT * FROM history_v2 WHERE session_id = ? ORDER BY created_at, id",
                    (session["session_id"],),
                )
                if not records:
                    if selected:
                        raise MigrationError("Selected session has no history_v2 records.")
                    info.setdefault("skipped_sessions", []).append(
                        {
                            "source_id": session["session_id"],
                            "reason": "No history_v2 records",
                        }
                    )
                    continue
                candidates.append({**session, "records": records})
    ids = [s.get("session_id", s.get("id")) for s in candidates]
    if any(not isinstance(source_id, str) or not source_id for source_id in ids):
        raise MigrationError("Every session requires a string session_id.")
    if selected - set(ids):
        raise MigrationError("Requested session IDs were not found.")
    if len(set(ids)) != len(ids):
        raise MigrationError("Duplicate session IDs in source.")
    if not candidates:
        raise MigrationError("No eligible sessions found.")
    if history_mode is not None:
        candidates = [{**raw, "history_mode": history_mode} for raw in candidates]
    return [make_session(raw, cwd) for raw in candidates], info


def list_source(path, key_env=None):
    info = probe(path)
    if info["kind"] == "json":
        doc = load_json(info["path"])
        if not isinstance(doc, dict) or doc.get("format") != "trae2codex.source.v1":
            raise MigrationError("Unsupported JSON format.")
        if not isinstance(doc.get("sessions"), list) or any(
            not isinstance(s, dict) for s in doc["sessions"]
        ):
            raise MigrationError("Source sessions must be an array of objects.")
        return [
            {
                "session_id": s.get("session_id", s.get("id")),
                "title": s.get("session_title", s.get("title")),
                "created_at": s.get("created_at"),
            }
            for s in doc["sessions"]
        ]
    with connect_readonly(info["path"], key_env) as connection:
        columns = {r[1] for r in connection.execute("PRAGMA table_info(chat_session)")}
        if not {"session_id", "created_at"}.issubset(columns):
            raise MigrationError("Unsupported chat_session schema.")
        title = "session_title" if "session_title" in columns else "session_id"
        return rows(
            connection,
            "SELECT session_id, " + title + " AS title, created_at "
            "FROM chat_session ORDER BY created_at DESC",
        )
