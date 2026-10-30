"""One contextual dialogue; validated tool operations are an internal detail."""
import copy
from datetime import datetime
import json
from pathlib import Path
import re
import urllib.request
import uuid
from zoneinfo import ZoneInfo

from spec import DEFAULTS, LIMITS, validate
from result_summary import value_text


def conversational_message(message):
    """Model deliberation is audit material; only the reply belongs in chat."""
    message = re.sub(r"<(think|analysis)>.*?</\1>", "", message, flags=re.S | re.I).strip()
    message = re.sub(r"<(?:think|analysis)>.*$", "", message, flags=re.S | re.I).strip()
    message = re.sub(r"^(?:已识别|识别为|正在理解|正在整理需求|意图识别|需求解析)[^。！？\n]*(?:[。！？\n]|$)", "", message).strip()
    return message


def workflow(text):
    text = text.lower()
    for name, words in [("floating", ["浮点", "fp16", "fp32", "floating"]),
                        ("dcim", ["dcim", "存内计算"]), ("multiplier", ["乘法器", "multiplier"]),
                        ("sram", ["sram", "6t", "存储阵列"]),
                        ("compressor", ["压缩器", "compressor", "sabetz", "akbar", "kong2", "momeni", "antonio"])]:
        if any(word in text for word in words):
            return name
    # Bank capacity/bitcells identify SRAM when no explicit workflow is named.
    if (re.search(r"子阵列|bitcell|读写.{0,20}snm|hold/read/write\s*snm", text)
            or (re.search(r"总容量|输出字宽|列复用比", text) and re.search(r"\bpu\b|\bpd\b|\bpg\b|snm|子阵列", text))):
        return "sram"
    return None


def requirements_update(text):
    """Declarative specifications also matter without an imperative or circuit name."""
    if re.search(r"怎么|如何|为什么|为何|什么|什麽|哪些|区别|是否|能否|能不能|[？?]|(?:只|先)(?:问|解释|介绍)", text, re.I):
        return False
    bindings = [r"(?:种子|seed)\s*(?:固定为|设置为|为|=|：|:)?\s*\d+",
                r"采样\s*\d+\s*(?:组|对)?", r"重复\s*\d+\s*次",
                r"(?:预算|评价|评估)\s*\d+\s*个(?:不同)?(?:架构|候选|配置)",
                r"\d+(?:\.\d+)?\s*(?:ns|fF)\s*(?:输入活动周期|活动周期|输出负载)",
                r"(?:MRED|NMED|SNM|功耗|延迟)\s*(?:≤|≥|<=|>=|不超过|不低于)\s*[\d.]",
                r"(?:改为|改成|固定为|设置为)\s*\d+"]
    return any(re.search(pattern, text, re.I) for pattern in bindings)


def comparison_conditions(text):
    return requirements_update(text) and bool(re.search(
        r"所有方法|每种方法|搜索(?:随机)?种子|不同架构|共同候选池|相同.*候选池", text))


def requirements_receipt(text):
    parts = []
    for pattern, label, suffix in [
        (r"(?:算术测试|测试输入|测试|评价)[^，。；]{0,18}?(?:种子|seed)\s*(?:为|=|：|:)?\s*(\d+)", "测试种子", ""),
        (r"采样\s*(\d+)\s*(?:组|对)?", "采样", "组"),
        (r"(\d+(?:\.\d+)?)\s*ns\s*(?:输入活动周期|活动周期)", "输入活动周期", "ns"),
        (r"(\d+(?:\.\d+)?)\s*fF\s*输出负载", "输出负载", "fF"),
        (r"重复\s*(\d+)\s*次", "每种方法重复", "次"),
        (r"(?:预算|评价|评估)\s*(\d+)\s*个(?:不同)?(?:架构|候选|配置)", "每轮最多", "个不同架构")]:
        matches = list(re.finditer(pattern, text, re.I))
        if matches:
            parts.append(label + matches[-1][1] + suffix)
    if re.search(r"搜索(?:随机)?种子.*测试.*种子.*分开", text):
        parts.append("搜索种子与测试种子分开")
    return "可以，先按这些条件：" + "、".join(parts) + "。" if parts else "可以，我会保留你给出的这些条件。"


def execution_requested(text):
    """Recognize explicit tool requests inside a mixed explanation/action turn."""
    if re.search(r"(?:不要|先不|暂不|暂时不)(?:跑|运行|仿真|执行|开展|进行|启动|发起)", text):
        return False
    verb = r"(?:复测|重测|重新跑|开始(?:运行|仿真|执行|探索)|运行|仿真|执行|探索|跑)"
    # These verbs request work only when paired with a design/EDA task. A bare
    # topic such as “搜索是什么” or “进行过哪些实验” is still a question.
    task = r"(?:架构搜索|设计空间搜索|架构寻优|参数搜索|探索|优化|仿真|复测|实验|试验)"
    initiation = r"(?:开展|进行|启动|发起|开始|新建)(?!过|了)"
    if (re.match(r"(?:基于|参考)[^。！？?]{0,90}新建[^。！？?]{0,25}(?:实验|复验)", text.strip())
            and not re.search(r"怎么|如何|为什么|是否|[？?]", text)):
        return True
    prefix = r"(?:请\s*)?(?:帮我\s*)?(?:现在\s*)?"
    if (re.match(prefix + initiation + r"[^？?。；]{0,80}" + task, text.strip())
            or re.search(r"(?:然后|接着|最后|并且|并|请|帮我)\s*" + initiation + r"[^？?。；]{0,80}" + task, text)):
        clause = re.split(r"[，,。；;]|然后|接着|并(?:解释|说明|分析)", text.strip(), maxsplit=1)[0]
        if not re.search(r"怎么|如何|是否|能否|能不能|什么|什麽|哪些|为什么|可以.*[吗么]|[？?]", clause):
            return True
    return bool(re.search(r"(?:然后|接着|最后|那就|帮我|请|直接|立即|现在|并)\s*(?:按[^，。；]{0,30}?(?:配置|方案)[，, ]*)?" + verb, text))


def tool_requested(text, fallback_mode, pending=None):
    if execution_requested(text) or fallback_mode == "plan":
        return True
    if fallback_mode != "action":
        return False
    # Mentioning a past operation is not a new request. These are request forms,
    # not the topic keywords used for offline explanations.
    return bool(pending or re.match(r"(?:好[，, ]*)?(?:请)?(?:帮我)?(?:做|生成|跑|运行|执行|仿真|探索|优化|复测|重测|开始|继续|重新|再跑|其余不变|只改|只把|仅改|需要|想要|我(?:需要|想要))", text.strip())
                or re.match(r"(?:ACCO|两级|二级|两阶段)\s*(?:探索|优化)", text.strip(), re.I)
                or (re.match(r"按(?:刚才|上次|之前|最佳|最优|这个)", text.strip()) and re.search(r"复测|重测|重新跑|运行", text))
                or (workflow(text) and re.match(r"(?:\d|精确|近似|SRAM|DCiM|sram|dcim|FP\d)", text.strip())))


