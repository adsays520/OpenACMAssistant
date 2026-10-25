"""Grounded Chinese summaries: model orders evidence, executor renders facts."""
from datetime import datetime
import json
from pathlib import Path
import urllib.request
import uuid


LABELS = {
    "read_delay_ns": ("读延迟", "ns"), "write_delay_ns": ("写延迟", "ns"),
    "read_power_uw": ("平均读功耗", "µW"), "write_power_uw": ("平均写功耗", "µW"),
    "hold_snm_v": ("保持 SNM", "V"), "read_snm_v": ("读 SNM", "V"), "write_snm_v": ("写 SNM", "V"),
    "cell_area_um2": ("单元估算面积", "µm²"), "delay_ns": ("传播延迟", "ns"),
    "power_uw": ("平均功耗", "µW"), "gate_area_um2": ("门面积估算", "µm²"),
    "estimated_area_um2": ("启发式面积估算", "µm²"), "mred": ("平均相对误差 MRED", ""),
    "nmed": ("归一化平均误差 NMED", ""), "mae": ("平均绝对误差", ""),
    "max_absolute_error": ("最大绝对误差", ""), "error_rate": ("错误率", "%"),
    "setup_wns_ns": ("最差 setup slack", "ns"), "hold_wns_ns": ("最差 hold slack", "ns"),
    "route_drc_count": ("详细布线 DRC 数量", ""), "routed_area_um2": ("后端面积", "µm²"),
    "routed_power_uw": ("当前测试负载下的后端模型功耗", "µW"),
    "synthesized_area_um2": ("Nangate45综合映射面积", "µm²"),
    "special_mismatch_count": ("浮点特殊值/边界不一致数量", ""),
    "pdp_fj": ("功耗延迟积 PDP", "fJ"), "sta_delay_ns": ("综合后STA延迟", "ns"),
    "sta_dynamic_power_uw": ("指定VCD活动下动态功耗", "µW"),
    "bank_area_um2_estimate": ("bank估算面积", "µm²"),
    "bank_read_power_uw_estimate": ("bank估算读功耗", "µW"),
    "bank_write_power_uw_estimate": ("bank估算写功耗", "µW"),
    "bank_read_delay_ns_estimate": ("bank估算读延迟", "ns"),
    "bank_write_delay_ns_estimate": ("bank估算写延迟", "ns"),
    "bank_pmax_uw_estimate": ("bank估算最差功耗", "µW"),
    "bank_dmax_ns_estimate": ("bank估算最差延迟", "ns"),
    "min_snm_v": ("最小SNM", "V"),
    "fom_arch_estimate": ("架构FOM估算", ""), "fom_robust_estimate": ("工程鲁棒FOM估算", ""),
}
STATUS = {"completed": "仿真完成", "infeasible": "仿真完成，但要求未全部达标",
          "unverified_constraints": "仿真完成，但部分要求尚未验证", "failed": "任务未完成",
          "incomplete": "流程未全部完成"}


def value_text(key, value):
    label, unit = LABELS.get(key, (key, ""))
    if key == "error_rate":
        value *= 100
    number = f"{value:.6g}" if type(value) in (int, float) else str(value)
    return f"{label}：{number}{' ' + unit if unit else ''}"


def design_text(plan):
    p, workflow = plan["parameters"], plan["workflow"]
    kind = {"exact": "精确", "approx": "近似", "log": "对数近似"}.get(p.get("kind"), "")
    if workflow == "multiplier":
        return f"{p['bit_width']} 位{kind}乘法器，{'穷举所有输入' if p['exhaustive'] else '固定种子采样验证'}"
    if workflow == "sram":
        return f"{p['rows']}×{p['cols']} SRAM，{p['vdd_v']:g} V、{p['corner']}、{p['temperature_c']:g}℃"
    if workflow == "compressor":
        return f"{p['name']} 压缩器，NMOS/PMOS 宽度倍率 {p['nmos_scale']:g}/{p['pmos_scale']:g}"
    if workflow == "floating":
        return f"{p['fp_width']}位浮点乘法器 {p['variant']}，指数{p['exp_width']}位"
    return f"{p['rows']}×{p['bit_width']} {kind}DCiM，{'完整后端' if p['backend'] == 'asic' else '功能验证'}"


