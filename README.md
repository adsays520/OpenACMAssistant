# OpenACMAssistant

本地 DeepSeek 驱动的 OpenACM 电路仿真与设计空间探索助手。通过中文交流提出需求，自动整理参数，在独立副本中运行 EDA 工具，直接输出实测结果总结。原始 OpenACM 程序只读；无需 sudo。

支持 SRAM 和八种压缩器 SPICE、整数/浮点乘法器 RTL 与综合评估、DCiM 功能仿真及完整 ASIC 后端。能力范围和限制见 [能力与需求指南](docs/OpenACM能力与需求指南.md)，实测记录见 [验证记录](docs/验证记录.md)。

研究探索采用 **ACCO 两级流程**：在精度/容量约束下搜索架构，复核入选项，再固定架构优化器件。SRAM先确定bank组织，再执行器件与明确PVT/Monte Carlo；近似乘法器先用原8/16位PEA-GNN筛选，再以RTL、综合、STA活动功耗及单元真值复核。LLM、随机和原NSGA-II是可选策略。流程与边界见[两级探索](skills/openacm-design/references/acco.md)。

固定bank组织的器件复验使用[SRAM复验模式](skills/openacm-design/references/sram-retest.md)：锁定容量/行列/子阵列/复用比，逐尺寸逐PVT运行，并保留同条件基线、原始样本和失败记录。不会将32768bit容量当成乘法器位宽或继承旧乘法器对照任务。

## 安装

