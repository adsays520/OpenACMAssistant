"""Guarded ACCO orchestration: architecture -> shortlist -> device refinement."""
import copy
from datetime import datetime
import itertools
import json
import math
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import time
import uuid
from zoneinfo import ZoneInfo

from acco_spec import validate_acco
from acco_models import PEA, aggregate_corners, architecture_grid, bank_metrics, load_module, sha
from acco_physics import compressor_width, multiplier_ppa, width_layout
from exploration import choose_model, json_write
from spec import validate, compressor_count


def key(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def frontier(records, objectives):
    good = [r for r in records if r["status"] == "completed" and all(type(r.get("metrics", {}).get(o["metric"])) in (int, float)
            and math.isfinite(r["metrics"][o["metric"]]) for o in objectives)]
    def score(r):
        return tuple(r["metrics"][o["metric"]] * (1 if o["direction"] == "min" else -1) for o in objectives)
    good.sort(key=score)
    pareto = [r for r in good if not any(all(a <= b for a, b in zip(score(s), score(r)))
                  and any(a < b for a, b in zip(score(s), score(r))) for s in good)]
    return good, pareto


def constraints(metrics, items):
    checked = []
    for c in items:
        value = metrics.get(c["metric"])
        valid = type(value) in (int, float) and math.isfinite(value)
        passed = None if not valid else (value <= c["value"] if c["op"] == "le" else value >= c["value"])
        checked.append({**c, "actual": value, "passed": passed})
    return checked


def copy_references(api, job, config):
    source = api.SOURCE
    refs = job / "references"
    entries = []
    def folder(src, dst, suffixes):
        for item in src.rglob("*"):
            if not item.is_file() or item.suffix.lower() not in suffixes or any(part in ("result", "results", "__pycache__", "ppa_results") for part in item.relative_to(src).parts):
                continue
            if item.is_symlink():
                raise ValueError("参考来源不能是符号链接")
            target = dst / item.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            entries.append({"source": str(item), "copy": str(target.relative_to(job)), "sha256": sha(item)})
    folder(source / "DCIM_OPT/algorithms/NSGA", refs / "NSGA", {".py"})
    if config["workflow"] == "sram":
        oy = Path(os.environ.get("OPENYIELD_SOURCE", str(source.parent / "OpenYield/OpenYield"))).resolve(strict=True)
        for item in [oy / "utils.py", oy / "size_optimization/experiment.py"]:
            if not item.is_file() or item.is_symlink():
                raise ValueError("缺少OpenYield原始架构/面积模型")
            target = refs / "OpenYield" / item.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            entries.append({"source": str(item), "copy": str(target.relative_to(job)), "sha256": sha(item)})
    else:
        bits = config["plan"]["parameters"]["bit_width"]
        folder(source / f"DCIM_OPT/multiplier_gnn/{bits}-bitGNN", refs / "gnn", {".py", ".txt", ".csv", ".pth"})
        folder(source / "DCIM_OPT/compressor_sizing", refs / "compressor_sizing", {".py", ".sp"})
        compiler = source / "openacm/multiplier_compiler/nbit_approx.py"
        shutil.copy2(compiler, refs / "nbit_approx.py")
        entries.append({"source": str(compiler), "copy": str((refs / "nbit_approx.py").relative_to(job)), "sha256": sha(compiler)})
    json_write(job / "reference_manifest.json", entries)
    return entries


class BudgetStop(BaseException):
    pass


class Runner:
    def __init__(self, api, job, config):
        self.api, self.job, self.config = api, Path(job), validate_acco(config)
        self.deadline = time.monotonic() + self.config["budgets"]["max_seconds"]
        self.rng = random.Random(self.config["plan"]["parameters"]["seed"])
        self.state = {"kind": "acco", "workflow": config["workflow"], "status": "running", "config": self.config,
                      "stages": {}, "best_design": None, "counters": {"gnn_queries": 0, "array_or_rtl_tasks": 0,
                          "sta_activity_evaluations": 0, "device_candidates": 0, "llm_calls": 0}}
        from worker import command
        self.command = command

    def remaining(self):
        n = self.deadline - time.monotonic()
        if n < 2:
            raise BudgetStop("time_budget")
        return n

    def save(self):
        json_write(self.job / "result.json", self.state)

    def run_array(self, folder, plan):
        self.remaining()
        self.state["counters"]["array_or_rtl_tasks"] += 1
        return self.api.run_one(folder, validate(plan), timeout_override=min(600, self.remaining()))

    def search(self, name, pool, evaluate, objectives, limits, count, solver, evidence, parent=None, domains=None, build=None):
        stage = {"name": name, "parent": parent, "solver": solver, "evidence_kind": evidence,
                 "budget": count, "records": [], "status": "running"}
        self.state["stages"][name] = stage
        records, cache = stage["records"], {}
        def observation(design, proposer="nsga2"):
            self.remaining()
            identity = key(design)
            if identity in cache:
                return cache[identity]
            if len(records) >= count:
                raise BudgetStop("candidate_budget")
            number = len(records) + 1
            task = self.job / "stages" / name / f"{number:04d}"
            task.mkdir(parents=True, exist_ok=True)
            json_write(task / "design.json", design)
            record = {"candidate": f"{name}/{number:04d}", "parameters": design, "parent": parent,
                      "task": str(task.relative_to(self.job)), "proposed_by": proposer, "evidence_kind": evidence}
            started = time.monotonic()
            try:
                metrics = evaluate(design, task)
                check = constraints(metrics, limits)
                missing = any(o["metric"] not in metrics for o in objectives)
                status = "unverified_constraints" if missing or any(c["passed"] is None for c in check) else "infeasible" if any(c["passed"] is False for c in check) else "completed"
                record.update(status=status, metrics=metrics, constraints=check,
                              acceptance="prediction_only" if evidence == "prediction" else "specified_model_and_cases")
            except Exception as exc:
                record.update(status="failed", error=str(exc), metrics={})
            record["elapsed_s"] = round(time.monotonic() - started, 3)
            records.append(record)
            cache[identity] = record
            json_write(task / "result.json", record)
            self.save()
            print(f"  {record['candidate']}：{record['status']}；{evidence}", flush=True)
            return record
        def numeric(record):
            if record["status"] != "completed":
                return [1e30] * len(objectives)
            return [record["metrics"][o["metric"]] * (1 if o["direction"] == "min" else -1) for o in objectives]
        try:
            observation(pool[0], "baseline")
            if solver == "nsga2" and len(pool) > 1:
                # Execute the original optimizer with a hard unique-evaluation cap.
                path = self.job / "references/NSGA"
                for n in ("individual", "population", "problem", "utils", "evolution"):
                    load_module(n, path / f"{n}.py")
                problem_cls = sys.modules["problem"].Problem
                evolution_cls = sys.modules["evolution"].Evolution
                dimensions = [len(v) for v in domains.values()] if domains else [len(pool)]
                calls = 0
                def metric(state, index):
                    nonlocal calls
                    calls += 1
                    if calls > max(100, count * 200):
                        raise BudgetStop("proposal_limit")
                    design = build({k: values[int(i)] for (k, values), i in zip(domains.items(), state)}) if domains else pool[int(state[0])]
                    return numeric(observation(design))[index]
                prob = problem_cls(objectives=[lambda s, i=i: metric(s, i) for i in range(len(objectives))],
                    num_of_variables=len(dimensions), variables_range=[(0, n-1) for n in dimensions], variables_type=["int"]*len(dimensions), expand=False)
                random.seed(self.config["plan"]["parameters"]["seed"])
                population_size = max(2, min(8, count, math.prod(dimensions)))
                generate = prob.generate_individual
                initial = set()
                def distinct_initial():
                    for _ in range(1000):
                        individual = generate()
                        token = tuple(individual.features)
                        if token not in initial:
                            initial.add(token)
                            if len(initial) >= population_size:
                                prob.generate_individual = generate
                            return individual
                    raise BudgetStop("initial_population_exhausted")
                prob.generate_individual = distinct_initial
                optimizer = evolution_cls(prob, num_of_generations=max(1, count), num_of_individuals=population_size, num_of_tour_particips=2)
                optimizer.evolve()
            else:
                pending = pool[1:]
                while pending and len(records) < count:
                    self.remaining()
                    if solver == "llm":
                        options = pending[:6] + (self.rng.sample(pending[6:], min(6, len(pending)-6)) if len(pending) > 6 else [])
                        audit = self.job / "decisions" / name / f"{len(records)+1:04d}"
                        audit.mkdir(parents=True, exist_ok=True)
                        json_write(audit / "menu.json", options)
                        self.state["counters"]["llm_calls"] += 1
                        try:
                            selected, reason = choose_model([{"parameters": d} for d in options], records,
                                {"plan": {"constraints": limits}, "objectives": objectives}, self.api.OLLAMA, self.api.MODEL,
                                audit, min(120, self.remaining()))
                            design, proposer = options[selected], "deepseek"
                            json_write(audit / "selection.json", {"index": selected, "reason": reason})
                        except Exception as exc:
                            (audit / "model_error.txt").write_text(str(exc))
                            design, proposer = self.rng.choice(pending), "random_fallback"
                    else:
                        design, proposer = self.rng.choice(pending), "random"
                    pending.remove(design)
                    observation(design, proposer)
            stage["stop_reason"] = "candidate_budget" if len(records) >= count else "space_or_solver_finished"
        except BudgetStop as exc:
            stage["stop_reason"] = str(exc)
        good, pareto = frontier(records, objectives)
        stage.update(status="completed" if good else "infeasible", pareto_candidates=[r["candidate"] for r in pareto],
                     best_candidate=good[0]["candidate"] if good else None)
        if good and records and records[0]["status"] == "completed":
            stage["baseline_comparison"] = {o["metric"]: {"baseline": records[0]["metrics"][o["metric"]],
                "best": good[0]["metrics"][o["metric"]], "difference": good[0]["metrics"][o["metric"]] - records[0]["metrics"][o["metric"]]}
                for o in objectives}
        self.save()
        return good, pareto

    def sram(self):
        c = self.config
        refs = self.job / "references/OpenYield"
        area_model = load_module("acco_openyield_area", refs / "utils.py")
        grid = architecture_grid(refs / "experiment.py", c)
        p = c["plan"]["parameters"]
        def evaluate(design, task, robust=False):
            if robust:
                self.state["counters"]["device_candidates"] += 1
            arch, devices = design["architecture"], design["devices"]
            if arch["rows"] * arch["cols"] * arch["num_arrays"] != c["capacity_bits"]:
                raise ValueError("候选破坏固定总容量")
            params = {**p, **devices, "rows": arch["rows"], "cols": arch["cols"], "column_mux": arch["mux_ratio"] == 2}
            needed = {x["metric"] for x in c["bank_constraints"] + c["architecture_objectives"]}
            all_snm = robust or bool(needed & {"min_snm_v", "fom_robust_estimate"})
            operations = ["read", "write"] + (["hold_snm", "read_snm", "write_snm"] if all_snm else
                [name.removesuffix("_v") for name in ("hold_snm_v", "read_snm_v", "write_snm_v") if name in needed])
            params["operations"] = list(dict.fromkeys(operations))
            cases = c["robustness"]["cases"] if robust else [{"corner": p["corner"], "temperature_c": p["temperature_c"], "vdd_v": p["vdd_v"]}]
            samples, coverage = [], []
            for i, case in enumerate(cases):
                par = {**params, **case, "variation": robust, "mc_runs": c["robustness"]["mc_runs"] if robust else 1,
                       "vth_std": c["robustness"]["vth_std"] if robust else p["vth_std"]}
                child = task / f"pvt_{i:02d}"
                result = self.run_array(child, {**c["plan"], "parameters": par, "constraints": [], "objective": None})
                if result["status"] != "completed":
                    raise ValueError(f"PVT案例{i}执行失败：{result.get('error', result['status'])}")
                samples.append(bank_metrics(child, par, arch, c["output_width"], area_model))
                coverage.append({**case, "mc_runs": par["mc_runs"], "task": str(child.relative_to(self.job))})
            metrics = aggregate_corners(samples)
            json_write(task / "coverage.json", {"cases": coverage, "variation": robust, "seed": p["seed"],
                "bank_model": "openyield_hybrid_v1", "array_metrics": "SPICE", "bank_metrics": "analytic_estimate",
                "area": "OpenYield estimate_scaled_array_area", "fom_units": "W,m2,s",
                "robust_fom_policy": "log10(min_snm/(Pmax*sqrt(Abank)*Dmax)); assistant formulation, not a quoted DAC equation",
                "limitations": "有限样本不证明良率；bank外围/面积为估算，不是完整bank SPICE或版图签核"})
            return metrics
        designs = [{"architecture": a, "devices": {k: p[k] for k in c["device_space"]}} for a in grid]
        good, pareto = self.search("architecture", designs, evaluate, c["architecture_objectives"], c["bank_constraints"],
            c["budgets"]["architecture"], c["solvers"]["architecture"], "array_spice_plus_bank_estimate")
        parents = (pareto + [r for r in good if r not in pareto])[:c["budgets"]["shortlist"]]
        self.state["shortlist"] = [r["candidate"] for r in parents]
        finalists = []
        for parent in parents:
            arch = parent["parameters"]["architecture"]
            pool = [{"architecture": arch, "devices": v} for v in self.device_pool(parent["parameters"]["devices"], c["device_space"], c["budgets"]["device_per_parent"])]
            name = "device_" + parent["candidate"].replace("/", "_")
            refined, _ = self.search(name, pool, lambda d, t: evaluate(d, t, True), c["device_objectives"], c["bank_constraints"],
                c["budgets"]["device_per_parent"], c["solvers"]["device"], "pvt_mc_array_spice_plus_bank_estimate", parent["candidate"])
            if refined:
                finalists.append(refined[0])
        ranked, _ = frontier(finalists, c["device_objectives"])
        self.state["best_design"] = ranked[0] if ranked else None
        self.state["status"] = "completed" if ranked else "infeasible"

    def device_pool(self, baseline, space, budget):
        result = [copy.deepcopy(baseline)]
        for name, values in space.items():
            for value in values:
                candidate = {**baseline, name: value}
                if candidate not in result:
                    result.append(candidate)
        for _ in range(max(64, budget*8)):
            candidate = {name: self.rng.choice(values) for name, values in space.items()}
            if candidate not in result:
                result.append(candidate)
        return result

    def multiplier(self):
        c, refs = self.config, self.job / "references"
        p = c["plan"]["parameters"]
        pea = PEA(refs / "gnn", p["bit_width"], refs / "nbit_approx.py", self.job / "artifacts/truth", self.command)
        json_write(self.job / "model_provenance.json", pea.provenance)
        types = c["architecture_space"]["compressor_types"]
        n = compressor_count(p["bit_width"], "native")
        base = p.get("compressor_ids", [p["compressor_id"]] * n)
        fixed = c["architecture_space"].get("fixed_assignment")
        if fixed:
            base = fixed
        if any(v not in types for v in base):
            raise ValueError("基线压缩器不在架构域中")
        domains = {str(i): [fixed[i]] if fixed else types for i in range(n)}
        pool = [{"compressor_ids": ids} for ids in self.device_pool(dict(zip(map(str, range(n)), base)), domains, c["budgets"]["architecture"])]
        # device_pool returns dicts; retain position order, never dictionary iteration of serialized IDs.
        pool = [{"compressor_ids": [d["compressor_ids"][str(i)] for i in range(n)]} for d in pool]
        def prediction(d, task):
            self.state["counters"]["gnn_queries"] += 1
            return pea.predict(d["compressor_ids"])
        limits = [{**item, "metric": "predicted_" + item["metric"]} for item in c["plan"]["constraints"] if item["metric"] in ("mred", "nmed")]
        good, pareto = self.search("architecture", pool, prediction, c["architecture_objectives"], limits,
            c["budgets"]["architecture"], c["solvers"]["architecture"], "prediction", domains=domains,
            build=lambda state: {"compressor_ids": [state[str(i)] for i in range(n)]})
        choices = (pareto + [r for r in good if r not in pareto])[:c["budgets"]["validation"]]
        # Predicted feasible points still have no measured acceptance. Include low-error fallbacks for calibration.
        if not choices:
            choices = sorted(self.state["stages"]["architecture"]["records"], key=lambda r: r.get("metrics", {}).get("predicted_mred", math.inf))[:c["budgets"]["validation"]]
        validated = []
        self.state["stages"]["validation"] = {"records": validated, "status": "running", "budget": c["budgets"]["validation"], "evidence_kind": "RTL_synthesis_STA_activity"}
        for i, parent in enumerate(choices):
            self.remaining()
            plan = validate({**c["plan"], "parameters": {**p, **parent["parameters"]}})
            task = self.job / "stages/validation" / f"{i+1:04d}"
            result = self.run_array(task, {**plan, "constraints": [x for x in plan["constraints"] if x["metric"] in ("mred", "nmed", "error_rate")]})
            json_write(task / "inputs/acco_spec_full.json", plan)
            if result["status"] in ("completed", "infeasible"):
                try:
                    self.state["counters"]["sta_activity_evaluations"] += 1
                    result["metrics"].update(multiplier_ppa(task, c["physical_evaluation"], self.command))
                except Exception as exc:
                    result.update(status="failed", error=str(exc))
            if result["status"] in ("completed", "infeasible"):
                result["constraints"] = constraints(result["metrics"], plan["constraints"])
                flags = [x["passed"] for x in result["constraints"]]
                result["status"] = "unverified_constraints" if None in flags else "infeasible" if False in flags else "completed"
            record = {**result, "candidate": f"validation/{i+1:04d}", "parent": parent["candidate"], "parameters": plan["parameters"],
                      "evidence_kind": "RTL_synthesis_STA_activity", "task": str(task.relative_to(self.job))}
            record["prediction_reference"] = parent["metrics"]
            pairs = {"mred": "predicted_mred", "nmed": "predicted_nmed", "sta_delay_ns": "predicted_delay_ns",
                     "synthesized_area_um2": "predicted_area_um2", "sta_dynamic_power_uw": "predicted_power_uw", "pdp_fj": "predicted_pdp_fj"}
            comparison = {actual: {"predicted": parent["metrics"][pred], "measured": result["metrics"][actual],
                          "difference": result["metrics"][actual] - parent["metrics"][pred]}
                          for actual, pred in pairs.items() if pred in parent["metrics"] and actual in result.get("metrics", {})}
            json_write(task / "prediction_comparison.json", {"metrics": comparison,
                "scope": "当前实现/条件下的预测与实测差异；未证明训练集与本次综合/活动完全同条件，不能直接称作GNN测试误差，也不自动修改权重"})
            validated.append(record)
            json_write(task / "acco_validation.json", record)
            self.save()
        ranking_objective = c["plan"].get("objective")
        valid, _ = frontier(validated, [ranking_objective] if ranking_objective else [{"metric": "pdp_fj", "direction": "min"}])
        self.state["stages"]["validation"]["status"] = "completed" if valid else "infeasible"
        parents = valid[:c["budgets"]["shortlist"]]
        self.state["shortlist"] = [r["candidate"] for r in parents]
        names = {1: "AP1", 2: "kong2", 3: "antonio", 4: "momeni", 5: "ha", 6: "AKbar1", 7: "AKbar2", 8: "sabetz"}
        all_refined = bool(parents)
        self.state["best_device_designs"] = []
        for parent in parents:
            for identity in sorted(set(parent["parameters"]["compressor_ids"])):
                name = names[identity]
                _, initial, labels = width_layout(refs / "compressor_sizing", name)
                domain = {str(i): c["device_space"]["width_scale"] for i in range(len(initial))}
                pool = self.device_pool({k: 1. for k in domain}, domain, c["budgets"]["device_per_parent"])
                name_stage = "device_" + parent["candidate"].replace("/", "_") + "_" + name
                def width_evaluate(d, task, name=name, identity=identity):
                    self.state["counters"]["device_candidates"] += 1
                    return compressor_width(refs / "compressor_sizing", name, [d[str(i)] for i in range(len(d))],
                                            task, pea.truth[str(identity)], self.command)
                refined, _ = self.search(name_stage, pool,
                    width_evaluate,
                    c["device_objectives"], [], c["budgets"]["device_per_parent"], c["solvers"]["device"],
                    "template_spice_truth_and_area_estimate", parent["candidate"])
                all_refined = all_refined and bool(refined)
                if refined:
                    self.state["best_device_designs"].append(refined[0])
        self.state["best_design"] = parents[0] if parents else None
        self.state["status"] = "completed" if all_refined else "incomplete" if parents else "infeasible"
        self.state["implementation_limit"] = "器件级是已选近似单元的逐组宽度/真值复核，LEVEL_1；没有把定制单元重新表征成Liberty并回灌乘法器/PE。不能将单元PDP收益冒充整PE收益。"

    def run(self):
        try:
            self.sram() if self.config["workflow"] == "sram" else self.multiplier()
            self.state["stop_reason"] = "stages_finished"
        except BudgetStop:
            self.state.update(status="incomplete", stop_reason="time_budget")
        except Exception as exc:
            self.state.update(status="failed", error=str(exc), stop_reason="error")
        self.save()
        return self.state


def launch(api, raw, use_model=True):
    config = validate_acco(raw)
    if not use_model:
        config["solvers"] = {k: "random" if v == "llm" else v for k, v in config["solvers"].items()}
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    job = api.ROOT / "runs" / f"{stamp}_acco_{config['workflow']}_{uuid.uuid4().hex[:6]}"
    job.mkdir(parents=True)
    for folder in ("logs", "reports", "artifacts", "scratch/home", "scratch/tmp", "scratch/cache"):
        (job / folder).mkdir(parents=True)
    before = api.manifest(api.SOURCE)
    json_write(job / "source_before.json", before)
    json_write(job / "config.json", config)
    entries = copy_references(api, job, config)
    env = {**os.environ, "HOME": str(job / "scratch/home"), "TMPDIR": str(job / "scratch/tmp"), "XDG_CACHE_HOME": str(job / "scratch/cache"),
           "MPLCONFIGDIR": str(job / "scratch/cache"), "MPLBACKEND": "Agg", "CUDA_VISIBLE_DEVICES": "-1", "OMP_NUM_THREADS": "4",
           "OPENBLAS_NUM_THREADS": "4", "PYTHONDONTWRITEBYTECODE": "1", "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    print(f"ACCO任务：{job}\n两级预算：{config['budgets']}；求解器：{config['solvers']}", flush=True)
    args = [sys.executable, str(api.ROOT / "app/guard.py"), str(job), sys.executable, str(api.ROOT / "app/acco_runner.py"), str(job)]
    try:
        with (job / "logs/pipeline.log").open("w") as stream:
            process = subprocess.Popen(args, cwd=job, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                process.wait(timeout=config["budgets"]["max_seconds"] + 15)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                file = job / "result.json"
                state = json.loads(file.read_text()) if file.exists() else {"kind": "acco", "workflow": config["workflow"], "config": config, "stages": {}}
                state.update(status="incomplete", stop_reason="time_budget_or_interrupted")
                json_write(file, state)
        if not (job / "result.json").is_file():
            json_write(job / "result.json", {"kind": "acco", "workflow": config["workflow"], "config": config, "status": "failed", "stages": {},
                       "error": f"隔离管线退出码{process.returncode}，见logs/pipeline.log"})
    finally:
        delta = [item["source"] for item in entries if sha(item["source"]) != item["sha256"]]
        unchanged = before == api.manifest(api.SOURCE) and not delta
        json_write(job / "source_integrity.json", {"unchanged": unchanged, "checked_files": len(before), "reference_changes": delta,
            "protection": "Landlock pipeline + nested worker confinement", "openyield_scope": "reused source files in reference_manifest.json"})
        if not unchanged:
            raise RuntimeError("源完整性核验失败")
    render(job)
    # Store actual array/RTL tasks in the existing experience index. ACCO records
    # retain their own full capacity/model/PVT scopes in a model-independent export.
    from experience import automatic_learn
    automatic_learn(api.ROOT, job)
    state = json.loads((job / "result.json").read_text())
    (job / "observations.jsonl").write_text("".join(json.dumps({"schema": "openacm-acco-observation/v1",
        "workflow": config["workflow"], "experiment": config, "source_integrity": unchanged,
        "record": row}, ensure_ascii=False, allow_nan=False) + "\n"
        for stage in state.get("stages", {}).values() for row in stage["records"]))
    from result_summary import summarize
    summarize(job, api.OLLAMA, api.MODEL, use_model=use_model)
    return job


def command(api, request="", spec_path=None, workflow=None, budget=64, max_seconds=7200, use_model=True, previous=None, plan_only=False):
    from acco_spec import from_request
    if spec_path:
        config = validate_acco(json.loads(Path(spec_path).read_text()))
    else:
        from conversation import workflow as detect
        wf = workflow or detect(request) or (previous or {}).get("workflow")
        if wf not in ("sram", "multiplier"):
            from acco_spec import NeedsExperiment
            raise NeedsExperiment("两级ACCO研究入口请选择SRAM或8/16位近似乘法器；DCiM后端用于入选系统验证")
        if use_model:
            plan, _ = api.plan_request(request, previous=previous)
            if plan["intent"] != "run":
                from acco_spec import NeedsExperiment
                raise NeedsExperiment(plan["message"])
        else:
            from bindings import explicit_parameters, explicit_constraints
            p = {**((previous or {}).get("parameters", {}) if (previous or {}).get("workflow") == wf else {}), **explicit_parameters(request, wf)}
            if wf == "multiplier":
                p.setdefault("kind", "approx")
            plan = validate({"workflow": wf, "parameters": p, "constraints": explicit_constraints(request, wf)})
        config = from_request(request, plan, budget, max_seconds, "llm" if use_model else "random")
    if plan_only:
        return config
    return launch(api, config, use_model)


def render(job):
    result = json.loads((job / "result.json").read_text())
    stages = result.get("stages", {})
    lines = ["# ACCO两级探索", "", f"状态：{result['status']}；停止原因：{result.get('stop_reason', 'error')}", "",
             "| 阶段 | 求解器 | 记录数 | 评价来源 | 最佳候选 |", "|---|---|---:|---|---|"]
    for name, stage in stages.items():
        lines.append(f"| {name} | {stage.get('solver', 'EDA复核')} | {len(stage['records'])} | {stage['evidence_kind']} | {stage.get('best_candidate', '—')} |")
    lines += ["", "计数：" + json.dumps(result.get("counters", {}), ensure_ascii=False), "",
              "入选父项：" + json.dumps(result.get("shortlist", []), ensure_ascii=False), "",
              "预测可行不等于实测达标；bank外围及面积为显式估算。器件阶段固定入选架构，并保留同条件baseline。"]
    for name, stage in stages.items():
        comparison = stage.get("baseline_comparison")
        if comparison:
            lines += ["", f"阶段 {name} 的同条件比较（{stage['evidence_kind']}）：", "",
                      "| 指标 | baseline | 最佳 | 差值（最佳−baseline） |", "|---|---:|---:|---:|"]
            lines += [f"| {metric} | {values['baseline']:.8g} | {values['best']:.8g} | {values['difference']:.8g} |" for metric, values in comparison.items()]
    for device in result.get("best_device_designs", []):
        lines += ["", f"器件已评价最佳：{device['candidate']}；父架构：{device['parent']}。", "",
                  "逐组宽度倍率：`" + json.dumps(device["parameters"], ensure_ascii=False) + "`。实际宽度见该候选widths.json。",
                  "指标：`" + json.dumps(device["metrics"], ensure_ascii=False) + "`。单元收益不能等同完整乘法器收益。"]
    if result.get("best_design"):
        best = result["best_design"]
        lines += ["", f"最佳已评价项：{best['candidate']}（{best['evidence_kind']}）", "", "```json", json.dumps(best, ensure_ascii=False, indent=2), "```"]
        json_write(job / "best_design.json", best)
        config = copy.deepcopy(result["config"])
        if result["workflow"] == "sram":
            design = best["parameters"]
            arch = design["architecture"]
            config["architecture_space"] = {"rows": [arch["rows"]], "cols": [arch["cols"]], "mux_ratio": [arch["mux_ratio"]]}
            config["plan"]["parameters"].update(design["devices"])
            config["device_space"] = {k: [v] for k, v in design["devices"].items()}
            config["budgets"].update(architecture=1, validation=1, shortlist=1, device_per_parent=1)
            json_write(job / "best_experiment.json", validate_acco(config))
        else:
            plan = validate({**config["plan"], "parameters": best["parameters"]})
            json_write(job / "best_plan.json", plan)
            config["plan"] = plan
            config["architecture_space"]["fixed_assignment"] = plan["parameters"]["compressor_ids"]
            config["budgets"].update(architecture=1, validation=1, shortlist=1, device_per_parent=1)
            json_write(job / "best_experiment.json", validate_acco(config))
    if result.get("error"):
        lines += ["", "错误：" + result["error"]]
    lines += ["", result.get("implementation_limit", "SRAM鲁棒性只覆盖coverage.json列出的PVT与MC样本，不能推断稀有失效率或流片签核。"),
              "", "来源与单位、输入、阶段父子关系、求解器调用、逐角样本和日志均在本目录内。"]
    text = "\n".join(lines) + "\n"
    (job / "reports/report.md").write_text(text)
    (job / "reports/summary.md").write_text(text)
    print(f"ACCO结果：{result['status']}；阶段{len(stages)}个；{result.get('counters', {})}")
    if result.get("best_design"):
        print("最佳已评价指标：" + json.dumps(result["best_design"]["metrics"], ensure_ascii=False))
    if result.get("error"):
        print("诊断：" + result["error"])
    print("报告：" + str(job / "reports/report.md"))
