"""Validate media references and decode multipart inference payloads."""

import base64
import binascii
from typing import Any
from urllib.parse import urlsplit

MULTIPART_PAYLOAD_KEY = "_logos_multipart"


# A nonempty list is required: vLLM treats an empty allowlist as unrestricted.
DISABLED_MEDIA_DOMAIN = "logos-media-disabled.invalid"
_MEDIA_FIELDS = frozenset(
    {
        "image",
        "image_url",
        "input_image",
        "audio",
        "audio_url",
        "input_audio",
        "video",
        "video_url",
        "input_video",
        "file",
        "file_url",
        "input_file",
        "document",
    }
)


class MediaReferenceError(ValueError):
    """A media reference would access a source outside the worker's policy."""

    def error_body(self) -> dict[str, Any]:
        """Return the OpenAI-compatible invalid-request body."""
        return {
            "error": {"message": str(self), "type": "invalid_request_error", "param": None, "code": "invalid_media_url"}
        }


def validate_media_references(payload: Any, allowed_domains: list[str] | tuple[str, ...] = ()) -> None:
    """Reject external media recursively, except HTTP(S) hosts explicitly allowed by the worker.

    Text and tool arguments remain opaque strings. Embedded audio data may be
    raw base64; media URL fields must contain base64 data URLs or allowed HTTP(S) URLs.
    Raises MediaReferenceError before any lane request is sent.
    """

    def reject() -> None:
        raise MediaReferenceError(
            "Worker media must use base64 data: URLs or HTTP(S) URLs on explicitly allowed media domains; "
            "local files and other URL schemes are not allowed."
        )

    def check_url(value: Any) -> None:
        if not isinstance(value, str):
            reject()
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
            reject()
        if value.startswith("data:"):
            header, separator, encoded = value.partition(",")
            if not separator or not header.endswith(";base64"):
                reject()
            try:
                base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                reject()
            return
        try:
            url = urlsplit(value)
            allowed = (
                url.scheme in {"http", "https"}
                and url.hostname in allowed_domains
                and url.username is None
                and url.password is None
                and url.port != 0
            )
        except ValueError:
            reject()
        if not allowed:
            reject()

    def walk(value: Any, media: bool = False, reference: bool = False) -> None:
        if isinstance(value, dict):
            media_type = value.get("type")
            media = media or (isinstance(media_type, str) and media_type in _MEDIA_FIELDS)
            for key, child in value.items():
                is_media_field = key in _MEDIA_FIELDS
                is_url = key == "url" or key.endswith("_url")
                if is_url and (media or is_media_field):
                    walk(child, media=True, reference=True)
                elif media and key in {"data", "file_data"} and isinstance(child, str):
                    if child.startswith("data:"):
                        check_url(child)
                    else:
                        try:
                            base64.b64decode(child, validate=True)
                        except (ValueError, binascii.Error):
                            reject()
                else:
                    walk(child, media=media or is_media_field, reference=is_media_field)
        elif isinstance(value, list):
            for child in value:
                walk(child, media=media, reference=reference)
        elif reference:
            check_url(value)

    walk(payload)


def httpx_request_parts(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Return httpx request kwargs and safe upstream headers for a payload."""
    multipart = payload.get(MULTIPART_PAYLOAD_KEY)
    if not isinstance(multipart, dict):
        return {"json": payload}, {"Content-Type": "application/json"}

    data = [(str(name), str(value)) for name, value in multipart.get("fields", [])]
    files: list[tuple[str, tuple]] = []
    for item in multipart.get("files", []):
        try:
            content = base64.b64decode(item["content_base64"], validate=True)
        except (KeyError, ValueError, binascii.Error) as exc:
            raise ValueError("Invalid encoded multipart file") from exc
        files.append(
            (
                str(item.get("field_name") or "file"),
                (
                    str(item.get("filename") or "audio"),
                    content,
                    str(item.get("content_type") or "application/octet-stream"),
                ),
            )
        )
    form_parts = [(name, (None, value)) for name, value in data]
    return {"files": [*form_parts, *files]}, {}
