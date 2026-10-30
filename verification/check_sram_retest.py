"""Reported no-SRAM-keyword request, stale multiplier state and partial PVT evidence."""
from contextlib import redirect_stdout
import copy
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
from acco import BudgetStop, Runner
from acco_spec import validate_acco
from conversation import Session, workflow
from sram_retest import from_request, render_retest

TEXT = (ROOT / "verification/fixtures/sram_device_retest.txt").read_text().strip()


def main():
    assert workflow(TEXT) == "sram"
    assert workflow("做16×8 DCiM，采用SRAM子阵列") == "dcim"
    c = from_request(assistant, TEXT)
    assert c["reference_design"]["candidate"] == "architecture/0013"
    assert c["capacity_bits"] == 32768 and c["output_width"] == 8
    assert c["architecture_space"] == {"rows": [32], "cols": [32], "mux_ratio": [1]}
    assert c["device_space"] == {"pd_width_nm": [90., 135., 180.]}
    assert c["budgets"]["device_per_parent"] == 3 and c["bank_constraints"] == []
    assert c["robustness"] == {"cases": [{"corner": "TT", "temperature_c": 27., "vdd_v": 1.},
        {"corner": "SS", "temperature_c": 85., "vdd_v": .9}, {"corner": "FF", "temperature_c": -40., "vdd_v": 1.1}], "mc_runs": 2, "vth_std": .05}
    p = c["plan"]["parameters"]
    assert p["pu_width_nm"] == 90 and p["pd_width_nm"] == 90 and p["pg_width_nm"] == 135 and p["length_nm"] == 50
    assert p["seed"] == 42 and p["mc_runs"] == 2 and p["variation"] is True and "bit_width" not in p
    with TemporaryDirectory(dir=ROOT / "verification") as tmp:
        root = Path(tmp)
        source = ROOT / "runs" / c["reference_design"]["task"] / "result.json"
        target = root / "runs" / source.parent.name / source.name
        target.parent.mkdir(parents=True)
        shutil.copy2(source, target)
        with patch.object(assistant, "ROOT", root), patch.object(assistant, "plan_request") as planner, patch("benchmark.launch") as compare:
            s = Session()
            s.comparison = json.loads((ROOT / "examples/compare-multiplier-8bit.json").read_text())
            out = s.turn(assistant, TEXT, plan_only=True, use_model=False)
            assert out["mode"] == "plan" and out["plan"]["experiment_mode"] == "sram_device_retest"
            assert s.comparison is None and s.experiment_draft["workflow"] == "sram"
            planner.assert_not_called()
            compare.assert_not_called()
            with patch("acco.launch") as launch:
                job = root / "runs/new_retest"
                (job / "reports").mkdir(parents=True)
                (job / "reports/summary.md").write_text("fixture")
                (job / "result.json").write_text(json.dumps({"kind": "acco", "status": "completed", "config": out["plan"], "stages": {}, "best_design": None}))
                launch.return_value = job
                s.turn(assistant, "开始运行", use_model=False)
                assert launch.call_args.args[1]["device_space"] == c["device_space"]
        def setup(name):
            job = root / name
            (job / "references/OpenYield").mkdir(parents=True)
            shutil.copy2(ROOT.parent / "OpenYield/OpenYield/utils.py", job / "references/OpenYield/utils.py")
            return job, Runner(assistant, job, c)
        # Inject a real failure at one PVT; later corners and other PDs must remain evaluable.
        job, runner = setup("partial")
        calls = []
        def evaluate(folder, plan, timeout_override=None):
            par = plan["parameters"]
            calls.append((par["pd_width_nm"], par["corner"]))
            assert par["rows"] == par["cols"] == 32 and par["mc_runs"] == 2 and par["seed"] == 42
            folder.mkdir(parents=True)
            if par["pd_width_nm"] == 90 and par["corner"] == "SS":
                return {"status": "failed", "error": "injected corner failure"}
            metrics = {"read_delay_ns": 1., "write_delay_ns": .5, "hold_snm_v": .3, "read_snm_v": .2, "write_snm_v": .4}
            (folder / "metrics.json").write_text(json.dumps(metrics))
            for op in ("read", "write"):
                path = folder / "artifacts" / op
                path.mkdir(parents=True)
                (path / "samples.json").write_text(json.dumps({"consumed_average_power_w": [1e-5, 1.1e-5], "source_signed_static_power_w": [-1e-6, -1e-6]}))
            return {"status": "completed", "metrics": metrics}
        with patch.object(assistant, "run_one", side_effect=evaluate), redirect_stdout(io.StringIO()):
            result = runner.run()
        assert len(calls) == 9 and set(calls) == {(pd, corner) for pd in (90, 135, 180) for corner in ("TT", "SS", "FF")}
        assert result["status"] == "failed" and result["counters"]["device_candidates"] == 3
        first = json.loads((job / "stages/device_fixed_architecture/0001/coverage.json").read_text())
        assert [case["status"] for case in first["cases"]] == ["completed", "failed", "completed"]
        (job / "reports").mkdir()
        report = render_retest(job, result)
        assert (job / "reports/baseline_comparison.json").is_file() and "器件复验" in report
        # Stop in the first candidate after one corner; leave explicit incomplete/not_run states.
        job, runner = setup("timeout")
        def expire(folder, plan, timeout_override=None):
            r = evaluate(folder, plan, timeout_override)
            runner.deadline = 0
            return r
        with patch.object(assistant, "run_one", side_effect=expire), redirect_stdout(io.StringIO()):
            result = runner.run()
        stage = result["stages"]["device_fixed_architecture"]
        assert result["status"] == "incomplete" and result["stop_reason"] == "time_budget"
        assert stage["records"][0]["status"] == "incomplete" and len(stage["pending_candidates"]) == 2
        assert all(r["status"] == "not_run" for r in stage["pending_candidates"])
        coverage = json.loads((job / stage["records"][0]["task"] / "coverage.json").read_text())
        assert [v["status"] for v in coverage["cases"]] == ["completed", "not_run", "not_run"]
        # Tool failures, with no numeric constraint verdict, cannot be called infeasible.
        job, runner = setup("all_failed")
        with patch.object(assistant, "run_one", return_value={"status": "failed", "error": "tool timeout"}), redirect_stdout(io.StringIO()):
            result = runner.run()
        assert result["status"] == "failed" and result["stages"]["device_fixed_architecture"]["status"] == "failed"
    print("PASS: exact SRAM request, stale multiplier isolation, locked architecture/PD/PVT/MC, matched baseline, failed-corner continuation, partial timeout and explicit unexecuted candidates")


if __name__ == "__main__":
    main()
