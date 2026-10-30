"""Final replies must survive consultation, planning, execution and errors."""
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
from conversation import Session, knowledge
from spec import validate


def checked(out, mode, executed=False):
    assert out["mode"] == mode
    audit = Path(out["audit_directory"])
    assert (audit / "answer.md").read_text().strip() == out["reply"].strip()
    metadata = json.loads((audit / "metadata.json").read_text())
    assert metadata["final_mode"] == mode and metadata["executed"] is executed
    assert metadata["response_complete"] and out["reply"].strip()
    return audit


def main():
    with TemporaryDirectory(dir=ROOT / "verification") as tmp:
        root = Path(tmp)
        shutil.copytree(ROOT / "skills", root / "skills")
        with patch.object(assistant, "ROOT", root):
            session = Session.restore(root)
            out = session.turn(assistant, "8位乘法器有哪些参数？MRED和NMED是什么？", use_model=False)
            checked(out, "consult")
            assert all(s in out["reply"] for s in ("位宽", "MRED", "NMED"))
            assert all(s not in out["reply"] for s in ('"bit_width"', "默认配置"))
            plan = validate({"workflow": "sram", "parameters": {"operations": ["read", "write", "read_snm"]}})
            with patch.object(assistant, "plan_request", return_value=(plan, {})):
                out = session.turn(assistant, "先给我看16行8列SRAM的计划。", use_model=False)
            audit = checked(out, "plan")
            assert json.loads((audit / "final_plan.json").read_text()) == plan
            assert "PU=90nm" in out["reply"] and "read_snm" not in out["reply"]
            assert Session.restore(root).conversation[-1]["content"] == out["reply"]
            session = Session.restore(root)
            out = session.turn(assistant, "这里的bank是什么意思？", use_model=False)
            checked(out, "consult")
            assert "子阵列" in out["reply"] and session.draft["parameters"] == plan["parameters"]
            assert "{\"" not in out["reply"]
            job = root / "runs/fixture_simulation"
            (job / "reports").mkdir(parents=True)
            (job / "result.json").write_text(json.dumps({"status": "failed", "error": "fixture failure"}))
            with patch.object(assistant, "run_plan", return_value=job), patch.object(assistant, "summarize", return_value="测试执行器失败；没有实测值。"):
                out = session.turn(assistant, "开始运行", use_model=False)
            checked(out, "run", executed=True)
            assert out["reply"] == "测试执行器失败；没有实测值。"
            with patch.object(assistant, "plan_request", side_effect=ValueError("模拟无效参数")):
                try:
                    session.turn(assistant, "其余不变，只把电压改为0.9V，重新仿真。", use_model=False)
                except ValueError as exc:
                    audit = session._turn_audit
                    assert (audit / "answer.md").read_text().strip() == session.error_reply(exc)
                    meta = json.loads((audit / "metadata.json").read_text())
                    assert meta["final_mode"] == "error" and not meta["execution_started"]
                else:
                    raise AssertionError("测试错误未出现")
            # The historical failure was input context exhaustion, not bad JSON syntax.
            valid = {"done_reason": "stop", "message": {"content": json.dumps({"intent": "run", "workflow": "sram", "parameters": {"vdd_v": .9}, "constraints": []})}}
            previous = validate({"workflow": "sram", "parameters": {"operations": ["read", "write", "hold_snm", "read_snm", "write_snm"]}})
            with patch("assistant.urllib.request.build_opener") as opener:
                opener.return_value.open.return_value = io.StringIO(json.dumps(valid))
                updated, _ = assistant.plan_request("其余不变，只把电压改为0.9V，重新仿真。", previous,
                    [{"role": "assistant", "content": "旧回复" * 4000}])
            assert updated["parameters"] == {**previous["parameters"], "vdd_v": .9}
            body = json.loads(opener.return_value.open.call_args.args[0].data)
            assert "## floating" not in body["messages"][0]["content"] and "## sram" in body["messages"][0]["content"]
            assert body["format"]["properties"]["parameters"]["required"] == ["vdd_v"]
            assert len(body["messages"][2]["content"]) <= 600
            from terminal_ui import review_plan
            dcim = validate({"workflow": "dcim", "parameters": {"backend": "asic"}})
            assert "时钟周期10ns" in review_plan(dcim)
            assert "按尺寸自动匹配参考宏" in review_plan(dcim)
            # Explicit historical references must replace every focused fact,
            # including essential facts, rather than mixing two tasks.
            recent = root / "runs/20990101_000001_sram_fixture"
            recent.mkdir()
            (recent / "result.json").write_text(json.dumps({
                "status": "completed", "metrics": {"read_delay_ns": 987.654},
                "config": {"plan": {"workflow": "sram"}}}))
            older = root / "runs/20200101_000000_explore_multiplier_fixture"
            older.mkdir()
            (older / "result.json").write_text(json.dumps({
                "kind": "exploration", "status": "completed", "budget": 1,
                "config": {"plan": {"workflow": "multiplier", "parameters": {"seed": 42, "samples": 4096}}},
                "best_feasible_candidate": 1, "candidates": [{"candidate": 1,
                    "parameters": {}, "metrics": {"mred": .01, "synthesized_area_um2": 291.802}}]}))
            from conversation import dialogue
            out = dialogue(assistant, "总结任务20200101_000000_explore_multiplier_fixture的探索结果", task=recent, use_model=False)
            assert out["task"] == str(older)
            assert "291.802" in out["text"] and "987.654" not in out["text"]
            topic, facts = knowledge(assistant, "经验库怎样记录和利用设计结果？会自动训练模型权重吗？", task=recent)
            assert topic == "learning" and "learning" in facts
            assert not any(k.startswith("result_") for k in facts)
            out = dialogue(assistant, "经验库怎样记录和利用设计结果？会自动训练模型权重吗？", task=recent, use_model=False)
            assert "不更新模型权重" in out["text"] and "987.654" not in out["text"]
    print("PASS: compound questions, readable plans, persistent replies, execution/failure audit consistency and bounded SRAM follow-up context")


if __name__ == "__main__":
    main()
