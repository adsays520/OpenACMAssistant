"""Regression for method lists overwritten by the phrase 只比较架构选点."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import assistant
from acco_spec import NeedsExperiment
from benchmark_spec import from_request, requested_methods, METHODS
from benchmark import schedule
from conversation import Session

FULL = (ROOT / "verification/fixtures/benchmark_full_request.txt").read_text().strip()
SHORT = (ROOT / "verification/fixtures/benchmark_reported_request.txt").read_text().strip()


def conditions(c):
    assert c["methods"] == list(METHODS) and len(schedule(c)) == 15
    assert c["candidate_budget"] == 32 and c["search_seeds"] == [1, 2, 3, 4, 5]
    p = c["experiment"]["plan"]
    assert p["parameters"]["bit_width"] == 8 and p["parameters"]["compressor_id"] == 7
    assert p["parameters"]["samples"] == 4096 and p["parameters"]["seed"] == 42
    assert p["constraints"] == [{"metric": "mred", "op": "le", "value": .02}]
    assert p["objective"] == {"metric": "pdp_fj", "direction": "min"}
    assert c["experiment"]["physical_evaluation"] == {"clock_period_ns": 5., "output_load_ff": 10.}


def main():
    variants = [FULL, SHORT, FULL.replace("随机搜索", "随机搜 索"), FULL.replace("NSGA-II", "NSGA Ⅱ"),
                "原始待澄清需求：" + FULL + "。用户补充：" + FULL + "。用户补充：" + SHORT]
    for text in variants:
        conditions(from_request(text))
    for text, expected in [
        ("只比较架构选点", None), ("仅比较架构，不优化器件", None),
        ("所有方法使用相同综合库和搜索随机种子", None),
        ("只用随机种子42，LLM次数另行记录", None),
        ("分别使用随机搜索、NSGA-II和LLM选择候选", list(METHODS)),
        ("使用random, NSGA2 and DeepSeek选择候选", list(METHODS)),
        ("方法组为随机、NSGA-II和LLM，后面只比较架构选点", list(METHODS)),
        ("方法组为随机、NSGA-II和LLM，改为只比较NSGA-II和LLM", ["nsga2", "llm"]),
        ("只比较NSGA-II和LLM，随机种子42", ["nsga2", "llm"]),
        ("只比较随机和NSGA-II", ["random", "nsga2"]),
        ("方法为LLM", ["llm"]),
    ]:
        assert requested_methods(text) == expected, (text, requested_methods(text))
    for text in ("方法组为未知A、未知B", "只比较MOBO和SMAC", "只比较随机和MOBO"):
        try:
            requested_methods(text)
        except NeedsExperiment:
            pass
        else:
            raise AssertionError("未支持方法被静默忽略：" + text)
    try:
        from_request(FULL + "方法组为LLM。")
    except NeedsExperiment as exc:
        assert "至少选择两个" in str(exc)
    else:
        raise AssertionError("显式单方法不能偷偷补成三方法")
    # Reproduce a failed, persisted chat; recover with the user's shorter retry.
    with TemporaryDirectory(dir=ROOT / "verification") as tmp, patch.object(assistant, "ROOT", Path(tmp)), \
            patch("benchmark.launch") as launch:
        session = Session.restore(tmp)
        session.pending = {"kind": "search_comparison", "request": FULL + "。用户补充：" + FULL,
                           "message": "至少选择两个不同方法：random/nsga2/llm", "plan_only": True}
        session.save()
        session = Session.restore(tmp)
        out = session.turn(assistant, SHORT, plan_only=True, use_model=False)
        assert out["mode"] == "plan" and session.pending is None
        conditions(out["plan"])
        launch.assert_not_called()
        assert "每种方法运行5次" in out["reply"]
        # A model's plan choice must not downgrade an explicit execution request.
        job = Path(tmp) / "runs/fixture_comparison"
        (job / "reports").mkdir(parents=True)
        (job / "reports/summary.md").write_text("测试执行器记录：仅验证调用，无EDA。")
        launch.return_value = job
        retry = Session.restore(tmp)
        retry.pending = {"kind": "search_comparison", "request": FULL,
                         "message": "至少选择两个不同方法：random/nsga2/llm", "plan_only": True}
        with patch("conversation.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.StringIO(json.dumps({"message": {"content": json.dumps({
                "operation": "plan", "message": "", "fact_ids": []})}}))
            out = retry.turn(assistant, FULL)
        assert out["mode"] == "compare" and launch.call_count == 1
        conditions(launch.call_args.args[1])
        meta = json.loads((Path(out["audit_directory"]) / "metadata.json").read_text())
        assert meta["model_operation"] == "plan" and meta["operation"] == "explore"
    print("PASS: full request, scope clauses, spaced aliases, later corrections, unsupported/single methods, persisted failed-chat recovery and explicit execution")


if __name__ == "__main__":
    main()
