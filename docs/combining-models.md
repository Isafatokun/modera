# Combine image models

Use a detector and a classifier together when they provide complementary evidence.
Configure up to four image models in `image_models` instead of the single `image`
field. Existing single-model configurations still work.

Start from [example model configuration](../examples/ensemble-models.json) and
[example policy](../examples/ensemble-policies.json). Replace the example model IDs,
pinned revisions and native labels with your chosen models before preparation.
These files demonstrate configuration; they are not a tested moderation profile.
Use the same [prepare and run steps](models.md#classifiers).

Give each model a different repository ID and separate category names, such as
`detect.exposed` and `classify.sexual`. Scores are kept separate. A high detector
score is not averaged away by a low classifier score. Findings include `model`
to identify their producer; `models` contains the revisions actually used.

## Choose how evidence becomes an action

Ordinary policy rules still work: any block threshold wins over review thresholds.
All checks required by the policy must finish before a decision is returned.

For combinations, add `image_composites` to a policy. Each rule has a unique `id`,
an `action` of `review` or `block`, and exactly one of:

| Field | When the rule fires |
| --- | --- |
| `all_of` | Every listed category reaches its own `min` score |
| `any_of` | At least one listed category reaches its own `min` score |

For example, review only when both signals reach their own thresholds:

```json
{
  "id": "signals-together",
  "action": "review",
  "all_of": [
    {"category": "detect.exposed", "min": 0.7},
    {"category": "classify.sexual", "min": 0.6}
  ]
}
```

Minimums are inclusive. They are example values, not recommended thresholds.
A combination produces no new probability or invented model finding. The response
keeps the original scores; `decision.reasons` lists contributing categories and
`decision.matched_rules` identifies composite rules contributing to the chosen
action. Block takes precedence over review.

Combinations apply to the whole image. They do not establish that two detections
belong to the same person, or that the content has a particular intent. The only
supported `scope` is `image`; `same_subject` is rejected.

## Failures and resource limits

Models needed by the selected policy run in configuration order, with one shared
inference deadline. Models not needed by that policy are not run. All listed
conditions require evidence, including `any_of`: an unavailable check cannot be
silently skipped just because another condition matched. A failure returns an
error without a decision, even if an earlier model found a blocking signal.

Each model has its own bounded worker and queue. Native work retains its slot after
timeout or cancellation. Models run sequentially within one request; different
requests can occupy different model workers at the same time. Multi-model requests
validate the photo first, then each selected model decodes and prepares the bytes
on its own worker. Budget for additional CPU, latency and memory. All configured
models load at startup, even when a particular request uses only one.

There is no image-model fallback or partial-success mode. Measure the combined
policy on independent labelled examples before relying on it. Supporting two model
architectures does not establish better moderation accuracy or production capacity.
