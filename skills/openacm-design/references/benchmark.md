# 架构搜索对照

适用于原生8/16位近似乘法器，比较random/nsga2/llm；固定器件尺寸，暂不支持SRAM方法对照或经验开关消融。单次两级ACCO仍用acco/explore。

```bash
./openacm-assistant.sh compare --spec examples/compare-multiplier-8bit.json --plan-only
./openacm-assistant.sh compare --spec examples/compare-multiplier-smoke.json
./openacm-assistant.sh compare --spec examples/compare-multiplier-8bit.json
```

自然语言先给出电路，再补每轮候选预算和重复次数；缺少这两项时保留原需求并询问缺项，不默认启动15轮。可连续补充测试种子、采样、输入活动周期、负载和MRED约束；/plan保存完整草案，“开始运行”执行该草案。已有字段不重复询问。默认目标是在约束下最小化PDP，未给误差上限时保持空约束并明确展示。默认AKbar2基线、Nangate45 typical、5ns、10fF；指定未知库或其它目标不能声称已落实。

“分别使用随机搜索、NSGA-II和LLM”是方法清单；“只比较架构选点”限定实验层次，不覆盖方法。后续明确改选方法时采用最新清单；只选一个或指定未支持算法要继续澄清。重复粘贴的相同约束去重，保留原失败记录后重发也应能恢复，无需清空对话。明确“开展实验”按执行要求处理；用户要求先看计划才预览。

search_comparison规格包含experiment（普通ACCO乘法器规格）、methods、search_seeds、candidate_budget、pool_seed/pool_size、validation_top_k、trial_max_seconds/max_seconds。示例为随机/原NSGA-II/LLM各五轮，搜索种子1–5；算术测试seed=42、4096组，与搜索种子无关联。统一池256项，pool_seed=1729。每轮架构上限32包含共同基线，入选复核上限3，器件不优化。

共同池在运行前冻结并保存SHA。NSGA-II只在池索引上演化；LLM每步从共同池内最多12个尚未评价候选选点；随机方法直接从共同池抽取。需要说明这些实际提案机制，不能宣称原生全基因NSGA与有限池LLM拥有完全相同机制。每轮预算只计唯一架构，缓存重复提案；记录实际完成数。LLM失败时保留失败轮次，不随机回退后标作纯LLM。

搜索历史为GNN预测；每轮按相同规则选前K项，重新做RTL/综合/STA，不能把32次代理查询写作32次实测。预测无可行项时按最低预测MRED复核。各worker实际vectors.hex和Nangate库哈希须与共同条件匹配。每轮禁止读取其它轮结果或注入后验经验。

所有产物在runs/<时间>_search_comparison_<ID>，含candidate_pool.json、test_vectors.hex、reference_manifest.json、source_integrity.json、trials/seed_<n>/<method>、逐次决策审计、reports/report.md、summary.md、trial_summary.csv、search_curves.csv、statistics.json和预测/实测分开的PNG/SVG/PDF图。

先读result.json再总结完成轮数、失败/无可行项及分母；未跑完不能说各五轮完成。耗时包括模型与EDA，共享模型初始化单列。中位数/标准差注明只涵盖哪些完整可行轮次，不隐藏失败种子；有限池与五次重复不证明全局最优或统计显著性。旧结果不覆盖，失败重跑创建新目录。
