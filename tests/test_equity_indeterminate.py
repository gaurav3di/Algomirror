"""
The single most safety-critical decision in the equity order engine.

equity_is_indeterminate_response decides whether a failed broker call may be
re-sent. Get it wrong in the permissive direction and the same stock is bought
twice across five live accounts.

The rule is enumerate-the-safe-cases and default to indeterminate, so an error
type nobody anticipated fails safe instead of being retried. These tests pin
that, including the two cases that were previously retried by mistake
(json_error and unknown_error), where the response arrived but could not be
trusted and the order may well already be at the broker.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def is_indeterminate(response):
    """
    Import lazily, inside the call rather than at module import.

    config.py resolves DATABASE_URL at import time, so whichever test module
    imports the app package first fixes the database path for the whole session.
    Importing here keeps this module out of that race: by the time a test runs,
    any module that needs its own database has already set the environment and
    imported the app on its own terms.
    """
    from app.models import equity_is_indeterminate_response
    return equity_is_indeterminate_response(response)


class TestDefiniteOutcomesAreNotIndeterminate:
    """These got a real answer, so re-sending is safe."""

    def test_success(self):
        assert is_indeterminate({"status": "success", "orderid": "251127000001"}) is False

    def test_api_error_is_a_real_refusal(self):
        assert is_indeterminate({"status": "error", "error_type": "api_error",
                                 "message": "insufficient funds"}) is False

    @pytest.mark.parametrize("code", [400, 401, 403, 404, 422, 499])
    def test_http_4xx_is_a_real_refusal(self, code):
        assert is_indeterminate({"status": "error", "error_type": "http_error",
                                 "code": code}) is False


class TestUnknownOutcomesAreIndeterminate:
    """These may or may not have reached the broker. Never re-send."""

    def test_timeout(self):
        assert is_indeterminate({"status": "error", "error_type": "timeout_error"}) is True

    def test_connection_failure(self):
        assert is_indeterminate({"status": "error", "error_type": "connection_error"}) is True

    def test_json_error_was_previously_retried(self):
        # The response arrived but could not be parsed. The order may be live.
        assert is_indeterminate({"status": "error", "error_type": "json_error"}) is True

    def test_unknown_error_was_previously_retried(self):
        assert is_indeterminate({"status": "error", "error_type": "unknown_error"}) is True

    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    def test_http_5xx_is_ambiguous(self, code):
        assert is_indeterminate({"status": "error", "error_type": "http_error",
                                 "code": code}) is True

    def test_http_error_with_no_readable_code(self):
        assert is_indeterminate({"status": "error", "error_type": "http_error"}) is True
        assert is_indeterminate({"status": "error", "error_type": "http_error",
                                 "code": "nonsense"}) is True

    def test_an_error_type_this_code_has_never_seen(self):
        # The whole point of defaulting to indeterminate: a new SDK error type
        # must fail safe rather than be re-sent.
        assert is_indeterminate({"status": "error", "error_type": "brand_new_thing"}) is True

    def test_error_with_no_error_type_at_all(self):
        assert is_indeterminate({"status": "error", "message": "something went wrong"}) is True

    def test_no_response_object(self):
        assert is_indeterminate(None) is True

    def test_a_non_dict_response(self):
        assert is_indeterminate("boom") is True
        assert is_indeterminate(["boom"]) is True


class TestTheRuleIsEnumerateSafeNotEnumerateUnsafe:
    def test_an_empty_dict_is_indeterminate(self):
        # No status, no error type. We know nothing, so we assume nothing.
        assert is_indeterminate({}) is True


class TestGTTUnsupportedIsADefiniteRefusal:
    """
    501 Not Implemented is how a broker without GTT support answers.

    It is a 5xx, but unlike 500 or 503 it is not ambiguous: the endpoint does
    not exist, so the order was definitely not placed. Classifying it as
    indeterminate would strand every GTT attempt against Upstox, Fyers and
    Angel One instead of reporting cleanly that the broker cannot do it.
    """

    def test_501_is_definite(self):
        assert is_indeterminate({"status": "error", "error_type": "http_error",
                                 "code": 501,
                                 "message": "GTT orders are not supported for broker"}) is False

    @pytest.mark.parametrize("code", [500, 502, 503, 504])
    def test_other_5xx_remain_ambiguous(self, code):
        assert is_indeterminate({"status": "error", "error_type": "http_error",
                                 "code": code}) is True
