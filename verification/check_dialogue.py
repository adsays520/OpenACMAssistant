"""Behavioral checks: questions cannot execute, and follow-ups preserve state."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import assistant
from conversation import Session, route, answer, dialogue, history_task
from spec import validate


def main():
    questions = ["你能干什么", "你能帮我做什么", "你能干什麽", "支持哪些电路", "能不能做128位乘法器",
                 "SRAM有哪些参数", "MRED是什么", "NMED是什么意思", "怎么开始仿真", "怎么重新跑",
                 "设计空间探索是什么", "怎么探索8位近似乘法器", "给我优化建议", "为什么上次运行失败",
                 "结果达标了吗", "跑完了么", "只是问参数，不要跑仿真", "训练集需要什么", "hello",
                 "在之前的对话记录中我已经有了一些探索得到的数值，我可以对其做出什麽操作",
                 "这些探索数值还能干啥", "这些数据有什么用途", "我可以如何利用已有的探索结果"]
    assert all(route(q) == "consult" for q in questions)
    assert route("做8位精确乘法器并解释结果") == "action"
    assert route("生成的8位乘法器支持什么输入") == "consult"
    with TemporaryDirectory(dir=ROOT / "verification") as tmp:
        root = Path(tmp)
        shutil.copytree(ROOT / "skills", root / "skills")
        with patch.object(assistant, "ROOT", root), patch.object(assistant, "plan_request") as planner, \
                patch.object(assistant, "run_plan") as runner, patch.object(assistant, "summarize") as summary:
            session = Session()
            for question in questions:
                output = session.turn(assistant, question, use_model=False)
                assert output["mode"] == "consult" and output["text"]
                metadata = json.loads((Path(output["audit_directory"]) / "metadata.json").read_text())
                assert not metadata["executed"]
            planner.assert_not_called()
            runner.assert_not_called()
            output = session.turn(assistant, "帮我跑仿真", use_model=False)
            assert output["mode"] == "clarify" and session.pending
            session.turn(assistant, "MRED是什么", use_model=False)
            assert session.pending  # Asking during clarification does not discard the original request.
            plan = validate({"workflow": "multiplier", "parameters": {"bit_width": 8, "kind": "exact", "exhaustive": True}})
            planner.return_value = (plan, {})
            output = session.turn(assistant, "8位精确乘法器，穷举", plan_only=True, use_model=False)
            assert output["mode"] == "plan" and session.draft == plan and session.last_task is None
            assert "原始待澄清需求" in planner.call_args.args[0] and "帮我跑仿真" in planner.call_args.args[0]
            runner.assert_not_called()
            session.turn(assistant, "这个乘法器支持哪些参数", use_model=False)
            assert session.draft == plan
            output = session.turn(assistant, "这个计划是否达标", use_model=False)
            assert "还未" in output["text"] and session.last_task is None
            output = session.turn(assistant, "先给我看8位乘法器的计划", use_model=False)
            assert output["mode"] == "plan"
            runner.assert_not_called()
            job = root / "runs/simulated"
            job.mkdir(parents=True)
            (job / "result.json").write_text(json.dumps({"status": "infeasible", "metrics": {"mred": .03},
                                                        "constraints": [{"metric": "mred", "op": "le", "value": .02, "actual": .03, "passed": False}]}))
            runner.return_value = job
            summary.return_value = "实测MRED=0.03，未达标。"
            calls = planner.call_count
            session.turn(assistant, "开始运行", use_model=False)
            assert planner.call_count == calls and runner.call_count == 1
            previous = session.previous.copy()
            output = session.turn(assistant, "为什么没有达标", use_model=False)
            assert "0.03" in output["text"] and "不通过" in output["text"]
            assert runner.call_count == 1 and session.previous == previous
            changed = validate({**plan, "parameters": {**plan["parameters"], "bit_width": 4}})
            planner.return_value = (changed, {})
            session.turn(assistant, "其余不变，只把位宽改为4位，重新跑", use_model=False)
            assert planner.call_args.args[1] == previous and runner.call_count == 2
            # A hostile or broken model response cannot inject executable actions or new facts.
            for response in ["broken", '{"fact_ids":["invented_success"],"intent":"run"}']:
                with patch("conversation.urllib.request.build_opener") as opener:
                    opener.return_value.open.return_value = io.StringIO(json.dumps({"message": {"content": response}}))
                    output = answer(assistant, "你能干什么")
                assert output["mode"] == "consult" and not output["model_used"]
                assert "五类" in output["text"] and "invented_success" not in output["text"]
            with patch("conversation.urllib.request.build_opener") as opener:
                opener.return_value.open.side_effect = ConnectionError("offline")
                output = answer(assistant, "你能干什么")
            assert not output["model_used"] and "五类" in output["text"]
        # Historical evidence survives restarting chat; audit/validation results are not EDA tasks.
        history = root / "runs/20261011_140055_explore_multiplier_example"
        history.mkdir()
        base = validate({"workflow": "multiplier", "parameters": {"kind": "approx", "compressor_id": 7, "backend": "synthesis"}})
        (history / "best_plan.json").write_text(json.dumps(base))
        (history / "result.json").write_text(json.dumps({"kind": "exploration", "status": "completed", "budget": 2,
            "config": {"plan": base}, "best_feasible_candidate": 1, "pareto_candidates": [1],
            "candidates": [{"candidate": 1, "parameters": base["parameters"], "metrics": {"mred": .01, "synthesized_area_um2": 280}}]}))
        validation = root / "runs/20990101_000000_validation"
        validation.mkdir()
        (validation / "result.json").write_text('{"status":"completed","kind":"dialogue_validation"}')
        assert history_task(root, "之前探索数值") == history
        with patch.object(assistant, "ROOT", root), patch.object(assistant, "plan_request") as planner, \
                patch.object(assistant, "run_plan") as runner, patch.object(assistant, "summarize") as summary:
            session = Session()
            question = questions[-4]
            with patch("conversation.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = io.StringIO(json.dumps({"message": {"content": json.dumps({
                    "operation": "respond", "message": "可以基于已有观测挑选起点，再确定复测或继续搜索的目标。", "fact_ids": ["use_compare", "use_retest"]})}}))
                output = session.turn(assistant, question)
                sent = json.loads(opener.return_value.open.call_args.args[0].data)
                sent = json.loads(sent["messages"][1]["content"].split("\n", 1)[1])
            assert output["model_used"] and "0.01" in output["text"] and "280" in output["text"]
            assert session.last_task == history and sent["state"]["task"] == str(history)
            assert "result_best_parameters" in sent["facts"]
            planner.assert_not_called()
            runner.assert_not_called()
            # One response can explain the historical design and ask a validated tool to run.
            changed = validate({**base, "parameters": {**base["parameters"], "exhaustive": True}})
            planner.return_value = (changed, {})
            runner.return_value = root / "runs/simulated"
            summary.return_value = "复测结果保存在新的任务。"
            with patch("conversation.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = io.StringIO(json.dumps({"message": {"content": json.dumps({
                    "operation": "run", "message": "我会结合刚才最佳配置进行穷举复测，并整理结果。", "fact_ids": ["use_retest"]})}}))
                session.turn(assistant, "解释刚才的最佳配置，然后复测，改为穷举")
                sent = json.loads(opener.return_value.open.call_args.args[0].data)
                history_messages = sent["messages"][2:-1]
                assert sent["messages"][-1]["content"] == "解释刚才的最佳配置，然后复测，改为穷举"
                sent = json.loads(sent["messages"][1]["content"].split("\n", 1)[1])
            assert runner.call_count == 1 and planner.call_args.args[1]["parameters"] == base["parameters"]
            assert planner.call_args.args[1]["constraints"] == base["constraints"]
            assert history_messages[-2]["content"] == question
            # An erroneous model tool choice cannot turn the user's historical question into EDA.
            with patch("conversation.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = io.StringIO(json.dumps({"message": {"content": json.dumps({
                    "operation": "explore", "message": "开始探索。", "fact_ids": ["use_training"]})}}))
                output = session.turn(assistant, question)
            assert output["mode"] == "consult" and runner.call_count == 1
            # Unsupported prose measurements are discarded; literal evidence is still rendered.
            with patch("conversation.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = io.StringIO(json.dumps({"message": {"content": json.dumps({
                    "operation": "respond", "message": "MRED为0.000001，已保证全局最优。", "fact_ids": ["use_compare"]})}}))
                output = dialogue(assistant, question)
            assert "0.000001" not in output["text"] and "已保证全局最优" not in output["text"]
        with patch.object(assistant, "ROOT", root), patch("assistant.urllib.request.build_opener") as opener:
            old = validate({"workflow": "multiplier", "parameters": {"kind": "approx", "compressor_id": 7},
                            "constraints": [{"metric": "mred", "op": "le", "value": .02}]})
            expected = validate({**old, "parameters": {**old["parameters"], "seed": 99}})
            opener.return_value.open.return_value = io.StringIO(json.dumps({"message": {"content": json.dumps(expected)}}))
            updated, _ = assistant.plan_request("其余不变，只把种子改为99，重新跑", previous=old)
            sent = json.loads(opener.return_value.open.call_args.args[0].data)
            assert updated["parameters"]["seed"] == 99
            assert updated["constraints"] == old["constraints"]
            assert sent["format"]["properties"]["constraints"]["const"] == old["constraints"]
        with patch.object(assistant, "ROOT", root), patch("assistant.urllib.request.build_opener") as opener:
            expected = validate({**old, "parameters": {**old["parameters"], "exhaustive": True}})
            opener.return_value.open.return_value = io.StringIO(json.dumps({"message": {"content": json.dumps(expected)}}))
            updated, _ = assistant.plan_request("解释刚才最佳配置，然后复测，改为穷举", previous=old)
            sent = json.loads(opener.return_value.open.call_args.args[0].data)
            assert updated["parameters"]["compressor_id"] == old["parameters"]["compressor_id"]
            assert updated["parameters"]["exhaustive"] and updated["constraints"] == old["constraints"]
            assert sent["format"]["properties"]["parameters"]["properties"]["compressor_id"]["const"] == 7
        with patch.object(assistant, "ROOT", root), patch("assistant.urllib.request.build_opener") as opener:
            raw = {"intent": "run", "workflow": "multiplier", "parameters": {"kind": "exact", "bit_width": 4, "exhaustive": True, "compressor_ids": []}}
            opener.return_value.open.return_value = io.StringIO(json.dumps({"message": {"content": json.dumps(raw)}}))
            exact, _ = assistant.plan_request("做4位精确乘法器，穷举")
            assert exact["intent"] == "run" and "compressor_ids" not in exact["parameters"]
    print(f"PASS: {len(questions)} question routes, no unintended EDA, clarification continuity, draft execution, measured-result Q&A, follow-up inheritance and offline/invalid-model fallback")


if __name__ == "__main__":
    main()
