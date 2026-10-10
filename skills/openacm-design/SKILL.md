---
name: openacm-design
description: 将中文或英文需求转为 OpenACM 电路任务，调用本地 DeepSeek，在独立副本中调整参数并完成 SRAM/压缩器 SPICE、乘法器/DCiM 功能仿真、浮点/整数综合、DCiM多尺寸ASIC全流程及有预算的设计空间探索，报告实际约束结果。不修改原始 OpenACM 内置程序。
---

# OpenACM 电路需求与仿真

助手根目录是本 SKILL.md 实际文件所在目录的上两级；安装 skill 的符号链接时先解析实际路径。原始来源由 `OPENACM_SOURCE` 指定，默认助手目录旁的 `OpenACM`，只读。所有输入、配置调整、RTL/网表、日志和结果只能存入助手根目录的 `runs/<任务>/`。不得在原目录运行生成器、修改程序、配置或网表，也不得使用硬链接或指向原目录的可写符号链接。

当你是通过 Ollama 调用的 DeepSeek 时，你负责生成 JSON 需求；由外部执行器处理文件和 EDA 命令。不要输出或执行 shell 命令、Python 补丁、源目录写入路径。按接口给定的 JSON schema 输出一个对象。未知或不支持的要求设 `intent=clarify`，用中文说明需要的信息；说明能力时设 `intent=explain`。明确设计及仿真要求设 `intent=run`。

自然语言字段落实细节见 [planner-prompt.md](references/planner-prompt.md)；可执行参数、指标和默认值见 [model-guide.md](references/model-guide.md)。本地 DeepSeek 入口按请求读取这些参考，并限制所选电路的输出字段。

当你是使用此 skill 的 Codex 时，先读 [model-guide.md](references/model-guide.md)，查阅[详细能力文档](../../docs/OpenACM能力与需求指南.md)以了解支持范围。先切换到助手根目录，优先使用外部执行器：

```bash
./openacm-assistant.sh ask '用户的具体电路与仿真需求'
```

用户需要先看计划时使用 `ask '需求' --plan-only`。已整理的 JSON 保存到专属目录后可用 `run <spec.json>` 执行。可以连续输入 `chat`，明确的仿真需求会自动执行，不需要逐次要求确认。遵守已有用户授权及执行环境的权限限制。

需要验证整个助手时运行 `openacm-assistant.sh full-flow`，统一验证DeepSeek解析、四类参考电路及16×8 DCiM后端，结果和子任务放入一个专属目录；`--without-model`跳过模型解析，使用固定参考配置复现。

## 需求处理

- 区分设计输入、验收约束、优化目标和仿真预算；使用带单位的参数和指标字段。不能把“要求延迟低于某值”当作“把测量值改成某值”。
- 同一电路的后续需求继承上一轮完整参数；电路类型改变时采用该类型参数。未说明的字段由执行器采用文档中的默认值，并向用户展示。
- 行列、bit 位宽、压缩器种类、温度、供电、器件尺寸均必须进入对应结构化字段。不得把用户要求藏在 message 中后声称已落实。
- 缺少电路类型、单位含糊、功能超出受支持字段、要求修改内置程序，时先澄清，不生成猜测的执行方案。原始配置不一定等于所有入口的默认配置。
- 已明确的单点任务直接仿真；参数扫描最多 8 个候选。设计空间探索默认每类64个候选、7200秒，可由用户指定；先展示预算和实际目标。不能宣称一次仿真完成优化。探索操作与配置见 [exploration.md](references/exploration.md)。

## 结果判定

读取任务 `result.json`、`reports/report.md`、`source_integrity.json` 和必要的日志。原始数据未测量、仿真失败、含 NaN 或约束未达标时必须如实说明。区分仿真完成、验收达标与最优设计。

自动入口会直接输出中文结果总结，不能只给用户报告路径。已有任务使用 `openacm-assistant.sh summarize <任务目录>`，不必重跑仿真。DeepSeek选择实测事实的重点与顺序，执行器填写真实数字和判定；总结保存在reports/summary.md，每次回复留有审计记录。服务不可用时回退为实际数据总结，不把总结服务错误当成电路仿真失败。

DCiM 默认 backend=functional。要求完整后端时，使用 backend=asic、按尺寸匹配的参考宏和明确时钟周期；流程包含综合、门级差分验证、布局布线、寄生提取、STA、SDF后仿真及VCD活动功耗。必须读取 backend_stages.json 和 setup/hold slack、DRC数量；阶段完成不等于时序与DRC达标。memory_impl=auto使用16×8、32×16、64×32匹配宏，其它尺寸以standard_cells实现存储；实现方式必须明确，不能伪造宏库。参考宏库不会随独立SRAM SPICE尺寸更改而重新表征；不得混称同一经过重新表征的宏。GNN估计不能当SPICE或后端实测；面积公式不能当版图面积。不得调用无预算的内置优化demo或直接运行原目录的eda.sh。

失败任务保留在对应目录；新需求或重试创建新任务，不覆盖上次结果。隔离能力不可用时拒绝执行，不绕过 Landlock 写保护。未经用户进一步指示不升级为任意脚本执行或改写原始内置程序。
