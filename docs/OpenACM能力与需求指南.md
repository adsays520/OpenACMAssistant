# OpenACM 能力、电路参数与自然语言需求指南



## 1. 它到底可以做什么

2026-10-25：研究探索新增[ACCO两级入口](../skills/openacm-design/references/acco.md)。SRAM/multiplier的新版explore默认先搜索架构、复核入选项，再固定架构优化器件；下文单点参数和旧局部探索仍保留。SRAM总容量必须按`rows×cols×num_arrays`约束，并明确输出字宽，不能把16×8局部扫描当作bank研究。

OpenACM 主要面向近似数字存内计算（DCiM）的生成和评估，不是接收任意电路描述就能自动设计所有模拟/数字电路的通用系统。可分别使用其子模块，不必每次都运行整个 DCiM 后端。

| 模块 | 实际功能与入口 | 输入 | 输出与边界 |
|---|---|---|---|
| 精确整数乘法器 | `openacm/multiplier_compiler/nbit_exact.py` 的 `generate_Exact` | 位宽 | 无符号乘法 RTL；需独立功能验证 |
| 可配置近似整数乘法器 | `nbit_approx.py` 的 `generate_Appro_4_2` | 位宽、各可近似位置的压缩器 ID | RTL 与精度/硬件代价取舍；位置数由生成器计算 |
| 对数近似乘法器 | `nbit_log.py` 的 `generate_Log` | 位宽 | `Log_our_<n>bit.v`；源码含符号处理，参考数值语义需单独核对 |
| 浮点乘法器 | `DCIM/multiplier/Approximate_Floating-Point_Multiplier/` | 宽度/指数宽度或不同 approximation mode，取决于模块 | Exact、AC、EAFPU、MMBS、SSFPM 等独立 RTL；未自动接入 SRAM PE |
| 行为级 SRAM | `openacm/pe_compiler/SRAM.py` | 字宽、地址数量 | `sram.v` 与去掉 specify 的 `sram_syn.v`；这不是晶体管仿真 |
| DCiM PE | `openacm/pe_compiler/pe_compiler.py` | 字宽、地址数量、乘法器模块 | SRAM、控制、初始化和乘积输出集成；不是浮点 PE 或自动神经网络映射 |
| SRAM 电路仿真 | `openacm/sram_compiler/` 的 `SRAM_CONFIG`、`Sram6TCoreMcTestbench` | 阵列、6T 器件、外围器件、PVT、RC、操作、样本 | SPICE 网表、波形、读写延迟/功耗、hold/read/write SNM |
| 近似压缩器尺寸评估 | `openacm/compressor_sizing/` | 压缩器类型、分组晶体管 W/L | Xyce 测量、延迟/功耗及面积估计；本目录网表是简化 LEVEL=1 模型 |
| Liberty 生成 | `compressor_sizing/lib_generate/` | 网表和输入转换/负载等表征条件 | `.lib` 表征；需要核对实际模型和测量条件 |
| GNN 代理预测 | `multiplier_gnn/8-bitGNN`、`16-bitGNN` | 压缩器分配图 | MRED/NMED/Delay/Area/Power 估计；本机有对应 `.pth`，预测不是实测 |
| DCiM 两级优化 | `DCIM_OPT/run_optimization.py` | config/width 阶段、8/16bit 或压缩器、算法、精度与预算 | 候选/Pareto 结果；存在脚本和权重，不代表所有依赖已安装或一键全流程已验证 |
| SRAM 优化 | `SRAM-OPT/` 与 `openacm/optimization_algorithm/sram/` | 架构、器件空间、目标、迭代预算 | SA、PSO、SMAC、CBO、RoSE-Opt、NSGA-II、MOEA/D、MOBO 等研究实现 |
| ASIC 后端 | `flow_script_generator.py`、`DCIM/tcl/eda.sh` | RTL、SDC、Liberty、LEF、宏坐标和工艺平台 | 综合、布局布线、寄生提取、时序与功耗步骤；依赖工具、匹配宏和完整验证，不能称为已签核可流片 |

