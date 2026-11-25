"""Core SQLite WAL + FTS5 storage & incremental transcript parser for agy-memory.

Zero third-party dependencies (Python stdlib only) so Antigravity lifecycle hooks
execute cleanly and fast on Windows, macOS, and Linux.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

HANDOFF_STALE_HOURS = 24.0

MAX_START_DECISIONS_CHARS = 900
MAX_QA_CHARS = 1450
MAX_DIRECTIVES_CHARS = 750
MAX_FILES_CHARS = 600
MAX_CROSS_DECISIONS_CHARS = 700

DEFAULT_TTL_DAYS: dict[str, int] = {
    "veto": 14,
    "architecture": 14,
    "preference": 14,
    "decision": 7,
    "lesson": 7,
}

# Declarative veto & redirection signals (English + Chinese, avoiding high-FP modal words)
VETO_RE = re.compile(
    r"(不要再|不要在|不要|禁止|严禁|决不|绝不|否决|别再|别用|别做|取消|移除|删去|删掉|去掉|"
    r"不接受|不允许|不采用|只提供|只做|改用|改为|硬约束|硬红线|"
    r"\b(?:never|do not|don't|must not|forbidden|reject|veto|stop using|switch to|only expose|no longer use)\b)",
    re.IGNORECASE,
)

QUESTION_RE = re.compile(
    r"([?？吗么呢]$|能不能|是不是|为什么|怎么会|请问|有没有|可不可以|是否|好不好|行不行|"
    r"^(?:what|why|how)\b)",
    re.IGNORECASE,
)

NOISE_LINE_RE = re.compile(
    r"^(<SYSTEM_MESSAGE>|<RULE|</|---|```|http://|https://|\[Attachment|TraceID:|Headers:|Error:|"
    r"Comments on artifact|The user has uploaded|The user has mentioned|The current local time)",
    re.IGNORECASE,
)


def get_memory_dir() -> Path:
    override = os.environ.get("AGY_MEMORY_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemini" / "config" / "memory"


def get_db_path() -> Path:
    override = os.environ.get("AGY_MEMORY_DB", "").strip()
    if override:
        return Path(override).expanduser()
    return get_memory_dir() / "agy_memory.db"


def get_brain_dir() -> Path:
    override = os.environ.get("AGY_BRAIN_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemini" / "antigravity" / "brain"


def get_summaries_db_path() -> Path:
    override = os.environ.get("AGY_SUMMARIES_DB", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"


def get_warn_ctx_tokens() -> int:
    try:
        return max(50_000, int(os.environ.get("AGY_WARN_CTX_TOKENS", "430000")))
    except ValueError:
        return 430_000


def get_lang() -> str:
    val = os.environ.get("AGY_MEMORY_LANG", "en").strip().lower()
    return "zh" if val.startswith("zh") else "en"


def utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def utc_now() -> str:
    return utc_now_dt().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = str(ts).strip().rstrip("Z")
    for fmt, sl in (("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(s[:sl], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def age_label(ts: str | None, lang: str | None = None) -> str:
    lg = lang or get_lang()
    dt = parse_utc(ts)
    if not dt:
        return "未知日期" if lg == "zh" else "unknown"
    days = max(0, int((utc_now_dt() - dt).total_seconds() // 86400))
    date_s = dt.strftime("%Y-%m-%d")
    if days == 0:
        return f"{date_s}·今天" if lg == "zh" else f"{date_s}·today"
    return f"{date_s}·{days}天前" if lg == "zh" else f"{date_s}·{days}d ago"


def normalize_scope(path: str | None, conversation_id: str = "") -> str:
    if not path:
        return "global"
    raw = str(path).strip()
    low = raw.lower()
    if not raw or low == "global":
        return "global"
    if low in ("session", "thread"):
        cid = (conversation_id or "").strip()
        if not cid:
            raise ValueError(
                "scope='session' requires a non-empty conversation_id (or pass scope='session:<conversation_id>')"
            )
        return f"session:{cid}"
    if low.startswith("session:") or low.startswith("thread:"):
        cid = raw.split(":", 1)[1].strip()
        if not cid:
            raise ValueError("scope='session:<conversation_id>' requires a non-empty conversation_id")
        return f"session:{cid}"
    s = unquote(raw)
    if s.startswith("file:///"):
        s = s[8:]
        # Preserve leading slash on POSIX paths like file:///Users/alice/repo -> /Users/alice/repo
        if not (len(s) >= 2 and s[1] == ":"):
            s = "/" + s.lstrip("/")
    s = s.replace("\\", "/").rstrip("/")
    if not s:
        return "/"
    if len(s) >= 2 and s[1] == ":":
        s = s[0].lower() + s[1:]
    return s


def connect_db(db_path: Path | None = None) -> sqlite3.Connection:
    target = db_path or get_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=3.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=3000;")
    ensure_schema(conn)
    return conn


def _add_column_if_missing(conn: sqlite3.Connection, table: str, col: str, col_def: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            conversation_id TEXT PRIMARY KEY,
            workspace_path TEXT NOT NULL DEFAULT '',
            transcript_path TEXT NOT NULL DEFAULT '',
            byte_offset INTEGER NOT NULL DEFAULT 0,
            line_count INTEGER NOT NULL DEFAULT 0,
            user_turn_count INTEGER NOT NULL DEFAULT 0,
            last_checkpoint_step INTEGER NOT NULL DEFAULT -1,
            last_injected_cp_step INTEGER NOT NULL DEFAULT -1,
            warn_injected_cp_step INTEGER NOT NULL DEFAULT -2,
            last_ctx_tokens INTEGER NOT NULL DEFAULT 0,
            first_user_goal TEXT NOT NULL DEFAULT '',
            is_subagent INTEGER NOT NULL DEFAULT 0,
            session_start_injected INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS session_directives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            workspace_path TEXT NOT NULL DEFAULT '',
            step_index INTEGER NOT NULL,
            turn_number INTEGER NOT NULL DEFAULT 0,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(conversation_id, step_index, kind)
        );
        CREATE INDEX IF NOT EXISTS idx_directives_conv ON session_directives(conversation_id, step_index);
        CREATE INDEX IF NOT EXISTS idx_directives_ws ON session_directives(workspace_path, kind);

        CREATE TABLE IF NOT EXISTS session_files (
            conversation_id TEXT NOT NULL,
            file_path TEXT NOT NULL,
            action TEXT NOT NULL,
            last_step INTEGER NOT NULL,
            touch_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(conversation_id, file_path)
        );
        CREATE INDEX IF NOT EXISTS idx_files_conv ON session_files(conversation_id, last_step DESC);

        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            source_conversation_id TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            expires_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_decisions_scope ON decisions(scope, active, category);

        CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
            scope,
            category,
            title,
            content,
            content='decisions',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );

        CREATE TRIGGER IF NOT EXISTS decisions_ai AFTER INSERT ON decisions BEGIN
            INSERT INTO decisions_fts(rowid, scope, category, title, content)
            VALUES (new.id, new.scope, new.category, new.title, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS decisions_ad AFTER DELETE ON decisions BEGIN
            INSERT INTO decisions_fts(decisions_fts, rowid, scope, category, title, content)
            VALUES ('delete', old.id, old.scope, old.category, old.title, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS decisions_au AFTER UPDATE ON decisions BEGIN
            INSERT INTO decisions_fts(decisions_fts, rowid, scope, category, title, content)
            VALUES ('delete', old.id, old.scope, old.category, old.title, old.content);
            INSERT INTO decisions_fts(rowid, scope, category, title, content)
            VALUES (new.id, new.scope, new.category, new.title, new.content);
        END;

        CREATE TABLE IF NOT EXISTS hook_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            event TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            elapsed_ms REAL NOT NULL DEFAULT 0
        );
        """
    )
    _add_column_if_missing(conn, "sessions", "is_subagent", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "sessions", "session_start_injected", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "decisions", "expires_at", "TEXT NOT NULL DEFAULT ''")
    conn.commit()


