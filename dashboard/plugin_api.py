"""Perf Lab backend for the Desktop pane. Mounted at /api/plugins/perf-lab/.

Read-mostly: lists measured scripts, returns one script's runs and latest flame graph,
forgets a script, and toggles the gate. It never runs user commands; measuring is
done by the agent's perf_bench / perf_profile tools.
"""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query

router = APIRouter()


def _load_core():
    # The router is mounted by the host, not imported as part of the package, so load the
    # shared core by path. Not registered in sys.modules (no core override).
    path = Path(__file__).resolve().parents[1] / "perf_core.py"
    spec = importlib.util.spec_from_file_location("perf_lab_core_dashboard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


core = _load_core()


@router.get("/summary")
def summary():
    return {"targets": core.summaries(), "settings": core.settings(), "pyspy": bool(shutil.which("py-spy"))}


@router.get("/target")
def target(id: str = Query(..., max_length=12)):
    detail = core.target_detail(id)
    if detail is None:
        raise HTTPException(status_code=404, detail="unknown script")
    return detail


@router.post("/forget")
def forget(payload: dict = Body(...)):
    tid = str(payload.get("id") or "")
    if not core.ID_RE.match(tid):
        raise HTTPException(status_code=400, detail="bad id")
    return {"ok": core.forget(tid)}


@router.post("/settings")
def set_settings(payload: dict = Body(...)):
    gate = payload.get("gate")
    if not isinstance(gate, bool):
        raise HTTPException(status_code=400, detail="gate must be true or false")
    return core.save_settings(gate=gate)
