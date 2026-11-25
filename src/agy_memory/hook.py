"""Antigravity lifecycle hook entry point (`PreInvocation` and `Stop`).

Contract: reads UTF-8 JSON from stdin, writes UTF-8 JSON to stdout, ALWAYS exits 0
with `{}` on error so the Antigravity agent loop is never blocked.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import store


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def _resolve_workspace(payload: dict) -> str:
    ws_paths = payload.get("workspacePaths")
    if isinstance(ws_paths, list) and ws_paths:
        return str(ws_paths[0])
    cwd = payload.get("cwd")
    if cwd:
        return str(cwd)
    return ""


def handle_hook(payload: dict, forced_event: str | None = None, db_path: Path | None = None) -> dict:
    t0 = time.perf_counter()
    event = forced_event or str(payload.get("hookEventName") or "")
    cid = str(payload.get("conversationId") or "").strip()
    workspace = _resolve_workspace(payload)
    transcript = str(payload.get("transcriptPath") or "").strip()

    conn = store.connect_db(db_path)
    try:
        if event == "PreInvocation":
            if not cid:
                return {}
            sess = store.sync_transcript(conn, cid, workspace, transcript)
            is_sub = int(sess["is_subagent"]) == 1
            start_injected = int(sess["session_start_injected"]) == 1
            last_cp = int(sess["last_checkpoint_step"])
            last_inj_cp = int(sess["last_injected_cp_step"])
            warn_cp = int(sess["warn_injected_cp_step"])
            ctx_tokens = int(sess["last_ctx_tokens"])
            line_count = int(sess["line_count"])
            warn_limit = store.get_warn_ctx_tokens()

            # Priority 1: Recent new CHECKPOINT detected -> inject recovery ONCE
            cp_is_recent = (line_count - 1 - last_cp) <= 15 or last_inj_cp >= 0
            if last_cp >= 0 and last_cp > last_inj_cp:
                conn.execute(
                    "UPDATE sessions SET last_injected_cp_step=?, session_start_injected=1, updated_at=? "
                    "WHERE conversation_id=?",
                    (last_cp, store.utc_now(), cid),
                )
                conn.commit()
                if cp_is_recent:
                    packet = store.build_post_compaction_packet(conn, sess, workspace)
                    elapsed = (time.perf_counter() - t0) * 1000.0
                    if packet:
                        action = "inject_subagent_cp_files" if is_sub else "inject_post_compaction"
                        store.log_hook_event(
                            conn, event, cid, action, f"cp_step={last_cp} chars={len(packet)}", elapsed
                        )
                        return {"injectSteps": [{"systemMessage": {"systemMessage": packet}}]}
                return {}

            # Priority 2: First turn of a fresh main session -> compact incremental decisions ONCE
            if not start_injected and line_count > 0:
                conn.execute(
                    "UPDATE sessions SET session_start_injected=1, updated_at=? WHERE conversation_id=?",
                    (store.utc_now(), cid),
                )
                conn.commit()
                if is_sub:
                    elapsed = (time.perf_counter() - t0) * 1000.0
                    store.log_hook_event(conn, event, cid, "skip_subagent_start", f"lines={line_count}", elapsed)
                    return {}
                if line_count <= 5:
                    packet = store.build_session_start_packet(conn, workspace, conversation_id=cid)
                    elapsed = (time.perf_counter() - t0) * 1000.0
                    if packet:
                        store.log_hook_event(
                            conn, event, cid, "inject_main_start", f"chars={len(packet)}", elapsed
                        )
                        return {"injectSteps": [{"systemMessage": {"systemMessage": packet}}]}

            # Priority 3: Approaching auto-compaction threshold (main sessions only)
            if not is_sub and ctx_tokens >= warn_limit and warn_cp != last_cp:
                conn.execute(
                    "UPDATE sessions SET warn_injected_cp_step=?, updated_at=? WHERE conversation_id=?",
                    (last_cp, store.utc_now(), cid),
                )
                conn.commit()
                if store.get_lang() == "zh":
                    msg = (
                        f"[Memory Guard · 压缩前静默落盘 | cid={cid}] 当前上下文已达 {ctx_tokens:,} tokens"
                        f"（接近原生 ~500k 自动压缩阈值）。请勿提示用户提前压缩或重开会话；仅在本轮顺手备存易失记忆：\n"
                        f"1. 将尚未落盘的本线程任务规格/局部约束用 `pin_decision(scope=\"session\", conversation_id=\"{cid}\", ...)` 入库；跨线程长期拍板才用工作区 `scope`；\n"
                        "2. 如工作区维护交接文档，按需同步最新状态。"
                    )
                else:
                    msg = (
                        f"[Memory Guard | Pre-Compaction Checkpoint | cid={cid}] Context reached {ctx_tokens:,} tokens "
                        "(approaching Antigravity's ~500k auto-compaction threshold). Do NOT prompt the user to compact early or restart; "
                        "silently checkpoint volatile state before native compaction wipes history:\n"
                        f"1. Pin any unsaved thread-local specs via `pin_decision(scope=\"session\", conversation_id=\"{cid}\", ...)` "
                        "(or workspace scope only for durable cross-thread decisions);\n"
                        "2. Sync your workspace handoff note if maintained."
                    )
                elapsed = (time.perf_counter() - t0) * 1000.0
                store.log_hook_event(
                    conn, event, cid, "inject_pre_cp_flush", f"ctx={ctx_tokens} cp={last_cp}", elapsed
                )
                return {"injectSteps": [{"systemMessage": {"systemMessage": msg}}]}

            return {}

        if event == "Stop":
            if cid:
                sess = store.sync_transcript(conn, cid, workspace, transcript)
                elapsed = (time.perf_counter() - t0) * 1000.0
                store.log_hook_event(
                    conn,
                    event,
                    cid,
                    "sync_stop",
                    f"sub={sess['is_subagent']} turns={sess['user_turn_count']} ctx={sess['last_ctx_tokens']} cp={sess['last_checkpoint_step']}",
                    elapsed,
                )
            return {}

        return {}
    finally:
        conn.close()


def main() -> None:
    try:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    forced_event = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        result = handle_hook(payload, forced_event=forced_event)
        _emit(result if isinstance(result, dict) else {})
    except Exception:
        _emit({})


if __name__ == "__main__":
    main()
