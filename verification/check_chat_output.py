"""Exercise the actual chat UI: readable dialogue and preview, no process narration."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import assistant


def main():
    turns = ["你能干什么", "开展8位近似乘法器的架构搜索对照实验。",
             "/plan 算术测试固定为种子42、采样4096组；所有方法使用相同的综合库、5ns输入活动周期和10fF输出负载。将搜索随机种子与测试输入种子分开，每种方法重复5次，每次最多评价32个不同架构。",
             "/exit"]
    transcript = io.StringIO()
    with TemporaryDirectory(dir=ROOT / "verification") as tmp, patch.object(assistant, "ROOT", Path(tmp)), \
            patch.object(sys, "argv", ["assistant.py", "chat", "--color", "never"]), \
            patch("builtins.input", side_effect=turns), patch("conversation.urllib.request.build_opener") as opener, \
            patch.object(assistant, "run_plan", side_effect=AssertionError("unrequested EDA")), \
            patch("benchmark.launch", side_effect=AssertionError("preview executed")), redirect_stdout(transcript):
        opener.return_value.open.side_effect = lambda *a, **kw: io.StringIO(json.dumps({"message": {"content": json.dumps({
            "operation": "respond", "message": "<think>内部分析过程</think>已识别为实验需求。", "fact_ids": []})}}))
        assistant.main()
    output = transcript.getvalue()
    assert all(s not in output for s in ["已识别", "正在理解", "正在整理", "内部分析过程", "<think>", '"candidate_budget"', '"search_seeds"'])
    assert "每轮最多评价" in output and "每种方法运行5次" in output
    assert "4096" in output and "32" in output and "5ns" in output and "10fF" in output
    print("PASS: actual chat loop shows direct questions and natural-language plans, hides deliberation/status narration/JSON, and does not execute previews")


if __name__ == "__main__":
    main()
