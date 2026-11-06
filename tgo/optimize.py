import os
from pathlib import Path
import subprocess
import hashlib
import shlex
import shutil
from .elf import ELF, is_executable_image, normalize_loads
from .runtime import build_runtime, make_launcher,publication_environment
from .util import read_json, write_json, run, sha256, tool, tool_env, IdentityError
from .capture_set import SCHEMA as CAPTURE_SET, verified_sources, observations
from .recipes import resolve_recipe, recovered_policy
from .binding import bind_now

RECOVERY = ["-relocs", "-force-patch", "-pgo128-trampoline-mode", "-pgo128-recover-code-relocs",
            "-aarch64-relaxation-non-reloc"]


def eligible(row, allow_stripped=False):
    name = Path(row["path"]).name
    if row["go"]:
        return "Go pclntab/stack maps require a Go-aware rewriter; layout rewrite disabled"
    if Path(row['path']).is_file():
        from .capabilities import classify_profile_image
        if classify_profile_image(row)['runtime']=='go':return 'Go metadata requires a Go-aware rewriter'
    if row.get("profile_unavailable"):
        return "no compatible object profile in this hybrid training set"
    if name.startswith(("ld-linux", "ld-musl", "libc.so", "libpthread.so", "libdl.so", "libtgo_")):
        return "loader/runtime support image excluded"
    if not row["symbols"] and not allow_stripped:
        return "symbol table absent; provide an unstripped matching ELF or use automatic policy/--allow-stripped"
    if row["machine"] not in (62, 183):
        return "unsupported ISA"
    if not row.get("dynamic_instructions", row.get("sample_percent", 0)):
        return "no samples in selected ROI"
    return None


def select_images(args, state, profile):
    selected, skipped = [], []
    automatic = args.bolt_mode == "auto"
    matched = set()
    def matches(selector, path):
        if selector == path or selector == Path(path).name:
            return True
        try:
            # Match symlinks and hard links to the recorded object, never a
            # different unloaded copy merely because its contents are equal.
            return Path(selector).samefile(path)
        except OSError:
            return False
    ranked = sorted(profile["images"], key=lambda row: row.get("dynamic_instructions", row.get("sample_percent", 0)), reverse=True)
    for row in ranked:
        selectors = {x for x in args.image if matches(x, row["path"])}
        matched.update(selectors)
        if args.image and not selectors:
            continue
        reason = eligible(row, args.allow_stripped or automatic)
        if not reason and is_executable_image(row) and Path(row["path"]).resolve() != Path(state["command"][0]).resolve():
            reason = "child executable needs an exec/toolchain adapter; loader redirection only applies to DSOs"
        if not reason and args.bolt_mode == "recover" and (row["machine"] != 183 or is_executable_image(row) or row["type"] != 3):
            reason = "recovery policy requires an AArch64 shared object"
        if reason:
            skipped.append({"path": row["path"], "reason": reason})
        elif len(selected) < args.top:
            selected.append(row)
    missing = set(args.image) - matched
    if missing:
        raise ValueError("显式 --image 未匹配画像中的映像（可用路径、文件名或同 inode 别名）: " + ", ".join(sorted(missing)))
    return selected, skipped


def verify_images(images):
    """Identity mismatches are fatal, never a recoverable rewrite failure."""
    for image in images:
        for path, expected in ((image["original"], image["input"]["sha256"]),
                               (image["optimized"], image["output"]["sha256"])):
            verify_identity(path, expected)


def verify_identity(path, expected):
    try:
        digest = sha256(path)
    except OSError as error:
        raise IdentityError(f"recorded input is no longer readable: {path}") from error
    if digest != expected:
        raise IdentityError(f"recorded input changed: {path}")


def tool_capabilities(binary, cache):
    if binary not in cache:
        cache[binary] = subprocess.check_output([binary, "--help-hidden"], text=True, env=tool_env(), timeout=30)
    return cache[binary]


