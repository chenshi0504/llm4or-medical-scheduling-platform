"""任务评价：把一次求解的产物折算成四个维度的状态与一条结论。

只做规则判断，不打分。每个维度输出 status + evidence + 一句话说明，
避免用 0-100 的伪精度掩盖"样本不足"或"零改进"。
"""

from __future__ import annotations

from typing import Any

# 平台阈值。写死在这里，README 同步说明；改动即视为口径变更。
RUNTIME_OK_RATIO = 0.80          # 求解耗时 / 墙钟上限
RUNTIME_WATCH_RATIO = 1.00
CONVERGENCE_MIN_IMPROVEMENT = 0.02
STABILITY_MIN_SAMPLES = 3
STABILITY_OK_CV = 0.05
STABILITY_WATCH_CV = 0.15

STATUS_LABEL = {
    "ok": "正常",
    "watch": "需复核",
    "blocked": "不通过",
    "insufficient": "样本不足",
}

VERDICT_LABEL = {
    "pass": "通过",
    "review": "待复核",
    "blocked": "不通过",
}


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def _dimension(name: str, status: str, note: str, **evidence: Any) -> dict[str, Any]:
    return {
        "dimension": name,
        "status": status,
        "label": STATUS_LABEL[status],
        "note": note,
        "evidence": evidence,
    }


def _convergence(trace: list[dict[str, Any]], sense: str = "max") -> dict[str, Any]:
    if not trace:
        return {"degenerate": True, "improvement": None, "t90": None, "plateau_ratio": None}
    best = [float(row["best_objective"]) for row in trace]
    z0, zt = best[0], best[-1]
    span = zt - z0
    degenerate = abs(span) < 1e-9
    better = (lambda value, target: value >= target) if sense == "max" else (lambda value, target: value <= target)
    gain = (lambda value, base: value > base + 1e-9) if sense == "max" else (lambda value, base: value < base - 1e-9)

    t90 = None
    if not degenerate:
        target = z0 + 0.9 * span
        for index, value in enumerate(best):
            if better(value, target):
                t90 = int(trace[index]["iteration"])
                break

    last_gain = 0
    for index in range(1, len(best)):
        if gain(best[index], best[index - 1]):
            last_gain = index
    plateau_ratio = (len(best) - 1 - last_gain) / max(len(best) - 1, 1)

    return {
        "degenerate": degenerate,
        "initial": z0,
        "final": zt,
        "improvement": None if degenerate else (span if sense == "max" else -span) / abs(z0) if z0 else None,
        "t90": t90,
        "plateau_ratio": plateau_ratio,
        "points": len(best),
    }


