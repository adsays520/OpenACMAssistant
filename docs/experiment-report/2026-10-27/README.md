# E1—E6问答与实验佐证报告

整理日期：2026-10-28。目录沿用实验清单的2026-10-27编号。

- [可编辑Word报告](OpenACMAssistant_E1-E6问答与实验佐证报告.docx)
- [同内容Markdown](OpenACMAssistant_E1-E6问答与实验佐证报告.md)
- [本轮12条实际问答](evidence/live_chat_2026-10-28/dialogue.md)
- [数据快照与任务索引](evidence_snapshot.json)
- [报告核对结果](validation.json)

正文含E1—E6分析、7幅图、数据表与明确的失败/未完成说明；附录保留本地助手的最终回复原文。原始OpenACM程序和用户原Word文稿未修改。

本轮真实启动两个sabetz乘法器任务（4096/8192组，均不满足MRED≤0.02），以及16×8、10ns精确DCiM完整后端（65536组零错误，12个后端阶段完成，时序/布线DRC约束通过）。8192组由问答生成计划后通过官方run入口显式补测，原对话没有自动运行这一轮。

E2使用本机已有的三方法五重复结果，14/15轮完整完成；E5复用4KB超时及64bit小规模记录，不重新启动同一批90分钟任务。E3经验消融和E4独立20配置实验尚未完成。补充预测—实测图来自E2选中的17个唯一配置，不能称独立验证集。

报告保留发现的对话问题：8192组请求停在计划、草案遮挡明确历史任务、E4回复混用其他任务指标。正文数据从指定任务JSON读取，未使用错误回复中的指标。

`evidence/20261027_220923_dialogue_317a6d`是先前被执行环境阻止的采集尝试，其中`model_used=false`，不算本轮真实模型成功证据；本轮使用已授权的原生CLI，记录在`evidence/live_chat_2026-10-28`。

重建和核对（不调用模型，不运行EDA）：

```bash
cd /data2/huangjj/OpenACMAssistant
.venv/bin/python -B docs/experiment-report/2026-10-27/tools/build_report.py
.venv/bin/python -B docs/experiment-report/2026-10-27/tools/verify_report.py
```

图像及CSV在`assets/`，关键任务JSON和问答快照在`evidence/snapshots/`，完整网表与日志仍保存在各自`runs/<任务>/`。报告数据快照保存任务及源文件哈希。
