"""Four-step eager decode adapter; runnable inside the existing Ascend host-stub image.

Run via its run_real_test_official.sh to initialize host-side stubs. On a real
device, the same protocol includes the real per-step synchronize operation.
"""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tgo.roi import emit_result, region


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="/models/Qwen3.5-0.8B")
    p.add_argument("--requests", type=int, default=5)
    p.add_argument("--warmup-requests", type=int, default=1)
    p.add_argument("--decode-steps", type=int, default=4)
    p.add_argument("--roi-cpu", type=int)
    a = p.parse_args()
    import torch
    import torch_npu
    from vllm import LLM, SamplingParams
    llm = LLM(model=a.model, seed=1024, tensor_parallel_size=1,
              dtype="bfloat16", trust_remote_code=True, max_num_seqs=32,
              max_model_len=8192, max_num_batched_tokens=8192,
              gpu_memory_utilization=0.8, enable_prefix_caching=False,
              disable_log_stats=True, enforce_eager=True,
              compilation_config={"mode": 0, "cudagraph_mode": "NONE"})
    engine = llm.llm_engine
    if a.roi_cpu is not None:
        os.sched_setaffinity(0, {a.roi_cpu})
    cfg = engine.vllm_config.compilation_config
    assert getattr(cfg.mode, "value", cfg.mode) == 0
    assert getattr(cfg.cudagraph_mode, "name", str(cfg.cudagraph_mode)) == "NONE"
    results = []
    for i in range(a.warmup_requests + a.requests):
        name = f"tgo-{i}"
        params = SamplingParams(temperature=0, max_tokens=a.decode_steps + 10, ignore_eos=True)
        engine.add_request(name, {"prompt_token_ids": [9707]}, params)
        engine.step()  # Prefill, deliberately outside decode ROI.
        torch.npu.synchronize()
        def decode():
            outputs = None
            for _ in range(a.decode_steps):
                outputs = engine.step()
                torch.npu.synchronize()
            return outputs
        if i < a.warmup_requests:
            outputs = decode()
        else:
            with region("decode"):
                outputs = decode()
            tokens = [list(o.outputs[0].token_ids) for o in outputs]
            if not tokens:
                raise RuntimeError("decode returned no tokens")
            results.append(tokens)
        if engine.has_unfinished_requests():
            engine.abort_request(name)
    emit_result({"decode_steps": a.decode_steps, "outputs": results})
    print("TGO_DECODE_COMPLETE", a.requests, a.decode_steps)


if __name__ == "__main__":
    main()
