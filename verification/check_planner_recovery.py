"""Regression tests for the captured repeated-array/truncated JSON failure."""
from contextlib import redirect_stdout
import importlib.util
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
from spec import validate, compressor_count, native_compressor_count, SCHEMA

REQUEST = "做8位近似乘法器，所有近似位置用sabetz，固定种子采样4096组，要求MRED≤0.02。"
captured = json.loads((ROOT / "verification/fixtures/truncated_reply.json").read_text())
valid = {"done_reason": "stop", "message": {"content": json.dumps({
    "intent": "run", "workflow": "multiplier", "parameters": {
        "bit_width": 8, "kind": "approx", "compressor_id": 8, "samples": 4096,
        "exhaustive": False, "seed": 42},
    "constraints": [{"metric": "mred", "op": "le", "value": 0.02}], "message": "采样验证。"})}}

with TemporaryDirectory(dir=ROOT / "verification") as temporary:
    fixture = Path(temporary)
    shutil.copytree(ROOT / "skills", fixture / "skills")
    for bad in [captured, {"done_reason": "stop", "message": {"content": '{"intent":"run",'}}]:
        with patch.object(assistant, "ROOT", fixture), patch("assistant.urllib.request.build_opener") as opener, redirect_stdout(io.StringIO()):
            opener.return_value.open.side_effect = [io.StringIO(json.dumps(bad)), io.StringIO(json.dumps(valid))]
            previous = validate({"workflow": "multiplier", "parameters": {"bit_width": 8, "exhaustive": True, "compressor_ids": [1] * 9}})
            plan, audit = assistant.plan_request(REQUEST, previous=previous)
        assert opener.return_value.open.call_count == 2
        assert plan["parameters"]["compressor_id"] == 8 and "compressor_ids" not in plan["parameters"]
        assert plan["parameters"]["samples"] == 4096 and not plan["parameters"]["exhaustive"]
        assert plan["constraints"] == [{"metric": "mred", "op": "le", "value": 0.02}]
        assert (Path(audit["audit_directory"]) / "validation_error_1.txt").exists()
        schema = json.loads(opener.return_value.open.call_args_list[0].args[0].data)["format"]
        assert "compressor_ids" not in schema["properties"]["parameters"]["properties"]
    with patch.object(assistant, "ROOT", fixture), patch("assistant.urllib.request.build_opener") as opener, redirect_stdout(io.StringIO()):
        opener.return_value.open.side_effect = [io.StringIO(json.dumps(captured)) for _ in range(2)]
        try:
            assistant.plan_request(REQUEST)
        except RuntimeError as exc:
            assert "尚未执行仿真" in str(exc) and "Expecting value" not in str(exc)
        else:
            raise AssertionError("两次无效回复不得进入仿真")
        assert opener.return_value.open.call_count == 2

for wrong in [[], [8] * 8, [8] * 10, [8] * 1000]:
    try:
        validate({"workflow": "multiplier", "parameters": {"kind": "approx", "bit_width": 8, "compressor_ids": wrong}})
    except ValueError:
        pass
    else:
        raise AssertionError("错误位置数被接受")
assert validate({"workflow": "multiplier", "parameters": {"kind": "approx", "bit_width": 8, "compressor_ids": [8] * 9}})
assert SCHEMA["properties"]["parameters"]["properties"]["operations"]["maxItems"] == 5

# The bounded cardinality must stay consistent with the unchanged generator.
path = assistant.SOURCE / "openacm/multiplier_compiler/nbit_approx.py"
if path.exists():
    module_spec = importlib.util.spec_from_file_location("readonly_count", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    assert all(native_compressor_count(n) == module._required_compressor_count(n) for n in range(4, 65))
print("PASS: captured truncation and malformed JSON recover once; repeated failure stops; sampling overrides exhaustive; bounded arrays match generator")
