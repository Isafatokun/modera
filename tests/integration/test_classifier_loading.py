from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from modera.config import Settings
from modera.errors import APIError
from modera.inference import Classifier
from modera.main import create_app
from modera.model_config import ImageModelSettings, TextModelSettings

REVISION = "a" * 40


def snapshot(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"models--example--{name}" / "snapshots" / REVISION
    path.mkdir(parents=True)
    return path


@pytest.mark.inference
def test_real_text_loader_rejects_silent_truncation_and_bad_label_maps(
    tmp_path: Path,
) -> None:
    pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    tokenizers = pytest.importorskip("tokenizers")
    path = snapshot(tmp_path, "text")
    vocab = {"[PAD]": 0, "[UNK]": 1, "[CLS]": 2, "[SEP]": 3, "hello": 4, "warmup": 5}
    raw = tokenizers.Tokenizer(
        tokenizers.models.WordPiece(vocab=vocab, unk_token="[UNK]")
    )
    raw.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    raw.post_processor = tokenizers.processors.TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 2), ("[SEP]", 3)]
    )
    tokenizer = transformers.PreTrainedTokenizerFast(
        tokenizer_object=raw, unk_token="[UNK]", pad_token="[PAD]", model_max_length=32
    )
    tokenizer.save_pretrained(path)
    config = transformers.BertConfig(
        vocab_size=len(vocab),
        hidden_size=16,
        num_hidden_layers=1,
        num_attention_heads=2,
        intermediate_size=32,
        max_position_embeddings=32,
        num_labels=2,
        id2label={0: "toxic", 1: "threat"},
        label2id={"toxic": 0, "threat": 1},
    )
    transformers.BertForSequenceClassification(config).save_pretrained(
        path, safe_serialization=True
    )
    settings = TextModelSettings(
        id="example/text",
        revision=REVISION,
        cache_dir=tmp_path,
        label_map={"toxic": "toxicity", "threat": "threat"},
        max_tokens=8,
    )
    adapter = Classifier(settings, 1)
    result = adapter.predict("hello")
    assert {x.category for x in result} == {"toxicity", "threat"}
    assert all(0 <= x.score <= 1 for x in result)
    with pytest.raises(APIError) as error:
        adapter.predict("hello " * 20)
    assert error.value.code == "input_too_long"
    with pytest.raises(ValueError, match="label mappings"):
        Classifier(
            settings.model_copy(update={"label_map": {"missing": "toxicity"}}), 1
        )
    with pytest.raises(OSError):
        Classifier(settings.model_copy(update={"revision": "b" * 40}), 1)


@pytest.mark.inference
def test_real_photo_loader_and_safe_weights_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth_headers: dict[str, str]
) -> None:
    pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    path = snapshot(tmp_path, "image")
    config = transformers.ViTConfig(
        image_size=16,
        patch_size=8,
        hidden_size=16,
        num_hidden_layers=1,
        num_attention_heads=2,
        intermediate_size=32,
        num_labels=2,
        id2label={0: "sfw", 1: "nsfw"},
        label2id={"sfw": 0, "nsfw": 1},
    )
    transformers.ViTForImageClassification(config).save_pretrained(
        path, safe_serialization=True
    )
    transformers.ViTImageProcessor(size={"height": 16, "width": 16}).save_pretrained(
        path
    )
    settings = ImageModelSettings(
        id="example/image",
        revision=REVISION,
        cache_dir=tmp_path,
        label_map={"nsfw": "sexual"},
    )
    original = transformers.AutoModelForImageClassification.from_pretrained
    calls: list[dict[str, Any]] = []

    def loader(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        transformers.AutoModelForImageClassification, "from_pretrained", loader
    )
    adapter = Classifier(settings, 1)
    with BytesIO() as buffer, Image.new("RGB", (16, 16), "white") as image:
        image.save(buffer, format="PNG")
        result = adapter.predict(buffer.getvalue())
    assert result[0].category == "sexual"
    assert 0 <= result[0].score <= 1
    assert calls[0]["local_files_only"] is True
    assert calls[0]["trust_remote_code"] is False
    assert calls[0]["use_safetensors"] is True
    policy = tmp_path / "policy.json"
    policy.write_text(
        '{"policies":[{"id":"default","version":"1",'
        '"image":[{"category":"sexual","block_threshold":0.8}]}]}'
    )
    with TestClient(
        create_app(Settings(image_model=settings, policy_file=policy)),
        headers=auth_headers,
    ) as client:
        opens: list[object] = []
        original_open = Image.open

        def counted_open(*args: Any, **kwargs: Any) -> Image.Image:
            opens.append(args[0])
            return original_open(*args, **kwargs)

        monkeypatch.setattr(Image, "open", counted_open)
        with BytesIO() as buffer, Image.new("RGB", (16, 16), "white") as image:
            image.save(buffer, format="PNG")
            response = client.post(
                "/v1/moderations", files={"file": ("photo.png", buffer.getvalue())}
            )
        assert response.status_code == 200
        assert len(opens) == 2
        assert response.json()["models"][0]["revision"] == REVISION
