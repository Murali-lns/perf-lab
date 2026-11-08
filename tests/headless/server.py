"""Harness server: REAL plugin_api router, data made by REAL perf_bench/perf_profile runs on the demo backtest."""
import importlib.util, os, sys, tempfile, shutil
from pathlib import Path
ROOT = Path(os.environ["PL_ROOT"])
os.environ["PERF_LAB_DATA_DIR"] = tempfile.mkdtemp(prefix="pl-harness-")
spec = importlib.util.spec_from_file_location("pl_api", ROOT / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec); spec.loader.exec_module(api)
core = api.core
proj = Path(tempfile.mkdtemp(prefix="pl-proj-"))
src = (ROOT / "examples" / "backtest" / "backtest.py").read_text()
PY = sys.executable

# Scenario A: a "faster" running-sum rewrite with an off-by-window bug that changes results.
wrong = proj / "wrong"; wrong.mkdir()
(wrong / "signals.py").write_text(src)
core.bench(f"{PY} signals.py", str(wrong), runs=3, warmup=0, session_id="demo", label="signals.py")
(wrong / "signals.py").write_text(src.replace("""    signals = []
    for i in range(len(prices)):
        f = moving_average(prices, i, fast)
        s = moving_average(prices, i, slow)
        signals.append(1 if f > s else 0)
    return signals""", """    signals, fs, ss = [], 0.0, 0.0
    for i, p in enumerate(prices):
        fs += p - (prices[i - fast] if i >= fast else 0)
        ss += p - (prices[i - slow] if i >= slow else 0)
        signals.append(1 if fs / fast > ss / slow else 0)  # bug: early days divided by the full window
    return signals"""))
core.bench(f"{PY} signals.py", str(wrong), runs=3, warmup=0, session_id="demo", label="signals.py")

# Scenario B: correct optimisation (sum() adds in the same order, so output is identical).
good = proj / "good"; good.mkdir()
(good / "backtest.py").write_text(src)
core.profile(f"{PY} backtest.py", str(good), session_id="demo")
core.bench(f"{PY} backtest.py", str(good), runs=5, warmup=1, session_id="demo")
(good / "backtest.py").write_text(src.replace("""    total = 0.0
    for p in window:
        total += p
    return total / len(window)""", """    return sum(window) / len(window)"""))
core.bench(f"{PY} backtest.py", str(good), runs=5, warmup=1, session_id="demo")
print("harness data ready", flush=True)

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn
app = FastAPI()
app.include_router(api.router, prefix="/api")
app.mount("/", StaticFiles(directory=str(Path(__file__).parent if not os.environ.get('PL_WEB') else os.environ['PL_WEB']), html=True))
uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8792")), log_level="warning")
