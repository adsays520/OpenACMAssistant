"""Pinned public circuit data, kept separate from locally measured experience."""
from concurrent.futures import ThreadPoolExecutor
import ast
import csv
from datetime import datetime, timezone
import hashlib
import json
import itertools
import math
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from zoneinfo import ZoneInfo

REPOSITORY = "xuzhuo0417/openacm-gnn-8bit-dataset"
REVISION = "72a0ad0ea83d5b724999c6c1a66d8e57fda7c1d1"
NAME = "openacm-gnn-8bit"
FILES = (
    ".gitattributes", "README.md", "code/data_pyg.py", "code/merge.py",
    "code/minmax.py", "code/node_feature.py", "data/configs_ppa.csv",
    "metadata/FEATURE.csv", "metadata/dataset_summary.json",
    "metadata/graph_edges.csv", "model/FEATURE.csv", "model/best_model_weights_8.pth",
    "model/gnn_predictor.py", "model/label_minmax_8.txt", "model/my_io.py",
    "raw/Graph.csv", "raw/dir_vectors.txt", "raw/label.txt",
    "raw/label_minmax.txt", "raw/merged.log",
)
TARGETS = ("mred", "nmed", "delay", "area", "power")
POLICY = ("公开数据仅为外部参考：测量工具、工艺/PVT、单位及测试集合未完整确认。"
          "标签不能作为当前任务的实测、约束验收或最优证明；配置须重新仿真。"
          "不覆盖用户指定参数，不把area直接当synthesized_area_um2；不更新LLM权重。")


