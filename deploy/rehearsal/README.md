# Gateway and lifecycle rehearsal

This acceptance check runs the actual Go SDK through NGINX to two independent
Modera processes using the pinned reference text and photo models. It uses normal
NGINX master/worker mode on Linux. No production service, deployment account or
external media source is required.

From the repository root, with Go 1.27.3 and NGINX installed:

```sh
uv sync --locked --extra inference
uv run --locked --extra inference python deploy/rehearsal/rehearse.py \
  --nginx /usr/sbin/nginx --download \
  --cache-dir model-cache/hub --output rehearsal-verification
```

`--download` prepares and warms the pinned models; omit it to use an already
prepared cache offline. The output directory must not exist. Model weights are
separate from the evidence directory. Allow memory for two model copies plus
Python/PyTorch, and outbound access for the initial preparation.

The `Deployment rehearsal` GitHub workflow runs this check and retains the report,
request traces, model selections, generated gateway configuration and process logs
as an artifact. It records the source commit and tool versions. Credentials are
fresh for each run, passed through the environment and never written to evidence.
Only generated neutral text and a white PNG are submitted.

## What must pass

1. Text and photo requests through the SDK carry the expected model/policy
   provenance, and the gateway distributes work across both instances.
2. Holding one real inference call open saturates a deliberately one-request
   instance. Excess work returns typed `503 service_busy`, no decision and
   `Retry-After: 1`. The SDK observes that failure, waits at least the advertised
   delay and succeeds on its second attempt with a new request ID.
3. With an active photo request, removing its instance from the NGINX upstream and
   acknowledging the new configuration and observing old workers close the listening
   socket sends five fresh SDK requests to the other
   instance. SIGTERM then drains the accepted photo request successfully.
4. Restarting and rejoining the stopped instance preserves the photo findings and
   model provenance while producing a new request ID.
5. An explicit uncooperative-exit fault combines an unreleased inference call
   with ignoring the final SIGTERM re-raised after Uvicorn cleanup. After a
   four-second grace period the harness sends SIGKILL, checks the process exit
   and verifies that the interrupted SDK call has an error and no decision.
   The remaining instance still handles a fresh request.
6. The gateway and remaining instance shut down cleanly. Failure paths reap child
   process groups as well as SDK probes.

The gate is a bounded, local, rehearsal-only wrapper around the real classifier's
prediction method. Once released, it invokes the original classifier. It creates
no HTTP control endpoints, changes no serving code and is excluded from the
runtime image. It is used because naturally slow requests are timing-dependent.
The restarted test instance additionally uses `--stubborn-exit` to exercise the
controller's forced-kill path. Normal Uvicorn restores its original signal handler
and re-raises SIGTERM after shutdown, which can terminate a blocked native thread
without needing SIGKILL. The fault mode explicitly suppresses that final exit; it
is not a claim about the normal server. A process regression covers both modes.

## Scope and limits

The gateway binds only to loopback, disables its own upstream retries, and uses
fresh SDK connections for routing assertions. A generation header acknowledges
that a reload took effect. This alone is insufficient: old workers can briefly
continue accepting connections. The Linux `/proc` listener-ownership barrier
waits for every old worker to close the listening socket before shutdown begins.
Already accepted requests continue draining on those workers. Failure to inspect
that ownership fails the check rather than substituting a fixed sleep. The upstream-address and
generation headers are diagnostics for this isolated check, not recommended
public response headers. Metrics/readiness remain reachable to the harness.
NGINX authentication headers and structured Modera errors are passed through.

On a killed upstream, NGINX may return an unstructured 502. The SDK treats that as
a protocol failure rather than inventing an allow or silently retrying it. Graceful
removal is tested separately from this deliberately interrupted request.

This proves Linux process/gateway behavior on the runner. It does not certify a
particular hosting platform's ingress removal, TLS, container memory limits or
termination controller. The harness itself enforces the final deadline; repeat
the scenario using the chosen platform's actual controller before release.
Existing CI separately tests the read-only inference container and Compose restart.
The two-second Uvicorn drain and four-second final grace are deliberately short
for this failure rehearsal; production retains its documented 30/45-second values.

This is not a benchmark, an accuracy evaluation, or approval of the experimental
reference model thresholds. Connection/body bounds here are small rehearsal
settings, not a complete public-ingress rate-limiting configuration. The gateway
port is selected ephemerally; a concurrent bind conflict fails the rehearsal.
