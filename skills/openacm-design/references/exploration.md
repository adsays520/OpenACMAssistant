# 单级局部探索与历史兼容

论文方向的SRAM/近似乘法器新探索默认走[ACCO两级入口](acco.md)。本文描述旧plan/space/objectives格式、明确“局部探索”或--legacy及历史resume；其结果不能代表完整两级协同优化。全工程旧套件仍是各工作流的独立局部探索。

使用`explore`或聊天中的“探索/设计空间/架构优化/loop”触发。每类默认64个候选、7200秒；用户可指定1–10000个候选及10–604800秒。展示预算和目标后自动执行，尊重现有授权。

```bash
./openacm-assistant.sh explore --workflow all --budget 64 --max-seconds 3600
./openacm-assistant.sh explore '探索8位近似乘法器，MRED≤0.02' --budget 128
./openacm-assistant.sh explore --spec examples/explore-multiplier.json
./openacm-assistant.sh explore --resume runs/<单类探索目录> --budget 256
```

每轮由执行器生成有限、合法、尚未运行的候选菜单，DeepSeek读取实测历史后选择下一项。菜单含模板覆盖、当前最佳项附近的单参数变动和多参数组合；模型不能输出任意代码。不可用时明确记录fallback，使用经校验的候选。`--without-model`主动关闭模型。

配置JSON包含`plan`（普通run计划）、`space`（参数到有限值列表）、`objectives`（1–4项`{metric,direction}`）。第一目标排名，其余目标打破同值并计算Pareto。约束均来自实际任务结果；失败、未测量、NaN或未达标候选不得成为最佳。

SRAM保持容量不变，`shape:[[16,8],[8,16]]`同时调整行列。整数/DCiM保持位宽和容量；所有任务固定种子、样本集合、温度、模型、负载及验证模式。不要通过改变规格或测试环境取得“改善”。单独比较尺寸需建立不同规格的探索任务。

数字探索默认backend=synthesis，面积来自Nangate45映射。DCiM的auto选匹配宏，其它尺寸标准单元存储；backend=asic时每个候选跑完整后端，使用路由面积/功耗。SRAM及压缩器跑实际SPICE；公式面积仍标为估算。

单类任务保存`config.json`、`result.json`、`history.csv`、`best_plan.json`、`report.md`，候选放入`candidates/`，模型请求/回复放入`decisions/`。每轮原子更新检查点；续跑不覆盖未完成尝试。达到候选/时间预算、空间确实耗尽、异常或中断时停止。最佳结果只对应有限候选；采样精度不是穷举证明。

直接给用户中文总结：执行多少个候选、最佳配置与目标实测值、约束是否满足、Pareto及停止原因。没有达标项时明确说明，保留失败数据，不推荐虚构的最优。
