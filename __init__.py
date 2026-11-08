"""Perf Lab agent half: measuring tools plus the hooks that make measuring unskippable.

Tools
  perf_bench    time a command N times, fingerprint its output, compare with the baseline
  perf_profile  sample where a Python command spends its time (flame graph in the pane)
  perf_skip     turn the gate off for this chat, with a reason that is shown to the user

Hooks (only active in chats about speed, and only while the gate setting is on)
  pre_llm_call          notices a speed request and tells the agent the rules for this chat
  pre_tool_call         blocks edits to code until a baseline exists for that project
  post_tool_call        records which files were edited, so old numbers are marked stale
  pre_verify            keeps the turn going if code changed after the last measurement
  transform_llm_output  appends the plugin's own measured result and corrects wrong speed claims

Every hook catches its own errors: a Perf Lab bug must never block the agent.
"""

from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("perf_lab")

TOOLSET = "perf_lab"
MAX_BLOCKS_PER_FILE = 2

BENCH_SCHEMA = {
    "name": "perf_bench",
    "description": (
        "Measure how long a script takes. Runs the command several times (no shell), records the median "
        "time and a fingerprint of its output, and compares it with the first measurement (the baseline) "
        "for the same command. Use it BEFORE changing code for speed (that run becomes the baseline) and "
        "AFTER every change. The verdict is decided by the plugin: 'faster' only if at least 90% of "
        "before/after run pairs favour the new code, the median improves 5%+, AND the output is identical. Report the plugin's numbers, not your estimate."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command to time, e.g. 'python backtest.py --year 2024'. Must start with an interpreter (python, node, ruby, uv run ...)."},
            "cwd": {"type": "string", "description": "Absolute path of the project folder to run in."},
            "runs": {"type": "integer", "description": "Timed runs, 1 to 20 (default 5)."},
            "warmup": {"type": "integer", "description": "Untimed runs first, 0 to 3 (default 1)."},
            "timeout_s": {"type": "number", "description": "Seconds allowed per run (default 120, max 600)."},
            "ignore_patterns": {"type": "array", "items": {"type": "string"}, "description": "Regexes for output lines that change every run (timings, dates) and must not count as different output."},
            "compare_files": {"type": "array", "items": {"type": "string"}, "description": "Output files the script writes (e.g. results.csv) that must also stay identical."},
            "set_baseline": {"type": "boolean", "description": "Make this run the new baseline. Only on the ORIGINAL code."},
            "label": {"type": "string", "description": "Short name shown in the pane."},
        },
        "required": ["command", "cwd"],
    },
}

PROFILE_SCHEMA = {
    "name": "perf_profile",
    "description": (
        "Find where a Python command spends its time. Samples the call stack while it runs (py-spy if "
        "installed, otherwise a built-in sampler) and returns the slowest functions; the user sees a "
        "flame graph in the Perf Lab pane. Use it to pick what to optimise, then perf_bench to prove it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Python command, e.g. 'python backtest.py'."},
            "cwd": {"type": "string", "description": "Absolute path of the project folder."},
            "timeout_s": {"type": "number", "description": "Seconds allowed (default 120, max 600)."},
        },
        "required": ["command", "cwd"],
    },
}

SKIP_SCHEMA = {
    "name": "perf_skip",
    "description": (
        "Turn off Perf Lab's measuring gate for this chat when the code truly cannot be timed (it needs a "
        "network service, real hardware, user input, or the task is not about speed). The reason is shown "
        "to the user under your reply, so be honest and specific."
    ),
    "parameters": {
        "type": "object",
        "properties": {"reason": {"type": "string", "description": "One plain sentence: why this cannot be measured."}},
        "required": ["reason"],
    },
}

RULES = (
    "[Perf Lab] This chat is about speed, so the Perf Lab plugin is enforcing measurement:\n"
    "1. Before editing code, run perf_bench on the original code (it becomes the baseline). perf_profile shows where the time goes.\n"
    "2. After each change, run perf_bench again with the SAME command.\n"
    "3. Quote only the plugin's measured result. 'faster' needs identical output; a change that alters output is rejected.\n"
    "If it really cannot be measured, call perf_skip with the reason."
)


def _core():
    from . import perf_core

    return perf_core


def _sid(kwargs: dict) -> str:
    return str(kwargs.get("session_id") or kwargs.get("task_id") or "")


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


# --------------------------------------------------------------------------- tools


