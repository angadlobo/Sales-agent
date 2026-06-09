"""Tests for the compliance guardrails (no network / API key required)."""

import pytest

from sales_agent.compliance import (
    ComplianceError,
    Suppression,
    check_email,
    check_phone,
    normalize_phone,
)


def test_normalize_phone_strips_formatting():
    assert normalize_phone("+1 (555) 123-4567") == "+15551234567"


def test_check_email_rejects_invalid():
    sup = Suppression()
    with pytest.raises(ComplianceError):
        check_email("not-an-email", sup)
    with pytest.raises(ComplianceError):
        check_email(None, sup)


def test_check_email_accepts_valid():
    sup = Suppression()
    check_email("owner@example.com", sup)  # should not raise


def test_suppression_blocks_listed_email(tmp_path):
    f = tmp_path / "dnc.txt"
    f.write_text("blocked@example.com\n")
    sup = Suppression(f)
    with pytest.raises(ComplianceError):
        check_email("blocked@example.com", sup)
    check_email("allowed@example.com", sup)  # not on list -> ok


def test_suppression_add_persists(tmp_path):
    f = tmp_path / "dnc.txt"
    f.write_text("")
    sup = Suppression(f)
    sup.add("new@example.com")
    assert Suppression(f).contains("new@example.com")


def test_check_phone_validates_e164():
    sup = Suppression()
    check_phone("+15551234567", sup)  # ok
    with pytest.raises(ComplianceError):
        check_phone("123", sup)