来源：[仓库说明](../../OpenACM/README.md)、[整数生成器](../../OpenACM/openacm/multiplier_compiler/multiplier_compiler.py)、[SRAM 主入口](../../OpenACM/openacm/sram_compiler/main_sram.py)、[优化入口](../../OpenACM/DCIM_OPT/run_optimization.py)。

## 2. 用户的需求应分成哪些部分

| 需求类别 | 例子 | 系统应如何处理 |
|---|---|---|
| 功能/结构 | 16行8列 SRAM；8位精确乘法器；使用 sabetz 的近似乘法器 | 选择电路和结构，生成对应设计 |
| 设计变量 | VDD=1 V；PD=180 nm；压缩器类型换成 kong2 | 修改任务配置或生成 RTL/网表 |
| 仿真条件 | TT、27℃；开启 RC；30个 Monte Carlo 样本 | 设置运行条件；样本真实不同，记录种子 |
| 验收约束 | 读延迟≤2 ns；read SNM≥0.2 V；MRED≤0.01 | 用仿真/测量结果判断通过与否 |
| 优化目标 | 在读延迟约束下使平均功耗最低 | 指定候选范围和预算，比较实际达标候选 |
| 预算/交付 | 只扫3个电压；输出 CSV 和波形；每任务600秒 | 控制资源，记录可复现结果 |

“32 KB 容量”并不能唯一决定行列、bank 数、端口、字宽或电压，应先确定这些结构。单点SRAM容量为`rows×cols` bit；ACCO bank容量为`rows×cols×num_arrays` bit。新版bank评价由单阵列SPICE和明确外围/面积模型组成，字段带_estimate；这不等于完整多bank版图或SPICE实测。

## 3. SRAM 可以提出哪些参数要求

### 结构、环境和测试

| 参数 | 单位/含义 | 内置对应 | 自动入口 |
|---|---|---|---|
| rows、cols | 行数、列数；容量为乘积 bit | `global.yaml` 的 num_rows/num_cols | 支持；行数2的幂，2–4096；每任务≤65536bit |
| vdd_v | V，供电 | vdd | 支持0.3–1.8；区间只作输入限制，不保证每个点电路可工作 |
| temperature_c | ℃ | temperature | 支持−40–125 |
| corner | TT/FF/SS/FS/SF | 对应 `tran_models/models_*.spice` | 支持 |
| operations | read/write/hold_snm/read_snm/write_snm | `run_mc_simulation(operation=...)` | 支持，可选择多个 |
| mc_runs、variation | 样本数、是否启用器件随机性 | use_mc/custom_mc 等 | 支持1–50；关闭 variation 时只运行1次 |
| vth_std、seed | 相对标准差、随机种子 | vth0/u0/voff | 支持；外部适配器生成可复现的独立 Gaussian 样本表 |
| wire_rc | 是否加入互连 RC | w_rc | 支持 |
| wire_res_ohm、wire_cap_pf | Ω、pF | pi_res/pi_cap | 支持；是模型输入，不是寄生提取结果 |
| column_mux | 是否使用列复用 | choose_columnmux | 支持；内置为2路，要求偶数列 |
| full_array | 全阵列实例或等效未选单元 | gen_unused_cells | 支持；等效模型与全阵列不能混称 |
| 目标地址 | 行、列位置 | target_row/target_col | 当前助手固定测试最后一行、最后一列 |
| 测试波形、预充电/字线时序 | s/ns | `BaseTestbench` 等 | 内置可编程；本次自动入口尚未暴露任意波形编辑 |

### 6T bitcell

| 自动字段 | 器件含义 | 单位 |
|---|---|---|
| pu_width_nm | 上拉 PMOS（PU）宽度 | nm |
| pd_width_nm | 下拉 NMOS（PD）宽度 | nm |
| pg_width_nm | 传输门 NMOS（PG/access）宽度 | nm |
| length_nm | 统一沟道长度 | nm |
| pu_model | PMOS_VTL / PMOS_VTG / PMOS_VTH | 模型名 |
| pd_model、pg_model | NMOS_VTL / NMOS_VTG / NMOS_VTH | 模型名 |

