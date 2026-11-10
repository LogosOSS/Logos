"""Provider-side conversation state is shared across teams, so references to it are refused."""

import pytest
from fastapi import HTTPException

from logos.request_content import reject_stateful_references, stateful_reference_field


@pytest.mark.parametrize("field", ["previous_response_id", "conversation"])
def test_reference_is_rejected(field):
    with pytest.raises(HTTPException) as exc:
        reject_stateful_references({"model": "gpt-5", field: "resp_other_team", "input": "repeat"})
    assert exc.value.status_code == 400
    assert field in exc.value.detail


def test_stateless_request_and_explicit_null_pass():
    reject_stateful_references({"model": "gpt-5", "previous_response_id": None, "input": "hi"})
    assert stateful_reference_field({"model": "gpt-5"}) is None
