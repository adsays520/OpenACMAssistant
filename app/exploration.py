"""Bounded, checkpointed LLM design exploration using measured candidates only."""
import copy
import csv
from datetime import datetime
import fcntl
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
import re
import time
import urllib.request
import uuid
from zoneinfo import ZoneInfo

from bindings import explicit_parameters, explicit_constraints
from spec import COMPRESSORS, DEFAULTS, METRICS, compressor_count, validate
from experience import automatic_learn, retrieve, compact_context, runtime_environment, source_digest, preferred_options
from public_dataset import retrieve_public, preferred_public_options, augment_default_space, decision_context, POLICY as PUBLIC_DATA_POLICY


WORKFLOWS = ["sram", "multiplier", "compressor", "dcim", "floating"]


def json_write(path, value):
    temporary=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n")
    temporary.replace(path)


def fingerprint(params):
    p = copy.deepcopy(params)
    if "bit_width" in p:
        p.setdefault("architecture","auto")
    if p.get("kind") != "approx":
        p.pop("compressor_id",None); p.pop("compressor_ids",None)
    elif "compressor_ids" in p:
        p.pop("compressor_id",None)
    if "variant" in p:
        if p["variant"] not in ["AC","EAFPU"]:
            p.pop("mode",None)
        if p["variant"] != "AC":
            p.pop("mantissa_keep",None)
    return hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest()


