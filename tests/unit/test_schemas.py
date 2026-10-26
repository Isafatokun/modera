import pytest
from pydantic import ValidationError

from modera.schemas import MAX_TEXT_CHARACTERS, ModerationRequest


def test_text_preserves_original_content() -> None:
    request = ModerationRequest.model_validate(
        {"input": {"type": "text", "text": " hello "}}
    )
    assert request.input.type == "text"
    assert request.input.text == " hello "
    assert request.policy == "default"


@pytest.mark.parametrize("length", [1, MAX_TEXT_CHARACTERS])
def test_text_length_boundaries(length: int) -> None:
    ModerationRequest.model_validate({"input": {"type": "text", "text": "x" * length}})


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"input": []},
        {"input": {"text": "hello"}},
        {"input": {"type": "audio", "url": "https://example.com/audio.mp3"}},
        {"input": {"type": "text", "text": ""}},
        {"input": {"type": "text", "text": " \n\t"}},
        {"input": {"type": "text", "text": "x" * (MAX_TEXT_CHARACTERS + 1)}},
        {"input": {"type": "text", "text": 42}},
        {"input": {"type": "text", "text": "hello", "source": {"type": "url"}}},
        {"input": {"type": "image"}},
        {"input": {"type": "image", "source": {"type": "base64", "data": "abc"}}},
        {"input": {"type": "text", "text": "hello"}, "unknown": True},
        {"input": {"type": "text", "text": "hello"}, "policy": ""},
        {"input": {"type": "text", "text": "hello"}, "policy": "invalid policy"},
        {"input": {"type": "text", "text": "hello"}, "policy": "x" * 65},
    ],
)
def test_invalid_fields_are_rejected(payload: object) -> None:
    with pytest.raises(ValidationError):
        ModerationRequest.model_validate(payload)


@pytest.mark.parametrize(
    "url",
    [
        "not a URL",
        "http://example.com/photo.jpg",
        "file:///tmp/photo.jpg",
        "https://user:secret@example.com/photo.jpg",
        "https://example.com/photo.jpg#fragment",
        "https://example.com/" + "x" * 2048,
    ],
)
def test_invalid_image_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        ModerationRequest.model_validate(
            {"input": {"type": "image", "source": {"type": "url", "url": url}}}
        )


def test_image_url_and_named_policy() -> None:
    request = ModerationRequest.model_validate(
        {
            "input": {
                "type": "image",
                "source": {
                    "type": "url",
                    "url": "https://media.example.com/photo.jpg?token=abc",
                },
            },
            "policy": "community_v1",
        }
    )
    assert request.input.type == "image"
    assert request.input.source.url.host == "media.example.com"
    assert request.policy == "community_v1"
