"""Real dialogue on the exact user request with a stale multiplier comparison."""
from datetime import datetime
import json
from pathlib import Path
import sys
from unittest.mock import patch
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import assistant
from conversation import Session
from terminal_ui import review_plan


def main():
    folder = ROOT / "runs" / (datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
                              + "_sram_retest_validation_" + uuid.uuid4().hex[:6])
    folder.mkdir()
    before = assistant.manifest(assistant.SOURCE)
    text = (ROOT / "verification/fixtures/sram_device_retest.txt").read_text().strip()
    session = Session()
    session.comparison = json.loads((ROOT / "examples/compare-multiplier-8bit.json").read_text())
    try:
        with patch.object(assistant, "plan_request", side_effect=AssertionError("unexpected single-point planner")), \
                patch("acco.launch", side_effect=AssertionError("preview executed")), \
                patch("benchmark.launch", side_effect=AssertionError("stale multiplier executed")):
            out = session.turn(assistant, text, plan_only=True)
        assert out["mode"] == "plan"
        c = out["plan"]
        assert c["workflow"] == "sram" and c["experiment_mode"] == "sram_device_retest"
        assert c["capacity_bits"] == 32768 and c["output_width"] == 8
        assert c["device_space"] == {"pd_width_nm": [90., 135., 180.]}
        assert c["robustness"]["mc_runs"] == 2 and len(c["robustness"]["cases"]) == 3 and c["bank_constraints"] == []
        assert c["reference_design"]["candidate"] == "architecture/0013"
        audit = Path(out["audit_directory"])
        metadata = json.loads((audit / "metadata.json").read_text())
        assert metadata["model_used"], f"未调用真实模型，见{audit}"
        assistant.write_json(folder / "plan.json", c)
        assistant.write_json(folder / "result.json", {"kind": "sram_retest_validation", "status": "completed", "model_used": True,
            "dialogue_audit": str(audit), "eda_started": False})
        (folder / "report.md").write_text("# SRAM器件复验原话：真实模型验证\n\n初始对话保留了上一轮乘法器对照；本次没有进入乘法器/单点规划器，没有执行EDA。\n\n用户："
            + text + "\n\n助手计划：\n\n" + review_plan(c) + "\n\n历史引用：" + json.dumps(c["reference_design"], ensure_ascii=False) + "\n")
        print(review_plan(c), flush=True)
        print("PASS；报告：" + str(folder / "report.md"), flush=True)
    except Exception as exc:
        assistant.write_json(folder / "result.json", {"kind": "sram_retest_validation", "status": "failed", "error": str(exc)})
        print("失败记录：" + str(folder), flush=True)
        raise
    finally:
        unchanged = before == assistant.manifest(assistant.SOURCE)
        assistant.write_json(folder / "source_integrity.json", {"unchanged": unchanged, "checked_files": len(before)})
        assert unchanged


if __name__ == "__main__":
    main()
