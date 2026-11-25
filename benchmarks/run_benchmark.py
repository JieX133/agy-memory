"""Reproducible benchmark suite for agy-memory (100% synthetic anonymized fixtures).

Measures:
  1. In-process store latency (ms) and subprocess hook wall-clock latency (ms)
  2. Cold-start payload size (main interactive session vs. subagent = 0 bytes)
  3. Post-compaction recovery payload size, dedup guarantee, evicted-turn recall,
     and recent Q&A reply recall vs. Antigravity's native CHECKPOINT baseline
  4. Parallel-thread leakage rate under `scope="session"` (target: 0%)
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from agy_memory import hook, store  # noqa: E402


def _build_fixture_transcript(path: Path, turns: int = 24, steps_per_turn: int = 26) -> tuple[int, int]:
    """Generate a realistic ~650-step transcript reaching 508k tokens and compacting."""
    lines: list[str] = []
    step = 0
    for t in range(1, turns + 1):
        u_msg = f"<USER_REQUEST>\nTurn {t}: update service_{t} and verify output."
        if t == 2:
            u_msg += " Never embed server-side LLMs; keep backend deterministic with MCP endpoints only."
        elif t == 5:
            u_msg += " Do not put feature-marketing subtitles under H1 headings."
        elif t == 22:
            u_msg += " Should we keep the grid engine or build a custom table?"
        elif t == 23:
            u_msg += " Why is there no Skill upload endpoint for humans and local agents?"
        elif t == 24:
            u_msg += " Summarize the two options and proceed with verification."
        u_msg += "\n</USER_REQUEST>"
        lines.append(json.dumps({"step_index": step, "type": "USER_INPUT", "created_at": "2026-11-25T09:00:00Z", "content": u_msg}))
        step += 1

        ctx = int(20_000 + (t / turns) * 488_000)
        for sub in range(steps_per_turn - 2):
            lines.append(
                json.dumps(
                    {
                        "step_index": step,
                        "type": "PLANNER_RESPONSE" if sub % 2 == 0 else "GENERIC",
                        "created_at": "2026-11-25T09:01:00Z",
                        "input_tokens": ctx if sub % 2 == 0 else 0,
                        "cache_read_tokens": 0,
                        "content": "tool output ok" if sub % 2 == 1 else "",
                        "tool_calls": (
                            [{"name": "edit_file" if sub == 0 else "read_file", "args": {"Path": f"/workspace/demo/src/components/mod_{t}.tsx"}}]
                            if sub % 2 == 0
                            else []
                        ),
                    }
                )
            )
            step += 1

        # Final assistant reply (no tool_calls)
        if t == 22:
            ans = (
                "Option comparison for the grid layer:\n"
                "1. **Option A (Keep AG Grid Community)**: replaces legacy spreadsheets cleanly.\n"
                "2. **Option B (Custom table)**: high maintenance cost for formulas and filtering."
            )
        elif t == 23:
            ans = (
                "Confirmed missing upload routes in `src/api/skills.py`:\n"
                "- **Human UI upload**: drag-and-drop `.zip` or inline `SKILL.md` editor.\n"
                "- **Agent publish API**: `POST /api/skills/publish` exposed via MCP."
            )
        elif t == 24:
            ans = (
                "Locked architecture summary before verification:\n"
                "- **Grid**: Option A (AG Grid Community).\n"
                "- **Skills**: dual human + MCP publish endpoints."
            )
        else:
            ans = f"Completed **service_{t}** edits and passed local unit checks."

        lines.append(
            json.dumps(
                {
                    "step_index": step,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-25T09:02:00Z",
                    "input_tokens": ctx,
                    "cache_read_tokens": 0,
                    "content": ans,
                }
            )
        )
        step += 1

    cp_step = step
    lines.append(
        json.dumps(
            {
                "step_index": cp_step,
                "type": "CHECKPOINT",
                "created_at": "2026-11-25T09:30:00Z",
                "output_tokens": 6400,
                "content": "# Resuming from a compaction\n# User Requests\n(last 10 turns only)\n<summary>## 1. Task Overview</summary>",
            }
        )
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines), cp_step


def run_benchmark() -> dict:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = tmp / "bench.db"
        os.environ["AGY_MEMORY_DB"] = str(db)
        os.environ["AGY_MEMORY_LANG"] = "en"

        conn = store.connect_db(db)
        store.pin_decision_record(
            conn,
            title="Zero server-side embedded LLM",
            content="Backend exposes deterministic MCP endpoints only; never embed server-side LLM calls.",
            category="veto",
            scope="/workspace/demo",
            ttl_days=14,
        )
        store.pin_decision_record(
            conn,
            title="Three-tier phased rollout",
            content="Phase 1 pain-point pilots -> Phase 2 SOP + training -> Phase 3 validation.",
            category="decision",
            scope="/workspace/demo",
            ttl_days=14,
        )
        store.pin_decision_record(
            conn,
            title="Thread A layout spec",
            content="Strict 16-slide deck with 36px serif headings.",
            category="decision",
            scope="session",
            source_conversation_id="bench-main-A",
        )
        conn.close()

        # 1. Measure cold start: main session vs. subagent
        t_start = tmp / "start.jsonl"
        t_start.write_text(
            json.dumps({"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-25T09:00:00Z", "content": "<USER_REQUEST>Build v2</USER_REQUEST>"}) + "\n",
            encoding="utf-8",
        )
        t0 = time.perf_counter()
        out_main_start = hook.handle_hook(
            {"conversationId": "bench-start", "workspacePaths": ["/workspace/demo"], "transcriptPath": str(t_start)},
            forced_event="PreInvocation",
            db_path=db,
        )
        main_start_ms = (time.perf_counter() - t0) * 1000.0
        main_start_chars = len(out_main_start["injectSteps"][0]["systemMessage"]["systemMessage"])

        t_sub = tmp / "sub.jsonl"
        t_sub.write_text(
            json.dumps({"step_index": 0, "type": "SYSTEM_MESSAGE", "created_at": "2026-11-25T09:00:00Z", "content": "[Message] sender=parent content=Verify"}) + "\n",
            encoding="utf-8",
        )
        t0 = time.perf_counter()
        out_sub_start = hook.handle_hook(
            {"conversationId": "bench-sub", "workspacePaths": ["/workspace/demo"], "transcriptPath": str(t_sub)},
            forced_event="PreInvocation",
            db_path=db,
        )
        sub_start_ms = (time.perf_counter() - t0) * 1000.0
        sub_start_chars = 0 if out_sub_start == {} else len(json.dumps(out_sub_start))

        # 2. Full 625-step transcript across 380k warning and CHECKPOINT
        t_full = tmp / "full.jsonl"
        total_lines, cp_step = _build_fixture_transcript(t_full, turns=24, steps_per_turn=26)
        raw_lines = t_full.read_text(encoding="utf-8").splitlines()

        t_live = tmp / "live.jsonl"
        # Feed pre-CP lines (ctx ~508k >= 380k)
        t_live.write_text("\n".join(raw_lines[:-1]) + "\n", encoding="utf-8")
        t0 = time.perf_counter()
        out_warn = hook.handle_hook(
            {"conversationId": "bench-main-A", "workspacePaths": ["/workspace/demo"], "transcriptPath": str(t_live)},
            forced_event="PreInvocation",
            db_path=db,
        )
        warn_ms = (time.perf_counter() - t0) * 1000.0
        warn_chars = len(out_warn["injectSteps"][0]["systemMessage"]["systemMessage"])

        # Measure 10 steady-state no-op PreInvocation calls (in-process + subprocess)
        noop_inproc_ms = []
        for _ in range(10):
            t0 = time.perf_counter()
            res = hook.handle_hook(
                {"conversationId": "bench-main-A", "workspacePaths": ["/workspace/demo"], "transcriptPath": str(t_live)},
                forced_event="PreInvocation",
                db_path=db,
            )
            noop_inproc_ms.append((time.perf_counter() - t0) * 1000.0)
            assert res == {}

        env = dict(os.environ)
        env["AGY_MEMORY_DB"] = str(db)
        env["PYTHONPATH"] = str(ROOT / "src")
        payload_s = json.dumps({"conversationId": "bench-main-A", "workspacePaths": ["/workspace/demo"], "transcriptPath": str(t_live)})
        noop_subproc_ms = []
        for _ in range(5):
            t0 = time.perf_counter()
            p = subprocess.run(
                [sys.executable, "-X", "utf8", "-m", "agy_memory.hook", "PreInvocation"],
                input=payload_s,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                timeout=5,
            )
            noop_subproc_ms.append((time.perf_counter() - t0) * 1000.0)
            assert p.returncode == 0 and p.stdout.strip() == "{}"

        # Append CHECKPOINT step and measure post-compaction recovery
        with t_live.open("a", encoding="utf-8") as f:
            f.write(raw_lines[-1] + "\n")
        t0 = time.perf_counter()
        out_cp = hook.handle_hook(
            {"conversationId": "bench-main-A", "workspacePaths": ["/workspace/demo"], "transcriptPath": str(t_live)},
            forced_event="PreInvocation",
            db_path=db,
        )
        cp_ms = (time.perf_counter() - t0) * 1000.0
        cp_txt = out_cp["injectSteps"][0]["systemMessage"]["systemMessage"]

        # Verify parallel thread B sees 0% of Thread A's private pin
        out_b = hook.handle_hook(
            {"conversationId": "bench-main-B", "workspacePaths": ["/workspace/demo"], "transcriptPath": str(t_live)},
            forced_event="PreInvocation",
            db_path=db,
        )
        b_txt = out_b["injectSteps"][0]["systemMessage"]["systemMessage"]
        thread_b_leak = "36px serif" in b_txt

        qa_hits = sum(
            1 for kw in ("Option A (Keep AG Grid Community)", "POST /api/skills/publish", "Locked architecture summary")
            if kw in cp_txt
        )
        evicted_hits = sum(
            1 for kw in ("Never embed server-side LLMs", "Do not put feature-marketing subtitles")
            if kw in cp_txt
        )
        private_hit = "36px serif" in cp_txt

        return {
            "fixture_steps": total_lines,
            "fixture_turns": 24,
            "checkpoint_step": cp_step,
            "main_startup_chars": main_start_chars,
            "main_startup_inproc_ms": round(main_start_ms, 2),
            "subagent_startup_chars": sub_start_chars,
            "subagent_startup_inproc_ms": round(sub_start_ms, 2),
            "ctx_380k_warn_chars": warn_chars,
            "ctx_380k_warn_inproc_ms": round(warn_ms, 2),
            "noop_inproc_median_ms": round(statistics.median(noop_inproc_ms), 2),
            "noop_subprocess_median_ms": round(statistics.median(noop_subproc_ms), 2),
            "post_cp_recovery_chars": len(cp_txt),
            "post_cp_recovery_inproc_ms": round(cp_ms, 2),
            "qa_reply_recall": f"{qa_hits}/3",
            "evicted_veto_recall": f"{evicted_hits}/2",
            "thread_private_pin_recovered": private_hit,
            "parallel_thread_leak": thread_b_leak,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run reproducible agy-memory benchmark")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    args = parser.parse_args()
    res = run_benchmark()
    if args.json:
        print(json.dumps(res, indent=2))
        return
    print("=== agy-memory Reproducible Benchmark ===")
    for k, v in res.items():
        print(f"  {k:32s}: {v}")


if __name__ == "__main__":
    main()
