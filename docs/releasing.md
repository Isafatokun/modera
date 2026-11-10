# Releasing

Notes for maintainers. Nothing here has been published yet: no version is tagged.

## Service

Before cutting a release:

- CI is green on the exact commit, including the deployment rehearsal.
- The reference models and thresholds are still experimental. Check they carry
  verified licences and documented limits before calling a profile ready, and
  validate the policy on representative photos with the
  [evaluation harness](../evaluation/README.md).
- Check the target deployment's ingress removal and termination deadline. The
  [rehearsal](../deploy/rehearsal/README.md) proves the process behaviour on a Linux
  runner, not your platform.

## Go client

The module lives in `clients/go`, so tags need the subdirectory prefix, for example
`clients/go/v0.1.0`. A root `v0.1.0` tag does not version the module; see the
[Go module rules](https://go.dev/ref/mod#vcs-version).

Before tagging, install the exact revision into a fresh module with no `replace`
directive and run the [example](../clients/go/cmd/moderate/README.md) against a real
service. CI does this on every pull request. The module builds with Go 1.23, but
deploy with a currently supported toolchain.
