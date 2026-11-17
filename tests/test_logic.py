from decimal import Decimal

import pytest

from app.logic import calculate_stats, classify_outcome, parse_amount_inr
from app.worker import _read_rows


@pytest.mark.parametrize(("value", "expected"), [
    ("1 145,70 INR", Decimal("1145.70")),
    ("1\u00a0145,70 INR", Decimal("1145.70")),
    ("₹ 12,500.25", Decimal("12500.25")),
    ("2.345,10 INR", Decimal("2345.10")),
    ("50 000 INR", Decimal("50000")),
    ("6 000 INR", Decimal("6000")),
    ("8 000 INR", Decimal("8000")),
    ("25 000 INR", Decimal("25000")),
    ("5 353 INR", Decimal("5353")),
    ("not an amount", None),
])
def test_parse_amount_inr(value, expected):
    assert parse_amount_inr(value) == expected


def test_classify_outcomes_and_failures():
    assert classify_outcome("Accept the request for processing") == "SUCCESS_POPUP"
    assert classify_outcome("Accepted by another trader") == "TAKEN_BY_OTHER"
    assert classify_outcome(otp_visible=True) == "OTP_REACHED"
    assert classify_outcome("", error_text="button timed out") == "FAIL"
    assert classify_outcome("unrecognized") == "FAIL"


def test_calculate_stats_includes_valid_other_and_failure_breakdown():
    stats = calculate_stats([
        {"outcome": "OTP_REACHED", "popup_shown": 1, "latency_ms": 100},
        {"outcome": "TAKEN_BY_OTHER", "popup_shown": 1, "latency_ms": 300},
        {"outcome": "FAIL", "popup_shown": 0, "latency_ms": 500, "error_text": "timeout"},
    ])
    assert stats == {
        "total_clicks": 3, "popups_shown": 2, "taken_by_other": 1, "otp_reached": 1, "accepted": 0, "limit_reached": 0, "rate_limited": 0, "skipped": 1,
        "failures": 1, "success_rate": 66.67, "avg_latency_ms": 300,
        "p95_latency_ms": 500, "failure_reasons": {"timeout": 1},
    }


class FakeButton:
    def count(self):
        return 1

    def is_visible(self):
        return True


class FakeCell:
    def __init__(self, text):
        self.text = text

    def inner_text(self, timeout=None):
        return self.text


class FakeCells:
    def __init__(self, texts):
        self.cells = [FakeCell(text) for text in texts]

    def all(self):
        return self.cells


class FakeRow:
    def __init__(self, texts):
        self.texts = texts

    def get_by_role(self, *_args, **_kwargs):
        return FakeButton()

    def locator(self, selector):
        assert selector == "th,td"
        return FakeCells(self.texts)


class FakeRowList:
    def all(self):
        return [
            FakeRow(["P-101", "15 Nov", "8 min", "UPI", "1\u00a0145,70 INR"]),
            FakeRow(["P-102", "15 Nov", "8 min", "UPI", "--"]),
        ]


class FakePage:
    def locator(self, selector):
        assert selector == "tr"
        return FakeRowList()


def test_read_rows_uses_mocked_page_and_skips_unparseable_rows():
    assert _read_rows(FakePage()) == [{"row_id": "P-101", "amount": Decimal("1145.70")}]
