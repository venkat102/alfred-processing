# Observability

Consolidated reference for how alfred_processing exposes operational signals.
Five independent surfaces, each answers a different question:

| Surface | Answer to | Where |
|---|---|---|
| Structured logs | "what's happening right now, line by line" | stdout (Docker picks it up), log-level configurable |
| Prometheus metrics | "how is the fleet doing, aggregated, over time" | `GET /metrics` |
| Span tracer | "how long did each pipeline phase take, with context" | JSONL file + optional stderr |
| Event stream (Redis) | "replay what this conversation actually emitted" | `alfred:{site_id}:events:{conversation_id}` stream |
| Admin portal usage reports | "how many tokens / conversations are billed to which site" | HTTP POST to admin portal |

Read this doc once; future changes to an observability surface should update it.

## 1. Logs

### Setup (`alfred/main.py:13-22`)

```
level=DEBUG, stream=stdout, format='%(asctime)s %(name)s %(levelname)s: %(message)s'
```

Root level is `DEBUG` but per-logger overrides trim production noise:
- `alfred.*` stays at DEBUG (application logs)
- `websockets` / `httpcore` / `LiteLLM` drop to WARNING (library chatter)

### Module loggers

Every module uses `logger = logging.getLogger("alfred.<area>")`. Common names:

| Logger | Used for |
|---|---|
| `alfred.auth` | REST + WS auth paths |
| `alfred.crew` | CrewAI dispatch, specialist selection |
| `alfred.defense` | Prompt sanitizer verdicts |
| `alfred.llm_client` | Ollama HTTP calls, timeouts, retries |
| `alfred.pipeline` | Pipeline phase lifecycle |
| `alfred.ratelimit` | Rate-limit hits |
| `alfred.state` | Redis reads/writes |
| `alfred.tracer` | Tracer own-diagnostics |

### PII + secret discipline

INFO lines carry metadata only (user email, site_id, msg_id, lengths, status codes). User-content-derived strings (prompts, clarifier answers, LLM raw output) are logged at DEBUG only.

Call sites that followed this split after 2026-04-24:

- `alfred/tools/user_interaction.py:66, 73` — clarifier question send + user-response receive
- `alfred/agents/reflection.py:204` — reflection LLM output
- `alfred/api/websocket.py:1041` — clarify LLM output

Any new logger call touching user content must follow the same pattern: INFO = lengths + ids, DEBUG = text.

Secrets are never logged. `API_SECRET_KEY` + `llm_api_key` only appear in settings objects that are never pretty-printed.

## 2. Prometheus metrics (`alfred/obs/metrics.py`)

Scrape at `GET /metrics` (mounted in `alfred/main.py:130` via `make_asgi_app`). No auth on this endpoint; firewall externally.

### Metrics shipped

| Metric | Type | Labels | What it answers |
|---|---|---|---|
| `alfred_pipeline_phase_duration_seconds` | Histogram | `phase` | "Which pipeline phase regressed when p99 blew up?" |
| `alfred_mcp_calls_total` | Counter | `tool`, `outcome` | "Is an agent stuck in a tool-call loop?" |
| `alfred_orchestrator_decisions_total` | Counter | `mode`, `source`, `confidence` | "Is the mode classifier LLM actually running, or is the fallback eating everything?" |
| `alfred_llm_errors_total` | Counter | `tier`, `error_type` | "Is Ollama down?" |

### What's deliberately NOT a metric

- LLM success throughput → the tracer's span duration already captures it
- Per-conversation event counts → the Redis event stream is the source of truth
- Individual user actions → privacy concern; aggregate via tracing infra if needed

### Adding a new metric

Register it in `alfred/obs/metrics.py` using the default registry (`DEFAULT_REGISTRY` from prometheus_client). `make_asgi_app()` picks it up automatically.

## 3. Span tracer (`alfred/obs/tracer.py`)

Zero-dep async-safe span tracer. Call-site API matches OpenTelemetry's context manager so switching to a real OTel SDK later is mechanical.

### Enabling

```sh
ALFRED_TRACING_ENABLED=1
ALFRED_TRACE_PATH=./alfred_trace.jsonl     # default
ALFRED_TRACE_STDOUT=1                       # optional stderr summary
```

Default OFF. Enable in production selectively if you need phase-level timing.

### What traces contain

Each span is a JSONL object: `{name, attrs, events, status, duration_s, start, end, trace_id, span_id, parent_span_id, error?}`.

Spans carry metadata only (phase name, module, intent, conversation_id, duration, status). Spans do NOT carry user prompts, replies, or LLM output. Verified: no `tracer.span(...)` caller passes user content as attrs.

### Path safety (`ALFRED_TRACE_PATH` whitelist)

`_safe_trace_path()` rejects inputs that would write outside a permitted-root whitelist (CWD, `$HOME`, `tempfile.gettempdir()`, `/tmp`, `/var/tmp`). Inputs with `..` components or targets outside the whitelist fall back to the default with a WARNING.

The cap defends against an attacker who can set process env vars (container env injection, CI secret leakage) from redirecting trace writes to `/etc/systemd/system/…override` or similar. Tests: `tests/test_tracer_path_validation.py`.

### Exporters

