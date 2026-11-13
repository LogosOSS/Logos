"""Which Azure deployments batch, probed before any batch is submitted.

Azure batches only on deployments of a Batch SKU (``GlobalBatch``,
``DataZoneBatch``). Such a deployment sits next to the Standard deployment
that serves interactive traffic, usually under a name of its own. The
data-plane deployment listing that the deployment sync reads with the
resource's ``api-key`` reports every deployment as ``scale_type: standard``,
and the management API that knows the SKU needs Entra credentials Logos does
not hold — so the answer is taken from the Batch API itself:

* a one-line batch is created against the deployment;
* a Standard deployment is refused right away (``invalid_deployment_type``,
  naming its SKU), before anything runs;
* an accepted creation is cancelled at once, so the probe line is not run.

The upload is deleted afterwards either way. A deployment is probed again only
when it changed since the last probe or the probe is older than
``LOGOS_AZURE_BATCH_PROBE_TTL_DAYS``; between those, the stored answer stands.
"""

import datetime
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

from logos.dbutils.dbmanager import DBManager

logger = logging.getLogger(__name__)

PROBE_ENABLED = os.getenv("LOGOS_AZURE_BATCH_PROBE_ENABLED", "true").lower() == "true"
PROBE_TTL = datetime.timedelta(days=int(os.getenv("LOGOS_AZURE_BATCH_PROBE_TTL_DAYS", "7")))
PROBE_CUSTOM_ID = "logos-batch-probe"

# Operations the Batch API runs, and the smallest request line for each.
_PROBE_BODIES: Dict[str, Dict[str, Any]] = {
    "chat/completions": {"messages": [{"role": "user", "content": "ping"}], "max_completion_tokens": 16},
    "responses": {"input": "ping", "max_output_tokens": 16},
    "embeddings": {"input": "ping"},
}


def probe_line(deployment_id: str, operation: str) -> Optional[bytes]:
    """The one-line batch input a probe uploads, or ``None`` for an operation Batch does not run."""
    body = _PROBE_BODIES.get(operation)
    if body is None:
        return None
    line = {
        "custom_id": PROBE_CUSTOM_ID,
        "method": "POST",
        "url": f"/{operation}",
        "body": {"model": deployment_id, **body},
    }
    return (json.dumps(line) + "\n").encode("utf-8")


async def probe_deployment(
    provider: Dict[str, Any], deployment_id: str, operation: str, client: httpx.AsyncClient
) -> Tuple[Optional[bool], str]:
    """Ask the provider's Batch API whether it batches on this deployment.

    Returns ``(True, detail)`` when a batch creation was accepted (and
    cancelled), ``(False, detail)`` when the provider refused it because of the
    deployment, and ``(None, detail)`` when the probe could not tell.
    """
    from logos.batch_api import BatchOperation, batch_operation_url, batch_provider_headers

    content = probe_line(deployment_id, operation)
    if content is None:
        return False, f"The Batch API does not run {operation} requests."

    headers = batch_provider_headers(provider)
    upload_url = batch_operation_url(provider, BatchOperation(resource="files", method="POST", path="v1/files"))
    try:
        uploaded = await client.post(
            upload_url,
            headers=headers,
            data={"purpose": "batch"},
            files=[("file", ("logos-batch-probe.jsonl", content, "application/jsonl"))],
            timeout=60.0,
        )
    except httpx.HTTPError as exc:
        return None, f"probe upload failed: {type(exc).__name__}: {exc}"
    if uploaded.status_code >= 400:
        return None, f"probe upload refused: HTTP {uploaded.status_code}"
    file_id = (uploaded.json() or {}).get("id")
    if not file_id:
        return None, "probe upload returned no file id"

    try:
        return await _create_and_cancel(provider, file_id, operation, headers, client)
    finally:
        delete = BatchOperation(resource="files", method="DELETE", path=f"v1/files/{file_id}", resource_id=file_id)
        try:
            await client.delete(batch_operation_url(provider, delete), headers=headers, timeout=30.0)
        except httpx.HTTPError:
            logger.debug("Could not delete batch probe file %s", file_id, exc_info=True)


async def _create_and_cancel(
    provider: Dict[str, Any],
    file_id: str,
    operation: str,
    headers: Dict[str, str],
    client: httpx.AsyncClient,
) -> Tuple[Optional[bool], str]:
    from logos.batch_api import BatchOperation, _looks_like_batch_model_error, _provider_error_text, batch_operation_url

    create_url = batch_operation_url(provider, BatchOperation(resource="batches", method="POST", path="v1/batches"))
    try:
        created = await client.post(
            create_url,
            headers={**headers, "Content-Type": "application/json"},
            json={
                "input_file_id": file_id,
                "endpoint": f"/{operation}",
                "completion_window": "24h",
                "metadata": {"purpose": "logos batch support probe"},
            },
            timeout=60.0,
        )
    except httpx.HTTPError as exc:
        return None, f"probe creation failed: {type(exc).__name__}: {exc}"
    try:
        body = created.json() or {}
    except ValueError:
        body = {}
    error_text = _provider_error_text(body) if isinstance(body, dict) else ""
    batch_id = body.get("id") if isinstance(body, dict) else None

    if created.status_code < 400 and batch_id and not error_text:
        cancel = BatchOperation(
            resource="batches",
            method="POST",
            path=f"v1/batches/{batch_id}/cancel",
            resource_id=batch_id,
            suboperation="cancel",
        )
        try:
            await client.post(batch_operation_url(provider, cancel), headers=headers, timeout=30.0)
        except httpx.HTTPError:
            logger.warning("Could not cancel batch probe %s on provider %s", batch_id, provider.get("name"))
        return True, "The Batch API accepted this deployment."

    if _looks_like_batch_model_error(error_text):
        return False, error_text[:400]
    return None, (error_text or f"HTTP {created.status_code}")[:400]