def evaluate(
    *,
    metrics: dict[str, Any],
    checker: dict[str, Any],
    trace: list[dict[str, Any]],
    params: dict[str, Any],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """返回 {verdict, summary, dimensions, next_actions}。

    history 是同实例、同迭代数的历史运行（含本任务），用于稳定性维度。
    """
    feasible = bool(checker.get("feasible"))
    violations = int(checker.get("violation_count") or 0)
    by_code = checker.get("violations_by_code") or {}
    objective = metrics.get("primary_objective")
    runtime_s = float(metrics.get("runtime_s") or 0.0)
    budget_s = float(params["wall_clock_s"])
    convergence = _convergence(trace)

    dimensions: list[dict[str, Any]] = []

    # 1. 解质量
    served = int(metrics.get("served_count") or 0)
    unassigned = int(metrics.get("unassigned_count") or 0)
    total_patients = served + unassigned
    mandatory_total = int((metrics.get("components") or {}).get("mandatory_total") or 0)
    if mandatory_total:
        coverage_note = f"强制患者覆盖率 {_pct(metrics.get('mandatory_coverage'))}"
    else:
        coverage_note = "本实例没有强制患者"
    if feasible and violations == 0:
        quality = _dimension(
            "解质量", "ok",
            f"独立校验通过；{served}/{total_patients} 名患者已安排，{coverage_note}。",
            objective=objective, assignment_rate=metrics.get("assignment_rate"),
            mandatory_coverage=metrics.get("mandatory_coverage"),
            served_count=served, total_patients=total_patients, violations=0,
        )
    else:
        codes = "、".join(f"{code}×{count}" for code, count in sorted(by_code.items())) or "无分码"
        quality = _dimension(
            "解质量", "blocked",
            f"独立校验未通过，硬约束违反 {violations} 条（{codes}）。"
            f"{served}/{total_patients} 名患者已安排，{coverage_note}。",
            objective=objective, assignment_rate=metrics.get("assignment_rate"),
            mandatory_coverage=metrics.get("mandatory_coverage"),
            served_count=served, total_patients=total_patients,
            violations=violations, violations_by_code=by_code,
        )
    dimensions.append(quality)

    # 2. 运行时间
    dimensions.append(_runtime_dimension(runtime_s, budget_s, params))

    # 3. 收敛性
    if convergence["degenerate"]:
        convergence_status = "watch"
        convergence_note = f"采样窗口内目标值未改进，轨迹全程持平（{convergence['points']} 个采样点）。"
    elif convergence["improvement"] is not None and convergence["improvement"] < CONVERGENCE_MIN_IMPROVEMENT:
        convergence_status = "watch"
        convergence_note = (
            f"相对初始解改善 {_pct(convergence['improvement'])}，低于平台阈值 "
            f"{_pct(CONVERGENCE_MIN_IMPROVEMENT)}。"
        )
    else:
        convergence_status = "ok"
        convergence_note = (
            f"相对初始解改善 {_pct(convergence['improvement'])}，"
            f"第 {convergence['t90']} 轮达到最终改善的 90%。"
        )
    dimensions.append(_dimension(
        "收敛性", convergence_status, convergence_note, **convergence
    ))

    # 4. 稳定性
    values = [float(item["objective"]) for item in history if item.get("objective") is not None]
    stability = _stability_dimension(values)
    dimensions.append(stability)

    # 结论
    if any(item["status"] == "blocked" for item in dimensions):
        verdict = "blocked"
    elif any(item["status"] in {"watch", "insufficient"} for item in dimensions):
        verdict = "review"
    else:
        verdict = "pass"

    summary = _summary(verdict, metrics, checker, convergence, runtime_s, budget_s)
    next_actions = _next_actions(verdict, metrics, checker, convergence, params, len(values))

    return {
        "verdict": verdict,
        "verdict_label": VERDICT_LABEL[verdict],
        "summary": summary,
        "dimensions": dimensions,
        "next_actions": next_actions,
    }




def _stability_dimension(values: list[float]) -> dict[str, Any]:
    if len(values) < STABILITY_MIN_SAMPLES:
        return _dimension(
            "稳定性", "insufficient",
            f"同参数样本 {len(values)} 个，少于 {STABILITY_MIN_SAMPLES} 个，暂不判断离散程度。",
            samples=len(values), mean=values[0] if len(values) == 1 else None,
        )
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    sd = variance ** 0.5
    cv = sd / abs(mean) if mean else None
    if cv is None:
        status, note = "watch", "目标值均值为零，无法计算离散系数。"
    elif cv <= STABILITY_OK_CV:
        status, note = "ok", f"{len(values)} 次同参数运行，离散系数 {_pct(cv)}。"
    elif cv <= STABILITY_WATCH_CV:
        status, note = "watch", f"{len(values)} 次同参数运行，离散系数 {_pct(cv)}，波动偏大。"
    else:
        status, note = "blocked", f"{len(values)} 次同参数运行，离散系数 {_pct(cv)}，结果不稳定。"
    if status == "blocked" and len(values) < 5:
        status = "watch"
        note += " 样本少于 5 个，暂不判为不通过。"
    return _dimension("稳定性", status, note, samples=len(values), mean=mean, sd=sd, cv=cv,
                      min=min(values), max=max(values))

def _runtime_dimension(runtime_s: float, budget_s: float, params: dict[str, Any]) -> dict[str, Any]:
    ratio = runtime_s / budget_s if budget_s else None
    stopped_by = str(params.get("stopped_by") or "")
    if stopped_by == "time_limit":
        runtime_status = "ok"
        ratio = min(runtime_s / budget_s, 1.0) if budget_s else None
        runtime_note = f"按 {budget_s:.0f} 秒预算停止，实际执行 {runtime_s:.1f} 秒。"
    elif ratio is None:
        runtime_status, runtime_note = "watch", "未记录时间预算，无法判断耗时占比。"
    elif ratio > RUNTIME_WATCH_RATIO:
        runtime_status, runtime_note = "blocked", f"求解耗时 {runtime_s:.1f} 秒，已超出墙钟上限 {budget_s:.0f} 秒。"
    elif ratio > RUNTIME_OK_RATIO:
        runtime_status, runtime_note = "watch", f"求解耗时占墙钟上限 {_pct(ratio)}，余量偏小。"
    else:
        runtime_status, runtime_note = "ok", f"求解耗时 {runtime_s:.1f} 秒，占上限 {_pct(ratio)}。"
    return _dimension(
        "运行时间", runtime_status, runtime_note,
        runtime_s=runtime_s, budget_s=budget_s, ratio=ratio,
        iterations=int(params.get("executed_iterations") or 0),
        iterations_per_s=((params.get("executed_iterations") or 0) / runtime_s) if runtime_s > 0 else None,
    )


def _summary(verdict, metrics, checker, convergence, runtime_s, budget_s) -> str:
    objective = metrics.get("primary_objective")
    if verdict == "blocked":
        if not checker.get("feasible"):
            return (
                f"独立校验未通过，硬约束违反 {int(checker.get('violation_count') or 0)} 条。"
                f"当前解不能作为可用排程。"
            )
        return f"求解耗时 {runtime_s:.1f} 秒，超出 {budget_s:.0f} 秒上限。"
    if verdict == "review":
        if convergence["degenerate"]:
            return (
                f"解可行、硬约束 0，但采样窗口内目标无改进；"
                f"当前结果只能说明这个配置没有找到更好解。"
            )
        return (
            f"解可行、硬约束 0，目标 {_num(objective)}；"
            f"改善 {_pct(convergence['improvement'])}，样本或耗时余量仍需复核。"
        )
    return (
        f"解可行、硬约束 0，目标 {_num(objective)}，"
        f"相对初始解改善 {_pct(convergence['improvement'])}，耗时 {runtime_s:.1f} 秒。"
    )


def _next_actions(verdict, metrics, checker, convergence, params, samples) -> list[dict]:
    """返回 0-2 条可执行建议；patch 直接用于新建任务。"""
    actions: list[dict] = []
    seed = int(params["seed"])
    budget = float(params["wall_clock_s"])

    if not checker.get("feasible"):
        actions.append({
            "label": f"时间预算加到 {int(min(budget * 2, 300))} 秒后重跑",
            "reason": "强制患者未安排，可能是搜索时间不足；先排除预算因素。",
            "patch": {"seed": seed, "wall_clock_s": min(budget * 2, 300)},
        })
    elif convergence["degenerate"]:
        actions.append({
            "label": "换种子重跑",
            "reason": "轨迹持平可能是该种子的搜索路径问题，也可能是实例本身已到瓶颈。",
            "patch": {"seed": seed + 101, "wall_clock_s": budget},
        })
    if samples < STABILITY_MIN_SAMPLES:
        actions.append({
            "label": "补一次同参数样本",
            "reason": f"稳定性需要至少 {STABILITY_MIN_SAMPLES} 个同参数样本，当前 {samples} 个。",
            "patch": {"seed": seed + 202, "wall_clock_s": budget},
        })
    elif verdict == "pass":
        actions.append({
            "label": "换种子复核",
            "reason": "结论通过，换种子复跑可确认不是单次偶然。",
            "patch": {"seed": seed + 101, "wall_clock_s": budget},
        })
    if metrics.get("runtime_s") and metrics["runtime_s"] > RUNTIME_OK_RATIO * budget:
        actions.append({
            "label": f"时间预算加到 {int(min(budget * 2, 300))} 秒",
            "reason": "当前耗时接近上限，放宽后可以减少被截断的风险。",
            "patch": {"seed": seed, "wall_clock_s": min(budget * 2, 300)},
        })
    return actions[:2]


def evaluate_failure(*, params: dict[str, Any], reason: str, detail: str = "") -> dict[str, Any]:
    """运行失败（超时、进程错误）时的评价：不伪装成一次有效求解。"""
    budget = float(params["wall_clock_s"])
    dimensions = [
        _dimension("解质量", "blocked", "本次运行没有产出可校验的解。", objective=None),
        _dimension("运行时间", "blocked", detail or reason, runtime_s=None, budget_s=budget, ratio=None),
        _dimension("收敛性", "blocked", "没有轨迹，无法判断收敛。", degenerate=True, points=0),
        _dimension("稳定性", "insufficient", "失败运行不计入稳定性样本。", samples=0),
    ]
    actions = [{
        "label": f"放宽墙钟到 {int(min(budget * 2, 600))} 秒",
        "reason": "先排除预算不足这一因素。",
        "patch": {"seed": int(params["seed"]), "wall_clock_s": min(budget * 2, 300)},
    }]
    return {
        "verdict": "blocked",
        "verdict_label": VERDICT_LABEL["blocked"],
        "summary": reason,
        "dimensions": dimensions,
        "next_actions": actions,
    }


def evaluate_hhcrsp(
    *,
    metrics: dict[str, Any],
    checker: dict[str, Any],
    trace: list[dict[str, Any]],
    params: dict[str, Any],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """居家护理场景的四维评价：口径与手术排程一致，证据换成访视与路线。"""
    feasible = bool(checker.get("feasible"))
    violations = int(checker.get("violation_count") or 0)
    by_code = checker.get("violations_by_code") or {}
    objective = metrics.get("primary_objective")
    runtime_s = float(metrics.get("runtime_s") or 0.0)
    budget_s = float(params["wall_clock_s"])
    convergence = _convergence(trace, sense="min")

    required = int(metrics.get("required_visits") or 0)
    served = int(metrics.get("served_visits") or 0)
    coverage = metrics.get("coverage_rate")
    active_routes = int(metrics.get("active_routes") or 0)

    dimensions: list[dict[str, Any]] = []
    if feasible:
        quality = _dimension(
            "解质量", "ok",
            f"独立校验通过；{served}/{required} 次访视全部落实，"
            f"启用 {active_routes} 条（护理员, 日）路线。",
            objective=objective, required_visits=required, served_visits=served,
            coverage_rate=coverage, active_routes=active_routes, violations=0,
        )
    else:
        codes = "、".join(f"{code}×{count}" for code, count in sorted(by_code.items())) or "无分码"
        quality = _dimension(
            "解质量", "blocked",
            f"独立校验未通过，约束违反 {violations} 条（{codes}）；"
            f"访视落实 {served}/{required}。",
            objective=objective, required_visits=required, served_visits=served,
            coverage_rate=coverage, active_routes=active_routes,
            violations=violations, violations_by_code=by_code,
        )
    dimensions.append(quality)
    dimensions.append(_runtime_dimension(runtime_s, budget_s, params))

    if convergence["degenerate"]:
        convergence_status = "watch"
        convergence_note = f"采样窗口内目标值未改进，轨迹全程持平（{convergence['points']} 个采样点）。"
    elif convergence["improvement"] is not None and convergence["improvement"] < CONVERGENCE_MIN_IMPROVEMENT:
        convergence_status = "watch"
        convergence_note = (f"相对初始解改善 {_pct(convergence['improvement'])}，低于平台阈值 "
                            f"{_pct(CONVERGENCE_MIN_IMPROVEMENT)}。")
    else:
        convergence_status = "ok"
        convergence_note = (f"相对初始解改善 {_pct(convergence['improvement'])}，"
                            f"第 {convergence['t90']} 轮达到最终改善的 90%。")
    dimensions.append(_dimension("收敛性", convergence_status, convergence_note, **convergence))

    values = [float(item["objective"]) for item in history if item.get("objective") is not None]
    dimensions.append(_stability_dimension(values))

    if any(item["status"] == "blocked" for item in dimensions):
        verdict = "blocked"
    elif any(item["status"] in {"watch", "insufficient"} for item in dimensions):
        verdict = "review"
    else:
        verdict = "pass"

    if verdict == "blocked":
        if not feasible:
            summary = (f"独立校验未通过，约束违反 {violations} 条；"
                       f"访视落实 {served}/{required}，当前解不能作为可执行班表。")
        else:
            summary = f"求解耗时 {runtime_s:.1f} 秒，超出 {budget_s:.0f} 秒上限。"
    elif verdict == "review":
        summary = ("解可行、约束 0 违反，但目标在采样窗口内没有改进；"
                   "当前结果只能说明这个配置没有找到更好解。"
                   if convergence["degenerate"] else
                   f"解可行、约束 0 违反，目标 {_num(objective)}（越小越好）；"
                   f"改善 {_pct(convergence['improvement'])}，样本或耗时余量仍需复核。")
    else:
        summary = (f"解可行、约束 0 违反，{served}/{required} 次访视全部落实，"
                   f"目标 {_num(objective)}，相对初始解改善 {_pct(convergence['improvement'])}。")

    return {
        "verdict": verdict,
        "verdict_label": VERDICT_LABEL[verdict],
        "summary": summary,
        "dimensions": dimensions,
        "next_actions": _next_actions_hhcrsp(verdict, metrics, checker, convergence, params, len(values)),
    }


def _next_actions_hhcrsp(verdict, metrics, checker, convergence, params, samples) -> list[dict]:
    actions: list[dict] = []
    seed = int(params["seed"])
    budget = float(params["wall_clock_s"])
    if not checker.get("feasible"):
        actions.append({
            "label": f"时间预算加到 {int(min(budget * 2, 300))} 秒后重跑",
            "reason": "仍有访视未落实或时间窗冲突，先排除搜索预算不足。",
            "patch": {"seed": seed, "wall_clock_s": min(budget * 2, 300)},
        })
    elif convergence["degenerate"]:
        actions.append({
            "label": "换种子重跑",
            "reason": "轨迹持平可能是该种子的搜索路径问题，也可能是实例已到瓶颈。",
            "patch": {"seed": seed + 101, "wall_clock_s": budget},
        })
    if samples < STABILITY_MIN_SAMPLES:
        actions.append({
            "label": "补一次同参数样本",
            "reason": f"稳定性需要至少 {STABILITY_MIN_SAMPLES} 个同参数样本，当前 {samples} 个。",
            "patch": {"seed": seed + 202, "wall_clock_s": budget},
        })
    elif verdict == "pass":
        actions.append({
            "label": "换种子复核",
            "reason": "结论通过，换种子复跑可确认不是单次偶然。",
            "patch": {"seed": seed + 101, "wall_clock_s": budget},
        })
    return actions[:2]