def image_policy(args, row, capabilities):
    mode, reason = args.bolt_mode, "explicit policy"
    if mode == "auto":
        wants_recovery = row["machine"] == 183 and row["type"] == 3 and not is_executable_image(row) and not row["relocations"]
        recovery_tool = tool("llvm-bolt-recover", args.bolt, required=False) if wants_recovery else None
        if recovery_tool and "pgo128-recover-code-relocs" in tool_capabilities(recovery_tool, capabilities):
            mode, reason = "recover", "AArch64 DSO has no retained code relocations; recovery tool available"
        else:
            mode, reason = "standard", "retained relocations, executable/ISA policy, or recovery tool unavailable"
    bolt = tool("llvm-bolt-recover" if mode == "recover" else "llvm-bolt", args.bolt)
    if mode == "recover" and "pgo128-recover-code-relocs" not in tool_capabilities(bolt, capabilities):
        raise ValueError("selected BOLT lacks the recorded AArch64 recovery capability")
    sibling = Path(bolt).with_name("perf2bolt")
    if not args.perf2bolt and not sibling.is_file():
        raise ValueError(f"matching perf2bolt missing next to {bolt}")
    p2b = tool("perf2bolt", args.perf2bolt or str(sibling))
    return {"mode": mode, "reason": reason, "bolt": bolt, "perf2bolt": p2b,
            "allow_stripped": args.allow_stripped or args.bolt_mode == "auto"}


def aggregate_environment(out, profile, result):
    env = tool_env()
    if profile["backend"] in ("qemu", "hybrid"):
        return env
    decoder=profile.get('perf_decoder')
    perf = tool("perf", decoder['path'] if decoder else None)
    if decoder:
        verify_identity(perf, decoder['sha256'])
    helper = out / "tools"; helper.mkdir()
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
    shim.write_text(script + "exec " + shlex.quote(perf) + ' "$@"\n'); shim.chmod(0o755)
    env["PATH"] = str(helper) + os.pathsep + env.get("PATH", "")
    result["perf"] = {"path": perf, "sha256": sha256(perf)}
    return env


def aggregate_observation(args, row, state, backend, directory, policy, aggregate_env):
    original = Path(row["path"])
    recover = policy["mode"] == "recover"
    recovery = RECOVERY if recover else []
    full = recovered_policy(args, row)
    if full:
        recovery = [*recovery, "-pgo128-assume-code-pages-safe"]
    fdata = directory / "profile.fdata"
    cmd = [policy["perf2bolt"], original, "-o", fdata]
    if backend == "qemu":
        cmd += ["-pa", "-p", row["preaggregate"]]
    else:
        cmd += ["-p", row["perf_data"]]
        cmd += ["-spe"] if backend == "spe" else ([] if backend == 'brbe' or args.branch_stack or state.get("branch_stack") else ["-nl"])
    cmd += recovery
    if policy["allow_stripped"]:
        cmd += ["-allow-stripped"]
    aggregate_env = dict(aggregate_env); aggregate_env["TGO_SPE_STATS"] = str(directory / "spe-bridge.json")
    aggregation = run(cmd, directory / "aggregate.log", env=aggregate_env, timeout=args.timeout)
    return fdata, aggregation