def route(text, previous=None, pending=None):
    """Conservative offline fallback; the model interprets complete turns online."""
    lowered = text.lower().strip()
    if re.search(r"(?:先|只|仅).*(?:看|生成|整理|给我).*(?:计划|方案)|先.*(?:计划|方案).*(?:不跑|不执行)", lowered):
        return "plan"
    if re.search(r"(?:先不|不要|暂不|暂时不)(?:跑|运行|仿真|执行|开展|进行|启动|发起)|不(?:执行|仿真)|只(?:是)?(?:问|咨询|解释|介绍)|先(?:解释|介绍|了解)", lowered):
        return "consult"
    if execution_requested(lowered):
        return "action"
    if re.search(r"(?:做|干|有)(?:什么|什麽|啥)|(?:什么|什麽|啥)(?:操作|用途|意义)|对其.*操作|可以.*操作|数值.*操作", lowered):
        return "consult"
    if re.match(r"(?:请)?(?:帮我)?(?:做\d|生成\d|探索\d|优化\d|跑(?:一次|一下)|运行(?:这个|计划))", lowered):
        return "action"
    if re.search(r"能.*(?:干|做)(?:什么|什麽)|干嘛|有什么(?:功能|能力)|会什么|介绍|解释|说明|了解|建议|推荐|为什么|为何|怎么|如何|哪些|有没有|是否|支持|能否|能不能|是什么|什么意思|需要什么|区别|what|why|how|can you do", lowered):
        return "consult"
    if re.search(r"(?:能|可以).+(?:吗|么|不)[？?。]*$", lowered) and "帮我" not in lowered:
        return "consult"
    if re.search(r"(?:重新|再)(?:跑|运行|仿真)|继续(?:运行|探索|仿真)|执行(?:这个|刚才|上面|计划)|直接(?:跑|运行|做|探索)|复测|重测", lowered) or re.fullmatch(r"(?:好[，, ]*)?开始(?:吧|运行|仿真|执行)?[。！!]*", lowered):
        return "action"
    if re.search(r"结果|报告|达标|失败原因|最优配置|跑完(?:了)?[吗么？?]|进度|总结", lowered):
        return "consult"
    if requirements_update(text):
        return "update"
    if pending and (workflow(lowered) or re.search(r"\d|精确|近似|穷举|采样|读|写|TT|FF|SS", text)):
        return "action"
    if re.search(r"帮我|做(?:个|一个|\d)|生成|仿真|跑|运行|执行|探索|优化|想要|需要|改(?:为|成|一下)|只改|只把|仅改", lowered):
        return "action"
    if workflow(lowered) and re.search(r"\d|精确|近似|穷举|采样", lowered):
        return "action"
    return "consult"


def history_task(root, text="", current=None):
    """Resolve only real EDA tasks, never validation/consultation audit folders."""
    runs = (Path(root) / "runs").resolve()
    named = re.findall(r"\d{8}_\d{6}_[a-z][a-z0-9_]*", text)
    wants_exploration = bool(re.search(r"探索|Pareto|最优|最佳|候选|这些数值|这些数据", text, re.I))
    wf = workflow(text)
    candidates = []
    if current:
        candidates.append(Path(current))
    candidates += sorted((p.parent for p in runs.glob("*/result.json")), key=lambda p: p.name, reverse=True)
    if named:
        candidates = [runs / name for name in named]
    for task in candidates:
        try:
            path = (task / "result.json").resolve(strict=True)
            if not path.is_relative_to(runs):
                continue
            result = json.loads(path.read_text())
            config = result.get("config", {})
            plan = config.get("plan", {}) or config.get("experiment", {}).get("plan", {})
            if not plan and (task / "inputs/spec.json").is_file():
                spec = (task / "inputs/spec.json").resolve()
                if spec.is_relative_to(runs):
                    plan = json.loads(spec.read_text())
            is_task = result.get("kind") in ("search_comparison", "acco", "exploration", "exploration_suite") or bool(plan.get("workflow")) or "metrics" in result
            if not is_task or (wf and plan.get("workflow") and plan["workflow"] != wf):
                continue
            if wants_exploration and result.get("kind") not in ("search_comparison", "acco", "exploration", "exploration_suite") and not named:
                continue
            return task.resolve()
        except (OSError, ValueError, TypeError):
            continue
    return None


