"""LLM4OR 医疗平台：DeepSeek 决策、命令行求解、闭环评价。

流程：提交 →（可选）DeepSeek 判断 → 新开 CMD 窗口跑 ALNS → 页面轮询真实进度
→ 结束后用独立 checker 重算并评价。

科学边界：checker 是 alns_lab 的 reconstructed-independent 实现，不是 FrontierOR
官方基准；"通过"只表示按当前模型与 K1–K11 无违规。
"""

from __future__ import annotations

import hashlib
import json
import os
import math
import importlib.util
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file

from decision import (
    DEFAULT_EFFORT, DEFAULT_MODEL, MODELS, decide, decide_hhcrsp, model_catalog,
    resolve_effort, resolve_model, review,
)
from evaluation import evaluate, evaluate_failure, evaluate_hhcrsp
import pipeline as pipeline_mod
from audit import trace_scope, emit
from model_usage import combine
from scenarios import SCENARIO_BY_ID, SCENARIOS, scan_all

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parents[1]
RUNS_DIR = ROOT / "results" / "orbench" / "runs"
ITERATION_CAP = 500000
CONSTRAINT_LABELS = {
    "K1": "唯一指派与资格", "K2": "强制患者必须服务", "K3": "手术室日容量与加班上限",
    "K4": "同一手术室同日不重叠", "K5": "医生日可用", "K6": "同一医生同日不重叠",
    "K7": "手术室归属与规划期", "K8": "截止日", "K9": "准备与清洁占用",
    "K10": "开机成本联动", "K11": "零收益可选患者可跳过",
}

ARTIFACTS = (
    "manifest.json", "run_config.json", "metrics.json", "checker_result.json",
    "trace.json", "solution.json", "instance_snapshot.json", "request.json", "evaluation.json",
    "decision.json", "model_trace.jsonl", "timeline.json",
)
BUILD_ARTIFACTS = ("spec.json", "model.json", "solver.py", "validation.json", "knowledge.json", "manifest.json")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.json.ensure_ascii = False

START_LOCK = threading.Lock()
FINALIZE_LOCK = threading.Lock()
_SOLVER = {"checked": False, "ok": False, "detail": ""}
_INSTANCES: dict[str, dict] = {}


