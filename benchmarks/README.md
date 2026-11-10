# Capacity benchmark

Manual HTTP measurements, separate from the tests. It starts a loopback server with a
throwaway key on Linux or macOS using the example Horizon text and ViT photo
classifiers, FP32 on CPU, one process and two threads per model. It cannot target an
existing deployment.

```sh
uv sync --locked --extra inference --group benchmark
uv run --locked --extra inference --group benchmark python benchmarks/prepare_models.py /tmp/modera-model-cache
uv run --locked --extra inference --group benchmark python benchmarks/run.py --cache /tmp/modera-model-cache --seconds 20
```

Preparation downloads about 0.9 GiB of pinned weights, and the benchmark then loads them
offline. Model IDs, revisions and the workload are fixed in the scripts, and no
`MODERA_*` settings or credentials are inherited.

The workloads are short and long text, generated 3 MP and 12 MP JPEG uploads, mixed
traffic, paced load and unpaced bursts of eight clients. Requests are never retried.
The generator is closed loop, so paced workers fall below their target rate when
requests are slow, and in-flight requests can stretch a scenario's wall time.

Each run writes to a new ignored directory under `benchmarks/results/`, or to
`--output`, which must not exist. It records the commit and dirty state, dependency
versions, model provenance, fixtures, raw latency samples, status and error counts,
health probes, metrics, peak server memory and a final recovery request. An interrupted
run keeps its finished scenarios and is marked `interrupted`.

Successful throughput and latency are reported apart from rejected or failed requests.
Peak RSS is a high-water mark, not steady-state or container memory. The images are
synthetic, so this measures processing cost, not moderation accuracy, and short runs
say little about tail latency. Compare runs only on the same commit, models, workload
and hardware, and save the result JSON with any published claim.

For deployment sizing, add a separate load generator, representative media, longer
arrival-rate and soak tests, your ingress limits and your callers' retry behaviour. CI
lints and type-checks these scripts and runs their `--help`, but downloads no weights and
enforces no timing targets.
