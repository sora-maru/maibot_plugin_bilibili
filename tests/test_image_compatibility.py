"""Image behavior used by the plugin, including Pillow 13 PNG mode handling."""
import base64
import io

import pytest
from PIL import Image

from maibot_plugin_bilibili.core.utils import create_qrcode, is_height_valid
from maibot_plugin_bilibili.services.logo_service import LogoService
from maibot_plugin_bilibili.services.renderer import Renderer


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "P", "I;16"])
def test_logo_modes_convert_to_valid_png(tmp_path, mode):
    source = tmp_path / "source.png"
    result = tmp_path / "logo.png"
    Image.new(mode, (3000, 1000)).save(source, format="PNG")

    LogoService._convert_to_png(source, result)

    with Image.open(result) as image:
        assert image.format == "PNG"
        assert image.mode in {"RGB", "RGBA"}
        assert image.size == (2048, 683)
        image.verify()
    assert Renderer._validate_image(str(result))
    assert is_height_valid(str(result))


def test_jpeg_exif_orientation_applied_before_resize(tmp_path):
    source = tmp_path / "source.jpg"
    result = tmp_path / "logo.png"
    image = Image.new("RGB", (80, 40), "pink")
    exif = image.getexif()
    exif[274] = 6  # 90 degrees clockwise
    image.save(source, format="JPEG", exif=exif)

    LogoService._convert_to_png(source, result)

    with Image.open(result) as converted:
        assert converted.size == (40, 80)
        assert converted.getexif().get(274) is None


def test_webp_logo_can_be_normalized(tmp_path):
    source = tmp_path / "source.webp"
    result = tmp_path / "logo.png"
    Image.new("RGBA", (100, 50), (255, 120, 150, 128)).save(source, format="WEBP")

    LogoService._convert_to_png(source, result)

    with Image.open(result) as converted:
        assert converted.format == "PNG"
        assert converted.size == (100, 50)
        assert converted.mode == "RGBA"


def test_qrcode_data_uri_decodes_as_png():
    data_uri = create_qrcode("https://www.bilibili.com/video/BV1xx411c7mD")
    assert data_uri.startswith("data:image/png;base64,")
    data = base64.b64decode(data_uri.split(",", 1)[1], validate=True)
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG"
        assert image.width == image.height > 0
        image.verify()


def test_very_tall_valid_image_uses_file_delivery(tmp_path):
    path = tmp_path / "long-card.png"
    Image.new("RGB", (10, 25001), "white").save(path, format="PNG")
    assert Renderer._validate_image(str(path))
    assert not is_height_valid(str(path))


def test_corrupt_image_is_rejected(tmp_path):
    source = tmp_path / "broken.png"
    source.write_bytes(b"this is not an image")
    assert not Renderer._validate_image(str(source))
    assert not is_height_valid(str(source))
    with pytest.raises(ValueError, match="无法识别图片"):
        LogoService._convert_to_png(source, tmp_path / "logo.png")
