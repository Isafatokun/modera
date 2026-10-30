import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from modera import prepare_models as preparation
from modera.model_config import ImageModelSettings, ModelConfig, TextModelSettings


@pytest.mark.parametrize("offline", [False, True])
def test_preparation_pins_downloads_and_validates_before_reporting_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], offline: bool
) -> None:
    calls: list[tuple[str, Any]] = []

    def download(repo: str, **kwargs: Any) -> None:
        calls.append(("download", kwargs))
        assert repo == "example/image"

    def classifier(selection: Any, threads: int) -> None:
        calls.append(("load", threads))
        assert selection.revision == "a" * 40

    monkeypatch.setattr(
        preparation,
        "importlib",
        SimpleNamespace(
            import_module=lambda name: SimpleNamespace(snapshot_download=download)
        ),
    )
    monkeypatch.setattr(preparation, "create_adapter", classifier)
    monkeypatch.delenv("MODERA_API_KEY")
    preparation.prepare_models(
        ModelConfig(
            image=ImageModelSettings(
                id="example/image", revision="a" * 40, label_map={"nsfw": "sexual"}
            )
        ),
        threads=2,
        offline=offline,
    )
    assert [name for name, _ in calls] == ["download", "load"]
    assert calls[0][1]["revision"] == "a" * 40
    assert calls[0][1]["local_files_only"] is offline
    assert calls[0][1]["allow_patterns"] == [
        "*.json",
        "*.safetensors",
        "*.txt",
        "*.model",
    ]
    assert json.loads(capsys.readouterr().out)["ready"]


@pytest.mark.parametrize("stage", ["download", "load"])
def test_cli_reports_failure_without_raw_exception_or_false_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stage: str,
) -> None:
    config = tmp_path / "models.json"
    config.write_text(
        ModelConfig(
            text=TextModelSettings(
                id="example/text", revision="a" * 40, label_map={"toxic": "toxicity"}
            )
        ).model_dump_json()
    )
    monkeypatch.setattr(sys, "argv", ["prepare", "--config", str(config)])

    def download(*args: Any, **kwargs: Any) -> None:
        if stage == "download":
            raise OSError("private token or source URL")

    def classifier(*args: Any, **kwargs: Any) -> None:
        raise ValueError("private token or source URL")

    monkeypatch.setattr(
        preparation,
        "importlib",
        SimpleNamespace(
            import_module=lambda name: SimpleNamespace(snapshot_download=download)
        ),
    )
    monkeypatch.setattr(preparation, "create_adapter", classifier)
    with pytest.raises(SystemExit) as error:
        preparation.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert not output.out
    assert ("OSError" if stage == "download" else "ValueError") in output.err
    assert "private token" not in output.err


def test_cli_validates_configuration_before_any_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "models.json"
    config.write_text("{}")
    monkeypatch.setattr(sys, "argv", ["prepare", "--config", str(config)])

    def unexpected(name: str) -> None:
        raise AssertionError("must not load a downloader for invalid configuration")

    monkeypatch.setattr(
        preparation, "importlib", SimpleNamespace(import_module=unexpected)
    )
    with pytest.raises(SystemExit) as error:
        preparation.main()
    assert error.value.code == 2
