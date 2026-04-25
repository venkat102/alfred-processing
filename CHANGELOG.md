# Changelog

All notable changes to alfred-processing land here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
starting at 0.2.0.

## [Unreleased]

### Security
- **Constant-time API key comparison** (TD-C1). REST and WebSocket auth now
  use `hmac.compare_digest` instead of `!=`. Closes a timing-side-channel
  brute-force on the gate-everything `API_SECRET_KEY`.
- **Split JWT signing key from API bearer** (TD-C2). New `JWT_SIGNING_KEY`
  setting; when set (>= 32 bytes and != `API_SECRET_KEY`) it is used for
  WebSocket-handshake JWT verification. When unset, falls back to
  `API_SECRET_KEY` with a loud deprecation warning at boot — safe rollout
  for legacy deployments.
- **SSRF protection on client-supplied LLM URLs** (TD-C3). Every outbound
  LLM request now passes through `alfred.security.url_allowlist.validate_llm_url`.
  Blocks private/loopback/metadata IP ranges (incl. AWS IMDS 169.254.169.254).
  Bypass for local dev via `DEBUG=true`; allow-list for production self-
  hosted Ollama via `ALFRED_LLM_ALLOWED_HOSTS`.
- **CORS fail-fast with dev escape** (TD-C5). Startup rejects invalid
  `ALLOWED_ORIGINS=*` + `allow_credentials=True` combo (CORS-spec-invalid).
  Dev escape: `DEBUG=true` + `*` boots with `credentials=False` to stay
  spec-compliant.
- **WebSocket prompt rate limit** (TD-C6). Server-side `SERVER_DEFAULT_RATE_LIMIT`
  cap (20/hr) on the WebSocket prompt path; matches REST behaviour. Prevents
  LLM DoS / cost exhaustion by a compromised client. Counter
  `alfred_rate_limit_block_total{source}` for observability.
- **Log redaction** (TD-C7). Sensitive dict keys (`api_key`, `llm_api_key`,
  `jwt_token`, `password`, ...) are redacted before log emission.
  Regex sweep catches Bearer-token and JWT-triple patterns in free-form
  messages. Prompts are deliberately NOT redacted — they're primary
  debugging signal.
- **Local safe-SQL validator** (TD-M8). `validate_safe_select` belt-and-
  suspenders for Frappe's own `check_safe_sql_query`. Rejects DDL/DML/
  multi-statement even if Frappe's check has a gap.
