"""Typed, bounded plans. Model output is data and never a shell command."""
import copy
import math

COMPRESSORS = ["AKbar1", "AKbar2", "antonio", "AP1", "ha", "kong2", "momeni", "sabetz"]
OPERATIONS = ["read", "write", "hold_snm", "read_snm", "write_snm"]
METRICS = ["read_delay_ns", "write_delay_ns", "read_power_uw", "write_power_uw",
           "hold_snm_v", "read_snm_v", "write_snm_v", "cell_area_um2", "delay_ns",
           "power_uw", "gate_area_um2", "estimated_area_um2", "mred", "nmed", "error_rate",
           "setup_wns_ns", "hold_wns_ns", "routed_area_um2", "routed_power_uw", "route_drc_count",
           "synthesized_area_um2", "special_mismatch_count"]
DEFAULTS = {
    "sram": {"rows": 16, "cols": 8, "vdd_v": 1.0, "temperature_c": 27.0,
             "corner": "TT", "operations": ["read", "write"], "mc_runs": 1,
             "variation": False, "vth_std": 0.05, "seed": 42,
             "pu_width_nm": 90.0, "pd_width_nm": 90.0, "pg_width_nm": 135.0,
             "length_nm": 50.0, "pu_model": "PMOS_VTG", "pd_model": "NMOS_VTG",
             "pg_model": "NMOS_VTG", "wire_rc": True, "wire_res_ohm": 100.0,
             "wire_cap_pf": 0.001, "column_mux": False, "full_array": True,
             **{name + "_scale": 1.0 for name in ["decoder", "wordline", "precharge", "senseamp", "write_driver", "mux"]}},
    "multiplier": {"bit_width": 8, "kind": "exact", "compressor_id": 1,
                   "samples": 4096, "exhaustive": False, "seed": 42, "backend": "functional", "architecture": "auto"},
    "compressor": {"name": "sabetz", "vdd_v": 1.2, "temperature_c": 27.0,
                   "nmos_scale": 1.0, "pmos_scale": 1.0, "length_scale": 1.0,
                   "load_scale": 1.0},
    "dcim": {"bit_width": 8, "rows": 16, "kind": "exact", "compressor_id": 1,
             "samples": 4096, "exhaustive": False, "seed": 42,
             "backend": "functional", "clock_period_ns": 10.0, "memory_impl": "auto", "architecture": "auto"},
    "floating": {"variant": "Exact", "fp_width": 32, "exp_width": 8, "mode": 2,
                 "mantissa_keep": 4, "samples": 4096, "seed": 42, "backend": "functional"},
}
LIMITS = {"rows": (2, 4096), "cols": (1, 4096), "vdd_v": (0.3, 1.8),
          "temperature_c": (-40, 125), "mc_runs": (1, 50), "vth_std": (0.0, 0.2),
          "pu_width_nm": (30, 1000), "pd_width_nm": (30, 1000),
          "pg_width_nm": (30, 1000), "length_nm": (30, 150),
          "wire_res_ohm": (0, 10000), "wire_cap_pf": (0, 10),
          "bit_width": (4, 64), "samples": (16, 65536), "seed": (0, 2147483647),
          "compressor_id": (1, 8), "nmos_scale": (0.25, 4), "pmos_scale": (0.25, 4),
          "length_scale": (0.6, 2), "load_scale": (0.1, 10), "clock_period_ns": (2, 1000),
          "fp_width": (8, 32), "exp_width": (3, 8), "mode": (0, 2), "mantissa_keep": (3, 6),
          **{name + "_scale": (0.25, 3) for name in ["decoder", "wordline", "precharge", "senseamp", "write_driver", "mux"]}}
INTEGERS = {"rows", "cols", "mc_runs", "bit_width", "samples", "seed", "compressor_id", "fp_width", "exp_width", "mode", "mantissa_keep"}
BOOLEAN = {"variation", "wire_rc", "column_mux", "full_array", "exhaustive"}
ENUMS = {"corner": ["TT", "FF", "SS", "FS", "SF"], "kind": ["exact", "approx", "log"],
         "backend": ["functional", "synthesis", "asic"], "memory_impl": ["auto", "macro", "standard_cells"],
         "variant": ["Exact", "AC", "EAFPU", "MMBS5", "MMBS6", "MMBS7", "SSFPM12", "SSFPM14", "SSFPM16", "SSFPM18", "SSFPMcorr12", "SSFPMcorr14", "SSFPMcorr16", "SSFPMcorr18"],
         "architecture": ["auto", "native", "tiled"],
         "name": COMPRESSORS, "pu_model": ["PMOS_VTL", "PMOS_VTG", "PMOS_VTH"],
         "pd_model": ["NMOS_VTL", "NMOS_VTG", "NMOS_VTH"],
         "pg_model": ["NMOS_VTL", "NMOS_VTG", "NMOS_VTH"]}


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def native_compressor_count(bits):
    # Matches the unchanged generator's _required_compressor_count for 4–64bit.
    return (bits - 2) ** 2 // 4


