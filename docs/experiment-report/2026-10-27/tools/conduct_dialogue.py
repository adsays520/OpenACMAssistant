"""Conduct the documented conversation with real DeepSeek and EDA executors.

E1 and E6 execute fresh tasks. E2/E5 preview plans and refer to existing EDA;
E3/E4 record the current unsupported/consultation behavior. No mocks are used.
"""
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import time
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import assistant
from conversation import Session

E2_TASK = "20261027_204422_search_comparison_d356cf"
SRAM_TASK = "20261026_144653_acco_sram_e635c9"


def main():
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    folder = HERE / "evidence" / (stamp + "_dialogue_" + uuid.uuid4().hex[:6])
    folder.mkdir(parents=True)
    assistant.write_json(HERE / "evidence/latest_collection.json", {"directory": str(folder)})
    text = (ROOT / "docs/experiment-plan/2026-10-27/实验1至6_可复制输入.md").read_text()
    groups = {m[1]: re.findall(r"```text\n(.*?)\n```", m[2], re.S)
              for m in re.finditer(r"^## (E[1-6])[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)}
    before = assistant.manifest(assistant.SOURCE)
    assistant.write_json(folder / "source_before.json", before)
    records = []
    print("真实问答记录：" + str(folder), flush=True)
    for group in ("E1", "E2", "E3", "E4", "E5", "E6"):
        session = Session.restore(folder / "sessions" / group)
        requests = [(s, group in ("E2", "E5"), "document") for s in groups[group]]
        if group == "E2":
            requests += [(f"任务{E2_TASK}的结果如何？", False, "historical_result_question")]
        if group == "E5":
            requests += [(f"任务{SRAM_TASK}为什么失败？", False, "historical_result_question")]
        for index, (request, preview, source) in enumerate(requests, 1):
            display = ("/plan " if preview else "") + request
            assistant.write_json(folder / "active.json", {"group": group, "turn": index, "request": display, "status": "running"})
            print(f"\n[{group}/{index}] 你：{display}", flush=True)
            started = time.monotonic()
            row = {"group": group, "turn": index, "request": request, "display_input": display,
                   "preview": preview, "source": source, "started_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()}
            try:
                outcome = session.turn(assistant, request, plan_only=preview)
                row.update(mode=outcome["mode"], reply=outcome["reply"], outcome=json.loads(json.dumps(outcome, default=str)))
                audit = Path(outcome["audit_directory"])
                row["metadata"] = json.loads((audit / "metadata.json").read_text())
                if "job" in outcome:
                    job = Path(outcome["job"])
                    row["job"] = str(job)
                    row["result"] = json.loads((job / "result.json").read_text())
                    row["plan"] = json.loads((job / "plan.json").read_text()) if (job / "plan.json").is_file() else None
                    row["integrity"] = json.loads((job / "source_integrity.json").read_text())
                print("助手：" + outcome["reply"], flush=True)
            except Exception as exc:
                row.update(mode="error", reply=session.error_reply(exc), error=repr(exc))
                if session._turn_audit:
                    row["audit_directory"] = str(session._turn_audit)
                print("助手：" + row["reply"], flush=True)
            row["elapsed_s"] = round(time.monotonic() - started, 3)
            records.append(row)
            assistant.write_json(folder / "turns.json", records)
            lines = ["# E1—E6 真实本地问答记录", "", "本文件保留实际输入和最终回复，不替换模型输出。E2/E5带/plan为预览；历史任务以任务ID引用。", ""]
            for r in records:
                lines += [f"## {r['group']} 第{r['turn']}轮", "", "你：" + r["display_input"], "", "助手：", "", r["reply"], "",
                          f"动作：{r['mode']}；本轮耗时{r['elapsed_s']}秒。", ""]
            (folder / "dialogue.md").write_text("\n".join(lines))
            assistant.write_json(folder / "active.json", {"group": group, "turn": index, "status": "finished"})
    after = assistant.manifest(assistant.SOURCE)
    changed = [k for k in set(before) | set(after) if before.get(k) != after.get(k)]
    assistant.write_json(folder / "source_integrity.json", {"unchanged": not changed, "checked_files": len(before), "changes": changed})
    errors = [r["error"] for r in records if r.get("error")]
    assistant.write_json(folder / "result.json", {"kind": "report_dialogue_collection", "status": "completed_with_errors" if errors else "completed",
        "model": assistant.MODEL, "turn_count": len(records), "new_eda_tasks": [r["job"] for r in records if r.get("job")],
        "errors": errors, "no_mocks": True,
        "model_used_turns": sum(bool(r.get("metadata", {}).get("model_used")) for r in records),
        "eda_scope": "计划调用E1两次RTL与E6完整后端，实际启动及成功数以new_eda_tasks及各任务result为准；E2/E5为预览和历史结果读取；E3/E4不执行新EDA"})
    assert not changed
    print("记录完成：" + str(folder / "dialogue.md"), flush=True)


if __name__ == "__main__":
    main()