- `jsonl_file_exporter(path)` — append one JSON object per line
- `stdout_exporter(span)` — human-readable summary to stderr
- Register additional exporters via `tracer.register_exporter(callable)`; each gets the finished span dict

## 4. Event stream (Redis — `alfred/state/store.py`)

Every user-visible WebSocket message is mirrored into a Redis stream keyed `alfred:{site_id}:events:{conversation_id}`. Used by the `resume` WS handler to replay events after a client reconnect (see `developer-api.md` for the WS contract).

### What's persisted

`ConnectionState.send()` appends to the stream automatically. Persisted: `agent_status`, `agent_activity`, `changeset`, `chat_reply`, `insights_reply`, `plan_doc`, `info`, `error`, `minimality_review`, `clarify`, `validation`, `question`, `run_cancelled`, `mode_switch`, `auth_success`, others by omission.

Skipped (transport/meta): `ack`, `ping`, `mcp_response`, `echo` — see `_STREAM_SKIP_TYPES`.

### Retention

- `maxlen` = 10,000 events per conversation (oldest trimmed on push)
- Key-level TTL = 7 days; refreshed on every `push_event`. Active conversations stay alive indefinitely; a silent conversation auto-reaps.
- TTL is configurable via `StateStore(stream_ttl_seconds=N)`; `0` disables auto-expiry.

### Read paths

- WS `resume` handler (`alfred/api/websocket.py` `_handle_custom_message`)
- REST `/api/v1/tasks/{task_id}/messages?since_id=…` (returns events since a stream ID)

### PII posture

Events contain the full WS payload, so they include user-visible content (agent replies, changeset contents, clarifier questions). The stream is tenant-scoped (`site_id` is part of the key) and only readable by the session owner's code path. Redis itself should be behind auth and not exposed externally — that is the trust boundary; the stream does not add additional protection.

## 5. Admin portal usage reports (`alfred/api/admin_client.py`)

SaaS-only: when `ADMIN_PORTAL_URL` + `ADMIN_SERVICE_KEY` are configured, the pipeline calls the admin portal for plan checks + usage reports.

Self-hosted: omit those env vars and this path is skipped entirely (`alfred/api/pipeline.py:944` short-circuits).

### check_plan

Invoked in `_phase_plan_check`. Returns `{allowed, tier, warning?, reason?, pipeline_mode?}`. Cached in Redis (`alfred:{site_id}:plan_cache`) for a short TTL so hot paths don't call out on every prompt.

**Policy note — fail-open on outage:** if the admin portal is unreachable and there's no cached verdict, `check_plan` returns `{allowed: True, tier: "offline", reason: "Admin Portal unreachable"}`. Intentional trade-off: customer UX > perfect billing accuracy during an outage. Out-of-band reconciliation (quota audits) is expected to catch overages after the portal recovers. Flip to fail-closed by editing `admin_client.py:65-66` if your deployment prefers reject-during-outage.

### report_usage

Fire-and-forget. Failures are queued in Redis and flushed when the portal recovers.

## Env-var index

All observability-adjacent env vars in one table:

| Var | Default | Effect |
|---|---|---|
| `ALFRED_TRACING_ENABLED` | off | Enable span tracer |
| `ALFRED_TRACE_PATH` | `./alfred_trace.jsonl` | JSONL output (whitelisted roots) |
| `ALFRED_TRACE_STDOUT` | off | Also emit stderr summary |
| `CREWAI_DISABLE_TELEMETRY` | `true` (set by `main.py:16-19`) | Opt out of CrewAI phone-home |
| `CREWAI_DISABLE_TRACKING` | `true` (set by `main.py`) | Opt out of CrewAI tracking |
| `OTEL_SDK_DISABLED` | `true` (set by `main.py`) | Skip OTel SDK cold-start |
| `ADMIN_PORTAL_URL` | empty | Disables admin portal integration when empty |
| `ADMIN_SERVICE_KEY` | empty | Same |

## Troubleshooting

**Traces aren't appearing.** Check `ALFRED_TRACING_ENABLED=1`. Look for the "Rejecting ALFRED_TRACE_PATH" warning in logs — the whitelist may have rejected your configured path.

**`/metrics` returns 404.** `prometheus_client` isn't installed or `app.mount("/metrics", ...)` failed at startup. Check startup logs for import errors.

**CrewAI telemetry is still hitting the network.** Confirm env vars are set BEFORE any `from crewai import …`. `alfred/main.py` sets them at module-import time via `os.environ.setdefault`; if you start the app with `python -c "import alfred.something_else"` you bypass that.

**Resume replay sends nothing on reconnect.** Three common causes: (1) client didn't include `last_msg_id` in the `resume` payload, (2) Redis is unreachable so the stream is empty, (3) the client's `last_msg_id` was trimmed out of the 10k/7d window — server replays the full remaining window, client should dedupe by `msg_id`.

**Admin portal plan check is "always allowed".** Either (a) `ADMIN_PORTAL_URL` is empty (self-hosted mode) and the plan-check phase is skipped, (b) the portal is reachable and actually returns `allowed: True`, or (c) the portal is unreachable and fail-open kicked in (log: "Admin Portal unreachable for plan check"). Cross-check in logs.
