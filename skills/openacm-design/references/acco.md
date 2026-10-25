# ACCO 两级探索

研究入口以 DATE 的编译/评价基础和 DAC 的 Accuracy-Constrained Co-Optimization 为依据。论文对照见[方向记录](../../../docs/research/2026-10-25_论文对照与探索方向.md)。普通单点仿真继续使用run；新的SRAM/近似乘法器explore默认走两级入口，显式旧spec、resume或`--legacy`/“局部探索”保留原单级方式。

## 使用

```bash
./openacm-assistant.sh acco --spec examples/acco-sram-smoke.json --plan-only
./openacm-assistant.sh acco --spec examples/acco-sram-4kb.json
./openacm-assistant.sh acco --spec examples/acco-multiplier-smoke.json
./openacm-assistant.sh acco --spec examples/acco-sram-smoke.json --without-model
./openacm-assistant.sh explore '探索SRAM，总容量4KB，输出字宽8位，预算32个候选'
./openacm-assistant.sh explore '探索8位近似乘法器，MRED≤0.02，预算32个候选'
```

`--without-model`只把llm求解器切换为random，原nsga2选择仍运行原NSGA-II。不能把它标成DeepSeek决策。`/plan`探索需求保存完整实验；“开始运行”使用该实验。缺少SRAM总容量/输出字宽时先补充规格，不把默认16×8当总容量。

自然语言支持“架构用NSGA-II，器件用随机”“采用LLM”及“限时10分钟”。预算和求解器写入完整实验配置；用户没有给阈值时，不能由模型自行添加验收上限。

## 配置与预算

`schema_version=1`，workflow=sram/multiplier，plan是原单点计划。其它字段：

- `architecture_space`：SRAM为rows/cols/mux_ratio；乘法器为compressor_types，可用fixed_assignment锁定逐位置分配。不能把器件尺寸放进架构域。
- `device_space`：SRAM为PU/PD/PG与外围宽度倍率；乘法器为width_scale，对原模板各共享宽度组独立取值。固定逻辑、沟道长、架构与容量。
- `budgets`：architecture为架构评价数；validation为乘法器入选实测上限；shortlist为进入器件阶段的架构数；device_per_parent为每个入选组织/单元的器件候选数，包含同条件baseline；max_seconds为整管线墙钟上限。单个器件候选可能需要多个PVT/MC或PPA/真值仿真，不能把候选预算当作底层SPICE调用总数。
- `solvers.architecture/device`：llm、random、nsga2。nsga2调用原DCIM_OPT的NSGA-II普通文件副本，初始种群在适配层去重；唯一评价缓存与外层进程超时约束其运行。不声称接入了尚未适配的MOEA/D、SMAC或MOBO。
- `architecture_objectives/device_objectives`：分别明确阶段目标；默认SRAM为FOM，乘法器架构为预测PDP/MRED、器件为单元PDP/面积。plan的明确目标在自然语言入口优先保留。

## SRAM 评价

要求capacity_bits与output_width。架构满足`rows*cols*num_arrays=capacity_bits`，原OpenYield的架构构造器在副本中接收这些值；不使用旧脚本32KB硬编码。mux_ratio当前只开放实际电路支持的1/2，不能宣称任意复用比。

架构阶段固定名义器件/PVT；入选后固定组织，进行器件搜索，按robustness.cases列出的corner/temperature_c/vdd_v以及mc_runs/vth_std实际执行SPICE。每个尺寸候选先后使用同一组指定条件与种子；保存每角原始样本。任何失败样本都不作为达标点，有限MC不能证明整阵列稀有失效率。

评价采用显式混合模型：单阵列延迟/功耗/SNM来自OpenACM SPICE；bank片选/复用延迟和未激活阵列漏电按OpenYield风格估算；面积调用原OpenYield的estimate_scaled_array_area。不会把整个单阵列动态功耗乘阵列数，也不重复加入已在SPICE里的2路列MUX。输出不够字宽时记录串行周期，模型不承诺并行吞吐。

`fom_arch_estimate=-log10(Pmax*sqrt(Abank)*Dmax)`，固定W/m²/s单位；Pmax/Dmax取读写最差值。器件阶段默认`fom_robust_estimate=log10(minSNM/(Pmax*sqrt(Abank)*Dmax))`，是本助手明确采用的工程公式，不能称为DAC给出的唯一原式。跨角对SNM取最小、功率/延迟取最大后重算FOM。

所有bank字段以`_estimate`标明来源。不能把cell_area_um2当bank面积；也不能将这些估算称为完整bank SPICE、内部宏版图、DRC/LVS或签核。

## 乘法器评价

仅原生8/16位近似压缩树可使用已有PEA-GNN；其它尺寸继续单点/局部验证，不能外推权重。助手对照GNN特征与原RTL的16组真值，建立1–8编号到GNN编号的映射，保留位置顺序。记录权重、图辅助代码和归一化文件SHA256。Power按原缩放文件W转µW，PDP单位fJ。

架构预测采用`predicted_*`命名空间和`evidence_kind=prediction`。入选项重新运行RTL、Nangate45综合、门级对照、OpenSTA延迟及VCD活动功耗。活动输入周期与配置clock_period_ns一致，负载为output_load_ff，必须有有效活动标注；这仍是综合后库模型评价，不是布线后测量。

只对实测精度达标项进入器件级。通过原DCIM_OPT/compressor_sizing的逐组宽度接口优化所用单元，并将每个候选的16组电压输出与该近似RTL真值表比较。当前模板为简化LEVEL_1，面积为模板估算；该阶段没有PVT/MC签核。模板与RTL不兼容或有电平失败时拒绝推荐。

没有把定制单元重新表征为Liberty并回灌乘法器/PE，因此不能把单元PDP改善当整PE改善，不能继续套旧GNN证明定制库的收益。图像PSNR、NN Top-1/Top-5及完整论文实验复现尚未自动接入。

## 结果与复用

任务全部放在`runs/<时间>_acco_<工作流>_<ID>/`：config、reference_manifest、原文件SHA/完整性、references普通副本、stages父子记录、逐角任务、decisions模型请求/回复、reports与observations.jsonl。预测、数组SPICE、bank估算和单元SPICE不混记来源。

执行结束自动整理中文摘要，包含阶段、验收及器件已评价最佳项；也可用summarize读取，不能只给路径。SRAM最佳项保存best_experiment.json，保持总容量/输出字宽/PVT和器件参数，可用acco --spec复验；不降成单阵列run。乘法器best_plan.json和best_experiment.json锁定已验证的原逻辑架构，不包含未回灌的定制库；器件尺寸另存best_device_designs及各候选widths.json。实际数组/RTL任务自动进入原经验索引，完整ACCO阶段另以模型无关JSONL导出。

当前没有开放ACCO中途续跑；阶段记录用于审计，超时显示incomplete，不声称已完成所有阶段。新实验使用新目录，旧单级任务仍用原resume。比较LLM/随机/NSGA-II时使用同一规格、评价模型、初始baseline、候选与时间预算、多种子重复，分别报告GNN次数、真实数组/RTL任务、STA、器件候选及LLM调用，不能仅凭本次冒烟测试宣称某策略更优。