def _deployment_changed_at(deployment: Dict[str, Any]) -> Optional[datetime.datetime]:
    stamp = deployment.get("updated_at") or deployment.get("created_at")
    if isinstance(stamp, (int, float)):
        return datetime.datetime.fromtimestamp(stamp, tz=datetime.timezone.utc)
    return None


def needs_probe(known: Optional[Dict[str, Any]], deployments: List[Dict[str, Any]], now: datetime.datetime) -> bool:
    """Whether a model's stored Batch answer is missing, stale, or no longer matches its deployments.

    A positive answer also expires when one of the batch deployments it names
    is gone from the provider: forwarded batches would otherwise keep naming a
    deleted deployment until the TTL runs out.
    """
    if not known or known.get("source") != "probe":
        return True
    current = {str(dep.get("id")) for dep in deployments}
    if known.get("eligible"):
        recorded = set(known.get("batch_deployment_ids") or [])
        if known.get("batch_deployment"):
            recorded.add(str(known["batch_deployment"]))
        if not recorded or not recorded <= current:
            return True
    checked_at = known.get("checked_at")
    if not isinstance(checked_at, datetime.datetime):
        return True
    checked_at = checked_at.astimezone(datetime.timezone.utc)
    if now - checked_at > PROBE_TTL:
        return True
    changed = [stamp for stamp in (_deployment_changed_at(dep) for dep in deployments) if stamp is not None]
    return any(stamp > checked_at for stamp in changed)


async def probe_provider(
    provider: Dict[str, Any],
    raw_deployments: List[Dict[str, Any]],
    client: httpx.AsyncClient,
) -> int:
    """Probe the deployments of every model the provider serves and store the answers.

    A model is batchable when any of its deployments is; that deployment is
    stored as the one forwarded batches use. Returns how many models were
    probed.
    """
    from logos.sdi.azure_deployment_sync import classify_azure_operation

    by_model: Dict[str, List[Dict[str, Any]]] = {}
    for dep in raw_deployments:
        if (dep.get("status") or "").lower() not in ("", "succeeded"):
            continue
        if dep.get("model") and dep.get("id"):
            by_model.setdefault(str(dep["model"]), []).append(dep)
    if not by_model:
        return 0
    # Only Azure deployments are probed; the Batch URL and the api-key header
    # depend on the provider being addressed as Azure.
    provider = {**provider, "cloud_provider_type": "azure"}

    with DBManager() as db:
        model_ids = {
            str(row["model_name"]).lower(): int(row["model_id"])
            for row in db.get_provider_model_deployments(int(provider["id"]))
        }
        support = db.get_batch_model_support(int(provider["id"]))

    now = datetime.datetime.now(datetime.timezone.utc)
    probed = 0
    for model_name, deps in sorted(by_model.items()):
        model_id = model_ids.get(model_name.lower())
        if model_id is None or not needs_probe(support.get(model_id), deps, now):
            continue
        operation = classify_azure_operation(model_name).suffix
        # Every deployment is probed, not just until the first acceptance: each
        # confirmed batch deployment has to be kept out of interactive routing.
        accepted: List[str] = []
        inconclusive: List[str] = []
        details: List[str] = []
        for dep in sorted(deps, key=lambda d: str(d["id"])):
            answer, detail = await probe_deployment(provider, str(dep["id"]), operation, client)
            details.append(f"{dep['id']}: {detail}")
            if answer:
                accepted.append(str(dep["id"]))
            elif answer is None:
                inconclusive.append(str(dep["id"]))
        probed += 1
        # A deployment confirmed before whose probe gave no clear answer now
        # stays a batch deployment; dropping it would hand it back to
        # interactive routing on a timeout.
        prior = support.get(model_id) or {}
        prior_ids = set(prior.get("batch_deployment_ids") or []) if prior.get("eligible") else set()
        if prior.get("eligible") and prior.get("batch_deployment"):
            prior_ids.add(str(prior["batch_deployment"]))
        accepted += [dep_id for dep_id in inconclusive if dep_id in prior_ids]
        eligible: Optional[bool] = True if accepted else (None if inconclusive else False)
        if eligible is None and prior_ids:
            # The confirmed batch deployments are gone and the rest gave no
            # clear answer: the stored positive answer would still route
            # batches to a deleted deployment, so it is dropped now and the
            # model stays due for the next pass.
            details.append("previously confirmed batch deployments are gone: " + ", ".join(sorted(prior_ids)))
            eligible = False
        if eligible is None:
            # Nothing conclusive: keep what was known and try again next pass.
            logger.info("Batch probe of %s on %s was inconclusive: %s", model_name, provider.get("name"), details)
            continue
        with DBManager() as db:
            preferred = prior.get("batch_deployment") if prior.get("batch_deployment") in accepted else None
            db.record_model_batch_probe(
                int(provider["id"]),
                model_id,
                bool(eligible),
                preferred or (sorted(accepted)[0] if accepted else None),
                " | ".join(details),
                batch_deployment_ids=accepted,
                # Some deployments gave no clear answer: keep the model due
                # for the next sync instead of trusting this pass for the TTL.
                checked_at=now - PROBE_TTL if inconclusive else None,
            )
    return probed
