"""Experiment contracts for accuracy-constrained, two-level DCiM exploration."""
import copy
import itertools
import math
import re

from spec import DEFAULTS, validate

SRAM_DEVICE = {"pu_width_nm", "pd_width_nm", "pg_width_nm", "wordline_scale", "precharge_scale",
               "senseamp_scale", "write_driver_scale", "decoder_scale", "mux_scale"}
BANK_ALIASES = {"read_delay_ns": "bank_read_delay_ns_estimate", "write_delay_ns": "bank_write_delay_ns_estimate",
                "read_power_uw": "bank_read_power_uw_estimate", "write_power_uw": "bank_write_power_uw_estimate"}
BANK_METRICS = {*BANK_ALIASES.values(), "bank_area_um2_estimate", "bank_pmax_uw_estimate",
                "bank_dmax_ns_estimate", "fom_arch_estimate", "fom_robust_estimate",
                "hold_snm_v", "read_snm_v", "write_snm_v", "min_snm_v"}
PREDICTED = {"predicted_mred", "predicted_nmed", "predicted_delay_ns", "predicted_area_um2",
             "predicted_power_uw", "predicted_pdp_fj"}


class NeedsExperiment(ValueError):
    """Missing experiment specifications can be supplied in the same conversation."""


def integer(value, name, lo=1, hi=10000):
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError(f"{name}必须是{lo}–{hi}的整数")
    return value


def objectives(items, allowed):
    if not isinstance(items, list) or not 1 <= len(items) <= 4:
        raise ValueError("需要1–4个明确的优化目标")
    for item in items:
        if (not isinstance(item, dict) or set(item) != {"metric", "direction"}
                or item["metric"] not in allowed or item["direction"] not in ("min", "max")):
            raise ValueError(f"目标必须使用允许的指标：{sorted(allowed)}")
    return copy.deepcopy(items)


