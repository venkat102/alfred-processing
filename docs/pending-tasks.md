# Pending Tasks — Post-Session Status

Companion to [`docs/tech-debt-backlog.md`](./tech-debt-backlog.md).
Backlog has the full 31-task specification; this doc tracks **what's
shipped, what's left, why, and what each needs before it can move**.

Last update: 2026-04-24 (session checkpoint).

---

## Executive status

| Severity | Total | Shipped | Pending |
|---|---|---|---|
| **Critical** | 7 | **7 ✅** | 0 |
| **High** | 9 | 4 | **5** |
| **Medium** | 10 | 8 | 2 |
| **Low** | 5 | 3 | 2 |
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

## TD-H1 — Decompose `_phase_post_crew` (351-line god-method)

**Why pending:** architectural refactor. Not a mechanical change —
every extracted helper needs to be tested against the real pipeline
context, and breakage would be subtle (drift detection, rescue, three
different safety nets all reorder behavior).

**Blocking factors:** none — ready to start. Low risk if done
carefully with tests.

**Rough effort:** M (1–2 d).

**Before-work checklist:**
- [ ] Re-read `alfred/api/pipeline.py::_phase_post_crew` end-to-end
  once, no skimming. Note every mutation to `ctx.*` and every emitted
  WebSocket event. There are seven discrete concerns: drift detection,
  rescue, registry backfill, report_name safety net, aggregation
  safety net, module-specialist validation, secondary severity cap.
- [ ] Decide the package shape before writing code. Recommended:
  `alfred/api/safety_nets/{drift.py, rescue.py, backfill.py,
  report_name.py, aggregation.py, module_validation.py,
  severity_cap.py}`. Each module exports a single `apply_<name>(ctx)`
  function.
- [ ] Check `tests/test_report_name_safety_net.py` — its inline
  `_apply_safety_net` helper is a copy of production code. Once the
  real function exists, delete the copy and call the real one. Same
  for the aggregation safety-net tests.
- [ ] Run the full pipeline test suite (`tests/test_crew*.py`,
  `tests/test_pipeline*.py`) **before** touching code to establish
  a passing baseline.

**Scope discipline:**
- **No behavior changes.** Pure extraction. Every test should pass on
  both the old and new code. If a test starts failing, the refactor
  is wrong.
- **Don't introduce new phases or safety nets.** Migrating what's
  already there is the whole PR.
- **`_phase_post_crew` itself** should end at ≤ 40 lines — an
  orchestrator that calls the named helpers in documented order.

**Unlocks:** TD-H9 (test/prod drift elimination) — blocked on this.

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

## TD-H3 — Replace broad `except Exception:` blocks (phase 2)

**Status update from session:** phase 1 is **done**. Ruff's `BLE001`
rule is enabled. All 119 existing sites are grandfathered with
`# noqa: BLE001`. New broad-exception code fails CI.

**What's pending:** the actual per-site cleanup. Each grandfathered
site should be replaced with a specific exception class and the noqa
removed.

**Why pending:** 119 sites × per-site judgment = real code-motion
work. Each site needs the committer to understand *why* it was
catching broadly. Some are legitimate (best-effort telemetry);
others are bugs waiting to happen.

**Rough effort:** M (2 d) to go through all 119 carefully.

**Before-work checklist:**
- [ ] `grep -rn 'noqa: BLE001' alfred/ tests/ | wc -l` should return
  119 — confirm baseline.
- [ ] Categorize the sites into three buckets:
  1. **Legitimate broad catch** (boundary logging, shutdown paths,
     best-effort telemetry). Keep the `# noqa` but rewrite the
     comment to explain *why* broad is correct — e.g.
     `# noqa: BLE001 — metrics emission must never block the hot path`.
  2. **Should narrow** (caller cares about specific exceptions).
     Replace `except Exception:` with the specific class(es), remove
     the noqa.
  3. **Silent `pass`** — always wrong. At minimum add a
     `logger.debug(...)` with `exc_info=True` and a counter
     increment.
- [ ] Target working through 20–30 sites per PR, grouped by subsystem
  (all pipeline broad catches in one PR, all obs/tracer in another).

**Scope discipline:**
- **Don't add retries** where they don't exist. That's a different
  change.
- **Don't reshape error reporting** beyond narrowing the catch and
  adding context.

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

## TD-H7 — Multi-worker state story

**Why pending:** design decision first, code second. Two viable
options, neither obviously better without knowing deployment
targets.

**Rough effort:** S (Option A) to L (Option B).

**The decision:**

**Option A — Single-worker-per-container.** Documented. Scale
horizontally via multiple container replicas behind a sticky LB.
Simplest; matches how most small-to-mid FastAPI apps ship. Loses
nothing because current WebSocket state (`ConnectionState`, MCP
futures, `_pending_questions`) is already worker-local; moving to
workers > 1 is the regression, not the fix.

