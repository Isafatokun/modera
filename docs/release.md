# First release review

The service branch consolidates the synchronous API; runtime experiments stay
on separate research branches. No model is selected automatically.

Implemented and tested:

- Shared text, photo-file and photo-URL contract; authenticated requests and limits.
- Pinned local classifiers, explicit label mappings, configurable policies and text fallback.
- Bounded photo waiting, stage deadlines, cancellation, shutdown and recovery.
- One decoded image shared with preparation; explicit image ownership and cleanup.
- Real-model integration checks, rules example over HTTP, health and protected metrics.

Before an external release:

- Validate the container and target deployment, including loaded-model shutdown.
- Publish reference model/policy profiles with verified licences, category meanings,
  language coverage and independent quality evidence; preserve known limitations.
- Run hosted CI and review the release branch before publishing a version.

Durable async jobs, additional modalities, SDKs and alternative runtimes remain
future work. Current queues are process-local; see [operations](operations.md).