def historical_plan(root, task, best=False):
    if task is None:
        return None
    runs = (Path(root) / "runs").resolve()
    paths = [Path(task) / "best_plan.json"] if best else [Path(task) / "inputs/spec.json"]
    for item in paths:
        try:
            path = item.resolve(strict=True)
            if path.is_relative_to(runs):
                return validate(json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            pass
    if not best:
        try:
            path = (Path(task) / "config.json").resolve(strict=True)
            if path.is_relative_to(runs):
                return validate(json.loads(path.read_text())["plan"])
        except (OSError, ValueError, TypeError, KeyError):
            pass
    return None


def task_facts(root, task):
    if task is None:
        task = history_task(root)
    if task is None:
        return {"result_empty": "当前没有可读取的任务结果。先提出电路与仿真需求，完成后可以直接追问是否达标、失败原因或最优配置。"}
    task = Path(task).resolve(strict=True)
    if not task.is_relative_to((Path(root) / "runs").resolve()):
        raise ValueError("结果只读取助手runs目录")
    path = (task / "result.json").resolve(strict=True)
    if not path.is_relative_to((Path(root) / "runs").resolve()):
        raise ValueError("结果文件不能指向助手目录外")
    result = json.loads(path.read_text())
    states = {"completed": "完成", "infeasible": "仿真完成，但验收未达标", "failed": "执行失败",
              "running": "运行中", "unverified_constraints": "存在未验证约束", "interrupted": "已中断"}
    facts = {"result_status": f"任务{task.name}：{states.get(result['status'], result['status'])}。",
             "result_path": f"任务及证据目录：{task}"}
    for key, value in result.get("metrics", {}).items():
        if type(value) in (int, float):
            facts["result_" + key] = value_text(key, value) + "。"
    for i, c in enumerate(result.get("constraints", [])):
        verdict = {True: "通过", False: "不通过", None: "未测量"}.get(c.get("passed"), "未测量")
        facts[f"result_constraint_{i}"] = f"验收{c['metric']} {c['op']} {c['value']}：{verdict}；实际值{c.get('actual','未测量')}。"
    if result.get("error"):
        facts["result_error"] = "执行器记录的失败原因：" + str(result["error"])[:1800]
        logs = sorted((task / "logs").glob("*.log"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        if logs:
            log = logs[0].resolve()
            if log.is_relative_to(task):
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size - 2400))
                    tail = stream.read().decode(errors="replace")
                facts["result_diagnostics"] = f"任务日志{log.name}末尾记录：\n{tail}"
    if result.get("kind") == "exploration":
        records = result.get("candidates", [])
        baseline = result.get("config", {}).get("plan", {})

        def readable_conditions(parameters):
            from result_summary import design_text
            from terminal_ui import parameter_lines
            wf = baseline.get("workflow")
            if wf not in DEFAULTS:
                return "历史记录未保存完整电路类型。"
            design = {"workflow": wf, "parameters": {**DEFAULTS[wf], **(parameters or {})}}
            return design_text(design) + "。" + " ".join(parameter_lines(design))

        facts["result_progress"] = f"已执行{len(records)}个候选，预算{result.get('budget')}；停止原因{result.get('stop_reason','运行中')}。"
        best = result.get("best_feasible_candidate")
        facts["result_best"] = f"本次已测候选中的最佳达标项：{best if best is not None else '暂无'}。有限探索不证明全局最优。"
        selected = next((r for r in records if r['candidate'] == best), None)
        if selected:
            facts["result_best_parameters"] = "最佳已测配置：" + readable_conditions(selected["parameters"])
            for key, value in selected.get("metrics", {}).items():
                if type(value) in (int, float):
                    facts["result_best_" + key] = "最佳候选" + value_text(key, value) + "。"
        facts["result_pareto"] = "本次原约束下保存的Pareto候选：" + json.dumps(result.get("pareto_candidates", [])) + "；权衡仅适用于该任务的指标和评价条件。"
        limits = "；".join(value_text(c["metric"], c["value"]) + ("及以下" if c["op"] == "le" else "及以上")
                          for c in baseline.get("constraints", []))
        facts["result_conditions"] = "本次探索的评价配置与验收条件：" + readable_conditions(baseline.get("parameters")) + ("验收要求：" + limits + "。" if limits else "未设置数值验收条件。")
    if result.get("kind") == "acco":
        facts["result_stages"] = "ACCO阶段：" + "；".join(f"{name}：{stage['status']}，{len(stage['records'])}条（{stage['evidence_kind']}）" for name, stage in result.get("stages", {}).items())
        facts["result_counts"] = "分开计数的评价预算使用：" + json.dumps(result.get("counters", {}), ensure_ascii=False)
        best = result.get("best_design")
        facts["result_best"] = "最佳已评价项：" + (best["candidate"] + "，来源" + best["evidence_kind"] if best else "暂无完整结果，不能推荐预测项为实测最优")
        if best:
            for name, value in best["metrics"].items():
                if type(value) in (int, float):
                    facts["result_best_" + name] = value_text(name, value)
        facts["result_scope"] = result.get("implementation_limit", "bank外围及面积为估算；鲁棒性只覆盖记录的PVT与MC样本，尚不代表良率或版图签核。")
    if result.get("kind") == "search_comparison":
        facts["result_comparison_progress"] = f"架构对照已记录{len(result.get('trials', []))}/{result.get('planned_trials')}轮；停止原因{result.get('stop_reason', '运行中')}。"
        facts["result_comparison_trials"] = "各轮真实结果：" + json.dumps([{k: r.get(k) for k in (
            "method", "search_seed", "status", "unique_architectures", "validation_count", "measured_feasible", "best_measured_pdp_fj", "elapsed_s")} for r in result.get("trials", [])], ensure_ascii=False)
        facts["result_comparison_scope"] = result.get("implementation_limit", "搜索为代理预测，入选项另行实测；固定器件。")
    return facts


def knowledge(api, text, previous=None, task=None, draft=False):
    """Curated, executable capability facts; model only selects grounded fact IDs."""
    topic = "general"
    learning_question = re.search(r"经验库|数据集|训练集|模型权重|如何学习|怎么学习", text)
    if re.search(r"结果|报告|达标|失败|进度|跑完|最优配置|总结", text) and not learning_question:
        if draft and not re.search(r"上次|上一轮|已跑|运行结果", text):
            return "result", {"result_pending": "当前只有待运行计划，还未进行该计划的仿真，不能判断是否达标；可以继续调整参数，或说“开始运行”。"}
        return "result", task_facts(api.ROOT, task)
    wf = workflow(text) or (previous or {}).get("workflow")
    facts = {
        "capabilities": "我可以回答OpenACM能力、参数和指标问题，也可以把电路需求整理成配置，在独立副本中仿真、总结结果，并进行有预算的设计空间探索。",
        "circuits": "支持五类工作流：SRAM与压缩器SPICE；整数乘法器RTL/综合；浮点乘法器RTL/综合；DCiM功能验证、综合和完整ASIC后端。",
        "usage": "你可以先问参数，再说“做8位精确乘法器，穷举所有输入”；只看计划用“/plan 需求”，确定后说“开始运行”；结果出来后可问“是否达标”“为什么失败”“下一步怎么优化”。",
        "protection": "原始OpenACM内置程序只读，调整在runs中的普通副本执行；任务保留配置、RTL/网表、日志、报告和源完整性校验。无需sudo。",
    }
    if re.search(r"参数|尺寸|位宽|支持|能做|能否|配置|默认|条件|流程|后端|局限|限制|要求|计划|方案", text):
        topic = "parameters"
        if wf:
            descriptions = {
                "multiplier": "整数乘法器可以设置位宽、精确/近似/对数类型、统一或逐位置压缩器、原生或分块架构、采样数/穷举、测试种子，以及功能验证或综合流程。",
                "dcim": "DCiM可以设置行数、乘法位宽与类型、压缩器分配、存储实现、测试输入和种子、后端流程与时钟周期。",
                "sram": "SRAM可以设置阵列行列、供电、温度与工艺角、PU/PD/PG宽度和沟道长、读写与三种SNM、线网RC、外围尺寸倍率及Monte Carlo条件。",
                "floating": "浮点乘法器可以设置变体、总位宽和指数宽度、近似模式与尾数保留位、采样数和种子、功能验证或综合流程；合法组合随变体不同。",
                "compressor": "压缩器可以设置类型、供电、温度、NMOS/PMOS宽度倍率、沟道长倍率和负载倍率。"}
            facts = {"parameters": descriptions[wf]}
            if re.search(r"默认|defaults?", text, re.I):
                from terminal_ui import parameter_lines
                facts["defaults"] = "默认条件：" + " ".join(parameter_lines({"workflow": wf, "parameters": DEFAULTS[wf]}))
            if previous and previous.get("workflow") == wf:
                if re.search(r"当前|刚才|这个|上次|现在", text):
                    from terminal_ui import parameter_lines
                    facts = {"current_design": "当前设计条件（不代表已通过仿真）：" + " ".join(parameter_lines(previous)), **facts}
        else:
            facts["choose_circuit"] = "参数随电路类型不同。请指定SRAM、压缩器、整数乘法器、浮点乘法器或DCiM，可以只问参数而不启动仿真。"
        if wf in ("multiplier", "dcim"):
            facts["integer_scope"] = f"整数乘法器/DCiM位宽支持{LIMITS['bit_width'][0]}–{LIMITS['bit_width'][1]}；auto/native/tiled架构，4位及大于32位近似通常采用8位分块。Log仅8/16/32位；穷举仅4–8位，较大位宽采用固定种子采样。"
            facts["compressors"] = "逐位置压缩器ID：1=ACCI1、2=kong2、3=antonio、4=momeni、5=ha、6=akbar1、7=akbar2、8=sabetz；8位native为9个位置、16位native为49个。全部同种用compressor_id，混合分配用compressor_ids。"
            facts["digital_flow"] = "functional验证RTL功能/误差；synthesis还测Nangate45综合面积并做门级对照。完整ASIC流程通过DCiM backend=asic执行，不把综合当布局布线完成。"
        if wf == "dcim":
            facts["dcim_scope"] = "DCiM自动选择16×8/32×16/64×32匹配SRAM宏，其它尺寸可用standard_cells；完整后端包含综合、布局布线、寄生提取、STA、SDF后仿真和活动功耗。时序、DRC和功能按实测分别验收。"
        if wf == "sram":
            facts["sram_scope"] = "SRAM可调阵列行列、VDD、PVT、MOS W/L、读写/SNM、RC、外围倍率和Monte Carlo。行数须为2的幂，单次容量至多65536 bit；估算单元面积不等于版图面积。"
        if wf == "floating":
            facts["floating_scope"] = "浮点Exact支持总宽8–32、指数3–8；AC支持总宽16–32、指数5–8及keep/mode。EAFPU/MMBS/SSFPM等源码固定FP32；Exact名称不等于所有IEEE边界已通过。"
        if wf == "compressor":
            facts["compressor_scope"] = "八种压缩器可调整VDD、温度、NMOS/PMOS宽度、长度和负载倍率，使用Xyce测传播延迟与平均功耗；内置simplified LEVEL=1模型不是工艺签核。"
    if re.search(r"MRED|NMED|误差率|误差|指标|SNM|slack|面积|功耗|延迟", text, re.I):
        parameter_facts = facts if topic == "parameters" else {}
        topic = "metrics"
        facts = {"mred": "MRED是输出相对于精确参考的平均相对误差；本助手排除精确参考为零的输入。MRED≤0.02是相对误差约束，不表示所有输出都正确。",
                 "nmed": "NMED=平均绝对误差/(2^位宽−1)²，按无符号乘法器的最大精确乘积归一化。error_rate是输出不等于精确参考的输入占比。",
                 "metric_scope": "采样误差只对本次测试集合成立；用固定samples/seed比较候选。综合面积、路由面积与SRAM估算面积分别记录；缺测延迟/功耗不能填0或当已达标。",
                 "snm": "SRAM的hold_snm/read_snm/write_snm分别描述保持、读、写操作的静态噪声裕量，约束涉及某项时必须实际运行对应操作。",
                 "slack": "setup/hold slack分别检查建立/保持时序，≥0才无对应违例；后端完成不等于时序和DRC都达标。"}
        named = [key for key, pattern in [("mred", "MRED"), ("nmed", "NMED"), ("snm", "SNM"), ("slack", "slack")]
                 if re.search(pattern, text, re.I)]
        if named:
            facts = {k: facts[k] for k in named + ["metric_scope"]}
        facts = {**parameter_facts, **facts}
    if re.search(r"探索|架构搜索|设计空间搜索|架构寻优|参数搜索|优化|loop|DSE|预算|Pareto|提高精度|降低功耗|减小面积|面积更小", text, re.I):
        related = facts if topic != "general" else {}
        topic = "exploration"
        facts = {"exploration": "设计空间探索按固定尺寸/容量、测试集合和模型条件，循环生成合法候选→DeepSeek选择→真实仿真→约束验收→更新最佳项和Pareto集合。",
                 "budget": "默认每类64个候选、7200秒，可明确指定预算；最多10000个候选，时间预算同时生效。只有已测且全部约束通过的候选参与最佳排名。",
                 "search_fields": "可探索整数/DCiM的精确/近似/Log及压缩器分配，SRAM的供电/器件/外围及等容量形状，压缩器类型和尺寸倍率，浮点变体及合法mode/keep。具体搜索域由任务配置决定。",
                 "example": "咨询探索方式不会启动任务。实际执行可以说“探索8位近似乘法器，预算32个候选，固定种子42，要求MRED≤0.02，优先减小面积”。"}
        facts["acco"] = "研究主线为ACCO两级优化：乘法器先用原8/16位PEA-GNN与优化器搜索精度/PDP，再实测复核和调整已选单元宽度；SRAM先固定总容量和输出字宽选bank组织，再对入选组织做器件与明确PVT/MC评价。普通局部扫描不等同两级协同优化。"
        facts["acco_specification"] = "SRAM两级任务需要总容量与输出字宽，例如“探索SRAM，总容量4KB，输出字宽8位”；容量不能从默认16×8推断。新增阶段支持LLM、随机和原NSGA-II；bank面积/外围显式估算，乘法器定制模板未回灌完整PE。"
        facts = {**related, **facts}
    if re.search(r"数据集|经验|学习|训练|GNN|模型|DeepSeek", text, re.I):
        related = facts if topic != "general" else {}
        topic = "learning"
        facts = {"learning": "DeepSeek理解需求与选择下一步，EDA提供实测标签。持久经验库记录并检索配置、指标和失败原因；积累与检索不更新模型权重。",
                 "training": "代理模型训练需要电路配置/图结构、评价条件和实测标签；LLM选择训练还需要目标、允许菜单和截至决策时已测历史，不能把未来结果提前放进输入。",
                 "public_data": "已接入公开8位近似乘法器数据的检索接口；外部标签单独标记并重新复测，不当本地验收或其它尺寸的标签。GNN/LLM权重训练尚未执行。"}
        path = api.ROOT / "datasets/openacm-gnn-8bit/audits/validation.json"
        if path.is_file():
            count = json.loads(path.read_text())["rows"]
            facts["public_status"] = f"本机已核验{count}个公开8位乘法器配置；存于datasets/openacm-gnn-8bit，检索只使用训练划分。"
        else:
            facts["public_data"] = "本机提供公开8位数据检索接口；尚无已准备的数据索引，可用dataset fetch和dataset prepare建立。外部标签不能替代本地实测。"
        facts = {**related, **facts}
    if re.search(r"你好|谢谢|hello|hi\b", text, re.I) and topic == "general":
        facts = {"greeting": "你好。我可以先帮你了解电路能力、参数和指标，再按明确需求运行仿真或设计空间探索。"}
    if re.search(r"bank|子阵列|列复用|总容量", text, re.I):
        related = facts if topic != "general" else {}
        topic = "organization"
        facts = {**related,
                 "bank_definition": "bank是一个存储块，由若干子阵列及片选、译码、复用等外围组成。这里一个子阵列的行×列决定它保存多少bit；整个bank的容量还要乘子阵列数。",
                 "bank_capacity": "总容量=行数×列数×子阵列数。例如32行×32列×32个子阵列=32768bit，即4KB；输出字宽是一次对外提供的数据位数，不等于总容量或子阵列列数。",
                 "bank_evaluation": "本助手用单阵列SPICE结果和明确的外围模型估算bank延迟、功耗与面积；这些估算不能当作完整bank版图或全bank SPICE实测。"}
    from acco_spec import is_search_comparison
    if is_search_comparison(text):
        from benchmark_spec import SCOPE
        topic = "search_comparison"
        facts = {"comparison_scope": SCOPE,
                 "comparison_conditions": "搜索对照需固定电路、候选空间和基线、误差约束与优化目标、每组预算、独立搜索种子、评价输入及物理条件；分别记录预测、实测和总耗时，不能以单次搜索宣称算法更优。"}
    return topic, facts


def dialogue(api, text, previous=None, task=None, conversation=None, use_model=True,
             draft=False, pending=None, plan_only=False, experiment_context=None):
    """A turn can explain evidence and request a tool in the same response."""
    # Resolve explicit task references before constructing focused result facts.
    # Otherwise essential facts can retain the latest unrelated task's metrics.
    task = history_task(api.ROOT, text, task)
    topic, focused = knowledge(api, text, previous, task, draft)
    facts = {}
    for question in ["你能干什么", "误差指标", "设计空间探索是什么", "模型如何学习"]:
        facts.update(knowledge(api, question)[1])
    wf = workflow(text) or (previous or {}).get("workflow")
    if wf:
        facts.update(knowledge(api, wf + "参数", previous)[1])
    facts.update(focused)
    if experiment_context and re.search(r"(?:当前|这个|刚才|现在).*(?:配置|参数|计划|条件|方案)", text):
        from terminal_ui import review_plan
        facts["acco_current_design"] = "当前实验条件（计划不代表已实测）：\n" + review_plan(experiment_context).rsplit("\n\n", 1)[0]
    if task and not (draft and topic == "result" and "result_pending" in focused):
        facts.update(task_facts(api.ROOT, task))
    facts.update({
        "verification_exact": "精确乘法器验证将每组输入的RTL输出与独立整数乘积逐一比较；小位宽可穷举所有组合，较大位宽固定种子采样。零错误的采样仅说明本次集合通过，穷举通过覆盖该位宽全部输入。",
        "use_compare": "已有探索数值可以比较误差、面积及已测功耗/时序，筛选满足约束的配置、查看Pareto权衡。跨任务比较前要核对尺寸、种子/采样、后端、库和环境；未测指标不能参与排序。",
        "use_retest": "可以把最佳已测配置作为起点，增加采样或对可穷举的小位宽做穷举，再验证综合/后端。复测会创建新任务，历史采样达标不等于新条件一定达标。",
        "use_continue": "可以围绕已有可行配置继续有预算的探索，明确保留条件、调整变量、目标和候选预算；最佳仅指当次已测且达标的候选。",
        "use_training": "可以整理配置、图结构、实测标签、失败记录和评价环境，用于代理模型训练；用于LLM决策训练还需当时目标、菜单和已测历史。经验检索已接入，导出样本不等于已训练模型权重。",
        "use_next": "例如可以接着说“按刚才最佳配置复测，改为穷举”，或“保持评价条件，围绕最佳配置继续探索，预算32个候选”。只问数据能做什么不会启动这些任务。",
    })
    fallback_mode = "plan" if plan_only else route(text, previous, pending)
    is_update = requirements_update(text) and fallback_mode in ("update", "plan")
    from exploration import is_exploration
    fallback_request = text + (pending["request"] if pending else "")
    operation = {"consult": "respond", "update": "update", "plan": "plan", "action": "explore" if is_exploration(fallback_request) else "run"}[fallback_mode]
    audit = api.ROOT / "runs" / (datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S") + "_dialogue_" + uuid.uuid4().hex[:6])
    audit.mkdir(parents=True)
    reuse_question = bool(re.search(r"(?:数值|数据|探索|结果).*(?:操作|用途|做什么|做什麽|干什么|用来|利用)|对其.*操作", text))
    essential = list(focused) if topic == "result" else list(focused)[:2 if topic == "general" else 1]
    # Each explicit question in a compound turn needs an answer. These IDs hold
    # verified definitions, so the model cannot omit or invent them.
    for key, pattern in [("parameters", r"哪些参数|什么参数|有哪些.*参数|参数.*哪些"),
                         ("mred", r"MRED.*(?:是什么|什么意思|含义)|(?:什么是|解释).*MRED"),
                         ("nmed", r"NMED.*(?:是什么|什么意思|含义)|(?:什么是|解释).*NMED"),
                         ("snm", r"SNM.*(?:是什么|什么意思|含义)|(?:什么是|解释).*SNM")]:
        if key in focused and re.search(pattern, text, re.I):
            essential.append(key)
    if reuse_question:
        essential = ["use_compare", "use_retest", "use_continue", "use_training"]
    if task and (reuse_question or re.search(r"之前|上次|上一轮|刚才|最佳|最优|这些数值|这些数据", text)):
        essential += [key for key in ("result_status", "result_best", "result_best_mred", "result_best_synthesized_area_um2") if key in facts]
    if "integer_scope" in facts and re.search(r"支持|能做|能否|能不能|可以", text):
        essential.append("integer_scope")
    if topic == "search_comparison" and re.search(r"怎么|如何|需要|条件", text):
        essential += list(focused)
    if fallback_mode != "consult":
        essential = []
    selected = list(dict.fromkeys((list(focused) if not reuse_question else essential) + essential))
    if fallback_mode != "consult":
        selected = []
    # The selectable evidence follows the question; unrelated reference material
    # must not spill into the reply when a model tool choice is rejected.
    relevant = set(focused) | set(essential)
    if reuse_question:
        relevant.update(k for k in facts if k.startswith("use_") or k.startswith("result_"))
    if topic == "result":
        relevant.update(focused)
    if re.search(r"精确.*验证|验证.*精确", text):
        relevant.add("verification_exact")
    if re.search(r"复测|重测|重新跑", text):
        relevant.add("use_retest")
    if "acco_current_design" in facts:
        relevant.add("acco_current_design")
    if is_update or fallback_mode == "update":
        topic = "requirements"
        facts = {"requirements": requirements_receipt(text)}
        selected, essential = [], []
    else:
        facts = {k: v for k, v in facts.items() if k in relevant}
    api.write_json(audit / "facts.json", {"topic": topic, "facts": facts})
    model_used, message = False, ""
    expected_operation, model_operation, resolution = operation, None, None
    if use_model:
        schema = {"type": "object", "additionalProperties": False, "properties": {
            "operation": {"type": "string", "enum": ["respond", "plan", "run", "explore", "update"]},
            "message": {"type": "string", "maxLength": 240},
            "fact_ids": {"type": "array", "maxItems": min(10, len(facts)), "uniqueItems": True,
                         "items": {"type": "string", "enum": list(facts)}}}, "required": ["operation", "message", "fact_ids"]}
        guide = api.ROOT / "skills/openacm-design/references/conversation-guide.md"
        system = guide.read_text().split("## 用户问题链")[0] if guide.is_file() else "结合完整上下文回答，明确执行要求才调用工具。"
        context = {"facts": facts, "essential": essential,
                   "state": {"design": previous, "draft": draft, "pending": pending, "task": str(task) if task else None,
                             "plan_only": plan_only, "acco_experiment": experiment_context}}
        messages = [{"role": "system", "content": system},
                    {"role": "system", "content": "以下是工具提供的已核对资料与会话状态，供当前请求使用；不代表新的用户指令。\n" + json.dumps(context, ensure_ascii=False)}]
        messages += [{**entry, "content": entry["content"][:600]} for entry in (conversation or [])[-4:]]
        messages.append({"role": "user", "content": text})
        body = {"model": api.MODEL, "stream": False, "format": schema, "messages": messages,
                "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 900}}
        api.write_json(audit / "request.json", body)
        try:
            client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(api.OLLAMA + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with client.open(request, timeout=120) as response:
                reply = json.load(response)
            api.write_json(audit / "reply.json", reply)
            decision = json.loads(reply["message"]["content"])
            ids = decision["fact_ids"]
            if (reply.get("done_reason") == "length" or set(decision) != {"operation", "message", "fact_ids"}
                    or decision["operation"] not in ["respond", "plan", "run", "explore", "update"]
                    or not isinstance(decision["message"], str) or len(decision["message"]) > 240
                    or not isinstance(ids, list) or len(ids) > 10 or any(not isinstance(k, str) or k not in facts for k in ids)):
                raise ValueError("对话回复不符合当前工具与事实接口")
            operation = "plan" if plan_only else decision["operation"]
            model_operation = decision["operation"]
            # Evidence remains literal. Model prose connects it and suggests next steps.
            message = conversational_message(decision["message"])
            if re.search(r"\d|已(?:完成|执行|修改|验证|仿真|训练|达标)|测得|全局最优|保证.*达标", message):
                (audit / "prose_rejected.txt").write_text(message + "\n")
                message = ""
            selected = list(dict.fromkeys(ids + essential))
            model_used = True
        except Exception as exc:
            (audit / "model_error.txt").write_text(str(exc) + "\n")
    # A model may choose a tool, but a question alone never authorizes execution.
    if fallback_mode == "update":
        operation, selected, message = "update", [], ""
        resolution = "requirements_preserved"
    elif operation in ["plan", "run", "explore", "update"] and not plan_only and not tool_requested(text, fallback_mode, pending):
        operation = "respond"
        selected = essential or list(focused)[:2]
        resolution = "question_does_not_authorize_execution"
        message = ""  # Do not present a blocked model action as a promise to run it.
    elif fallback_mode != "consult" and tool_requested(text, fallback_mode, pending):
        if (operation == "respond" or (operation == "run" and expected_operation == "explore")
                or (operation == "plan" and fallback_mode == "action")):
            operation = expected_operation
            resolution = "explicit_task_preserved"
            message = ""
    if operation == "respond" and not selected:
        selected = essential or list(facts)[:2]
    if operation != "respond" and not re.search(r"解释|说明|为什么|为何|分析|介绍", text):
        selected = []
    rendered = "\n\n".join(([message] if message else []) + [facts[k] for k in selected])
    # Keep the model selection separate from the reply produced by the tools.
    # Session.turn replaces answer.md with the actual final user-facing answer.
    (audit / "dialogue_answer.md").write_text(rendered + "\n")
    (audit / "answer.md").write_text(rendered + "\n")
    api.write_json(audit / "metadata.json", {"interface": "contextual_dialogue", "operation": operation, "topic": topic,
                                           "model_used": model_used, "model_operation": model_operation,
                                           "fallback_mode": fallback_mode, "resolution": resolution,
                                           "selected_facts": selected, "executed": False})
    return {"mode": "consult" if operation == "respond" else operation, "operation": operation,
            "text": rendered, "audit_directory": str(audit), "model_used": model_used, "task": str(task) if task else None}


def answer(api, text, previous=None, task=None, conversation=None, use_model=True, draft=False):
    """Compatibility helper for read-only clients; never invokes an executor."""
    outcome = dialogue(api, text, previous, task, conversation, use_model, draft)
    outcome["mode"] = "consult"
    return outcome


class Session:
    def __init__(self):
        self._state_root = None
        self.previous = None
        self.draft = None
        self.experiment_draft = None
        self.experiment = None
        self.comparison_draft = None
        self.comparison = None
        self.pending = None
        self.last_task = None
        self.conversation = []
        self.phase = "idle"

    @classmethod
    def restore(cls, root, reset=False):
        session = cls()
        session._state_root = Path(root).resolve()
        path = session._state_root / "conversations/current.json"
        if reset:
            session.save()
            return session
        if not path.is_file():
            return session
        try:
            raw = json.loads(path.read_text())
            if not isinstance(raw, dict) or raw.get("schema_version") != 1:
                raise ValueError("不支持的会话存档格式")
            for name in ("previous", "draft"):
                if raw.get(name):
                    setattr(session, name, validate(raw[name]))
            from acco_spec import validate_acco
            from benchmark_spec import validate_benchmark
            for name in ("experiment", "experiment_draft"):
                if raw.get(name):
                    setattr(session, name, validate_acco(raw[name]))
            for name in ("comparison", "comparison_draft"):
                if raw.get(name):
                    setattr(session, name, validate_benchmark(raw[name]))
            pending = raw.get("pending")
            if pending and isinstance(pending, dict) and isinstance(pending.get("request"), str):
                session.pending = pending
            conversation = raw.get("conversation", [])
            if isinstance(conversation, list) and all(isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) for m in conversation):
                session.conversation = conversation[-8:]
            if raw.get("last_task"):
                task = Path(raw["last_task"]).resolve()
                if task.is_relative_to(session._state_root / "runs") and task.is_dir():
                    session.last_task = task
        except (OSError, ValueError, TypeError, KeyError):
            # Preserve the unreadable file; a new turn writes a separate backup.
            session = cls()
            session._state_root = Path(root).resolve()
            session._invalid_state = True
        return session

    def save(self):
        if self._state_root is None:
            return
        folder = self._state_root / "conversations"
        if not folder.resolve().is_relative_to(self._state_root):
            raise ValueError("会话目录必须位于助手目录内")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "current.json"
        if path.is_symlink():
            raise ValueError("会话存档不能是符号链接")
        if getattr(self, "_invalid_state", False) and path.exists():
            path.replace(folder / ("unreadable_" + uuid.uuid4().hex[:8] + ".json"))
            self._invalid_state = False
        data = {"schema_version": 1, "saved_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                **{name: getattr(self, name) for name in ("previous", "draft", "experiment", "experiment_draft",
                                                        "comparison", "comparison_draft", "pending", "conversation")},
                "last_task": str(self.last_task) if self.last_task else None}
        from exploration import json_write
        json_write(path, data)

    def remember(self, text, reply):
        self.conversation += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        self.conversation = self.conversation[-8:]

    def turn(self, api, text, plan_only=False, ui=None, use_model=True):
        self._turn_audit = None
        try:
            outcome = self._turn(api, text, plan_only, ui, use_model)
            if outcome["mode"] == "plan":
                from terminal_ui import review_plan
                reply = review_plan(outcome["plan"])
            elif "job" in outcome:
                reply = self.conversation[-1]["content"]
            else:
                reply = outcome["text"]
            outcome["reply"] = reply
            # Subsequent turns see the same readable answer the user received.
            if self.conversation and self.conversation[-1]["role"] == "assistant":
                self.conversation[-1]["content"] = reply
            if self._turn_audit:
                outcome.setdefault("audit_directory", str(self._turn_audit))
                self.record_answer(api, reply, outcome)
            return outcome
        except Exception as exc:
            if self._turn_audit:
                self.record_answer(api, self.error_reply(exc), {"mode": "error", "error": str(exc)})
            raise
        finally:
            self.save()

    def error_reply(self, exc):
        label = {"dialogue": "本轮对话暂未完成", "planning": "需求尚未形成有效计划，未启动仿真",
                 "execution": "执行未完成", "summary": "实测已保存，总结暂未完成"}.get(self.phase, "操作未完成")
        return f"{label}：{exc}"

    def record_answer(self, api, reply, outcome):
        audit = self._turn_audit
        (audit / "answer.md").write_text(reply.rstrip() + "\n")
        metadata = json.loads((audit / "metadata.json").read_text())
        metadata.update(final_mode=outcome["mode"], response_complete=True,
                        executed="job" in outcome, execution_started=self.phase in ("execution", "summary"))
        if "job" in outcome:
            metadata["task"] = str(outcome["job"])
        if "error" in outcome:
            metadata["error"] = outcome["error"]
        api.write_json(audit / "metadata.json", metadata)
        if outcome.get("plan"):
            api.write_json(audit / "final_plan.json", outcome["plan"])

    def _turn(self, api, text, plan_only=False, ui=None, use_model=True):
        self.phase = "dialogue"
        outcome = dialogue(api, text, self.draft or self.previous, self.last_task, self.conversation,
                           use_model, draft=self.draft is not None or self.experiment_draft is not None or self.comparison_draft is not None,
                           pending=self.pending or ({"request": self.comparison_draft.get("request", "架构搜索对照")} if self.comparison_draft else None),
                           plan_only=plan_only, experiment_context=self.comparison_draft or self.experiment_draft or self.experiment)
        self._turn_audit = Path(outcome["audit_directory"])
        operation = outcome["operation"]
        requested_workflow = workflow(text)
        old_workflow = ((self.comparison_draft or self.comparison or {}).get("experiment", {}).get("workflow")
                        or (self.experiment_draft or self.experiment or {}).get("workflow")
                        or (self.draft or self.previous or {}).get("workflow")
                        or workflow((self.pending or {}).get("request", "")))
        if operation != "respond" and requested_workflow and old_workflow and requested_workflow != old_workflow:
            self.previous = self.draft = self.experiment_draft = self.comparison_draft = None
            self.experiment = self.comparison = self.pending = None
        is_update = requirements_update(text) and operation in ("update", "plan")
        had_context = bool(self.pending or self.draft or self.previous or self.experiment_draft or self.comparison_draft or self.comparison)
        if is_update and not had_context and not requested_workflow:
            message = requirements_receipt(text) + "\n\n这些条件用于哪种电路、多少位？例如：8位近似乘法器。"
            self.pending = {"kind": "search_comparison" if comparison_conditions(text) else "requirements",
                            "request": text, "message": message, "plan_only": True}
            api.write_json(Path(outcome["audit_directory"]) / "requirements.json", {
                "request": text, "status": "needs_circuit", "execution_started": False})
            self.remember(text, message)
            return {"mode": "clarify", "text": message, "audit_directory": outcome["audit_directory"],
                    "model_used": outcome["model_used"]}
        start_experiment = self.experiment_draft and re.fullmatch(r"(?:好[，, ]*)?开始(?:吧|运行|仿真|执行)?[。！!]*", text) and not plan_only
        if start_experiment:
            operation = "explore"
        start_comparison = self.comparison_draft and re.fullmatch(r"(?:好[，, ]*)?开始(?:吧|运行|仿真|执行)?[。！!]*", text) and not plan_only
        if start_comparison:
            operation = "explore"
        plan_only = plan_only or operation == "plan"
        pending_preview = self.pending and self.pending.get("plan_only")
        explicit_start = (execution_requested(text) or bool(re.search(r"(?:开始|直接|立即)(?:运行|执行|仿真)", text))) and route(text) != "consult"
        if (pending_preview or (is_update and not self.pending)) and not explicit_start:
            plan_only = True
        if operation == "respond":
            if outcome["task"] and re.search(r"之前|上次|上一轮|刚才|最佳|最优|探索.*数值|探索.*数据|结果|报告", text):
                self.last_task = Path(outcome["task"])
            self.remember(text, outcome["text"])
            return outcome
        self.phase = "planning"
        request = text
        replacing_comparison = self.pending and self.pending.get("kind") == "search_comparison" and re.search(
            r"(?:取消|不做|不要|放弃).{0,8}(?:对照|对比|消融)|(?:改成|改为|只做|只跑)单次", text)
        if self.pending and not replacing_comparison:
            request = f"原始待澄清需求：{self.pending['request']}。用户补充：{text}"
        if replacing_comparison:
            self.pending = None
            self.comparison_draft = None
        context_plan = self.draft or self.previous
        from acco_spec import NeedsExperiment, is_search_comparison
        from exploration import is_exploration
        from sram_retest import is_device_retest, from_request as retest_request
        if is_device_retest(request):
            try:
                config = retest_request(api, request)
            except NeedsExperiment as exc:
                message = str(exc)
                self.pending = {"request": request, "message": message, "plan_only": bool(plan_only)}
                self.remember(text, message)
                return {"mode": "clarify", "text": message, "audit_directory": outcome["audit_directory"]}
            api.write_json(Path(outcome["audit_directory"]) / "sram_retest_plan.json", config)
            self.pending = self.comparison_draft = self.comparison = self.draft = None
            self.experiment_draft = config
            self.save()
            if plan_only:
                self.remember(text, "待运行SRAM器件复验：" + json.dumps(config, ensure_ascii=False))
                return {"mode": "plan", "plan": config, "audit_directory": outcome["audit_directory"]}
            from acco import launch
            self.phase = "execution"
            job = launch(api, config, use_model)
            self.experiment, self.experiment_draft, self.last_task = config, None, job
            self.remember(text, (job / "reports/summary.md").read_text())
            return {"mode": "explore", "job": job, "audit_directory": outcome["audit_directory"]}
        pending_comparison = self.pending and self.pending.get("kind") == "search_comparison"
        if is_search_comparison(request) or pending_comparison or (self.comparison_draft and not replacing_comparison) or (is_update and self.comparison):
            from benchmark import launch as launch_comparison
            from benchmark_spec import from_request as comparison_from_request
            try:
                config = self.comparison_draft if start_comparison else comparison_from_request(request, context_plan, self.comparison_draft or self.comparison)
            except NeedsExperiment as exc:
                message = str(exc)
                self.pending = {"kind": "search_comparison", "request": request, "message": message, "plan_only": bool(plan_only)}
                api.write_json(Path(outcome["audit_directory"]) / "comparison_request.json", {
                    "kind": "search_comparison", "status": "needs_specification", "request": request, "execution_started": False})
                self.remember(text, message)
                return {"mode": "clarify", "text": message, "audit_directory": outcome["audit_directory"]}
            api.write_json(Path(outcome["audit_directory"]) / "benchmark_plan.json", config)
            self.pending = None
            if plan_only:
                self.comparison_draft, self.draft, self.experiment_draft = config, None, None
                self.remember(text, "待运行搜索对照：" + json.dumps(config, ensure_ascii=False))
                return {"mode": "plan", "plan": config, "audit_directory": outcome["audit_directory"]}
            self.phase = "execution"
            self.comparison_draft = config
            self.save()
            job = launch_comparison(api, config)
            self.last_task, self.comparison, self.comparison_draft = job, config, None
            self.remember(text, (job / "reports/summary.md").read_text())
            return {"mode": "compare", "job": job, "audit_directory": outcome["audit_directory"]}
        if outcome["text"] and not plan_only:
            print(outcome["text"])
        if plan_only and is_exploration(request) and not re.search(r"局部探索|单级探索|legacy", request, re.I):
            from acco import command
            try:
                config = command(api, request, previous=context_plan, use_model=use_model, plan_only=True)
            except NeedsExperiment as exc:
                self.pending = {"request": request, "message": str(exc)}
                self.remember(text, str(exc))
                return {"mode": "clarify", "text": str(exc)}
            self.experiment_draft, self.draft = config, None
            self.remember(text, "待运行ACCO实验：" + json.dumps(config, ensure_ascii=False))
            return {"mode": "plan", "plan": config}
        retest_experiment = None
        if outcome["task"] and re.search(r"最佳|最优", text) and re.search(r"复测|重测|重新运行", text):
            source = Path(outcome["task"]) / "best_experiment.json"
            if source.is_file() and source.resolve().is_relative_to((api.ROOT / "runs").resolve()):
                from acco_spec import validate_acco
                retest_experiment = validate_acco(json.loads(source.read_text()))
                from bindings import explicit_parameters
                changes = explicit_parameters(text, retest_experiment["workflow"])
                if changes:
                    message = "完整bank最佳实验已找到；带参数的复测请先用/plan明确ACCO实验，避免把bank条件降成单阵列。"
                    self.pending = {"request": request, "message": message}
                    self.remember(text, message)
                    return {"mode": "clarify", "text": message}
                operation = "explore"
        if not retest_experiment and re.search(r"最佳|最优|刚才.*配置|之前.*配置|上次.*配置", text):
            reference = historical_plan(api.ROOT, outcome["task"], best=bool(re.search(r"最佳|最优", text)))
            if reference:
                context_plan = reference
            elif re.search(r"最佳|最优", text) and outcome["task"]:
                message = "该历史任务没有可读取的最佳达标配置。请指定已测候选，或提出新的电路、约束与探索预算。"
                self.remember(text, message)
                return {"mode": "clarify", "text": message}
        from exploration import explore_command
        if not plan_only and operation == "explore":
            if not workflow(request) and not context_plan and not start_experiment and not retest_experiment and not re.search(r"全工程|全部电路|所有.*电路", request):
                message = "要继续探索哪类电路或哪次历史任务？可以给出任务名，或说明电路、约束和预算。"
                self.pending = {"request": request, "message": message}
                self.remember(text, message)
                return {"mode": "clarify", "text": message}
            self.phase = "execution"
            try:
                if start_experiment or retest_experiment:
                    from acco import launch
                    job = launch(api, retest_experiment or self.experiment_draft, use_model)
                else:
                    job = explore_command(api, request, previous=context_plan, use_model=use_model)
            except NeedsExperiment as exc:
                message = str(exc)
                self.pending = {"request": request, "message": message}
                self.remember(text, message)
                return {"mode": "clarify", "text": message}
            self.last_task = job
            result = json.loads((job / "result.json").read_text())
            self.experiment = result["config"] if result.get("kind") == "acco" else None
            self.experiment_draft = None
            if (job / "best_plan.json").exists():
                self.previous = json.loads((job / "best_plan.json").read_text())
            self.pending, self.draft = None, None
            summary = (job / "reports/summary.md").read_text()
            self.remember(text, outcome["text"] + "\n\n" + summary if outcome["text"] else summary)
            return {"mode": "explore", "job": job}
        if not workflow(request) and not context_plan:
            message = "请先说明要仿真的电路：SRAM、压缩器、整数乘法器、浮点乘法器或DCiM；可以直接补充，例如“8位精确乘法器，穷举”。"
            self.pending = {"request": request, "message": message}
            self.remember(text, message)
            return {"mode": "clarify", "text": message}
        start_draft = self.draft and re.fullmatch(r"(?:好[，,、 ]*)?(?:开始(?:运行|执行|仿真)?|运行|执行)(?:吧|仿真|计划|这个计划)?[。！!]*", text)
        if start_draft:
            plan, reply = copy.deepcopy(self.draft), None
        else:
            plan, reply = api.plan_request(request, context_plan, self.conversation)
        if plan["intent"] != "run":
            message = conversational_message(plan["message"]) or "请补充你希望调整的电路参数或仿真条件。"
            if plan["intent"] == "clarify":
                self.pending = {"request": request, "message": message}
            self.remember(text, message)
            return {"mode": plan["intent"], "text": message}
        self.pending = None
        if plan_only:
            self.experiment_draft = None
            self.draft = plan
            self.remember(text, outcome["text"] + "\n待运行计划：" + json.dumps(plan, ensure_ascii=False))
            return {"mode": "plan", "plan": plan}
        self.phase = "execution"
        self.previous, self.draft = plan, None
        job = api.run_plan(plan, prompt=text, model_reply=reply)
        self.last_task = job
        self.phase = "summary"
        summary = api.summarize(job, api.OLLAMA, api.MODEL, ui=ui, use_model=use_model)
        self.remember(text, outcome["text"] + "\n\n" + summary if outcome["text"] else summary)
        return {"mode": "run", "job": job, "plan": plan}
