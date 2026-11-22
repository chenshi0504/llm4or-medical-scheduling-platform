"""建模流水线：自然语言 → 结构化规格 → 数学模型 → 求解代码 → 一致性校验。

对应参考图（CPAM-LLM, Nature Communications 2026）的 Online Inference Pipeline：
不同模块各做一件事，每一段的产物都落盘、可打开检查，而不是只给一句解释。

与参考图刻意不同的地方：
- 不训练、不微调，也不建概念格；检索层只用数据集 manifest 与实例画像（知识卡）。
- 生成的求解代码**不执行**：只做 AST 与白名单校验，然后供人阅读。真正跑的是
  alns_lab / hhcrsp_lab，结果由独立 checker 复算。这样"可检查"不等于"可执行"，
  避免把不可控代码塞进实验链路。
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from decision import DEFAULT_EFFORT, DEFAULT_MODEL, _chat, _strip_json
from model_usage import collect_calls, combine
from audit import trace_scope, emit

SURGICAL_CODES = {
    "K1": "唯一指派与资格", "K2": "强制患者必须服务", "K3": "手术室日容量与加班上限",
    "K4": "同一手术室同日不重叠", "K5": "医生日可用", "K6": "同一医生同日不重叠",
    "K7": "手术室归属与规划期", "K8": "截止日", "K9": "准备与清洁占用",
    "K10": "开机成本联动", "K11": "零收益可选患者可跳过",
}
HHCRSP_CODES = {
    "H1": "技能匹配", "H2": "排班可用", "H3": "访视频次", "H4": "同日不重复",
    "H5": "最小间隔", "H6": "服务时间窗", "H7": "日工时上限", "H8": "路线闭合",
}
MATHML_TAGS = {
    "math", "mrow", "mi", "mo", "mn", "msub", "msup", "mfrac", "munder", "mover",
    "mtext", "mspace", "msqrt", "mtable", "mtr", "mtd", "semantics", "annotation",
}
CODE_IMPORTS = {"gurobipy", "ortools", "pulp", "pyomo", "math", "itertools", "typing", "dataclasses", "functools"}
FORBIDDEN_CALLS = {"open", "eval", "exec", "compile", "__import__", "input", "system", "popen"}


def codes_for(scenario: str) -> dict[str, str]:
    return HHCRSP_CODES if scenario == "hhcrsp" else SURGICAL_CODES


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _instance_facts(scenario: str, meta: dict[str, Any]) -> dict[str, Any]:
    """从原始算例读出可核对的事实；LLM 只负责组织，不负责编数字。"""
    raw: dict[str, Any] = {}
    path = meta.get("path")
    if path:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            raw = {}
    if scenario == "hhcrsp":
        patients = raw.get("patients") or []
        clinicians = raw.get("clinicians") or []
        windows = [float((p.get("time_window") or [0, 0])[1]) - float((p.get("time_window") or [0, 0])[0])
                   for p in patients]
        return {
            "instance_id": meta.get("instance_id"),
            "patients": len(patients) or meta.get("patients"),
            "clinicians": len(clinicians) or meta.get("clinicians"),
            "days": raw.get("planning_days") or meta.get("days"),
            "visits": sum(int(p.get("visit_frequency") or 0) for p in patients) or meta.get("visits"),
            "window_width": round(sum(windows) / len(windows), 1) if windows else None,
            "work_limit": float((clinicians[0] or {}).get("daily_work_limit") or 480) if clinicians else 480,
            "skills": sorted({s for p in patients for s in (p.get("required_skills") or [])}),
        }
    patients = raw.get("patients") or []
    hospitals = raw.get("hospitals") or []
    return {
        "instance_id": meta.get("instance_id"),
        "patients": len(patients) or meta.get("patients"),
        "days": raw.get("num_days") or meta.get("days"),
        "hospitals": len(hospitals) or meta.get("hospitals"),
        "ors": sum(len(h.get("ORs") or []) for h in hospitals),
        "mandatory": sum(1 for p in patients if p.get("is_mandatory")),
        "demand_minutes": round(sum(float(p.get("base_surgical_time") or 0) for p in patients), 1),
    }


def _knowledge_card(scenario: str, meta: dict[str, Any]) -> dict[str, Any]:
    """检索层：数据集 manifest + 实例画像。参考图里叫 Domain Knowledge Base。"""
    facts = _instance_facts(scenario, meta)
    return {
        "source": "dataset manifest + instance profile",
        "scenario": "居家护理（HHCRSP）" if scenario == "hhcrsp" else "手术排程（OR）",
        "facts": facts,
        "constraint_catalog": codes_for(scenario),
        "reduction_rules": ([
            "七天规划期；时间窗按天统一，宽度见 facts",
            "访视频次按次计数；同一患者同日最多一次",
            "名义模型：服务与旅行时间取均值，鲁棒预算不启用",
        ] if scenario == "hhcrsp" else [
            "L0 重建模型：K1–K11 由 alns_lab 固定",
            "可选患者可跳过；强制患者未服务计硬违反",
            "目标为 reward 与开机/加班/医生固定成本的加权差",
        ]),
    }


def _safe(fn, *args, **kwargs) -> tuple[Any, dict]:
    """单段失败不拖垮整条流水线：返回 (None, 错误信息)，由校验层标红。"""
    trace_path = kwargs.pop("_trace_path", None)
    phase = kwargs.pop("_phase", fn.__name__)
    artifact_path = kwargs.pop("_artifact_path", None)
    with trace_scope(trace_path, phase), collect_calls() as calls:
        try:
            value, info = fn(*args, **kwargs)
        except Exception as exc:
            detail = getattr(exc, "reason", None) or str(exc)
            value, info = None, {"error": f"{type(exc).__name__}: {detail}", "model": kwargs.get("model")}
    usage = combine(calls)
    if artifact_path and (value is not None or info.get("raw")):
        _write(artifact_path, value if value is not None else {"raw": info.get("raw")}, "llm")
    return value, {**info, "latency_s": usage["request_seconds"], "usage": usage}


def _call_json(system: str, prompt: str, *, model: str, effort: str, timeout: float,
               max_tokens: int = 2000) -> tuple[Any, dict]:
    """返回空内容或半截 JSON 时自动加预算重试一次——推理模型会把额度吃在思考上。"""
    budget = max(max_tokens, {"high": 10000, "max": 16000}.get(effort, 0))
    info: dict[str, Any] = {}
    text = ""
    for _attempt in range(2):
        message, info = _chat(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            model=model, effort=effort, timeout=timeout, max_tokens=budget,
        )
        text = message.get("content") or ""
        if text.strip():
            try:
                value = json.loads(_strip_json(text))
                if isinstance(value, dict) and info.get("finish_reason") != "length":
                    return value, {**info, "raw": text}
            except json.JSONDecodeError:
                pass
        budget = int(budget * 1.6)
    error = "重试后仍未返回完整的 JSON 对象" if text.strip() else "两次请求均未取得有效规格，最后一次回复正文为空"
    if info.get("finish_reason") == "length":
        error = "输出达到 token 上限，重试后仍缺少完整 JSON"
    return None, {**info, "raw": text, "error": error}


# ---------------------------------------------------------------------------
# 阶段 A：自然语言结构化
# ---------------------------------------------------------------------------


def _stage_spec(scenario: str, card: dict[str, Any], *, model: str, effort: str, timeout: float):
    codes = codes_for(scenario)
    system = ("你是运筹建模助手。把给定的算例事实整理成结构化规格，只输出 JSON。\n"
              "不要虚构数字：所有数值必须来自 facts；拿不到就写 null。")
    prompt = (
        f"场景：{card['scenario']}\n事实：{json.dumps(card['facts'], ensure_ascii=False)}\n"
        f"约束目录：{json.dumps(codes, ensure_ascii=False)}\n"
        f"已知约定：{json.dumps(card['reduction_rules'], ensure_ascii=False)}\n"
        "输出 JSON：\n"
        "parameters: [{symbol, name, value}]，最多 8 条，只保留关键量，value 用字符串；\n"
        "variables: [{symbol, index: [...], domain, meaning}]，最多 4 组；\n"
        "constraints: [{code, name, expr, kind}]，code 必须取自约束目录且全覆盖，expr 一行、不超过 60 字符；\n"
        "objective: {sense: minimize|maximize, expression, terms: [{name, meaning}]}，terms 最多 4 条；\n"
        "assumptions: 最多 3 条。控制篇幅，不要展开解释。"
    )
    data, info = _call_json(system, prompt, model=model, effort=effort, timeout=timeout, max_tokens=4200)
    return data, info


def _check_spec(spec: Any, scenario: str, facts: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(code: str, name: str, ok: bool, detail: str = "") -> None:
        checks.append({"id": code, "name": name, "ok": ok, "detail": detail})

    if not isinstance(spec, dict):
        add("A1", "规格是 JSON 对象", False, "模型没有返回 JSON 对象")
        return checks
    params = spec.get("parameters") or []
    variables = spec.get("variables") or []
    constraints = spec.get("constraints") or []
    objective = spec.get("objective") or {}
    add("A1", "四块齐全", all([params, variables, constraints, objective]),
        f"参数 {len(params)} / 变量 {len(variables)} / 约束 {len(constraints)}")
    expected = set(codes_for(scenario))
    got = {str(item.get("code")) for item in constraints if isinstance(item, dict)}
    add("A2", "约束编号覆盖", got == expected,
        f"缺 {sorted(expected - got) or '无'}；多 {sorted(got - expected) or '无'}")
    names_ok = all(str(item.get("name") or "").strip() for item in constraints if isinstance(item, dict))
    add("A3", "约束都有名称", names_ok)
    numeric = facts.get("patients")
    text = json.dumps(spec, ensure_ascii=False)
    add("A4", "关键数字与算例一致", numeric is None or str(numeric) in text,
        f"算例患者数 {numeric}")
    add("A5", "目标有方向与表达式",
        str(objective.get("sense")) in {"minimize", "maximize"} and bool(objective.get("expression")))
    return checks


# ---------------------------------------------------------------------------
# 阶段 B：数学模型
# ---------------------------------------------------------------------------


def _stage_model(scenario: str, spec: dict[str, Any], *, model: str, effort: str, timeout: float):
    system = ("你是运筹建模助手。把结构化规格写成 MathML 公式，只输出 JSON。\n"
              "只用这些标签：mi、mo、mn、mrow、msub、msup、mfrac、munder、mover、mtext、mspace。\n"
              "严格精简：不要嵌套 mrow，符号平铺；每条公式不超过 400 字符、40 个元素。")
    prompt = (
        f"约束与目标：{json.dumps({'constraints': spec.get('constraints'),
                                'objective': spec.get('objective')}, ensure_ascii=False)}\n"
        "输出 JSON：constraints: [{code, mathml, note}]，code 与输入一一对应；"
        "objective_mathml: 一行目标式；symbols: [用到的符号]。\n"
        "mathml 必须是完整片段，根元素 <math xmlns=\"http://www.w3.org/1998/Math/MathML\">。"
        "整个回答压成一行 JSON，不要缩进，不要解释。"
    )
    data, info = _call_json(system, prompt, model=model, effort=effort, timeout=timeout, max_tokens=6000)
    return data, info


def _check_mathml(fragment: str) -> tuple[bool, str]:
    if not isinstance(fragment, str) or "<math" not in fragment:
        return False, "缺少 <math> 根元素"
    try:
        root = ET.fromstring(fragment)
    except ET.ParseError as exc:
        return False, f"XML 解析失败：{exc}"
    tags = {node.tag.split("}")[-1] for node in root.iter()}
    bad = tags - MATHML_TAGS
    if bad:
        return False, f"出现非 MathML 标签：{sorted(bad)}"
    if len(fragment) > 900:
        return False, f"公式过长（{len(fragment)} 字符），要求精简"
    if len(list(root.iter())) > 90:
        return False, "元素过多，疑似堆砌"
    return True, "ok"


def _check_model(model_obj: Any, spec: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if not isinstance(model_obj, dict):
        return [{"id": "B1", "name": "模型是 JSON 对象", "ok": False, "detail": "没有返回 JSON"}]
    expected = {str(item.get("code")) for item in spec.get("constraints") or []}
    items = model_obj.get("constraints") or []
    got = {str(item.get("code")) for item in items if isinstance(item, dict)}
    checks.append({"id": "B1", "name": "约束编号与规格一致", "ok": expected == got,
                   "detail": f"缺 {sorted(expected - got) or '无'}"})
    bad = []
    for item in items:
        ok, detail = _check_mathml(str(item.get("mathml") or ""))
        if not ok:
            bad.append(f"{item.get('code')}: {detail}")
    checks.append({"id": "B2", "name": "每条公式是合法 MathML", "ok": not bad,
                   "detail": "；".join(bad[:3])})
    ok, detail = _check_mathml(str(model_obj.get("objective_mathml") or ""))
    checks.append({"id": "B3", "name": "目标式是合法 MathML", "ok": ok, "detail": detail})
    return checks


# ---------------------------------------------------------------------------
# 阶段 C：求解代码
# ---------------------------------------------------------------------------


def _stage_code(scenario: str, spec: dict[str, Any], *, model: str, effort: str, timeout: float):
    system = ("你是运筹求解代码助手。按规格写 Python 代码，只输出代码块，不要解释。\n"
              "使用 gurobipy 写 Gurobi 模型；也允许 math、itertools、typing、dataclasses、functools。\n"
              "不要读写文件、不要联网、不要调用 eval/exec/open。")
    prompt = (
        f"场景：{'HHCRSP 居家护理' if scenario == 'hhcrsp' else '手术排程'}\n"
        f"约束：{json.dumps(spec.get('constraints'), ensure_ascii=False)}\n"
        f"目标：{json.dumps(spec.get('objective'), ensure_ascii=False)}\n"
        "输出一个紧凑模块，最多 60 行，不写注释、不重复定义数据校验工具函数：常量 CONSTRAINTS = [约束编号...]；"
        "函数 build_model(instance) 里用 gurobipy 建主要变量与约束；函数 solve(instance) 返回解；"
        "数据字段缺失时 raise ValueError。只写关键骨架，不要展开数据读取与日志。"
        "输出必须完整闭合，优先使用紧凑的 addConstrs/quicksum 而不是展开长循环。"
    )
    budget = 16000
    info: dict[str, Any] = {}
    code = ""
    for _attempt in range(2):
        message, info = _chat([{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                              model=model, effort=effort, timeout=timeout, max_tokens=budget)
        text = message.get("content") or ""
        fence = re.search(r"```(?:python)?\s*(.+?)```", text, re.S)
        code = fence.group(1).strip() if fence else re.sub(r"^\s*```(?:python)?\s*\n", "", text).strip()
        try:
            tree = ast.parse(code)
            functions = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
            if info.get("finish_reason") != "length" and {"build_model", "solve"} <= functions:
                return code, {**info, "raw": text}
        except SyntaxError:
            pass
        budget = int(budget * 1.6)
    return code, {**info, "raw": text}


def _check_code(code: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [{"id": "C1", "name": "代码可解析", "ok": False, "detail": f"第 {exc.lineno} 行：{exc.msg}"}]
    checks.append({"id": "C1", "name": "代码可解析", "ok": True, "detail": f"{len(code.splitlines())} 行"})

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    bad_imports = imports - CODE_IMPORTS
    checks.append({"id": "C2", "name": "依赖在白名单内", "ok": not bad_imports,
                   "detail": f"越界 {sorted(bad_imports) or '无'}"})

    funcs = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    checks.append({"id": "C3", "name": "有 build_model 与 solve", "ok": {"build_model", "solve"} <= funcs,
                   "detail": f"函数 {sorted(funcs)}"})

    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.add(node.func.id)
    bad_calls = calls & FORBIDDEN_CALLS
    checks.append({"id": "C4", "name": "无危险调用", "ok": not bad_calls,
                   "detail": f"命中 {sorted(bad_calls) or '无'}"})

    expected = {str(item.get("code")) for item in spec.get("constraints") or []}
    literal = set(re.findall(r'["\']([KH]\d+)["\']', code))
    checks.append({"id": "C5", "name": "覆盖规格中的约束编号", "ok": expected <= literal,
                   "detail": f"缺 {sorted(expected - literal) or '无'}"})
    return checks


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


def _write(path: Path, payload: Any, origin: str = "system") -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        data = payload.encode("utf-8")
    else:
        data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(data)
    return {"path": path.name, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data), "origin": origin}


def build(
    *,
    scenario: str,
    meta: dict[str, Any],
    out_dir: Path,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """跑三段生成 + 一致性校验，全部落盘。单段失败不吞：标 failed 并保留原文。"""
    started = time.perf_counter()
    if effort in {"high", "max"}:
        timeout = max(timeout, 240.0)
    card = _knowledge_card(scenario, meta)
    out_dir.mkdir(parents=True, exist_ok=True)
    stage_rows: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []

    # A：后续两段都依赖它，只能先跑
    trace_path = out_dir.parent / "model_trace.jsonl"
    spec, info_a = _safe(_stage_spec, scenario, card, model=model, effort=effort, timeout=timeout,
                         _trace_path=trace_path, _phase="build.spec", _artifact_path=out_dir / "spec.json")
    checks_a = _check_spec(spec, scenario, card["facts"])
    ok_a = bool(spec) and all(item["ok"] for item in checks_a)
    artifacts.append(_write(out_dir / "spec.json", spec if spec is not None else {"raw": info_a.get("raw")},
                            "llm" if spec is not None or info_a.get("raw") else "system"))
    stage_rows.append({"id": "A", "name": "自然语言结构化", "state": "done" if ok_a else "failed",
                       "model": info_a.get("model"), "latency_s": info_a.get("latency_s"),
                       "error": info_a.get("error"), "checks": checks_a, "artifact": "spec.json",
                       "summary": f"{len((spec or {}).get('parameters') or [])} 个参数 / "
                                  f"{len((spec or {}).get('variables') or [])} 组变量 / "
                                  f"{len((spec or {}).get('constraints') or [])} 条约束"})

    # A 不合格就不调用下游：避免用空规格生成看似通过的垃圾
    if not spec or not ok_a:
        model_obj, info_b = None, {"error": "上游规格不合格，未调用"}
        code, info_c = "", {"error": "上游规格不合格，未调用"}
    # B / C 只依赖 A，并发跑
    with ThreadPoolExecutor(max_workers=2) as pool:
        if spec and ok_a:
            future_b = pool.submit(_safe, _stage_model, scenario, spec or {}, model=model,
                                   effort=effort, timeout=timeout, _trace_path=trace_path, _phase="build.model",
                                   _artifact_path=out_dir / "model.json")
            future_c = pool.submit(_safe, _stage_code, scenario, spec or {}, model=model,
                                   effort=effort, timeout=timeout, _trace_path=trace_path, _phase="build.code",
                                   _artifact_path=out_dir / "solver.py")
            model_obj, info_b = future_b.result()
            code, info_c = future_c.result()
    checks_b = _check_model(model_obj, spec or {}) if ok_a else []
    ok_b = bool(model_obj) and all(item["ok"] for item in checks_b)
    artifacts.append(_write(out_dir / "model.json", model_obj if model_obj is not None else {"raw": info_b.get("raw")},
                            "llm" if model_obj is not None or info_b.get("raw") else "system"))
    stage_rows.append({"id": "B", "name": "数学模型生成", "state": ("done" if ok_b else "failed") if ok_a else "skipped",
                       "model": info_b.get("model"), "latency_s": info_b.get("latency_s"),
                       "error": info_b.get("error"), "checks": checks_b, "artifact": "model.json",
                       "summary": f"{len((model_obj or {}).get('constraints') or [])} 条公式"
                                  + ("，目标式 1 条" if (model_obj or {}).get("objective_mathml") else "")})

    checks_c = _check_code(code, spec or {}) if code else (
        [{"id": "C1", "name": "收到求解代码", "ok": False, "detail": "模型未返回代码"}] if ok_a else [])
    ok_c = bool(code) and all(item["ok"] for item in checks_c)
    artifacts.append(_write(out_dir / "solver.py", code or "# 模型没有返回代码\n", "llm" if code else "system"))
    stage_rows.append({"id": "C", "name": "求解代码生成", "state": ("done" if ok_c else "failed") if ok_a else "skipped",
                       "model": info_c.get("model"), "latency_s": info_c.get("latency_s"),
                       "error": info_c.get("error"), "checks": checks_c, "artifact": "solver.py",
                       "summary": f"{len((code or '').splitlines())} 行，未执行，仅静态校验"})

    all_checks = checks_a + checks_b + checks_c
    passed = sum(1 for item in all_checks if item["ok"])
    validation = {
        "passed": passed,
        "failed": len(all_checks) - passed,
        "ok": passed == len(all_checks),
        "checks": all_checks,
    }
    artifacts.append(_write(out_dir / "validation.json", validation))
    artifacts.append(_write(out_dir / "knowledge.json", card))

    manifest = {
        "state": "done",
        "scenario": scenario,
        "instance_id": meta.get("instance_id"),
        "model": model,
        "effort": effort,
        "elapsed_s": round(time.perf_counter() - started, 2),
        "usage": combine(info.get("usage") for info in (info_a, info_b, info_c)),
        "validation": {"passed": passed, "failed": validation["failed"], "ok": validation["ok"]},
        "stages": stage_rows,
        "artifacts": artifacts,
        "boundary": {
            "solver_code_executed": False,
            "note": "生成代码只做静态校验；实际求解由 alns_lab / hhcrsp_lab 完成，结果由独立 checker 复算。",
        },
    }
    manifest["stages_digest"] = _sha(json.dumps(stage_rows, ensure_ascii=False, sort_keys=True))
    _write(out_dir / "manifest.json", manifest)
    return manifest
