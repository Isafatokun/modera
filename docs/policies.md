# Policies

Set `MODERA_POLICY_FILE` to an operator-managed JSON file; see the
[example](../examples/policies.json). Clients select a name, never thresholds.

Each policy has an `id`, string `version`, and `text` and/or `image` rule arrays.
IDs must be unique; categories must be unique within each input type.

| Rule field | Meaning |
| --- | --- |
| `category` | Category score to evaluate |
| `required` | Defaults to `true`; the check cannot be skipped |
| `review_threshold` | Optional inclusive threshold for review |
| `block_threshold` | Optional inclusive threshold for block |

At least one rule per policy and one threshold per rule are required. Thresholds
must be finite numbers in [0, 1]; review must be lower than block when both exist.
Unknown fields and invalid types are rejected. Omit a rule to remove that check.
Example thresholds are placeholders. Choose yours from your own labelled sample. A cutoff-selection command is kept at
the `evaluation-archive` git tag.

## Evaluation

Coverage is checked first. Block takes precedence over review, then allow. Reasons
list the winning categories in policy order. Findings outside the policy do not affect
its decision.

| Condition | Outcome |
| --- | --- |
| Required category unsupported/not applicable | `400 policy_unsupported` |
| Optional category explicitly unsupported/not applicable | Skip rule |
| Configured check missing from all coverage lists | `503 check_unavailable`, retryable, `Retry-After: 2` |
| No rules evaluated or input type unsupported by policy | `400 policy_not_applicable` |
| Policy name unknown | `400 unknown_policy` |

Failed checks are not unsupported checks. Missing results cause errors, even for
optional rules or when another category already triggers block. Inconsistent findings
are an internal error, never allow. Models declare their mapped categories; rules
declare only `rules.*` categories. Explicit [fallback policies](models.md) are recorded
under their own ID/version, rather than claiming the original policy completed.

## Deployment

An unreadable, invalid, empty or oversized (over 1 MiB) file prevents startup. The
service keeps an immutable snapshot, so restart after a change and bump the version.
Versions are yours to assign and Modera keeps no policy history. Mount the file
read-only in containers; the example is not in the runtime image.

No file or thresholds are installed by default. Configuring any moderation backend
requires a policy file; unconfigured modalities return `501`.

For image-wide combinations of evidence from different models, see
[combining models](image-models.md#combine-image-models). Existing individual thresholds still apply.
