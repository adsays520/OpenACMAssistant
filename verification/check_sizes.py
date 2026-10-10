"""Run actual RTL/SPICE adapters across sizes; keep failed points as evidence."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"app"))
import assistant
from spec import validate


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--all-widths",action="store_true",help="逐个检查4–64整数位宽")
    args=parser.parse_args()
    widths=list(range(4,65)) if args.all_widths else [4,5,8,12,16,24,32,48,64]
    cases=[]
    for bits in widths:
        for kind in ["exact","approx"]:
            cases.append({"workflow":"multiplier","parameters":{"bit_width":bits,"kind":kind,"samples":64,"compressor_id":1}})
    for bits in [8,16,32]:
        cases.append({"workflow":"multiplier","parameters":{"bit_width":bits,"kind":"log","samples":64}})
    for rows,bits in [(2,4),(8,5),(32,16),(64,32),(128,64)]:
        cases.append({"workflow":"dcim","parameters":{"rows":rows,"bit_width":bits,"samples":64}})
    for rows,cols in [(2,2),(4,2),(16,8),(32,16),(64,32)]:
        cases.append({"workflow":"sram","parameters":{"rows":rows,"cols":cols,"operations":["read","write"]}})
    for width,exp in [(8,3),(16,5),(24,7),(32,8)]:
        cases.append({"workflow":"floating","parameters":{"fp_width":width,"exp_width":exp,"variant":"Exact","samples":256}})
    directory=ROOT/"runs"/(datetime.now().strftime("%Y%m%d_%H%M%S")+"_size_verification_"+uuid.uuid4().hex[:6])
    directory.mkdir(parents=True)
    before=assistant.manifest(assistant.SOURCE)
    records=[]
    print(directory,flush=True)
    try:
        for i,raw in enumerate(cases,1):
            plan=validate(raw)
            task=directory/f"cases/{i:03d}"
            result=assistant.run_one(task,plan,timeout_override=180)
            assistant.write_json(task/"plan.json",plan)
            records.append({"case":i,"workflow":plan["workflow"],"parameters":plan["parameters"],"task":str(task.relative_to(directory)),**result})
            assistant.write_json(directory/"result.json",{"kind":"size_verification","cases":records})
            print(i,len(cases),plan["workflow"],raw["parameters"],result["status"],flush=True)
    finally:
        after=assistant.manifest(assistant.SOURCE)
        changed=[k for k in sorted(set(before)|set(after)) if before.get(k)!=after.get(k)]
        assistant.write_json(directory/"source_integrity.json",{"unchanged":not changed,"checked_files":len(before),"changes":changed})
        (directory/"report.md").write_text("# 尺寸接口验证\n\n"+"\n".join(f"- {r['case']}：{r['workflow']} {r['parameters'].get('bit_width',r['parameters'].get('fp_width',str(r['parameters'].get('rows'))+'×'+str(r['parameters'].get('cols'))))}，{r['status']}；[报告]({r['task']}/reports/report.md)" for r in records)+"\n\n接口接受不等于本点电路或RTL正确；浮点源码Exact的IEEE边界错误保留在comparison.csv。\n")
        if changed:
            raise RuntimeError("原始源程序完整性变化")


if __name__=="__main__":
    main()
