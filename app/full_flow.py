"""Run the four supported reference circuits and collect one evidence report."""
from datetime import datetime
import json
import uuid
from zoneinfo import ZoneInfo


CASES = [
    ("sram", "sram.json", "做16行8列SRAM，1V，TT，27℃，PU90nm，PD90nm，PG135nm，L50nm，跑读写和三种SNM。"),
    ("multiplier", "multiplier.json", "生成8位精确乘法器，穷举验证。"),
    ("compressor", "compressor.json", "把sabetz的NMOS和PMOS都加宽10%，1.2V，27℃，运行SPICE仿真。"),
    ("dcim", "dcim-full.json", "生成16×8精确DCiM全流程，时钟周期10ns，8位精确乘法器穷举；setup slack不低于0ns，hold slack不低于0ns，布线DRC不超过0。"),
]


def run_full_flow(root, source, planner, runner, manifest, use_model=True, summarizer=None):
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    suite = root / "runs" / f"{stamp}_full_flow_{uuid.uuid4().hex[:6]}"
    (suite / "inputs").mkdir(parents=True)
    (suite / "tasks").mkdir()
    results = []
    integrity_ok = None
    before = manifest(source)
    (suite / "source_before.json").write_text(json.dumps(before, indent=2) + "\n")
    print(f"全工程流程目录：{suite}", flush=True)

    def save():
        complete = integrity_ok is not False and len(results) == len(CASES) and all(r["status"] == "completed" for r in results)
        data = {"status": "completed" if complete else "incomplete", "model_planning": use_model,
                "cases": results, "scope": "四类参考电路，含16×8 DCiM完整后端；不含所有研究优化算法与浮点模块"}
        (suite / "result.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        lines = ["# 全工程参考流程报告", "", f"状态：{data['status']}", "",
                 f"本地DeepSeek需求规划：{'启用' if use_model else '关闭，使用固定参考配置'}", "",
                 "| 电路 | 结果 | 任务报告 |", "|---|---|---|"]
        for item in results:
            link = f"[查看]({item['task']}/reports/report.md)" if item.get("task") else item.get("error", "")
            lines.append(f"| {item['workflow']} | {item['status']} | {link} |")
        lines += ["", "覆盖SRAM读写/SNM、乘法器穷举、压缩器SPICE及16×8 DCiM综合/布局布线/寄生/后仿真。",
                  "SRAM独立SPICE与后端参考宏库是两套模型；没有重新表征宏，也不代表所有内置模块或流片签核。",
                  "真实约束结果与限制以各任务报告、backend_stages.json和原始日志为准。"]
        (suite / "report.md").write_text("\n".join(lines) + "\n")

    try:
        for workflow, example, request in CASES:
            item = {"workflow": workflow, "request": request, "status": "failed"}
            print(f"\n全工程流程：{workflow}", flush=True)
            try:
                baseline = json.loads((root / "examples" / example).read_text())
                if use_model:
                    plan, reply = planner(request)
                    if plan.get("intent") != "run" or plan.get("workflow") != workflow:
                        raise RuntimeError("模型没有落实本次电路和执行要求")
                    expected = {"sram": {"operations": ["read", "write", "hold_snm", "read_snm", "write_snm"]},
                                "multiplier": {"exhaustive": True, "kind": "exact", "bit_width": 8},
                                "compressor": {"name": "sabetz", "nmos_scale": 1.1, "pmos_scale": 1.1},
                                "dcim": {"backend": "asic", "rows": 16, "bit_width": 8, "kind": "exact", "clock_period_ns": 10}}[workflow]
                    if any(plan["parameters"].get(key) != value for key, value in expected.items()):
                        raise RuntimeError("模型计划未完整落实参考需求，拒绝执行变更后的设计")
                    if workflow == "dcim" and any(c not in plan["constraints"] for c in baseline["constraints"]):
                        raise RuntimeError("模型遗漏后端时序/DRC验收，拒绝报告全流程通过")
                else:
                    plan, reply = baseline, None
                (suite / "inputs" / f"{workflow}.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
                job = runner(plan, prompt=request, model_reply=reply, job_parent=suite / "tasks")
                result = json.loads((job / "result.json").read_text())
                item.update(status=result["status"], task=str(job.relative_to(suite)), metrics=result.get("metrics", {}))
                if summarizer:
                    summarizer(job)
            except Exception as exc:
                item["error"] = str(exc)
                print(f"{workflow} 未完成：{exc}", flush=True)
            results.append(item)
            save()
    finally:
        after = manifest(source)
        delta = [key for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]
        integrity_ok = not delta
        (suite / "source_integrity.json").write_text(json.dumps({"unchanged": not delta, "checked_files": len(before), "changes": delta}, indent=2) + "\n")
        save()
        if delta:
            raise RuntimeError(f"原仓库完整性变化，见 {suite / 'source_integrity.json'}")
    print(f"汇总报告：{suite / 'report.md'}", flush=True)
    if summarizer:
        summarizer(suite)
    return 0 if all(r["status"] == "completed" for r in results) and len(results) == len(CASES) else 2