def load_config() -> dict:
    config = {"host": "127.0.0.1", "port": 8037, "db": str(BASE_DIR / "orbench.db")}
    file_config = BASE_DIR / "config.json"
    if file_config.exists():
        try:
            config.update(json.loads(file_config.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    config["host"] = os.environ.get("HOST", config["host"])
    config["port"] = int(os.environ.get("PORT", config["port"]))
    config["db"] = os.environ.get("ORBENCH_DB", config["db"])
    return config


CONFIG = load_config()


@contextmanager
def db():
    conn = sqlite3.connect(CONFIG["db"])
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                instance_id TEXT NOT NULL,
                instance_path TEXT NOT NULL,
                instance_sha256 TEXT NOT NULL,
                iterations INTEGER NOT NULL,
                seed INTEGER NOT NULL,
                wall_clock_s REAL NOT NULL,
                run_id TEXT,
                parent_task_id INTEGER,
                status TEXT NOT NULL,
                verdict TEXT,
                summary TEXT,
                objective REAL,
                feasible INTEGER,
                violations INTEGER,
                runtime_s REAL,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                dimension TEXT NOT NULL,
                status TEXT NOT NULL,
                note TEXT NOT NULL,
                evidence TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                detail TEXT
            );
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                mode TEXT NOT NULL,
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                meta TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_instance ON tasks(instance_id, iterations);
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        for name, ddl in {
            "use_deepseek": "ALTER TABLE tasks ADD COLUMN use_deepseek INTEGER DEFAULT 1",
            "decision_status": "ALTER TABLE tasks ADD COLUMN decision_status TEXT",
            "console": "ALTER TABLE tasks ADD COLUMN console INTEGER DEFAULT 1",
            "executed_iterations": "ALTER TABLE tasks ADD COLUMN executed_iterations INTEGER",
            "stopped_by": "ALTER TABLE tasks ADD COLUMN stopped_by TEXT",
            "decision_started_at": "ALTER TABLE tasks ADD COLUMN decision_started_at TEXT",
            "scenario": "ALTER TABLE tasks ADD COLUMN scenario TEXT DEFAULT 'surgical'",
            "model": "ALTER TABLE tasks ADD COLUMN model TEXT",
            "effort": "ALTER TABLE tasks ADD COLUMN effort TEXT",
            "build_pipeline": "ALTER TABLE tasks ADD COLUMN build_pipeline INTEGER DEFAULT 1",
            "tool_mode": "ALTER TABLE tasks ADD COLUMN tool_mode TEXT DEFAULT 'native'",
            "decision_sha256": "ALTER TABLE tasks ADD COLUMN decision_sha256 TEXT",
            "solver_started_at": "ALTER TABLE tasks ADD COLUMN solver_started_at TEXT",
        }.items():
            if name not in columns:
                conn.execute(ddl)
        # 历史任务没有模型列：当时默认走的就是 flash / high，按此回填，避免列表空白。
        conn.execute("UPDATE tasks SET model='deepseek-flash', effort='high' "
                     "WHERE use_deepseek=1 AND (model IS NULL OR model='')")


def reconcile_stale() -> int:
    """启动或提交前清理：worker 已退出但没落 status.json 的任务不该一直占着队列。"""
    marked = 0
    now = time.time()
    with db() as conn:
        rows = conn.execute(
            "SELECT id, run_id, created_at FROM tasks WHERE status IN ('preparing','running')").fetchall()
        for row in rows:
            run_dir = RUNS_DIR / row["run_id"] if row["run_id"] else None
            if run_dir and (run_dir / "status.json").is_file():
                continue
            progress = run_dir / "progress.json" if run_dir else None
            last = progress.stat().st_mtime if progress and progress.is_file() else None
            if last is not None:
                if now - last < 150:
                    continue
            else:
                # 决策阶段还没有 progress.json：给 5 分钟，别把正在思考的任务判死。
                try:
                    age = now - datetime.fromisoformat(row["created_at"]).timestamp()
                except ValueError:
                    age = 0
                if age < 300:
                    continue
            conn.execute(
                "UPDATE tasks SET status='failed', verdict='blocked', summary=?, error=? WHERE id=?",
                ("命令行窗口没有回报结果，任务已停止。", "worker exited without status.json", row["id"]))
            log_event(conn, row["id"], "task.reconciled", {"reason": "stale worker"})
            marked += 1
    return marked


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log_event(conn, task_id: int | None, kind: str, detail: dict) -> None:
    conn.execute("INSERT INTO events(task_id, ts, kind, detail) VALUES (?,?,?,?)",
                 (task_id, now_iso(), kind, json.dumps(detail, ensure_ascii=False)))


def scan_instances() -> dict[str, dict]:
    return scan_all(ROOT)


def instance_list() -> list[dict]:
    order = {item["id"]: index for index, item in enumerate(SCENARIOS)}
    return sorted(_INSTANCES.values(),
                  key=lambda x: (order.get(x["scenario"], 9),
                                 0 if x["dataset"] == "FrontierOR" else 1,
                                 0 if x["instance_id"].startswith("tiny") else 1,
                                 x["instance_id"]))


def solver_status() -> dict:
    if not _SOLVER["checked"]:
        try:
            proc = subprocess.run([sys.executable, "-m", "alns_lab", "version"], cwd=str(ROOT),
                                  capture_output=True, text=True, encoding="utf-8", timeout=20)
            _SOLVER["ok"] = proc.returncode == 0
            _SOLVER["detail"] = (proc.stdout or proc.stderr).strip().splitlines()[0:1]
        except (OSError, subprocess.SubprocessError) as exc:
            _SOLVER["ok"], _SOLVER["detail"] = False, str(exc)
        _SOLVER["checked"] = True
    return dict(_SOLVER)


# ---------------------------------------------------------------------------
# 启动：决策线程 → 新开 CMD 窗口跑 worker
# ---------------------------------------------------------------------------
def task_row(task_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def history_for(conn, instance_id: str, cap: int) -> list[dict]:
    rows = conn.execute(
        """SELECT id, seed, objective, feasible FROM tasks
           WHERE instance_id=? AND iterations=? AND status='succeeded' AND objective IS NOT NULL
           ORDER BY id""", (instance_id, cap)).fetchall()
    return [dict(row) for row in rows]


def _meta(instance: dict) -> dict:
    return {
        "instance_id": instance["instance_id"],
        "patients": instance["patients"], "days": instance["days"],
        "hospitals": instance["hospitals"], "ors_per_hospital": instance["ors_per_hospital"],
        "mandatory": instance["mandatory"],
    }


def _meta_hhcrsp(instance: dict) -> dict:
    return {
        "instance_id": instance["instance_id"],
        "patients": instance["patients"], "days": instance["days"],
        "clinicians": instance.get("clinicians"),
        "visits": instance.get("required_visits") or instance.get("visits"),
        "window_width": instance.get("window_width"),
        "skills": instance.get("skills"),
        "mandatory": 0,
    }


def _pipeline_task(task_id: int) -> None:
    """后台生成建模产物：三段调用 + 一致性校验，全部落到 run_dir/pipeline/。"""
    row = task_row(task_id)
    if row is None or not row["run_id"]:
        return
    run_dir = RUNS_DIR / row["run_id"]
    out_dir = run_dir / "pipeline"
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "status.json", {"state": "running", "task_id": task_id, "started_at": now_iso()})
    # 重新生成时先撤掉旧产物，避免界面把上一轮的结果当成本轮
    for stale in ("manifest.json", "validation.json"):
        (out_dir / stale).unlink(missing_ok=True)
    instance = _INSTANCES.get(row["instance_id"], {})
    scenario = row["scenario"] or instance.get("scenario") or "surgical"
    meta = _meta(instance) if scenario == "surgical" else _meta_hhcrsp(instance)
    meta["path"] = str(ROOT / instance["path"]) if instance.get("path") else None
    try:
        pipeline_mod.build(
            scenario=scenario, meta=meta, out_dir=out_dir,
            model=row["model"] or DEFAULT_MODEL,
            effort=row["effort"] or DEFAULT_EFFORT,
        )
        _write_json(out_dir / "status.json", {"state": "done", "task_id": task_id, "finished_at": now_iso()})
        log_event_db = {"scenario": scenario, "model": row["model"]}
        with db() as conn:
            log_event(conn, task_id, "pipeline.done", log_event_db)
    except Exception as exc:  # noqa: BLE001 - 任何失败都要在界面上说清楚
        _write_json(out_dir / "status.json", {
            "state": "failed", "task_id": task_id, "finished_at": now_iso(),
            "error": f"{type(exc).__name__}: {exc}",
        })
        with db() as conn:
            log_event(conn, task_id, "pipeline.failed", {"error": str(exc)})


def _prepare(task_id: int) -> None:
    try:
        _prepare_decision(task_id)
    except Exception as exc:
        with db() as conn:
            conn.execute("UPDATE tasks SET status='failed', error=? WHERE id=?", (str(exc), task_id))
            log_event(conn, task_id, "decision.failed", {"error": str(exc)})


def _prepare_decision(task_id: int) -> None:
    with db() as conn:
        conn.execute("UPDATE tasks SET decision_started_at=? WHERE id=?", (now_iso(), task_id))
    row = task_row(task_id)
    instance = _INSTANCES[row["instance_id"]]
    scenario = row["scenario"] or instance["scenario"]
    if row["use_deepseek"] and (row["build_pipeline"] if row["build_pipeline"] is not None else 1):
        with _PIPELINE_LOCK:
            _PIPELINE_RUNNING.add(task_id)
        threading.Thread(target=_pipeline_worker, args=(task_id,), daemon=True).start()
    program = {key: (list(value) if isinstance(value, list) else value)
               for key, value in SCENARIO_BY_ID[scenario]["default_program"].items()}
    if row["use_deepseek"]:
        with db() as conn:
            history = history_for(conn, row["instance_id"], row["iterations"])
        decider = decide if scenario == "surgical" else decide_hhcrsp
        meta = _meta(instance) if scenario == "surgical" else _meta_hhcrsp(instance)
        meta["path"] = str(ROOT / instance["path"])
        started = time.perf_counter()
        with trace_scope(RUNS_DIR / row["run_id"] / "model_trace.jsonl", "decision"):
            result = decider(meta, history, model=row["model"] or DEFAULT_MODEL,
                             effort=row["effort"] or DEFAULT_EFFORT, mode=row["tool_mode"] or "native")
        decision_s = round(time.perf_counter() - started, 2)
        program = result["program"]
        with db() as conn:
            conn.execute("DELETE FROM decisions WHERE task_id=?", (task_id,))
            shared = {"fallback": result["fallback"], "model": result["model"],
                      "effort": result.get("effort"), "latency_s": decision_s,
                      "usage": result.get("usage"), "rounds": result.get("rounds"),
                      "error": result["error"]}
            for seq, step in enumerate(result["steps"]):
                conn.execute(
                    "INSERT INTO decisions(task_id, seq, mode, title, text, meta) VALUES (?,?,?,?,?,?)",
                    (task_id, seq, step["mode"], step["title"], step["text"],
                     json.dumps(shared, ensure_ascii=False)))
            conn.execute("UPDATE tasks SET decision_status='ready', model=?, effort=? WHERE id=?",
                         (result["model"], result.get("effort"), task_id))
            log_event(conn, task_id, "decision.ready",
                      {"fallback": result["fallback"], "model": result["model"],
                       "rounds": result.get("rounds")})
    else:
        with db() as conn:
            conn.execute("UPDATE tasks SET decision_status='skipped' WHERE id=?", (task_id,))

    run_dir = RUNS_DIR / row["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    proposal = {"program": program, "scenario": scenario, "instance_id": row["instance_id"],
                "tool_mode": row["tool_mode"] or "native",
                "origin": "llm" if row["use_deepseek"] and not result["fallback"] else "system",
                "fallback": bool(result["fallback"]) if row["use_deepseek"] else False,
                "solver": SCENARIO_BY_ID[scenario]["kernel"],
                "reasoning": next((s["text"] for s in result["steps"] if s["title"] == "判断"), "")
                             if row["use_deepseek"] else "",
                "created_at": now_iso()}
    _write_json(run_dir / "decision.json", proposal)
    digest = hashlib.sha256((run_dir / "decision.json").read_bytes()).hexdigest()
    with db() as conn:
        conn.execute("UPDATE tasks SET status='awaiting_start', console=0, decision_sha256=? WHERE id=?", (digest, task_id))
        log_event(conn, task_id, "decision.awaiting_start", {"sha256": digest, "program": program})


def _launch(task_id: int, proposal: dict) -> None:
    row = task_row(task_id)
    instance = _INSTANCES[row["instance_id"]]
    scenario, program = proposal["scenario"], proposal["program"]
    run_dir = RUNS_DIR / row["run_id"]
    console_python = Path(sys.executable)
    if console_python.name.lower() == "pythonw.exe":
        console_python = console_python.with_name("python.exe")
    command = [
        str(console_python), "-X", "utf8", str(BASE_DIR / "worker.py"),
        "--task-id", str(task_id), "--run-dir", str(run_dir),
        "--instance", str(ROOT / instance["path"]),
        "--seconds", str(row["wall_clock_s"]), "--seed", str(row["seed"]),
        "--cap", str(row["iterations"]),
        "--destroy", ",".join(program["destroy_operators"]),
        "--repair", ",".join(program["repair_operators"]),
        "--fraction", str(program["destroy_fraction"]), "--acceptance", program["acceptance"],
        "--scenario", scenario,
    ]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        subprocess.Popen(
            ["cmd.exe", "/k", "chcp 65001>nul & " + subprocess.list2cmdline(command)],
            cwd=str(BASE_DIR), env=env, creationflags=flags, close_fds=True,
        )
    except OSError as exc:
        with db() as conn:
            conn.execute("UPDATE tasks SET status='failed', error=?, console=0 WHERE id=?",
                         (f"无法启动命令行窗口：{exc}", task_id))
            log_event(conn, task_id, "worker.spawn_failed", {"error": str(exc)})
        return
    with db() as conn:
        conn.execute("UPDATE tasks SET status='running', console=1, solver_started_at=? WHERE id=?", (now_iso(), task_id))
        log_event(conn, task_id, "worker.spawned",
                  {"run_dir": str(run_dir), "program": program})
    threading.Thread(target=_watch, args=(task_id,), daemon=True).start()


def _watch(task_id: int) -> None:
    """后台等待 worker 落 status.json，避免页面不开就没人收尾。"""
    for _ in range(3600):
        row = task_row(task_id)
        if row is None or row["status"] in {"succeeded", "failed"}:
            return
        if row["run_id"] and (RUNS_DIR / row["run_id"] / "status.json").is_file():
            _finalize(task_id)
            return
        time.sleep(1)


# ---------------------------------------------------------------------------
# 进度与收尾
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None


def _review_task(task_id: int) -> None:
    """运行结束后请模型做一次复盘；失败就保持原状态，不编内容。"""
    row = task_row(task_id)
    if row is None or not row["use_deepseek"]:
        return
    run_dir = RUNS_DIR / row["run_id"] if row["run_id"] else None
    metrics = (_read_json(run_dir / "metrics.json") if run_dir else None) or {}
    checker = (_read_json(run_dir / "checker_result.json") if run_dir else None) or {}
    with db() as conn:
        evaluations = conn.execute(
            "SELECT dimension, status, note FROM evaluations WHERE task_id=? ORDER BY id",
            (task_id,)).fetchall()
        conn.execute("UPDATE tasks SET decision_status='reviewing' WHERE id=?", (task_id,))
    outcome = {
        "verdict": row["verdict"],
        "summary": row["summary"],
        "objective": metrics.get("primary_objective"),
        "feasible": checker.get("feasible"),
        "violations": checker.get("violation_count"),
        "violations_by_code": checker.get("violations_by_code"),
        "executed_iterations": row["executed_iterations"],
        "runtime_s": metrics.get("runtime_s"),
        "budget_s": row["wall_clock_s"],
        "coverage_rate": metrics.get("coverage_rate"),
        "served_visits": metrics.get("served_visits"),
        "required_visits": metrics.get("required_visits"),
        "dimensions": [{"name": item["dimension"], "status": item["status"], "note": item["note"]}
                       for item in evaluations],
    }
    instance = _INSTANCES.get(row["instance_id"], {})
    meta = (_meta(instance) if (row["scenario"] or "surgical") == "surgical"
            else _meta_hhcrsp(instance))
    meta["path"] = str(ROOT / instance["path"]) if instance.get("path") else None
    with trace_scope(RUNS_DIR / row["run_id"] / "model_trace.jsonl", "review"):
        result = review(scenario=row["scenario"] or "surgical", meta=meta, outcome=outcome,
                        model=row["model"] or DEFAULT_MODEL, effort=row["effort"] or DEFAULT_EFFORT)
    with db() as conn:
        if result is None:
            conn.execute("UPDATE tasks SET decision_status='ready' WHERE id=?", (task_id,))
            log_event(conn, task_id, "review.failed", {})
            return
        conn.execute("DELETE FROM decisions WHERE task_id=? AND seq>=1000", (task_id,))
        conn.execute(
            "INSERT INTO decisions(task_id, seq, mode, title, text, meta) VALUES (?,?,?,?,?,?)",
            (task_id, 1000, "review", "复盘", result["text"],
             json.dumps({"model": (result.get("info") or {}).get("model"),
                         "effort": (result.get("info") or {}).get("effort"),
                         "latency_s": (result.get("info") or {}).get("latency_s"),
                         "usage": result.get("info"),
                         "fallback": False, "error": None}, ensure_ascii=False)))
        conn.execute("UPDATE tasks SET decision_status='reviewed' WHERE id=?", (task_id,))
        log_event(conn, task_id, "review.ready", {})


def _finalize(task_id: int) -> None:
    with FINALIZE_LOCK:
        fresh = task_row(task_id)
        if fresh is None or fresh["status"] in {"succeeded", "failed"}:
            return
        run_dir = RUNS_DIR / fresh["run_id"]
        status = _read_json(run_dir / "status.json") or {}
        if status.get("state") not in {"done", "failed"}:
            return
        params = {"iterations": fresh["iterations"], "seed": fresh["seed"],
                  "wall_clock_s": fresh["wall_clock_s"],
                  "executed_iterations": status.get("executed_iterations"),
                  "stopped_by": status.get("stopped_by")}
        eval_started = time.perf_counter()
        with db() as conn:
            if status.get("state") == "done":
                metrics = _read_json(run_dir / "metrics.json") or {}
                checker = _read_json(run_dir / "checker_result.json") or {}
                trace = (_read_json(run_dir / "trace.json") or {}).get("points") or []
                history = history_for(conn, fresh["instance_id"], fresh["iterations"])
                history.append({"id": task_id, "objective": metrics.get("primary_objective"),
                                "seed": fresh["seed"], "feasible": checker.get("feasible")})
                evaluator = evaluate_hhcrsp if (fresh["scenario"] or "surgical") == "hhcrsp" else evaluate
                report = evaluator(metrics=metrics, checker=checker, trace=trace,
                                   params=params, history=history)
                _write_json(run_dir / "request.json", {"task_id": task_id, "params": params})
                _write_json(run_dir / "evaluation.json", report)
                conn.execute(
                    """UPDATE tasks SET finished_at=?, status='succeeded', verdict=?, summary=?,
                           objective=?, feasible=?, violations=?, runtime_s=?,
                           executed_iterations=?, stopped_by=? WHERE id=?""",
                    (now_iso(), report["verdict"], report["summary"],
                     metrics.get("primary_objective"), int(bool(checker.get("feasible"))),
                     int(checker.get("violation_count") or 0), metrics.get("runtime_s"),
                     status.get("executed_iterations"), status.get("stopped_by"), task_id))
                _save_evaluations(conn, task_id, report)
                latency = 0.0
                rowd = conn.execute(
                    "SELECT meta FROM decisions WHERE task_id=? ORDER BY seq LIMIT 1",
                    (task_id,)).fetchone()
                if rowd:
                    try:
                        latency = float(json.loads(rowd["meta"]).get("latency_s") or 0)
                    except (ValueError, TypeError):
                        latency = 0.0
                _write_timeline(run_dir, fresh, metrics, time.perf_counter() - eval_started, latency)
            else:
                report = evaluate_failure(params=params, reason="命令行求解进程失败。",
                                          detail=str(status.get("error") or ""))
                _write_json(run_dir / "evaluation.json", report)
                conn.execute(
                    "UPDATE tasks SET finished_at=?, status='failed', verdict=?, summary=?, error=? WHERE id=?",
                    (now_iso(), report["verdict"], report["summary"], status.get("error"), task_id))
                _save_evaluations(conn, task_id, report)
            log_event(conn, task_id, "task.finished", {"status": status.get("state")})
    if fresh["use_deepseek"]:
        threading.Thread(target=_review_task, args=(task_id,), daemon=True).start()


def _write_timeline(run_dir: Path, row, metrics: dict, eval_s: float, latency: float) -> None:
    """把可测量的阶段时长写成时间轴；缺数据的阶段就不写，不估算。"""
    def ms(value):
        return max(0, int(round(float(value or 0) * 1000)))

    phases = []
    try:
        created = datetime.fromisoformat(row["created_at"]).timestamp()
        started = datetime.fromisoformat(row["decision_started_at"]).timestamp() if row["decision_started_at"] else created
        phases.append({"lane": "input", "label": "请求", "ms": ms(max(started - created, 0))})
    except ValueError:
        pass
    if latency:
        phases.append({"lane": "model", "label": row["model"] or "模型决策", "ms": ms(latency)})
    solve_ms = ms(metrics.get("runtime_s"))
    if solve_ms:
        label = "HHCRSP-ALNS 求解" if (row["scenario"] or "surgical") == "hhcrsp" else "ALNS 求解"
        phases.append({"lane": "tool", "label": label, "ms": solve_ms})
    phases.append({"lane": "check", "label": "校验与评价", "ms": ms(eval_s)})
    _write_json(run_dir / "timeline.json", {"phases": phases, "total_ms": sum(p["ms"] for p in phases)})


def _save_evaluations(conn, task_id: int, report: dict) -> None:
    conn.execute("DELETE FROM evaluations WHERE task_id=?", (task_id,))
    for item in report["dimensions"]:
        conn.execute("INSERT INTO evaluations(task_id, dimension, status, note, evidence) VALUES (?,?,?,?,?)",
                     (task_id, item["dimension"], item["status"], item["note"],
                      json.dumps(item["evidence"], ensure_ascii=False)))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return app.send_static_file("workbench.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "instances": len(_INSTANCES),
                    "scenarios": [dict(item, instances=sum(1 for x in _INSTANCES.values()
                                                           if x["scenario"] == item["id"]))
                                  for item in SCENARIOS],
                    "solver": solver_status()})