def evidence(job):
    result_path = job / "result.json"
    batch_path = job / "batch_results.json"
    if result_path.exists():
        result = json.loads(result_path.read_text())
    else:
        # Early sweep tasks predate the aggregate result.json; their original
        # candidate measurements remain the source of truth.
        batch = json.loads(batch_path.read_text())
        result = {"status": "completed" if any(c["status"] == "completed" for c in batch["results"]) else "failed"}
    facts, essential = {}, []
    status = result["status"]
    plan_path = job / "plan.json"
    plan = json.loads(plan_path.read_text()) if plan_path.exists() else None
    title = design_text(plan) if plan else "ACCO两级探索" if result.get("kind") == "acco" else "DeepSeek设计空间探索" if result.get("kind") in ["exploration","exploration_suite"] else "全工程参考流程"
    verdict = STATUS.get(status, status)
    facts["status"] = f"{title}：{verdict}。"
    essential.append("status")
    metrics = result.get("metrics", {})
    if "sample_count" in metrics:
        count = metrics["sample_count"]
        if "error_count" in metrics:
            facts["vectors"] = f"已验证 {count:,} 组输入，其中 {metrics['error_count']:,} 组输出与精确参考乘积不一致。"
        else:
            facts["vectors"] = f"已验证 {count:,} 组输入。"
        essential.append("vectors")
    if plan and plan["workflow"] in ["multiplier", "dcim"]:
        p = plan["parameters"]
        if p["kind"] == "exact" and status != "failed" and metrics.get("error_count") == 0:
            if p["exhaustive"]:
                facts["conclusion"] = "结论：在本次位宽的全部输入组合上，乘法器输出与精确乘积一致，穷举功能验证通过。"
            else:
                facts["conclusion"] = "结论：本次采样未发现乘法器计算错误；采样通过不等同于全部输入均正确。"
        elif p["kind"] == "log":
            facts["scope"] = "误差按无符号精确乘积计算；对数生成器含符号处理，不能混称相同数值语义的精度保证。"
        else:
            facts["scope"] = "误差以精确乘积为参考；MRED 排除参考乘积为零的输入。" + ("本次已穷举该位宽所有输入。" if p["exhaustive"] else "采样结果不代表完整穷举。")
    for key in LABELS:
        if key in metrics:
            facts[key] = value_text(key, metrics[key]) + "。"
    if plan:
        important = {
            "sram": ["read_delay_ns", "write_delay_ns", "read_power_uw", "write_power_uw", "hold_snm_v", "read_snm_v", "write_snm_v"],
            "multiplier": ["error_rate"] if plan["parameters"].get("kind") == "exact" else ["error_rate", "mred", "nmed", "max_absolute_error"],
            "compressor": ["delay_ns", "power_uw"],
            "dcim": ["routed_power_uw", "routed_area_um2"],
            "floating": ["error_rate","mred","special_mismatch_count","synthesized_area_um2"],
        }[plan["workflow"]]
        essential += [key for key in important if key in facts]
    if plan and plan["workflow"] == "dcim":
        checks = [("pe_constant_input_checks", "PE 常数输入"), ("post_synth_functional_checks", "综合后"),
                  ("post_route_functional_checks", "布线后功能"), ("post_route_sdf_checks", "SDF 后仿真")]
        measured = [f"{name} {metrics[key]} 次" for key, name in checks if key in metrics]
        if measured:
            facts["pe_checks"] = "有效输出检查：" + "；".join(measured) + "。"
            essential.append("pe_checks")
        for key in ["setup_wns_ns", "hold_wns_ns", "route_drc_count"]:
            if key in facts:
                essential.append(key)
        stages = job / "reports/backend_stages.json"
        if stages.exists():
            entries = json.loads(stages.read_text())["stages"]
            done = sum(x["status"] == "completed" for x in entries)
            facts["stages"] = f"后端阶段：已完成 {done}/{len(entries)} 个已启动阶段。"
            failed = [x["name"] for x in entries if x["status"] == "failed"]
            if failed:
                facts["stages"] += " 未完成阶段：" + "、".join(failed) + "。"
            essential.append("stages")
    if result.get("error"):
        facts["error"] = "失败原因：" + str(result["error"])[:1200]
        essential.append("error")
    constraints = result.get("constraints", [])
    if constraints:
        lines = []
        for c in constraints:
            label, unit = LABELS.get(c["metric"], (c["metric"], ""))
            limit = c["value"] * (100 if c["metric"] == "error_rate" else 1)
            symbol = "≤" if c["op"] == "le" else "≥"
            verdict = "通过" if c["passed"] is True else "未达标" if c["passed"] is False else "未测量"
            actual = value_text(c["metric"], c["actual"]) if c["actual"] is not None else "没有实测值"
            lines.append(f"{label} {symbol} {limit:g}{unit}：{verdict}（{actual}）")
        facts["constraints"] = "要求验收：\n" + "\n".join("  • " + line for line in lines)
    elif batch_path.exists():
        facts["constraints"] = "验收按各候选的实际测量分别判定；最佳项只从已达标候选中选择。"
    elif result.get("kind") == "acco":
        facts["stages"] = "两级阶段记录：\n" + "\n".join(f"  • {name}：{stage['status']}，{len(stage['records'])}条；来源{stage['evidence_kind']}" for name, stage in result.get("stages", {}).items())
        best = result.get("best_design")
        facts["best"] = ("最佳已评价项：" + best["candidate"] + "；来源：" + best["evidence_kind"] + "\n" + "；".join(value_text(k, v) for k, v in best["metrics"].items() if type(v) in (int, float))) if best else "本次没有可推荐的完整入选结果，不能用预测值代替实测验收。"
        facts["scope"] = result.get("implementation_limit", "SRAM bank外围及面积为单阵列SPICE加OpenYield模型估算；PVT/MC只覆盖已记录案例，不证明稀有失效率或版图签核。")
        essential += ["stages", "best", "scope"]
        checks = best.get("constraints", []) if best else [c for stage in result.get("stages", {}).values()
            if stage["evidence_kind"] != "prediction" for row in stage["records"] for c in row.get("constraints", []) if c["passed"] is not True][:3]
        if checks:
            facts["constraints"] = "验收依据：" + "；".join(f"{c['metric']} {'≤' if c['op']=='le' else '≥'} {c['value']:g}，实际{c['actual']}："
                + {True: "通过", False: "未达标", None: "未测量"}[c["passed"]] for c in checks)
            essential.append("constraints")
        for i, device in enumerate(result.get("best_device_designs", [])):
            label = f"device_best_{i}"
            facts[label] = ("单元器件阶段最佳已评价项：" + device["candidate"] + "；父项：" + device["parent"] + "；逐组宽度倍率："
                + json.dumps(device["parameters"], ensure_ascii=False) + "\n"
                + "；".join(value_text(k, v) for k, v in device["metrics"].items() if type(v) in (int, float))
                + "。此处是单元SPICE及估算面积，未回灌完整乘法器。")
            essential.append(label)
    elif result.get("kind") == "exploration":
        config=result["config"]
        candidates=result["candidates"]
        best=next((r for r in candidates if r["candidate"]==result.get("best_feasible_candidate")),None)
        facts["exploration"]=f"已运行 {len(candidates)} 个候选，其中 {sum(r['status']=='completed' for r in candidates)} 个通过约束；DeepSeek选择 {sum(r['proposed_by']=='deepseek' for r in candidates)} 次。停止原因：{result.get('stop_reason')}。"
        facts["best"]=(f"本次最优已达标候选：{best['candidate']}。\n配置："+json.dumps(best['parameters'],ensure_ascii=False)+"\n"+"；".join(value_text(o['metric'],best['metrics'][o['metric']]) for o in config['objectives'])) if best else "本次没有满足全部约束且目标已测量的候选，不能推荐最优配置。"
        facts["scope"]="只比较本次已验证候选；容量、位宽和测试条件固定，不宣称全局最优。最佳配置可用best_plan.json复现。"
        essential += ["exploration","best","scope"]
    elif "cases" not in result:
        facts["constraints"] = "未指定性能验收条件；以上结论只对应已执行的仿真与功能检查。"
    else:
        cases = result["cases"]
        facts["cases"] = "电路结果：\n" + "\n".join(f"  • {c['workflow']}：{STATUS.get(c['status'], c['status'])}" for c in cases)
        essential.append("cases")
        if result.get("kind")=="exploration_suite":
            for c in cases:
                key="explore_"+c["workflow"]
                facts[key]=f"{c['workflow']}：已测{c['candidate_count']}个候选，最佳已达标候选{c['best_feasible_candidate']}。"+("\n"+"；".join(value_text(o['metric'],c['metrics'][o['metric']]) for o in c['objectives'])+"\n配置："+json.dumps(c['parameters'],ensure_ascii=False) if c.get('parameters') else "没有达标配置可推荐。")
                essential.append(key)
    if batch_path.exists():
        batch = json.loads(batch_path.read_text())
        candidates = batch["results"]
        compare_keys = set(c["metric"] for c in (plan or {}).get("constraints", []))
        objective = (plan or {}).get("objective")
        if objective:
            compare_keys.add(objective["metric"])
        facts["candidates"] = "候选比较：\n" + "\n".join(
            f"  • 候选 {c['candidate']}（参数值 {c['value']}）：{STATUS.get(c['status'], c['status'])}"
            + ("；" + "，".join(value_text(key, c['metrics'][key]) for key in sorted(compare_keys) if key in c.get('metrics', {})) if compare_keys else "")
            for c in candidates)
        best = batch.get("best_feasible_candidate")
        facts["best"] = f"本次已达标候选中，按指定目标选出候选 {best}；只比较本次候选，不宣称全局最优。" if best else "本次没有选出最佳候选；请查看候选验收与是否指定了可测量的优化目标。"
        essential += ["candidates", "best"]
    if "constraints" in facts:
        essential.append("constraints")
    if "conclusion" in facts:
        essential.append("conclusion")
    if plan:
        wf = plan["workflow"]
        if wf == "sram":
            facts["scope"] = "SNM 对应目标 bitcell；面积为内置公式估算，不能当完整 SRAM 宏版图面积。"
        elif wf == "compressor":
            facts["scope"] = "本次使用简化 LEVEL=1 晶体管模型；面积为估算，不能当真实工艺签核数据。"
        elif wf == "dcim":
            facts["scope"] = "本次覆盖给定测试序列与参考宏库；功能/后端完成不代表覆盖所有输入流或流片签核。"
        elif wf == "floating":
            facts["scope"]="独立整数运算浮点参考使用RNE舍入，包含零/NaN/Inf/非规格化边界；只覆盖本次测试集合。MRED仅在有效有限非零参考样本上计算；出现非有限错误输出时不报告MRED。源码名Exact不等于已证明IEEE正确。"
    if "scope" in facts:
        essential.append("scope")
    integrity = job / "source_integrity.json"
    if integrity.exists():
        unchanged = json.loads(integrity.read_text())["unchanged"]
        facts["integrity"] = "原始 OpenACM 完整性校验通过，文件未发生变化。" if unchanged else "原始 OpenACM 完整性校验未通过，请检查 source_integrity.json。"
        essential.append("integrity")
    return result, facts, essential


