"""DeepSeek 决策层：带工具调用的代理循环，而不是一次问答。

与 DSH 的关系：沿用它的三段式——先取事实、再调工具、最后交结构化程序；
事件类型（状态 / 思考 / 工具 / 程序 / 异常 / 结论 / 复盘）与轨迹视图的
角色色块一一对应。这里不 import DSH 的 Node 包，而是按它公开的交互契约
在本平台重写，因此可以离线运行、可被 checker 复核。

两个场景共用同一台机器，差别只在系统提示、工具集与校验规则。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from model_usage import collect_calls, combine, observe_request
from audit import emit
from ptc import run_code
import codex_relay
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 模型目录：与 https://api.deepseek.com/models 返回的两个条目一致
# ---------------------------------------------------------------------------

MODELS: dict[str, dict[str, Any]] = {
    "deepseek-flash": {
        "label": "Flash",
        "name": "DeepSeek-V4.1-Flash",
        "context_window": 1048576,
        "efforts": ["low", "high", "max"],
        "default_effort": "high",
        "note": "响应快，适合反复试配置",
    },
    "deepseek-v4-pro": {
        "label": "Pro",
        "name": "DeepSeek-V4-Pro",
        "context_window": 1048576,
        "efforts": ["low", "high", "max"],
        "default_effort": "high",
        "note": "推理更稳，适合难实例与复盘",
    },
}
for _id, _label, _context in (("gpt-6.2-astra", "6.2 Astra", 1050000), ("gpt-6.1-sol", "6.1 Sol", 272000)):
    MODELS[_id] = {"label": _label, "name": f"GPT-{_label}", "provider": "codex-relay",
                   "context_window": _context, "efforts": ["low", "medium", "high", "xhigh"],
                   "default_effort": "medium"}
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_EFFORT = "high"
EFFORT_LABEL = {"low": "低", "high": "高", "max": "最高"}

DESTROY_WHITELIST = (
    "random_removal", "worst_due", "mandatory_cluster", "surgeon_conflict",
    "or_conflict", "emergency_neighbourhood", "cross_day", "high_reward_optional",
)
REPAIR_WHITELIST = (
    "greedy_earliest", "min_incremental_cost", "regret_k", "priority_weighted",
    "stability_preserving", "resource_joint", "mandatory_first",
)
GENERAL_REPAIRS = ("greedy_earliest", "min_incremental_cost", "regret_k", "priority_weighted")
ACCEPTANCE = ("sa", "hill_climb")
FRACTION_RANGE = (0.05, 0.40)

DEFAULT_PROGRAM = {
    "destroy_operators": ["worst_due"],
    "repair_operators": ["regret_k"],
    "destroy_fraction": 0.15,
    "acceptance": "sa",
}

HHCRSP_DESTROY = ("random_visit", "worst_detour", "related_visit", "route_day", "clinician_day")
HHCRSP_REPAIR = ("greedy", "regret2")
HHCRSP_ACCEPTANCE = ("sa", "hill_climb")
HHCRSP_FRACTION = (0.10, 0.40)

HHCRSP_DEFAULT = {
    "destroy_operators": ["worst_detour"],
    "repair_operators": ["greedy"],
    "destroy_fraction": 0.20,
    "acceptance": "sa",
}


def model_catalog() -> list[dict[str, Any]]:
    return [{"id": key, **value} for key, value in MODELS.items()]


def resolve_model(model: str | None) -> str:
    return model if model in MODELS else DEFAULT_MODEL


def resolve_effort(model: str, effort: str | None) -> str:
    allowed = MODELS[model]["efforts"]
    return effort if effort in allowed else MODELS[model]["default_effort"]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _credentials() -> tuple[str, str]:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    if not key:
        cred = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
        try:
            text = open(cred, encoding="utf-8").read()
            match = re.search(r"DEEPSEEK_API_KEY:\s*(\S+)", text)
            if match:
                key = match.group(1).strip().strip("'\"")
        except OSError:
            pass
    return key, base


def _chat(
    messages: list[dict[str, Any]],
    *,
    model: str,
    effort: str,
    tools: list[dict[str, Any]] | None = None,
    timeout: float = 60.0,
    max_tokens: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if MODELS.get(model, {}).get("provider") == "codex-relay":
        return codex_relay.chat(messages, model, effort, tools, timeout, max_tokens)
    key, base = _credentials()
    if not key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "stream": False,
        "reasoning_effort": effort,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if max_tokens:
        payload["max_tokens"] = max_tokens
    request = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    emit("model.request", payload)
    try:
        with observe_request() as call:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            usage = body.get("usage") or {}
            hit = usage.get("prompt_cache_hit_tokens")
            if hit is None:
                hit = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            call.update(prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                        reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                        prompt_cache_hit_tokens=hit, prompt_cache_miss_tokens=usage.get("prompt_cache_miss_tokens"))
        emit("model.response", {"body": body, "usage": call}, "llm")
    except Exception as exc:
        emit("model.error", {"error": str(exc), "usage": call})
        raise
    message = body["choices"][0]["message"]
    return message, {
        **call,
        "model": body.get("model") or model,
        "effort": effort,
        "latency_s": round(call["request_seconds"], 2),
        "finish_reason": (body["choices"][0] or {}).get("finish_reason"),
    }


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _strip_json(text: str) -> str:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


def _short(text: str, limit: int = 420) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------
# 场景规格：工具、画像、校验
# ---------------------------------------------------------------------------

_PROGRAM_ARGS = {
    "type": "object",
    "properties": {
        "destroy_operators": {"type": "array", "items": {"type": "string"}},
        "repair_operators": {"type": "array", "items": {"type": "string"}},
        "destroy_fraction": {"type": "number"},
        "acceptance": {"type": "string"},
    },
    "required": ["destroy_operators", "repair_operators", "destroy_fraction", "acceptance"],
    "additionalProperties": False,
}


def _tools_for(scenario: str) -> list[dict[str, Any]]:
    if scenario == "hhcrsp":
        inspect_desc = "读取算例画像：患者数、访视次数、护理员数、技能分布、时间窗宽度、日均工时上限。"
        pressure_desc = "估计班表压力：每周可用护理员日、每次访视平均可分到的分钟、访视密度。"
    else:
        inspect_desc = "读取实例的容量与需求画像：患者数、强制患者数、手术室日容量、总手术时长、负荷率。"
        pressure_desc = "估计容量压力：每间手术室每天平均可用分钟、需求分钟、强制患者占比、一次破坏涉及多少患者。"
    return [
        {"type": "function", "function": {
            "name": "inspect_instance", "description": inspect_desc,
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
        {"type": "function", "function": {
            "name": "estimate_pressure", "description": pressure_desc,
            "parameters": {
                "type": "object",
                "properties": {"destroy_fraction": {"type": "number", "minimum": 0.05, "maximum": 0.4}},
                "required": ["destroy_fraction"], "additionalProperties": False}}},
        {"type": "function", "function": {
            "name": "validate_program",
            "description": "提交候选程序做白名单与范围校验；返回是否通过、错误列表与提示。",
            "parameters": _PROGRAM_ARGS}},
    ]


def _load_json(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def _inspect_surgical(meta: dict[str, Any]) -> dict[str, Any]:
    raw = _load_json(meta.get("path"))
    patients = raw.get("patients") or []
    hospitals = raw.get("hospitals") or []
    capacity = 0.0
    for hospital in hospitals:
        for or_item in hospital.get("ORs") or []:
            capacity += sum(float(day.get("regular_time") or 0)
                            for day in (or_item.get("daily") or {}).values())
    demand = sum(float(p.get("base_surgical_time") or 0) for p in patients)
    mandatory = sum(1 for p in patients if p.get("is_mandatory"))
    return {
        "patients": len(patients),
        "mandatory": mandatory,
        "days": raw.get("num_days") or meta.get("days"),
        "hospitals": len(hospitals) or meta.get("hospitals"),
        "ors": sum(len(h.get("ORs") or []) for h in hospitals),
        "capacity_minutes": round(capacity, 1),
        "demand_minutes": round(demand, 1),
        "load_ratio": round(demand / capacity, 4) if capacity else None,
        "mandatory_share": round(mandatory / len(patients), 4) if patients else None,
    }


def _inspect_hhcrsp(meta: dict[str, Any]) -> dict[str, Any]:
    raw = _load_json(meta.get("path"))
    patients = raw.get("patients") or []
    clinicians = raw.get("clinicians") or []
    visits = sum(int(p.get("visit_frequency") or 0) for p in patients)
    skills: dict[str, int] = {}
    for patient in patients:
        for skill in patient.get("required_skills") or []:
            skills[skill] = skills.get(skill, 0) + 1
    coverage: dict[str, int] = {}
    for clinician in clinicians:
        for skill in clinician.get("skills") or []:
            coverage[skill] = coverage.get(skill, 0) + 1
    window = ((patients[0].get("time_window") if patients else [0, 0]) or [0, 0])
    work_limit = float((clinicians[0] or {}).get("daily_work_limit") or 480) if clinicians else 480.0
    return {
        "patients": len(patients),
        "clinicians": len(clinicians),
        "visits": visits,
        "days": raw.get("planning_days") or 7,
        "window_width": round(float(window[1]) - float(window[0]), 1),
        "work_limit": work_limit,
        "skills_required": skills,
        "skills_available": coverage,
    }


def _pressure_surgical(meta: dict[str, Any], fraction: float) -> dict[str, Any]:
    profile = _inspect_surgical(meta)
    capacity = profile.get("capacity_minutes") or 0
    demand = profile.get("demand_minutes") or 0
    days = max(1, int(profile.get("days") or 1))
    ors = max(1, int(profile.get("ors") or 1))
    per_or_day = capacity / (days * ors) if capacity else None
    return {
        "per_or_day_minutes": round(per_or_day, 1) if per_or_day else None,
        "demand_per_patient": round(demand / profile["patients"], 1) if profile.get("patients") else None,
        "patients_removed_per_iteration": max(1, round((profile.get("patients") or 0) * fraction)),
        "slack_minutes": round(capacity - demand, 1),
        "load_ratio": profile.get("load_ratio"),
        "mandatory": profile.get("mandatory"),
    }


def _pressure_hhcrsp(meta: dict[str, Any], fraction: float) -> dict[str, Any]:
    profile = _inspect_hhcrsp(meta)
    raw = _load_json(meta.get("path"))
    clinicians = raw.get("clinicians") or []
    clinician_days = sum(sum(1 for value in (c.get("availability") or {}).values() if value)
                         for c in clinicians)
    capacity = clinician_days * float(profile.get("work_limit") or 480)
    visits = max(1, int(profile.get("visits") or 1))
    patients = raw.get("patients") or []
    service = sum(float((p.get("service") or {}).get("mean") or 0) for p in patients)
    service = service * (visits / max(1, len(patients)))
    return {
        "clinician_days": clinician_days,
        "capacity_minutes": round(capacity, 1),
        "service_minutes": round(service, 1),
        "minutes_per_visit": round(capacity / visits, 1),
        "visits_per_clinician_day": round(visits / clinician_days, 2) if clinician_days else None,
        "visits_removed_per_iteration": max(1, round(visits * fraction)),
        "window_width": profile.get("window_width"),
    }


def _validate(program: dict[str, Any], meta: dict[str, Any], scenario: str) -> list[str]:
    errors: list[str] = []
    destroys = program.get("destroy_operators") or []
    repairs = program.get("repair_operators") or []
    if scenario == "hhcrsp":
        allowed_destroy, allowed_repair = HHCRSP_DESTROY, HHCRSP_REPAIR
        allowed_acceptance, fraction_range = HHCRSP_ACCEPTANCE, HHCRSP_FRACTION
    else:
        allowed_destroy, allowed_repair = DESTROY_WHITELIST, REPAIR_WHITELIST
        allowed_acceptance, fraction_range = ACCEPTANCE, FRACTION_RANGE
    if not 1 <= len(destroys) <= 2:
        errors.append("destroy_operators 需 1–2 个")
    if not 1 <= len(repairs) <= 2:
        errors.append("repair_operators 需 1–2 个")
    bad_d = [name for name in destroys if name not in allowed_destroy]
    bad_r = [name for name in repairs if name not in allowed_repair]
    if bad_d:
        errors.append(f"destroy 不在白名单：{bad_d}")
    if bad_r:
        errors.append(f"repair 不在白名单：{bad_r}")
    if scenario == "surgical":
        if "mandatory_first" in repairs and not any(name in GENERAL_REPAIRS for name in repairs):
            errors.append("选了 mandatory_first 时必须搭配一个通用 repair")
        if int(meta.get("mandatory") or 0) == 0 and (
                "mandatory_first" in repairs or "mandatory_cluster" in destroys):
            errors.append("本实例没有强制患者，不该使用 mandatory 专用算子")
    fraction = program.get("destroy_fraction")
    if not isinstance(fraction, (int, float)) or not fraction_range[0] <= float(fraction) <= fraction_range[1]:
        errors.append(f"destroy_fraction 越界：{fraction!r}")
    if program.get("acceptance") not in allowed_acceptance:
        errors.append(f"acceptance 非法：{program.get('acceptance')!r}")
    return errors


def _execute_tool(name: str, args: dict[str, Any], meta: dict[str, Any], scenario: str) -> dict[str, Any]:
    emit("tool.call", {"name": name, "arguments": args}, "llm")
    try:
        result = _execute_registered_tool(name, args, meta, scenario)
    except (TypeError, ValueError, OverflowError, RuntimeError) as exc:
        result = {"ok": False, "error": str(exc)}
    emit("tool.result", {"name": name, "result": result}, "tool")
    return result


def _execute_registered_tool(name: str, args: dict[str, Any], meta: dict[str, Any], scenario: str) -> dict[str, Any]:
    if name == "run_code":
        return run_code(args.get("code"), lambda tool, params: _execute_tool(tool, params, meta, scenario))
    if name == "inspect_instance":
        return _inspect_surgical(meta) if scenario == "surgical" else _inspect_hhcrsp(meta)
    if name == "estimate_pressure":
        fraction = float(args.get("destroy_fraction") or (0.2 if scenario == "hhcrsp" else 0.15))
        return (_pressure_surgical(meta, fraction) if scenario == "surgical"
                else _pressure_hhcrsp(meta, fraction))
    if name == "validate_program":
        program = {
            "destroy_operators": _as_list(args.get("destroy_operators")),
            "repair_operators": _as_list(args.get("repair_operators")),
            "destroy_fraction": args.get("destroy_fraction"),
            "acceptance": args.get("acceptance"),
        }
        errors = _validate(program, meta, scenario)
        return {"ok": not errors, "errors": errors}
    return {"error": f"未知工具：{name}"}


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


def _system_prompt(scenario: str) -> str:
    if scenario == "hhcrsp":
        return (
            "你是居家护理排班的算法配置助手。先用工具核实实例容量与班表压力，再给出受限程序。\n"
            "只使用提供的工具，不要臆造字段；工具返回的数字以工具为准。\n"
            "最终回答必须是一个 JSON 对象，不要 Markdown、不要解释段落。\n"
            "JSON 键：reasoning（80–140 字，说明判断依据，不要复述字段）、"
            "destroy_operators、repair_operators、destroy_fraction、acceptance、"
            "expected_effect（一句话）、confidence（low/medium/high）。\n"
            f"约束：destroy 从 {', '.join(HHCRSP_DESTROY)} 里选 1–2 个；"
            f"repair 从 {', '.join(HHCRSP_REPAIR)} 里选 1–2 个；destroy_fraction 在 0.10–0.40；"
            "acceptance 只能是 sa 或 hill_climb。只能用这些名字，不要自造。"
        )
    return (
        "你是手术室排程的算法配置助手。先用工具核实实例容量与需求，再给出受限程序。\n"
        "只使用提供的工具，不要臆造字段；工具返回的数字以工具为准。\n"
        "最终回答必须是一个 JSON 对象，不要 Markdown、不要解释段落。\n"
        "JSON 键：reasoning（80–140 字，说明判断依据，不要复述字段）、"
        "destroy_operators、repair_operators、destroy_fraction、acceptance、"
        "expected_effect（一句话）、confidence（low/medium/high）。\n"
        f"约束：destroy 从 {', '.join(DESTROY_WHITELIST)} 里选 1–2 个；"
        f"repair 从 {', '.join(REPAIR_WHITELIST)} 里选 1–2 个；"
        "destroy_fraction 在 0.05–0.40；acceptance 只能是 sa 或 hill_climb；"
        "若选 mandatory_first，必须同时选一个通用 repair；强制患者为 0 时不要用 mandatory 专用算子。"
        "只能用这些名字，不要自造。"
    )


def _user_prompt(meta: dict[str, Any], history: list[dict[str, Any]], scenario: str) -> str:
    hist = "无历史运行"
    if history:
        rows = []
        for item in history[-3:]:
            flag = "可行" if item.get("feasible") else "不可行"
            value = item.get("objective")
            rows.append(f"seed {item.get('seed')}：目标 {value:.1f}（{flag}）"
                        if isinstance(value, (int, float)) else
                        f"seed {item.get('seed')}：无目标值（{flag}）")
        hist = "；".join(rows)
    if scenario == "hhcrsp":
        head = (f"实例 {meta['instance_id']}：{meta['patients']} 名患者 / {meta.get('visits')} 次访视，"
                f"{meta.get('clinicians')} 名护理员，{meta.get('days')} 天。")
    else:
        head = (f"实例 {meta['instance_id']}：{meta['patients']} 名患者，{meta['days']} 天，"
                f"{meta.get('hospitals')} 家医院 × {meta.get('ors_per_hospital')} 个手术室，"
                f"强制患者 {meta.get('mandatory')} 名。")
    return (f"{head}\n历史：{hist}。\n"
            "请先调用 inspect_instance 与 estimate_pressure 了解容量，再用 validate_program 校验候选程序，"
            "通过后输出最终 JSON。若校验报错，按错误修正后再次校验。")


# ---------------------------------------------------------------------------
# 代理循环
# ---------------------------------------------------------------------------


def _fallback_copy(program: dict[str, Any]) -> dict[str, Any]:
    return {key: (list(value) if isinstance(value, list) else value)
            for key, value in program.items()}


def _run_agent(
    scenario: str,
    meta: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    model: str,
    effort: str,
    timeout: float,
    mode: str = "ptc",
) -> dict[str, Any]:
    if mode != "ptc":
        raise ValueError("Only PTC / Code Mode is supported")
    sdk = ("\n本次使用 PTC (Code Mode)。只用 run_code 编写 JavaScript 异步函数体调用工具；"
           "使用 await tools.inspect_instance({})、await tools.estimate_pressure({destroy_fraction:0.2})、"
           "await tools.validate_program(program)。允许变量、条件、循环、Promise.all 和 console.log。"
           "代码通过 return 返回观察与校验结果。无文件、网络、进程或模块导入。"
           "工具编排代码与最终求解器代码是两种不同产物。工具结果出来后再输出最终决策 JSON。")
    tools = [{"type": "function", "function": {
        "name": "run_code", "description": "PTC：执行 JavaScript 编排已注册工具，返回日志与值。",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"}, "description": {"type": "string"}},
            "required": ["code", "description"], "additionalProperties": False}}}]
    fallback_program = HHCRSP_DEFAULT if scenario == "hhcrsp" else DEFAULT_PROGRAM
    head = {
        "mode": "status",
        "title": "读取实例",
        "text": (f"{meta['instance_id']} · {meta.get('patients')} 名患者 · "
                 f"{meta.get('visits')} 次访视 · {meta.get('clinicians')} 名护理员 · {meta.get('days')} 天"
                 if scenario == "hhcrsp" else
                 f"{meta['instance_id']} · {meta.get('patients')} 名患者 · {meta.get('days')} 天 · "
                 f"{meta.get('hospitals')} 家医院 × {meta.get('ors_per_hospital')} 个手术室"),
    }
    steps: list[dict[str, Any]] = [head]
    model_seen = model
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt(scenario) + sdk},
        {"role": "user", "content": _user_prompt(meta, history, scenario)},
    ]
    content = ""
    rounds = 0
    thinking_shown = False
    seen_calls: set[str] = set()
    ptc_used = False

    try:
        while rounds < 3:
            rounds += 1
            message, info = _chat(messages, model=model, effort=effort, tools=tools, timeout=timeout)
            model_seen = info["model"]
            if message.get("reasoning_content") and not thinking_shown:
                thinking_shown = True
                steps.append({"mode": "thinking", "title": "思考",
                              "text": _short(str(message["reasoning_content"]), 480)})
            calls = message.get("tool_calls") or []
            if not calls:
                content = message.get("content") or ""
                break
            messages.append({"role": "assistant", "content": message.get("content") or "",
                             "tool_calls": calls, **({"_response_items": message["_response_items"]}
                                                     if "_response_items" in message else {})})
            for call in calls:
                name = call["function"]["name"]
                ptc_used = ptc_used or name == "run_code"
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                signature = name + json.dumps(args, sort_keys=True, ensure_ascii=False)
                if signature in seen_calls:
                    result = {"ok": False, "errors": ["与上一次调用参数相同，结果不变；请改参数或直接输出 JSON"]}
                else:
                    seen_calls.add(signature)
                    result = (_execute_tool(name, args, meta, scenario)
                              if name in {t["function"]["name"] for t in tools}
                              else {"ok": False, "error": "当前模式没有注册此工具"})
                steps.append({
                    "mode": "tool",
                    "title": name,
                    "text": f"入参 {json.dumps(args, ensure_ascii=False)} → "
                            f"{json.dumps(result, ensure_ascii=False)}",
                })
                messages.append({"role": "tool", "tool_call_id": call.get("id"),
                                 "content": json.dumps(result, ensure_ascii=False)})
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as exc:
        detail = getattr(exc, "reason", None) or str(exc)
        steps.append({"mode": "error", "title": "在线调用失败",
                      "text": f"{detail}。已退回本地规则。"})
        steps.append({"mode": "json", "title": "回退程序",
                      "text": json.dumps(fallback_program, ensure_ascii=False, indent=2)})
        return {"steps": steps, "program": _fallback_copy(fallback_program), "fallback": True,
                "model": model_seen, "effort": effort, "latency_s": None,
                "error": str(detail), "rounds": rounds}

    if not content.strip():
        # 工具轮次用完还没给最终答案：关掉工具，强制收口。
        try:
            messages.append({"role": "user", "content":
                             "工具轮次已用完。现在只输出最终 JSON（不要调用工具、不要解释）。"})
            message, info = _chat(messages, model=model, effort=effort, timeout=timeout)
            model_seen = info["model"]
            content = message.get("content") or ""
        except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as exc:
            steps.append({"mode": "error", "title": "收口失败", "text": str(exc)})

    program, parse_error = _parse_program(content, fallback_program)
    if not ptc_used:
        parse_error = "模型未调用 run_code，PTC 未执行；本次决策使用本地回退。"
    errors = _validate(program, meta, scenario)
    if parse_error:
        steps.append({"mode": "error", "title": "输出不可解析", "text": parse_error})

    if errors and not parse_error:
        steps.append({"mode": "error", "title": "本地校验未通过",
                      "text": "；".join(errors) + "。把错误回传模型重试一次。"})
        try:
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": (
                "本地校验未通过：" + "；".join(errors) + "。请只输出修正后的 JSON，键与之前相同。")})
            message, info = _chat(messages, model=model, effort=effort, timeout=timeout)
            model_seen = info["model"]
            content = message.get("content") or ""
            program, parse_error = _parse_program(content, fallback_program)
            errors = _validate(program, meta, scenario)
            steps.append({"mode": "tool", "title": "重新校验",
                          "text": "通过" if not errors else "；".join(errors)})
        except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as exc:
            steps.append({"mode": "error", "title": "重试失败", "text": str(exc)})

    if errors or parse_error:
        steps.append({"mode": "json", "title": "回退程序",
                      "text": json.dumps(fallback_program, ensure_ascii=False, indent=2)})
        return {"steps": steps, "program": _fallback_copy(fallback_program), "fallback": True,
                "model": model_seen, "effort": effort, "latency_s": None,
                "error": parse_error or "；".join(errors), "rounds": rounds}

    raw = _last_raw_json(content) or {}
    reasoning = str(raw.get("reasoning") or "").strip()
    if reasoning:
        steps.append({"mode": "assistant", "title": "判断", "text": reasoning})
    steps.append({"mode": "json", "title": "提交的受限程序",
                  "text": json.dumps(program, ensure_ascii=False, indent=2)})
    if raw.get("expected_effect"):
        steps.append({"mode": "assistant", "title": "预期", "text": str(raw["expected_effect"])})
    return {"steps": steps, "program": program, "fallback": False,
            "model": model_seen, "effort": effort, "latency_s": None,
            "error": None, "rounds": rounds}


def _last_raw_json(content: str) -> dict[str, Any] | None:
    try:
        raw = json.loads(_strip_json(content))
    except (json.JSONDecodeError, TypeError):
        return None
    return raw if isinstance(raw, dict) else None


def _parse_program(content: str, fallback: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    raw = _last_raw_json(content)
    if raw is None:
        return _fallback_copy(fallback), "模型没有返回 JSON 对象"
    program = {
        "destroy_operators": _as_list(raw.get("destroy_operators")),
        "repair_operators": _as_list(raw.get("repair_operators")),
        "destroy_fraction": raw.get("destroy_fraction"),
        "acceptance": raw.get("acceptance"),
    }
    return program, None


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------


def decide(
    meta: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    timeout: float = 60.0,
    mode: str = "ptc",
) -> dict[str, Any]:
    model = resolve_model(model)
    effort = resolve_effort(model, effort)
    with collect_calls() as calls:
        result = _run_agent("surgical", meta, history, model=model, effort=effort, timeout=timeout, mode=mode)
    return {**result, "usage": combine(calls)}


def decide_hhcrsp(
    meta: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    timeout: float = 60.0,
    mode: str = "ptc",
) -> dict[str, Any]:
    model = resolve_model(model)
    effort = resolve_effort(model, effort)
    with collect_calls() as calls:
        result = _run_agent("hhcrsp", meta, history, model=model, effort=effort, timeout=timeout, mode=mode)
    return {**result, "usage": combine(calls)}


def review(
    *,
    scenario: str,
    meta: dict[str, Any],
    outcome: dict[str, Any],
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    timeout: float = 40.0,
) -> dict[str, Any] | None:
    """运行结束后的一次复盘：读结果，指出问题，给下一条可执行建议。

    失败不抛异常：返回 None，调用方保留原决策记录。
    """
    model = resolve_model(model)
    effort = resolve_effort(model, effort)
    system = ("你是排程实验的复盘助手。基于给出的运行结果做简短复盘，"
              "只依据数字，不臆测原因；若证据不足就说不足。")
    prompt = (f"场景：{'居家护理路径' if scenario == 'hhcrsp' else '手术排程'}。\n"
              f"运行结果：\n{json.dumps(outcome, ensure_ascii=False, indent=1)}\n"
              "请输出 JSON：observations（2–3 条，各一句，须引用具体数字）、"
              "diagnosis（一句话，指出最可能的原因或说明证据不足）、"
              "next_step（一句话，给出下一步可执行动作）。")
    try:
        message, info = _chat(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            model=model, effort=effort, timeout=timeout,
        )
        text = message.get("content") or ""
        raw = _last_raw_json(text)
        if not raw:
            return {"text": _short(text, 400), "info": info}
        lines = [f"· {str(item)}" for item in (raw.get("observations") or [])][:3]
        if raw.get("diagnosis"):
            lines.append(f"判断：{raw['diagnosis']}")
        if raw.get("next_step"):
            lines.append(f"下一步：{raw['next_step']}")
        return {"text": "\n".join(lines) or _short(text, 400), "info": info}
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError):
        return None
