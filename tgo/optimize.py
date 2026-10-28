import os
from pathlib import Path
import shutil
import subprocess
import hashlib
import shlex
from .elf import ELF
from .runtime import build_runtime, make_launcher
from .util import read_json, write_json, run, sha256, tool, tool_env


def eligible(row):
    name = Path(row["path"]).name
    if row["go"]:
        return "Go pclntab/stack maps require a Go-aware rewriter; layout rewrite disabled"
    if name.startswith(("ld-linux", "ld-musl", "libc.so", "libpthread.so", "libdl.so", "libtgo_")):
        return "loader/runtime support image excluded"
    if not row["symbols"]:
        return "symbol table absent; provide an unstripped matching ELF"
    if row["machine"] not in (62, 183):
        return "unsupported ISA"
    if not row.get("dynamic_instructions", row.get("sample_percent", 0)):
        return "no samples in selected ROI"
    return None


def optimize(args):
    source = Path(args.run).resolve()
    state, profile = read_json(source / "run.json"), read_json(source / "profile.json")
    if state["state"] != "recorded":
        raise ValueError("only successfully recorded runs can be optimized")
    if not state["command"]:
        raise ValueError("attach profile needs a replay command: use tgo bind before optimizing")
    out = Path(args.output).absolute()
    out.mkdir(parents=True, exist_ok=False)
    build_runtime(out)
    bolt = tool("llvm-bolt", args.bolt)
    p2b = tool("perf2bolt", args.perf2bolt or str(Path(bolt).with_name("perf2bolt")))
    patchelf = tool("patchelf", required=False)
    selected = []
    skipped = []
    for row in profile["images"]:
        if args.image and not any(x == row["path"] or x == Path(row["path"]).name for x in args.image):
            continue
        reason = eligible(row)
        if reason:
            skipped.append({"path": row["path"], "reason": reason})
        elif len(selected) < args.top:
            selected.append(row)
    result = {"schema": "tgo.build.v1", "source_run": str(source), "profile_sha256": sha256(source / "profile.json"),
              "bolt_sha256": sha256(bolt), "images": [], "skipped": skipped, "state": "building"}
    write_json(out / "build.json", result)
    try:
        aggregate_env = tool_env()
        if profile["backend"] != "qemu":
            perf = tool("perf")
            helper = out / "tools"
            helper.mkdir()
            shim = helper / "perf"
            script = "#!/bin/sh\n"
            if profile["backend"] == "spe":
                bridge = out / "libtgo_perf_spe.so"
                if not bridge.exists():
                    raise ValueError("SPE compatibility bridge absent; rebuild the TGO package")
                script += ('if [ "${1-}" = script ] && [ "${2-}" = -F ] && [ "${3-}" = pid,brstack ]; then\n'
                           '  shift 3\n  case "${1-}" in --itrace=*) shift;; esac\n'
                           '  exec ' + shlex.quote(perf) + ' script --itrace=b --dlfilter=' + shlex.quote(str(bridge)) + ' -F pid,ip,addr "$@"\nfi\n')
                result["spe_bridge"] = {"path": str(bridge), "sha256": sha256(bridge),
                                        "prediction": "unknown unless event explicitly denotes a miss"}
            shim.write_text(script + "exec " + shlex.quote(perf) + ' "$@"\n')
            shim.chmod(0o755)
            aggregate_env["PATH"] = str(helper) + os.pathsep + aggregate_env.get("PATH", "")
            result["perf"] = {"path": perf, "sha256": sha256(perf)}
        for row in selected:
            original = Path(row["path"])
            if sha256(original) != row["sha256"]:
                raise ValueError(f"source changed since capture: {original}")
            image_id = row.get("image_id") or row["sha256"][:16] + "-" + hashlib.sha256(row["path"].encode()).hexdigest()[:8]
            image_dir = out / "images" / image_id
            image_dir.mkdir(parents=True)
            fdata = image_dir / "profile.fdata"
            cmd = [p2b, original, "-o", fdata]
            if profile["backend"] == "qemu":
                cmd += ["-pa", "-p", row["preaggregate"]]
            else:
                cmd += ["-p", row["perf_data"]]
                cmd += ["-spe"] if profile["backend"] == "spe" else ([] if args.branch_stack or state.get("branch_stack") else ["-nl"])
            aggregate_env["TGO_SPE_STATS"] = str(image_dir / "spe-bridge.json")
            aggregation = run(cmd, image_dir / "aggregate.log", env=aggregate_env)
            if not fdata.exists() or not fdata.stat().st_size:
                raise ValueError(f"empty BOLT profile: {original}")
            target = image_dir / original.name
            flags = ["-reorder-blocks=ext-tsp", "-reorder-functions=" + ("hfsort+" if row["relocations"] else "none"),
                     "-split-functions", "-split-all-cold", "-split-strategy=profile2", "-dyno-stats",
                     "-align-functions=4", "-align-functions-max-bytes=4", "-enable-bat", "-thread-count=4"]
            if row["machine"] == 183:
                flags += ["-group-stubs=false"]
                # Existing pages BOLT supports ADR/ADRP reconstruction for non-reloc ARM inputs.
                if not row["relocations"]:
                    flags += ["-aarch64-relaxation-non-reloc"]
            flags += args.bolt_arg
            optimization = run([bolt, original, "-data=" + str(fdata), "-o", target, *flags], image_dir / "bolt.log")
            # Preserve $ORIGIN search semantics after moving an image out of its original directory.
            origin = str(original.parent)
            if patchelf and (row["type"] == 3 or row["interpreter"]):
                rpath = subprocess.check_output([patchelf, "--print-rpath", original], text=True, env=tool_env()).strip()
                expanded = rpath.replace("${ORIGIN}", origin).replace("$ORIGIN", origin)
                paths = ':'.join(p for p in (expanded, origin) if p)
                preserve = ["--force-rpath"] if row.get("rpath_kind") == "rpath" else []
                run([patchelf, *preserve, "--set-rpath", paths, target], image_dir / "rpath.log")
            elif row["interpreter"] is None and row["type"] == 3 and b"$ORIGIN" in ELF(original).data:
                raise ValueError("DSO uses $ORIGIN; patchelf is required to preserve dependency search")
            # Reparse emitted ELF: an exit code alone is not an artifact check.
            output_meta = ELF(target).metadata()
            os.chmod(target, original.stat().st_mode & 0o777)
            result["images"].append({"original": str(original), "optimized": str(target),
                                     "input": row, "output": output_meta,
                                     "aggregation": aggregation, "optimization": optimization})
            write_json(out / "build.json", result)
        if not result["images"]:
            raise ValueError("no eligible hot ELF images; see build.json skipped reasons")
        entry = Path(args.entry).absolute() if args.entry else out / "run"
        command = state["command"]
        # Moving the main ELF changes $ORIGIN too. Keep a sibling copy only on explicit entry install.
        result["entry_mode"] = args.entry_mode
        deployed = command[:1] if args.entry_mode == "executable" else command
        result["entry"] = make_launcher(out, entry, deployed, result["images"], state["env"],
                                         None if args.entry_mode == "executable" else state["cwd"])
        result["state"] = "candidate"
        write_json(out / "build.json", result)
        if args.validate:
            from .validate import validate
            verification = validate(out, args.validate, args.timeout, args.metric)
            result["validation"] = verification
            result["state"] = "validated" if verification["equivalent"] else "rejected"
            if not verification["equivalent"]:
                entry.unlink()
                raise ValueError("candidate failed output/load validation; entry withdrawn")
    except BaseException as e:
        result.update(state="failed", error=str(e)); raise
    finally:
        write_json(out / "build.json", result)
    return result
