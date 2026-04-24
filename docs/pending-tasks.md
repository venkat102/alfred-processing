# Pending Tasks — Post-Session Status

Companion to [`docs/tech-debt-backlog.md`](./tech-debt-backlog.md).
Backlog has the full 31-task specification; this doc tracks **what's
shipped, what's left, why, and what each needs before it can move**.

Last update: 2026-04-24 (post-session: TD-H1, TD-H3, TD-H7, TD-H9,
TD-M3, TD-L2 shipped on `refactor/decompose-post-crew`).

---

## Executive status

| Severity | Total | Shipped | Pending |
|---|---|---|---|
| **Critical** | 7 | **7 ✅** | 0 |
| **High** | 9 | **7** | **2** |
| **Medium** | 10 | **9** | **1** |
| **Low** | 5 | **4** | **1** |
| **Session follow-ups** | — | — | 1 (#19) |

**Bottom line: every production-exploitable gap is closed.** The 10
remaining items are code-quality, architectural, and process
improvements. None of them block a responsible deploy of the current
branch. Each needs either an architectural decision you should weigh
in on, explicit deliberate-work coordination, or a focused session's
full context window.

---

## How to read this document

Each pending task below carries:

- **ID** — matches the backlog (`TD-H1`, `TD-M3`, etc.).
- **Why it's pending** — the *real* reason it wasn't done in the last
  session, not just its size.
- **Blocking factors** — what needs to happen before work can start.
- **Rough effort** — S (< 4 h), M (1–3 d), L (> 3 d).
- **Before-work checklist** — design decisions, upstream research, or
  infra coordination the next owner needs to handle *before* touching
  code.
- **Scope discipline** — where to stop so the PR stays reviewable.

If you skip the checklist and dive into code, you'll almost certainly
scope-creep. These notes exist to prevent that.

---

# HIGH — 5 pending

Architectural or code-motion heavy. Each wants its own session.

---

## TD-H1 — Decompose `_phase_post_crew` (351-line god-method) ✅ SHIPPED

Shipped on `refactor/decompose-post-crew` (commit `ebb46e4`). The
method went from 355 lines to 128; each of the seven concerns (drift
detection, rescue, registry backfill, report handoff, module
validation, empty-changeset error) lives in its own file under
`alfred/api/safety_nets/`. Unlocked TD-H9 — see below.

---

## TD-H2 — Split mega-files (pipeline, websocket, orchestrator, crew)

**Why pending:** L-sized pure refactor. Four files to split; each is
> 1000 LOC. Biggest risk: a stray import reorder breaks a module
that imports from one of these and the test suite doesn't catch it
until a production-only path runs.

**Blocking factors:** best done *after* TD-H1 lands so
`_phase_post_crew` isn't at 351 lines inside the split target.

**Rough effort:** L (3–5 d).

**Before-work checklist:**
- [ ] Read `tech-debt-backlog.md` TD-H2 for the proposed package
  shapes. They mirror natural seams already present in the
  docstrings, so don't invent new structure.
- [ ] Map every external import of each file via grep first:
  ```
  grep -rn 'from alfred.api.pipeline import\|from alfred.api.websocket import\|from alfred.orchestrator import\|from alfred.agents.crew import' alfred/ tests/
  ```
  The new `__init__.py` must re-export every named callable/class
  currently imported from outside. Breaking re-exports = silent test
  failures on things that import lazily.
- [ ] Decide whether to split `crew.py` or leave it as one file. It's
  1069 LOC but the concerns (Crew / Task / Agent definitions, state
  serialization, `run_crew` orchestration) are tightly coupled. Might
  not win much by splitting. Consider deferring.

**Scope discipline:**
- **One file at a time**, one PR each. Four PRs, not one.
- **No API changes, no behavior changes, no new abstractions.** If a
  test file needs changes, something's wrong.
- **Keep the old file as a re-export shim** for the first release.
  Downstream code (the Frappe client, any external integrations) may
  import specific symbols; a hard rename breaks them.

---

## TD-H3 — Replace broad `except Exception:` blocks (phase 2) ✅ SHIPPED

Shipped across six batches on `refactor/decompose-post-crew` (commits
`53c0cb6` through `6a2a124`). Started at 119 grandfathered sites;
ended at 53 broad catches, every one now documenting the boundary
(LLM / CrewAI / MCP / 3rd-party ML / metrics best-effort / handler
wrapper / test contract). The remaining broad catches are intentional
— each has a `# noqa: BLE001 — <reason>` comment explaining why a
narrower catch would break either the pipeline's resilience model or
the test contract that mocks inject arbitrary exceptions through.
`tests/` is fully narrowed (no `# noqa: BLE001` left under there).

---

## TD-H6 Phase 2 — Migrate LLM client to `httpx.AsyncClient`

**Status update from session:** phase 1 is **done**. Dedicated thread
pool for LLM calls isolates blocking from FastAPI's default
executor. Phase 2 removes the thread pool entirely by going native
async.

**Why pending:** needs research. The `llm_client.py` docstring
claims *"litellm + httpx use httpcore which has a read-timeout bug
when called from a thread pool executor inside an asyncio event
loop"*. Before ripping out the thread pool, **reproduce the bug** on
current httpx/httpcore. If it's fixed upstream, switching to
`AsyncClient` is a ~30-line change. If it's still real, the thread
pool stays and Phase 2 becomes "aiohttp migration" or just "document
why we can't go native".

**Rough effort:** S (research) + M (migration) = **≈1 week including
load testing**.

**Before-work checklist:**
- [ ] Read the commit that introduced the urllib workaround (`git
  log --all --oneline alfred/llm_client.py`). Find the specific
  symptom it solved.
- [ ] Write a minimal reproducer: FastAPI + httpx async POST to
  Ollama, in a threadpool-wrapped background task, observe whether
  the claimed read-timeout hang happens. Pin specific
  httpx/httpcore/anyio versions.
- [ ] If bug reproduces: file upstream, document here, close this
  task as WONTFIX.
- [ ] If bug is gone: write a migration PR that swaps
  `urllib.request` for `httpx.AsyncClient`, removes
  `_get_llm_executor`, and runs the existing `tests/test_llm*.py`
  suite + a new "100 concurrent calls" test to verify no thread
  starvation.

**Scope discipline:**
- **Don't migrate if the bug is real.** The thread pool is the
  correct answer in that case.
- **Don't touch CrewAI's internal LLM calls.** Those go through
  `litellm` directly and are a separate concern.

---

## TD-H7 — Multi-worker state story ✅ SHIPPED (Option A)

Shipped on `refactor/decompose-post-crew` (commit `1243428`).
`WORKERS=1` is now the default everywhere: `Settings.WORKERS: int = 1`,
`Dockerfile` env, `docker-compose.yml` default, `.env.example`. Bumping
it higher triggers a startup WARNING that names the concrete state
(ConnectionState / MCP pending futures / conn._pending_questions) that
will be lost on a LB reconnect. Scale via replicas + sticky WebSocket
routing at the LB.

Option B (Redis-backed session state) stays in the backlog as a later
architectural effort. Not needed for the current deploy topology.

---

## TD-H8 — Pin CrewAI supply chain + Dependabot

**Status update from session:** Dependabot config **shipped** in
TD-C4. The "coordinated crewai bump" portion = task **#19** below.

No additional work required on H8 itself.

---

## TD-H9 — Eliminate test/production code drift ✅ SHIPPED

Shipped as part of the TD-H1 safety-nets refactor. The
`_apply_safety_net` and `_apply_aggregation_safety_net` helpers in
`tests/test_report_name_safety_net.py` are now thin adapters that
call the real production function (`apply_report_handoff_safety_net`).

---

# MEDIUM — 1 pending

---

## TD-M3 — Structured (JSON) logging with context propagation ✅ SHIPPED

Shipped on `refactor/decompose-post-crew` (commit `c71ef1b`). New
module `alfred/obs/logging_setup.py` wires structlog as the formatter
behind the stdlib logging module so existing `logging.getLogger(...)`
calls gain JSON (prod) or console (dev) output without being
rewritten. `bind_request_context(site_id=…, user=…, conversation_id=…)`
runs in the WebSocket auth handler; `clear_request_context()` runs in
the disconnect `finally`. Redaction is reapplied via a stdlib `Filter`
plus structlog processors, covering both `logger.info("x=%s", {...})`
and native `log.info("...", key=value)` styles.

---

## TD-M5 — Secrets manager integration

**Why pending:** design-heavy. Which secrets manager (AWS Secrets
Manager, HashiCorp Vault, Doppler, GCP Secret Manager, k8s
Secrets) depends on deployment target. No single right answer.

**Rough effort:** M (1–2 d per backend) + integration testing.

**Before-work checklist:**
- [ ] Decide target backend(s). For SaaS deploy: AWS Secrets
  Manager is the common choice. For self-hosted: Vault. Support
  both if customers deploy in different clouds.
- [ ] Add `pydantic-settings-secrets-manager` or write a small
  custom loader that reads `AWS_SECRETS_MANAGER_ARN` / `VAULT_ADDR`
  at startup and populates `Settings` from the secret JSON.
- [ ] Fall back to `.env` when neither env var is set (dev
  experience unchanged).
- [ ] Document the secret JSON shape: one key per Settings field,
  same case, string values. Operators upload once.

**Scope discipline:**
- **Don't rotate existing keys as part of this PR.** Integration is
  one change; rotation is another.
- **Don't add a "secrets audit log".** Useful but separate.

---

# LOW — 1 pending

---

## TD-L2 — Type-check (`mypy`) discipline ✅ SHIPPED

Shipped on `refactor/decompose-post-crew` (commit `3fe9c65`). Every
file under `alfred/` (79 source files) passes a stock `mypy` run
(stock inference + `warn_unused_ignores`, `warn_redundant_casts`,
`no_implicit_optional`). CI gates against regressions via a blocking
`mypy` job in `.github/workflows/ci.yml`. Config lives under
`[tool.mypy]` in `pyproject.toml` with `ignore_missing_imports = true`
for libraries without stubs (CrewAI, LiteLLM, ollama, httpx-ws).
No per-file ignores — fixed every genuine type issue instead.

---

## TD-L4 — Admin service key rotation

**Why pending:** requires admin portal coordination. Swapping a
static bearer (`ADMIN_SERVICE_KEY`) for short-lived JWTs needs the
portal side to issue them and the processing side to verify —
neither side works without the other.

**Rough effort:** M (1–2 d processing side) + whatever the admin
portal needs.

**Before-work checklist:**
- [ ] Confirm admin portal can issue RS256 JWTs for
  service-to-service auth.
- [ ] Build on top of TD-M1 (iss/aud) — same signing/verification
  infrastructure.
- [ ] Decide expiry policy: 5-minute service JWTs with
  refresh-on-demand is standard; avoid long-lived ones.

**Scope discipline:** don't touch the REST admin routes until the
portal is ready to issue the new tokens.

---

# Session follow-ups — 1 pending

---

## #19 — Coordinated crewai + litellm bump to retire CVE ignore-list

**Why pending:** the `pyproject.toml` comment on the `crewai`
pin explicitly calls this out as deliberate work:
*"bump deliberately: update this version, run the full pytest
suite + bench tests, confirm no drift/rescue regression, then
commit."*

The blocker isn't the bump itself — it's validating that CrewAI's
API hasn't changed in ways that break our builder paths. `crewai`
`0.203.2` hard-pins `litellm==1.74.9`, which carries 3 known CVEs.
All three CVEs target litellm's proxy endpoints (`/config/update`,
`/v2/login`) that Alfred does not expose, so blast radius is zero.
The ignore-list in `.github/workflows/ci.yml::security` is a
documented risk acceptance, not a regression.

