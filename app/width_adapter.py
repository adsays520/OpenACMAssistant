"""External 8-bit tiling; never patch the original multiplier generators."""
from pathlib import Path
import re


def generate_tiled(bits,kind,ids,output):
    from nbit_exact import generate_Exact
    from nbit_approx import generate_Appro_4_2
    blocks=(bits+7)//8
    top=f"{'Exact' if kind=='exact' else 'Appro4_2'}_{bits}bit"
    modules=[]
    instances=[]
    terms=[]
    exact_tile=None
    if kind=="exact":
        exact_tile=generate_Exact([8],output_dir=output/"tile_exact")[0]
        text=exact_tile.read_text()
        names=re.findall(r"\bmodule\s+(\w+)",text)
        expression=r"\b("+"|".join(map(re.escape,names))+r")\b"
        modules.append(re.sub(expression,lambda m:"tile_exact_"+m[0],text))
    for i in range(blocks):
        for j in range(blocks):
            number=i*blocks+j
            if kind=="approx":
                path=generate_Appro_4_2([8],compressor_config={8:ids[number*9:(number+1)*9]},output_dir=output/f"tile_{number}",prompt_for_missing=False)[0]
                text=path.read_text()
                names=re.findall(r"\bmodule\s+(\w+)",text)
                expression=r"\b("+"|".join(map(re.escape,names))+r")\b"
                modules.append(re.sub(expression,lambda m:f"tile_{number}_"+m[0],text))
                tile_top=f"tile_{number}_Appro4_2_8bit"
            else:
                tile_top="tile_exact_Exact_8bit"
            instances += [f"wire [7:0] a_{number}=a >> {8*i};",f"wire [7:0] b_{number}=b >> {8*j};",
                          f"wire [15:0] t_{number};",f"{tile_top} tile_{number}(a_{number},b_{number},t_{number});",
                          f"wire [{2*bits-1}:0] term_{number}=t_{number};"]
            terms.append(f"(term_{number} << {8*(i+j)})")
    path=output/f"{top}.v"
    wrapper=f"module {top}(input [{bits-1}:0] a,b,output [{2*bits-1}:0] p);\n"+"\n".join(instances)+"\nassign p="+" + ".join(terms)+";\nendmodule\n"
    path.write_text("\n".join(modules)+"\n"+wrapper)
    return path
