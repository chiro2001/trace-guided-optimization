"""由gem5内嵌Python执行；仅m5.simulate计入宿主优化ROI。"""
import argparse
import json
import hashlib
import os
from pathlib import Path
import sys

import m5
from m5.objects import (System,SrcClockDomain,VoltageDomain,AddrRange,ArmO3CPU,ArmMinorCPU,ArmTimingSimpleCPU,
    SystemXBar,Cache,MemCtrl,DDR3_1600_8x8,SEWorkload,Process,Root)

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tgo.roi import region,emit_result
from tgo.workloads.gem5_workload import checksum

p=argparse.ArgumentParser();p.add_argument('--guest',required=True)
p.add_argument('--steps',type=int,default=100000);p.add_argument('--seed',type=int,default=101)
p.add_argument('--cpu',choices=['o3','minor','timing'],default='o3')
p.add_argument('--guest-sha256',help='本次guest输入绑定，非已知文件名单')
args=p.parse_args()
if args.steps<=0 or args.seed<0:raise ValueError('steps须为正数，seed不可为负数')
if args.guest_sha256 and hashlib.sha256(Path(args.guest).read_bytes()).hexdigest()!=args.guest_sha256:
    raise ValueError('本次gem5 guest输入发生变化')
if os.environ.get('TGO_RESULT_FILE'):
    out=Path(os.environ['TGO_RESULT_FILE']).with_suffix('.gem5-work');out.mkdir(parents=True,exist_ok=False)
else:
    import tempfile
    out=Path(tempfile.mkdtemp(prefix='tgo-gem5-'))
m5.options.outdir=str(out);m5.core.setOutputDir(str(out))
# gem5的text visitor是单例；复用启动时登记的默认stats.txt。
stats=out/'stats.txt'

system=System();system.clk_domain=SrcClockDomain(clock='2GHz',voltage_domain=VoltageDomain())
system.mem_mode='timing';system.mem_ranges=[AddrRange('512MiB')];system.cache_line_size=64
system.cpu={'o3':ArmO3CPU,'minor':ArmMinorCPU,'timing':ArmTimingSimpleCPU}[args.cpu]();system.membus=SystemXBar()
system.icache=Cache(size='32KiB',assoc=4,tag_latency=2,data_latency=2,response_latency=2,mshrs=8,tgts_per_mshr=8)
system.dcache=Cache(size='32KiB',assoc=4,tag_latency=2,data_latency=2,response_latency=2,mshrs=8,tgts_per_mshr=8)
system.cpu.icache_port=system.icache.cpu_side;system.cpu.dcache_port=system.dcache.cpu_side
system.icache.mem_side=system.membus.cpu_side_ports;system.dcache.mem_side=system.membus.cpu_side_ports
system.cpu.createInterruptController()
system.mem_ctrl=MemCtrl(dram=DDR3_1600_8x8(range=system.mem_ranges[0]))
system.mem_ctrl.port=system.membus.mem_side_ports;system.system_port=system.membus.cpu_side_ports
guest=str(Path(args.guest).resolve());system.workload=SEWorkload.init_compatible(guest)
system.cpu.workload=Process(executable=guest,cmd=[guest,str(args.steps),str(args.seed)],output=str(out/'guest.stdout'))
system.cpu.createThreads();root=Root(full_system=False,system=system)
m5.instantiate();m5.stats.reset()
with region('gem5-simulate'):event=m5.simulate()
m5.stats.dump()
text=(out/'guest.stdout').read_text()
expected='gem5-guest-ok '+format(checksum(args.steps,args.seed),'016x')+'\n'
if event.getCode()!=0 or text!=expected:raise RuntimeError('gem5 guest语义或退出码不符:'+repr((event.getCause(),event.getCode(),text,expected)))
values={}
for line in stats.read_text().splitlines():
    fields=line.split()
    if len(fields)>=2 and fields[0] in ['simTicks','simInsts','simOps']:values[fields[0]]=int(fields[1])
if set(values)!={'simTicks','simInsts','simOps'}:raise RuntimeError('gem5统计缺少确定性计数')
result={'workload':'gem5-'+args.cpu,'steps':args.steps,'seed':args.seed,'guest_output':text,
        'exit_cause':event.getCause(),'exit_code':event.getCode(),'simulation':values}
emit_result(result);print(json.dumps(result,sort_keys=True))