def directory(root):
    return Path(root) / "datasets" / NAME


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".partial_", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def fetch(root):
    """Download data and provenance; never import downloaded Python or weights."""
    folder = directory(root)
    folder.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(f"https://huggingface.co/api/datasets/{REPOSITORY}/revision/{REVISION}", timeout=60) as response:
        metadata = json.load(response)
    if metadata["sha"] != REVISION or {r["rfilename"] for r in metadata["siblings"]} != set(FILES):
        raise ValueError("公开数据快照与预期版本/文件清单不一致")
    write_json(folder / "repository.json", metadata)
    previous = json.loads((folder / "download_manifest.json").read_text()) if (folder / "download_manifest.json").exists() else {}
    known = {r["path"]: r for r in previous.get("files", [])} if previous.get("revision") == REVISION else {}

    def download(name):
        target = folder / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and name in known and digest(target) == known[name]["sha256"]:
            return known[name]
        url = f"https://huggingface.co/datasets/{REPOSITORY}/resolve/{REVISION}/{name}"
        fd, temporary = tempfile.mkstemp(prefix=".partial_", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream, urllib.request.urlopen(url, timeout=90) as response:
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    stream.write(block)
            os.replace(temporary, target)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()
        print(f"已拉取 {name} ({target.stat().st_size} bytes)", flush=True)
        return {"path": name, "bytes": target.stat().st_size, "sha256": digest(target), "url": url}

    with ThreadPoolExecutor(max_workers=4) as pool:
        files = list(pool.map(download, FILES))
    result = {"repository": REPOSITORY, "revision": REVISION,
              "downloaded_at": datetime.now(timezone.utc).isoformat(), "license": metadata.get("cardData", {}).get("license"),
              "files": files, "total_bytes": sum(f["bytes"] for f in files)}
    write_json(folder / "download_manifest.json", result)
    return result


def compare(root, source):
    """Check actual compressor truth tables without executing downloaded code."""
    folder = directory(root)
    downloaded = folder / "source"
    compiler = Path(source) / "openacm/multiplier_compiler/nbit_approx.py"
    tree = ast.parse(compiler.read_text())
    mapping = next(ast.literal_eval(n.value) for n in ast.walk(tree)
                   if isinstance(n, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "compressor_mapping" for t in n.targets))
    def module_text(name):
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        return next(n.value.value for n in ast.walk(function) if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant))
    public_tree = ast.parse((downloaded / "code/node_feature.py").read_text())
    values = next(n.value for n in ast.walk(public_tree) if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "ap_value" for t in n.targets) and isinstance(n.value, ast.Dict))
    def tensor_values(value):
        while isinstance(value, ast.Call):
            if value.args and isinstance(value.args[0], ast.List):
                return ast.literal_eval(value.args[0])
            value = value.func.value
        raise ValueError("未知公开压缩器特征格式")
    features = {int(ast.literal_eval(k)): tensor_values(v) for k, v in zip(values.keys, values.values)}
    artifact = folder / "audits/compressor_truth"
    artifact.mkdir(parents=True, exist_ok=True)
    ports = "\n".join(f"wire s{i},c{i}; {name} u{i}(a,b,c,d,s{i},c{i});" for i, name in sorted(mapping.items()))
    displays = ",".join(f"s{i},c{i}" for i in sorted(mapping))
    testbench = ("module tb; reg a,b,c,d; integer i;\n" + ports + "\ninitial begin\n"
                 + 'for(i=0;i<16;i=i+1) begin {a,b,c,d}=i; #1; $display("'
                 + " ".join(["%b%b"] * 8) + '",' + displays + "); end\n$finish; end endmodule\n")
    (artifact / "compressors.v").write_text(module_text("_generate_compressor_module") + module_text("_generate_basic_adder_modules") + testbench)
    guard = Path(root) / "app/guard.py"
    def run(arguments):
        command = [sys.executable, str(guard), str(artifact), *arguments]
        environment = {**os.environ, "TMPDIR": str(artifact), "TMP": str(artifact), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(command, cwd=artifact, env=environment, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError("压缩器真值表核验失败：" + result.stderr[-1500:])
        return result.stdout
    iverilog, vvp = shutil.which("iverilog"), shutil.which("vvp")
    if not iverilog or not vvp:
        raise ValueError("压缩器真值核验需要PATH中的iverilog和vvp")
    run([iverilog, "-g2012", "-s", "tb", "-o", str(artifact / "truth.vvp"), str(artifact / "compressors.v")])
    output = run([vvp, str(artifact / "truth.vvp")])
    (artifact / "truth.txt").write_text(output)
    rows = [row.split() for row in output.splitlines() if len(row.split()) == 8]
    if len(rows) != 16:
        raise ValueError("压缩器真值表输出长度不符")
    verified = {}
    for state, feature in features.items():
        expected = [f"{feature[i]}{feature[32+i]}" for i in range(16)]
        matches = [identity for identity in mapping if [row[identity-1] for row in rows] == expected]
        if len(matches) != 1:
            raise ValueError(f"公开压缩器类型{state}不能唯一匹配本地RTL：{matches}")
        identity = matches[0]
        verified[str(state)] = {"assistant_id": identity, "name": mapping[identity], "truth_vectors": 16}
    if set(verified) != set(map(str, range(8))):
        raise ValueError("公开压缩器特征缺少类型")
    local = Path(source) / "openacm/multiplier_gnn/8-bitGNN"
    checks = {}
    for public, original in [("raw/Graph.csv", "Graph.csv"), ("metadata/FEATURE.csv", "FEATURE.csv"),
                             ("model/gnn_predictor.py", "gnn_predictor.py"), ("model/my_io.py", "my_io.py"),
                             ("model/label_minmax_8.txt", "label_minmax_8.txt"), ("model/best_model_weights_8.pth", "best_model_weights_8.pth")]:
        checks[original] = {"local_sha256": digest(local / original), "public_sha256": digest(downloaded / public),
                            "equal": (local / original).read_bytes().replace(b"\r\n", b"\n") == (downloaded / public).read_bytes().replace(b"\r\n", b"\n")}
    result = {"mapping": verified, "compiler_sha256": digest(compiler), "local_gnn_comparison": checks,
              "position_policy": "保留数据集九个位置顺序，作为本地生成器候选；仍须用整电路RTL复测确认标签可比性。",
              "usage_policy": POLICY}
    write_json(folder / "audits/compatibility.json", result)
    return result


def prepare(root, source):
    """Validate all original rows and create an atomic, model-independent index."""
    folder = directory(root)
    downloaded = folder / "source"
    manifest = json.loads((folder / "download_manifest.json").read_text())
    if manifest["repository"] != REPOSITORY or manifest["revision"] != REVISION or {f["path"] for f in manifest["files"]} != set(FILES):
        raise ValueError("数据集清单不匹配")
    for item in manifest["files"]:
        if digest(downloaded / item["path"]) != item["sha256"]:
            raise ValueError(f"下载数据校验失败：{item['path']}")
    compatibility = compare(root, source)
    summary = json.loads((downloaded / "metadata/dataset_summary.json").read_text())
    scaling = [[float(x) for x in line.split()] for line in (downloaded / "raw/label_minmax.txt").read_text().splitlines()]
    mapping = {int(k): v["assistant_id"] for k, v in compatibility["mapping"].items()}
    derived = folder / "derived"
    derived.mkdir(exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".index_", dir=derived)
    os.close(fd)
    export_path = derived / ".observations.jsonl.partial"
    stats = {m: {"min": math.inf, "max": -math.inf, "distinct": set()} for m in TARGETS}
    seen = set()
    count = 0
    rounding = {m: 0.0 for m in TARGETS}
    try:
        with sqlite3.connect(temporary) as db, export_path.open("w") as export, \
                (downloaded / "data/configs_ppa.csv").open() as main, \
                (downloaded / "raw/dir_vectors.txt").open() as vectors, \
                (downloaded / "raw/merged.log").open() as raw, \
                (downloaded / "raw/label.txt").open() as normalized:
            db.execute("CREATE TABLE samples(sample_id INTEGER PRIMARY KEY,config TEXT UNIQUE,raw_config TEXT,mred REAL,nmed REAL,delay REAL,area REAL,power REAL)")
            db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)")
            for row, vector, labels, norm in itertools.zip_longest(csv.DictReader(main), vectors, raw, normalized):
                if any(x is None for x in [row, vector, labels, norm]):
                    raise ValueError("主表、配置与标签的行数不一致")
                public = [int(row[f"config_{i}"]) for i in range(9)]
                if public != [int(x) for x in vector.replace(",", " ").split()] or any(x not in mapping for x in public):
                    raise ValueError("配置向量不匹配或非法")
                sample = int(row["sample_id"])
                if sample != count:
                    raise ValueError("样本编号不是连续原始行号")
                values = [float(row[m]) for m in TARGETS]
                expected = [float(x) for x in labels.split()]
                norms = [float(row[m + "_normalized"]) for m in TARGETS]
                if len(expected) != 5 or len(norms) != 5 or norms != [float(x) for x in norm.split()] or any(abs(a-b) > 5.1e-9 for a,b in zip(values,expected)):
                    raise ValueError("主表标签与原始文件不一致")
                for metric,a,b in zip(TARGETS,values,expected):
                    rounding[metric] = max(rounding[metric],abs(a-b))
                values = expected  # Preserve raw power precision beyond the rounded CSV.
                if any(not math.isfinite(x) or x < 0 for x in values + norms):
                    raise ValueError("标签含非有限数或负数")
                for i, value in enumerate(values):
                    calculated = (value - scaling[0][i]) / (scaling[1][i] - scaling[0][i] + 1e-12)
                    if not math.isclose(calculated, norms[i], abs_tol=2e-6):
                        raise ValueError("标签归一化不匹配")
                ids = [mapping[x] for x in public]
                encoded = json.dumps(ids, separators=(",", ":"))
                if encoded in seen:
                    raise ValueError("重复配置不能跨集合训练")
                seen.add(encoded)
                db.execute("INSERT INTO samples VALUES(?,?,?,?,?,?,?,?)", (sample, encoded, json.dumps(public), *values))
                for metric, value in zip(TARGETS, values):
                    stats[metric]["min"] = min(stats[metric]["min"], value)
                    stats[metric]["max"] = max(stats[metric]["max"], value)
                    stats[metric]["distinct"].add(value)
                split = "train" if sample < 46000 else "validation" if sample < 48000 else "test"
                export.write(json.dumps({"schema_version": 1, "dataset": REPOSITORY, "revision": REVISION,
                                         "sample_id": sample, "split": split, "workflow": "multiplier",
                                         "parameters": {"bit_width": 8, "architecture": "native", "kind": "approx", "compressor_ids": ids},
                                         "raw_config": public, "published_labels": dict(zip(TARGETS, values)),
                                         "normalized_labels": dict(zip(TARGETS, norms)), "measurement_conditions": None,
                                         "match": "external_reference_only", "locally_measured": False}, ensure_ascii=False) + "\n")
                count += 1
            if count != 50000 or count != summary["num_samples"]:
                raise ValueError("数据集大小不符")
            with (downloaded / "raw/Graph.csv").open(encoding="utf-8-sig") as graph_file:
                graph = [[int(x) for x in row] for row in csv.reader(graph_file)]
            if len(graph) != 102 or any(len(r) != 102 or any(x < 0 for x in r) for r in graph):
                raise ValueError("电路图结构非法")
            edge_count = sum(map(sum, graph))
            if edge_count != summary["graph_edges"]:
                raise ValueError("电路图边数不匹配")
            with (downloaded / "metadata/FEATURE.csv").open(encoding="utf-8-sig") as feature_file:
                static_features = [[float(x) for x in row] for row in csv.reader(feature_file)]
            if len(static_features) != 38 or any(len(r) != 32 or any(not math.isfinite(x) for x in r) for r in static_features):
                raise ValueError("静态节点特征维度或数值不符")
            report = {"repository": REPOSITORY, "revision": REVISION, "rows": count,
                      "distinct_configurations": len(seen), "license": manifest["license"],
                      "graph_nodes": 102, "graph_edges_with_multiplicity": edge_count,
                      "graph_unique_edges": sum(x != 0 for r in graph for x in r),
                      "labels": {m: {**s, "distinct": len(s["distinct"])} for m, s in stats.items()},
                      "split_policy": "重新按唯一配置行号划分46000/2000/2000，避免沿用矛盾的上游划分元信息",
                      "upstream_split_conflict": "README为46000/2000/2000，metadata为26000/2000/20000未用/2000",
                      "training_split": {"train": 46000, "validation": 2000, "test": 2000},
                      "csv_rounding_max_absolute_difference": rounding,
                      "label_authority": "raw/merged.log; CSV只容许八位小数舍入差异",
                      "reference_split": "train only; validation/test rows never fed to LLM or proposal generation",
                      "compatibility": compatibility, "usage_policy": POLICY}
            db.execute("CREATE INDEX by_area ON samples(area)")
            db.execute("CREATE INDEX by_mred ON samples(mred)")
            db.execute("INSERT INTO metadata VALUES('report',?)", (json.dumps(report),))
        os.replace(temporary, derived / "samples.sqlite3")
        os.replace(export_path, derived / "observations.jsonl")
        write_json(folder / "audits/validation.json", report)
        return report
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
        if export_path.exists():
            export_path.unlink()


def query(root, plan, limit=6, objectives=None, allow_assignments=False):
    """Retrieve bounded TRAIN reference rows. Holdout labels never enter prompts."""
    context = {"dataset": REPOSITORY, "revision": REVISION, "usage_policy": POLICY,
               "reference_split": "train", "observations": []}
    p = plan.get("parameters", {})
    if (os.environ.get("OPENACM_PUBLIC_DATA", "1") == "0" or plan.get("workflow") != "multiplier"
            or p.get("bit_width", 8) != 8 or p.get("architecture", "auto") == "tiled"
            or p.get("kind", "approx") != "approx"):
        return context
    database = directory(root) / "derived/samples.sqlite3"
    if not database.exists():
        return context
    if not 1 <= limit <= 64:
        raise ValueError("参考检索条数需1–64")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        report = json.loads(db.execute("SELECT value FROM metadata WHERE key='report'").fetchone()[0])
        compiler = Path(os.environ.get("OPENACM_SOURCE", str(Path(root).parent / "OpenACM"))) / "openacm/multiplier_compiler/nbit_approx.py"
        if compiler.exists() and digest(compiler) != report["compatibility"]["compiler_sha256"]:
            context["retrieval_error"] = "原始生成器与已核验映射的版本不同，请重新dataset prepare"
            return context
        context["label_quality"] = {"delay_distinct_values": report["labels"]["delay"]["distinct"],
                                    "measurement_conditions_confirmed": False}
        clauses, args = ["sample_id < 46000"], []
        ids = p.get("compressor_ids") or ([p["compressor_id"]] * 9 if "compressor_id" in p else None)
        if ids is not None and not allow_assignments:
            clauses.append("config = ?")
            args.append(json.dumps(ids, separators=(",", ":")))
        # Only unitless error constraints may be used to retrieve reference examples.
        for c in plan.get("constraints", []):
            if c["metric"] in ("mred", "nmed") and c["op"] in ("le", "ge"):
                clauses.append(c["metric"] + (" <= ?" if c["op"] == "le" else " >= ?"))
                args.append(c["value"])
        primary = (objectives or [plan.get("objective") or {"metric": "mred", "direction": "min"}])[0]
        metric = {"synthesized_area_um2": "area", "mred": "mred", "nmed": "nmed"}.get(primary["metric"], "mred")
        order = "DESC" if primary["direction"] == "max" else "ASC"
        rows = db.execute(f"SELECT * FROM samples WHERE {' AND '.join(clauses)} ORDER BY {metric} {order},sample_id LIMIT ?", [*args, limit]).fetchall()
        for row in rows:
            context["observations"].append({"sample_id": row["sample_id"], "compressor_ids": json.loads(row["config"]),
                                            "published_labels": {m: row[m] for m in TARGETS},
                                            "match": "external_reference_only", "locally_measured": False})
    return context


def retrieve_public(root, plan, **kwargs):
    try:
        return query(root, plan, **kwargs)
    except Exception as exc:
        return {"dataset": REPOSITORY, "usage_policy": POLICY, "observations": [], "retrieval_error": str(exc)[:200]}


def augment_default_space(root, config):
    """Extend ONLY the default mutable vector domain, leaving fixed requests intact."""
    if "compressor_ids" not in config["space"]:
        return config
    reference_plan = {**config["plan"], "parameters": dict(config["plan"]["parameters"])}
    if reference_plan["parameters"]["kind"] != "approx":
        if "approx" not in config["space"].get("kind", []):
            return config
        reference_plan["parameters"]["kind"] = "approx"
    context = retrieve_public(root, reference_plan, limit=32, objectives=config["objectives"], allow_assignments=True)
    vectors = config["space"]["compressor_ids"]
    for row in context["observations"]:
        if row["compressor_ids"] not in vectors and len(vectors) < 256:
            vectors.append(row["compressor_ids"])
    return config


def preferred_public_options(context, config, records, limit=3):
    """Propose only already permitted domain values; never import public results."""
    from exploration import candidate_plan, fingerprint
    seen = {fingerprint(r["parameters"]) for r in records}
    options = []
    for row in context["observations"]:
        ids = row["compressor_ids"]
        if ids not in config["space"].get("compressor_ids", []):
            continue
        if config["plan"]["parameters"]["kind"] != "approx" and "approx" not in config["space"].get("kind", []):
            continue
        plan = candidate_plan(config, {"kind": "approx", "compressor_ids": ids})
        key = fingerprint(plan["parameters"])
        if key not in seen:
            seen.add(key)
            options.append(plan)
        if len(options) >= limit:
            break
    return options


def decision_context(context, options, limit=6):
    """Keep menu-matching rows ahead of global references within the prompt budget."""
    vectors = [p["parameters"].get("compressor_ids") for p in options]
    rows = context["observations"]
    matching = [r for r in rows if r["compressor_ids"] in vectors]
    return {**context, "observations": (matching + [r for r in rows if r not in matching])[:limit]}


def verify(api, use_model=True):
    """A real local-model choice followed by isolated RTL/synthesis comparisons."""
    from exploration import choose_model
    from spec import validate
    folder = directory(api.ROOT)
    before = api.manifest(api.SOURCE)
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    job = api.ROOT / "runs" / f"{stamp}_public_dataset_validation_{os.getpid()}"
    job.mkdir(parents=True)
    write_json(job / "source_before.json", before)
    plan = validate({"workflow": "multiplier", "parameters": {"bit_width": 8, "kind": "approx", "backend": "synthesis", "samples": 4096, "seed": 42},
                     "constraints": [{"metric": "mred", "op": "le", "value": 0.02}]})
    config = {"plan": plan, "objectives": [{"metric": "synthesized_area_um2", "direction": "min"}, {"metric": "mred", "direction": "min"}]}
    public = query(api.ROOT, plan, objectives=config["objectives"], allow_assignments=True)
    low_error = query(api.ROOT, plan, allow_assignments=True, limit=1)
    if len(public["observations"]) < 2 or not low_error["observations"]:
        raise ValueError("请先执行dataset fetch和prepare")
    write_json(job / "public_dataset_retrieval.json", public)
    records, comparisons = [], []
    def measured(row, number, proposer):
        candidate = validate({**plan, "parameters": {**plan["parameters"], "compressor_ids": row["compressor_ids"]}})
        task = job / "candidates" / f"{number:04d}"
        result = api.run_one(task, candidate, timeout_override=600)
        write_json(task / "plan.json", candidate)
        record = {"candidate": number, "parameters": candidate["parameters"], "proposed_by": proposer, "task": str(task.relative_to(job)), **result}
        records.append(record)
        comparisons.append({"sample_id": row["sample_id"], "published_labels": row["published_labels"],
                            "local_status": result["status"], "local_metrics": result.get("metrics", {}),
                            "mred_difference": result.get("metrics", {}).get("mred", row["published_labels"]["mred"]) - row["published_labels"]["mred"],
                            "conditions": candidate["parameters"], "task": str(task.relative_to(job))})
        write_json(job / "comparisons.json", comparisons)
        print(f"配置{row['sample_id']}复测：{result['status']}；指标={result.get('metrics',{})}", flush=True)
    try:
        measured(low_error["observations"][0], 1, "reference_low_error")
        options = [validate({**plan, "parameters": {**plan["parameters"], "compressor_ids": r["compressor_ids"]}}) for r in public["observations"]]
        audit = job / "decisions/0002"
        audit.mkdir(parents=True)
        write_json(audit / "menu.json", options)
        if use_model:
            print("DeepSeek正在读取公开参考与本地复测结果，选择下一配置…", flush=True)
            index, reason = choose_model(options, records, config, api.OLLAMA, api.MODEL, audit, 600, public=public)
        else:
            index, reason = 0, "确定性公开候选复测"
        write_json(audit / "selection.json", {"index": index, "reason": reason, "proposed_by": "deepseek" if use_model else "deterministic"})
        measured(public["observations"][index], 2, "deepseek" if use_model else "deterministic")
        other = next(r for i, r in enumerate(public["observations"]) if i != index)
        measured(other, 3, "reference_comparison")
        result = {"status": "completed" if all(r["status"] in ("completed", "infeasible") for r in records) else "failed",
                  "kind": "public_dataset_validation", "model_used": api.MODEL if use_model else None,
                  "dataset": REPOSITORY, "revision": REVISION, "comparisons": comparisons,
                  "decision": {"index": index, "reason": reason}, "usage_policy": POLICY}
        write_json(job / "result.json", result)
        lines = ["# 公开数据接入与本地DeepSeek验证", "", f"数据集：{REPOSITORY}；快照：{REVISION}", "",
                 f"模型：{result['model_used']}；模型选择：{index}，{reason}", "",
                 "| 原样本 | 发布MRED | 本地MRED | 发布area（单位未确认） | 本地综合面积µm² | 状态 |", "|---|---:|---:|---:|---:|---|"]
        for row in comparisons:
            metrics = row["local_metrics"]
            lines.append(f"| {row['sample_id']} | {row['published_labels']['mred']:.8g} | {metrics.get('mred','缺测')} | {row['published_labels']['area']} | {metrics.get('synthesized_area_um2','缺测')} | {row['local_status']} |")
        lines += ["", POLICY, "", "本地复测采用8位原生RTL、Nangate45 typical综合、4096组固定seed=42向量与门级对照。",
                  "request/reply/selection、工作副本和测量证据均在本目录。数据检索接入没有更新DeepSeek权重。"]
        (job / "report.md").write_text("\n".join(lines) + "\n")
        write_json(folder / "audits/local_model_validation.json", {"task": str(job), **result})
        return {"task": str(job), **result}
    except Exception as exc:
        write_json(job / "result.json", {"status": "failed", "kind": "public_dataset_validation",
                                         "comparisons": comparisons, "error": str(exc)})
        raise
    finally:
        write_json(job / "source_integrity.json", {"unchanged": api.manifest(api.SOURCE) == before})
