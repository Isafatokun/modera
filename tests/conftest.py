import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

TEST_API_KEY = "test-only-key-for-modera-not-a-deployment-secret"


@pytest.fixture(autouse=True)
def configure_test_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODERA_API_KEY", TEST_API_KEY)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


@pytest.fixture
def image_bytes() -> bytes:
    with BytesIO() as buffer, Image.new("RGB", (2, 2), "white") as image:
        image.save(buffer, format="PNG")
        return buffer.getvalue()


@pytest.fixture
def image_deployment(tmp_path: Path) -> tuple[Path, Path]:
    pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    revision = "a" * 40
    snapshot = tmp_path / "models--example--image" / "snapshots" / revision
    snapshot.mkdir(parents=True)
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
        snapshot, safe_serialization=True
    )
    transformers.ViTImageProcessor(size={"height": 16, "width": 16}).save_pretrained(
        snapshot
    )
    selection = {
        "image": {
            "id": "example/image",
            "revision": revision,
            "cache_dir": ".",
            "label_map": {"nsfw": "sexual"},
        }
    }
    model_file = tmp_path / "models.json"
    model_file.write_text(json.dumps(selection))
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        '{"policies":[{"id":"default","version":"1",'
        '"image":[{"category":"sexual","block_threshold":0.8}]}]}'
    )
    return model_file, policy_file
