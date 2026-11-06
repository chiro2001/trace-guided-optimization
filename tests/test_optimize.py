import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tgo.elf import ELF
from tgo.optimize import optimize, image_policy, select_images, IdentityError
from tgo.util import write_json, read_json


def arguments(root, capture):
    return argparse.Namespace(run=str(capture), output=str(root / "out"), image=[], top=3,
        bolt_mode="auto", allow_stripped=False, bolt=None, perf2bolt=None, bolt_arg=[],
        branch_stack=False, timeout=10, entry=None, entry_mode="command", validate=1, metric=None)


class ObjectFailureFallback(unittest.TestCase):
    def fixture(self, root):
        source = root / "lib.c"; source.write_text("int f(void){return 7;}\n")
        libs = [root / "a.so", root / "b.so"]
        subprocess.run(["cc", "-shared", "-fPIC", source, "-o", libs[0]], check=True)
        shutil.copy2(libs[0], libs[1])
        rows = []
        for n, lib in enumerate(libs):
            row = ELF(lib).metadata(); row["dynamic_instructions"] = 100-n
            rows.append(row)
        capture = root / "capture"; capture.mkdir()
        driver = "import ctypes;print(sum(ctypes.CDLL(p).f() for p in " + repr(list(map(str, libs))) + "))"
        write_json(capture / "run.json", {"state": "recorded", "command": [sys.executable, "-c", driver],
            "cwd": str(root), "env": {}, "backend": "qemu"})
        write_json(capture / "profile.json", {"backend": "qemu", "images": rows})
        return capture, libs

    def test_failed_object_keeps_original_while_other_dso_is_really_redirected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); capture, libs=self.fixture(root)
            def rewrite(args, row, state, profile, directory, policy, env, patchelf):
                if row["path"] == str(libs[1]):
                    raise RuntimeError("fixture rewriter rejects this object")
                target=directory/"a.so";shutil.copy2(row["path"],target)
                return {"original":row["path"],"optimized":str(target),"input":row,"output":ELF(target).metadata()}
            with patch("tgo.optimize.image_policy", return_value={}), patch("tgo.optimize.rewrite_image", side_effect=rewrite):
                built=optimize(arguments(root,capture))
            self.assertTrue(built["partial"])
            self.assertEqual(built["failed_images"][0]["path"],str(libs[1]))
            self.assertTrue(built["validation"]["equivalent"])
            self.assertEqual(built["validation"]["pairs"][0]["candidate"]["loaded_optimized"], [built["images"][0]["optimized"]])
            self.assertEqual(subprocess.check_output([built["entry"]],text=True),"14\n")

    def test_source_drift_is_fatal_instead_of_being_treated_as_an_object_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); capture, libs=self.fixture(root)
            with libs[1].open("ab") as stream:stream.write(b"changed")
            with patch("tgo.optimize.rewrite_image") as rewrite:
                with self.assertRaises(IdentityError):optimize(arguments(root,capture))
                rewrite.assert_not_called()
            self.assertFalse((root/"out/run").exists())
            self.assertEqual(read_json(root/"out/build.json")["state"],"failed")

    def test_automatic_recovery_is_limited_to_arm64_nonreloc_shared_objects(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            binary=root/"llvm-bolt"
            binary.write_text("#!/bin/sh\nprintf '%s\\n' pgo128-recover-code-relocs\n");binary.chmod(0o755)
            (root/"perf2bolt").symlink_to(binary)
            args=arguments(root,root);args.bolt=str(binary)
            row={"machine":183,"type":3,"interpreter":None,"entry":0,"relocations":False}
            self.assertEqual(image_policy(args,row,{})["mode"],"recover")
            for changed in ({"relocations":True},{"machine":62},{"type":2,"entry":4096}):
                self.assertEqual(image_policy(args,{**row,**changed},{})["mode"],"standard")

    def test_explicit_image_matches_symlink_and_inode_but_not_identical_copy(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); capture, libs=self.fixture(root)
            profile=read_json(capture/'profile.json'); state=read_json(capture/'run.json')
            alias=root/'symlink.so';alias.symlink_to(libs[0])
            hardlink=root/'hardlink.so';hardlink.hardlink_to(libs[0])
            copy=root/'copy.so';shutil.copy2(libs[0],copy)
            for selector in (str(alias),str(hardlink),libs[0].name):
                args=arguments(root,capture);args.image=[selector]
                selected,skipped=select_images(args,state,profile)
                self.assertEqual([r['path'] for r in selected],[str(libs[0])]);self.assertFalse(skipped)
            args.image=[str(copy)]
            with self.assertRaisesRegex(ValueError,'未匹配'):select_images(args,state,profile)
            args.image=[str(alias),'missing.so']
            with self.assertRaisesRegex(ValueError,'missing.so'):select_images(args,state,profile)


if __name__ == "__main__":
    unittest.main()
