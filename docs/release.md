# First release review

The serving package provides the synchronous API. Research tools are under
`evaluation/` and are excluded from the runtime image. No model is selected automatically.

Implemented and tested:

- Shared text, photo-file and photo-URL contract; authenticated requests and limits.
- Pinned local classifiers, explicit label mappings, configurable policies and text fallback.
- A shared model configuration file and preparation command with offline verification.
- Bounded photo waiting, stage deadlines, cancellation, shutdown and recovery.
- One decoded image shared with preparation; explicit image ownership and cleanup.
- Real-model HTTP startup, active-request draining, shutdown, restart and missing-weight
  failure; rules example, health and protected metrics.
- Container checks for authentication, codecs, rules and read-only inference restart.

- Blinded text/photo annotation packets, declared-human review/adjudication, explicit
  score-category mappings, uncertainty exclusions and proposal-bound test-label export.
- A standard-library Go client with typed errors/results, replayable uploads, bounded
  retries, request contexts, redirection protection and live rules-only API checks.

- A reproducible gateway/SDK lifecycle rehearsal with real-model provenance,
  overload/retry, active-request removal/draining, restart and final-deadline checks.
  See [rehearsal scope](../deploy/rehearsal/README.md); hosted CI retains evidence.

Before an external release:

- Validate the target deployment, including ingress removal and
  the platform's final termination deadline.
- Publish reference model/policy profiles with verified licences, category meanings,
  language coverage and independent quality evidence; preserve known limitations.
- Run hosted CI and review the release branch before publishing a version.

Durable async jobs, additional modalities, other SDKs and alternative runtimes remain
future work. Current queues are process-local; see [operations](operations.md).

Reference profiles remain experimental: the published external-source diagnostics
and guard experiments do not establish production-quality defaults. Genuine category-
aligned independent review is still required. Reviewer IDs/origins and source novelty
are declarations, not identity or training-independence verification. No real human
labels were manufactured by the workflow tests or examples.
