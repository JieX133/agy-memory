"""agy-memory: Zero-dependency stdio JSON-RPC 2.0 MCP server for lossless session recall & decisions."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

from . import __version__, store


def _find_transcript(conversation_id: str, full: bool = True) -> Path | None:
    cid = conversation_id.strip()
    if not cid:
        return None
    brain = store.get_brain_dir()
    base = brain / cid / ".system_generated" / "logs"
    if not base.exists():
        matches = [d for d in brain.glob(f"{cid}*") if d.is_dir()]
        if len(matches) == 1:
            base = matches[0] / ".system_generated" / "logs"
        else:
            return None
    if full and (base / "transcript_full.jsonl").exists():
        return base / "transcript_full.jsonl"
    if (base / "transcript.jsonl").exists():
        return base / "transcript.jsonl"
    return None


def _safe_fts_query(q: str) -> str:
    tokens = [t for t in re.findall(r"[\w\u4e00-\u9fff]+", q) if t]
    if not tokens:
        return ""
    return " AND ".join(f'"{t}"' for t in tokens[:8])


def _row_matches_workspace(ws_norm: str, fts_ws: str, uris_json: str) -> tuple[bool, str]:
    decoded_uris: list[str] = []
    if uris_json:
        try:
            arr = json.loads(uris_json)
            if isinstance(arr, list):
                decoded_uris = [store.normalize_scope(u) for u in arr if u]
        except Exception:
            decoded_uris = [store.normalize_scope(uris_json)]
    display = decoded_uris[0] if decoded_uris else (fts_ws or "")
    if not ws_norm or ws_norm == "global":
        return True, display
    ws_base = Path(ws_norm).name.lower()
    for u in decoded_uris:
        if ws_norm == u or ws_norm in u or u in ws_norm:
            return True, display
    if fts_ws:
        fw = store.normalize_scope(fts_ws)
        if ws_norm in fw or fw == ws_base:
            return True, display
    return False, display


def pin_decision(
    title: str,
    content: str,
    category: str = "decision",
    scope: str = "global",
    conversation_id: str = "",
    ttl_days: int = -1,
) -> str:
    conn = store.connect_db()
    try:
        try:
            res = store.pin_decision_record(
                conn,
                title=title,
                content=content,
                category=category,
                scope=scope,
                source_conversation_id=conversation_id,
                ttl_days=int(ttl_days),
            )
        except ValueError as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
        return json.dumps(res, ensure_ascii=False, indent=2)
    finally:
        conn.close()


def list_decisions(
    scope: str = "",
    conversation_id: str = "",
    category: str = "",
    include_global: bool = True,
    active_only: bool = True,
    include_expired: bool = False,
    limit: int = 30,
) -> str:
    conn = store.connect_db()
    try:
        now_iso = store.utc_now()
        clauses: list[str] = []
        params: list[Any] = []
        if active_only:
            clauses.append("active=1")
        if not include_expired:
            clauses.append("(expires_at='' OR expires_at>=?)")
            params.append(now_iso)
        if scope:
            try:
                norm = store.normalize_scope(scope, conversation_id=conversation_id)
            except ValueError as e:
                return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
            if norm.startswith("session:"):
                clauses.append("scope=?")
                params.append(norm)
            elif include_global and norm != "global":
                clauses.append("scope IN (?, 'global')")
                params.append(norm)
            else:
                clauses.append("scope=?")
                params.append(norm)
        else:
            clauses.append("scope NOT LIKE 'session:%'")
        if category:
            clauses.append("category=?")
            params.append(category.strip().lower())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT id, scope, category, title, content, source_conversation_id, active, expires_at, updated_at "
            f"FROM decisions {where} "
            f"ORDER BY CASE category WHEN 'veto' THEN 0 WHEN 'architecture' THEN 1 WHEN 'decision' THEN 2 WHEN 'preference' THEN 3 ELSE 4 END, updated_at DESC "
            f"LIMIT ?"
        )
        params.append(max(1, min(int(limit), 100)))
        rows = []
        for r in conn.execute(sql, params).fetchall():
            d = dict(r)
            d["expired"] = store.is_expired_decision(d, now_iso)
            d["age"] = store.age_label(d.get("updated_at"))
            rows.append(d)
        return json.dumps({"count": len(rows), "decisions": rows}, ensure_ascii=False, indent=2)
    finally:
        conn.close()


def retire_decision(decision_id: int, reason: str = "") -> str:
    conn = store.connect_db()
    try:
        did = int(decision_id)
        row = conn.execute("SELECT id, title, content FROM decisions WHERE id=?", (did,)).fetchone()
        if not row:
            return json.dumps({"ok": False, "error": f"Decision #{did} not found"}, ensure_ascii=False)
        new_content = row["content"]
        if reason:
            new_content = f"{new_content} [retired: {reason.strip()}]"
        conn.execute(
            "UPDATE decisions SET active=0, content=?, updated_at=? WHERE id=?",
            (new_content, store.utc_now(), did),
        )
        conn.commit()
        return json.dumps({"ok": True, "id": did, "title": row["title"], "active": 0}, ensure_ascii=False)
    finally:
        conn.close()


def search_sessions(query: str, workspace: str = "", limit: int = 10) -> str:
    q = (query or "").strip()
    if not q:
        return json.dumps({"error": "query must not be empty"}, ensure_ascii=False)
    lim = max(1, min(int(limit), 30))
    ws_norm = store.normalize_scope(workspace) if workspace else ""
    out: dict[str, Any] = {"query": q, "workspace_filter": ws_norm, "decisions": [], "sessions": []}

    conn = store.connect_db()
    try:
        fts_q = _safe_fts_query(q)
        if fts_q:
            try:
                drows = conn.execute(
                    """
                    SELECT d.id, d.scope, d.category, d.title, d.content, d.updated_at
                    FROM decisions_fts f
                    JOIN decisions d ON d.id = f.rowid
                    WHERE decisions_fts MATCH ? AND d.active=1
                    ORDER BY rank LIMIT ?
                    """,
                    (fts_q, lim),
                ).fetchall()
                out["decisions"] = [dict(r) for r in drows]
            except Exception:
                pass
        if not out["decisions"]:
            like = f"%{q}%"
            drows = conn.execute(
                "SELECT id, scope, category, title, content, updated_at FROM decisions "
                "WHERE active=1 AND (title LIKE ? OR content LIKE ?) ORDER BY updated_at DESC LIMIT ?",
                (like, like, lim),
            ).fetchall()
            out["decisions"] = [dict(r) for r in drows]
    finally:
        conn.close()

    summaries_db = store.get_summaries_db_path()
    if summaries_db.exists():
        sconn = sqlite3.connect(f"file:{summaries_db}?mode=ro", uri=True)
        sconn.row_factory = sqlite3.Row
        try:
            seen_cids: set[str] = set()
            fts_q = _safe_fts_query(q)
            if fts_q:
                try:
                    rows = sconn.execute(
                        """
                        SELECT
                            f.conversation_id,
                            COALESCE(NULLIF(cs.title, ''), f.title) AS title,
                            f.workspace AS fts_workspace,
                            COALESCE(cs.workspace_uris, '') AS workspace_uris,
                            f.last_modified_time,
                            COALESCE(cs.step_count, f.step_count) AS step_count,
                            f.chunk_first_step,
                            f.chunk_last_step,
                            snippet(conversations_fts, 13, '[', ']', '...', 24) AS user_snip,
                            snippet(conversations_fts, 14, '[', ']', '...', 24) AS agent_snip
                        FROM conversations_fts f
                        LEFT JOIN conversation_summaries cs ON cs.conversation_id = f.conversation_id
                        WHERE conversations_fts MATCH ?
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (fts_q, lim * 5),
                    ).fetchall()
                    for r in rows:
                        d = dict(r)
                        ok, disp_ws = _row_matches_workspace(
                            ws_norm, d.pop("fts_workspace", ""), d.pop("workspace_uris", "")
                        )
                        if not ok or d["conversation_id"] in seen_cids:
                            continue
                        d["workspace"] = disp_ws
                        seen_cids.add(d["conversation_id"])
                        out["sessions"].append(d)
                        if len(out["sessions"]) >= lim:
                            break
                except Exception:
                    pass

            if len(out["sessions"]) < lim:
                like = f"%{q}%"
                rows = sconn.execute(
                    """
                    SELECT
                        f.conversation_id,
                        COALESCE(NULLIF(cs.title, ''), f.title) AS title,
                        f.workspace AS fts_workspace,
                        COALESCE(cs.workspace_uris, '') AS workspace_uris,
                        f.last_modified_time,
                        COALESCE(cs.step_count, f.step_count) AS step_count,
                        f.chunk_first_step,
                        f.chunk_last_step,
                        f.user_body,
                        f.agent_body
                    FROM conversations_fts f
                    LEFT JOIN conversation_summaries cs ON cs.conversation_id = f.conversation_id
                    WHERE cs.title LIKE ? OR f.title LIKE ? OR f.user_body LIKE ? OR f.agent_body LIKE ?
                    ORDER BY f.last_modified_time DESC
                    LIMIT ?
                    """,
                    (like, like, like, like, lim * 5),
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    ok, disp_ws = _row_matches_workspace(
                        ws_norm, d.pop("fts_workspace", ""), d.pop("workspace_uris", "")
                    )
                    if not ok or d["conversation_id"] in seen_cids:
                        continue
                    d["workspace"] = disp_ws
                    seen_cids.add(d["conversation_id"])
                    for col, key in (("user_body", "user_snip"), ("agent_body", "agent_snip")):
                        body = str(d.pop(col, "") or "")
                        pos = body.lower().find(q.lower())
                        d[key] = body[max(0, pos - 80) : pos + 160].replace("\n", " ") if pos >= 0 else ""
                    out["sessions"].append(d)
                    if len(out["sessions"]) >= lim:
                        break
        finally:
            sconn.close()

    return json.dumps(out, ensure_ascii=False, indent=2)


def get_session_brief(conversation_id: str, tail_turns: int = 6) -> str:
    tp = _find_transcript(conversation_id, full=False)
    if not tp:
        return json.dumps({"error": f"Transcript not found for {conversation_id}"}, ensure_ascii=False)

    users: list[dict[str, Any]] = []
    checkpoints: list[int] = []
    modified_files: dict[str, str] = {}
    recent_turns: list[dict[str, Any]] = []
    total_steps = 0

    for line in tp.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        total_steps += 1
        stype = obj.get("type", "")
        step = int(obj.get("step_index", total_steps - 1))
        if stype == "USER_INPUT":
            txt = store.clean_user_content(str(obj.get("content") or ""))
            if txt:
                users.append({"turn": len(users) + 1, "step": step, "text": txt[:260].replace("\n", " ")})
                recent_turns.append({"step": step, "role": "user", "text": txt[:400]})
        elif stype == "PLANNER_RESPONSE":
            txt = str(obj.get("content") or "").strip()
            if txt:
                recent_turns.append({"step": step, "role": "assistant", "text": txt[:500]})
            for tc in obj.get("tool_calls") or []:
                name = str(tc.get("name") or "")
                fp = (tc.get("args") or {}).get("Path") or (tc.get("args") or {}).get("path") or ""
                if fp and name in ("write_file", "edit_file", "create_file", "replace_file_content"):
                    modified_files[str(fp).replace("\\", "/")] = f"{name}@step{step}"
        elif stype == "CHECKPOINT":
            checkpoints.append(step)

    n = max(1, min(int(tail_turns), 20))
    return json.dumps(
        {
            "conversation_id": tp.parent.parent.parent.name,
            "total_steps": total_steps,
            "compaction_steps": checkpoints,
            "user_turn_count": len(users),
            "all_user_requests": users,
            "modified_files": list(modified_files.items())[-30:],
            "recent_turns": recent_turns[-n * 2 :],
        },
        ensure_ascii=False,
        indent=2,
    )


def search_transcript(conversation_id: str, query: str, max_hits: int = 10) -> str:
    tp = _find_transcript(conversation_id, full=True)
    if not tp:
        return json.dumps({"error": f"Transcript not found for {conversation_id}"}, ensure_ascii=False)
    q = (query or "").strip()
    if not q:
        return json.dumps({"error": "query must not be empty"}, ensure_ascii=False)
    ql = q.lower()
    lim = max(1, min(int(max_hits), 25))
    hits: list[dict[str, Any]] = []

    with tp.open("r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f):
            if ql not in line.lower():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            stype = obj.get("type", "")
            step = int(obj.get("step_index", idx))
            body = str(obj.get("content") or "")
            if stype == "USER_INPUT":
                body = store.clean_user_content(body)
            elif not body and obj.get("tool_calls"):
                body = json.dumps(obj.get("tool_calls"), ensure_ascii=False)
            pos = body.lower().find(ql)
            snippet = (
                body[max(0, pos - 120) : pos + 240].replace("\n", " ")
                if pos >= 0
                else body[:300].replace("\n", " ")
            )
            hits.append({"step": step, "type": stype, "created_at": obj.get("created_at", ""), "snippet": snippet})
            if len(hits) >= lim:
                break

    return json.dumps(
        {"conversation_id": tp.parent.parent.parent.name, "query": q, "hit_count": len(hits), "hits": hits},
        ensure_ascii=False,
        indent=2,
    )


def get_transcript_steps(conversation_id: str, start_step: int, end_step: int, max_chars: int = 8000) -> str:
    tp = _find_transcript(conversation_id, full=True)
    if not tp:
        return json.dumps({"error": f"Transcript not found for {conversation_id}"}, ensure_ascii=False)
    s0 = max(0, int(start_step))
    s1 = max(s0, min(int(end_step), s0 + 25))
    budget = max(500, min(int(max_chars), 24000))
    items: list[dict[str, Any]] = []
    used = 0

    with tp.open("r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f):
            try:
                obj = json.loads(line)
            except Exception:
                continue
            step = int(obj.get("step_index", idx))
            if step < s0:
                continue
            if step > s1:
                break
            stype = obj.get("type", "")
            body = str(obj.get("content") or "")
            if stype == "USER_INPUT":
                body = store.clean_user_content(body)
            if len(body) > 2500:
                body = body[:2500] + "\n...(truncated)"
            entry: dict[str, Any] = {
                "step": step,
                "type": stype,
                "created_at": obj.get("created_at", ""),
                "content": body,
            }
            if obj.get("tool_calls"):
                entry["tool_calls"] = [
                    {"name": tc.get("name"), "args_keys": list((tc.get("args") or {}).keys())}
                    for tc in (obj.get("tool_calls") or [])
                ]
            sz = len(json.dumps(entry, ensure_ascii=False))
            if used + sz > budget and items:
                break
            items.append(entry)
            used += sz

    return json.dumps(
        {"conversation_id": tp.parent.parent.parent.name, "range": [s0, s1], "steps": items},
        ensure_ascii=False,
        indent=2,
    )


def memory_status() -> str:
    conn = store.connect_db()
    try:
        now_iso = store.utc_now()
        sess_cnt = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        dir_cnt = conn.execute("SELECT COUNT(*) FROM session_directives").fetchone()[0]
        dec_live = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE active=1 AND (expires_at='' OR expires_at>=?)", (now_iso,)
        ).fetchone()[0]
        dec_exp = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE active=1 AND expires_at!='' AND expires_at<?", (now_iso,)
        ).fetchone()[0]
        recent_hooks = [
            dict(r)
            for r in conn.execute(
                "SELECT ts, event, substr(conversation_id,1,8) AS cid, action, detail, elapsed_ms "
                "FROM hook_events ORDER BY id DESC LIMIT 15"
            ).fetchall()
        ]
        return json.dumps(
            {
                "db_path": str(store.get_db_path()),
                "summaries_db_exists": store.get_summaries_db_path().exists(),
                "tracked_sessions": sess_cnt,
                "captured_directives": dir_cnt,
                "active_live_decisions": dec_live,
                "expired_decisions": dec_exp,
                "warn_threshold_tokens": store.get_warn_ctx_tokens(),
                "recent_hook_events": recent_hooks,
            },
            ensure_ascii=False,
            indent=2,
        )
    finally:
        conn.close()


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "pin_decision",
        "description": (
            "Save or update an incremental decision/veto (avoid duplicating GEMINI.md/AGENTS.md). "
            "Use scope='session' (with conversation_id) for thread-private specs that never leak to parallel threads, "
            "or workspace path / 'global' for shared decisions (7-14d default TTL + conflict detection)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short decision title"},
                "content": {"type": "string", "description": "Decision or constraint details"},
                "category": {
                    "type": "string",
                    "enum": ["veto", "architecture", "decision", "preference", "lesson"],
                    "default": "decision",
                },
                "scope": {"type": "string", "default": "global", "description": "'session', 'session:<cid>', workspace path, or 'global'"},
                "conversation_id": {"type": "string", "default": "", "description": "Required when scope='session'"},
                "ttl_days": {"type": "integer", "default": -1, "description": "-1 = category default (7d/14d); 0 = never expire"},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "list_decisions",
        "description": "List active decisions for a scope. Pass scope='session' + conversation_id for thread-private pins; workspace queries never return other threads' private pins.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "default": ""},
                "conversation_id": {"type": "string", "default": ""},
                "category": {"type": "string", "default": ""},
                "include_global": {"type": "boolean", "default": True},
                "active_only": {"type": "boolean", "default": True},
                "include_expired": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 30},
            },
        },
    },
    {
        "name": "retire_decision",
        "description": "Deactivate an outdated or superseded decision by its numeric ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "decision_id": {"type": "integer"},
                "reason": {"type": "string", "default": ""},
            },
            "required": ["decision_id"],
        },
    },
    {
        "name": "search_sessions",
        "description": "Search across all historical Antigravity sessions (via native FTS5) and pinned decisions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "workspace": {"type": "string", "default": ""},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_session_brief",
        "description": "Return a structured brief of any session: all user requests (uncapped), compaction steps, modified files, and tail turns.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string"},
                "tail_turns": {"type": "integer", "default": 6},
            },
            "required": ["conversation_id"],
        },
    },
    {
        "name": "search_transcript",
        "description": "Lossless verbatim search inside a conversation's full transcript.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string"},
                "query": {"type": "string"},
                "max_hits": {"type": "integer", "default": 10},
            },
            "required": ["conversation_id", "query"],
        },
    },
    {
        "name": "get_transcript_steps",
        "description": "Read verbatim steps [start_step..end_step] from a conversation's full transcript (bounded output).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string"},
                "start_step": {"type": "integer"},
                "end_step": {"type": "integer"},
                "max_chars": {"type": "integer", "default": 8000},
            },
            "required": ["conversation_id", "start_step", "end_step"],
        },
    },
    {
        "name": "memory_status",
        "description": "Return health metrics of the agy-memory SQLite store and recent hook latencies.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

TOOL_HANDLERS: dict[str, Callable[..., str]] = {
    "pin_decision": pin_decision,
    "list_decisions": list_decisions,
    "retire_decision": retire_decision,
    "search_sessions": search_sessions,
    "get_session_brief": get_session_brief,
    "search_transcript": search_transcript,
    "get_transcript_steps": get_transcript_steps,
    "memory_status": memory_status,
}


def _send(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle_message(msg: dict[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    # Notifications (no id) require no response
    if req_id is None:
        return None

    if method == "initialize":
        client_ver = str(params.get("protocolVersion") or "2024-11-05")
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": client_ver,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agy-memory", "version": __version__},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOL_SPECS}}

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}

    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        fn = TOOL_HANDLERS.get(name)
        if fn is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"Unknown tool: {name}"},
            }
        try:
            text = fn(**arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps({"error": str(e)}, ensure_ascii=False)}], "isError": True},
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    try:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as e:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {e}"}})
            continue
        if isinstance(msg, dict):
            resp = _handle_message(msg)
            if resp is not None:
                _send(resp)


if __name__ == "__main__":
    main()
