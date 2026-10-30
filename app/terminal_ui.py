"""Small, dependency-free terminal presentation for the chat entry point."""
import os
import shutil
import sys


THEMES = {
    "neon": (51, 45, 39, 99, 135, 213),
    "ocean": (159, 123, 87, 51, 45, 39),
    "sunset": (229, 221, 215, 209, 203, 198),
    "mono": (255,) * 6,
}
LOGO = (
    "  ██████╗ ██████╗ ███████╗███╗   ██╗ █████╗  ██████╗███╗   ███╗",
    " ██╔═══██╗██╔══██╗██╔════╝████╗  ██║██╔══██╗██╔════╝████╗ ████║",
    " ██║   ██║██████╔╝█████╗  ██╔██╗ ██║███████║██║     ██╔████╔██║",
    " ██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║██╔══██║██║     ██║╚██╔╝██║",
    " ╚██████╔╝██║     ███████╗██║ ╚████║██║  ██║╚██████╗██║ ╚═╝ ██║",
    "  ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝╚═╝  ╚═╝ ╚═════╝╚═╝     ╚═╝",
)


def parameter_lines(plan):
    """Readable design conditions, including non-default advanced settings."""
    from spec import DEFAULTS
    p, wf = plan["parameters"], plan["workflow"]
    lines, shown = [], set()

    def line(keys, content):
        shown.update(keys.split())
        lines.append(content)

    if wf in ("multiplier", "dcim", "floating"):
        line("samples exhaustive seed", f"验证输入：{'穷举所有组合' if p.get('exhaustive') else str(p['samples'])+'组采样'}，测试种子{p['seed']}。")
        backends = {"functional": "RTL功能验证", "synthesis": "RTL验证与综合", "asic": "完整ASIC后端"}
        line("backend", "评价流程：" + backends[p["backend"]] + "。")
    if wf in ("multiplier", "dcim"):
        shown.update(("bit_width", "kind"))
        ids = p.get("compressor_ids") or [p["compressor_id"]]
        names = {1: "ACCI1", 2: "kong2", 3: "antonio", 4: "momeni", 5: "ha", 6: "akbar1", 7: "akbar2", 8: "sabetz"}
        assignment = ("所有近似位置使用" + names[ids[0]]) if len(set(ids)) == 1 else f"逐位置混合分配，共{len(ids)}处"
        if p["kind"] == "approx":
            line("compressor_id compressor_ids", "压缩器：" + assignment + "。")
        shown.update(("compressor_id", "compressor_ids"))
        arch = {"auto": "按尺寸自动选择", "native": "原生结构", "tiled": "8位分块结构"}
        line("architecture", "架构：" + arch[p["architecture"]] + "。")
    if wf == "dcim":
        memory = {"auto": "按尺寸自动匹配参考宏，否则使用标准单元存储", "macro": "匹配参考SRAM宏", "standard_cells": "标准单元存储"}
        line("clock_period_ns memory_impl", f"时钟周期{p['clock_period_ns']:g}ns；存储实现：{memory[p['memory_impl']]}。")
    if wf in ("sram", "compressor"):
        line("vdd_v temperature_c", f"供电{p['vdd_v']:g}V，温度{p['temperature_c']:g}℃。")
    if wf == "sram":
        shown.update(("rows", "cols"))
        ops = {"read": "读", "write": "写", "hold_snm": "保持SNM", "read_snm": "读SNM", "write_snm": "写SNM"}
        line("corner operations", f"工艺角{p['corner']}；测量" + "、".join(ops[k] for k in p["operations"]) + "。")
        line("pu_width_nm pd_width_nm pg_width_nm length_nm", f"器件尺寸：PU={p['pu_width_nm']:g}nm、PD={p['pd_width_nm']:g}nm、PG={p['pg_width_nm']:g}nm，沟道长{p['length_nm']:g}nm。")
        if p["variation"]:
            line("variation mc_runs vth_std seed", f"Monte Carlo每组合{p['mc_runs']}次，种子{p['seed']}，相对标准差{100*p['vth_std']:g}%。")
    if wf == "floating":
        shown.update(("variant", "fp_width", "exp_width"))
    if wf == "compressor":
        shown.add("name")
        line("nmos_scale pmos_scale length_scale load_scale", f"NMOS/PMOS宽度倍率{p['nmos_scale']:g}/{p['pmos_scale']:g}，沟道长倍率{p['length_scale']:g}，负载倍率{p['load_scale']:g}。")
    labels = {"rows": "行数", "clock_period_ns": "时钟周期(ns)", "memory_impl": "存储实现", "mantissa_keep": "尾数保留位", "mode": "近似模式",
              "wire_rc": "线网RC", "wire_res_ohm": "线电阻(Ω)", "wire_cap_pf": "线电容(pF)", "column_mux": "列复用", "full_array": "完整阵列",
              "pu_model": "PU模型", "pd_model": "PD模型", "pg_model": "PG模型", "mc_runs": "MC样本数", "variation": "器件扰动", "seed": "种子", "vth_std": "相对标准差"}
    labels.update({k + "_scale": label + "倍率" for k, label in [("decoder", "译码器"), ("wordline", "字线"), ("precharge", "预充电"), ("senseamp", "灵敏放大器"), ("write_driver", "写驱动"), ("mux", "复用器")]})
    extra = [f"{labels.get(k, k)}={('开启' if v else '关闭') if type(v) is bool else v}" for k, v in p.items()
             if k not in shown and v != DEFAULTS[wf].get(k)]
    if extra:
        lines.append("其它设置：" + "；".join(extra) + "。")
    return lines