def aggregate_image(args, row, state, profile, image_dir, policy, aggregate_env):
    if row.get("native_fdata"):
        verify_identity(row["native_fdata"], row["native_fdata_sha256"])
        verify_identity(row["native_profile"], row["native_profile_sha256"])
        native = read_json(row["native_profile"])
        if native["source_sha256"] != row["sha256"]:
            raise IdentityError("native edge profile belongs to a different rewrite input")
        fdata = image_dir / "profile.fdata"
        shutil.copy2(row["native_fdata"], fdata)
        return fdata, {"strategy": "native-bolt-roi-edges", "manifest": row["native_profile"],
                       "manifest_sha256": row["native_profile_sha256"], "sha256": sha256(fdata)}
    if row.get("profile_backend") == "qemu":
        verify_identity(row["preaggregate"], row["preaggregate_sha256"])
        fdata, record = aggregate_observation(args, row, state, "qemu", image_dir, policy, aggregate_env)
        if not fdata.exists() or not fdata.stat().st_size:
            raise ValueError("QEMU object profile emitted no BOLT counts")
        return fdata, {"strategy": "independent-qemu-object-profile", "source_run": row["profile_source_run"],
                       "preaggregate_sha256": row["preaggregate_sha256"], "aggregation": record}
    inputs = observations(profile, row)
    if not inputs:
        raise ValueError(f"no positive observations for {row['path']}")
    if profile.get("schema") != CAPTURE_SET:
        fdata, record = aggregate_observation(args, row, state, profile["backend"], image_dir, policy, aggregate_env)
        if not fdata.exists() or not fdata.stat().st_size:
            raise ValueError(f"empty BOLT profile: {row['path']}")
        return fdata, record
    records, paths, stats = [], [], []
    for index, original in enumerate(inputs):
        directory = image_dir / "observations" / str(index); directory.mkdir(parents=True)
        fdata, record = aggregate_observation(args, original, state, profile["backend"], directory, policy, aggregate_env)
        used = fdata.exists() and fdata.stat().st_size > 0
        records.append({"path": str(fdata), "sha256": sha256(fdata) if fdata.exists() else None,
                        "used": used, "aggregation": record})
        if used:
            paths.append(fdata)
        if profile["backend"] == "spe":
            stats.append(read_json(directory / "spe-bridge.json"))
    if not paths:
        raise ValueError(f"no usable BOLT counts across captures: {row['path']}")
    merged = image_dir / "profile.fdata"
    merger = tool("merge-fdata")
    merging = run([merger, "-q", "-o", merged, *paths], image_dir / "merge.log", timeout=args.timeout)
    if not merged.exists() or not merged.stat().st_size:
        raise ValueError("merge-fdata emitted an empty profile")
    if stats:
        summary = {key: sum(s[key] for s in stats) for key in ("branch_samples", "emitted", "unavailable_code", "unsupported_opcode")}
        summary.update(prediction="unknown except named miss events", input_captures=len(stats), inputs=stats)
        write_json(image_dir / "spe-bridge.json", summary)
    return merged, {"strategy": "independent-ELF-normalization-and-count-merge", "inputs": records,
                    "merge": merging, "merge_tool_sha256": sha256(merger)}


