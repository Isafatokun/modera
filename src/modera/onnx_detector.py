"""Fixed-layout YOLOv8 proposal scoring with pinned, offline ONNX artifacts."""

import ast
import hashlib
import importlib
import json
import os
from contextlib import ExitStack, closing
from typing import Any

from PIL import ExifTags, Image, ImageOps

from modera.errors import APIError
from modera.images import decode_image
from modera.inference import InvalidModelOutput, PreparedImage
from modera.model_config import OnnxDetectorSettings
from modera.results import Finding

MAX_MODEL_BYTES = 256 * 1024 * 1024
MAX_SOURCE_SIDE = 4096
MAX_PROPOSALS = 40_000


def reject_external_data(message: Any) -> None:
    """Reject external tensors, including tensors nested in graph attributes."""
    if message.DESCRIPTOR.full_name == "onnx.TensorProto":
        external = importlib.import_module("onnx").TensorProto.EXTERNAL
        if message.data_location == external or message.external_data:
            raise ValueError("ONNX artifacts must contain their own tensor data")
    for field, value in message.ListFields():
        if field.message_type is None:
            continue
        if field.is_repeated:
            for item in value:
                reject_external_data(item)
        else:
            reject_external_data(value)


def decode_proposals(output: Any, settings: OnnxDetectorSettings) -> list[Finding]:
    """Score winning proposal labels; class-wise suppression cannot change maxima."""
    np = importlib.import_module("numpy")
    data = np.asarray(output)
    if (
        data.ndim != 3
        or data.shape[:2] != (1, 4 + len(settings.labels))
        or data.shape[2] > MAX_PROPOSALS
        or data.dtype != np.float32
        or not np.isfinite(data).all()
    ):
        raise InvalidModelOutput("invalid ONNX proposal tensor")
    coordinates = data[0, :4]
    scores = data[0, 4:]
    if (scores < 0).any() or (scores > 1).any() or (coordinates[2:] < 0).any():
        raise InvalidModelOutput("invalid ONNX proposal values")
    winners = scores.argmax(axis=0)
    confidence = scores.max(axis=0)
    if (((coordinates[2] == 0) | (coordinates[3] == 0)) & (confidence > 0)).any():
        raise InvalidModelOutput("nonzero evidence requires a nonempty box")
    maxima = {
        label: float(confidence[winners == index].max(initial=0))
        for index, label in enumerate(settings.labels)
        if label in settings.label_map
    }
    return [
        Finding(category=category, score=maxima[label])
        for label, category in settings.label_map.items()
    ]


class OnnxDetector:
    """CPU-only, batch-one float32 input and [1, 4 + classes, proposals] output."""

    def __init__(self, settings: OnnxDetectorSettings, threads: int) -> None:
        self.settings = settings.model_copy(deep=True)
        if not settings.label_map:
            raise ValueError("ONNX detectors require explicit category mappings")
        self.categories = frozenset(settings.label_map.values())
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["ORT_DISABLE_TELEMETRY"] = "1"
        hub = importlib.import_module("huggingface_hub")
        filename = hub.hf_hub_download(
            settings.id,
            settings.filename,
            revision=settings.revision,
            cache_dir=settings.cache_dir,
            local_files_only=True,
        )
        with open(filename, "rb") as source:
            content = source.read(MAX_MODEL_BYTES + 1)
        if (
            len(content) > MAX_MODEL_BYTES
            or hashlib.sha256(content).hexdigest() != settings.sha256
        ):
            raise ValueError(
                "ONNX artifact size or digest does not match configuration"
            )
        onnx = importlib.import_module("onnx")
        graph = onnx.load_model_from_string(content)
        reject_external_data(graph)
        onnx.checker.check_model(graph)
        del graph
        self._np = importlib.import_module("numpy")
        self._cv = importlib.import_module("cv2")
        runtime = importlib.import_module("onnxruntime")
        runtime.disable_telemetry_events()
        options = runtime.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.execution_mode = runtime.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 4
        self._session = runtime.InferenceSession(
            content,
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        inputs = self._session.get_inputs()
        outputs = self._session.get_outputs()
        expected = [1, 3, settings.input_size, settings.input_size]
        if (
            len(inputs) != 1
            or inputs[0].type != "tensor(float)"
            or len(inputs[0].shape) != 4
            or any(
                isinstance(actual, int) and actual != wanted
                for actual, wanted in zip(inputs[0].shape, expected, strict=True)
            )
        ):
            raise ValueError("ONNX detector input shape does not match configuration")
        if len(outputs) != 1 or outputs[0].type != "tensor(float)":
            raise ValueError("ONNX detector requires one float32 proposal output")
        shape = outputs[0].shape
        if (
            len(shape) != 3
            or (isinstance(shape[0], int) and shape[0] != 1)
            or (isinstance(shape[1], int) and shape[1] != len(settings.labels) + 4)
            or (isinstance(shape[2], int) and shape[2] > MAX_PROPOSALS)
        ):
            raise ValueError("ONNX detector output layout does not match configuration")
        self.artifact_metadata = dict(self._session.get_modelmeta().custom_metadata_map)
        names = self.artifact_metadata.get("names")
        if names is not None:
            if len(names) > 64 * 1024:
                raise ValueError("ONNX label metadata is too large")
            try:
                ordered = json.loads(names)
            except json.JSONDecodeError:
                ordered = ast.literal_eval(names)
            if isinstance(ordered, dict):
                ordered = [
                    ordered.get(index, ordered.get(str(index)))
                    for index in range(len(ordered))
                ]
            if not isinstance(ordered, list) or tuple(ordered) != settings.labels:
                raise ValueError("ONNX label order does not match artifact metadata")
        self._input_name = inputs[0].name
        with Image.new("RGB", (32, 32), "white") as image:
            self.predict(self.prepare_image(image))

    def prepare_image(self, image: Image.Image) -> PreparedImage:
        # Padding a long, thin source to a square must not bypass pixel limits.
        if max(image.size) > MAX_SOURCE_SIDE:
            raise APIError(
                413,
                "image_too_large",
                "The detector accepts image sides up to 4096 pixels.",
                param="file",
            )
        with ExitStack() as owned:
            oriented = image
            if image.getexif().get(ExifTags.Base.Orientation) in range(2, 9):
                oriented = owned.enter_context(closing(ImageOps.exif_transpose(image)))
            rgb = oriented
            if oriented.mode != "RGB":
                rgb = owned.enter_context(closing(oriented.convert("RGB")))
            pixels = self._np.asarray(rgb)
            side = max(rgb.size)
            square = self._cv.copyMakeBorder(
                pixels,
                0,
                side - rgb.height,
                0,
                side - rgb.width,
                self._cv.BORDER_CONSTANT,
                value=(0, 0, 0),
            )
            blob = self._cv.dnn.blobFromImage(
                square,
                1 / 255.0,
                (self.settings.input_size, self.settings.input_size),
                swapRB=self.settings.channel_order == "bgr",
                crop=False,
            )
            return PreparedImage({self._input_name: blob})

    def predict(self, content: str | bytes | PreparedImage) -> list[Finding]:
        if isinstance(content, bytes):
            with closing(decode_image(content)) as image:
                content = self.prepare_image(image)
        if not isinstance(content, PreparedImage):
            raise TypeError("ONNX detector requires image bytes or prepared image")
        output = self._session.run(None, content.inputs)
        if len(output) != 1:
            raise InvalidModelOutput("ONNX detector returned multiple outputs")
        return decode_proposals(output[0], self.settings)
