import contextlib
import copy
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trae2codex.bundle import (
    build_bundle,
    home_lock,
    install_bundle,
    prepare,
    read_bundle,
    rollback,
)
from trae2codex.cli import main
from trae2codex.common import (
    MigrationError,
    digest,
    encode,
    fingerprint,
    load_json,
    safe_child,
    timestamp,
    write_private,
)
from trae2codex.convert import Converter
from trae2codex.source import connect_readonly, list_source, load_source, make_session, probe
from trae2codex.verify import check_context, isolated_env

ROOT = Path(__file__).resolve().parents[1]


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="trae2codex-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.document = load_json(ROOT / "examples/session.json")
        self.document["sessions"][0]["cwd"] = str(self.root / "project")
        self.source = self.root / "export.json"
        self.write_source()

    def write_source(self):
        self.source.write_text(encode(self.document), encoding="utf-8")

    def convert(self):
        return Converter(make_session(self.document["sessions"][0])).run()

    def bundle(self, name="bundle"):
        destination = self.root / name
        build_bundle(self.source, destination)
        return destination

    def create_db(self):
        path = self.root / "database.db"
        connection = sqlite3.connect(path)
        connection.executescript("""
            CREATE TABLE chat_session (id INTEGER PRIMARY KEY, session_id TEXT, session_title TEXT, created_at INTEGER);
            CREATE TABLE history_v2 (id INTEGER PRIMARY KEY, history_v2_id TEXT, session_id TEXT, messages TEXT, created_at INTEGER);
        """)
        connection.execute("INSERT INTO chat_session VALUES (1, 'one', 'Test', 1788256800)")
        connection.execute(
            "INSERT INTO history_v2 VALUES (1, 'h1', 'one', ?, 1788256801)",
            (
                encode(
                    {
                        "raw_messages": [
                            {"role": "user", "content": "Hello"},
                            {"role": "assistant", "content": "Hi"},
                        ]
                    }
                ),
            ),
        )
        connection.commit()
        return path, connection

    def test_all_native_types_and_context_pairs(self):
        data, report = self.convert()
        self.assertEqual(
            report["native_items"],
            {
                "userMessage": 2,
                "agentMessage": 3,
                "reasoning": 1,
                "commandExecution": 1,
                "dynamicToolCall": 1,
                "mcpToolCall": 1,
                "fileChange": 1,
            },
        )
        self.assertEqual(report["turns"], 2)
        self.assertEqual(report["losses"], [])
        self.assertEqual(len(report["mappings"]), 12)
        lines = [json.loads(line) for line in data.splitlines()]
        self.assertEqual([line["ordinal"] for line in lines], list(range(len(lines))))
        calls = [
            line["payload"]["call_id"]
            for line in lines
            if line["payload"].get("type") == "function_call"
        ]
        results = [
            line["payload"]["call_id"]
            for line in lines
            if line["payload"].get("type") == "function_call_output"
        ]
        self.assertEqual(calls, results)
        self.assertFalse((self.root / "project" / "hello.txt").exists())

    def test_deterministic_bundle(self):
        first = prepare(self.source)
        self.assertEqual(first, prepare(self.source))
        raw = self.document["sessions"][0]
        before = self.convert()[1]["thread_id"]
        raw["title"] = "Renamed"
        self.assertEqual(before, self.convert()[1]["thread_id"])

    def test_duplicate_call_id_rejected(self):
        raw = self.document["sessions"][0]
        raw["messages"][3]["content"][0]["id"] = "command-1"
        with self.assertRaisesRegex(MigrationError, "Reused tool"):
            self.convert()

    def test_missing_native_result_is_explicit_not_fabricated(self):
        raw = self.document["sessions"][0]
        raw["messages"] = raw["messages"][:2]
        data, report = self.convert()
        self.assertEqual(report["source_counts"]["synthetic_results"], 1)
        self.assertNotIn("tool_results", report["source_counts"])
        self.assertIn("missing_tool_result", {loss["kind"] for loss in report["losses"]})
        self.assertNotIn("commandExecution", report["native_items"])
        self.assertIn(b"result not present in source", data)
        self.assertIn(b"turn_aborted", data)

    def test_unknown_user_attachment_preserves_role(self):
        raw = self.document["sessions"][0]
        raw["messages"] = [
            {
                "role": "user",
                "content": [{"type": "image", "url": "https://example.invalid/image"}],
            },
            {"role": "assistant", "content": "Done"},
        ]
        data, report = self.convert()
        self.assertEqual(report["native_items"]["userMessage"], 1)
        self.assertEqual(report["losses"][0]["kind"], "user_attachment_or_block")
        lines = [json.loads(line) for line in data.splitlines()]
        messages = [line["payload"] for line in lines if line["payload"].get("type") == "message"]
        self.assertEqual(messages[0]["role"], "user")

    def test_mixed_user_results_rejected(self):
        self.document["sessions"][0]["messages"][2]["content"].append(
            {"type": "text", "text": "Next request"}
        )
        with self.assertRaisesRegex(MigrationError, "Mixed user"):
            self.convert()

    def test_original_system_instructions_are_not_activated(self):
        self.document["sessions"][0]["messages"].insert(
            0, {"role": "system", "content": "source-only-instruction"}
        )
        data, report = self.convert()
        self.assertNotIn(b"source-only-instruction", data)
        self.assertEqual(report["losses"][0]["kind"], "historical_instructions")

    def test_unknown_duration_is_not_fabricated(self):
        del self.document["sessions"][0]["messages"][2]["content"][0]["duration_ms"]
        data, _ = self.convert()
        lines = [json.loads(line) for line in data.splitlines()]
        command = next(
            line["payload"]["item"]
            for line in lines
            if line["payload"].get("item", {}).get("type") == "CommandExecution"
        )
        self.assertIsNone(command["duration"])

    def test_invalid_duration_and_exit_code_rejected(self):
        result = self.document["sessions"][0]["messages"][2]["content"][0]
        for invalid in (-1, float("nan"), True, "3"):
            result["duration_ms"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(MigrationError):
                self.convert()
        result["duration_ms"] = 1
        result["exit_code"] = True
        with self.assertRaises(MigrationError):
            self.convert()

    def test_tool_name_alias_retains_original_in_ui(self):
        self.document["sessions"][0]["messages"][3]["content"][0]["name"] = "functions.Read"
        data, _ = self.convert()
        self.assertIn(b"trae_tool_", data)
        self.assertIn(b'"tool":"functions.Read"', data)

    def test_snapshot_prefix_only_no_global_deduplication(self):
        raw = self.document["sessions"][0]
        first = [{"role": "user", "content": "Again"}, {"role": "assistant", "content": "OK"}]
        raw.pop("messages")
        raw.update(
            history_mode="snapshots",
            records=[
                {"messages": first, "created_at": "2026-09-01T10:00:00Z"},
                {"messages": first + first, "created_at": "2026-09-01T10:01:00Z"},
            ],
        )
        self.assertEqual(len(make_session(raw).entries), 4)
        self.assertEqual(make_session(raw).entries[-1]["source_ref"], "1:3")
        raw["history_mode"] = "increments"
        with self.assertRaisesRegex(MigrationError, "Cumulative"):
            make_session(raw)

    def test_divergent_snapshots_rejected(self):
        raw = self.document["sessions"][0]
        raw["history_mode"] = "snapshots"
        raw["records"] = [
            {"messages": [{"role": "user", "content": "a"}]},
            {"messages": [{"role": "user", "content": "b"}]},
        ]
        with self.assertRaisesRegex(MigrationError, "Divergent"):
            make_session(raw)

    def test_repeated_short_messages_not_removed(self):
        raw = self.document["sessions"][0]
        raw["records"] = [{"messages": [{"role": "user", "content": "again"}]}] * 2
        self.assertEqual(len(make_session(raw).entries), 2)

    def test_duplicate_history_identity_checked(self):
        raw = self.document["sessions"][0]
        record = {"history_v2_id": "h", "messages": [{"role": "user", "content": "a"}]}
        raw["records"] = [record, copy.deepcopy(record)]
        self.assertEqual(len(make_session(raw).entries), 1)
        raw["records"][1]["messages"][0]["content"] = "different"
        with self.assertRaisesRegex(MigrationError, "Conflicting"):
            make_session(raw)

    def test_explicit_source_selection(self):
        self.assertEqual(len(load_source(self.source, ["trae-demo-001"])[0]), 1)
        with self.assertRaisesRegex(MigrationError, "not found"):
            load_source(self.source, ["missing"])

    def test_history_mode_override_is_archived(self):
        raw = self.document["sessions"][0]
        first = [{"role": "user", "content": "One"}]
        raw["records"] = [
            {"messages": first},
            {"messages": first + [{"role": "assistant", "content": "Two"}]},
        ]
        self.write_source()
        sessions, _ = load_source(self.source, history_mode="snapshots")
        self.assertEqual(len(sessions[0].entries), 2)
        self.assertEqual(sessions[0].raw["history_mode"], "snapshots")

    def test_openai_style_tool_pair(self):
        self.document["sessions"][0]["messages"] = [
            {"role": "user", "content": "Read the data"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "openai-call",
                        "function": {"name": "Read", "arguments": '{"path":"data"}'},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "openai-call", "content": "result"},
            {"role": "assistant", "content": "Done"},
        ]
        _, report = self.convert()
        self.assertEqual(report["turns"], 1)
        self.assertEqual(report["native_items"]["dynamicToolCall"], 1)
        self.assertEqual(report["losses"], [])

    def test_no_cwd_guessing(self):
        raw = self.document["sessions"][0]
        raw.pop("cwd")
        with self.assertRaisesRegex(MigrationError, "cwd"):
            make_session(raw)
        session = make_session(raw, str(self.root))
        self.assertEqual(session.cwd, str(self.root))

    def test_timestamp_units_and_timezone(self):
        self.assertEqual(timestamp(1788256800), timestamp(1788256800000))
        self.assertEqual(timestamp("2026-09-01T18:00:00+08:00"), timestamp(1788256800))
        for bad in (True, None, "2026-09-01T10:00:00", "invalid"):
            with self.subTest(bad=bad), self.assertRaises(MigrationError):
                timestamp(bad)

    def test_reordered_timestamps_rejected(self):
        self.document["sessions"][0]["messages"][1]["timestamp"] = "2025-09-01T10:00:00Z"
        with self.assertRaisesRegex(MigrationError, "chronological"):
            self.convert()

    def test_readonly_sqlite_source(self):
        path, connection = self.create_db()
        connection.close()
        before = path.read_bytes()
        sessions, _ = load_source(path, cwd=str(self.root))
        self.assertEqual(sessions[0].source_id, "one")
        self.assertEqual(len(sessions[0].entries), 2)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list_source(path)[0]["title"], "Test")

    def test_sessions_without_history_are_reported(self):
        path, connection = self.create_db()
        connection.execute("INSERT INTO chat_session VALUES (2, 'empty', 'Empty', 1788256800)")
        connection.commit()
        connection.close()
        report = prepare(path, cwd=str(self.root))[2]
        self.assertEqual(report["skipped_sessions"][0]["source_id"], "empty")

    def test_wal_records_not_lost(self):
        path, connection = self.create_db()
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "INSERT INTO history_v2 VALUES (2, 'h2', 'one', ?, 1788256802)",
            (encode({"raw_messages": [{"role": "user", "content": "WAL record"}]}),),
        )
        connection.commit()
        self.assertTrue(probe(path)["wal_present"])
        self.assertEqual(len(load_source(path, cwd=str(self.root))[0][0].entries), 3)

    def test_unsupported_schema_fails(self):
        path = self.root / "empty.db"
        # sqlite3.Connection's context manager ends a transaction, not the connection.
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE unrelated (a TEXT)")
        with self.assertRaisesRegex(MigrationError, "schema"):
            load_source(path, cwd=str(self.root))

    def test_unsupported_schema_closes_connections_before_cleanup(self):
        path = self.root / "empty.db"
        connections = []
        original_connect = sqlite3.connect

        def track_connection(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connections.append(connection)
            self.addCleanup(connection.close)
            return connection

        # Holding references prevents GC from hiding a connection leak on POSIX.
        with patch("sqlite3.connect", side_effect=track_connection):
            self.test_unsupported_schema_fails()
        self.assertEqual(len(connections), 2)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        path.unlink()

    def test_encrypted_input_is_not_treated_as_empty_database(self):
        path = self.root / "encrypted.db"
        path.write_bytes(b"x" * 4096)
        self.assertEqual(probe(path)["kind"], "encrypted-or-unknown")
        with self.assertRaisesRegex(MigrationError, "Encrypted"):
            load_source(path, cwd=str(self.root))
        with (
            patch.dict(os.environ, {"MIGRATION_TEST_KEY": "not-valid"}),
            self.assertRaisesRegex(MigrationError, "64 hex"),
            connect_readonly(path, "MIGRATION_TEST_KEY"),
        ):
            pass

    def test_secret_gate_does_not_print_secret(self):
        secret = "sk-" + "A" * 30
        self.document["sessions"][0]["messages"][0]["content"] = secret
        self.write_source()
        output = self.root / "sensitive"
        with self.assertRaises(MigrationError) as raised:
            build_bundle(self.source, output)
        self.assertNotIn(secret, str(raised.exception))
        self.assertFalse(output.exists())
        build_bundle(self.source, output, accept_sensitive=True)
        self.assertGreater(read_bundle(output)[2]["sessions"][0]["sensitive_candidates"], 0)

    def test_duplicate_json_keys_and_nan_rejected(self):
        for text in ('{"format":1,"format":2}', '{"a":NaN}'):
            self.source.write_text(text, encoding="utf-8")
            with self.assertRaises(MigrationError):
                load_json(self.source)

    def test_private_atomic_exclusive_file(self):
        path = self.root / "private"
        write_private(path, b"first")
        with self.assertRaises(FileExistsError):
            write_private(path, b"second")
        self.assertEqual(path.read_bytes(), b"first")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(self.root.glob(".trae2codex-*")), [])

    def test_path_traversal_and_symlinks(self):
        for path in (
            "../outside",
            "/absolute",
            "sub/../../file",
            "C:/elsewhere",
            "a\\b",
            ".",
            "\x00",
        ):
            with self.subTest(path=path), self.assertRaises(MigrationError):
                safe_child(self.root, path)
        link = self.root / "link"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError:
            return
        with self.assertRaisesRegex(MigrationError, "Symlinks"):
            safe_child(self.root, "link/file")

    def test_bundle_tampering_rejected(self):
        bundle = self.bundle()
        _, manifest, _ = read_bundle(bundle)
        path = bundle / manifest["threads"][0]["rollout"]
        with path.open("ab") as stream:
            stream.write(b"bad")
        with self.assertRaisesRegex(MigrationError, "checksum"):
            read_bundle(bundle)

    def test_rechecksummed_arbitrary_rollout_rejected(self):
        bundle = self.bundle()
        _, manifest, _ = read_bundle(bundle)
        relative = manifest["threads"][0]["rollout"]
        file = bundle / relative
        file.write_bytes(file.read_bytes() + b'{"type":"arbitrary"}\n')
        manifest["files"][relative] = digest(file.read_bytes())
        manifest["bundle_id"] = fingerprint(
            {key: value for key, value in manifest.items() if key != "bundle_id"}
        )
        (bundle / "manifest.json").write_text(encode(manifest), encoding="utf-8")
        with self.assertRaisesRegex(MigrationError, "reproducible"):
            read_bundle(bundle)

    def test_output_exists_never_overwritten(self):
        bundle = self.bundle()
        with self.assertRaisesRegex(MigrationError, "already exists"):
            build_bundle(self.source, bundle)

    def test_install_dry_run_writes_nothing(self):
        bundle = self.bundle()
        home = self.root / "codex"
        self.assertEqual(install_bundle(bundle, home, "not-installed")["status"], "dry_run")
        self.assertFalse(home.exists())

    def test_loss_requires_explicit_acknowledgement(self):
        self.document["sessions"][0]["messages"].insert(
            0, {"role": "system", "content": "Old rules"}
        )
        self.write_source()
        bundle = self.bundle()
        with self.assertRaisesRegex(MigrationError, "allow-loss"):
            install_bundle(bundle, self.root / "codex", "codex")
        self.assertEqual(
            install_bundle(bundle, self.root / "codex", "codex", allow_loss=True)["status"],
            "dry_run",
        )

    @patch("trae2codex.verify.verify_bundle", return_value={"status": "passed"})
    def test_install_idempotence_and_guarded_rollback(self, verify):
        bundle = self.bundle()
        _, manifest, _ = read_bundle(bundle)
        home = self.root / "codex"
        self.assertEqual(install_bundle(bundle, home, "codex", apply=True)["status"], "installed")
        self.assertEqual(
            install_bundle(bundle, home, "codex", apply=True)["status"], "already_installed"
        )
        unrelated = home / "user-file"
        unrelated.write_text("untouched", encoding="utf-8")
        self.assertEqual(rollback(home, manifest["bundle_id"])["status"], "dry_run")
        file = home / manifest["threads"][0]["rollout"]
        with file.open("ab") as stream:
            stream.write(b"new conversation")
        with self.assertRaisesRegex(MigrationError, "changed"):
            rollback(home, manifest["bundle_id"], apply=True)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "untouched")

    @patch("trae2codex.verify.verify_bundle", return_value={"status": "passed"})
    def test_rollback_removes_only_own_unchanged_files(self, verify):
        bundle = self.bundle()
        _, manifest, _ = read_bundle(bundle)
        home = self.root / "codex"
        install_bundle(bundle, home, "codex", apply=True)
        unrelated = home / "keep"
        unrelated.write_bytes(b"keep")
        self.assertEqual(rollback(home, manifest["bundle_id"], apply=True)["status"], "rolled_back")
        self.assertFalse((home / manifest["threads"][0]["rollout"]).exists())
        self.assertEqual(unrelated.read_bytes(), b"keep")
        install_bundle(bundle, home, "codex", apply=True)
        self.assertEqual(rollback(home, manifest["bundle_id"], apply=True)["status"], "rolled_back")
        self.assertEqual(len(list((home / ".trae2codex/receipts").glob("*.rolled-back-*.json"))), 2)

    @patch("trae2codex.verify.verify_bundle", return_value={"status": "passed"})
    def test_archived_or_compressed_uuid_collision_rejected(self, verify):
        bundle = self.bundle()
        _, manifest, _ = read_bundle(bundle)
        home = self.root / "codex"
        archived = home / "archived_sessions"
        archived.mkdir(parents=True)
        file = archived / ("rollout-" + manifest["threads"][0]["id"] + ".jsonl.zst")
        file.write_bytes(b"archived data")
        with self.assertRaisesRegex(MigrationError, "already exists"):
            install_bundle(bundle, home, "codex", apply=True)
        self.assertEqual(file.read_bytes(), b"archived data")

    @patch("trae2codex.verify.verify_bundle", side_effect=MigrationError("failed test"))
    def test_verification_failure_leaves_target_absent(self, verify):
        bundle = self.bundle()
        home = self.root / "codex"
        with self.assertRaises(MigrationError):
            install_bundle(bundle, home, "codex", apply=True)
        self.assertFalse(home.exists())

    @patch("trae2codex.verify.verify_bundle", return_value={"status": "passed"})
    def test_partial_install_can_be_retried(self, verify):
        second = copy.deepcopy(self.document["sessions"][0])
        second["session_id"] = "second-session"
        self.document["sessions"].append(second)
        self.write_source()
        bundle = self.bundle()
        home = self.root / "codex"
        original_write = write_private
        calls = []

        def fail_second(path, data):
            if Path(path).suffix == ".jsonl":
                calls.append(path)
                if len(calls) == 2:
                    raise OSError("simulated disk failure")
            original_write(path, data)

        with (
            patch("trae2codex.bundle.write_private", side_effect=fail_second),
            self.assertRaises(OSError),
        ):
            install_bundle(bundle, home, "codex", apply=True)
        self.assertEqual(len(list(home.glob("sessions/**/*.jsonl"))), 1)
        self.assertEqual(install_bundle(bundle, home, "codex", apply=True)["status"], "installed")
        self.assertEqual(len(list(home.glob("sessions/**/*.jsonl"))), 2)

    def test_exclusive_migration_lock(self):
        home = self.root / "codex"
        with home_lock(home), self.assertRaisesRegex(MigrationError, "lock"), home_lock(home):
            pass
        self.assertFalse((home / ".trae2codex/lock").exists())

    def test_context_checks_match_order_and_duplicates(self):
        message = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "same"}],
        }
        rollout = [{"type": "response_item", "payload": message}] * 2
        self.assertEqual(check_context(rollout, {"input": [message, message]})["messages"], 2)
        with self.assertRaises(MigrationError):
            check_context(rollout, {"input": [message]})

    def test_isolated_child_environment_contains_no_credentials(self):
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "private", "TRAE_DB_KEY": "private", "HTTPS_PROXY": "private"},
        ):
            env = isolated_env(self.root)
            for key in ("OPENAI_API_KEY", "TRAE_DB_KEY", "HTTPS_PROXY"):
                self.assertNotIn(key, env)
            self.assertEqual(env["HOME"], str(self.root))

    def test_cli_failure_uses_json_without_traceback(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            code = main(
                [
                    "convert",
                    "--source",
                    str(self.root / "missing"),
                    "--output",
                    str(self.root / "bundle"),
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("error", json.loads(error.getvalue()))
        self.assertNotIn("Traceback", error.getvalue())


if __name__ == "__main__":
    unittest.main()
