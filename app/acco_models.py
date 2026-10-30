"""Explicit measurement/estimate boundaries and adapters to copied project code."""
import ast
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sys


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def architecture_grid(reference, config):
    """Reuse the original capacity/array-count constructor, without its CLI imports."""
    tree = ast.parse(Path(reference).read_text())
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ArchitectureConfigurationSpace")
    namespace = {"TOTAL_BITS": config["capacity_bits"], "ROW_CHOICES": config["architecture_space"]["rows"],
                 "COLUMN_CHOICES": config["architecture_space"]["cols"], "List": list, "Dict": dict}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(reference), "exec"), namespace)
    rows = namespace["ArchitectureConfigurationSpace"]().valid_configs
    return [{"rows": c["rows"], "cols": c["cols"], "num_arrays": c["num_arrays"], "mux_ratio": mu}
            for c in rows for mu in config["architecture_space"]["mux_ratio"]
            if c["array_capacity"] <= 65536 and c["cols"] % mu == 0]


def bank_area_estimate(params, arch, area_model):
    rows, cols, arrays = arch["rows"], arch["cols"], arch["num_arrays"]
    return area_model.estimate_scaled_array_area(rows, cols, arrays,
        w_access=params["pg_width_nm"] * 1e-9, w_pd=params["pd_width_nm"] * 1e-9,
        w_pu=params["pu_width_nm"] * 1e-9, l_transistor=params["length_nm"] * 1e-9,
        sa_max_width=.54e-6 * params["senseamp_scale"],
        wld_max_width=.27e-6 * params["wordline_scale"], prc_max_width=.27e-6 * params["precharge_scale"]) * 1e12


def bank_metrics(task, params, arch, output_width, area_model):
    """OpenYield bank model: array SPICE plus analytic periphery, never bank signoff."""
    raw = json.loads((Path(task) / "metrics.json").read_text())
    rows, cols, arrays = arch["rows"], arch["cols"], arch["num_arrays"]
    area = bank_area_estimate(params, arch, area_model)
    mux_levels = math.ceil(math.log2(arrays)) if arrays > 1 else 0
    # Ratio-2 column mux is already in the array SPICE; do not count it twice.
    serialization = math.ceil(output_width / (cols // arch["mux_ratio"]))
    result = {"bank_area_um2_estimate": area, "serialization_cycles_estimate": serialization}
    for op in ("read", "write"):
        sample = json.loads((Path(task) / "artifacts" / op / "samples.json").read_text())
        powers = sample["consumed_average_power_w"]
        leakage = [-v for v in sample["source_signed_static_power_w"]]
        if len(leakage) != len(powers) or any(not math.isfinite(x) or x < 0 for x in leakage):
            raise ValueError("静态功耗符号/样本无效，不能估算bank功耗")
        bank_power = max(p + (arrays - 1) * s for p, s in zip(powers, leakage)) * 1e6
        if op == "read":
            bank_power += (arrays - 1) * mux_levels * .1
        delay = raw[f"{op}_delay_ns"] + .041672135 * mux_levels + (.18 * mux_levels if op == "read" else 0)
        result[f"bank_{op}_power_uw_estimate"] = bank_power
        result[f"bank_{op}_delay_ns_estimate"] = delay * serialization
    result["bank_pmax_uw_estimate"] = max(result[f"bank_{o}_power_uw_estimate"] for o in ("read", "write"))
    result["bank_dmax_ns_estimate"] = max(result[f"bank_{o}_delay_ns_estimate"] for o in ("read", "write"))
    for key in ("hold_snm_v", "read_snm_v", "write_snm_v"):
        if key in raw:
            result[key] = raw[key]
    # Formula uses W, m², s, so the logarithm has a reproducible unit convention.
    denominator = result["bank_pmax_uw_estimate"] * 1e-6 * math.sqrt(area * 1e-12) * result["bank_dmax_ns_estimate"] * 1e-9
    if not math.isfinite(denominator) or denominator <= 0:
        raise ValueError("bank FOM需要有限正功率、面积和延迟")
    result["fom_arch_estimate"] = -math.log10(denominator)
    if all(k in result for k in ("hold_snm_v", "read_snm_v", "write_snm_v")):
        result["min_snm_v"] = min(result[k] for k in ("hold_snm_v", "read_snm_v", "write_snm_v"))
        result["fom_robust_estimate"] = math.log10(result["min_snm_v"] / denominator)
    return result


def aggregate_corners(cases):
    if not cases:
        raise ValueError("没有完成的PVT案例")
    keys = set.intersection(*(set(c) for c in cases))
    result = {k: (min(c[k] for c in cases) if "snm" in k or k.startswith("fom_") else max(c[k] for c in cases)) for k in keys}
    # Conservative extrema may occur in different corners; compute rather than reuse a nominal FOM.
    d = result["bank_pmax_uw_estimate"] * 1e-6 * math.sqrt(result["bank_area_um2_estimate"] * 1e-12) * result["bank_dmax_ns_estimate"] * 1e-9
    result["fom_arch_estimate"] = -math.log10(d)
    if "min_snm_v" in result:
        result["fom_robust_estimate"] = math.log10(result["min_snm_v"] / d)
    return result


def rtl_truth(compiler, artifact, command):
    artifact.mkdir(parents=True, exist_ok=True)
    tree = ast.parse(Path(compiler).read_text())
    mapping = next(ast.literal_eval(n.value) for n in ast.walk(tree) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Attribute) and t.attr == "compressor_mapping" for t in n.targets))
    def source(name):
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        return next(n.value.value for n in ast.walk(fn) if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant))
    text = source("_generate_compressor_module") + source("_generate_basic_adder_modules")
    text += "\nmodule tb; reg a,b,c,d; integer i;\n" + "\n".join(f"wire s{i},c{i}; {name} u{i}(a,b,c,d,s{i},c{i});" for i, name in sorted(mapping.items()))
    text += '\ninitial begin for(i=0;i<16;i=i+1) begin {a,b,c,d}=i; #1; $display("' + " ".join(["%b%b"] * 8) + '",' + ",".join(f"s{i},c{i}" for i in sorted(mapping)) + "); end $finish; end endmodule\n"
    design = artifact / "truth.v"
    design.write_text(text)
    command(["iverilog", "-g2012", "-s", "tb", "-o", str(artifact / "truth.vvp"), str(design)], artifact, artifact / "compile.log")
    command(["vvp", str(artifact / "truth.vvp")], artifact, artifact / "simulation.log")
    rows = [s.split() for s in (artifact / "simulation.log").read_text().splitlines() if len(s.split()) == 8 and all(re.fullmatch("[01]{2}", v) for v in s.split())]
    if len(rows) != 16:
        raise ValueError("压缩器真值表未完成")
    return {str(i): [row[i-1] for row in rows] for i in mapping}


