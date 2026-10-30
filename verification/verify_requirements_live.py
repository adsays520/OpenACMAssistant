"""Real DeepSeek, literal reported input, fresh session and restart continuation."""
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
from terminal_ui import review_plan

TEXT = ("算术测试固定为种子42、采样4096组；所有方法使用相同的综合库、5ns输入活动周期和10fF输出负载。"
        "将搜索随机种子与测试输入种子分开，每种方法重复5次，每次最多评价32个不同架构")


def main():
    folder = ROOT / "runs" / (datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
                              + "_requirements_validation_" + uuid.uuid4().hex[:6])
    folder.mkdir()
    before = assistant.manifest(assistant.SOURCE)
    records = []
    try:
        state_root = folder / "isolated_chat"
        with patch.object(assistant, "run_plan", side_effect=AssertionError("unrequested EDA")), \
                patch("benchmark.launch", side_effect=AssertionError("unrequested comparison")):
            for i, text in enumerate((TEXT, "8位近似乘法器")):
                session = Session.restore(state_root)  # A real restart between the two messages.
                print("用户：" + text, flush=True)
                out = session.turn(assistant, text)  # No /plan and no plan_only override.
                audit = Path(out["audit_directory"])
                metadata = json.loads((audit / "metadata.json").read_text())
                assert metadata["model_used"], f"未调用真实模型：{audit}"
                if i == 0:
                    assert out["mode"] == "clarify" and "哪种电路" in out["text"]
                    assert all(s in out["text"] for s in ("42", "4096", "5ns", "10fF", "5次", "32"))
                    assert all(s not in out["text"] for s in ("50000", "公开", "训练", "我可以回答", "已识别"))
                    assert set(json.loads((audit / "facts.json").read_text())["facts"]) == {"requirements"}
                    reply = out["text"]
                else:
                    assert out["mode"] == "plan"
                    c = out["plan"]
                    assert c["kind"] == "search_comparison" and c["candidate_budget"] == 32 and c["search_seeds"] == [1, 2, 3, 4, 5]
                    p = c["experiment"]["plan"]["parameters"]
                    assert p["bit_width"] == 8 and p["seed"] == 42 and p["samples"] == 4096
                    assert c["experiment"]["physical_evaluation"] == {"clock_period_ns": 5., "output_load_ff": 10.}
                    assert c["experiment"]["plan"]["constraints"] == []
                    reply = review_plan(c)
                records.append({"request": text, "reply": reply, "outcome": out, "metadata": metadata})
                assistant.write_json(folder / "turns.json", records)
                print("助手：" + reply, flush=True)
        restored = Session.restore(state_root)
        assert restored.comparison_draft["candidate_budget"] == 32
        assistant.write_json(folder / "result.json", {"kind": "requirements_validation", "status": "completed",
            "model": assistant.MODEL, "turn_count": len(records), "restart_between_turns": True,
            "explicit_plan_command": False, "eda_started": False})
        (folder / "report.md").write_text("# 条件补充与重启衔接：真实模型验证\n\n" + "\n\n".join(
            f"用户：{r['request']}\n\n助手：{r['reply']}\n\n实际模型：{r['metadata']['model_used']}；动作：{r['outcome']['mode']}" for r in records)
            + "\n\n两轮之间重建Session并从磁盘恢复，没有/plan、没有预设电路上下文、没有运行EDA。\n")
        print("PASS；报告：" + str(folder / "report.md"), flush=True)
    except Exception as exc:
        assistant.write_json(folder / "result.json", {"kind": "requirements_validation", "status": "failed", "error": str(exc)})
        print("失败记录：" + str(folder), flush=True)
        raise
    finally:
        unchanged = before == assistant.manifest(assistant.SOURCE)
        assistant.write_json(folder / "source_integrity.json", {"unchanged": unchanged, "checked_files": len(before)})
        assert unchanged


if __name__ == "__main__":
    main()