def validate_acco(raw):
    allowed = {"schema_version", "workflow", "plan", "capacity_bits", "output_width", "architecture_space",
               "device_space", "budgets", "solvers", "robustness", "architecture_objectives", "device_objectives",
               "bank_constraints", "physical_evaluation"}
    if not isinstance(raw, dict) or set(raw) - allowed or raw.get("schema_version", 1) != 1:
        raise ValueError("ACCO实验格式无效或含未知字段")
    config = copy.deepcopy(raw)
    wf = config.get("workflow")
    if wf not in ("sram", "multiplier"):
        raise ValueError("ACCO两级入口支持sram/multiplier；其它工作流保留独立验证入口")
    config["schema_version"] = 1
    plan = validate(config.get("plan", {"workflow": wf, "parameters": {}}))
    if plan["intent"] != "run" or plan["workflow"] != wf or plan["sweep"]:
        raise ValueError("ACCO基线需为同一电路、无sweep的run计划")
    config["plan"] = plan
    p = plan["parameters"]
    config.setdefault("budgets", {})
    defaults = {"architecture": 64, "shortlist": 3, "validation": 3, "device_per_parent": 8, "max_seconds": 7200}
    if set(config["budgets"]) - set(defaults):
        raise ValueError("未知阶段预算")
    config["budgets"] = {**defaults, **config["budgets"]}
    for key, val in config["budgets"].items():
        integer(val, key, 10 if key == "max_seconds" else 1, 604800 if key == "max_seconds" else 10000)
    if config["budgets"]["shortlist"] > min(config["budgets"]["architecture"], config["budgets"]["validation"]):
        raise ValueError("shortlist不能超过架构或复核预算")
    config["solvers"] = {"architecture": "llm", "device": "llm", **config.get("solvers", {})}
    if set(config["solvers"]) != {"architecture", "device"} or any(v not in ("llm", "random", "nsga2") for v in config["solvers"].values()):
        raise ValueError("阶段求解器支持llm/random/nsga2，nsga2复用原项目实现")
    if wf == "sram":
        if not p["full_array"]:
            raise ValueError("bank探索要求full_array=true，不能把单bit测试当成阵列评价")
        capacity = integer(config.get("capacity_bits"), "capacity_bits", 4, 1 << 24)
        config["output_width"] = integer(config.get("output_width"), "output_width", 1, 512)
        grid = config.setdefault("architecture_space", {"rows": [16, 32, 64, 128, 256, 512],
                                                        "cols": [8, 16, 32, 64, 128, 256], "mux_ratio": [1, 2]})
        if set(grid) != {"rows", "cols", "mux_ratio"}:
            raise ValueError("SRAM架构空间仅为rows/cols/mux_ratio；器件参数属于第二级")
        for key, values in grid.items():
            if not isinstance(values, list) or not 1 <= len(values) <= 64:
                raise ValueError("架构网格必须为有限非空列表")
            for value in values:
                integer(value, key, 1 if key == "mux_ratio" else 2, 2 if key == "mux_ratio" else 4096)
                if value & (value - 1):
                    raise ValueError("行列与复用比必须为2的幂；当前电路支持mux_ratio=1/2")
        if not any(capacity % (r*c) == 0 and r*c <= 65536 and c % mu == 0
                   for r, c, mu in itertools.product(grid["rows"], grid["cols"], grid["mux_ratio"])):
            raise ValueError("给定网格无法满足整数阵列数与容量约束")
        config.setdefault("device_space", {"pu_width_nm": [60, 90, 120], "pd_width_nm": [90, 135, 180], "pg_width_nm": [90, 135, 180]})
        if set(config["device_space"]) - SRAM_DEVICE:
            raise ValueError("器件级仅调整宽度/外围尺寸，架构、供电和沟道长保持固定")
        for key, values in config["device_space"].items():
            if not isinstance(values, list) or not 1 <= len(values) <= 64:
                raise ValueError("器件搜索域必须为有限非空列表")
            for value in values:
                validate({**plan, "parameters": {**p, key: value}})
        bank_constraints = config.get("bank_constraints", [{**c, "metric": BANK_ALIASES.get(c["metric"], c["metric"])} for c in plan["constraints"]])
        for c in bank_constraints:
            if (set(c) != {"metric", "op", "value"} or c["metric"] not in BANK_METRICS or c["op"] not in ("le", "ge")
                    or type(c["value"]) not in (int, float) or not math.isfinite(c["value"])):
                raise ValueError("bank约束需使用bank估算指标或SNM")
        config["bank_constraints"] = bank_constraints
        config["architecture_objectives"] = objectives(config.get("architecture_objectives", [{"metric": "fom_arch_estimate", "direction": "max"}]), BANK_METRICS)
        config["device_objectives"] = objectives(config.get("device_objectives", [{"metric": "fom_robust_estimate", "direction": "max"}]), BANK_METRICS)
        robust = config.setdefault("robustness", {"cases": [{"corner": p["corner"], "temperature_c": p["temperature_c"], "vdd_v": p["vdd_v"]}], "mc_runs": 5, "vth_std": p["vth_std"]})
        if set(robust) != {"cases", "mc_runs", "vth_std"} or not isinstance(robust["cases"], list) or not 1 <= len(robust["cases"]) <= 15:
            raise ValueError("robustness需列出cases/mc_runs/vth_std，不自动声称覆盖全部PVT")
        integer(robust["mc_runs"], "mc_runs", 2, 50)
        for case in robust["cases"]:
            if set(case) != {"corner", "temperature_c", "vdd_v"}:
                raise ValueError("每个PVT案例需corner/temperature_c/vdd_v")
            validate({**plan, "parameters": {**p, **case, "variation": True, "mc_runs": robust["mc_runs"], "vth_std": robust["vth_std"]}})
        config["physical_evaluation"] = {"bank_model": "openyield_hybrid_v1", **config.get("physical_evaluation", {})}
        if config["physical_evaluation"] != {"bank_model": "openyield_hybrid_v1"}:
            raise ValueError("当前bank评价为单阵列SPICE加OpenYield外围/面积估算，不能指定虚假的全bank实测")
    else:
        if p["bit_width"] not in (8, 16) or p["kind"] != "approx" or p["architecture"] == "tiled":
            raise ValueError("原PEA-GNN仅支持8/16位原生近似压缩树，其它尺寸不能外推")
        p.update(architecture="native", backend="synthesis")
        if any(item["metric"] not in ("mred", "nmed", "error_rate", "pdp_fj", "sta_delay_ns", "sta_dynamic_power_uw", "synthesized_area_um2") for item in plan["constraints"]):
            raise ValueError("乘法器ACCO只验收误差、PDP、STA延迟/动态功耗及综合面积；其它指标没有对应实测入口")
        config.setdefault("architecture_space", {"compressor_types": list(range(1, 9))})
        grid = config["architecture_space"]
        if set(grid) - {"compressor_types", "fixed_assignment"} or "compressor_types" not in grid or not grid["compressor_types"] or any(type(v) is not int or v not in range(1, 9) for v in grid["compressor_types"]):
            raise ValueError("乘法器架构空间需compressor_types，采用助手1–8编号")
        if "fixed_assignment" in grid:
            fixed = grid["fixed_assignment"]
            validate({**plan, "parameters": {**p, "compressor_ids": fixed}})
            if any(v not in grid["compressor_types"] for v in fixed):
                raise ValueError("固定位置分配超出类型域")
        config.setdefault("device_space", {"width_scale": [.8, 1, 1.2]})
        if set(config["device_space"]) != {"width_scale"} or not config["device_space"]["width_scale"] or any(type(v) not in (int, float) or not .5 <= v <= 2 for v in config["device_space"]["width_scale"]):
            raise ValueError("压缩器器件域为width_scale，逐组原始宽度的0.5–2倍；固定逻辑和沟道长")
        config["architecture_objectives"] = objectives(config.get("architecture_objectives", [{"metric": "predicted_pdp_fj", "direction": "min"}, {"metric": "predicted_mred", "direction": "min"}]), PREDICTED)
        config["device_objectives"] = objectives(config.get("device_objectives", [{"metric": "pdp_fj", "direction": "min"}, {"metric": "estimated_area_um2", "direction": "min"}]), {"pdp_fj", "estimated_area_um2", "delay_ns", "power_uw"})
        config["physical_evaluation"] = {"clock_period_ns": 5.0, "output_load_ff": 10.0, **config.get("physical_evaluation", {})}
        if set(config["physical_evaluation"]) != {"clock_period_ns", "output_load_ff"} or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in config["physical_evaluation"].values()):
            raise ValueError("乘法器物理评价需正数clock_period_ns/output_load_ff")
        if set(config) & {"capacity_bits", "output_width", "bank_constraints", "robustness"}:
            raise ValueError("当前乘法器宽度模板为LEVEL_1，仅标称功能/PPA复核；不能承诺尚未接入的PVT或SRAM条件")
    return config


