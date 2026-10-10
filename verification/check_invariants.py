"""Validate source protection and rejection of executable model output."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from spec import validate
from assistant import SOURCE
from bindings import explicit_parameters, explicit_constraints

checks = []
for bad in [
    {"workflow":"sram", "command":"rm -rf /data2/huangjj/OpenACM"},
    {"workflow":"sram", "parameters":{"rows":3}},
    {"workflow":"sram", "parameters":{"vdd_v":float("nan")}},
    {"workflow":"sram", "parameters":{"pdk_path":"/tmp/evil.spice"}},
    {"workflow":"sram", "parameters":{"mc_runs":10, "variation":False}},
    {"workflow":"sram", "sweep":{"parameter":"vdd_v","values":[1]*9}},
    {"workflow":"multiplier", "parameters":{"bit_width":16,"exhaustive":True}},
    {"workflow":"dcim", "parameters":{"backend":"asic","rows":32,"bit_width":8,"memory_impl":"macro"}},
    {"workflow":"dcim", "parameters":{"backend":"asic","clock_period_ns":0}},
]:
    try:
        validate(bad)
    except ValueError:
        checks.append("rejected_invalid_plan")
    else:
        raise AssertionError(f"应拒绝的计划被接受：{bad}")
job = ROOT / "verification/isolation_workspace"
job.mkdir(exist_ok=True)
outside = ROOT / "verification/outside_workspace.txt"
outside.write_text("must remain unchanged\n")
probe = job / "probe.py"
probe.write_text('''import os
from pathlib import Path
import subprocess
import sys
source=sys.argv[2]
try:
    fd=os.open(source,os.O_WRONLY)
except PermissionError:
    pass
else:
    os.close(fd)
    raise AssertionError("source write access granted")
Path(__file__).with_name("allowed.txt").write_text("allowed\\n")
outside=sys.argv[1]
try:
    Path(outside).write_text("bad")
except PermissionError:
    pass
else:
    raise AssertionError("outside workspace write granted")
code="import os; os.open("+repr(source)+", os.O_WRONLY)"
r=subprocess.run([sys.executable,"-c",code],capture_output=True,text=True)
assert r.returncode != 0 and "PermissionError" in r.stderr
print("source writes denied, child inherits guard, task writes allowed")
''')
result = subprocess.run([sys.executable,str(ROOT / "app/guard.py"),str(job),sys.executable,str(probe),str(outside),str(SOURCE / "README.md" if (SOURCE / "README.md").exists() else outside)],capture_output=True,text=True)
assert result.returncode == 0, result.stderr
assert outside.read_text() == "must remain unchanged\n"
checks.append(result.stdout.strip())
assert explicit_parameters("8位精确乘法器，穷举验证，不修改原文件", "multiplier") == {"bit_width":8,"exhaustive":True,"kind":"exact"}
assert explicit_parameters("不要穷举，做16位精确乘法器", "multiplier")["exhaustive"] is False
facts = explicit_parameters("16行8列SRAM，PD0.18um，读写和三种SNM，27℃，TT", "sram")
assert facts["pd_width_nm"] == 180 and len(facts["operations"]) == 5 and facts["rows"] == 16
checks.append("explicit user values and exhaustive choice enforced")
assert explicit_constraints("读延迟不超过200ps，read SNM不低于250mV", "sram") == [
    {"metric":"read_delay_ns","op":"le","value":0.2},
    {"metric":"read_snm_v","op":"ge","value":0.25}]
assert explicit_constraints("MRED≤2%", "multiplier")[0]["value"] == 0.02
checks.append("explicit constraints and units bound to measured metrics")
facts = explicit_parameters("16×8精确DCiM全流程，时钟周期10ns，8位精确乘法器穷举", "dcim")
assert facts["backend"] == "asic" and facts["clock_period_ns"] == 10 and facts["exhaustive"]
assert explicit_constraints("setup slack不低于0ns，hold slack不低于0ns，布线DRC不超过0", "dcim") == [
    {"metric":"setup_wns_ns","op":"ge","value":0.0},
    {"metric":"hold_wns_ns","op":"ge","value":0.0},
    {"metric":"route_drc_count","op":"le","value":0.0}]
checks.append("ASIC mode, matching macro, clock and physical acceptance enforced")
facts = explicit_parameters("把sabetz的NMOS和PMOS都加宽10%，27℃", "compressor")
assert facts["name"] == "sabetz" and facts["nmos_scale"] == 1.1 and facts["pmos_scale"] == 1.1
facts = explicit_parameters("sabetz，NMOS缩窄10%，PMOS加宽20%", "compressor")
assert facts["nmos_scale"] == 0.9 and facts["pmos_scale"] == 1.2
checks.append("explicit compressor width changes cannot be omitted by the model")
(ROOT / "verification/invariants.json").write_text(json.dumps({"passed":True,"checks":checks},indent=2)+"\n")
print("PASS:", len(checks), "checks")