def summarize(job, endpoint, model, ui=None, use_model=True):
    """Only trusted evidence reaches output; model output cannot supply numbers."""
    result, facts, essential = evidence(job)
    audit = job / "reports/summaries" / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    audit.mkdir(parents=True)
    fallback_order = list(facts)
    alternatives = ["可以继续提出参数修改，我会按同样流程重新验证。", ""]
    if result["status"] == "failed":
        alternatives = ["建议先根据失败阶段和日志修复运行条件，再重新仿真。", ""]
    elif result["status"] == "infeasible":
        alternatives = ["当前设计未满足全部要求；可以给出参数候选范围，比较其它设计。", ""]
    elif result["status"] == "unverified_constraints":
        alternatives = ["需要先补充对应指标的测量流程，再判断未验证要求是否达标。", ""]
    elif "conclusion" in facts and "routed_power_uw" not in result.get("metrics", {}):
        alternatives = ["乘法功能已验证；若还要评估面积、功耗或时序，需要追加对应的后端评估。", ""]
    selected, recommendation, model_used = fallback_order, alternatives[0], False
    (audit / "evidence.json").write_text(json.dumps({"facts": facts, "essential": essential}, ensure_ascii=False, indent=2) + "\n")
    if use_model:
        if ui:
            ui.status("仿真结果已就绪，DeepSeek 正在整理总结…")
        else:
            print("仿真结果已就绪，DeepSeek 正在整理总结…", flush=True)
        schema = {"type": "object", "additionalProperties": False, "properties": {
            "highlights": {"type": "array", "minItems": 1, "maxItems": len(facts),
                           "items": {"type": "string", "enum": list(facts)}},
            "next_step": {"type": "string", "enum": alternatives}}, "required": ["highlights", "next_step"]}
        body = {"model": model, "stream": False, "format": schema,
                "messages": [{"role": "system", "content": "你是电路仿真结果总结助手。仿真已经执行，下面给出唯一可信的实测事实。选择对用户最有用的事实ID并按结论、关键数据、验收、范围排列。必须选入essential中的所有ID。可以省略次要指标，不能重复ID。选择合适的next_step。只输出指定JSON，不执行命令。"},
                             {"role": "user", "content": json.dumps({"facts": facts, "essential": essential}, ensure_ascii=False)}],
                "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 700}}
        (audit / "request.json").write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
        try:
            client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request(endpoint + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with client.open(request, timeout=120) as response:
                reply = json.load(response)
            (audit / "reply.json").write_text(json.dumps(reply, ensure_ascii=False, indent=2) + "\n")
            choice = json.loads(reply["message"]["content"])
            ids = choice["highlights"]
            if not isinstance(ids, list) or not ids or any(x not in facts for x in ids) or choice["next_step"] not in alternatives:
                raise ValueError("总结返回了未经实测的字段或建议")
            selected = list(dict.fromkeys(ids + essential))
            # Lead with the actual outcome even if the model orders it later.
            selected = ["status"] + [x for x in selected if x != "status"]
            recommendation, model_used = choice["next_step"], True
        except Exception as exc:
            (audit / "error.txt").write_text(str(exc) + "\n")
    text = "\n\n".join(facts[key] for key in selected)
    if recommendation:
        text += "\n\n" + recommendation
    if use_model and not model_used:
        text += "\n\n（DeepSeek 总结暂不可用，以上为依据实测数据直接整理的结果。）"
    report = job / ("report.md" if "cases" in result or result.get("kind") == "exploration" or (job / "batch_results.json").exists() else "reports/report.md")
    text += f"\n\n详细报告：{report}"
    rendered = "# 仿真结果总结\n\n" + text + "\n"
    (audit / "summary.md").write_text(rendered)
    (job / "reports/summary.md").write_text(rendered)
    (audit / "metadata.json").write_text(json.dumps({"model_used": model_used, "selected_facts": selected}, ensure_ascii=False, indent=2) + "\n")
    if ui:
        print("\n" + ui.paint("━━ 仿真结果总结 ━━", 1, bold=True))
        print(text, flush=True)
    else:
        print("\n━━ 仿真结果总结 ━━\n" + text, flush=True)
    return text
