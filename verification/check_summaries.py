"""Check faithful user-visible summaries, including omitted/invented model facts."""
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
from result_summary import evidence, summarize

passed = ROOT / "verification/fixtures/passed"
failed = ROOT / "verification/fixtures/failed"
infeasible = ROOT / "verification/fixtures/infeasible"
backend = ROOT / "verification/fixtures/backend"
sweep = ROOT / "verification/fixtures/sweep"
for job in [passed, failed, infeasible, backend, sweep]:
    _, facts, essential = evidence(job)
    assert set(essential) <= set(facts)
assert "65,536" in evidence(passed)[1]["vectors"]
assert "未全部达标" in evidence(infeasible)[1]["status"]
assert "未达标" in evidence(infeasible)[1]["constraints"]
assert "任务未完成" in evidence(failed)[1]["status"]
assert "conclusion" not in evidence(failed)[1]
assert "4.64 ns" in evidence(backend)[1]["setup_wns_ns"]
assert "5.22511 µW" in evidence(sweep)[1]["candidates"]

with TemporaryDirectory(dir=ROOT / "verification") as temporary:
    fixture = Path(temporary)
    for name in ["result.json", "plan.json", "source_integrity.json"]:
        shutil.copyfile(passed / name, fixture / name)
    # A valid reply that omits key facts must still display those facts.
    reply = {"message": {"content": json.dumps({"highlights": ["status"], "next_step": ""})}}
    with patch("result_summary.urllib.request.build_opener") as opener, redirect_stdout(io.StringIO()):
        opener.return_value.open.return_value = io.StringIO(json.dumps(reply))
        text = summarize(fixture, "http://unused", "fixture")
    assert "65,536" in text and "错误率：0 %" in text and "穷举功能验证通过" in text
    # Invented measurements from a model must not reach the user's summary.
    reply["message"]["content"] = json.dumps({"highlights": ["fake_delay_999ns"], "next_step": ""})
    with patch("result_summary.urllib.request.build_opener") as opener, redirect_stdout(io.StringIO()):
        opener.return_value.open.return_value = io.StringIO(json.dumps(reply))
        text = summarize(fixture, "http://unused", "fixture")
    assert "999ns" not in text and "总结暂不可用" in text and "65,536" in text
print("PASS: success/failure/constraints/sweep/backend summaries; missing facts restored; invented facts rejected")
