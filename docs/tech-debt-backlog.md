# Technical Debt Backlog — alfred-processing

Generated 2026-04-24 from a CTO-level code audit. 31 tasks spanning security,
architecture, DevOps, performance, and resilience. Each task is self-contained
— a developer should be able to pick one up, complete it, and validate it
without reading the rest.

Tasks are numbered `TD-<severity><n>` (e.g. `TD-C1` = Critical 1, `TD-H3` =
High 3) so they can be referenced from commits and PRs.

**Recommended ordering:** take Critical tasks in numeric order first, then the
Week-2/3/4 plan at the bottom. Do not batch multiple Critical fixes into one PR
— each should be small, reviewable, and independently revertable.

---

## How to use this document

Every task follows the same template:

- **Severity**: Critical / High / Medium / Low (operational risk)
- **Area**: Security / Architecture / DevOps / Performance / Resilience / Data
- **Effort**: S (< 4 hrs), M (1–3 days), L (> 3 days)
- **Why it matters**: the concrete production risk
- **Current state**: what's wrong, with file:line evidence
- **Desired state**: what "done" looks like
- **Implementation steps**: concrete, numbered
- **Acceptance criteria**: checkbox list — every box must be checked to close
- **Testing**: exact commands / assertions to validate
- **Out of scope**: what NOT to touch in this PR

If a step is ambiguous, surface it in the PR description rather than guessing.

---

# CRITICAL (Week 1)

These are exploitable or actively degrading production. Every Critical must
ship behind a feature-flag-free PR, reviewed by at least one other engineer,
with passing tests.

---

## TD-C1 — Use constant-time comparison for API keys

**Severity:** Critical
**Area:** Security
**Effort:** S

### Why it matters
Python's `!=` short-circuits on the first differing byte. An attacker with
network access can brute-force the API key one character at a time by
measuring request latency, reducing a 32-byte key from infeasible to
minutes. The API key gates everything — prompt submission, MCP tool
dispatch, admin endpoints — so a brute-forced key grants full impersonation
of any site.

### Current state
- `alfred/middleware/auth.py:47` — `if api_key != expected_key:`
- `alfred/api/websocket.py:453` — `if api_key != expected_key:`

Both compare the client-supplied key to the server secret using normal
string inequality.

### Desired state
Every API-key comparison uses `hmac.compare_digest`, which runs in time
proportional to the *length* of the input, not the position of the first
mismatch.

### Implementation steps
1. Add `import hmac` to both files.
2. Replace each `if api_key != expected_key:` with:
   ```python
   if not hmac.compare_digest(api_key.encode("utf-8"), expected_key.encode("utf-8")):
   ```
3. Grep for any other string-compare on secret-equivalents (`ADMIN_SERVICE_KEY`,
   JWT internals). Apply the same fix.

### Acceptance criteria
- [ ] `grep -rn "!= expected_key\|!= settings.API_SECRET_KEY" alfred/` returns no hits.
- [ ] `grep -rn "hmac.compare_digest" alfred/` shows the three new usages.
- [ ] Existing auth tests (`tests/test_websocket_auth*`, `tests/test_routes*`) pass.
- [ ] One new test: `test_auth_rejects_wrong_key_in_constant_time` — asserts
      behavior, not timing (timing tests are flaky; just verify correctness).

### Testing
```bash
pytest tests/ -k "auth or websocket" -q
```

### Out of scope
- Key rotation (→ TD-C2).
- JWT signing key separation (→ TD-C2).

---

## TD-C2 — Separate API-key secret from JWT signing key

**Severity:** Critical
**Area:** Security
**Effort:** M

### Why it matters
`API_SECRET_KEY` is used for two distinct cryptographic purposes: (1)
authenticating REST/WebSocket requests, (2) HMAC-signing JWTs. If either
channel leaks the key (log spill, developer `.env` mishandling, memory
dump), both are compromised with no path to rotate one without breaking the
other.

### Current state
- `alfred/config.py:20` defines `API_SECRET_KEY`.
- `alfred/api/websocket.py:460` — `verify_jwt_token(jwt_token, expected_key)`
  where `expected_key = settings.API_SECRET_KEY`.
- `alfred/middleware/auth.py::verify_jwt_token` accepts the secret as a
  parameter — the call site decides what it is.

### Desired state
Two independent secrets:
- `API_SECRET_KEY` — static bearer token, REST + WebSocket handshake only.
- `JWT_SIGNING_KEY` — HMAC key for issuing/verifying JWTs.

Startup fails fast if they are identical or either is shorter than 32 bytes.

### Implementation steps
1. Add `JWT_SIGNING_KEY: str` to `alfred/config.py::Settings` with no default.
2. In `get_settings()` (or a new `validate()` call), raise `ValueError` if
   `JWT_SIGNING_KEY == API_SECRET_KEY` or `len(JWT_SIGNING_KEY) < 32`.
3. Update `alfred/api/websocket.py:460` to pass `settings.JWT_SIGNING_KEY`.
4. Update `alfred/middleware/auth.py::create_jwt_token` callers (tests
   include) to use the new key.
5. Update `.env.example` with the new var and `python3 -c` one-liner to
   generate it.
6. Update `docs/SETUP.md` with rotation procedure (set new key, restart,
   old JWTs invalidate — acceptable because `exp` is 24h).

### Acceptance criteria
- [ ] Settings raises `ValueError` if the two keys match or either is < 32 chars.
- [ ] `.env.example` documents both keys.
- [ ] `grep -n 'API_SECRET_KEY' alfred/middleware/auth.py` shows no JWT-signing usage.
- [ ] Existing JWT tests pass with the new signing key.
- [ ] New test: `test_settings_rejects_identical_keys`.
- [ ] New test: `test_jwt_signed_with_jwt_key_rejected_with_api_key`.

### Testing
```bash
pytest tests/ -k "auth or config" -q
```

### Out of scope
- Moving to asymmetric (RS256) — future task, tracked at TD-M1.
- Key rotation automation.

---

## TD-C3 — SSRF protection on client-supplied `llm_base_url`

**Severity:** Critical
**Area:** Security
**Effort:** M

### Why it matters
The WebSocket handshake accepts a `site_config` dict from the client and
passes it verbatim into `llm_client._resolve_ollama_config`. The client
controls `llm_base_url`. A malicious or compromised client can point the
processing app at:
- `http://169.254.169.254/latest/meta-data/` (AWS IMDSv1) → IAM role credentials
- `http://localhost:6379/` → internal Redis
- `http://internal-admin.corp.lan/` → internal services

The response fails to JSON-parse (not a `/api/chat` response), but the
*request is issued* — classic SSRF. In cloud deployments this is typically
step 1 of credential theft → lateral movement.

### Current state
- `alfred/llm_client.py:44-48` and `:64-68`:
  ```python
  base_url = site_config.get("llm_base_url") or os.environ.get(...) or "http://localhost:11434"
  ```
- `alfred/llm_client.py:138`:
  ```python
  url = f"{base_url.rstrip('/')}/api/chat"
  ```
- `urllib.request.urlopen(req, timeout=timeout)` at `:160` — no URL validation.

