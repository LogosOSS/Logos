"""Hugging Face license fetch for a model repository, via the central HF_TOKEN.

Reads what a requested model's license actually is — the SPDX expression the
model card declares plus the license file's text when the repository carries
one — so the web service can store it and show it to the roles that must
accept the agreement. Gated repositories are only readable once the token's
account has been granted access, so callers ask for the license of a
repository the reachability check has already answered ``reachable`` for.
"""

import logging
import os
import re
from dataclasses import dataclass

import httpx

from logos.hf_reachability import _TIMEOUT_S, _new_client

logger = logging.getLogger(__name__)

_LICENSE_FILE_NAME = re.compile(
    r"^(licen[cs]e|copying|community-license-agreement)(\.[a-z0-9]+)?$", re.IGNORECASE
)

# License files live in a handful of conventional names; the repository's
# tree is scanned for them rather than assuming one spelling.
_DEFAULT_REFS = ("main", "master")


@dataclass(frozen=True)
class HfLicense:
    hf_repo_id: str
    # SPDX expression from the model card (``cardData.license``); several
    # declared licenses are joined as an OR expression.
    license_id: str | None
    # The repository file the text was read from (None when there is none).
    license_file: str | None
    license_text: str | None
    # Set when the license could not be determined; the fields are then all
    # None and the caller should treat the result as "not fetched yet".
    error: str | None


async def _fetch(hf_repo_id: str, token: str | None) -> tuple[str | None, str | None, str | None, str | None]:
    from huggingface_hub import constants
    from huggingface_hub.errors import GatedRepoError, HFValidationError, RepositoryNotFoundError
    from huggingface_hub.utils import build_hf_headers, hf_raise_for_status, validate_repo_id

    try:
        validate_repo_id(hf_repo_id)
        # False, not None: None would fall back to a token cached on disk —
        # the same discipline as the reachability check.
        headers = build_hf_headers(token=token or False)
        async with _new_client() as client:
            info = await client.get(f"{constants.ENDPOINT}/api/models/{hf_repo_id}", headers=headers)
            hf_raise_for_status(info)
            card_data = info.json().get("cardData") or {}
            license_id = card_data.get("license")
            if isinstance(license_id, list):
                license_id = " OR ".join(str(entry) for entry in license_id) or None
            elif not isinstance(license_id, str):
                license_id = None

            license_file: str | None = None
            license_ref: str | None = None
            for ref in _DEFAULT_REFS:
                tree = await client.get(
                    f"{constants.ENDPOINT}/api/models/{hf_repo_id}/tree/{ref}", headers=headers
                )
                if tree.status_code == 404:
                    continue  # not this repository's default branch
                hf_raise_for_status(tree)
                candidates = [
                    entry["path"]
                    for entry in tree.json()
                    if entry.get("type") == "file"
                    and _LICENSE_FILE_NAME.match(entry["path"].rsplit("/", 1)[-1])
                ]
                if candidates:
                    license_file = min(candidates, key=_license_file_rank)
                    license_ref = ref
                    break

            license_text: str | None = None
            if license_file is not None and license_ref is not None:
                file = await client.get(
                    f"{constants.ENDPOINT}/api/models/{hf_repo_id}/resolve/{license_ref}/{license_file}",
                    headers=headers,
                )
                hf_raise_for_status(file)
                license_text = file.text
        return license_id, license_file, license_text, None
    except HFValidationError:
        return None, None, None, "Not a valid Hugging Face repository id."
    # GatedRepoError subclasses RepositoryNotFoundError, so it must come first.
    except GatedRepoError:
        return None, None, None, "Repository access requires an authorized HF_TOKEN."
    except RepositoryNotFoundError:
        # The Hub answers identically for a missing repo and a private one.
        return (
            None,
            None,
            None,
            "Repository does not exist or is not visible to the configured HF_TOKEN.",
        )
    except httpx.TimeoutException:
        return None, None, None, f"Hugging Face Hub did not answer within {_TIMEOUT_S:g}s."
    except Exception:  # noqa: BLE001
        # Library/network errors can name internal hosts or proxies; log only.
        logger.warning("HF license fetch failed for %s", hf_repo_id, exc_info=True)
        return None, None, None, "Hugging Face Hub could not be reached."


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
    """Fetch one repository's license. Never raises.

    A returned ``error`` (network error, Hub outage, timeout, or a repository
    the token may not read) is not a statement about the repository's
    licensing — callers must leave the row pending and retry later.
    """
    repo_id = hf_repo_id.strip()
    token = os.getenv("HF_TOKEN", "").strip() or None
    license_id, license_file, license_text, error = await _fetch(repo_id, token)
    return HfLicense(repo_id, license_id, license_file, license_text, error)
