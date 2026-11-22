import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_temp = tempfile.TemporaryDirectory()
os.environ["ORBENCH_DB"] = str(Path(_temp.name) / "test.db")
import app as server
from audit import trace_scope
from decision import decide_hhcrsp, HHCRSP_DEFAULT, DEFAULT_PROGRAM
from model_usage import combine
from pipeline import _safe, _call_json, _stage_code
from ptc import run_code


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        server.RUNS_DIR = Path(_temp.name) / "runs"
        with server.db() as conn:
            for table in ("decisions", "evaluations", "events", "tasks"):
                conn.execute(f"DELETE FROM {table}")
        self.client = server.app.test_client()

    @staticmethod
    def reply(message, cache=0):
        usage = {"prompt_tokens": 100, "completion_tokens": 20}
        if cache is not None:
            usage["prompt_cache_hit_tokens"] = cache
        return io.BytesIO(json.dumps({"choices": [{"message": message}], "usage": usage}).encode())

    @staticmethod
    def decision_fixture(*args, **kwargs):
        return {"steps": [{"mode": "judgment", "title": "判断", "text": "fixture"}],
                "program": DEFAULT_PROGRAM, "fallback": False, "model": "test",
                "effort": "low", "usage": {}, "error": None, "rounds": 1}

    def test_ptc_async_tools_are_executed_and_traced(self):
        seen = []
        path = Path(_temp.name) / "ptc.jsonl"
        with trace_scope(path, "test"):
            result = run_code(
                "const [a,b] = await Promise.all([tools.inspect_instance({}),"
                "tools.estimate_pressure({destroy_fraction:0.2})]);"
                "console.log(a); return {total:a.n+b.n};",
                lambda name, args: seen.append(name) or {"n": 2})
        self.assertEqual(result["value"], {"total": 4})
        self.assertEqual(len(seen), 2)
        self.assertEqual(json.loads(path.read_text().splitlines()[0])["kind"], "ptc.code")

    def test_ptc_cannot_access_host_and_has_time_limit(self):
        for code in ("return process.env;", "return require('fs');", "while(true){}"):
            with self.assertRaises((ValueError, RuntimeError)):
                run_code(code, lambda *args: {})
        with self.assertRaises(ValueError):
            run_code("for(let i=0;i<17;i++) await tools.inspect_instance({});", lambda *args: {})

    def test_real_chat_transport_records_usage_and_raw_response(self):
        path = Path(_temp.name) / "calls.jsonl"
        with patch("decision._credentials", return_value=("test", "http://localhost")), \
             patch("decision.urllib.request.urlopen", side_effect=[
                 self.reply({"content": ""}, 0), self.reply({"content": "{}"}, 50)]), \
             trace_scope(path, "build"):
            value, info = _safe(_call_json, "system", "prompt", model="test", effort="low",
                                timeout=1, _trace_path=path)
        self.assertEqual(value, {})
        self.assertEqual(info["usage"]["requests"], 2)
        self.assertEqual(info["usage"]["prompt_tokens"], 200)
        self.assertEqual(info["usage"]["prompt_cache_hit_tokens"], 50)
        records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([r["kind"] for r in records], ["model.request", "model.response"] * 2)
        self.assertNotIn("Authorization", path.read_text())
        self.assertIsNone(combine([{"requests": 1, "prompt_cache_hit_tokens": 0}, {"requests": 1}])["prompt_cache_hit_tokens"])

    def test_model_ptc_loop_calls_registered_tools(self):
        code = "const p=await tools.inspect_instance({}); return await tools.validate_program(" + json.dumps(HHCRSP_DEFAULT) + ");"
        messages = [
            self.reply({"tool_calls": [{"id": "call1", "function": {"name": "run_code",
                       "arguments": json.dumps({"code": code, "description": "check"})}}]}),
            self.reply({"content": json.dumps(HHCRSP_DEFAULT)}),
        ]
        with patch("decision._credentials", return_value=("test", "http://localhost")), \
             patch("decision.urllib.request.urlopen", side_effect=messages):
            result = decide_hhcrsp({"instance_id": "test", "patients": 2, "days": 7}, [], mode="ptc")
        self.assertFalse(result["fallback"])
        self.assertEqual(result["usage"]["requests"], 2)
        self.assertEqual(result["program"], HHCRSP_DEFAULT)

    def test_prepare_never_spawns_solver_and_start_is_once_only(self):
        instance = next(iter(server._INSTANCES))
        with patch("app.threading.Thread"), patch("app.subprocess.Popen") as spawn, \
             patch("app.decide", side_effect=self.decision_fixture):
            response = self.client.post("/api/tasks", json={"instance_id": instance, "seed": 7, "seconds": 5,
                                                          "build_pipeline": False})
            self.assertEqual(response.status_code, 200)
            task_id = response.json["id"]
            server._prepare(task_id)
            task = self.client.get(f"/api/tasks/{task_id}").json
            self.assertEqual(task["status"], "awaiting_start")
            self.assertEqual(task["tool_mode"], "ptc")
            proposal = self.client.get(f"/api/tasks/{task_id}/proposal").json["proposal"]
            self.assertEqual(proposal["program"], DEFAULT_PROGRAM)
            self.assertEqual(proposal["reasoning"], "fixture")
            spawn.assert_not_called()
            self.assertEqual(self.client.post(f"/api/tasks/{task_id}/start").status_code, 200)
            self.assertEqual(self.client.post(f"/api/tasks/{task_id}/start").status_code, 409)
            spawn.assert_called_once()

    def test_modified_proposal_is_rejected(self):
        instance = next(iter(server._INSTANCES))
        with patch("app.threading.Thread"), patch("app.subprocess.Popen") as spawn, \
             patch("app.decide", side_effect=self.decision_fixture):
            task = self.client.post("/api/tasks", json={"instance_id": instance, "seed": 7, "seconds": 5,
                                                      "build_pipeline": False}).json
            server._prepare(task["id"])
            path = server.RUNS_DIR / task["run_id"] / "decision.json"
            path.write_text("{}")
            self.assertEqual(self.client.post(f"/api/tasks/{task['id']}/start").status_code, 409)
            spawn.assert_not_called()

    def test_artifact_whitelist_includes_code_but_blocks_paths(self):
        instance = next(iter(server._INSTANCES))
        with patch("app.threading.Thread"):
            task = self.client.post("/api/tasks", json={"instance_id": instance, "seed": 7, "seconds": 5,
                                                      "build_pipeline": False}).json
        path = server.RUNS_DIR / task["run_id"] / "pipeline" / "solver.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# generated")
        with self.client.get(f"/api/tasks/{task['id']}/artifacts/pipeline/solver.py") as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f"/api/tasks/{task['id']}/artifacts/pipeline/config.json").status_code, 403)

    def test_only_ptc_experiments_are_accepted(self):
        instance = next(iter(server._INSTANCES))
        for options in ({"tool_mode": "native"}, {"tool_mode": "local"}, {"use_deepseek": False}):
            response = self.client.post("/api/tasks", json={
                "instance_id": instance, "seed": 7, "seconds": 5, **options})
            self.assertEqual(response.status_code, 400)
        with server.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)

    def test_relay_models_keep_own_efforts(self):
        request = {"instance_id": next(iter(server._INSTANCES)), "seed": 1, "seconds": 5}
        for model in ("gpt-6.2-astra", "gpt-6.1-sol"):
            for effort in ("low", "medium", "high", "xhigh", None):
                with patch("app.threading.Thread"):
                    response = self.client.post("/api/tasks", json={**request, "effort": effort, "model": model})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["model"], model)
                self.assertEqual(response.json["effort"], effort or "medium")
                with server.db() as conn:
                    conn.execute("UPDATE tasks SET status='awaiting_start' WHERE id=?", (response.json["id"],))
            self.assertEqual(self.client.post("/api/tasks", json={**request, "model": model, "effort": "max"}).status_code, 400)

    def test_truncated_solver_code_is_retried_without_fabrication(self):
        complete = "import gurobipy as gp\nCONSTRAINTS=[]\ndef build_model(instance):\n return gp.Model()\ndef solve(instance):\n return build_model(instance)\n"
        with patch("pipeline._chat", side_effect=[
                ({"content": "```python\ndef build_model("}, {"finish_reason": "length"}),
                ({"content": f"```python\n{complete}```"}, {"finish_reason": "stop"}),
        ]) as chat:
            code, info = _stage_code("surgical", {}, model="test", effort="low", timeout=1)
        self.assertEqual(code, complete.strip())
        self.assertEqual(info["finish_reason"], "stop")
        self.assertGreater(chat.call_args_list[1].kwargs["max_tokens"],
                           chat.call_args_list[0].kwargs["max_tokens"])


if __name__ == "__main__":
    unittest.main()
