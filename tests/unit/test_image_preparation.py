from collections.abc import Callable

import pytest
from PIL import ExifTags, Image

from modera.inference import Classifier
from modera.model_config import ImageModelSettings


def adapter(processor: Callable[..., object]) -> Classifier:
    classifier = object.__new__(Classifier)
    classifier.settings = ImageModelSettings(
        id="example/image", revision="a" * 40, label_map={"nsfw": "sexual"}
    )
    classifier._processor = processor
    return classifier


def test_rgb_pixels_are_borrowed_without_closing_the_callers_image() -> None:
    with Image.new("RGB", (2, 1), "red") as image:

        def processor(images: Image.Image, return_tensors: str) -> dict[str, str]:
            assert images is image
            assert return_tensors == "pt"
            return {"pixel_values": "prepared"}

        assert adapter(processor).prepare_image(image).inputs == {
            "pixel_values": "prepared"
        }
        assert image.getpixel((0, 0)) == (255, 0, 0)


def test_exif_rotation_is_applied_and_the_temporary_image_is_closed() -> None:
    captured: list[Image.Image] = []
    with Image.new("RGB", (2, 1), "red") as image:
        image.putpixel((1, 0), (0, 0, 255))
        image.getexif()[ExifTags.Base.Orientation] = 6

        def processor(images: Image.Image, return_tensors: str) -> dict[str, str]:
            captured.append(images)
            assert images.size == (1, 2)
            assert images.getpixel((0, 0)) == (255, 0, 0)
            assert images.getpixel((0, 1)) == (0, 0, 255)
            return {"pixel_values": "prepared"}

        adapter(processor).prepare_image(image)
        assert image.size == (2, 1)
    with pytest.raises(ValueError, match="closed"):
        captured[0].getpixel((0, 0))


def test_rgba_conversion_preserves_existing_rgb_semantics() -> None:
    captured: list[Image.Image] = []
    with Image.new("RGBA", (1, 1), (255, 0, 0, 0)) as image:

        def processor(images: Image.Image, return_tensors: str) -> dict[str, str]:
            captured.append(images)
            assert images.mode == "RGB"
            assert images.getpixel((0, 0)) == (255, 0, 0)
            return {"pixel_values": "prepared"}

        adapter(processor).prepare_image(image)
        assert image.getpixel((0, 0)) == (255, 0, 0, 0)
    with pytest.raises(ValueError, match="closed"):
        captured[0].getpixel((0, 0))
