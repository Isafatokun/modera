# Policies

Policies are server-controlled, versioned rules that map category scores to `allow`,
`review`, or `block`. Clients select a policy by name; they cannot supply thresholds.

## Configuration

Set `MODERA_POLICY_FILE` to an operator-managed JSON file. The file is validated when
the application is created. Unreadable, invalid, empty, or oversized configurations
prevent startup. The maximum file size is 1 MiB.

The [example configuration](../examples/policies.json) contains a policy named `default`:

```sh
MODERA_POLICY_FILE=examples/policies.json \
  uv run --locked uvicorn modera.main:create_app --factory
```

The example thresholds demonstrate configuration; they are not calibrated safety
recommendations. Choose categories supported by your models and validate thresholds
against representative content and your application's policy.

No policy file is loaded by default and no thresholds are installed automatically.
With no file, the unfinished moderation endpoint still validates requests and returns
`501`. With a file, it additionally checks that the requested policy exists and defines
rules for the input type. Valid selections still return `501`; model inference is not
connected, so the HTTP endpoint does not yet evaluate scores or return decisions.

Configuration is an immutable snapshot per application instance. Editing the file
does not update an existing instance; restart or redeploy to apply changes. Increment
the policy version whenever its rules change. Versions are operator-assigned; Modera
does not maintain policy history or enforce uniqueness across deployments.

For containers, mount the file read-only and set `MODERA_POLICY_FILE` to its container
path. The example file is not included in the runtime image.

## Rule fields

Each policy has an `id`, a string `version`, and `text` and/or `image` rule arrays.
At least one rule is required. Policy IDs must be unique in the file; categories must
be unique within each policy's input type.

| Rule field | Meaning |
| --- | --- |
| `category` | Category score to evaluate |
| `required` | Defaults to `true`; required checks cannot be skipped |
| `review_threshold` | Optional inclusive threshold for human-review recommendation |
| `block_threshold` | Optional inclusive threshold for blocking |

At least one threshold is required. Thresholds must be finite numbers between 0 and 1.
When both are present, review must be lower than block. Unknown fields, string-valued
thresholds, and invalid booleans are rejected. Omit a rule to remove that policy check;
omit either threshold to use only review or only block.

## Evaluation

The evaluator operates on validated findings and coverage. It does not fetch media,
run models, store results, or create review cases.

1. Check that findings match evaluated coverage exactly, without duplicate categories.
2. Check coverage for every configured rule before making a decision.
3. If any evaluated rule reaches its block threshold, return `block`.
4. Otherwise, if any reaches its review threshold, return `review`.
5. Otherwise, return `allow` with no reasons.

Reasons are the categories that triggered the winning action, in policy order. A block
decision does not include categories that triggered only review. Additional model
findings outside the policy do not influence its decision.

| Coverage condition | Outcome |
| --- | --- |
| Required category unsupported or not applicable | `400 policy_unsupported` |
| Optional category explicitly unsupported or not applicable | Skip that rule |
| Configured category missing from all coverage lists | `503 check_unavailable`, retryable |
| No configured rules were evaluated | `400 policy_not_applicable` |
| Requested policy does not exist | `400 unknown_policy` |
| Policy has no rules for the input type | `400 policy_not_applicable` |

Failed checks must not be described as unsupported. Missing results for a configured
check cause an error even when the rule is optional. A missing required check is not
ignored merely because another category already reached a block threshold.
Inconsistent findings are an internal error, not an allow decision.

Required coverage is enforced at evaluation time. Model capability checks during
startup will be added with model integration. The current schemas do not represent
failed checks as completed findings: a processing failure must remain an error.

The evaluator is tested independently. Its check-failure responses will reach clients
once inference is connected; they are not produced by the current HTTP validation path.
