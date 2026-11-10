# Gateway and lifecycle rehearsal

An acceptance check that runs the real Go client through NGINX to two independent Modera
processes with the pinned reference text and photo models. It uses NGINX's normal
master and worker mode on Linux. It needs no production service, account or external
media source.

From the repository root, with Go 1.27.3 and NGINX installed:

```sh
uv sync --locked --extra inference
uv run --locked --extra inference python deploy/rehearsal/rehearse.py \
  --nginx /usr/sbin/nginx --download \
  --cache-dir model-cache/hub --output rehearsal-verification
```

`--download` prepares and warms the pinned models; leave it out to use an already
prepared cache offline. The output directory must not exist. Model weights live apart
from the evidence directory, and you need memory for two model copies plus Python and
PyTorch.

The `Deployment rehearsal` workflow runs this and keeps the report, request traces,
model selections, generated gateway configuration and process logs for 30 days, with
the source commit and tool versions. Credentials are fresh for each run, come from the
environment and are never written to the evidence. Only generated neutral text and a
white PNG are submitted.

## What must pass

1. Text and photo requests through the client carry the expected model and policy
   provenance, and the gateway spreads work across both instances.
2. Holding one real inference call open saturates a deliberately one-request instance.
   Extra work returns a typed `503 service_busy` with no decision and `Retry-After: 1`.
   The client sees the failure, waits at least that long and succeeds on its second
   attempt with a new request ID.
3. With a photo request in flight, one instance is removed from the NGINX upstream. The
   harness waits for the new configuration to be acknowledged and for the old workers
   to close the listening socket, then sends five fresh requests to the other instance.
   `SIGTERM` then drains the accepted photo request successfully.
4. Restarting and rejoining the stopped instance keeps the photo findings and model
   provenance and gives a new request ID.
5. An explicit uncooperative-exit fault combines an unreleased inference call with
   ignoring the final `SIGTERM` that Uvicorn re-raises after cleanup. After a
   four-second grace period the harness sends `SIGKILL`, checks the exit, and verifies
   the interrupted client call has an error and no decision. The other instance still
   serves a fresh request.
6. The gateway and the remaining instance shut down cleanly, and failure paths reap
   child process groups as well as client probes.

Slow requests are timing-dependent, so the harness gates the real classifier's predict
method with a bounded, local, rehearsal-only wrapper. Once released it calls the original
classifier. The wrapper adds no HTTP control endpoints, changes no serving code and is
not in the runtime image. The restarted instance also uses `--stubborn-exit` to exercise
the forced-kill path. Normal Uvicorn restores its handler and re-raises `SIGTERM` after
shutdown, which can end a blocked native thread without `SIGKILL`; the fault mode
suppresses that final exit and is not how the normal server behaves. A process test
covers both modes.

## Scope and limits

The gateway binds to loopback, disables its own upstream retries, and uses fresh client
connections for routing assertions. A generation header acknowledges that a reload took
effect, but that alone is not enough, since old workers can briefly keep accepting
connections. The Linux `/proc` check waits until every old worker has closed the
listening socket, and if the ownership check cannot run, the rehearsal fails instead of
substituting a fixed sleep. Those headers are diagnostics for this check, not something
to expose publicly. NGINX passes authentication headers and structured Modera errors
through.

If an upstream is killed mid-request, NGINX may return an unstructured 502. The client
treats that as a protocol failure, with no invented allow and no silent retry.
Graceful removal is tested separately.

This proves process and gateway behaviour on a Linux runner. It does not certify a
hosting platform's ingress removal, TLS, container memory limits or termination
controller, so repeat the scenario with your platform's real controller before a
release. The harness enforces the final deadline itself. Existing CI separately covers the
read-only inference container and Compose restart. The two-second Uvicorn drain and
four-second final grace are deliberately short for this check, while production keeps
the documented 30 and 45 seconds.

It is not a benchmark or an accuracy evaluation, and it does not approve the reference
thresholds. Connection and body bounds are small rehearsal settings, not a full
rate-limiting configuration. The gateway port is chosen at random, and a bind conflict
fails the run.
