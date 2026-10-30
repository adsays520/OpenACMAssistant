"""Real local model regression for the reported search request; planning only."""
from datetime import datetime
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


def main():
    folder = ROOT / "runs" / (datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
                              + "_search_intent_validation_" + uuid.uuid4().hex[:6])
    folder.mkdir()
    before = assistant.manifest(assistant.SOURCE)
    session, turns = Session(), []
    try:
        with patch.object(assistant, "run_plan", side_effect=AssertionError("unexpected EDA")), \
                patch("acco.launch", side_effect=AssertionError("unexpected search execution")), \
                patch("benchmark.launch", side_effect=AssertionError("unexpected comparison execution")):
            cases = [
                ("开展8位近似乘法器的架构搜索对照实验。", False, "clarify"),
                ("如何开展这个对照实验，先不要运行？", False, "consult"),
                ("算术测试固定为种子42、采样4096组；所有方法使用相同的综合库、5ns输入活动周期和10fF输出负载。将搜索随机种子与测试输入种子分开，每种方法重复5次，每次最多评价32个不同架构。", True, "plan"),
            ]
            for text, only, expected in cases:
                print("真实模型验证：" + text, flush=True)
                outcome = session.turn(assistant, text, plan_only=only)
                assert outcome["mode"] == expected, outcome
                if expected in ("clarify", "consult"):
                    audit = Path(outcome["audit_directory"])
                else:
                    # The dialogue audit for this turn is distinct from planner audits.
                    matches = [p.parent for p in (ROOT / "runs").glob("*_dialogue_*/request.json")
                               if json.loads(p.read_text())["messages"][-1]["content"] == text]
                    audit = max(matches, key=lambda p: p.stat().st_mtime_ns)
                metadata = json.loads((audit / "metadata.json").read_text())
                assert metadata["model_used"], f"DeepSeek was unavailable; see {audit}"
                if expected == "clarify":
                    assert "每轮最多评价" in outcome["text"] and session.pending
                elif expected == "consult":
                    assert session.pending["kind"] == "search_comparison"
                else:
                    config = outcome["plan"]
                    assert config["kind"] == "search_comparison"
                    assert config["experiment"]["plan"]["parameters"]["bit_width"] == 8
                    assert config["experiment"]["plan"]["parameters"]["kind"] == "approx"
                    assert config["experiment"]["plan"]["parameters"]["seed"] == 42
                    assert config["candidate_budget"] == 32 and config["search_seeds"] == [1, 2, 3, 4, 5]
                    assert config["experiment"]["plan"]["constraints"] == []
                    assert session.last_task is None
                turns.append({"request": text, "expected_mode": expected, "outcome": outcome,
                              "dialogue_audit": str(audit), "metadata": metadata})
                assistant.write_json(folder / "turns.json", turns)
        assistant.write_json(folder / "result.json", {"kind": "search_intent_validation", "status": "completed",
            "model": assistant.MODEL, "turn_count": len(turns), "eda_started": False})
        (folder / "report.md").write_text("# 架构搜索意图真实模型复测\n\n" + "\n\n".join(
            f"用户：{r['request']}\n\n动作：{r['outcome']['mode']}；真实模型：{r['metadata']['model_used']}\n\n"
            + r["outcome"].get("text", json.dumps(r["outcome"].get("plan"), ensure_ascii=False, indent=2))
            for r in turns) + "\n\n仅验证对话与计划，没有启动EDA或算法对照实验。\n")
        print("PASS: real DeepSeek request recognition, budget clarification, retained context and exact user comparison-plan continuation", flush=True)
        print("验证报告：" + str(folder / "report.md"), flush=True)
    except Exception as exc:
        assistant.write_json(folder / "result.json", {"kind": "search_intent_validation", "status": "failed", "error": str(exc)})
        print("诊断目录：" + str(folder), flush=True)
        raise
    finally:
        unchanged = before == assistant.manifest(assistant.SOURCE)
        assistant.write_json(folder / "source_integrity.json", {"unchanged": unchanged, "checked_files": len(before)})
        assert unchanged


if __name__ == "__main__":
    main()
