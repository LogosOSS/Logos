"""Worker-node reconstruction of orchestrator multipart payloads."""

import base64

import pytest

from logos_worker_node.request_content import (
    MULTIPART_PAYLOAD_KEY,
    MediaReferenceError,
    httpx_request_parts,
    validate_media_references,
)


def test_worker_reconstructs_multipart_request():
    payload = {
        "model": "whisper-1",
        MULTIPART_PAYLOAD_KEY: {
            "fields": [["model", "whisper-1"], ["language", "de"]],
            "files": [
                {
                    "field_name": "file",
                    "filename": "speech.webm",
                    "content_type": "audio/webm",
                    "content_base64": base64.b64encode(b"webm-audio").decode(),
                }
            ],
        },
    }

    kwargs, headers = httpx_request_parts(payload)

    assert headers == {}
    assert kwargs["files"] == [
        ("model", (None, "whisper-1")),
        ("language", (None, "de")),
        ("file", ("speech.webm", b"webm-audio", "audio/webm")),
    ]


def test_worker_keeps_json_requests_unchanged():
    payload = {"model": "gpt-4o", "messages": []}
    kwargs, headers = httpx_request_parts(payload)

    assert kwargs == {"json": payload}
    assert headers == {"Content-Type": "application/json"}


DATA_URL = "data:image/png;base64,aGVsbG8="


def _media_payload(part):
    return {"messages": [{"role": "user", "content": [part]}]}


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:80/",
        "https://169.254.169.254/",
        "file:///etc/passwd",
        "ftp://host/image",
        "HTTP://127.0.0.1/",
        "HTTPS://host/image",
        "FILE:///etc/passwd",
        "\thttp://host/image",
        " https://host/image",
        "http://ho\nst/image",
        "http://host/image ",
        "\x00https://host/image",
        "//host/image",
        "/etc/passwd",
        "data:image/png,http://host/image",
        "data:image/png;base64,!!!",
    ],
)
def test_media_urls_reject_nonembedded_sources(url):
    with pytest.raises(MediaReferenceError, match="base64 data:"):
        validate_media_references(_media_payload({"type": "image_url", "image_url": {"url": url}}))


@pytest.mark.parametrize(
    "part",
    [
        {"type": "image_url", "image_url": {"url": DATA_URL}},
        {"type": "input_image", "image_url": DATA_URL},
        {"type": "video_url", "video_url": {"url": DATA_URL}},
        {"type": "audio_url", "audio_url": {"url": DATA_URL}},
        {"type": "input_audio", "input_audio": {"data": "aGVsbG8=", "format": "wav"}},
        {"type": "file", "file": {"filename": "document.pdf", "file_data": "data:application/pdf;base64,aGVsbG8="}},
        {"type": "input_file", "file_url": "data:application/pdf;base64,aGVsbG8="},
    ],
)
def test_embedded_media_is_allowed_without_changing_payload(part):
    import copy

    payload = _media_payload(part)
    original = copy.deepcopy(payload)
    validate_media_references(payload)
    assert httpx_request_parts(payload)[0]["json"] == original


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_image", "image_url": "https://host/image"},
        {"type": "input_file", "file_url": "file:///etc/passwd"},
        {"type": "file", "file": {"file_data": "https://host/file"}},
        {"type": "input_audio", "input_audio": {"url": "https://host/audio"}},
        {"type": "input_audio", "input_audio": {"data": "https://host/audio", "format": "wav"}},
        {"type": "audio_url", "audio_url": "https://host/audio"},
        {"type": "video_url", "video_url": {"url": "https://host/video"}},
        {"type": "image", "source": {"type": "url", "url": "https://host/image"}},
        {"type": "input_file", "source": {"nested": [{"download_url": "https://host/file"}]}},
    ],
)
@pytest.mark.parametrize("nested", [False, True])
def test_responses_media_items_are_checked_at_every_depth(part, nested):
    item = {"type": "message", "role": "user", "content": [{"wrapper": [part]}]} if nested else part
    with pytest.raises(MediaReferenceError):
        validate_media_references({"input": [item]})


@pytest.mark.parametrize(
    "url", ["http://media.example/image", "https://media.example:8443/image", "HTTPS://MEDIA.EXAMPLE/image"]
)
def test_exact_allowlisted_host_is_allowed(url):
    validate_media_references(_media_payload({"type": "image_url", "image_url": {"url": url}}), ["media.example"])


@pytest.mark.parametrize(
    "url",
    [
        "https://sub.media.example/image",
        "https://media.example.evil/image",
        "https://evilmedia.example/image",
        "https://media.example@127.0.0.1/image",
        "https://user@media.example/image",
        "https://media.example./image",
        "https://media.example%2e/image",
        " https://media.example/image",
        "https://me\tdia.example/image",
        "https://media.example\\@127.0.0.1/image",
        "file://media.example/image",
        "ftp://media.example/image",
        "https://media.example:invalid/image",
        "https://[invalid/image",
    ],
)
def test_allowlist_does_not_allow_other_hosts_or_ambiguous_urls(url):
    with pytest.raises(MediaReferenceError):
        validate_media_references(_media_payload({"type": "image_url", "image_url": {"url": url}}), ["media.example"])


def test_text_urls_and_tool_arguments_are_not_media():
    validate_media_references(
        {
            "messages": [{"role": "user", "content": [{"type": "text", "text": "https://host/image"}]}],
            "input": [{"type": "function_call", "arguments": '{"image_url": "https://host/image"}'}],
            "tools": [{"parameters": {"type": "object", "properties": {"url": {"type": ["string", "null"]}}}}],
        }
    )
