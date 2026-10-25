"""Additional isolated EDA checks for shortlisted ACCO designs."""
import csv
import json
import math
from pathlib import Path
import re
import shutil

from acco_models import load_module


def multiplier_ppa(task, physical, command):
    task = Path(task)
    out = task / "artifacts/synthesis"
    platform = task / "workspace/OpenACM/DCIM/nangate45"
    top = re.search(r"\bmodule\s+(\w+)", (out / "mapped.v").read_text())[1]
    vcd = out / "activity.vcd"
    tb = (out / "gate_tb.v").read_text()
    tb = tb.replace(" #1;", f" #{physical['clock_period_ns']};")
    tb = tb.replace("initial begin", f'initial begin\n $dumpfile("{vcd}"); $dumpvars(0,tb.dut);', 1)
    (out / "activity_tb.v").write_text(tb)
    command(["iverilog", "-g2012", "-s", "tb", "-o", str(out / "activity.vvp"), str(out / "mapped.v"),
             str(platform / "NangateOpenCellLibrary.v"), str(out / "activity_tb.v")], out, task / "logs/acco_activity_compile.log")
    command(["vvp", str(out / "activity.vvp")], out, task / "logs/acco_activity.log")
    script = task / "inputs/acco_sta.tcl"
    timing, power = task / "reports/acco_timing.rpt", task / "reports/acco_power.rpt"
    script.write_text(f"""read_liberty {platform / 'lib/NangateOpenCellLibrary_typical.lib'}
read_verilog {out / 'mapped.v'}
link_design {top}
create_clock -name virtual_clock -period {physical['clock_period_ns']}
set_input_delay 0 -clock virtual_clock [all_inputs]
set_output_delay 0 -clock virtual_clock [all_outputs]
set_input_transition 0.05 [all_inputs]
set_load {physical['output_load_ff']} [all_outputs]
report_checks -path_delay max -digits 8 > {timing}
read_vcd -scope tb/dut {vcd}
report_power -digits 8 > {power}
exit
""")
    command([shutil.which("sta") or "opensta", "-exit", str(script)], out, task / "logs/acco_sta.log")
    transcript = (task / "logs/acco_sta.log").read_text()
    annotated = re.search(r"Annotated\s+(\d+)\s+pin activities", transcript)
    if re.search(r"(?mi)^Error:|No activities", transcript) or not annotated or int(annotated[1]) == 0:
        raise ValueError("STA或活动标注失败，不能报告PDP")
    arrivals = re.findall(r"([0-9.eE+-]+)\s+data arrival time", timing.read_text())
    row = re.search(r"(?m)^Total\s+([\deE.+-]+)\s+([\deE.+-]+)\s+([\deE.+-]+)\s+([\deE.+-]+)", power.read_text())
    if not arrivals or not row:
        raise ValueError("STA未提供有效delay/power")
    delay = max(map(float, arrivals))
    dynamic = (float(row[1]) + float(row[2])) * 1e6
    if not all(math.isfinite(v) and v > 0 for v in (delay, dynamic)):
        raise ValueError("PDP需要有限正延迟与动态功耗")
    return {"sta_delay_ns": delay, "sta_dynamic_power_uw": dynamic,
            "sta_total_power_uw": float(row[4]) * 1e6, "pdp_fj": delay * dynamic,
            "output_load_ff": physical["output_load_ff"], "sta_clock_period_ns": physical["clock_period_ns"],
            "activity_sample_period_ns": physical["clock_period_ns"], "sta_vcd_annotated_pins": int(annotated[1])}


def width_layout(reference, name):
    module = load_module("acco_run_ppa", Path(reference) / "run_ppa.py")
    netlist, _ = module.find_files_in_directory(str(Path(reference) / name))
    tracker = module.InstanceTracker()
    processor = module.NetlistProcessor(netlist, module.TransistorModifier({}), tracker)
    processor.read_netlist()
    processor.detect_includes()
    processor.analyze_flattened_instances()
    groups, total = module.analyze_cell_transistor_requirements(tracker)
    labels = [(cell, group) for cell in tracker.get_ordered_cell_types() for group in groups[cell]["types"]]
    widths = [float(group.rsplit("_", 1)[-1]) for _, group in labels]
    if not widths or len(widths) != total:
        raise ValueError("原始分组宽度无法解析")
    return module, widths, labels


