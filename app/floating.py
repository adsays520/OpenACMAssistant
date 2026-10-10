"""Independent binary floating point oracle with round-to-nearest-even."""
import csv
import math
import random
import re


def classify(bits, width, exp):
    m = width-exp-1
    e, f = (bits >> m) & ((1 << exp)-1), bits & ((1 << m)-1)
    return "nan" if e == (1<<exp)-1 and f else "inf" if e == (1<<exp)-1 else "zero" if e == 0 and f == 0 else "subnormal" if e == 0 else "normal"


def components(bits, width, exp):
    m = width-exp-1
    e, f = (bits >> m) & ((1 << exp)-1), bits & ((1 << m)-1)
    return (-1 if bits >> (width-1) else 1), f + ((1<<m) if e else 0), (e if e else 1) - ((1<<(exp-1))-1) - m


def rounded_shift(value, shift):
    if shift <= 0:
        return value << -shift
    q, r = divmod(value, 1 << shift)
    return q + (r > (1 << (shift-1)) or (r == (1 << (shift-1)) and q & 1))


def product_bits(a, b, width, exp):
    """Exact integer arithmetic, including signed zero, subnormals, Inf and NaN."""
    m, bias, maximum = width-exp-1, (1<<(exp-1))-1, (1<<exp)-1
    sign = ((a ^ b) >> (width-1)) << (width-1)
    ca, cb = classify(a,width,exp), classify(b,width,exp)
    if "nan" in [ca,cb] or ("zero" in [ca,cb] and "inf" in [ca,cb]):
        return (maximum << m) | (1 << (m-1))
    if "inf" in [ca,cb]:
        return sign | (maximum << m)
    if "zero" in [ca,cb]:
        return sign
    _, ma, ea = components(a,width,exp)
    _, mb, eb = components(b,width,exp)
    integer, power = ma*mb, ea+eb
    exponent = integer.bit_length()-1+power
    if exponent < 1-bias:
        mantissa = rounded_shift(integer, (1-bias-m)-power)
        return sign | mantissa
    mantissa = rounded_shift(integer, integer.bit_length()-1-m)
    if mantissa >= 1 << (m+1):
        mantissa >>= 1
        exponent += 1
    if exponent+bias >= maximum:
        return sign | (maximum<<m)
    return sign | ((exponent+bias)<<m) | (mantissa & ((1<<m)-1))


def numeric(bits,width,exp):
    c = classify(bits,width,exp)
    if c == "nan":
        return math.nan
    if c == "inf":
        return -math.inf if bits >> (width-1) else math.inf
    sign,mantissa,power = components(bits,width,exp)
    return sign * math.ldexp(mantissa,power)