W/L 和阈值模型会共同影响读稳定性、写入能力、速度与功耗。不能只要求“越宽越好”，也不能认为提高 SNM 总会改善写入。输入值范围不等于工艺设计规则验证。

自动入口默认：16×8、1 V、27℃、TT、PU/PD/PG=90/90/135 nm、L=50 nm、PMOS_VTG/NMOS_VTG；这些尺寸取自 `config_sram.yaml` 的参考点。它们与现有 `sram_6t_cell.yaml` 的120/180/70 nm、L74nm配置不同。现有配置点的本机 write 测量出现 FAILED，保留在验证任务中；不能通过改报告值使它“通过”。

### 外围电路

内置 YAML 可设置 PRECHARGE、SENSEAMP、WRITEDRIVER、COLUMNMUX、WORDLINEDRIVER 和 DECODER 的 PMOS/NMOS 宽度、长度和阈值模型。字线驱动器与译码器还分 NAND/INV 器件组；例如译码器默认 PMOS 两组270nm、NMOS两组180/90nm。

这说明 OpenACM 本身能描述外围尺寸，但当前助手支持的自动字段范围以 [model-guide.md](../skills/openacm-design/references/model-guide.md) 为准。未暴露的字段会要求澄清，不会藏进文字备注并假称已修改。

### 可以要求哪些结果

读/写延迟、平均读/写功耗、静态/动态功耗原始样本、hold/read/write SNM、6T 单元估算面积、样本统计、波形及工艺角/参数候选比较。

延迟使用内置分段组合定义，例如 read 包括译码、预充电、字线驱动和位线摆幅项；不是直接量得的完整 SRAM 宏访问时间或系统 fmax。SNM 操作为单个目标 bitcell 的噪声裕量分析，不能当整阵列所有单元的良率证明。

原测试台的 PAVG/PSTC 记录 `V(VDD)*I(VVDD)`，通常为电源送出的负功率。助手保留原始有符号测量，并以其负数表示消耗功率；不会对任意错误值直接取绝对值。面积使用内置估算公式，不包含完整外围版图、布线和 bank 开销；该公式虽接收 L 参数，当前公式主体并未使用 L。

## 4. 乘法器和压缩器的参数需求

整数乘法器可要求位宽、exact/approx/log、压缩器编号或逐位置列表、固定种子采样/小位宽穷举，以及误差预算。8bit近似位置为9个，16bit为49个，其他位宽由生成器计算。

源码的编号为：1=ACCI1、2=kong2、3=antonio、4=momeni、5=ha、6=akbar1、7=akbar2、8=sabetz。编号1的 RTL 名称 ACCI1 和其他目录的 AP1 不能只凭 README 自动视为相同电路。

压缩器晶体管级尺寸向量的原工具维数：AKbar1/AKbar2为10、antonio/AP1/kong2为16、ha为14、momeni为12、sabetz为4。区分“逐门/逐组宽度向量”和“统一尺寸比例”。当前自然语言入口提供 nmos_scale、pmos_scale、length_scale 和 load_scale，支持所有八种模板，以及电压和温度。

评估指标包括 error rate、MAE、最大绝对误差、MRED、NMED；晶体管压缩器另测延迟、平均功耗和面积估算。当前数字入口通过 Icarus Verilog 运行实际 RTL；不会将 GNN 预测包装成真实仿真。

本次误差口径：MRED 排除参考乘积0；NMED=MAE/(2^n−1)^2。采样时含边界值及固定种子随机向量。log 源码包含符号处理，使用当前无符号参考时必须如实解释误差，不能将不同数值语义混合后直接用于误差保证。

浮点模块可要求总宽度/指数宽度（参数化模块）、近似 mode、保留的 mantissa 分段长度或 correction 版本。它们需要专用浮点参考与 NaN/Inf/舍入测试；已由floating入口独立接入，不把整数 bit_width 当作 FP32 参数。

