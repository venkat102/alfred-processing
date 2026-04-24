"""SSRF-protection for client-supplied LLM base URLs.

The Alfred processing app accepts ``site_config`` from the client at the
WebSocket handshake and uses ``site_config["llm_base_url"]`` verbatim as
the HTTP endpoint for LLM calls. Without validation, a malicious or
compromised client can point Alfred at:

  - ``http://169.254.169.254/...`` — AWS IMDSv1, returns IAM role credentials
  - ``http://localhost:6379/...`` — internal Redis
  - ``http://admin.internal.corp/...`` — any internal service

The JSON response to ``/api/chat`` would fail to parse, but the *request*
is issued — classic Server-Side Request Forgery. This module is the
gate: every outbound LLM URL must pass ``validate_llm_url`` before any
network I/O.

Policy:
  - Scheme must be ``http`` or ``https``. Anything else (``file://``,
    ``ftp://``, ``gopher://``, ...) is rejected.
  - Hostname must resolve via DNS.
  - Resolved IP must NOT be in any private / loopback / link-local /
    ULA range. Those blocks cover: AWS/GCP metadata, localhost,
    RFC1918 private, IPv6 loopback, IPv6 ULA.
  - Two escapes from the private-IP block:
      1. DEBUG=true (``ALFRED_DEBUG_MODE``): private IPs are accepted
         with a single startup log. For local dev where Ollama usually
         runs on localhost or an RFC1918 LAN address.
      2. ``ALFRED_LLM_ALLOWED_HOSTS`` (comma-separated): explicit allow-
         list of hostnames or CIDR blocks. For production self-hosted
         Ollama on a private VPC.

On reject the function raises ``ValueError`` with a specific reason,
and increments ``alfred_ssrf_block_total{reason=...}``. Callers map the
exception to their own error type (e.g. ``OllamaError``) so the SSRF
policy surfaces as "URL rejected by policy" to end-users rather than a
raw stack trace.

Threat model: the client is NOT fully trusted. Authenticated clients
can still be compromised or misconfigured, and this module assumes so.
Infrastructure-level egress rules (VPC security groups, egress firewall)
are the outer defence; this is the application-level inner defence.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

logger = logging.getLogger("alfred.security.url_allowlist")


# IP ranges that must NEVER be reachable from a client-supplied URL in
# production. Built once at module load from the canonical RFC lists.
_BLOCKED_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = tuple(
	ipaddress.ip_network(cidr)
	for cidr in (
		# IPv4
		"127.0.0.0/8",         # loopback
		"169.254.0.0/16",      # link-local — AWS/GCP metadata lives here
		"10.0.0.0/8",          # RFC1918
		"172.16.0.0/12",       # RFC1918
		"192.168.0.0/16",      # RFC1918
		"0.0.0.0/8",            # current-network, unspecified
		"100.64.0.0/10",       # CGNAT
		"224.0.0.0/4",         # multicast
		"240.0.0.0/4",         # reserved / broadcast
		# IPv6
		"::1/128",              # loopback
		"fc00::/7",             # ULA
		"fe80::/10",            # link-local
		"ff00::/8",             # multicast
		"::/128",               # unspecified
	)
)


class SsrfPolicyError(ValueError):
	"""Raised when a URL fails the SSRF policy.

	Subclass of ValueError so existing `except ValueError` blocks still
	catch it, but callers can narrow to this type for dedicated handling.
	Carries a ``reason`` attribute matching the Prometheus counter label
	so operators can correlate rejections.
	"""

	def __init__(self, message: str, reason: str):
		super().__init__(message)
		self.reason = reason


def _allowed_hosts() -> set[str]:
	"""Parse ALFRED_LLM_ALLOWED_HOSTS into a set of (lowercased) entries.

	Entries can be hostnames ("my-ollama.example.com") or CIDRs
	("10.243.88.0/24", "10.243.88.140"). Lookups check both forms.
	"""
	raw = os.environ.get("ALFRED_LLM_ALLOWED_HOSTS", "").strip()
	if not raw:
		return set()
	return {entry.strip().lower() for entry in raw.split(",") if entry.strip()}


def _host_matches_allowlist(host: str, resolved_ip: str, allowed: set[str]) -> bool:
	"""True if (host, resolved_ip) matches any allow-list entry."""
	if not allowed:
		return False
	host_lc = host.lower()
	if host_lc in allowed:
		return True
	# CIDR match on the resolved IP.
	try:
		ip_obj = ipaddress.ip_address(resolved_ip)
	except ValueError:
		return False
	for entry in allowed:
		if "/" in entry:
			try:
				if ip_obj in ipaddress.ip_network(entry, strict=False):
					return True
			except ValueError:
				continue
		else:
			# Bare IP entry (e.g. "10.243.88.140"): compare directly.
			try:
				if ip_obj == ipaddress.ip_address(entry):
					return True
			except ValueError:
				continue
	return False


def _debug_bypass_enabled() -> bool:
	"""True if DEBUG mode has relaxed SSRF protection.

	We read the env directly rather than importing Settings because this
	module sits below the FastAPI app and should not depend on app state.
	"""
	return os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes")


def _record_block(reason: str) -> None:
	"""Increment the SSRF block counter. Best-effort; a broken metrics
	import must not prevent the block from being enforced.
	"""
	try:
		from alfred.obs.metrics import ssrf_block_total
		ssrf_block_total.labels(reason=reason).inc()
	except Exception:  # noqa: BLE001 — metrics best-effort; a broken metrics import must not prevent the block from being enforced
		pass


def _resolve_host(host: str) -> str:
	"""Resolve a hostname to a single IP string. Raises OSError on failure.

	Only the first address-family result is returned; that's acceptable
	for the private-IP check because we block conservatively: if ANY
	resolution lands on a blocked range we reject. Multi-A-record
	round-robin attacks against a host that resolves sometimes-public
	and sometimes-private are out of scope — the allow-list is the tool
	for those.
	"""
	infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
	if not infos:
		raise OSError(f"getaddrinfo returned no results for {host!r}")
	# infos[i] == (family, type, proto, canonname, sockaddr)
	# sockaddr[0] is the IP string.
	return infos[0][4][0]


def validate_llm_url(url: str) -> None:
	"""Validate a client-supplied LLM URL against the SSRF policy.

	Raises ``SsrfPolicyError`` (a ValueError subclass) on reject,
	carrying a ``reason`` attribute. Returns None on accept.

	Args:
		url: The base URL or full URL the LLM client intends to hit.
			Scheme + host + port are inspected; path / query are ignored.

	Raises:
		SsrfPolicyError: with ``reason`` in {bad_scheme, no_host,
			dns_fail, private_ip, host_not_allowed}.
	"""
	if not url or not isinstance(url, str):
		_record_block("bad_scheme")
		raise SsrfPolicyError(f"URL is empty or not a string: {url!r}", reason="bad_scheme")

	parsed = urlparse(url)
	if parsed.scheme.lower() not in ("http", "https"):
		_record_block("bad_scheme")
		raise SsrfPolicyError(
			f"URL scheme {parsed.scheme!r} is not allowed (use http or https): {url!r}",
			reason="bad_scheme",
		)

	host = parsed.hostname
	if not host:
		_record_block("no_host")
		raise SsrfPolicyError(f"URL has no host: {url!r}", reason="no_host")

	# Resolve the host. If this fails, we reject — callers should see a
	# clear "DNS failed" error rather than a later connection timeout.
	try:
		ip_str = _resolve_host(host)
	except OSError as e:
		_record_block("dns_fail")
		raise SsrfPolicyError(
			f"DNS resolution failed for {host!r}: {e}",
			reason="dns_fail",
		) from e

	try:
		ip_obj = ipaddress.ip_address(ip_str)
	except ValueError:
		_record_block("dns_fail")
		raise SsrfPolicyError(
			f"DNS returned non-IP result for {host!r}: {ip_str!r}",
			reason="dns_fail",
		)

	in_blocked = any(ip_obj in net for net in _BLOCKED_NETWORKS)
	if not in_blocked:
		# Public IP — accept.
		return

	# Private / loopback / link-local range. Two escapes:
	allowed = _allowed_hosts()
	if _host_matches_allowlist(host, ip_str, allowed):
		logger.info(
			"SSRF policy: %s (%s) matched ALFRED_LLM_ALLOWED_HOSTS - allowed",
			host, ip_str,
		)
		return

	if _debug_bypass_enabled():
		logger.warning(
			"SSRF policy: DEBUG=true - allowing private/loopback URL %s -> %s. "
			"Set DEBUG=false and configure ALFRED_LLM_ALLOWED_HOSTS for production.",
			host, ip_str,
		)
		return

	_record_block("private_ip")
	raise SsrfPolicyError(
		f"URL {url!r} resolves to private/loopback/metadata IP {ip_str} "
		f"({host}). Add the host to ALFRED_LLM_ALLOWED_HOSTS if this is a "
		f"legitimate internal LLM endpoint, or set DEBUG=true for local "
		f"development.",
		reason="private_ip",
	)
