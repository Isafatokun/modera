# Design memo: Modera

This memo explains what Modera is, why it is built the way it is, and what it costs.
It describes the repository as it stands, and where something is unproven it says so.

## Summary

Two products can look at the same photo and decide differently. A swimming club's event
gallery may accept a swimwear photo that another community holds for review. Modera
lets them share one endpoint and the same models while each keeps its own rules.

It does this by splitting three things that are usually tangled together:

- **Detection** runs pinned local models and reports category scores.
- **Policy** is a named, versioned set of rules that turns scores into `allow`,
  `review` or `block`. The operator writes it and the caller picks it by name.
- **Action** is whatever the product does next. Modera never publishes, holds or
  stores anything.

The service is small, about 3,300 lines of Python, with a Go client alongside. The
parts worth reading closely are the failure semantics, not the happy path.

## Requirements

**Functional**

- Accept one text, one photo upload, or one photo URL per request.
- Evaluate it against a named policy and return a decision with its evidence.
- Support several photo models on one image, with rules that combine their scores.
- Let an operator opt in to a narrower rules-only text evaluation if the text model is down.

**Non-functional**

- A result must be trustworthy: a decision is never returned for an evaluation that did
  not finish.
- Overload must be bounded and visible, not silent.
- Models are pinned and run locally, so content stays in the deployment.
- Callers must be able to tell a retryable failure from a permanent one without guessing.

**Non-goals.** No stored results or images, no review queue, no batch or async jobs, no
audio or video, and no claim about moderation accuracy. A shared API key is the only
credential, so this is a single-tenant service.

## Domain model

The bounded context is *evaluating one input against a policy*. The caller's
application is a separate context that consumes the result and owns its consequences.
Modera is the supplier of evidence and the application is the customer, and the
contract between them is the response.

The language is used consistently in the code and the docs:

| Term | Meaning | Where |
| --- | --- | --- |
| Category | A named signal such as `toxicity` or `detect.exposed` | `results.py` |
| Finding | One category with a score from 0 to 1 and its source (model or rules) | `results.py` |
| Coverage | Which categories were evaluated, unsupported or not applicable | `results.py` |
| Policy | Named, versioned rules over categories: thresholds, required checks, composites | `policies.py` |
| Decision | `allow`, `review` or `block`, with the categories that triggered it | `results.py` |
| Provenance | The policy, model revisions and rules version actually used | `results.py` |

Findings, coverage and decisions are immutable value objects. `ModerationResult`
enforces its own invariants: findings match evaluated coverage exactly, an `allow` has
no reasons, a fallback names its rules provenance, and several models require every
finding to name its producer. An inconsistent result cannot be built.

A `Policy` is an immutable aggregate that validates itself on load. Review thresholds
must sit below block thresholds, categories are unique per input type, and an
unreadable or oversized file stops the service starting. The caller selects a policy by
name and never supplies a threshold.

Models are kept behind a small port, `ModelAdapter`, with three adapters: a Hugging Face
classifier, a Hugging Face object detector and an ONNX YOLO detector. Each model's native
labels are translated to policy categories by an explicit `label_map`. That mapping is
the anti-corruption layer: a different model with different labels changes
configuration, not policy or code.

## API

One endpoint, `POST /v1/moderations`, with a bearer key. Text and photo URLs are JSON,
and photos can also be uploaded as multipart. Limits are fixed and documented: text up
to 4,096 characters, photos up to 8 MiB and 16 million decoded pixels, JSON bodies up to
32 KiB, and a 15 second body deadline.

A completed evaluation returns `200` with the decision, findings, coverage, models,
policy version and a request ID. Anything else is a typed error with a `retryable` flag.
A `200` means the evaluation completed, not that the content was allowed.

## Request flow

Checks run cheapest first, and none of the expensive work starts before the cheap
checks pass:

1. Authenticate, then admit the request against a per-process limit (default 8).
2. Read the body under a deadline and validate it.
3. Resolve the policy.
4. For a URL: check the host allowlist and that a backend exists before any fetch, then fetch.
5. Decode and validate the image, then prepare it for the model.
6. Confirm a backend exists, then run only the models the policy needs, under one
   shared deadline.
7. Apply the policy and return the result.

State is in memory and per process. Queues, metrics and loaded models belong to the
process. Scaling means running more instances and scraping each one.

## Decisions

Each entry gives the decision, the alternatives, the cost, and the evidence.

### 1. The policy lives in the service, and the decision lives with the operator

*Alternatives:* hard-code one policy; or return scores and let every caller write its
own thresholds.

Hard-coding makes every new use case a service change. Scores-only pushes thresholds,
missing-check handling and versioning into every integration. Named, versioned policies
keep one place for the rules. The cost is operational: a policy change is a restart and
a version bump, and Modera keeps no policy history.

*Evidence:* `policies.py`, `docs/policies.md`.

### 2. An unfinished evaluation is an error, never a partial verdict

If a detector has crossed a block threshold and a second required check times out,
Modera returns a retryable `503` and no decision. A successful result always means the
selected evaluation completed. The cost is real: the caller loses a verdict it might
have acted on. Policy evaluation therefore treats a missing result as an error even for
optional rules, and `any_of` composites still require evidence for every listed
condition.

*Evidence:* `evaluate_policy` and its tests, `tests/integration/test_multiple_models.py`.

### 3. Scores stay separate

