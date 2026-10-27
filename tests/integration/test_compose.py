import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = [
    pytest.mark.inference,
    pytest.mark.skipif(
        not os.environ.get("MODERA_TEST_CONTAINER_IMAGE"),
        reason="requires an explicitly selected inference-enabled Docker image",
    ),
]


def test_reference_compose_prepares_serves_and_restarts(
    tmp_path: Path, image_deployment: tuple[Path, Path], auth_headers: dict[str, str]
) -> None:
    transformers = pytest.importorskip("transformers")
    tokenizers = pytest.importorskip("tokenizers")
    revision = "a" * 40
    snapshot = tmp_path / "models--example--text" / "snapshots" / revision
    snapshot.mkdir(parents=True)
    vocab = {"[PAD]": 0, "[UNK]": 1, "hello": 2, "warmup": 3}
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.WordPiece(vocab=vocab, unk_token="[UNK]")
    )
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    transformers.PreTrainedTokenizerFast(
        tokenizer_object=tokenizer, unk_token="[UNK]", pad_token="[PAD]"
    ).save_pretrained(snapshot)
    transformers.BertForSequenceClassification(
        transformers.BertConfig(
            vocab_size=len(vocab),
            hidden_size=16,
            num_hidden_layers=1,
            num_attention_heads=2,
            intermediate_size=32,
            max_position_embeddings=32,
            num_labels=2,
            id2label={0: "toxicity", 1: "other"},
        )
    ).save_pretrained(snapshot, safe_serialization=True)
    model_file, policy_file = image_deployment
    models = json.loads(model_file.read_text())
    models["text"] = {
        "id": "example/text",
        "revision": revision,
        "max_tokens": 32,
        "label_map": {"toxicity": "toxicity"},
    }
    for selection in models.values():
        selection["cache_dir"] = "/var/cache/modera/hub"
    model_file.write_text(json.dumps(models))
    policy_file.write_text(
        json.dumps(
            {
                "policies": [
                    {
                        "id": "default",
                        "version": "test",
                        "text": [{"category": "toxicity", "block_threshold": 0.85}],
                        "image": [{"category": "sexual", "block_threshold": 0.95}],
                    }
                ]
            }
        )
    )
    tmp_path.chmod(0o755)
    for path in tmp_path.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    override = tmp_path / "compose.json"
    override.write_text(
        json.dumps(
            {
                "services": {
                    "prepare": {
                        "volumes": [
                            {
                                "type": "bind",
                                "source": str(tmp_path),
                                "target": "/fixtures",
                                "read_only": True,
                            }
                        ],
                        "command": [
                            "python",
                            "-c",
                            "import shutil; from pathlib import Path; "
                            "from modera.model_config import load_models; "
                            "from modera.prepare_models import prepare_models; "
                            "[shutil.copytree(p,Path('/var/cache/modera/hub')/p.name,"
                            "dirs_exist_ok=True) "
                            "for p in Path('/fixtures').glob('models--*')]; "
                            "prepare_models(load_models(Path('/etc/modera/models.json')),threads=1,offline=True)",
                        ],
                    }
                }
            }
        )
    )
    root = Path(__file__).resolve().parents[2]
    live_sources = os.environ.get("MODERA_TEST_LIVE_SOURCES") == "1"
    environment = dict(os.environ)
    environment.update(
        MODERA_IMAGE=os.environ["MODERA_TEST_CONTAINER_IMAGE"],
        MODERA_API_KEY=auth_headers["Authorization"][7:],
        MODERA_MODELS_FILE=str(model_file),
        MODERA_POLICIES_FILE=str(policy_file),
        MODERA_PORT="0",
        MODERA_IMAGE_ALLOWED_HOSTS='["httpbingo.org"]' if live_sources else "[]",
        MODERA_CHECK_IMAGE_URL="https://httpbingo.org/image/png"
        if live_sources
        else "",
        MODERA_CHECK_UNAVAILABLE_URL="https://httpbingo.org/status/503"
        if live_sources
        else "",
    )
    command = [
        "docker",
        "compose",
        "--project-name",
        f"modera-test-{uuid4().hex}",
        "--file",
        str(root / "compose.yaml"),
        "--file",
        str(override),
    ]

    def compose(*args: str) -> str:
        result = subprocess.run(
            command + list(args),
            env=environment,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    try:
        compose("up", "--no-build", "--wait", "--wait-timeout", "120")
        for attempt in range(2):
            if attempt:
                compose("restart", "modera")
            output = compose("--profile", "checks", "run", "--rm", "--no-deps", "check")
            assert "text: passed" in output and "photo file: passed" in output
            if live_sources:
                assert "photo URL: passed" in output
                assert (
                    "unavailable source: retryable error without a decision" in output
                )
            else:
                assert "photo URL: skipped" in output
    except BaseException:
        print(compose("logs", "--no-color"))
        raise
    finally:
        compose("down", "--volumes", "--remove-orphans")