@app.get("/api/models")
def api_models():
    return jsonify({"models": model_catalog(), "default": DEFAULT_MODEL,
                    "default_effort": DEFAULT_EFFORT})


@app.get("/api/scenarios")
def api_scenarios():
    return jsonify([dict(item, instances=sum(1 for x in _INSTANCES.values()
                                             if x["scenario"] == item["id"]))
                    for item in SCENARIOS])


@app.get("/api/instances")
def api_instances():
    return jsonify(instance_list())


@app.get("/api/tasks")
def api_tasks():
    with db() as conn:
        rows = conn.execute(
            """SELECT id, created_at, instance_id, scenario, iterations, seed, wall_clock_s, status, verdict,
                      objective, feasible, violations, runtime_s, parent_task_id,
                      decision_status, use_deepseek, model, effort, tool_mode
               FROM tasks ORDER BY id DESC LIMIT 60""").fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/tasks")
def api_create_task():
    payload = request.get_json(force=True, silent=True) or {}
    instance_id = str(payload.get("instance_id") or "")
    if instance_id not in _INSTANCES:
        return jsonify({"error": "请选择一个有效实例。"}), 400
    try:
        seed = int(payload.get("seed"))
        seconds = float(payload.get("seconds"))
    except (TypeError, ValueError):
        return jsonify({"error": "时间预算与种子必须是数字。"}), 400
    if not math.isfinite(seconds) or not 5 <= seconds <= 300:
        return jsonify({"error": "时间预算需在 5 到 300 秒之间。"}), 400
    if not 0 <= seed <= 2147483647:
        return jsonify({"error": "种子需在 0 到 2147483647 之间。"}), 400
    if payload.get("use_deepseek", True) is not True:
        return jsonify({"error": "新实验统一使用 PTC。"}), 400
    use_deepseek = True
    model = resolve_model(str(payload.get("model") or DEFAULT_MODEL))
    if payload.get("model") and payload["model"] != model:
        return jsonify({"error": "模型不在实验目录中。"}), 400
    effort = resolve_effort(model, payload.get("effort"))
    if payload.get("effort") and payload["effort"] != effort:
        return jsonify({"error": "该模型不支持此推理强度。"}), 400
    build_pipeline = bool(payload.get("build_pipeline", True))
    tool_mode = payload.get("tool_mode", "ptc")
    if tool_mode != "ptc":
        return jsonify({"error": "仅支持 PTC / Code Mode。"}), 400
    parent = payload.get("parent_task_id")
    parent_task_id = int(parent) if str(parent or "").isdigit() else None

    reconcile_stale()
    with db() as conn:
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('preparing','running')").fetchone()["n"]
    if active:
        return jsonify({"error": "已有任务在运行，等它结束后再提交。"}), 409
    if not START_LOCK.acquire(blocking=False):
        return jsonify({"error": "上一个任务还在准备，请稍后再试。"}), 409
    try:
        instance = _INSTANCES[instance_id]
        with db() as conn:
            if conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('preparing','running')").fetchone()[0]:
                return jsonify({"error": "已有实验在准备或运行。"}), 409
            cursor = conn.execute(
                """INSERT INTO tasks(created_at, instance_id, instance_path, instance_sha256,
                                     iterations, seed, wall_clock_s, parent_task_id, status,
                                     use_deepseek, decision_status, scenario, model, effort,
                                     build_pipeline, tool_mode)
                   VALUES (?,?,?,?,?,?,?,?,'preparing',?,?,?,?,?,?,?)""",
                (now_iso(), instance_id, instance["path"], instance["sha256"],
                 ITERATION_CAP, seed, seconds, parent_task_id,
                 int(use_deepseek), "thinking" if use_deepseek else "skipped",
                 instance["scenario"], model, effort, int(build_pipeline), tool_mode))
            task_id = int(cursor.lastrowid)
            run_id = f"run_{datetime.now().strftime('%Y%m%dT%H%M%S')}_{task_id:04d}"
            conn.execute("UPDATE tasks SET run_id=?, console=0 WHERE id=?", (run_id, task_id))
            log_event(conn, task_id, "task.created",
                      {"seconds": seconds, "seed": seed, "use_deepseek": use_deepseek})
        threading.Thread(target=_prepare, args=(task_id,), daemon=True).start()
    finally:
        START_LOCK.release()
    return jsonify(task_detail(task_id))


@app.post("/api/tasks/<int:task_id>/start")
def api_start_task(task_id: int):
    if not START_LOCK.acquire(blocking=False):
        return jsonify({"error": "另一个任务正在启动。"}), 409
    try:
        row = task_row(task_id)
        if row is None:
            return jsonify({"error": "任务不存在。"}), 404
        if row["status"] != "awaiting_start":
            return jsonify({"error": "需先生成决策，且同一任务只能启动一次。"}), 409
        with db() as conn:
            active = conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('preparing','running')").fetchone()[0]
        if active:
            return jsonify({"error": "已有实验在准备或运行。"}), 409
        path = RUNS_DIR / row["run_id"] / "decision.json"
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row["decision_sha256"]:
            return jsonify({"error": "决策产物已变更，不能启动。请重新准备任务。"}), 409
        proposal = _read_json(path)
        _launch(task_id, proposal)
        return jsonify(task_detail(task_id))
    finally:
        START_LOCK.release()


