import gzip
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import CODEX_VERSION
from .common import MigrationError, encode, safe_child, write_private


class AppServer:
    def __init__(self, binary, home, cwd):
        env = dict(os.environ)
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_SQLITE_HOME", "OPENAI_BASE_URL",
                    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"):
            env.pop(key, None)
        env["CODEX_HOME"] = str(home)
        env["CODEX_SQLITE_HOME"] = str(home)
        self.process = subprocess.Popen(
            [str(binary), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=env, cwd=cwd,
        )
        self.messages = queue.Queue()
        self.notifications = []
        self.serial = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.request("initialize", {"clientInfo": {"name": "trae2codex", "version": "0.1.0"},
                                       "capabilities": {"experimentalApi": True}})
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
            self.send({"id": value["id"], "error": {"code": -32601, "message": "Verification is read-only."}})
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
                    raise MigrationError("Codex rejected %s (RPC %s)." % (method, value["error"].get("code")))
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
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
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
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                try:
                    owner.requests.append(json.loads(data))
                except (ValueError, UnicodeError):
                    self.send_error(400)
                    return
                message = {
                    "type": "message", "id": "msg_migration_probe", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "migration-probe-ok", "annotations": []}],
                }
                events = [
                    {"type": "response.created", "response": {"id": "resp_migration_probe", "status": "in_progress"}},
                    {"type": "response.output_item.added", "output_index": 0, "item": {**message, "content": []}},
                    {"type": "response.output_text.delta", "item_id": message["id"], "output_index": 0,
                     "content_index": 0, "delta": "migration-probe-ok"},
                    {"type": "response.output_item.done", "output_index": 0, "item": message},
                    {"type": "response.completed", "response": {
                        "id": "resp_migration_probe", "status": "completed", "output": [message],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    }},
                ]
                body = "".join("event: %s\ndata: %s\n\n" % (e["type"], encode(e)) for e in events).encode("utf-8")
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


def get_binary(binary):
    found = shutil.which(str(binary))
    path = Path(found or binary).expanduser().resolve()
    if not path.is_file():
        raise MigrationError("Codex executable not found. Install @openai/codex@%s or use --codex /path/to/codex." % CODEX_VERSION)
    with tempfile.TemporaryDirectory(prefix="trae2codex-version-") as temp:
        env = {**os.environ, "CODEX_HOME": temp, "CODEX_SQLITE_HOME": temp}
        try:
            result = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=15, env=env)
        except (OSError, subprocess.TimeoutExpired):
            raise MigrationError("Cannot execute Codex --version.") from None
    if result.returncode or result.stdout.strip() != "codex-cli " + CODEX_VERSION:
        raise MigrationError("Unsupported Codex version. This adapter is pinned to %s; do not force incompatible imports." % CODEX_VERSION)
    return path


def items_from_thread(thread):
    return [item for turn in thread.get("turns", []) for item in turn.get("items", [])]


def hydrate_thread(server, thread):
    turns = []
    cursor = None
    while True:
        page = server.request("thread/turns/list", {
            "threadId": thread["id"], "limit": 100, "cursor": cursor,
            "sortDirection": "asc", "itemsView": "full",
        })
        turns.extend(page["data"])
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return {**thread, "turns": turns}


def check_projection(thread, expected):
    items = items_from_thread(thread)
    counts = Counter(item.get("type") for item in items)
    if dict(counts) != expected["native_items"]:
        raise MigrationError("Codex native item counts differ for thread %s: expected %s, got %s"
                             % (expected["thread_id"], expected["native_items"], dict(counts)))
    if len(thread.get("turns", [])) != expected["turns"]:
        raise MigrationError("Codex turn count differs from the source mapping.")
    return counts


