"""Run a Python entry after installing an explicit function ROI hook."""
import argparse
from pathlib import Path
import runpy
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tgo.roi import install_hook


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hook", required=True)
    p.add_argument("command", nargs=argparse.REMAINDER)
    a = p.parse_args()
    command = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not command:
        p.error("Python script, -m module or -c code required")
    install_hook(a.hook)
    if command[0] == "-m":
        sys.argv = [command[1], *command[2:]]
        runpy.run_module(command[1], run_name="__main__", alter_sys=True)
    elif command[0] == "-c":
        sys.argv = ["-c", *command[2:]]
        exec(compile(command[1], "<string>", "exec"), {"__name__": "__main__"})
    else:
        sys.argv = command
        sys.path.insert(0, str(Path(command[0]).resolve().parent))
        runpy.run_path(command[0], run_name="__main__")


if __name__ == "__main__":
    main()
