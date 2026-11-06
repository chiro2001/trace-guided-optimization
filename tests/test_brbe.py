"""Linux perf UAPI夹具 + 实际perf解码。夹具不代表BRBE硬件实测。"""
import argparse
from pathlib import Path
import os
import platform
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tgo import brbe
from tgo.agent import call
from tgo.collect import record
from tgo.elf import ELF
from tgo.optimize import aggregate_observation
from tgo.perf import perf_profile
from tgo.util import read_json, tool, write_json


def perf_fixture(path, n=120, loss=0, endian='<', binary=None, source=0x401100, target=0x401110):
    # perf.data-file-format + PERF_RECORD_SAMPLE / BRANCH_STACK / MMAP UAPI.
    def rec(kind,payload,misc=2):
        return struct.pack(endian+'IHH',kind,misc,len(payload)+8)+payload
    attr=bytearray(128)
    sample_type=(1<<0)|(1<<1)|(1<<2)|(1<<7)|(1<<8)|(1<<11)
    struct.pack_into(endian+'IIQQQQQ',attr,0,0,128,0,100000,sample_type,0,(1<<5)|(1<<6)|(1<<18))
    struct.pack_into(endian+'Q',attr,72,9)
    data=bytearray()
    sample_id=struct.pack(endian+'IIQII',42,43,1,2,0)
    if binary:
        filename=str(binary).encode()+b'\0';filename+=b'\0'*((-len(filename))%8)
        mapping=source&~0xfff
        data+=rec(10,struct.pack(endian+'IIQQQIIQQII',42,43,mapping,0x1000,mapping-0x400000,0,0,1,0,5,2)+filename+sample_id)
    for i in range(n):
        payload=struct.pack(endian+'QIIQIIQQQQQ',source,42,43,1000+i,2,0,100000,1,source,target,2)
        data+=rec(9,payload)
    if loss:
        data+=rec(2,struct.pack(endian+'QQ',1,loss)+sample_id)
        data+=rec(13,struct.pack(endian+'Q',loss)+sample_id)
    features=[]
    if binary:
        buildid=bytes.fromhex(ELF(binary).metadata()['build_id']).ljust(24,b'\0')
        features.append((2,rec(67,struct.pack(endian+'I',42)+buildid+filename)))
    features.append((7,struct.pack(endian+'II',8,8)))
    feature_offset=256+len(data)+16*len(features);sections=bytearray();content=bytearray()
    for _,payload in features:
        sections+=struct.pack(endian+'QQ',feature_offset+len(content),len(payload));content+=payload
    header=struct.pack(endian+'8sQQQQQQQQQQQQ',b'PERFILE2' if endian=='<' else b'2ELIFREP',104,144,104,144,256,len(data),0,0,sum(1<<bit for bit,_ in features),0,0,0)
    path.write_bytes(header+attr+struct.pack(endian+'QQQ',248,8,1)+data+sections+content)


