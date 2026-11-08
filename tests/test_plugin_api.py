"""Pane backend over HTTP (needs fastapi; run with the Hermes venv python)."""

import importlib.util
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    FastAPI = None


@unittest.skipIf(FastAPI is None, "fastapi not installed")
class TestPluginApi(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="perflab-api-")
        os.environ["PERF_LAB_DATA_DIR"] = self.data
        spec = importlib.util.spec_from_file_location("perf_lab_api_test", ROOT / "dashboard" / "plugin_api.py")
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        app = FastAPI()
        app.include_router(self.api.router, prefix="/api/plugins/perf-lab")
        self.client = TestClient(app)
        self.proj = Path(tempfile.mkdtemp(prefix="perflab-proj-"))
        (self.proj / "job.py").write_text(textwrap.dedent("""
            def work():
                return sum(i * i for i in range(400_000))
            print(work())
        """))

    def tearDown(self):
        shutil.rmtree(self.data, ignore_errors=True)
        shutil.rmtree(self.proj, ignore_errors=True)
        os.environ.pop("PERF_LAB_DATA_DIR", None)

    def test_summary_detail_forget_settings(self):
        core = self.api.core
        cmd = f"{sys.executable} job.py"
        run = core.bench(cmd, str(self.proj), runs=2, warmup=0, session_id="s")
        core.profile(cmd, str(self.proj), session_id="s")
        s = self.client.get("/api/plugins/perf-lab/summary").json()
        self.assertEqual(len(s["targets"]), 1)
        tid = s["targets"][0]["id"]
        self.assertEqual(s["targets"][0]["latest"]["verdict"], "baseline")
        self.assertNotIn("output", s["targets"][0]["latest"])  # raw program output is not sent to the pane
        d = self.client.get(f"/api/plugins/perf-lab/target?id={tid}").json()
        self.assertEqual(d["runs"][0]["id"], run["id"])
        self.assertTrue(d["profile"]["tree"]["children"])
        self.assertEqual(self.client.get("/api/plugins/perf-lab/target?id=nothex").status_code, 404)
        self.assertEqual(self.client.post("/api/plugins/perf-lab/settings", json={"gate": "yes"}).status_code, 400)
        self.assertFalse(self.client.post("/api/plugins/perf-lab/settings", json={"gate": False}).json()["gate"])
        self.assertEqual(self.client.post("/api/plugins/perf-lab/forget", json={"id": "../x"}).status_code, 400)
        self.assertTrue(self.client.post("/api/plugins/perf-lab/forget", json={"id": tid}).json()["ok"])
        self.assertEqual(self.client.get("/api/plugins/perf-lab/summary").json()["targets"], [])


if __name__ == "__main__":
    unittest.main()