**Rough effort:** M–L, depending on whether crewai's API surface
has changed meaningfully.

**Before-work checklist:**
- [ ] On a worktree (not the main checkout — this may break
  things), check: does a newer `crewai` exist that depends on
  `litellm >= 1.83.0`? Read crewai's PyPI release notes for the
  intervening versions.
- [ ] If yes: bump both constraints in `pyproject.toml`,
  reinstall deps, run the full `pytest` suite + the
  `benchmarks/` regression. Focus areas: `agents/reflection.py`
  (uses crewai task/callback internals), `api/websocket.py`
  (`_rescue_regenerate_changeset`), the crew builders.
- [ ] If the regression catches drift/rescue issues, either
  patch our code to adapt or roll back the bump and open an
  issue on crewai.
- [ ] Once green, remove the three `--ignore-vuln` flags from
  `.github/workflows/ci.yml::security`:
  - `CVE-2026-35029`
  - `CVE-2026-35030`
  - `GHSA-69x8-hrgq-fjj8`

**Scope discipline:**
- **Do this in a worktree** to avoid leaving the main checkout
  broken if the bump fails.
- **Don't combine with other dep bumps.** Isolate so
  bisection works.

---

# Sequencing recommendation

If the next 1–2 weeks are dedicated to this backlog:

