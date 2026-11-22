"""命令行窗口里的求解进程：实时打印迭代过程，结束后等待手动关闭。

由平台用 `cmd /k` 拉起，单独占用一个 CMD 窗口。所有产物写进 --run-dir，
界面通过 progress.json / status.json 读取，不依赖窗口是否可见。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
for entry in (str(BASE), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from runner import run_job  # noqa: E402
from runner_hhcrsp import run_job as run_hhcrsp_job  # noqa: E402


def _configure() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _atomic_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    _configure()
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--scenario", default="surgical", choices=("surgical", "hhcrsp"))
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--cap", type=int, required=True)
    parser.add_argument("--destroy", required=True)
    parser.add_argument("--repair", required=True)
    parser.add_argument("--fraction", type=float, required=True)
    parser.add_argument("--acceptance", required=True)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    progress_path = run_dir / "progress.json"
    status_path = run_dir / "status.json"
    params = {"seconds": args.seconds, "seed": args.seed, "iteration_cap": args.cap}
    program = {
        "destroy_operators": [name for name in args.destroy.split(",") if name],
        "repair_operators": [name for name in args.repair.split(",") if name],
        "destroy_fraction": args.fraction,
        "acceptance": args.acceptance,
    }

    print("=" * 72)
    scenario_name = "手术排程" if args.scenario == "surgical" else "居家护理路径"
    print(f"任务 #{args.task_id}   [{scenario_name}] {Path(args.instance).stem}")
    print(f"算子  {'、'.join(program['destroy_operators'])} → "
          f"{'、'.join(program['repair_operators'])}   破坏比例 {args.fraction:.2f}   接受 {args.acceptance}")
    print(f"预算  {args.seconds:.0f} 秒（迭代上限 {args.cap}）   种子 {args.seed}")
    print("=" * 72)
    print("  迭代      当前最优          本轮           接受  算子                     用时")
    sys.stdout.flush()

    started = time.perf_counter()
    last_print = [0.0]
    last_progress = [0.0]

    def _fmt(value) -> str:
        return "—" if value is None else f"{value:,.1f}"

    def on_iteration(row: dict) -> None:
        now = time.perf_counter()
        if now - last_print[0] >= 0.35:
            last_print[0] = now
            mark = "是" if row["accepted"] else "否"
            print(f"{row['iteration']:>7}  {_fmt(row.get('best_objective')):>14}  "
                  f"{_fmt(row.get('current_objective')):>13}"
                  f"   {mark}   {row['destroy_operator'][:11]:<11}→{row['repair_operator'][:12]:<12}"
                  f" {row['elapsed_s']:>6.1f}s")
            sys.stdout.flush()
        if now - last_progress[0] >= 0.5:
            last_progress[0] = now
            _atomic_json(progress_path, {
                "state": "running",
                "task_id": args.task_id,
                "iteration": row["iteration"],
                "best_objective": row["best_objective"],
                "current_objective": row["current_objective"],
                "accepted": row["accepted"],
                "destroy_operator": row["destroy_operator"],
                "repair_operator": row["repair_operator"],
                "elapsed_s": round(row["elapsed_s"], 2),
            })

    runner = run_job if args.scenario == "surgical" else run_hhcrsp_job
    try:
        _atomic_json(progress_path, {"state": "running", "task_id": args.task_id, "iteration": 0})
        summary = runner(
            instance_path=Path(args.instance),
            out_dir=run_dir,
            params=params,
            program=program,
            on_iteration=on_iteration,
        )
    except Exception as exc:  # 失败也要留下状态，界面才不会一直转
        _atomic_json(status_path, {"state": "failed", "error": f"{type(exc).__name__}: {exc}"})
        print("\n求解失败：")
        traceback.print_exc()
        _hold()
        return 1

    elapsed = time.perf_counter() - started
    metrics = summary["metrics"]
    checker = summary["checker"]
    print("-" * 72)
    print(f"完成  {summary['executed_iterations']} 轮 / {elapsed:.1f} 秒（{summary['stopped_by']}）")
    print(f"目标  {_fmt(metrics['primary_objective'])}")
    print(f"校验  {'可行' if checker['feasible'] else '不可行'}   违反 {checker['violation_count']} 条")
    print(f"产物  {run_dir}")
    print("-" * 72)
    elapsed_s = round(elapsed, 2)
    _atomic_json(progress_path, {
        "state": "done", "task_id": args.task_id,
        "iteration": summary["executed_iterations"],
        "best_objective": metrics["primary_objective"],
        "elapsed_s": elapsed_s,
    })
    _atomic_json(status_path, {
        "state": "done",
        "elapsed_s": elapsed_s,
        "executed_iterations": summary["executed_iterations"],
        "stopped_by": summary["stopped_by"],
    })
    _hold()
    return 0


def _hold() -> None:
    print("\n窗口保持打开，看完后手动关闭。")
    try:
        input("按回车关闭…")
    except (EOFError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    raise SystemExit(main())
