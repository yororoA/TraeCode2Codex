import json
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import __version__
from .common import MigrationError, digest, encode, load_json, safe_child, secret_count, write_json, write_private
from .convert import Converter
from .source import load_source


def prepare(source, session_ids=None, cwd=None, key_env=None):
    sessions, info = load_source(source, session_ids, cwd, key_env)
    files, reports = {}, []
    for session in sessions:
        data, report = Converter(session).run()
        date = session.created_at[:10].replace("-", "/")
        time = session.created_at[:19].replace(":", "-")
        relative = "sessions/%s/rollout-%s-%s.jsonl" % (date, time, report["thread_id"])
        report["rollout"] = relative
        report["sensitive_candidates"] = secret_count(session.raw)
        report["archive"] = "source/%s.json" % report["thread_id"]
        files[relative] = data
        files[report["archive"]] = (encode(session.raw) + "\n").encode("utf-8")
        reports.append(report)
    report_document = {
        "format": "trae2codex.report.v1", "source_kind": info["kind"],
        "sessions": reports,
        "limits": [
            "Only supplied source records are covered; source-side missing history is unknowable.",
            "No process state, rollback checkpoints, credentials, tools or skills are installed.",
            "Reasoning is saved visible text, not internal model state.",
            "No commands or patches are replayed; project files are not migrated.",
            "Source instructions remain historical data; imported sessions should be reviewed before resuming.",
        ],
    }
    files["report.json"] = (encode(report_document) + "\n").encode("utf-8")
    manifest = {
        "format": "trae2codex.bundle.v1", "converter_version": __version__,
        "files": {name: digest(data) for name, data in files.items()},
        "threads": [{"id": r["thread_id"], "rollout": r["rollout"]} for r in reports],
    }
    manifest["bundle_id"] = digest(encode(manifest).encode("utf-8"))
    return files, manifest, report_document


def build_bundle(source, output, session_ids=None, cwd=None, key_env=None, accept_sensitive=False):
    files, manifest, report = prepare(source, session_ids, cwd, key_env)
    if not accept_sensitive and any(s["sensitive_candidates"] for s in report["sessions"]):
        raise MigrationError(
            "Possible secrets found. Review the source locally and redact it, or explicitly use "
            "--accept-sensitive to preserve it. Secret scanning is heuristic, not a safety guarantee."
        )
    output = Path(output).expanduser().resolve()
    if output.exists():
        raise MigrationError("Output already exists; choose a new bundle directory.")
    output.mkdir(mode=0o700, parents=True)
    # The manifest is the commit marker. Partial bundles cannot be installed.
    for name, data in files.items():
        write_private(safe_child(output, name), data)
    write_json(output / "manifest.json", manifest)
    return report


