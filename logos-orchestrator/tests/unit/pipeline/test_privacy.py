"""Tests for the router's privacy gate (privacy_allows / PRIVACY_ORDER).

The gate decides which deployments may serve a request given the policy's
threshold_privacy. It is a security decision: the failure mode is not a
misrouting but an over-routing — a deployment receiving more confidential
data than the caller asked to keep away from it.
"""

from unittest.mock import MagicMock

import pytest

from logos import main
from logos.dbutils.dbmanager import VALID_PRIVACY_LEVELS
from logos.dbutils.dbmodules import PRIVACY_ORDER, ThresholdLevel, privacy_allows


def test_order_is_trust_descending_and_complete() -> None:
    # The declaration order of ThresholdLevel IS the ordering; a copy that
    # drifts from it (or loses a level) fails here.
    assert PRIVACY_ORDER == tuple(level.value for level in ThresholdLevel)
    assert set(PRIVACY_ORDER) == VALID_PRIVACY_LEVELS
    # Most trusted first, third-party hardware last: a Mac lane must never
    # outrank a datacentre box.
    assert PRIVACY_ORDER[0] == "LOCAL"
    assert PRIVACY_ORDER[-1] == "THIRD_PARTY_HARDWARE"


@pytest.mark.parametrize(
    ("stored", "requested", "effective"),
    [
        ("LOCAL", "CLOUD_NOT_IN_EU_BY_US_PROVIDER", "LOCAL"),
        ("CLOUD_IN_EU_BY_US_PROVIDER", "LOCAL", "LOCAL"),
        ("CLOUD_IN_EU_BY_EU_PROVIDER", "CLOUD_IN_EU_BY_US_PROVIDER", "CLOUD_IN_EU_BY_EU_PROVIDER"),
        (None, "CLOUD_IN_EU_BY_US_PROVIDER", "CLOUD_IN_EU_BY_US_PROVIDER"),
    ],
)
def test_model_string_can_only_tighten_selected_profile(monkeypatch, stored, requested, effective):
    db = MagicMock()
    selected = {"threshold_privacy": stored, "name": "routing profile"}
    db.return_value.__enter__.return_value.get_policy.return_value = selected
    monkeypatch.setattr(main, "DBManager", db)
    body = {"model": f"logos-v1.0__policy_default=false__policy_privacy={requested}"}
    policy = main._extract_policy({"policy": "17"}, "key", body)
    assert policy == {"threshold_privacy": effective, "name": "routing profile"}
    db.return_value.__enter__.return_value.get_policy.assert_called_once_with("key", 17)


def test_local_threshold_is_datacentre_only() -> None:
    # The acute gap: a caller demanding LOCAL must not be routed to a
    # personal Mac, even though LOCAL used to be the *default* provider
    # level.
    assert privacy_allows("LOCAL", "LOCAL")
    assert not privacy_allows("LOCAL", "CLOUD_IN_EU_BY_EU_PROVIDER")
    assert not privacy_allows("LOCAL", "CLOUD_IN_EU_BY_US_PROVIDER")
    assert not privacy_allows("LOCAL", "CLOUD_NOT_IN_EU_BY_US_PROVIDER")
    assert not privacy_allows("LOCAL", "THIRD_PARTY_HARDWARE")


def test_third_party_hardware_is_opt_in_only() -> None:
    # A Mac lane serves only requests whose policy explicitly allows
    # third-party hardware — which, being the least trusted tier, allows
    # every deployment.
    assert privacy_allows("THIRD_PARTY_HARDWARE", "LOCAL")
    assert privacy_allows("THIRD_PARTY_HARDWARE", "CLOUD_NOT_IN_EU_BY_US_PROVIDER")
    assert privacy_allows("THIRD_PARTY_HARDWARE", "THIRD_PARTY_HARDWARE")


def test_intermediate_thresholds_keep_their_ordering() -> None:
    assert privacy_allows("CLOUD_IN_EU_BY_EU_PROVIDER", "LOCAL")
    assert privacy_allows("CLOUD_IN_EU_BY_EU_PROVIDER", "CLOUD_IN_EU_BY_EU_PROVIDER")
    assert not privacy_allows("CLOUD_IN_EU_BY_EU_PROVIDER", "CLOUD_NOT_IN_EU_BY_US_PROVIDER")
    assert not privacy_allows("CLOUD_IN_EU_BY_EU_PROVIDER", "THIRD_PARTY_HARDWARE")


def test_unknown_threshold_fails_closed_to_strictest() -> None:
    # A typo in a policy must not accept every deployment — it may accept
    # only the most trusted ones.
    assert privacy_allows("TYPO_LEVEL", "LOCAL")
    assert not privacy_allows("TYPO_LEVEL", "CLOUD_IN_EU_BY_EU_PROVIDER")
    assert not privacy_allows("TYPO_LEVEL", "THIRD_PARTY_HARDWARE")


def test_unknown_level_fails_closed_to_least_trusted() -> None:
    # A deployment type the router has not been taught about (e.g. a new
    # enum value that reached the DB before the router did) must not
    # qualify for the strictest requests.
    assert not privacy_allows("LOCAL", "FUTURE_LEVEL")
    assert not privacy_allows("CLOUD_IN_EU_BY_US_PROVIDER", "FUTURE_LEVEL")
    assert privacy_allows("THIRD_PARTY_HARDWARE", "FUTURE_LEVEL")