def uses_tiles(bits,kind="approx",architecture="auto"):
    if kind=="log":
        return False
    return architecture=="tiled" or (architecture=="auto" and (bits==4 or bits>32 or (kind=="exact" and bits%2==1 and bits not in [5,9,17])))


def compressor_count(bits,architecture="auto"):
    return 9*((bits+7)//8)**2 if uses_tiles(bits,"approx",architecture) else native_compressor_count(bits)


def validate(plan):
    if not isinstance(plan, dict):
        raise ValueError("计划必须为 JSON 对象")
    unknown = set(plan) - {"intent", "workflow", "parameters", "constraints", "objective", "sweep", "message", "assumptions"}
    if unknown:
        raise ValueError(f"不接受执行命令、任意路径或未知字段：{sorted(unknown)}")
    intent = plan.get("intent", "run")
    if intent not in ["run", "clarify", "explain"]:
        raise ValueError("intent 无效")
    if intent != "run":
        return {"intent": intent, "message": str(plan.get("message", "请补充电路类型和需求"))[:3000]}
    workflow = plan.get("workflow")
    if workflow not in DEFAULTS:
        raise ValueError("workflow 需为 sram、multiplier、compressor 或 dcim")
    supplied = plan.get("parameters", {})
    if not isinstance(supplied, dict):
        raise ValueError("parameters 必须为对象")
    allowed = set(DEFAULTS[workflow]) | ({"compressor_ids"} if workflow in ["multiplier", "dcim"] else set())
    if set(supplied) - allowed:
        raise ValueError(f"{workflow} 不支持参数：{sorted(set(supplied) - allowed)}")
    params = copy.deepcopy(DEFAULTS[workflow])
    params.update(supplied)
    for key, value in params.items():
        if key in LIMITS:
            low, high = LIMITS[key]
            if not number(value) or not low <= value <= high:
                raise ValueError(f"{key} 必须在 [{low}, {high}] 内且使用字段所示单位")
            if key in INTEGERS and type(value) is not int:
                raise ValueError(f"{key} 必须为整数")
        if key in BOOLEAN and type(value) is not bool:
            raise ValueError(f"{key} 必须为布尔值")
        if key in ENUMS and value not in ENUMS[key]:
            raise ValueError(f"{key} 需为 {ENUMS[key]}")
    if workflow == "sram":
        if params["rows"] & (params["rows"] - 1):
            raise ValueError("SRAM 行数需为 2 的幂")
        if not params["operations"] or not isinstance(params["operations"], list) or any(x not in OPERATIONS for x in params["operations"]):
            raise ValueError(f"operations 需为 {OPERATIONS} 的非空子集")
        if len(params["operations"]) != len(set(params["operations"])):
            raise ValueError("operations 不允许重复")
        if params["rows"] * params["cols"] > 65536:
            raise ValueError("单次SPICE资源上限65536 bit，超出时请分银行建模")
        if params["cols"] == 1 and params["wire_rc"]:
            raise ValueError("内置列RC含(cols−1)分母；单列阵列需明确wire_rc=false")
        if params["column_mux"] and params["cols"] % 2:
            raise ValueError("内置列复用为2路，启用时列数必须为偶数")
        if not params["variation"] and params["mc_runs"] != 1:
            raise ValueError("mc_runs > 1 时必须 variation=true，不能将重复确定性结果称为 Monte Carlo")
    if workflow in ["multiplier", "dcim"]:
        if params["exhaustive"] and params["bit_width"] > 8:
            raise ValueError("穷举只支持 4–8 bit；更大位宽使用固定种子的采样")
        ids = params.get("compressor_ids")
        if ids is not None and (not isinstance(ids, list) or any(type(x) is not int or x not in range(1, 9) for x in ids)):
            raise ValueError("compressor_ids 必须为 1–8 的整数列表")
        count=compressor_count(params["bit_width"],params["architecture"])
        if ids is not None and len(ids) != count:
            raise ValueError(f"{params['bit_width']}位/{params['architecture']}需恰好{count}个压缩器ID")
        if params["kind"]=="log" and params["bit_width"] not in [8,16,32]:
            raise ValueError("原始对数架构只开放经测试的8/16/32位；其它位宽可用exact/approx分块架构")
        if workflow == "dcim" and params["rows"] & (params["rows"] - 1):
            raise ValueError("DCiM SRAM 行数需为 2 的幂")
        if workflow == "dcim" and params["rows"] * params["bit_width"] > 131072:
            raise ValueError("DCiM单任务存储容量资源上限131072 bit")
        if workflow == "dcim" and params["memory_impl"] == "macro" and (params["rows"], params["bit_width"]) not in [(16, 8), (32, 16), (64, 32)]:
            raise ValueError("匹配宏库只有16×8、32×16、64×32；其它尺寸可选择standard_cells")
        if workflow == "multiplier" and params["backend"] == "asic":
            raise ValueError("独立乘法器支持functional或synthesis；完整ASIC流程通过DCiM执行")
    if workflow == "floating":
        if params["fp_width"] - params["exp_width"] - 1 < 2:
            raise ValueError("浮点格式需要至少2位尾数")
        if params["variant"] == "AC" and (params["fp_width"] < 16 or params["exp_width"] < 5 or params["mantissa_keep"] > params["fp_width"]-params["exp_width"]-1):
            raise ValueError("AC支持总宽度16–32、指数5–8，截取长度不能超过尾数长度")
        if params["variant"] not in ["Exact", "AC"] and (params["fp_width"], params["exp_width"]) != (32, 8):
            raise ValueError("EAFPU/MMBS/SSFPM源码是固定FP32，不能伪称支持任意格式")
        if params["backend"] == "asic":
            raise ValueError("独立浮点乘法器支持functional或synthesis")
    constraints = plan.get("constraints", [])
    if not isinstance(constraints, list) or len(constraints) > 16:
        raise ValueError("constraints 需为至多 16 项的列表")
    for c in constraints:
        if not isinstance(c, dict) or set(c) != {"metric", "op", "value"} or c["metric"] not in METRICS or c["op"] not in ["le", "ge"] or not number(c["value"]):
            raise ValueError("约束格式：{metric: 带单位的指标, op: le/ge, value: 有限数值}")
    objective = plan.get("objective")
    if objective is not None and (not isinstance(objective, dict) or set(objective) != {"metric", "direction"} or objective["metric"] not in METRICS or objective["direction"] not in ["min", "max"]):
        raise ValueError("objective 格式：{metric, direction: min/max}")
    sweep = plan.get("sweep")
    if sweep is not None:
        if not isinstance(sweep, dict) or set(sweep) != {"parameter", "values"} or sweep["parameter"] not in params:
            raise ValueError("sweep 需指定一个已支持的设计参数")
        if not isinstance(sweep["values"], list) or not 1 <= len(sweep["values"]) <= 8:
            raise ValueError("一次扫描需为 1–8 个候选")
        for value in sweep["values"]:
            candidate = copy.deepcopy(plan)
            candidate.pop("sweep", None)
            candidate["parameters"] = dict(params, **{sweep["parameter"]: value})
            validate(candidate)
    return {"intent": "run", "workflow": workflow, "parameters": params,
            "constraints": constraints, "objective": objective, "sweep": sweep,
            "message": str(plan.get("message", ""))[:3000],
            "assumptions": [f"{k}={params[k]}（使用默认值）" for k in params if k not in supplied]}


PARAM_PROPERTIES = {k: {"type": "integer" if k in INTEGERS else "number", "minimum": v[0], "maximum": v[1]} for k, v in LIMITS.items()}
PARAM_PROPERTIES.update({k: {"type": "boolean"} for k in BOOLEAN})
PARAM_PROPERTIES.update({k: {"type": "string", "enum": v} for k, v in ENUMS.items()})
PARAM_PROPERTIES["operations"] = {"type": "array", "minItems": 1, "maxItems": 5, "uniqueItems": True, "items": {"type": "string", "enum": OPERATIONS}}
PARAM_PROPERTIES["compressor_ids"] = {"type": "array", "minItems": 1, "maxItems": 961, "items": {"type": "integer", "minimum": 1, "maximum": 8}}
SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {
              "intent": {"type": "string", "enum": ["run", "clarify", "explain"]},
              "workflow": {"type": "string", "enum": list(DEFAULTS)},
              "parameters": {"type": "object", "properties": PARAM_PROPERTIES, "additionalProperties": False},
              "constraints": {"type": "array", "maxItems": 16, "items": {"type": "object", "additionalProperties": False, "properties": {"metric": {"type": "string", "enum": METRICS}, "op": {"type": "string", "enum": ["le", "ge"]}, "value": {"type": "number"}}, "required": ["metric", "op", "value"]}},
              "objective": {"type": "object", "properties": {"metric": {"type": "string", "enum": METRICS}, "direction": {"type": "string", "enum": ["min", "max"]}}, "required": ["metric", "direction"], "additionalProperties": False},
              "sweep": {"type": "object", "properties": {"parameter": {"type": "string", "enum": list(PARAM_PROPERTIES)}, "values": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": ["number", "string", "boolean"]}}}, "required": ["parameter", "values"], "additionalProperties": False},
              "message": {"type": "string", "maxLength": 512}}, "required": ["intent", "workflow", "parameters", "constraints", "message"]}
