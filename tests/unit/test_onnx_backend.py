import hashlib
import os
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from modera.errors import APIError
from modera.inference import InvalidModelOutput
from modera.model_config import OnnxDetectorSettings
from modera.onnx_detector import OnnxDetector, decode_proposals, reject_external_data

pytestmark = pytest.mark.onnx
os.environ["ORT_DISABLE_TELEMETRY"] = "1"
np = pytest.importorskip("numpy")
onnx = pytest.importorskip("onnx")
cv = pytest.importorskip("cv2")
pytest.importorskip("onnxruntime")


def settings(**updates: Any) -> OnnxDetectorSettings:
    return OnnxDetectorSettings.model_validate(
        {
            "id": "example/detector",
            "revision": "a" * 40,
            "filename": "model.onnx",
            "sha256": "0" * 64,
            "labels": ("a", "b"),
            "input_size": 32,
            "channel_order": "rgb",
            "label_map": {"a": "detect.a", "b": "detect.b"},
            **updates,
        }
    )


def output() -> Any:
    return np.array(
        [
            [
                [10, 10, 10],
                [10, 10, 10],
                [4, 4, 4],
                [4, 4, 4],
                [0.8, 0.2, 0.1],
                [0.1, 0.7, 0.9],
            ]
        ],
        dtype=np.float32,
    )


def test_scores_keep_per_category_max_without_a_hidden_cutoff() -> None:
    findings = decode_proposals(output(), settings())
    assert [f.score for f in findings] == pytest.approx([0.8, 0.9])
    tiny = output()
    tiny[0, 4:] *= 0.001
    assert [f.score for f in decode_proposals(tiny, settings())] == pytest.approx(
        [0.0008, 0.0009]
    )
    assert [
        f.score
        for f in decode_proposals(np.zeros((1, 6, 0), dtype=np.float32), settings())
    ] == [0, 0]


@pytest.mark.parametrize(
    "broken",
    ["nan", "range", "width", "empty_box", "shape", "dtype", "float64", "bound"],
)
def test_invalid_output_never_becomes_clean_evidence(broken: str) -> None:
    raw = output()
    if broken == "nan":
        raw[0, 0, 0] = np.nan
    elif broken == "range":
        raw[0, 4, 0] = 1.1
    elif broken == "width":
        raw[0, 2, 0] = -1
    elif broken == "empty_box":
        raw[0, 2, 0] = 0
    elif broken == "shape":
        raw = raw.transpose(0, 2, 1)
    elif broken == "dtype":
        raw = raw.astype(np.int32)
    elif broken == "float64":
        raw = raw.astype(np.float64)
    else:
        raw = np.zeros((1, 6, 40001), dtype=np.float32)
    with pytest.raises(InvalidModelOutput):
        decode_proposals(raw, settings())


@pytest.fixture
def artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OnnxDetectorSettings:
    huggingface_hub = pytest.importorskip("huggingface_hub")

    tensor = onnx.numpy_helper.from_array(output(), name="evidence")
    graph = onnx.helper.make_graph(
        [onnx.helper.make_node("Constant", [], ["proposals"], value=tensor)],
        "fixture",
        [
            onnx.helper.make_tensor_value_info(
                "images", onnx.TensorProto.FLOAT, [1, 3, 32, 32]
            )
        ],
        [
            onnx.helper.make_tensor_value_info(
                "proposals", onnx.TensorProto.FLOAT, [1, 6, 3]
            )
        ],
    )
    model = onnx.helper.make_model(
        graph, opset_imports=[onnx.helper.make_opsetid("", 13)]
    )
    model.ir_version = 8
    entry = model.metadata_props.add()
    entry.key, entry.value = "names", "{0: 'a', 1: 'b'}"
    content = model.SerializeToString()
    path = tmp_path / "model.onnx"
    path.write_bytes(content)

    def cached(repo: str, filename: str, **kwargs: Any) -> str:
        assert repo == "example/detector" and filename == "model.onnx"
        assert kwargs["revision"] == "a" * 40
        assert kwargs["local_files_only"] is True
        return str(path)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", cached)
    return settings(sha256=hashlib.sha256(content).hexdigest())


def test_real_onnx_runtime_loads_warms_and_scores(
    artifact: OnnxDetectorSettings,
) -> None:
    adapter = OnnxDetector(artifact, 1)
    with Image.new("RGB", (16, 32), "white") as image:
        prepared = adapter.prepare_image(image)
        assert prepared.inputs["images"].shape == (1, 3, 32, 32)
        assert [f.score for f in adapter.predict(prepared)] == pytest.approx([0.8, 0.9])
    assert adapter._session.get_providers() == ["CPUExecutionProvider"]


