import copy
import os
import tempfile
import unittest
from pathlib import Path

from trae2codex.bundle import build_bundle, install_bundle, read_bundle, rollback
from trae2codex.common import encode, load_json
from trae2codex.verify import verify_bundle

ROOT = Path(__file__).resolve().parents[1]
CODEX = os.environ.get("TRAE2CODEX_TEST_CODEX")


@unittest.skipUnless(CODEX, "Set TRAE2CODEX_TEST_CODEX to the native Codex 0.154.0 executable")
class CodexIntegrationTests(unittest.TestCase):
    def test_real_codex_native_history_restart_context_install_and_rollback(self):
        with tempfile.TemporaryDirectory(prefix="trae2codex-integration-") as temporary:
            root = Path(temporary).resolve()
            source = load_json(ROOT / "examples/session.json")
            session = source["sessions"][0]
            session["cwd"] = str(root / "project")
            more = copy.deepcopy(session)
            more["session_id"] = "native-errors-and-rename"
            more["messages"][6]["content"][0]["is_error"] = True
            more["messages"][6]["content"][0]["content"] = "Synthetic MCP failure"
            more["messages"][7]["content"][0]["changes"] = {
                "before.txt": {
                    "type": "update",
                    "unified_diff": "@@ -1 +1 @@\n-before\n+after",
                    "move_path": "after.txt",
                },
                "deleted.txt": {"type": "delete", "content": "removed\n"},
            }
            more["messages"][8]["content"][0]["success"] = False
            more["messages"][8]["content"][0]["is_error"] = True
            del more["messages"][4]["content"][0]["duration_ms"]
            source["sessions"].append(more)
            input_file = root / "source.json"
            input_file.write_text(encode(source), encoding="utf-8")
            bundle = root / "bundle"
            build_bundle(input_file, bundle)
            original_files = {
                p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()
            }
            result = verify_bundle(bundle, CODEX)
            self.assertEqual(result["status"], "passed")
            self.assertEqual(len(result["sessions"]), 2)
            for session_result in result["sessions"]:
                self.assertTrue(session_result["resumed_after_restart"])
                self.assertTrue(session_result["offline_continuation"])
                self.assertEqual(session_result["model_context_checks"]["tool_calls"], 4)
            self.assertEqual(
                original_files,
                {p.relative_to(bundle): p.read_bytes() for p in bundle.rglob("*") if p.is_file()},
            )
            target = root / "installed"
            self.assertEqual(
                install_bundle(bundle, target, CODEX, apply=True)["status"], "installed"
            )
            _, manifest, _ = read_bundle(bundle)
            self.assertEqual(
                rollback(target, manifest["bundle_id"], apply=True)["status"], "rolled_back"
            )
            self.assertEqual(list(target.glob("sessions/**/*.jsonl")), [])
            self.assertFalse((root / "project").exists())


if __name__ == "__main__":
    unittest.main()
