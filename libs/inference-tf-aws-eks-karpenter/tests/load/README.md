# Load / benchmark harness

Opt-in performance benchmarks — **not** part of the pass/fail e2e gate. They provision
real (often GPU) nodes and measure timings, so they run only when explicitly invoked and
are excluded from the CI e2e suite by the `benchmark` marker.

## GPU parallel image pull

`test_gpu_parallel_pull_bench.py` measures the effect of the `gpu_parallel_image_pull`
feature (the SOCI snapshotter's parallel pull/unpack mode, enabled on GPU nodes via nodeadm's
FastImagePull gate — see
[SOCI parallel pull mode for EKS](https://aws.amazon.com/blogs/containers/introducing-seekable-oci-parallel-pull-mode-for-amazon-eks/)).

The snapshotter is a bootstrap-time node setting, so timing is an **absolute** measurement on the
booted `gpu` node (no same-node on/off flip). On that node it:

1. asserts the node's effective `containerd config dump` sets `snapshotter = "soci"` under the CRI
   images plugin (proof the FastImagePull gate took effect);
2. onboards a large multi-layer image into ECR, evicts it from the node, and times a **real
   kubelet pod pull** (the pod's `Pulled` event — kubelet's actual CRI path, not `ctr`).

Node-host access is the shared `_serving_helpers.node_shell`; the SOCI detection, image eviction,
and pod-pull timing live in the test itself.

Run it:

```bash
# against an already-deployed project (onboards a large image into ECR via the onboarder)
just bench-gpu-parallel-pull sandbox-e2e
```

The benchmark **hard-asserts the snapshotter is SOCI** on the node, and reports the pod-pull time
as **informational** output (no perf threshold — pull timing is too environment-dependent for a
CI gate).