def spice_truth(netlist, folder, expected, command):
    """Check every input against the selected approximate RTL's own truth table."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    statements = []
    for line in Path(netlist).read_text().splitlines():
        if line.lstrip().startswith("+") and statements:
            statements[-1] += "\n" + line
        else:
            statements.append(line)
    text = "\n".join(s for s in statements if not re.match(r"\s*(?:V[ABCD]\s|\.(?:tran|print|measure|meas|end|options)\b)", s, re.I))
    for bit, name in enumerate("abcd"):
        points = ["0n 0"]
        old = 0
        for code in range(16):
            value = ((code >> (3-bit)) & 1) * 1.2
            points += [f"{code*20+.1:g}n {old}", f"{code*20+.2:g}n {value}"]
            old = value
        points.append(f"320n {old}")
        text += f"\nV{name} {name} 0 PWL(" + " ".join(points) + ")"
    text += "\n.TRAN 0.05n 320n\n.PRINT TRAN FORMAT=CSV V(S) V(cout)\n.END\n"
    design = folder / "truth.sp"
    design.write_text(text)
    command(["Xyce", str(design)], folder, folder / "xyce.log")
    output = folder / "truth.sp.csv"
    if not output.exists():
        raise ValueError("缺少功能电平核验波形")
    with output.open() as stream:
        rows = list(csv.DictReader(stream))
    samples = []
    for code in range(16):
        target = (code*20+19) * 1e-9
        row = min(rows, key=lambda r: abs(float(r["TIME"]) - target))
        bits = []
        for key in ("V(S)", "V(COUT)"):
            v = float(row[key])
            if not math.isfinite(v) or .24 < v < .96:
                raise ValueError("压缩器输出不满足VOL/VOH电平界限")
            bits.append("1" if v >= .96 else "0")
        samples.append("".join(bits))
    (folder / "truth.json").write_text(json.dumps({"expected": expected, "actual": samples, "passed": samples == expected,
                                                  "vol_max_v": .24, "voh_min_v": .96}, indent=2))
    if samples != expected:
        raise ValueError("器件尺寸模板不满足对应RTL近似压缩器真值表，不能保持架构精度")
    return {"truth_vectors": 16, "truth_mismatches": 0}


def compressor_width(reference, name, scales, task, expected, command):
    module, initial, labels = width_layout(reference, name)
    if len(scales) != len(initial):
        raise ValueError("逐组尺寸向量长度错误")
    widths = [w * scale for w, scale in zip(initial, scales)]
    if any(not .145 <= w <= 2.7 for w in widths):
        raise ValueError("逐组宽度超出原器件模板0.145–2.7um范围")
    task = Path(task)
    task.mkdir(parents=True, exist_ok=True)
    # Original adapter uses relative ppa_results; the guarded pipeline changes cwd here.
    import os
    old = Path.cwd()
    try:
        os.chdir(task)
        values = module.run_ppa_vector(str(Path(reference) / name), widths, verbose=False)
    finally:
        os.chdir(old)
    if values is None or any(not math.isfinite(float(v)) or float(v) <= 0 for v in values):
        raise ValueError("原压缩器PPA评价未产生有效结果")
    outputs = sorted(task.glob("ppa_results/*/*.sp"))
    if len(outputs) != 1:
        raise ValueError("压缩器尺寸网表产物不唯一")
    truth = spice_truth(outputs[0], task / "truth", expected, command)
    delay, power, area = map(float, values)
    delay /= 1000  # Original adapter's delay is ps.
    (task / "widths.json").write_text(json.dumps({"groups": labels, "width_um": widths, "length_policy": "unchanged"}, indent=2))
    return {"delay_ns": delay, "power_uw": power, "estimated_area_um2": area, "pdp_fj": delay * power, **truth,
            "model": "simplified_LEVEL_1", "evidence_kind": "spice_with_area_estimate"}