def read_bundle(path):
    path = Path(path).expanduser().resolve()
    manifest = load_json(path / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("format") != "trae2codex.bundle.v1":
        raise MigrationError("Unsupported bundle manifest.")
    files, threads = manifest.get("files"), manifest.get("threads")
    if not isinstance(files, dict) or not isinstance(threads, list) or not threads:
        raise MigrationError("Invalid bundle manifest.")
    base = {k: v for k, v in manifest.items() if k != "bundle_id"}
    if digest(encode(base).encode("utf-8")) != manifest.get("bundle_id"):
        raise MigrationError("Bundle manifest checksum mismatch.")
    ids = set()
    for thread in threads:
        if not isinstance(thread, dict):
            raise MigrationError("Invalid thread manifest.")
        try:
            thread_id = str(uuid.UUID(thread["id"]))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise MigrationError("Invalid thread UUID.") from None
        if thread_id in ids:
            raise MigrationError("Duplicate thread UUID.")
        ids.add(thread_id)
        relative = thread.get("rollout", "")
        if not re.fullmatch(r"sessions/\d{4}/\d{2}/\d{2}/rollout-[\dT-]+-" + re.escape(thread_id) + r"\.jsonl", relative):
            raise MigrationError("Unexpected rollout path.")
        if relative not in files:
            raise MigrationError("Rollout missing from file checksums.")
    expected = {"report.json"}
    expected.update(t["rollout"] for t in threads)
    expected.update("source/%s.json" % t["id"] for t in threads)
    if set(files) != expected:
        raise MigrationError("Unexpected files in bundle manifest.")
    for relative, checksum in files.items():
        file = safe_child(path, relative)
        if not isinstance(checksum, str) or digest(file.read_bytes()) != checksum:
            raise MigrationError("Bundle file checksum mismatch: " + relative)
    report = load_json(safe_child(path, "report.json"))
    if not isinstance(report, dict) or report.get("format") != "trae2codex.report.v1":
        raise MigrationError("Invalid bundle report.")
    if {s.get("thread_id") for s in report.get("sessions", [])} != ids:
        raise MigrationError("Report thread IDs do not match the manifest.")
    return path, manifest, report


@contextmanager
def home_lock(home):
    home = Path(home).expanduser().resolve()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    control = safe_child(home, ".trae2codex")
    control.mkdir(mode=0o700, exist_ok=True)
    lock = safe_child(home, ".trae2codex/lock")
    try:
        write_private(lock, str(os.getpid()).encode("ascii"))
    except FileExistsError:
        raise MigrationError("Another migration holds the lock. Do not remove it until that process exits.") from None
    try:
        yield home
    finally:
        lock.unlink()


def install_bundle(bundle, home, codex, apply=False, allow_loss=False):
    from .verify import verify_bundle
    root, manifest, report = read_bundle(bundle)
    if any(s["losses"] for s in report["sessions"]) and not allow_loss:
        raise MigrationError("Bundle has lossy mappings. Review report.json and explicitly pass --allow-loss.")
    if not apply:
        return {"status": "dry_run", "target": str(Path(home).expanduser().resolve()),
                "threads": manifest["threads"], "note": "Use --apply after closing Codex. Verification runs before writing."}
    # Verify against the caller's binary, never trust a copied verification claim.
    verification = verify_bundle(root, codex)
    if verification["status"] != "passed":
        raise MigrationError("Codex verification failed; target was not modified.")
    with home_lock(home) as target:
        receipt_rel = ".trae2codex/receipts/%s.json" % manifest["bundle_id"]
        receipt_path = safe_child(target, receipt_rel)
        if receipt_path.exists():
            receipt = load_json(receipt_path)
            for name, checksum in receipt["files"].items():
                file = safe_child(target, name)
                if not file.is_file() or digest(file.read_bytes()) != checksum:
                    raise MigrationError("Previously imported session changed or disappeared; refusing to overwrite it.")
            return {"status": "already_installed", "receipt": str(receipt_path)}
        names = [t["rollout"] for t in manifest["threads"]]
        for thread in manifest["threads"]:
            # UUID identity must not collide with a renamed or archived rollout.
            for parent in ("sessions", "archived_sessions"):
                directory = safe_child(target, parent)
                if directory.exists() and any(directory.rglob("*" + thread["id"] + "*.jsonl")):
                    raise MigrationError("Thread ID already exists in target: " + thread["id"])
            if safe_child(target, thread["rollout"]).exists():
                raise MigrationError("Destination rollout already exists.")
        receipt = {
            "format": "trae2codex.receipt.v1", "bundle_id": manifest["bundle_id"],
            "home": str(target), "files": {name: manifest["files"][name] for name in names},
            "verification": verification,
        }
        # A receipt is written before data so interrupted installs remain auditable.
        write_json(receipt_path, receipt)
        written = []
        try:
            for name in names:
                data = safe_child(root, name).read_bytes()
                if digest(data) != manifest["files"][name]:
                    raise MigrationError("Bundle changed during installation.")
                write_private(safe_child(target, name), data)
                written.append(name)
        except Exception:
            for name in written:
                file = safe_child(target, name)
                if digest(file.read_bytes()) == receipt["files"][name]:
                    file.unlink()
            # Retain the receipt for recovery; never erase evidence on a partial failure.
            raise
    return {"status": "installed", "receipt": str(receipt_path), "threads": manifest["threads"],
            "note": "Reopen Codex or use codex resume <id>. UI titles may use the first user message."}


def rollback(home, bundle_id, apply=False):
    if not re.fullmatch(r"[a-f0-9]{64}", bundle_id):
        raise MigrationError("Invalid bundle ID.")
    target = Path(home).expanduser().resolve()
    receipt_rel = ".trae2codex/receipts/%s.json" % bundle_id
    receipt = load_json(safe_child(target, receipt_rel))
    if receipt.get("format") != "trae2codex.receipt.v1" or receipt.get("home") != str(target):
        raise MigrationError("Receipt does not belong to this Codex home.")
    if receipt.get("bundle_id") != bundle_id or not isinstance(receipt.get("files"), dict):
        raise MigrationError("Invalid receipt.")
    files = []
    for name, checksum in receipt["files"].items():
        if not re.fullmatch(r"sessions/\d{4}/\d{2}/\d{2}/rollout-[\dT-]+-[a-f0-9-]{36}\.jsonl", name):
            raise MigrationError("Unsafe rollback receipt path.")
        file = safe_child(target, name)
        if file.exists() and digest(file.read_bytes()) != checksum:
            raise MigrationError("Session changed after import; rollback refuses to delete new conversation data.")
        files.append(file)
    if not apply:
        return {"status": "dry_run", "files": [str(f) for f in files]}
    with home_lock(target):
        for file in files:
            if file.exists():
                name = file.relative_to(target).as_posix()
                if digest(file.read_bytes()) != receipt["files"][name]:
                    raise MigrationError("Session changed during rollback.")
        for file in files:
            if file.exists():
                file.unlink()
        saved_receipt = safe_child(target, receipt_rel)
        rolled_back = safe_child(target, ".trae2codex/receipts/%s.rolled-back.json" % bundle_id)
        if rolled_back.exists():
            raise MigrationError("Rollback archive already exists.")
        saved_receipt.rename(rolled_back)
    return {"status": "rolled_back", "note": "Only unchanged imported rollouts removed. Codex may retain a stale index until refresh."}
