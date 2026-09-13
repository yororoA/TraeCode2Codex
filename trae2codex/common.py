import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class MigrationError(Exception):
    """An actionable failure that must not expose source content or secrets."""


def encode(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(value):
    return digest(encode(value).encode("utf-8"))


def parse_json(text):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MigrationError("Duplicate JSON object keys are not supported.")
            result[key] = value
        return result

    def reject_constant(_):
        raise MigrationError("Non-finite JSON numbers are not supported.")

    return json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)


def load_json(path, max_bytes=256 * 1024 * 1024):
    path = Path(path)
    if path.stat().st_size > max_bytes:
        raise MigrationError("JSON input exceeds 256 MiB; export one session at a time.")
    try:
        return parse_json(path.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        raise MigrationError("Invalid UTF-8 JSON input: " + path.name) from None


def write_private(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".trae2codex-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # An atomic, exclusive link publishes only complete files and never overwrites.
        # Filesystems without hardlinks fail closed instead of weakening this guarantee.
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def write_json(path, value):
    write_private(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def timestamp(value):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError()
        if isinstance(value, (int, float)):
            number = value / 1000 if value > 100_000_000_000 else value
            dt = datetime.fromtimestamp(number, timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError()
        return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError, TypeError):
        raise MigrationError(
            "Missing or invalid timestamp; use ISO 8601 with timezone or Unix time."
        ) from None


def safe_child(root, relative):
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise MigrationError("Unsafe path in migration manifest.")
    path = Path(relative)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or "\\" in relative
        or ":" in relative
    ):
        raise MigrationError("Unsafe path in migration manifest.")
    root = Path(root).resolve()
    candidate = root.joinpath(path)
    for parent in [candidate, *candidate.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise MigrationError("Symlinks are not allowed in migration paths.")
    if root not in candidate.resolve().parents:
        raise MigrationError("Migration path escapes its root.")
    return candidate


SECRET_PATTERNS = (
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|authorization|cookie|password|secret)\b"
        r"""["']?\s*[:=]\s*["']?([^\s"',;}{]{4,})"""
    ),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def secret_count(value):
    text = encode(value)
    return sum(len(pattern.findall(text)) for pattern in SECRET_PATTERNS)
