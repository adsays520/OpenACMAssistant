"""Bind explicit, unambiguous units and test choices before model planning.

These facts constrain the JSON grammar; they are never silently changed by the LLM.
More complex interpretation remains the model's responsibility.
"""
import re


def explicit_parameters(text, workflow):
    s = text.lower()
    facts = {}
    if workflow in ["sram", "dcim"]:
        shape = re.search(r"(\d+)\s*[×x*]\s*(\d+)", s)
        rows = re.search(r"(\d+)\s*行", s)
        cols = re.search(r"(\d+)\s*列", s)
        if shape:
            facts["rows"] = int(shape[1])
            facts["cols" if workflow == "sram" else "bit_width"] = int(shape[2])
        if rows:
            facts["rows"] = int(rows[1])
        if cols:
            facts["cols" if workflow == "sram" else "bit_width"] = int(cols[1])
    if workflow in ["multiplier", "dcim"]:
        bits = re.search(r"(\d+)\s*(?:位|[- ]?bit)", s)
        if bits:
            facts["bit_width"] = int(bits[1])
        sampled = re.search(r"(?:采样\s*|samples?\s*(?:=|：|:)?\s*)(\d+)\s*(?:组|对|次)?", s)
        if sampled:
            facts.update(samples=int(sampled[1]), exhaustive=False)
        seed = re.search(r"(?:种子|seed)\s*(?:设置为|改为|改成|设为|为|=|：|:)?\s*(\d+)", s)
        if seed:
            facts["seed"] = int(seed[1])
        if "穷举" in s or "exhaustive" in s:
            facts["exhaustive"] = not bool(re.search(r"(?:不要|不做|不|非)\s*穷举|no\s+exhaustive", s))
        if re.search(r"精确乘法器|exact\s+multiplier", s) and "近似" not in s:
            facts["kind"] = "exact"
        elif "对数" in s or "logarithmic" in s:
            facts["kind"] = "log"
        elif "近似乘法器" in s or "approximate multiplier" in s:
            facts["kind"] = "approx"
        if "近似" in s and "sabetz" in s:
            facts.update(kind="approx", compressor_id=8)
        if workflow == "dcim":
            if any(x in s for x in ["全流程", "完整后端", "布局布线", "full flow", "full-flow", "asic"]):
                facts["backend"] = "asic"
            if "仅功能" in s or "只跑功能" in s:
                facts["backend"] = "functional"
            clock = re.search(r"(?:时钟周期|clock(?:[_ ]period)?)\s*(?:为|=|：|:)?\s*(\d+(?:\.\d+)?)\s*ns", s)
            if clock:
                facts["clock_period_ns"] = float(clock[1])
    if workflow in ["sram", "compressor"]:
        volt = re.search(r"(?:vdd|供电|电压)\s*(?:为|设为|改为|设置为|=|：|:)?\s*(\d+(?:\.\d+)?)\s*v(?![a-z])", s)
        if volt:
            facts["vdd_v"] = float(volt[1])
        temp = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:℃|°c|摄氏度)", s)
        if temp:
            facts["temperature_c"] = float(temp[1])
    if workflow == "compressor":
        for name in ["AKbar1", "AKbar2", "antonio", "AP1", "ha", "kong2", "momeni", "sabetz"]:
            if re.search(r"(?<![a-z0-9])" + name.lower() + r"(?![a-z0-9])", s):
                facts["name"] = name
        operation = r"(加宽|增宽|缩窄|减小宽度)\s*(\d+(?:\.\d+)?)\s*[%％]"
        both = re.search(r"(nmos|pmos)\s*(?:和|与|及|、|and|&)\s*(nmos|pmos)\s*(?:都|同时)?\s*" + operation, s)
        if both and both[1] != both[2]:
            scale = 1 + (1 if both[3] in ["加宽", "增宽"] else -1) * float(both[4]) / 100
            facts.update(nmos_scale=scale, pmos_scale=scale)
        for device in ["nmos", "pmos"]:
            match = re.search(device + r"\s*(?:宽度)?\s*(?:都)?\s*" + operation, s)
            if match:
                facts[device + "_scale"] = 1 + (1 if match[1] in ["加宽", "增宽"] else -1) * float(match[2]) / 100
    if workflow == "sram":
        for prefix, field in [("pu", "pu_width_nm"), ("pd", "pd_width_nm"), ("pg", "pg_width_nm"), ("l", "length_nm")]:
            match = re.search(rf"(?<![a-z]){prefix}\s*(?:宽度|长度)?\s*(?:为|=|：|:)?\s*(\d+(?:\.\d+)?)\s*(nm|um|µm|μm)", s)
            if match:
                facts[field] = float(match[1]) * (1 if match[2] == "nm" else 1000)
        corners = re.findall(r"(?<![a-z])(tt|ff|ss|fs|sf)(?![a-z])", s)
        if len(set(corners)) == 1:
            facts["corner"] = corners[0].upper()
        operations = []
        if "读写" in s:
            operations += ["read", "write"]
        elif "只跑读" in s or "只做读" in s:
            operations += ["read"]
        elif "只跑写" in s or "只做写" in s:
            operations += ["write"]
        if "三种snm" in s or "三种 snm" in s:
            operations += ["hold_snm", "read_snm", "write_snm"]
        if operations:
            facts["operations"] = operations
    if workflow == "floating":
        fmt = re.search(r"fp(16|32)",s)
        if fmt:
            facts.update(fp_width=int(fmt[1]),exp_width=5 if fmt[1]=="16" else 8)
        for variant in ["Exact","AC","EAFPU","MMBS5","MMBS6","MMBS7","SSFPM12","SSFPM14","SSFPM16","SSFPM18","SSFPMcorr12","SSFPMcorr14","SSFPMcorr16","SSFPMcorr18"]:
            if re.search(r"(?<![a-z0-9])"+variant.lower()+r"(?![a-z0-9])",s):
                facts["variant"]=variant
        sampled=re.search(r"采样\s*(\d+)",s)
        if sampled:
            facts["samples"]=int(sampled[1])
    if workflow in ["multiplier","floating","dcim"] and any(x in s for x in ["综合面积","综合评估","synthesis"]):
        facts["backend"]="synthesis"
    if workflow == "dcim":
        if "标准单元" in s or "standard_cells" in s:
            facts["memory_impl"]="standard_cells"
        elif "匹配宏" in s:
            facts["memory_impl"]="macro"
    return facts


