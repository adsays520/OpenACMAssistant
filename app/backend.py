"""Bounded Nangate45 reference flow, executed inside the guarded task only.

Original generators and library files stay untouched. These Tcl scripts are
external adapters, with their inputs and stage evidence retained per task.
"""
import json
import math
from pathlib import Path
import random
import re
import shutil
import time


TOP = "sram_multiplier_system"
MACRO = "SRAM_6T_CORE_16x8_MC_TB"


def run_backend(job, params, repo, command):
    n, rows = params["bit_width"], params["rows"]
    MACRO = f"SRAM_6T_CORE_{rows}x{n}_MC_TB"
    use_macro = params["memory_impl"] != "standard_cells" and (rows, n) in [(16, 8), (32, 16), (64, 32)]
    out = job / "artifacts/backend"
    scripts = job / "inputs/backend"
    out.mkdir(parents=True)
    scripts.mkdir(parents=True)
    stage_file = job / "reports/backend_stages.json"
    stages = []
    metrics = {}
    platform = repo / "DCIM/nangate45"
    lib = platform / "lib/NangateOpenCellLibrary_typical.lib"
    macro_lib = platform / f"lib/{MACRO}.lib"
    cell_model = platform / "NangateOpenCellLibrary.v"
    sram_model = repo / "DCIM/results/synth/sram_syn.v"
    control = repo / "DCIM/rtl/sram_control.v"
    prefix = {"exact": "Exact", "approx": "Appro4_2", "log": "Log_our"}[params["kind"]]
    multiplier = job / f"artifacts/rtl/{prefix}_{n}bit.v"
    sta = shutil.which("sta") or shutil.which("opensta")
    if not sta:
        raise RuntimeError("未找到 STA 可执行文件（需 sta 或 opensta 在 PATH 中）；shell alias 不会被 subprocess 继承")
    for path in [lib, cell_model, sram_model] + ([macro_lib, platform / f"lef/{MACRO}.lef"] if use_macro else []):
        if not path.is_file():
            raise RuntimeError(f"后端缺少匹配资源：{path}")

    def save():
        stage_file.write_text(json.dumps({"stages": stages, "sta": sta, "platform": "Nangate45",
                                          "macro": MACRO if use_macro else None, "memory_impl": "macro" if use_macro else "standard_cells", "dimensions": [rows, n]}, ensure_ascii=False, indent=2) + "\n")
        partial = job / "metrics.json"
        existing = json.loads(partial.read_text()) if partial.exists() else {}
        partial.write_text(json.dumps({**existing, **metrics}, ensure_ascii=False, indent=2) + "\n")

    def stage(name, executable, text, artifacts):
        script = scripts / (name + (".ys" if executable == "yosys" else ".tcl"))
        script.write_text(text)
        entry = {"name": name, "status": "running", "script": str(script.relative_to(job)),
                 "log": f"logs/backend_{name}.log"}
        stages.append(entry)
        save()
        print(f"[后端 {len(stages):02d}] {name}", flush=True)
        started = time.monotonic()
        log = job / entry["log"]
        try:
            if executable == "yosys":
                args = ["yosys", "-s", str(script)]
            elif executable == "openroad":
                args = ["openroad", "-no_init", "-threads", "4", "-exit", str(script)]
            else:
                args = [sta, "-no_init", "-exit", str(script)]
            command(args, repo, log, timeout=600)
            transcript = log.read_text()
            if re.search(r"(?m)^\s*(?:Error:|\[ERROR\b)", transcript):
                raise RuntimeError(f"工具报告错误：{log}")
            for path in artifacts:
                if not path.is_file() or not path.stat().st_size:
                    raise RuntimeError(f"阶段未生成有效产物：{path}")
            entry.update(status="completed", artifacts=[str(p.relative_to(job)) for p in artifacts])
        except Exception as exc:
            entry.update(status="failed", error=str(exc))
            raise
        finally:
            entry["elapsed_s"] = round(time.monotonic() - started, 3)
            save()

    synthesis = out / "synth.v"
    stage("synthesis", "yosys", f"""
read_liberty -lib {lib}
{("read_liberty -lib " + str(macro_lib)) if use_macro else ("read_verilog " + str(sram_model))}
read_verilog {control}
read_verilog {multiplier}
hierarchy -check -top {TOP}
synth -top {TOP} -flatten
dfflibmap -liberty {lib}
abc -liberty {lib}
clean
check -assert
stat -liberty {lib}
write_verilog -noattr -noexpr {synthesis}
""", [synthesis])

    sdc = scripts / "constraint.sdc"
    period = params["clock_period_ns"]
    io_delay = period * 0.2
    sdc.write_text(f"""create_clock -name core_clock -period {period} [get_ports clk]
set_clock_uncertainty 0.1 [get_clocks core_clock]
set_input_delay {io_delay} -clock core_clock [get_ports {{data_in* pe_ce init_enable}}]
set_output_delay {io_delay} -clock core_clock [get_ports {{data_out* init_done valid_out}}]
set_input_transition 0.05 [get_ports {{data_in* pe_ce init_enable}}]
set_load 0.005 [get_ports {{data_out* init_done valid_out}}]
set_false_path -from [get_ports rst_n]
""")

    # Differential checks cover varying initialization, input streams, enable
    # pauses, address wrap, and a second reset. The RTL is the functional oracle;
    # multiplier arithmetic was independently checked before entering this flow.
    golden = scripts / "golden_control.v"
    golden_mult = scripts / "golden_multiplier.v"
    source_mult = multiplier.read_text()
    names = re.findall(r"\bmodule\s+(\w+)", source_mult)
    expression = r"\b(" + "|".join(map(re.escape, names)) + r")\b"
    golden_mult.write_text(re.sub(expression, lambda m: "golden_" + m[0], source_mult))
    golden_control = re.sub(r"\bmodule\s+" + TOP + r"\b", "module golden_pe", control.read_text(), count=1)
    golden.write_text(re.sub(expression, lambda m: "golden_" + m[0], golden_control))
    timed_macro = scripts / "sram_timing_model.v"
    paths = "\n".join(f"    (clk => rd_out[{i}]) = (0,0);" for i in range(n))
    checks = ["we_in", "ce_in"] + [f"addr_in[{i}]" for i in range((rows-1).bit_length())] + [f"wd_in[{i}]" for i in range(n)]
    paths += "\n" + "\n".join(f"    $setuphold(posedge clk, {pin}, 0,0,notifier);" for pin in checks)
    paths += "\n    $width(posedge clk,0,0,notifier);\n    $width(negedge clk,0,0,notifier);"
    timed_macro.write_text("`timescale 1ns/1ps\n" + sram_model.read_text().replace("endmodule", "reg notifier;\nspecify\n" + paths + "\nendspecify\nendmodule"))
    timed_cells = scripts / "standard_cell_model.v"
    timed_cells.write_text("`timescale 1ns/1ps\n" + cell_model.read_text())

    def simulation(label, netlist, sdf=None):
        tb = scripts / f"{label}_tb.v"
        wave = out / f"{label}.vcd"
        half = period / 2
        settle = min(period * 0.2, 1.0)
        annotation = f'$sdf_annotate("{sdf}",dut,,,"MAXIMUM");' if sdf else ""
        cycles=rows+160
        stimuli=scripts / "backend_vectors.hex"
        rng=random.Random(params["seed"])
        if not stimuli.exists():
            stimuli.write_text("\n".join(f"{rng.randrange(1<<n):x}" for _ in range(2*cycles))+"\n")
        tb.write_text(f'''`timescale 1ns/1ps
module tb;
reg clk=0,rst_n=0,pe_ce=1,init_enable=0;
reg [{n-1}:0] data_in=0;
wire [{2*n-1}:0] data_out,expected;
wire init_done,valid_out,golden_done,golden_valid;
reg [{n-1}:0] stimuli[0:{2*cycles-1}];
integer checks=0,round,i,fill;
always #{half} clk=~clk;
{TOP} dut(.clk(clk),.rst_n(rst_n),.pe_ce(pe_ce),.init_enable(init_enable),.data_in(data_in),.data_out(data_out),.init_done(init_done),.valid_out(valid_out));
golden_pe golden(.clk(clk),.rst_n(rst_n),.pe_ce(pe_ce),.init_enable(init_enable),.data_in(data_in),.data_out(expected),.init_done(golden_done),.valid_out(golden_valid));
initial begin
  $readmemh("{stimuli}",stimuli);
  $dumpfile("{wave}"); $dumpvars(0,dut);
  {annotation}
  repeat(4) @(negedge clk);
  for(round=0;round<2;round=round+1) begin
    fill=0;
    rst_n=0; init_enable=0; pe_ce=1;
    repeat(4) @(negedge clk);
    #{settle}; rst_n=1; init_enable=1;
    for(i=0;i<{cycles};i=i+1) begin
      @(negedge clk); #{settle};
      if(init_done !== golden_done || valid_out !== golden_valid)
        $fatal(1,"control mismatch cycle=%d",i);
      // The built-in valid flag precedes pipeline fill after INIT; allow
      // four fill cycles, then require known outputs for every check.
      if(golden_valid) fill=fill+1;
      if(golden_valid && fill>6) begin
        if(data_out !== expected || (^data_out === 1'bx))
          $fatal(1,"data mismatch cycle=%d actual=%h expected=%h",i,data_out,expected);
        checks=checks+1;
      end
      data_in=stimuli[round*{cycles}+i];
      pe_ce=!(i>50 && i<55);
    end
  end
  if(checks<200) $fatal(1,"insufficient checks");
  $display("BACKEND_SIM_PASS checked=%0d",checks); $finish;
end
endmodule
''')
        model = timed_macro if sdf else sram_model
        if sdf:
            vcs = shutil.which("vcs")
            if not vcs:
                raise RuntimeError("完整SDF仿真需要可用VCS及许可证；Icarus不能完整解析此宏的总线SDF，拒绝降级宣称通过")
            build = out / "vcs"
            build.mkdir()
            compiled = build / "simv"
            # VCS 2018 expects pthread_yield, removed from newer glibc. Link a
            # task-local forwarding object; do not alter the vendor installation.
            compat = build / "pthread_compat.c"
            compat.write_text("#include <sched.h>\nint pthread_yield(void) { return sched_yield(); }\n")
            compat_obj = build / "pthread_compat.o"
            command(["gcc", "-fPIC", "-c", str(compat), "-o", str(compat_obj)], build,
                    job / "logs/backend_vcs_compat.log")
            args = [vcs, "-full64", "-sverilog", "-timescale=1ns/1ps", "+define+NTC",
                    "-top", "tb", "-Mdir=" + str(build / "csrc"), "-o", str(compiled), "+sdfverbose", "+neg_tchk",
                    "-LDFLAGS", str(compat_obj)]
        else:
            compiled = out / f"{label}.vvp"
            args = ["iverilog", "-g2012", "-s", "tb", "-o", str(compiled)]
        args += [str(netlist), str(timed_cells if sdf else cell_model), str(model), str(golden), str(golden_mult), str(tb)]
        entry = {"name": label, "status": "running", "log": f"logs/backend_{label}.log"}
        stages.append(entry); save()
        started = time.monotonic()
        try:
            command(args, build if sdf else repo, job / f"logs/backend_{label}_compile.log")
            command([str(compiled)] if sdf else ["vvp", str(compiled)], build if sdf else repo, job / entry["log"])
            transcript = (job / entry["log"]).read_text()
            match = re.search(r"BACKEND_SIM_PASS checked=(\d+)", transcript)
            if not match:
                raise RuntimeError("缺少门级差分验证通过标记")
            if sdf and re.search(r"SDF (?:WARNING|ERROR)|Unable to match|Invalid/malformed|Warning-\[SDF|Error-\[|Timing violation", transcript, re.I):
                raise RuntimeError("SDF 标注未完整匹配，不能宣称时序后仿真通过；见阶段日志")
            if sdf:
                entry["simulator"] = "VCS"
                info = build / "sdfAnnotateInfo"
                if not info.exists():
                    raise RuntimeError("VCS未生成SDF标注统计，拒绝宣称后仿真完成")
                statistics = info.read_text()
                expected_checks = re.search(r"TIMINGCHECK\(ALL\)\s*=\s*(\d+)", statistics)
                actual_checks = re.search(r"Timing Checks\s*=\s*(\d+)", statistics)
                actual_paths = re.search(r"IOPATH Delays\s*=\s*(\d+)", statistics)
                if not expected_checks or not actual_checks or not actual_paths or int(actual_paths[1]) == 0 or actual_checks[1] != expected_checks[1]:
                    raise RuntimeError("SDF路径或timing check标注统计不完整，见sdfAnnotateInfo")
                metrics["sdf_annotated_timing_checks"] = int(actual_checks[1])
                metrics["sdf_annotated_iopaths"] = int(actual_paths[1])
                entry["artifacts"] = [str(wave.relative_to(job)), str(info.relative_to(job))]
            metrics[label + "_checks"] = int(match[1])
            entry["status"] = "completed"
            entry.setdefault("artifacts", [str(wave.relative_to(job))])
        except Exception as exc:
            entry.update(status="failed", error=str(exc)); raise
        finally:
            entry["elapsed_s"] = round(time.monotonic() - started, 3); save()
        return wave

    synth_wave = simulation("post_synth_functional", synthesis)

    def sta_reports(label, netlist, wave, spef=None):
        sdf = out / f"{label}.sdf"
        prefix_path = job / f"reports/{label}"
        text = f"""read_liberty {lib}
{("read_liberty " + str(macro_lib)) if use_macro else ""}
read_verilog {netlist}
link_design {TOP}
read_sdc {sdc}
"""
        if spef:
            text += f"read_spef {spef}\nset_propagated_clock [all_clocks]\n"
        text += f"""check_setup -verbose > {prefix_path}_coverage.rpt
report_checks -path_delay min_max -fields {{slew cap input_pins}} -format full_clock_expanded > {prefix_path}_timing.rpt
report_worst_slack -max -digits 8
report_worst_slack -min -digits 8
write_sdf {sdf}
read_vcd -scope tb/dut {wave}
if {{[llength [info commands report_activity_annotation]]}} {{
  report_activity_annotation > {prefix_path}_activity.rpt
}} else {{
  set activity_report [open {prefix_path}_activity.rpt w]
  puts $activity_report "This STA version lacks detailed activity coverage reporting. Actual annotated pin count is in the stage log; power uses read_vcd activities."
  close $activity_report
}}
report_power -digits 8 > {prefix_path}_power.rpt
exit
"""
        artifacts = [sdf, Path(str(prefix_path) + "_timing.rpt"), Path(str(prefix_path) + "_power.rpt"), Path(str(prefix_path) + "_activity.rpt")]
        stage(label + "_sta_power", "sta", text, artifacts)
        if spef:
            transcript = (job / f"logs/backend_{label}_sta_power.log").read_text()
            slacks = re.findall(r"worst slack\s+(-?[\d.]+)", transcript, re.I)
            if len(slacks) != 2:
                raise RuntimeError("未获得有效 setup/hold slack")
            metrics.update(setup_wns_ns=float(slacks[0]), hold_wns_ns=float(slacks[1]))
            power = Path(str(prefix_path) + "_power.rpt").read_text()
            total = re.search(r"(?m)^Total\s+([\deE.+-]+)\s+([\deE.+-]+)\s+([\deE.+-]+)\s+([\deE.+-]+)", power)
            if not total or not math.isfinite(float(total[4])):
                raise RuntimeError("未获得有效功耗 Total")
            metrics["routed_power_uw"] = float(total[4]) * 1e6
        return sdf

    sta_reports("post_synth", synthesis, synth_wave)

    load = f"""read_lef {platform / 'lef/NangateOpenCellLibrary.tech.lef'}
read_lef {platform / 'lef/NangateOpenCellLibrary.macro.mod.lef'}
{("read_lef " + str(platform / ("lef/" + MACRO + ".lef"))) if use_macro else ""}
read_liberty {lib}
{("read_liberty " + str(macro_lib)) if use_macro else ""}
"""
    transcript = (job / "logs/backend_synthesis.log").read_text()
    areas = re.findall(r"Chip area for (?:top )?module[^:]*:\s*([0-9.]+)", transcript)
    mapped_area = float(areas[-1]) if areas else 0
    if use_macro:
        dimensions = re.search(r"SIZE\s+([0-9.]+)\s+BY\s+([0-9.]+)", (platform / f"lef/{MACRO}.lef").read_text())
        if not dimensions:
            raise RuntimeError("匹配LEF缺少宏尺寸")
        macro_w, macro_h = map(float, dimensions.groups())
    else:
        macro_w = macro_h = 0
    die_w = max(200, math.ceil(macro_w + 125), math.ceil(math.sqrt(mapped_area / 0.3) + 40))
    die_h = max(220, math.ceil(macro_h + 125), die_w)
    macro_place = "place_macro -macro_name sram_inst -location {60 60} -orient N\ncut_rows -halo_width_x 3 -halo_width_y 3" if use_macro else ""
    macro_pdn = f"""define_pdn_grid -name macro_grid -voltage_domains CORE -macro -cells {{{MACRO}}} -orient {{R0}} -halo {{2 2 2 2}}
add_pdn_stripe -grid macro_grid -layer metal5 -width 0.93 -pitch 10 -offset 0.5
add_pdn_stripe -grid macro_grid -layer metal6 -width 0.93 -pitch 10 -offset 0.5
add_pdn_connect -grid macro_grid -layers {{metal3 metal5}}
add_pdn_connect -grid macro_grid -layers {{metal4 metal5}}
add_pdn_connect -grid macro_grid -layers {{metal5 metal6}}
add_pdn_connect -grid macro_grid -layers {{metal6 metal7}}""" if use_macro else ""
    floorplan = out / "floorplan.odb"
    stage("floorplan_pdn", "openroad", load + f"""
read_verilog {synthesis}
link_design {TOP}
read_sdc {sdc}
initialize_floorplan -die_area {{0 0 {die_w} {die_h}}} -core_area {{5 5 {die_w-5} {die_h-5}}} -site FreePDK45_38x28_10R_NP_162NW_34O
source {platform / 'make_tracks.tcl'}
{macro_place}
add_global_connection -net VDD -inst_pattern {{.*}} -pin_pattern {{^(VDD|vdd)$}} -power
add_global_connection -net VSS -inst_pattern {{.*}} -pin_pattern {{^(VSS|gnd)$}} -ground
global_connect
set_voltage_domain -name CORE -power VDD -ground VSS
define_pdn_grid -name grid -voltage_domains CORE
add_pdn_stripe -grid grid -layer metal1 -width 0.17 -pitch 2.8 -offset 0 -followpins
add_pdn_stripe -grid grid -layer metal4 -width 0.48 -pitch 28 -offset 2
add_pdn_stripe -grid grid -layer metal7 -width 1.4 -pitch 30 -offset 2
add_pdn_connect -grid grid -layers {{metal1 metal4}}
add_pdn_connect -grid grid -layers {{metal4 metal7}}
{macro_pdn}
pdngen
write_db {floorplan}
write_def {out / 'floorplan.def'}
exit
""", [floorplan, out / "floorplan.def"])

    def reload(db):
        return load + f"read_db {db}\nread_sdc {sdc}\nsource {platform / 'setRC.tcl'}\n"

    placed = out / "placement.odb"
    stage("placement", "openroad", reload(floorplan) + f"""
insert_tiecells -prefix TIELO LOGIC0_X1/Z
insert_tiecells -prefix TIEHI LOGIC1_X1/Z
set_wire_rc -signal -layer metal3
set_wire_rc -clock -layer metal5
set_placement_padding -global -left 1 -right 1
global_placement -skip_io -density 0.45
place_pins -hor_layers metal5 -ver_layers metal6
global_placement -density 0.45 -timing_driven
estimate_parasitics -placement
repair_design
repair_timing -setup
detailed_placement
check_placement -verbose
write_db {placed}
write_def {out / 'placement.def'}
exit
""", [placed, out / "placement.def"])

    clocked = out / "cts.odb"
    stage("cts", "openroad", reload(placed) + f"""
set_wire_rc -signal -layer metal3
set_wire_rc -clock -layer metal5
repair_clock_inverters
clock_tree_synthesis -buf_list {{CLKBUF_X1 CLKBUF_X2 CLKBUF_X3}} -root_buf CLKBUF_X3 -sink_clustering_enable
set_propagated_clock [all_clocks]
repair_clock_nets
detailed_placement
estimate_parasitics -placement
repair_timing -setup
repair_timing -hold -hold_margin 0.03
detailed_placement
check_placement -verbose
write_db {clocked}
write_def {out / 'cts.def'}
exit
""", [clocked, out / "cts.def"])

    routed = out / "route.odb"
    netlist = out / "route.v"
    drc = job / "reports/route_drc.rpt"
    stage("routing", "openroad", reload(clocked) + f"""
set_propagated_clock [all_clocks]
set_routing_layers -signal metal2-metal10 -clock metal2-metal10
set_global_routing_layer_adjustment metal2-metal10 0.3
global_route -congestion_report_file {job / 'reports/congestion.rpt'}
estimate_parasitics -global_routing
detailed_route -output_drc {drc} -or_seed {params['seed']}
filler_placement {{FILLCELL_X1 FILLCELL_X2 FILLCELL_X4 FILLCELL_X8 FILLCELL_X16 FILLCELL_X32}}
global_connect
write_db {routed}
write_def {out / 'route.def'}
write_verilog {netlist}
report_design_area
exit
""", [routed, out / "route.def", netlist])
    transcript = (job / "logs/backend_routing.log").read_text()
    counts = re.findall(r"(?:Number of violations\s*[:=]|#violations\s*=)\s*(\d+)", transcript, re.I)
    if not counts:
        raise RuntimeError("布线日志缺少最终 DRC 数量，不能把运行完成当作 DRC 通过")
    metrics["route_drc_count"] = int(counts[-1])
    area = re.search(r"Design area\s+([\d.]+)\s+u", transcript)
    if area:
        metrics["routed_area_um2"] = float(area[1])

    spef = out / "route.spef"
    stage("rc_extraction", "openroad", reload(routed) + f"""
define_process_corner -ext_model_index 0 X
extract_parasitics -ext_model_file {platform / 'rcx_patterns.rules'}
write_spef {spef}
exit
""", [spef])
    if "*D_NET" not in spef.read_text():
        raise RuntimeError("SPEF 缺少提取的网络，不能当有效寄生结果")
    route_wave = simulation("post_route_functional", netlist)
    sdf = sta_reports("post_route_before_sdf", netlist, route_wave, spef)
    timing_wave = simulation("post_route_sdf", netlist, sdf)
    sta_reports("post_route", netlist, timing_wave, spef)
    return metrics