def request_is_acco(text):
    return bool(re.search(r"ACCO|两级|二级|两阶段|论文|bank|总容量|阵列数|架构.*器件|架构.*晶体管", text, re.I))


def from_request(text, plan, budget=64, seconds=7200, solver="llm"):
    from bindings import explicit_parameters
    count = re.search(r"(?:预算|最多|跑|探索)\s*(\d+)\s*(?:个候选|个配置|轮|次)", text)
    if count:
        budget = int(count[1])
    duration = re.search(r"(?:时间预算|限时|最长运行)\s*(\d+)\s*(秒|分钟|小时|s|min|h)", text, re.I)
    if duration:
        seconds = int(duration[1]) * {"秒": 1, "s": 1, "分钟": 60, "min": 60, "小时": 3600, "h": 3600}[duration[2].lower()]
    wf = plan["workflow"]
    config = {"workflow": wf, "plan": plan, "budgets": {"architecture": budget, "shortlist": min(3, budget),
              "validation": min(3, budget), "device_per_parent": min(8, budget), "max_seconds": seconds},
              "solvers": {"architecture": solver, "device": solver}}
    if re.search(r"(?:使用|采用|用)\s*(?:MOEA[-/]?D|SMAC|MOBO)", text, re.I):
        raise NeedsExperiment("当前已接通的求解器是LLM、随机和NSGA-II；其它论文算法尚未接入执行器。")
    algorithms = r"NSGA[- ]?(?:II|2)|随机|random|LLM|DeepSeek"
    def solver_name(value):
        return "nsga2" if value.upper().startswith("NSGA") else "random" if value.lower() in ("random", "随机") else "llm"
    scoped = list(re.finditer(r"(架构|器件)(?:级|阶段)?\s*(?:使用|采用|用|：|:)\s*(" + algorithms + r")", text, re.I))
    remainder = text
    for match in scoped:
        config["solvers"]["architecture" if match[1] == "架构" else "device"] = solver_name(match[2])
    for match in reversed(scoped):
        remainder = remainder[:match.start()] + remainder[match.end():]
    global_choices = {solver_name(m[1]) for m in re.finditer(r"(?:使用|采用|用)\s*(" + algorithms + r")", remainder, re.I)}
    if len(global_choices) > 1:
        raise NeedsExperiment("请明确架构阶段和器件阶段分别使用哪个求解器。")
    if global_choices:
        selected = global_choices.pop()
        for stage in ("architecture", "device"):
            if not any((m[1] == "架构") == (stage == "architecture") for m in scoped):
                config["solvers"][stage] = selected
    facts = explicit_parameters(text, wf)
    if wf == "multiplier" and "compressor_id" in facts:
        config["architecture_space"] = {"compressor_types": [facts["compressor_id"]]}
    objective = plan.get("objective")
    if objective and wf == "sram":
        mapped = BANK_ALIASES.get(objective["metric"], objective["metric"])
        if mapped in BANK_METRICS:
            config["architecture_objectives"] = config["device_objectives"] = [{**objective, "metric": mapped}]
        else:
            raise NeedsExperiment("该单点目标不等于bank目标。两级SRAM可用bank面积/读写功耗延迟、SNM或FOM；单元面积请使用局部任务。")
    if objective and wf == "multiplier":
        mapped = {"synthesized_area_um2": "predicted_area_um2", "mred": "predicted_mred", "nmed": "predicted_nmed", "pdp_fj": "predicted_pdp_fj",
                  "sta_delay_ns": "predicted_delay_ns", "sta_dynamic_power_uw": "predicted_power_uw"}.get(objective["metric"])
        if mapped:
            config["architecture_objectives"] = [{**objective, "metric": mapped}]
        else:
            raise NeedsExperiment("该目标没有GNN对应输出。两级乘法器可用MRED/NMED、PDP、面积或STA延迟/动态功耗目标。")
    if wf == "sram":
        matches = list(re.finditer(r"(?:总容量|容量)\s*(?:为|=|：|:)?\s*(\d+)\s*(KiB|KB|Kb|Kbit|bits|bit|字节|位|B)", text, re.I))
        if not matches:
            raise NeedsExperiment("SRAM两级探索需要明确总容量，例如“总容量4KB”，以及输出字宽；原16×8只是单阵列组织。若只做该单阵列局部调优，可明确说“局部探索”。")
        m = matches[-1]
        unit = m[2]
        factor = 8192 if unit.lower() == "kib" or (unit[-1] == "B" and len(unit) > 1) else 1024 if unit.lower() in ("kb", "kbit") else 8 if unit in ("B", "字节") else 1
        config["capacity_bits"] = int(m[1]) * factor
        widths = list(re.finditer(r"(?:输出字宽|输出位宽|字宽)\s*(?:为|=|：|:)?\s*(\d+)", text))
        if not widths:
            raise NeedsExperiment("SRAM两级探索需要明确输出字宽，例如“输出字宽8位”，以便公平比较bank组织")
        config["output_width"] = int(widths[-1][1])
    try:
        return validate_acco(config)
    except ValueError as exc:
        raise NeedsExperiment(str(exc)) from exc
