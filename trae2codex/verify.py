import gzip
import json
import os
import platform
import queue
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import CODEX_VERSION
from .common import MigrationError, encode, parse_json, safe_child, write_private


def isolated_env(home):
    home = str(home)
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC")
    }
    env.update(
        {
            "HOME": home,
            "USERPROFILE": home,
            "CODEX_HOME": home,
            "CODEX_SQLITE_HOME": home,
            "XDG_CONFIG_HOME": home,
            "XDG_CACHE_HOME": home,
            "APPDATA": home,
            "LOCALAPPDATA": home,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "TMPDIR": home,
            "TEMP": home,
            "TMP": home,
        }
    )
    return env


class AppServer:
    def __init__(self, binary, home, cwd):
        self.process = subprocess.Popen(
            [str(binary), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env=isolated_env(home),
            cwd=cwd,
            start_new_session=os.name != "nt",
        )
        self.messages = queue.Queue()
        self.notifications = []
        self.serial = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {"name": "trae2codex", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.send({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            self.messages.put(None)

    def send(self, message):
        try:
            self.process.stdin.write(encode(message) + "\n")
            self.process.stdin.flush()
        except (OSError, BrokenPipeError):
            raise MigrationError("Codex app-server exited unexpectedly.") from None

    def next_message(self, timeout):
        try:
            value = self.messages.get(timeout=max(0.01, timeout))
        except queue.Empty:
            raise MigrationError("Codex app-server timed out.") from None
        if value is None:
            raise MigrationError("Codex app-server closed the connection.")
        if "method" in value and "id" in value:
            # The verifier never approves or executes any requested operation.
            self.send(
                {
                    "id": value["id"],
                    "error": {"code": -32601, "message": "Verification is read-only."},
                }
            )
            raise MigrationError("Unexpected tool/approval request during verification.")
        return value

    def request(self, method, params, timeout=40):
        self.serial += 1
        request_id = self.serial
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.next_message(deadline - time.monotonic())
            if value.get("id") == request_id:
                if "error" in value:
                    # Do not echo errors that may contain source history or local secrets.
                    raise MigrationError(
                        "Codex rejected {} (RPC {}).".format(method, value["error"].get("code"))
                    )
                return value.get("result", {})
            self.notifications.append(value)
        raise MigrationError("Codex request timed out: " + method)

    def wait_completed(self, timeout=40):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.notifications:
                value = self.notifications.pop(0)
            else:
                value = self.next_message(deadline - time.monotonic())
            if value.get("method") == "turn/completed":
                turn = value.get("params", {}).get("turn", {})
                if turn.get("status") != "completed":
                    raise MigrationError("Offline continuation probe did not complete.")
                return
        raise MigrationError("Offline continuation probe timed out.")

    def close(self):
        if self.process.poll() is None:
            if os.name == "nt":
                self.process.terminate()
            else:
                os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                self.process.kill()
            else:
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        if self.process.stdin:
            self.process.stdin.close()
        self.reader.join(timeout=5)
        if self.process.stdout:
            self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class OfflineModel:
    """A loopback Responses endpoint: verifies input without using an account."""

    def __init__(self):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if length < 0 or length > 256 * 1024 * 1024:
                        self.send_error(413)
                        return
                    data = self.rfile.read(length)
                    if self.headers.get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)
                    value = parse_json(data)
                    if not isinstance(value, dict):
                        self.send_error(400)
                        return
                    owner.requests.append(value)
                except (ValueError, UnicodeError, OSError, MigrationError):
                    self.send_error(400)
                    return
                message = {
                    "type": "message",
                    "id": "msg_migration_probe",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "migration-probe-ok", "annotations": []}
                    ],
                }
                events = [
                    {
                        "type": "response.created",
                        "response": {"id": "resp_migration_probe", "status": "in_progress"},
                    },
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**message, "content": []},
                    },
                    {
                        "type": "response.output_text.delta",
                        "item_id": message["id"],
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "migration-probe-ok",
                    },
                    {"type": "response.output_item.done", "output_index": 0, "item": message},
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_migration_probe",
                            "status": "completed",
                            "output": [message],
                            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                        },
                    },
                ]
                body = "".join(
                    "event: {}\ndata: {}\n\n".format(e["type"], encode(e)) for e in events
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def native_binary(binary):
    """Resolve the supported npm layouts without launching a Windows shell shim."""
    found = shutil.which(str(binary))
    path = Path(found or binary).expanduser().resolve()
    arch = {"arm64": "arm64", "aarch64": "arm64", "amd64": "x64", "x86_64": "x64"}.get(
        platform.machine().lower()
    )
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}.get(platform.system())
    roots = []
    if path.name == "codex.js":
        roots.append(path.parent.parent)
    if path.suffix.lower() in (".cmd", ".ps1", ".bat"):
        roots.extend(
            [path.parent / "node_modules/@openai/codex", path.parent.parent / "@openai/codex"]
        )
    triples = {
        ("darwin", "arm64"): "aarch64-apple-darwin",
        ("darwin", "x64"): "x86_64-apple-darwin",
        ("linux", "arm64"): "aarch64-unknown-linux-musl",
        ("linux", "x64"): "x86_64-unknown-linux-musl",
        ("win32", "arm64"): "aarch64-pc-windows-msvc",
        ("win32", "x64"): "x86_64-pc-windows-msvc",
    }
    triple = triples.get((system, arch))
    for root in roots:
        if not triple:
            break
        for package in (root.parent / (f"codex-{system}-{arch}"), root):
            candidate = (
                package
                / "vendor"
                / triple
                / "bin"
                / ("codex.exe" if system == "win32" else "codex")
            )
            if candidate.is_file():
                return candidate.resolve()
    if path.suffix.lower() in (".cmd", ".ps1", ".bat"):
        raise MigrationError(
            "Cannot resolve the npm Codex binary; pass --codex with the native codex.exe path."
        )
    return path


