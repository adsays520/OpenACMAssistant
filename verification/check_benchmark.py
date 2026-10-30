"""Independent contract, language-continuity, finite-universe and stop-state checks."""
import copy
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
from acco import BudgetStop, Runner, key
from acco_spec import NeedsExperiment
from benchmark import schedule
from benchmark_spec import candidate_pool, from_request, validate_benchmark
from conversation import Session
from worker import arithmetic_vector_text
from terminal_ui import review_plan

START = "开展8位近似乘法器的架构搜索对照实验。"
DETAILS = ("算术测试固定为种子42、采样4096组；所有方法使用相同的综合库、5ns输入活动周期和10fF输出负载。"
           "将搜索随机种子与测试输入种子分开，每种方法重复5次，每次最多评价32个不同架构。")


def main():
    c = from_request(START + DETAILS)
    assert c["candidate_budget"] == 32 and c["search_seeds"] == [1, 2, 3, 4, 5]
    assert len(schedule(c)) == 15 and len(set(schedule(c))) == 15
    e, p = c["experiment"], c["experiment"]["plan"]["parameters"]
    assert p["seed"] == 42 and p["samples"] == 4096 and p["compressor_id"] == 7
    assert e["physical_evaluation"] == {"clock_period_ns": 5., "output_load_ff": 10.}
    assert e["plan"]["constraints"] == []  # Do not invent a missing MRED ceiling.
    review = review_plan(c)
    assert "4096" in review and "32" in review and "5次" in review
    assert "candidate_budget" not in review and "search_seeds" not in review
    scoped = from_request(START + "搜索种子为11、22，重复2次，每次最多评价32个不同架构，测试种子42，采样4096组，MRED≤0.02。")
    assert scoped["search_seeds"] == [11, 22] and scoped["experiment"]["plan"]["parameters"]["seed"] == 42
    assert from_request(START + DETAILS + "只比较随机和NSGA-II。")["methods"] == ["random", "nsga2"]
    pool = candidate_pool(c)
    assert len(pool) == 256 and len({key(d) for d in pool}) == 256 and pool[0]["compressor_ids"] == [7] * 9
    assert pool == candidate_pool({**c, "search_seeds": [91, 92]})
    vectors = arithmetic_vector_text(p)
    assert len(vectors.splitlines()) == 4096
    changed_search = validate_benchmark({**c, "search_seeds": [111, 222]})
    assert arithmetic_vector_text(changed_search["experiment"]["plan"]["parameters"]) == vectors
    assert arithmetic_vector_text({**p, "seed": 43}) != vectors
    assert vectors.splitlines()[:3] == ["0", "1", "2"]
    for change in [{"search_seeds": [1, 1]}, {"methods": ["invented", "random"]}, {"candidate_budget": 257},
                   {"search_seeds": [-1]}, {"validation_top_k": 33}]:
        try:
            validate_benchmark({**c, **change})
        except ValueError:
            pass
        else:
            raise AssertionError(f"非法对照规格被接受：{change}")
    with TemporaryDirectory(dir=ROOT / "verification") as tmp:
        root = Path(tmp)
        with patch.object(assistant, "ROOT", root), patch("benchmark.launch") as launch:
            s = Session()
            from unittest.mock import Mock
            ui = Mock()
            out = s.turn(assistant, START, use_model=False, ui=ui)
            assert out["mode"] == "clarify" and "已识别" not in out["text"]
            ui.status.assert_not_called()
            assert s.turn(assistant, "MRED是什么", use_model=False)["mode"] == "consult"
            outcome = s.turn(assistant, DETAILS, use_model=False, plan_only=True)
            assert outcome["mode"] == "plan" and outcome["plan"]["candidate_budget"] == 32
            assert s.comparison_draft["search_seeds"] == [1, 2, 3, 4, 5]
            assert s.comparison_draft["experiment"]["plan"]["parameters"]["seed"] == 42
            assert s.turn(assistant, "这个计划是否已达标", use_model=False)["mode"] == "consult"
            launch.assert_not_called()
            job = root / "runs/test_comparison"
            (job / "reports").mkdir(parents=True)
            (job / "reports/summary.md").write_text("对照已按实际结果汇总。")
            launch.return_value = job
            assert s.turn(assistant, "开始运行", use_model=False)["mode"] == "compare"
            assert launch.call_args.args[1]["candidate_budget"] == 32
            assert launch.call_args.args[1]["experiment"]["plan"]["parameters"]["seed"] == 42
        # Both random and the real original NSGA-II must evaluate only the finite pool.
        selections = []
        for method in ("random", "nsga2"):
            job = root / method
            shutil.copytree(ROOT.parent / "OpenACM/DCIM_OPT/algorithms/NSGA", job / "references/NSGA")
            runner = Runner(assistant, job, {**e, "search_seed": 11})
            with redirect_stdout(io.StringIO()):
                runner.search("architecture", pool, lambda d, t: {"predicted_pdp_fj": float(sum(d["compressor_ids"]))},
                    [{"metric": "predicted_pdp_fj", "direction": "min"}], [], 32, method, "prediction")
            records = runner.state["stages"]["architecture"]["records"]
            assert len(records) == 32 and len({key(r["parameters"]) for r in records}) == 32
            assert all(r["parameters"] in pool for r in records) and records[0]["parameters"] == pool[0]
            selections.append([r["parameters"] for r in records])
        assert selections[0] != selections[1]
        # Expired phases propagate to the caller; partial work is not a feasibility result.
        timed = root / "expired"
        timed.mkdir()
        runner = Runner(assistant, timed, e)
        runner.deadline = 0
        try:
            runner.search("architecture", pool, lambda d, t: {}, e["architecture_objectives"], [], 32, "random", "prediction")
        except BudgetStop:
            assert runner.state["stages"]["architecture"]["status"] == "incomplete"
        else:
            raise AssertionError("时间耗尽被吞掉")
    print("PASS: exact user continuation, all 15 paired trials, separate seeds, immutable pool/vectors, original NSGA finite-domain budgets, preview/start and timeout propagation")


if __name__ == "__main__":
    main()