def default_config(workflow, request="", base=None):
    facts = explicit_parameters(request,workflow)
    supplied=copy.deepcopy(base or {"workflow":workflow,"parameters":{},"constraints":[]})
    supplied["parameters"]={**supplied.get("parameters",{}),**facts}
    constraints=explicit_constraints(request,workflow)
    if constraints:
        supplied["constraints"]=constraints
    plan=validate(supplied)
    if plan["intent"] != "run":
        raise ValueError(plan["message"])
    p = plan["parameters"]
    if workflow in ["multiplier","dcim","floating"] and p["backend"] == "functional":
        p["backend"] = "synthesis"
    if workflow == "sram":
        needed = {c["metric"].removesuffix("_v") for c in plan["constraints"] if "snm" in c["metric"]}
        p["operations"] = list(dict.fromkeys(p["operations"] + ["hold_snm","read_snm","write_snm"] + sorted(needed)))
        space = {"pu_width_nm":[60,90,120],"pd_width_nm":[90,135,180,225],"pg_width_nm":[90,135,180],
                 "vdd_v":[0.9,1.0,1.1],"column_mux":[False,True],
                 "wordline_scale":[0.8,1,1.2],"senseamp_scale":[0.8,1,1.2],"write_driver_scale":[0.8,1,1.2]}
        if not any(k in facts for k in ["rows","cols"]):
            capacity = p["rows"]*p["cols"]
            space["shape"] = [[r,capacity//r] for r in [4,8,16,32,64] if capacity%r==0 and capacity//r>=2]
        objective = {"metric":"read_power_uw","direction":"min"}
        secondary = {"metric":"read_delay_ns","direction":"min"}
        if not plan["constraints"]:
            plan["constraints"] = [{"metric":"read_delay_ns","op":"le","value":2},
                                   {"metric":"write_delay_ns","op":"le","value":2},
                                   {"metric":"read_snm_v","op":"ge","value":0.2}]
    elif workflow in ["multiplier","dcim"]:
        count = compressor_count(p["bit_width"],p["architecture"])
        space = {"kind":["exact","approx"]+(["log"] if p["bit_width"] in [8,16,32] else []),"compressor_id":list(range(1,9)),
                 "compressor_ids":[[a if i<count//2 else b for i in range(count)] for a,b in [(1,8),(8,1),(1,5),(5,1),(2,8),(8,2),(1,2),(2,1)]]}
        assignment_rng=random.Random(p["seed"])
        space["compressor_ids"] += [[assignment_rng.randrange(1,9) for _ in range(count)] for _ in range(128)]
        if workflow == "dcim" and p["backend"] == "asic":
            objective = {"metric":"routed_area_um2","direction":"min"}
            secondary = {"metric":"routed_power_uw","direction":"min"}
            for key in ["setup_wns_ns","hold_wns_ns"]:
                if not any(c["metric"] == key for c in plan["constraints"]):
                    plan["constraints"].append({"metric":key,"op":"ge","value":0})
            if not any(c["metric"] == "route_drc_count" for c in plan["constraints"]):
                plan["constraints"].append({"metric":"route_drc_count","op":"le","value":0})
        else:
            objective = {"metric":"synthesized_area_um2","direction":"min"}
            secondary = {"metric":"mred","direction":"min"}
        if not any(c["metric"] == "mred" for c in plan["constraints"]):
            plan["constraints"].append({"metric":"mred","op":"le","value":0.02})
    elif workflow == "compressor":
        space = {"name":COMPRESSORS,"nmos_scale":[0.75,1,1.25,1.5],"pmos_scale":[0.75,1,1.25,1.5]}
        objective,secondary = {"metric":"power_uw","direction":"min"},{"metric":"delay_ns","direction":"min"}
        if not plan["constraints"]:
            plan["constraints"]=[{"metric":"delay_ns","op":"le","value":2}]
    else:
        variants = ["Exact"]+(["AC"] if p["fp_width"]>=16 and p["exp_width"]>=5 else []) if (p["fp_width"],p["exp_width"]) != (32,8) else list(__import__('spec').ENUMS['variant'])
        space={"variant":variants,"mode":[0,1,2],"mantissa_keep":[3,4,5,6]}
        objective,secondary={"metric":"synthesized_area_um2","direction":"min"},{"metric":"mred","direction":"min"}
        if not plan["constraints"]:
            plan["constraints"]=[{"metric":"mred","op":"le","value":0.02},{"metric":"special_mismatch_count","op":"le","value":0}]
    # Explicit input values stay fixed. A custom exploration JSON can unlock fields.
    space={k:v for k,v in space.items() if k not in facts}
    if "compressor_id" in facts:
        space.pop("compressor_ids",None)
    if "compressor_ids" in p:
        space.pop("compressor_id",None)
    if facts.get("kind") in ["exact","log"]:
        space.pop("compressor_id",None); space.pop("compressor_ids",None)
    config={"plan":validate(plan),"space":space,"objectives":[plan.get("objective") or objective,secondary]}
    # Remove default options that are not legal for this requested size/format.
    for key,values in list(space.items()):
        accepted=[]
        for value in values:
            trial={key:value}
            if key in ["compressor_id","compressor_ids"]:
                trial["kind"]="approx"
            try:
                candidate_plan(config,trial)
            except ValueError:
                continue
            accepted.append(value)
        if accepted:
            space[key]=accepted
        else:
            space.pop(key)
    return augment_default_space(Path(__file__).resolve().parents[1], config)


def candidate_plan(config, changes):
    plan = copy.deepcopy(config["plan"])
    p = plan["parameters"]
    changes=copy.deepcopy(changes)
    if "shape" in changes:
        p["rows"],p["cols"]=changes.pop("shape")
    if "compressor_id" in changes:
        p.pop("compressor_ids",None)
    p.update(changes)
    if p.get("kind") != "approx":
        p.pop("compressor_ids",None)
    plan["sweep"]=None
    candidate=validate(plan)
    baseline=config["plan"]["parameters"]
    wf=config["plan"]["workflow"]
    invariants=["samples","exhaustive","seed","temperature_c","corner","operations","full_array","variation","mc_runs","vth_std","load_scale","fp_width","exp_width","bit_width","clock_period_ns","backend","memory_impl"]
    if any(candidate["parameters"].get(k)!=baseline.get(k) for k in invariants):
        raise ValueError("探索不能更改位宽、测试条件、参考格式、模型或验证模式")
    if wf == "sram" and p["rows"]*p["cols"]!=baseline["rows"]*baseline["cols"]:
        raise ValueError("SRAM探索保持容量不变；使用shape同时调整行列")
    if wf == "dcim" and p["rows"]!=baseline["rows"]:
        raise ValueError("DCiM探索保持容量不变")
    return candidate


def validate_config(config):
    if not isinstance(config,dict) or set(config)-{"plan","space","objectives"}:
        raise ValueError("探索配置仅允许plan、space、objectives")
    config=copy.deepcopy(config)
    config["plan"]=validate(config["plan"])
    if config["plan"]["intent"]!="run" or config["plan"]["sweep"]:
        raise ValueError("探索基线必须是无sweep的run计划")
    if not isinstance(config["space"],dict) or len(config["space"])>24:
        raise ValueError("space必须为至多24个参数的候选值对象")
    for key,values in config["space"].items():
        if key not in config["plan"]["parameters"] and key not in ["shape","compressor_ids"]:
            raise ValueError(f"未知探索参数：{key}")
        if not isinstance(values,list) or not 1<=len(values)<=256:
            raise ValueError("每个参数需为1–256个候选值")
        for value in values:
            trial={key:value}
            if key in ["compressor_id","compressor_ids"]:
                trial["kind"]="approx"
            candidate_plan(config,trial)
    if not 1<=len(config["objectives"])<=4:
        raise ValueError("需1–4个实测目标，第一项用于排名")
    for objective in config["objectives"]:
        if set(objective)!={"metric","direction"} or objective["metric"] not in METRICS or objective["direction"] not in ["min","max"]:
            raise ValueError("目标格式为{metric,direction:min/max}")
    return config


def feasible(record,objectives):
    return record["status"]=="completed" and all(type(record.get("metrics",{}).get(o["metric"])) in [float,int] and math.isfinite(record["metrics"][o["metric"]]) for o in objectives)


def score(record,objectives):
    return tuple(record["metrics"][o["metric"]]*(1 if o["direction"]=="min" else -1) for o in objectives)


def ranking(records,objectives):
    valid=[r for r in records if feasible(r,objectives)]
    valid.sort(key=lambda r:score(r,objectives))
    pareto=[r["candidate"] for r in valid if not any(all(a<=b for a,b in zip(score(other,objectives),score(r,objectives))) and any(a<b for a,b in zip(score(other,objectives),score(r,objectives))) for other in valid)]
    return (valid[0] if valid else None),pareto


def menu(config,records,rng,limit=12):
    seen={fingerprint(r["parameters"]) for r in records}
    options={}
    def add(changes):
        try:
            plan=candidate_plan(config,changes)
        except (ValueError,TypeError):
            return
        key=fingerprint(plan["parameters"])
        if key not in seen:
            options.setdefault(key,plan)
    if not records:
        add({})
        return list(options.values())
    # Early exploration covers all template types, then combines/mutates settings.
    for key in ["name","variant","kind","compressor_id"]:
        for value in config["space"].get(key,[]):
            patch={key:value}
            if key=="compressor_id" and "kind" in config["space"]:
                patch["kind"]="approx"
            add(patch)
    best,_=ranking(records,config["objectives"])
    if best:
        current=best["parameters"]
        for key,values in config["space"].items():
            for value in values:
                patch={k:v for k,v in current.items() if k in config["space"]}
                if "shape" in config["space"]:
                    patch["shape"]=[current["rows"],current["cols"]]
                patch[key]=value
                add(patch)
    for _ in range(160):
        patch={k:rng.choice(v) for k,v in config["space"].items()}
        if "compressor_id" in patch and "compressor_ids" in patch:
            patch.pop(rng.choice(["compressor_id","compressor_ids"]))
        add(patch)
    if not options:
        # Random sampling alone cannot establish that the finite space is empty.
        keys=list(config["space"])
        for values in itertools.product(*(config["space"][key] for key in keys)):
            add(dict(zip(keys,values)))
            if len(options)>=limit:
                break
    items=list(options.values())
    if len(items)<=limit:
        return items
    head=items[:max(1,limit//2)]
    return head+rng.sample(items[len(head):],limit-len(head))


def choose_model(options,records,config,endpoint,model,audit,timeout,persistent=None,public=None):
    schema={"type":"object","additionalProperties":False,"properties":{
        "candidate_index":{"type":"integer","enum":list(range(len(options)))},
        "reason":{"type":"string","maxLength":200}},"required":["candidate_index","reason"]}
    def brief(p):
        value=copy.deepcopy(p)
        ids=value.get("compressor_ids")
        if ids and len(ids)>32:
            value["compressor_ids"]={"count":len(ids),"head":ids[:12],"tail":ids[-12:]}
        return value
    history=[{"candidate":r["candidate"],"parameters":brief(r["parameters"]),"status":r["status"],"metrics":r.get("metrics",{}),"constraints":r.get("constraints",[]),"error":r.get("error","")[:180]} for r in records[-12:]]
    best,_=ranking(records,config["objectives"])
    if best:
        best={**best,"parameters":brief(best["parameters"])}
    body={"model":model,"stream":False,"format":schema,"options":{"temperature":0,"num_ctx":8192,"num_predict":220},
          "messages":[{"role":"system","content":"你是OpenACM设计空间探索工程师。根据实测反馈选择下一项尚未运行的设计。先满足约束，再优化objectives第一项；兼顾其余目标，探索不同架构和参数组合。失败点不能当优胜者。menu内候选尚未在本轮实测，reason只能说明待验证的选择依据，不能说候选已达标或已有本地测量。仅返回菜单candidate_index和简短中文reason，不生成程序、不编造仿真结果。" + PUBLIC_DATA_POLICY},
                      {"role":"user","content":json.dumps({"constraints":config["plan"]["constraints"],"objectives":config["objectives"],"best_measured":best,"recent_measured":history,"persistent_experience":compact_context(persistent) if persistent else None,"public_dataset_reference":public,"menu":[{"candidate_index":i,"parameters":brief(o["parameters"])} for i,o in enumerate(options)]},ensure_ascii=False)}]}
    json_write(audit/"request.json",body)
    client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request=urllib.request.Request(endpoint+"/api/chat",data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
    with client.open(request,timeout=timeout) as response:
        reply=json.load(response)
    json_write(audit/"reply.json",reply)
    if reply.get("done_reason")=="length":
        raise ValueError("探索建议被截断")
    choice=json.loads(reply["message"]["content"])
    index=choice["candidate_index"]
    if type(index) is not int or not 0<=index<len(options) or not isinstance(choice["reason"],str):
        raise ValueError("探索建议不在允许菜单中")
    if public and public["observations"]:
        ids=options[index]["parameters"].get("compressor_ids")
        evidence=next((r for r in public["observations"] if r["compressor_ids"]==ids),None)
        if evidence:
            # Raw model prose is retained in reply.json; external labels cannot
            # turn speculative selection prose into a passed local constraint.
            reason=f"选择菜单{index}复测；参考公开样本{evidence['sample_id']}（发布MRED={evidence['published_labels']['mred']:.6g}），本地是否达标待验收。"
            json_write(audit/"reason_grounding.json",{"raw_model_reason":choice["reason"],"reason":reason,"sample_id":evidence["sample_id"]})
            return index,reason
        reason=f"选择菜单{index}探索其它配置；公开数据仅供参考，须本地实测验收。"
        json_write(audit/"reason_grounding.json",{"raw_model_reason":choice["reason"],"reason":reason,"sample_id":None})
        return index,reason
    return index,choice["reason"][:200]


def save(job,state):
    config,records=state["config"],state["candidates"]
    best,pareto=ranking(records,config["objectives"])
    state["best_feasible_candidate"]=best["candidate"] if best else None
    state["pareto_candidates"]=pareto
    json_write(job/"result.json",state)
    if best:
        # Preserve compound row/column changes exactly.
        best_plan=copy.deepcopy(config["plan"]); best_plan["parameters"]=best["parameters"]; best_plan["sweep"]=None
        json_write(job/"best_plan.json",validate(best_plan))
    objective=config["objectives"][0]
    lines=["# DeepSeek 设计空间探索","",f"状态：{state['status']}；停止原因：{state.get('stop_reason','探索中')}","",
           f"电路：{config['plan']['workflow']}；已执行 {len(records)} 个候选；预算 {state['budget']}。", "",
           "| 候选 | 选择来源 | 状态 | 主要目标 | MRED | 报告 |", "|---|---|---|---:|---:|---|"]
    for r in records:
        lines.append(f"| {r['candidate']} | {r['proposed_by']} | {r['status']} | {r.get('metrics',{}).get(objective['metric'],'—')} | {r.get('metrics',{}).get('mred','—')} | [查看]({r['task']}/reports/report.md) |")
    lines += ["",f"最佳已达标候选：{state['best_feasible_candidate']}；Pareto候选：{pareto}。",
              "仅在本次已运行且所有约束、优化目标均有实测值的候选中选择，不宣称全局最优。",
              "固定容量、位宽、随机种子、采样集合和模型条件；采样精度约束仅对本次测试集合成立。",
              "综合面积来自Nangate45映射单元，SRAM标准单元实现不等于6T宏；SPICE面积仍为估算。",
              "模型选择下一候选，外部执行器运行实际仿真；原始OpenACM程序只读。", "",
              "最佳配置保存为best_plan.json，可直接run复现；完整历史为result.json和history.csv。"]
    (job/"report.md").write_text("\n".join(lines)+"\n")
    with (job/"history.csv").open("w") as stream:
        writer=csv.writer(stream); writer.writerow(["candidate","status","proposed_by",*[o["metric"] for o in config["objectives"]],"parameters"])
        for r in records:
            writer.writerow([r["candidate"],r["status"],r["proposed_by"],*[r.get("metrics",{}).get(o["metric"],"") for o in config["objectives"]],json.dumps(r["parameters"],ensure_ascii=False)])


def run_exploration(root,source,config,runner,manifest,endpoint,model,budget=64,max_seconds=7200,use_model=True,resume=None,request="",parent=None):
    if type(budget) is not int or not 1<=budget<=10000 or not 10<=max_seconds<=604800:
        raise ValueError("预算需1–10000个候选，单次时间10–604800秒")
    config=validate_config(config)
    if resume:
        job=Path(resume).resolve(strict=True)
        if not job.is_relative_to(root/"runs"):
            raise ValueError("只允许续跑runs内的探索任务")
        state=json.loads((job/"result.json").read_text())
        if state.get("kind")!="exploration" or validate_config(state["config"])!=config:
            raise ValueError("续跑需使用原探索配置")
        state["config"]=config
    else:
        stamp=datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
        job=(parent or root/"runs")/f"{stamp}_explore_{config['plan']['workflow']}_{uuid.uuid4().hex[:6]}"
        job.mkdir(parents=True)
        state={"kind":"exploration","status":"running","config":config,"candidates":[],"budget":budget,"sessions":[]}
        json_write(job/"config.json",config)
        (job/"request.txt").write_text(request+"\n")
        json_write(job/"source_before.json",manifest(source))
    with (job/".lock").open("a") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("此探索任务正在运行，不能同时续跑") from exc
        before=json.loads((job/"source_before.json").read_text())
        if manifest(source)!=before:
            raise RuntimeError("源程序与初始快照不符，停止续跑")
        state.update(status="running",budget=budget,stop_reason="running")
        session={"started":datetime.now().isoformat(),"max_seconds":max_seconds,"use_model":use_model}
        state["sessions"].append(session)
        started=time.monotonic()
        rng=random.Random(config["plan"]["parameters"].get("seed",42)+len(state["candidates"]))
        persistent=retrieve(root,config["plan"],runtime_environment(root,source_digest(before)),consumer=str(job.relative_to(root)),
                            exclude_prefix=str(job.relative_to(root)),objectives=config["objectives"],allow_shapes="shape" in config["space"])
        json_write(job/"experience_retrieval.json",persistent)
        reference_plan=copy.deepcopy(config["plan"])
        if "compressor_ids" in config["space"] or "compressor_id" in config["space"]:
            reference_plan["parameters"]["kind"]="approx"
        public=retrieve_public(root,reference_plan,limit=32,objectives=config["objectives"],allow_assignments="compressor_ids" in config["space"])
        json_write(job/"public_dataset_retrieval.json",public)
        print(f"探索目录：{job}\n目标：{config['objectives']}；约束：{config['plan']['constraints']}\n预算：{budget} 个候选，{max_seconds} 秒。",flush=True)
        try:
            while len(state["candidates"])<budget:
                remaining=max_seconds-(time.monotonic()-started)
                if remaining<5:
                    state["stop_reason"]="time_budget"; break
                options=menu(config,state["candidates"],rng)
                if state["candidates"]:
                    prior=preferred_options(persistent,config,state["candidates"])
                    prior += preferred_public_options(public,config,state["candidates"])
                    seen={fingerprint(p["parameters"]) for p in prior}
                    options=(prior+[p for p in options if fingerprint(p["parameters"]) not in seen])[:12]
                if not options:
                    state["stop_reason"]="space_exhausted"; break
                index,reason,proposer=0,"基线或确定性覆盖", "baseline" if not state["candidates"] else "deterministic"
                number=len(state["candidates"])+1
                audit=job/f"decisions/{number:04d}/{uuid.uuid4().hex[:8]}"
                audit.mkdir(parents=True,exist_ok=True)
                json_write(audit/"menu.json",options)
                if use_model and state["candidates"]:
                    print(f"[{number}/{budget}] DeepSeek根据已测结果选择下一配置…",flush=True)
                    try:
                        visible_public=decision_context(public,options)
                        json_write(audit/"public_dataset_retrieval.json",visible_public)
                        index,reason=choose_model(options,state["candidates"],config,endpoint,model,audit,min(120,max(1,remaining-2)),persistent,visible_public)
                        proposer="deepseek"
                    except Exception as exc:
                        (audit/"error.txt").write_text(str(exc)+"\n")
                        proposer="fallback"
                        reason="模型建议不可用，使用已校验候选；详见decision日志"
                json_write(audit/"selection.json",{"index":index,"reason":reason,"proposed_by":proposer})
                remaining=max_seconds-(time.monotonic()-started)
                if remaining<2:
                    state["stop_reason"]="time_budget"; break
                plan=options[index]
                task=job/f"candidates/{number:04d}"
                if task.exists():
                    # Keep an incomplete attempt intact when resuming after a crash.
                    interrupted=job/"interrupted"
                    interrupted.mkdir(exist_ok=True)
                    task.rename(interrupted/(task.name+"_"+uuid.uuid4().hex[:8]))
                print(f"[{number}/{budget}] {proposer}选择菜单配置{index}，开始实测验收。",flush=True)
                result=runner(task,plan,timeout_override=remaining)
                json_write(task/"plan.json",plan)
                record={"candidate":number,"parameters":plan["parameters"],"task":str(task.relative_to(job)),"proposed_by":proposer,**result}
                state["candidates"].append(record)
                best,_=ranking(state["candidates"],config["objectives"])
                primary=config["objectives"][0]["metric"]
                print(f"  结果：{result['status']}；{primary}={result.get('metrics',{}).get(primary,'未测量')}；当前最佳={best['candidate'] if best else '暂无达标项'}",flush=True)
                save(job,state)
                if "用户中断" in result.get("error",""):
                    state["stop_reason"]="interrupted"; break
            else:
                state["stop_reason"]="candidate_budget"
            state["status"]="completed" if ranking(state["candidates"],config["objectives"])[0] else "failed" if state["candidates"] and all(r["status"]=="failed" for r in state["candidates"]) else "infeasible"
        except KeyboardInterrupt:
            state.update(status="interrupted",stop_reason="interrupted")
        except Exception as exc:
            state.update(status="failed",stop_reason="execution_error",error=str(exc))
            raise
        finally:
            after=manifest(source)
            delta=[k for k in sorted(set(before)|set(after)) if before.get(k)!=after.get(k)]
            json_write(job/"source_integrity.json",{"unchanged":not delta,"checked_files":len(before),"changes":delta,"protection":"Landlock worker write confinement"})
            session["elapsed_s"]=round(time.monotonic()-started,3)
            save(job,state)
            if delta:
                raise RuntimeError("原始OpenACM完整性校验未通过")
            automatic_learn(root,job)
    return job


def is_exploration(text):
    return bool(re.search(r"设计空间|探索|架构优化|\bloop\b|\bdse\b|跑.*最优|优化.*(?:候选|轮|次)",text,re.I))


def explore_command(api,request="",workflow=None,budget=64,max_seconds=7200,use_model=True,spec_path=None,resume=None,previous=None,legacy=False):
    """CLI/chat routing; model still makes the sequential, feedback-based choices."""
    if resume:
        config=json.loads((Path(resume)/"config.json").read_text())
        if config.get("schema_version") == 1 and "budgets" in config:
            raise ValueError("ACCO阶段记录已保存；当前请用原config创建新任务，尚未开放中途续跑，不能用旧单级入口续跑")
        job=run_exploration(api.ROOT,api.SOURCE,config,api.run_one,api.manifest,api.OLLAMA,api.MODEL,budget,max_seconds,use_model,resume,request)
        api.summarize(job,api.OLLAMA,api.MODEL,use_model=use_model)
        return job
    count=re.search(r"(?:预算|最多|跑|探索)\s*(\d+)\s*(?:个候选|个配置|轮|次)",request)
    if count:
        budget=int(count[1])
    legacy = legacy or bool(re.search(r"局部探索|单级探索|legacy", request, re.I))
    from acco_spec import request_is_acco
    from conversation import workflow as detect
    detected = workflow or detect(request) or (previous or {}).get("workflow")
    is_acco_spec = spec_path and "budgets" in json.loads(Path(spec_path).read_text())
    if is_acco_spec or (not legacy and not spec_path and (request_is_acco(request) or detected in ("sram", "multiplier"))):
        from acco import command
        return command(api, request, spec_path, workflow, budget, max_seconds, use_model, previous)
    if workflow=="all" or re.search(r"所有.*(?:电路|工程)|全工程|全部电路",request):
        selected=WORKFLOWS
    elif workflow:
        selected=[workflow]
    elif spec_path:
        selected=[]
    else:
        lowered=request.lower()
        if any(x in lowered for x in ["浮点","fp32","fp16","floating"]):
            selected=["floating"]
        elif any(x in lowered for x in ["dcim","存内计算","pe "]):
            selected=["dcim"]
        elif "乘法器" in request or "multiplier" in lowered:
            selected=["multiplier"]
        elif "sram" in lowered or "存储阵列" in request:
            selected=["sram"]
        elif "压缩器" in request or any(x.lower() in lowered for x in COMPRESSORS):
            selected=["compressor"]
        elif previous:
            selected=[previous["workflow"]]
        else:
            raise ValueError("请指定电路或‘全工程探索’，例如‘探索8位近似乘法器，预算64个候选，MRED≤0.02’")
    if spec_path:
        configs=[json.loads(Path(spec_path).read_text())]
    else:
        baseline=previous if previous and len(selected)==1 and previous.get("workflow")==selected[0] else None
        if len(selected)==1 and request and use_model:
            baseline,_=api.plan_request(f"为后续设计空间探索先整理一个run基线，无sweep。候选预算{budget}个，探索循环由外部执行器执行。忠实落实用户尺寸、约束和优化目标：{request}",previous=baseline)
        configs=[default_config(w,request,baseline if len(selected)==1 else None) for w in selected]
    configs=[validate_config(c) for c in configs]
    if len(configs)==1:
        job=run_exploration(api.ROOT,api.SOURCE,configs[0],api.run_one,api.manifest,api.OLLAMA,api.MODEL,budget,max_seconds,use_model,request=request)
        api.summarize(job,api.OLLAMA,api.MODEL,use_model=use_model)
        return job
    stamp=datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    suite=api.ROOT/"runs"/f"{stamp}_exploration_suite_{uuid.uuid4().hex[:6]}"
    suite.mkdir()
    before=api.manifest(api.SOURCE)
    json_write(suite/"source_before.json",before)
    data={"kind":"exploration_suite","status":"running","budget_per_circuit":budget,"cases":[]}
    print(f"全工程探索目录：{suite}；{len(configs)}类电路，每类最多{budget}个候选。",flush=True)
    try:
        for config in configs:
            job=run_exploration(api.ROOT,api.SOURCE,config,api.run_one,api.manifest,api.OLLAMA,api.MODEL,budget,max_seconds,use_model,request=request,parent=suite/"tasks")
            result=json.loads((job/"result.json").read_text())
            best=next((r for r in result["candidates"] if r["candidate"]==result["best_feasible_candidate"]),None)
            data["cases"].append({"workflow":config["plan"]["workflow"],"task":str(job.relative_to(suite)),"status":result["status"],
                                  "candidate_count":len(result["candidates"]),"best_feasible_candidate":result["best_feasible_candidate"],
                                  "parameters":best["parameters"] if best else None,"metrics":best["metrics"] if best else {},"objectives":config["objectives"]})
            json_write(suite/"result.json",data)
            api.summarize(job,api.OLLAMA,api.MODEL,use_model=use_model)
            if result.get("stop_reason")=="interrupted":
                break
        data["status"]="completed" if len(data["cases"])==len(configs) and all(c["status"]=="completed" for c in data["cases"]) else "incomplete"
    finally:
        after=api.manifest(api.SOURCE)
        changes=[k for k in sorted(set(before)|set(after)) if before.get(k)!=after.get(k)]
        json_write(suite/"source_integrity.json",{"unchanged":not changes,"checked_files":len(before),"changes":changes})
        json_write(suite/"result.json",data)
        lines=["# 全工程设计空间探索","",f"状态：{data['status']}；每类预算{budget}个候选。","",
               "| 电路 | 实际候选 | 最佳已达标候选 | 状态 | 详细报告 |","|---|---:|---:|---|---|"]
        lines += [f"| {c['workflow']} | {c['candidate_count']} | {c['best_feasible_candidate']} | {c['status']} | [查看]({c['task']}/report.md) |" for c in data["cases"]]
        lines += ["","五类电路分开排名，数字探索采用综合映射面积，不混称为路由后PPA。每项保持规格一致；探索结果只对应有限实测候选。",
                  "各子任务包含best_plan.json、history.csv、模型决策与原始仿真记录。无达标候选时不产生最优配置。"]
        (suite/"report.md").write_text("\n".join(lines)+"\n")
        if changes:
            raise RuntimeError("原始程序完整性校验失败")
    api.summarize(suite,api.OLLAMA,api.MODEL,use_model=use_model)
    return suite
