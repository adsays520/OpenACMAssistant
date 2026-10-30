"""Acceptance tests of documented dialogues, using real local model and planner.

No EDA is run: previews exercise planning; all executor entry points are blocked.
The fixture records expected behavior before the run. Failures remain in report.
"""
import argparse
from contextlib import redirect_stdout
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import assistant
from conversation import Session


def field(obj, path):
    for key in path.split("."):
        obj = obj[key]
    return obj


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", nargs="*")
    args = parser.parse_args()
    fixture = ROOT / "verification/fixtures/dialogue_examples.json"
    scenarios = json.loads(fixture.read_text())
    if args.cases:
        assert set(args.cases) <= {s["id"] for s in scenarios}
        scenarios = [s for s in scenarios if s["id"] in args.cases]
    folder = ROOT / "runs" / (datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
                              + "_example_acceptance_" + uuid.uuid4().hex[:6])
    folder.mkdir()
    assistant.write_json(folder / "cases.json", scenarios)
    before = assistant.manifest(assistant.SOURCE)
    records = []
    print("验证目录：" + str(folder), flush=True)
    try:
        with patch.object(assistant, "run_plan", side_effect=AssertionError("禁止在对话验证中运行EDA")), \
                patch("acco.launch", side_effect=AssertionError("禁止在对话验证中运行ACCO")), \
                patch("benchmark.launch", side_effect=AssertionError("禁止在对话验证中运行compare")), \
                patch("exploration.explore_command", side_effect=AssertionError("禁止在对话验证中运行探索")):
            for scenario in scenarios:
                state_root = folder / "sessions" / scenario["id"]
                session = Session.restore(state_root)
                if scenario.get("initial_comparison"):
                    session.comparison = json.loads((ROOT / scenario["initial_comparison"]).read_text())
                    session.save()
                if scenario.get("initial_pending_file"):
                    session.pending = {"kind": "search_comparison", "request": (ROOT / scenario["initial_pending_file"]).read_text().strip(),
                                       "message": "至少选择两个不同方法：random/nsga2/llm", "plan_only": True}
                    session.save()
                for i, case in enumerate(scenario["turns"], 1):
                    if scenario.get("restart"):
                        session = Session.restore(state_root)
                    text = case.get("text") or (ROOT / case["text_file"]).read_text().strip()
                    row = {"case": scenario["id"], "turn": i, "request": text, "source": scenario["source"],
                           "preview_override": case.get("preview", False), "restart": bool(scenario.get("restart")), "errors": []}
                    print(f"开始 {row['case']}/{i}：{text[:65]}", flush=True)
                    transcript = io.StringIO()
                    try:
                        with redirect_stdout(transcript):
                            out = session.turn(assistant, text, plan_only=case.get("preview", False))
                        row.update(outcome=out, reply=out["reply"])
                        audit = Path(out["audit_directory"])
                        metadata = json.loads((audit / "metadata.json").read_text())
                        row["metadata"] = metadata
                        if not metadata["model_used"]:
                            row["errors"].append("真实模型回复无效/服务不可用；规则回退不算通过")
                        if out["mode"] != case["mode"]:
                            row["errors"].append(f"动作应为{case['mode']}，实际{out['mode']}")
                        for path, value in case.get("fields", {}).items():
                            try:
                                actual = field(out.get("plan", {}), path)
                                if actual != value:
                                    row["errors"].append(f"{path}: 期望{value}，实际{actual}")
                            except (KeyError, TypeError):
                                row["errors"].append("缺少计划字段：" + path)
                        for token in case.get("contains", []):
                            if token not in out["reply"]:
                                row["errors"].append("回答缺少：" + token)
                        for token in case.get("excludes", []) + ["已识别为", "正在理解", "正在整理需求", "<think>", '"candidate_budget"', '"bit_width"']:
                            if token in out["reply"]:
                                row["errors"].append("回答包含不应出现的内容：" + token)
                        if not out["reply"].strip():
                            row["errors"].append("空回复")
                        if (audit / "answer.md").read_text().strip() != out["reply"].strip():
                            row["errors"].append("审计回答与实际回复不一致")
                        if metadata["executed"] or metadata.get("execution_started"):
                            row["errors"].append("咨询/预览触发执行")
                    except Exception as exc:
                        row["errors"].append(type(exc).__name__ + ": " + str(exc))
                    row["stdout"] = transcript.getvalue()
                    row["passed"] = not row["errors"]
                    records.append(row)
                    assistant.write_json(folder / "turns.json", records)
                    print(("PASS " if row["passed"] else "FAIL ") + f"{row['case']}/{i} " + "; ".join(row["errors"]), flush=True)
        unchanged = before == assistant.manifest(assistant.SOURCE)
        assistant.write_json(folder / "source_integrity.json", {"unchanged": unchanged, "checked_files": len(before)})
        assert unchanged
    finally:
        passed = sum(r["passed"] for r in records)
        assistant.write_json(folder / "result.json", {"kind": "example_acceptance", "status": "completed" if len(records) == sum(len(s["turns"]) for s in scenarios) and passed == len(records) else "failed",
            "model": assistant.MODEL, "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
            "passed": passed, "total": len(records), "eda_started": False,
            "scope": "真实对话与规划模型；执行器禁止调用；不代表实际电路仿真通过"})
        lines = ["# 文档示例对话验收", "", f"真实本地模型：{assistant.MODEL}；{passed}/{len(records)}轮通过。",
                 "", "核对最终自然回复、完整计划字段、约束、跨轮继承、磁盘恢复及审计一致性。所有执行入口均禁止调用；不包含EDA运行验证。", ""]
        for r in records:
            lines += [f"## {r['case']}/{r['turn']}：{'通过' if r['passed'] else '未通过'}", "", "来源：" + r["source"], "", "用户：" + r["request"], "", "助手：", "", r.get("reply", "（异常，无回复）"), ""]
            if r["errors"]:
                lines += ["失败项：" + "；".join(r["errors"]), ""]
        (folder / "report.md").write_text("\n".join(lines))
        print(f"结果 {passed}/{len(records)}；报告：{folder / 'report.md'}", flush=True)
    return 0 if passed == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
