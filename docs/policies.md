# Policies

Set `MODERA_POLICY_FILE` to an operator-managed JSON file; see the
[example](../examples/policies.json). Clients select a policy by name and never supply
thresholds.

Each policy has an `id`, a string `version`, and `text` and/or `image` rule arrays. IDs are
unique, and categories are unique within each input type.

| Rule field | Meaning |
| --- | --- |
| `category` | Category score to evaluate |
| `required` | Defaults to `true`; the check cannot be skipped |
| `review_threshold` | Optional inclusive threshold for review |
| `block_threshold` | Optional inclusive threshold for block |

Each policy needs at least one rule, and each rule at least one threshold. Thresholds are
finite numbers in [0, 1], and review must be lower than block when both exist. Unknown
fields and invalid types are rejected. Omit a rule to remove that check.

The example thresholds are placeholders. Choose yours from your own labelled sample; a
cutoff-selection command is kept at the `evaluation-archive` git tag.

## Evaluation

Coverage is checked first. Block takes precedence over review, then allow. Reasons list the
winning categories in policy order, and findings outside the policy do not affect its
decision.

| Condition | Outcome |
| --- | --- |
| Required category unsupported or not applicable | `400 policy_unsupported` |
| Optional category explicitly unsupported or not applicable | Skip the rule |
| Configured check missing from all coverage lists | `503 check_unavailable`, retryable, `Retry-After: 2` |
| No rules evaluated, or input type unsupported by the policy | `400 policy_not_applicable` |
| Policy name unknown | `400 unknown_policy` |

A failed check is not an unsupported check. A missing result is an error even for optional
rules, or when another category already triggers block. Inconsistent findings are an
internal error, never an allow. Models declare their mapped categories, and rules declare
only `rules.*` categories. A [fallback policy](models.md#fallback) is recorded under its
own ID and version, so it never claims the original policy completed.

## Deployment

An unreadable, invalid, empty or oversized (over 1 MiB) file prevents startup. The service
keeps an immutable snapshot, so restart after a change and bump the version. Versions are
yours to assign, and Modera keeps no policy history. Mount the file read-only in
containers, since the example is not in the runtime image. No file or thresholds are installed by default, and configuring any backend
requires a policy file. Unconfigured modalities return `501`.

To combine evidence from different photo models, see
[combining models](image-models.md#combine-image-models).
