"""Contracts and explicit language bindings for matched architecture experiments."""
import copy
import random
import re

from acco_spec import NeedsExperiment, integer, validate_acco
from bindings import explicit_constraints, explicit_parameters
from spec import compressor_count, validate

METHODS = ("random", "nsga2", "llm")
SCOPE = ("支持原生8/16位近似乘法器的架构搜索对照：随机、原NSGA-II、LLM共享冻结候选池及基线，"
         "搜索种子与算术测试种子分开，每轮入选项按相同RTL/综合/STA条件复核。"
         "该对照固定器件尺寸，不执行器件优化；经验开关消融尚未接入。")


def requested_methods(text):
    """Bind method lists, without treating the compared design level as a list.

    Read every declaration so a later conversational correction can supersede
    the earlier list. A phrase like 只比较架构选点 is a scope restriction.
    """
    declarations = re.finditer(
        r"(?=(?P<label>方法组|对照方法|比较方法|方法为|只比较|只对比|只用|仅比较|仅对比|"
        r"分别(?:使用|采用|用)|使用|采用)\s*(?:是|为|：|:)?(?P<value>[^。；;]{1,240}))", text, re.I)
    token = re.compile(
        r"(?:原\s*)?(?:(?P<random>随机(?!\s*(?:种子|输入|采样|初始化|顺序))\s*(?:搜\s*索)?|random(?:\s+search)?(?![a-z_]))|"
        r"(?P<nsga2>NSGA\s*[-–—－]?\s*(?:II|Ⅱ|2)(?![a-z0-9_]))|"
        r"(?P<llm>LLM(?![a-z0-9_])|DeepSeek(?![a-z0-9_])))", re.I)
    unsupported = re.compile(r"(?:MOBO|MOEA[-/]?D|SMAC|TPE)(?![a-z0-9_])|贝叶斯优化", re.I)
    separator = re.compile(r"\s*(?:、|,|，|/|以及|和|与|及|and\b|vs\.?\b)\s*", re.I)
    selected = None
    for declaration in declarations:
        value = declaration["value"].strip()
        value = re.sub(r"^(?:以下)?(?:三种|两种|三个|两个)?(?:方法|算法|策略)?\s*[:：]?\s*", "", value)
        first = token.match(value)
        labelled = declaration["label"] in ("方法组", "对照方法", "比较方法", "方法为")
        if not first:
            if labelled or unsupported.match(value):
                raise NeedsExperiment("架构搜索方法支持随机搜索、NSGA-II和LLM；请明确选择其中至少两种。")
            continue
        methods, match = set(), first
        while match:
            methods.add(match.lastgroup)
            sep = separator.match(value, match.end())
            if not sep:
                break
            if unsupported.match(value, sep.end()):
                raise NeedsExperiment("架构搜索方法支持随机搜索、NSGA-II和LLM；列表中包含尚未接入的方法。")
            match = token.match(value, sep.end())
        selected = [m for m in METHODS if m in methods]
    return selected


def validate_benchmark(raw):
    allowed = {"kind", "schema_version", "experiment", "methods", "search_seeds", "candidate_budget", "pool_seed",
               "pool_size", "validation_top_k", "trial_max_seconds", "max_seconds", "request"}
    if not isinstance(raw, dict) or set(raw) - allowed or raw.get("kind") != "search_comparison" or raw.get("schema_version", 1) != 1:
        raise ValueError("搜索对照格式无效或含未知字段")
    c = copy.deepcopy(raw)
    c["schema_version"] = 1
    c["experiment"] = validate_acco(c["experiment"])
    e = c["experiment"]
    if e["workflow"] != "multiplier":
        raise ValueError("当前公平对照入口仅支持原生8/16位近似乘法器架构")
    if e["plan"].get("objective") not in (None, {"metric": "pdp_fj", "direction": "min"}):
        raise ValueError("本版对照目标为在约束下最小化PDP，不能忽略其它明确目标")
    e["plan"]["objective"] = {"metric": "pdp_fj", "direction": "min"}
    e["architecture_objectives"] = [{"metric": "predicted_pdp_fj", "direction": "min"}]
    c.setdefault("methods", list(METHODS))
    if (not isinstance(c["methods"], list) or len(c["methods"]) < 2 or len(set(c["methods"])) != len(c["methods"])
            or any(m not in METHODS for m in c["methods"])):
        raise ValueError("至少选择两个不同方法：random/nsga2/llm")
    c.setdefault("search_seeds", [1, 2, 3, 4, 5])
    if not isinstance(c["search_seeds"], list) or not 1 <= len(c["search_seeds"]) <= 20:
        raise ValueError("搜索重复需1–20个独立种子")
    for s in c["search_seeds"]:
        integer(s, "search_seed", 0, 2147483647)
    if len(set(c["search_seeds"])) != len(c["search_seeds"]):
        raise ValueError("重复实验的搜索种子不能重复")
    for k, default, lo, hi in [("candidate_budget", 32, 2, 10000), ("pool_seed", 1729, 0, 2147483647),
                              ("validation_top_k", min(3, c.get("candidate_budget", 32)), 1, 100), ("trial_max_seconds", 1800, 10, 86400),
                              ("max_seconds", 7200, 10, 604800)]:
        c[k] = integer(c.get(k, default), k, lo, hi)
    c["pool_size"] = integer(c.get("pool_size", max(256, c["candidate_budget"] * 8)), "pool_size", 2, 100000)
    if c["candidate_budget"] > c["pool_size"] or c["validation_top_k"] > c["candidate_budget"]:
        raise ValueError("候选池必须覆盖单轮预算，复核数不能大于架构预算")
    grid = e["architecture_space"]
    n = compressor_count(e["plan"]["parameters"]["bit_width"], "native")
    if "fixed_assignment" in grid or len(grid["compressor_types"]) ** n < c["pool_size"]:
        raise ValueError("固定/过小架构域不足以构建指定大小的共同候选池")
    return c