def review_plan(plan):
    from result_summary import design_text, value_text
    if plan.get("kind") == "search_comparison":
        e, p = plan["experiment"], plan["experiment"]["plan"]["parameters"]
        labels = {"random": "随机搜索", "nsga2": "NSGA-II", "llm": "DeepSeek"}
        methods = "、".join(labels[m] for m in plan["methods"])
        physical = e["physical_evaluation"]
        lines = [f"这次将比较{methods}，每种方法运行{len(plan['search_seeds'])}次。",
                 f"每轮最多评价{plan['candidate_budget']}个不同架构，包含同一个初始配置；从中选最多{plan['validation_top_k']}个进行RTL、综合和STA复核。",
                 f"测试输入固定为种子{p['seed']}、{'穷举所有输入' if p['exhaustive'] else str(p['samples'])+'组采样'}；搜索种子为{plan['search_seeds']}。",
                 f"各组使用同一候选池和Nangate45 typical库，输入活动周期{physical['clock_period_ns']:g}ns、输出负载{physical['output_load_ff']:g}fF，器件尺寸固定。"]
        constraints = e["plan"]["constraints"]
    elif "budgets" in plan:
        b = plan["budgets"]
        lines = [f"将为{design_text(plan['plan'])}做两级探索。",
                 f"架构最多评价{b['architecture']}项，复核最多{b['validation']}项；每个入选设计的器件最多评价{b['device_per_parent']}项，总时限{b['max_seconds']}秒。"]
        if plan.get("experiment_mode") == "sram_device_retest":
            p, g = plan["plan"]["parameters"], plan["architecture_space"]
            r, c = g["rows"][0], g["cols"][0]
            cases = plan["robustness"]
            lines = [f"固定SRAM组织为{r}行×{c}列×{plan['capacity_bits']//(r*c)}个子阵列，列复用比{g['mux_ratio'][0]}。",
                     "架构阶段只做容量与接口校验，SPICE时间留给器件复验。",
                     f"基线PU={p['pu_width_nm']:g}nm、PD={p['pd_width_nm']:g}nm、PG={p['pg_width_nm']:g}nm、沟道长{p['length_nm']:g}nm；只比较" + "、".join(k.split('_')[0].upper()+"="+"/".join(f"{v:g}" for v in values)+"nm" for k,values in plan["device_space"].items()) + "。",
                     "逐个评价这些PVT组合：" + "、".join(f"{v['corner']}/{v['temperature_c']:g}℃/{v['vdd_v']:g}V" for v in cases["cases"]) + "。",
                     f"每个组合MC={cases['mc_runs']}，种子{p['seed']}，相对标准差{cases['vth_std']*100:g}%；总时限{b['max_seconds']}秒。",
                     "保留读写、三种SNM、原始样本、失败记录和同条件基线比较；bank外围/面积为估算，小样本不代表良率。"]
        if plan["workflow"] == "sram":
            lines.append(f"总容量{plan['capacity_bits']}bit，输出字宽{plan['output_width']}bit。")
        constraints = plan["plan"]["constraints"]
    else:
        lines = ["准备运行：" + design_text(plan) + "。"]
        constraints = plan["constraints"]
        lines.extend(parameter_lines(plan))
    if constraints:
        lines.append("验收要求：" + "；".join(value_text(c["metric"], c["value"]) + ("及以下" if c["op"] == "le" else "及以上") for c in constraints) + "。")
    else:
        lines.append("尚未设置数值验收上限；如有精度要求，可以继续补充。")
    lines.append("输入“开始运行”启动这项计划，也可以继续调整条件。")
    return "\n\n".join(lines)


