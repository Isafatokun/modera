# Run an ONNX detector

Use `onnx_yolo_v8` for compatible detector exports. This backend runs on CPU and
can be installed without torch or Transformers:

```sh
uv sync --locked --extra onnx
```

For a classifier and ONNX detector together, install both `--extra inference` and
`--extra onnx`, then follow [combining models](combining-models.md).

## Model configuration

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

## What the scores mean

Preprocessing applies EXIF orientation, converts to RGB, pads the bottom/right
with black to make a square, then uses OpenCV linear resizing and scales by
`1 / 255`. The configured channel order is applied explicitly.

For each proposal, its highest-scoring label wins. Each mapped category receives
the highest confidence among proposals with that label, or zero when none wins.
There is no confidence cutoff before the policy. Boxes are not returned.
Class-wise non-maximum suppression would preserve these category maxima, so it is
not run. This does **not** reproduce a package that suppresses different classes
against each other or applies its own confidence floor.

Artifacts are limited to 256 MiB; images to 4096 pixels per side for this backend.
The existing byte/pixel limits still apply. Output is limited to 40,000 proposals
and 256 labels. These limits bound processing; they do not establish capacity.

## Research candidate

The pinned [candidate configuration](../evaluation/candidates/nudenet-onnx.json)
loads and warms successfully. It is not a recommended moderation profile. Its
model-card terms and embedded artifact terms disagree; that remains unresolved.
The [example policy](../examples/onnx-policy.json) illustrates configuration only.
Do not treat its threshold as calibrated.

```sh
uv run --locked --extra onnx python -m modera.prepare_models \
  --config evaluation/candidates/nudenet-onnx.json
```

Set `MODERA_MODEL_FILE` to your model file and `MODERA_POLICY_FILE` to your policy;
use the usual bearer key and serving command. See [Start here](start-here.md).
For a container, add `--build-arg ONNX=true`; add `INFERENCE=true` too when needed.
The reference Compose setup accepts `MODERA_ONNX=true` to include the extra.