def floating(job, params, repo, command):
    width, exp, variant = params["fp_width"], params["exp_width"], params["variant"]
    base = repo / "floating"
    out = job / "artifacts/rtl"
    out.mkdir(parents=True)
    sources, overrides = [], ""
    if variant in ["Exact", "AC"]:
        path = base / ("Exact/V_EFPM.v" if variant == "Exact" else "AC/V_AFPM_v1.v")
        top = "V_EFPM" if variant == "Exact" else "V_AFPM_v1"
        overrides = f"#(.WIDTH({width}),.EXP_W({exp})" + (f",.n({params['mantissa_keep']}),.mode({params['mode']})" if variant == "AC" else "") + ")"
    elif variant == "EAFPU":
        path, top = base / "EAFPU/EAFPU.v", "EAFPU"
        overrides = f"#(.MODE({params['mode']}))"
    elif variant.startswith("MMBS"):
        path, top = base / f"MMBS/{variant}.v", variant
    else:
        path, top = base / "SSFPM/FPmult_opt_pipe_SSM.v", "FPmult_opt_pipe_SSM"
        keep = int(re.search(r"\d+",variant)[0])
        unit = f"MMssm_{'corr2_' if 'corr' in variant else ''}n23_m{keep}"
        selected = out / "selected_ssfpm.v"
        selected.write_text(path.read_text().replace("MMssm_n23_m12",unit))
        path = selected
        sources.append(base / f"SSFPM/{unit}.v")
    sources.insert(0,path)
    wrapper = out / "floating_design.v"
    wrapper.write_text(f"module floating_design(input [{width-1}:0] a,b, output [{width-1}:0] p);\n{top} {overrides} dut(a,b,p);\nendmodule\n")
    sources.append(wrapper)
    m, bias, maximum = width-exp-1, (1<<(exp-1))-1, (1<<exp)-1
    corners = [0, 1<<(width-1), 1, (1<<m)-1, 1<<m, bias<<m, (bias<<m)|1,
               ((maximum-1)<<m)|((1<<m)-1), maximum<<m, (maximum<<m)|(1<<(m-1))]
    rng = random.Random(params["seed"])
    pairs = [(a,b) for a in corners for b in corners][:params["samples"]]
    while len(pairs) < params["samples"]:
        def normal():
            return (rng.randrange(2)<<(width-1)) | (rng.randint(max(1,bias-2),min(maximum-1,bias+2))<<m) | rng.randrange(1<<m)
        pairs.append((normal(),normal()))
    vectors = job / "inputs/vectors.hex"
    vectors.write_text("\n".join(f"{(a<<width)|b:x}" for a,b in pairs)+"\n")
    products = job / "artifacts/products.txt"
    tb = job / "artifacts/testbench.v"
    tb.write_text(f'''module tb;
reg [{width-1}:0] a,b;
wire [{width-1}:0] p;
reg [{2*width-1}:0] vectors[0:{len(pairs)-1}];
integer i,fd;
floating_design dut(a,b,p);
initial begin
$readmemh("{vectors}",vectors);
fd=$fopen("{products}","w");
for(i=0;i<{len(pairs)};i=i+1) begin {{a,b}}=vectors[i]; #1; $fdisplay(fd,"%h",p); end
$fclose(fd); $finish; end
endmodule
''')
    command(["iverilog","-g2012","-s","tb","-o",str(job/"artifacts/sim.vvp"),*map(str,sources),str(tb)],repo,job/"logs/rtl_compile.log")
    command(["vvp",str(job/"artifacts/sim.vvp")],repo,job/"logs/rtl_simulation.log")
    observed = products.read_text().splitlines()
    if len(observed) != len(pairs):
        raise RuntimeError("浮点输出数量不符")
    mismatches, special, nonfinite, relatives, rows = 0, 0, 0, [], []
    for (a,b), raw in zip(pairs,observed):
        try:
            actual = int(raw,16)
        except ValueError as exc:
            raise RuntimeError("浮点输出含X/Z，拒绝计算误差") from exc
        ref = product_bits(a,b,width,exp)
        mismatch = actual != ref and not (classify(actual,width,exp)==classify(ref,width,exp)=="nan")
        mismatches += mismatch
        is_special = any(classify(x,width,exp) != "normal" for x in [a,b,ref])
        special += is_special and mismatch
        expected, value = numeric(ref,width,exp), numeric(actual,width,exp)
        if math.isfinite(expected) and expected != 0:
            if math.isfinite(value):
                relatives.append(abs(value-expected)/abs(expected))
            else:
                nonfinite += 1
        rows.append([f"{a:x}",f"{b:x}",f"{ref:x}",raw,classify(ref,width,exp),mismatch])
    with (job/"artifacts/comparison.csv").open("w") as stream:
        writer=csv.writer(stream); writer.writerow(["a_hex","b_hex","reference_hex","actual_hex","reference_class","mismatch"]); writer.writerows(rows)
    metrics = {"sample_count":len(pairs),"error_count":mismatches,"error_rate":mismatches/len(pairs),
               "special_mismatch_count":special,"nonfinite_output_count":nonfinite,"finite_relative_samples":len(relatives),
               "reference":"binary_RNE_integer_oracle", "fp_format":f"{width}/{exp}/{m}"}
    if relatives and nonfinite == 0:
        metrics["mred"] = sum(relatives)/len(relatives)
    if params["backend"] == "synthesis":
        from digital_synthesis import synthesize
        gate = job/"artifacts/gate_products.txt"
        metrics.update(synthesize(job,repo,sources,"floating_design",tb.read_text().replace(str(products),str(gate)),command))
        if gate.read_text().splitlines() != observed:
            raise RuntimeError("浮点综合后与RTL输出不一致")
        metrics["post_synth_functional_checks"] = len(pairs)
    return metrics