## 5. DCiM、后端与优化需求

可表达 SRAM 深度、数据字宽、乘法器类型、宏位置、时钟周期、Liberty/LEF、工艺平台、误差上限和功耗/面积/延迟目标。宏结构发生改变时，SRAM 的 RTL、LEF 和 Liberty 必须一致；仓库现有16×8、32×16、64×32示例宏不能任意互换。

`DCIM_OPT/run_optimization.py` 实际有 config/width 两阶段与 NSGA/MOEAD/MOBO/SMAC，提供 max_mred/max_pdp 和各算法迭代/种群选项。默认研究预算可很大；直接运行默认 demo 不符合交互式自动任务的资源约束。GNN Torch/CUDA/PyG 等依赖不同于轻量 SPICE 环境，目前没有为助手安装整套训练/优化依赖。

当前自动 dcim 默认 backend=functional，生成 SRAM/PE/乘法器，完成乘法器误差检查及 PE 常数输入的初始化、RUN 和连续输出检查。选择 backend=asic 后，按实际存储实现增加综合、变化输入门级差分、floorplan/PDN、placement、CTS、routing、SPEF寄生提取、STA/SDF、后仿真和VCD活动功耗。读取 `reports/backend_stages.json` 和各阶段实际产物判断是否完成，不仅依赖shell退出码。

本机 `/usr/bin/sta` 2.6.0可用；`.bashrc` 的 `opensta` alias指向的路径不存在，而且非交互子进程不继承alias，助手直接查找可执行文件。完整后端使用16×8/32×16/64×32匹配宏，其它尺寸采用标准单元存储，clock_period_ns默认10ns、范围2–1000ns；较短周期可能无法满足时序，不能保证所有输入点达标。

验收指标：setup_wns_ns、hold_wns_ns均≥0、route_drc_count=0；功耗为routed_power_uw。routed_area_um2来自OpenROAD报告，包含宏与标准单元，不是SRAM单元公式。SRAM SPICE任务和后端现有Liberty是不同表征条件，未重新生成宏库；后端使用现有Nangate45 typical及SRAM参考库的模型条件。

