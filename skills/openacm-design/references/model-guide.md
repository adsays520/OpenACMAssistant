# DeepSeek 可执行需求映射

接口顶层：`intent`（run/clarify/explain）、`workflow`（sram/multiplier/compressor/dcim/floating）、`parameters`、`constraints`、`message`。可选 `objective` 和 `sweep`。仅提供与选中 workflow 相关的字段；需要澄清时 parameters={}、constraints=[]。数值必须按字段单位换算，如 0.18 µm -> pd_width_nm=180。

## sram

实际适配 OpenACM 的 SRAM_CONFIG 和 Sram6TCoreMcTestbench，Xyce SPICE 仿真。
可用参数：rows、cols、vdd_v、temperature_c、corner(TT/FF/SS/FS/SF)、operations（read/write/hold_snm/read_snm/write_snm）、mc_runs、variation、vth_std、seed、pu_width_nm、pd_width_nm、pg_width_nm、length_nm、pu_model(PMOS_VTL/VTG/VTH)、pd_model/pg_model(NMOS_VTL/VTG/VTH)、wire_rc、wire_res_ohm、wire_cap_pf、column_mux、full_array。

默认 16×8，1 V，27℃，TT，read/write，单次确定性运行，PU/PD/PG=90/90/135 nm，L=50 nm，PMOS_VTG、NMOS_VTG；取自 config_sram.yaml 的设计参考值；带 RC，100Ω，0.001 pF，无列复用，全阵列。单任务不超过 65536 bit；rows 为 2 的幂；启用2路列复用时 cols 为偶数。variation=true 才启用 Monte Carlo，mc_runs=1–50；vth_std 是 vth0/u0/voff 三参数的相对标准差，默认5%，使用固定种子生成真实不同样本。

指标：read_delay_ns/write_delay_ns（样本最大值），read_power_uw/write_power_uw（平均运行功率的样本最大值），hold_snm_v/read_snm_v/write_snm_v（样本最小值），cell_area_um2（估算）。若约束涉及 SNM，operations 必须包含对应 SNM 操作。功率是平均功率，不是峰值。精确时钟波形及外围器件组尺寸尚未暴露为自动参数。

## multiplier

参数：bit_width（4–64）、kind(exact/approx/log)、compressor_id（1–8，所有可近似位置同种）、compressor_ids（逐位置列表，可替代单ID）、samples（默认4096）、exhaustive（穷举仅4–8bit）、seed（默认42）。
压缩器映射由代码定义：1=ACCI1，2=kong2，3=antonio，4=momeni，5=ha，6=akbar1，7=akbar2，8=sabetz。ACCI1 是 RTL 名称，不能自动认定等同于其他路径 AP1。8bit 的位置数为9，16bit为49；其他位宽由生成器计算。用户只说“用 sabetz 的8位近似乘法器”时 kind=approx,bit_width=8,compressor_id=8。
生成后使用 Icarus Verilog + vvp 验证实际 RTL。精确与可配置乘法器是无符号；log 源码含符号处理，当前统一无符号参考下的结果必须作为实测误差解释，不能假定其为无符号精确实现。
指标：error_rate、mred、nmed；同时保存 MAE 和最大绝对误差。MRED 排除参考值0；NMED=MAE/(2^n−1)^2。不能通过 samples 参数要求功耗、面积或后端时序。

## compressor

参数：name（AKbar1/AKbar2/antonio/AP1/ha/kong2/momeni/sabetz）、vdd_v（默认1.2）、temperature_c（27）、nmos_scale/pmos_scale/length_scale/load_scale（默认1）。使用实际网表 W/L 和比例调整；每项须为有限正数且在接口范围内。运行 Xyce。
指标：delay_ns（所测路径中最大传播延迟）、power_uw（平均功耗）、gate_area_um2（ΣW×L）、estimated_area_um2（7×门面积的粗略估算）。基础网表采用简化 LEVEL=1 模型；不能称为真实工艺 PVT 签核。不能用 scale 字段表达任意新增晶体管拓扑。

## dcim

参数：bit_width、rows、kind、compressor_id/compressor_ids、samples、exhaustive、seed、backend（functional/asic）、clock_period_ns（2–1000，默认10ns）。
从工作副本生成 SRAM/PE 和 multiplier；做乘法器误差仿真，并检查常数输入下 PE 初始化、进入 RUN 和连续16个有效乘积。使用仓库生成器，未改写原始内置程序。该自动入口不等于完整控制验证或 ASIC 后端完成。

默认 backend=functional；用户明确“DCiM全流程/完整后端/布局布线”时设 backend=asic。完整后端memory_impl=auto选择16×8/32×16/64×32匹配宏，其它尺寸使用标准单元存储，memory_impl=macro仅接受这三种宏尺寸，使用Nangate45 typical库和该宏原有Liberty/LEF。流程执行RTL检查、Yosys综合、变化输入的门级差分验证、floorplan/PDN/placement/CTS/routing、OpenRCX SPEF、STA/SDF、后仿真及VCD活动功耗。单任务总预算1800秒，各EDA阶段600秒；默认4线程。

