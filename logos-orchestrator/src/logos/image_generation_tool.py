"""The Responses API ``image_generation`` tool.

A Responses request can ask its model to call the built-in image tool, which
names its own image model (``{"type": "image_generation", "model":
"gpt-image-1.5"}``). That image model is a second model the request runs, so
the key has to be allowed to use it like any other. On Azure it is also a
separate deployment of the resource, and Azure only runs the tool when that
deployment is named in a request header; the body's tool entry alone is
refused with ``imagegen deployment must be provided through header``.
"""

import logging
from typing import Any, Dict, Iterable, List, Optional

from logos.dbutils.dbmanager import DBManager
from logos.sdi.providers.azure_provider import extract_azure_deployment_name

logger = logging.getLogger(__name__)

IMAGE_GENERATION_TOOL = "image_generation"

# Header Azure reads the image deployment of the tool from.
AZURE_IMAGE_DEPLOYMENT_HEADER = "x-ms-oai-image-generation-deployment"

# The image model the tool uses when the entry names none (OpenAI's default).
DEFAULT_IMAGE_MODEL = "gpt-image-1"


def requested_image_models(payload: Any) -> List[str]:
    """The image models a request's ``image_generation`` tools name, in order, without duplicates."""
    if not isinstance(payload, dict):
        return []
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return []
    names: List[str] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != IMAGE_GENERATION_TOOL:
            continue
        model = tool.get("model")
        name = model.strip() if isinstance(model, str) and model.strip() else DEFAULT_IMAGE_MODEL
        if name.lower() not in {existing.lower() for existing in names}:
            names.append(name)
    return names


def _deployment_names(deployment: Dict[str, Any]) -> List[str]:
    """The names a deployment answers to: its model name and the model's aliases."""
    names = [str(deployment.get("model_name") or "")]
    aliases = deployment.get("aliases")
    if isinstance(aliases, str) and aliases:
        names.extend(alias.strip() for alias in aliases.split(","))
    return [name.lower() for name in names if name]


def image_tool_access_error(payload: Any, deployments: Iterable[Dict[str, Any]]) -> Optional[str]:
    """Why the request's image tool may not run for this key, or ``None`` when it may.

    The image model is checked against the same deployments that decide which
    chat model the key may use, so a key without access to the image model
    cannot reach it through another model's tool call.
    """
    wanted = requested_image_models(payload)
    if not wanted:
        return None
    if len(wanted) > 1:
        return (
            "A request may use one image model in its image_generation tools; "
            f"this one names {', '.join(repr(name) for name in wanted)}."
        )
    permitted = {name for deployment in deployments for name in _deployment_names(deployment)}
    if wanted[0].lower() not in permitted:
        return (
            f"The image_generation tool uses {wanted[0]!r}, which this key may not use. "
            "Ask the team owner to grant the image model to the key."
        )
    return None


def azure_image_deployment_headers(provider_id: int, payload: Any) -> Dict[str, str]:
    """The header naming the image tool's Azure deployment on this provider.

    Empty when the request uses no image tool or the provider serves no
    deployment of the image model; the provider then answers with its own
    refusal, which names what is missing.
    """
    wanted = requested_image_models(payload)
    if not wanted:
        return {}
    model_name = wanted[0].lower()
    try:
        with DBManager() as db:
            rows = db.get_provider_model_deployments(int(provider_id))
    except Exception:  # noqa: BLE001 - a lookup failure leaves the provider's own refusal
        logger.exception("Could not look up the image deployment on provider %s", provider_id)
        return {}
    for row in rows:
        # The tool may name the image model by an alias the key is allowed to
        # use; the deployment belongs to the canonical model.
        if model_name not in _deployment_names(row):
            continue
        deployment = extract_azure_deployment_name(str(row.get("endpoint") or ""))
        if deployment:
            return {AZURE_IMAGE_DEPLOYMENT_HEADER: deployment}
    logger.info("Provider %s serves no deployment of image model %s", provider_id, wanted[0])
    return {}
