# OpenACM 仿真任务报告

状态：**completed**

电路类型：`multiplier`

## 设计参数

```json
{
  "bit_width": 4,
  "kind": "exact",
  "compressor_id": 1,
  "samples": 4096,
  "exhaustive": true,
  "seed": 42,
  "backend": "functional",
  "architecture": "auto"
}
```

## 实测指标

| 指标（字段后缀为单位） | 结果 |
|---|---:|
| sample_count | 256 |
| error_count | 0 |
| error_rate | 0 |
| mae | 0 |
| max_absolute_error | 0 |
| mred | 0 |
| nmed | 0 |
| architecture | tiled_8bit |

## 约束判定

未指定数值验收条件；完成仿真不代表满足某个功耗、时序或精度预算。

## 依据与限制

- 原始 OpenACM 仅作为复制来源；仿真进程受 Landlock 写保护，只能写入本任务目录及标准输出设备。
- 原始日志见 `../logs/`，新生成网表、RTL、测量和波形见 `../artifacts/`。
- SRAM 面积是内置公式估算；压缩器使用原仓库的简化 LEVEL=1 模型，7×W×L 面积是启发式估算。
- multiplier及backend=functional的DCiM报告为数字功能仿真；backend=asic的结果还包含参考库后端，均不等于流片签核。
- MRED 排除参考乘积为零的输入；NMED=MAE/(2^n−1)^2；采样结果不等同于穷举证明。
- DCiM 的 PE 检查覆盖常数输入的初始化与运行；不是所有控制序列和输入流的完整验证。
- 有限候选扫描给出候选中最好的已达标项，不宣称全局最优。
