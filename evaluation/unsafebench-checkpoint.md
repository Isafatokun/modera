# UnsafeBench photo evaluation checkpoint

The next step is calibration on data separate from the inspected test set.
The uploaded test file has been recovered and its SHA-256 matches the original:
`c1fd60ac62277a45c568d670adb60824881a05f4e041148a08f2547278b8f59c`.
Keep the raw Parquet and extracted images outside Git.

## Recorded result from 31 October 2026

The prior run selected all 211 `Sexual` rows from the 2,037-row uploaded test
artifact: 150 unsafe and 61 safe, with 102 LAION-5B and 109 Lexica cases.
A historical experimental cutoff of 0.95 was frozen before reading these rows.

| Configuration | Unsafe escalated / 150 | Unsafe missed | Safe escalated / 61 | API failures |
| --- | --- | --- | --- | --- |
| Classifier | 87 | 63 | 8 | 0 |
| Detector | 0 | 150 | 0 | 0 |
| Combined | 87 | 63 | 8 | 0 |

Classifier and combined decisions matched. Real-world cases: 32/79 unsafe and
2/23 safe escalated. Generated cases: 55/71 unsafe and 6/38 safe escalated.
The maximum mapped detector score was 0.8685214519500732, below the fixed cutoff.
This compares the specified policies, not detectors and classifiers at equally
calibrated error rates. No production threshold follows from the result.

Models recorded in that run:

- `viddexa/nsfw-detection-2-mini` at
  `15f61cddc0a1a2a9176f018fb6838ef92c8163cc`, softmax `porn`/`hentai` categories.
- `deepghs/nudenet_onnx` at `44956a0587943016350c7815f9e0dec089d45cae`,
  `320n.onnx` SHA-256
  `c15d8273adad2d0a92f014cc69ab2d6c311a06777a55545f2c4eb46f51911f0f`;
  five exposed-category maxima using Modera's backend, without package NMS.
- Combined: OR of those category thresholds, with all required evidence completed.

The labels describe broad sexual safety, not explicit nudity, clothing coverage
or sexual activity separately. Model-training overlap remains unknown. The
candidate detector's model-card and artifact terms still disagree; see
[detector readiness](detector-readiness.md).

## Recovery limits — 2 November 2026

Workspace maintenance removed the unpublished evaluation checkout, including its
runner, receipts and raw response records. The historical local commit `fc04915`
was not pushed and is not present in the recovered GitHub repository.
This checkpoint reconstructs the counts and model settings from the recorded
conversation, not from recovered raw predictions. It is not a rerun or a substitute
for the original machine-readable report. No original runner hash, receipts or
per-case results are asserted to have been restored.

Merged service code remains available in PR #12. The user-uploaded test Parquet
was recovered separately and verified against the recorded digest above.

## Inputs for the next study

Obtain the separate `data/train-*.parquet` files from the approved UnsafeBench
repository. Upload all shards belonging to that split; the test file is already
available. Do not commit the dataset or credentials.

Before inference or threshold selection:

1. Validate the new artifact's schema and record file hashes. Keep only the
   declared task cohort, without choosing examples by model outcome.
2. Audit byte duplicates and known related sources against the inspected test
   cohort and within the new data. Group related images before partitioning;
   exact hashes alone do not establish independence.
3. Reserve a previously uninspected assessment partition from the new data before
   scoring. Seal its labels from the calibration process. The publisher's `train`
   designation does not establish that a candidate model has never seen it.
4. Freeze the models, eligible score categories and empirical error budgets.
   Calibrate on the validation partition, allowing different model thresholds.
   Compare complete policies, including combined-model failures; do not hide
   missed unsafe cases behind a zero-false-alarm result.
5. Assess the frozen candidate on the reserved partition. The already-inspected
   211 test cases may remain a regression diagnostic, but cannot be reused to
   validate tuning decisions informed by their outcomes.

The existing [calibration and held-out assessment](calibration.md) tools address
one category's review threshold. They do not by themselves calibrate an entire
multi-rule ensemble. Restore and verify the photo collection workflow before
running the new study; preserve its scripts and aggregate reports through Git.

No new inference, threshold selection or accuracy measurement was performed while
creating this recovery checkpoint.