def test_digest_and_native_label_order_are_checked_before_serving(
    artifact: OnnxDetectorSettings,
) -> None:
    with pytest.raises(ValueError, match="digest"):
        OnnxDetector(artifact.model_copy(update={"sha256": "0" * 64}), 1)
    with pytest.raises(ValueError, match="label order"):
        OnnxDetector(artifact.model_copy(update={"labels": ("b", "a")}), 1)


def test_external_tensors_are_rejected_even_inside_node_attributes() -> None:
    tensor = onnx.TensorProto()
    tensor.name = "external"
    tensor.data_location = onnx.TensorProto.EXTERNAL
    tensor.external_data.add(key="location", value="elsewhere.bin")
    node = onnx.helper.make_node("Constant", [], ["x"], value=tensor)
    with pytest.raises(ValueError, match="own tensor data"):
        reject_external_data(node)


@pytest.mark.parametrize("order", ["rgb", "bgr"])
def test_preprocessing_channel_order_and_padding_are_explicit(order: str) -> None:
    adapter = object.__new__(OnnxDetector)
    adapter.settings = settings(channel_order=order)
    adapter._np, adapter._cv, adapter._input_name = np, cv, "images"
    with Image.new("RGB", (32, 16), (255, 128, 0)) as image:
        blob = adapter.prepare_image(image).inputs["images"]
        expected = [1, 128 / 255, 0] if order == "rgb" else [0, 128 / 255, 1]
        assert blob[0, :, 0, 0] == pytest.approx(expected)
        assert not blob[0, :, 16:, :].any()
        assert image.getpixel((0, 0)) == (255, 128, 0)


def test_long_thin_image_cannot_cause_unbounded_square_padding() -> None:
    adapter = object.__new__(OnnxDetector)
    with (
        Image.new("RGB", (4097, 1), "white") as image,
        pytest.raises(APIError) as failure,
    ):
        adapter.prepare_image(image)
    assert failure.value.status_code == 413


def test_onnx_and_second_model_share_api_policy_without_torch(
    artifact: OnnxDetectorSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_headers: dict[str, str],
    image_bytes: bytes,
) -> None:
    import json

    from fastapi.testclient import TestClient

    from modera import engine
    from modera.config import Settings
    from modera.inference import PreparedImage
    from modera.main import create_app
    from modera.model_config import ImageModelSettings, ImageSettings, TextModelSettings
    from modera.results import Finding

    class ClassifierFixture:
        categories = frozenset({"classify.flag"})

        def predict(self, content: str | bytes | PreparedImage) -> list[Finding]:
            return [Finding(category="classify.flag", score=0.7)]

    def factory(selection: TextModelSettings | ImageSettings, threads: int) -> Any:
        if isinstance(selection, OnnxDetectorSettings):
            return OnnxDetector(selection, threads)
        return ClassifierFixture()

    monkeypatch.setattr(engine, "create_adapter", factory)
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "policies": [
                    {
                        "id": "default",
                        "version": "1",
                        "image_composites": [
                            {
                                "id": "both",
                                "action": "review",
                                "all_of": [
                                    {"category": "detect.a", "min": 0.7},
                                    {"category": "classify.flag", "min": 0.6},
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )
    service = Settings(
        policy_file=policy,
        image_models=(
            artifact,
            ImageModelSettings(
                id="example/classifier",
                revision="b" * 40,
                label_map={"flag": "classify.flag"},
            ),
        ),
    )
    with TestClient(create_app(service), headers=auth_headers) as client:
        response = client.post(
            "/v1/moderations", files={"file": ("photo.png", image_bytes)}
        )
        assert response.status_code == 200
        result = response.json()
        assert result["decision"]["action"] == "review"
        assert result["decision"]["matched_rules"] == ["both"]
        assert {f["model"] for f in result["findings"]} == {
            "example/detector",
            "example/classifier",
        }
        assert len(result["models"]) == 2


def test_artifact_size_limit_is_enforced_before_graph_loading(
    artifact: OnnxDetectorSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modera import onnx_detector

    monkeypatch.setattr(onnx_detector, "MAX_MODEL_BYTES", 16)
    with pytest.raises(ValueError, match="size or digest"):
        OnnxDetector(artifact, 1)


def test_loader_rejects_external_tensors_before_runtime_initialization(
    artifact: OnnxDetectorSettings,
    tmp_path: Path,
) -> None:
    path = tmp_path / "model.onnx"
    model = onnx.load_model_from_string(path.read_bytes())
    tensor = model.graph.node[0].attribute[0].t
    tensor.ClearField("raw_data")
    tensor.data_location = onnx.TensorProto.EXTERNAL
    tensor.external_data.add(key="location", value="must-not-open.bin")
    content = model.SerializeToString()
    path.write_bytes(content)
    selection = artifact.model_copy(
        update={"sha256": hashlib.sha256(content).hexdigest()}
    )
    with pytest.raises(ValueError, match="own tensor data"):
        OnnxDetector(selection, 1)