Icarus不能完整解析该宏的总线SDF且不支持完整timing check，因此仅用于门级功能验证，SDF后仿真使用本机VCS与已配置许可证；静态时序单独报告setup/hold slack。VCS2018依赖已从新glibc移除的pthread_yield，外部适配器在任务内编译转发到sched_yield的兼容对象，不修改工具安装或内置代码。SRAM timing wrapper只在任务inputs中生成，与功能模型端口相同，声明可标注的路径和timing check。OpenSTA2.6可读取VCD，但没有新版本的详细活动覆盖报告命令，实际标注pin数量保留在日志，不能声称所有内部节点活动均来自仿真。OpenROAD详细布线DRC不替代独立全芯片DRC/LVS、宏内部版图或GDS签核。[OpenSTA功耗示例](https://openroad.readthedocs.io/en/latest/main/src/sta/doc/Examples.html)、[OpenRCX寄生提取](https://openroad.readthedocs.io/en/latest/main/src/rcx/README.html)

统一命令 `./openacm-assistant.sh full-flow` 依次运行DeepSeek规划和四类参考电路，将子任务集中在同一任务文件夹并汇总；`--without-model` 使用固定配置复现。已接入14个浮点变体，GNN训练和内置研究优化demo不自动运行。

## 6. DeepSeek 如何接入，以及原程序如何保护

```mermaid
flowchart LR
    A[自然语言需求] --> B[本地 DeepSeek 生成 JSON]
    B --> C[字段、类型、单位与预算校验]
    C --> D[创建独立任务与普通文件副本]
    D --> E[Landlock 限制仿真进程写入范围]
    E --> F[Xyce 或 Icarus 仿真]
    F --> G[原始数据、约束判断与报告]
    G --> A
```

DeepSeek 使用已部署的本地 `deepseek-script`，地址 `127.0.0.1:11435`。每次以此 skill 和参数参考作为上下文，使用 Ollama JSON schema 输出；输出不携带可执行 shell、Python 补丁或任意文件路径。执行器再次校验数据，发现无效字段可让模型纠正一次，仍无效则终止。[Ollama 结构化输出说明](https://docs.ollama.com/capabilities/structured-outputs)

对明确、无歧义的位宽、行列、W/L单位和穷举等要求，入口先将其绑定为 schema 中必须满足的值，防止模型在备注中说“穷举”而实际省略 exhaustive=true。连续交流时，同一电路的上一轮参数由执行器继承；每次执行前展示最终落实参数。

保护有三层：不提供原目录写入工具；所有运行使用普通文件工作副本；Linux Landlock 在仿真进程及其子进程中禁止任务目录以外的文件内容写入。只允许标准设备输出例外。隔离初始化失败时拒绝执行，不回退到无保护模式。[Linux Landlock 文档](https://docs.kernel.org/userspace-api/landlock.html)

每个任务前后检查原仓库文件的 SHA-256、大小、权限和修改时间，保存 `source_integrity.json`；排除 `.git` 元数据以避免外部 Git 操作引起假阳性，但写保护同样覆盖原仓库的 `.git`。这不是设置原文件权限、chmod 原目录或改写内置程序。

功能/SPICE任务600秒超时，完整后端1800秒总超时、各EDA阶段600秒，默认4线程；候选扫描最多8项，逐项运行。每次重试和后续修改创建新任务，不覆盖之前的结果。

## 7. 用户可以直接这样提出需求

- “做一个16行8列 SRAM，1 V、TT、27℃，PU90nm、PD90nm、PG135nm、L50nm，跑读写和三种SNM。”
- “在上一轮 SRAM 参数基础上，只把电压改为0.9 V，重新跑读写。”
- “扫描0.9、1.0、1.1 V，要求读延迟不超过2 ns，比较平均读功耗，选已达标的最省电候选。”
- “做8位精确乘法器，穷举所有输入。”
- “做8位近似乘法器，所有近似位置用sabetz，固定种子采样4096组，要求MRED≤0.02。”
- “把sabetz的NMOS和PMOS都加宽10%，1.2 V、27℃，重新跑Xyce。”
- “生成16×8的精确DCiM PE，运行功能检查。”

有歧义时会追问，例如“容量32K”究竟是32Kb还是32KB、“功耗低于100”使用何单位，以及“最优”允许多少候选/迭代。超出当前接口的任意程序改写、新拓扑或完整后端需求不会静默执行。

## 8. 专属文件组织

```text
OpenACMAssistant/
  README.md                         启动与使用
  docs/                             本文与验证结果
  app/                              外部适配器及执行器
  skills/openacm-design/             skill 与参数参考
  .venv/                            专属 Python 依赖
  examples/                         可复现 JSON 示例
  verification/                     写保护及输入校验验证
  runs/<时间_类型_ID>/
    request.txt / plan.json          用户需求与落实参数
    inputs/                         JSON、YAML、测试向量
    workspace/OpenACM/              普通文件工作副本
    artifacts/                      新网表/RTL/测量/波形
    logs/                           编译、仿真、失败信息
    reports/                        报告与工作副本变更记录
    result.json                     实测指标和约束判断
    source_integrity.json            原仓库完整性检查
```

规划阶段的模型请求和回复单独保存在 `runs/<时间>_planning_<ID>/`，参数扫描结果按 candidate_01 等归档。所有文件都在上述专属目录内。

状态 completed 表示选定仿真完成且已给出的约束通过；infeasible 表示仿真完成但不达标；unverified_constraints 表示要求的指标未测量；failed 表示执行或测量失败。没有验收约束的 completed 不承诺具体电路性能。

## 9. 多尺寸与设计空间探索（2026-10-10更新）

接口与默认值以[模型参数参考](../skills/openacm-design/references/model-guide.md)和app/spec.py为准。整数/DCiM允许4–64位，新增auto/native/tiled架构。原生生成器在4位、部分奇数精确位宽和超过32位有真实失败；auto使用独立8位分块适配器处理这些尺寸，不修改内置程序。分块位置数量不同于原生压缩树，必须按所选架构生成列表。Log限定8/16/32位。

DCiM完整后端扩展到16×8、32×16、64×32匹配宏，其它尺寸可以采用标准单元存储；没有伪造其它6T宏的Liberty/LEF。尺寸范围受到单点时间和存储容量预算限制，不承诺任意尺寸收敛。SRAM可调整六类外围组宽度倍率。浮点14变体另有RNE参考和边界测试，已有模板的边界缺陷如实报告。

探索入口：`./openacm-assistant.sh explore --workflow all --budget 64`，或聊天输入“探索8位近似乘法器，预算64个候选，MRED≤0.02”。每类保持容量、位宽和测试条件一致，由DeepSeek选择下一候选，真实测量后反馈。只有可行候选参与排名，保存Pareto、最优配置、模型决策和可续跑检查点。详见[探索接口](../skills/openacm-design/references/exploration.md)。

## 10. 持久经验与探索训练数据（2026-10-11更新）

新增本地 `experience/` 经验库：完成任务后自动记录完整参数、实测指标、失败原因与环境证据；新规划/探索检索相关观测，同范围且符合允许空间的成功配置可加入待验证候选。仍重新仿真和验收，不更新模型权重。可用 `memory stats/learn/query/export/import` 管理，并通过标准JSON/JSONL接入其它模型；经验文件不进入Git。详见[持久经验库](持久经验库.md)。

针对设计空间探索的训练，区分两类样本：代理模型使用“配置或电路图→统一条件下的实测指标”；LLM使用“用户目标、约束、已测历史、允许候选→经核验的下一步选择”。已有GNN预测器为8/16位，图特征文件和权重不等同于完整带标签训练集；GNN预测值不能充当新实测标签。数据字段、目标和缺失标签处理见[设计空间探索训练数据](设计空间探索训练数据.md)。

已下载50,000配置的公开8位近似乘法器数据，核验压缩器真值映射并接入DeepSeek检索。新探索可参考公开配置并实际复测；数据标签与本地测试条件不完全对应，不参与本地验收。数据、图和模型附件放在`datasets/`，没有更改原程序或更新模型权重。用`dataset status/query/verify`查看及验证，详见[公开数据接入](公开数据接入.md)。

## 11. 连续咨询与执行（2026-10-11更新）

能力解释、需求、历史分析和下一步建议在同一个设计对话中处理，不要求切换“咨询/需求”模式。每轮DeepSeek结合聊天上下文、当前草案和历史实测输出自然衔接与工具选择，实际数值和状态由证据原文呈现；模型不可用时回退到本地事实。可以问“你能干什么”“哪些参数能调”“MRED是什么意思”，也可以在一句话中要求解释并运行具体设计。

澄清保留原需求，补充电路类型后继续规划；`/plan`或“先给我看计划”保存草案，“开始运行”直接执行该草案。咨询不中断设计上下文，草案与已测结果分别保存。运行完成后可直接问“是否达标”“为什么失败”“最优配置是什么”；读取当前结果、约束和日志，不重跑。进一步咨询“怎么优化”先介绍允许变量与预算，明确提出探索任务才执行循环。详见[用户问题链](../skills/openacm-design/references/conversation-guide.md)。

“之前探索得到的数值可以做什麽”会读取匹配历史任务，说明条件一致时的比较筛选、Pareto权衡、增强验证、后续探索及训练样本整理。可随后说“解释刚才最佳配置，然后复测，改为穷举”：继承该历史任务的最佳已测配置，创建新任务。未达标探索没有最佳项时明确说明，不用失败项冒充最佳。已有任务名可直接写入消息；重新启动后仍可检索runs中的真实结果。