需要 Python 3.10+、Linux x86_64、Landlock ABI≥3，以及单独准备的 OpenACM 源目录和 EDA 工具。此仓库不包含 OpenACM 源码、模型权重或商业工具。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
export OPENACM_SOURCE=/path/to/OpenACM
export OPENACM_OLLAMA_URL=http://127.0.0.1:11435
export OPENACM_MODEL=deepseek-script
./openacm-assistant.sh doctor
./openacm-assistant.sh chat
```

默认源目录为本仓库旁的 `../OpenACM`。可设置 `OPENACM_PYTHON` 使用其它 Python 环境。Ollama 地址和模型名可切换；模型需要支持结构化 JSON 输出。已有本机部署可直接使用 `~/DeepSeekLocal/deepseek.sh start`。

Xyce 用于 SPICE；Icarus/vvp 用于 RTL；Yosys 用于综合；完整 DCiM 后端还需要 OpenROAD、OpenSTA（PATH 中的 `sta`/`opensta`）、GCC 和有可用许可证的 VCS。bash alias不会被 Python 子进程继承。依赖缺失时保留错误，不假称仿真通过。

ACCO附加CPU依赖：`.venv/bin/pip install -r requirements.acco.lock.txt`。SRAM bank模型需可读的OpenYield源目录，默认`../OpenYield/OpenYield`，可通过`OPENYIELD_SOURCE`指定。原NSGA-II、GNN和压缩器尺寸适配器从OpenACM的`DCIM_OPT`复制；执行受Landlock约束，原目录不写入。没有sudo要求。

## 语言交流

```text
你能干什么？
8位乘法器有哪些参数？MRED是什么意思？
做8位近似乘法器，所有近似位置用sabetz，固定种子采样4096组，要求MRED≤0.02。
做16行8列SRAM，1V、TT，PU90nm、PD90nm、PG135nm、L50nm，跑读写和三种SNM。
其余不变，只把电压改为0.9V，重新仿真。
做FP16的Exact浮点乘法器，采样256组。
探索8位近似乘法器，预算64个候选，要求MRED≤0.02。
探索SRAM，总容量4KB，输出字宽8位，预算32个候选。
局部探索16行8列SRAM，读延迟≤0.5ns，read SNM≥0.2V，平均读功耗最小。
```

`/plan 需求`只查看计划，`/new`清除上下文，`/exit`退出。启动界面提供 OPENACM 字符画及 `neon/ocean/sunset/mono` 主题；使用 `/theme ocean` 切换，`NO_COLOR=1` 禁用颜色。

聊天只呈现自然回复、缺项问题、可读计划、真实运行进度和结果；不打印意图识别、需求解析或模型思考过程。原始模型回复与结构化配置保留在任务审计文件，chat的计划预览用自然语言展示，CLI的`--plan-only`仍可输出JSON。

每轮`answer.md`记录工具处理后的最终回复，计划另存`final_plan.json`；模型中间选句单独保存在`dialogue_answer.md`。更新代码后需要退出旧chat并重新启动，本地会话可恢复。

直接粘贴测试条件也会保存，即使没有“做/运行”等动词。若缺电路类型或位宽，只问这一项；后续补齐时沿用已给采样、种子、负载与预算。chat将当前对话保存到本地`conversations/current.json`，退出重启后可继续；`/new`清空对话上下文，已有任务和经验保留。`ask`仍为独立请求，不自动采用其它聊天的设计。

交流使用统一的上下文：DeepSeek同时看到当前问题、聊天记录、设计草案和历史实测，一轮可以解释、提出建议并调用参数/仿真工具。例如“之前探索的数值可以做什麽”会结合历史探索说明比较筛选、Pareto权衡、复测、继续优化与训练数据用途；随后可以说“解释刚才最佳配置，然后复测，改为穷举”。运行工具仍校验明确要求和参数，不因话里出现“探索”就启动循环。历史任务可通过名称指定，未指定时读取匹配的最近真实任务，跳过验证/对话审计目录。

`/plan`或“先给我看计划”保存草案，随后“开始运行”直接执行；“其余不变，只改…”保留设计条件和验收约束。结果追问读取实测与日志，草案不能被称为已达标。模型连接或回复无效时回退到本地事实并保留上下文。CLI也可用`ask '你能干什么'`。详见[对话参考](skills/openacm-design/references/conversation-guide.md)。

“开展/进行/启动…架构搜索”会被识别为任务要求；“如何开展”“先不要运行”仍是咨询。8/16位近似乘法器架构对照已提供共同候选池、独立搜索种子和批量执行：先说对照对象，补充每轮预算及重复次数，即可生成实验。详见[架构搜索对照](docs/架构搜索对照实验.md)。经验开关消融尚未接入，不与已支持的方法对照混称。可明确说“改成单次探索…”转为普通ACCO。

按参赛论点组织的[实验1至6可复制输入](docs/experiment-plan/2026-10-27/实验1至6_可复制输入.md)包含逐轮文本、完整任务书、预期证据与当前执行范围。

新版[参赛视频拍摄流程](docs/video-demo/2026-10-30/新版参赛视频拍摄流程.md)突出自由探索，附6分钟分镜、解说、可复制输入与素材索引；拍摄问句已完成真实模型验收，并实际验证4位精确乘法器短流程。

DeepSeek 整理需求和总结重点；实际数字、验收和最佳候选由执行器依据测量填写。无效或截断的计划自动重试一次；仍无效时停止执行。总结服务不可用时直接输出实测总结。

## 仿真与探索

```bash
./openacm-assistant.sh acco --spec examples/acco-sram-smoke.json --plan-only
./openacm-assistant.sh acco --spec examples/acco-sram-4kb.json
./openacm-assistant.sh acco --spec examples/acco-multiplier-smoke.json
./openacm-assistant.sh compare --spec examples/compare-multiplier-8bit.json --plan-only
./openacm-assistant.sh compare --spec examples/compare-multiplier-smoke.json
./openacm-assistant.sh ask '做8位精确乘法器，穷举所有输入'
./openacm-assistant.sh run examples/dcim-full.json
./openacm-assistant.sh summarize runs/<任务目录>
./openacm-assistant.sh full-flow
./openacm-assistant.sh explore --workflow all --budget 64 --max-seconds 3600 --legacy
./openacm-assistant.sh explore '探索16位近似乘法器，MRED≤0.02' --budget 128
./openacm-assistant.sh explore --spec examples/explore-multiplier.json --budget 64
./openacm-assistant.sh explore --resume runs/<单类探索目录> --budget 256
```

SRAM/近似乘法器新探索默认两级ACCO；显式旧JSON、历史resume、`--legacy`或“局部探索”保持旧单级格式。ACCO分别记录架构、入选复核和每个入选项的器件预算，max_seconds约束整管线。预测用predicted_*，bank外围/面积用*_estimate；仅实测精度通过的乘法器进入器件阶段。SRAM必须给总容量和输出字宽，不能用默认小阵列替代容量规格。完整ACCO暂未开放中途续跑，已完成阶段与超时记录保留。

旧单级循环逐次反馈实测数据，默认64候选并保留Pareto；数字局部探索默认测综合面积，backend=asic的DCiM局部探索跑完整后端。单级局部结果不能当作论文两级协同优化已完成。

每次运行放入 `runs/<时间>_<任务>_<ID>/`，包含配置、普通工作副本、产物、日志、报告和源文件完整性校验。旧单级探索额外保存 `best_plan.json`、`history.csv`、逐轮模型决策和可续跑检查点。`--without-model`使用固定种子的本地搜索；不会把它标为 LLM 决策。

ACCO使用`stages/`保存架构/验证/器件父子关系，`decisions/`保留LLM请求与回复，`reference_manifest.json`记录原代码/权重SHA，`observations.jsonl`导出模型无关经验。SRAM最佳项用`best_experiment.json`复验，保持总容量/PVT；完整结果可直接追问或summarize。器件模板目前为简化LEVEL_1，没有将定制单元重新表征并回灌完整PE；应用PSNR/NN精度和论文全部实验尚未自动接通。

## 持久经验积累

实际结果自动记录到专属 `experience/`，新规划和探索检索历史配置、指标及失败原因。匹配测试范围的成功设计可成为待验证候选；旧记录或不同条件只作参考。经验积累不更新模型权重，原始OpenACM仍只读。

```bash
./openacm-assistant.sh memory learn          # 首次导入本机历史，重复执行不重复入库
./openacm-assistant.sh memory stats
./openacm-assistant.sh memory query --spec examples/explore-multiplier.json
./openacm-assistant.sh memory export         # experience/exports/observations.jsonl
```

聊天 `/memory` 查看统计。SQLite本地索引及标准JSON/JSONL接口不依赖DeepSeek，换模型可继续共用；外部导入只作未验证参考。文件、证据规则与其它模型接入见[持久经验库](docs/持久经验库.md)。经验目录不进入Git。

## 公开电路数据

公开8位乘法器数据可独立下载到`datasets/`并核验。接入后，DeepSeek在规划和探索时检索配置/标签参考，候选仍重新仿真；不会更新模型权重或将公开标签当本地实测。用`dataset fetch`、`dataset prepare`、`dataset status`管理，用`dataset verify`验证实际模型与仿真接入。完整命令、对照结果和限制见[公开数据接入](docs/公开数据接入.md)。

## 尺寸与实现

- SRAM：行数2的幂，2–4096行，1–4096列，单点最多65536bit；单列内置RC有分母限制，需 `wire_rc=false`。参数范围只表示接口允许，电路能否工作由 SPICE 判定。
- 整数/DCiM：4–64位，支持逐位置压缩器配置；`architecture=auto/native/tiled`。auto对已发现有问题的原生尺寸采用外部分块适配器，组合未经改动的8位模块；具体位置数取决于架构。原生 Log 只开放8/16/32位，按无符号参考报告误差。
- DCiM：`memory_impl=auto/macro/standard_cells`。auto选择16×8、32×16、64×32匹配宏，其余尺寸综合为标准单元存储。标准单元存储不等于6T SRAM宏。
- 浮点：Exact和AC暴露源码格式参数；EAFPU、MMBS5/6/7及八个SSFPM变体固定FP32。采用独立RNE参考、正常数和零/非规格化/NaN/Inf边界检查。源码名称Exact不能替代IEEE正确性验证。

所有最优结论仅针对本次有限实测候选；采样精度不是穷举保证。综合面积不是布线面积；SRAM/压缩器公式面积是估算。完整后端包含综合、门级差分、PDN、布局布线、寄生提取、STA、SDF后仿真和VCD活动功耗，仍不等于流片签核。

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_invariants.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_planner_recovery.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_summaries.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_exploration.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_experience.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_public_dataset.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_dialogue.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_response_contract.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_chat_output.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_search_intent.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_benchmark.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_benchmark_methods.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_requirements_continuation.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_sram_retest.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_acco.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_sizes.py --all-widths
```

前四项覆盖输入校验、真实 Landlock 隔离、计划重试、结果总结、探索续跑及浮点参考；其中结果总结和计划重试使用仓库附带的小型夹具，基本回归不要求安装 EDA 或部署 DeepSeek。check_acco需要可读的原OpenACM/OpenYield代码，用于核对架构构造器和原NSGA-II的小种群行为，不启动EDA。尺寸验证需要本机 EDA/OpenACM 环境，失败尺寸也会保留记录。本次两级实测范围见[验证记录](docs/验证记录.md#2026-10-25论文方向校正与acco两级探索)。完整仿真目录、虚拟环境、模型和私密配置不进入 Git。

真实本地模型的文档示例验收：`.venv/bin/python -B verification/verify_examples_live.py`。这项检查逐轮核对自然回复和完整计划，包含重启、补充条件及切换电路，禁止执行EDA。范围、记录和单场景复查方法见[对话验收](docs/对话验收.md)。

领域 skill：[openacm-design](skills/openacm-design/SKILL.md)。DeepSeek 入口直接读取该 skill 和参数参考，不要求模型原生支持 skill/tool calling。
