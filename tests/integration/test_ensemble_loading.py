"""Run two tiny local architectures; this tests integration, not moderation quality."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from modera.config import Settings
from modera.main import create_app
from modera.model_config import load_models
from modera.prepare_models import prepare_models

pytestmark = pytest.mark.inference


def test_real_detector_and_classifier_share_one_request(
    tmp_path: Path,
    auth_headers: dict[str, str],
    image_bytes: bytes,
) -> None:
    pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    revision = "a" * 40
    detector_path = tmp_path / "models--example--detector" / "snapshots" / revision
    classifier_path = tmp_path / "models--example--classifier" / "snapshots" / revision
    detector_path.mkdir(parents=True)
    classifier_path.mkdir(parents=True)
    backbone = transformers.ResNetConfig(
        depths=[1, 1, 1, 1],
        hidden_sizes=[8, 16, 32, 64],
        embedding_size=8,
        out_features=["stage4"],
    )
    detector_config = transformers.DetrConfig(
        backbone_config=backbone,
        use_timm_backbone=False,
        use_pretrained_backbone=False,
        d_model=32,
        encoder_layers=1,
        decoder_layers=1,
        encoder_attention_heads=4,
        decoder_attention_heads=4,
        encoder_ffn_dim=64,
        decoder_ffn_dim=64,
        num_queries=4,
        num_labels=2,
        id2label={0: "a", 1: "b"},
        label2id={"a": 0, "b": 1},
    )
    transformers.DetrForObjectDetection(detector_config).save_pretrained(
        detector_path, safe_serialization=True
    )
    transformers.DetrImageProcessorPil(
        size={"shortest_edge": 32, "longest_edge": 32}
    ).save_pretrained(detector_path)
    classifier_config = transformers.ViTConfig(
        image_size=16,
        patch_size=8,
        hidden_size=16,
        num_hidden_layers=1,
        num_attention_heads=2,
        intermediate_size=32,
        num_labels=2,
        id2label={0: "safe", 1: "flag"},
        label2id={"safe": 0, "flag": 1},
    )
    transformers.ViTForImageClassification(classifier_config).save_pretrained(
        classifier_path, safe_serialization=True
    )
    transformers.ViTImageProcessor(size={"height": 16, "width": 16}).save_pretrained(
        classifier_path
    )
    models = tmp_path / "models.json"
    models.write_text(
        json.dumps(
            {
                "image_models": [
                    {
                        "id": "example/detector",
                        "revision": revision,
                        "cache_dir": ".",
                        "backend": "transformers_object_detection",
                        "label_map": {"a": "detect.a"},
                    },
                    {
                        "id": "example/classifier",
                        "revision": revision,
                        "cache_dir": ".",
                        "label_map": {"flag": "classify.flag"},
                    },
                ]
            }
        )
    )
    policies = tmp_path / "policies.json"
    policies.write_text(
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
                                    {"category": "detect.a", "min": 0.0},
                                    {"category": "classify.flag", "min": 0.0},
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )
    prepare_models(load_models(models), threads=1, offline=True)
    with TestClient(
        create_app(
            Settings(model_file=models, policy_file=policies, inference_threads=1)
        ),
        headers=auth_headers,
    ) as client:
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
        assert result["coverage"]["evaluated"] == ["detect.a", "classify.flag"]
        assert not result["fallback"]