def rewrite_image(args, row, state, profile, image_dir, policy, aggregate_env, patchelf):
    original = Path(row["path"])
    recover = policy["mode"] == "recover"
    recovery = RECOVERY if recover else []
    full = recovered_policy(args, row)
    level=getattr(args,'optimization_level','balanced')
    binding = None
    binding_skip = None
    rewritten_input = original
    if full:
        if not recover:
            raise ValueError("full code recovery requires recovery BOLT")
        recovery = [*recovery, "-pgo128-assume-code-pages-safe"]
    if recover and level!='conservative':
        from .capabilities import inspect_image
        structure=inspect_image(original)
        wants_plt=structure['runtime']!='cpython' and '.rela.plt' in ELF(original).sections
    else:wants_plt=False
    if wants_plt:
        rewritten_input = image_dir / (original.name + ".bind-now")
        try:binding = bind_now(original, rewritten_input)
        except (ValueError,OSError) as error:
            if isinstance(error,IdentityError):raise
            binding_skip=str(error);rewritten_input=original
    fdata, aggregation = aggregate_image(args, row, state, profile, image_dir, policy, aggregate_env)
    target = image_dir / original.name
    flags = ["-reorder-blocks=ext-tsp", "-reorder-functions=" + ("hfsort+" if row["relocations"] or recover else "none"),
             "-split-functions", "-split-all-cold", "-split-strategy=profile2", "-dyno-stats",
             "-align-functions=4", "-align-functions-max-bytes=4", "-enable-bat", "-thread-count=4"]
    if getattr(args, "_recipe_plan", None):
        flags += ["-cold-threshold=0"]
    if row["machine"] == 183:
        flags += ["-group-stubs=false"]
        if not row["relocations"] and not recover:
            flags += ["-aarch64-relaxation-non-reloc"]
    if recover:
        flags += [*recovery, "-use-old-text=false"]
        if not full:
            flags += ["-lite", "-lite-threshold-count=1"]
    if level!='conservative':
        flags += ["-eliminate-unreachable", "-peepholes=all", "-split-eh"]
    if level=='aggressive':
        flags += ["-simplify-conditional-tail-calls", "-sctc-mode=always"]
    if binding:flags += ["-plt=all"]
    if policy["allow_stripped"]:
        flags += ["-allow-stripped"]
    flags += args.bolt_arg
    optimization = run([policy["bolt"], rewritten_input, "-data=" + str(fdata), "-o", target, *flags],
                       image_dir / "bolt.log", timeout=args.timeout)
    shutil.copy2(target,target.with_name(target.name+'.bolt-output'))
    normalization = None
    if recover:
        raw = target.with_name(target.name + ".raw"); target.rename(raw)
        normalization = normalize_loads(raw, target)
    origin = str(original.parent)
    if patchelf and (row["type"] == 3 or row["interpreter"]):
        rpath = subprocess.check_output([patchelf, "--print-rpath", original], text=True, env=tool_env(), timeout=30).strip()
        expanded = rpath.replace("${ORIGIN}", origin).replace("$ORIGIN", origin)
        paths = ':'.join(p for p in (expanded, origin) if p)
        preserve = ["--force-rpath"] if row.get("rpath_kind") == "rpath" else []
        run([patchelf, *preserve, "--set-rpath", paths, target], image_dir / "rpath.log", timeout=30)
    elif row["interpreter"] is None and row["type"] == 3 and b"$ORIGIN" in ELF(original).data:
        raise ValueError("DSO uses $ORIGIN; patchelf is required to preserve dependency search")
    before_final=target.with_name(target.name+'.pre-final-normalize');target.rename(before_final)
    final_normalization=normalize_loads(before_final,target)
    normalization={'pre_rpath':normalization,'final':final_normalization}
    output = ELF(target).metadata()
    os.chmod(target, original.stat().st_mode & 0o777)
    result = {"original": str(original), "optimized": str(target), "input": row, "output": output,
              "fdata_sha256": sha256(fdata), "recovered_relocations": recover,
              "optimization_level":level,"binding_skip_reason":binding_skip,
              "load_order_normalization": normalization, "aggregation": aggregation, "optimization": optimization,
              "policy": {**policy, "bolt_sha256": sha256(policy["bolt"]), "perf2bolt_sha256": sha256(policy["perf2bolt"])}}
    if binding:
        result.update(binding=binding, recipe="structural-v1", full_function_scope=full)
    if profile["backend"] == "spe" and not row.get("native_fdata") and row.get("profile_backend") != "qemu":
        result["spe_conversion"] = read_json(image_dir / "spe-bridge.json")
    return result


