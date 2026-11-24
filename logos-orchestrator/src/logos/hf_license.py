"""Hugging Face license and use-policy fetch for a model repository.

Reads what a requested model's license actually is — the SPDX expression the
model card declares plus the license file's text when the repository carries
one — and, where the repository publishes one, the acceptable-use policy
alongside it (Meta's Llama repositories carry USE_POLICY.md, Black Forest
Labs' FLUX a POLICY.md). The web service stores both and shows them to the
roles that must accept the agreement. Gated repositories are only readable
once the token's account has been granted access, so callers ask for the
license of a repository the reachability check has already answered
``reachable`` for.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass

import httpx

from logos.hf_reachability import _TIMEOUT_S, _new_client

logger = logging.getLogger(__name__)

_LICENSE_FILE_NAME = re.compile(r"^(licen[cs]e|copying|community-license-agreement)(\.[a-z0-9]+)?$", re.IGNORECASE)

# Use policies publish under a handful of conventional names (USE_POLICY.md,
# POLICY.md, ACCEPTABLE_USE.md, ...); like the license files they are found
# by scanning the repository's tree rather than assuming one spelling.
_USE_POLICY_FILE_NAME = re.compile(
    r"^(use[_-]?policy|acceptable[_-]?use[_-]?policy|acceptable[_-]?use|policy|aup)\.[a-z0-9]+$", re.IGNORECASE
)

_DEFAULT_REFS = ("main", "master")

# The fetch is several sequential Hub round trips (card, tree, the files),
# so it is bounded as a unit, not only per request: a slow Hub must not hold
# the caller's round trip open longer than this.
OVERALL_DEADLINE_S = 60.0

# The license and use-policy files are the response bodies the repository
# controls itself: stream them and refuse them, before they are buffered or
# decoded, once they outgrow this cap. The longest real policy texts are far
# below one megabyte.
MAX_REPO_FILE_BYTES = 1_048_576


class _RepoFileTooLarge(Exception):
    """A repository file outgrew the streaming byte cap."""

    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.label = label


@dataclass(frozen=True)
class HfLicense:
    hf_repo_id: str
    # SPDX expression from the model card (``cardData.license``); several
    # declared licenses are joined as an OR expression.
    license_id: str | None
    # The repository file the text was read from (None when there is none).
    license_file: str | None
    license_text: str | None
    # The acceptable-use policy the repository publishes alongside the
    # license, if any: the file it was read from and its text.
    use_policy_file: str | None
    use_policy_text: str | None
    # Set when the license could not be determined; the fields are then all
    # None and the caller should treat the result as "not fetched yet".
    error: str | None


async def _fetch(
    hf_repo_id: str, token: str | None
) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None]:
    from huggingface_hub import constants
    from huggingface_hub.errors import GatedRepoError, HFValidationError, RepositoryNotFoundError
    from huggingface_hub.utils import build_hf_headers, hf_raise_for_status, validate_repo_id

    try:
        validate_repo_id(hf_repo_id)
        # False, not None: None would fall back to a token cached on disk —
        # the same discipline as the reachability check.
        headers = build_hf_headers(token=token or False)
        async with _new_client() as client:

            async def get(url: str, follow_redirects: bool = False) -> httpx.Response:
                return await client.get(url, headers=headers, timeout=_TIMEOUT_S, follow_redirects=follow_redirects)

            # A file comes from the canonical resolve URL (not the
            # /api/... variant, which 404s for these repositories), and LFS
            # files 302 to CDN storage, so follow the redirect. The body is
            # streamed chunk by chunk and refused once it outgrows the cap:
            # the filename matched the conventional pattern, but the content
            # is the repository's, and an unbounded buffer would let it
            # exhaust memory before this function could reject it.
            async def read_repo_file(url: str, label: str) -> str:
                async with client.stream(
                    "GET", url, headers=headers, timeout=_TIMEOUT_S, follow_redirects=True
                ) as file:
                    hf_raise_for_status(file)
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in file.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_REPO_FILE_BYTES:
                            raise _RepoFileTooLarge(label)
                        chunks.append(chunk)
                    return b"".join(chunks).decode("utf-8", errors="replace")

            # The overall deadline bounds elapsed time across redirects and
            # streamed chunks, which a per-request inactivity timeout
            # cannot: it expires the in-flight operation and raises
            # TimeoutError at the boundary.
            async with asyncio.timeout(OVERALL_DEADLINE_S):
                info = await get(f"{constants.ENDPOINT}/api/models/{hf_repo_id}")
                hf_raise_for_status(info)
                card_data = info.json().get("cardData") or {}
                license_id = card_data.get("license")
                if isinstance(license_id, list):
                    license_id = " OR ".join(str(entry) for entry in license_id) or None
                elif not isinstance(license_id, str):
                    license_id = None

                license_file = license_text = policy_file = use_policy_text = None
                for ref in _DEFAULT_REFS:
                    tree = await get(f"{constants.ENDPOINT}/api/models/{hf_repo_id}/tree/{ref}")
                    if tree.status_code == 404:
                        continue
                    hf_raise_for_status(tree)
                    paths = [entry["path"] for entry in tree.json() if entry.get("type") == "file"]
                    licenses = [path for path in paths if _LICENSE_FILE_NAME.match(path.rsplit("/", 1)[-1])]
                    policies = [path for path in paths if _USE_POLICY_FILE_NAME.match(path.rsplit("/", 1)[-1])]
                    base_url = f"{constants.ENDPOINT}/{hf_repo_id}/resolve/{ref}"
                    if licenses:
                        license_file = min(licenses, key=_license_file_rank)
                        license_text = await read_repo_file(f"{base_url}/{license_file}", "License file")
                    if policies:
                        policy_file = min(policies)
                        use_policy_text = await read_repo_file(f"{base_url}/{policy_file}", "Use policy file")
                    # A successful tree selects the revision for both documents, even when either is absent.
                    break
        return license_id, license_file, license_text, policy_file, use_policy_text, None
    except HFValidationError:
        return (None, None, None, None, None, "Not a valid Hugging Face repository id.")
    # GatedRepoError subclasses RepositoryNotFoundError, so it must come first.
    except GatedRepoError:
        return (None, None, None, None, None, "Repository access requires an authorized HF_TOKEN.")
    except RepositoryNotFoundError:
        # The Hub answers identically for a missing repo and a private one.
        return (
            None,
            None,
            None,
            None,
            None,
            "Repository does not exist or is not visible to the configured HF_TOKEN.",
        )
    except _RepoFileTooLarge as too_large:
        return (
            None,
            None,
            None,
            None,
            None,
            f"{too_large.label} is larger than {MAX_REPO_FILE_BYTES} bytes and was not fetched.",
        )
    except TimeoutError:
        # asyncio.timeout expired while a request or a response body was
        # still in flight: the Hub as a whole was too slow, not one request.
        return (None, None, None, None, None, f"Hugging Face Hub did not answer within {OVERALL_DEADLINE_S:g}s.")
    except httpx.TimeoutException:
        return (None, None, None, None, None, f"Hugging Face Hub did not answer within {_TIMEOUT_S:g}s.")
    except Exception:  # noqa: BLE001
        # Library/network errors can name internal hosts or proxies; log only.
        logger.warning("HF license fetch failed for %s", hf_repo_id, exc_info=True)
        return (None, None, None, None, None, "Hugging Face Hub could not be reached.")


def _license_file_rank(path: str) -> tuple[int, str]:
    """Prefer the conventional names: LICENSE* over the provider-specific
    agreement files over COPYING, then alphabetically for determinism."""
    name = path.rsplit("/", 1)[-1].lower()
    if name.startswith("licen"):
        rank = 0
    elif name.startswith("community-license"):
        rank = 1
    else:
        rank = 2
    return (rank, path)


async def fetch_hf_license(hf_repo_id: str) -> HfLicense:
    """Fetch one repository's license and use policy. Never raises.

    A returned ``error`` (network error, Hub outage, timeout, an oversized
    license or use-policy file, or a repository the token may not read) is
    not a statement about the repository's licensing — callers must leave
    the row pending and retry later.
    """
    repo_id = hf_repo_id.strip()
    token = os.getenv("HF_TOKEN", "").strip() or None
    license_id, license_file, license_text, use_policy_file, use_policy_text, error = await _fetch(repo_id, token)
    return HfLicense(repo_id, license_id, license_file, license_text, use_policy_file, use_policy_text, error)
