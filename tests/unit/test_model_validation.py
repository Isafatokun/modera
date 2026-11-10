from types import SimpleNamespace
from typing import Any

import pytest

from modera.inference import hub_options, label_indices, load_pretrained
from modera.model_config import ImageModelSettings, OnnxDetectorSettings
from modera.onnx_detector import check_label_order, check_signature

REVISION = "a" * 40


class FakeModel:
    def __init__(self, labels: dict[int, str], num_labels: int | None = None) -> None:
        self.config = SimpleNamespace(
            id2label=labels,
            num_labels=len(labels) if num_labels is None else num_labels,
        )
        self.evaluating = False

    def eval(self) -> None:
        self.evaluating = True


class FakeLoader:
    def __init__(self, loading: dict[str, Any]) -> None:
        self.loading = loading
        self.model = FakeModel({0: "safe", 1: "nsfw"})
        self.call: dict[str, Any] = {}

    def from_pretrained(self, name: str, **options: Any) -> tuple[FakeModel, Any]:
        self.call = {"name": name, **options}
        return self.model, self.loading


def image_settings(activation: str = "softmax") -> ImageModelSettings:
    return ImageModelSettings(
        id="example/image",
        revision=REVISION,
        activation=activation,  # type: ignore[arg-type]
        label_map={"nsfw": "sexual"},
    )


def test_hub_options_are_offline_and_never_run_remote_code(tmp_path: Any) -> None:
    options = hub_options(image_settings())
    assert options == {
        "revision": REVISION,
        "cache_dir": None,
        "local_files_only": True,
        "trust_remote_code": False,
    }
    cached = image_settings().model_copy(update={"cache_dir": tmp_path})
    assert hub_options(cached)["cache_dir"] == str(tmp_path)


def test_load_pretrained_requires_safetensors_and_sets_eval_mode() -> None:
    loader = FakeLoader({})
    torch = SimpleNamespace(float32="float32")
    model = load_pretrained(
        loader, image_settings(), hub_options(image_settings()), torch, "classifier"
    )
    assert model is loader.model and model.evaluating
    assert loader.call["use_safetensors"] is True
    assert loader.call["output_loading_info"] is True
    assert loader.call["dtype"] == "float32"
    assert loader.call["local_files_only"] is True


@pytest.mark.parametrize(
    "key", ["missing_keys", "unexpected_keys", "mismatched_keys", "error_msgs"]
)
@pytest.mark.parametrize("architecture", ["classifier", "detector"])
def test_checkpoint_problems_are_rejected_before_use(
    key: str, architecture: str
) -> None:
    loader = FakeLoader({key: ["layer.weight"]})
    with pytest.raises(
        ValueError, match=f"does not match the {architecture} architecture"
    ):
        load_pretrained(
            loader,
            image_settings(),
            {},
            SimpleNamespace(float32="float32"),
            architecture,
        )
    assert not loader.model.evaluating


def test_label_indices_map_native_labels_to_positions() -> None:
    model = FakeModel({0: "safe", 1: "nsfw"})
    assert label_indices(model, image_settings()) == {"safe": 0, "nsfw": 1}


@pytest.mark.parametrize(
    "model,activation,message",
    [
        (FakeModel({0: "safe", 2: "nsfw"}), "softmax", "label mappings"),
        (FakeModel({0: "safe", 1: "nsfw"}, num_labels=3), "softmax", "label mappings"),
        (FakeModel({0: "safe", 1: "other"}), "softmax", "label mappings"),
        (FakeModel({0: "nsfw"}), "softmax", "at least two labels"),
    ],
)
def test_label_indices_reject_inconsistent_models(
    model: FakeModel, activation: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        label_indices(model, image_settings(activation))


def test_single_label_sigmoid_model_is_allowed() -> None:
    assert label_indices(FakeModel({0: "nsfw"}), image_settings("sigmoid")) == {
        "nsfw": 0
    }


def onnx_settings() -> OnnxDetectorSettings:
    return OnnxDetectorSettings(
        id="example/onnx",
        revision=REVISION,
        filename="model.onnx",
        sha256="b" * 64,
        labels=("face", "hand"),
        input_size=32,
        channel_order="rgb",
        label_map={"face": "face"},
    )


def tensor(kind: str, shape: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(type=kind, shape=shape, name="images")


def session(inputs: list[Any], outputs: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(get_inputs=lambda: inputs, get_outputs=lambda: outputs)


GOOD_INPUT = tensor("tensor(float)", [1, 3, 32, 32])
GOOD_OUTPUT = tensor("tensor(float)", [1, 6, 100])


def test_matching_signature_is_accepted_including_dynamic_dimensions() -> None:
    check_signature(session([GOOD_INPUT], [GOOD_OUTPUT]), onnx_settings())
    dynamic_in = tensor("tensor(float)", ["batch", 3, "h", "w"])
    dynamic_out = tensor("tensor(float)", [1, 6, "proposals"])
    check_signature(session([dynamic_in], [dynamic_out]), onnx_settings())


@pytest.mark.parametrize(
    "inputs,outputs,message",
    [
        ([], [GOOD_OUTPUT], "input shape"),
        ([GOOD_INPUT, GOOD_INPUT], [GOOD_OUTPUT], "input shape"),
        ([tensor("tensor(double)", [1, 3, 32, 32])], [GOOD_OUTPUT], "input shape"),
        ([tensor("tensor(float)", [1, 3, 32])], [GOOD_OUTPUT], "input shape"),
        ([tensor("tensor(float)", [1, 3, 64, 64])], [GOOD_OUTPUT], "input shape"),
        ([GOOD_INPUT], [], "one float32 proposal output"),
        ([GOOD_INPUT], [GOOD_OUTPUT, GOOD_OUTPUT], "one float32 proposal output"),
        ([GOOD_INPUT], [tensor("tensor(double)", [1, 6, 10])], "float32"),
        ([GOOD_INPUT], [tensor("tensor(float)", [1, 6])], "output layout"),
        ([GOOD_INPUT], [tensor("tensor(float)", [2, 6, 10])], "output layout"),
        ([GOOD_INPUT], [tensor("tensor(float)", [1, 5, 10])], "output layout"),
        ([GOOD_INPUT], [tensor("tensor(float)", [1, 6, 40_001])], "output layout"),
    ],
)
def test_signature_mismatches_are_rejected(
    inputs: list[Any], outputs: list[Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        check_signature(session(inputs, outputs), onnx_settings())


@pytest.mark.parametrize(
    "names",
    [None, '["face", "hand"]', '{"0": "face", "1": "hand"}', "{0: 'face', 1: 'hand'}"],
)
def test_matching_label_metadata_is_accepted(names: str | None) -> None:
    check_label_order(names, ("face", "hand"))


@pytest.mark.parametrize(
    "names,message",
    [
        ('["hand", "face"]', "label order"),
        ('["face"]', "label order"),
        ('"face"', "label order"),
        ('{"0": "face"}', "label order"),
        ("x" * (64 * 1024 + 1), "too large"),
    ],
)
def test_mismatched_label_metadata_is_rejected(names: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_label_order(names, ("face", "hand"))
