"""Regression for the reported search command, authorization and experiment scope."""
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import assistant
from acco_spec import NeedsExperiment, from_request
from conversation import Session, dialogue
from exploration import explore_command
from spec import validate

REPORTED = "开展8位近似乘法器的架构搜索对照实验。"


def response(operation):
    return io.StringIO(json.dumps({"message": {"content": json.dumps({
        "operation": operation, "message": "", "fact_ids": []})}}))


def main():
    commands = [REPORTED, "开展8位近似乘法器的架构搜索。", "进行8位近似乘法器的架构搜索。",
                "启动8位近似乘法器的架构寻优。", "帮我开展8位近似乘法器架构搜索，并解释为什么这样做。"]
    questions = ["如何开展8位近似乘法器的架构搜索对照实验？", "开展架构搜索需要哪些参数？",
                 "进行过哪些实验？", "开展过哪些架构搜索？", "先不要开展架构搜索对照实验，解释一下方法。",
                 "你能干什么", "之前探索得到的数据可以做什么？"]
    checks = 0
    with TemporaryDirectory(dir=ROOT / "verification") as tmp, patch.object(assistant, "ROOT", Path(tmp)), \
            patch.object(assistant, "plan_request") as planner, patch.object(assistant, "run_plan") as run, \
            patch("acco.launch") as launch:
        for command in commands:
            outcome = dialogue(assistant, command, use_model=False)
            assert outcome["operation"] == "explore", (command, outcome)
            checks += 1
        # The actual DeepSeek output was explore; the old authorization rule blocked it.
        # A mistaken respond/run must also preserve a clearly requested search task.
        for model_op in ("explore", "respond", "run"):
            with patch("conversation.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = response(model_op)
                outcome = dialogue(assistant, REPORTED)
            assert outcome["model_used"] and outcome["operation"] == "explore"
            assert "我可以回答OpenACM能力" not in outcome["text"]
            metadata = json.loads((Path(outcome["audit_directory"]) / "metadata.json").read_text())
            assert metadata["model_operation"] == model_op
            checks += 1
        for question in questions:
            with patch("conversation.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = response("explore")
                outcome = Session().turn(assistant, question)
            assert outcome["mode"] == "consult", (question, outcome)
            checks += 1
        # No single-run execution can stand in for a comparative experiment.
        for use_model in (False, True):
            session = Session()
            with patch("conversation.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = response("explore")
                outcome = session.turn(assistant, REPORTED, use_model=use_model)
            assert outcome["mode"] == "clarify" and "每轮最多评价" in outcome["text"]
            assert session.pending["kind"] == "search_comparison"
            audit = Path(outcome["audit_directory"]) / "comparison_request.json"
            assert json.loads(audit.read_text())["execution_started"] is False
            outcome = session.turn(assistant, "MRED是什么", use_model=False)
            assert outcome["mode"] == "consult" and REPORTED in session.pending["request"]
            outcome = session.turn(assistant, "随机、NSGA-II和LLM，预算32个候选", use_model=False)
            assert outcome["mode"] == "clarify" and "随机、NSGA-II和LLM" in session.pending["request"]
            assert REPORTED in session.pending["request"]
            checks += 3
        planner.assert_not_called()
        run.assert_not_called()
        launch.assert_not_called()
        # An explicit change to a single search leaves the comparison pending state.
        outcome = session.turn(assistant, "改成单次探索8位近似乘法器，预算2个候选，MRED≤0.02",
                               plan_only=True, use_model=False)
        assert outcome["mode"] == "plan" and outcome["plan"]["budgets"]["architecture"] == 2
        assert session.experiment_draft["plan"]["constraints"] == [{"metric": "mred", "op": "le", "value": .02}]
        launch.assert_not_called()
        # Direct commands and config conversion must not silently lose comparison scope.
        plan = validate({"workflow": "multiplier", "parameters": {"kind": "approx", "bit_width": 8}})
        from acco import command
        for call in (lambda: command(assistant, REPORTED), lambda: explore_command(assistant, REPORTED),
                     lambda: from_request(REPORTED, plan)):
            try:
                call()
            except NeedsExperiment as exc:
                assert "每轮最多评价" in str(exc) or "compare入口" in str(exc)
            else:
                raise AssertionError("对照要求被降成单次搜索")
            checks += 1
        planner.assert_not_called()
        run.assert_not_called()
        launch.assert_not_called()
    print(f"PASS: {checks} search/consultation, model-intent, comparison-scope and continuity checks; no EDA launched")


if __name__ == "__main__":
    main()
