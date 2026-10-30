import pytest
from pydantic import ValidationError

from modera.inference import InvalidModelOutput, aggregate_detections
from modera.model_config import DetectorModelSettings, ModelConfig

LABELS = {0: "a", 1: "b", 2: "unmapped"}
MAPPING = {"a": "test.a", "b": "test.b"}


def test_detector_aggregates_maxima_and_includes_absent_categories() -> None:
    result = aggregate_detections(
        [0.2, 0.8, 0.9],
        [0, 0, 2],
        [[0.0, 0.0, 1.0, 1.0]] * 3,
        LABELS,
        MAPPING,
    )
    assert [(f.category, f.score) for f in result] == [("test.a", 0.8), ("test.b", 0.0)]
    assert [f.score for f in aggregate_detections([], [], [], LABELS, MAPPING)] == [
        0.0,
        0.0,
    ]


@pytest.mark.parametrize(
    "score,label,box",
    [
        (float("nan"), 0, [0.0, 0.0, 1.0, 1.0]),
        (float("inf"), 0, [0.0, 0.0, 1.0, 1.0]),
        (1.1, 0, [0.0, 0.0, 1.0, 1.0]),
        (0.5, 99, [0.0, 0.0, 1.0, 1.0]),
        (0.5, 0, [1.0, 0.0, 0.0, 1.0]),
        (0.5, 0, [0.0, 0.0, float("nan"), 1.0]),
    ],
)
def test_invalid_detector_evidence_is_not_a_verdict(
    score: float,
    label: int,
    box: list[float],
) -> None:
    with pytest.raises(InvalidModelOutput):
        aggregate_detections([score], [label], [box], LABELS, MAPPING)


def test_mismatched_output_lengths_fail() -> None:
    with pytest.raises(InvalidModelOutput):
        aggregate_detections([0.2], [], [], LABELS, MAPPING)


def test_detector_configuration_and_legacy_classifier() -> None:
    common = {"id": "org/model", "revision": "a" * 40, "label_map": MAPPING}
    config = ModelConfig.model_validate(
        {"image": {**common, "backend": "transformers_object_detection"}}
    )
    assert isinstance(config.image, DetectorModelSettings)
    assert ModelConfig.model_validate({"image": common}).image is not None
    with pytest.raises(ValidationError):
        ModelConfig.model_validate(
            {"text": {**common, "backend": "transformers_object_detection"}}
        )


def test_detector_prediction_uses_zero_cutoff_and_validates_native_output() -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace
    from typing import Any, cast

    from modera.inference import Detector, PreparedImage

    detector = object.__new__(Detector)
    detector.settings = DetectorModelSettings(
        id="org/model", revision="a" * 40, label_map=MAPPING
    )
    detector._labels = LABELS
    finite = True
    detector._torch = cast(
        Any,
        SimpleNamespace(
            inference_mode=nullcontext,
            isfinite=lambda _: SimpleNamespace(
                all=lambda: SimpleNamespace(item=lambda: finite)
            ),
        ),
    )
    detector._model = lambda **_: SimpleNamespace(logits=object(), pred_boxes=object())

    def tensor(value: object) -> object:
        return SimpleNamespace(tolist=lambda: value)

    def postprocess(output: object, threshold: float) -> list[dict[str, object]]:
        assert threshold == 0.0
        return [
            {
                "scores": tensor([0.8]),
                "labels": tensor([0]),
                "boxes": tensor([[0.1, 0.2, 0.8, 0.9]]),
            }
        ]

    detector._processor = cast(
        Any, SimpleNamespace(post_process_object_detection=postprocess)
    )
    assert [f.score for f in detector.predict(PreparedImage({}))] == [0.8, 0.0]
    finite = False
    with pytest.raises(InvalidModelOutput):
        detector.predict(PreparedImage({}))
