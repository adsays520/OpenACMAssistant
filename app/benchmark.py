"""Guarded, matched finite-pool architecture search and independent RTL/STA checks."""
import copy
import csv
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import signal
import statistics
import subprocess
import sys
import time
import uuid
from zoneinfo import ZoneInfo

from acco import BudgetStop, Runner, copy_references, frontier, key
from acco_models import PEA, sha
from benchmark_spec import candidate_pool, from_request, validate_benchmark
from exploration import json_write


def schedule(config):
    methods = config["methods"]
    return [(seed, method) for i, seed in enumerate(config["search_seeds"])
            for method in methods[i % len(methods):] + methods[:i % len(methods)]]


def trial_summary(folder, state, seed, method, elapsed):
    stages = state.get("stages", {})
    records = stages.get("architecture", {}).get("records", [])
    validations = stages.get("validation", {}).get("records", [])
    measured, _ = frontier(validations, [{"metric": "pdp_fj", "direction": "min"}])
    predicted, _ = frontier(records, [{"metric": "predicted_pdp_fj", "direction": "min"}])
    return {"search_seed": seed, "method": method, "status": state["status"], "stop_reason": state.get("stop_reason"),
            "task": str(folder), "evaluated_architectures": len(records),
            "unique_architectures": len({key(r["parameters"]) for r in records}),
            "prediction_feasible": len(predicted), "validation_count": len(validations), "measured_feasible": len(measured),
            "best_predicted_pdp_fj": predicted[0]["metrics"]["predicted_pdp_fj"] if predicted else None,
            "best_measured_pdp_fj": measured[0]["metrics"]["pdp_fj"] if measured else None,
            "best_measured_mred": measured[0]["metrics"].get("mred") if measured else None,
            "best_design": measured[0] if measured else None, "elapsed_s": round(elapsed, 3),
            "counters": state["counters"], "error": state.get("error")}


