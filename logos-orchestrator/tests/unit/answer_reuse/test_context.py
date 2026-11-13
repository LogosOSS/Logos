"""Reading the logos-context header and scoping it to the key's owner."""

from __future__ import annotations

import pytest

from logos.answer_reuse.context import MAX_CONTEXT_ID_LENGTH, InvalidContextId, cache_scope, context_id_from_headers


def test_no_header_means_no_reuse():
    assert context_id_from_headers({"Authorization": "Bearer x"}) is None
    assert context_id_from_headers({}) is None
    assert context_id_from_headers(None) is None


@pytest.mark.parametrize("name", ["logos-context", "Logos-Context", "LOGOS-CONTEXT", "logos_context"])
def test_both_spellings_in_any_case_are_read(name):
    assert context_id_from_headers({name: "shop-a"}) == "shop-a"


def test_the_hyphen_spelling_wins_when_both_are_sent():
    assert context_id_from_headers({"logos-context": "shop-a", "logos_context": "shop-b"}) == "shop-a"


def test_surrounding_whitespace_is_trimmed():
    assert context_id_from_headers({"logos-context": "  shop-a  "}) == "shop-a"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_header_means_no_reuse(blank):
    assert context_id_from_headers({"logos-context": blank}) is None


def test_free_form_names_are_kept_as_sent():
    # Typical names: a course, a user within a course, a project.
    for name in ["course-42", "user-7+course-42", "wasnertobias-iPraktikumSS26"]:
        assert context_id_from_headers({"logos-context": name}) == name


def test_the_maximum_length_is_accepted():
    name = "x" * MAX_CONTEXT_ID_LENGTH
    assert context_id_from_headers({"logos-context": name}) == name


def test_an_overlong_name_is_rejected():
    with pytest.raises(InvalidContextId):
        context_id_from_headers({"logos-context": "x" * (MAX_CONTEXT_ID_LENGTH + 1)})


@pytest.mark.parametrize("bad", ["shop\na", "shop\x00a", "shop\x7fa"])
def test_control_characters_are_rejected(bad):
    with pytest.raises(InvalidContextId):
        context_id_from_headers({"logos-context": bad})


def test_the_same_name_from_two_teams_gives_two_scopes():
    assert cache_scope("shop-a", team_id=3, api_key_id=10) != cache_scope("shop-a", team_id=7, api_key_id=11)


def test_keys_of_one_team_share_the_team_scope():
    assert cache_scope("shop-a", team_id=3, api_key_id=10) == cache_scope("shop-a", team_id=3, api_key_id=11)


def test_a_key_without_a_team_is_its_own_owner():
    assert cache_scope("shop-a", team_id=None, api_key_id=10) == "key:10:shop-a"
    assert cache_scope("shop-a", team_id=None, api_key_id=10) != cache_scope("shop-a", team_id=None, api_key_id=11)


def test_a_team_scope_cannot_be_forged_through_the_name():
    # A key without a team naming its context "3:shop-a" must not land in
    # team 3's "shop-a": the owner prefixes differ ("key:" vs "team:").
    forged = cache_scope("3:shop-a", team_id=None, api_key_id=99)
    assert forged == "key:99:3:shop-a"
    assert forged != cache_scope("shop-a", team_id=3, api_key_id=10)
