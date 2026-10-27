# First release review

The service branch consolidates the synchronous API; runtime experiments stay
on separate research branches. No model is selected automatically.

Implemented and tested:

- Shared text, photo-file and photo-URL contract; authenticated requests and limits.
- Pinned local classifiers, explicit label mappings, configurable policies and text fallback.
- A shared model configuration file and preparation command with offline verification.
- Bounded photo waiting, stage deadlines, cancellation, shutdown and recovery.
- One decoded image shared with preparation; explicit image ownership and cleanup.
- Real-model HTTP startup, active-request draining, shutdown, restart and missing-weight
  failure; rules example, health and protected metrics.
- Container checks for authentication, codecs, rules and read-only inference restart.

Before an external release:

- Validate the target deployment, including ingress removal and
  the platform's final termination deadline.
- Publish reference model/policy profiles with verified licences, category meanings,
  language coverage and independent quality evidence; preserve known limitations.
- Run hosted CI and review the release branch before publishing a version.

Durable async jobs, additional modalities, SDKs and alternative runtimes remain
future work. Current queues are process-local; see [operations](operations.md).
