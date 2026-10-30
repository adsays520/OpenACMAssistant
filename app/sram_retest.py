"""Fixed-bank SRAM device revalidation: explicit fields, PVT and failure evidence."""
import csv
import hashlib
import itertools
import json
from pathlib import Path
import re

from acco_spec import NeedsExperiment, validate_acco
from bindings import explicit_constraints, explicit_parameters
from spec import validate


def is_device_retest(text):
    return bool(re.search(r"器件复验|器件复测|固定.{0,12}(?:组织|架构).{0,12}(?:复验|器件)|只比较\s*(?:PU|PD|PG)\s*=", text, re.I))


def record_digest(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def resolve_reference(api, text, arch, capacity):
    number = re.search(r"第\s*(\d+)\s*号(?:架构|组织|候选)", text)
    if not number:
        return None
    named = re.findall(r"\d{8}_\d{6}_acco_sram_[a-z0-9]+", text)
    runs = (api.ROOT / "runs").resolve()
    matches = []
    for file in runs.glob("*/result.json"):
        if named and file.parent.name not in named:
            continue
        try:
            if not file.resolve().is_relative_to(runs):
                continue
            d = json.loads(file.read_text())
            if d.get("kind") != "acco" or d.get("workflow") != "sram" or d.get("config", {}).get("capacity_bits") != capacity:
                continue
            for r in d.get("stages", {}).get("architecture", {}).get("records", []):
                if r["candidate"].endswith("/" + f"{int(number[1]):04d}"):
                    if r["parameters"]["architecture"] == arch:
                        matches.append({"task": file.parent.name, "candidate": r["candidate"], "record_sha256": record_digest(r)})
        except (ValueError, KeyError, TypeError, OSError):
            continue
    if len(matches) != 1:
        raise NeedsExperiment("请给出4KB历史任务名；需要唯一核对第" + number[1] + "号组织与你给定的容量、行列、子阵列数和复用比。")
    return matches[0]


def verify_reference(api, config):
    ref = config.get("reference_design")
    if not ref:
        return
    runs = (api.ROOT / "runs").resolve()
    path = (runs / ref["task"] / "result.json").resolve(strict=True)
    if not path.is_relative_to(runs):
        raise ValueError("历史引用只能读取助手runs目录")
    d = json.loads(path.read_text())
    row = next((r for r in d.get("stages", {}).get("architecture", {}).get("records", []) if r["candidate"] == ref["candidate"]), None)
    if row is None or record_digest(row) != ref["record_sha256"]:
        raise ValueError("历史候选发生变化或不存在，请重新整理复验计划")


def from_request(api, text):
    s = text.replace("−", "-").replace("－", "-")
    shape = re.search(r"(\d+)\s*行\s*[×x*]\s*(\d+)\s*列\s*[×x*]\s*(\d+)\s*个子阵列", s, re.I)
    capacity = re.search(r"总容量\s*(?:为|=|：|:)?\s*(\d+)\s*bits?", s, re.I)
    width = re.search(r"输出(?:字宽|位宽)\s*(?:为|=|：|:)?\s*(\d+)\s*(?:bit|位)", s, re.I)
    mux = re.search(r"列复用比\s*(?:为|=|：|:)?\s*(\d+)", s)
    if not all((shape, capacity, width, mux)):
        raise NeedsExperiment("请明确固定组织的总容量、输出字宽、行×列×子阵列数和列复用比，避免把bank复验降成单阵列。")
    arch = dict(zip(("rows", "cols", "num_arrays"), map(int, shape.groups())))
    arch["mux_ratio"] = int(mux[1])
    if arch["rows"] * arch["cols"] * arch["num_arrays"] != int(capacity[1]):
        raise NeedsExperiment("行×列×子阵列数与总容量不一致，请统一这两个条件。")
    ref = resolve_reference(api, text, arch, int(capacity[1]))
    inherited = {}
    if ref:
        old = json.loads((api.ROOT / "runs" / ref["task"] / "result.json").read_text())
        inherited = old["config"]["plan"]["parameters"]
    p = {**inherited, **explicit_parameters(s, "sram")}
    p.update(rows=arch["rows"], cols=arch["cols"], full_array=True,
             operations=["read", "write", "hold_snm", "read_snm", "write_snm"])
    length = re.search(r"沟道长(?:度)?\s*(?:为|=|：|:)?\s*(\d+(?:\.\d+)?)\s*nm", s, re.I)
    if length:
        p["length_nm"] = float(length[1])
    seed = re.search(r"种子\s*(?:为|=|：|:)?\s*(\d+)", s)
    if seed:
        p["seed"] = int(seed[1])
    case_matches = list(re.finditer(r"(?<![a-z])(TT|SS|FF|FS|SF)\s*/\s*(-?\d+(?:\.\d+)?)\s*(?:℃|°C)\s*/\s*(\d+(?:\.\d+)?)\s*V(?![a-z])", s, re.I))
    if not case_matches:
        raise NeedsExperiment("请列出需要实际评价的PVT组合，例如TT/27℃/1V。")
    cases = [{"corner": m[1].upper(), "temperature_c": float(m[2]), "vdd_v": float(m[3])} for m in case_matches]
    p.update(cases[0])
    mc = re.search(r"(?<![a-z])MC\s*=\s*(\d+)", s, re.I)
    std = re.search(r"相对\s*标准差\s*(\d+(?:\.\d+)?)\s*[%％]", s)
    if not mc or not std:
        raise NeedsExperiment("请明确每个PVT组合的MC次数与相对标准差。")
    p["vth_std"] = float(std[1]) / 100
    p.update(variation=True, mc_runs=int(mc[1]))
    varying = re.search(r"只比较\s*(PU|PD|PG)\s*=\s*([\d.、，,\s]+)\s*nm", s, re.I)
    if not varying:
        raise NeedsExperiment("请给出唯一要扫描的器件及尺寸列表，例如只比较PD=90、135、180nm。")
    field = varying[1].lower() + "_width_nm"
    values = list(dict.fromkeys(float(x) for x in re.findall(r"\d+(?:\.\d+)?", varying[2])))
    if not values or len(values) > 8:
        raise NeedsExperiment("固定组织复验支持1–8个明确器件尺寸。")
    plan = validate({"workflow": "sram", "parameters": p, "constraints": explicit_constraints(s, "sram")})
    if plan["parameters"][field] not in values:
        raise NeedsExperiment("尺寸列表应包含明确给定的基线值，才能做同条件基线比较。")
    duration = re.search(r"(?:时间预算|限时|最长运行)\s*(\d+)\s*(秒|分钟|小时)", s)
    seconds = int(duration[1]) * {"秒": 1, "分钟": 60, "小时": 3600}[duration[2]] if duration else 7200
    config = {"workflow": "sram", "experiment_mode": "sram_device_retest", "plan": plan,
              "capacity_bits": int(capacity[1]), "output_width": int(width[1]),
              "architecture_space": {"rows": [arch["rows"]], "cols": [arch["cols"]], "mux_ratio": [arch["mux_ratio"]]},
              "device_space": {field: values}, "solvers": {"architecture": "random", "device": "random"},
              "budgets": {"architecture": 1, "validation": 1, "shortlist": 1, "device_per_parent": len(values), "max_seconds": seconds},
              "robustness": {"cases": cases, "mc_runs": int(mc[1]), "vth_std": p["vth_std"]}}
    if ref:
        config["reference_design"] = ref
    try:
        return validate_acco(config)
    except ValueError as exc:
        raise NeedsExperiment(str(exc)) from exc


def run_fixed(runner):
    from acco import BudgetStop, frontier
    from acco_models import aggregate_corners, bank_metrics, load_module
    from exploration import json_write
    c, job = runner.config, runner.job
    p = c["plan"]["parameters"]
    grid = c["architecture_space"]
    arch = {"rows": grid["rows"][0], "cols": grid["cols"][0], "mux_ratio": grid["mux_ratio"][0]}
    arch["num_arrays"] = c["capacity_bits"] // (arch["rows"] * arch["cols"])
    # Device evaluation includes the fresh TT baseline; no redundant architecture SPICE consumes its time.
    runner.state["stages"]["architecture"] = {"status": "completed", "stop_reason": "fixed_architecture",
        "evidence_kind": "capacity_and_interface_check", "budget": 1, "records": [{"candidate": "architecture/0001",
        "status": "completed", "evidence_kind": "capacity_and_interface_check", "parameters": {"architecture": arch},
        "metrics": {"capacity_bits": c["capacity_bits"], "output_width": c["output_width"]}}]}
    import time
    runner.state["budget_reservation"] = {"device_seconds": max(0, runner.deadline - time.monotonic()), "architecture_spice_tasks": 0}
    runner.state["shortlist"] = ["architecture/0001"]
    area = load_module("acco_retest_area", job / "references/OpenYield/utils.py")
    baseline = {k: p[k] for k in c["device_space"]}
    devices = [baseline] + [dict(zip(c["device_space"], v)) for v in itertools.product(*c["device_space"].values())]
    devices = list({json.dumps(d, sort_keys=True): d for d in devices}.values())
    pool = [{"architecture": arch, "devices": d} for d in devices]
    planned = job / "planned_candidates.json"
    json_write(planned, pool)
    def evaluate(design, task):
        runner.state["counters"]["device_candidates"] += 1
        coverage = {"seed": p["seed"], "mc_runs": c["robustness"]["mc_runs"], "vth_std": c["robustness"]["vth_std"],
            "cases": [{**case, "status": "not_run"} for case in c["robustness"]["cases"]],
            "limitations": "有限MC不代表宏级良率；阵列SPICE与bank外围/面积估算分开记录"}
        json_write(task / "coverage.json", coverage)
        runner.save()
        metrics = []
        for i, case in enumerate(c["robustness"]["cases"]):
            runner.remaining()
            runner.state["active_case"] = {**case, "devices": design["devices"], "task": str(task.relative_to(job)), "case_index": i}
            runner.save()
            params = {**p, "rows": arch["rows"], "cols": arch["cols"], **design["devices"], **case, "variation": True, "mc_runs": c["robustness"]["mc_runs"],
                      "vth_std": c["robustness"]["vth_std"], "column_mux": arch["mux_ratio"] == 2}
            child = task / f"pvt_{i:02d}"
            result = runner.run_array(child, {**c["plan"], "parameters": params, "constraints": [], "objective": None})
            row = {**case, "status": result["status"], "task": str(child.relative_to(job)),
                   "array_metrics": result.get("metrics", {}), "error": result.get("error")}
            partial = child / "metrics.json"
            if not row["array_metrics"] and partial.is_file():
                row["array_metrics"] = json.loads(partial.read_text())
            if row["status"] == "completed":
                try:
                    row["bank_metrics"] = bank_metrics(child, params, arch, c["output_width"], area)
                    metrics.append(row["bank_metrics"])
                except Exception as exc:
                    row.update(status="failed", error=str(exc))
            coverage["cases"][i] = row
            json_write(task / "coverage.json", coverage)
        if len(metrics) != len(coverage["cases"]):
            raise ValueError("存在失败PVT案例，详见coverage.json及逐角日志；其它案例结果已保留")
        return aggregate_corners(metrics)
    try:
        good, _ = runner.search("device_fixed_architecture", pool, evaluate, c["device_objectives"], c["bank_constraints"],
            len(pool), "random", "pvt_mc_array_spice_plus_bank_estimate", "architecture/0001")
        runner.state["best_design"] = good[0] if good else None
        records = runner.state["stages"]["device_fixed_architecture"]["records"]
        runner.state["status"] = "completed" if len(records) == len(pool) and all(r["status"] == "completed" for r in records) else "failed"
    finally:
        stage = runner.state["stages"].get("device_fixed_architecture", {})
        seen = [r["parameters"] for r in stage.get("records", [])]
        stage["pending_candidates"] = [{"status": "not_run", "parameters": d} for d in pool if d not in seen]
        partial, _ = frontier(stage.get("records", []), c["device_objectives"])
        if partial:
            runner.state["best_design"] = partial[0]
        runner.state["active_case"] = None
        runner.state["implementation_limit"] = "固定组织只做容量/接口校验；所有器件在相同PVT/MC下重新SPICE评价。bank外围/面积为估算，少量MC不证明良率。"
        runner.save()


def render_retest(job, result):
    """Keep partial cases and unexecuted candidates visible; baseline is matched by PVT."""
    rows, base, comparisons = [], {}, []
    from acco_models import bank_area_estimate, load_module
    area = load_module("acco_retest_report_area", job / "references/OpenYield/utils.py")
    stage = result.get("stages", {}).get("device_fixed_architecture", {})
    for r in stage.get("records", []):
        file = job / r["task"] / "coverage.json"
        if not file.is_file():
            continue
        cov = json.loads(file.read_text())
        for i, case in enumerate(cov["cases"]):
            array = case.get("array_metrics", {})
            partial = job / case.get("task", "") / "metrics.json"
            if not array and partial.is_file():
                array = json.loads(partial.read_text())
            parameters = {**result["config"]["plan"]["parameters"], **r["parameters"]["devices"]}
            estimated_area = bank_area_estimate(parameters, r["parameters"]["architecture"], area)
            row = {"candidate": r["candidate"], **r["parameters"]["devices"], "case_index": i,
                   "corner": case["corner"], "temperature_c": case["temperature_c"], "vdd_v": case["vdd_v"],
                   "status": case["status"], "task": case.get("task"), "error": case.get("error"),
                   "bank_area_um2_estimate": estimated_area, **{"array_" + k: v for k, v in array.items()}, **case.get("bank_metrics", {})}
            rows.append(row)
            if r.get("proposed_by") == "baseline":
                base[i] = row
    for row in rows:
        b = base.get(row["case_index"])
        if b:
            comparisons.append({"candidate": row["candidate"], "case_index": row["case_index"], "baseline_candidate": b["candidate"],
                "complete_case": b["status"] == row["status"] == "completed",
                "scope": "每项已有测量或明确模型估算独立比较；缺失写/SNM不能据此验收整个PVT",
                "metrics": {k: {"baseline": b[k], "actual": row[k], "difference": row[k] - b[k]}
                            for k in row if k in b and (k.startswith(("bank_", "array_")) or "snm" in k) and type(row[k]) in (int, float)}})
    from exploration import json_write
    json_write(job / "reports/baseline_comparison.json", comparisons)
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with (job / "reports/pvt_results.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    lines = ["", "## 固定组织器件复验", "", "架构阶段仅检查容量与接口，全部SPICE预算保留给器件阶段，包含基线各PVT复测。", "",
        "| 候选 | 器件尺寸(nm) | PVT | 状态 | 读延迟估算(ns) | 写延迟估算(ns) | bank面积估算(µm²) | hold/read/write SNM(V) |", "|---|---|---|---|---:|---:|---:|---|"]
    for row in rows:
        devices = ",".join(f"{k}={row[k]:g}" for k in result["config"]["device_space"])
        snm = "/".join(str(row.get(k, "未测")) for k in ("hold_snm_v", "read_snm_v", "write_snm_v"))
        lines.append(f"| {row['candidate']} | {devices} | {row['corner']}/{row['temperature_c']}℃/{row['vdd_v']}V | {row['status']} | {row.get('bank_read_delay_ns_estimate','未测')} | {row.get('bank_write_delay_ns_estimate','未测')} | {row.get('bank_area_um2_estimate','未测')} | {snm} |")
    for pending in stage.get("pending_candidates", []):
        lines.append("\n未执行候选：" + json.dumps(pending["parameters"]["devices"], ensure_ascii=False) + "；状态not_run，无可行性结论。")
    lines += ["", "| 候选 | PVT | 阵列读延迟(ns) | 阵列读功耗(µW) | 阵列写延迟(ns) | 阵列写功耗(µW) |",
              "|---|---|---:|---:|---:|---:|"]
    for row in rows:
        lines.append(f"| {row['candidate']} | {row['corner']} | {row.get('array_read_delay_ns','未测')} | {row.get('array_read_power_uw','未测')} | {row.get('array_write_delay_ns','未测')} | {row.get('array_write_power_uw','未测')} |")
    lines.append("\n每角全部指标见pvt_results.csv，同条件逐项基线比较见baseline_comparison.json；原始样本在各候选pvt_XX/artifacts/<操作>/samples.json，失败日志保留。失败案例只保留已完成操作的部分测量，不能当作整个PVT通过。")
    return "\n".join(lines)
