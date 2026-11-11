# Photo models

A classifier scores a whole photo. A detector reports labelled regions, and Modera keeps the
highest confidence per category. Run either alone, or put several models on one photo and
write policy rules over their separate scores. Set up weights and the model file as
described in [models](models.md#set-up-classifiers).

## Object detectors

Use a detector when you need labels for objects or regions, not one score for the whole
photo. The Transformers backend supports models compatible with
`AutoModelForObjectDetection` and an image processor with `post_process_object_detection`.
The [ONNX backend](#onnx-detectors) supports explicitly configured YOLOv8 exports. Neither
loads arbitrary ONNX layouts or PyTorch pickle files.

In your models file, set the image backend to `transformers_object_detection`, set `id` and
`revision` to the repository and its full pinned commit, and map the model's native labels
to policy categories with `label_map`. Other setup steps are unchanged, and you need the
inference extra, as with classifiers. Preparation checks that the model loads and runs. It
does not establish accuracy.

For each mapped category the service returns the highest detection confidence. If nothing
has that label, the score is zero and coverage says evaluated. Unmapped labels do not
become findings. Invalid scores, unknown output labels or malformed boxes fail the request
with a non-retryable internal error. No confidence cutoff is applied before the policy, and
boxes are not returned. Scores are model confidence values, not probabilities.

Person association is not supported. A generic object detector is not automatically a
suitable moderation model, so evaluate the actual model and categories before use.

## ONNX detectors

`onnx_yolo_v8` runs compatible detector exports on CPU, without torch or Transformers:

```sh
uv sync --locked --extra onnx
```

To use a classifier and an ONNX detector together, install both `--extra inference` and
`--extra onnx`, then follow [combining models](#combine-image-models).

### Model configuration

The entry needs a pinned repository revision, a single `.onnx` filename, its SHA-256
digest, the complete native label order, a `label_map`, a square `input_size` and an
explicit `channel_order` (`rgb` or `bgr`). Embedded label metadata, when present, must agree
with the configured order.

The supported contract is float32 input `[1, 3, size, size]` and one float32 output
`[1, 4 + classes, proposals]`. The first four channels are centre-x, centre-y, width and
height in input-image coordinates. The rest are class scores in `[0, 1]`, with no separate
objectness channel. Symbolic dimensions are allowed, but the actual output is checked during
warmup and on every request. Other ONNX layouts, end-to-end suppressed outputs and PyTorch
`.pt` files are not supported.

Preparation downloads only the named artifact and checks its hash, graph and warmup.
Serving loads the verified cached artifact offline, and external tensor files are rejected.
A digest identifies bytes. It does not establish quality or provenance, so use only trusted
models.

### What the scores mean

Preprocessing applies EXIF orientation, converts to RGB, pads the bottom and right with
black to a square, resizes linearly with OpenCV, scales by `1 / 255`, and applies the
configured channel order. Each proposal's highest-scoring label wins. Each mapped category
gets the highest confidence among proposals with that label, or zero when none wins. There
is no confidence cutoff before the policy. Class-wise non-maximum suppression would keep
these maxima, so it is not run. A package that suppresses across classes or applies its own
confidence floor will score differently.

Limits: artifacts up to 256 MiB, images up to 4096 pixels per side, 40,000 proposals and 256
labels per output. The general byte and pixel limits still apply. These bound processing and
say nothing about capacity.

### Research candidate

The pinned [candidate configuration](../examples/nudenet-onnx.json) loads and warms, but it
is not a recommended moderation profile: its model-card terms and embedded artifact terms
disagree, and that is unresolved. The [example policy](../examples/onnx-policy.json) only
illustrates configuration, and its threshold is not calibrated.

```sh
uv run --locked --extra onnx python -m modera.prepare_models \
  --config examples/nudenet-onnx.json
```

Set `MODERA_MODEL_FILE` and `MODERA_POLICY_FILE` as for any model. For a container, add
`--build-arg ONNX=true`, and `INFERENCE=true` too when needed.

## Combine image models

Use a detector and a classifier together when they give complementary evidence. Configure up
to four image models in `image_models` instead of the single `image` field. Start from the
[example models](../examples/ensemble-models.json) and
[example policy](../examples/ensemble-policies.json), replace the example IDs, pinned
revisions and labels, then use the same [prepare and run steps](models.md#set-up-classifiers).
These files show configuration; they are not a tested moderation profile.

Give each model a different repository ID and separate category names, such as
`detect.exposed` and `classify.sexual`. Scores stay separate, so a high detector score is
not averaged away by a low classifier score. Findings include `model` to name their producer,
and `models` lists the revisions actually used.

### Combining evidence

Ordinary rules still work, and any block threshold beats review thresholds. For
combinations, add `image_composites` to a policy. Each rule has a unique `id`, an `action` of
`review` or `block`, and exactly one of:

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

Minimums are inclusive, and these values are examples. A combination produces no new score.
The response keeps the original scores, `decision.reasons` lists the contributing
categories, and `decision.matched_rules` names the composite rules behind the action. Block
beats review.

Combinations apply to the whole image. They do not show that two detections are the same
person or that the content has any particular intent. The only supported `scope` is `image`,
and `same_subject` is rejected.

### Failures and limits

Models the policy needs run sequentially, in configuration order, under one shared inference
deadline. Others do not run. Every listed condition needs evidence, including each one in
an `any_of`, so an unavailable check cannot be skipped just because another matched. A
failure returns an error with no decision, even if an earlier model found a blocking signal.

Each model has its own bounded worker and queue, and native work keeps its slot after a
timeout or cancellation. Different requests can use different model workers at the same
time. A multi-model request validates the photo once, then each model decodes and prepares
the bytes on its own worker, so budget extra CPU, latency and memory. All configured models
load at startup, even when a request uses only one.

There is no image-model fallback or partial-success mode. Measure a combined policy on
independent labelled examples before relying on it. Supporting two architectures does not
show better accuracy or production capacity.
