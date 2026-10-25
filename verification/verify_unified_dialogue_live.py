"""Historical discussion and mixed explanation/execution with real DeepSeek."""
from datetime import datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import assistant
from conversation import Session


def main():
    folder = ROOT / "runs" / (datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S") + "_unified_dialogue_validation")
    folder.mkdir()
    before = assistant.manifest(assistant.SOURCE)
    session, records = Session(), []
    historical = ROOT / "runs/20261011_140055_explore_multiplier_fdabd4"
    original = json.loads((historical / "result.json").read_text())
    best = next(c for c in original["candidates"] if c["candidate"] == original["best_feasible_candidate"])
    try:
        questions = [
            "在之前的对话记录中我已经有了一些探索得到的数值，我可以对其做出什麽操作",
            f"具体看看{historical.name}，解释它的最佳配置和数值可以如何利用，先不运行",
        ]
        for question in questions:
            print("完整对话：" + question, flush=True)
            outcome = session.turn(assistant, question)
            assert outcome["mode"] == "consult" and outcome["model_used"]
            records.append({"question": question, **outcome})
            assistant.write_json(folder / "turns.json", records)
        assert session.last_task == historical
        assert "MRED" in records[-1]["text"]
        audit = json.loads((Path(records[-1]["audit_directory"]) / "facts.json").read_text())["facts"]
        assert f"{best['metrics']['mred']:.6g}" in audit["result_best_mred"]
        assert "use_compare" in audit and "use_training" in audit
        # Stay in this session: a single turn explains and starts a concrete new design.
        question = "解释精确乘法器验证方式，然后运行4位精确乘法器，穷举所有输入"
        print("完整对话：" + question, flush=True)
        outcome = session.turn(assistant, question)
        assert outcome["mode"] == "run"
        job = outcome["job"]
        result = json.loads((job / "result.json").read_text())
        assert result["status"] == "completed" and result["metrics"]["sample_count"] == 256 and result["metrics"]["error_count"] == 0
        records.append({"question": question, "mode": outcome["mode"], "job": str(job), "result": result})
        question = "刚才结果如何，后续还需要什么验证"
        outcome = session.turn(assistant, question)
        assert outcome["mode"] == "consult" and outcome["model_used"] and "256" in outcome["text"]
        assert session.last_task == job
        records.append({"question": question, **outcome})
        assistant.write_json(folder / "turns.json", records)
        assistant.write_json(folder / "result.json", {"kind": "unified_dialogue_validation", "status": "completed",
            "model": assistant.MODEL, "historical_task": str(historical), "execution_tasks": [str(job)], "turn_count": len(records)})
        (folder / "report.md").write_text("# 统一连续对话实测\n\n" + "\n\n".join(
            f"用户：{r['question']}\n\n回复：{r.get('text', r['mode'])}" for r in records)
            + f"\n\n历史追问没有启动EDA；混合解释/执行的一轮启动一个新任务：{job}。256组穷举，0错误。\n")
        print("PASS: historical-data discussion, explicit task grounding, mixed explanation/execution and measured follow-up")
        print("验证报告：" + str(folder / "report.md"))
    except Exception as exc:
        assistant.write_json(folder / "result.json", {"kind": "unified_dialogue_validation", "status": "failed", "error": str(exc)})
        raise
    finally:
        assistant.write_json(folder / "source_integrity.json", {"unchanged": before == assistant.manifest(assistant.SOURCE), "checked_files": len(before)})


if __name__ == "__main__":
    main()
