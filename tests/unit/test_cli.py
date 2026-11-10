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


def prepared_demo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drop: tuple[str, ...] = ()
) -> None:
    directory = tmp_path / "demo"
    assert main(["init", str(directory)]) == 0
    monkeypatch.chdir(directory)
    monkeypatch.delenv("MODERA_API_KEY", raising=False)
    env = directory / ".env"
    kept = [
        line
        for line in env.read_text().splitlines()
        if not line.startswith(drop) or not drop
    ]
    env.write_text("\n".join(kept) + "\n")


@pytest.mark.parametrize(
    "case,message",
    [
        ("missing_env", "--env-file does not name a readable file."),
        ("bad_key", "api_key is missing or invalid."),
        ("unknown_field", "configuration is missing or invalid."),
        (
            "bad_policy",
            "policy_file could not be loaded; check its path and policy fields.",
        ),
        (
            "bad_rules",
            "text_rules_file could not be loaded; check its path and fields.",
        ),
        ("no_policy", "policy_file must select at least one policy for this setup."),
        ("missing_category", "policy_file requires a category absent from the checks."),
        ("no_checks", "configure a text model, image model or text_rules_file."),
        (
            "bad_fallback",
            "check text_fallback, text_fallback_policy and text_rules_file.",
        ),
    ],
)
def test_doctor_names_the_first_problem_it_finds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    message: str,
) -> None:
    drop = {
        "no_policy": ("MODERA_POLICY_FILE",),
        "no_checks": ("MODERA_TEXT_RULES_FILE",),
    }.get(case, ())
    prepared_demo(tmp_path, monkeypatch, drop)
    capsys.readouterr()
    env = Path(".env")
    if case == "missing_env":
        env.unlink()
    elif case == "bad_key":
        key = Settings(_env_file=env).api_key.get_secret_value()
        env.write_text(env.read_text().replace(key, "short"))
    elif case == "unknown_field":
        env.write_text(env.read_text() + "SOMETHING_ELSE=1\n")
    elif case == "bad_policy":
        Path("policies.json").write_text("invalid")
    elif case == "bad_rules":
        Path("text-rules.json").write_text("invalid")
    elif case == "missing_category":
        policy = json.loads(Path("policies.json").read_text())
        policy["policies"][0]["text"][0]["category"] = "rules.missing"
        Path("policies.json").write_text(json.dumps(policy))
    elif case == "bad_fallback":
        env.write_text(
            env.read_text()
            + "MODERA_TEXT_FALLBACK=rules\nMODERA_TEXT_FALLBACK_POLICY=absent\n"
        )
    assert main(["doctor", "--env-file", ".env"]) == 1
    assert capsys.readouterr().out.splitlines() == [f"ERROR: {message}"]


def test_doctor_reports_success_and_its_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared_demo(tmp_path, monkeypatch)
    capsys.readouterr()
    assert main(["doctor", "--env-file", ".env"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "OK: configuration and policy categories checked.",
        "Not checked: weights, quality, memory, container permissions or running API.",
    ]


def test_doctor_prints_one_line_per_configuration_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared_demo(tmp_path, monkeypatch)
    capsys.readouterr()
    env = Path(".env")
    key = Settings(_env_file=env).api_key.get_secret_value()
    env.write_text(
        env.read_text().replace(key, "short") + "MODERA_INFERENCE_THREADS=0\n"
    )
    assert main(["doctor", "--env-file", ".env"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert sorted(lines) == [
        "ERROR: api_key is missing or invalid.",
        "ERROR: inference_threads is missing or invalid.",
    ]
