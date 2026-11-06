"""入口事务：停车、字节/新区域核验、持久journal和回滚。"""
import time,os,json,hashlib
from .ptrace import Ptrace,ProcFS,StopWorld
from tgo.util import write_json as atomic_json

def patch(s,loaded,apply,tag):
    s.identity();ptrace=Ptrace();proc=ProcFS();events=[]
    plan={'pid':s.pid,'starttime_ticks':s.starttime}
    patches=[p for lib in loaded for p in lib['patches']]
    words={}
    started=time.monotonic_ns()
    with StopWorld(plan,proc,ptrace,events):
        s.identity()
        entries={p['address'] for p in patches}
        for tid in proc.tasks(s.pid):
            if ptrace.pc(tid) in entries:raise RuntimeError('线程PC处于入口指令窗口')
        fd=os.open('/proc/%d/mem'%s.pid,os.O_RDONLY)
        try:
            for lib in loaded:
                for check in lib.get('got_checks',[]):
                    if os.pread(fd,8,check['address']).hex()!=check['expected_hex']:raise RuntimeError('GOT绑定在快捷调用提交前改变')
                for region in lib.get('new_regions',[]):
                    raw=os.pread(fd,region['size'],region['address'])
                    if hashlib.sha256(raw).hexdigest()!=region['sha256']:raise RuntimeError('提交前新代码/只读元数据SHA发生改变')
            for p in patches:
                address=p['address'];wordaddr=address&~7
                if wordaddr not in words:
                    raw=os.pread(fd,8,wordaddr)
                    if len(raw)!=8:raise RuntimeError('入口读取不足')
                    words[wordaddr]={'old':raw,'new':bytearray(raw)}
                offset=address-wordaddr;expected=bytes.fromhex(p['old'] if apply else p['new']);new=bytes.fromhex(p['new'] if apply else p['old'])
                if words[wordaddr]['old'][offset:offset+4]!=expected:raise RuntimeError('入口核对不符:'+p['name'])
                words[wordaddr]['new'][offset:offset+4]=new
            journal={'operation':'apply' if apply else 'rollback','host_pid':s.pid,'starttime':s.starttime,
                'patch_count':len(patches),'entries':patches,'words':[{'address':a,'old':w['old'].hex(),'new':bytes(w['new']).hex()} for a,w in sorted(words.items())],
                'events':events,'transaction_started_monotonic_ns':started}
            path=s.out/(tag+'-journal.json')
            if path.exists():raise RuntimeError('拒绝覆盖journal')
            with path.open('x') as f:
                json.dump(journal,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
            attempted=[]
            try:
                for address,w in sorted(words.items()):
                    attempted.append(address);ptrace.request(4,s.pid,address,int.from_bytes(w['new'],'little'))
                for address,w in words.items():
                    if os.pread(fd,8,address)!=bytes(w['new']):raise RuntimeError('入口读回错误')
            except BaseException:
                for address in reversed(attempted):
                    w=words[address];current=os.pread(fd,8,address)
                    if current not in (w['old'],bytes(w['new'])):raise RuntimeError('第三方修改，拒绝盲恢复')
                    ptrace.request(4,s.pid,address,int.from_bytes(w['old'],'little'))
                raise
        finally:os.close(fd)
    result={'patch_count':len(patches),'word_count':len(words),'elapsed_ns':time.monotonic_ns()-started,'events':events}
    atomic_json(s.out/(tag+'-result.json'),result);return result
