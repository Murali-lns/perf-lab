"""Requirement tests for Perf Lab. Run: python -m unittest discover -s tests -v

Each test maps to an agreed behaviour:
  R1 faster + identical output  -> verdict 'faster' with the measured ratio
  R2 faster but different output -> 'changed_output' (rejected), with the differing lines
  R3 difference inside noise     -> 'no_change'
  R4 a quoted speed-up that disagrees with the measurement is corrected under the reply
  R5 in a speed chat, editing code before a baseline exists is blocked
  R6 'done' is held when code changed after the last measurement
  R7 only interpreters run; no inline code; hooks fail open on internal errors
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = Path(sys.executable).name if Path(sys.executable).name.startswith("python") else "python3"
PY = sys.executable  # absolute interpreter path; basename still matches the allowlist


def load_pkg():
    spec = importlib.util.spec_from_file_location("perf_lab", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["perf_lab"] = mod
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="perflab-test-")
        os.environ["PERF_LAB_DATA_DIR"] = self.data
        self.proj = Path(tempfile.mkdtemp(prefix="perflab-proj-"))
        self.pkg = load_pkg()
        from perf_lab import perf_core

        self.core = perf_core

    def tearDown(self):
        shutil.rmtree(self.data, ignore_errors=True)
        shutil.rmtree(self.proj, ignore_errors=True)
        os.environ.pop("PERF_LAB_DATA_DIR", None)

    def script(self, delay, result="42", name="job.py", extra=""):
        (self.proj / name).write_text(textwrap.dedent(f"""
            import time
            time.sleep({delay})
            {extra}
            print("result={result}")
        """))
        return f"{PY} {name}"

    def bench(self, cmd, sid="chat-1", **kw):
        kw.setdefault("runs", 3)
        kw.setdefault("warmup", 0)
        return self.core.bench(cmd, str(self.proj), session_id=sid, **kw)


class TestVerdicts(Base):
    def test_r1_faster_with_identical_output(self):
        cmd = self.script(0.30)
        first = self.bench(cmd)
        self.assertEqual(first["verdict"], "baseline")
        self.script(0.05)
        second = self.bench(cmd)
        self.assertEqual(second["verdict"], "faster")
        self.assertGreater(second["ratio"], 3)
        self.assertEqual(second["fingerprint"], first["fingerprint"])

    def test_r2_faster_but_changed_output_is_rejected(self):
        cmd = self.script(0.30, result="42")
        self.bench(cmd)
        self.script(0.05, result="41")
        run = self.bench(cmd)
        self.assertEqual(run["verdict"], "changed_output")
        self.assertEqual(run["diff"][0]["before"], "result=42")
        self.assertEqual(run["diff"][0]["after"], "result=41")

    def test_r3_noise_is_no_change(self):
        base = {"id": "a", "times": [1.00, 1.10, 0.95], "median": 1.0, "fingerprint": "x"}
        new = {"id": "b", "times": [0.97, 1.05, 0.93], "median": 0.97, "fingerprint": "x"}
        self.assertEqual(self.core.compare(base, new)["verdict"], "no_change")

    def test_one_outlier_does_not_hide_a_real_speedup(self):
        # Times seen on a loaded laptop: the same code swung between 1.7 s and 5.0 s.
        base = {"id": "a", "times": [4.88, 1.70, 4.98, 1.70, 4.97], "median": 4.88, "fingerprint": "x"}
        new = {"id": "b", "times": [0.98, 0.88, 4.08, 0.88, 0.85], "median": 0.88, "fingerprint": "x"}
        res = self.core.compare(base, new)
        self.assertEqual(res["verdict"], "faster")
        self.assertIn("noisy", res["reason"])

    def test_bimodal_machine_noise_still_proves_a_real_speedup(self):
        # Seen live on this laptop: real ~2x speed-up, but 1 run in 3 landed near 4-5 s.
        base = {"id": "a", "times": [1.53, 4.71, 1.55, 4.78, 1.52], "median": 1.55, "fingerprint": "x"}
        new = {"id": "b", "times": [0.82, 4.00, 0.78, 0.83, 0.78], "median": 0.82, "fingerprint": "x"}
        res = self.core.compare(base, new)
        self.assertEqual(res["verdict"], "faster")
        self.assertAlmostEqual(res["ratio"], 1.96, places=2)
        self.assertAlmostEqual(res["before_s"] / res["after_s"], res["ratio"], places=1)  # shown numbers match the verdict

    def test_too_noisy_is_inconclusive_not_no_change(self):
        base = {"id": "a", "times": [1.0, 1.6, 2.2, 3.0, 3.5], "median": 2.2, "fingerprint": "x"}
        new = {"id": "b", "times": [0.8, 1.4, 2.0, 2.8, 3.3], "median": 2.0, "fingerprint": "x"}
        res = self.core.compare(base, new)
        self.assertEqual(res["verdict"], "inconclusive")
        self.assertIn("too much to prove", res["reason"])

    def test_outlier_does_not_invent_a_speedup(self):
        base = {"id": "a", "times": [1.00, 1.02, 0.98, 1.01, 5.0], "median": 1.01, "fingerprint": "x"}
        new = {"id": "b", "times": [0.99, 1.01, 1.00, 0.97, 1.03], "median": 1.00, "fingerprint": "x"}
        self.assertEqual(self.core.compare(base, new)["verdict"], "no_change")

    def test_slower_detected(self):
        base = {"id": "a", "times": [1.0, 1.01, 1.02], "median": 1.01, "fingerprint": "x"}
        new = {"id": "b", "times": [1.5, 1.52, 1.55], "median": 1.52, "fingerprint": "x"}
        self.assertEqual(self.core.compare(base, new)["verdict"], "slower")

    def test_compare_files_must_also_match(self):
        extra = "open('out.csv','w').write('a,1\\n')"
        cmd = self.script(0.2, extra=extra)
        self.bench(cmd, compare_files=["out.csv"])
        self.script(0.02, extra="open('out.csv','w').write('a,2\\n')")
        self.assertEqual(self.bench(cmd, compare_files=["out.csv"])["verdict"], "changed_output")

    def test_ignore_patterns_allow_timing_lines(self):
        extra = "print('took', time.time())"
        cmd = self.script(0.2, extra=extra)
        unstable = self.bench(cmd)
        self.assertEqual(unstable["verdict"], "unstable_output")
        base = self.bench(cmd, ignore_patterns=[r"^took "], set_baseline=True)
        self.assertEqual(base["verdict"], "baseline")
        self.script(0.02, extra=extra)
        self.assertEqual(self.bench(cmd, ignore_patterns=[r"^took "])["verdict"], "faster")

    def test_failing_command_is_failed(self):
        (self.proj / "bad.py").write_text("raise SystemExit(3)\n")
        run = self.bench(f"{PY} bad.py")
        self.assertEqual(run["verdict"], "failed")


class TestCommandSafety(Base):
    def test_r7_only_interpreters(self):
        for bad in ["rm -rf /", "bash -c 'echo hi'", "sh job.py", "curl http://x"]:
            with self.assertRaises(self.core.CommandError):
                self.core.parse_command(bad, str(self.proj))

    def test_r7_no_inline_code(self):
        with self.assertRaises(self.core.CommandError):
            self.core.parse_command(f"{PY} -c 'print(1)'", str(self.proj))

    def test_cwd_must_be_absolute(self):
        with self.assertRaises(self.core.CommandError):
            self.core.parse_command(f"{PY} job.py", "relative/dir")

    def test_compare_file_outside_project_refused(self):
        cmd = self.script(0.01)
        with self.assertRaises(self.core.CommandError):
            self.bench(cmd, compare_files=["/etc/passwd"])


class TestClaims(Base):
    def test_claim_parsing(self):
        self.assertEqual(self.core.speed_claims("Done, it is now 10x faster!"), [10.0])
        self.assertEqual(self.core.speed_claims("a speed-up of about 6.8×"), [6.8])
        self.assertEqual(self.core.speed_claims("```\n99x faster\n```"), [])

    def test_r4_wrong_claim_is_corrected(self):
        cmd = self.script(0.40)
        self.bench(cmd)
        self.script(0.05)
        run = self.bench(cmd)
        sess = self.core.load_session("chat-1")
        footer, _ = self.core.footer_for("Done, it is now 100x faster!", sess)
        self.assertIn("The reply says 100× faster; Perf Lab measured", footer)
        self.assertIn(f"{run['ratio']}× faster", footer)

    def test_close_claim_is_not_flagged(self):
        cmd = self.script(0.40)
        self.bench(cmd)
        self.script(0.05)
        run = self.bench(cmd)
        sess = self.core.load_session("chat-1")
        footer, _ = self.core.footer_for(f"It is about {run['ratio']}x faster.", sess)
        self.assertNotIn("The reply says", footer)

    def test_claim_without_any_measurement(self):
        self.core.update_session("chat-2", lambda s: s.__setitem__("perf_mode", True))
        footer, _ = self.core.footer_for("Optimised it, now 5x faster.", self.core.load_session("chat-2"))
        self.assertIn("nothing was measured", footer)


class TestHooks(Base):
    def speed_turn(self, sid="chat-1"):
        return self.pkg.on_pre_llm_call(session_id=sid, user_message="backtest.py is slow, make it faster")

    def test_gateway_notices_and_memories_do_not_start_speed_mode(self):
        notice = ("[IMPORTANT: Background process proc_1 completed normally (exit code 0).\n"
                  "Command: python make_it_faster.py --optimize\n]\n\n"
                  "[INTERNAL NOTIFICATION - generated by the Hermes gateway, not written by a human user.]")
        self.assertIsNone(self.pkg.on_pre_llm_call(session_id="n", user_message=notice))
        memory = "yes do it\n\n<memory-context>\n- The build was 3x faster after the fix\n</memory-context>"
        self.assertIsNone(self.pkg.on_pre_llm_call(session_id="n", user_message=memory))
        self.assertFalse(self.core.load_session("n")["perf_mode"])
        typed = "this is slow, speed it up\n\n<memory-context>\n- unrelated\n</memory-context>"
        self.assertIn("[Perf Lab]", self.pkg.on_pre_llm_call(session_id="n", user_message=typed)["context"])

    def test_talking_about_perf_lab_itself_is_not_a_speed_request(self):
        for msg in ["record a new video with perf lab opened on the side",
                    "update the perf-lab README", "set my profile picture"]:
            self.assertIsNone(self.pkg.on_pre_llm_call(session_id="p", user_message=msg), msg)
        self.assertIn("[Perf Lab]", self.pkg.on_pre_llm_call(session_id="p", user_message="profile this script, it is slow")["context"])

    def test_rules_injected_only_for_speed_requests(self):
        self.assertIsNone(self.pkg.on_pre_llm_call(session_id="c", user_message="rename this variable"))
        self.assertIn("[Perf Lab]", self.speed_turn()["context"])

    def test_r5_edit_blocked_before_baseline_then_allowed(self):
        cmd = self.script(0.05)
        self.speed_turn()
        args = {"path": str(self.proj / "job.py"), "content": "x"}
        res = self.pkg.on_pre_tool_call(tool_name="write_file", args=args, session_id="chat-1")
        self.assertEqual(res["action"], "block")
        self.assertIn("no baseline", res["message"])
        self.bench(cmd)
        self.assertIsNone(self.pkg.on_pre_tool_call(tool_name="write_file", args=args, session_id="chat-1"))
        rel = {"path": "job.py", "old_string": "a", "new_string": "b"}
        self.assertIsNone(self.pkg.on_pre_tool_call(tool_name="patch", args=rel, session_id="chat-1"))

    def test_gate_never_traps_a_chat(self):
        self.script(0.05)
        self.speed_turn()
        args = {"path": str(self.proj / "job.py"), "content": "x"}
        for _ in range(2):
            self.assertEqual(self.pkg.on_pre_tool_call(tool_name="write_file", args=args, session_id="chat-1")["action"], "block")
        self.assertIsNone(self.pkg.on_pre_tool_call(tool_name="write_file", args=args, session_id="chat-1"))
        text = self.pkg.on_transform_llm_output(response_text="Done.", session_id="chat-1")
        self.assertIn("Edited without a baseline measurement: job.py", text)

    def test_gate_ignores_non_speed_chats_and_non_code(self):
        args = {"path": str(self.proj / "job.py"), "content": "x"}
        self.assertIsNone(self.pkg.on_pre_tool_call(tool_name="write_file", args=args, session_id="other"))
        self.speed_turn()
        notes = {"path": str(self.proj / "NOTES.md"), "content": "x"}
        self.assertIsNone(self.pkg.on_pre_tool_call(tool_name="write_file", args=notes, session_id="chat-1"))

    def test_gate_switch_off(self):
        self.speed_turn()
        self.core.save_settings(gate=False)
        args = {"path": str(self.proj / "job.py"), "content": "x"}
        self.assertIsNone(self.pkg.on_pre_tool_call(tool_name="write_file", args=args, session_id="chat-1"))

    def test_skip_is_shown_to_user(self):
        self.speed_turn()
        out = json.loads(self.pkg.handle_skip({"reason": "needs the live exchange API"}, session_id="chat-1"))
        self.assertTrue(out["ok"])
        args = {"path": str(self.proj / "job.py"), "content": "x"}
        self.assertIsNone(self.pkg.on_pre_tool_call(tool_name="write_file", args=args, session_id="chat-1"))
        text = self.pkg.on_transform_llm_output(response_text="Done.", session_id="chat-1")
        self.assertIn("needs the live exchange API", text)

    def test_r6_done_held_until_remeasured(self):
        cmd = self.script(0.2)
        self.speed_turn()
        self.bench(cmd)
        self.assertIsNone(self.pkg.on_pre_verify(session_id="chat-1", attempt=0, changed_paths=["job.py"]))
        time.sleep(0.01)
        self.script(0.05)  # the agent edits the code ...
        self.pkg.on_post_tool_call(tool_name="write_file", args={"path": str(self.proj / "job.py")}, session_id="chat-1")
        res = self.pkg.on_pre_verify(session_id="chat-1", attempt=0, changed_paths=["job.py"])
        self.assertEqual(res["action"], "continue")
        self.assertIn("perf_bench(command=", res["message"])
        self.assertIsNone(self.pkg.on_pre_verify(session_id="chat-1", attempt=2, changed_paths=["job.py"]))
        self.bench(cmd)  # ... and re-measures
        self.assertIsNone(self.pkg.on_pre_verify(session_id="chat-1", attempt=0, changed_paths=["job.py"]))

    def test_r6_edit_by_terminal_is_caught_by_file_hash(self):
        cmd = self.script(0.2)
        self.speed_turn()
        self.bench(cmd)
        self.script(0.1)  # changed without write_file/patch (e.g. sed in a terminal)
        res = self.pkg.on_pre_verify(session_id="chat-1", attempt=0, changed_paths=["job.py"])
        self.assertEqual(res["action"], "continue")

    def test_footer_appended_once_per_new_result(self):
        cmd = self.script(0.3)
        self.speed_turn()
        self.bench(cmd)
        self.script(0.05)
        self.bench(cmd)
        first = self.pkg.on_transform_llm_output(response_text="All done.", session_id="chat-1")
        self.assertIn("**Perf Lab** (measured by the plugin", first)
        self.assertIn("faster", first)
        self.assertIsNone(self.pkg.on_transform_llm_output(response_text="Anything else?", session_id="chat-1"))

    def test_footer_never_stacks_when_hermes_transforms_twice(self):
        # Seen live: the output transform ran twice on one reply and two footers stacked.
        cmd = self.script(0.3)
        self.speed_turn()
        self.bench(cmd)
        self.script(0.05)
        self.bench(cmd)
        once = self.pkg.on_transform_llm_output(response_text="Done.", session_id="chat-1")
        twice = self.pkg.on_transform_llm_output(response_text=once, session_id="chat-1")
        self.assertEqual(twice.count("**Perf Lab**"), 1)
        self.assertTrue(twice.startswith("Done."))

    def test_quoting_a_rejected_runs_ratio_is_not_called_unbacked(self):
        cmd = self.script(0.4, result="42")
        self.bench(cmd)
        self.script(0.05, result="41")
        run = self.bench(cmd)
        sess = self.core.load_session("chat-1")
        footer, _ = self.core.footer_for(f"It ran {run['ratio']}x faster but the output changed.", sess)
        self.assertIn("output CHANGED", footer)
        self.assertNotIn("no measured speed-up backs it", footer)

    def test_r7_hooks_fail_open(self):
        orig = self.core.load_session
        self.core.load_session = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertIsNone(self.pkg.on_pre_tool_call(tool_name="write_file", args={"path": "a.py"}, session_id="x"))
            self.assertIsNone(self.pkg.on_pre_verify(session_id="x", attempt=0, changed_paths=["a.py"]))
            self.assertIsNone(self.pkg.on_transform_llm_output(response_text="hi", session_id="x"))
        finally:
            self.core.load_session = orig


class TestToolsAndProfile(Base):
    def test_bench_tool_returns_json(self):
        cmd = self.script(0.05)
        out = json.loads(self.pkg.handle_bench({"command": cmd, "cwd": str(self.proj), "runs": 2, "warmup": 0}, session_id="s"))
        self.assertEqual(out["verdict"], "baseline")
        bad = json.loads(self.pkg.handle_bench({"command": "rm -rf /", "cwd": str(self.proj)}, session_id="s"))
        self.assertIn("not an allowed program", bad["error"])

    def test_profile_finds_hotspot_with_builtin_sampler(self):
        (self.proj / "hot.py").write_text(textwrap.dedent("""
            def slow_part():
                t = 0
                for i in range(3_000_000):
                    t += i % 7
                return t

            def quick_part():
                return sum(range(1000))

            if __name__ == "__main__":
                print(slow_part() + quick_part())
        """))
        orig = shutil.which
        self.core.shutil.which = lambda name: None if name == "py-spy" else orig(name)
        try:
            out = json.loads(self.pkg.handle_profile({"command": f"{PY} hot.py", "cwd": str(self.proj)}, session_id="s"))
        finally:
            self.core.shutil.which = orig
        self.assertEqual(out["engine"], "built-in sampler")
        self.assertIn("slow_part", out["hotspot"]["name"])
        self.assertGreater(out["hotspot"]["self_pct"], 50)


if __name__ == "__main__":
    unittest.main()
