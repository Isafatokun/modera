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

## Go SDK release preparation

Consumer compatibility and installation are separate checks. CI compiles the SDK
and example with Go 1.23.12, runs race/static/live checks with Go 1.27.3, and installs
the exact PR head into a fresh external module without a local `replace` directive.
The [application example](../clients/go/cmd/moderate/README.md) demonstrates decisions
and failures without performing application business actions.

Before tagging the SDK:

- Include the chosen repository licence files; none have been selected here.
- Confirm all exact-revision checks and review the public API and limitations.
- Use a submodule-prefixed tag, for example `clients/go/v0.1.0`, rather than a root
  `v0.1.0` tag. The module lives under `clients/go`; see the
  [Go module mapping rules](https://go.dev/ref/mod#vcs-version).
- From a new consumer, install the tagged module, compile the three methods and
  run the live application example against the release service configuration.
- Publish SDK compatibility notes separately from service deployment evidence and
  classifier accuracy. Do not describe an experimental model profile as approved.

No version tag or release is created by these checks. The SDK's untagged revision
can already be installed by commit; Go generates its pseudo-version. Language
compatibility with Go 1.23 does not imply it still receives upstream security fixes;
use a maintained toolchain for deployment (see the
[Go release policy](https://go.dev/doc/devel/release#policy)).