**Option B — Redis-backed session state.** All WebSocket-scoped
state moves to Redis keyed by `conversation_id`. Workers are truly
stateless. Reconnects can hit any worker. Meaningful design work:
MCP futures are in-memory on a specific worker and can't survive
worker loss, so you need either a "handoff" protocol or acceptance
that reconnect-during-pipeline cancels the pipeline.

**Recommendation:** **Option A for now.** Single worker per
container is fine for the expected deploy topology. Revisit if
you hit a point where you need > 1 worker per container for CPU
reasons.

**Before-work checklist (Option A):**
- [ ] Change `Dockerfile`: `ENV WORKERS=1`.
- [ ] Update `docker-compose.yml` scaling docs.
- [ ] Add a startup WARN in `alfred/main.py::lifespan` if
  `settings.WORKERS > 1`.
- [ ] Update `README.md` / `docs/SETUP.md` with horizontal-scale
  guidance: "Scale by increasing replica count on your
  orchestrator, NOT `WORKERS`."

**Before-work checklist (Option B — if chosen):**
- [ ] Write a design doc in `docs/specs/` first. Decide: reconnect
  during in-flight pipeline → cancel or resume?
- [ ] Migrate `ConnectionState.*` to Redis hashes with TTL.
- [ ] Design handoff protocol for MCP client futures.
- [ ] End-to-end test: kill worker mid-pipeline, reconnect, verify
  expected behavior.

**Scope discipline:** don't mix A and B. Pick one and ship it
cleanly.

---

## TD-H8 — Pin CrewAI supply chain + Dependabot

**Status update from session:** Dependabot config **shipped** in
TD-C4. The "coordinated crewai bump" portion = task **#19** below.

No additional work required on H8 itself.

---

## TD-H9 — Eliminate test/production code drift

**Why pending:** blocked on TD-H1 (decompose `_phase_post_crew`).
The copy-paste in `tests/test_report_name_safety_net.py` and the
aggregation safety-net tests only goes away after the safety nets
are standalone callable functions.

**Blocking factors:** TD-H1.

**Rough effort:** S (once H1 lands).

**Before-work checklist:**
- [ ] Wait for TD-H1.
- [ ] Then: delete the `_apply_safety_net` helpers in test files
  and replace with imports of the real production functions.
- [ ] Add a `grep` CI check that fails if a test file defines a
  function named `_apply_*` — locks in the rule permanently.

---

# MEDIUM — 2 pending

---

## TD-M3 — Structured (JSON) logging with context propagation

**Why pending:** touches every log call site. The value proposition
(`conversation_id`, `site_id`, `user`, `phase` bound per-request)
requires discipline at every emission point; a half-done migration
produces mixed plain/JSON logs that are worse than either alone.

**Rough effort:** M (2 d) to migrate everything cleanly.

**Before-work checklist:**
- [ ] Add `structlog` to deps.
- [ ] Configure `structlog` + `logging` bridging in
  `alfred/main.py` (JSONRenderer in prod, ConsoleRenderer in dev).
  Keep the existing `RedactingFormatter` from TD-C7 — its
  redaction logic moves into a structlog processor.
- [ ] Write a middleware that binds `site_id`, `user`,
  `conversation_id` as context vars per WebSocket message and per
  REST request.
- [ ] Migrate logger calls in this order: `alfred/main.py`,
  `alfred/api/*`, `alfred/handlers/*`, then the rest. Each file
  should use `structlog.get_logger(__name__)`, then
  `log.info("event_name", key=value, ...)`.
- [ ] Verify the Prometheus counters keep firing — structlog's
  processors shouldn't touch them but an import reorder could.

**Scope discipline:**
- **All-or-nothing.** Do NOT leave `alfred/tools/*` on `logging`
  while migrating `alfred/api/*` to structlog. Mixed output is
  harder to query than uniform plain text.
- **Keep log levels.** Don't "fix" DEBUG-vs-INFO classification in
  the same PR.

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

# LOW — 2 pending

---

## TD-L2 — Type-check (`mypy`) discipline

**Why pending:** 109 pre-existing type errors per the backlog
estimate. Fixing them is mostly mechanical (adding return type
annotations, narrowing `dict` to `dict[str, Any]`, handling
`None`-returning paths), but it's 109 individual judgments.

**Rough effort:** M (1 d per 30 sites, so 3–4 days total).

**Before-work checklist:**
- [ ] Add `mypy` to dev deps.
- [ ] Start with `mypy alfred/api/ alfred/orchestrator.py` — the
  hot path. Grandfather `alfred/agents/crew.py` and
  `alfred/api/pipeline.py` with per-file ignores (they're
  scheduled for TD-H2 refactor; type-check after the split).
- [ ] Wire into CI as informational initially; promote to
  blocking once `alfred/api/*` is clean.

**Scope discipline:**
- **One directory per PR.** Don't try to fix 109 sites in one
  review.
- **Don't add `# type: ignore` with no comment.** Every ignore
  needs a reason.

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