class Benchmark:
    def __init__(self, api, job, config):
        self.api, self.job, self.config = api, Path(job), validate_benchmark(config)
        self.deadline = time.monotonic() + self.config["max_seconds"]
        self.state = {"kind": "search_comparison", "workflow": "multiplier", "config": self.config, "status": "running",
                      "trials": [], "planned_trials": len(schedule(self.config)), "active_trial": None,
                      "implementation_limit": "有限冻结池内的搜索策略对照；搜索轨迹为GNN预测，只有每轮入选项做RTL/综合/STA复核。固定器件，未作版图或经验消融。"}

    def save(self):
        json_write(self.job / "result.json", self.state)

    def run(self, pea=None):
        started = time.monotonic()
        c, pool = self.config, candidate_pool(self.config)
        json_write(self.job / "candidate_pool.json", pool)
        pool_hash = sha(self.job / "candidate_pool.json")
        self.state["candidate_pool_sha256"] = pool_hash
        from worker import arithmetic_vector_text, command
        vectors = self.job / "test_vectors.hex"
        vectors.write_text(arithmetic_vector_text(c["experiment"]["plan"]["parameters"]))
        self.state["test_vectors_sha256"] = sha(vectors)
        self.state["synthesis_lib_sha256"] = sha(self.api.SOURCE / "DCIM/nangate45/lib/NangateOpenCellLibrary_typical.lib")
        refs = self.job / "references"
        if pea is None:
            pea = PEA(refs / "gnn", c["experiment"]["plan"]["parameters"]["bit_width"],
                      refs / "nbit_approx.py", self.job / "artifacts/truth", command)
            json_write(self.job / "model_provenance.json", pea.provenance)
        self.state["shared_initialization_s"] = round(time.monotonic() - started, 3)
        self.save()
        for seed, method in schedule(c):
            if self.deadline - time.monotonic() < 10:
                break
            relative = f"trials/seed_{seed}/{method}"
            folder = self.job / relative
            folder.mkdir(parents=True)
            (folder / "references").mkdir()
            shutil.copytree(refs / "NSGA", folder / "references/NSGA", ignore=shutil.ignore_patterns("__pycache__"))
            e = copy.deepcopy(c["experiment"])
            e["search_seed"] = seed
            e["budgets"].update(architecture=c["candidate_budget"], validation=c["validation_top_k"],
                shortlist=c["validation_top_k"], max_seconds=max(10, min(c["trial_max_seconds"], int(self.deadline - time.monotonic()))))
            e["solvers"]["architecture"] = method
            json_write(folder / "config.json", e)
            json_write(folder / "conditions.json", {"candidate_pool_sha256": pool_hash,
                "test_vectors_sha256": self.state["test_vectors_sha256"], "search_seed": seed,
                "evaluation_seed": e["plan"]["parameters"]["seed"], "pool_seed": c["pool_seed"],
                "physical_evaluation": e["physical_evaluation"], "device_sizing": "fixed",
                "nsga_domain": "index_of_shared_finite_pool", "llm_menu": "up_to_12_unmeasured_from_shared_pool",
                "experience_injection": False, "random_fallback": False})
            runner = Runner(self.api, folder, e)
            runner.strict_llm = True
            self.state["active_trial"] = {"method": method, "search_seed": seed, "task": relative}
            self.save()
            print(f"对照进度：{len(self.state['trials'])+1}/{self.state['planned_trials']}；{method}，搜索种子{seed}", flush=True)
            trial_started = time.monotonic()
            def prediction(design, task):
                runner.state["counters"]["gnn_queries"] += 1
                return pea.predict(design["compressor_ids"])
            limits = [{**x, "metric": "predicted_" + x["metric"]} for x in e["plan"]["constraints"] if x["metric"] in ("mred", "nmed")]
            try:
                good, _ = runner.search("architecture", pool, prediction, e["architecture_objectives"], limits,
                    c["candidate_budget"], method, "prediction")
                records = runner.state["stages"]["architecture"]["records"]
                choices = good[:c["validation_top_k"]]
                if not choices:
                    choices = sorted((r for r in records if r.get("metrics")),
                        key=lambda r: r["metrics"].get("predicted_mred", math.inf))[:c["validation_top_k"]]
                best = runner.validate_multiplier(choices)
                # The independently produced worker inputs must match the frozen test set.
                for row in runner.state["stages"]["validation"]["records"]:
                    actual = folder / row["task"] / "inputs/vectors.hex"
                    if not actual.is_file() or sha(actual) != self.state["test_vectors_sha256"]:
                        raise ValueError("实际RTL测试输入与冻结输入不一致")
                    row["test_vectors_sha256"] = sha(actual)
                    actual_lib = folder / row["task"] / "workspace/OpenACM/DCIM/nangate45/lib/NangateOpenCellLibrary_typical.lib"
                    row["synthesis_lib_sha256"] = sha(actual_lib)
                    if row["synthesis_lib_sha256"] != self.state["synthesis_lib_sha256"]:
                        raise ValueError("实际综合库与共同库不一致")
                runner.state["best_design"] = best[0] if best else None
                stop = runner.state["stages"]["architecture"]["stop_reason"]
                complete_budget = len(records) == c["candidate_budget"]
                failed_validation = any(r["status"] == "failed" for r in runner.state["stages"]["validation"]["records"])
                runner.state.update(status="incomplete" if not complete_budget else "failed" if failed_validation else "completed",
                                    stop_reason="stages_finished" if complete_budget else stop)
            except BudgetStop:
                runner.state.update(status="incomplete", stop_reason="time_budget")
            except Exception as exc:
                runner.state.update(status="failed", stop_reason="error", error=str(exc))
            for stage in runner.state["stages"].values():
                if stage.get("status") == "running":
                    stage.update(status=runner.state["status"], stop_reason=runner.state.get("stop_reason"))
            runner.save()
            summary = trial_summary(relative, runner.state, seed, method, time.monotonic() - trial_started)
            summary["candidate_pool_sha256"] = pool_hash
            summary["test_vectors_sha256"] = self.state["test_vectors_sha256"]
            self.state["trials"].append(summary)
            self.save()
            render(self.job)
        done = len(self.state["trials"]) == self.state["planned_trials"]
        statuses = {r["status"] for r in self.state["trials"]}
        self.state.update(status="completed" if done and statuses == {"completed"} else "incomplete",
            active_trial=None, stop_reason="trials_finished" if done else "time_budget",
            elapsed_s=round(time.monotonic() - started, 3))
        self.save()
        render(self.job)
        try:
            plot(self.job)
        except Exception as exc:
            (self.job / "reports/plot_error.txt").write_text(str(exc) + "\n")
        return self.state