def clean_user_content(raw: str) -> str:
    if not raw:
        return ""
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", raw, re.S)
    text = m.group(1) if m else raw
    text = re.sub(r"<ADDITIONAL_METADATA>.*", "", text, flags=re.S)
    text = re.sub(r"\r\n?", "\n", text).strip()
    return text


def summarize_assistant_reply(raw: str, max_chars: int = 320) -> str:
    """Distill a user-facing assistant reply into high-signal conclusions/options without LLM cost."""
    if not raw:
        return ""
    cleaned = re.sub(r"```.*?```", " [code] ", raw, flags=re.S)
    lines: list[str] = []
    for ln in cleaned.splitlines():
        s = ln.strip()
        if not s or re.match(r"^\|?[\s:\-|]+\|?$", s):
            continue
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|") if c.strip()]
            if not cells:
                continue
            s = " / ".join(cells[:4])
        s = re.sub(r"\[([^\]]+)\]\(file:///[^)]+\)", r"\1", s)
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) >= 4:
            lines.append(s)
    if not lines:
        return ""

    picked: list[str] = []
    used = 0

    def add_piece(p: str) -> bool:
        nonlocal used
        p = p.lstrip("#").strip()
        if len(p) > 130:
            p = p[:127] + "..."
        if p in picked:
            return True
        if used + len(p) + 3 > max_chars:
            return False
        picked.append(p)
        used += len(p) + 3
        return True

    add_piece(lines[0])
    for ln in lines[1:]:
        is_key = (
            ln.startswith("#")
            or re.match(
                r"^(\d+[\.、\)]|[-*]\s*\*\*|[A-C][\.、:：]|Option\s*[A-C1-3]|方案\s*[A-C1-3]|Summary|Conclusion|Recommendation|结论|建议)",
                ln,
                re.IGNORECASE,
            )
            or "**" in ln
        )
        if is_key and not add_piece(ln):
            break
    if len(picked) == 1 and len(lines) > 1:
        for ln in lines[1:4]:
            if not add_piece(ln):
                break
    return " | ".join(picked)