### Desired state
Every outbound LLM URL passes a scheme + host + IP validation gate before a
request is issued. Violations log a `WARN` with the offending URL and
site_id, increment a counter (`alfred_ssrf_block_total`), and raise
`OllamaError("URL rejected by SSRF policy")`.

### Implementation steps
1. Create `alfred/security/url_allowlist.py` exposing `validate_llm_url(url: str) -> None`.
2. Logic:
   - Parse via `urllib.parse.urlparse`.
   - Scheme must be `http` or `https`. No `file://`, `ftp://`, etc.
   - Host must resolve (one `socket.getaddrinfo` call, cached).
   - Resolved IP must NOT be in: `127.0.0.0/8`, `169.254.0.0/16` (link-local +
     cloud metadata), `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`,
     `::1/128`, `fc00::/7`. Use `ipaddress.ip_network`.
   - Optional env `ALFRED_LLM_ALLOWED_HOSTS` — comma-separated allow-list of
     hostnames or CIDRs that bypass the private-IP block (for self-hosted
     Ollama on the same VPC). Log when this bypass fires.
3. Call `validate_llm_url(base_url)` at the top of `ollama_chat_sync`.
4. Add `llm_probe` in `pipeline.py:820+` to also validate the URL before
   probing.
5. Add Prometheus counter `alfred_ssrf_block_total{reason=...}`.

### Acceptance criteria
- [ ] `validate_llm_url("http://169.254.169.254/...")` raises.
- [ ] `validate_llm_url("http://127.0.0.1/...")` raises.
- [ ] `validate_llm_url("file:///etc/passwd")` raises.
- [ ] `validate_llm_url("https://api.openai.com/v1/chat/completions")` passes.
- [ ] With `ALFRED_LLM_ALLOWED_HOSTS=10.243.88.140`, that host passes despite
      being in RFC1918.
- [ ] `ollama_chat_sync` raises `OllamaError` (not a stack trace) when blocked.
- [ ] New tests under `tests/test_url_allowlist.py` cover 10+ cases.
- [ ] Metric `alfred_ssrf_block_total` increments on block.

### Testing
```bash
pytest tests/test_url_allowlist.py -v
# Integration: attempt handshake with llm_base_url=http://localhost/ — connection should succeed
# but ollama_chat should raise OllamaError("URL rejected by SSRF policy") before any network I/O.
```

### Out of scope
- Egress firewall rules (infrastructure).
- Rate-limiting DNS lookups (if abuse surfaces, follow-up task).

---

## TD-C4 — Set up CI/CD: GitHub Actions workflow

**Severity:** Critical
**Area:** DevOps
**Effort:** M

### Why it matters
15,837 SLOC of Python with 964 tests and NOTHING enforces them on PR.
Ship-gate is "did the developer remember to run pytest". Supply-chain
attacks (the only realistic vector for LLM apps in 2026) are invisible
without automated dependency scanning.

### Current state
- No `.github/workflows/` directory.
- `pyproject.toml:[project.optional-dependencies].dev` includes `pytest`,
  `ruff`, but neither is run in CI.
- No Docker image built on merge.

### Desired state
Every PR to `main` runs: ruff (lint + format check), pytest (full suite),
`pip-audit` (CVE scan), Docker build (smoke). Required-status checks block
merge if any fails. `main` pushes also publish a Docker image to the
registry.

### Implementation steps
1. Create `.github/workflows/ci.yml`:
   ```yaml
   name: CI
   on: [pull_request, push]
   jobs:
     lint:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - uses: actions/setup-python@v5
           with: { python-version: "3.11" }
         - run: pip install -e ".[dev]"
         - run: ruff check alfred/ tests/
         - run: ruff format --check alfred/ tests/
     test:
       runs-on: ubuntu-latest
       services:
         redis:
           image: redis:7-alpine
           ports: ["6379:6379"]
       steps:
         - uses: actions/checkout@v4
         - uses: actions/setup-python@v5
           with: { python-version: "3.11" }
         - run: pip install -e ".[dev]"
         - run: pytest -q --maxfail=3 --cov=alfred --cov-report=xml
         - uses: codecov/codecov-action@v4
           with: { files: coverage.xml }
     security:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - uses: actions/setup-python@v5
           with: { python-version: "3.11" }
         - run: pip install pip-audit
         - run: pip-audit --desc
     docker:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - uses: docker/setup-buildx-action@v3
         - uses: docker/build-push-action@v5
           with: { context: ., push: false }
   ```
2. Create `.github/workflows/release.yml` (push-to-main only): build + push
   Docker image to `ghcr.io/<org>/alfred-processing:${{ github.sha }}` and
   `:latest`.
3. Enable branch protection on `main`: require `lint`, `test`, `security`,
   `docker` to pass, require 1 reviewer, dismiss stale reviews.

### Acceptance criteria
- [ ] PR that breaks ruff fails CI with a clear error.
- [ ] PR that breaks a test fails CI.
- [ ] PR that introduces a known-CVE dependency fails CI.
- [ ] `main` branch shows branch-protection rules in GitHub UI.
- [ ] Coverage report visible on Codecov.
- [ ] A successful merge to `main` publishes a Docker image tagged with the commit SHA.

### Testing
Open a PR that deliberately breaks (a) ruff (b) a test (c) adds `pycrypto==2.6.1`
(known CVE). Verify each check fails with actionable output.

### Out of scope
- Deployment automation (that's a separate infra task).
- Performance benchmarks in CI (already exists via `benchmarks/`).

---

## TD-C5 — Fix CORS configuration: reject `*` origin with credentials

**Severity:** Critical
**Area:** Security
**Effort:** S

### Why it matters
`allow_origins=["*"]` combined with `allow_credentials=True` is invalid per
the CORS spec — browsers reject credentialed requests when origin is `*`.
Either this is deliberately broken (masking security intent), or the
operator intends it to "just work" and will set a real origin list later —
at which point credentials start flowing to ANY origin in that list with
all methods and headers wide open.

### Current state
`alfred/main.py:97-104`:
```python
origins = settings.ALLOWED_ORIGINS.split(",") if settings.ALLOWED_ORIGINS != "*" else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### Desired state
- Startup fails if `ALLOWED_ORIGINS=*` AND any credentialed path is exposed.
- `allow_methods` and `allow_headers` explicitly listed — no `*`.

### Implementation steps
1. Replace the block with:
   ```python
   if settings.ALLOWED_ORIGINS.strip() == "*":
       raise ValueError(
           "ALLOWED_ORIGINS=* is not allowed; set an explicit comma-separated "
           "list (e.g. https://client1.example.com,https://client2.example.com). "
           "For localhost dev use ALLOWED_ORIGINS=http://localhost:3000"
       )
   origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]
   app.add_middleware(
       CORSMiddleware,
       allow_origins=origins,
       allow_credentials=True,
       allow_methods=["GET", "POST", "OPTIONS"],
       allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
   )
   ```
2. Update `.env.example` with a real dev default (`http://localhost:3000` or
   the client app's dev URL).
3. Update `docker-compose.yml` to set `ALLOWED_ORIGINS` explicitly.

### Acceptance criteria
- [ ] Starting the app with `ALLOWED_ORIGINS=*` raises `ValueError` at boot.
- [ ] Starting with a comma-separated list succeeds; preflight from an
      origin NOT in the list returns a browser-rejectable response.