def get_binary(binary):
    path = native_binary(binary)
    if not path.is_file():
        raise MigrationError(
            f"Codex executable not found. Install @openai/codex@{CODEX_VERSION} or use --codex /path/to/codex."
        )
    with tempfile.TemporaryDirectory(prefix="trae2codex-version-") as temp:
        try:
            result = subprocess.run(
                [str(path), "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                env=isolated_env(temp),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise MigrationError("Cannot execute Codex --version.") from None
    if result.returncode or result.stdout.strip() != "codex-cli " + CODEX_VERSION:
        raise MigrationError(
            f"Unsupported Codex version. This adapter is pinned to {CODEX_VERSION}; do not force incompatible imports."
        )
    return path


def items_from_thread(thread):
    return [item for turn in thread.get("turns", []) for item in turn.get("items", [])]


def hydrate_thread(server, thread):
    turns = []
    cursor = None
    while True:
        page = server.request(
            "thread/turns/list",
            {
                "threadId": thread["id"],
                "limit": 100,
                "cursor": cursor,
                "sortDirection": "asc",
                "itemsView": "full",
            },
        )
        turns.extend(page["data"])
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return {**thread, "turns": turns}


def check_projection(thread, expected, rollout):
    items = items_from_thread(thread)
    counts = Counter(item.get("type") for item in items)
    if dict(counts) != expected["native_items"]:
        raise MigrationError(
            "Codex native item counts differ for thread {}: expected {}, got {}".format(
                expected["thread_id"], expected["native_items"], dict(counts)
            )
        )
    if len(thread.get("turns", [])) != expected["turns"]:
        raise MigrationError("Codex turn count differs from the source mapping.")
    persisted = [
        line["payload"]["item"]
        for line in rollout
        if line["type"] == "event_msg" and line["payload"]["type"] == "item_completed"
    ]
    if [item["id"] for item in items] != [item["id"] for item in persisted]:
        raise MigrationError("Codex native item identity/order differs from the rollout.")
    for original, actual in zip(persisted, items):
        kind = original["type"]
        fields = {}
        if kind == "UserMessage":
            fields["content"] = original["content"]
        elif kind == "AgentMessage":
            fields.update(
                text="".join(b["text"] for b in original["content"]), phase=original.get("phase")
            )
        elif kind == "Reasoning":
            fields.update(summary=original["summary_text"], content=original["raw_content"])
        elif kind == "DynamicToolCall":
            fields.update(
                tool=original["tool"],
                arguments=original["arguments"],
                status=original["status"],
                contentItems=original["content_items"],
                success=original["success"],
            )
        elif kind == "McpToolCall":
            fields.update(
                server=original["server"],
                tool=original["tool"],
                arguments=original["arguments"],
                status=original["status"],
                error=original.get("error"),
            )
            if (
                original.get("result")
                and actual.get("result", {}).get("content") != original["result"]["content"]
            ):
                raise MigrationError("Codex MCP result content differs from the rollout.")
        elif kind == "CommandExecution":
            uri = urlsplit(original["cwd"])
            path = unquote(uri.path)
            if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
                path = path[1:]
            if uri.netloc:
                path = "//" + uri.netloc + path
            if os.path.normcase(os.path.normpath(actual["cwd"])) != os.path.normcase(
                os.path.normpath(path)
            ):
                raise MigrationError("Codex command working directory differs from the rollout.")
            fields.update(
                command=shlex.join(original["command"]),
                status=original["status"],
                aggregatedOutput=original["aggregated_output"],
                exitCode=original["exit_code"],
            )
        elif kind == "FileChange":
            changes = []
            for path, change in sorted(original["changes"].items()):
                change_kind = {"type": change["type"]}
                diff = change.get("content", change.get("unified_diff"))
                if change["type"] == "update":
                    change_kind["move_path"] = change.get("move_path")
                    if change.get("move_path"):
                        diff += "\n\nMoved to: " + change["move_path"]
                changes.append({"path": path, "kind": change_kind, "diff": diff})
            fields.update(changes=changes, status=original["status"])
        if any(actual.get(key) != value for key, value in fields.items()):
            raise MigrationError(f"Codex native {kind} content differs from the rollout.")
    return counts


def check_context(rollout, request):
    actual_input = request.get("input", [])
    checks = Counter()
    position = 0
    for line in rollout:
        if line["type"] != "response_item":
            continue
        item = line["payload"]
        kind = item["type"]
        if kind == "message":
            fields = {key: item[key] for key in ("type", "role", "content")}
            checks["messages"] += 1
        elif kind == "function_call":
            fields = {key: item[key] for key in ("type", "call_id", "name", "arguments")}
            checks["tool_calls"] += 1
        elif kind == "function_call_output":
            fields = {key: item[key] for key in ("type", "call_id", "output")}
            checks["tool_results"] += 1
        else:
            continue
        while position < len(actual_input):
            actual = actual_input[position]
            position += 1
            if all(actual.get(key) == value for key, value in fields.items()):
                break
        else:
            raise MigrationError(
                f"Historical {kind} missing/modified/reordered in resumed model request."
            )
    return dict(checks)


def verify_bundle(bundle, binary="codex"):
    from .bundle import read_bundle

    root, manifest, report = read_bundle(bundle)
    binary = get_binary(binary)
    results = []
    rollouts = {
        session["thread_id"]: [
            parse_json(line)
            for line in safe_child(root, session["rollout"])
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        for session in report["sessions"]
    }
    with (
        tempfile.TemporaryDirectory(prefix="trae2codex-verify-") as temporary,
        OfflineModel() as model,
    ):
        home = Path(temporary) / "codex"
        cwd = Path(temporary) / "project"
        home.mkdir(mode=0o700)
        cwd.mkdir(mode=0o700)
        config = (
            'model = "gpt-5.4"\nmodel_provider = "migration_verify"\n'
            'approval_policy = "never"\nsandbox_mode = "read-only"\nweb_search = "disabled"\n'
            "[analytics]\nenabled = false\n[feedback]\nenabled = false\n"
            '[model_providers.migration_verify]\nname = "Offline migration verification"\n'
            f'base_url = "http://127.0.0.1:{model.server.server_port}/v1"\nwire_api = "responses"\n'
            "requires_openai_auth = false\nsupports_websockets = false\n"
        )
        write_private(home / "config.toml", config.encode("utf-8"))
        for thread in manifest["threads"]:
            write_private(
                safe_child(home, thread["rollout"]),
                safe_child(root, thread["rollout"]).read_bytes(),
            )
        # A new app-server instance must discover every imported thread from disk.
        with AppServer(binary, home, cwd) as server:
            listed = set()
            cursor = None
            while True:
                page = server.request(
                    "thread/list", {"limit": 100, "cursor": cursor, "modelProviders": []}
                )
                listed.update(t["id"] for t in page["data"])
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            if not {t["id"] for t in manifest["threads"]}.issubset(listed):
                raise MigrationError("Codex did not discover all imported sessions.")
            for session in report["sessions"]:
                # First resume builds the paginated item index from a foreign rollout.
                resumed = server.request(
                    "thread/resume",
                    {
                        "threadId": session["thread_id"],
                        "cwd": str(cwd),
                        "modelProvider": "migration_verify",
                        "model": "gpt-5.4",
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                    },
                )
                check_projection(
                    hydrate_thread(server, resumed["thread"]),
                    session,
                    rollouts[session["thread_id"]],
                )
        # Restart proves that the native index and history survive a process boundary.
        with AppServer(binary, home, cwd) as server:
            for session in report["sessions"]:
                read = server.request(
                    "thread/read", {"threadId": session["thread_id"], "includeTurns": True}
                )
                check_projection(
                    hydrate_thread(server, read["thread"]), session, rollouts[session["thread_id"]]
                )
                resumed = server.request(
                    "thread/resume",
                    {
                        "threadId": session["thread_id"],
                        "cwd": str(cwd),
                        "modelProvider": "migration_verify",
                        "model": "gpt-5.4",
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                    },
                )
                native = check_projection(
                    hydrate_thread(server, resumed["thread"]),
                    session,
                    rollouts[session["thread_id"]],
                )
                before = len(model.requests)
                server.request(
                    "turn/start",
                    {
                        "threadId": session["thread_id"],
                        "input": [
                            {
                                "type": "text",
                                "text": "Reply only migration-probe-ok. Do not call tools.",
                            }
                        ],
                    },
                )
                server.wait_completed()
                if len(model.requests) <= before:
                    raise MigrationError("No request reached the offline model.")
                checks = check_context(rollouts[session["thread_id"]], model.requests[before])
                results.append(
                    {
                        "thread_id": session["thread_id"],
                        "native_items": dict(native),
                        "model_context_checks": checks,
                        "listed": True,
                        "resumed_after_restart": True,
                        "offline_continuation": True,
                    }
                )
    return {
        "status": "passed",
        "codex_version": CODEX_VERSION,
        "sessions": results,
        "first_open": "Run codex resume <id> once to build the paginated native item index.",
        "scope": "Real Codex app-server projection/restart/resume; loopback mock model, not a live paid model or desktop UI.",
        "reasoning_context": "Native reasoning projection checked; provider retention of unsigned reasoning is not guaranteed.",
    }