@app.get("/api/tasks/<int:task_id>")
def api_task(task_id: int):
    payload = task_detail(task_id)
    return jsonify(payload) if payload else (jsonify({"error": "任务不存在。"}), 404)


@app.get("/api/tasks/<int:task_id>/progress")
def api_progress(task_id: int):
    row = task_row(task_id)
    if row is None:
        return jsonify({"error": "任务不存在。"}), 404
    run_dir = RUNS_DIR / row["run_id"] if row["run_id"] else None
    status = _read_json(run_dir / "status.json") if run_dir else None
    if status and row["status"] not in {"succeeded", "failed"}:
        _finalize(task_id)
        row = task_row(task_id)
    progress = _read_json(run_dir / "progress.json") if run_dir else None
    if row["status"] == "running" and progress is None:
        try:
            age = (datetime.now().astimezone()
                   - datetime.fromisoformat(row["solver_started_at"] or row["created_at"])).total_seconds()
        except ValueError:
            age = 0
        if age > 20:
            with db() as conn:
                conn.execute(
                    "UPDATE tasks SET status='failed', verdict='blocked', summary=?, error=? WHERE id=?",
                    ("命令行窗口没有回报进度，任务已停止。", "worker did not report progress", task_id))
                log_event(conn, task_id, "worker.no_progress", {"age_s": round(age, 1)})
            row = task_row(task_id)
    return jsonify({
        "task_id": task_id, "status": row["status"],
        "decision_status": row["decision_status"], "console": bool(row["console"]),
        "progress": progress or {}, "executed_iterations": row["executed_iterations"],
        "stopped_by": row["stopped_by"],
    })


