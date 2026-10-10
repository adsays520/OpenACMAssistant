# OpenACMAssistant

本地 DeepSeek 驱动的 OpenACM 电路仿真与设计空间探索助手。通过中文交流提出需求，自动整理参数，在独立副本中运行 EDA 工具，直接输出实测结果总结。原始 OpenACM 程序只读；无需 sudo。

支持 SRAM 和八种压缩器 SPICE、整数/浮点乘法器 RTL 与综合评估、DCiM 功能仿真及完整 ASIC 后端。能力范围和限制见 [能力与需求指南](docs/OpenACM能力与需求指南.md)，实测记录见 [验证记录](docs/验证记录.md)。

## 安装

需要 Python 3.11+、Linux x86_64、Landlock ABI≥3，以及单独准备的 OpenACM 源目录和 EDA 工具。此仓库不包含 OpenACM 源码、模型权重或商业工具。

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

## 语言交流

```text
做8位近似乘法器，所有近似位置用sabetz，固定种子采样4096组，要求MRED≤0.02。
做16行8列SRAM，1V、TT，PU90nm、PD90nm、PG135nm、L50nm，跑读写和三种SNM。
其余不变，只把电压改为0.9V，重新仿真。
做FP16的Exact浮点乘法器，采样256组。
探索8位近似乘法器，预算64个候选，要求MRED≤0.02。
全工程设计空间探索，预算64个候选。
```

`/plan 需求`只查看计划，`/new`清除上下文，`/exit`退出。启动界面提供 OPENACM 字符画及 `neon/ocean/sunset/mono` 主题；使用 `/theme ocean` 切换，`NO_COLOR=1` 禁用颜色。

DeepSeek 整理需求和总结重点；实际数字、验收和最佳候选由执行器依据测量填写。无效或截断的计划自动重试一次；仍无效时停止执行。总结服务不可用时直接输出实测总结。

## 仿真与探索

```bash
./openacm-assistant.sh ask '做8位精确乘法器，穷举所有输入'
./openacm-assistant.sh run examples/dcim-full.json
./openacm-assistant.sh summarize runs/<任务目录>
./openacm-assistant.sh full-flow
./openacm-assistant.sh explore --workflow all --budget 64 --max-seconds 3600
./openacm-assistant.sh explore '探索16位近似乘法器，MRED≤0.02' --budget 128
./openacm-assistant.sh explore --spec examples/explore-multiplier.json --budget 64
./openacm-assistant.sh explore --resume runs/<单类探索目录> --budget 256
```

探索循环逐次反馈实测数据，由 DeepSeek 从经校验的候选菜单中选择下一配置。每类默认64个候选，可提高到10000；时间预算和候选预算共同生效。只有约束全部达标且目标已测量的候选参与排名，同时保留 Pareto 候选。默认数字探索测量综合映射面积；指定 `backend=asic` 的 DCiM 探索实际执行完整后端。位宽、容量、随机种子、测试向量和模型条件保持一致。

每次运行放入 `runs/<时间>_<任务>_<ID>/`，包含配置、普通工作副本、产物、日志、报告和源文件完整性校验。探索额外保存 `best_plan.json`、`history.csv`、逐轮模型决策和可续跑检查点。`--without-model`使用确定性选择；不会把它标为 LLM 决策。

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
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python verification/check_sizes.py --all-widths
```

前三种结果解析与探索回归测试使用随仓库提供的少量真实结果夹具；尺寸验证需要本机 EDA/OpenACM 环境，失败尺寸也会保留记录。完整仿真目录、虚拟环境、模型和私密配置不进入 Git。

领域 skill：[openacm-design](skills/openacm-design/SKILL.md)。DeepSeek 入口直接读取该 skill 和参数参考，不要求模型原生支持 skill/tool calling。