class BRBEQuality(unittest.TestCase):
    def test_bounded_loss_scan_both_endians_and_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'perf.data'
            for endian in ('<','>'):
                perf_fixture(path,loss=3,endian=endian)
                result=brbe.record_counts(path)
                self.assertEqual(result['samples'],120)
                self.assertEqual((result['lost_records'],result['lost_samples']),(3,3))
            data=path.read_bytes();size=struct.unpack_from('>Q',data,48)[0]
            path.write_bytes(data[:256+size-1])
            with self.assertRaisesRegex(ValueError,'截断'):brbe.record_counts(path)
            path.unlink();os.mkfifo(path)
            with self.assertRaisesRegex(ValueError,'普通文件'):brbe.record_counts(path)

    def test_branch_history_retains_order_depth_partial_and_unknown_flags(self):
        stats=brbe.BranchStats()
        stats.feed('42/43 [002] 401100 0x401100/0x401110/P/-/-/8/CALL/- 0x0/0x401100/-/-/-/0/-/-')
        stats.feed('42/ 44 [003] 401100')
        result=stats.result()
        self.assertEqual(result['depth_histogram'],{'0':1,'2':1})
        self.assertEqual(result['empty_stack_percent'],50)
        self.assertEqual(result['complete_branches'],1);self.assertEqual(result['partial_branches'],1)
        self.assertEqual(result['samples_per_cpu'],{'2':1,'3':1})
        self.assertEqual(result['tracked_tasks'],2)
        with self.assertRaisesRegex(ValueError,'SPE'):stats.feed('42/43 [002] 401100 0x1/0x2/PN/-/-/0/-/-')

    def test_quality_gates_real_perf_decoder_and_uapi_data(self):
        perf=shutil.which('perf')
        if not perf:self.skipTest('缺少perf用户态工具')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);perf_fixture(root/'perf.data')
            result=brbe.inspect(root,perf)
            self.assertTrue(result['usable'],result)
            self.assertEqual(result['samples'],120)
            self.assertEqual(result['prediction_flags'],{'P':120})
            self.assertLessEqual((root/'brbe-samples.txt').stat().st_size,8*16385)
            perf_fixture(root/'perf.data',n=5,loss=10)
            result=brbe.inspect(root,perf)
            self.assertFalse(result['usable'])
            self.assertGreater(result['loss_upper_bound_percent'],1)
            self.assertTrue(any('样本不足' in e for e in result['errors']))

    def test_missing_branch_attr_empty_stacks_throttling_and_decode_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);perf_fixture(root/'perf.data')
            good='sample_type: IP|TID|TIME|CPU|BRANCH_STACK, branch_sample_type: USER|ANY, exclude_kernel: 1'
            def script(command,root,stats,timeout):
                for i in range(120):stats.feed('42/43 [002] 401100')
            with patch('tgo.brbe.subprocess.run',return_value=subprocess.CompletedProcess([],0,good,'')),patch('tgo.brbe._script',side_effect=script):
                result=brbe.inspect(root,'unused-perf')
                self.assertFalse(result['usable']);self.assertEqual(result['empty_stack_percent'],100)
            for bad in ('sample_type: IP|CPU','sample_type: 2051, branch_sample_type: 1, exclude_kernel: 1'):
                with patch('tgo.brbe.subprocess.run',return_value=subprocess.CompletedProcess([],0,bad,'')),patch('tgo.brbe._script') as decode:
                    self.assertFalse(brbe.inspect(root,'unused-perf')['usable']);decode.assert_not_called()
            raw=brbe.record_counts(root/'perf.data');raw['throttle_records']=1
            def partial(command,root,stats,timeout):
                stats.feed('42/43 [002] 401100 0x1/0x2/M/-/-/0/-/-')
            with patch('tgo.brbe.record_counts',return_value=raw),patch('tgo.brbe.subprocess.run',return_value=subprocess.CompletedProcess([],0,good,'')),patch('tgo.brbe._script',side_effect=partial):
                result=brbe.inspect(root,'unused-perf',brbe.policy(min_samples=1))
                self.assertFalse(result['usable']);self.assertTrue(any('节流' in e for e in result['errors']))
                self.assertTrue(any('数量不符' in e for e in result['errors']))

    def test_stream_timeout_terminates_decoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'超时'):
                brbe._script([sys.executable,'-c','import time;time.sleep(30)'],Path(tmp),brbe.BranchStats(),.1)

    def test_standard_perf2bolt_and_bolt_with_real_binary_and_buildid(self):
        perf=shutil.which('perf');bolt=tool('llvm-bolt',required=False);p2b=tool('perf2bolt',required=False)
        if platform.machine()!='x86_64' or not all((perf,bolt,p2b,shutil.which('cc'),shutil.which('objdump'))):
            self.skipTest('标准ABI端到端夹具需要x86_64、perf、BOLT和C编译器；不需要BRBE硬件')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'loop.c';image=root/'loop'
            source.write_text('#include <stdio.h>\n__attribute__((noinline)) int work(int n){volatile int x=0;for(int i=0;i<n;i++) x+=(i&1)?3:7;return x;}\nint main(void){printf("%d\\n",work(10000));return 0;}\n')
            subprocess.run(['cc','-O1','-fno-pie','-no-pie','-Wl,--emit-relocs','-Wl,--build-id',str(source),'-o',str(image)],check=True,capture_output=True)
            disassembly=subprocess.check_output(['objdump','-d',str(image)],text=True).split('<work>:',1)[1].split('\n\n',1)[0]
            import re
            branch=re.search(r'^\s*([0-9a-f]+):.*?\s+j\w*\s+([0-9a-f]+)',disassembly,re.M)
            self.assertIsNotNone(branch,disassembly)
            pc,target=(int(x,16) for x in branch.groups())
            perf_fixture(root/'perf.data',binary=image,source=pc,target=target)
            write_json(root/'run.json',{'backend':'brbe'})
            profile=perf_profile(root,perf)
            self.assertEqual(len(profile['images']),1)
            self.assertTrue(profile['quality']['branch_stack']['usable'])
            self.assertEqual(profile['images'][0]['build_id'],ELF(image).metadata()['build_id'])
            with patch('tgo.perf.subprocess.check_output',return_value=''):
                with self.assertRaisesRegex(ValueError,'build-id'):perf_profile(root,perf)
            for command in ([p2b,str(image),'-p',str(root/'perf.data'),'-o',str(root/'loop.fdata')],
                            [bolt,str(image),'-data',str(root/'loop.fdata'),'-o',str(root/'loop.bolt'),'-reorder-blocks=ext-tsp','-split-functions']):
                completed=subprocess.run(command,capture_output=True,text=True,timeout=30)
                self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
            self.assertGreater((root/'loop.fdata').stat().st_size,0)
            (root/'loop.bolt').chmod(0o755)
            self.assertEqual(subprocess.check_output([str(image)]),subprocess.check_output([str(root/'loop.bolt')]))


