"""Deterministic Python frontend workload with warmup excluded by an explicit ROI."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tgo.roi import region


class Node:
    def __init__(self, x):
        self.x = x
    def evaluate(self, y):
        return (self.x * 17 + y) & 65535


def work(n):
    nodes = [Node(i) for i in range(128)]
    state = {str(i): i * i for i in range(128)}
    total = 0
    for i in range(n):
        node = nodes[i & 127]
        key = str((total + i) & 127)
        total = (total + node.evaluate(state.get(key, 0))) & 0xffffffff
    return total


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--iterations", type=int, default=2000000)
    a = p.parse_args()
    work(10000)
    with region("decode"):
        checksum = work(a.iterations)
    print(json.dumps({"checksum": checksum, "iterations": a.iterations}, sort_keys=True))
