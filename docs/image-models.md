# Photo detectors and combined models

A classifier scores a whole photo. A detector reports labelled regions, and Modera keeps
the highest confidence per category. You can run either on its own, or put several
models on one photo and write policy rules over their separate scores. Set up weights
and the model file as described in [models](models.md#set-up-classifiers).

## Object detectors

Choose a detector when you need labels for objects or visible regions rather than
one classification for the whole photo. The first detector backend supports
Transformers models compatible with `AutoModelForObjectDetection` and an image
processor with `post_process_object_detection`. A separate [ONNX backend](#onnx-detectors)
supports explicitly configured YOLOv8 proposal exports. Neither loads arbitrary
ONNX layouts or PyTorch pickle files.

In your existing models file, set the image backend to
`transformers_object_detection`. Set `id` and `revision` to the model repository
and its full pinned commit, and map its native labels to your policy categories
with `label_map`. The remaining setup and model preparation steps are unchanged.
Use the inference extra, as with classifiers. Preparation checks that the model
loads and runs; it does not establish moderation accuracy.

For each mapped category, the service returns the highest detection confidence.
If no detection has that label, its score is zero and coverage says evaluated.
Unmapped labels do not become findings. Invalid scores, unknown output labels,
or malformed boxes fail the request with a non-retryable internal error.

No confidence cutoff is applied before policy evaluation. Your policy thresholds
still decide allow, review or block. Scores are model confidence values, not
calibrated probabilities. Boxes remain internal and are not returned.

To use a detector with a classifier on the same photo, see
[combining models](#combine-image-models). Person association is not supported. A generic
object detector is not automatically a suitable moderation model, so evaluate the actual
model and categories before use.

## ONNX detectors

Use `onnx_yolo_v8` for compatible detector exports. This backend runs on CPU and
can be installed without torch or Transformers:

```sh
uv sync --locked --extra onnx
```

For a classifier and ONNX detector together, install both `--extra inference` and
`--extra onnx`, then follow [combining models](#combine-image-models).

### Model configuration

The model entry needs a pinned repository revision, a single `.onnx` filename,
its SHA-256 digest, the complete native label order, selected `label_map`, square
`input_size` and explicit `channel_order` (`rgb` or `bgr`). Embedded native label
metadata, when present, must agree with the configured order.

The supported contract is float32 input `[1, 3, size, size]` and one float32 output
`[1, 4 + classes, proposals]`. The first four channels are centre-x, centre-y,
width and height in input-image coordinates. Remaining channels are class scores
in `[0, 1]`, without a separate objectness channel. Symbolic dimensions are allowed,
but actual output is checked during warmup and every request. Other ONNX layouts,
end-to-end suppressed outputs and PyTorch `.pt` files are not supported.

Preparation downloads only the named artifact and checks its hash, graph and
warmup. Serving loads the verified cached artifact offline. External tensor files
are rejected. Only use trusted models: a digest identifies bytes; it does not
establish their quality or provenance.

### What the scores mean

Preprocessing applies EXIF orientation, converts to RGB, pads the bottom/right
with black to make a square, then uses OpenCV linear resizing and scales by
`1 / 255`. The configured channel order is applied explicitly.

For each proposal, its highest-scoring label wins. Each mapped category receives
the highest confidence among proposals with that label, or zero when none wins.
There is no confidence cutoff before the policy. Boxes are not returned.
Class-wise non-maximum suppression would preserve these category maxima, so it is
not run. A package that suppresses across classes or applies its own confidence floor
will score differently.

Artifacts are limited to 256 MiB; images to 4096 pixels per side for this backend.
The existing byte/pixel limits still apply. Output is limited to 40,000 proposals
and 256 labels. These limits bound processing and say nothing about capacity.

### Research candidate

The pinned [candidate configuration](../examples/nudenet-onnx.json)
loads and warms successfully. It is not a recommended moderation profile. Its
model-card terms and embedded artifact terms disagree; that remains unresolved.
The [example policy](../examples/onnx-policy.json) illustrates configuration only.
Do not treat its threshold as calibrated.

```sh
uv run --locked --extra onnx python -m modera.prepare_models \
  --config examples/nudenet-onnx.json
```

Set `MODERA_MODEL_FILE` to your model file and `MODERA_POLICY_FILE` to your policy;
use the usual bearer key and serving command. See [Start here](start-here.md).
For a container, add `--build-arg ONNX=true`; add `INFERENCE=true` too when needed.
The reference Compose setup accepts `MODERA_ONNX=true` to include the extra.

## Combine image models

Use a detector and a classifier together when they provide complementary evidence.
Configure up to four image models in `image_models` instead of the single `image`
field.

Start from [example model configuration](../examples/ensemble-models.json) and
[example policy](../examples/ensemble-policies.json). Replace the example model IDs,
pinned revisions and native labels with your chosen models before preparation.
These files demonstrate configuration; they are not a tested moderation profile.
Use the same [prepare and run steps](models.md#set-up-classifiers).

Give each model a different repository ID and separate category names, such as
`detect.exposed` and `classify.sexual`. Scores are kept separate. A high detector
score is not averaged away by a low classifier score. Findings include `model`
to identify their producer; `models` contains the revisions actually used.

### Choose how evidence becomes an action

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

Minimums are inclusive, and these are example values, not recommended thresholds.
A combination produces no new score. The response keeps the original scores; `decision.reasons` lists contributing categories and
`decision.matched_rules` identifies composite rules contributing to the chosen
action. Block takes precedence over review.

Combinations apply to the whole image. They do not establish that two detections
belong to the same person, or that the content has a particular intent. The only
supported `scope` is `image`; `same_subject` is rejected.

### Failures and resource limits

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
