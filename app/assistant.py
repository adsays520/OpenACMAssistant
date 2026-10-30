"""Natural-language OpenACM front end with isolated, auditable execution."""
import argparse
import copy
from datetime import datetime
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from zoneinfo import ZoneInfo

from spec import DEFAULTS, SCHEMA, validate, compressor_count
from bindings import explicit_parameters, explicit_constraints
from terminal_ui import TerminalUI, THEMES
from result_summary import design_text, summarize
from experience import (ExperienceStore, automatic_learn, retrieve, compact_context,
                        runtime_environment, source_digest)
from public_dataset import retrieve_public

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("OPENACM_SOURCE", str(ROOT.parent / "OpenACM"))).resolve()
PYTHON = Path(sys.executable)
OLLAMA = os.environ.get("OPENACM_OLLAMA_URL", "http://127.0.0.1:11435")
MODEL = os.environ.get("OPENACM_MODEL", "deepseek-script")


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def manifest(root):
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            path = Path(directory) / name
            key = str(path.relative_to(root))
            if path.is_symlink():
                result[key] = {"symlink": os.readlink(path)}
            else:
                h = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        h.update(block)
                st = path.stat()
                result[key] = {"sha256": h.hexdigest(), "size": st.st_size, "mode": st.st_mode & 0o777, "mtime_ns": st.st_mtime_ns}
    return result


def copy_folder(source, destination):
    import shutil
    def ignore(directory, names):
        ignored = []
        for name in names:
            path = Path(directory) / name
            if path.is_symlink():
                raise RuntimeError(f"工作副本拒绝符号链接：{path}")
            if name in {".git", "__pycache__", "sim", "results", "result", "reports"} or name.endswith((".pyc", ".prn", ".mt0", ".json", "_PPA_report.txt")):
                ignored.append(name)
        return ignored
    shutil.copytree(source, destination, ignore=ignore)


def prepare(job, plan):
    for name in ["inputs", "workspace", "artifacts", "logs", "reports", "scratch/home", "scratch/tmp", "scratch/cache"]:
        (job / name).mkdir(parents=True, exist_ok=True)
    write_json(job / "inputs/spec.json", plan)
    repo = job / "workspace/OpenACM"
    (repo / "openacm").mkdir(parents=True)
    workflow = plan["workflow"]
    modules = {"sram": ["sram_compiler"], "multiplier": ["multiplier_compiler"],
               "compressor": ["compressor_sizing"], "dcim": ["multiplier_compiler", "pe_compiler"], "floating": []}[workflow]
    for module in modules:
        copy_folder(SOURCE / "openacm" / module, repo / "openacm" / module)
    if workflow == "dcim":
        copy_folder(SOURCE / "DCIM", repo / "DCIM")
    elif workflow == "floating" or plan["parameters"].get("backend") == "synthesis":
        (repo / "DCIM").mkdir()
        copy_folder(SOURCE / "DCIM/nangate45", repo / "DCIM/nangate45")
        if workflow == "floating":
            copy_folder(SOURCE / "DCIM/multiplier/Approximate_Floating-Point_Multiplier", repo / "floating")
    write_json(job / "inputs/workspace_before.json", manifest(repo))


def evaluate_constraints(metrics, constraints):
    results = []
    for constraint in constraints:
        actual = metrics.get(constraint["metric"])
        passed = None if actual is None else (actual <= constraint["value"] if constraint["op"] == "le" else actual >= constraint["value"])
        results.append(dict(constraint, actual=actual, passed=passed))
    return results