def from_request(text, previous=None, draft=None, require_budget=True):
    if re.search(r"消融|(?:有|无|开启|关闭|开关).{0,4}经验", text):
        raise NeedsExperiment("经验开关消融尚未接入，暂时不能直接运行这组实验；需要先支持冻结经验快照和逐轮注入审计。我会保留你给出的条件，不替换成普通搜索。")
    c = copy.deepcopy(draft) if draft else {"kind": "search_comparison"}
    e = c.get("experiment", {})
    baseline = e.get("plan") or previous
    if baseline and baseline.get("workflow") != "multiplier":
        raise NeedsExperiment("搜索对照当前支持8/16位近似乘法器；不能将当前其它电路自动转换。")
    if not baseline and not re.search(r"乘法器|multiplier", text, re.I):
        raise NeedsExperiment("请先明确对照对象，例如“8位近似乘法器架构搜索对照”。已给评价条件会保留。")
    p = {"bit_width": 8, "kind": "approx", "compressor_id": 7, "samples": 4096, "seed": 42,
         "backend": "synthesis", "architecture": "native", **(baseline or {}).get("parameters", {})}
    facts = explicit_parameters(text, "multiplier")
    facts.pop("seed", None)
    p.update(facts)
    # Prefer explicitly scoped test seeds over any earlier search seed mention.
    test = list(re.finditer(r"(?:算术测试|测试输入|测试|评价输入|评价|算术)[^，。；;]{0,18}?(?:种子|seed)\s*(?:为|=|：|:)?\s*(\d+)", text, re.I))
    generic = re.search(r"(?:固定种子|种子|seed)\s*(?:为|=|：|:)?\s*(\d+)", text, re.I)
    if test:
        p["seed"] = int(test[-1][1])
    elif generic and not re.search(r"搜索(?:随机)?种子\s*(?:为|=|：|:)?\s*\d+", text):
        p["seed"] = int(generic[1])
    limits = explicit_constraints(text, "multiplier") or (baseline or {}).get("constraints", [])
    objective = (baseline or {}).get("objective")
    if re.search(r"(?:最小|最低|降低|减小|优化|目标)[^，。；]{0,12}(?:面积|延迟|功耗)|(?:面积|延迟|功耗)[^，。；]{0,8}(?:最小|最低)", text):
        raise NeedsExperiment("本版搜索对照优化PDP。其它明确目标需要对应对照协议，不能自动替换为PDP。")
    plan = validate({"workflow": "multiplier", "parameters": p, "constraints": limits, "objective": objective})
    physical = {**e.get("physical_evaluation", {})}
    for key, pattern in [("clock_period_ns", r"(\d+(?:\.\d+)?)\s*ns\s*(?:输入活动周期|活动周期|输入周期)"),
                         ("output_load_ff", r"(\d+(?:\.\d+)?)\s*fF\s*(?:输出负载|负载)")]:
        matches = list(re.finditer(pattern, text, re.I))
        if matches:
            physical[key] = float(matches[-1][1])
    period = re.search(r"(?:输入活动周期|活动周期)\s*(?:为|=|：|:)?\s*(\d+(?:\.\d+)?)\s*ns", text, re.I)
    load = re.search(r"输出负载\s*(?:为|=|：|:)?\s*(\d+(?:\.\d+)?)\s*fF", text, re.I)
    if period:
        physical["clock_period_ns"] = float(period[1])
    if load:
        physical["output_load_ff"] = float(load[1])
    if re.search(r"(?:使用|采用|换成|综合库)[^，。；]{0,12}(?:ASAP|sky130|GF180|其它库|其他库)", text, re.I):
        raise NeedsExperiment("本版实测仅支持Nangate45 typical，不能承诺尚未配置的综合库。")
    e.update(workflow="multiplier", plan=plan, physical_evaluation=physical)
    c["experiment"] = e
    methods = requested_methods(text)
    if methods is not None:
        c["methods"] = methods
    budget = list(re.finditer(r"(?:预算|最多(?:评价|评估)?|评价|评估)\s*(\d+)\s*个(?:不同|新|唯一)?(?:架构|候选|配置)", text))
    repeat = list(re.finditer(r"重复\s*(\d+)\s*次", text))
    if budget:
        c["candidate_budget"] = int(budget[-1][1])
    if repeat:
        c["search_seeds"] = list(range(1, int(repeat[-1][1]) + 1))
    seeds = list(re.finditer(r"搜索(?:随机)?种子\s*(?:分别为|采用|使用|为|=|：|:)?\s*\[?([\d,，、 ]+)", text))
    if seeds:
        c["search_seeds"] = [int(v) for v in re.findall(r"\d+", seeds[-1][1])]
        if repeat and len(c["search_seeds"]) != int(repeat[-1][1]):
            raise NeedsExperiment("搜索种子个数与重复次数不一致，请统一。")
    for key, pattern in [("pool_seed", r"候选池种子\s*(?:为|=|：|:)?\s*(\d+)"),
                         ("pool_size", r"候选池(?:大小)?\s*(?:为|=|：|:)?\s*(\d+)"),
                         ("validation_top_k", r"(?:前|top\s*)(\d+)\s*个(?:候选)?(?:做|进行|实测|复核)")]:
        match = re.search(pattern, text, re.I)
        if match:
            c[key] = int(match[1])
    for key, prefix in [("max_seconds", r"(?:总时间预算|整体限时|总限时)"),
                        ("trial_max_seconds", r"(?:每轮|每次)(?:时间预算|限时)")]:
        duration = re.search(prefix + r"\s*(\d+)\s*(秒|分钟|小时)", text)
        if duration:
            c[key] = int(duration[1]) * {"秒": 1, "分钟": 60, "小时": 3600}[duration[2]]
    # An initial one-line request is collected, rather than launching default 15 trials.
    if require_budget and ("candidate_budget" not in c or "search_seeds" not in c):
        if "candidate_budget" not in c and "search_seeds" not in c:
            question = "每轮最多评价多少个不同架构，每种方法重复几次？"
        elif "candidate_budget" not in c:
            question = "每轮最多评价多少个不同架构？"
        else:
            question = "每种方法重复几次，或使用哪些搜索种子？"
        raise NeedsExperiment(question + " 默认比较随机、NSGA-II和DeepSeek，以PDP最小为目标。你也可以指定方法和误差上限。")
    c["request"] = text
    try:
        return validate_benchmark(c)
    except (ValueError, TypeError, KeyError) as exc:
        raise NeedsExperiment(str(exc)) from exc


def candidate_pool(c):
    c = validate_benchmark(c)
    p, grid = c["experiment"]["plan"]["parameters"], c["experiment"]["architecture_space"]
    n = compressor_count(p["bit_width"], "native")
    types = sorted(set(grid["compressor_types"]))
    base = p.get("compressor_ids", [p["compressor_id"]] * n)
    if any(t not in types for t in base):
        raise ValueError("基线超出共同候选域")
    pool, seen = [], set()
    def add(ids):
        token = tuple(ids)
        if token not in seen and len(pool) < c["pool_size"]:
            seen.add(token)
            pool.append({"compressor_ids": list(ids)})
    add(base)
    for t in types:
        add([t] * n)
    for i in range(n):
        for t in types:
            ids = base.copy()
            ids[i] = t
            add(ids)
    rng = random.Random(c["pool_seed"])
    for _ in range(c["pool_size"] * 100):
        if len(pool) == c["pool_size"]:
            return pool
        add([rng.choice(types) for _ in range(n)])
    raise ValueError("未能生成指定数量的唯一候选")