- **Supply-chain CVE scanning** (TD-C4). CI `pip-audit` job blocking.
  Three known CVEs in `litellm` 1.74.9 explicitly ignored with rationale
  (crewai pins that version; CVEs target litellm's proxy endpoints which
  Alfred doesn't expose).

### Added
- **Structured JSON logging with context propagation** (TD-M3). New
  module `alfred/obs/logging_setup.py` bridges stdlib `logging` through
  structlog so existing `logging.getLogger(...)` calls gain JSON output
  (production) or console output (dev) without being rewritten.
  `bind_request_context(site_id=…, user=…, conversation_id=…)` runs on
  WebSocket auth; `clear_request_context()` runs on disconnect. Every
  log line in the connection's scope auto-carries those fields.
  Redaction is reapplied via a stdlib `Filter` and structlog processors
  so both `logger.info("x=%s", {...})` and native `log.info("...", k=v)`
  styles are scrubbed.
- **Mypy clean baseline** (TD-L2). Every file under `alfred/`
  (79 source files) passes a stock mypy run. CI gates against
  regressions via a blocking `mypy` job. Config under `[tool.mypy]` in
  `pyproject.toml` with `ignore_missing_imports = true` for CrewAI /
  LiteLLM / ollama / httpx-ws (no stubs). No per-file ignores — every
  genuine type issue was fixed instead.
- **Unified error response shape** (TD-M2). Global `HTTPException`
  handler rewrites every error body to the canonical
  `{error, code, details}` shape defined by `ErrorResponse`. Callers
  use `alfred.api.errors.raise_error(...)` for new code; legacy
  string-detail exceptions and third-party middleware are wrapped by
  the same handler so all clients branch on a single JSON shape.
- **LLM dedicated thread pool** (TD-H6 phase 1). `alfred/llm_client.py`
  now uses a module-level `ThreadPoolExecutor(max_workers=LLM_POOL_SIZE)`
  (default 16) with `alfred-llm-*` thread prefix, instead of the
  shared default executor. Prevents a spike of concurrent LLM calls
  from starving heartbeats / health checks / admin calls.
- **CI/CD pipeline** (TD-C4). `.github/workflows/ci.yml` with lint (ruff,
  informational), test (pytest + coverage + Redis service), security
  (pip-audit, blocking), docker (build + boot smoke, blocking), and a
  `.env.example` drift guard (blocking). `.github/dependabot.yml` for
  weekly dep bumps.
- **Pre-commit hooks** (TD-L1). `.pre-commit-config.yaml` with ruff,
  ruff-format, trailing-whitespace, EOF fixer, and YAML/TOML/JSON
  validators. CI also runs `pre-commit run --all-files` as a second opinion.
- **CHANGELOG.md** (TD-L5). This file.
- **`.env.example` enforcement** (TD-M10). `scripts/check_env_example.py`
  reads `Settings.model_fields` and fails CI when any field is
  undocumented. Blocks the silent-config-drift failure mode.
- **Frappe parameter placeholders in generated SQL** (TD-M7). Aggregation
  Reports now use `%(from_date)s` / `%(to_date)s` with filter defaults,
  so the same Report stays correct after the quarter advances.
- **Graceful shutdown** (TD-M6). Lifespan sets `shutting_down` flag,
  WebSocket handler rejects new prompts (`SHUTTING_DOWN` error code),
  boot waits up to `GRACEFUL_SHUTDOWN_TIMEOUT` (default 30s) for
  in-flight pipelines to drain before closing Redis.
- **Redis TTL on task state** (TD-H5). `set_task_state` now uses `setex`
  with default 7-day TTL (`TASK_STATE_TTL_SECONDS`). Prevents unbounded
  Redis growth and silent maxmemory-policy eviction of in-flight state.

### Changed
- **`_phase_post_crew` decomposed into `safety_nets/` package** (TD-H1).
  The 351-line god-method now orchestrates seven standalone helpers
  (drift detection, rescue, backfill, report-handoff, module
  validation, empty-changeset error). No behavior change — pure
  extraction, every existing test passes against the new shape.
- **Single-worker default per container** (TD-H7, Option A).
  `Settings.WORKERS: int = 1`, `Dockerfile ENV WORKERS=1`,
  `docker-compose.yml` default, `.env.example`. Startup logs a
  WARNING if the operator overrides it higher, naming the state
  (`ConnectionState`, MCP pending futures, `_pending_questions`) that
  will be lost on a LB reconnect. Scale via container replicas with
  sticky WebSocket routing at the LB instead.
- **Broad exceptions narrowed across `alfred/`** (TD-H3 phase 2).
  119 grandfathered `# noqa: BLE001` sites were worked down to 53.
  Every remaining broad catch now carries a rationale comment
  identifying the boundary (LLM, CrewAI, MCP, 3rd-party ML, metrics
  best-effort, handler wrapper, or test contract). `tests/` is fully
  narrowed (no `# noqa: BLE001` remaining there).
- **JWT issuer / audience claims** (TD-M1). `verify_jwt_token` now
  accepts optional `issuer` and `audience` arguments; when
  `JWT_ISSUER` / `JWT_AUDIENCE` settings are set, the values are
  enforced against the token's `iss` / `aud` claims. Backward-
  compatible: empty settings skip enforcement so existing tokens
  keep working.
- **Config unification** (TD-H4). All `ALFRED_*` feature flags moved into
  `Settings` as typed fields. `get_settings()` now `@lru_cache`d so hot-path
  reads don't re-validate Pydantic per call. One exception: `alfred.security.
  url_allowlist` still reads env directly to stay below the Settings layer.
- **Dockerfile hygiene** (TD-M4). Pinned `python:3.11.9-slim-bookworm`
  base; multi-stage builder + runtime; `.dockerignore` excludes `.git`,
  `.venv`, `tests`, `docs`; healthcheck replaced `curl` with stdlib
  `urllib` (smaller image); exec-form CMD so SIGTERM reaches uvicorn.
- **Log level env-controlled** (TD-C7). `LOG_LEVEL` env defaults to INFO;
  was hardcoded to DEBUG which leaked prompts/PII and blew up log
  ingestion cost.
- **Report Builder insights aggregation fix**. When prompt carries
  aggregation semantics (`top N X by Y`), extractor now correctly picks
  Query Report + source DocType where the metric lives (e.g. `Sales
  Invoice` for "revenue", not `Customer`).
- **API versioning** (TD-M9). All functional REST routes prefixed
  `/api/v1/`; `/health` unversioned per probe standard. Routes.py
  docstring documents the convention for future additions.

### Fixed
- **`_run_insights_short_circuit` NameError**. The conversation-memory
  write referenced an undefined `reply` (rename regression) — crashed
  every insights-mode turn that had `conversation_memory` set. Now
  uses `result.reply` consistently.
- **`INSIGHTS_TASK_DESCRIPTION.format(...)` KeyError**. The template
  contains literal JSON examples like `{"disabled": 0}` that `.format`
  treated as format keys and crashed the whole insights-crew build.
  Switched to two targeted `str.replace` calls for the two real
  placeholders (`{prompt}`, `{user_context}`).
- **Test/production code drift closed** (TD-H9). The
  `_apply_safety_net` and `_apply_aggregation_safety_net` helpers in
  `tests/test_report_name_safety_net.py` are now thin adapters that
  call the real production functions from
  `alfred/api/safety_nets/report_handoff.py` — bundled with TD-H1.
- **Analytics-shape prompts routed correctly in Dev mode**. Hybrid
  redirect: Dev + analytics-shape → Insights with `source=analytics_redirect`.
  User can override via `force_dev_override=true` from UI banner.
- **Intent classifier guardrails**. `_looks_like_analytics_query` on the
  dev side deflects read-side prompts to `intent=unknown` so the
  generic Developer (not a specialist) handles it. LLM classifier
  system prompt tightened — demands build-verb + target-primitive.
- **Data-reply heuristic recognises supplier/territory/item nouns**.
  `_reply_looks_like_data`'s count regex now matches the entity set
  the aggregation detector supports.

### Follow-ups tracked
- TD-H2: split mega-files (`pipeline.py`, `websocket.py`,
  `orchestrator.py`, `crew.py`). Pure refactor, four PRs.
- TD-H6 Phase 2: migrate LLM client to `httpx.AsyncClient`. Gated on
  reproducing the historical httpcore read-timeout bug against
  current versions before ripping out the thread pool.
- TD-M5: secrets manager integration (AWS Secrets Manager / Vault /
  Doppler). Needs deploy-target decision first.
- TD-L4: admin service key rotation. Needs admin-portal-side JWT
  issuance to coordinate with.
- #19: coordinated crewai + litellm bump to retire the CVE
  ignore-list. Deliberate coordinated work per the pyproject comment.
