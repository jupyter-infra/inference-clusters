# Load / benchmark harness

Opt-in performance benchmarks — **not** part of the pass/fail e2e gate. They provision
real (often GPU) nodes and measure timings, so they run only when explicitly invoked and
are excluded from the CI e2e suite by the `benchmark` marker.

## GPU parallel image pull

`test_gpu_parallel_pull_bench.py` measures the effect of the `gpu_parallel_image_pull`
feature (containerd 2.2 parallel download + unpack — see
[thenewstack.io/accelerating-eks-image-pulls](https://thenewstack.io/accelerating-eks-image-pulls/)).

It provisions **one** real `gpu` node (the actual cluster NodeClass — no synthetic pair, so
instance size is whatever the cluster picks) and compares on-vs-off on that **same instance**
by rolling the config in place. Holding the instance constant keeps instance type / AZ / EBS /
NIC out of the measurement — the only variable is the config. On that node it:

1. runs `containerd config dump` to confirm the three keys
   (`max_concurrent_downloads`, `concurrent_layer_fetch_buffer`, `max_concurrent_unpacks`)
   land under the **active** `io.containerd.transfer.v1.local` plugin table, and reports
   whether CRI image pulls honor the transfer service (the correctness check the PR review asked for);
2. **warms the registry cache once** (a throwaway pull) so neither measured pull pays the
   upstream/ECR cache-miss — the on/off delta then reflects node-side concurrency, not a
   first-vs-second caching artifact;
3. does a cold `crictl pull` of a large multi-layer image (timed, repeated) with parallel-pull
   **ON** (the booted default);
4. flips the node's transfer-plugin config to concurrency=1 in place, restarts containerd, and
   re-measures the **same** image **OFF**; then restores the node's config and reports the delta.

Generic node/pull primitives live in the shared `tests/_cluster_helpers.py` (reusable by e2e
and load tests alike); the parallel-pull specifics (the transfer-plugin keys and config block)
live in the test itself.

Run it:

```bash
# against an already-deployed project (onboards vllm-qwen to get a large image in ECR)
just bench-gpu-parallel-pull sandbox-e2e
```

The benchmark **hard-asserts the config lands correctly** (the keys are active on the node) but
treats the timing delta as **informational** — it prints a table and only sanity-checks that
"on" is not slower than "off". It is a measurement tool, not a threshold gate (pull timing is
too environment-dependent for CI).
