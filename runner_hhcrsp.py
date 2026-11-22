"""HHCRSP 的求解执行与产物落盘，产物名与手术排程完全一致。"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hhcrsp_lab import (
    ALNSConfig, CHECK_SCHEMA, CHECKER_MODE, METRICS_SCHEMA, OFFICIAL_CHECKER,
    SOLUTION_SCHEMA, TRACE_SCHEMA, canonical_json, check, compute_metrics,
    load_instance, run_alns,
)


def _write(path: Path, payload: Any) -> dict[str, Any]:
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": path.name, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


def run_job(
    *,
    instance_path: Path,
    out_dir: Path,
    params: dict[str, Any],
    program: dict[str, Any],
    on_iteration: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    instance = load_instance(instance_path)
    config = ALNSConfig(
        iterations=int(params["iteration_cap"]),
        seed=int(params["seed"]),
        acceptance=str(program["acceptance"]),
        destroy_operators=tuple(str(name) for name in program["destroy_operators"]),
        repair_operators=tuple(str(name) for name in program["repair_operators"]),
        destroy_fraction=float(program["destroy_fraction"]),
    )
    solver_started = time.perf_counter()
    result = run_alns(instance, config, time_limit_s=float(params["seconds"]),
                      on_iteration=on_iteration)
    runtime_s = time.perf_counter() - solver_started
    checker = check(instance, result.best_solution)
    metrics = compute_metrics(instance, result.best_solution, checker, runtime_s=runtime_s,
                              executed_iterations=int(result.stats.get("executed_iterations") or 0))

    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, Any]] = []
    snapshot = json.dumps(instance.to_raw_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    snapshot_path = out_dir / "instance_snapshot.json"
    snapshot_path.write_bytes(snapshot.encode("utf-8"))
    artifacts.append({"path": snapshot_path.name,
                      "sha256": hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
                      "size_bytes": len(snapshot.encode("utf-8"))})
    artifacts.append(_write(out_dir / "run_config.json", config.to_dict()))
    artifacts.append(_write(out_dir / "solution.json",
                            {"schema_version": SOLUTION_SCHEMA, **result.solution_dict()}))
    artifacts.append(_write(out_dir / "checker_result.json", checker))
    artifacts.append(_write(out_dir / "metrics.json", metrics))
    trace_payload = {
        "schema_version": TRACE_SCHEMA,
        "instance_id": instance.instance_id,
        "seed": config.seed,
        "points": result.trace,
        "points_sha256": hashlib.sha256(canonical_json(result.trace).encode("utf-8")).hexdigest(),
        "stats": result.stats,
    }
    artifacts.append(_write(out_dir / "trace.json", trace_payload))
    manifest = {
        "schema_version": "orbench-run-manifest-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scenario": "hhcrsp",
        "instance": {
            "instance_id": instance.instance_id,
            "source_path": instance.source_path,
            "source_sha256": instance.source_sha256,
            "num_patients": instance.num_patients,
            "num_clinicians": instance.num_clinicians,
            "num_days": instance.planning_days,
            "required_visits": instance.required_visits,
        },
        "program": program,
        "config": config.to_dict(),
        "stats": result.stats,
        "checker": {
            "schema_version": CHECK_SCHEMA,
            "mode": CHECKER_MODE,
            "official_checker": OFFICIAL_CHECKER,
            "feasible": checker["feasible"],
            "violation_count": checker["violation_count"],
            "violations_by_code": checker["violations_by_code"],
        },
        "metrics": {"schema_version": METRICS_SCHEMA, **metrics},
        "artifacts": artifacts,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "solver": "hhcrsp_lab",
        },
        "evidence_boundary": {
            "official_checker": False,
            "checker_mode": CHECKER_MODE,
            "robust_model": False,
            "note": "Nominal seven-day HHCRSP on a deterministic Huaxi subsample; "
                    "not an official Huaxi Hospital result, robustness budget not active.",
        },
    }
    _write(out_dir / "manifest.json", manifest)
    return {
        "metrics": metrics,
        "checker": checker,
        "trace_points": len(result.trace),
        "elapsed_s": time.perf_counter() - started,
        "executed_iterations": int(result.stats.get("executed_iterations") or 0),
        "stopped_by": result.stats.get("stopped_by"),
        "solver_runtime_s": runtime_s,
    }
