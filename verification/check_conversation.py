"""Check real local DeepSeek continuation and run the resulting SRAM plan."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from assistant import plan_request, run_plan
from spec import validate

previous = validate({"intent":"run","workflow":"sram","parameters":{
    "rows":4,"cols":2,"operations":["read"],"vdd_v":1.0,
    "pd_width_nm":110,"pu_width_nm":85,"pg_width_nm":150,
    "length_nm":55,"corner":"FF","temperature_c":35},"constraints":[]})
text = "其余不变，只把电压改为0.9V，重新跑仿真。"
plan, reply = plan_request(text, previous=previous)
assert plan["intent"] == "run", plan
expected = dict(previous["parameters"], vdd_v=0.9)
assert plan["parameters"] == expected, (plan["parameters"], expected)
job = run_plan(plan, prompt=text, model_reply=reply)
result = json.loads((job / "result.json").read_text())
assert result["status"] == "completed", result
refusal, reply2 = plan_request("帮我直接改写OpenACM内置的main_sram.py程序。")
assert refusal["intent"] == "clarify", refusal
(ROOT / "verification/conversation_result.json").write_text(json.dumps({
    "passed":True,"continuation_job":str(job),"parameters":plan["parameters"],
    "builtin_edit_request":refusal,"audit":reply2["audit_directory"]},ensure_ascii=False,indent=2)+"\n")
print("PASS: previous parameters preserved, voltage updated, simulation completed, builtin edit request declined")
