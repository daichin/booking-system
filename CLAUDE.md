# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository status

Task 0 has landed. **Read `CONTRACT.md` first** — it is the locked output of spec §11 Task 0 and fixes the stack, module ownership, service interfaces, and the HTTP contract. Do not change what it specifies without raising it.

The stack: **Python 3.13, standard library only** in development and test. Production adds exactly two packages (`psycopg[binary]`, `gunicorn` — see `requirements.txt`). Neon Postgres, Render hosting, Brevo email, GitHub Actions for deploy, cron-job.org for the reminder schedule.

### Commands

```bash
python -m unittest discover -s tests -t .        # whole suite
python -m unittest tests.test_preemption -v      # one module
python -m unittest tests.test_preemption.LevelRuleTests.test_c1_higher_level_preempts_lower
python -m compileall -q app tests manage.py serve.py   # lint

python serve.py                  # run locally on SQLite; no setup required
python manage.py migrate         # migrate + seed settings, admin, example rooms
python manage.py check-secrets   # names any missing deploy secret
python manage.py health --url https://HOST/api/health --retries 20
```

The suite needs no setup and no network: each test gets a fresh temporary SQLite database. Set `TEST_DATABASE_URL` to run the *same* suite against a real Postgres (each test gets its own schema); CI does this so the production backend is genuinely exercised.

### Layout

`app/db/` backends and migrations · `app/services/` all business logic · `app/web/` WSGI layer, `pages/` per area · `app/i18n/` zh-TW strings · `tests/`.

Acceptance coverage is enforced: `tests/test_acceptance.py` fails the build if any spec §12 scenario loses the test named after it.

## The spec is the source of truth

`meeting_room_booking_spec.md` is marked *locked* and confirmed with the product owner. Per its cross-cutting rules: if you find a contradiction between the spec and reality (e.g. a free tier that no longer exists), **stop and report it — do not improvise**. Anything the spec does not specify is the implementer's choice, provided it does not violate §2.

## Constraints that shape every technical decision (§2)

- **US$0/month, permanent free tiers only.** No trial credits, no paid analytics or error tracking.
- **DB free tier must not expire data** — this rules out Render Postgres (90-day expiry). Idle suspension is fine; data loss is not.
- **Deploy is a GitHub web-UI button** (`workflow_dispatch`). The owner is non-technical and may only have a phone. No local terminal in the normal path.
- **Free platform subdomain only** (`*.vercel.app`, `*.onrender.com`). Never depend on owning a domain.
- **Cron via cron-job.org** calling a secret-protected endpoint — no paid scheduler. Spec §2 named GitHub Actions `schedule`; the owner replaced it because GitHub's scheduler drifts under load and disables itself after 60 days without a push. cron-job.org is free with no card, but caps a request at **30 seconds**, which is shorter than Render's ~60s cold start — so a second job pings `/api/health` every 10 minutes to stop the host ever sleeping. Without it every reminder call times out, and 25 consecutive failures switch the job off automatically.
- **Email free tier ≥300/day**; budget ~200/day for 200 users.
- Verify every provider's current free tier at build time; the §2.1 candidate list is non-binding and dated August 2026.

## Cross-cutting invariants

- **No hardcoded business constants.** Every tunable in §5 (`slot_minutes`, `max_booking_minutes`, `preemption_protection_minutes`, `quota_by_level`, token lifetimes, `daily_email_cap`, …) is read from the `settings` table at runtime, admin-editable.
- **Times stored UTC, displayed `Asia/Taipei`.** Every email states the local time with an explicit timezone label.
- **All user-facing text is zh-TW, in a single i18n file** so it can be swapped later.
- **Never change the DB schema or API contract unilaterally** — raise it to the orchestrator.
- Booking rows are never deleted. Only `status = 'confirmed'` occupies a room; `cancelled_*` and `preempted` rows are history, kept forever. Same for the audit trails in FR-7.
- `bookings.level_at_booking` is an **audit-only snapshot**. Preemption decisions always use the owner's *current* `users.level` (§7.3, test C8).

## Preemption engine (§7) — the highest-risk component

Implement **once, in one service function**, with an exhaustive unit + concurrency test suite. It must be independently testable with fixtures. Per §11, it cannot merge without a separate reviewing agent checking concurrency behaviour.

Rules that are easy to get subtly wrong:

