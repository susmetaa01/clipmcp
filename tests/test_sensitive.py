"""Tests for sensitive.py — sensitive data detection."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

from clipmcp.sensitive import is_sensitive, matched_pattern


class TestTruePositives:
    """Content that SHOULD be flagged as sensitive."""

    def test_openai_api_key(self):
        assert is_sensitive("sk-abc123XYZ789abc123XYZ789abc123XYZ789abc123XYZ789")

    def test_anthropic_api_key(self):
        assert is_sensitive("sk-ant-api03-abc123def456ghi789jkl012mno345pqr678stu")

    def test_generic_pk_prefix(self):
        assert is_sensitive("pk_live_abcdefghijklmnopqrstuvwx")

    def test_aws_access_key(self):
        assert is_sensitive("AKIAIOSFODNN7EXAMPLE")

    def test_jwt_token(self):
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        assert is_sensitive(jwt)

    def test_github_token(self):
        assert is_sensitive("ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZaAbBcCdD")

    def test_slack_token(self):
        assert is_sensitive("xoxb-123456789012-123456789012-abcdefghijklmnopqrstuvwx")

    def test_private_key_header(self):
        assert is_sensitive("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA...")

    def test_password_equals(self):
        assert is_sensitive("password=mysecretpassword123")

    def test_password_colon(self):
        assert is_sensitive("secret: hunter2")

    def test_matched_pattern_returns_name(self):
        name = matched_pattern("sk-abc123XYZ789abc123XYZ789abc123XYZ789abc123XYZ789")
        assert name == "api_key_prefix"


class TestFalsePositives:
    """Content that should NOT be flagged as sensitive."""

    def test_plain_text(self):
        assert not is_sensitive("Hello, world!")

    def test_url(self):
        assert not is_sensitive("https://example.com/path?query=value")

    def test_short_string(self):
        assert not is_sensitive("abc123")

    def test_email(self):
        assert not is_sensitive("susmeta@example.com")

    def test_no_match_returns_none(self):
        assert matched_pattern("just a normal sentence") is None
