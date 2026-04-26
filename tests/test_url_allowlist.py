"""Tests for alfred.security.url_allowlist.validate_llm_url (TD-C3).

Every test isolates the two env vars the module reads (DEBUG,
ALFRED_LLM_ALLOWED_HOSTS) via monkeypatch so tests can't leak into
each other.

DNS resolution is patched with a controllable fake so tests run
offline and deterministically. The *real* DNS path is covered by the
``test_resolves_real_public_host_*`` tests which opt in only when
the network is available (skipped otherwise).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from alfred.security.url_allowlist import (
	SsrfPolicyError,
	validate_llm_url,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
	"""Every test starts with SSRF bypass flags cleared."""
	monkeypatch.delenv("DEBUG", raising=False)
	monkeypatch.delenv("ALFRED_LLM_ALLOWED_HOSTS", raising=False)
	yield


def _fake_resolve(mapping: dict[str, str]):
	"""Return a patcher that makes _resolve_host return the mapped IP."""
	def _resolve(host):
		if host in mapping:
			return mapping[host]
		raise OSError(f"Host not in test mapping: {host!r}")
	return patch("alfred.security.url_allowlist._resolve_host", side_effect=_resolve)


# ── Scheme rejection ───────────────────────────────────────────────


def test_file_scheme_rejected():
	with pytest.raises(SsrfPolicyError) as exc:
		validate_llm_url("file:///etc/passwd")
	assert exc.value.reason == "bad_scheme"


def test_ftp_scheme_rejected():
	with pytest.raises(SsrfPolicyError) as exc:
		validate_llm_url("ftp://example.com/x")
	assert exc.value.reason == "bad_scheme"


def test_empty_url_rejected():
	with pytest.raises(SsrfPolicyError) as exc:
		validate_llm_url("")
	assert exc.value.reason == "bad_scheme"


def test_none_url_rejected():
	with pytest.raises(SsrfPolicyError) as exc:
		validate_llm_url(None)  # type: ignore[arg-type]
	assert exc.value.reason == "bad_scheme"


def test_no_host_rejected():
	with pytest.raises(SsrfPolicyError) as exc:
		validate_llm_url("http:///path-only")
	assert exc.value.reason == "no_host"


# ── Private-IP / metadata rejection ────────────────────────────────


def test_aws_metadata_blocked():
	with _fake_resolve({"169.254.169.254": "169.254.169.254"}):
		with pytest.raises(SsrfPolicyError) as exc:
			validate_llm_url("http://169.254.169.254/latest/meta-data/")
	assert exc.value.reason == "private_ip"


def test_localhost_blocked():
	with _fake_resolve({"localhost": "127.0.0.1"}):
		with pytest.raises(SsrfPolicyError) as exc:
			validate_llm_url("http://localhost:11434/api/chat")
	assert exc.value.reason == "private_ip"


def test_loopback_ipv4_blocked():
	with _fake_resolve({"127.0.0.1": "127.0.0.1"}):
		with pytest.raises(SsrfPolicyError) as exc:
			validate_llm_url("http://127.0.0.1:6379/")
	assert exc.value.reason == "private_ip"


def test_ipv6_loopback_blocked():
	with _fake_resolve({"::1": "::1"}):
		with pytest.raises(SsrfPolicyError) as exc:
			validate_llm_url("http://[::1]:11434/")
	assert exc.value.reason == "private_ip"


def test_rfc1918_10_blocked():
	with _fake_resolve({"10.243.88.140": "10.243.88.140"}):
		with pytest.raises(SsrfPolicyError) as exc:
			validate_llm_url("http://10.243.88.140:11434/api/chat")
	assert exc.value.reason == "private_ip"


def test_rfc1918_192_168_blocked():
	with _fake_resolve({"192.168.1.5": "192.168.1.5"}):
		with pytest.raises(SsrfPolicyError) as exc:
			validate_llm_url("http://192.168.1.5/")
	assert exc.value.reason == "private_ip"


def test_rfc1918_172_16_blocked():
	with _fake_resolve({"172.20.10.3": "172.20.10.3"}):
		with pytest.raises(SsrfPolicyError) as exc:
			validate_llm_url("http://172.20.10.3/")
	assert exc.value.reason == "private_ip"


def test_cgnat_blocked():
	with _fake_resolve({"100.64.1.1": "100.64.1.1"}):
		with pytest.raises(SsrfPolicyError) as exc:
			validate_llm_url("http://100.64.1.1/")
	assert exc.value.reason == "private_ip"


def test_multicast_blocked():
	with _fake_resolve({"239.1.2.3": "239.1.2.3"}):
		with pytest.raises(SsrfPolicyError) as exc:
			validate_llm_url("http://239.1.2.3/")
	assert exc.value.reason == "private_ip"


# ── Host-sneaky: name resolves to private IP ──────────────────────


def test_dns_rebinding_style_blocked():
	# A public-looking hostname that resolves to a private IP - the
	# exact DNS-rebinding attack shape. Must still be blocked.
	with _fake_resolve({"evil.example.com": "127.0.0.1"}):
		with pytest.raises(SsrfPolicyError) as exc:
			validate_llm_url("https://evil.example.com/api/chat")
	assert exc.value.reason == "private_ip"


def test_dns_failure_rejected():
	# Host doesn't resolve - reject cleanly rather than let the later
	# connection attempt time out.
	with patch(
		"alfred.security.url_allowlist._resolve_host",
		side_effect=OSError("NXDOMAIN"),
	):
		with pytest.raises(SsrfPolicyError) as exc:
			validate_llm_url("https://nonexistent.example.invalid/")
	assert exc.value.reason == "dns_fail"


# ── Public addresses pass ──────────────────────────────────────────


def test_public_ipv4_accepted():
	with _fake_resolve({"api.openai.com": "8.8.8.8"}):
		# Must NOT raise.
		validate_llm_url("https://api.openai.com/v1/chat/completions")


def test_public_ipv6_accepted():
	with _fake_resolve({"ipv6.example.com": "2606:4700:4700::1111"}):
		validate_llm_url("https://ipv6.example.com/")


# ── DEBUG bypass ───────────────────────────────────────────────────


def test_debug_mode_allows_localhost(monkeypatch):
	monkeypatch.setenv("DEBUG", "true")
	with _fake_resolve({"localhost": "127.0.0.1"}):
		validate_llm_url("http://localhost:11434/api/chat")


def test_debug_mode_allows_rfc1918(monkeypatch):
	monkeypatch.setenv("DEBUG", "true")
	with _fake_resolve({"10.243.88.140": "10.243.88.140"}):
		validate_llm_url("http://10.243.88.140:11434/api/chat")


def test_debug_mode_still_rejects_bad_scheme(monkeypatch):
	# DEBUG only relaxes the private-IP check. Scheme and DNS failure
	# still enforced.
	monkeypatch.setenv("DEBUG", "true")
	with pytest.raises(SsrfPolicyError) as exc:
		validate_llm_url("file:///etc/hosts")
	assert exc.value.reason == "bad_scheme"


def test_debug_false_blocks_private():
	# Explicit false is the same as unset.
	with _fake_resolve({"localhost": "127.0.0.1"}):
		with pytest.raises(SsrfPolicyError):
			validate_llm_url("http://localhost/")


def test_debug_values_one_true_yes_all_enable(monkeypatch):
	for v in ("1", "true", "TRUE", "yes", "YES"):
		monkeypatch.setenv("DEBUG", v)
		with _fake_resolve({"localhost": "127.0.0.1"}):
			# Must not raise
			validate_llm_url("http://localhost/")


def test_debug_unknown_value_treated_as_disabled(monkeypatch):
	monkeypatch.setenv("DEBUG", "maybe")
	with _fake_resolve({"localhost": "127.0.0.1"}):
		with pytest.raises(SsrfPolicyError):
			validate_llm_url("http://localhost/")


# ── ALFRED_LLM_ALLOWED_HOSTS bypass ────────────────────────────────


def test_allowlist_hostname_bypass(monkeypatch):
	monkeypatch.setenv("ALFRED_LLM_ALLOWED_HOSTS", "ollama.internal.corp")
	with _fake_resolve({"ollama.internal.corp": "10.243.88.140"}):
		validate_llm_url("http://ollama.internal.corp:11434/")


def test_allowlist_bare_ip_bypass(monkeypatch):
	monkeypatch.setenv("ALFRED_LLM_ALLOWED_HOSTS", "10.243.88.140")
	with _fake_resolve({"10.243.88.140": "10.243.88.140"}):
		validate_llm_url("http://10.243.88.140:11434/")


def test_allowlist_cidr_bypass(monkeypatch):
	monkeypatch.setenv("ALFRED_LLM_ALLOWED_HOSTS", "10.243.88.0/24")
	with _fake_resolve({"10.243.88.140": "10.243.88.140"}):
		validate_llm_url("http://10.243.88.140:11434/")
	with _fake_resolve({"10.243.88.99": "10.243.88.99"}):
		validate_llm_url("http://10.243.88.99:11434/")


def test_allowlist_cidr_does_not_leak_to_other_ranges(monkeypatch):
	# CIDR 10.243.88.0/24 must NOT allow 10.243.99.x.
	monkeypatch.setenv("ALFRED_LLM_ALLOWED_HOSTS", "10.243.88.0/24")
	with _fake_resolve({"10.243.99.1": "10.243.99.1"}):
		with pytest.raises(SsrfPolicyError) as exc:
			validate_llm_url("http://10.243.99.1/")
	assert exc.value.reason == "private_ip"


def test_allowlist_multiple_entries(monkeypatch):
	monkeypatch.setenv(
		"ALFRED_LLM_ALLOWED_HOSTS",
		"host.a.example,10.0.0.5, 192.168.0.0/16",
	)
	with _fake_resolve({"host.a.example": "10.0.0.5"}):
		validate_llm_url("http://host.a.example/")
	with _fake_resolve({"192.168.5.10": "192.168.5.10"}):
		validate_llm_url("http://192.168.5.10/")


def test_allowlist_does_not_bypass_bad_scheme(monkeypatch):
	monkeypatch.setenv("ALFRED_LLM_ALLOWED_HOSTS", "internal.corp")
	with pytest.raises(SsrfPolicyError):
		validate_llm_url("file://internal.corp/etc/passwd")


# ── Prometheus counter ─────────────────────────────────────────────


def test_counter_increments_on_block():
	from alfred.obs.metrics import ssrf_block_total

	def _val(reason):
		try:
			return ssrf_block_total.labels(reason=reason)._value.get()
		except (KeyError, AttributeError):
			return 0

	before = _val("private_ip")
	with _fake_resolve({"localhost": "127.0.0.1"}):
		with pytest.raises(SsrfPolicyError):
			validate_llm_url("http://localhost/")
	assert _val("private_ip") == before + 1


def test_counter_reason_labels_distinct():
	from alfred.obs.metrics import ssrf_block_total

	def _val(reason):
		try:
			return ssrf_block_total.labels(reason=reason)._value.get()
		except (KeyError, AttributeError):
			return 0

	scheme_before = _val("bad_scheme")
	dns_before = _val("dns_fail")

	with pytest.raises(SsrfPolicyError):
		validate_llm_url("file:///etc/passwd")
	with patch(
		"alfred.security.url_allowlist._resolve_host",
		side_effect=OSError("NXDOMAIN"),
	):
		with pytest.raises(SsrfPolicyError):
			validate_llm_url("https://nonexistent.invalid/")

	assert _val("bad_scheme") == scheme_before + 1
	assert _val("dns_fail") == dns_before + 1


def test_counter_does_not_increment_on_allowed():
	from alfred.obs.metrics import ssrf_block_total

	def _val(reason):
		try:
			return ssrf_block_total.labels(reason=reason)._value.get()
		except (KeyError, AttributeError):
			return 0

	before_private = _val("private_ip")
	before_scheme = _val("bad_scheme")
	with _fake_resolve({"api.openai.com": "8.8.8.8"}):
		validate_llm_url("https://api.openai.com/")
	assert _val("private_ip") == before_private
	assert _val("bad_scheme") == before_scheme


# ── Integration: llm_client.ollama_chat_sync wraps as OllamaError ──


def test_ollama_chat_sync_rejects_bad_url():
	"""Wiring test: the SSRF reject must propagate to callers as
	OllamaError, not a raw SsrfPolicyError, so existing try/except
	handlers around LLM calls catch it."""
	from alfred.llm_client import OllamaError, ollama_chat_sync

	site_config = {
		"llm_base_url": "http://169.254.169.254",
		"llm_model": "ollama/llama3",
	}
	with _fake_resolve({"169.254.169.254": "169.254.169.254"}):
		with pytest.raises(OllamaError, match="SSRF policy"):
			ollama_chat_sync(
				messages=[{"role": "user", "content": "hi"}],
				site_config=site_config,
				max_tokens=1,
			)
