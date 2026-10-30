"""Plot existing 4KB architecture observations; do not run or rewrite experiments."""
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.lines import Line2D

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[2]
TASK = ROOT / "runs/20261025_143705_acco_sram_324799"
FONT = FontProperties(fname="/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
plt.rcParams.update({"font.family": FONT.get_name(), "font.size": 10, "axes.unicode_minus": False,
    "svg.fonttype": "path", "figure.facecolor": "white", "savefig.facecolor": "white"})
TEAL, NAVY, ORANGE, GRAY = "#087F83", "#15324F", "#BD7920", "#6A7C8E"


def digest(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def save(fig, name):
    for ext in ("png", "svg", "pdf"):
        fig.savefig(HERE / "assets" / (name + "." + ext), dpi=240, bbox_inches="tight")
    plt.close(fig)


def style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(alpha=.18)
    ax.set_axisbelow(True)


def main():
    source = TASK / "result.json"
    before = digest(source)
    r = json.loads(source.read_text())
    stage = r["stages"]["architecture"]
    rows = stage["records"]
    good = [x for x in rows if x["status"] == "completed"]
    bad = [x for x in rows if x["status"] != "completed"]
    assert len(rows) == 27 and len(good) == 23 and len(bad) == 4
    assert all(x.get("stop_reason") == "time_budget" and not x["records"] for name, x in r["stages"].items() if name.startswith("device_"))
    assert r["config"]["capacity_bits"] == 32768 and r["config"]["output_width"] == 8
    for x in rows:
        a = x["parameters"]["architecture"]
        assert a["rows"] * a["cols"] * a["num_arrays"] == 32768
    best = max(good, key=lambda x: x["metrics"]["fom_arch_estimate"])
    baseline = rows[0]
    assert best["candidate"] == "architecture/0013"
    snapshot = {"source_task": str(TASK.relative_to(ROOT)), "source_result_sha256": before,
        "input_docx_sha256": digest(ROOT.parent / "修改中(1).docx"),
        "original_status": r["status"], "original_stop_reason": r["stop_reason"],
        "interpretation": "Architecture evaluation stopped on time budget; device stages have no observations. Original infeasible/stages_finished is misleading; no raw files are changed.",
        "config": r["config"], "architecture": stage, "source_integrity": json.loads((TASK / "source_integrity.json").read_text())}
    (HERE / "evidence_snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    metrics = ["bank_area_um2_estimate", "bank_read_power_uw_estimate", "bank_write_power_uw_estimate",
        "bank_read_delay_ns_estimate", "bank_write_delay_ns_estimate", "fom_arch_estimate"]
    with (HERE / "architecture_data.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["candidate", "status", "rows", "cols", "num_arrays", "mux_ratio", "elapsed_s"] + metrics)
        for row in rows:
            a = row["parameters"]["architecture"]
            writer.writerow([row["candidate"], row["status"], *[a[k] for k in ["rows", "cols", "num_arrays", "mux_ratio"]], row["elapsed_s"],
                *[row.get("metrics", {}).get(k, "") for k in metrics]])

    fig, ax = plt.subplots(figsize=(7.5, 3.9))
    current, trace = float("-inf"), []
    for x in rows:
        if x["status"] == "completed": current = max(current, x["metrics"]["fom_arch_estimate"])
        trace.append(current)
    ax.step(range(1, len(rows)+1), trace, where="post", color=TEAL, linewidth=2, label="截至该次尝试的最佳有效FOM")
    ax.axhline(trace[0], color=GRAY, linestyle="--", linewidth=1, label="枚举起点FOM")
    failed = [i+1 for i, x in enumerate(rows) if x["status"] != "completed"]
    ax.scatter(failed, [trace[i-1] for i in failed], marker="x", color=ORANGE, s=60, zorder=5, label="候选超时（未取得有效指标）")
    ax.scatter([13], [trace[12]], color=NAVY, s=40, zorder=6)
    ax.annotate("#13：FOM=15.68631", (13, trace[12]), xytext=(14.0, trace[12]-.10), color=NAVY,
        arrowprops={"arrowstyle": "->", "color": GRAY}, fontsize=9)
    ax.set(xlabel="累计候选尝试数（含超时）", ylabel="最佳架构FOM（越大越好）", xlim=(.5, 28), ylim=(15.27, 15.76),
        title="4KB SRAM架构搜索：27次尝试，23项有效、4项超时")
    ax.legend(loc="lower right", fontsize=8.5, frameon=True, facecolor="white", framealpha=.96, edgecolor="#DAE1E7"); style(ax)
    fig.text(.5, .015, "2小时预算在架构阶段耗尽；器件/PVT-MC未执行。该单次曲线不证明LLM优于其他算法。", ha="center", fontsize=8, color=GRAY)
    fig.tight_layout(rect=(0, .045, 1, 1))
    save(fig, "01_4KB架构搜索轨迹")

    fig, ax = plt.subplots(figsize=(7.5, 4.4))
    area = [x["metrics"]["bank_area_um2_estimate"] / 1e4 for x in good]
    for ratio, marker in [(1, "o"), (2, "^")]:
        subset = [x for x in good if x["parameters"]["architecture"]["mux_ratio"] == ratio]
        dots = ax.scatter([x["metrics"]["bank_read_delay_ns_estimate"] for x in subset],
            [x["metrics"]["bank_read_power_uw_estimate"] for x in subset],
            c=[x["metrics"]["bank_area_um2_estimate"]/1e4 for x in subset], marker=marker, s=65,
            cmap="viridis", vmin=min(area), vmax=max(area), alpha=.85, edgecolors="white", linewidths=.6)
    for row, color, label, position in [(baseline, ORANGE, "枚举起点 #1", (2.65, 550)), (best, "#C52C38", "FOM入选 #13", (2.40, 710))]:
        m = row["metrics"]
        ax.scatter([m["bank_read_delay_ns_estimate"]], [m["bank_read_power_uw_estimate"]], marker="*", s=190, color=color, edgecolors="white", linewidths=.5, zorder=5)
        ax.annotate(label, (m["bank_read_delay_ns_estimate"], m["bank_read_power_uw_estimate"]), xytext=position, fontsize=8.5, color=color,
            arrowprops={"arrowstyle": "->", "color": color, "linewidth": .8}, bbox={"facecolor": "white", "alpha": .9, "edgecolor": "none", "pad": 2})
    for identity, position in [(5, (1.26, 345)), (11, (1.98, 350)), (15, (1.56, 1080)), (22, (1.40, 1420))]:
        x = next(v for v in good if v["candidate"] == f"architecture/{identity:04d}")
        ax.annotate(f"#{identity}", (x["metrics"]["bank_read_delay_ns_estimate"], x["metrics"]["bank_read_power_uw_estimate"]),
            xytext=position, fontsize=8.5, color=NAVY, arrowprops={"arrowstyle": "-", "color": GRAY, "linewidth": .7},
            bbox={"facecolor": "white", "alpha": .85, "edgecolor": "none", "pad": 1})
    fig.colorbar(dots, ax=ax, pad=.02, label="bank面积估算 / 10⁴ µm²")
    ax.legend(handles=[Line2D([], [], marker="o", linestyle="none", color=GRAY, label="列复用比1"),
        Line2D([], [], marker="^", linestyle="none", color=GRAY, label="列复用比2")], loc="upper right", frameon=False, fontsize=9)
    ax.set(xlabel="bank读延迟估算 / ns", ylabel="bank读功耗估算 / µW", xlim=(1.15, 4.45), ylim=(300, 1470),
        title="固定4KB、8位输出的架构取舍（23项有效记录）")
    style(ax)
    fig.text(.5, .012, "TT / 27℃ / 1V；阵列SPICE＋bank外围/面积估算。散点为权衡展示，不是三目标Pareto前沿。", ha="center", fontsize=8, color=GRAY)
    fig.tight_layout(rect=(0, .045, 1, 1))
    save(fig, "02_4KB功耗延迟权衡")

    labels = ["bank面积估算", "bank读功耗估算", "bank写功耗估算", "bank读延迟估算", "bank写延迟估算"]
    values = [(best["metrics"][k]/baseline["metrics"][k]-1)*100 for k in metrics[:5]]
    fig, ax = plt.subplots(figsize=(7.5, 3.8))
    ax.barh(range(5), values, color=[TEAL if v < 0 else ORANGE for v in values], height=.6)
    for i, value in enumerate(values):
        ax.text(value + (-1 if value < 0 else 1), i, f"{value:+.2f}%", ha="right" if value < 0 else "left", va="center", fontsize=10)
    ax.axvline(0, color=GRAY, linewidth=1)
    ax.set(yticks=range(5), yticklabels=labels, xlabel="相对枚举起点的变化（这些指标下降为改善）", xlim=(-68, 16),
        title="32×32×32入选组织相对16×8×256起点的指标变化")
    ax.invert_yaxis(); style(ax)
    fig.text(.5, .018, "相同4KB、8位接口与标称条件；写功耗增加6.95%。仅为本次模型评价，非硅片测量或全局最优。", ha="center", fontsize=8, color=GRAY)
    fig.tight_layout(rect=(0, .06, 1, 1))
    save(fig, "03_4KB初始与入选对照")

    assert digest(source) == before
    files = list((HERE / "assets").glob("*"))
    assert len(files) == 9
    manifest = {"source_result_sha256": before, "raw_source_unchanged": True, "attempted": len(rows), "valid": len(good),
        "timed_out": len(bad), "best_architecture_candidate": best["candidate"],
        "relative_changes_percent": dict(zip(metrics[:5], values)),
        "files": {str(p.relative_to(HERE)): {"sha256": digest(p), "bytes": p.stat().st_size} for p in files},
        "checks": ["fixed capacity", "valid-only metric scatter", "timeout markers", "no device-stage measurements claimed", "original result unchanged"]}
    (HERE / "figure_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print("PASS: 3 figures in PNG/SVG/PDF; original observations unchanged; 27 attempts, 23 valid, 4 timeouts")


if __name__ == "__main__":
    main()
