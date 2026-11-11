# Design

What Modera is, why it is built this way, and what each choice costs. Where something is
unproven, this page says so.

## Summary

Two products can look at the same photo and decide differently. A swimming club's gallery
may accept a swimwear photo that another community holds for review. Modera lets them
share one endpoint and the same models while each keeps its own rules. It splits three
things that are usually tangled together:

- **Detection** runs pinned local models and reports category scores.
- **Policy** is a named, versioned set of rules that turns scores into `allow`, `review`
  or `block`. The operator writes it and the caller picks it by name.
- **Action** is whatever the product does next. Modera never publishes, holds or stores
  anything.

The service is about 3,300 lines of Python, with a Go client alongside.

## Goals and non-goals

- Accept one text, one photo upload, or one photo URL per request, evaluate it against a
  named policy, and return a decision with its evidence.
- Never return a decision for an evaluation that did not finish.
- Bound overload and make it visible.
- Pin models and run them locally, so content stays in the deployment.
- Let callers tell a retryable failure from a permanent one.

Not goals: stored results or images, a review queue, batch or async jobs, audio or video,
and any claim about moderation accuracy. A shared API key is the only credential, so this
is a single-tenant service.

## Model

The service evaluates one input against one policy. The caller's application consumes the
result and owns what follows.

| Term | Meaning |
| --- | --- |
| Category | A named signal such as `toxicity` or `detect.exposed` |
| Finding | One category with a score from 0 to 1 and its source (model or rules) |
| Coverage | Which categories were evaluated, unsupported or not applicable |
| Policy | Named, versioned rules over categories: thresholds, required checks, composites |
| Decision | `allow`, `review` or `block`, with the categories that triggered it |
| Provenance | The policy, model revisions and rules version actually used |

A result validates itself: findings match evaluated coverage, an `allow` has no reasons,
a fallback names its rules provenance, and several models require every finding to name
its producer. A policy also validates on load: review thresholds sit below block
thresholds, categories are unique per input type, and an unreadable or oversized file
stops the service starting. Callers pick a policy by name and never supply a threshold.

Models sit behind one adapter interface (Hugging Face classifier, Hugging Face object
detector, ONNX YOLO detector). An explicit `label_map` translates each model's labels
into policy categories, so a different model changes configuration, not policy or code.

## Request flow

Cheap checks run first, and expensive work starts only after they pass:

1. Authenticate, then admit the request against a per-process limit (default 8).
2. Read the body under a deadline and validate it.
3. Resolve the policy.
4. For a URL: check the host allowlist and that a backend exists, then fetch.
5. Decode and validate the image, then prepare it.
6. Confirm a backend exists, then run only the models the policy needs, under one shared
   deadline.
7. Apply the policy and return the result.

State is in memory and per process. To scale, run more instances and scrape each one.

## Decisions

### 1. The policy lives in the service

*Alternatives:* hard-code one policy, or return scores and let every caller write its own
thresholds.

Hard-coding makes every new use case a service change. Scores-only pushes thresholds and
missing-check handling into every integration. Named, versioned policies keep the rules in
one place. *Cost:* a policy change is a restart and a version bump, and there is no policy
history.

### 2. An unfinished evaluation is an error, never a partial verdict

If a detector has crossed a block threshold and a second required check times out, Modera
returns a retryable `503` and no decision. A successful result always means the selected
evaluation completed. *Cost:* the caller loses a verdict it might have acted on. For the
same reason a missing result is an error even for optional rules, and every condition in
an `any_of` still needs evidence.

### 3. Scores stay separate

With a policy that blocks at 0.8 and reviews at 0.5 on each category, a detector at 0.9
with a classifier at 0.1 blocks, 0.1 with 0.6 reviews, and 0.1 with 0.1 allows. Modera
never averages scores, because they describe different things and an average would hide
the signal that drove the decision. Composite rules (`all_of`, `any_of`) test each score
against its own minimum. They apply to the whole image: two detections may be of
different people, and Modera does not claim otherwise.

### 4. Failures are classified for the caller

Every error carries a `retryable` flag, and the Go client retries only when the flag is
true and the status is 408, 429 or 503.

| Kind | Status | Retryable |
| --- | --- | --- |
| Busy, queue wait expired, deadline exceeded, source briefly unavailable, stalled body | `503` (`408` for the body) | yes, with `Retry-After` |
| Bad input, unknown policy, unsupported image, source rejected | `4xx` | no |
| Invalid model output, internal failure, model never loaded | `500` | no |
| No backend configured | `501` | no |