class TerminalUI:
    def __init__(self, theme="neon", color="auto"):
        self.theme = theme
        self.color = color == "always" or (
            color == "auto" and sys.stdout.isatty()
            and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
        )
        self.readline = False
        if sys.stdin.isatty():
            try:
                import readline  # noqa: F401; enables editing and input history
                self.readline = True
            except ImportError:
                pass

    def paint(self, text, index=0, bold=False):
        if not self.color:
            return text
        code = THEMES[self.theme][index % 6]
        weight = "1;" if bold else ""
        return f"\033[{weight}38;5;{code}m{text}\033[0m"

    def banner(self):
        columns = shutil.get_terminal_size((88, 24)).columns
        logo_width = max(map(len, LOGO))
        if columns >= logo_width + 6:
            width = min(columns - 2, 84) - 2
            print(self.paint("╭" + "─" * width + "╮", 0))
            print(self.paint("│" + " CIRCUIT STUDIO  /  LOCAL DEEPSEEK ".center(width) + "│", 0))
            print(self.paint("│" + " " * width + "│", 0))
            for index, line in enumerate(LOGO):
                print(self.paint("│" + line.center(width) + "│", index, bold=True))
            print(self.paint("│" + " " * width + "│", 5))
            print(self.paint("│" + "[ SRAM ]──[ MULTIPLIER ]──[ COMPRESSOR ]──[ DCiM ]".center(width) + "│", 5))
            print(self.paint("╰" + "─" * width + "╯", 5))
        else:
            # Narrow terminals keep the name and controls readable.
            title = "[ OPENACM ]" if columns >= 11 else "OPENACM"
            print(self.paint(title, bold=True))
            print(self.paint("LOCAL DEEPSEEK", 3))
        print(self.paint("  从电路想法，到仿真结果。", 1, bold=True))
        print("  可以先问能力、参数和指标；明确执行需求后启动任务并总结结果。")
        self.help()

    def help(self):
        print(self.paint("  /plan 需求  查看计划    /new  新对话    /exit  退出", 2))
        print(self.paint("  /theme neon|ocean|sunset|mono  切换配色", 3))
        print(self.paint("  /clear  重绘界面    /help  帮助", 4))
        print(self.paint("  探索8位近似乘法器，预算64个候选，MRED≤0.02", 1))

    def prompt(self):
        if not self.color:
            return "\n你 › "
        start = f"\033[1;38;5;{THEMES[self.theme][0]}m"
        end = "\033[0m"
        if self.readline:
            start, end = f"\001{start}\002", f"\001{end}\002"
        return f"\n{start}你 › {end}"

    def command(self, text):
        """Handle presentation commands; return whether a command was consumed."""
        if text == "/help":
            self.help()
        elif text == "/clear":
            if sys.stdout.isatty():
                print("\033[2J\033[H", end="")
            self.banner()
        elif text == "/theme" or text.startswith("/theme "):
            name = text.partition(" ")[2].strip()
            if name not in THEMES:
                print("可选配色：" + " / ".join(THEMES))
            else:
                self.theme = name
                self.banner()
        else:
            return False
        return True

    def status(self, message):
        print(self.paint("  ◇ " + message, 2), flush=True)

    def error(self, message):
        print(self.paint("  ! " + message, 5), file=sys.stderr)
