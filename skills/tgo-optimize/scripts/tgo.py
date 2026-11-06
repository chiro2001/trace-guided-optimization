#!/usr/bin/env python3
"""从技能调用当前TGO源码，保持调用者工作目录。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

skill=Path(__file__).resolve().parents[1]
explicit=os.environ.get('TGO_PROJECT_ROOT')
candidates=[Path(explicit)] if explicit else [Path(__file__).resolve().parents[3]]
local=skill/'references/local-install.json'
if not explicit and local.is_file():candidates.insert(0,Path(json.loads(local.read_text())['project_root']))
for project in candidates:
    if (project/'tgo/cli.py').is_file() and (project/'tgo/capabilities.py').is_file():
        sys.path.insert(0,str(project.resolve()))
        from tgo.cli import main
        raise SystemExit(main())
entry=shutil.which('tgo')
if entry:
    probe=subprocess.run([entry,'capabilities','--help'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
    if probe.returncode==0:raise SystemExit(subprocess.call([entry,*sys.argv[1:]]))
raise SystemExit('找不到支持结构化资格诊断的TGO，请设置TGO_PROJECT_ROOT或安装新版工具')
