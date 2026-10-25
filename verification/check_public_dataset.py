"""Check public data provenance, holdout isolation and legal model integration."""
from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import random
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import assistant
from public_dataset import (directory, query, augment_default_space, preferred_public_options,
                            decision_context, digest)
from exploration import default_config, choose_model, run_exploration, validate_config, fingerprint
from spec import validate


def main():
    with TemporaryDirectory(dir=ROOT / "verification") as temporary:
        root = Path(temporary)
        shutil.copytree(ROOT / "app", root / "app")
        shutil.copytree(ROOT / "skills", root / "skills")
        source = root / "OpenACM"
        source.mkdir()
        (source / "original.txt").write_text("original")
        before = assistant.manifest(source)
        database = directory(root) / "derived/samples.sqlite3"
        database.parent.mkdir(parents=True)
        vectors = [[1, 2, 3, 4, 5, 6, 7, 8, 1], [2] * 9, [7] * 9, [8] * 9]
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE samples(sample_id INTEGER,config TEXT,mred REAL,nmed REAL,delay REAL,area REAL,power REAL)")
            db.execute("CREATE TABLE metadata(key TEXT,value TEXT)")
            db.execute("INSERT INTO metadata VALUES('report',?)", (json.dumps({"labels": {"delay": {"distinct": 2}}}),))
            for identity, ids, mred, area in [(1, vectors[0], .01, 450), (2, vectors[1], .2, 430),
                                            (3, vectors[2], .015, 460), (48000, vectors[3], .0001, 1)]:
                db.execute("INSERT INTO samples VALUES(?,?,?,?,?,?,?)", (identity, json.dumps(ids, separators=(',', ':')), mred, .001, 1.31, area, .00015))
        plan = validate({"workflow": "multiplier", "parameters": {"kind": "approx", "bit_width": 8},
                         "constraints": [{"metric": "mred", "op": "le", "value": .02}]})
        context = query(root, plan, allow_assignments=True, objectives=[{"metric": "synthesized_area_um2", "direction": "min"}])
        assert [r["sample_id"] for r in context["observations"]] == [1, 3]
        assert all(not r["locally_measured"] and r["match"] == "external_reference_only" for r in context["observations"])
        assert not query(root, plan)["observations"]  # Fixed uniform ID1 has no exact dataset row.
        for bad in [validate({"workflow": "sram", "parameters": {}}),
                    validate({"workflow": "multiplier", "parameters": {"kind": "approx", "bit_width": 16}}),
                    validate({"workflow": "multiplier", "parameters": {"kind": "exact", "bit_width": 8}}),
                    validate({"workflow": "multiplier", "parameters": {"kind": "approx", "architecture": "tiled"}})]:
            assert not query(root, bad, allow_assignments=True)["observations"]
        with patch.dict('os.environ', {"OPENACM_PUBLIC_DATA": "0"}):
            assert not query(root, plan, allow_assignments=True)["observations"]
        with patch("exploration.augment_default_space", side_effect=lambda root, config: config):
            config = default_config("multiplier", "8位近似乘法器")
            fixed = default_config("multiplier", "8位近似乘法器，所有位置用sabetz")
        original = copy.deepcopy(config)
        augment_default_space(root, config)
        assert vectors[0] in config["space"]["compressor_ids"]
        assert config["plan"] == original["plan"]
        fixed_before = copy.deepcopy(fixed)
        augment_default_space(root, fixed)
        assert fixed == fixed_before
        options = preferred_public_options(context, config, [])
        assert len(options) == 2 and options[0]["parameters"]["compressor_ids"] == vectors[0]
        assert not preferred_public_options(context, fixed, [])
        assert all(o["parameters"] != options[0]["parameters"] for o in preferred_public_options(context, config, [{"parameters": options[0]["parameters"]}]))
        custom = copy.deepcopy(config)
        custom["space"]["compressor_ids"] = [[1] * 9]
        assert not preferred_public_options(context, custom, [])  # No implicit expansion of explicit domains.
        visible = decision_context(context, options)
        audit = root / "decision"
        audit.mkdir()
        with patch("exploration.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.StringIO(json.dumps({"message": {"content": '{"candidate_index":0,"reason":"已满足约束，本地面积最优"}'}}))
            _,reason = choose_model(options, [], config, "http://unused", "model", audit, 10, public=visible)
        assert "待验收" in reason and "已满足" not in reason and "最优" not in reason
        assert (audit / "reason_grounding.json").exists()
        request = json.loads((audit / "request.json").read_text())
        payload = json.loads(request["messages"][-1]["content"])
        assert payload["public_dataset_reference"]["observations"][0]["sample_id"] == 1
        assert payload["recent_measured"] == [] and payload["best_measured"] is None
        reply = {"message": {"content": json.dumps(plan)}}
        with patch.object(assistant, "ROOT", root), patch.object(assistant, "SOURCE", source), patch("assistant.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.StringIO(json.dumps(reply))
            result, info = assistant.plan_request("做8位近似乘法器，要求MRED≤0.02")
        sent = json.loads(opener.return_value.open.call_args.args[0].data)
        assert any("公开配置与标签参考" in m["content"] for m in sent["messages"])
        assert result["parameters"] == plan["parameters"] and result["constraints"] == plan["constraints"]
        assert (Path(info["audit_directory"]) / "public_dataset_retrieval.json").exists()
        def runner(task, plan, **kwargs):
            (task / "inputs").mkdir(parents=True)
            (task / "inputs/spec.json").write_text(json.dumps(plan))
            result = {"status": "completed", "metrics": {"mred": .01, "synthesized_area_um2": 320}}
            (task / "result.json").write_text(json.dumps(result))
            return result
        with redirect_stdout(io.StringIO()):
            job = run_exploration(root, source, config, runner, assistant.manifest, "unused", "unused", 2, 20, False)
        run = json.loads((job / "result.json").read_text())
        assert run["candidates"][0]["parameters"] == config["plan"]["parameters"]
        assert run["candidates"][1]["parameters"]["compressor_ids"] == vectors[0]
        assert run["candidates"][1]["metrics"]["synthesized_area_um2"] == 320
        assert assistant.manifest(source) == before
    print("PASS: training-only retrieval, reference provenance, fixed requests, explicit domains, baseline preservation, planner/DSE model payloads and actual-result separation")


if __name__ == "__main__":
    main()
