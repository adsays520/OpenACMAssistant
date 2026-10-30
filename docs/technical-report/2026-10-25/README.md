# 参赛技术报告 V1.0

日期：2026-10-25（北京时间）。技术基准：助手提交`8566e1a`。本报告根据已有源码与本机证据整理，没有重新启动EDA、设计探索或模型推理，没有修改原始OpenACM/OpenYield和既有任务。

- [PDF阅读版](OpenACMAssistant_技术报告.pdf)：正式排版，适合阅读与提交。
- [Word编辑版](OpenACMAssistant_技术报告.docx)：可继续补充参赛队伍、比赛模板或学校信息。
- [Markdown源稿](OpenACMAssistant_技术报告.md)：正文、表格及图片引用。
- [证据快照](evidence_snapshot.json)：用于表图的任务配置、实际指标、状态、完整性与文件哈希。
- [交付校验清单](manifest.json)：报告、图表及来源文件的生成校验信息。

内容覆盖OpenACM内核、助手架构、连续对话、乘法器/SRAM两级探索、隔离执行、经验与公开数据、实际验证、亮点、部署及后续研究。正文包含4张图和11张表。原内核与本作品新增功能分别说明，预测、估算与仿真结果明确标记。

`assets/`保存PNG、矢量SVG和SRAM图表CSV；`tools/`保存生成脚本；`build/`仅保存PDF检查文本、页面预览及LibreOffice独立配置，不作为报告交付内容。两份论文PDF、原模型权重和完整任务数据不随本目录分发。

## 重建与校验

在助手根目录执行。生成器读取本机已有实验和原论文文件；需要保持证据路径可读。仅阅读PDF/DOCX不需要这些依赖。

```bash
.venv/bin/pip install -r docs/technical-report/2026-10-25/requirements-report.txt
.venv/bin/python -B docs/technical-report/2026-10-25/tools/build_report.py
libreoffice -env:UserInstallation=file:///tmp/openacm-report-lo --headless --convert-to pdf \
  --outdir docs/technical-report/2026-10-25 \
  docs/technical-report/2026-10-25/OpenACMAssistant_技术报告.docx
.venv/bin/python -B docs/technical-report/2026-10-25/tools/build_report.py --audit-only
```

生成使用本机Noto CJK字体和LibreOffice。字体或排版引擎变化可能造成分页变化。生成器核对本文对应的核心源文件版本；若实现已变更，应先更新报告结论和技术基准，再重建，不能沿用旧证据冒充新版本验证。

最终PDF另经文本提取和页面预览检查；检查结果保存在`manifest.json`，中间文件留在`build/`。