def report(job, result, plan):
    lines = ["# OpenACM 仿真任务报告", "", f"状态：**{result['status']}**", "",
             f"电路类型：`{plan['workflow']}`", "", "## 设计参数", "", "```json",
             json.dumps(plan["parameters"], ensure_ascii=False, indent=2), "```", "", "## 实测指标", "",
             "| 指标（字段后缀为单位） | 结果 |", "|---|---:|"]
    for key, value in result.get("metrics", {}).items():
        lines.append(f"| {key} | {value:.8g} |" if type(value) in (float, int) else f"| {key} | {value} |")
    lines += ["", "## 约束判定", ""]
    for constraint in result.get("constraints", []):
        verdict = {True: "通过", False: "未达标", None: "未测量"}[constraint["passed"]]
        lines.append(f"- `{constraint['metric']}` {constraint['op']} {constraint['value']}：{verdict}，实际 {constraint['actual']}")
    if not plan["constraints"]:
        lines.append("未指定数值验收条件；完成仿真不代表满足某个功耗、时序或精度预算。")
    lines += ["", "## 依据与限制", "",
              "- 原始 OpenACM 仅作为复制来源；仿真进程受 Landlock 写保护，只能写入本任务目录及标准输出设备。",
              "- 原始日志见 `../logs/`，新生成网表、RTL、测量和波形见 `../artifacts/`。",
              "- SRAM 面积是内置公式估算；压缩器使用原仓库的简化 LEVEL=1 模型，7×W×L 面积是启发式估算。",
              "- multiplier及backend=functional的DCiM报告为数字功能仿真；backend=asic的结果还包含参考库后端，均不等于流片签核。",
              "- MRED 排除参考乘积为零的输入；NMED=MAE/(2^n−1)^2；采样结果不等同于穷举证明。",
              "- DCiM 的 PE 检查覆盖常数输入的初始化与运行；不是所有控制序列和输入流的完整验证。",
              "- 有限候选扫描给出候选中最好的已达标项，不宣称全局最优。"]
    if result.get("error"):
        lines += ["", "## 失败原因", "", result["error"]]
    if plan["workflow"] == "dcim" and plan["parameters"]["backend"] == "asic":
        lines += ["", "## 后端阶段与依据", "",
                  "- 阶段记录：[backend_stages.json](backend_stages.json)；各阶段输入脚本在 `../inputs/backend/`，日志在 `../logs/backend_*.log`。",
                  "- 网表、ODB、DEF、SPEF、SDF与VCD在 `../artifacts/backend/`。",
                  "- memory_impl=auto按尺寸使用16×8/32×16/64×32匹配宏，否则采用标准单元存储；这些实现不会随独立SPICE任务自动重新表征。",
                  "- 后仿真采用VCS的SDF路径与timing check，并由STA独立报告静态setup/hold slack；Icarus只用于无延迟门级功能检查。",
                  "- 测试覆盖变化初始化/输入、使能暂停、地址回绕及二次复位；内置valid早于流水线填满，填充期后检查已知有效输出。",
                  "- 后端功耗使用实测VCD活动和Liberty模型，是该工作负载下的模型估算；OpenSTA2.6未提供详细活动覆盖命令，实际标注pin数量见阶段日志。",
                  "- route_drc_count来源于OpenROAD详细布线器，不等于独立全芯片DRC/LVS、宏内部版图或GDS签核。"]
    (job / "reports/report.md").write_text("\n".join(lines) + "\n")