后端验收指标：setup_wns_ns、hold_wns_ns（均≥0才无相应违例）、route_drc_count（≤0）、routed_power_uw（给定测试负载下的Liberty活动功耗）。routed_area_um2来源于OpenROAD面积报告。要验收时序/DRC，应明确给出对应constraints。门级功能使用Icarus，完整SDF与timing check使用VCS及已配置许可证，不允许静默降级为不完整的SDF解析。completed不承诺覆盖所有功能或物理签核；不能把单独修改SRAM W/L的SPICE结果替换为后端宏表征。

示例：做16×8精确DCiM全流程，时钟周期10ns，要求setup和hold slack不低于0ns、布线DRC为0。
parameters={"rows":16,"bit_width":8,"kind":"exact","backend":"asic","clock_period_ns":10}；constraints使用setup_wns_ns≥0、hold_wns_ns≥0、route_drc_count≤0。

## 约束和候选比较

constraints 数组的每项为 {"metric":"read_delay_ns","op":"le","value":2.0}，或 ge 表示下限。约束指标必须能由此任务所选仿真实际测量。
objective 示例 {"metric":"read_power_uw","direction":"min"}。
sweep 示例 {"parameter":"vdd_v","values":[0.9,1.0,1.1]}，最多8个候选，其他参数固定。
一次运行不承诺自动找到满足指标的电路；只有扫描中已通过全部约束的候选参与最佳值比较。

## 示例

用户：做16行8列SRAM，1V、27℃、TT，PU120nm、PD180nm、PG70nm、L74nm，读写仿真，读延迟不超过3ns。
输出：{"intent":"run","workflow":"sram","parameters":{"rows":16,"cols":8,"vdd_v":1.0,"temperature_c":27,"corner":"TT","pu_width_nm":120,"pd_width_nm":180,"pg_width_nm":70,"length_nm":74,"operations":["read","write"]},"constraints":[{"metric":"read_delay_ns","op":"le","value":3}],"message":"按给定参数生成独立工作副本并运行读写仿真。"}

用户：做一个8位精确乘法器，穷举验证。
输出：{"intent":"run","workflow":"multiplier","parameters":{"bit_width":8,"kind":"exact","exhaustive":true},"constraints":[],"message":"生成并穷举验证8位精确乘法器。"}

用户：帮我改OpenACM内置main_sram.py。
输出：{"intent":"clarify","workflow":"sram","parameters":{},"constraints":[],"message":"原始OpenACM内置程序只读。请说明要改变的电路参数或仿真操作，我会在任务目录生成对应配置并运行。"}

## 新增执行参数

整数乘法器/DCiM增加architecture=auto/native/tiled。auto对4位、超过32位及已发现错误的原生奇数精确位宽使用外部分块：8位模块拼接，仍不修改原始生成器。approx分块位置数=9×ceil(bit_width/8)^2；native位置数=floor((bit_width−2)^2/4)。auto的5–32位approx使用native，其余用分块。所有同种位置优先使用compressor_id，不生成长数组。kind=log仅支持8/16/32位。

multiplier增加backend=functional/synthesis；synthesis执行Nangate45综合并与RTL逐向量对比，测synthesized_area_um2。DCiM支持functional/synthesis/asic、memory_impl=auto/macro/standard_cells。SRAM外围增加decoder_scale/wordline_scale/precharge_scale/senseamp_scale/write_driver_scale/mux_scale（0.25–3），分别缩放原配置组的MOS宽度。

## floating

参数variant、fp_width、exp_width、mode、mantissa_keep、samples、seed、backend=functional/synthesis。variant为Exact、AC、EAFPU、MMBS5/6/7、SSFPM12/14/16/18、SSFPMcorr12/14/16/18。Exact总宽8–32、指数3–8；AC总宽16–32、指数5–8、mantissa_keep3–6、mode0–2；其余源码固定FP32（32/8），EAFPU的mode0–2。尾数至少2位。

独立整数运算RNE浮点参考与实际RTL比较，测error_rate、mred、special_mismatch_count，综合模式另测synthesized_area_um2。非有限错误输出时不报告MRED。Exact名称不代表IEEE已验证，不得忽略边界错误。用户FP16对应fp_width=16、exp_width=5，FP32为32/8。

设计空间探索由外部explore入口控制，基线计划仍为普通run计划；模型逐轮仅选择允许菜单中的候选。见[exploration.md](exploration.md)。

若输入带public_dataset_reference，则为公开8位近似乘法器训练样本参考。published_labels不属于本地实测；测量条件、单位和位置可比性未完整确认，不得把area当synthesized_area_um2，也不得声称待运行菜单候选已通过用户约束。明确用户参数优先，参考不允许修改固定种子、尺寸、后端或指定的压缩器。此机制检索数据而不更新模型权重。
