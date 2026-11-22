"""问题场景注册表。

平台的单位是「一个场景 + 一个实例 + 一次运行」。场景决定实例从哪来、
用哪个求解内核、默认程序是什么、模型页展示哪套三要素。
两套内核的对外形状刻意一致，所以任务、轨迹、评价、产物都不用分叉。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# 手术排程（alns_lab）：FrontierOR 原生语义 + IHTC2024 确定性简化
SURGICAL_ROOTS = (
    ("FrontierOR", "data/frontieror/tasks/roshanaei2017/instance"),
    ("IHTC2024", "data/instances/ihtc2024"),
)

SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "surgical",
        "name": "手术排程",
        "alias": "OR scheduling",
        "kernel": "alns_lab",
        "datasets": ["FrontierOR", "IHTC2024"],
        "decision": "指派 x[p,h,o,d] · 开机 y[h,o,d] · 到院 z[s,h,d] · 未安排 u[p]",
        "constraints": "K1–K11",
        "objective": "max α·reward − (1−α)(开机 + 加班 + 医生固定) − M·强制未服务",
        "unit": "患者 → 医院 / 手术室 / 日",
        "default_program": {
            "destroy_operators": ["worst_due"],
            "repair_operators": ["regret_k"],
            "destroy_fraction": 0.15,
            "acceptance": "sa",
        },
        "evidence": "checker 为 L0 重建模型，不是 FrontierOR 官方基准。",
    },
    {
        "id": "hhcrsp",
        "name": "居家护理路径",
        "alias": "HHCRSP",
        "kernel": "hhcrsp_lab",
        "datasets": ["HHCRSP 华西"],
        "decision": "指派 x[p,k,d] · 启用 y[k,d] · 路线顺序 π[k,d]",
        "constraints": "H1–H8",
        "objective": "min Σ 固定成本·y + 2 × 旅行分钟",
        "unit": "患者 → 护理员 / 日 / 路线",
        "default_program": {
            "destroy_operators": ["worst_detour"],
            "repair_operators": ["greedy"],
            "destroy_fraction": 0.20,
            "acceptance": "sa",
        },
        "evidence": "名义模型；双预算鲁棒参数只登记不启用；华西算例为已注册实例的确定性前缀裁剪。",
    },
]

SCENARIO_BY_ID = {item["id"]: item for item in SCENARIOS}


def _entry(path: Path, root: Path, scenario: str, dataset: str) -> dict[str, Any]:
    return {
        "instance_id": path.stem,
        "scenario": scenario,
        "dataset": dataset,
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
    }


def scan_surgical(root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for dataset, relative in SURGICAL_ROOTS:
        directory = root / relative
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if path.name == "manifest.json":
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
                patients = len(raw.get("patients") or [])
                days = raw.get("num_days")
                hospitals = len(raw.get("hospitals") or []) or raw.get("num_hospitals")
                ors = (raw.get("ORs_per_hospital") or raw.get("num_ors_per_hospital")
                       or raw.get("ors_per_hospital"))
                mandatory = sum(1 for p in (raw.get("patients") or []) if p.get("is_mandatory"))
            except (OSError, json.JSONDecodeError, AttributeError):
                patients = days = hospitals = ors = mandatory = None
            record = _entry(path, root, "surgical", dataset)
            record.update({
                "patients": patients, "days": days, "hospitals": hospitals,
                "ors_per_hospital": ors, "mandatory": mandatory,
            })
            items.append(record)
    return items


def scan_hhcrsp(root: Path) -> list[dict[str, Any]]:
    directory = root / "data" / "instances" / "hhcrsp_huaxi"
    if not directory.is_dir():
        return []
    manifest = {}
    manifest_path = directory / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
    extra = {item["instance_id"]: item for item in manifest.get("instances", [])}
    items: list[dict[str, Any]] = []
    for path in sorted(directory.glob("huaxi_p*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            patients = len(raw.get("patients") or [])
            clinicians = len(raw.get("clinicians") or [])
            days = raw.get("planning_days")
            visits = sum(int(p.get("visit_frequency") or 0) for p in raw.get("patients") or [])
            window = (raw.get("patients") or [{}])[0].get("time_window") or [0, 0]
            width = max(0.0, float(window[1]) - float(window[0]))
            skills = len({skill for p in raw.get("patients") or []
                          for skill in (p.get("required_skills") or [])})
        except (OSError, json.JSONDecodeError, AttributeError, ValueError):
            patients = clinicians = days = visits = width = skills = None
        record = _entry(path, root, "hhcrsp", "HHCRSP 华西")
        record.update({
            "patients": patients, "clinicians": clinicians, "days": days,
            "visits": visits, "window_width": width, "skills": skills,
            "mandatory": 0, "hospitals": None, "ors_per_hospital": None,
            "required_visits": extra.get(path.stem, {}).get("required_visits", visits),
        })
        items.append(record)
    return items


def scan_all(root: Path) -> dict[str, dict[str, Any]]:
    items: list[dict[str, Any]] = scan_surgical(root) + scan_hhcrsp(root)
    return {item["instance_id"]: item for item in items}


def describe(scenario: str = "surgical") -> dict[str, Any]:
    item = SCENARIO_BY_ID.get(scenario, SCENARIO_BY_ID["surgical"])
    return {key: item[key] for key in
            ("id", "name", "alias", "kernel", "datasets", "decision",
             "constraints", "objective", "unit", "evidence")}