- [ ] `allow_methods` in the running app excludes `PUT`, `DELETE`, `PATCH`.
- [ ] New test: `test_cors_star_origin_rejected_at_startup`.
- [ ] `docs/SETUP.md` updated to document the new requirement.

### Testing
```bash
# Boot with * — must fail
ALLOWED_ORIGINS='*' python -m alfred.main
# Boot with real list — must succeed
ALLOWED_ORIGINS='https://example.com' python -m alfred.main
```

### Out of scope
- CSRF tokens (WebSocket doesn't need them; REST endpoints can add later).

---

## TD-C6 — Rate-limit the WebSocket `prompt` handler

**Severity:** Critical
**Area:** Security / Cost control
**Effort:** S

### Why it matters
`check_rate_limit` exists and is wired into REST `/tasks` but NOT the
WebSocket `type:"prompt"` flow, which is the primary pipeline trigger. A
compromised or misbehaving site can submit unlimited prompts via
WebSocket. Each prompt costs 2–3 LLM calls + N MCP tool calls. Uncapped
LLM spend by a single tenant is OWASP LLM Top-10 category LLM04 (Model
Denial of Service).

### Current state
- `alfred/middleware/rate_limit.py::check_rate_limit` — implemented, unused outside REST.
- `alfred/api/websocket.py:576` — `type:"prompt"` handler has no rate-limit call.
- Only guard is "one pipeline at a time per conversation" at
  `websocket.py:601-613`. A user opens N conversations → N parallel prompts.

### Desired state
Every `type:"prompt"` message passes through `check_rate_limit(site_id, user)`
before the pipeline is spawned. Exceeded limit returns a `type:"error"`
frame with `code:"RATE_LIMITED"` and `retry_after` seconds. Prometheus
counter `alfred_rate_limit_block_total` increments on block.

### Implementation steps
1. In `alfred/api/websocket.py::handle_prompt` (or equivalent — the `if
   msg_type == "prompt":` block at line 576), inject just before the
   "concurrent pipelines" check:
   ```python
   redis = websocket.app.state.redis
   max_per_hour = int(conn.site_config.get("max_tasks_per_user_per_hour", 20))
   allowed, remaining, retry_after = await check_rate_limit(
       redis, conn.site_id, conn.user, max_per_hour=max_per_hour,
   )
   if not allowed:
       await websocket.send_json({
           "msg_id": str(uuid.uuid4()),
           "type": "error",
           "data": {
               "error": f"Rate limit exceeded. Retry in {retry_after}s.",
               "code": "RATE_LIMITED",
               "retry_after": retry_after,
               "remaining": remaining,
           },
       })
       return
   ```
2. Add a counter in `alfred/obs/metrics.py`:
   ```python
   rate_limit_block_total = Counter(
       "alfred_rate_limit_block_total",
       "WebSocket prompts blocked by rate limit.",
       labelnames=("site_id",),
   )
   ```
3. Increment it inside `check_rate_limit` on the blocked path.

### Acceptance criteria
- [ ] Sending 21 prompts in an hour with default limit 20 returns RATE_LIMITED on the 21st.
- [ ] `retry_after` is ≤ 3600 and decreases on subsequent blocked attempts.
- [ ] The pipeline does NOT spawn when blocked (verify via absence of
      `alfred_pipeline_phase_duration_seconds` ticks for that user).
- [ ] New test: `test_websocket_rate_limited_after_threshold`.
- [ ] New test: `test_websocket_rate_limit_respects_site_config_override`.

### Testing
```bash
pytest tests/ -k rate_limit -v
```

### Out of scope
- Token-budget rate limiting (see TD-H10 follow-up).
- Per-site global quotas.

---

## TD-C7 — Replace hardcoded DEBUG logging with env-controlled log level

**Severity:** Critical
**Area:** Security / Cost
**Effort:** S

### Why it matters
`main.py:13-22` sets root logger to DEBUG at import time, then explicitly
bumps all `alfred.*` loggers to DEBUG. In production:
- Log volume explodes (CloudWatch / Loki bills).
- Prompts (often containing customer PII, invoice amounts, user emails)
  are written to stdout.
- `site_config` contents — which may include the client's LLM API key —
  land in logs.

### Current state
```python
logging.basicConfig(level=logging.DEBUG, format="...", stream=sys.stdout)
logging.getLogger("alfred").setLevel(logging.DEBUG)
```

### Desired state
- Log level controlled by `LOG_LEVEL` env var, default `INFO`.
- `DEBUG` only in dev (`DEBUG=true` in `.env`).
- A formatter that redacts known-sensitive fields:
  `llm_api_key`, `api_key`, `jwt_token`, `password`, `secret`, `token`.

### Implementation steps
1. Add `LOG_LEVEL: str = "INFO"` to `Settings` (`alfred/config.py`).
2. In `alfred/main.py`, compute level from settings:
   ```python
   level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
   logging.basicConfig(level=level, format="...", stream=sys.stdout)
   logging.getLogger("alfred").setLevel(level)
   ```
3. Create `alfred/obs/log_redaction.py::RedactingFormatter` — subclass of
   `logging.Formatter` that walks `record.args` and `record.msg` for the
   sensitive keys and replaces values with `***REDACTED***`.
4. Attach the formatter to the root handler.
5. Add a test: log a dict containing `{"llm_api_key": "sk-xyz"}`, assert the
   emitted record contains `***REDACTED***` and not `sk-xyz`.

### Acceptance criteria
- [ ] `LOG_LEVEL=INFO` (default) — no DEBUG lines in output.
- [ ] `LOG_LEVEL=DEBUG` — DEBUG lines visible.
- [ ] Logging `{"llm_api_key": "sk-xyz"}` produces `***REDACTED***`.
- [ ] Logging a prompt string does NOT redact it (don't over-redact).
- [ ] `.env.example` documents `LOG_LEVEL`.
- [ ] New tests under `tests/test_log_redaction.py`.

### Testing
```bash
LOG_LEVEL=INFO python -m alfred.main  # observe stdout — no DEBUG
LOG_LEVEL=DEBUG python -m alfred.main  # observe stdout — DEBUG visible
pytest tests/test_log_redaction.py -v
```

### Out of scope
- Structured logging with structlog (→ TD-M3).

---

# HIGH (Weeks 2–3)

---

## TD-H1 — Decompose the `_phase_post_crew` god-method

**Severity:** High
**Area:** Architecture
**Effort:** M

### Why it matters
351 lines in a single async method (`alfred/api/pipeline.py:1928`). Handles
drift detection, rescue, registry backfill, report-name safety net,
aggregation safety net, module-specialist validation, and secondary-module
severity capping. Every new "safety net" grows it further. The test file
`tests/test_report_name_safety_net.py` copy-pastes the production logic
into a `_apply_safety_net` helper — prod and tests can drift silently.

### Current state
`alfred/api/pipeline.py:1928` — `async def _phase_post_crew(self) -> None:` runs 351 lines.

### Desired state
`_phase_post_crew` is a 20-line orchestrator that calls named safety-net
functions in documented order. Each safety net lives in
`alfred/api/safety_nets/*.py` and has its own test file. Tests call the
real production function, not a copy.

### Implementation steps
1. Create `alfred/api/safety_nets/` package.
2. Extract each step into a pure-ish function taking `(ctx)` or
   `(changes, candidate)` and mutating `ctx.changes` in place (or
   returning the new list):
   - `detect_drift(ctx) -> str | None`
   - `apply_rescue(ctx)`
   - `backfill_registry_defaults(ctx)`
   - `apply_report_name_safety_net(ctx)`
   - `apply_aggregation_safety_net(ctx)`  ← added in the insights-handoff fix
   - `validate_module_output(ctx)`
   - `cap_secondary_module_severity(ctx)`
3. `_phase_post_crew` becomes:
   ```python
   async def _phase_post_crew(self) -> None:
       ctx = self.ctx
       drift_reason = detect_drift(ctx)
       if drift_reason:
           await apply_rescue(ctx, drift_reason)
           return
       backfill_registry_defaults(ctx)
       apply_report_name_safety_net(ctx)
       apply_aggregation_safety_net(ctx)
       await validate_module_output(ctx)
       cap_secondary_module_severity(ctx)
   ```
4. Move `tests/test_report_name_safety_net.py::_apply_safety_net` to call
   the real `apply_report_name_safety_net` function. Same for aggregation
   tests added in the 2026-04-24 Insights-handoff PR.
5. Update `docs/specs/2026-04-21-doctype-builder-specialist.md` if it
   references line numbers.

### Acceptance criteria
- [ ] `_phase_post_crew` is ≤ 40 lines.
- [ ] Every safety-net test imports and calls the real production function.
- [ ] No test file contains a copy of production logic.
- [ ] Full pipeline tests (`tests/test_pipeline*.py`) still pass.
- [ ] Coverage on `alfred/api/safety_nets/` is ≥ 85%.

### Testing
```bash
pytest tests/ -k "pipeline or safety" -v
```

### Out of scope
- Further splitting of `pipeline.py` (→ TD-H2).

---

## TD-H2 — Split mega-files (`pipeline.py`, `websocket.py`, `orchestrator.py`)

**Severity:** High
**Area:** Architecture
**Effort:** L

### Why it matters
- `alfred/api/pipeline.py` — 2,291 lines, 44 functions.
- `alfred/api/websocket.py` — 1,224 lines.
- `alfred/orchestrator.py` — 1,058 lines, 25 functions.
- `alfred/agents/crew.py` — 1,069 lines.

These are review/maintenance bombs. Code review on a file this size is
ineffective; navigation requires constant `grep`.

### Current state
See above.

### Desired state
Each file ≤ 800 lines. Large files split along natural seams already
present in docstrings.

### Implementation steps
`pipeline.py` → break along phase boundaries:
1. `alfred/api/pipeline/__init__.py` — re-exports for backwards compat.
2. `alfred/api/pipeline/context.py` — `PipelineContext` dataclass.
3. `alfred/api/pipeline/runner.py` — `AgentPipeline` class.
4. `alfred/api/pipeline/phases/` — one file per `_phase_*` method.
5. `alfred/api/pipeline/extractors.py` — `_parse_report_candidate_marker`, `_detect_drift`, `_extract_target_doctypes`.
6. `alfred/api/safety_nets/` — from TD-H1.

`orchestrator.py` → split by classification domain:
1. `alfred/orchestrator/__init__.py`
2. `alfred/orchestrator/mode.py` — `classify_mode`, `ModeDecision`, `_fast_path`.
3. `alfred/orchestrator/intent.py` — `classify_intent`, `IntentDecision`, heuristic patterns, LLM classifier.
4. `alfred/orchestrator/module.py` — module detection.
5. `alfred/orchestrator/analytics_guardrails.py` — `_looks_like_analytics_query`, prefix lists.

`websocket.py` → split by concern:
1. `alfred/api/websocket/__init__.py` — router registration.
2. `alfred/api/websocket/connection.py` — `ConnectionState` class.
3. `alfred/api/websocket/handshake.py` — auth + MCP wiring.
4. `alfred/api/websocket/handlers.py` — per-message-type handlers.
5. `alfred/api/websocket/heartbeat.py` — `_heartbeat_loop`.

### Acceptance criteria
- [ ] `find alfred/ -name '*.py' | xargs wc -l | awk '$1 > 800'` returns no hits.
- [ ] All existing imports continue to work via `__init__.py` re-exports.
- [ ] No test changes required (this is a pure refactor).
- [ ] `git log` annotations in the new files point to the original PR for blame.

### Testing
```bash
pytest  # full suite
```

### Out of scope
- API changes.
- Behavioral changes. If a test fails, the split introduced a regression —
  debug it.

---

## TD-H3 — Replace broad `except Exception:` blocks with specific exceptions

**Severity:** High
**Area:** Resilience / Observability
**Effort:** M

### Why it matters
109 bare-ish `except Exception:` blocks across `alfred/`. 39 in `pipeline.py`
alone. 5 silent `pass` blocks in `pipeline.py` absorb errors with no log,
no metric, no trace. Real bugs — a Redis disconnect, a serializer crash —
become "the UI just sat there".

### Current state
```bash
$ grep -rn "except Exception" alfred/api/pipeline.py | wc -l
39
$ grep -rn "pass$" alfred/api/pipeline.py | head -5
alfred/api/pipeline.py:185:		pass
alfred/api/pipeline.py:408:			pass
...
```

### Desired state
- Every `except Exception:` replaced with a specific exception class, OR
  justified with a code comment explaining why broad catching is correct
  (boundaries, shutdown, best-effort telemetry) AND logs with `exc_info=True` AND
  increments a Prometheus counter.
- Ruff rule `BLE001` (blind-except) added to `pyproject.toml` and
  enforced in CI.

### Implementation steps
1. Audit every `except Exception:` block in `alfred/`. Categorize:
   (a) Narrow to specific exceptions (e.g. `except (json.JSONDecodeError, KeyError):`).
   (b) Justified broad — keep, but ensure `logger.exception(...)` fires.
   (c) Silent `pass` — always wrong; at minimum `logger.debug(...)`.
2. Add to `pyproject.toml`:
   ```toml
   [tool.ruff.lint]
   extend-select = ["BLE001"]
   ```
3. Use `# noqa: BLE001` with an inline reason comment on intentional broad catches.
4. Run `ruff check alfred/` — should be clean.

### Acceptance criteria
- [ ] `grep -rn 'except Exception:' alfred/ | wc -l` shows < 30.
- [ ] `grep -rn 'except Exception as e:\s*$' alfred/ | wc -l` shows < 10.
- [ ] Every remaining broad catch has a `# noqa: BLE001 — <reason>` comment.
- [ ] No `pass` statement silently follows an exception without at least a debug log.
- [ ] `ruff check alfred/` passes in CI.

### Testing
```bash
ruff check alfred/ tests/
pytest  # ensure no regression
```

### Out of scope
- Introducing retry logic where it doesn't exist (separate task).

---

## TD-H4 — Unify configuration: move `ALFRED_*` flags into Settings

**Severity:** High
**Area:** Architecture
**Effort:** M

### Why it matters
`alfred/config.py:50` docstring says "No other module should read env vars
directly". Reality: `os.environ.get("ALFRED_*")` appears in `orchestrator.py`,
`pipeline.py` (multiple times), `crew.py`, `main.py`, `llm_client.py`.
Inline comment rationalizes this as "runtime toggles, not config" — a
distinction without a difference. Consequence: new flags are invisible;
tests can't override them atomically; there's no central documentation of
what toggles exist.

### Current state
```bash
$ grep -rn 'os\.environ\.get("ALFRED_' alfred/ | wc -l
~18
```

### Desired state
All `ALFRED_*` flags declared on `Settings`. Callers use
`get_settings().FLAG_NAME`. Ruff rule blocks new `os.environ.get("ALFRED_*")`.

### Implementation steps
1. List every `ALFRED_*` flag via grep.
2. For each, add to `Settings` with the same default as the current `get(...) != "1"` pattern:
   ```python
   ALFRED_PER_INTENT_BUILDERS: bool = False
   ALFRED_MODULE_SPECIALISTS: bool = False
   ALFRED_MULTI_MODULE: bool = False
   ALFRED_REPORT_HANDOFF: bool = False
   ALFRED_ORCHESTRATOR_ENABLED: bool = True
   ALFRED_REFLECTION_ENABLED: bool = True
   ALFRED_TRACING_ENABLED: bool = False
   ```
   Pydantic handles the `"1" → True` coercion; document the mapping.
3. Replace `os.environ.get("ALFRED_X") == "1"` with `get_settings().ALFRED_X`.
4. For hot paths (inside functions called per request), cache:
   `_settings = get_settings()` at module level, but respect that
   `get_settings()` uses `@lru_cache` anyway (add it if missing).
5. Add ruff custom rule (via `ruff.lint.per-file-ignores` or a grep-based
   CI step) blocking new `os.environ.get("ALFRED_` introductions.
6. Update README flag matrix with the new canonical location.

### Acceptance criteria
- [ ] `grep -rn 'os\.environ\.get("ALFRED_' alfred/ | wc -l` shows 0.
- [ ] All feature flags visible in `Settings`.
- [ ] `.env.example` lists each flag with default and description.
- [ ] Flag-override in tests uses `Settings(ALFRED_X=True)`, not `monkeypatch.setenv`.

### Testing
```bash
pytest  # ensure no regression on flag-gated paths
```

### Out of scope
- Hot-reload of flags at runtime — explicitly not supported; restart required.

---

## TD-H5 — TTL on Redis task state to prevent unbounded growth

**Severity:** High
**Area:** Data / Resilience
**Effort:** S

### Why it matters
`alfred/state/store.py:74` — `set_task_state` uses `self._redis.set(key, value)` with no expiration. Every conversation's task state blob lives in Redis forever until manually evicted. At scale, Redis OOMs and triggers maxmemory policy (random eviction in the default config) — which can evict *in-flight* task state, silently corrupting active pipelines.

### Current state
- `store.py:74` — `await self._redis.set(key, value)`.
- `store.py:171-187` — `set_with_ttl` exists but is unused by `set_task_state`.
- Streams use `MAXLEN=10_000` — OK.

### Desired state
- `set_task_state` takes a `ttl_seconds` kwarg with a sensible default
  (7 days = 604800).
- Overrideable via `ALFRED_TASK_STATE_TTL_SECONDS` env var.
- New Prometheus gauge `alfred_redis_key_count_by_prefix` for monitoring.

### Implementation steps
1. Add to `Settings`: `TASK_STATE_TTL_SECONDS: int = 604800`.
2. Modify `TaskStateStore.set_task_state`:
   ```python
   async def set_task_state(self, site_id, task_id, state_dict, ttl_seconds: int | None = None):
       ...
       ttl = ttl_seconds if ttl_seconds is not None else get_settings().TASK_STATE_TTL_SECONDS
       await self._redis.setex(key, ttl, value)
   ```
3. Add Prometheus gauge updated by a periodic background task:
   ```python
   redis_key_count_by_prefix = Gauge(
       "alfred_redis_key_count_by_prefix",
       "Number of Redis keys grouped by alfred:* prefix.",
       labelnames=("prefix",),
   )
   ```
4. Add a periodic coroutine in lifespan that samples `SCAN` every 5 minutes
   and updates the gauge (bounded scan: `COUNT 1000`, cap at 10k keys sampled).

### Acceptance criteria
- [ ] `TTL <key>` on a newly-written task state returns > 0.
- [ ] After TTL elapses in a test (use a small TTL), the key is evicted.
- [ ] Metric `alfred_redis_key_count_by_prefix` exposes counts per prefix.
- [ ] New test: `test_set_task_state_applies_default_ttl`.
- [ ] New test: `test_set_task_state_custom_ttl`.

### Testing
```bash
pytest tests/test_store.py -v
# Integration: run pipeline, then redis-cli TTL alfred:<site>:task:<id>
```

### Out of scope
- Redis cluster / sentinel.
- Task state archival before eviction.

---

## TD-H6 — Dedicated thread pool for LLM calls

**Severity:** High
**Area:** Performance / Resilience
**Effort:** M

### Why it matters
`alfred/llm_client.py:214-218` — `loop.run_in_executor(None, ...)` uses
FastAPI's default `ThreadPoolExecutor` (40 threads on Python 3.11). Each
LLM call blocks a thread for up to 60s. Five concurrent pipelines × 3
sequential LLM calls = 15 threads easily; traffic spikes saturate the pool
and the whole app — including heartbeat, health checks, admin endpoints —
hangs.

`asyncio.timeout()` on the async side does NOT cancel the blocking urllib
call — the thread keeps running even after the caller gives up.

### Current state
```python
return await loop.run_in_executor(
    None, lambda: ollama_chat_sync(messages, site_config, tier=tier, **kwargs)
)
```

### Desired state
- Dedicated `ThreadPoolExecutor(max_workers=LLM_POOL_SIZE)` shared across LLM calls.
- Size configurable via `LLM_POOL_SIZE` env var, default 16.
- Pool exhaustion surfaces as a specific `OllamaError("LLM pool exhausted")`
  rather than a silent hang.
- Ideally: migrate to `httpx.AsyncClient` and eliminate the thread pool
  entirely (author's comment blames httpcore; this claim should be
  re-tested with current versions before accepting the workaround as
  permanent).

### Implementation steps
Phase 1 (quick — ship this sprint):
1. Add `LLM_POOL_SIZE: int = 16` to `Settings`.
2. Create a module-level executor in `llm_client.py`:
   ```python
   _llm_executor = concurrent.futures.ThreadPoolExecutor(
       max_workers=get_settings().LLM_POOL_SIZE,
       thread_name_prefix="alfred-llm",
   )
   ```
3. Use it in `ollama_chat`:
   ```python
   return await loop.run_in_executor(_llm_executor, ...)
   ```
4. Add Prometheus gauge `alfred_llm_pool_busy_threads` sampled every 30s.

Phase 2 (follow-up): migrate to `httpx.AsyncClient`. File the httpcore bug
if it's still reproducible on current versions.

### Acceptance criteria
- [ ] LLM calls run on threads with name prefix `alfred-llm-*`.
- [ ] `LLM_POOL_SIZE=2`, then issue 5 concurrent LLM calls; 2 should
      proceed, 3 should queue.
- [ ] The default executor (for tool calls etc.) is NOT starved by LLM load.
- [ ] Metric `alfred_llm_pool_busy_threads` reflects active count.

### Testing
```bash
pytest tests/test_llm_client.py -v
# Load test: 20 parallel calls, verify pool sizing is enforced.
```

### Out of scope
- Per-tier pool separation (triage/reasoning/agent) — defer unless a real bottleneck.

---

## TD-H7 — Multi-worker state story: Redis-backed ConnectionState

**Severity:** High
**Area:** Architecture / Resilience
**Effort:** L

### Why it matters
`Dockerfile:32` — `ENV WORKERS=2`. FastAPI workers don't share memory.
Every WebSocket-scoped object (`conn.active_pipeline`, `conn._pending_questions`,
`mcp_client._pending_futures`) lives in a single worker. Load balancer
reconnect to a different worker → state lost, pipeline orphaned.

The app *looks* scaled with 2+ workers; state coherence is worse than
single-worker.

### Current state
- `ConnectionState` (in `alfred/api/websocket.py`) is a plain dataclass held in worker memory.
- No sticky routing configured anywhere.

### Desired state
Choose one of:
- **Option A (quick, recommended):** `WORKERS=1`, document clearly. Scale
  horizontally via multiple *containers*, each with its own state, behind
  a sticky LB (session cookie or IP-hash). Best ROI.
- **Option B (long-term):** move WebSocket-scoped state to Redis with
  conversation_id as key. Workers become stateless. MCP client futures
  live in the connection-handling worker only; if reconnect lands on a
  different worker, the pipeline needs to look up state from Redis and
  resume. Requires real design work.

### Implementation steps (Option A)
1. Change `Dockerfile`: `ENV WORKERS=1`.
2. Change `docker-compose.yml` health check / scaling docs.
3. Update `docs/SETUP.md` with: "Scale horizontally via replicas; set
   `workers: 1` per container; configure sticky WebSocket routing on the LB".
4. At startup in `main.py`, log a warning if `WORKERS > 1`.

### Implementation steps (Option B — separate task if chosen)
1. Design doc in `docs/specs/`.
2. Move `conn.active_pipeline` state to Redis with `conversation_id:pipeline_status` key.
3. Heartbeat the owning worker; on reconnect to a new worker, fetch status from Redis.
4. MCP client futures remain worker-local (those can't survive worker loss).

### Acceptance criteria (Option A)
- [ ] Dockerfile WORKERS=1.
- [ ] Startup warns if `WORKERS > 1`.
- [ ] Documentation for horizontal scaling via replicas is clear.

### Testing
Integration: kill one of two containers mid-pipeline; LB routes
reconnecting client to the surviving container; the client sees the
pipeline-ended message (or a "retry" prompt) rather than silence.

### Out of scope
- Distributed MCP client futures (not feasible without major redesign).

---

## TD-H8 — Pin CrewAI supply chain + Dependabot

**Severity:** High
**Area:** Security / Supply chain
**Effort:** S

### Why it matters
`pyproject.toml` pins `crewai==0.203.2`. Everything else is range-bounded
(`>=X,<Y`). No Renovate/Dependabot — bumps happen only when someone
notices. A CVE in CrewAI or a transitive (LiteLLM, httpx, chromadb,
pydantic) has no automatic discovery path.

### Current state
No `.github/dependabot.yml`, no `renovate.json`.

### Desired state
Automated weekly dependency-update PRs; CI runs the full test suite on
every dep bump; merge only after green.

### Implementation steps
1. Create `.github/dependabot.yml`:
   ```yaml
   version: 2
   updates:
     - package-ecosystem: "pip"
       directory: "/"
       schedule: { interval: "weekly", day: "monday" }
       open-pull-requests-limit: 5
       groups:
         dev-deps:
           dependency-type: "development"
     - package-ecosystem: "docker"
       directory: "/"
       schedule: { interval: "weekly" }
     - package-ecosystem: "github-actions"
       directory: "/"
       schedule: { interval: "weekly" }
   ```
2. Add `pip-audit` job to CI (already in TD-C4) — fails PRs introducing known-CVE deps.
3. Document the CrewAI bump procedure in `docs/` — "bump, run pytest, run
   `benchmarks/`, check drift/rescue regression, then merge".

### Acceptance criteria
- [ ] Dependabot file present and valid (GitHub UI confirms).
- [ ] A week after merge, at least one dep-bump PR exists (even if only a patch).
- [ ] `pip-audit` is a required check.

### Testing
Manual: wait a week, confirm PRs open.

### Out of scope
- Auto-merge policies.

---

## TD-H9 — Eliminate test/production code drift

**Severity:** High
**Area:** Testing
**Effort:** S (once TD-H1 lands)

### Why it matters
`tests/test_report_name_safety_net.py::_apply_safety_net` is a *copy* of
the production safety-net block. I replicated this anti-pattern for the
aggregation safety net in the 2026-04-24 handoff PR. Prod logic can
diverge from the test's copy; tests still pass.

### Current state
- `tests/test_report_name_safety_net.py:26` — `_apply_safety_net(ctx)` duplicates production logic.
- `tests/test_report_name_safety_net.py:<newer>` — `_apply_aggregation_safety_net(ctx)` same.

### Desired state
Tests import the production function and call it with a minimal mock
context. Zero duplication.

### Implementation steps
1. Depends on TD-H1: refactor safety nets into callable functions.
2. Update each test file to import and call the production function:
   ```python
   from alfred.api.safety_nets.report_name import apply_report_name_safety_net
   ...
   apply_report_name_safety_net(ctx)
   ```
3. Delete the `_apply_*` helpers from tests.

### Acceptance criteria
- [ ] `grep -rn "def _apply_" tests/` returns 0 hits.
- [ ] All safety-net tests pass.
- [ ] Coverage on the safety nets is ≥ 90%.

### Testing
```bash
pytest tests/ -k safety -v --cov=alfred/api/safety_nets
```

### Out of scope
- Refactor itself (→ TD-H1).

---

# MEDIUM (Week 4 + rolling backlog)

---

## TD-M1 — Harden JWT: issuer/audience claims, prep for RS256

**Severity:** Medium
**Area:** Security
**Effort:** M

### Why it matters
`alfred/middleware/auth.py:80-82` — `algorithms=["HS256"]`, no `aud`, no
`iss` validation. A JWT signed for one Alfred instance can be replayed
against another if they share the signing key. HS256 forces the signing
key to be present on the verifying side — blocks client-side verification
without secret leakage.

### Desired state
- JWT includes `iss` (instance identity, e.g. hostname) and `aud` (target
  instance URL).
- `verify_jwt_token` takes and enforces expected `iss` and `aud`.
- Support path for RS256: `JWT_SIGNING_ALGORITHM: str = "HS256"` in Settings;
  when `RS256`, read `JWT_PRIVATE_KEY_PEM` and `JWT_PUBLIC_KEY_PEM`.

### Implementation steps
1. Add `JWT_ISSUER`, `JWT_AUDIENCE` to Settings.
2. Extend `create_jwt_token` / `verify_jwt_token` to include/verify these.
3. Emit deprecation warning when tokens lack `iss`/`aud` (during migration).
4. Document migration in `docs/SETUP.md`.

### Acceptance criteria
- [ ] New tokens include `iss` and `aud`.
- [ ] Tokens missing either claim are rejected.
- [ ] A token signed for instance A is rejected at instance B.
- [ ] New tests cover all three cases.

### Out of scope
- Actual RS256 migration — future task.

---

## TD-M2 — Unified error response shape

**Severity:** Medium
**Area:** API design
**Effort:** S

### Why it matters
`alfred/api/routes.py` raises `HTTPException(detail=dict)` in some places
and `HTTPException(detail=str)` in others. WebSocket error frames
sometimes use `{"error": str, "code": str}` and sometimes just `{"error": str}`.
OpenAPI consumers (admin portal, client app) have to branch on shape.

### Implementation steps
1. Define `alfred/api/errors.py::ErrorResponse(BaseModel)` with
   `{error: str, code: str, details: dict | None}`.
2. Define `alfred/api/errors.py::raise_error(status, code, message, **details)` helper.
3. Global exception handler attached in `main.py` that converts any uncaught
   `HTTPException` with string detail into the shape.
4. Replace every ad-hoc `HTTPException(detail=...)` with `raise_error(...)`.

### Acceptance criteria
- [ ] `grep -rn 'HTTPException(.*detail=' alfred/api/' | wc -l` drops to 0.
- [ ] Every error response parses as `ErrorResponse`.
- [ ] Admin portal can switch to typed error handling.

---

## TD-M3 — Structured (JSON) logging with context propagation

**Severity:** Medium
**Area:** Observability
**Effort:** M

### Why it matters
Plain-text logs make Grafana/Loki queries regex-heavy and slow. Prometheus
metrics tell you *what* happened; logs tell you *why*. Without
`conversation_id`, `site_id`, `user`, `phase` bound as context, tracing a
single user's path through the pipeline requires a lot of eyeballing.

### Implementation steps
1. Add `structlog` to dependencies.
2. Configure in `main.py`:
   - JSONRenderer in production.
   - ConsoleRenderer in dev.
3. Bind context per-request in a middleware / WebSocket handler:
   `structlog.contextvars.bind_contextvars(site_id=..., user=..., conversation_id=...)`.
4. Unbind at request end.
5. Apply the redaction processor from TD-C7.

### Acceptance criteria
- [ ] Log lines in production are single-line JSON.
- [ ] Every log line from within a request/pipeline includes site_id, user, conversation_id.
- [ ] Sensitive keys still redacted.

---

## TD-M4 — Dockerfile hygiene

**Severity:** Medium
**Area:** DevOps
**Effort:** S

### Why it matters
- Base image `python:3.11-slim` — no patch pin. Rebuilds are non-reproducible.
- Single-stage build — includes pip build cache (though `--no-cache-dir` mitigates).
- No `.dockerignore` → `.git/`, `tests/`, `.venv/` may be copied, bloating image.
- `HEALTHCHECK` uses `curl` — 4MB extra on slim.

### Implementation steps
1. Pin base: `FROM python:3.11.9-slim-bookworm`.
2. Add `.dockerignore`:
   ```
   .git
   .venv
   .pytest_cache
   tests
   docs
   benchmarks
   *.md
   __pycache__
   *.pyc
   ```
3. Multi-stage:
   ```dockerfile
   FROM python:3.11.9-slim-bookworm AS builder
   ...
   RUN pip install --user .
   
   FROM python:3.11.9-slim-bookworm AS runtime
   COPY --from=builder /root/.local /home/alfreduser/.local
   ENV PATH=/home/alfreduser/.local/bin:$PATH
   ...
   ```
4. Replace curl-based healthcheck with a Python one:
   `CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen(f'http://localhost:{__import__(\"os\").environ[\"PORT\"]}/health', timeout=3).status == 200 else 1)"`.

### Acceptance criteria
- [ ] `docker images alfred-processing` shows smaller size than before.
- [ ] `docker inspect` shows pinned base image digest.
- [ ] Healthcheck passes without curl in the image.

---

## TD-M5 — Secrets manager integration

**Severity:** Medium
**Area:** Security / DevOps
**Effort:** M

### Why it matters
Production deployment relies on a plaintext `.env` file on disk. No
integration with AWS Secrets Manager, Vault, Doppler, or Kubernetes
Secrets. Key rotation requires file edits + restarts; there's no audit
trail of who read which secret.

### Implementation steps
1. Add `pydantic-settings-secrets-manager` (or equivalent) to deps.
2. Extend `Settings.model_config` with a dynamic loader:
   - If `AWS_SECRETS_MANAGER_ARN` set, read from there first.
   - If `VAULT_ADDR` set, read from Vault.
   - Else fall back to `.env`.
3. Document in `docs/SETUP.md`: which secret paths map to which settings.

### Acceptance criteria
- [ ] App starts with only `AWS_SECRETS_MANAGER_ARN` set (no `.env`) given a
      secret of the right shape.
- [ ] Failure mode is clear ("unable to fetch secret X from source Y").

---

## TD-M6 — Graceful shutdown for in-flight pipelines

**Severity:** Medium
**Area:** Resilience
**Effort:** S

### Why it matters
`alfred/main.py:77-82` closes Redis on shutdown but doesn't stop
`conn.active_pipeline` tasks. In-flight LLM calls keep running after
SIGTERM; Kubernetes kills the pod at `terminationGracePeriodSeconds` and
the user sees silence.

### Implementation steps
1. In lifespan, on shutdown:
   - Set `app.state.shutting_down = True`.
   - Reject new prompts with `{"error": "Server shutting down", "code": "SHUTTING_DOWN"}`.
   - Wait up to `GRACEFUL_SHUTDOWN_TIMEOUT` (default 30s) for active pipelines
     to complete (via an `active_pipelines_count` counter on app.state).
   - After timeout, cancel remaining tasks cleanly.
2. Add Prometheus gauge `alfred_active_pipelines`.

### Acceptance criteria
- [ ] SIGTERM triggers shutdown flow.
- [ ] New prompts rejected during shutdown.
- [ ] In-flight pipelines get up to 30s to finish.
- [ ] Kubernetes preStop hook respects the timeout.

---

## TD-M7 — Generated SQL: use Frappe parameter placeholders

**Severity:** Medium
**Area:** Correctness
**Effort:** S

### Why it matters
`alfred/handlers/insights_candidate.py::_build_aggregation_sql` (added
2026-04-24) embeds absolute dates: `BETWEEN '2026-04-01' AND '2026-06-30'`.
Re-running the Report in the next quarter returns stale results. Users
would need to edit the SQL manually.

### Implementation steps
1. Change SQL body to use `%(from_date)s` and `%(to_date)s`.
2. Populate `filters_json` on the candidate with two date filters whose
   defaults are the preset-resolved dates:
   ```json
   [
     {"fieldname": "from_date", "label": "From Date", "default": "2026-04-01", ...},
     {"fieldname": "to_date", "label": "To Date", "default": "2026-06-30", ...}
   ]
   ```
3. Pipeline safety net forwards the filters into `data.filters_json`.
4. Update tests: assert placeholders in SQL, assert filters in candidate.

### Acceptance criteria
- [ ] Generated SQL contains `%(from_date)s`, not a hard-coded date.
- [ ] Report runs correctly after the quarter advances.
- [ ] Existing aggregation tests updated.

---

## TD-M8 — Local safe-SQL validator

**Severity:** Medium
**Area:** Security
**Effort:** S

### Why it matters
`check_safe_sql_query` is a Frappe-side invariant. If the extractor ever
emits dangerous SQL (DDL / DML / multi-statement), the only gate is
Frappe's check. Belt-and-suspenders: validate locally before handoff.

### Implementation steps
1. Create `alfred/security/sql_safety.py::validate_safe_select(sql: str) -> None`.
2. Logic:
   - Reject if more than one statement (count `;` outside string literals).
   - Reject if any of: `INSERT`, `UPDATE`, `DELETE`, `DROP`, `CREATE`, `ALTER`,
     `TRUNCATE`, `GRANT`, `REVOKE`, `EXEC`, `CALL`, `LOAD`, `HANDLER`.
   - Must start with `SELECT` (after optional whitespace/comments).
3. Call from `_build_aggregation_sql` before returning.

### Acceptance criteria
- [ ] `validate_safe_select("SELECT * FROM x")` passes.
- [ ] `validate_safe_select("SELECT 1; DROP TABLE x")` raises.
- [ ] `validate_safe_select("INSERT INTO x VALUES (1)")` raises.
- [ ] `validate_safe_select("SELECT 1 /* DROP TABLE x */")` passes (safe comment).
- [ ] Invoked from extractor.

---

## TD-M9 — OpenAPI versioning

**Severity:** Medium
**Area:** API design
**Effort:** S

### Why it matters
REST routes are unversioned (`/tasks`, `/health`). Breaking changes force
lockstep client deploys.

### Implementation steps
1. Prefix REST router: `router = APIRouter(prefix="/api/v1")`.
2. Keep `/health` unversioned (standard practice for probes).
3. When a v2 API is needed, add a new router and support both for a
   deprecation window.
4. Update client-app docs to call `/api/v1/*`.

### Acceptance criteria
- [ ] `/api/v1/tasks` works.
- [ ] `/tasks` returns 404 (or 301 to v1 during migration).
- [ ] `/health` unchanged.

---

## TD-M10 — `.env.example` enforcement

**Severity:** Medium
**Area:** DevOps
**Effort:** S

### Why it matters
New settings added to `Settings` may not be reflected in `.env.example`.
Operators deploy and discover missing keys at runtime.

### Implementation steps
1. Write a `scripts/check_env_example.py` that:
   - Parses `Settings` for declared fields.
   - Parses `.env.example` for declared keys.
   - Fails on orphans (in `Settings` but missing in example) or extras
     (in example but not in `Settings`).
2. Run in CI.

### Acceptance criteria
- [ ] Script passes in current state (fix drift first if any).
- [ ] PR that adds a `Settings` field without updating `.env.example` fails CI.

---

# LOW (Rolling backlog)

---

## TD-L1 — Pre-commit hooks

**Severity:** Low
**Area:** DevOps
**Effort:** S

Add `.pre-commit-config.yaml` with `ruff`, `ruff-format`, `trailing-whitespace`,
`end-of-file-fixer`, `check-yaml`. Document `pre-commit install` in
README. CI runs `pre-commit run --all-files` as a second opinion.

**Acceptance criteria:**
- [ ] `.pre-commit-config.yaml` committed.
- [ ] `pre-commit run --all-files` passes locally.

---

## TD-L2 — Type-check (mypy) discipline

**Severity:** Low
**Area:** Code quality
**Effort:** M

Add `mypy` to dev deps. Start with `alfred/api/` and `alfred/orchestrator/`
(these tend to hit other code). Configure:

```toml
[tool.mypy]
python_version = "3.11"
strict = true
exclude = ["tests/"]
```

Per-file-ignores for the biggest current files (grandfathered). Cover in CI.

**Acceptance criteria:**
- [ ] `mypy alfred/api/ alfred/orchestrator/` passes.
- [ ] Grandfathered files are marked with explicit ignores.
- [ ] CI gate active.

---

## TD-L3 — Test coverage reporting

**Severity:** Low
**Area:** Testing
**Effort:** S

Add `pytest-cov` to dev deps. CI generates `coverage.xml`, uploads to
Codecov. Badge in README.

Target: 80% line, 70% branch on `alfred/`.

**Acceptance criteria:**
- [ ] Coverage report visible in Codecov UI.
- [ ] README coverage badge.
- [ ] CI fails on coverage drop > 2 percentage points on a PR.

---

## TD-L4 — Admin service key rotation

**Severity:** Low
**Area:** Security
**Effort:** M

`ADMIN_SERVICE_KEY` is a static bearer token. Build a JWT-based
service-to-service auth with short expiry and rotation, signed by the
same mechanism as TD-M1 (once RS256 lands).

**Acceptance criteria:**
- [ ] Admin portal and processing app use short-lived JWTs.
- [ ] Key rotation is documented and tested.

---

## TD-L5 — Process hygiene: CHANGELOG + release notes

**Severity:** Low
**Area:** DevOps / Docs
**Effort:** S

Add `CHANGELOG.md`. Each release documented with "Added / Changed /
Fixed / Security" sections. Link commits. Makes upgrade review for
operators trivial.

**Acceptance criteria:**
- [ ] `CHANGELOG.md` committed, documenting the last 3 releases.
- [ ] Release PRs require a changelog entry.

---

# 30-Day Sequencing

**Week 1 — Security + foundation (all Critical):**
TD-C1, TD-C5, TD-C7 (day 1 — all small).
TD-C3, TD-C6 (day 2–3).
TD-C2, TD-C4 (day 4–5).

**Week 2 — Structural de-risk:**
TD-H5, TD-H8 (small).
TD-H3 (audit + pass), TD-H4 (config unification).

**Week 3 — Architecture:**
TD-H1 (god-method split), TD-H9 (test/prod alignment — trivial after H1).
TD-H2 (file splits).

**Week 4 — Polish + hardening:**
TD-H6 (LLM thread pool), TD-H7 (workers decision).
TD-M3 (structured logging), TD-M1 (JWT hardening).

**Rolling:** every Medium and Low slots into the next 3 sprints.

---

# Validation checklist for the CTO

Before declaring the backlog complete:

- [ ] No Critical task remains open.
- [ ] CI is green on `main` with pytest + ruff + pip-audit.
- [ ] Docker image builds in CI and publishes on merge.
- [ ] Prometheus `/metrics` exposes the new counters (SSRF, rate-limit, LLM pool).
- [ ] Log lines in production are JSON with bound context.
- [ ] External security review (recommend an outside firm once TD-C1..C7
      land) has signed off on the API-key + JWT + SSRF + CORS surface.
- [ ] A load test at 10× current peak traffic shows no thread-pool exhaustion.
- [ ] A chaos-engineering drill (kill Redis mid-pipeline; kill one of two
      workers during WS; force LLM timeout) shows graceful degradation with
      clear user messaging.

Anything unchecked is either a real gap or requires a documented exception
with an owner and a date.