- Only a **strictly higher** current level preempts. Equal level never wins.
- The protection window is evaluated against the **victim's** `start_at`, not the new booking's: a booking is immune once `now >= victim.start_at - preemption_protection_minutes`.
- Any overlap cancels the victim's booking **in its entirety** — no splitting or trimming.
- **All-or-nothing**: if a request overlaps several bookings and any one is non-preemptible, the whole request is rejected and nothing changes.
- Overlapping your own booking is `BLOCKED: SELF_OVERLAP`, never a self-preemption.
- Admin status grants no preemption privilege; only `level` matters. (Admin **is** the exemption for booking over a room closure — that is a §6.5 validation rule, not a §7 conflict, and it was the owner's explicit decision.)
- **Two-phase UX is required.** Phase 1 returns `AVAILABLE` / `PREEMPTION_REQUIRED(victims)` / `BLOCKED(reason)`. Phase 2 commits only on explicit confirmation and **re-runs the entire check inside the transaction** — never trust the phase-1 result.
- Concurrency: `SELECT … FOR UPDATE` on the overlap set, or `SERIALIZABLE` + retry-on-conflict. Two simultaneous attempts on the same victim must yield exactly one winner, one victim record, and one E5 email.
- **Emails are enqueued only after commit.** Sending inside the transaction risks a "your booking was cancelled" email for a booking that a rollback preserved.
- Preemption must cancel the victim's pending E10 reminder.
- E5 names the room and time but **never the preempting user**; `BLOCKED` responses expose the blocker's name and department, never their email.

## Room closures

An admin may close a date+time range of one room (`room_closures`, migration 5) — a rule, not a list of days, so a six-week cleaning slot is one row. The spec is silent on this; §13's "recurring bookings" is a member-facing feature about repeating *reservations*, not room availability. Checked as **step 7b** of §6.5, between the open/close window and the quota: both 7 and 7b answer "the room is not bookable then", while 8 answers "you have booked too much". **Admins are exempt** (see above). Overlap is half-open and lives in one function, `closures.overlaps`, used by both booking validation and conflict detection so the two can never disagree.

## Booking validation order (§6.5)

Validate in the spec's order with a distinct error message per failure: active user → active room → 30-min boundaries and `end > start` → duration ≤ max → start in future → within horizon → inside the room's open/close window and not crossing midnight → level quota on *future confirmed* bookings → conflict resolution per §7.

## Account lifecycle (§6.1)

```
pending_email --verify--> pending_approval --approve--> active
                                           --reject---> rejected
(invite link)  ------------------------------------> active
active --admin suspend--> suspended --reactivate--> active
```

The invitation path is deliberately shorter: clicking an admin-issued invite link proves the address, so the account is created directly as `active` with `email_verified_at` set — **no verification email and no approval step**. Pending members can log in and view availability read-only; every booking action must explain they are awaiting approval. Registration attempts must never confirm or deny that an account exists.

## Email subsystem (§9)

Ten templates E1–E10 behind a provider adapter interface, with a **fake transport for tests**. `email_log` backs retries (3 with backoff), the admin log, and the daily quota guard. When `daily_email_cap` is exceeded, **drop E10 reminders first** — E1, E5, E8, E9 must still send. E7 admin notices are batched to at most one digest per admin per hour. The reminder cron endpoint must be **idempotent**: a double invocation must not double-send. Known limitation to document in `SETUP.md`: cron-job.org switches a job off after 25 consecutive failures, so the admin dashboard must surface "last reminder job ran at ___".

## Deployment deliverables (§10)

`.github/workflows/deploy.yml` (`workflow_dispatch`: install → lint → test → build → migrate → deploy → smoke-test `/api/health` → print the live URL; fails loudly naming any missing secret), the reminder schedule (two cron-job.org jobs, documented in `SETUP.md` because it lives outside the repository), `SETUP.md` and `ROLLBACK.md` **written in Traditional Chinese for a non-developer**, and idempotent first-run seeding (settings row, admin account from secrets, 3 example rooms).

Secrets, named exactly: `DATABASE_URL`, `EMAIL_PROVIDER_API_KEY`, `EMAIL_FROM_ADDRESS`, `APP_BASE_URL`, `SESSION_SECRET`, `ADMIN_EMAIL`, `ADMIN_INITIAL_PASSWORD`, `CRON_SECRET`, plus the host's deploy token.

The seeded admin account **must be forced to change its password on first login**.

## Definition of done

Spec §12 groups A–E are the acceptance tests; Task 8 implements them as automated tests plus a manual pass on a real phone. The build is not done until every scenario passes. Group C (preemption) is the critical set. Mobile-responsive is a requirement, not a nice-to-have.

## Out of scope for v1 (§13)

Calendar/ICS integration, recurring bookings, check-in/no-show tracking, attendee invitations, SSO/LDAP, custom domains, languages beyond zh-TW, mobile apps, waiting lists, automatic rebooking suggestions, per-booking approval workflows.


# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