def optimize(args):
    source = Path(args.run).resolve()
    state, profile = read_json(source / "run.json"), read_json(source / "profile.json")
    args._recipe_plan = resolve_recipe(args, state, profile)
    if state.get("backend") == "go-cpu":
        raise ValueError("Go CPU profiles require tune --strategy go-compiler")
    if state["state"] != "recorded":
        raise ValueError("only successfully recorded runs can be optimized")
    if not state["command"]:
        raise ValueError("attach profile needs a replay command: use tgo bind before optimizing")
    out = Path(args.output).absolute(); out.mkdir(parents=True, exist_ok=False)
    selected, skipped = select_images(args, state, profile)
    result = {"schema": "tgo.build.v1", "source_run": str(source), "profile_sha256": sha256(source / "profile.json"),
              "images": [], "skipped": skipped, "failed_images": [], "state": "building",
              "automatic_images": not bool(args.image), "bolt_policy": args.bolt_mode,
              "recipe": args._recipe_plan,
              "selected_inputs": [row["path"] for row in selected],
              "workload": {k: state[k] for k in ("command", "cwd", "env")}}
    write_json(out / "build.json", result)
    try:
        if profile.get("baseline_capture_ref"):
            from .hybrid import verify_reference
            verify_reference(profile["baseline_capture_ref"])
        if profile.get("schema") == CAPTURE_SET:
            verified_sources(profile)
        if profile.get("library_capture_ref"):
            verified_sources({"sources": [profile["library_capture_ref"]]})
        if profile.get("raw_data"):
            verify_identity(profile["raw_data"]["path"], profile["raw_data"]["sha256"])
        for row in selected:
            verify_identity(row["path"], row["sha256"])
            if row.get("native_fdata"):
                verify_identity(row["native_fdata"], row["native_fdata_sha256"])
                verify_identity(row["native_profile"], row["native_profile_sha256"])
            if row.get("preaggregate_sha256"):
                verify_identity(row["preaggregate"], row["preaggregate_sha256"])
        build_runtime(out)
        env = aggregate_environment(out, profile, result)
        patchelf = tool("patchelf", required=False)
        capabilities = {}
        for row in selected:
            image_id = row.get("image_id") or row["sha256"][:16] + "-" + hashlib.sha256(row["path"].encode()).hexdigest()[:8]
            image_dir = out / "images" / image_id; image_dir.mkdir(parents=True)
            try:
                policy = image_policy(args, row, capabilities)
                image = rewrite_image(args, row, state, profile, image_dir, policy, env, patchelf)
                replacement = next((r for r in state.get("training_images", []) if r["optimized"] == row["path"]), None)
                if replacement:
                    image["rewrite_input"] = image["input"]
                    image["original"] = replacement["original"]
                    image["input"] = replacement["input"]
                    image["training_replacement"] = replacement
                result["images"].append(image)
            except IdentityError:
                raise
            except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
                result["failed_images"].append({"path": row["path"], "error": str(error), "logs": str(image_dir)})
            write_json(out / "build.json", result)
        for row in selected:
            verify_identity(row["path"], row["sha256"])
            if row.get("native_fdata"):
                verify_identity(row["native_fdata"], row["native_fdata_sha256"])
                verify_identity(row["native_profile"], row["native_profile_sha256"])
        if profile.get("schema") == CAPTURE_SET:
            verified_sources(profile)
        if profile.get("library_capture_ref"):
            verified_sources({"sources": [profile["library_capture_ref"]]})
        if profile.get("baseline_capture_ref"):
            verify_reference(profile["baseline_capture_ref"])
        if not result["images"]:
            raise ValueError("no hot ELF images were rewritten; see build.json skipped and failed_images")
        verify_images(result["images"])
        result["partial"] = bool(result["failed_images"])
        entry = Path(args.entry).absolute() if args.entry else out / "run"
        result["entry_mode"] = args.entry_mode
        deployed = state["command"][:1] if args.entry_mode == "executable" else state["command"]
        affinity=getattr(args,'publish_affinity','inherit');result['published_affinity']=affinity
        result["entry"] = make_launcher(out, entry, deployed, result["images"], publication_environment(state["env"],affinity),
                                         None if args.entry_mode == "executable" else state["cwd"])
        result["state"] = "candidate"; write_json(out / "build.json", result)
        if args.validate:
            from .validate import validate
            verification = validate(out, args.validate, args.timeout, args.metric)
            result["validation"] = verification
            result["state"] = "validated" if verification["equivalent"] else "rejected"
            if not verification["equivalent"]:
                raise ValueError("candidate failed output/load validation; entry withdrawn")
    except BaseException as error:
        if result.get("entry"):
            Path(result["entry"]).unlink(missing_ok=True)
        result.update(state="failed", error=str(error)); raise
    finally:
        write_json(out / "build.json", result)
    return result