class PEA:
    def __init__(self, folder, bits, compiler, artifact, command):
        import torch
        self.torch = torch
        self.folder, self.bits = Path(folder), bits
        self.truth = rtl_truth(compiler, artifact, command)
        tree = ast.parse((self.folder / "my_io.py").read_text())
        value = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ap_value" for t in n.targets) and isinstance(n.value, ast.Dict))
        def literal_tensor(n):
            while isinstance(n, ast.Call):
                if n.args and isinstance(n.args[0], ast.List):
                    return ast.literal_eval(n.args[0])
                n = n.func.value
            raise ValueError("GNN特征无法核对")
        self.mapping = {}
        for key, item in zip(value.keys, value.values):
            features = literal_tensor(item)
            expected = [f"{features[i]}{features[32+i]}" for i in range(16)]
            matches = [int(i) for i, truth in self.truth.items() if truth == expected]
            if len(matches) != 1:
                raise ValueError("GNN压缩器特征与RTL不能唯一匹配")
            self.mapping[matches[0]] = int(ast.literal_eval(key))
        if set(self.mapping) != set(range(1, 9)):
            raise ValueError("GNN映射不完整")
        sys.path.insert(0, str(self.folder))
        self.predictor = load_module("gnn_predictor", self.folder / "gnn_predictor.py")
        self.io = load_module("my_io", self.folder / "my_io.py")
        self.io.device = torch.device("cpu")  # Fixes the original 8-bit graph helper's CUDA-only global.
        self.scale = [[float(v) for v in row.split()] for row in (self.folder / f"label_minmax_{bits}.txt").read_text().splitlines()]
        if len(self.scale) != 2 or any(len(r) != 5 for r in self.scale):
            raise ValueError("GNN反归一化文件无效")
        self.provenance = {"evidence_kind": "prediction", "weights_sha256": sha(self.folder / f"best_model_weights_{bits}.pth"),
            "scaling_sha256": sha(self.folder / f"label_minmax_{bits}.txt"), "graph_helper_sha256": sha(self.folder / "my_io.py"),
            "compressor_mapping": self.mapping, "units": {"delay": "ns", "area": "um2", "power_file": "W", "power_output": "uW"},
            "scope": "原8/16位GNN，名义标准单元；预测不是验收，尺寸定制后必须重新表征"}

    def predict(self, ids):
        graph = self.io.state_to_graph([self.mapping[v] for v in ids], self.torch.device("cpu"))
        norm = self.predictor.predict_metrics(graph)
        values = [float(v) * (hi-lo) + lo for v, lo, hi in zip(norm, self.scale[0], self.scale[1])]
        if len(values) != 5 or any(not math.isfinite(v) or v < 0 for v in values):
            raise ValueError("GNN输出非有限或物理指标无效")
        mred, nmed, delay, area, power_w = values
        return {"predicted_mred": mred, "predicted_nmed": nmed, "predicted_delay_ns": delay,
                "predicted_area_um2": area, "predicted_power_uw": power_w * 1e6,
                "predicted_pdp_fj": delay * power_w * 1e6}
