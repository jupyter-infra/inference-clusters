# Load / benchmark harness

Opt-in performance benchmarks — **not** part of the pass/fail e2e gate. They provision
real (often GPU) nodes and measure timings, so they run only when explicitly invoked and
are excluded from the CI e2e suite by the `benchmark` marker.

## GPU parallel image pull

`test_gpu_parallel_pull_bench.py` measures the effect of the `gpu_parallel_image_pull`
feature (containerd 2.2 parallel download + unpack — see
[thenewstack.io/accelerating-eks-image-pulls](https://thenewstack.io/accelerating-eks-image-pulls/)).

It compares ON vs OFF on the **same** `gpu` node — only the pull path changes, so instance type
/ AZ / EBS / NIC are held constant. On that node it:

1. asserts the booted config routes pod pulls through the transfer service
   (`discard_unpacked_layers = false`, which the feature sets — EKS defaults it `true`, forcing
   local pull mode) with `max_concurrent_downloads = 20` under the transfer plugin;
2. onboards a large multi-layer image into ECR, evicts it from the node, and times a **real
   kubelet pod pull** (the pod's `Pulled` event — kubelet's actual CRI path, not `ctr`) — **ON**;
3. flips the node to EKS-default local pull mode in place (a conf.d drop-in setting
   `discard_unpacked_layers = true`, then `systemctl restart containerd`), evicts, re-times — **OFF**;
   then removes the drop-in and restarts to restore the booted config.

Node-level primitives (debug-exec, config read, image eviction, config flip) live in
`_bench_helpers.py`; the pod-pull timing and orchestration live in the test.

Run it:

```bash
# against an already-deployed project (onboards a large image into ECR via the onboarder)
just bench-gpu-parallel-pull sandbox-e2e
```

The benchmark **hard-asserts the config is effective** on the node, and reports the ON/OFF pod-pull
times as **informational** output (no perf threshold — pull timing is too environment-dependent
for a CI gate).
