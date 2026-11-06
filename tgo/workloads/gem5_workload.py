"""gem5宿主前端工作集：私有SE guest、独立输出和确定性仿真语义。"""
import argparse
from pathlib import Path
import json
from tgo.util import run,tool,sha256,write_json

MASK=(1<<64)-1
def checksum(steps,seed):
    def step(x,index):
        which=(x^index)&7
        value=((x<<3)+(index^0x1234567) if which==0 else (x>>3)^(x*17) if which==1 else
               x*33+index if which==2 else (x^0x9e3779b9)+(x>>17) if which==3 else
               (x<<9)^(x>>7)^index if which==4 else (x+0xabcdef01)*5 if which==5 else
               (x>>11)+(x<<5)+index*7 if which==6 else (x^index)*0x10203041)
        return value&MASK
    table=[step(seed+n,n) for n in range(256)];value=seed
    for n in range(steps):
        index=(value+n)&255;value=step(value^table[index],n)
        table[index]=(value+(table[(index+17)&255]>>3))&MASK
    for item in table:value^=item
    return value

def prepare(output,clang=None,lld=None):
    root=Path(output).resolve();root.mkdir(parents=True,exist_ok=False)
    source=Path(__file__).with_name('gem5_guest.c');guest=root/'guest.aarch64'
    compiler=tool('clang',clang)
    command=[compiler,'--target=aarch64-linux-gnu','-O2','-fno-stack-protector','-fno-builtin','-nostdlib','-static',
        '-Wl,--emit-relocs','-Wl,-e,_start','-Wl,-Ttext=0x10000',str(source),'-o',str(guest)]
    linker=tool('aarch64-linux-gnu-ld',required=False) if lld is None else None
    command.insert(1,'--ld-path='+(linker or tool('ld.lld',lld)))
    compilation=run(command,root/'compile.log')
    result={'schema':'tgo.gem5-fixture.v1','guest':str(guest),'guest_sha256':sha256(guest),
        'source_sha256':sha256(source),'compilation':compilation,'config':str(Path(__file__).with_name('gem5_se.py')),
        'metric':'gem5-simulate','semantic_fields':['guest_output','simTicks','simInsts','simOps','exit_code'],
        'measurement_scope':'宿主gem5的m5.simulate原生wall；仿真tick只作语义检查'}
    write_json(root/'fixture.json',result);return result

def main(argv=None):
    parser=argparse.ArgumentParser();commands=parser.add_subparsers(dest='action',required=True)
    create=commands.add_parser('prepare');create.add_argument('-o','--output',required=True)
    create.add_argument('--clang');create.add_argument('--linker','--lld',dest='lld')
    args=parser.parse_args(argv);result=prepare(args.output,args.clang,args.lld)
    print(json.dumps(result,ensure_ascii=False,indent=2));return result

if __name__=='__main__':main()