def extract_veto_clauses(user_text: str) -> list[str]:
    """Extract concise, declarative constraint / veto sentences (excluding questions & code)."""
    if not user_text:
        return []
    clauses: list[str] = []
    in_fence = False
    for line in user_text.splitlines():
        line = line.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line or len(line) < 5 or NOISE_LINE_RE.match(line):
            continue
        if len(line) > 500 and ("{" in line or "Traceback" in line):
            continue
        parts = re.split(r"(?<=[。！？!?；;])\s*", line)
        for p in parts:
            p = p.strip()
            if len(p) < 5:
                continue
            if QUESTION_RE.search(p):
                continue
            if VETO_RE.search(p):
                if len(p) > 160:
                    p = p[:157] + "..."
                if p not in clauses:
                    clauses.append(p)
    return clauses[:3]


def _record_file_touch(
    conn: sqlite3.Connection, conversation_id: str, file_path: str, action: str, step_idx: int
) -> None:
    if not file_path:
        return
    fp = str(file_path).strip().replace("\\", "/")
    if "/.system_generated/" in fp.lower():
        return
    existing = conn.execute(
        "SELECT action FROM session_files WHERE conversation_id=? AND file_path=?",
        (conversation_id, fp),
    ).fetchone()
    if existing:
        new_action = action if action in ("write", "edit") else existing["action"]
        conn.execute(
            "UPDATE session_files SET action=?, last_step=?, touch_count=touch_count+1 "
            "WHERE conversation_id=? AND file_path=?",
            (new_action, step_idx, conversation_id, fp),
        )
    else:
        conn.execute(
            "INSERT INTO session_files(conversation_id, file_path, action, last_step, touch_count) "
            "VALUES (?, ?, ?, ?, 1)",
            (conversation_id, fp, action, step_idx),
        )