class BRBECollection(unittest.TestCase):
    def arguments(self,root):
        return argparse.Namespace(output=str(root/'capture'),command=[sys.executable,'-c','print(1)'],pid=None,
            backend='brbe',event=None,roi='whole',python_hook=None,env=[],branch_stack=False,perf='/bin/true',
            period=None,frequency=999,timeout=10,qemu_header=None,allow_truncated=False,cpus=None)

    def test_discovery_checks_real_caps_instead_of_cpu_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);pmu=root/'armv8_pmuv3_0';(pmu/'caps').mkdir(parents=True)
            (pmu/'caps/branches').write_text('64');(pmu/'cpus').write_text('0-3');(pmu/'type').write_text('8')
            self.assertTrue(brbe.capabilities(root,architecture='aarch64')['advertised'])
            self.assertFalse(brbe.capabilities(root,architecture='x86_64')['advertised'])
            (pmu/'caps/branches').unlink()
            found=brbe.capabilities(root,architecture='aarch64')
            self.assertTrue(found['platform_candidate']);self.assertFalse(found['advertised'])

    def test_explicit_record_uses_standard_filters_and_preserves_workload_affinity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);args=self.arguments(root)
            runtime=root/'runtime';runtime.mkdir()
            allowed=sorted(os.sched_getaffinity(0));args.cpus=','.join(map(str,allowed[:2]))
            with patch('tgo.collect.build_runtime',return_value=runtime),patch('tgo.brbe.capabilities',return_value={'platform_candidate':True}),patch('tgo.perf.perf_profile',return_value={}):
                result=record(args)
            command=result['collector_command'];separator=command.index('--')
            self.assertEqual(command[command.index('-j')+1],'any,u')
            self.assertIn('--sample-cpu',command);self.assertIn('--buildid-all',command)
            self.assertNotIn('taskset',command[:separator])
            self.assertIn(args.cpus,command[separator:]);self.assertTrue(result['branch_stack'])
            self.assertEqual(result['sampling']['backend'],'brbe')
            self.assertEqual(set(result['collector_allowed_cpus']),set(allowed))

    def test_explicit_unsupported_platform_and_attach_affinity_do_not_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);args=self.arguments(root);runtime=root/'runtime';runtime.mkdir()
            with patch('tgo.collect.build_runtime',return_value=runtime),patch('tgo.brbe.capabilities',return_value={'platform_candidate':False}):
                with self.assertRaisesRegex(ValueError,'不回退'):record(args)
            self.assertEqual(read_json(root/'capture/run.json')['state'],'failed')
            args.output=str(root/'attach');args.pid=os.getpid();args.cpus='0'
            with self.assertRaisesRegex(ValueError,'attach不修改'):record(args)

    def test_attach_does_not_compile_runtime_inject_or_change_existing_affinity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);args=self.arguments(root);args.command=[];args.pid=os.getpid();args.duration=.1;args.delay=0
            before=os.sched_getaffinity(0)
            with patch('tgo.collect.build_runtime') as compile_runtime,patch('tgo.brbe.capabilities',return_value={'platform_candidate':True}),patch('tgo.perf.perf_profile',return_value={}):
                result=record(args)
            compile_runtime.assert_not_called()
            self.assertEqual(os.sched_getaffinity(0),before)
            self.assertIn('-p',result['collector_command'])
            self.assertFalse(any('taskset' in x or 'LD_AUDIT=' in x for x in result['collector_command']))
            affinity=read_json(root/'capture/brbe-target-affinity.json')
            self.assertIn('before',affinity);self.assertIn('after',affinity)

    def test_brbe_does_not_request_nolbr_or_spe_in_perf2bolt(self):
        with tempfile.TemporaryDirectory() as tmp:
            args=argparse.Namespace(branch_stack=False,timeout=10)
            with patch('tgo.optimize.recovered_policy',return_value=False),patch('tgo.optimize.run',return_value={}) as run:
                aggregate_observation(args,{'path':'/a.so','perf_data':'/perf.data'}, {},'brbe',Path(tmp),
                    {'mode':'standard','perf2bolt':'/perf2bolt','allow_stripped':False},{})
            command=run.call_args.args[0]
            self.assertNotIn('-nl',command);self.assertNotIn('-spe',command)

    def test_pack_excludes_business_arguments_environment_raw_data_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);write_json(root/'brbe-capabilities.json',{'schema':'tgo.brbe-capabilities.v1'})
            write_json(root/'run.json',{'backend':'brbe','command':['business','secret'], 'env':{'TOKEN':'secret'},
                'collector_command':['perf','record','-e','cycles:u','--','business','secret']})
            (root/'perf.data').write_text('secret');(root/'stdout').write_text('secret')
            output=root/'diagnostic.tar.gz';brbe.diagnostic_pack(root,output)
            with tarfile.open(output) as archive:
                self.assertNotIn('perf.data',archive.getnames());self.assertNotIn('run.json',archive.getnames())
                self.assertNotIn('secret',archive.extractfile('diagnostic.json').read().decode())
            with self.assertRaisesRegex(ValueError,'已存在'):brbe.diagnostic_pack(root,output)
            (root/'brbe-script.stderr').symlink_to(root/'stdout')
            with self.assertRaisesRegex(ValueError,'普通文件'):brbe.diagnostic_pack(root,root/'bad.tar.gz')

    def test_mcp_validates_attach_and_never_silently_ignores_fields(self):
        base={'job_output':'/unused','output':'/unused'}
        for fields in ({},{'pid':42,'command':['true']},{'pid':42,'duration':1,'cpus':'0'},
                       {'pid':42},{'command':['true'],'roi':'window'},
                       {'command':['true'],'period':10,'frequency':100}):
            with self.subTest(fields=fields),patch('tgo.jobs.launch') as launch:
                with self.assertRaises(ValueError):call('tgo_brbe_record_start',{**base,**fields})
                launch.assert_not_called()


if __name__=='__main__':unittest.main()