@app.get("/api/tasks/<int:task_id>/decision")
def api_decision(task_id: int):
    with db() as conn:
        row = conn.execute("SELECT decision_status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return jsonify({"error": "任务不存在。"}), 404
        steps = conn.execute(
            "SELECT seq, mode, title, text, meta FROM decisions WHERE task_id=? ORDER BY seq",
            (task_id,)).fetchall()
    meta = json.loads(steps[0]["meta"]) if steps else {}
    return jsonify({
        "status": row["decision_status"] or "skipped",
        "fallback": bool(meta.get("fallback")), "model": meta.get("model"),
        "latency_s": meta.get("latency_s"), "error": meta.get("error"),
        "effort": meta.get("effort"), "usage": meta.get("usage"), "rounds": meta.get("rounds"),
        "steps": [{"seq": s["seq"], "mode": s["mode"], "title": s["title"], "text": s["text"],
                   "origin": "llm" if s["mode"] in {"thinking", "assistant", "review", "json"}
                   and s["title"] != "回退程序" else "tool" if s["mode"] == "tool" else "system"}
                  for s in steps],
    })


@app.get("/api/tasks/<int:task_id>/model-usage")
def api_model_usage(task_id: int):
    task = task_row(task_id)
    if task is None:
        return jsonify({"error": "任务不存在。"}), 404
    with db() as conn:
        rows = conn.execute("SELECT seq, meta FROM decisions WHERE task_id=? AND seq IN (0,1000)",
                            (task_id,)).fetchall()
    phases = {row["seq"]: json.loads(row["meta"]) for row in rows}
    directory = RUNS_DIR / task["run_id"] / "pipeline" if task["run_id"] else None
    build = (_read_json(directory / "manifest.json") or {}) if directory else {}
    payload = {"decision": phases.get(0), "review": phases.get(1000),
               "build": {k: build[k] for k in ("usage", "stages", "model") if k in build}}
    trace = RUNS_DIR / task["run_id"] / "model_trace.jsonl" if task["run_id"] else None
    observed = {}
    if trace and trace.is_file():
        for line in trace.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event["kind"] not in {"model.response", "model.error"}:
                continue
            phase = "build" if event["phase"].startswith("build.") else event["phase"]
            if phase in {"decision", "review", "build"}:
                observed.setdefault(phase, []).append(event["payload"].get("usage", {"requests": 1}))
    for phase, calls in observed.items():
        payload[phase] = {**(payload.get(phase) or {}), "usage": combine(calls)}
    return jsonify(payload)


@app.get("/api/solver-roles")
def api_solver_roles():
    return jsonify({
        "primary": {"name": "ALNS", "role": "当前实验实际求解", "kernels": ["alns_lab", "hhcrsp_lab"]},
        "gurobi": {"name": "Gurobi", "role": "精确求解 / 参考解",
                   "installed": importlib.util.find_spec("gurobipy") is not None,
                   "active": False, "license": "未检测",
                   "reference": "apps/platform/reference_llm_experiment.py",
                   "note": "参考实验使用简化手术模型；当前两类实例尚未接入 Gurobi 执行。"},
        "generated": {"role": "模型求解代码提案", "target": "Gurobi / gurobipy",
                      "executed": False},
    })


@app.get("/api/tasks/<int:task_id>/audit")
def api_audit(task_id: int):
    row = task_row(task_id)
    if row is None:
        return jsonify({"error": "任务不存在。"}), 404
    path = RUNS_DIR / row["run_id"] / "model_trace.jsonl" if row["run_id"] else None
    records = []
    if path and path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue  # The final line may still be being appended.
    with db() as conn:
        events = [dict(r) for r in conn.execute(
            "SELECT ts,kind,detail FROM events WHERE task_id=? ORDER BY id", (task_id,))]
        legacy = [dict(r) for r in conn.execute(
            "SELECT seq,mode,title,text FROM decisions WHERE task_id=? ORDER BY seq", (task_id,))]
    return jsonify({"task_id": task_id, "events": records, "lifecycle": events,
                    "legacy_steps": legacy if not records else [],
                    "complete_messages": bool(records),
                    "status": row["status"], "tool_mode": row["tool_mode"] or "native"})


@app.get("/api/tasks/<int:task_id>/proposal")
def api_proposal(task_id: int):
    row = task_row(task_id)
    if row is None:
        return jsonify({"error": "任务不存在。"}), 404
    path = RUNS_DIR / row["run_id"] / "decision.json" if row["run_id"] else None
    proposal = _read_json(path) if path else None
    if not proposal and row["run_id"]:
        manifest = _read_json(RUNS_DIR / row["run_id"] / "manifest.json") or {}
        if manifest.get("program"):
            with db() as conn:
                first = conn.execute("SELECT meta FROM decisions WHERE task_id=? ORDER BY seq LIMIT 1", (task_id,)).fetchone()
            metadata = json.loads(first["meta"]) if first else {}
            proposal = {"program": manifest["program"], "historical": True,
                        "origin": "llm" if row["use_deepseek"] and first and not metadata.get("fallback") else "system",
                        "fallback": bool(metadata.get("fallback"))}
    return jsonify({"status": row["status"], "proposal": proposal,
                    "sha256": row["decision_sha256"]})


MATH_NS = 'xmlns="http://www.w3.org/1998/Math/MathML" display="block"'


def _m(inner: str) -> str:
    return f"<math {MATH_NS}>{inner}</math>"


def _sub(base: str, sub: str) -> str:
    return f"<msub><mi>{base}</mi><mrow>{sub}</mrow></msub>"


X = _sub("x", "<mi>p</mi><mo>,</mo><mi>h</mi><mo>,</mo><mi>o</mi><mo>,</mo><mi>d</mi>")
Y = _sub("y", "<mi>h</mi><mo>,</mo><mi>o</mi><mo>,</mo><mi>d</mi>")
Z = _sub("z", "<mi>s</mi><mo>,</mo><mi>h</mi><mo>,</mo><mi>d</mi>")
TAU = _sub("τ", "<mi>p</mi>")

CONSTRAINT_MATH = {
    "K1": _m(f"<munder><mo>∑</mo><mrow><mi>h</mi><mo>,</mo><mi>o</mi><mo>,</mo><mi>d</mi></mrow></munder>{X}<mo>=</mo><mn>1</mn><mo>,</mo><mspace width=\"0.3em\"/><mo>∀</mo><mi>p</mi><mo>∈</mo><mi>P</mi>"),
    "K2": _m(f"<munder><mo>∑</mo><mrow><mi>h</mi><mo>,</mo><mi>o</mi><mo>,</mo><mi>d</mi></mrow></munder>{X}<mo>=</mo><mn>1</mn><mo>,</mo><mspace width=\"0.3em\"/><mo>∀</mo><mi>p</mi><mo>∈</mo><mi>M</mi><mo>⊆</mo><mi>P</mi>"),
    "K3": _m(f"<munder><mo>∑</mo><mi>p</mi></munder>{TAU}{X}<mo>≤</mo><msub><mi>R</mi><mrow><mi>h</mi><mo>,</mo><mi>o</mi><mo>,</mo><mi>d</mi></mrow></msub><mo>+</mo><msub><mi>O</mi><mrow><mi>h</mi><mo>,</mo><mi>o</mi><mo>,</mo><mi>d</mi></mrow></msub><mo>,</mo><mspace width=\"0.3em\"/><msub><mi>O</mi><mrow><mi>h</mi><mo>,</mo><mi>o</mi><mo>,</mo><mi>d</mi></mrow></msub><mo>≤</mo><msubsup><mi>O</mi><mrow></mrow><mi>max</mi></msubsup>"),
    "K4": _m("<mo>[</mo><msub><mi>s</mi><mi>i</mi></msub><mo>,</mo><msub><mi>s</mi><mi>i</mi></msub><mo>+</mo><msub><mi>τ</mi><mi>i</mi></msub><mo>)</mo><mo>∩</mo><mo>[</mo><msub><mi>s</mi><mi>j</mi></msub><mo>,</mo><msub><mi>s</mi><mi>j</mi></msub><mo>+</mo><msub><mi>τ</mi><mi>j</mi></msub><mo>)</mo><mo>=</mo><mo>∅</mo>"),
    "K5": _m(f"<munder><mo>∑</mo><mrow><mi>p</mi><mo>∈</mo><msub><mi>P</mi><mi>s</mi></msub></mrow></munder>{TAU}{X}<mo>≤</mo><msub><mi>A</mi><mrow><mi>s</mi><mo>,</mo><mi>d</mi></mrow></msub>"),
    "K6": _m("<mo>[</mo><msub><mi>s</mi><mi>i</mi></msub><mo>,</mo><msub><mi>s</mi><mi>i</mi></msub><mo>+</mo><msub><mi>τ</mi><mi>i</mi></msub><mo>)</mo><mo>∩</mo><mo>[</mo><msub><mi>s</mi><mi>j</mi></msub><mo>,</mo><msub><mi>s</mi><mi>j</mi></msub><mo>+</mo><msub><mi>τ</mi><mi>j</mi></msub><mo>)</mo><mo>=</mo><mo>∅</mo><mo>,</mo><mspace width=\"0.3em\"/><msub><mi>s</mi><mi>i</mi></msub><mo>=</mo><msub><mi>s</mi><mi>j</mi></msub>"),
    "K7": _m("<mn>0</mn><mo>≤</mo><mi>d</mi><mo>≤</mo><mi>D</mi><mo>−</mo><mn>1</mn><mo>,</mo><mspace width=\"0.3em\"/><mi>o</mi><mo>∈</mo><msub><mi>OR</mi><mi>h</mi></msub>"),
    "K8": _m("<mi>d</mi><mo>≤</mo><msub><mi>due</mi><mi>p</mi></msub>"),
    "K9": _m(f"<munder><mo>∑</mo><mi>p</mi></munder><mrow><mo>(</mo>{TAU}<mo>+</mo><msub><mi>π</mi><mi>p</mi></msub><mo>+</mo><msub><mi>κ</mi><mi>p</mi></msub><mo>)</mo></mrow>{X}<mo>≤</mo><msub><mi>R</mi><mrow><mi>h</mi><mo>,</mo><mi>o</mi><mo>,</mo><mi>d</mi></mrow></msub>"),
    "K10": _m(f"{Y}<mo>=</mo><mn>1</mn><mspace width=\"0.3em\"/><mo>⇔</mo><mspace width=\"0.3em\"/><munder><mo>∑</mo><mi>p</mi></munder>{X}<mo>&gt;</mo><mn>0</mn>"),
    "K11": _m(f"<mi>p</mi><mo>∉</mo><mi>M</mi><mo>,</mo><mspace width=\"0.3em\"/><msub><mi>r</mi><mi>p</mi></msub><mo>=</mo><mn>0</mn><mspace width=\"0.3em\"/><mo>⇒</mo><mspace width=\"0.3em\"/>{X}<mo>=</mo><mn>0</mn><mspace width=\"0.3em\"/><mtext>允许</mtext>"),
}

OBJECTIVE_MATH = _m(
    "<munder><mtext>max</mtext><mrow><mi>x</mi><mo>,</mo><mi>y</mi><mo>,</mo><mi>z</mi></mrow></munder>"
    "<mspace width=\"0.3em\"/><mi>α</mi><mi>R</mi><mo>−</mo><mo>(</mo><mn>1</mn><mo>−</mo><mi>α</mi><mo>)</mo>"
    "<mo>(</mo><msub><mi>C</mi><mi>or</mi></msub><mo>+</mo><msub><mi>C</mi><mi>ot</mi></msub><mo>+</mo>"
    "<msub><mi>C</mi><mi>s</mi></msub><mo>)</mo><mo>−</mo><mi>M</mi><mi>U</mi>"
)

OBJECTIVE_DEFS = _m(
    "<mi>R</mi><mo>=</mo><munder><mo>∑</mo><mi>p</mi></munder><msub><mi>r</mi><mi>p</mi></msub>"
    "<munder><mo>∑</mo><mrow><mi>h</mi><mo>,</mo><mi>o</mi><mo>,</mo><mi>d</mi></mrow></munder>"
    + X + "<mspace width=\"1em\"/><mi>U</mi><mo>=</mo><mo>|</mo><mi>M</mi><mo>|</mo><mo>−</mo>"
    "<munder><mo>∑</mo><mrow><mi>p</mi><mo>∈</mo><mi>M</mi></mrow></munder>"
    "<munder><mo>∑</mo><mrow><mi>h</mi><mo>,</mo><mi>o</mi><mo>,</mo><mi>d</mi></mrow></munder>" + X
)


def _program_of(run_dir: Path | None) -> dict:
    if not run_dir:
        return {}
    payload = _read_json(run_dir / "manifest.json") or {}
    return payload.get("program") or {}


_PIPELINE_RUNNING: set[int] = set()
_PIPELINE_LOCK = threading.Lock()


def _pipeline_view(manifest):
    """Normalize older reports that ran static checks against skipped stages' placeholders."""
    stages = [dict(stage) for stage in manifest.get("stages", [])]
    skipped = 0
    for stage in stages:
        if stage.get("state") == "skipped" or stage.get("error") == "上游规格不合格，未调用":
            stage.update(state="skipped", checks=[])
            skipped += 1
    checks = [check for stage in stages for check in stage.get("checks", [])]
    passed = sum(bool(check["ok"]) for check in checks)
    validation = dict(manifest.get("validation") or {})
    if skipped:
        validation.update(passed=passed, failed=len(checks) - passed, ok=False)
    return {**manifest, "stages": stages, "validation": validation}


@app.get("/api/tasks/<int:task_id>/pipeline")
def api_pipeline(task_id: int):
    row = task_row(task_id)
    if row is None:
        return jsonify({"error": "任务不存在。"}), 404
    run_dir = RUNS_DIR / row["run_id"] if row["run_id"] else None
    pdir = run_dir / "pipeline" if run_dir else None
    manifest = _read_json(pdir / "manifest.json") if pdir else None
    if manifest:
        payload = _pipeline_view(manifest)
        payload["state"] = "done"
        contents = {}
        for name in BUILD_ARTIFACTS:
            path = pdir / name
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                contents[name] = text
        payload["contents"] = contents
        for stage in payload["stages"]:
            raw = contents.get(stage.get("artifact"), "").strip()
            missing = not raw or raw.startswith("# 模型没有返回代码")
            if stage.get("artifact", "").endswith(".json"):
                try:
                    value = json.loads(raw)
                    missing = isinstance(value, dict) and set(value) == {"raw"} and not value["raw"]
                except ValueError:
                    pass
            if missing or stage["state"] == "skipped":
                contents.pop(stage.get("artifact"), None)
                stage["summary"] = ""
                if stage["state"] == "failed" and not stage.get("error"):
                    stage["error"] = "模型回复正文为空，未生成此阶段产物。"
        payload["task_id"] = task_id
        return jsonify(payload)
    status = _read_json(pdir / "status.json") if pdir else None
    if status:
        status["task_id"] = task_id
        status["contents"] = {}
        status["artifacts"] = []
        started = datetime.fromisoformat(status["started_at"]).timestamp() if status.get("started_at") else float("inf")
        for name in ("spec.json", "model.json", "solver.py"):
            path = pdir / name
            if path.is_file() and path.stat().st_mtime >= started:
                status["contents"][name] = path.read_text(encoding="utf-8")
                status["artifacts"].append({"path": name, "origin": "llm"})
        return jsonify(status)
    return jsonify({"state": "idle", "task_id": task_id})


@app.post("/api/tasks/<int:task_id>/pipeline")
def api_pipeline_start(task_id: int):
    row = task_row(task_id)
    if row is None:
        return jsonify({"error": "任务不存在。"}), 404
    if not row["run_id"]:
        return jsonify({"error": "任务还没有运行目录。"}), 400
    if not row["use_deepseek"]:
        return jsonify({"error": "该任务没有启用 DeepSeek。"}), 400
    with _PIPELINE_LOCK:
        if task_id in _PIPELINE_RUNNING:
            return jsonify({"error": "这条任务正在生成。"}), 409
        _PIPELINE_RUNNING.add(task_id)
    threading.Thread(target=_pipeline_worker, args=(task_id,), daemon=True).start()
    return jsonify({"state": "running", "task_id": task_id})


def _pipeline_worker(task_id: int) -> None:
    try:
        _pipeline_task(task_id)
    finally:
        with _PIPELINE_LOCK:
            _PIPELINE_RUNNING.discard(task_id)


@app.get("/api/tasks/<int:task_id>/model")
def api_model(task_id: int):
    row = task_row(task_id)
    if row is None:
        return jsonify({"error": "任务不存在。"}), 404
    if (row["scenario"] or "surgical") == "hhcrsp":
        return _model_hhcrsp(task_id, row)
    run_dir = RUNS_DIR / row["run_id"] if row["run_id"] else None
    snapshot = _read_json(run_dir / "instance_snapshot.json") if run_dir else None
    checker = (_read_json(run_dir / "checker_result.json") if run_dir else None) or {}
    metrics = (_read_json(run_dir / "metrics.json") if run_dir else None) or {}
    if not snapshot:
        return jsonify({"error": "实例快照尚未生成。"}), 404

    patients = snapshot.get("patients") or []
    hospitals = snapshot.get("hospitals") or []
    surgeons = snapshot.get("surgeons") or []
    ors = sum(len(item.get("ORs") or []) for item in hospitals)
    days = int(snapshot.get("num_days") or 0)
    capacity = 0.0
    for hospital in hospitals:
        for or_item in hospital.get("ORs") or []:
            capacity += sum(float(day.get("regular_time") or 0)
                            for day in (or_item.get("daily") or {}).values())
    total_duration = sum(float(p.get("base_surgical_time") or 0) for p in patients)
    components = metrics.get("components") or {}

    variables = [
        {"symbol": "P, D, H, O, S", "name": "参数规模",
         "value": f"{len(patients)} 患者 / {days} 天 / {len(hospitals)} 医院 / {ors} 手术室 / {len(surgeons)} 医生",
         "note": "来自实例快照，不可由搜索改变"},
        {"symbol": "x[p,h,o,d] ∈ {0,1}", "name": "患者指派",
         "value": f"已指派 {int(components.get('served_count') or 0)} / {len(patients)}",
         "note": "决定患者在哪家医院、哪间手术室、哪一天"},
        {"symbol": "y[h,o,d] ∈ {0,1}", "name": "手术室开启",
         "value": f"开启 {int(components.get('active_or_days') or 0)} 个 OR-日",
         "note": "开启才计固定成本，也才允许排入患者"},
        {"symbol": "z[s,h,d] ∈ {0,1}", "name": "医生到院",
         "value": f"{int(components.get('active_surgeon_hospital_days') or 0)} 个医生-医院-日",
         "note": "与医生固定成本联动"},
        {"symbol": "u[p] ∈ {0,1}", "name": "未安排",
         "value": f"{int(components.get('unassigned_count') or 0)} 名患者",
         "note": "可选患者可跳过；强制患者不计为违反以外的惩罚"},
        {"symbol": "容量", "name": "可用手术时长",
         "value": f"{capacity:,.0f} 分钟 / 需求 {total_duration:,.0f} 分钟",
         "note": f"负荷率 {total_duration / capacity * 100:.1f}%" if capacity else "无容量数据"},
    ]

    raw_constraints = checker.get("constraints") or {}
    constraints = []
    for code in [f"K{i}" for i in range(1, 12)]:
        item = raw_constraints.get(code) or {}
        constraints.append({
            "code": code,
            "name": CONSTRAINT_LABELS.get(code, code),
            "math": CONSTRAINT_MATH.get(code, ""),
            "status": item.get("status") or "unknown",
            "violations": int(item.get("violation_count") or 0),
            "note": item.get("note") or "",
        })

    formulas = checker.get("objective_formulas") or {}
    objective = {
        "sense": (checker.get("objective_sense") or {}).get("O2", "maximize"),
        "expression": formulas.get("O2") or "",
        "math": OBJECTIVE_MATH,
        "definitions": OBJECTIVE_DEFS,
        "value": metrics.get("primary_objective"),
        "components": [
            {"name": "reward", "value": components.get("reward")},
            {"name": "or_fixed", "value": components.get("or_fixed")},
            {"name": "overtime", "value": components.get("overtime")},
            {"name": "surgeon_fixed", "value": components.get("surgeon_fixed")},
            {"name": "mandatory_unserved", "value": components.get("mandatory_unserved")},
        ],
    }

    program = _program_of(run_dir)
    with db() as conn:
        meta = conn.execute(
            "SELECT meta FROM decisions WHERE task_id=? ORDER BY seq LIMIT 1", (task_id,)).fetchone()
        prev = conn.execute(
            """SELECT id, run_id, wall_clock_s, seed FROM tasks
               WHERE instance_id=? AND id<? AND status='succeeded'
               ORDER BY id DESC LIMIT 1""", (row["instance_id"], task_id)).fetchone()
    decision_meta = json.loads(meta["meta"]) if meta else {}
    config = {
        "dataset": _INSTANCES.get(row["instance_id"], {}).get("dataset"),
        "operators": f"{'、'.join(program.get('destroy_operators') or []) or '—'} → "
                     f"{'、'.join(program.get('repair_operators') or []) or '—'}",
        "fraction": program.get("destroy_fraction"),
        "acceptance": program.get("acceptance"),
        "budget": row["wall_clock_s"],
        "seed": row["seed"],
        "decision": (decision_meta.get("model") or row["model"]) if row["use_deepseek"] else "未启用",
        "fallback": bool(decision_meta.get("fallback")),
    }
    diff = []
    previous = None
    if prev is not None and prev["run_id"]:
        prev_program = _program_of(RUNS_DIR / prev["run_id"])
        previous = {
            "task_id": prev["id"],
            "operators": f"{'、'.join(prev_program.get('destroy_operators') or []) or '—'} → "
                         f"{'、'.join(prev_program.get('repair_operators') or []) or '—'}",
            "fraction": prev_program.get("destroy_fraction"),
            "acceptance": prev_program.get("acceptance"),
            "budget": prev["wall_clock_s"],
            "seed": prev["seed"],
        }
        for field, label in (("operators", "算子"), ("fraction", "破坏比例"),
                             ("acceptance", "接受准则"), ("budget", "时间预算"), ("seed", "种子")):
            if config[field] != previous[field]:
                diff.append({"field": label, "from": previous[field], "to": config[field]})

    return jsonify({
        "task_id": task_id,
        "scenario": "surgical",
        "dataset": config["dataset"],
        "instance_id": row["instance_id"],
        "checker_mode": (checker.get("checker") or {}).get("mode"),
        "official_checker": (checker.get("checker") or {}).get("official_checker"),
        "assumptions": (checker.get("checker") or {}).get("assumption_ids") or [],
        "variables": variables,
        "constraints": constraints,
        "objective": objective,
        "config": config,
        "previous": previous,
        "diff": diff,
        "pipeline": [
            {"stage": "结构化", "text": "实例与决策口径"},
            {"stage": "数学模型", "text": "变量 · 约束 · 目标"},
            {"stage": "求解", "text": "ALNS + 独立校验"},
        ],
    })


HHCRSP_MATH = {
    "H1": '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><mi>R</mi><mo>(</mo><mi>p</mi><mo>)</mo><mo>\u2286</mo><mi>S</mi><mo>(</mo><mi>k</mi><mo>)</mo></math>',
    "H2": '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><msub><mi>x</mi><mrow><mi>p</mi><mo>,</mo><mi>k</mi><mo>,</mo><mi>d</mi></mrow></msub><mo>\u2264</mo><msub><mi>a</mi><mrow><mi>k</mi><mo>,</mo><mi>d</mi></mrow></msub><mo>,</mo><mspace width="0.3em"/><mo>\u2200</mo><mi>p</mi><mo>,</mo><mi>k</mi><mo>,</mo><mi>d</mi></math>',
    "H3": '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><munder><mo>\u2211</mo><mrow><mi>k</mi><mo>\u2208</mo><mi>K</mi></mrow></munder><munder><mo>\u2211</mo><mrow><mi>d</mi><mo>\u2208</mo><mi>D</mi></mrow></munder><msub><mi>x</mi><mrow><mi>p</mi><mo>,</mo><mi>k</mi><mo>,</mo><mi>d</mi></mrow></msub><mo>=</mo><msub><mi>f</mi><mi>p</mi></msub><mo>,</mo><mspace width="0.3em"/><mo>\u2200</mo><mi>p</mi><mo>\u2208</mo><mi>P</mi></math>',
    "H4": '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><munder><mo>\u2211</mo><mrow><mi>k</mi><mo>\u2208</mo><mi>K</mi></mrow></munder><msub><mi>x</mi><mrow><mi>p</mi><mo>,</mo><mi>k</mi><mo>,</mo><mi>d</mi></mrow></msub><mo>\u2264</mo><mn>1</mn><mo>,</mo><mspace width="0.3em"/><mo>\u2200</mo><mi>p</mi><mo>,</mo><mi>d</mi></math>',
    "H5": '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><msub><mi>x</mi><mrow><mi>p</mi><mo>,</mo><mi>k</mi><mo>,</mo><mi>d</mi></mrow></msub><mo>+</mo><msub><mi>x</mi><mrow><mi>p</mi><mo>,</mo><msup><mi>k</mi><mo>\u2032</mo></msup><mo>,</mo><msup><mi>d</mi><mo>\u2032</mo></msup></mrow></msub><mo>\u2264</mo><mn>1</mn><mo>,</mo><mspace width="0.3em"/><mn>0</mn><mo>&lt;</mo><mo>|</mo><mi>d</mi><mo>\u2212</mo><msup><mi>d</mi><mo>\u2032</mo></msup><mo>|</mo><mo>&lt;</mo><msub><mi>m</mi><mi>p</mi></msub></math>',
    "H6": '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><msub><mi>e</mi><mi>p</mi></msub><mo>\u2264</mo><msub><mi>s</mi><mrow><mi>p</mi><mo>,</mo><mi>k</mi><mo>,</mo><mi>d</mi></mrow></msub><mo>\u2264</mo><msub><mi>l</mi><mi>p</mi></msub><mo>,</mo><mspace width="0.3em"/><mo>\u2200</mo><mi>p</mi></math>',
    "H7": '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><munder><mo>\u2211</mo><mrow><mo>(</mo><mi>i</mi><mo>,</mo><mi>j</mi><mo>)</mo><mo>\u2208</mo><msub><mi>\u03c0</mi><mrow><mi>k</mi><mo>,</mo><mi>d</mi></mrow></msub></mrow></munder><msub><mi>t</mi><mrow><mi>i</mi><mi>j</mi></mrow></msub><mo>+</mo><munder><mo>\u2211</mo><mrow><mi>p</mi><mo>\u2208</mo><msub><mi>\u03c0</mi><mrow><mi>k</mi><mo>,</mo><mi>d</mi></mrow></msub></mrow></munder><msub><mi>\u03c4</mi><mi>p</mi></msub><mo>\u2264</mo><msub><mi>W</mi><mi>k</mi></msub></math>',
    "H8": '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><msub><mi>\u03c0</mi><mrow><mi>k</mi><mo>,</mo><mi>d</mi></mrow></msub><mo>=</mo><mo>(</mo><mn>0</mn><mo>,</mo><msub><mi>p</mi><mn>1</mn></msub><mo>,</mo><mo>\u2026</mo><mo>,</mo><msub><mi>p</mi><mi>n</mi></msub><mo>,</mo><mn>0</mn><mo>)</mo></math>',
}

HHCRSP_OBJECTIVE = (
    '<math xmlns="http://www.w3.org/1998/Math/MathML" display="block">'
    '<munder><mtext>min</mtext><mrow><mi>x</mi><mo>,</mo><mi>y</mi><mo>,</mo><mi>\u03c0</mi></mrow></munder>'
    '<mspace width="0.3em"/><munder><mo>\u2211</mo><mrow><mi>k</mi><mo>\u2208</mo><mi>K</mi></mrow></munder>'
    '<munder><mo>\u2211</mo><mrow><mi>d</mi><mo>\u2208</mo><mi>D</mi></mrow></munder>'
    '<mrow><mo>[</mo><msub><mi>f</mi><mi>k</mi></msub><msub><mi>y</mi><mrow><mi>k</mi><mo>,</mo><mi>d</mi></mrow></msub>'
    '<mo>+</mo><mi>c</mi><munder><mo>\u2211</mo><mrow><mo>(</mo><mi>i</mi><mo>,</mo><mi>j</mi><mo>)</mo>'
    '<mo>\u2208</mo><msub><mi>\u03c0</mi><mrow><mi>k</mi><mo>,</mo><mi>d</mi></mrow></msub></mrow></munder>'
    '<msub><mi>t</mi><mrow><mi>i</mi><mi>j</mi></mrow></msub><mo>]</mo></mrow></math>'
)

HHCRSP_LABELS = {
    "H1": "\u6280\u80fd\u5339\u914d", "H2": "\u6392\u73ed\u53ef\u7528", "H3": "\u8bbf\u89c6\u9891\u6b21",
    "H4": "\u540c\u65e5\u4e0d\u91cd\u590d", "H5": "\u6700\u5c0f\u95f4\u9694", "H6": "\u670d\u52a1\u65f6\u95f4\u7a97",
    "H7": "\u65e5\u5de5\u65f6\u4e0a\u9650", "H8": "\u8def\u7ebf\u95ed\u5408",
}


def _model_hhcrsp(task_id: int, row):
    run_dir = RUNS_DIR / row["run_id"] if row["run_id"] else None
    snapshot = _read_json(run_dir / "instance_snapshot.json") if run_dir else None
    checker = (_read_json(run_dir / "checker_result.json") if run_dir else None) or {}
    metrics = (_read_json(run_dir / "metrics.json") if run_dir else None) or {}
    if not snapshot:
        return jsonify({"error": "\u5b9e\u4f8b\u5feb\u7167\u5c1a\u672a\u751f\u6210\u3002"}), 404

    patients = snapshot.get("patients") or []
    clinicians = snapshot.get("clinicians") or []
    days = int(snapshot.get("planning_days") or 7)
    visits = sum(int(p.get("visit_frequency") or 0) for p in patients)
    window = (patients[0].get("time_window") if patients else [0, 0]) or [0, 0]
    width = float(window[1]) - float(window[0])
    work_limit = float((clinicians[0] or {}).get("daily_work_limit") or 480) if clinicians else 480.0
    components = metrics.get("components") or {}
    served = int(components.get("served_visits") or 0)
    active_routes = int(components.get("active_routes") or 0)
    avg_route = (served / active_routes) if active_routes else 0.0

    variables = [
        {"symbol": "P, K, D", "name": "\u53c2\u6570\u89c4\u6a21",
         "value": f"{len(patients)} \u540d\u60a3\u8005 / {len(clinicians)} \u540d\u62a4\u7406\u5458 / {days} \u5929\uff0c\u5171 {visits} \u6b21\u8bbf\u89c6",
         "note": "\u6765\u81ea\u5b9e\u4f8b\u5feb\u7167\uff0c\u4e0d\u53ef\u7531\u641c\u7d22\u6539\u53d8"},
        {"symbol": "x[p,k,d] \u2208 {0,1}", "name": "\u8bbf\u89c6\u6307\u6d3e",
         "value": f"\u5df2\u6307\u6d3e {served} / {visits} \u6b21",
         "note": "\u5b9a\u4e49\u60a3\u8005\u7684\u67d0\u6b21\u8bbf\u89c6\u7531\u54ea\u540d\u62a4\u7406\u5458\u5728\u54ea\u4e00\u5929\u5b8c\u6210"},
        {"symbol": "y[k,d] \u2208 {0,1}", "name": "\u62a4\u7406\u5458\u542f\u7528",
         "value": f"\u542f\u7528 {active_routes} \u4e2a\uff08\u62a4\u7406\u5458, \u65e5\uff09",
         "note": "\u542f\u7528\u624d\u8ba1\u56fa\u5b9a\u6210\u672c\uff0c\u4e5f\u624d\u5141\u8bb8\u6392\u7ebf"},
        {"symbol": "\u03c0[k,d]", "name": "\u8def\u7ebf\u987a\u5e8f",
         "value": f"\u5e73\u5747\u6bcf\u6761\u7ebf {avg_route:.1f} \u4e2a\u8bbf\u89c6",
         "note": "\u51b3\u5b9a\u5230\u8fbe\u65f6\u95f4\u4e0e\u65c5\u884c\u6210\u672c"},
        {"symbol": "u[p] \u2208 \u2124\u2265\u2080", "name": "\u672a\u843d\u5b9e\u8bbf\u89c6",
         "value": f"{max(0, visits - served)} \u6b21",
         "note": "\u8bbf\u89c6\u9891\u6b21\u672a\u6ee1\u8db3\u65f6\u8ba1\u5165 H3 \u8fdd\u53cd"},
        {"symbol": "[e_p, l_p], W_k", "name": "\u65f6\u95f4\u7a97\u4e0e\u5de5\u65f6",
         "value": f"\u7a97\u5bbd {width:.0f} \u5206\u949f\uff1b\u65e5\u5de5\u65f6 {work_limit:.0f} \u5206\u949f",
         "note": "\u63d0\u524d\u5230\u8fbe\u5141\u8bb8\u7b49\u5f85\uff0c\u665a\u4e8e\u6700\u665a\u5f00\u59cb\u5373\u8fdd\u53cd"},
    ]

    raw_constraints = checker.get("constraints") or {}
    constraints = []
    for code in [f"H{i}" for i in range(1, 9)]:
        item = raw_constraints.get(code) or {}
        constraints.append({
            "code": code,
            "name": HHCRSP_LABELS.get(code, code),
            "math": HHCRSP_MATH.get(code, ""),
            "status": item.get("status") or "unknown",
            "violations": int(item.get("violation_count") or 0),
            "note": item.get("note") or "",
        })

    program = _program_of(run_dir)
    with db() as conn:
        meta = conn.execute(
            "SELECT meta FROM decisions WHERE task_id=? ORDER BY seq LIMIT 1", (task_id,)).fetchone()
        prev = conn.execute(
            """SELECT id, run_id, wall_clock_s, seed FROM tasks
               WHERE instance_id=? AND id<? AND status='succeeded'
               ORDER BY id DESC LIMIT 1""", (row["instance_id"], task_id)).fetchone()
    decision_meta = json.loads(meta["meta"]) if meta else {}
    config = {
        "dataset": _INSTANCES.get(row["instance_id"], {}).get("dataset"),
        "operators": f"{'\u3001'.join(program.get('destroy_operators') or []) or '\u2014'} \u2192 "
                     f"{'\u3001'.join(program.get('repair_operators') or []) or '\u2014'}",
        "fraction": program.get("destroy_fraction"),
        "acceptance": program.get("acceptance"),
        "budget": row["wall_clock_s"],
        "seed": row["seed"],
        "decision": (decision_meta.get("model") or row["model"]) if row["use_deepseek"] else "未启用",
        "fallback": bool(decision_meta.get("fallback")),
    }
    diff = []
    previous = None
    if prev is not None and prev["run_id"]:
        prev_program = _program_of(RUNS_DIR / prev["run_id"])
        previous = {
            "task_id": prev["id"],
            "operators": f"{'\u3001'.join(prev_program.get('destroy_operators') or []) or '\u2014'} \u2192 "
                         f"{'\u3001'.join(prev_program.get('repair_operators') or []) or '\u2014'}",
            "fraction": prev_program.get("destroy_fraction"),
            "acceptance": prev_program.get("acceptance"),
            "budget": prev["wall_clock_s"],
            "seed": prev["seed"],
        }
        for field, label in (("operators", "\u7b97\u5b50"), ("fraction", "\u7834\u574f\u6bd4\u4f8b"),
                             ("acceptance", "\u63a5\u53d7\u51c6\u5219"), ("budget", "\u65f6\u95f4\u9884\u7b97"),
                             ("seed", "\u79cd\u5b50")):
            if config[field] != previous[field]:
                diff.append({"field": label, "from": previous[field], "to": config[field]})

    checker_meta = checker.get("checker") or {}
    return jsonify({
        "task_id": task_id,
        "scenario": "hhcrsp",
        "dataset": config["dataset"],
        "instance_id": row["instance_id"],
        "checker_mode": checker_meta.get("mode"),
        "official_checker": checker_meta.get("official_checker"),
        "assumptions": checker_meta.get("assumption_ids") or [],
        "model": "nominal-seven-day-hhcrsp",
        "variables": variables,
        "constraints": constraints,
        "config": config,
        "previous": previous,
        "diff": diff,
        "objective": {
            "sense": "minimize",
            "expression": (checker.get("objective_formulas") or {}).get("O1") or "",
            "math": HHCRSP_OBJECTIVE,
            "definitions": "",
            "value": metrics.get("primary_objective"),
            "components": [
                {"name": "fixed_cost", "value": components.get("fixed_cost")},
                {"name": "travel_cost", "value": components.get("travel_cost")},
                {"name": "travel_minutes", "value": components.get("travel_minutes")},
                {"name": "active_routes", "value": components.get("active_routes")},
            ],
        },
        "pipeline": [
            {"stage": "\u7ed3\u6784\u5316", "text": "\u534e\u897f\u7b97\u4f8b\u4e0e\u4e03\u5929\u53e3\u5f84"},
            {"stage": "\u6570\u5b66\u6a21\u578b", "text": "\u53d8\u91cf \u00b7 \u7ea6\u675f \u00b7 \u76ee\u6807"},
            {"stage": "\u6c42\u89e3", "text": "HHCRSP-ALNS + \u72ec\u7acb\u6821\u9a8c"},
        ],
    })


@app.get("/api/tasks/<int:task_id>/trajectory")
def api_trajectory(task_id: int):
    detail = task_detail(task_id)
    if detail is None:
        return jsonify({"error": "任务不存在。"}), 404
    row = task_row(task_id)
    run_dir = RUNS_DIR / row["run_id"] if row["run_id"] else None
    timeline = _read_json(run_dir / "timeline.json") if run_dir else None
    phases = []
    if timeline:
        cursor = 0
        for phase in timeline.get("phases", []):
            phases.append({**phase, "start": cursor})
            cursor += phase["ms"]
    else:
        # 旧任务没有 timeline.json：只用确定存在的耗时，不补假数据。
        cursor = 0
        rowd = None
        with db() as conn:
            rowd = conn.execute(
                "SELECT meta FROM decisions WHERE task_id=? ORDER BY seq LIMIT 1", (task_id,)).fetchone()
        if rowd:
            try:
                latency = float(json.loads(rowd["meta"]).get("latency_s") or 0)
            except (ValueError, TypeError):
                latency = 0.0
            if latency:
                phases.append({"lane": "model", "label": detail.get("model") or "模型决策",
                               "ms": int(latency * 1000), "start": cursor})
                cursor += int(latency * 1000)
        if detail["runtime_s"]:
            ms = int(detail["runtime_s"] * 1000)
            phases.append({"lane": "tool", "label": "ALNS 求解", "ms": ms, "start": cursor})
            cursor += ms

    with db() as conn:
        steps = conn.execute(
            "SELECT mode, title, text, meta FROM decisions WHERE task_id=? ORDER BY seq",
            (task_id,)).fetchall()
    meta = json.loads(steps[0]["meta"]) if steps else {}
    by_mode = {}
    for step in steps:
        by_mode.setdefault(step["mode"], []).append(step)

    events_round1 = [
        {"role": "context", "label": "上下文",
         "text": f"实例 {detail['instance_id']}，预算 {detail['wall_clock_s']:.0f} 秒，种子 {detail['seed']}；"
                 f"历史样本见稳定性维度。"},
        {"role": "user", "label": "用户",
         "text": f"运行 {detail['instance_id']}，预算 {detail['wall_clock_s']:.0f} 秒，"
                 f"{'先由模型判断' if detail['use_deepseek'] else '使用默认参数'}。"},
    ]
    for step in by_mode.get("thinking", [])[:1]:
        events_round1.append({"role": "thinking", "label": "思考", "text": step["text"]})
    for step in by_mode.get("assistant", [])[:1]:
        events_round1.append({"role": "assistant", "label": "助手", "text": step["text"]})
    for step in by_mode.get("tool", []):
        events_round1.append({"role": "tool", "label": "工具", "text": f"{step['title']}：{step['text']}"})
    for step in by_mode.get("error", []):
        events_round1.append({"role": "error", "label": "异常", "text": step["text"]})

    events_round2 = [
        {"role": "context", "label": "上下文",
         "text": f"求解完成：执行 {detail['executed_iterations'] or 0} 轮，"
                 f"停止原因 {'时间到' if detail['stopped_by'] == 'time_limit' else detail['stopped_by'] or '未知'}。"},
        {"role": "tool", "label": "工具",
         "text": (f"{'hhcrsp_lab' if (row['scenario'] or 'surgical') == 'hhcrsp' else 'alns_lab'} 返回解；"
                  f"独立 checker 重算：{'可行' if detail['feasible'] else '不可行'}，"
                  f"{'硬违反' if (row['scenario'] or 'surgical') == 'surgical' else '约束违反'} "
                  f"{detail['violations'] or 0} 条。")},
    ]
    if detail["report"]:
        events_round2.append({"role": "assistant", "label": "助手",
                              "text": f"{detail['report']['verdict_label']}；{detail['report']['summary']}"})

    return jsonify({
        "task_id": task_id,
        "model": meta.get("model"),
        "fallback": bool(meta.get("fallback")),
        "phases": phases,
        "total_ms": timeline.get("total_ms") if timeline else (phases[-1]["start"] + phases[-1]["ms"] if phases else 0),
        "system": ("居家护理排班的算法配置助手：先用工具核实容量与班表压力，"
                   "再给受限程序；算子白名单与参数范围由本地校验。"
                   if (row["scenario"] or "surgical") == "hhcrsp" else
                   "手术室排程的算法配置助手：先用工具核实容量与需求，"
                   "再给受限程序；算子白名单与参数范围由本地校验。"),
        "rounds": rounds_payload(events_round1, events_round2, steps),
    })


def rounds_payload(round1, round2, steps) -> list[dict]:
    rounds = [
        {"index": 1, "title": "第 1 轮", "events": round1},
        {"index": 2, "title": "第 2 轮", "events": round2},
    ]
    reviews = [step for step in steps if step["mode"] == "review"]
    if reviews:
        rounds.append({"index": 3, "title": "第 3 轮 · 复盘",
                       "events": [{"role": "assistant", "label": "助手", "text": item["text"]}
                                  for item in reviews]})
    return rounds


@app.get("/api/tasks/<int:task_id>/trace")
def api_trace(task_id: int):
    row = task_row(task_id)
    if row is None:
        return jsonify({"error": "任务不存在。"}), 404
    path = RUNS_DIR / row["run_id"] / "trace.json" if row["run_id"] else None
    if not path or not path.is_file():
        return jsonify({"points": []})
    payload = _read_json(path) or {}
    points = payload.get("points") or []
    step = max(1, len(points) // 300)
    sampled = [[int(p["iteration"]), float(p["best_objective"])] for p in points[::step]]
    if points and sampled and sampled[-1][0] != int(points[-1]["iteration"]):
        sampled.append([int(points[-1]["iteration"]), float(points[-1]["best_objective"])])
    return jsonify({"points": sampled})


@app.get("/api/tasks/<int:task_id>/artifacts/<path:name>")
def api_artifact(task_id: int, name: str):
    if name not in ARTIFACTS and name not in {f"pipeline/{n}" for n in BUILD_ARTIFACTS}:
        return jsonify({"error": "不允许读取该文件。"}), 403
    row = task_row(task_id)
    if row is None or not row["run_id"]:
        return jsonify({"error": "任务没有产物。"}), 404
    path = (RUNS_DIR / row["run_id"] / name).resolve()
    if not path.is_file() or RUNS_DIR.resolve() not in path.parents:
        return jsonify({"error": "产物不存在。"}), 404
    return send_file(path, as_attachment=True, download_name=f"task{task_id}_{Path(name).name}")


def task_detail(task_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        evaluations = conn.execute(
            "SELECT dimension, status, note, evidence FROM evaluations WHERE task_id=? ORDER BY id",
            (task_id,)).fetchall()
    report = None
    if evaluations or row["verdict"]:
        report = {
            "verdict": row["verdict"] or "blocked",
            "verdict_label": {"pass": "通过", "review": "待复核", "blocked": "不通过"}.get(row["verdict"], "不通过"),
            "summary": row["summary"] or row["error"] or "任务未完成。",
            "dimensions": [{
                "dimension": item["dimension"], "status": item["status"],
                "label": {"ok": "正常", "watch": "需复核", "blocked": "不通过",
                          "insufficient": "样本不足"}.get(item["status"], item["status"]),
                "note": item["note"], "evidence": json.loads(item["evidence"]),
            } for item in evaluations],
            "next_actions": _next_actions(row),
        }
    pipeline_state = None
    artifact_sources = {}
    if row["run_id"]:
        pdir = RUNS_DIR / row["run_id"] / "pipeline"
        manifest = _read_json(pdir / "manifest.json")
        status = _read_json(pdir / "status.json")
        if manifest:
            pipeline_state = {"state": "done", **_pipeline_view(manifest)["validation"]}
            for artifact in manifest.get("artifacts", []):
                name = artifact["path"]
                stage = next((s for s in manifest.get("stages", []) if s.get("artifact") == name), {})
                artifact_sources[f"pipeline/{name}"] = artifact.get("origin") or ("llm" if stage.get("model") else "system")
        elif status:
            pipeline_state = {"state": status.get("state")}
    return {
        "id": row["id"], "created_at": row["created_at"], "finished_at": row["finished_at"],
        "solver_started_at": row["solver_started_at"],
        "instance_id": row["instance_id"], "scenario": row["scenario"] or "surgical",
        "dataset": _INSTANCES.get(row["instance_id"], {}).get("dataset"),
        "model": row["model"], "effort": row["effort"],
        "tool_mode": row["tool_mode"] or "native",
        "solver": SCENARIO_BY_ID[row["scenario"] or "surgical"]["kernel"],
        "decision_sha256": row["decision_sha256"],
        "seed": row["seed"], "wall_clock_s": row["wall_clock_s"],
        "run_id": row["run_id"], "parent_task_id": row["parent_task_id"], "status": row["status"],
        "decision_status": row["decision_status"], "use_deepseek": row["use_deepseek"],
        "console": row["console"], "executed_iterations": row["executed_iterations"],
        "stopped_by": row["stopped_by"], "objective": row["objective"],
        "feasible": row["feasible"], "violations": row["violations"], "runtime_s": row["runtime_s"],
        "error": row["error"], "report": report, "pipeline": pipeline_state,
        "artifact_sources": artifact_sources,
        "artifacts": [n for n in ARTIFACTS if row["run_id"] and (RUNS_DIR / row["run_id"] / n).is_file()]
                     + [f"pipeline/{n}" for n in BUILD_ARTIFACTS
                        if row["run_id"] and (RUNS_DIR / row["run_id"] / "pipeline" / n).is_file()],
    }


def _next_actions(row) -> list[dict]:
    if row["run_id"]:
        payload = _read_json(RUNS_DIR / row["run_id"] / "evaluation.json") or {}
        return payload.get("next_actions", [])
    return []


init_db()
_INSTANCES = scan_instances()
reconcile_stale()


if __name__ == "__main__":
    print(f"orbench platform: http://{CONFIG['host']}:{CONFIG['port']}  instances={len(_INSTANCES)}")
    app.run(host=CONFIG["host"], port=CONFIG["port"], debug=False)
