"""Stage independently produced DSOs while preserving their original dependency roots."""
import hashlib
from pathlib import Path
import shutil
import subprocess

from .elf import ELF
from .util import IdentityError, sha256, tool, tool_env, run


def executable_sections(path):
    elf = ELF(path)
    return {name: {"address": section[3], "size": section[5],
                   "sha256": hashlib.sha256(elf.read(section[4],section[5])).hexdigest()}
            for name, section in elf.sections.items() if section[2] & 4}


def stage_image(original, source, directory, expected_sha256=None):
    """Import a candidate, not a performance recommendation. Never alter its source."""
    original, source, directory = Path(original).resolve(), Path(source).resolve(), Path(directory)
    metadata, candidate = ELF(original).metadata(), ELF(source).metadata()
    if expected_sha256 and candidate["sha256"] != expected_sha256:
        raise IdentityError(f"candidate identity differs: {source}")
    if any(metadata[key] != candidate[key] for key in ("machine", "type", "interpreter")):
        raise ValueError("candidate ELF architecture/type does not match original")
    directory.mkdir(parents=True, exist_ok=False)
    target = directory / original.name
    shutil.copy2(source, target)
    before = executable_sections(target)
    patchelf = tool("patchelf")
    rpath = subprocess.check_output([patchelf, "--print-rpath", target], env=tool_env(), text=True).strip()
    expanded = rpath.replace("${ORIGIN}", str(original.parent)).replace("$ORIGIN", str(original.parent))
    paths = ':'.join(p for p in (expanded, str(original.parent)) if p)
    preserve = ["--force-rpath"] if candidate.get("rpath_kind") == "rpath" else []
    fix = run([patchelf, *preserve, "--set-rpath", paths, target], directory / "rpath.log", timeout=30)
    after = executable_sections(target)
    if before != after:
        raise IdentityError("dependency-root fix changed executable sections or their addresses")
    if sha256(source) != candidate["sha256"] or sha256(original) != metadata["sha256"]:
        raise IdentityError("DSO changed during candidate import")
    return {"original": str(original), "optimized": str(target), "input": metadata,
            "output": ELF(target).metadata(), "imported_from": str(source),
            "imported_sha256": candidate["sha256"], "dependency_fix": fix,
            "executable_sections": before, "executable_sections_unchanged": True}
