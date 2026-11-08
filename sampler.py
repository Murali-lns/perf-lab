"""Perf Lab built-in sampling profiler (stdlib only).

Run as: <python> sampler.py --out FILE [--interval SECONDS] -- script.py args...
     or: <python> sampler.py --out FILE -- -m package.module args...

It runs the target with the project's own interpreter (so its imports resolve),
samples the main thread's call stack every few milliseconds from a helper
thread, and writes collapsed stacks ("outer;inner;leaf COUNT" per line), the same
format py-spy and flame-graph tools use. Used only when py-spy is not installed.
"""

from __future__ import annotations

import os
import runpy
import sys
import threading
import time


def _frame_name(code) -> str:
    name = getattr(code, "co_qualname", code.co_name)
    return f"{name} ({code.co_filename}:{code.co_firstlineno})"


def main(argv: list[str]) -> int:
    out, interval = None, 0.005
    i = 0
    while i < len(argv) and argv[i] != "--":
        if argv[i] == "--out":
            out = argv[i + 1]
            i += 2
        elif argv[i] == "--interval":
            interval = max(0.001, float(argv[i + 1]))
            i += 2
        else:
            i += 1
    target = argv[i + 1:]
    if not out or not target:
        print("usage: sampler.py --out FILE -- script.py [args]", file=sys.stderr)
        return 2
    while target and target[0].startswith("-") and target[0] != "-m":
        target = target[1:]  # drop interpreter flags such as -u or -O
    if not target:
        return 2

    counts: dict[str, int] = {}
    main_id = threading.main_thread().ident
    stop = threading.Event()
    active = threading.Event()  # set once the target starts, so thread start-up is not sampled
    me = os.path.abspath(__file__)

    def sample():
        active.wait()
        while not stop.is_set():
            frame = sys._current_frames().get(main_id)
            stack = []
            while frame is not None:
                if frame.f_code.co_filename != me:
                    stack.append(_frame_name(frame.f_code))
                frame = frame.f_back
            if stack:
                key = ";".join(reversed(stack))
                counts[key] = counts.get(key, 0) + 1
            time.sleep(interval)

    def flush():
        with open(out, "w", encoding="utf-8") as fh:
            for key, n in counts.items():
                fh.write(f"{key} {n}\n")

    thread = threading.Thread(target=sample, name="perf-lab-sampler", daemon=True)
    code = 0
    if hasattr(sys, "setswitchinterval"):
        sys.setswitchinterval(min(interval, 0.005))
    thread.start()
    active.set()
    try:
        if target[0] == "-m":
            sys.argv = [target[1], *target[2:]]
            runpy.run_module(target[1], run_name="__main__", alter_sys=True)
        else:
            script = os.path.abspath(target[0])
            sys.argv = [script, *target[1:]]
            sys.path.insert(0, os.path.dirname(script))
            runpy.run_path(script, run_name="__main__")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    finally:
        stop.set()
        active.set()
        thread.join(timeout=1)
        flush()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
