"""Rehearse the video's short dialogue with a real model and real RTL run.

Uses an isolated conversation, leaves the user's current chat unchanged, and
records failures. This script deliberately executes only a 4-bit multiplier.
"""
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import assistant
from conversation import Session


def main():
    folder = ROOT / "runs" / (datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
                              + "_video_short_flow_" + uuid.uuid4().hex[:6])
    folder.mkdir()
    session = Session.restore(folder)
    before = assistant.manifest(assistant.SOURCE)
    turns = []
    job = None
    print("验证目录：" + str(folder), flush=True)
    cases = [
        ("做4位精确乘法器，穷举所有输入。", True, "plan"),
        ("开始运行", False, "run"),
        ("刚才的仿真结果如何？验证了多少组输入，错误数是多少？", False, "consult"),
    ]
    try:
        for index, (text, preview, mode) in enumerate(cases, 1):
            row = {"request": text, "preview": preview, "expected_mode": mode, "errors": []}
            print(f"开始 {index}：{text}", flush=True)
            stream = io.StringIO()
            try:
                with redirect_stdout(stream):
                    out = session.turn(assistant, text, plan_only=preview)
                row["outcome"] = json.loads(json.dumps(out, default=str, ensure_ascii=False))
                audit = Path(out["audit_directory"])
                metadata = json.loads((audit / "metadata.json").read_text())
                assert metadata["model_used"], "无效模型回复或服务回退不能计为通过"
                assert out["mode"] == mode, out["mode"]
                assert (audit / "answer.md").read_text().strip() == out["reply"].strip()
                assert all(t not in out["reply"] for t in ["<think>", "已识别为", "正在理解", "正在整理需求"])
                if mode == "plan":
                    p = out["plan"]
                    assert p["workflow"] == "multiplier" and p["parameters"]["bit_width"] == 4
                    assert p["parameters"]["kind"] == "exact" and p["parameters"]["exhaustive"]
                    assert not metadata["executed"]
                elif mode == "run":
                    job = Path(out["job"])
                    result = json.loads((job / "result.json").read_text())
                    assert result["status"] == "completed", result
                    assert result["metrics"]["sample_count"] == 256
                    assert result["metrics"]["error_count"] == 0
                    assert json.loads((job / "source_integrity.json").read_text())["unchanged"]
                    assert metadata["executed"]
                else:
                    assert "256" in out["reply"] and "0" in out["reply"]
                    assert not metadata["executed"] and Path(out["task"]) == job
            except Exception as exc:
                row["errors"].append(type(exc).__name__ + ": " + str(exc))
            row["stdout"] = stream.getvalue()
            row["passed"] = not row["errors"]
            turns.append(row)
            assistant.write_json(folder / "turns.json", turns)
            print(("PASS " if row["passed"] else "FAIL ") + str(index) + " " + "; ".join(row["errors"]), flush=True)
            if not row["passed"]:
                break
    finally:
        unchanged = before == assistant.manifest(assistant.SOURCE)
        assistant.write_json(folder / "source_integrity.json", {"unchanged": unchanged, "checked_files": len(before)})
        passed = sum(t["passed"] for t in turns)
        good = passed == len(cases) and unchanged
        assistant.write_json(folder / "result.json", {"kind": "video_short_flow_acceptance",
            "status": "completed" if good else "failed", "passed": passed, "total": len(cases),
            "model": assistant.MODEL, "task": str(job) if job else None,
            "scope": "真实模型、计划预览、开始运行、4位精确乘法器256组RTL实测及结果追问"})
        lines = ["# 视频短流程实测", "", f"{passed}/{len(cases)}轮通过；原始程序未变：{unchanged}。", ""]
        for t in turns:
            lines += ["## " + t["request"], "", t.get("outcome", {}).get("reply", "未取得回复"), ""]
            if t["errors"]:
                lines += ["失败项：" + "; ".join(t["errors"]), ""]
        (folder / "report.md").write_text("\n".join(lines))
        print("报告：" + str(folder / "report.md"), flush=True)
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
