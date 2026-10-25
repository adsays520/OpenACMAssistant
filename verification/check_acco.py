"""Contracts, objective units, conservative aggregation and conversational handoff."""
import copy
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
from acco import frontier
from acco_spec import NeedsExperiment, from_request, validate_acco
from acco_models import aggregate_corners, architecture_grid, bank_metrics
from spec import validate
import assistant
from conversation import Session


def main():
    config = validate_acco(json.loads((ROOT / "examples/acco-sram-smoke.json").read_text()))
    patches = [{"capacity_bits": 63}, {"output_width": 0}, {"architecture_space": {"rows": [4], "cols": [4], "mux_ratio": [4]}},
               {"device_space": {"rows": [4]}}, {"device_space": {"length_nm": [60]}},
               {"robustness": {**config["robustness"], "mc_runs": 1}}, {"solvers": {"architecture": "invented"}}]
    for values in patches:
        try:
            validate_acco({**config, **values})
        except ValueError:
            pass
        else:
            raise AssertionError(f"接受了无效规格：{values}")
    with redirect_stdout(io.StringIO()):
        grid = architecture_grid(ROOT.parent / "OpenYield/OpenYield/size_optimization/experiment.py", config)
    assert grid and all(a["rows"]*a["cols"]*a["num_arrays"] == 64 for a in grid)
    with TemporaryDirectory(dir=ROOT / "verification") as tmp:
        task = Path(tmp)
        metrics = {"read_delay_ns": 1., "write_delay_ns": 2.}
        (task / "metrics.json").write_text(json.dumps(metrics))
        for op in ("read", "write"):
            folder = task / "artifacts" / op
            folder.mkdir(parents=True)
            (folder / "samples.json").write_text(json.dumps({"consumed_average_power_w": [10e-6, 12e-6],
                "source_signed_static_power_w": [-1e-6, -2e-6]}))
        area = Mock()
        area.estimate_scaled_array_area.return_value = 1e-10
        result = bank_metrics(task, config["plan"]["parameters"], {"rows": 4, "cols": 4, "num_arrays": 4, "mux_ratio": 1}, 4, area)
        # Only inactive-array leakage is replicated, not the complete dynamic read power.
        assert abs(result["bank_read_power_uw_estimate"] - 18.6) < 1e-9
        assert result["bank_area_um2_estimate"] == 100
        other = {**result, "bank_pmax_uw_estimate": 30., "bank_dmax_ns_estimate": 1., "min_snm_v": .2}
        baseline = {**result, "min_snm_v": .1}
        combined = aggregate_corners([baseline, other])
        assert combined["bank_pmax_uw_estimate"] == 30 and combined["bank_dmax_ns_estimate"] == result["bank_dmax_ns_estimate"]
        assert combined["min_snm_v"] == .1 and combined["fom_arch_estimate"] < result["fom_arch_estimate"]
    for request in ("两级探索SRAM", "两级探索SRAM，总容量4KB"):
        try:
            from_request(request, config["plan"])
        except NeedsExperiment:
            pass
        else:
            raise AssertionError("缺少容量或接口却启动研究实验")
    parsed = from_request("两级探索SRAM，总容量4KB，输出字宽8位", config["plan"])
    assert parsed["capacity_bits"] == 32768 and parsed["output_width"] == 8
    assert from_request("两级探索SRAM，总容量4Kb，输出字宽8位", config["plan"])["capacity_bits"] == 4096
    selected = from_request("两级探索SRAM，总容量4KiB，输出字宽8位，预算2个候选，架构用NSGA-II，器件用随机，限时10分钟", config["plan"])
    assert selected["solvers"] == {"architecture": "nsga2", "device": "random"}
    assert selected["capacity_bits"] == 32768 and selected["budgets"]["max_seconds"] == 600 and selected["budgets"]["architecture"] == 2
    assert from_request("总容量4KB，输出字宽8位，采用NSGA-II", config["plan"])["solvers"] == {"architecture": "nsga2", "device": "nsga2"}
    # Model predictions have a different metric namespace and cannot satisfy measured constraints.
    record = {"status": "completed", "candidate": "x", "metrics": {"predicted_pdp_fj": 1}}
    assert frontier([record], [{"metric": "pdp_fj", "direction": "min"}]) == ([], [])
    multiplier = json.loads((ROOT / "examples/acco-multiplier-smoke.json").read_text())
    assert validate_acco(multiplier)["plan"]["parameters"]["architecture"] == "native"
    try:
        validate_acco({**multiplier, "plan": {**multiplier["plan"], "parameters": {**multiplier["plan"]["parameters"], "bit_width": 32}}})
    except ValueError:
        pass
    else:
        raise AssertionError("GNN被外推到未训练尺寸")
    # The original small-population optimizer cannot hang on duplicate initial points.
    with TemporaryDirectory(dir=ROOT / "verification") as tmp:
        from acco import Runner
        job = Path(tmp)
        shutil.copytree(ROOT.parent / "OpenACM/DCIM_OPT/algorithms/NSGA", job / "references/NSGA")
        runner = Runner(assistant, job, config)
        with redirect_stdout(io.StringIO()):
            ranked, _ = runner.search("architecture", [{"v": i} for i in range(4)], lambda d, _: {"x": float(d["v"])},
                [{"metric": "x", "direction": "min"}], [], 2, "nsga2", "fixture")
        records = runner.state["stages"]["architecture"]["records"]
        assert len(records) == 2 and len({r["parameters"]["v"] for r in records}) == 2
    # Missing bank specs remain a normal dialogue; preview/start preserves full capacity.
    with TemporaryDirectory(dir=ROOT / "verification") as tmp:
        root = Path(tmp)
        with patch.object(assistant, "ROOT", root), patch("acco.launch") as launch:
            session = Session()
            outcome = session.turn(assistant, "两级探索SRAM", use_model=False)
            assert outcome["mode"] == "clarify" and session.pending
            outcome = session.turn(assistant, "总容量8KB，输出字宽16位", use_model=False, plan_only=True)
            assert outcome["mode"] == "plan" and outcome["plan"]["capacity_bits"] == 65536
            assert session.experiment_draft["output_width"] == 16
            launch.assert_not_called()
            job = root / "runs/20261025_000000_acco_sram_test"
            (job / "reports").mkdir(parents=True)
            (job / "reports/summary.md").write_text("两级结果：有限PVT样本验证。")
            (job / "result.json").write_text(json.dumps({"kind": "acco", "status": "completed", "config": session.experiment_draft,
                "stages": {}, "best_design": None}))
            launch.return_value = job
            outcome = session.turn(assistant, "开始运行", use_model=False)
            assert outcome["mode"] == "explore" and launch.call_args.args[1]["capacity_bits"] == 65536
            assert session.last_task == job and session.experiment_draft is None
            (job / "best_experiment.json").write_text(json.dumps(session.experiment))
            outcome = session.turn(assistant, "按刚才最佳配置复测", use_model=False)
            assert outcome["mode"] == "explore" and launch.call_count == 2
            assert launch.call_args.args[1]["output_width"] == 16
    print("PASS: ACCO capacity/interface, level separation, PVT/MC contracts, bank power/area units, conservative FOM, prediction isolation and model scope")


if __name__ == "__main__":
    main()
