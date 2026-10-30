import json
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from modera.cli import main
from modera.config import Settings
from modera.main import create_app


def test_generated_setup_works_and_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "demo"
    assert main(["init", str(directory)]) == 0
    original = (directory / ".env").read_bytes()
    assert stat.S_IMODE((directory / ".env").stat().st_mode) == 0o600
    assert main(["init", str(directory)]) == 1
    assert (directory / ".env").read_bytes() == original
    monkeypatch.chdir(directory)
    assert main(["doctor", "--env-file", ".env"]) == 0
    # Existing environment variables deliberately take priority, just as in Uvicorn.
    monkeypatch.delenv("MODERA_API_KEY", raising=False)
    settings = Settings(_env_file=directory / ".env")
    key = settings.api_key.get_secret_value()
    assert key not in capsys.readouterr().out
    with TestClient(
        create_app(settings), headers={"Authorization": f"Bearer {key}"}
    ) as client:
        response = client.post(
            "/v1/moderations",
            json={
                "input": {"type": "text", "text": "example prohibited phrase"},
                "policy": "rules-example",
            },
        )
        assert response.status_code == 200
        assert response.json()["decision"]["action"] == "block"


@pytest.mark.parametrize(
    "broken", ["key", "policy", "rules", "category", "unknown", "env"]
)
def test_doctor_reports_broken_setup_without_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    broken: str,
) -> None:
    directory = tmp_path / "demo"
    assert main(["init", str(directory)]) == 0
    monkeypatch.chdir(directory)
    monkeypatch.delenv("MODERA_API_KEY", raising=False)
    env = directory / ".env"
    key = Settings(_env_file=env).api_key.get_secret_value()
    if broken == "key":
        env.write_text(env.read_text().replace(key, "private-short-key"))
    elif broken == "unknown":
        env.write_text(env.read_text() + "PRIVATE_SECRET_FIELD=private-value\n")
    elif broken == "env":
        env.unlink()
    elif broken == "category":
        policy = json.loads(Path("policies.json").read_text())
        policy["policies"][0]["text"][0]["category"] = "rules.missing"
        Path("policies.json").write_text(json.dumps(policy))
    else:
        Path("policies.json" if broken == "policy" else "text-rules.json").write_text(
            "invalid"
        )
    assert main(["doctor", "--env-file", ".env"]) == 1
    output = capsys.readouterr().out
    for secret in (key, "private-short-key", "private-value", "private_secret_field"):
        assert secret not in output.lower()
    assert "ERROR:" in output
