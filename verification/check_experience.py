"""Exercise persistent memory across processes, evidence boundaries and DSE reuse."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import assistant
from experience import ExperienceStore, compact_context, observation, preferred_options, runtime_environment, source_digest
from exploration import default_config, choose_model, run_exploration
from spec import validate


def main():
    with TemporaryDirectory(dir=ROOT / "verification") as temporary:
        root = Path(temporary)
        shutil.copytree(ROOT / "app", root / "app")
        shutil.copytree(ROOT / "skills", root / "skills")
        source = root / "source"
        source.mkdir()
        (source / "original.txt").write_text("unchanged")
        snapshot = assistant.manifest(source)
        environment = runtime_environment(root, source_digest(snapshot))
        store = ExperienceStore(root)
        config = default_config("multiplier")
        baseline = config["plan"]
        good = validate({**baseline, "parameters": {**baseline["parameters"], "kind": "approx", "compressor_id": 7}})
        def task(name, plan, result, integrity=True, env=environment):
            path = root / "runs" / name
            (path / "inputs").mkdir(parents=True)
            (path / "inputs/spec.json").write_text(json.dumps(plan))
            (path / "inputs/execution_environment.json").write_text(json.dumps(env))
            (path / "result.json").write_text(json.dumps(result))
            (path / "source_integrity.json").write_text(json.dumps({"unchanged": integrity}))
            return path
        metrics = {"synthesized_area_um2": 291.8, "mred": 0.014}
        good_task = task("good", good, {"status": "completed", "metrics": metrics})
        task("failed", baseline, {"status": "failed", "error": "任务超过180秒", "metrics": {}})
        task("tampered_source", good, {"status": "completed", "metrics": {"synthesized_area_um2": 1, "mred": 0}}, False)
        other = validate({**good, "parameters": {**good["parameters"], "bit_width": 16}})
        task("other_size", other, {"status": "completed", "metrics": metrics})
        task("wrong_seed", validate({**good, "parameters": {**good["parameters"], "seed": 99}}), {"status": "completed", "metrics": metrics})
        task("unknown_tool", good, {"status": "completed", "metrics": metrics}, env={**environment, "tools_id": "different"})
        task("rounded_slack", good, {"status": "completed", "metrics": {**metrics, "hold_wns_ns": -0.0}})
        task("nonfinite", good, {"status": "completed", "metrics": {**metrics, "mred": float('nan')}})
        first = store.learn()
        assert first["added"] == 8 and not first["errors"], first
        again = store.learn()
        assert again["added"] == 0 and again["duplicates"] == 8
        context = store.query(baseline, environment, limit=20, objectives=config["objectives"])
        records = {Path(r["task_id"]).name: r for r in context["observations"]}
        assert "tampered_source" not in records and "other_size" not in records
        assert records["good"]["match"] == "same_evaluation_scope"
        for name in ["wrong_seed", "unknown_tool", "rounded_slack", "nonfinite"]:
            assert records[name]["match"] == "reference_only", name
        assert records["failed"]["failure_categories"] == ["timeout"]
        seeds = preferred_options(context, config, [])
        assert len(seeds) == 1 and seeds[0]["parameters"]["compressor_id"] == 7
        assert not preferred_options(context, config, [{"parameters": good["parameters"]}])
        fixed = default_config("multiplier", "8位近似乘法器，全部用sabetz")
        assert not preferred_options(context, fixed, [])
        payload = compact_context(context, max_chars=5500)
        assert len(json.dumps(payload, ensure_ascii=False, separators=(',', ':'), sort_keys=True)) <= 5500
        # Complete persistence is independent of model service and process lifetime.
        exported = store.export()
        script = "import sys;sys.path.insert(0,sys.argv[1]);from experience import ExperienceStore;print(ExperienceStore(sys.argv[2]).stats()['active_observations'])"
        process = subprocess.run([sys.executable, "-c", script, str(root / "app"), str(root)], capture_output=True, text=True)
        assert process.returncode == 0 and process.stdout.strip() == "8", process.stderr
        foreign = root / "foreign"
        imported = ExperienceStore(foreign)
        assert imported.import_file(exported) == 8
        assert imported.import_file(exported) == 0
        imported_context = imported.query(baseline, environment, limit=20)
        assert imported_context["observations"] and all(r["match"] == "reference_only" for r in imported_context["observations"])
        assert not preferred_options(imported_context, config, [])
        # Revisions retain the old observation; a repeated old import cannot reactivate it.
        old = observation(root, good_task)
        (good_task / "result.json").write_text(json.dumps({"status": "infeasible", "metrics": {**metrics, "mred": 0.3}}))
        assert store.learn(good_task)["added"] == 1
        assert not store.add(old)
        assert store.stats()["active_observations"] == 8 and store.stats()["all_revisions"] == 9
        revised = store.query(good, environment, limit=20)
        assert next(r for r in revised["observations"] if r["task_id"] == "runs/good")["status"] == "infeasible"
        # Concurrent completion of the same task remains idempotent.
        record = observation(root, good_task)
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert not any(pool.map(store.add, [record] * 8))
        sram_config = default_config("sram")
        alternative = validate({"workflow": "sram", "parameters": {"rows": 8, "cols": 16,
                               "operations": sram_config["plan"]["parameters"]["operations"]}})
        sram_task = task("sram_shape", alternative, {"status": "completed", "metrics": {"read_power_uw": 10, "read_delay_ns": 1, "write_delay_ns": 1, "read_snm_v": 0.3}})
        store.learn(sram_task)
        assert not store.query(sram_config["plan"], environment)["observations"]
        shapes = store.query(sram_config["plan"], environment, allow_shapes=True)
        assert shapes["observations"][0]["match"] == "same_evaluation_scope"
        assert preferred_options(shapes, sram_config, [])[0]["parameters"]["rows"] == 8
        # Planner receives cited observations before the latest user request.
        reply = {"message": {"content": json.dumps({**good, "constraints": []})}}
        with patch.object(assistant, "ROOT", root), patch.object(assistant, "SOURCE", source), patch("assistant.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.StringIO(json.dumps(reply))
            plan, audit = assistant.plan_request("做8位近似乘法器")
        sent = json.loads(opener.return_value.open.call_args.args[0].data)
        assert sent["messages"][-1]["content"] == "做8位近似乘法器"
        assert any("本地历史经验" in m["content"] for m in sent["messages"])
        assert (Path(audit["audit_directory"]) / "experience_retrieval.json").is_file()
        assert plan["parameters"]["bit_width"] == 8
        assert plan["constraints"] == []  # A retrieved design must not add unrequested thresholds.
        # Exploration passes persistent observations into the real model request body.
        decision = root / "decision"
        decision.mkdir()
        with patch("exploration.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.StringIO(json.dumps({"message": {"content": '{"candidate_index":0,"reason":"选已验证候选重新实测"}'}}))
            choose_model([good], [], config, "http://unused", "another-model", decision, 10, context)
        request = json.loads((decision / "request.json").read_text())
        assert json.loads(request["messages"][-1]["content"])["persistent_experience"]["observations"]
        def runner(path, plan, **kwargs):
            (path / "inputs").mkdir(parents=True)
            (path / "inputs/spec.json").write_text(json.dumps(plan))
            (path / "inputs/execution_environment.json").write_text(json.dumps(environment))
            result = {"status": "completed", "metrics": metrics}
            (path / "result.json").write_text(json.dumps(result))
            return result
        with redirect_stdout(io.StringIO()):
            job = run_exploration(root, source, config, runner, assistant.manifest, "unused", "unused", 2, 20, False)
        assert (job / "reports/experience_recording.json").is_file()
        assert store.stats()["active_observations"] == 11
        assert assistant.manifest(source) == snapshot
        assert not store.learn(job)["added"]
    print("PASS: persistence, idempotency, revisions, concurrent writes, scoped retrieval, untrusted import, legal DSE seeds, planner and exploration integration")


if __name__ == "__main__":
    main()
