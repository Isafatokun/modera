import asyncio
import importlib
import os
from collections.abc import Callable
from contextlib import ExitStack, closing
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from PIL import ExifTags, Image, ImageOps

from modera.errors import APIError
from modera.images import decode_image
from modera.model_config import ImageModelSettings, TextModelSettings
from modera.results import Finding
from modera.workers import BoundedWorker


@dataclass(frozen=True, slots=True, repr=False)
class PreparedImage:
    inputs: dict[str, Any]


class Classifier:
    def __init__(
        self, settings: TextModelSettings | ImageModelSettings, threads: int
    ) -> None:
        settings = settings.model_copy(deep=True)
        if not settings.label_map:
            raise ValueError("configured models require explicit label mappings")
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["ORT_DISABLE_TELEMETRY"] = "1"
        self._torch = importlib.import_module("torch")
        transformers = importlib.import_module("transformers")
        self._torch.set_num_threads(threads)
        self.settings = settings
        self.categories = frozenset(settings.label_map.values())
        self._label_map = dict(settings.label_map)
        options = {
            "revision": settings.revision,
            "cache_dir": str(settings.cache_dir) if settings.cache_dir else None,
            "local_files_only": True,
            "trust_remote_code": False,
        }
        is_text = isinstance(settings, TextModelSettings)
        loader = (
            transformers.AutoModelForSequenceClassification
            if is_text
            else transformers.AutoModelForImageClassification
        )
        self._model: Any = loader.from_pretrained(
            settings.id,
            **options,
            use_safetensors=True,
            dtype=self._torch.float32,
            output_loading_info=True,
        )
        self._model, loading = self._model
        if any(
            loading.get(key)
            for key in (
                "missing_keys",
                "unexpected_keys",
                "mismatched_keys",
                "error_msgs",
            )
        ):
            raise ValueError("checkpoint does not match the classifier architecture")
        self._model.eval()
        labels = self._model.config.id2label
        if set(labels) != set(range(self._model.config.num_labels)) or not set(
            self._label_map
        ) <= set(labels.values()):
            raise ValueError("label mappings do not match the model outputs")
        if settings.activation == "softmax" and len(labels) < 2:
            raise ValueError("softmax requires at least two labels")
        self._indices = {label: index for index, label in labels.items()}
        if isinstance(settings, TextModelSettings):
            self._processor = transformers.AutoTokenizer.from_pretrained(
                settings.id, **options
            )
            self._max_tokens = min(
                settings.max_tokens,
                self._processor.model_max_length,
                getattr(
                    self._model.config, "max_position_embeddings", settings.max_tokens
                ),
            )
            self.predict("Warmup")
        else:
            self._processor = transformers.AutoImageProcessor.from_pretrained(
                settings.id, **options, backend="pil"
            )
            with BytesIO() as buffer, Image.new("RGB", (2, 2), "white") as image:
                image.save(buffer, format="PNG")
                self.predict(buffer.getvalue())

    def prepare_image(self, image: Image.Image) -> PreparedImage:
        if not isinstance(self.settings, ImageModelSettings):
            raise TypeError("image preparation requires an image classifier")
        with ExitStack() as owned:
            oriented = image
            if image.getexif().get(ExifTags.Base.Orientation) in range(2, 9):
                oriented = owned.enter_context(closing(ImageOps.exif_transpose(image)))
            rgb = oriented
            if oriented.mode != "RGB":
                rgb = owned.enter_context(closing(oriented.convert("RGB")))
            return PreparedImage(dict(self._processor(images=rgb, return_tensors="pt")))

    def predict(self, content: str | bytes | PreparedImage) -> list[Finding]:
        if isinstance(self.settings, TextModelSettings):
            if not isinstance(content, str):
                raise TypeError("text classifier requires text")
            inputs = self._processor(content, return_tensors="pt", truncation=False)
            if inputs["input_ids"].shape[1] > self._max_tokens:
                raise APIError(
                    422,
                    "input_too_long",
                    "Text exceeds the configured model token limit.",
                    param="input.text",
                )
        else:
            if isinstance(content, bytes):
                with closing(decode_image(content)) as image:
                    content = self.prepare_image(image)
            if not isinstance(content, PreparedImage):
                raise TypeError("image classifier requires image bytes")
            inputs = content.inputs
        with self._torch.inference_mode():
            logits = self._model(**inputs).logits
            if (
                logits.shape != (1, len(self._indices))
                or not self._torch.isfinite(logits).all().item()
            ):
                raise ValueError("invalid classifier output")
            scores = (
                logits.sigmoid()
                if self.settings.activation == "sigmoid"
                else logits.softmax(-1)
            )
            values = scores[0].tolist()
        return [
            Finding(category=category, score=values[self._indices[label]])
            for label, category in self._label_map.items()
        ]


class InferenceWorker:
    def __init__(
        self, timeout: float, queue_size: int = 0, wait_seconds: float = 2
    ) -> None:
        self._worker = BoundedWorker("modera-inference", queue_size, wait_seconds)
        self._timeout = timeout

    async def initialize[T](self, operation: Callable[[], T]) -> T:
        return await self._worker.run(operation)

    async def run(self, operation: Callable[[], list[Finding]]) -> list[Finding]:
        try:
            async with asyncio.timeout(self._timeout):
                return await self._worker.run(operation)
        except APIError:
            raise
        except Exception as exc:
            raise APIError(
                503,
                "check_unavailable",
                "Model evaluation did not complete.",
                retryable=True,
                headers={"Retry-After": "1"},
            ) from exc

    def close(self) -> None:
        self._worker.close()
