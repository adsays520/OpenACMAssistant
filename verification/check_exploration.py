"""Check constraints, fixed specifications, checkpoint resume and FP oracle."""
from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import random
import struct
import sys
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"app"))
from exploration import candidate_plan,default_config,fingerprint,ranking,run_exploration,validate_config
from floating import product_bits


def main():
    config=validate_config(default_config("multiplier"))
    for patch in [{"bit_width":16},{"seed":99},{"samples":16},{"backend":"functional"}]:
        try:
            candidate_plan(config,patch)
        except ValueError:
            pass
        else:
            raise AssertionError("探索更改了固定规格")
    objective=config["objectives"]
    records=[{"candidate":1,"status":"infeasible","metrics":{"synthesized_area_um2":1,"mred":0.9}},
             {"candidate":2,"status":"completed","metrics":{"synthesized_area_um2":300,"mred":0.01}},
             {"candidate":3,"status":"completed","metrics":{"synthesized_area_um2":200,"mred":float('nan')}},
             {"candidate":4,"status":"failed","metrics":{"synthesized_area_um2":2,"mred":0}}]
    assert ranking(records,objective)==(records[1],[2])
    with TemporaryDirectory(dir=ROOT/"verification") as tmp:
        fixture=Path(tmp); source=fixture/"source"; source.mkdir()
        (source/"protected.txt").write_text("original")
        def manifest(_):
            return {"protected.txt":(source/"protected.txt").read_text()}
        def runner(task,plan,**kwargs):
            task.mkdir(parents=True)
            result={"status":"completed","metrics":{"synthesized_area_um2":300,"mred":0.01}}
            (task/"result.json").write_text(json.dumps(result))
            return result
        with redirect_stdout(io.StringIO()):
            job=run_exploration(fixture,source,config,runner,manifest,"unused","unused",2,10,False)
            run_exploration(fixture,source,config,runner,manifest,"unused","unused",4,10,False,resume=job)
        result=json.loads((job/"result.json").read_text())
        assert len(result["candidates"])==4 and len(result["sessions"])==2
        assert len({fingerprint(r['parameters']) for r in result['candidates']})==4
        assert json.loads((job/"source_integrity.json").read_text())["unchanged"]
        assert (job/"best_plan.json").is_file()
    rng=random.Random(42)
    for _ in range(20000):
        a,b=rng.randrange(1<<32),rng.randrange(1<<32)
        av,bv=struct.unpack('>f',a.to_bytes(4,'big'))[0],struct.unpack('>f',b.to_bytes(4,'big'))[0]
        try:
            expected=int.from_bytes(struct.pack('>f',av*bv),'big')
        except OverflowError:
            expected=(((a^b)>>31)<<31)|0x7f800000
        actual=product_bits(a,b,32,8)
        assert (actual&0x7fffffff>0x7f800000) if expected&0x7fffffff>0x7f800000 else actual==expected
    print("PASS: fixed specifications, measured feasible ranking, checkpoint resume, 20000 independent FP32 oracle checks")


if __name__=="__main__":
    main()
