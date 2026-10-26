# Capacity benchmark

Manual HTTP measurements, separate from the correctness tests in `tests/`.
Runs on Linux/macOS with the pinned example Horizon text and ViT photo classifiers,
FP32 CPU inference, one server process, and two inference threads. It starts a
loopback server with an ephemeral key; it does not target an existing deployment.

From the repository root:

```sh
uv sync --locked --extra inference --group benchmark
uv run --locked --extra inference --group benchmark python benchmarks/prepare_models.py /tmp/modera-model-cache
uv run --locked --extra inference --group benchmark python benchmarks/run.py --cache /tmp/modera-model-cache --seconds 20
```

Preparation downloads about 0.9 GiB of pinned weights. The benchmark service loads
them locally with network model loading disabled. Model IDs, revisions, mappings,
and the workload matrix are explicit in the scripts; no deployment settings or
credentials are inherited from `MODERA_*` variables.

The workload covers short/long text, generated 3 MP/12 MP JPEG uploads, mixed
traffic, paced load, and unpaced eight-client bursts. Requests are never retried.
Each scenario starts requests for the configured duration; outstanding requests
and pacing sleeps can extend its wall time. The generator is closed-loop: paced
workers can fall below their target rate when requests are slow.

Each run creates a unique ignored directory under `benchmarks/results/`. Use
`--output /path/to/new-directory` to choose another location; existing directories
are rejected. Outputs include the commit/dirty state, dependency versions, model
provenance from successful warm-ups, fixtures, raw latency samples, status/error
counts, health probes, metrics, peak server memory, and a final recovery request.
Interrupted runs retain completed scenarios and are marked `interrupted`.

Successful-request throughput and latency are separate from rejected/failed
requests. Peak RSS is a process high-water mark, not steady memory or container
usage. Synthetic images test processing costs, not moderation accuracy. Short
runs and small samples do not establish tail latency or production capacity.

Use the same commit, models, workload, and hardware for comparisons; save the
result JSON alongside any published claims. For deployment sizing, also use a
separate generator, representative media, longer arrival-rate/soak tests, ingress
limits, and caller retry behavior. Normal CI lints/type-checks these scripts and
checks their entry points; it does not download weights or enforce timing targets.
