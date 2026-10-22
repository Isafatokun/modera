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
Example thresholds are illustrative; calibrate against your models and content.

## Evaluation

Coverage is checked first. Block takes precedence over review, then allow. Reasons
list the winning categories in policy order. Findings outside the policy do not affect
its decision. Evaluation does not run models, store results, or create review cases.

| Condition | Outcome |
| --- | --- |
| Required category unsupported/not applicable | `400 policy_unsupported` |
| Optional category explicitly unsupported/not applicable | Skip rule |
| Configured check missing from all coverage lists | `503 check_unavailable`, retryable, `Retry-After: 2` |
| No rules evaluated or input type unsupported by policy | `400 policy_not_applicable` |
| Policy name unknown | `400 unknown_policy` |

Failed checks are not unsupported checks. Missing results cause errors, even for
optional rules or when another category already triggers block. Inconsistent findings
are an internal error, never allow. Model capability checks at startup await inference
integration; the HTTP endpoint does not yet produce evaluation decisions or failures.

## Deployment

Unreadable, invalid, empty, or oversized (>1 MiB) configurations prevent startup.
Each application keeps an immutable snapshot; restart/redeploy after changes and bump
the version. Versions are operator-assigned; there is no stored policy history.
Mount files read-only in containers. The example is not included in the runtime image.

No file or thresholds are installed by default. Without a file, requests receive
syntax validation only; configured policy selections are checked when a file is loaded.
Valid authenticated requests still return `501` until inference is connected.
