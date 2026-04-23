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
- TD-H1: decompose `_phase_post_crew` (351-line god-method).
- TD-H2: split mega-files (`pipeline.py`, `websocket.py`, `orchestrator.py`, `crew.py`).
- TD-H3: replace 109 broad `except Exception:` blocks with specific ones.
- TD-H6: dedicated LLM thread pool (pool-exhaustion risk at scale).
- TD-H7: multi-worker state story (single-worker-per-container or Redis-backed).
- TD-H8 / #19: coordinated crewai + litellm bump to retire the CVE ignore-list.
- TD-M1/M2/M3/M5, TD-L2/L3/L4: see `docs/tech-debt-backlog.md` for the
  full 31-task backlog + sequencing.