def explicit_constraints(text, workflow):
    mappings = {
        "sram": [(r"读延迟|read[_ ]delay", "read_delay_ns", "time"),
                 (r"写延迟|write[_ ]delay", "write_delay_ns", "time"),
                 (r"(?:平均)?读功耗|read[_ ]power", "read_power_uw", "power"),
                 (r"(?:平均)?写功耗|write[_ ]power", "write_power_uw", "power"),
                 (r"保持\s*snm|hold[_ ]snm", "hold_snm_v", "voltage"),
                 (r"读\s*snm|read[_ ]snm", "read_snm_v", "voltage"),
                 (r"写\s*snm|write[_ ]snm", "write_snm_v", "voltage")],
        "multiplier": [(r"mred", "mred", "ratio"), (r"nmed", "nmed", "ratio"),
                       (r"pdp|功耗延迟积", "pdp_fj", "energy")],
        "floating": [(r"mred", "mred", "ratio"),(r"特殊值错误数", "special_mismatch_count", "ratio")],
        "dcim": [(r"mred", "mred", "ratio"), (r"nmed", "nmed", "ratio"),
                 (r"setup[_ ]slack|setup[_ ]wns", "setup_wns_ns", "time"),
                 (r"hold[_ ]slack|hold[_ ]wns", "hold_wns_ns", "time"),
                 (r"(?:布线)?drc", "route_drc_count", "ratio"),
                 (r"后端功耗|routed[_ ]power", "routed_power_uw", "power")],
        "compressor": [(r"延迟|delay", "delay_ns", "time"), (r"(?:平均)?功耗|power", "power_uw", "power")],
    }
    units = {"time":{"ns":1,"ps":0.001,"us":1000,"µs":1000,"μs":1000,"纳秒":1},
             "power":{"uw":1,"µw":1,"μw":1,"mw":1000,"微瓦":1},
             "voltage":{"v":1,"mv":0.001,"伏":1}, "ratio":{"":1,"%":0.01},
             "energy":{"fj":1,"pj":1000,"飞焦":1}}
    operator = r"(不超过|不高于|小于等于|<=|≤|不低于|至少|大于等于|>=|≥)"
    found = []
    lowered = text.lower()
    for names, metric, family in mappings.get(workflow, []):
        choices = sorted(units[family], key=len, reverse=True)
        unit_pattern = "|".join(re.escape(x) for x in choices)
        expression = rf"(?:{names})\s*{operator}\s*(\d+(?:\.\d+)?)\s*({unit_pattern})"
        for match in re.finditer(expression, lowered):
            op, value, unit = match.groups()
            condition = {"metric":metric,"op":"ge" if op in ["不低于","至少","大于等于",">=","≥"] else "le",
                         "value":float(value)*units[family][unit]}
            if condition not in found:
                found.append(condition)
    return found
