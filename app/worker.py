"""Deterministic adapters; run only through guard.py in a task workspace."""
import csv
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import traceback
from spec import compressor_count,uses_tiles


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def command(args, cwd, log, timeout=180):
    with log.open("w") as stream:
        stream.write(json.dumps(args) + "\n")
        stream.flush()
        result = subprocess.run(args, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"退出码 {result.returncode}，详见 {log}")


def multiplier(job, params, repo):
    sys.path.insert(0, str(repo / "openacm/multiplier_compiler"))
    from nbit_exact import generate_Exact
    from nbit_approx import generate_Appro_4_2, _required_compressor_count
    from nbit_log import generate_Log
    n, kind = params["bit_width"], params["kind"]
    rtl = job / "artifacts/rtl"
    rtl.mkdir(parents=True, exist_ok=True)
    tiled=uses_tiles(n,kind,params.get("architecture","auto"))
    if tiled:
        from width_adapter import generate_tiled
        ids=params.get("compressor_ids",[params["compressor_id"]]*compressor_count(n,"tiled"))
        if kind=="approx":
            (job/"inputs/compressors.cfg").write_text(" ".join(map(str,ids))+"\n")
        design=generate_tiled(n,kind,ids,rtl)
    elif kind == "exact":
        design = generate_Exact([n], output_dir=rtl)[0]
    elif kind == "log":
        design = generate_Log([n], output_dir=rtl)[0]
    else:
        count = _required_compressor_count(n)
        ids = params.get("compressor_ids", [params["compressor_id"]] * count)
        if len(ids) != count:
            raise ValueError(f"{n} bit 需要 {count} 个压缩器 ID，实际提供 {len(ids)} 个")
        (job / "inputs/compressors.cfg").write_text(" ".join(map(str, ids)) + "\n")
        design = generate_Appro_4_2([n], compressor_config={n: ids}, output_dir=rtl, prompt_for_missing=False)[0]
    top = design.stem
    maximum = (1 << n) - 1
    rng = random.Random(params["seed"])
    if params["exhaustive"]:
        pairs = [(a, b) for a in range(maximum + 1) for b in range(maximum + 1)]
    else:
        corners = [0, 1, 2, maximum // 2, maximum - 1, maximum]
        pairs = list(dict.fromkeys((a, b) for a in corners for b in corners))
        pairs = pairs[:params["samples"]]
        pairs += [(rng.randrange(maximum + 1), rng.randrange(maximum + 1)) for _ in range(params["samples"] - len(pairs))]
    # Drive each pair from a file: one observed RTL result for every reference value.
    vectors = job / "inputs/vectors.hex"
    vectors.write_text("\n".join(f"{(a << n) | b:x}" for a, b in pairs) + "\n")
    tb = job / "artifacts/testbench.v"
    tb.write_text(f'''`timescale 1ns/1ps
module tb;
reg [{n-1}:0] a,b;
wire [{2*n-1}:0] p;
reg [{2*n-1}:0] vectors [0:{len(pairs)-1}];
integer i, fd;
{top} dut(a,b,p);
initial begin
  $readmemh("{vectors}",vectors);
  fd=$fopen("{job / 'artifacts/products.txt'}","w");
  for(i=0;i<{len(pairs)};i=i+1) begin
    {{a,b}}=vectors[i]; #1; $fdisplay(fd,"%d",p);
  end
  $fclose(fd); $finish;
end
endmodule
''')
    command(["iverilog", "-g2012", "-s", "tb", "-o", str(job / "artifacts/sim.vvp"), str(design), str(tb)], repo, job / "logs/rtl_compile.log")
    command(["vvp", str(job / "artifacts/sim.vvp")], repo, job / "logs/rtl_simulation.log")
    products = (job / "artifacts/products.txt").read_text().splitlines()
    if len(products) != len(pairs):
        raise RuntimeError("仿真输出向量数量不符")
    try:
        actual = [int(x.strip()) for x in products]
    except ValueError as exc:
        raise RuntimeError("RTL 输出含 X/Z 或无效值，不能计算误差指标") from exc
    expected = [a * b for a, b in pairs]
    errors = [abs(a - b) for a, b in zip(actual, expected)]
    relative = [err / ref for err, ref in zip(errors, expected) if ref != 0]
    metrics = {"sample_count": len(pairs), "error_count": sum(e != 0 for e in errors),
               "error_rate": sum(e != 0 for e in errors) / len(pairs), "mae": sum(errors) / len(pairs),
               "max_absolute_error": max(errors), "mred": sum(relative) / len(relative),
               "nmed": sum(errors) / len(pairs) / (maximum * maximum)}
    metrics["architecture"]="tiled_8bit" if tiled else "native"
    with (job / "artifacts/comparison.csv").open("w") as stream:
        writer = csv.writer(stream)
        writer.writerow(["a", "b", "expected", "actual", "absolute_error"])
        writer.writerows((a, b, ref, out, err) for (a, b), ref, out, err in zip(pairs, expected, actual, errors))
    if kind == "exact" and metrics["error_count"]:
        raise RuntimeError(f"内置 Exact RTL 在独立参考检查中出现 {metrics['error_count']} 个错误；不将生成成功当作功能正确")
    if params.get("backend") == "synthesis" and "rows" not in params:
        from digital_synthesis import synthesize
        gate_output = job / "artifacts/synthesis_products.txt"
        metrics.update(synthesize(job, repo, [design], top, tb.read_text().replace(str(job / "artifacts/products.txt"), str(gate_output)), command))
        if gate_output.read_text().splitlines() != products:
            raise RuntimeError("综合后乘法器与原始RTL输出不一致")
        metrics["post_synth_functional_checks"] = len(pairs)
    return metrics


def dcim(job, params, repo):
    metrics = multiplier(job, params, repo)
    dcim_root = repo / "DCIM"
    (dcim_root / "results/synth").mkdir(parents=True, exist_ok=True)
    prefix = {"exact": "Exact", "approx": "Appro4_2", "log": "Log_our"}[params["kind"]]
    command([sys.executable, str(repo / "openacm/pe_compiler/SRAM.py"), "--data_width", str(params["bit_width"]), "--addr_count", str(params["rows"])], repo, job / "logs/sram_rtl_generate.log")
    command([sys.executable, str(repo / "openacm/pe_compiler/pe_compiler.py"), "--data_width", str(params["bit_width"]), "--addr_count", str(params["rows"]), "--multiplier_module", prefix], repo, job / "logs/pe_generate.log")
    # Exercise the complete PE datapath with constant nonzero input; SRAM is initialized
    # through its normal control interface. The expected product is obtained from the
    # separately tested multiplier, rather than assuming approximation is exact.
    n, rows = params["bit_width"], params["rows"]
    design = job / f"artifacts/rtl/{prefix}_{n}bit.v"
    tb = job / "artifacts/pe_testbench.v"
    tb.write_text(f'''`timescale 1ns/1ps
module tb;
reg clk=0, rst_n=1, pe_ce=0, init_enable=0;
reg [{n-1}:0] data_in=3;
wire [{2*n-1}:0] data_out, reference;
wire init_done,valid_out;
integer seen=0;
always #5 clk=~clk;
sram_multiplier_system dut(clk,rst_n,pe_ce,init_enable,data_in,data_out,init_done,valid_out);
{prefix}_{n}bit reference_mult(data_in,data_in,reference);
initial begin
  $dumpfile("{job / 'artifacts/pe_waveform.vcd'}"); $dumpvars(0,tb);
  #2; rst_n=0; #40; pe_ce=1; #20; rst_n=1; init_enable=1;
  repeat({rows+12}) @(negedge clk);
  repeat(16) begin
    @(negedge clk); #1;
    if(!init_done || !valid_out) $fatal(1,"PE has not entered RUN");
    if(data_out !== reference) $fatal(1,"PE product mismatch");
    seen=seen+1;
  end
  $display("PE_PASS checked=%d",seen); $finish;
end
endmodule
''')
    # The generator's separate synthesis model excludes specify paths, which
    # Icarus cannot parse in the timing model. Use its own functional output.
    command(["iverilog", "-g2012", "-s", "tb", "-o", str(job / "artifacts/pe.vvp"), str(dcim_root / "results/synth/sram_syn.v"), str(dcim_root / "rtl/sram_control.v"), str(design), str(tb)], repo, job / "logs/pe_compile.log")
    command(["vvp", str(job / "artifacts/pe.vvp")], repo, job / "logs/pe_simulation.log")
    if "PE_PASS" not in (job / "logs/pe_simulation.log").read_text():
        raise RuntimeError("缺少 PE 功能验证完成标记")
    metrics["pe_constant_input_checks"] = 16
    dump(job / "metrics.json", metrics)
    if params["backend"] == "asic":
        from backend import run_backend
        metrics.update(run_backend(job, params, repo, command))
    elif params["backend"] == "synthesis":
        from digital_synthesis import synthesize
        names = re.findall(r"\bmodule\s+(\w+)", design.read_text())
        expression = r"\b(" + "|".join(map(re.escape, names)) + r")\b"
        reference = job / "inputs/reference_multiplier.v"
        reference.write_text(re.sub(expression, lambda m: "reference_" + m[0], design.read_text()))
        gate_tb = tb.read_text().replace(f"{prefix}_{n}bit reference_mult", f"reference_{prefix}_{n}bit reference_mult")
        sram_model=dcim_root / "results/synth/sram_syn.v"
        use_macro=params["memory_impl"]!="standard_cells" and (rows,n) in [(16,8),(32,16),(64,32)]
        libraries=[dcim_root / f"nangate45/lib/SRAM_6T_CORE_{rows}x{n}_MC_TB.lib"] if use_macro else []
        sources=([sram_model] if not use_macro else [])+[dcim_root / "rtl/sram_control.v", design]
        models=[reference]+([sram_model] if use_macro else [])
        metrics.update(synthesize(job, repo, sources, "sram_multiplier_system", gate_tb, command, models, libraries))
        metrics["post_synth_functional_checks"] = 16
        metrics["memory_impl"] = "macro" if use_macro else "standard_cells"
    return metrics


def compressor(job, params, repo):
    path = repo / "openacm/compressor_sizing" / params["name"]
    files = list(path.glob("*.sp"))
    if len(files) != 1:
        raise RuntimeError("压缩器基础网表不唯一")
    original = files[0].read_text()
    text = original
    ratio = params["vdd_v"] / 1.2
    text = re.sub(r"(?im)^(V(?:DD|S)\s+\S+\s+\S+\s+DC\s+)1\.2\b", lambda m: m[1] + str(params["vdd_v"]), text)
    text = re.sub(r"([0-9.]+)V\b", lambda m: f"{float(m[1])*ratio:.9g}V", text)
    text = re.sub(r"(V\([^)]+\)=)([0-9.]+)", lambda m: m[1] + f"{float(m[2])*ratio:.9g}", text)
    text = re.sub(r"TEMP=[-0-9.]+", f"TEMP={params['temperature_c']}", text)
    gate_area = 0.0
    count = 0
    lines = []
    for line in text.splitlines():
        if line.lstrip().upper().startswith("M") and "W=" in line.upper():
            scale = params["pmos_scale"] if "PMOS" in line.upper() else params["nmos_scale"]
            w = re.search(r"W=([0-9.]+)u", line, re.I)
            l = re.search(r"L=([0-9.]+)u", line, re.I)
            if not w or not l:
                raise RuntimeError("晶体管 W/L 格式不支持")
            new_w = float(w[1]) * scale
            new_l = float(l[1]) * params["length_scale"]
            line = re.sub(r"W=[0-9.]+u", f"W={new_w:.6f}u", line, flags=re.I)
            line = re.sub(r"L=[0-9.]+u", f"L={new_l:.6f}u", line, flags=re.I)
            gate_area += new_w * new_l
            count += 1
        if line.lstrip().lower().startswith("cload"):
            line = re.sub(r"([0-9.]+)f\s*$", lambda m: f"{float(m[1])*params['load_scale']:.9g}f", line)
        lines.append(line)
    design = job / "artifacts" / files[0].name
    design.write_text("\n".join(lines) + "\n")
    command(["Xyce", str(design)], job / "artifacts", job / "logs/xyce.log")
    measures = Path(str(design) + ".mt0")
    if not measures.is_file():
        raise RuntimeError("未生成新的 Xyce 测量文件")
    raw = {}
    for line in measures.read_text().splitlines():
        match = re.match(r"\s*(\w+)\s*=\s*(\S+)", line)
        if match:
            try:
                raw[match[1].upper()] = float(match[2])
            except ValueError:
                raise RuntimeError(f"无效测量：{line}")
    dump(job / "artifacts/raw_measurements.json", raw)
    delays = [v for k, v in raw.items() if k.startswith(("TPLH_", "TPHL_"))]
    if not delays or "P_AVG" not in raw or any(not math.isfinite(v) or v <= 0 for v in delays) or raw["P_AVG"] < 0:
        raise RuntimeError("缺少有效的延迟或功耗测量；请检查原始测量和 xyce.log")
    return {"delay_ns": max(delays) * 1e9, "power_uw": raw["P_AVG"] * 1e6,
            "gate_area_um2": gate_area, "estimated_area_um2": gate_area * 7,
            "transistor_count": count, "model": "simplified_LEVEL_1"}


def sram(job, params, repo):
    import numpy as np
    import yaml
    base = repo / "openacm/sram_compiler"
    sys.path.insert(0, str(base))
    from config import SRAM_CONFIG
    from utils import estimate_bitcell_area, parse_spice_models
    from PySpice.Spice.Simulation import CircuitSimulator
    from PySpice.Unit import u_Ohm, u_pF
    from sram_compiler.testbenches.sram_6t_core_MC_testbench import Sram6TCoreMcTestbench
    CircuitSimulator.DEFAULT_SIMULATOR = "xyce-serial"
    cfg_dir = job / "inputs/sram_config"
    cfg_dir.mkdir(exist_ok=True)
    global_cfg = yaml.safe_load((base / "config/global.yaml").read_text())
    global_cfg.update(vdd=params["vdd_v"], temperature=params["temperature_c"], num_rows=params["rows"],
                      num_cols=params["cols"], monte_carlo_runs=params["mc_runs"], choose_columnmux=params["column_mux"], timeout=180)
    for corner in ["TT", "FF", "SS", "FS", "SF"]:
        global_cfg[f"pdk_path_{corner}"] = str(base / f"tran_models/models_{corner}.spice")
    (cfg_dir / "global.yaml").write_text(yaml.safe_dump(global_cfg, sort_keys=False))
    mapping = {"SRAM_6T_CELL": "sram_6t_cell", "WORDLINEDRIVER": "wordline_driver", "PRECHARGE": "precharge",
               "COLUMNMUX": "mux", "SENSEAMP": "sa", "WRITEDRIVER": "write_driver", "DECODER": "decoder"}
    files = {}
    for key, stem in mapping.items():
        data = yaml.safe_load((base / f"config/{stem}.yaml").read_text())
        if key == "SRAM_6T_CELL":
            fields = data[key]["parameters"]
            fields["pmos_width"]["value"] = params["pu_width_nm"] * 1e-9
            fields["nmos_width"]["value"] = [params["pd_width_nm"] * 1e-9, params["pg_width_nm"] * 1e-9]
            fields["length"]["value"] = params["length_nm"] * 1e-9
            fields["pmos_model"]["value"] = params["pu_model"]
            fields["nmos_model"]["value"] = [params["pd_model"], params["pg_model"]]
        else:
            scale_key = {"WORDLINEDRIVER": "wordline", "PRECHARGE": "precharge", "COLUMNMUX": "mux",
                         "SENSEAMP": "senseamp", "WRITEDRIVER": "write_driver", "DECODER": "decoder"}[key] + "_scale"
            for field in ["nmos_width", "pmos_width"]:
                if field in data[key]["parameters"]:
                    value = data[key]["parameters"][field]["value"]
                    data[key]["parameters"][field]["value"] = [x * params[scale_key] for x in value] if isinstance(value, list) else value * params[scale_key]
        target = cfg_dir / f"{stem}.yaml"
        target.write_text(yaml.safe_dump(data, sort_keys=False))
        files[key] = str(target)
    cfg = SRAM_CONFIG()
    cfg.load_all_configs(global_file=str(cfg_dir / "global.yaml"), circuit_configs=files)
    cell_area = estimate_bitcell_area(w_access=params["pg_width_nm"] * 1e-9,
                                     w_pd=params["pd_width_nm"] * 1e-9, w_pu=params["pu_width_nm"] * 1e-9,
                                     l_transistor=params["length_nm"] * 1e-9) * 1e12
    metrics = {"cell_area_um2": float(cell_area)}
    for operation in params["operations"]:
        sim_dir = job / "artifacts" / operation
        tb = Sram6TCoreMcTestbench(cfg, w_rc=params["wire_rc"], pi_res=params["wire_res_ohm"] @ u_Ohm,
                                  pi_cap=params["wire_cap_pf"] @ u_pF, use_mc=params["variation"],
                                  custom_mc=params["variation"], sweep_senseamp=False,
                                  coner=params["corner"], choose_columnmux=params["column_mux"],
                                  sim_path=str(sim_dir), gen_unused_cells=params["full_array"], vth_std=params["vth_std"])
        variables = None
        if params["variation"]:
            models = parse_spice_models(global_cfg[f"pdk_path_{params['corner']}"])
            names = [params["pg_model"], params["pg_model"], params["pd_model"], params["pu_model"], params["pd_model"], params["pu_model"]]
            means = [float(models[name]["parameters"][key]) for name in names for key in ["vth0", "u0", "voff"]]
            cells = 1 if "snm" in operation else params["rows"] * params["cols"]
            means = np.tile(means, cells)
            rng = np.random.default_rng(params["seed"])
            variables = rng.normal(means, np.abs(means) * params["vth_std"], (params["mc_runs"], len(means)))
        try:
            outputs = tb.run_mc_simulation(operation=operation, target_row=params["rows"] - 1,
                                           target_col=params["cols"] - 1, mc_runs=params["mc_runs"],
                                           temperature=params["temperature_c"], vars=variables)
        except KeyError as exc:
            failures = [line for f in sim_dir.glob("*.mt*") for line in f.read_text().splitlines() if "FAILED" in line.upper()]
            raise RuntimeError(f"{operation} 缺少有效测量 {exc}；原始失败项：{failures}。此参数点不能视为可用设计") from exc
        if "snm" in operation:
            arr = np.asarray(outputs, dtype=float)
            if arr.size != params["mc_runs"] or not np.isfinite(arr).all() or (arr <= 0).any():
                raise RuntimeError(f"{operation} 测量缺失或非正数")
            metrics[f"{operation}_v"] = float(arr.min())
        else:
            delay, average, static, dynamic = [np.asarray(x, dtype=float) for x in outputs]
            # Built-in PAVG/PSTC measure V(VDD)*I(VVDD), the source's signed
            # delivered power. Load consumption is its negative, not abs().
            consumed = -average
            if any(x.size != params["mc_runs"] or not np.isfinite(x).all() for x in [delay, average, static, dynamic]) or (delay <= 0).any() or (consumed < 0).any():
                raise RuntimeError(f"{operation} 测量缺失、NaN 或无效")
            metrics[f"{operation}_delay_ns"] = float(delay.max()) * 1e9
            metrics[f"{operation}_power_uw"] = float(consumed.max()) * 1e6
            dump(sim_dir / "samples.json", {"delay_s": delay.tolist(), "consumed_average_power_w": consumed.tolist(),
                                           "source_signed_average_power_w": average.tolist(),
                                           "source_signed_static_power_w": static.tolist(), "source_signed_dynamic_power_w": dynamic.tolist()})
        dump(job / "metrics.json", metrics)
    return metrics


def main():
    job = Path(sys.argv[1]).resolve()
    plan = json.loads((job / "inputs/spec.json").read_text())
    try:
        if plan["workflow"] == "floating":
            from floating import floating
            metrics = floating(job, plan["parameters"], job / "workspace/OpenACM", command)
        else:
            metrics = {"sram": sram, "multiplier": multiplier, "compressor": compressor, "dcim": dcim}[plan["workflow"]](job, plan["parameters"], job / "workspace/OpenACM")
        dump(job / "metrics.json", metrics)
        dump(job / "worker_result.json", {"status": "completed", "metrics": metrics})
    except Exception as exc:
        traceback.print_exc()
        partial = job / "metrics.json"
        dump(job / "worker_result.json", {"status": "failed", "error": str(exc),
                                         "metrics": json.loads(partial.read_text()) if partial.exists() else {}})
        sys.exit(1)


if __name__ == "__main__":
    main()
