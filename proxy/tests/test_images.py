"""OpenAI multimodal requests: image_url parts -> files for the extension."""
import pytest

from nemapi.utils.tools_format import MAX_IMAGES, extract_images

PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def user_with(parts):
    return [{"role": "user", "content": parts}]


def test_image_url_part_is_extracted():
    msgs = user_with([
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": PNG}},
    ])
    assert extract_images(msgs) == [{"url": PNG}]


def test_filename_is_kept():
    msgs = user_with([{"type": "image_url", "image_url": {"url": PNG}, "filename": "bug.png"}])
    assert extract_images(msgs)[0]["name"] == "bug.png"


def test_plain_string_content_has_no_image():
    assert extract_images([{"role": "user", "content": "hello"}]) == []


def test_only_the_last_user_message_counts():
    msgs = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": PNG}}]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": [{"type": "text", "text": "next"}]},
    ]
    assert extract_images(msgs) == []


def test_remote_url_is_refused():
    msgs = user_with([{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}])
    with pytest.raises(ValueError):
        extract_images(msgs)


def test_too_many_images_is_refused():
    msgs = user_with([{"type": "image_url", "image_url": {"url": PNG}}] * (MAX_IMAGES + 1))
    with pytest.raises(ValueError):
        extract_images(msgs)


def test_input_image_type_is_accepted():
    msgs = user_with([{"type": "input_image", "image_url": PNG}])
    assert extract_images(msgs) == [{"url": PNG}]