def sync_transcript(
    conn: sqlite3.Connection,
    conversation_id: str,
    workspace_path: str,
    transcript_path: str,
) -> dict[str, Any]:
    """Incrementally parse newly appended JSONL lines from transcript_path (O(delta) seek)."""
    ws = normalize_scope(workspace_path)
    now = utc_now()
    row = conn.execute(
        "SELECT * FROM sessions WHERE conversation_id=?", (conversation_id,)
    ).fetchone()

    if not row:
        conn.execute(
            "INSERT INTO sessions(conversation_id, workspace_path, transcript_path, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (conversation_id, ws, transcript_path or "", now),
        )
        row = conn.execute(
            "SELECT * FROM sessions WHERE conversation_id=?", (conversation_id,)
        ).fetchone()

    byte_offset = int(row["byte_offset"])
    line_count = int(row["line_count"])
    user_turn_count = int(row["user_turn_count"])
    last_cp_step = int(row["last_checkpoint_step"])
    last_ctx_tokens = int(row["last_ctx_tokens"])
    first_goal = str(row["first_user_goal"] or "")
    is_subagent = int(row["is_subagent"] or 0)

    tp = Path(transcript_path) if transcript_path else None
    if tp and not tp.exists() and conversation_id:
        cand = get_brain_dir() / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
        if cand.exists():
            tp = cand

    if tp and tp.exists():
        file_size = tp.stat().st_size
        if file_size < byte_offset:
            byte_offset = 0
            line_count = 0

        if file_size > byte_offset:
            with tp.open("rb") as f:
                f.seek(byte_offset)
                chunk = f.read()
            last_nl = chunk.rfind(b"\n")
            if last_nl >= 0:
                valid_bytes = chunk[: last_nl + 1]
                byte_offset += len(valid_bytes)
                for raw_line in valid_bytes.decode("utf-8", errors="replace").splitlines():
                    line_count += 1
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        obj = json.loads(raw_line)
                    except Exception:
                        continue

                    stype = obj.get("type", "")
                    step_idx = int(obj.get("step_index", line_count - 1))
                    created_at = str(obj.get("created_at") or now)

                    if line_count == 1:
                        is_subagent = 0 if stype == "USER_INPUT" else 1

                    if stype == "CHECKPOINT":
                        if step_idx > last_cp_step:
                            last_cp_step = step_idx
                        # Compaction resets active context window; prevent stale pre-CP token count from triggering warning
                        last_ctx_tokens = 0
                    elif stype == "USER_INPUT":
                        is_subagent = 0
                        user_text = clean_user_content(str(obj.get("content") or ""))
                        if user_text:
                            user_turn_count += 1
                            if not first_goal:
                                first_goal = user_text.replace("\n", " ")[:220]
                                conn.execute(
                                    "INSERT OR IGNORE INTO session_directives"
                                    "(conversation_id, workspace_path, step_index, turn_number, kind, text, created_at) "
                                    "VALUES (?, ?, ?, ?, 'goal', ?, ?)",
                                    (conversation_id, ws, step_idx, user_turn_count, first_goal, created_at),
                                )
                            compact_turn = re.sub(r"\s+", " ", user_text)[:180]
                            conn.execute(
                                "INSERT OR IGNORE INTO session_directives"
                                "(conversation_id, workspace_path, step_index, turn_number, kind, text, created_at) "
                                "VALUES (?, ?, ?, ?, 'user_turn', ?, ?)",
                                (conversation_id, ws, step_idx, user_turn_count, compact_turn, created_at),
                            )
                            vetoes = extract_veto_clauses(user_text)
                            if vetoes:
                                veto_joined = "; ".join(vetoes)[:280]
                                conn.execute(
                                    "INSERT OR IGNORE INTO session_directives"
                                    "(conversation_id, workspace_path, step_index, turn_number, kind, text, created_at) "
                                    "VALUES (?, ?, ?, ?, 'veto', ?, ?)",
                                    (conversation_id, ws, step_idx, user_turn_count, veto_joined, created_at),
                                )
                    elif stype == "PLANNER_RESPONSE":
                        in_t = int(obj.get("input_tokens") or 0)
                        cache_t = int(obj.get("cache_read_tokens") or 0)
                        if in_t + cache_t > 0:
                            last_ctx_tokens = in_t + cache_t
                        tcs = obj.get("tool_calls") or []
                        reply_txt = str(obj.get("content") or "").strip()
                        if not tcs and reply_txt and user_turn_count > 0:
                            digest = summarize_assistant_reply(reply_txt, max_chars=320)
                            if digest:
                                conn.execute(
                                    "INSERT OR REPLACE INTO session_directives"
                                    "(conversation_id, workspace_path, step_index, turn_number, kind, text, created_at) "
                                    "VALUES (?, ?, ?, ?, 'assistant_reply', ?, ?)",
                                    (conversation_id, ws, step_idx, user_turn_count, digest, created_at),
                                )
                        for tc in tcs:
                            if not isinstance(tc, dict):
                                continue
                            name = str(tc.get("name") or "")
                            args = tc.get("args") or {}
                            if not isinstance(args, dict):
                                continue
                            fp = args.get("Path") or args.get("path") or ""
                            if name in ("write_file", "create_file"):
                                _record_file_touch(conn, conversation_id, fp, "write", step_idx)
                            elif name in ("edit_file", "replace_file_content", "multi_replace_file_content"):
                                _record_file_touch(conn, conversation_id, fp, "edit", step_idx)
                            elif name in ("read_file", "view_file"):
                                _record_file_touch(conn, conversation_id, fp, "read", step_idx)

    conn.execute(
        """
        UPDATE sessions
        SET workspace_path=?, transcript_path=?, byte_offset=?, line_count=?,
            user_turn_count=?, last_checkpoint_step=?, last_ctx_tokens=?,
            first_user_goal=?, is_subagent=?, updated_at=?
        WHERE conversation_id=?
        """,
        (
            ws or row["workspace_path"],
            str(tp) if tp else row["transcript_path"],
            byte_offset,
            line_count,
            user_turn_count,
            last_cp_step,
            last_ctx_tokens,
            first_goal,
            is_subagent,
            now,
            conversation_id,
        ),
    )
    conn.commit()
    updated = conn.execute(
        "SELECT * FROM sessions WHERE conversation_id=?", (conversation_id,)
    ).fetchone()
    return dict(updated)


def read_workspace_handoff(
    workspace_path: str, max_chars: int = 200
) -> tuple[str, str, str, bool]:
    """Locate workspace handoff file metadata. Returns (rel_path, content, mtime_str, is_stale)."""
    if not workspace_path or workspace_path == "global":
        return "", "", "", False
    root = Path(workspace_path)
    if not root.exists():
        return "", "", "", False
    for rel in ("ops/handoff.md", ".agents/handoff.md", "HANDOFF.md"):
        p = root / rel
        if p.exists() and p.is_file():
            try:
                st = p.stat()
                mtime_utc = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                age_hours = (utc_now_dt() - mtime_utc).total_seconds() / 3600.0
                is_stale = age_hours > HANDOFF_STALE_HOURS
                mtime_str = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
                txt = p.read_text(encoding="utf-8", errors="replace").strip()
                if len(txt) > max_chars:
                    txt = txt[:max_chars].rstrip() + "..."
                return rel, txt, mtime_str, is_stale
            except Exception:
                continue
    return "", "", "", False


