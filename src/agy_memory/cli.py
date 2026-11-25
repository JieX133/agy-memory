"""CLI for installing, diagnosing, and inspecting agy-memory."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, store


def _default_hooks_path() -> Path:
    return Path.home() / ".gemini" / "config" / "hooks.json"


def _default_mcp_path() -> Path:
    shared = Path.home() / ".agent-runtime" / "mcp" / "mcp.json"
    if shared.exists():
        return shared
    return Path.home() / ".gemini" / "config" / "mcp_config.json"


def _backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, bak)
    return bak


def _load_json_dict(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8-sig").strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _hook_cmd(event: str) -> str:
    py = sys.executable.replace("\\", "/")
    return f'"{py}" -X utf8 -m agy_memory.hook {event}'


def cmd_install(args: argparse.Namespace) -> int:
    hooks_path = Path(args.hooks_config).expanduser() if args.hooks_config else _default_hooks_path()
    hooks_data = _load_json_dict(hooks_path)
    bak_h = _backup_file(hooks_path)
    hooks_data["memory-guard"] = {
        "PreInvocation": [{"type": "command", "command": _hook_cmd("PreInvocation"), "timeout": 5}],
        "Stop": [{"type": "command", "command": _hook_cmd("Stop"), "timeout": 5}],
    }
    _write_json(hooks_path, hooks_data)
    print(f"[OK] Registered memory-guard hooks in {hooks_path}" + (f" (backup: {bak_h.name})" if bak_h else ""))

    if not args.hooks_only:
        mcp_path = Path(args.mcp_config).expanduser() if args.mcp_config else _default_mcp_path()
        mcp_data = _load_json_dict(mcp_path)
        bak_m = _backup_file(mcp_path)
        servers = mcp_data.setdefault("mcpServers", {})
        entry: dict = {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-X", "utf8", "-m", "agy_memory.mcp_server"],
            "description": "Local lossless session memory and compaction recovery (SQLite FTS5)",
        }
        if args.lang:
            entry["env"] = {"AGY_MEMORY_LANG": args.lang}
        servers["agy-memory"] = entry
        _write_json(mcp_path, mcp_data)
        print(f"[OK] Registered agy-memory MCP server in {mcp_path}" + (f" (backup: {bak_m.name})" if bak_m else ""))

    conn = store.connect_db()
    conn.close()
    print(f"[OK] SQLite WAL + FTS5 store ready at {store.get_db_path()}")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    hooks_path = Path(args.hooks_config).expanduser() if args.hooks_config else _default_hooks_path()
    if hooks_path.exists():
        data = _load_json_dict(hooks_path)
        if "memory-guard" in data:
            _backup_file(hooks_path)
            del data["memory-guard"]
            _write_json(hooks_path, data)
            print(f"[OK] Removed memory-guard from {hooks_path}")

    mcp_path = Path(args.mcp_config).expanduser() if args.mcp_config else _default_mcp_path()
    if mcp_path.exists():
        data = _load_json_dict(mcp_path)
        servers = data.get("mcpServers")
        if isinstance(servers, dict) and "agy-memory" in servers:
            _backup_file(mcp_path)
            del servers["agy-memory"]
            _write_json(mcp_path, data)
            print(f"[OK] Removed agy-memory from {mcp_path}")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    fts5_ok = False
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE _t USING fts5(x)")
        fts5_ok = True
    except Exception:
        pass
    mcp_ok = False
    try:
        import mcp  # noqa: F401
        mcp_ok = True
    except Exception:
        pass

    hooks_path = _default_hooks_path()
    hook_wired = False
    if hooks_path.exists():
        try:
            hook_wired = "memory-guard" in _load_json_dict(hooks_path)
        except Exception:
            pass

    db_path = store.get_db_path()
    conn = store.connect_db(db_path)
    now_iso = store.utc_now()
    sess_cnt = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    dec_live = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE active=1 AND (expires_at='' OR expires_at>=?)", (now_iso,)
    ).fetchone()[0]
    hooks_recent = [
        dict(r)
        for r in conn.execute(
            "SELECT ts, event, action, elapsed_ms FROM hook_events ORDER BY id DESC LIMIT 5"
        ).fetchall()
    ]
    conn.close()

    report = {
        "version": __version__,
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "fts5": fts5_ok,
        "mcp_sdk": mcp_ok,
        "hooks_config": str(hooks_path),
        "memory_guard_wired": hook_wired,
        "db_path": str(db_path),
        "brain_dir_exists": store.get_brain_dir().exists(),
        "summaries_db_exists": store.get_summaries_db_path().exists(),
        "tracked_sessions": sess_cnt,
        "active_decisions": dec_live,
        "recent_hooks": hooks_recent,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="agy-memory", description="Antigravity compaction guard & local memory CLI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_inst = sub.add_parser("install", help="Register memory-guard hooks and agy-memory MCP server")
    p_inst.add_argument("--hooks-config", default="", help="Path to hooks.json (default: ~/.gemini/config/hooks.json)")
    p_inst.add_argument("--mcp-config", default="", help="Path to MCP config JSON")
    p_inst.add_argument("--hooks-only", action="store_true", help="Install hooks only (skip MCP registration)")
    p_inst.add_argument("--lang", choices=["en", "zh"], default="", help="Optional language for injected headers")
    p_inst.set_defaults(func=cmd_install)

    p_un = sub.add_parser("uninstall", help="Remove memory-guard hooks and agy-memory MCP entry")
    p_un.add_argument("--hooks-config", default="")
    p_un.add_argument("--mcp-config", default="")
    p_un.set_defaults(func=cmd_uninstall)

    p_doc = sub.add_parser("doctor", help="Inspect runtime, SQLite FTS5, hook registration, and recent events")
    p_doc.set_defaults(func=cmd_doctor)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
