from pathlib import Path
import re
import subprocess
from .elf import ELF
from .util import read_json, write_json, tool_env


def perf_profile(root, perf, pid=None):
    state = read_json(root / "run.json")
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
    ids = subprocess.check_output([perf, "buildid-list", "-i", str(root / "perf.data")], text=True, env=tool_env())
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
    report = subprocess.run(report_args,
                            capture_output=True, text=True, env=tool_env())
    (root / "hotspots.txt").write_text(report.stdout + report.stderr)
    if report.returncode:
        raise ValueError("perf report failed; see hotspots.txt")
    weights = {}
    for line in report.stdout.splitlines():
        f = line.strip().split("\t")
        if f and re.match(r"^[0-9.]+%?$", f[0].strip()):
            for field in f[1:]:
                if field.strip().startswith("/"):
                    weights[field.strip()] = float(f[0].strip().rstrip("%"))
    paths.update(weights)
    if not weights or sum(weights.values()) == 0:
        raise ValueError("no sampled ELF instructions in ROI; check ROI control, event support and capture scope (hotspots.txt)")
    rows = []
    for path in sorted(paths):
        try:
            row = ELF(path).metadata()
        except (OSError, ValueError):
            continue
        recorded = recorded_ids.get(path)
        if recorded and row["build_id"] and recorded != row["build_id"]:
            raise ValueError(f"recorded ELF build-id differs: {path}")
        row.update(sample_percent=weights.get(path, 0.0), perf_data=str(root / "perf.data"))
        rows.append(row)
    if not rows:
        raise ValueError("no ELF images discovered in perf.data")
    result = {"schema": "tgo.profile.v1", "backend": state["backend"],
              "images": sorted(rows, key=lambda r: -r["sample_percent"]),
              "quality": {"kind": "hardware-sampling", "hardware_frontend_counters": False,
                          "hotspot_weight": "synthesized-branch-events" if state["backend"] == "spe" else "sample-period-share",
                          "scope": "perf inherited task samples; attach covers selected PID and inherited tasks"}}
    write_json(root / "profile.json", result)
    return result
