"""Real DeepSeek question chain, explicit draft and one isolated simulation."""
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
    session = Session()
    folder = ROOT / "runs" / (datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S") + "_dialogue_validation")
    folder.mkdir()
    before = assistant.manifest(assistant.SOURCE)
    records = []
    try:
        for question in ["你能干什么", "设计空间探索是什么，先不要运行", "8位乘法器有哪些参数", "MRED是什么意思"]:
            print("咨询验证：" + question, flush=True)
            result = session.turn(assistant, question)
            assert result["mode"] == "consult" and result["model_used"], result
            assert session.last_task is None
            records.append({"question": question, **result})
            assistant.write_json(folder / "turns.json", records)
        question = "做4位精确乘法器，穷举所有输入"
        result = session.turn(assistant, question, plan_only=True)
        assert result["mode"] == "plan" and session.last_task is None
        records.append({"question": "/plan " + question, **result})
        result = session.turn(assistant, "开始运行")
        job = result["job"]
        actual = json.loads((job / "result.json").read_text())
        assert actual["status"] == "completed" and actual["metrics"]["sample_count"] == 256 and actual["metrics"]["error_count"] == 0
        records.append({"question": "开始运行", "mode": result["mode"], "job": str(job), "result": actual})
        result = session.turn(assistant, "刚才结果是否达标，验证了多少组输入")
        assert result["mode"] == "consult" and result["model_used"] and "256" in result["text"]
        assert session.last_task == job
        records.append({"question": "刚才结果是否达标，验证了多少组输入", **result})
        assistant.write_json(folder / "turns.json", records)
        assistant.write_json(folder / "result.json", {"status": "completed", "kind": "dialogue_validation", "questions": 5, "model": assistant.MODEL, "execution_tasks": [str(job)], "turn_count": len(records)})
        (folder / "report.md").write_text("# 真实DeepSeek连续对话验证\n\n" + "\n\n".join(f"用户：{r['question']}\n\n回复：{r.get('text', r['mode'])}" for r in records) + f"\n\n仅“开始运行”启动了1个任务：{job}；4位精确乘法器256组穷举、0错误。\n")
        print("PASS: 5 real-model questions, plan preview, explicit execution, 256-vector exhaustive simulation and grounded result follow-up")
        print("验证报告：" + str(folder / "report.md"))
    except Exception as exc:
        assistant.write_json(folder / "result.json", {"status": "failed", "error": str(exc), "turn_count": len(records)})
        raise
    finally:
        assistant.write_json(folder / "source_integrity.json", {"unchanged": before == assistant.manifest(assistant.SOURCE), "checked_files": len(before)})


if __name__ == "__main__":
    main()
