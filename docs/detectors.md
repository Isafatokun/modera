# Use an object detector

Choose a detector when you need labels for objects or visible regions rather than
one classification for the whole photo. The first detector backend supports
Transformers models compatible with `AutoModelForObjectDetection` and an image
processor with `post_process_object_detection`. A separate [ONNX backend](onnx.md)
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

Existing classifier configurations and API requests continue to work. For a detector and classifier on the same photo, see
[combining models](combining-models.md). Person association is not supported. A generic object detector is not automatically a
suitable moderation model: evaluate the actual model and categories before use.
