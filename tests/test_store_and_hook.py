"""Unit & lifecycle hook integration tests for agy-memory."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agy_memory import hook, store


def _make_line(step: int, stype: str, content: str = "", **extra) -> str:
    obj = {
        "step_index": step,
        "type": stype,
        "created_at": "2026-11-25T10:00:00Z",
        "content": content,
    }
    obj.update(extra)
    return json.dumps(obj, ensure_ascii=False)


def _build_multi_turn_transcript(path: Path, include_cp: bool = True, hit_warn: bool = True) -> None:
    lines: list[str] = []
    step = 0
    for turn in range(1, 16):
        u_text = f"<USER_REQUEST>\nTurn {turn}: implement module_{turn}."
        if turn == 2:
            u_text += " Never embed a server-side LLM; only expose local agent MCP endpoints."
        elif turn == 3:
            u_text += " Do not add marketing subtitles under H1 headings."
        elif turn == 14:
            u_text += " Should we keep AG Grid or drop it?"
        elif turn == 15:
            u_text += " What about Skill upload UX?"
        u_text += "\n</USER_REQUEST>"
        lines.append(_make_line(step, "USER_INPUT", u_text))
        step += 1

        # Intermediate tool call step
        in_tok = 440_000 if (hit_warn and turn == 15) else 20_000 * turn
        lines.append(
            _make_line(
                step,
                "PLANNER_RESPONSE",
                "",
                input_tokens=in_tok,
                cache_read_tokens=5_000,
                tool_calls=[
                    {"name": "edit_file", "args": {"Path": f"/repo/src/components/mod_{turn}.tsx"}},
                    {"name": "read_file", "args": {"Path": "/repo/src/api/routes.py"}},
                ],
            )
        )
        step += 1

        # Final user-facing assistant reply (no tool_calls)
        if turn == 14:
            reply = (
                "Recommendation after comparing both options:\n"
                "1. **Keep AG Grid Community** as the spreadsheet replacement grid.\n"
                "2. **Option A (Slate Cobalt)**: high-contrast neutral palette."
            )
        elif turn == 15:
            reply = (
                "Confirmed gap in the Skills view:\n"
                "- **Human upload**: drag-and-drop `.zip` or inline `SKILL.md` editor.\n"
                "- **Agent upload**: expose `POST /api/skills/publish` over MCP."
            )
        else:
            reply = f"Completed **module_{turn}** and verified unit tests."
        lines.append(_make_line(step, "PLANNER_RESPONSE", reply, input_tokens=in_tok, cache_read_tokens=5_000))
        step += 1

    if include_cp:
        lines.append(
            _make_line(
                step,
                "CHECKPOINT",
                "# Resuming from a compaction\n# User Requests\n1. Turn 6...\n<summary>## 1. Task Overview</summary>",
                output_tokens=6200,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_question_filter_and_veto_precision() -> None:
    questions = [
        "Can we improve context quality?",
        "Why not embed an LLM?",
        "能不能提升你的上下文水平？",
        "这个文件不用管吗？",
        "为什么不能这样？",
    ]
    for q in questions:
        assert store.extract_veto_clauses(q) == [], f"False positive on question: {q}"

    vetoes = [
        "Never commit secrets into git.",
        "Do not add subtitles under H1 titles.",
        "不要加入二级小字。",
        "取消默认排会，只提供开放接口。",
    ]
    for v in vetoes:
        assert len(store.extract_veto_clauses(v)) >= 1, f"Missed veto: {v}"


def test_ttl_expiry_and_conflict_detection(tmp_path: Path) -> None:
    db = tmp_path / "mem.db"
    conn = store.connect_db(db)
    try:
        r1 = store.pin_decision_record(
            conn,
            title="Zero server-side embedded LLM",
            content="Backend exposes deterministic MCP endpoints only; never embed server-side LLM calls.",
            category="veto",
            scope="/workspace/app",
            ttl_days=14,
        )
        assert r1["operation"] == "created"
        assert r1["expires_at"] != "never"

        # Insert an already-expired decision
        conn.execute(
            "INSERT INTO decisions(scope, category, title, content, active, expires_at, created_at, updated_at) "
            "VALUES ('/workspace/app', 'decision', 'Expired rule', 'Old temporary rule', 1, '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
        )
        conn.commit()
        active = store.get_active_decisions(conn, "/workspace/app", exclude_expired=True)
        assert all(d["title"] != "Expired rule" for d in active)

        # Conflicting sibling should trigger potential_conflicts
        r2 = store.pin_decision_record(
            conn,
            title="Switch to server-side embedded LLM",
            content="Add server-side embedded LLM calls inside backend routes.",
            category="decision",
            scope="/workspace/app",
        )
        assert r2.get("potential_conflicts")
        assert r2["potential_conflicts"][0]["id"] == r1["id"]
    finally:
        conn.close()


def test_parallel_thread_session_scope_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "mem.db"
    monkeypatch.setenv("AGY_MEMORY_DB", str(db))
    monkeypatch.setenv("AGY_MEMORY_LANG", "en")

    conn = store.connect_db(db)
    with pytest.raises(ValueError):
        store.pin_decision_record(conn, title="Missing cid", content="must fail", scope="session")

    store.pin_decision_record(
        conn,
        title="Workspace shared rule",
        content="All API responses use camelCase.",
        category="architecture",
        scope="/workspace/demo",
    )
    store.pin_decision_record(
        conn,
        title="Deck A layout lock",
        content="Keep slide count strictly at 16 with 36px serif headings.",
        category="decision",
        scope="session",
        source_conversation_id="thread-A",
    )
    conn.close()

    # Parallel thread B in the SAME workspace must never see thread A's private pin
    tb = tmp_path / "thread_b.jsonl"
    tb.write_text(_make_line(0, "USER_INPUT", "<USER_REQUEST>Work on backend tests</USER_REQUEST>") + "\n", encoding="utf-8")
    out_b0 = hook.handle_hook(
        {"conversationId": "thread-B", "workspacePaths": ["/workspace/demo"], "transcriptPath": str(tb)},
        forced_event="PreInvocation",
        db_path=db,
    )
    b0_txt = out_b0["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "Workspace shared rule" in b0_txt
    assert "cid=thread-B" in b0_txt
    assert "36px serif" not in b0_txt

    # Thread B compacts -> still 0 leak from Thread A
    _build_multi_turn_transcript(tb, include_cp=True)
    out_bcp = hook.handle_hook(
        {"conversationId": "thread-B", "workspacePaths": ["/workspace/demo"], "transcriptPath": str(tb)},
        forced_event="PreInvocation",
        db_path=db,
    )
    bcp_txt = out_bcp["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "36px serif" not in bcp_txt

    # Thread A compacts -> recovers its own private pin AND recent Q&A replies AND evicted turn #2!
    ta = tmp_path / "thread_a.jsonl"
    _build_multi_turn_transcript(ta, include_cp=True)
    out_acp = hook.handle_hook(
        {"conversationId": "thread-A", "workspacePaths": ["/workspace/demo"], "transcriptPath": str(ta)},
        forced_event="PreInvocation",
        db_path=db,
    )
    acp_txt = out_acp["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "36px serif" in acp_txt
    assert "Keep AG Grid Community" in acp_txt
    assert "POST /api/skills/publish" in acp_txt
    assert "Never embed a server-side LLM" in acp_txt
    assert "/repo/src/components/mod_15.tsx" in acp_txt
    assert len(acp_txt) <= 3900

    # Immediate repeat call must dedup to {}
    out_repeat = hook.handle_hook(
        {"conversationId": "thread-A", "workspacePaths": ["/workspace/demo"], "transcriptPath": str(ta)},
        forced_event="PreInvocation",
        db_path=db,
    )
    assert out_repeat == {}


def test_subagent_zero_startup_pollution_and_files_only_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "mem.db"
    monkeypatch.setenv("AGY_MEMORY_DB", str(db))
    conn = store.connect_db(db)
    store.pin_decision_record(conn, title="Shared decision", content="Visible to main only", scope="/workspace/demo")
    conn.close()

    sub_t = tmp_path / "sub.jsonl"
    sub_t.write_text(
        _make_line(0, "SYSTEM_MESSAGE", "[Message] sender=parent content=Verify UI screenshots") + "\n",
        encoding="utf-8",
    )
    # Startup: subagent MUST receive {} (0 bytes)
    out_s0 = hook.handle_hook(
        {"conversationId": "sub-1", "workspacePaths": ["/workspace/demo"], "transcriptPath": str(sub_t)},
        forced_event="PreInvocation",
        db_path=db,
    )
    assert out_s0 == {}

    # Subagent touches a file and compacts -> receives files ONLY, never shared workspace decisions
    with sub_t.open("a", encoding="utf-8") as f:
        f.write(
            _make_line(
                1,
                "PLANNER_RESPONSE",
                "",
                input_tokens=420_000,
                tool_calls=[{"name": "read_file", "args": {"Path": "/workspace/demo/src/App.tsx"}}],
            )
            + "\n"
        )
        f.write(_make_line(2, "CHECKPOINT", "# Browser Session Checkpoint") + "\n")

    out_scp = hook.handle_hook(
        {"conversationId": "sub-1", "workspacePaths": ["/workspace/demo"], "transcriptPath": str(sub_t)},
        forced_event="PreInvocation",
        db_path=db,
    )
    scp_txt = out_scp["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "/workspace/demo/src/App.tsx" in scp_txt
    assert "Shared decision" not in scp_txt


def test_380k_warning_dedup_and_malformed_stdin_resilience(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "mem.db"
    monkeypatch.setenv("AGY_MEMORY_DB", str(db))
    t = tmp_path / "warn.jsonl"
    _build_multi_turn_transcript(t, include_cp=False, hit_warn=True)

    # First PreInvocation at 445k tokens -> silently checkpoints ONCE, never nudges /compact
    out1 = hook.handle_hook(
        {"conversationId": "warn-1", "workspacePaths": ["/workspace/demo"], "transcriptPath": str(t)},
        forced_event="PreInvocation",
        db_path=db,
    )
    warn_txt = out1["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "Pre-Compaction Checkpoint" in warn_txt
    assert "Do NOT prompt the user to compact" in warn_txt
    assert "/compact" not in warn_txt

    # Second PreInvocation before compaction -> deduped to {}
    out2 = hook.handle_hook(
        {"conversationId": "warn-1", "workspacePaths": ["/workspace/demo"], "transcriptPath": str(t)},
        forced_event="PreInvocation",
        db_path=db,
    )
    assert out2 == {}

    # Malformed stdin to CLI subprocess -> exit 0, stdout "{}"
    env = dict(os.environ)
    env["AGY_MEMORY_DB"] = str(db)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "agy_memory.hook", "PreInvocation"],
        input="{not-valid-json",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=5,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "{}"
