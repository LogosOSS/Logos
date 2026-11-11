"""Public inference operations accepted before credentials or provider routing."""

from fastapi import HTTPException, Request

# Keep in sync with LogosBridgeClient._ALLOWED_REQUEST_PATHS. The worker also
# allows v1/models for its model catalogue; the public catalogue is served
# locally and must never enter the POST inference pipeline.
ALLOWED_INFERENCE_PATHS = frozenset(
    {
        "v1/chat/completions",
        "v1/completions",
        "v1/embeddings",
        "v1/responses",
        "v1/messages",
        "v1/audio/transcriptions",
        "v1/audio/translations",
        "v1/audio/speech",
        "v1/systemone",
        "v1/rerank",
        "v1/score",
        "v2/embed",
        "v2/rerank",
        "pooling",
        "score",
        "rerank",
        "classify",
        "tokenize",
        "detokenize",
    }
)


def require_inference_path(path: str) -> None:
    """Reject operations outside the inference surface without normalizing them."""
    if path.removeprefix("/") not in ALLOWED_INFERENCE_PATHS:
        raise HTTPException(status_code=404, detail="Not found")


def require_canonical_path(request: Request) -> None:
    """Reject ambiguous public paths before any route authenticates the caller."""
    path = request.scope["path"].removeprefix("/")
    raw = request.scope.get("raw_path", b"").lower()
    if (
        any(part in {"", ".", ".."} for part in path.split("/"))
        or any(char in "\\%" or ord(char) < 32 or 127 <= ord(char) <= 159 for char in path)
        or b"%" in raw
    ):
        raise HTTPException(status_code=404, detail="Not found")
