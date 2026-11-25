"""Real stdio JSON-RPC integration tests across all 8 agy-memory MCP tools."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def _seed_fake_antigravity_env(tmp_path: Path) -> tuple[Path, Path, Path]:
    db_path = tmp_path / "agy_memory.db"
    brain_dir = tmp_path / "brain"
    cid = "11111111-2222-3333-4444-555555555555"
    log_dir = brain_dir / cid / ".system_generated" / "logs"
    log_dir.mkdir(parents=True)

    # Create a 14-turn transcript so uncapped brief + verbatim recall can be verified
    lines = []
    step = 0
    for turn in range(1, 15):
        u_txt = f"<USER_REQUEST>Turn {turn}: design api endpoint v{turn}</USER_REQUEST>"
        if turn == 2:
            u_txt = "<USER_REQUEST>Turn 2: reject embedded server LLM, expose MCP only.</USER_REQUEST>"
        lines.append(json.dumps({"step_index": step, "type": "USER_INPUT", "created_at": "2026-11-25T08:00:00Z", "content": u_txt}))
        step += 1
        lines.append(
            json.dumps(
                {
                    "step_index": step,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-25T08:01:00Z",
                    "content": f"Implemented v{turn} endpoint and verified schema.",
                    "tool_calls": [{"name": "write_file", "args": {"Path": f"/repo/api/v{turn}.py"}}],
                }
            )
        )
        step += 1
    lines.append(json.dumps({"step_index": step, "type": "CHECKPOINT", "created_at": "2026-11-25T08:30:00Z", "content": "# Resuming"}))
    raw = "\n".join(lines) + "\n"
    (log_dir / "transcript.jsonl").write_text(raw, encoding="utf-8")
    (log_dir / "transcript_full.jsonl").write_text(raw, encoding="utf-8")

    # Create synthetic Antigravity conversation_summaries.db matching native schema
    summaries_db = tmp_path / "conversation_summaries.db"
    sc = sqlite3.connect(str(summaries_db))
    sc.executescript(
        """
        CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            workspace_uris TEXT NOT NULL DEFAULT '[]',
            step_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE VIRTUAL TABLE conversations_fts USING fts5(
            conversation_id UNINDEXED,
            chunk_index UNINDEXED,
            chunk_first_step UNINDEXED,
            chunk_last_step UNINDEXED,
            last_modified_time UNINDEXED,
            step_count UNINDEXED,
            last_step_created_at UNINDEXED,
            archived UNINDEXED,
            user_step_byte_offsets UNINDEXED,
            agent_step_byte_offsets UNINDEXED,
            title,
            workspace,
            project,
            user_body,
            agent_body,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    sc.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?)",
        (cid, "API Gateway Architecture", '["file:///c%3A/demo-repo"]', len(lines)),
    )
    # Intentionally leave FTS title/workspace blank to mirror Antigravity 2.0 real behavior
    sc.execute(
        "INSERT INTO conversations_fts VALUES (?, 0, 0, ?, '2026-11-25T08:30:00Z', ?, '2026-11-25T08:30:00Z', 0, '', '', '', '', '', ?, ?)",
        (cid, len(lines) - 1, len(lines), "reject embedded server LLM expose MCP only", "Implemented v14 endpoint"),
    )
    sc.commit()
    sc.close()
    return db_path, brain_dir, summaries_db


def test_all_8_mcp_tools_over_stdio(tmp_path: Path) -> None:
    db_path, brain_dir, summaries_db = _seed_fake_antigravity_env(tmp_path)
    env = dict(os.environ)
    env["AGY_MEMORY_DB"] = str(db_path)
    env["AGY_BRAIN_DIR"] = str(brain_dir)
    env["AGY_SUMMARIES_DB"] = str(summaries_db)

    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-m", "agy_memory.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert proc.stdin and proc.stdout

    def rpc(msg: dict) -> dict | None:
        proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        if "id" not in msg:
            return None
        line = proc.stdout.readline()
        return json.loads(line)

    try:
        init = rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1.0"},
                },
            }
        )
        assert init and "result" in init
        rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})

        tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {t["name"] for t in tools["result"]["tools"]}
        assert names == {
            "pin_decision",
            "list_decisions",
            "retire_decision",
            "search_sessions",
            "get_session_brief",
            "search_transcript",
            "get_transcript_steps",
            "memory_status",
        }

        def call(cid_num: int, name: str, arguments: dict) -> dict:
            resp = rpc({"jsonrpc": "2.0", "id": cid_num, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
            return json.loads(resp["result"]["content"][0]["text"])

        # 1. pin_decision (workspace + session-private + missing-cid error)
        p_ws = call(3, "pin_decision", {"title": "MCP only", "content": "No embedded LLM in backend", "category": "veto", "scope": "c:/demo-repo"})
        assert p_ws["operation"] == "created"

        p_err = call(4, "pin_decision", {"title": "Bad", "content": "No cid", "scope": "session"})
        assert p_err["ok"] is False

        p_sess = call(5, "pin_decision", {"title": "Private spec", "content": "16 slides max", "scope": "session", "conversation_id": "11111111"})
        assert p_sess["scope"] == "session:11111111"

        # 2. list_decisions isolation
        l_ws = call(6, "list_decisions", {"scope": "c:/demo-repo"})
        assert l_ws["count"] == 1
        l_sess = call(7, "list_decisions", {"scope": "session", "conversation_id": "11111111"})
        assert l_sess["count"] == 1 and l_sess["decisions"][0]["title"] == "Private spec"

        # 3. search_sessions (joins conversation_summaries for URL-encoded workspace_uris!)
        sr = call(8, "search_sessions", {"query": "embedded", "workspace": "c:/demo-repo"})
        assert len(sr["decisions"]) >= 1
        assert len(sr["sessions"]) == 1
        assert sr["sessions"][0]["title"] == "API Gateway Architecture"

        # 4. get_session_brief (must return all 14 user turns, uncapped!)
        brief = call(9, "get_session_brief", {"conversation_id": "11111111"})
        assert brief["user_turn_count"] == 14
        assert len(brief["all_user_requests"]) == 14

        # 5. search_transcript + get_transcript_steps
        hits = call(10, "search_transcript", {"conversation_id": "11111111", "query": "embedded server LLM"})
        assert hits["hit_count"] >= 1
        step_num = hits["hits"][0]["step"]
        expanded = call(11, "get_transcript_steps", {"conversation_id": "11111111", "start_step": step_num, "end_step": step_num + 1})
        assert len(expanded["steps"]) == 2

        # 6. retire_decision
        ret = call(12, "retire_decision", {"decision_id": p_ws["id"], "reason": "test done"})
        assert ret["ok"] is True and ret["active"] == 0

        # 7. memory_status
        st = call(13, "memory_status", {})
        assert st["summaries_db_exists"] is True
        assert st["active_live_decisions"] == 1  # only p_sess remains active
    finally:
        proc.terminate()
        proc.wait(timeout=5)