def is_expired_decision(row: dict[str, Any], now_iso: str | None = None) -> bool:
    exp = str(row.get("expires_at") or "").strip()
    if not exp:
        return False
    now_s = now_iso or utc_now()
    return exp < now_s


def get_active_decisions(
    conn: sqlite3.Connection,
    scope: str,
    include_global: bool = True,
    exclude_expired: bool = True,
    limit: int = 8,
) -> list[dict[str, Any]]:
    norm = normalize_scope(scope)
    scopes = [norm]
    if include_global and norm != "global" and not norm.startswith("session:"):
        scopes.append("global")
    placeholders = ",".join("?" for _ in scopes)
    now_iso = utc_now()
    exp_clause = "AND (expires_at = '' OR expires_at >= ?)" if exclude_expired else ""
    params: list[Any] = list(scopes)
    if exclude_expired:
        params.append(now_iso)
    params.extend([norm, limit])
    rows = conn.execute(
        f"""
        SELECT id, scope, category, title, content, source_conversation_id, expires_at, updated_at
        FROM decisions
        WHERE active=1 AND scope IN ({placeholders}) {exp_clause}
        ORDER BY
            CASE scope WHEN ? THEN 0 ELSE 1 END,
            CASE category WHEN 'veto' THEN 0 WHEN 'architecture' THEN 1 WHEN 'decision' THEN 2 WHEN 'preference' THEN 3 ELSE 4 END,
            updated_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def get_session_decisions(
    conn: sqlite3.Connection,
    conversation_id: str,
    exclude_expired: bool = True,
    limit: int = 10,
) -> list[dict[str, Any]]:
    cid = (conversation_id or "").strip()
    if not cid:
        return []
    return get_active_decisions(
        conn,
        scope=f"session:{cid}",
        include_global=False,
        exclude_expired=exclude_expired,
        limit=limit,
    )


def find_potential_conflicts(
    conn: sqlite3.Connection, scope: str, title: str, content: str, exclude_id: int
) -> list[dict[str, Any]]:
    """Detect active decisions in the same scope with overlapping terms to prevent contradictory rules."""
    norm = normalize_scope(scope)
    tokens = [t for t in re.findall(r"[\w\u4e00-\u9fff]{2,}", f"{title} {content}") if len(t) >= 2]
    if not tokens:
        return []
    uniq: list[str] = []
    for t in tokens:
        if t.lower() not in [u.lower() for u in uniq]:
            uniq.append(t)
        if len(uniq) >= 8:
            break
    now_iso = utc_now()
    candidates = conn.execute(
        "SELECT id, category, title, content, updated_at FROM decisions "
        "WHERE active=1 AND scope=? AND id!=? AND (expires_at='' OR expires_at>=?)",
        (norm, exclude_id, now_iso),
    ).fetchall()
    hits: list[tuple[int, dict[str, Any]]] = []
    for r in candidates:
        hay = f"{r['title']} {r['content']}".lower()
        score = sum(1 for t in uniq if t.lower() in hay)
        t_clean = re.sub(r"\s+", "", title)
        bigrams = [t_clean[i : i + 2] for i in range(max(0, len(t_clean) - 1))]
        b_score = sum(1 for bg in bigrams if len(bg) == 2 and bg.lower() in hay)
        total = score + b_score
        if total >= 2:
            hits.append((total, dict(r)))
    hits.sort(key=lambda x: -x[0])
    return [
        {
            "id": h["id"],
            "category": h["category"],
            "title": h["title"],
            "updated_at": h["updated_at"],
        }
        for _, h in hits[:3]
    ]


def pin_decision_record(
    conn: sqlite3.Connection,
    title: str,
    content: str,
    category: str = "decision",
    scope: str = "global",
    source_conversation_id: str = "",
    ttl_days: int = -1,
) -> dict[str, Any]:
    norm_scope = normalize_scope(scope, conversation_id=source_conversation_id)
    cat = category.strip().lower() if category else "decision"
    if cat not in DEFAULT_TTL_DAYS:
        cat = "decision"
    now_dt = utc_now_dt()
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    eff_ttl = DEFAULT_TTL_DAYS[cat] if ttl_days < 0 else int(ttl_days)
    expires_at = (
        (now_dt + timedelta(days=eff_ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if eff_ttl > 0
        else ""
    )

    existing = conn.execute(
        "SELECT id FROM decisions WHERE scope=? AND title=? AND active=1",
        (norm_scope, title.strip()),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE decisions SET category=?, content=?, source_conversation_id=?, expires_at=?, updated_at=? WHERE id=?",
            (cat, content.strip(), source_conversation_id, expires_at, now, existing["id"]),
        )
        did = int(existing["id"])
        op = "updated"
    else:
        cur = conn.execute(
            "INSERT INTO decisions(scope, category, title, content, source_conversation_id, active, expires_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (norm_scope, cat, title.strip(), content.strip(), source_conversation_id, expires_at, now, now),
        )
        did = int(cur.lastrowid)
        op = "created"
    conn.commit()
    conflicts = find_potential_conflicts(conn, norm_scope, title.strip(), content.strip(), exclude_id=did)
    res: dict[str, Any] = {
        "id": did,
        "operation": op,
        "scope": norm_scope,
        "category": cat,
        "title": title.strip(),
        "expires_at": expires_at or "never",
    }
    if conflicts:
        res["potential_conflicts"] = conflicts
        res["conflict_hint"] = (
            "Similar active decision(s) found in the same scope; call retire_decision if this supersedes them."
        )
    return res


def _format_decisions_block(decs: list[dict[str, Any]], budget_chars: int, lang: str) -> list[str]:
    lines: list[str] = []
    used = 0
    for d in decs:
        age = age_label(d.get("updated_at"), lang=lang)
        item = f"- [#{d['id']}|{d['category'].upper()}|{age}] **{d['title']}**: {d['content']}"
        if used + len(item) > budget_chars:
            break
        lines.append(item)
        used += len(item)
    return lines


def build_session_start_packet(
    conn: sqlite3.Connection, workspace_path: str, conversation_id: str = ""
) -> str:
    """Compact cold-start injection for main interactive sessions ONLY (zero handoff duplication)."""
    ws = normalize_scope(workspace_path)
    lang = get_lang()
    decs = get_active_decisions(conn, ws, include_global=True, exclude_expired=True, limit=8)
    if not decs:
        return ""
    items = _format_decisions_block(decs, MAX_START_DECISIONS_CHARS, lang=lang)
    if not items:
        return ""
    cid_tag = f" | cid={conversation_id}" if conversation_id else ""
    if lang == "zh":
        lines = [
            f"[Memory Guard · 增量跨会话决策 | workspace={ws}{cid_tag}]",
            "优先级：用户当前最新口头指令永远高于以下历史记录；单线程临时规格请用 `pin_decision(scope=\"session\", conversation_id=...)` 避免串台：",
            *items,
        ]
    else:
        lines = [
            f"[Memory Guard | Incremental Workspace Decisions | workspace={ws}{cid_tag}]",
            "Priority: the user's latest instruction always overrides stored memory. Use `pin_decision(scope=\"session\", conversation_id=...)` for thread-private specs:",
            *items,
        ]
    return "\n".join(lines)


def build_post_compaction_packet(
    conn: sqlite3.Connection, session: dict[str, Any], workspace_path: str
) -> str:
    """Build bounded post-compaction recovery packet (<= ~3.6k chars).

    - Subagents receive ONLY touched file paths + their own session-private pins.
    - Main sessions receive: priority guardrail, initial goal, session-private pins,
      last 4 Q&A turns (user prompt -> distilled assistant reply + exact step number),
      evicted turns beyond Antigravity's 10-turn window, non-expired workspace decisions,
      real file path anchors, and a 1-line handoff pointer.
    """
    cid = session["conversation_id"]
    ws = normalize_scope(workspace_path or session.get("workspace_path"))
    cp_step = int(session.get("last_checkpoint_step") or -1)
    is_sub = int(session.get("is_subagent") or 0) == 1
    lang = get_lang()
    sess_pins = get_session_decisions(conn, cid, exclude_expired=True, limit=10)

    if is_sub:
        edited = conn.execute(
            "SELECT file_path, action, last_step FROM session_files "
            "WHERE conversation_id=? AND action IN ('write', 'edit') ORDER BY last_step DESC LIMIT 15",
            (cid,),
        ).fetchall()
        reads = conn.execute(
            "SELECT file_path, touch_count FROM session_files "
            "WHERE conversation_id=? AND action='read' ORDER BY touch_count DESC, last_step DESC LIMIT 10",
            (cid,),
        ).fetchall()
        if not edited and not reads and not sess_pins:
            return ""
        hdr = (
            f"[Memory Guard · 子代理压缩恢复 | CP@step {cp_step} | cid={cid}]"
            if lang == "zh"
            else f"[Memory Guard | Subagent Compaction Recovery | CP@step {cp_step} | cid={cid}]"
        )
        lines = [hdr]
        if sess_pins:
            lines.append("### Thread-Private Pins" if lang != "zh" else "### 本子代理专用置顶")
            lines.extend(_format_decisions_block(sess_pins, MAX_CROSS_DECISIONS_CHARS, lang=lang))
        budget = 0
        if edited or reads:
            lines.append("### Verified Touched Paths" if lang != "zh" else "### 本子代理已触达路径")
            for r in edited:
                item = f"- `{r['file_path']}` ({r['action']}@step {r['last_step']})"
                if budget + len(item) > MAX_FILES_CHARS:
                    break
                lines.append(item)
                budget += len(item)
            for r in reads:
                item = f"- `{r['file_path']}` (read x{r['touch_count']})"
                if budget + len(item) > MAX_FILES_CHARS:
                    break
                lines.append(item)
                budget += len(item)
        lines.append(f"> Verbatim lookup: `search_transcript(conversation_id=\"{cid}\", query=...)`.")
        return "\n".join(lines)

    total_turns = int(session.get("user_turn_count") or 0)
    if lang == "zh":
        lines = [
            f"[Memory Guard · 压缩后防失忆恢复包 | CP@step {cp_step} | 累计 {total_turns} 轮 | cid={cid}]",
            "**优先级护栏**：用户后期新指令永远覆盖前期旧要求；下方仅用于防止遗忘，不可用早期旧话推翻后期决定。",
        ]
    else:
        lines = [
            f"[Memory Guard | Post-Compaction Recovery | CP@step {cp_step} | {total_turns} turns | cid={cid}]",
            "**Priority rule**: later user instructions always override earlier turns; never revert to superseded constraints.",
        ]

    # 1. Original goal
    goal = str(session.get("first_user_goal") or "").strip()
    if goal:
        h1 = "### 1. 本会话初始目标（Turn #1）" if lang == "zh" else "### 1. Initial Session Goal (Turn #1)"
        lines.append(f"\n{h1}\n- {goal}")

    # 1b. Thread-private pinned specs
    if sess_pins:
        pin_items = _format_decisions_block(sess_pins, MAX_CROSS_DECISIONS_CHARS, lang=lang)
        if pin_items:
            h1b = (
                f"### 1b. 本线程专用置顶规格/约束（scope=session:{cid[:8]}，仅本线程可见）"
                if lang == "zh"
                else f"### 1b. Thread-Private Pinned Specs (scope=session:{cid[:8]})"
            )
            lines.append(f"\n{h1b}")
            lines.extend(pin_items)

    # 2. Recent Q&A continuity (last 4 turns: User -> distilled Assistant final reply + exact step)
    recent_replies = conn.execute(
        "SELECT turn_number, step_index, text FROM session_directives "
        "WHERE conversation_id=? AND kind='assistant_reply' ORDER BY step_index DESC LIMIT 8",
        (cid,),
    ).fetchall()
    if recent_replies:
        reply_by_turn: dict[int, tuple[int, str]] = {}
        for r in recent_replies:
            tn = int(r["turn_number"])
            if tn not in reply_by_turn:
                reply_by_turn[tn] = (int(r["step_index"]), str(r["text"]))
        last_turns = sorted(reply_by_turn.keys())[-4:]
        turn_q_rows = conn.execute(
            f"SELECT turn_number, text FROM session_directives "
            f"WHERE conversation_id=? AND kind='user_turn' AND turn_number IN ({','.join('?' for _ in last_turns)})",
            (cid, *last_turns),
        ).fetchall()
        q_map = {int(r["turn_number"]): str(r["text"]) for r in turn_q_rows}
        qa_lines: list[str] = []
        budget = 0
        for tn in reversed(last_turns):
            astep, adigest = reply_by_turn[tn]
            qsnip = q_map.get(tn, "")[:90]
            if lang == "zh":
                item = f"- **Turn #{tn}** 问：{qsnip}\n  → **我刚回复（step {astep}）**：{adigest}"
            else:
                item = f"- **Turn #{tn}** User: {qsnip}\n  -> **Assistant reply (step {astep})**: {adigest}"
            if budget + len(item) > MAX_QA_CHARS and qa_lines:
                break
            qa_lines.append(item)
            budget += len(item)
        qa_lines.reverse()
        if qa_lines:
            h2 = (
                "### 2. 压缩前最近对话问答还原（弥补原生压缩丢弃 Agent 回答）"
                if lang == "zh"
                else "### 2. Recent Q&A Continuity (Recovers Assistant Replies Dropped by Compaction)"
            )
            lines.append(f"\n{h2}")
            lines.extend(qa_lines)

    # 3. Evicted turns (>10 turns ago) dropped by native # User Requests window
    evicted_cutoff = max(0, total_turns - 10)
    if evicted_cutoff > 0:
        evicted_vetoes = conn.execute(
            "SELECT turn_number, text FROM session_directives "
            "WHERE conversation_id=? AND kind='veto' AND turn_number <= ? ORDER BY turn_number ASC",
            (cid, evicted_cutoff),
        ).fetchall()
        evicted_turns = conn.execute(
            "SELECT turn_number, text FROM session_directives "
            "WHERE conversation_id=? AND kind='user_turn' AND turn_number <= ? ORDER BY turn_number ASC",
            (cid, evicted_cutoff),
        ).fetchall()
        veto_turns = {int(r["turn_number"]): f"- Turn #{r['turn_number']} {'[含约束]' if lang == 'zh' else '[constraint]'}: {r['text']}" for r in evicted_vetoes}
        plain_turns = {int(r["turn_number"]): f"- Turn #{r['turn_number']}: {r['text'][:120]}" for r in evicted_turns if int(r["turn_number"]) not in veto_turns}
        # Prioritize evicted declarative constraints first (newest constraints first if huge), then fill remaining budget with recent plain turns
        selected: dict[int, str] = {}
        budget = 0
        for tn in sorted(veto_turns.keys(), reverse=True):
            item = veto_turns[tn]
            if budget + len(item) > MAX_DIRECTIVES_CHARS and selected:
                break
            selected[tn] = item
            budget += len(item)
        for tn in sorted(plain_turns.keys(), reverse=True):
            item = plain_turns[tn]
            if budget + len(item) > MAX_DIRECTIVES_CHARS:
                continue
            selected[tn] = item
            budget += len(item)
        kept = [selected[k] for k in sorted(selected.keys())]
        total_evicted = len(veto_turns) + len(plain_turns)
        if kept:
            h3 = (
                f"### 3. 被原生 10 轮窗口挤出的早期指令（Turn #1–#{evicted_cutoff}，后期覆盖前期）"
                if lang == "zh"
                else f"### 3. Evicted Early User Turns (Turn #1–#{evicted_cutoff}, Dropped Beyond Native 10-Turn Cap)"
            )
            lines.append(f"\n{h3}")
            if len(kept) < total_evicted:
                omitted = total_evicted - len(kept)
                lines.append(f"- ... ({omitted} earlier turns available via `search_transcript`)")
            lines.extend(kept)

    # 4. Active, non-expired workspace decisions
    decs = get_active_decisions(conn, ws, include_global=True, exclude_expired=True, limit=6)
    if decs:
        dec_items = _format_decisions_block(decs, MAX_CROSS_DECISIONS_CHARS, lang=lang)
        if dec_items:
            h4 = (
                f"### 4. 未过期的跨会话增量决策（scope={ws}）"
                if lang == "zh"
                else f"### 4. Active Workspace Decisions (scope={ws})"
            )
            lines.append(f"\n{h4}")
            lines.extend(dec_items)

    # 5. Compact real file paths + 1-line handoff pointer
    edited = conn.execute(
        "SELECT file_path, action, last_step FROM session_files "
        "WHERE conversation_id=? AND action IN ('write', 'edit') ORDER BY last_step DESC LIMIT 10",
        (cid,),
    ).fetchall()
    reads = conn.execute(
        "SELECT file_path, touch_count FROM session_files "
        "WHERE conversation_id=? AND action='read' ORDER BY touch_count DESC, last_step DESC LIMIT 5",
        (cid,),
    ).fetchall()
    if edited or reads:
        h5 = (
            "### 5. 本会话真实路径锚点（勿猜目录、避免盲目重读）"
            if lang == "zh"
            else "### 5. Verified File Path Anchors (Do Not Guess Directories or Blindly Re-Read)"
        )
        lines.append(f"\n{h5}")
        budget = 0
        for r in edited:
            item = f"- `{r['file_path']}` ({r['action']}@step {r['last_step']})"
            if budget + len(item) > MAX_FILES_CHARS:
                break
            lines.append(item)
            budget += len(item)
        for r in reads:
            if budget >= MAX_FILES_CHARS:
                break
            item = f"- `{r['file_path']}` (read x{r['touch_count']})"
            if budget + len(item) > MAX_FILES_CHARS:
                break
            lines.append(item)
            budget += len(item)

    rel_h, _, mtime_str, is_stale = read_workspace_handoff(
        workspace_path or session.get("workspace_path") or "", max_chars=200
    )
    if rel_h:
        if lang == "zh":
            stale_tag = "，已超 24h 未更" if is_stale else ""
            lines.append(f"\n> 交接锚点：`{rel_h}`（更新于 {mtime_str}{stale_tag}，跨线程核对时再按需 `read_file`）。")
        else:
            stale_tag = ", stale >24h" if is_stale else ""
            lines.append(f"\n> Handoff pointer: `{rel_h}` (updated {mtime_str}{stale_tag}; `read_file` only if needed).")

    lines.append(
        f"> Need exact pre-compaction text? Call `get_transcript_steps(conversation_id=\"{cid}\", start_step=N, end_step=N)` or `search_transcript`."
    )
    return "\n".join(lines)


def log_hook_event(
    conn: sqlite3.Connection,
    event: str,
    conversation_id: str,
    action: str,
    detail: str = "",
    elapsed_ms: float = 0.0,
) -> None:
    conn.execute(
        "INSERT INTO hook_events(ts, event, conversation_id, action, detail, elapsed_ms) VALUES (?, ?, ?, ?, ?, ?)",
        (utc_now(), event, conversation_id or "", action, detail[:300], round(elapsed_ms, 2)),
    )
    conn.execute(
        "DELETE FROM hook_events WHERE id NOT IN (SELECT id FROM hook_events ORDER BY id DESC LIMIT 500)"
    )
    conn.commit()
