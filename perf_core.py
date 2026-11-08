"""Perf Lab core: measuring, profiling, comparing and per-chat gate state.

Pure stdlib. Imported by the agent half (``__init__.py``) and, by file path, by the
dashboard router, so both read and write the same JSON files under the plugin's data
directory. Every write is atomic (temp file + ``os.replace``) and serialised with a
file lock, because the agent and the router are separate module copies.

Nothing here talks to the network. Commands run with an argument list (never a
shell) and only when the program is a known interpreter.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

try:  # POSIX only; Windows falls back to the in-process lock.
    import fcntl
except ImportError:  # pragma: no cover - windows
    fcntl = None

PLUGIN_NAME = "perf-lab"
HERE = Path(__file__).resolve().parent
SAMPLER = HERE / "sampler.py"

MAX_RUNS = 20
MAX_TIMEOUT_S = 600
MAX_TOTAL_S = 900
MAX_OUTPUT_BYTES = 5 * 1024 * 1024
KEEP_OUTPUT_CHARS = 200_000
MAX_PATTERNS = 20
MAX_COMPARE_FILES = 10
MAX_RUNS_KEPT = 50
MIN_RATIO = 1.05  # under 5 % is never called a speed-up
WIN_SHARE = 0.9  # share of before/after run pairs that must favour one side
CLAIM_TOLERANCE = 0.20  # a quoted "N× faster" within 20 % of the measurement is accepted

ID_RE = re.compile(r"^[a-f0-9]{12}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,200}$")
INTERPRETER_RE = re.compile(
    r"^(python(\d+(\.\d+)?)?|pypy3?|node|bun|deno|ruby|perl|php|Rscript|julia|uv)$"
)
EVAL_FLAGS = {"-c", "-e", "--eval", "-p", "--print", "-E", "-r"}
CODE_EXT = (
    ".py", ".pyx", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".rb", ".go", ".rs",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".java", ".kt", ".jl", ".r", ".R", ".php", ".pl",
)
EDIT_TOOLS = {"write_file", "patch"}
SPEED_WORDS = re.compile(
    r"\b(faster|speed\s?-?up|sped\s+up|slow(er|ness|ly)?|optimi[sz](e|es|ed|ing|ation)|"
    r"performance|perf\b|takes?\s+(too\s+)?long|bottleneck|profil(e|er|ing)|latency|"
    r"quicker|run\s?time)\b",
    re.I,
)


# --------------------------------------------------------------------------- storage


def data_dir() -> Path:
    override = os.environ.get("PERF_LAB_DATA_DIR")
    if override:
        root = Path(override)
        root.mkdir(parents=True, exist_ok=True)
        return root
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir(PLUGIN_NAME)
    except Exception:  # running outside Hermes (tests, CLI)
        root = Path(os.path.expanduser("~/.hermes/plugin-data")) / PLUGIN_NAME
        root.mkdir(parents=True, exist_ok=True)
        return root


@contextlib.contextmanager
def locked():
    path = data_dir() / ".lock"
    with open(path, "a+") as fh:
        if fcntl:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _targets_path() -> Path:
    return data_dir() / "targets.json"


def _session_path(session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    return data_dir() / "sessions" / f"{digest}.json"


def _run_path(run_id: str) -> Path:
    return data_dir() / "runs" / f"{run_id}.json"


def _profile_path(prof_id: str) -> Path:
    return data_dir() / "profiles" / f"{prof_id}.json"


def load_targets() -> dict:
    return _read(_targets_path(), {})


def load_run(run_id: str) -> dict | None:
    if not ID_RE.match(run_id or ""):
        return None
    return _read(_run_path(run_id), None)


def load_profile(prof_id: str) -> dict | None:
    if not ID_RE.match(prof_id or ""):
        return None
    return _read(_profile_path(prof_id), None)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------- sessions


def _blank_session() -> dict:
    return {"perf_mode": False, "targets": [], "edits": {}, "skip": None,
            "last_footer_run": None, "blocked": 0, "updated": 0}


def load_session(session_id: str) -> dict:
    if not session_id or not SESSION_RE.match(session_id):
        return _blank_session()
    data = _read(_session_path(session_id), None)
    return {**_blank_session(), **data} if isinstance(data, dict) else _blank_session()


def update_session(session_id: str, fn) -> dict:
    if not session_id or not SESSION_RE.match(session_id):
        return _blank_session()
    with locked():
        sess = load_session(session_id)
        fn(sess)
        sess["updated"] = time.time()
        _write(_session_path(session_id), sess)
        return sess


def settings() -> dict:
    return {"gate": True, **_read(data_dir() / "settings.json", {})}


def save_settings(**changes) -> dict:
    with locked():
        cur = settings()
        cur.update({k: v for k, v in changes.items() if k in {"gate"}})
        _write(data_dir() / "settings.json", cur)
        return cur


def detect_speed_request(text: Any) -> bool:
    if isinstance(text, list):  # multimodal turns: join the text parts
        text = " ".join(p.get("text", "") for p in text if isinstance(p, dict))
    return bool(text) and bool(SPEED_WORDS.search(str(text)))


# --------------------------------------------------------------------------- commands


class CommandError(ValueError):
    pass


def parse_command(command: str, cwd: str) -> tuple[list[str], Path]:
    if not isinstance(command, str) or not command.strip():
        raise CommandError("command is empty")
    if len(command) > 4000:
        raise CommandError("command is too long")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise CommandError(f"could not parse command: {exc}") from exc
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise CommandError("cwd must be an absolute path to the project folder")
    root = Path(cwd).resolve()
    if not root.is_dir():
        raise CommandError(f"cwd does not exist: {cwd}")
    prog = os.path.basename(argv[0])
    if not INTERPRETER_RE.match(prog):
        raise CommandError(
            f"'{prog}' is not an allowed program. Perf Lab only runs scripts through an interpreter "
            "(python, node, ruby, uv run, ...), so it never becomes a way around Hermes' command approvals."
        )
    if prog == "uv" and (len(argv) < 3 or argv[1] != "run"):
        raise CommandError("uv is allowed only as 'uv run <interpreter or script> ...'")
    if any(a in EVAL_FLAGS for a in argv[1:4]):
        raise CommandError("inline code (-c / -e) is not allowed; benchmark a script file")
    return argv, root


def script_files(argv: list[str], root: Path) -> list[str]:
    """Code files named on the command line, as absolute paths."""
    found = []
    for arg in argv[1:]:
        if arg.startswith("-"):
            continue
        cand = (root / arg) if not os.path.isabs(arg) else Path(arg)
        if cand.suffix in CODE_EXT and cand.is_file():
            found.append(str(cand.resolve()))
    return found


def target_id(argv: list[str], root: Path) -> str:
    key = json.dumps([str(root), argv])
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def file_hashes(paths: Iterable[str]) -> dict:
    out = {}
    for p in sorted(set(paths)):
        try:
            out[p] = hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]
        except OSError:
            out[p] = "missing"
    return out


def _normalise(text: str, patterns: list[re.Pattern]) -> str:
    if not patterns:
        return text
    return "\n".join(line for line in text.splitlines() if not any(p.search(line) for p in patterns))


def _compile_patterns(raw: Any) -> list[re.Pattern]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_PATTERNS:
        raise CommandError(f"ignore_patterns must be a list of at most {MAX_PATTERNS} regexes")
    out = []
    for pat in raw:
        if not isinstance(pat, str) or not pat or len(pat) > 300:
            raise CommandError("each ignore pattern must be a short non-empty string")
        try:
            out.append(re.compile(pat))
        except re.error as exc:
            raise CommandError(f"bad ignore pattern {pat!r}: {exc}") from exc
    return out


def _compare_paths(raw: Any, root: Path) -> list[Path]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_COMPARE_FILES:
        raise CommandError(f"compare_files must be a list of at most {MAX_COMPARE_FILES} paths")
    out = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise CommandError("compare_files entries must be paths")
        p = (root / item).resolve() if not os.path.isabs(item) else Path(item).resolve()
        if root not in p.parents and p != root:
            raise CommandError(f"compare file must be inside the project folder: {item}")
        out.append(p)
    return out


def _run_once(argv: list[str], root: Path, timeout: float) -> dict:
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            argv, cwd=str(root), stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"seconds": round(time.perf_counter() - start, 4), "exit": None, "timeout": True,
                "stdout": "", "stderr": f"timed out after {timeout:.0f} s"}
    except OSError as exc:
        raise CommandError(f"could not start the command: {exc}") from exc
    seconds = time.perf_counter() - start
    out = proc.stdout[:MAX_OUTPUT_BYTES].decode("utf-8", "replace")
    err = proc.stderr[-4000:].decode("utf-8", "replace")
    return {"seconds": round(seconds, 4), "exit": proc.returncode, "timeout": False, "stdout": out, "stderr": err}


def _fingerprint(stdout: str, exit_code: Any, files: list[Path]) -> tuple[str, dict]:
    h = hashlib.sha256()
    h.update(f"exit={exit_code}\n".encode())
    h.update(stdout.encode("utf-8", "replace"))
    file_fp = {}
    for p in files:
        try:
            digest = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        except OSError:
            digest = "missing"
        file_fp[str(p)] = digest
        h.update(f"\n--file {p}={digest}".encode())
    return h.hexdigest()[:16], file_fp


def _first_difference(a: str, b: str, limit: int = 3) -> list[dict]:
    la, lb = a.splitlines(), b.splitlines()
    diffs = []
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else "<missing>"
        y = lb[i] if i < len(lb) else "<missing>"
        if x != y:
            diffs.append({"line": i + 1, "before": x[:200], "after": y[:200]})
            if len(diffs) >= limit:
                break
    return diffs


def compare(base: dict, new: dict) -> dict:
    """Decide what a new run means against the baseline. Pure function, unit tested."""
    if new.get("failed"):
        return {"verdict": "failed", "ratio": None,
                "reason": "the command failed or timed out, so its speed means nothing"}
    if new.get("unstable"):
        return {"verdict": "unstable_output", "ratio": None,
                "reason": "the output differs between runs of the same code; add ignore_patterns for "
                          "lines that change every run (timings, dates, random seeds)"}
    if base is None or base.get("id") == new.get("id"):
        return {"verdict": "baseline", "ratio": 1.0, "reason": "first measurement, saved as the baseline"}
    if new["fingerprint"] != base["fingerprint"]:
        return {"verdict": "changed_output", "ratio": _ratio(base, new),
                "reason": "the output is different from the baseline, so the change is not accepted"}
    # Background load only ever ADDS time, so compare the quickest half of each set of runs
    # (the same reasoning as Python's timeit docs). A slow outlier then cannot hide a real
    # speed-up, and one lucky run cannot invent one.
    bq, nq = quick_half(base["times"]), quick_half(new["times"])
    ratio = round(statistics.median(bq) / statistics.median(nq), 2) if nq and statistics.median(nq) > 0 else None
    win = win_share(bq, nq)
    shown = {"before_s": round(statistics.median(bq), 4) if bq else None,
             "after_s": round(statistics.median(nq), 4) if nq else None}
    noisy = _noisy(base["times"]) or _noisy(new["times"])
    tail = " (this machine was noisy; more runs give a firmer answer)" if noisy else ""
    if win >= WIN_SHARE and ratio is not None and ratio >= MIN_RATIO:
        return {**shown, "verdict": "faster", "ratio": ratio, "win_share": win,
                "reason": f"{round(win * 100)}% of before/after run pairs (quickest half of each) favour the new code; output identical{tail}"}
    if (1 - win) >= WIN_SHARE and ratio is not None and ratio <= 1 / MIN_RATIO:
        return {**shown, "verdict": "slower", "ratio": ratio, "win_share": win,
                "reason": f"{round((1 - win) * 100)}% of before/after run pairs favour the old code{tail}"}
    if noisy and ratio is not None and (ratio >= MIN_RATIO or ratio <= 1 / MIN_RATIO):
        return {**shown, "verdict": "inconclusive", "ratio": ratio, "win_share": win,
                "reason": f"the median moved {ratio}x, but run times jumped around too much to prove it "
                          f"({round(win * 100)}% of pairs agree, 90% needed). Re-run with more runs on a quieter machine"}
    return {**shown, "verdict": "no_change", "ratio": ratio, "win_share": win,
            "reason": "the before and after run times overlap too much to call it a real change" + tail}


def quick_half(times: list[float]) -> list[float]:
    ordered = sorted(t for t in times if isinstance(t, (int, float)))
    if len(ordered) < 3:
        return ordered
    return ordered[: (len(ordered) + 1) // 2]


def win_share(before: list[float], after: list[float]) -> float:
    """Share of (before, after) pairs where the after run was quicker; ties count half.

    The same idea as a Mann-Whitney test: one slow outlier run moves it by a few
    percent instead of hiding a real speed-up (or inventing one)."""
    if not before or not after:
        return 0.5
    score = sum(1.0 if a < b else 0.5 if a == b else 0.0 for b in before for a in after)
    return round(score / (len(before) * len(after)), 3)


def _noisy(times: list[float]) -> bool:
    if len(times) < 2:
        return False
    med = statistics.median(times)
    return med > 0 and (max(times) - min(times)) / med > 0.5


def _ratio(base: dict, new: dict) -> float | None:
    try:
        return round(base["median"] / new["median"], 2) if new["median"] > 0 else None
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def bench(command: str, cwd: str, *, runs: int = 5, warmup: int = 1, timeout_s: float = 120,
          ignore_patterns: Any = None, compare_files: Any = None, set_baseline: bool = False,
          label: str = "", session_id: str = "") -> dict:
    argv, root = parse_command(command, cwd)
    if isinstance(runs, bool) or not isinstance(runs, int) or not 1 <= runs <= MAX_RUNS:
        raise CommandError(f"runs must be a whole number from 1 to {MAX_RUNS}")
    if isinstance(warmup, bool) or not isinstance(warmup, int) or not 0 <= warmup <= 3:
        raise CommandError("warmup must be 0 to 3")
    timeout = float(timeout_s or 120)
    if not 1 <= timeout <= MAX_TIMEOUT_S:
        raise CommandError(f"timeout_s must be 1 to {MAX_TIMEOUT_S}")
    patterns = _compile_patterns(ignore_patterns)
    cmp_files = _compare_paths(compare_files, root)
    tid = target_id(argv, root)

    started = time.time()
    deadline = time.monotonic() + MAX_TOTAL_S
    samples, prints = [], set()
    first_out = ""
    failed = False
    stderr_tail = ""
    for i in range(warmup + runs):
        if time.monotonic() > deadline:
            raise CommandError("benchmark took longer than 15 minutes in total; use fewer runs")
        res = _run_once(argv, root, timeout)
        norm = _normalise(res["stdout"], patterns)
        fp, file_fp = _fingerprint(norm, res["exit"], cmp_files)
        if res["timeout"] or res["exit"] != 0:
            failed = True
            stderr_tail = res["stderr"][-1500:]
            break
        prints.add(fp)
        if i == 0:
            first_out = norm
        if i >= warmup:
            samples.append(res["seconds"])

    sess = load_session(session_id) if session_id else _blank_session()
    with locked():
        targets = load_targets()
        tgt = targets.get(tid) or {
            "id": tid, "command": command, "argv": argv, "cwd": str(root), "label": "",
            "created": started, "baseline": None, "runs": [], "profiles": [], "watch": [],
        }
        tgt["label"] = (label or tgt.get("label") or _default_label(argv))[:80]
        tgt["command"] = command
        edited = [p for p in sess.get("edits", {}) if _inside(p, root)]
        watch = sorted(set(tgt.get("watch", [])) | set(script_files(argv, root)) | set(edited))[:200]
        tgt["watch"] = watch
        run = {
            "id": new_id(), "target": tid, "at": started, "session": _short(session_id),
            "times": samples, "runs": len(samples), "warmup": warmup,
            "median": round(statistics.median(samples), 4) if samples else None,
            "min": min(samples) if samples else None, "max": max(samples) if samples else None,
            "stdev": round(statistics.stdev(samples), 4) if len(samples) > 1 else 0.0,
            "fingerprint": next(iter(prints)) if len(prints) == 1 else None,
            "unstable": len(prints) > 1, "failed": failed, "stderr": stderr_tail,
            "output": first_out[:KEEP_OUTPUT_CHARS], "compare_files": [str(p) for p in cmp_files],
            "patterns": [p.pattern for p in patterns], "code": file_hashes(watch),
        }
        base = load_run(tgt["baseline"]) if tgt.get("baseline") else None
        if (set_baseline or base is None or base.get("failed") or base.get("unstable")) and not failed and not run["unstable"]:
            tgt["baseline"] = run["id"]
            base = run
        elif base is not None and (run["patterns"] != base.get("patterns") or run["compare_files"] != base.get("compare_files")):
            # Different normalisation would make fingerprints incomparable: re-baseline is required.
            run["note"] = "ignore_patterns/compare_files differ from the baseline; pass set_baseline=true on the original code to compare"
        result = compare(base, run)
        if run.get("note") and result["verdict"] not in {"failed", "unstable_output"}:
            result = {"verdict": "no_comparison", "ratio": None, "reason": run["note"]}
        run.update(result)
        run["baseline"] = tgt.get("baseline")
        if result["verdict"] == "changed_output" and base:
            run["diff"] = _first_difference(base.get("output", ""), run["output"])
        if base and base is not run:
            run["base_median"] = base.get("median")
        _write(_run_path(run["id"]), run)
        tgt["runs"] = (tgt.get("runs", []) + [run["id"]])[-MAX_RUNS_KEPT:]
        tgt["updated"] = time.time()
        targets[tid] = tgt
        _write(_targets_path(), targets)

    if session_id:
        def mark(s):
            s["perf_mode"] = True
            if tid not in s["targets"]:
                s["targets"].append(tid)
        update_session(session_id, mark)
    return run


def _short(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:10] if session_id else ""


def _default_label(argv: list[str]) -> str:
    for a in argv[1:]:
        if not a.startswith("-") and a not in {"run"}:
            return os.path.basename(a)
    return os.path.basename(argv[0])


def _inside(path: str, root: Path) -> bool:
    try:
        p = Path(path).resolve()
    except (OSError, ValueError):
        return False
    return p == root or root in p.parents


# --------------------------------------------------------------------------- profiling


def profile(command: str, cwd: str, *, timeout_s: float = 120, session_id: str = "") -> dict:
    argv, root = parse_command(command, cwd)
    timeout = float(timeout_s or 120)
    if not 1 <= timeout <= MAX_TIMEOUT_S:
        raise CommandError(f"timeout_s must be 1 to {MAX_TIMEOUT_S}")
    prog = os.path.basename(argv[0])
    py_argv = argv[2:] if prog == "uv" else argv
    if not re.match(r"^(python(\d+(\.\d+)?)?|pypy3?)$", os.path.basename(py_argv[0])):
        raise CommandError("profiling supports Python commands only (python script.py ...); "
                           "perf_bench still works for other languages")
    out_file = Path(tempfile.mkstemp(prefix="perf-lab-", suffix=".txt")[1])
    pyspy = shutil.which("py-spy")
    if pyspy:
        cmd = [pyspy, "record", "--format", "raw", "--rate", "200", "--output", str(out_file), "--", *argv]
        engine = "py-spy"
    else:
        prefix = argv[:2] if prog == "uv" else []
        cmd = [*prefix, py_argv[0], str(SAMPLER), "--out", str(out_file), "--", *py_argv[1:]]
        engine = "built-in sampler"
    start = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=str(root), stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        exit_code = proc.returncode
        err = proc.stderr[-1500:].decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        exit_code, err = None, f"timed out after {timeout:.0f} s"
    wall = time.perf_counter() - start
    try:
        collapsed = out_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        collapsed = ""
    finally:
        with contextlib.suppress(OSError):
            out_file.unlink()
    stacks = parse_collapsed(collapsed)
    if not stacks:
        raise CommandError(f"no samples were collected (exit {exit_code}). {err.strip()[:500]}")
    tree, total = build_tree(stacks)
    tops = top_functions(stacks, total, root)
    tid = target_id(argv, root)
    prof = {
        "id": new_id(), "target": tid, "at": time.time(), "engine": engine, "wall": round(wall, 3),
        "exit": exit_code, "samples": total, "tree": tree, "top": tops, "stderr": err if exit_code else "",
        "hotspot": next((t for t in tops if t["user"]), tops[0] if tops else None),
    }
    with locked():
        targets = load_targets()
        tgt = targets.get(tid) or {
            "id": tid, "command": command, "argv": argv, "cwd": str(root), "label": _default_label(argv),
            "created": time.time(), "baseline": None, "runs": [], "profiles": [], "watch": [],
        }
        tgt["watch"] = sorted(set(tgt.get("watch", [])) | set(script_files(argv, root)))[:200]
        tgt["profiles"] = (tgt.get("profiles", []) + [prof["id"]])[-10:]
        tgt["updated"] = time.time()
        targets[tid] = tgt
        _write(_profile_path(prof["id"]), prof)
        _write(_targets_path(), targets)
    if session_id:
        def mark(s):
            s["perf_mode"] = True
            if tid not in s["targets"]:
                s["targets"].append(tid)
        update_session(session_id, mark)
    return prof


_LINE_RE = re.compile(r"^(.*)\s+(\d+)$")
_SKIP_FRAME = re.compile(r"(sampler\.py|<frozen runpy>|runpy\.py|^<module> \(<string>)")


def parse_collapsed(text: str) -> list[tuple[list[str], int]]:
    out = []
    for line in text.splitlines()[:200_000]:
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        frames = [f for f in m.group(1).split(";") if f]
        # py-spy raw lines start with "process ..."/"thread ..." frames; drop wrapper frames too.
        frames = [f for f in frames if not f.startswith(("process ", "thread ")) and not _SKIP_FRAME.search(f)]
        if frames:
            out.append((frames[:80], int(m.group(2))))
    return out


def build_tree(stacks: list[tuple[list[str], int]], min_share: float = 0.005) -> tuple[dict, int]:
    total = sum(n for _, n in stacks)
    root: dict = {"name": "all", "value": 0, "self": 0, "children": {}}
    for frames, n in stacks:
        node = root
        node["value"] += n
        for f in frames:
            node = node["children"].setdefault(f, {"name": f, "value": 0, "self": 0, "children": {}})
            node["value"] += n
        node["self"] += n
    budget = [3000]

    def finish(node):
        kids = sorted(node["children"].values(), key=lambda c: -c["value"])
        kept = []
        for k in kids:
            if k["value"] / max(total, 1) < min_share or budget[0] <= 0:
                continue
            budget[0] -= 1
            kept.append(finish(k))
        return {"name": node["name"], "value": node["value"], "self": node["self"], "children": kept}

    return finish(root), total


def _is_user(frame: str, root: Path) -> bool:
    m = re.search(r"\((.*?)(?::\d+)?\)$", frame)
    path = m.group(1) if m else ""
    if not path or "site-packages" in path or "dist-packages" in path or path.startswith("<"):
        return False
    if "/lib/python" in path and not str(root) in path:
        return False
    return True


def top_functions(stacks, total: int, root: Path, n: int = 8) -> list[dict]:
    own: dict[str, int] = {}
    incl: dict[str, int] = {}
    for frames, cnt in stacks:
        own[frames[-1]] = own.get(frames[-1], 0) + cnt
        for f in set(frames):
            incl[f] = incl.get(f, 0) + cnt
    ranked = sorted(incl.items(), key=lambda kv: (-own.get(kv[0], 0), -kv[1]))
    out = []
    for name, inc in ranked[: n * 3]:
        out.append({"name": name, "self_pct": round(100 * own.get(name, 0) / max(total, 1), 1),
                    "total_pct": round(100 * inc / max(total, 1), 1), "user": _is_user(name, root)})
    out.sort(key=lambda t: (-t["self_pct"], -t["total_pct"]))
    return out[:n]


# --------------------------------------------------------------------------- gate helpers


def paths_in_edit(tool_name: str, args: dict) -> list[str]:
    if tool_name not in EDIT_TOOLS or not isinstance(args, dict):
        return []
    found = []
    if isinstance(args.get("path"), str):
        found.append(args["path"])
    body = args.get("patch")
    if isinstance(body, str):
        found += re.findall(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", body, re.M)
    return [p for p in found if p]


def is_code(path: str) -> bool:
    return path.endswith(CODE_EXT)


def resolve_against(path: str, roots: Iterable[str]) -> list[str]:
    """Absolute candidates for an edited path (relative paths are tried against each project)."""
    if os.path.isabs(path):
        return [str(Path(path).resolve())]
    return [str((Path(r) / path).resolve()) for r in roots]


def session_targets(sess: dict) -> list[dict]:
    targets = load_targets()
    return [targets[t] for t in sess.get("targets", []) if t in targets]


def covering_target(path: str, sess: dict) -> dict | None:
    """A target in this chat that has a baseline and whose project folder holds *path*."""
    for tgt in session_targets(sess):
        if not tgt.get("baseline"):
            continue
        for cand in resolve_against(path, [tgt["cwd"]]):
            if _inside(cand, Path(tgt["cwd"])):
                return tgt
    return None


def latest_run(tgt: dict) -> dict | None:
    for rid in reversed(tgt.get("runs", [])):
        run = load_run(rid)
        if run:
            return run
    return None


def stale_targets(sess: dict) -> list[dict]:
    """Targets whose code changed after their last measurement (edit seen or file hash moved)."""
    stale = []
    for tgt in session_targets(sess):
        run = latest_run(tgt)
        if not run:
            continue
        root = Path(tgt["cwd"])
        edited_after = [p for p, ts in sess.get("edits", {}).items() if ts > run["at"] and _inside(p, root)]
        moved = [p for p, h in file_hashes(run.get("code", {}).keys()).items() if run["code"].get(p) != h]
        if edited_after or moved:
            stale.append({"target": tgt, "files": sorted(set(edited_after + moved))})
    return stale


# --------------------------------------------------------------------------- claims

_CLAIM_RES = [
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:x|×|times)\s+(?:faster|quicker|speed\s?-?up)", re.I),
    re.compile(r"(?:speed\s?-?up|faster)\s+(?:of|by)\s+(?:about\s+|around\s+|roughly\s+|~)?(\d+(?:\.\d+)?)\s*(?:x|×)", re.I),
    re.compile(r"(?:speed\s?-?up|faster)[^.\n]{0,20}?\b(\d+(?:\.\d+)?)\s*(?:x|×)(?![a-z])", re.I),
]
_QUALITATIVE = re.compile(r"\b(faster|sped\s+up|speed\s?-?up|quicker|more\s+performant|optimi[sz]ed)\b", re.I)
_CODE_BLOCK = re.compile(r"```.*?```", re.S)


def speed_claims(text: str) -> list[float]:
    prose = _CODE_BLOCK.sub("", text or "")
    vals = []
    for rx in _CLAIM_RES:
        for m in rx.finditer(prose):
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            if 1 < v < 100_000:
                vals.append(v)
    return sorted(set(vals))


def claims_speed(text: str) -> bool:
    return bool(_QUALITATIVE.search(_CODE_BLOCK.sub("", text or "")))


def fmt_s(v: Any) -> str:
    if v is None:
        return "?"
    return f"{v:.2f} s" if v >= 1 else f"{v * 1000:.0f} ms"


VERDICT_TEXT = {
    "faster": "✅ {label}: {base} → {new} ({ratio}× faster, {n} runs each), output identical",
    "slower": "⚠️ {label}: {base} → {new}, slower than before",
    "no_change": "➖ {label}: {base} → {new}, no real change (inside normal run-to-run noise)",
    "inconclusive": "❔ {label}: {base} → {new} looks like {ratio}×, but the timings were too noisy to prove it",
    "changed_output": "❌ {label}: {base} → {new}, but the output CHANGED, so this is not accepted",
    "failed": "❌ {label}: the command failed, so there is no valid timing",
    "unstable_output": "⚠️ {label}: the output differs between runs; set ignore_patterns before comparing",
    "baseline": "📏 {label}: baseline {new} ({n} runs)",
    "no_comparison": "⚠️ {label}: measured {new}, but with different output filters than the baseline, so not compared",
}


def describe(run: dict, tgt: dict) -> str:
    tmpl = VERDICT_TEXT.get(run.get("verdict"), "{label}: measured {new}")
    return tmpl.format(label=tgt.get("label", "script"), base=fmt_s(run.get("before_s") or run.get("base_median")),
                       new=fmt_s(run.get("after_s") or run.get("median")), ratio=run.get("ratio"), n=run.get("runs"))


def footer_for(response_text: str, sess: dict) -> tuple[str | None, str | None]:
    """Text to append to the reply, and the newest run id it covers (to avoid repeats)."""
    runs = []
    for tgt in session_targets(sess):
        run = latest_run(tgt)
        if run:
            runs.append((run, tgt))
    claims = speed_claims(response_text)
    claimed = claims_speed(response_text)
    newest = max((r["at"] for r, _ in runs), default=0)
    newest_id = next((r["id"] for r, _ in runs if r["at"] == newest), None)
    fresh = newest_id and newest_id != sess.get("last_footer_run")
    lines = []
    if not runs:
        if sess.get("perf_mode") and (claims or claimed):
            lines.append("⚠️ This reply describes a speed-up, but nothing was measured in this chat.")
    else:
        comparisons = [(r, t) for r, t in runs if r.get("verdict") not in {"baseline"}]
        if fresh or claims or claimed:
            for r, t in runs:
                lines.append(describe(r, t))
            for st in stale_targets(sess):
                lines.append(f"⚠️ {st['target']['label']}: code changed after the last measurement; "
                             "the numbers above are out of date.")
        measured = [r["ratio"] for r, _ in comparisons if r.get("verdict") == "faster" and r.get("ratio")]
        for c in claims:
            if not measured:
                lines.append(f"⚠️ The reply says {c:g}× faster, but no measured speed-up backs it.")
            elif all(abs(c - m) / m > CLAIM_TOLERANCE for m in measured):
                best = max(measured, key=lambda m: -abs(c - m))
                lines.append(f"⚠️ The reply says {c:g}× faster; Perf Lab measured {best:g}×.")
        if claimed and not claims and comparisons and not measured:
            lines.append("⚠️ The reply describes a speed-up, but the measurement does not show one.")
    if not lines:
        return None, None
    body = "\n".join(f"- {line}" for line in lines)
    return f"\n\n---\n**Perf Lab** (measured by the plugin, not the model)\n{body}", newest_id


# --------------------------------------------------------------------------- read API for the pane


def summaries(limit: int = 30) -> list[dict]:
    out = []
    for tgt in sorted(load_targets().values(), key=lambda t: -t.get("updated", 0))[:limit]:
        run = latest_run(tgt)
        history = []
        for rid in tgt.get("runs", [])[-20:]:
            r = load_run(rid)
            if r and r.get("median") is not None:
                history.append({"id": r["id"], "at": r["at"], "median": r["median"], "verdict": r.get("verdict")})
        out.append({
            "id": tgt["id"], "label": tgt.get("label"), "command": tgt.get("command"), "cwd": tgt.get("cwd"),
            "updated": tgt.get("updated"), "baseline": tgt.get("baseline"), "latest": _public_run(run),
            "history": history, "profile": tgt.get("profiles", [None])[-1] if tgt.get("profiles") else None,
            "stale": bool(run and [p for p, h in file_hashes(run.get("code", {}).keys()).items() if run["code"].get(p) != h]),
        })
    return out


def _public_run(run: dict | None) -> dict | None:
    if not run:
        return None
    keep = ("id", "at", "times", "runs", "median", "min", "max", "stdev", "verdict", "ratio", "reason",
            "base_median", "diff", "failed", "unstable", "stderr", "note", "fingerprint", "baseline", "win_share",
            "before_s", "after_s")
    return {k: run.get(k) for k in keep}


def target_detail(tid: str) -> dict | None:
    if not ID_RE.match(tid or ""):
        return None
    tgt = load_targets().get(tid)
    if not tgt:
        return None
    runs = [_public_run(load_run(r)) for r in tgt.get("runs", [])]
    prof = load_profile(tgt["profiles"][-1]) if tgt.get("profiles") else None
    return {"target": {k: tgt.get(k) for k in ("id", "label", "command", "cwd", "baseline", "watch", "updated")},
            "runs": [r for r in runs if r], "profile": prof}


def forget(tid: str) -> bool:
    if not ID_RE.match(tid or ""):
        return False
    with locked():
        targets = load_targets()
        tgt = targets.pop(tid, None)
        if not tgt:
            return False
        for rid in tgt.get("runs", []):
            with contextlib.suppress(OSError):
                _run_path(rid).unlink()
        for pid in tgt.get("profiles", []):
            with contextlib.suppress(OSError):
                _profile_path(pid).unlink()
        _write(_targets_path(), targets)
        return True