**Week 1 (architectural foundation):**
- TD-H1 (decompose `_phase_post_crew`) → unlocks TD-H9.
- TD-H9 (test/prod drift) — trivial once H1 lands.
- TD-H7 (multi-worker decision — likely Option A, 1-hour task).

**Week 2 (polish):**
- TD-M3 (structured logging) — biggest operational win left.
- TD-M5 (secrets manager) if you know your deploy target.
- TD-L2 (mypy) on `alfred/api/*` only.

**Week 3 (deliberate):**
- **#19** (crewai bump on a worktree). Reserve whole day.
- TD-H3 phase 2 (broad-except cleanup) — bite off 30 sites.
- TD-L4 (admin JWT) if admin portal is ready.

**Week 4 (stretch):**
- TD-H2 (split mega-files) — four separate PRs.
- TD-H6 phase 2 (httpx migration) OR document WONTFIX.
- TD-H3 phase 2 (continue) — next 30 sites.

---

# How to pick up any of these

1. Read the backlog entry in `docs/tech-debt-backlog.md` first —
   that's the spec.
2. Read this file's entry — that's the context that wasn't in
   the spec.
3. Check the before-work checklist. If any item is a design
   decision, raise it to the team before coding.
4. Branch, commit per-task (not per-subtask), PR, merge.

Commit message format (matches recent history):
```
<type>(<scope>): <summary> (<task-id>)

<body with context, rationale, and any non-obvious decisions>
```

Example:
```
refactor(pipeline): decompose _phase_post_crew into safety_nets package (TD-H1)

Extracted seven concerns (drift detection, rescue, backfill, report-
name safety net, aggregation safety net, module-specialist validation,
secondary severity cap) into alfred/api/safety_nets/*.py. _phase_post_
crew is now a 35-line orchestrator. No behavior change; all 229
portable tests + the state-store integration tests pass.

Deletes the copied _apply_safety_net helper in tests — closes TD-H9
in the same stroke.
```