def handle_bench(args, **kwargs):
    core = _core()
    try:
        run = core.bench(
            args.get("command"), args.get("cwd"), runs=args.get("runs", 5), warmup=args.get("warmup", 1),
            timeout_s=args.get("timeout_s", 120), ignore_patterns=args.get("ignore_patterns"),
            compare_files=args.get("compare_files"), set_baseline=bool(args.get("set_baseline")),
            label=str(args.get("label") or ""), session_id=_sid(kwargs),
        )
    except core.CommandError as exc:
        return _err(str(exc))
    except Exception as exc:  # never crash the agent turn
        log.exception("perf_bench failed")
        return _err(f"perf_bench failed: {exc}")
    tgt = core.load_targets().get(run["target"], {})
    out = {
        "verdict": run["verdict"], "summary": core.describe(run, tgt), "reason": run.get("reason"),
        "median_s": run.get("median"), "runs_s": run.get("times"), "baseline_median_s": run.get("base_median"),
        "speedup": run.get("ratio"), "output_fingerprint": run.get("fingerprint"),
    }
    if run.get("diff"):
        out["first_output_differences"] = run["diff"]
    if run.get("failed"):
        out["stderr_tail"] = run.get("stderr")
    if run["verdict"] == "baseline":
        out["next"] = "Baseline saved. Edit the code, then run perf_bench again with the same command."
    elif run["verdict"] == "changed_output":
        out["next"] = "Fix the change so the output matches, or tell the user the optimisation changes results."
    return json.dumps(out)


def handle_profile(args, **kwargs):
    core = _core()
    try:
        prof = core.profile(args.get("command"), args.get("cwd"), timeout_s=args.get("timeout_s", 120),
                            session_id=_sid(kwargs))
    except core.CommandError as exc:
        return _err(str(exc))
    except Exception as exc:
        log.exception("perf_profile failed")
        return _err(f"perf_profile failed: {exc}")
    return json.dumps({
        "engine": prof["engine"], "samples": prof["samples"], "wall_s": prof["wall"],
        "hotspot": prof.get("hotspot"), "top_functions": prof["top"],
        "note": "self_pct = time spent in that function's own lines; total_pct = including what it calls. "
                "The flame graph is in the Perf Lab pane.",
    })


def handle_skip(args, **kwargs):
    core = _core()
    reason = str(args.get("reason") or "").strip()[:300]
    sid = _sid(kwargs)
    if not reason:
        return _err("give a reason")
    if not sid:
        return _err("no chat id; nothing to skip")

    def mark(s):
        s["skip"] = {"reason": reason, "at": time.time()}
    core.update_session(sid, mark)
    return json.dumps({"ok": True, "note": "Gate off for this chat. The user will see your reason under the reply."})


# --------------------------------------------------------------------------- hooks


def on_pre_llm_call(session_id="", user_message="", **kwargs):
    try:
        core = _core()
        if not core.settings().get("gate", True) or not session_id:
            return None
        if not core.detect_speed_request(user_message):
            return None
        sess = core.update_session(session_id, lambda s: s.__setitem__("perf_mode", True))
        if sess.get("skip"):
            return None
        return {"context": RULES}
    except Exception:
        log.debug("perf-lab pre_llm_call failed", exc_info=True)
        return None


def on_pre_tool_call(tool_name="", args=None, **kwargs):
    try:
        core = _core()
        if tool_name not in core.EDIT_TOOLS:
            return None
        sid = _sid(kwargs)
        if not sid or not core.settings().get("gate", True):
            return None
        sess = core.load_session(sid)
        if not sess.get("perf_mode") or sess.get("skip"):
            return None
        missing = [p for p in core.paths_in_edit(tool_name, args or {})
                   if core.is_code(p) and not core.covering_target(p, sess)]
        if not missing:
            return None
        key = missing[0]
        if sess.get("blocks", {}).get(key, 0) >= MAX_BLOCKS_PER_FILE:
            # Safety valve: never trap a chat (e.g. tools not loaded yet). Let it through, but say so.
            def note(s):
                s.setdefault("unmeasured", [])
                if key not in s["unmeasured"]:
                    s["unmeasured"].append(key)
            core.update_session(sid, note)
            return None

        def count(s):
            s.setdefault("blocks", {})
            s["blocks"][key] = s["blocks"].get(key, 0) + 1
        core.update_session(sid, count)
        return {
            "action": "block",
            "message": (
                f"Perf Lab: no baseline measurement yet for {missing[0]}. This chat is about speed, so measure "
                "the original code first: perf_bench(command='python <script> ...', cwd='<absolute project folder>'). "
                "Then make the edit. If it cannot be timed, call perf_skip with the reason."
            ),
        }
    except Exception:
        log.debug("perf-lab pre_tool_call failed", exc_info=True)
        return None  # fail open: a Perf Lab bug must not block edits


