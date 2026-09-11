import argparse
import json
import shutil
import sys
from pathlib import Path

from . import CODEX_VERSION, __version__
from .bundle import build_bundle, install_bundle, prepare, rollback
from .common import MigrationError
from .source import discover, list_source, probe
from .verify import get_binary, verify_bundle


def parser():
    root = argparse.ArgumentParser(
        prog="trae2codex",
        description="Local TRAE to Codex migration. No uploads, credential extraction, or historical command replay.",
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command")
    doctor = commands.add_parser("doctor", help="Discover local sources and check prerequisites (read-only)")
    doctor.add_argument("--source")
    doctor.add_argument("--codex", default="codex", help="Codex executable, pinned to " + CODEX_VERSION)
    listing = commands.add_parser("list", help="List source session IDs (read-only)")
    listing.add_argument("--source", required=True)
    listing.add_argument("--key-env", help="Name of environment variable containing an authorized SQLCipher raw hex key")
    for name in ("plan", "convert"):
        cmd = commands.add_parser(name, help="Inspect conversion without writing" if name == "plan" else "Build a portable migration bundle")
        cmd.add_argument("--source", required=True, help="TRAE user-data root, database.db, or structured JSON export")
        cmd.add_argument("--session", action="append", dest="session_ids", help="Source session ID; repeat to select multiple")
        cmd.add_argument("--cwd", help="Absolute local project path override; required when database has no cwd")
        cmd.add_argument("--key-env", help="Environment variable name, never the key itself")
        if name == "convert":
            cmd.add_argument("--output", required=True, help="New directory, never an existing Codex home")
            cmd.add_argument("--accept-sensitive", action="store_true", help="Explicitly preserve detected possible secrets in the private local bundle")
    verify = commands.add_parser("verify", help="Test with real Codex and an offline loopback model, in isolation")
    verify.add_argument("bundle")
    verify.add_argument("--codex", default="codex")
    install = commands.add_parser("install", help="Dry-run by default; verify then install native rollouts")
    install.add_argument("bundle")
    install.add_argument("--codex-home", required=True, help="Explicit target Codex data directory")
    install.add_argument("--codex", default="codex")
    install.add_argument("--apply", action="store_true", help="Write after verification; close Codex first")
    install.add_argument("--allow-loss", action="store_true", help="Acknowledge reviewed lossy mappings in report.json")
    undo = commands.add_parser("rollback", help="Remove only unchanged imported rollouts; dry-run by default")
    undo.add_argument("--codex-home", required=True)
    undo.add_argument("--bundle-id", required=True)
    undo.add_argument("--apply", action="store_true", help="Close Codex before removing imported rollouts")
    return root


def run(args):
    if args.command == "doctor":
        sources = [args.source] if args.source else discover()
        codex = {"required_version": CODEX_VERSION, "available": False}
        try:
            codex.update({"available": True, "path": str(get_binary(args.codex))})
        except MigrationError as error:
            codex.update({"available": False, "message": str(error)})
        return {
            "sources": [probe(source) for source in sources], "codex": codex,
            "python": sys.version.split()[0],
            "note": "Encrypted sources need an authorized key/export. Discovery does not read conversations.",
        }
    if args.command == "list":
        return list_source(args.source, args.key_env)
    if args.command in ("plan", "convert"):
        options = {"session_ids": args.session_ids, "cwd": args.cwd, "key_env": args.key_env}
        if args.command == "plan":
            return prepare(args.source, **options)[2]
        return build_bundle(args.source, args.output, accept_sensitive=args.accept_sensitive, **options)
    if args.command == "verify":
        return verify_bundle(args.bundle, args.codex)
    if args.command == "install":
        return install_bundle(args.bundle, args.codex_home, args.codex, args.apply, args.allow_loss)
    if args.command == "rollback":
        return rollback(args.codex_home, args.bundle_id, args.apply)
    raise MigrationError("Unknown command.")


def main(argv=None):
    command_parser = parser()
    args = command_parser.parse_args(argv)
    if args.command is None:
        command_parser.print_help()
        return 0
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
        return 0
    except MigrationError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError) as error:
        # Never include source values or SQL statements in a public error message.
        print(json.dumps({"error": "Operation failed (%s). Check paths, permissions, and the documented source schema."
                          % type(error).__name__}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('{"error":"Interrupted. No historical commands were replayed."}', file=sys.stderr)
        return 130