def run_one(job, plan, verbose=False, timeout_override=None):
    prepare(job, plan)
    write_json(job / "inputs/execution_environment.json", runtime_environment(ROOT, source_digest(manifest(SOURCE))))
    env = os.environ.copy()
    env.update(HOME=str(job / "scratch/home"), TMPDIR=str(job / "scratch/tmp"),
               XDG_CACHE_HOME=str(job / "scratch/cache"), MPLCONFIGDIR=str(job / "scratch/cache/matplotlib"),
               MPLBACKEND="Agg", PYTHONDONTWRITEBYTECODE="1", OMP_NUM_THREADS="4", OPENBLAS_NUM_THREADS="4",
               XYCE_NUM_THREADS="4", CUDA_VISIBLE_DEVICES="-1")
    args = [str(PYTHON), str(ROOT / "app/guard.py"), str(job), str(PYTHON), str(ROOT / "app/worker.py"), str(job)]
    started = time.monotonic()
    result = {}
    with (job / "logs/worker.log").open("w") as stream:
        process = subprocess.Popen(args, cwd=job / "workspace/OpenACM", env=env, stdout=stream,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            timeout = 1800 if plan["workflow"] == "dcim" and plan["parameters"]["backend"] == "asic" else 600
            if timeout_override is not None:
                timeout = max(1, min(timeout, timeout_override))
            process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            result = {"status": "failed", "error": f"任务超过 {timeout} 秒，已终止全部子进程" if isinstance(exc, subprocess.TimeoutExpired) else "用户中断，已终止子进程"}
    if not result:
        file = job / "worker_result.json"
        result = json.loads(file.read_text()) if file.exists() else {"status": "failed", "error": f"隔离执行未完成，退出码 {process.returncode}，见 logs/worker.log"}
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    result["constraints"] = evaluate_constraints(result.get("metrics", {}), plan["constraints"])
    if result["status"] == "completed":
        flags = [c["passed"] for c in result["constraints"]]
        if None in flags:
            result["status"] = "unverified_constraints"
        elif False in flags:
            result["status"] = "infeasible"
    repo = job / "workspace/OpenACM"
    before = json.loads((job / "inputs/workspace_before.json").read_text())
    after = manifest(repo)
    changes = [key for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]
    write_json(job / "reports/workspace_changes.json", changes)
    diffs = []
    for key in changes:
        old, new = SOURCE / key, repo / key
        if old.is_file() and new.is_file() and new.stat().st_size < 200000:
            try:
                diffs.extend(difflib.unified_diff(old.read_text().splitlines(True), new.read_text().splitlines(True), fromfile=f"source/{key}", tofile=f"workspace/{key}"))
            except UnicodeError:
                pass
    (job / "reports/workspace_changes.diff").write_text("".join(diffs))
    write_json(job / "result.json", result)
    report(job, result, plan)
    if verbose:
        print(f"{job.name}: {result['status']}；报告 {job / 'reports/report.md'}", flush=True)
    return result


def run_plan(plan, prompt="", model_reply=None, job_parent=None, verbose=False):
    plan = validate(plan)
    if plan["intent"] != "run":
        print(plan["message"])
        return None
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    job = (job_parent or ROOT / "runs") / f"{stamp}_{plan['workflow']}_{uuid.uuid4().hex[:6]}"
    job.mkdir(parents=True)
    before = manifest(SOURCE)
    write_json(job / "source_before.json", before)
    (job / "request.txt").write_text(prompt + "\n")
    write_json(job / "plan.json", plan)
    if model_reply:
        write_json(job / "model_reply.json", model_reply)
    print(f"开始仿真：{design_text(plan)}。", flush=True)
    if verbose:
        print(f"任务目录：{job}\n执行参数：{json.dumps(plan['parameters'], ensure_ascii=False)}", flush=True)
    try:
        if plan["sweep"]:
            results = []
            for index, value in enumerate(plan["sweep"]["values"]):
                candidate = copy.deepcopy(plan)
                candidate["parameters"][plan["sweep"]["parameter"]] = value
                candidate["sweep"] = None
                print(f"扫描候选 {index+1}/{len(plan['sweep']['values'])}…", flush=True)
                result = run_one(job / f"candidate_{index+1:02d}", candidate, verbose=verbose)
                results.append({"candidate": index + 1, "value": value, **result})
            feasible = [r for r in results if r["status"] == "completed"]
            objective = plan["objective"]
            best = None
            if objective:
                measured = [r for r in feasible if objective["metric"] in r.get("metrics", {})]
                if measured:
                    choose = min if objective["direction"] == "min" else max
                    best = choose(measured, key=lambda r: r["metrics"][objective["metric"]])["candidate"]
            write_json(job / "batch_results.json", {"results": results, "best_feasible_candidate": best})
            write_json(job / "result.json", {"status": "completed" if feasible else "failed", "candidate_count": len(results),
                                            "completed_candidates": len(feasible), "best_feasible_candidate": best})
            (job / "report.md").write_text("# 候选扫描报告\n\n" + "\n".join(f"- [候选 {r['candidate']}](candidate_{r['candidate']:02d}/reports/report.md)：{r['status']}，参数值 {r['value']}" for r in results) + f"\n\n最佳已达标候选：{best}（只比较本次有限候选）\n")
        else:
            run_one(job, plan, verbose=verbose)
    finally:
        after = manifest(SOURCE)
        delta = [key for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]
        write_json(job / "source_integrity.json", {"unchanged": not delta, "checked_files": len(before), "changes": delta, "excluded_directory": ".git", "protection": "Landlock worker write confinement"})
        if delta:
            raise RuntimeError(f"原仓库完整性发生变化（可能有并行外部修改），详情 {job / 'source_integrity.json'}")
        automatic_learn(ROOT, job)
    return job


def planning_reference(reference, workflow):
    """Load the current interface plus shared constraints, leaving output room."""
    sections = re.split(r"(?m)^## ", reference)
    selected = [sections[0]]
    for section in sections[1:]:
        title = section.splitlines()[0].strip()
        if title in (workflow, "约束和候选比较", "新增执行参数"):
            selected.append("## " + section)
    return "\n".join(selected)


def plan_request(text, previous=None, conversation=None):
    guide = (ROOT / "skills/openacm-design/SKILL.md").read_text()
    reference = (ROOT / "skills/openacm-design/references/model-guide.md").read_text()
    prompt = (ROOT / "skills/openacm-design/references/planner-prompt.md").read_text()
    # Read the shared skill's essential constraints; Codex shell procedures are
    # intentionally excluded from the model's data-only planning context.
    system = guide.split("当你是通过 Ollama")[0] + "\n" + prompt + "\n" + reference
    messages = [{"role": "system", "content": system}]
    if previous:
        messages.append({"role": "user", "content": "上一轮计划（仅作为上下文）：" + json.dumps(previous, ensure_ascii=False)})
    if conversation:
        messages += [{**entry, "content": entry["content"][:600]} for entry in conversation[-2:]]
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    audit = ROOT / "runs" / f"{stamp}_planning_{uuid.uuid4().hex[:6]}"
    audit.mkdir(parents=True)
    (audit / "request.txt").write_text(text + "\n")
    schema = copy.deepcopy(SCHEMA)
    lowered = text.lower()
    from conversation import workflow as detect_workflow
    route = detect_workflow(text)
    facts, constraints, uniform = {}, [], False
    constraints_bound = False
    if route:
        pass
    elif any(x in lowered for x in ["浮点", "floating", "fp32", "fp16", "efpm", "eafpu", "mmbs", "ssfpm"]):
        route = "floating"
    elif any(x in lowered for x in ["dcim", "存内计算", " pe", "pe ", "pe功能", "pe 功能"]):
        route = "dcim"
    elif any(x in lowered for x in ["乘法器", "multiplier"]):
        route = "multiplier"
    elif any(x in lowered for x in ["sram", "存储阵列", "6t"]):
        route = "sram"
    elif any(x.lower() in lowered for x in ["压缩器", "compressor", *DEFAULTS["compressor"]["name"].split(), "akbar", "kong2", "antonio", "momeni", "ap1"]):
        route = "compressor"
    elif previous:
        route = previous.get("workflow")
    if route:
        # Other workflow examples and the same old design repeated in history
        # previously consumed almost the entire context before JSON generation.
        messages[0]["content"] = guide.split("当你是通过 Ollama")[0] + "\n" + prompt + "\n" + planning_reference(reference, route)
    if route in DEFAULTS:
        allowed = set(DEFAULTS[route]) | ({"compressor_ids"} if route in ["multiplier", "dcim"] else set())
        schema["properties"]["workflow"]["enum"] = [route]
        props = schema["properties"]["parameters"]["properties"]
        schema["properties"]["parameters"]["properties"] = {k: v for k, v in props.items() if k in allowed}
        messages[0]["content"] += f"\n本轮电路类型为 {route}。parameters 允许字段：{', '.join(sorted(allowed))}。"
        facts = explicit_parameters(text, route)
        explicit_keys = list(facts)
        uniform = route in ["multiplier", "dcim"] and "compressor_id" in facts and not any(
            marker in lowered for marker in ["逐位置", "逐个", "列表", "其余", "前", "list", "分配"])
        if previous and previous.get("workflow") == route and any(marker in text for marker in ["其余不变", "其他不变", "其它不变", "只把", "只改", "仅改", "最佳配置", "最优配置", "刚才的最佳", "按刚才", "复测"]):
            facts = {**previous["parameters"], **facts}
        if uniform:
            facts.pop("compressor_ids", None)
            schema["properties"]["parameters"]["properties"].pop("compressor_ids", None)
            messages[0]["content"] += "\n本轮所有近似位置使用同一种压缩器，只提供compressor_id，不输出compressor_ids数组，由执行器按位宽展开。"
        elif route in ["multiplier", "dcim"] and "bit_width" in facts and 4 <= facts["bit_width"] <= 64:
            count = compressor_count(facts["bit_width"],facts.get("architecture","auto"))
            ids_schema = schema["properties"]["parameters"]["properties"]["compressor_ids"]
            ids_schema.update(minItems=count, maxItems=count)
        for key, value in facts.items():
            schema["properties"]["parameters"]["properties"][key] = {"const": value}
        schema["properties"]["parameters"]["required"] = explicit_keys
        write_json(audit / "explicit_parameters.json", facts)
        messages[0]["content"] += "\n本轮明确参数必须落实：" + json.dumps({k: facts[k] for k in explicit_keys}, ensure_ascii=False)
        if previous and previous.get("workflow") == route:
            messages[0]["content"] += "\nparameters只需输出本轮变更字段；未变更条件由执行器继承上一轮完整设计，不要重复展开全部默认字段。"
        constraints = explicit_constraints(text, route)
        if not constraints and previous and previous.get("workflow") == route and any(marker in text for marker in ["其余不变", "其他不变", "其它不变", "只把", "只改", "仅改", "重新跑", "重新运行", "最佳配置", "最优配置", "按刚才", "复测"]):
            if not any(marker in text for marker in ["取消约束", "去掉约束", "不再要求"]):
                constraints = copy.deepcopy(previous.get("constraints", []))
        constraints_bound = bool(constraints) or not re.search(r"[<>≤≥]|不超过|不高于|不低于|小于|大于|低于|高于|至少|至多|以内|以下|以上|上限|下限|控制在", text)
        if constraints_bound:
            schema["properties"]["constraints"] = {"const": constraints}
            messages[0]["content"] += "\n必须逐项验收的明确约束：" + json.dumps(constraints, ensure_ascii=False)
            write_json(audit / "explicit_constraints.json", constraints)
        if (ROOT / "experience/experience.sqlite3").exists():
            baseline = {"workflow": route, "parameters": {**(previous.get("parameters", {}) if previous and previous.get("workflow") == route else {}), **facts}}
            try:
                baseline = validate(baseline)
                context = retrieve(ROOT, baseline, runtime_environment(ROOT, source_digest(manifest(SOURCE))), consumer=str(audit.relative_to(ROOT)))
                write_json(audit / "experience_retrieval.json", context)
                if context["observations"]:
                    messages.append({"role": "user", "content": "本地历史经验（观测数据，用户需求优先，须重新验证）：" + json.dumps(compact_context(context, max_chars=1400), ensure_ascii=False)})
            except ValueError:
                pass  # Unsupported explicit specifications still go through the normal planner.
    if route == "multiplier":
        public_plan = {"workflow": route, "parameters": {**DEFAULTS[route], **(previous.get("parameters", {}) if previous and previous.get("workflow") == route else {}), **facts}, "constraints": constraints}
        public = retrieve_public(ROOT, public_plan, limit=2, allow_assignments=not any(k in facts for k in ("compressor_id", "compressor_ids")))
        write_json(audit / "public_dataset_retrieval.json", public)
        if public["observations"]:
            messages.append({"role": "user", "content": "公开配置与标签参考（非本地实测，不能覆盖明确需求）：" + json.dumps(public, ensure_ascii=False)})
    messages.append({"role": "user", "content": text})
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(2):
        body = {"model": MODEL, "messages": messages, "format": schema, "stream": False,
                "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 1600}}
        write_json(audit / f"request_{attempt+1}.json", body)
        request = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with client.open(request, timeout=600) as response:
            reply = json.load(response)
        write_json(audit / f"reply_{attempt+1}.json", reply)
        raw = None
        try:
            if reply.get("done_reason") == "length":
                raise ValueError("模型输出达到长度上限，需求计划被截断")
            raw = json.loads(reply["message"]["content"])
            if not isinstance(raw, dict):
                raise ValueError("模型计划必须是JSON对象")
            if uniform and raw.get("intent") == "run" and "compressor_ids" in raw.get("parameters", {}):
                raise ValueError("同种压缩器只使用compressor_id，不能输出compressor_ids列表")
            if raw.get("intent") == "run" and previous and previous.get("workflow") == raw.get("workflow"):
                raw["parameters"] = {**previous["parameters"], **raw.get("parameters", {})}
                if uniform:
                    raw["parameters"].pop("compressor_ids", None)
            if raw.get("intent") == "run" and raw.get("parameters", {}).get("kind") in ["exact", "log"] and "compressor_ids" not in facts:
                raw["parameters"].pop("compressor_ids", None)  # These implementations do not use an approximate assignment.
            plan = validate(raw)
            if plan["intent"] == "run":
                if any(plan["parameters"].get(key) != value for key, value in facts.items()):
                    raise ValueError("模型未落实明确给定的参数")
                if constraints_bound and plan["constraints"] != constraints:
                    raise ValueError("模型未落实明确给定的验收约束，或添加了用户未要求的阈值")
            write_json(audit / "plan.json", plan)
            return plan, {"audit_directory": str(audit), "reply": reply}
        except (ValueError, TypeError, KeyError) as exc:
            (audit / f"validation_error_{attempt+1}.txt").write_text(str(exc) + "\n")
            if attempt == 1:
                raise RuntimeError(f"DeepSeek连续两次未生成有效的需求计划，尚未执行仿真。可重试；诊断记录：{audit}") from exc
            workflow = route or (raw.get("workflow") if isinstance(raw, dict) else None)
            if workflow in DEFAULTS:
                permitted = set(DEFAULTS[workflow]) | ({"compressor_ids"} if workflow in ["multiplier", "dcim"] else set())
                schema["properties"]["workflow"]["enum"] = [workflow]
                props = schema["properties"]["parameters"]["properties"]
                schema["properties"]["parameters"]["properties"] = {k: v for k, v in props.items() if k in permitted}
            # Do not feed the truncated repetitive array back into the model.
            messages.append({"role": "system", "content": f"上次计划未通过校验：{exc}。忠实整理最后的原始需求，给出短小完整JSON。只输出本轮修改字段；不重复默认值或数组。同种压缩器仅用compressor_id。不支持的需求用intent=clarify。"})


def main():
    parser = argparse.ArgumentParser(description="只读 OpenACM 的本地 DeepSeek 电路助手")
    sub = parser.add_subparsers(dest="mode", required=True)
    ask = sub.add_parser("ask", help="自然语言生成并执行任务")
    ask.add_argument("request")
    ask.add_argument("--plan-only", action="store_true")
    run = sub.add_parser("run", help="执行已保存的 JSON 需求")
    run.add_argument("spec", type=Path)
    run.add_argument("--no-model-summary", action="store_true", help="直接汇总实测数据，不调用DeepSeek整理总结")
    chat = sub.add_parser("chat", help="连续交流；默认自动执行明确的仿真需求")
    chat.add_argument("--theme", choices=THEMES, default="neon", help="启动配色（默认 neon）")
    chat.add_argument("--color", choices=["auto", "always", "never"], default="auto", help="终端颜色模式")
    chat.add_argument("--banner-only", action="store_true", help="预览启动界面后退出")
    sub.add_parser("doctor", help="只读依赖与 Landlock 检查")
    full = sub.add_parser("full-flow", help="运行四类参考电路，含DCiM完整后端，并汇总结果")
    full.add_argument("--without-model", action="store_true", help="使用固定参考配置，跳过DeepSeek需求规划")
    explore = sub.add_parser("explore", help="DeepSeek根据实测反馈逐轮进行设计空间探索")
    explore.add_argument("request", nargs="?", default="")
    explore.add_argument("--workflow", choices=[*DEFAULTS,"all"])
    explore.add_argument("--budget", type=int, default=64, help="每类候选预算，默认64，可提高到10000")
    explore.add_argument("--max-seconds", type=int, default=7200, help="每类本次运行的时间预算")
    explore.add_argument("--spec", type=Path, help="明确指定plan/space/objectives的探索JSON")
    explore.add_argument("--resume", type=Path, help="续跑已有单类探索目录，保留历史")
    explore.add_argument("--without-model", action="store_true", help="确定性候选选择，便于复现与调试")
    explore.add_argument("--legacy", action="store_true", help="明确使用旧的单级局部探索，兼容历史实验")
    acco = sub.add_parser("acco", help="论文方向：架构搜索、实测复核、器件优化的两级ACCO")
    acco.add_argument("request", nargs="?", default="")
    acco.add_argument("--spec", type=Path, help="两级实验JSON，包含容量/精度与分阶段预算")
    acco.add_argument("--workflow", choices=["sram", "multiplier"])
    acco.add_argument("--budget", type=int, default=64)
    acco.add_argument("--max-seconds", type=int, default=7200)
    acco.add_argument("--without-model", action="store_true")
    acco.add_argument("--plan-only", action="store_true")
    compare = sub.add_parser("compare", help="冻结候选池的多方法、多搜索种子架构对照，含入选实测")
    compare.add_argument("request", nargs="?", default="")
    compare.add_argument("--spec", type=Path, help="search_comparison实验JSON")
    compare.add_argument("--plan-only", action="store_true")
    summary = sub.add_parser("summarize", help="直接总结已有任务结果，不重跑仿真")
    summary.add_argument("task", type=Path)
    summary.add_argument("--no-model", action="store_true", help="直接汇总实测数据")
    memory = sub.add_parser("memory", help="本地经验积累、检索和跨模型JSONL交换")
    actions = memory.add_subparsers(dest="action", required=True)
    actions.add_parser("stats", help="查看经验库统计")
    learn = actions.add_parser("learn", help="导入历史实际运行，重复导入不会增加重复记录")
    learn.add_argument("--task", type=Path)
    actions.add_parser("export", help="导出模型无关JSONL到experience/exports")
    load = actions.add_parser("import", help="导入其它模型/机器的JSONL，仅作为未验证参考")
    load.add_argument("file", type=Path)
    query = actions.add_parser("query", help="输出模型无关经验上下文JSON")
    query.add_argument("--workflow", choices=list(DEFAULTS))
    query.add_argument("--spec", type=Path)
    query.add_argument("--limit", type=int, default=6)
    dataset = sub.add_parser("dataset", help="公开电路数据下载、对照和模型检索")
    dataset.add_argument("action", choices=["fetch", "prepare", "status", "query", "verify"])
    dataset.add_argument("--spec", type=Path)
    dataset.add_argument("--limit", type=int, default=6)
    dataset.add_argument("--without-model", action="store_true", help="verify仅复测，不调用DeepSeek")
    args = parser.parse_args()
    if args.mode == "dataset":
        from public_dataset import fetch, prepare, directory, query, verify
        if args.action == "fetch":
            result = fetch(ROOT)
            result = {"repository": result["repository"], "revision": result["revision"], "files": len(result["files"]), "total_bytes": result["total_bytes"]}
        elif args.action == "prepare":
            result = prepare(ROOT, SOURCE)
        elif args.action == "status":
            report = directory(ROOT) / "audits/validation.json"
            result = json.loads(report.read_text()) if report.exists() else {"ready": False, "directory": str(directory(ROOT))}
        elif args.action == "verify":
            result = verify(sys.modules[__name__], use_model=not args.without_model)
        else:
            plan = json.loads(args.spec.read_text()) if args.spec else {"workflow": "multiplier", "parameters": {"bit_width": 8, "kind": "approx"}, "constraints": []}
            if "plan" in plan:
                plan = plan["plan"]
            result = query(ROOT, plan, limit=args.limit, allow_assignments=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if args.action == "verify" and result["status"] != "completed" else 0
    if args.mode == "memory":
        store = ExperienceStore(ROOT)
        if args.action == "stats":
            print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
        elif args.action == "learn":
            result = store.learn(args.task)
            store.export()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2 if result["errors"] else 0
        elif args.action == "export":
            print(store.export())
        elif args.action == "import":
            print(f"新增外部参考经验：{store.import_file(args.file)}")
            store.export()
        else:
            if not args.spec and not args.workflow:
                raise ValueError("memory query需提供--spec或--workflow")
            plan = json.loads(args.spec.read_text()) if args.spec else {"workflow": args.workflow, "parameters": {}}
            if "plan" in plan:
                plan = plan["plan"]
            environment = runtime_environment(ROOT, source_digest(manifest(SOURCE)))
            context = store.query(plan, environment, limit=args.limit, consumer="cli_query")
            print(json.dumps(context, ensure_ascii=False, indent=2))
        return 0
    if args.mode == "compare":
        from benchmark import command
        outcome = command(sys.modules[__name__], args.request, args.spec, plan_only=args.plan_only)
        if args.plan_only:
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
            return 0
        return 0 if json.loads((outcome / "result.json").read_text())["status"] == "completed" else 2
    if args.mode == "acco":
        from acco import command
        result = command(sys.modules[__name__], args.request, args.spec, args.workflow, args.budget,
                         args.max_seconds, not args.without_model, plan_only=args.plan_only)
        if args.plan_only:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        return 0 if json.loads((result / "result.json").read_text())["status"] == "completed" else 2
    if args.mode == "explore":
        from exploration import explore_command
        job = explore_command(sys.modules[__name__],args.request,args.workflow,args.budget,args.max_seconds,
                              not args.without_model,args.spec,args.resume,legacy=args.legacy)
        return 0 if json.loads((job / "result.json").read_text())["status"] == "completed" else 2
    if args.mode == "full-flow":
        from full_flow import run_full_flow
        return run_full_flow(ROOT, SOURCE, plan_request, run_plan, manifest, use_model=not args.without_model,
                             summarizer=lambda job: summarize(job, OLLAMA, MODEL, use_model=not args.without_model))
    if args.mode == "summarize":
        task = args.task.resolve(strict=True)
        if not task.is_relative_to(ROOT / "runs") or not any((task / name).is_file() for name in ["result.json", "batch_results.json"]):
            raise ValueError("请选择OpenACMAssistant/runs中的已有任务目录，且需包含result.json或batch_results.json")
        if (task / "reports").is_symlink():
            raise ValueError("总结目录不能是符号链接")
        summarize(task, OLLAMA, MODEL, use_model=not args.no_model)
        return
    if args.mode == "doctor":
        import ctypes
        import shutil
        print("源目录：", SOURCE)
        print("Landlock ABI：", ctypes.CDLL(None).syscall(444, 0, 0, 1))
        for binary in ["Xyce", "iverilog", "vvp", "yosys", "openroad", "sta", "opensta", "vcs", "gcc"]:
            print(binary + ":", shutil.which(binary))
        return
    if args.mode == "run":
        job = run_plan(json.loads(args.spec.read_text()), prompt=f"执行配置 {args.spec.resolve()}")
        if job:
            summarize(job, OLLAMA, MODEL, use_model=not args.no_model_summary)
        if job and json.loads((job / "result.json").read_text())["status"] != "completed":
            return 2
    elif args.mode == "ask":
        from conversation import Session
        outcome = Session().turn(sys.modules[__name__], args.request, plan_only=args.plan_only)
        if outcome["mode"] == "plan" and args.plan_only:
            print(json.dumps(outcome["plan"], ensure_ascii=False, indent=2))
        elif "job" not in outcome:
            print(outcome["reply"])
        if "job" in outcome and json.loads((outcome["job"] / "result.json").read_text())["status"] != "completed":
            return 2
    else:
        from conversation import Session
        ui = TerminalUI(args.theme, args.color)
        session = Session.restore(ROOT)
        ui.banner()
        if args.banner_only:
            return
        while True:
            try:
                text = input(ui.prompt()).strip()
                if text == "/exit":
                    ui.status("已退出交流。")
                    break
                if text == "/new":
                    session = Session.restore(ROOT, reset=True)
                    ui.status("已开启新对话。")
                    continue
                if text == "/memory":
                    print(json.dumps(ExperienceStore(ROOT).stats(), ensure_ascii=False, indent=2))
                    ui.status(f"经验目录：{ROOT / 'experience'}")
                    continue
                if not text:
                    continue
                if ui.command(text):
                    continue
                only = text.startswith("/plan ")
                request = text[6:] if only else text
                outcome = session.turn(sys.modules[__name__], request, plan_only=only, ui=ui)
                if "job" not in outcome:
                    print(outcome["reply"])
            except (EOFError, KeyboardInterrupt):
                ui.status("已退出交流。")
                break
            except Exception as exc:
                ui.error(session.error_reply(exc))


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)
