from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from tgo.gopgo import adapter_env, profile_totals, blend_profiles, correctness_fixture
from tgo.go_layout import linker_overlay, layout_report
from tgo.runtime import build_runtime, make_launcher


@unittest.skipUnless(shutil.which("go"), "Go SDK unavailable")
class GoToolAdapter(unittest.TestCase):
    def test_private_linker_reorders_final_elf_and_preserves_runtime_metadata(self):
        go = shutil.which("go")
        goroot = subprocess.check_output([go, "env", "GOROOT"], text=True).strip()
        if not (Path(goroot) / "src/cmd/link/internal/loader/loader.go").exists():
            self.skipTest("Go linker source unavailable")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); build_runtime(root)
            _, overlay = linker_overlay(goroot, root / "overlay")
            linker = root / "link"
            env = os.environ.copy(); env["GOFLAGS"] = ""
            subprocess.run([go, "build", "-pgo=off", "-overlay="+str(overlay), "-o", linker, "cmd/link"],
                           env=env, check=True, capture_output=True)
            tool_dir = subprocess.check_output([go, "env", "GOTOOLDIR"], text=True).strip()
            original = str(Path(tool_dir) / "link")
            version = subprocess.check_output([original, "-V=full"], text=True).strip()
            order = root / "order.txt"; order.write_text("runtime.GC\nruntime.gcStart\nmain.main\nmain.recoverCheck\n")
            for runtime in ("0", "1"):
                with self.subTest(runtime=runtime):
                    trial = root / ("trial-"+runtime); trial.mkdir()
                    changed = adapter_env(env, root, original, version, "layout-test-"+runtime, linker)
                    changed.update(TGO_GO_LAYOUT_ORDER=str(order), TGO_GO_LAYOUT_REPORT=str(trial / "layout.tsv"),
                                   TGO_GO_LAYOUT_RUNTIME=runtime)
                    correctness_fixture(go, changed, trial, 180)
                    nm = subprocess.check_output([go, "tool", "nm", "-size", "-sort", "address", trial / "correctness-program"], text=True)
                    report = layout_report(trial / "layout.tsv", {"main.main": 10, "main.recoverCheck": 5,
                        "runtime.GC": 20, "runtime.gcStart": 15}, nm)
                    self.assertEqual(report["matched_symbols"], 4 if runtime == "1" else 2)
                    self.assertEqual(report["final_address_order_inversions"], 0)
                    self.assertGreater(report["moved_symbols"], 0)
                    if runtime == "1":
                        self.assertGreater(report["runtime_moved_symbols"], 0)

    def test_cache_identity_replaces_compiler_and_disable_uses_original(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); build_runtime(root)
            go = shutil.which("go")
            tool_dir = subprocess.check_output([go, "env", "GOTOOLDIR"], text=True).strip()
            original = str(Path(tool_dir) / "compile")
            optimized = root / "compile-copy"; shutil.copy2(original, optimized)
            version = subprocess.check_output([original, "-V=full"], text=True).strip()
            env = os.environ.copy(); env["GOCACHE"] = str(root / "cache")
            source = root / "package.go"; source.write_text("package fixture\nfunc Add(a,b int) int{return a+b}\n")
            command = [go, "build", "-o", str(root / "fixture.a"), str(source)]
            subprocess.run(command, env=env, check=True, capture_output=True)
            adapted = adapter_env({}, root, original, version, "test-cache-id", optimized)
            adapted.pop("GOFLAGS")
            adapted["TGO_GO_TOOLEXEC"] = str(root / "go-tool")
            entry = root / "go.tgo"
            make_launcher(root, entry, [go], [], adapted)
            env["TGO_GO_EXEC_LOG"] = str(root / "executed.tsv")
            subprocess.run([entry, *command[1:]], env=env, check=True, capture_output=True)
            self.assertIn(str(optimized), (root / "executed.tsv").read_text())
            probe = adapter_env(env, root, original, version, "test-cache-id", optimized)
            line = subprocess.check_output([root / "go-tool", original, "-V=full"], env=probe, text=True)
            self.assertIn("tgo=test-cache-id", line)
            probe["TGO_DISABLE"] = "1"
            self.assertEqual(subprocess.check_output([root / "go-tool", original, "-V=full"], env=probe, text=True).strip(), version)

    def test_actual_compiler_cpu_profiles_are_readable(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); build_runtime(root)
            go=shutil.which("go")
            original=str(Path(subprocess.check_output([go,"env","GOTOOLDIR"],text=True).strip())/"compile")
            version=subprocess.check_output([original,"-V=full"],text=True).strip()
            source=root/"large.go"
            source.write_text("package fixture\n" + ''.join(f"func F{i}(x int) int {{ for j:=0;j<50;j++{{x=(x*33+j)^(x>>3)}};return x+{i} }}\n" for i in range(4000)))
            profiles=root/"profiles"; profiles.mkdir()
            env=adapter_env(os.environ,root,original,version,"profile-test")
            env["TGO_GO_PROFILE_DIR"]=str(profiles)
            subprocess.run([root/"go-tool",original,"-p","fixture","-o",root/"fixture.a",source],env=env,check=True,capture_output=True)
            profile=next(profiles.glob("*.pprof"))
            raw=subprocess.check_output([go,"tool","pprof","-raw",profile],text=True,stderr=subprocess.DEVNULL)
            self.assertGreater(profile_totals(raw)["samples"],0)
            merged=root/"merged.pprof"
            subprocess.run([go,"tool","pprof","-proto","-symbolize=none","-output",merged,profile],check=True,capture_output=True)
            subprocess.run([go,"tool","preprofile","-i",merged,"-o",root/"preprocessed.pgo"],check=True,capture_output=True)
            blended,result=blend_profiles(go,merged,merged,root,os.environ,60)
            self.assertEqual(result["format"],"pprof")
            subprocess.run([go,"tool","preprofile","-i",blended,"-o",root/"blend-verified.pgo"],check=True,capture_output=True)


if __name__ == "__main__":
    unittest.main()