def on_post_tool_call(tool_name="", args=None, result="", status=None, **kwargs):
    try:
        core = _core()
        if tool_name not in core.EDIT_TOOLS:
            return
        sid = _sid(kwargs)
        if not sid or status in {"error", "blocked"}:
            return
        sess = core.load_session(sid)
        if not sess.get("targets"):
            return
        roots = [t["cwd"] for t in core.session_targets(sess)]
        now = time.time()
        hits = []
        for p in core.paths_in_edit(tool_name, args or {}):
            for cand in core.resolve_against(p, roots):
                if any(core._inside(cand, core.Path(r)) for r in roots) and core.Path(cand).exists():
                    hits.append(cand)
                    break
        if hits:
            def mark(s):
                for h in hits:
                    s["edits"][h] = now
            core.update_session(sid, mark)
    except Exception:
        log.debug("perf-lab post_tool_call failed", exc_info=True)


def on_pre_verify(session_id="", attempt=0, changed_paths=None, **kwargs):
    try:
        core = _core()
        if attempt >= 2 or not session_id or not core.settings().get("gate", True):
            return None
        sess = core.load_session(session_id)
        if not sess.get("perf_mode") or sess.get("skip") or not sess.get("targets"):
            return None
        stale = core.stale_targets(sess)
        if not stale:
            return None
        cmds = "\n".join(
            f"- perf_bench(command={s['target']['command']!r}, cwd={s['target']['cwd']!r})  # changed: "
            + ", ".join(core.Path(f).name for f in s["files"][:4])
            for s in stale[:5]
        )
        return {
            "action": "continue",
            "message": "Perf Lab: the code changed after the last measurement, so there is no proof yet. "
                       f"Re-measure before finishing:\n{cmds}\nThen report the plugin's verdict.",
        }
    except Exception:
        log.debug("perf-lab pre_verify failed", exc_info=True)
        return None


def on_transform_llm_output(response_text="", session_id="", **kwargs):
    try:
        core = _core()
        if not session_id or not response_text:
            return None
        sess = core.load_session(session_id)
        if not sess.get("perf_mode") and not sess.get("targets"):
            return None
        response_text, had_footer = core.split_footer(response_text)
        footer, newest = core.footer_for(response_text, sess, force=had_footer)
        skip = sess.get("skip")
        if skip and not skip.get("shown"):
            footer = (footer or "\n\n---\n**Perf Lab**") + f"\n- ⚠️ Measuring was skipped in this chat: {skip['reason']}"

            def shown(s):
                s["skip"]["shown"] = True
            core.update_session(session_id, shown)
        unmeasured = [u for u in sess.get("unmeasured", []) if u not in sess.get("unmeasured_shown", [])]
        if unmeasured:
            footer = (footer or "\n\n---\n**Perf Lab**") + (
                "\n- ⚠️ Edited without a baseline measurement: " + ", ".join(core.Path(u).name for u in unmeasured[:5])
                + ". Speed claims about these files are unverified.")

            def shown_u(s):
                s["unmeasured_shown"] = list(set(s.get("unmeasured_shown", [])) | set(unmeasured))
            core.update_session(session_id, shown_u)
        if not footer:
            return response_text if had_footer else None
        if newest:
            core.update_session(session_id, lambda s: s.__setitem__("last_footer_run", newest))
        return response_text + footer
    except Exception:
        log.debug("perf-lab transform_llm_output failed", exc_info=True)
        return None


def register(ctx):
    ctx.register_tool(name="perf_bench", toolset=TOOLSET, schema=BENCH_SCHEMA, handler=handle_bench,
                      description=BENCH_SCHEMA["description"], emoji="⏱️")
    ctx.register_tool(name="perf_profile", toolset=TOOLSET, schema=PROFILE_SCHEMA, handler=handle_profile,
                      description=PROFILE_SCHEMA["description"], emoji="🔥")
    ctx.register_tool(name="perf_skip", toolset=TOOLSET, schema=SKIP_SCHEMA, handler=handle_skip,
                      description=SKIP_SCHEMA["description"])
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("pre_verify", on_pre_verify)
    ctx.register_hook("transform_llm_output", on_transform_llm_output)
