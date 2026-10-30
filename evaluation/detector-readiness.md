# Detector evaluation readiness — 30 October 2026

The local detector/classifier integration passes with generated tiny DETR and ViT
weights. Those tests establish loading, evidence, policy and failure behaviour.
They do not establish nudity detection or a gain in moderation quality.

## Artifact compatibility checked

Public Hugging Face metadata and author cards were inspected before downloading
or executing candidate weights. The following are candidates, not recommendations:

| Candidate and inspected revision | Artifacts | Backend at initial audit |
| --- | --- | --- |
| `deepghs/nudenet_onnx` at `44956a0587943016350c7815f9e0dec089d45cae` | `320n.onnx`, `nms-yolov8.onnx` | Unsupported: needs an ONNX detector backend and explicit decoding/preprocessing |
| `erax-ai/EraX-NSFW-V1.0` at `aea60ac8d2ebcbe0fcbb29e623eba99945b988a6` | YOLO11 `.pt` weights; also a `config.json` | Unsupported: a config file alone does not make these Transformers safetensors checkpoints |

Sources: [NudeNet ONNX card](https://huggingface.co/deepghs/nudenet_onnx),
[EraX author card](https://huggingface.co/erax-ai/EraX-NSFW-V1.0), and the public
model metadata endpoints under `https://huggingface.co/api/models/`.
The checked revisions freeze this observation; future repository updates may differ.
Publisher accuracy and efficiency claims were not independently reproduced.

At the initial audit, the detector backend required compatible Transformers models,
pinned revisions, safetensors, an image processor and no remote model code.
Neither candidate was loaded at that stage. No weights were converted, no pickle checkpoints
were executed and no new serving default was installed.

## What must happen before claiming improvement

1. Add and verify the appropriate backend, with explicit input preprocessing,
   output layout, class order, proposal filtering and suppression semantics.
   Pin the artifact revision and digest. Validate its scores and boxes, including
   malformed outputs and an empty detection result.
2. Review the candidate's model terms and training/evaluation documentation.
   Repository metadata alone is not verification of model provenance or terms.
3. Define the target categories before collecting labels. Explicit nudity,
   suggestive clothing and contextual sexual harm are different tasks. Covered
   body-part labels alone do not establish revealing clothing.
4. Freeze a suitable, independently labelled image cohort and separate calibration
   from held-out evaluation. Existing inspected examples remain diagnostics.
5. Compare classifier alone, detector alone and the combined policy on the same
   held-out cases. Keep model revisions and thresholds fixed during that comparison.
6. Report category precision/recall, sample counts, failed checks and uncertainty.
   Record latency and hardware separately; a smaller artifact does not prove faster
   inference. Do not omit failures or treat missing evidence as clean content.

The existing public photo challenge uses broader contextual sexual-safety labels;
it is not an explicit-nudity benchmark. Reusing it would not resolve that mismatch.
There is currently no evidence here that adding a detector improves moderation
accuracy, and no production model/profile recommendation follows from this audit.

## Follow-up: ONNX integration

A local `onnx_yolo_v8` backend now verifies, loads and warms the pinned
`320n.onnx` artifact above. Its embedded `names` metadata matches the configured
18-label order. Its embedded `license` value says `AGPL-3.0 License`, while the
repository card declares `apache-2.0`. This discrepancy is unresolved; the artifact
is a research candidate, not a licence-verified reference profile. Modera's project
licensing is unchanged.

The backend exposes winning-proposal category maxima without a confidence floor
or cross-class suppression. It is not a claim of parity with NudeNet's package
postprocessing. Training quality and independent moderation accuracy remain
unverified. See [the backend contract](../docs/onnx.md).

A synthetic 32×32 white-image API smoke also completed with this artifact and
`viddexa/nsfw-detection-2-mini` at revision
`15f61cddc0a1a2a9176f018fb6838ef92c8163cc`. The response contained two model
references and ten separately attributed findings. This confirms the trained
artifacts can run together; it does not measure accuracy. The built wheel also
loaded and warmed the ONNX artifact in an environment without torch or Transformers.