In the integration tests' policy (block at 0.8 and review at 0.5 on each category), a
detector at 0.9 with a classifier at 0.1 blocks, 0.1 with 0.6 reviews, and 0.1 with 0.1
allows. Modera never averages the scores, because they
describe different things and an average would hide the signal that drove the decision.
Composite rules test each score against its own minimum with `all_of` or `any_of`.
They apply to the whole image: two detections may refer to different people, and
Modera does not claim otherwise.

### 4. Failure is classified for the caller

The server marks every error `retryable` or not, and the Go client retries only when the
flag is true *and* the status is 408, 429 or 503.

| Kind | Status | Retryable |
| --- | --- | --- |
| Busy, queue wait expired, deadline exceeded, source briefly unavailable, stalled request body | `503` (`408` for the body) | yes, with `Retry-After` where applicable |
| Bad input, unknown policy, unsupported image, source rejected | `4xx` | no |
| Invalid model output, internal failure, **model never loaded** | `500` | no |
| No backend configured | `501` | no |

The last row was a defect. A model that failed to load at startup used to return the
same retryable `503` as a timeout, so callers retried until their deadline when only a
restart could help. It now returns `500 model_not_loaded`.

### 5. Bounded queues, one slot per model, and a slot that outlives its request

Each photo stage and each model has one active slot and a small FIFO queue (default five
jobs, two seconds). Text has no queue and rejects when busy. If a request times out
while native inference is running, the work keeps its slot until it exits. Releasing it
early would let retries stack new work on top of work still using the CPU. The cost is
that a timed-out request can hold capacity for a while, and the benefit is that
overload cannot compound.

Queues absorb short bursts only. They are not durable and do not coordinate instances.

### 6. Text fallback is opt-in and only for outages

An operator can allow a rules-only text evaluation when the text model is unavailable.
The result says `fallback: true` and names the policy actually applied. A busy worker
never triggers fallback, because that would silently weaken verdicts under load.

### 7. Models are a supply chain

Models are pinned to a commit, loaded from a local cache, as safetensors, with remote
code disabled. The ONNX adapter also checks the artifact's digest, rejects external
tensor files, and validates the input and output shapes. Nothing is downloaded at
startup, so a service that starts is running exactly the bytes that were prepared.

### 8. Fetching a URL is a security boundary

URL input is off until an operator allowlists exact hosts. Requests must be HTTPS on port
443. Every attempt resolves and validates the destination again, rejects private
addresses, follows no redirects, ignores proxy settings and refuses compressed
responses. Downloads are capped at four at once. The host and backend checks run before
the fetch so a doomed request costs nothing.

### 9. No state, so a retry is just a new evaluation

Moderation has no side effect, so Modera stores nothing and needs no idempotency keys.
A retry is a new evaluation with a new request ID. The cost moves to the caller, who
must make its own follow-up actions (publishing, opening a review case) idempotent.
That is deliberate: a payment gateway needs idempotency because retries can charge twice,
and this service has no equivalent hazard.

## Verification

[`docs/verification.md`](verification.md) maps each claim to the check that supports it.
In short: over 500 tests run on every change, covering model loading with small
generated models, the hardened container and the Go client. Two heavier checks run against the real
stack. One starts the Compose setup from an empty cache with the real checkpoints. The
other drives the Go client through NGINX to two real-model processes and exercises
overload, retry, drain, restart and forced termination.

Verification also found real defects, which is the best argument for it:

- A race in the worker that freed a slot after the caller resumed, causing spurious
  `503`s. It was first seen on Python 3.14, and whether 3.12 was affected was never
  established.
- Text fallback firing on a merely busy worker.
- A URL being downloaded before cheap checks that would have rejected it.
- The model-not-loaded misclassification above.
- A flaky crash in the rehearsal when a worker's process ID was reused.

Large functions were split behind tests. Where coverage was thin (the doctor command,
the Go result validator, `Retry-After` parsing) tests were written first, and the
verification scripts were compared old against new before they replaced the originals.

## What this does not establish

- **Moderation accuracy.** The reference models and thresholds are examples. No
  independently labelled evidence ships with the repository. The
  [evaluation harness](../evaluation/README.md) exists to produce that evidence on your
  own photos, and it should be run before a policy acts on real content.
- **Behaviour on your platform.** The rehearsal proves process and gateway behaviour on
  a Linux runner, not your load balancer, TLS or termination controller.
- **Production capacity.** The [load test](../loadtests/README.md) is a starting point on
  one machine, and CI does not run it.
- **A first integration.** No application yet calls Modera, keeps a photo pending on
  failure and survives a burst. That was the next milestone and is not done.

## Trade-offs at a glance

| Choice | Gain | Cost |
| --- | --- | --- |
| Policy in the service | One place for rules, versioned | Restart to change, no history |
| Error over partial verdict | A result always means complete | Discards a possibly useful verdict |
| Separate scores | Evidence is not averaged away | Rules must name each category |
| Slot kept after timeout | Retries cannot pile up work | Capacity held longer |
| Fallback only on outage | Verdicts do not weaken under load | Busy text is a hard `503` |
| Stateless | Trivial to scale and reason about | Caller owns idempotency |
| Pinned local models | Content stays put, bytes are known | Operator owns compute and updates |
| Shared API key | Simple | Single tenant, no per-caller limits |

## Next

1. Build the first integration: store `pending` before calling, record the verdict with
   its provenance, and retry stuck items. The fault-injecting backend exists for this and
   has not been merged.
2. Run the evaluation harness on a representative photo set and decide which policy to
   pilot.
3. Decide whether to keep the ONNX research candidate, whose model licence is unresolved.
