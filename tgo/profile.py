"""Normalize QEMU observations with loader-provided load biases and exact ELF identities."""
from collections import Counter
from bisect import bisect_right
import hashlib
from pathlib import Path
from .elf import ELF
from .util import write_json


def qemu_profile(root, allow_truncated=False):
    root = Path(root)
    images, cache = {}, {}
    total, windows, truncated = 0, 0, False
    unmapped = 0
    for trace in root.glob("capture.*.trace"):
        imagefile = trace.with_suffix(".images")
        if not imagefile.exists():
            from .util import read_json
            command = read_json(root / "run.json")["command"]
            binary = ELF(command[0])
            if binary.type == 2 and binary.interpreter is None:
                imagefile.write_text(f"M\t0\t{binary.path}\n")
            else:
                raise ValueError(f"loader map missing: {imagefile}; static PIE requires load-bias support")
        loaded = []
        for line in imagefile.read_text().splitlines():
            kind, bias, path = line.split("\t", 2)
            if kind != "M" or not Path(path).is_file():
                continue
            if path not in cache:
                cache[path] = ELF(path)
            elf = cache[path]
            base = int(bias, 16)
            if (base, path) not in [(b, p) for b, p, _ in loaded]:
                loaded.append((base, path, elf))
        intervals = sorted((lo + base, hi + base, path, base)
                           for base, path, elf in loaded
                           for lo, hi, _, flags, _ in elf.loads if flags & 1)
        starts = [r[0] for r in intervals]
        prefix_end = []
        maximum = 0
        for lo, hi, path, base in intervals:
            maximum = max(maximum, hi); prefix_end.append(maximum)
        locations = {}
        def locate(pc):
            if pc in locations:
                return locations[pc]
            i = bisect_right(starts, pc) - 1
            matches = set()
            while i >= 0 and prefix_end[i] > pc:
                lo, hi, path, base = intervals[i]
                if lo <= pc < hi:
                    matches.add((path, pc - base))
                i -= 1
            if len(matches) > 1:
                raise ValueError("overlapping image lifetimes: use perf or shorter ROI")
            result = next(iter(matches)) if matches else None
            locations[pc] = result
            return result
        def image(path):
            if path not in images:
                images[path] = {**cache[path].metadata(), "counts": Counter(), "dynamic_instructions": 0}
            return images[path]
        with trace.open() as handle:
            for line in handle:
                f = line.split("\t")
                if f[0] == "H":
                    total += int(f[1]); truncated |= bool(int(f[2])); windows += int(f[3]); continue
                kind, a, b, n = f[0], int(f[1], 16), int(f[2], 16), int(f[3])
                src, dst = locate(a), locate(b)
                if kind == "N":
                    if src:
                        image(src[0])["dynamic_instructions"] += n
                    else:
                        unmapped += n
                elif kind in ("T", "F"):
                    if src and dst and src[0] == dst[0]:
                        image(src[0])["counts"]["F", src[1], dst[1]] += n
                elif kind == "B":
                    if src:
                        image(src[0])["counts"]["B", src[1], dst[1] if dst and dst[0] == src[0] else 0] += n
                    if dst and (not src or src[0] != dst[0]):
                        image(dst[0])["counts"]["B", 0, dst[1]] += n
    if not total:
        raise ValueError("no instructions recorded; check ROI markers and QEMU logs")
    if truncated and not allow_truncated:
        raise ValueError("QEMU instruction limit reached; increase --max-insns or explicitly --allow-truncated")
    rows = []
    for path, row in images.items():
        counts = row.pop("counts")
        row["image_id"] = row["sha256"][:16] + "-" + hashlib.sha256(path.encode()).hexdigest()[:8]
        dest = root / (row["image_id"] + ".pa")
        with dest.open("w") as f:
            for (kind, a, b), n in sorted(counts.items()):
                f.write(f"{kind} {a:x} {b:x} {n}" + (" 0" if kind == "B" else "") + "\n")
        row["preaggregate"] = str(dest)
        rows.append(row)
    result = {"schema": "tgo.profile.v1", "backend": "qemu", "instructions": total,
              "unmapped_instructions": unmapped, "marker_windows": windows,
              "truncated": truncated, "images": sorted(rows, key=lambda x: -x["dynamic_instructions"]),
              "quality": {"kind": "dynamic-tb-counts", "hardware_frontend_counters": False,
                          "scope": "QEMU process and threads; native exec children excluded"}}
    write_json(root / "profile.json", result)
    return result