def check_context(rollout, request):
    actual_input = encode(request.get("input", []))
    checks = Counter()
    for line in rollout:
        if line["type"] != "response_item":
            continue
        item = line["payload"]
        kind = item["type"]
        if kind == "message":
            for block in item["content"]:
                text = block["text"]
                if text and encode(text)[1:-1] not in actual_input:
                    raise MigrationError("A historical message is missing from the resumed model request.")
                checks["messages"] += 1
        elif kind == "function_call":
            if item["call_id"] not in actual_input:
                raise MigrationError("A historical tool call is missing from the resumed model request.")
            checks["tool_calls"] += 1
        elif kind == "function_call_output":
            if item["output"] and encode(item["output"])[1:-1] not in actual_input:
                raise MigrationError("A historical tool result is missing from the resumed model request.")
            checks["tool_results"] += 1
    return dict(checks)


def verify_bundle(bundle, binary="codex"):
    from .bundle import read_bundle
    root, manifest, report = read_bundle(bundle)
    binary = get_binary(binary)
    results = []
    with tempfile.TemporaryDirectory(prefix="trae2codex-verify-") as temporary, OfflineModel() as model:
        home = Path(temporary) / "codex"
        cwd = Path(temporary) / "project"
        home.mkdir(mode=0o700)
        cwd.mkdir(mode=0o700)
        config = (
            'model = "gpt-5.4"\nmodel_provider = "migration_verify"\n'
            'approval_policy = "never"\nsandbox_mode = "read-only"\nweb_search = "disabled"\n'
            '[analytics]\nenabled = false\n[feedback]\nenabled = false\n'
            '[model_providers.migration_verify]\nname = "Offline migration verification"\n'
            'base_url = "http://127.0.0.1:%s/v1"\nwire_api = "responses"\n'
            'requires_openai_auth = false\nsupports_websockets = false\n' % model.server.server_port
        )
        write_private(home / "config.toml", config.encode("utf-8"))
        for thread in manifest["threads"]:
            write_private(safe_child(home, thread["rollout"]), safe_child(root, thread["rollout"]).read_bytes())
        # A new app-server instance must discover every imported thread from disk.
        with AppServer(binary, home, cwd) as server:
            listed = set()
            cursor = None
            while True:
                page = server.request("thread/list", {"limit": 100, "cursor": cursor, "modelProviders": []})
                listed.update(t["id"] for t in page["data"])
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            if not {t["id"] for t in manifest["threads"]}.issubset(listed):
                raise MigrationError("Codex did not discover all imported sessions.")
            for session in report["sessions"]:
                read = server.request("thread/read", {"threadId": session["thread_id"], "includeTurns": True})
                check_projection(hydrate_thread(server, read["thread"]), session)
        # Restart proves that the native index and history survive a process boundary.
        with AppServer(binary, home, cwd) as server:
            for session in report["sessions"]:
                resumed = server.request("thread/resume", {
                    "threadId": session["thread_id"], "cwd": str(cwd),
                    "modelProvider": "migration_verify", "model": "gpt-5.4",
                    "approvalPolicy": "never", "sandbox": "read-only",
                })
                native = check_projection(hydrate_thread(server, resumed["thread"]), session)
                before = len(model.requests)
                server.request("turn/start", {
                    "threadId": session["thread_id"],
                    "input": [{"type": "text", "text": "Reply only migration-probe-ok. Do not call tools."}],
                })
                server.wait_completed()
                if len(model.requests) <= before:
                    raise MigrationError("No request reached the offline model.")
                rollout = [json.loads(line) for line in safe_child(root, session["rollout"]).read_text(encoding="utf-8").splitlines()]
                checks = check_context(rollout, model.requests[before])
                results.append({
                    "thread_id": session["thread_id"], "native_items": dict(native),
                    "model_context_checks": checks, "listed": True, "resumed_after_restart": True,
                    "offline_continuation": True,
                })
    return {
        "status": "passed", "codex_version": CODEX_VERSION, "sessions": results,
        "scope": "Real Codex app-server projection/restart/resume; loopback mock model, not a live paid model or desktop UI.",
        "reasoning_context": "Native reasoning projection checked; provider retention of unsigned reasoning is not guaranteed.",
    }
