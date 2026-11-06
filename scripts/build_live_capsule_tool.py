#!/usr/bin/env python3
"""在新目录复用只读LLVM对象，构建保留旧入口/旧帧的在线专用代码包生成器。"""
import argparse
import json
from pathlib import Path
import shlex
import shutil
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tgo.util import read_json,write_json,sha256,run

def build(args):
    baseline=Path(args.identity_build).resolve();llvm_build=Path(args.llvm_build).resolve()
    llvm_source=Path(args.llvm_source).resolve();out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=False)
    record={'schema':'tgo.live-generator-build.v1','state':'building','started_unix':time.time(),
        'qualification':'生成器源结构与编译能力；输入哈希仅作构建证据，不是应用资格名单',
        'baseline_tool_sha256':sha256(baseline/'llvm-bolt'),'sources':[],'commands':[]}
    write_json(out/'build.json',record)
    try:
        old=read_json(baseline/'build.json');commands=read_json(llvm_build/'compile_commands.json')
        rewrite_source=baseline/'RewriteInstance.cpp';text=rewrite_source.read_text()
        anchor='namespace opts {\n'
        if text.count(anchor)!=1 or 'runtime-preserve-function-identity' not in text or 'runtime-online-capsule' in text:
            raise ValueError('函数身份生成器源码注入点或能力不符')
        option='''cl::opt<bool> RuntimeOnlineCapsule(
    "runtime-online-capsule",
    cl::desc("ONLINE CAPSULE ONLY: keep original code and let the runtime transact entries"),
    cl::init(false), cl::Hidden, cl::cat(BoltCategory));
'''
        text=text.replace(anchor,anchor+option,1)
        pass_source=llvm_source/'bolt/lib/Passes/PatchEntries.cpp';pass_text=pass_source.read_text()
        pass_anchor='Error PatchEntries::runOnFunctions(BinaryContext &BC) {'
        if pass_text.count(pass_anchor)!=1:raise ValueError('PatchEntries注入点不唯一')
        pass_text=pass_text.replace('namespace opts {','namespace opts {\nextern llvm::cl::opt<bool> RuntimeOnlineCapsule;\nextern llvm::cl::opt<bool> RuntimePreserveFunctionIdentity;',1)
        pass_text=pass_text.replace(pass_anchor,pass_anchor+'''
  if (opts::RuntimeOnlineCapsule) {
    if (!opts::RuntimePreserveFunctionIdentity || !BC.isAArch64()) {
      BC.errs() << "BOLT-ERROR: runtime capsule requires AArch64 function identity" << char(10);
      exit(1);
    }
    BC.outs() << "BOLT-RUNTIME-CAPSULE: original entries retained; runtime transaction required" << char(10);
    return Error::success();
  }
''',1)
        for source,content,name,command in [
            (rewrite_source,text,'RewriteInstance.cpp',old['compile']),
            (pass_source,pass_text,'PatchEntries.cpp',shlex.split(next(c['command'] for c in commands if Path(c['file'])==pass_source)))]:
            target=out/name;target.write_text(content);obj=out/(name+'.o');argv=list(command)
            for flag,value in [('-o',str(obj)),('-MF',str(out/(name+'.d'))),('-MT',str(obj))]:
                if flag in argv:argv[argv.index(flag)+1]=value
            argv=[str(target) if value==str(source) else value for value in argv]
            if str(target) not in argv:raise ValueError('编译命令没有引用新源码')
            record['sources'].append({'original':str(source),'original_sha256':sha256(source),'modified':str(target),'modified_sha256':sha256(target)})
            record['commands'].append(argv);write_json(out/'build.json',record)
            run(argv,out/(name+'.compile.log'),cwd=llvm_build,timeout=1200)
        for source,obj,archive in [(baseline/'libLLVMBOLTRewrite.a','RewriteInstance.cpp.o','libLLVMBOLTRewrite.a'),
            (llvm_build/'lib/libLLVMBOLTPasses.a','PatchEntries.cpp.o','libLLVMBOLTPasses.a')]:
            shutil.copy2(source,out/archive)
            run(['ar','r',str(out/archive),str(out/obj)],out/(archive+'.log'),cwd=llvm_build,timeout=120)
        link=[str(out/'libLLVMBOLTRewrite.a') if x==str(baseline/'libLLVMBOLTRewrite.a') else
              str(out/'libLLVMBOLTPasses.a') if x=='lib/libLLVMBOLTPasses.a' else
              '-Wl,--dependency-file='+str(out/'link.d') if x.startswith('-Wl,--dependency-file=') else x for x in old['link']]
        link[link.index('-o')+1]=str(out/'llvm-bolt');record['commands'].append(link);write_json(out/'build.json',record)
        run(link,out/'link.log',cwd=llvm_build,timeout=1200)
        record.update(state='complete',tool_sha256=sha256(out/'llvm-bolt'),finished_unix=time.time());return record
    except BaseException as error:record.update(state='failed',error=str(error));raise
    finally:write_json(out/'build.json',record)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--identity-build',required=True)
    parser.add_argument('--llvm-build',required=True);parser.add_argument('--llvm-source',required=True)
    parser.add_argument('-o','--output',required=True)
    print(json.dumps(build(parser.parse_args()),ensure_ascii=False,indent=2))