def plot(job):
    """Export figures from recorded observations; no missing point becomes zero."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    job = Path(job)
    state = json.loads((job / "result.json").read_text())
    rows = list(csv.DictReader((job / "reports/search_curves.csv").open()))
    fig, ax = plt.subplots(figsize=(9, 5))
    for method in state["config"]["methods"]:
        values = []
        for i in range(1, state["config"]["candidate_budget"] + 1):
            samples = [float(r["best_predicted_pdp_fj"]) for r in rows if r["method"] == method
                       and int(r["evaluation"]) == i and r["best_predicted_pdp_fj"]]
            values.append((i, np.median(samples), *np.percentile(samples, [25, 75]), len(samples)) if samples else (i, np.nan, np.nan, np.nan, 0))
        data = np.array(values)
        ax.step(data[:, 0], data[:, 1], where="post", label=method)
        ax.fill_between(data[:, 0], data[:, 2], data[:, 3], step="post", alpha=.15)
    ax.set(xlabel="Unique architectures evaluated (baseline included)", ylabel="Best prediction-feasible PDP (fJ)",
           title="PEA-GNN search: median and interquartile range; observed trials only")
    ax.grid(alpha=.2)
    ax.legend()
    fig.tight_layout()
    for suffix in ("png", "svg", "pdf"):
        fig.savefig(job / f"reports/prediction_search_curve.{suffix}", dpi=180)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(8, 5))
    for i, method in enumerate(state["config"]["methods"]):
        group = [r for r in state["trials"] if r["method"] == method]
        data = [r["best_measured_pdp_fj"] for r in group if r["status"] == "completed" and r["best_measured_pdp_fj"] is not None]
        if data:
            ax.scatter(np.linspace(i-.08, i+.08, len(data)), data)
            ax.plot([i-.16, i+.16], [statistics.median(data)] * 2, color="black")
        ax.text(i, .02, f"{len(data)}/{len(state['config']['search_seeds'])} complete & feasible", transform=ax.get_xaxis_transform(), ha="center", fontsize=8)
    ax.set_xticks(range(len(state["config"]["methods"])), state["config"]["methods"])
    ax.set(ylabel="Best measured PDP among shortlisted designs (fJ)", title="Independent RTL / synthesis / STA validation; fixed test conditions")
    ax.grid(axis="y", alpha=.2)
    fig.tight_layout()
    for suffix in ("png", "svg", "pdf"):
        fig.savefig(job / f"reports/measured_final_comparison.{suffix}", dpi=180)
    plt.close(fig)


def render(job):
    job = Path(job)
    state = json.loads((job / "result.json").read_text())
    reports = job / "reports"
    reports.mkdir(exist_ok=True)
    rows, curves = state.get("trials", []), []
    fields = ["method", "search_seed", "status", "evaluated_architectures", "unique_architectures", "prediction_feasible",
              "validation_count", "measured_feasible", "best_predicted_pdp_fj", "best_measured_pdp_fj", "best_measured_mred", "elapsed_s", "stop_reason"]
    with (reports / "trial_summary.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    for row in rows:
        result = json.loads((job / row["task"] / "result.json").read_text())
        incumbent = None
        for i, obs in enumerate(result.get("stages", {}).get("architecture", {}).get("records", []), 1):
            if obs["status"] == "completed":
                value = obs["metrics"]["predicted_pdp_fj"]
                incumbent = min(incumbent, value) if incumbent is not None else value
            curves.append({"method": row["method"], "search_seed": row["search_seed"], "evaluation": i,
                           "best_predicted_pdp_fj": incumbent, "status": obs["status"]})
    with (reports / "search_curves.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=["method", "search_seed", "evaluation", "best_predicted_pdp_fj", "status"])
        writer.writeheader()
        writer.writerows(curves)
    statistics_rows = []
    for method in state["config"]["methods"]:
        group = [r for r in rows if r["method"] == method]
        measured = [r["best_measured_pdp_fj"] for r in group if r["status"] == "completed" and r["best_measured_pdp_fj"] is not None]
        statistics_rows.append({"method": method, "planned_repeats": len(state["config"]["search_seeds"]),
            "attempted": len(group), "completed": sum(r["status"] == "completed" for r in group), "completed_with_measured_feasible": len(measured),
            "measured_median_pdp_fj": statistics.median(measured) if measured else None,
            "measured_stdev_pdp_fj": statistics.stdev(measured) if len(measured) > 1 else None,
            "total_trial_elapsed_s": round(sum(r["elapsed_s"] for r in group), 3)})
    json_write(reports / "statistics.json", statistics_rows)
    cfg = state["config"]
    p, physical = cfg["experiment"]["plan"]["parameters"], cfg["experiment"]["physical_evaluation"]
    lines = ["# 架构搜索对照实验", "", f"状态：{state['status']}；完成记录{len(rows)}/{state['planned_trials']}轮。",
             f"固定测试种子{p['seed']}、{'穷举' if p['exhaustive'] else str(p['samples'])+'组'}；搜索种子{cfg['search_seeds']}；每轮不同架构上限{cfg['candidate_budget']}（含同一基线）；入选复核上限{cfg['validation_top_k']}。",
             f"共同候选池{cfg['pool_size']}项，池种子{cfg['pool_seed']}，SHA256：{state.get('candidate_pool_sha256', '待生成')}。",
             f"Nangate45 typical；输入活动周期{physical['clock_period_ns']}ns；输出负载{physical['output_load_ff']}fF；器件尺寸固定。",
             "", "| 方法 | 搜索种子 | 状态 | 唯一架构 | 实测/达标 | 预测最佳PDP(fJ) | 实测最佳PDP(fJ) | 总耗时(s) |",
             "|---|---:|---|---:|---:|---:|---:|---:|"]
    def fmt(v):
        return "未测/无可行项" if v is None else f"{v:.6g}"
    for r in rows:
        lines.append(f"| {r['method']} | {r['search_seed']} | {r['status']} | {r['unique_architectures']} | {r['validation_count']}/{r['measured_feasible']} | {fmt(r['best_predicted_pdp_fj'])} | {fmt(r['best_measured_pdp_fj'])} | {r['elapsed_s']} |")
    lines += ["", "共同池对所有方法相同：NSGA-II在池索引上演化，LLM每步从池中至多12个未评价选项选点，随机方法从池中抽取。该实验比较这些具体提案机制，不能推广为所有NSGA-II/LLM实现。",
              "LLM失败会保留失败轮次，不随机回退；无经验或公开标签注入，不跨轮共享未来观测。每轮32等架构计数为GNN查询，未把它们称为32次实测。",
              "搜索轨迹只说明预测可行PDP；每轮按同一规则复核前K项，预测无可行项时复核最低预测MRED项。实测可行性独立判定。GNN训练PPA与本地测量条件未证实完全一致，不把预测/实测差异直接称同分布测试误差。",
              "失败、无可行项和未跑轮次均在表和statistics.json保留；实测中位数/标准差仅对完整且有实测可行项的轮次计算，并报告分母，不能据少量重复宣称统计显著优越。",
              "共享模型初始化单列；每轮elapsed含选点、GNN和EDA，整管线elapsed包含初始化与汇总。详见各trial/result.json中的计数及决策审计。",
              "", state["implementation_limit"]]
    (reports / "report.md").write_text("\n".join(lines) + "\n")
    summary = f"架构对照：{state['status']}，已记录{len(rows)}/{state['planned_trials']}轮。\n" + "\n".join(
        f"{r['method']}：完整{r['completed']}/{r['planned_repeats']}轮，有实测可行项{r['completed_with_measured_feasible']}轮；实测PDP中位数{fmt(r['measured_median_pdp_fj'])}fJ。" for r in statistics_rows)
    (reports / "summary.md").write_text(summary + "\n预测与实测分别统计，有限共同池结果不证明全局最优。\n")


def wait_with_progress(process, job, config):
    deadline = time.monotonic() + config["max_seconds"] + 15
    last_update = 0.
    while True:
        try:
            return process.wait(timeout=min(5, max(.1, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                raise
            if time.monotonic() - last_update >= 20:
                last_update = time.monotonic()
                try:
                    state = json.loads((job / "result.json").read_text())
                    active = state.get("active_trial")
                    detail = ""
                    if active:
                        trial = json.loads((job / active["task"] / "result.json").read_text())
                        count = len(trial.get("stages", {}).get("architecture", {}).get("records", []))
                        detail = f"；{active['method']}，搜索种子{active['search_seed']}，架构{count}/{config['candidate_budget']}"
                    print(f"对照进度：已记录{len(state.get('trials', []))}/{len(schedule(config))}轮" + detail, flush=True)
                except (OSError, ValueError, KeyError):
                    print("对照任务正在初始化，日志写入logs/pipeline.log。", flush=True)


def launch(api, raw):
    config = validate_benchmark(raw)
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    job = api.ROOT / "runs" / f"{stamp}_search_comparison_{uuid.uuid4().hex[:6]}"
    job.mkdir(parents=True)
    for folder in ("logs", "reports", "artifacts", "scratch/home", "scratch/tmp", "scratch/cache"):
        (job / folder).mkdir(parents=True)
    before = api.manifest(api.SOURCE)
    json_write(job / "source_before.json", before)
    json_write(job / "config.json", config)
    implementation = []
    for src in sorted((api.ROOT / "app").glob("*.py")):
        target = job / "implementation/app" / src.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        implementation.append({"path": str(src.relative_to(api.ROOT)), "sha256": sha(src)})
    json_write(job / "implementation_manifest.json", {"files": implementation})
    entries = copy_references(api, job, config["experiment"])
    env = {**os.environ, "HOME": str(job / "scratch/home"), "TMPDIR": str(job / "scratch/tmp"),
           "XDG_CACHE_HOME": str(job / "scratch/cache"), "MPLCONFIGDIR": str(job / "scratch/cache"),
           "MPLBACKEND": "Agg", "CUDA_VISIBLE_DEVICES": "-1", "OMP_NUM_THREADS": "4", "OPENBLAS_NUM_THREADS": "4",
           "PYTHONDONTWRITEBYTECODE": "1", "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    names = {"random": "随机搜索", "nsga2": "NSGA-II", "llm": "DeepSeek"}
    print(f"开始比较{'、'.join(names[m] for m in config['methods'])}，各运行{len(config['search_seeds'])}轮；每轮最多评价{config['candidate_budget']}个架构，复核{config['validation_top_k']}个。", flush=True)
    args = [sys.executable, str(api.ROOT / "app/guard.py"), str(job), sys.executable,
            str(api.ROOT / "app/benchmark_runner.py"), str(job)]
    with (job / "logs/pipeline.log").open("w") as stream:
        process = subprocess.Popen(args, cwd=job, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            wait_with_progress(process, job, config)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            state_path = job / "result.json"
            state = json.loads(state_path.read_text()) if state_path.exists() else {"kind": "search_comparison", "config": config, "trials": [], "planned_trials": len(schedule(config)), "implementation_limit": "管线未完成"}
            state.update(status="incomplete", stop_reason="time_budget_or_interrupted")
            json_write(state_path, state)
    delta = [e["source"] for e in entries if sha(e["source"]) != e["sha256"]]
    unchanged = before == api.manifest(api.SOURCE) and not delta
    json_write(job / "source_integrity.json", {"unchanged": unchanged, "checked_files": len(before), "reference_changes": delta,
        "protection": "Landlock pipeline and nested workers"})
    if not unchanged:
        raise RuntimeError("原始源完整性核验失败")
    if not (job / "result.json").exists():
        json_write(job / "result.json", {"kind": "search_comparison", "status": "failed", "config": config,
            "trials": [], "planned_trials": len(schedule(config)), "implementation_limit": "隔离管线未完成", "error": f"退出码{process.returncode}，见logs/pipeline.log"})
    render(job)
    from experience import automatic_learn
    automatic_learn(api.ROOT, job)  # Only after the entire comparison: no within-run retrieval leakage.
    print((job / "reports/summary.md").read_text(), flush=True)
    print("详细报告：" + str(job / "reports/report.md"), flush=True)
    return job


def command(api, request="", spec_path=None, previous=None, draft=None, plan_only=False):
    config = validate_benchmark(json.loads(Path(spec_path).read_text())) if spec_path else from_request(request, previous, draft)
    return config if plan_only else launch(api, config)
