# 固定组织SRAM器件复验

用户给固定容量、输出字宽、行×列×子阵列数、列复用、基线W/L、唯一器件扫描域和PVT/MC时，使用experiment_mode=sram_device_retest。不要沿用旧乘法器/方法对照规格，也不要将总容量32768bit当bit_width。参考历史第N项时核对指定容量及完整组织，唯一匹配后保存候选记录哈希；启动前再校验，原结果不修改。

```bash
./openacm-assistant.sh acco --spec examples/sram-device-retest-4kb.json --plan-only
./openacm-assistant.sh acco --spec examples/sram-device-retest-smoke.json --without-model
```

本模式架构阶段只作锁定组织的容量/接口结构校验，不声称重新测得架构PPA；基线也在器件阶段逐PVT重新SPICE评价，将时间保留给明确器件域。例为PD90/135/180nm，其余PU90/PG135/L50nm不变；TT27℃1V、SS85℃0.9V、FF−40℃1.1V，MC2、seed42、相对标准差5%。无额外验收阈值，不从历史任务继承MRED等其它电路约束。

每个尺寸的每个角独立保留coverage.json、worker输入、模型、metrics.json与各操作samples.json。一角失败后继续剩余角和尺寸；失败时保留已有操作的部分测量。输出reports/pvt_results.csv、baseline_comparison.json与报告。bank外围与面积明确为估算，原始阵列测量和bank估算分开，不凭两次MC声明良率。

全局预算耗尽为incomplete；正在评价项也是incomplete，未开始项pending_candidates标not_run。工具/单任务超时是failed，不能称设计infeasible。仅全部已测硬约束不满足才有不可行判定依据。某个读操作成功不能替代未完成的写/SNM。对每项已有指标可做同角基线比较，但要标注complete_case=false。

实测已发现32×32完整子阵列MC读写＋SNM可能达到单个worker600秒上限；不得静默缩小阵列、关闭随机性、改工艺角或伪造缺失指标来“通过”。需要继续改进执行预算/分批流程时，保持原实验与失败证据，新建任务。
