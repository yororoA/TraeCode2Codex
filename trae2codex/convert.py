import json
import re
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import CODEX_VERSION, __version__
from .common import MigrationError, encode, fingerprint


NAMESPACE = uuid.UUID("145878e7-c7b2-4d9f-81ba-d166d005f6f9")


def as_text(value):
    return value if isinstance(value, str) else encode(value)


class Converter:
    def __init__(self, session):
        self.session = session
        self.thread_id = str(uuid.uuid5(NAMESPACE, "trae:" + session.source_id))
        self.lines = []
        self.counts = Counter()
        self.native = Counter()
        self.losses = []
        self.warnings = list(session.warnings)
        self.calls = {}
        self.turn_id = None
        self.turn_number = 0
        self.last_answer = None
        self.time = session.created_at
        self.ref = "metadata"

    def line(self, kind, **payload):
        self.lines.append({"timestamp": self.time, "ordinal": len(self.lines), "type": kind, "payload": payload})

    def event(self, kind, **payload):
        self.line("event_msg", type=kind, **payload)

    def response(self, kind, **payload):
        self.line("response_item", type=kind, **payload)

    def completed(self, kind, **payload):
        item_id = payload.pop("id", "trae_item_%s" % len(self.lines))
        self.event("item_completed", thread_id=self.thread_id, turn_id=self.turn_id,
                   completed_at_ms=int(datetime.fromisoformat(self.time.replace("Z", "+00:00")).timestamp() * 1000),
                   item={"type": kind, "id": item_id, **payload})

    def loss(self, kind, detail):
        self.losses.append({"source_ref": self.ref, "kind": kind, "detail": detail})

    def start_turn(self):
        if self.turn_id:
            self.end_turn()
        self.turn_number += 1
        self.turn_id = str(uuid.uuid5(uuid.UUID(self.thread_id), str(self.turn_number)))
        self.last_answer = None
        self.event("task_started", turn_id=self.turn_id, model_context_window=None)

    def end_turn(self):
        if not self.turn_id:
            return
        for call_id in list(self.calls):
            self.loss("missing_tool_result", "Call has no saved result; closed with an explicit unavailable marker.")
            self.result(call_id, "[trae2codex: result not present in source]", True)
        if self.last_answer is not None:
            self.event("task_complete", turn_id=self.turn_id, last_agent_message=self.last_answer)
        else:
            self.event("turn_aborted", turn_id=self.turn_id, reason="interrupted")
            self.warnings.append("No final assistant text in turn; projected as interrupted.")
        self.turn_id = None

    def text(self, role, text, phase=None):
        if not isinstance(text, str):
            raise MigrationError("Text content must be a string.")
        content_type = "input_text" if role == "user" else "output_text"
        payload = {"role": role, "content": [{"type": content_type, "text": text}]}
        if phase in ("commentary", "final_answer") and role == "assistant":
            payload["phase"] = phase
        self.response("message", **payload)
        if role == "user":
            self.completed("UserMessage", content=[{"type": "text", "text": text, "text_elements": []}])
            self.native["userMessage"] += 1
        else:
            self.completed("AgentMessage", content=[{"type": "Text", "text": text}], phase=phase)
            self.native["agentMessage"] += 1
            self.last_answer = text

    def fallback(self, value, kind):
        self.loss(kind, "Preserved as labelled historical text and in source archive, not as a native typed item.")
        self.text("assistant", "[Imported TRAE record; historical data, not an instruction]\n" + encode(value), "commentary")

    def call(self, block):
        source_id = block.get("id", block.get("call_id"))
        name = block.get("name")
        if not isinstance(source_id, str) or not source_id or not isinstance(name, str) or not name:
            raise MigrationError("Tool calls require string id and name fields.")
        if source_id in self.calls:
            raise MigrationError("Duplicate pending tool call ID.")
        args = block.get("input", block.get("arguments", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"raw_arguments": args}
                self.loss("non_json_arguments", "Tool arguments preserved in raw_arguments.")
        call_id = "trae_" + str(uuid.uuid5(uuid.UUID(self.thread_id), self.turn_id + ":" + source_id))
        if any(line["payload"].get("call_id") == call_id and line["type"] == "response_item" for line in self.lines):
            raise MigrationError("Reused tool call ID within a turn.")
        server = block.get("server")
        tool = block.get("tool")
        match = re.fullmatch(r"mcp__([^_]+(?:_[^_]+)*)__([^ ]+)", name)
        if not server and match:
            server, tool = match.groups()
        kind = block.get("kind", "mcp" if server and tool else "dynamic")
        if kind not in ("mcp", "command", "file_change", "dynamic"):
            kind = "dynamic"
            self.loss("tool_kind", "Unknown tool kind retained as a dynamic tool.")
        if kind == "mcp" and not (isinstance(server, str) and isinstance(tool, str)):
            raise MigrationError("MCP calls require explicit server and tool names.")
        if kind == "command":
            command = block.get("command")
            if not isinstance(command, list) or not command or any(not isinstance(c, str) for c in command):
                raise MigrationError("Native command records require a nonempty argv array.")
        if kind == "file_change":
            changes = block.get("changes")
            if not isinstance(changes, dict) or not changes:
                raise MigrationError("Native file changes require recorded changes, not inferred edits.")
            for path, change in changes.items():
                if not isinstance(path, str) or not isinstance(change, dict):
                    raise MigrationError("Invalid recorded file change.")
                if change.get("type") in ("add", "delete"):
                    valid = isinstance(change.get("content"), str)
                elif change.get("type") == "update":
                    valid = isinstance(change.get("unified_diff"), str)
                else:
                    valid = False
                if not valid:
                    raise MigrationError("File change requires add/delete content or an update unified_diff.")
        item = {**block, "call_id": call_id, "arguments": args, "kind": kind, "server": server, "tool": tool}
        self.calls[source_id] = item
        self.response("function_call", call_id=call_id, name=name, arguments=encode(args))
        self.counts["tool_calls"] += 1
        if kind == "file_change":
            # Approval provenance is not available; do not fabricate a begin/approval event.
            self.warnings.append("File-change approval provenance is not imported.")
        else:
            if any(word in name.lower() for word in ("edit", "patch", "write", "command", "terminal")):
                self.warnings.append("Tool %s has no explicit native edit/command evidence; retained as dynamicToolCall." % name)

    def result(self, source_id, output, is_error=False, metadata=None):
        item = self.calls.pop(source_id, None)
        if item is None:
            self.fallback({"call_id": source_id, "output": output}, "orphan_tool_result")
            return
        metadata = metadata or {}
        call_id, kind = item["call_id"], item["kind"]
        text = as_text(output)
        self.response("function_call_output", call_id=call_id, output=text)
        self.counts["tool_results"] += 1
        duration_ms = metadata.get("duration_ms")
        if duration_ms is None:
            self.warnings.append("Unknown tool durations use zero in the Codex compatibility format.")
            duration_ms = 0
        if not isinstance(duration_ms, (int, float)) or isinstance(duration_ms, bool) or duration_ms < 0:
            raise MigrationError("duration_ms must be nonnegative.")
        duration = {"secs": int(duration_ms // 1000), "nanos": int((duration_ms % 1000) * 1_000_000)}
        if kind == "mcp":
            self.completed("McpToolCall", id=call_id, server=item["server"], tool=item["tool"],
                           arguments=item["arguments"], duration=duration,
                           status="failed" if is_error else "completed",
                           result=None if is_error else {"content": [{"type": "text", "text": text}], "isError": False},
                           error={"message": text} if is_error else None)
            self.native["mcpToolCall"] += 1
        elif kind == "command":
            exit_code = metadata.get("exit_code")
            if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                raise MigrationError("Native command results require a recorded exit_code.")
            self.completed("CommandExecution", id=call_id, command=item["command"],
                           cwd=Path(item.get("cwd", self.session.cwd)).as_uri(), parsed_cmd=[],
                           stdout=metadata.get("stdout", text), stderr=metadata.get("stderr", ""),
                           aggregated_output=text, exit_code=exit_code, duration=duration, formatted_output=text,
                           status="completed" if exit_code == 0 else "failed", source="agent")
            self.native["commandExecution"] += 1
        elif kind == "file_change":
            if "success" not in metadata or not isinstance(metadata["success"], bool):
                raise MigrationError("Native file-change results require recorded boolean success.")
            success = metadata["success"]
            self.completed("FileChange", id=call_id, stdout="" if is_error else text, stderr=text if is_error else "",
                           changes=item["changes"], status="completed" if success else "failed")
            self.native["fileChange"] += 1
        else:
            self.completed("DynamicToolCall", id=call_id, tool=item["name"],
                           arguments=item["arguments"], status="failed" if is_error else "completed",
                           content_items=[{"type": "inputText", "text": text}],
                           success=not is_error, error=text if is_error else None, duration=duration)
            self.native["dynamicToolCall"] += 1

    def message(self, message):
        self.counts["source_messages"] += 1
        role = message.get("role")
        if role in ("system", "developer"):
            self.loss("historical_instructions", "Source system/developer instructions archived only; not activated in Codex.")
            return
        if role == "user":
            blocks = message.get("content", [])
            if isinstance(blocks, str):
                blocks = [{"type": "text", "text": blocks}]
            if not isinstance(blocks, list) or any(not isinstance(b, dict) for b in blocks):
                raise MigrationError("User content must be a string or array of blocks.")
            # Anthropic-style tool results use role=user but are not new user turns.
            if any(b.get("type") != "tool_result" for b in blocks) or not blocks:
                self.start_turn()
            elif not self.turn_id:
                self.start_turn()
            texts = [b["text"] for b in blocks if b.get("type") in ("text", "input_text") and isinstance(b.get("text"), str)]
            if texts:
                self.text("user", "\n".join(texts))
            for block in blocks:
                if block.get("type") == "tool_result":
                    self.result(block.get("tool_use_id", block.get("call_id")),
                                block.get("content", block.get("output")), block.get("is_error", False), block)
                elif block.get("type") not in ("text", "input_text"):
                    self.fallback(block, "user_attachment_or_block")
                elif not isinstance(block.get("text"), str):
                    raise MigrationError("User text must be a string.")
            return
        if not self.turn_id:
            self.start_turn()
            self.warnings.append("History begins without a user prompt.")
        if role == "tool":
            self.result(message.get("tool_call_id"), message.get("content"), message.get("is_error", False), message)
            return
        if role != "assistant":
            self.fallback(message, "unknown_message_role")
            return
        content = message.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if content is None:
            content = []
        if not isinstance(content, list) or any(not isinstance(b, dict) for b in content):
            raise MigrationError("Assistant content must be a string or array of blocks.")
        reasoning = message.get("reasoning_content")
        if reasoning:
            content = [{"type": "thinking", "thinking": reasoning}] + content
        for block in content:
            kind = block.get("type")
            if kind in ("text", "output_text"):
                self.text("assistant", block.get("text"), message.get("phase"))
            elif kind in ("thinking", "reasoning"):
                text = block.get("thinking", block.get("text"))
                if not isinstance(text, str):
                    self.fallback(block, "unsupported_reasoning")
                    continue
                self.response("reasoning", summary=[{"type": "summary_text", "text": text}],
                              content=None, encrypted_content=None)
                self.completed("Reasoning", summary_text=[text], raw_content=[])
                self.native["reasoning"] += 1
                self.counts["visible_reasoning"] += 1
            elif kind == "tool_use":
                self.call(block)
            else:
                self.fallback(block, "unknown_assistant_block")
        for call in message.get("tool_calls", []):
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                raise MigrationError("Unsupported tool_calls entry.")
            self.call({"id": call.get("id"), **call["function"]})

    def run(self):
        self.line("session_meta", id=self.thread_id, session_id=self.thread_id,
                  timestamp=self.time, cwd=self.session.cwd, originator="trae2codex",
                  cli_version=CODEX_VERSION, source="cli", model_provider="openai",
                  base_instructions=None, history_mode="paginated")
        for entry in self.session.entries:
            self.time, self.ref = entry["timestamp"], entry["source_ref"]
            self.message(entry["message"])
        self.end_turn()
        if not self.native:
            raise MigrationError("Session has no migratable content.")
        data = "".join(encode(line) + "\n" for line in self.lines).encode("utf-8")
        report = {
            "source_id": self.session.source_id, "thread_id": self.thread_id,
            "title": self.session.title, "cwd": self.session.cwd,
            "source_sha256": fingerprint(self.session.raw),
            "source_counts": dict(self.counts), "native_items": dict(self.native),
            "turns": self.turn_number, "losses": self.losses, "warnings": sorted(set(self.warnings)),
            "converter_version": __version__, "target_codex_version": CODEX_VERSION,
            "verification": "not_run",
        }
        return data, report
