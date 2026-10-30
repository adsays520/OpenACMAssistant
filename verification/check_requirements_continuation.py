"""Regression: the reported paragraph is useful with no preceding command or /plan."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import assistant
from conversation import Session, dialogue
from terminal_ui import review_plan

TEXT = ("算术测试固定为种子42、采样4096组；所有方法使用相同的综合库、5ns输入活动周期和10fF输出负载。"
        "将搜索随机种子与测试输入种子分开，每种方法重复5次，每次最多评价32个不同架构")
BAD_FACTS = ["public_status", "verification_exact", "use_compare", "use_retest", "use_continue", "use_training", "use_next"]


def reply(operation="explore", ids=BAD_FACTS):
    return io.StringIO(json.dumps({"message": {"content": json.dumps({
        "operation": operation, "message": "你希望进行架构对照。", "fact_ids": ids})}}))


def check_config(config):
    assert config["kind"] == "search_comparison"
    assert config["candidate_budget"] == 32 and config["search_seeds"] == [1, 2, 3, 4, 5]
    p = config["experiment"]["plan"]["parameters"]
    assert p["seed"] == 42 and p["samples"] == 4096 and p["bit_width"] == 8
    assert config["experiment"]["physical_evaluation"] == {"clock_period_ns": 5., "output_load_ff": 10.}


def main():
    with TemporaryDirectory(dir=ROOT / "verification") as tmp, patch.object(assistant, "ROOT", Path(tmp)), \
            patch.object(assistant, "run_plan", side_effect=AssertionError("unrequested run")), \
            patch("benchmark.launch", side_effect=AssertionError("unrequested comparison")):
        root = Path(tmp)
        shutil.copytree(ROOT / "skills", root / "skills")
        # Replay the actual model's unrelated selection; no fact dumping is possible.
        for model in (False, True):
            session = Session()
            with patch("conversation.urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = lambda *a, **kw: reply()
                out = session.turn(assistant, TEXT, use_model=model)
            assert out["mode"] == "clarify" and "哪种电路" in out["text"]
            assert all(s in out["text"] for s in ("42", "4096", "5ns", "10fF", "5次", "32"))
            assert all(s not in out["text"] for s in ("50000", "公开", "训练", "我可以回答", "已识别"))
            assert session.pending["request"] == TEXT
            # Supplying just the missing circuit forms a preview, not an accidental 15-run batch.
            out = session.turn(assistant, "8位近似乘法器", use_model=False)
            assert out["mode"] == "plan", out
            check_config(out["plan"])
        # Restart after a request that still needs conditions; only chat explicitly restores state.
        session = Session.restore(root)
        out = session.turn(assistant, "开展8位近似乘法器架构搜索对照实验。", use_model=False)
        assert out["mode"] == "clarify"
        session = Session.restore(root)
        assert session.pending and "8位" in session.pending["request"]
        with patch("benchmark.launch") as launch:
            job = root / "runs/comparison_fixture"
            (job / "reports").mkdir(parents=True)
            (job / "reports/summary.md").write_text("fixture")
            launch.return_value = job
            out = session.turn(assistant, TEXT, use_model=False)
            assert out["mode"] == "compare"
            check_config(launch.call_args.args[1])
        # Restart after the standalone conditions, then supply the missing circuit.
        session = Session.restore(root, reset=True)
        session.turn(assistant, TEXT, use_model=False)
        session = Session.restore(root)
        assert session.pending["request"] == TEXT
        out = session.turn(assistant, "8位近似乘法器", use_model=False)
        check_config(out["plan"])
        session = Session.restore(root)
        check_config(session.comparison_draft)
        assert Session.restore(root, reset=True).pending is None
        assert Session.restore(root).comparison_draft is None
        # Asking what a parameter means cannot become an update or an execution.
        for question in ["为什么搜索种子和测试种子要分开？", "采样4096组是什么意思？", "怎么设置5ns输入活动周期？"]:
            with patch("conversation.urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = lambda *a, **kw: reply("run", ["public_status"])
                out = Session().turn(assistant, question)
            assert out["mode"] == "consult"
            assert "50000" not in out["text"] and "训练划分" not in out["text"]
        # Actual CLI chat loop: fresh session, literal request, one clarification, readable preview.
        capture = io.StringIO()
        with patch.object(sys, "argv", ["assistant.py", "chat", "--color", "never"]), \
                patch("builtins.input", side_effect=[TEXT, "8位近似乘法器", "/exit"]), \
                patch("conversation.urllib.request.build_opener") as opener, redirect_stdout(capture):
            opener.return_value.open.side_effect = lambda *a, **kw: reply()
            assistant.main()
        output = capture.getvalue()
        assert "哪种电路" in output and "每种方法运行5次" in output
        assert "4096" in output and "32" in output
        assert not any(s in output for s in ("50000", "已有探索数值可以", "已识别", '"candidate_budget"'))
    print("PASS: exact bare paragraph, irrelevant model-fact replay, circuit-only clarification, CLI preview, persistent context across restarts, /new reset and question safety")


if __name__ == "__main__":
    main()
