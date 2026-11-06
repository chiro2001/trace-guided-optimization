from pathlib import Path
import re
import subprocess
from .elf import ELF
from .util import read_json, write_json, tool_env, sha256


def perf_profile(root, perf, pid=None):
    state = read_json(root / "run.json")
    branch_quality = None
    if state['backend'] == 'brbe':
        from .brbe import inspect
        branch_quality = inspect(root, perf, state.get('brbe_policy'))
        if not branch_quality['usable']:
            raise ValueError('BRBE 画像未通过质量检查：' + '；'.join(branch_quality['errors']) + '；见 brbe-quality.json')
    paths = set()
    for f in root.glob("capture.*.images"):
        for line in f.read_text().splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                paths.add(parts[2])
    if pid:
        maps = Path(f"/proc/{pid}/maps")
        if maps.exists():
            for line in maps.read_text().splitlines():
                f = line.split(None, 5)
                if len(f) == 6 and "x" in f[1] and f[5].startswith("/"):
                    paths.add(f[5])
    try:
        ids = subprocess.check_output([perf, "buildid-list", "-i", str(root / "perf.data")], text=True, env=tool_env(),stderr=subprocess.PIPE,timeout=120)
    except subprocess.SubprocessError as error:
        diagnostic=getattr(error,'stderr',None) or str(error)
        (root/'buildids.stderr').write_text(diagnostic.decode(errors='replace') if isinstance(diagnostic,bytes) else diagnostic)
        raise ValueError('perf buildid-list 失败；见 buildids.stderr') from error
    (root / "buildids.txt").write_text(ids)
    recorded_ids = {}
    for line in ids.splitlines():
        parts = line.split(None, 1)
        if line.strip().startswith("/"):
            paths.add(line.strip())
            continue
        if len(parts) == 2 and parts[1].startswith("/"):
            paths.add(parts[1]); recorded_ids[parts[1]] = parts[0]
    report_args = [perf, "report", "-v", "-i", str(root / "perf.data"), "--stdio",
                   "--sort=dso", "--field-separator=\t", "--percent-limit=0", "--no-children"]
    if state["backend"] == "spe":
        # ARM SPE branch-filtered traces do not synthesize instruction events
        # on all perf versions. Branch synthesis exposes usable hotspot shares.
        report_args += ["--itrace=b"]
    elif state['backend'] == 'brbe':
        report_args += ['--no-branch-stack']
    try:
        report = subprocess.run(report_args,
                                capture_output=True, text=True, env=tool_env(),timeout=120)
    except subprocess.SubprocessError as error:
        raise ValueError('perf report 超时或无法处理画像') from error
    (root / "hotspots.txt").write_text(report.stdout + report.stderr)
    if report.returncode:
        raise ValueError("perf report failed; see hotspots.txt")
    weights, shares = {}, {}
    for line in report.stdout.splitlines():
        f = line.strip().split("\t")
        if f and re.match(r"^[0-9.]+%?$", f[0].strip()):
            if len(f) > 1:
                shares[f[1].strip()] = float(f[0].strip().rstrip("%"))
            for field in f[1:]:
                if field.strip().startswith("/"):
                    weights[field.strip()] = float(f[0].strip().rstrip("%"))
    paths.update(weights)
    if not weights or sum(weights.values()) == 0:
        raise ValueError("no sampled ELF instructions in ROI; check ROI control, event support and capture scope (hotspots.txt)")
    rows_by_path, readable = {}, set()
    for path in sorted(paths):
        try:
            row = ELF(path).metadata()
        except (OSError, ValueError):
            continue
        recorded = recorded_ids.get(path)
        if state['backend']=='brbe' and weights.get(path,0)>0 and (not recorded or not row['build_id']):
            raise ValueError(f'BRBE 热映像缺少可核对的 GNU build-id: {path}；使用 --buildid-all 采集并保留原映像')
        if recorded and row["build_id"] and recorded != row["build_id"]:
            raise ValueError(f"recorded ELF build-id differs: {path}")
        readable.add(path)
        canonical = row["path"]
        if canonical not in rows_by_path:
            row.update(sample_percent=0.0, perf_data=str(root / "perf.data"))
            rows_by_path[canonical] = row
        rows_by_path[canonical]["sample_percent"] += weights.get(path, 0.0)
    rows = list(rows_by_path.values())
    if not rows:
        raise ValueError("no ELF images discovered in perf.data")
    result = {"schema": "tgo.profile.v1", "backend": state["backend"],
              "raw_data": branch_quality['raw_data'] if branch_quality else {"path": str(root / "perf.data"), "sha256": sha256(root / "perf.data")},
              "images": sorted(rows, key=lambda r: -r["sample_percent"]),
              "quality": {"kind": "hardware-sampling", "hardware_frontend_counters": False,
                          "hotspot_weight": "synthesized-branch-events" if state["backend"] == "spe" else "sample-period-share",
                          "scope": "perf inherited task samples; attach covers selected PID and inherited tasks"}}
    resolved = sum(row["sample_percent"] for row in rows)
    total = sum(shares.values())
    unresolved = max(0.0, total-resolved)
    result["quality"].update(resolved_sample_percent=resolved,
        unresolved_sample_percent=unresolved,
        unresolved_dsos=[{"name": name, "sample_percent": share} for name, share in shares.items()
                         if name not in readable])
    if branch_quality is not None:
        result['quality'].update(kind='hardware-branch-stack-sampling', branch_stack=branch_quality,
            scope='perf task inheritance / selected PID threads; samples_per_cpu and top_tasks describe observed coverage')
    if Path(perf).is_file():
        result['perf_decoder'] = {'path': str(Path(perf).absolute()), 'sha256': sha256(perf)}
    write_json(root / "profile.json", result)
    if unresolved >= total/2:
        raise ValueError(f"most samples ({unresolved:.2f}%) do not resolve to readable ELF images; inspect hotspots.txt, container mappings and deleted/JIT code")
    return result
