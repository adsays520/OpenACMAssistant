"""Measured mapped cell area, followed by a gate-level functional check."""
import math
from pathlib import Path
import re


def synthesize(job, repo, sources, top, testbench, command, extra_sources=(), extra_libs=()):
    platform = repo / "DCIM/nangate45"
    lib = platform / "lib/NangateOpenCellLibrary_typical.lib"
    cells = platform / "NangateOpenCellLibrary.v"
    out = job / "artifacts/synthesis"
    out.mkdir(parents=True, exist_ok=True)
    mapped = out / "mapped.v"
    script = job / "inputs/synthesis.ys"
    script.write_text("\n".join([
        f"read_liberty -lib {lib}", *[f"read_liberty -lib {p}" for p in extra_libs], *[f"read_verilog {p}" for p in sources],
        f"hierarchy -check -top {top}", f"synth -top {top} -flatten",
        f"dfflibmap -liberty {lib}", f"abc -liberty {lib}", "clean", "check -assert",
        f"stat -liberty {lib}" + "".join(f" -liberty {p}" for p in extra_libs), f"write_verilog -noattr -noexpr {mapped}"]) + "\n")
    command(["yosys", "-s", str(script)], repo, job / "logs/synthesis.log", timeout=600)
    transcript = (job / "logs/synthesis.log").read_text()
    values = re.findall(r"Chip area for (?:top )?module[^:]*:\s*([0-9.]+)", transcript)
    if not values or not math.isfinite(float(values[-1])) or float(values[-1]) <= 0:
        raise RuntimeError("综合未给出有效映射面积，不使用RTL行数或预测值替代")
    tb = out / "gate_tb.v"
    tb.write_text(testbench)
    compiled = out / "gate.vvp"
    command(["iverilog", "-g2012", "-s", "tb", "-o", str(compiled), str(mapped), str(cells),
             *map(str, extra_sources), str(tb)], repo, job / "logs/synthesis_gate_compile.log")
    command(["vvp", str(compiled)], repo, job / "logs/synthesis_gate_simulation.log")
    return {"synthesized_area_um2": float(values[-1]), "synthesis_library": "Nangate45_typical"}