A model that failed to load at startup used to return the same retryable `503` as a
timeout, so callers retried until their deadline when only a restart could help. It is now
`500 model_not_loaded`.

### 5. Bounded queues, and a slot that outlives its request

Each photo stage and each model has one active slot and a small FIFO queue (default five
jobs, waiting at most two seconds). Text has no queue and rejects when busy. If a request
times out while native inference is still running, the work keeps its slot until it exits.
Releasing it early would let retries stack new work on work still using the CPU. *Cost:* a
timed-out request can hold capacity for a while. Queues absorb short bursts only. They are
not durable and do not coordinate instances.

### 6. Text fallback is opt-in and only for outages

An operator can allow a rules-only text evaluation when the text model is unavailable. The
result says `fallback: true` and names the policy applied. A busy worker never triggers
it, because that would quietly weaken verdicts under load.

### 7. Models are a supply chain

Models are pinned to a commit, loaded from a local cache as safetensors, with remote code
disabled. The ONNX adapter also checks the artifact's digest, rejects external tensor
files and validates input and output shapes. Nothing downloads at startup, so a running
service uses exactly the bytes that were prepared.

### 8. Fetching a URL is a security boundary

URL input is off until an operator allowlists exact hosts. Requests must be HTTPS on port
443. Each attempt resolves and validates the destination again, rejects private addresses,
follows no redirects, ignores proxy settings and refuses compressed responses. At most
four downloads run at once, and the host and backend checks run before any fetch.

### 9. No state, so a retry is a new evaluation

Moderation has no side effect, so Modera stores nothing and needs no idempotency keys. A
retry gets a new request ID. *Cost:* the caller must make its own follow-up actions
(publishing, opening a review case) safe to repeat.
[`examples/upload_gate.py`](../examples/upload_gate.py) shows one way.

## What the measurements show

[Accuracy](accuracy.md) has the method, numbers and limits. In short: six pinned text
models were scored on two public human-labelled datasets, with cutoffs chosen on a
validation half and every figure from the held-out half. The target, fixed beforehand, was
at least 90% of toxic text caught with at most 5% of clean text flagged.

- No model met it. Near a 5% false-flag budget, the best caught 32% of toxic French text
  and 48% of toxic English text.
- Scores are not probabilities, so read them as a ranking and choose cutoffs from data.
- The datasets are about half toxic. At 1% toxic traffic, more than nine in ten flags from
  the best models would be false alarms.
- Cutoffs do not transfer between models or datasets. Choose them on your own labelled
  sample. A command for that is kept at the `evaluation-archive` git tag, along with the
  harness that produced these numbers.

Photo accuracy has not been measured.

## Testing

Over 500 tests call the API in process. They cover policies, limits, error classification,
queues, overload, shutdown, model loading with small generated models, and the hardened
container. The upload gate is tested against a real Modera process with a stalled model.
The Go client has unit tests with the race detector.

Testing found real defects: a worker race that freed a slot after the caller resumed (seen
on Python 3.14, never established on 3.12), text fallback firing on a merely busy worker,
a URL downloaded before cheap checks could reject it, and the model-not-loaded
misclassification above.

## What this does not establish

- **Accuracy for your content.** Text accuracy is measured on two public datasets and the
  target is not met. Photo accuracy is unmeasured.
- **Behaviour on your platform.** Container checks cover one container on one runner. Your
  load balancer, TLS and termination are not tested.
- **Capacity.** Nothing here measures real-model throughput or latency under load.
- **A real integration.** No product uses Modera. The Go client has no live check against
  the API.

## Trade-offs

| Choice | Gain | Cost |
| --- | --- | --- |
| Policy in the service | One versioned place for rules | Restart to change, no history |
| Error over partial verdict | A result always means complete | Discards a possibly useful verdict |
| Separate scores | Evidence is not averaged away | Rules must name each category |
| Slot kept after timeout | Retries cannot pile up work | Capacity held longer |
| Fallback only on outage | Verdicts do not weaken under load | Busy text is a hard `503` |
| Stateless | Easy to scale and reason about | Caller owns idempotency |
| Pinned local models | Content stays put, bytes are known | Operator owns compute and updates |
| Shared API key | Simple | Single tenant, no per-caller limits |
