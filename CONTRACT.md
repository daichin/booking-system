# CONTRACT.md — Task 0 Foundation output

**Status:** locked. Spec §11 forbids any other task starting before this exists.
Nothing here may be changed unilaterally; raise it to the orchestrator.

---

## 1. Stack decision and justification

| Layer | Choice | Why it satisfies §2 |
|---|---|---|
| Language | **Python 3.13**, standard library only in dev/test | Nothing to install to run the suite; keeps CI fast and free |
| Web | **WSGI app**, hand-rolled router in `app/web/` | No framework dependency; runs under `wsgiref` locally and `gunicorn` in production |
| Production deps | `psycopg[binary]`, `gunicorn` (see `requirements.txt`) | Installed by CI/host, which have network access |
| Database | **Neon Postgres** (prod) / **SQLite** (dev+test) | C1, C6 — Neon's free tier is permanent, 0.5 GB, no card, **data never expires**; unlike Render Postgres which expires at 90 days |
| Hosting | **Render** free web service | C1, C3, C4 — free, no card, `*.onrender.com` subdomain, deploys from a GitHub Actions step. Sleeps after 15 min idle with ~60 s cold start, which §2.1 explicitly tolerates |
| Email | **Brevo** transactional API | C5 — 300 emails/day, full API access, no card, no time limit |
| Cron | **GitHub Actions `schedule`** → `POST /api/cron/send-reminders` | C7 — no paid scheduler |
| Deploy | **GitHub Actions `workflow_dispatch`** → Render deploy hook | C3 — one button in the GitHub web UI |
| Timezone / language | UTC storage, `Asia/Taipei` display, zh-TW | C8 |

Free tiers were re-verified on 2026-08-25 as §2.1 requires. Sources:
[Neon free plan](https://neon.com/blog/how-to-make-the-most-of-neons-free-plan),
[Brevo email API](https://www.brevo.com/features/email-api/),
[Render free tier 2026](https://render.com/articles/platforms-with-a-real-free-tier-for-developers-in-2026).

### Why two database backends

The spec's production requirements (C6) demand hosted Postgres. The test suite
must run with zero setup. Both backends sit behind `app/db/base.py` and are
given the **same write-serialisation guarantee**, which is the only property
the preemption engine depends on:

* Postgres: `BEGIN ISOLATION LEVEL SERIALIZABLE` + `SELECT … FOR UPDATE` on the
  overlap set, with automatic retry on serialisation failure.
* SQLite: `BEGIN IMMEDIATE`, which holds the database write lock for the whole
  transaction and therefore serialises writers outright. `Connection.for_update()`
  returns `""` there because the coarser lock subsumes it.

Spec §7.3 offers a `tstzrange` exclusion constraint as the strongest option but
notes it must be dropped and re-added around a preemption commit. We take the
`FOR UPDATE` / `SERIALIZABLE` branch the same sentence offers, because it needs
no DDL churn on the hot path.

---

## 2. Repository layout and file ownership

Each task owns its files exclusively. Do not edit another task's files; if you
need a change there, raise it.

```
app/
  __init__.py        config.py       errors.py        models.py
  security.py        settings.py     timeutil.py                   [Task 0]
  db/                base.py  __init__.py  migrations.py           [Task 0]
  i18n/              __init__.py  zh_TW.py         [Task 0 owns; all tasks append keys]
  web/               framework.py  html.py  layout.py  app.py      [Task 0]
    pages/           auth_pages.py                                 [Task 1]
                     member_pages.py                               [Task 5]
                     admin_pages.py                                [Task 6]
  services/
    accounts.py      sessions.py                                   [Task 1]
    mailer.py        templates.py    transports.py                 [Task 2]
    rooms.py         bookings.py                                   [Task 3]
    preemption.py                                                  [Task 4]
    audit.py                                                       [Task 0]
tests/
  support.py  test_foundation.py                                   [Task 0]
  test_accounts.py                                                 [Task 1]
  test_email.py                                                    [Task 2]
  test_rooms.py  test_bookings.py                                  [Task 3]
  test_preemption.py                                               [Task 4]
  test_acceptance.py                                               [Task 8]
.github/workflows/  deploy.yml  reminders.yml  ci.yml              [Task 7]
SETUP.md  ROLLBACK.md                                              [Task 7]
```

---

## 3. Conventions every task must follow

1. **Time.** Store UTC; display Taipei. Only `app.timeutil` converts. Never call
   `datetime.now()` directly — use `timeutil.now_utc()`.
2. **Settings.** Every §5 tunable is read from the database via
   `Settings.load(conn)`. Importing a value from `app.settings.DEFAULTS` as a
   constant is a contract violation.
3. **Strings.** No user-facing sentence outside `app/i18n/zh_TW.py`. Add keys
   under your area's namespace; never edit another task's keys.
4. **Errors.** Raise `AppError(CODE, details)` using codes from `app.errors`.
   Add new codes there and a matching `error.<CODE>` string.
5. **Transactions.** All writes go through `db.run_in_transaction(work)`. The
   callable may be **retried**, so it must contain no side effects outside the
   database.
6. **Email is enqueued after commit — never inside a transaction.** Spec §7.3:
   a rollback would otherwise send "your booking was cancelled" for a booking
   that still exists.
7. **Privacy.** Never return another member's email address to a non-admin.
   `User.public_view()` is the safe projection (name, department, level).
8. **Tests.** Subclass `tests.support.AppTestCase`. Use its factories, and
   `taipei_at(days_ahead, hour, minute)` to express booking times the way a
   member would.

---

## 4. Database schema

Defined in `app/db/migrations.py`, both dialects side by side. Tables:
`users`, `rooms`, `bookings`, `email_tokens`, `preemption_log`, `settings`,
`email_log`, `sessions`, `login_attempts`, `audit_log`, `cron_runs`,
`schema_migrations`.

Deviations from spec §4, all deliberate:

* UUID keys are 36-char strings in both backends (avoids per-dialect casting).
* Room hours are `open_minutes` / `close_minutes` integers past local midnight
  rather than `TIME`, because the booking grid is minute arithmetic.
* Added beyond §4: `sessions`, `login_attempts` (FR-2 rate limiting),
  `audit_log` (FR-7), `cron_runs` (§9.3 "last reminder job ran at"),
  `users.must_change_password` (§10.3), `email_log.dedupe_key` (§9.3
  idempotency, enforced by a unique index so a double cron invocation
  physically cannot double-send).

---

## 5. Service interfaces

Signatures are binding. `db` is an `app.db.Database`.

### Task 1 — `app/services/accounts.py`

```python
register(db, *, email, password, full_name, department, phone) -> RegisterResult
verify_email(db, raw_token) -> User
resend_verification(db, email) -> None
accept_invitation(db, raw_token, *, password, full_name, department, phone) -> User
invite(db, actor, emails: list[str], level: int | None) -> list[InviteResult]
revoke_invitation(db, actor, token_id) -> None
approve(db, actor, user_id) -> User
reject(db, actor, user_id) -> User
set_level(db, actor, user_id, level) -> User
set_suspended(db, actor, user_id, suspended: bool) -> User
authenticate(db, email, password) -> User          # raises AuthError / RateLimitError
request_password_reset(db, email) -> None
reset_password(db, raw_token, new_password) -> User
change_password(db, user, current_password, new_password) -> User
```

`RegisterResult` and `InviteResult` are dataclasses carrying
`emails: list[EmailEvent]` for the caller to enqueue after commit.

Spec rules that are easy to miss: re-registering a `pending_email` address
resends rather than duplicating; re-registering an `active` address returns the
same generic "check your email" response and sends the "you already have an
account" mail; the UI must never confirm or deny that an account exists.

### Task 1 — `app/services/sessions.py`

```python
create_session(db, user) -> tuple[str, datetime]   # (raw cookie value, expires)
resolve_session(db, raw_cookie) -> User | None
revoke_session(db, raw_cookie) -> None
revoke_all_for_user(db, user_id) -> int            # used by password reset (A7)
```

Cookie name `session`; flags `HttpOnly`, `SameSite=Lax`, and `Secure` whenever
the request is HTTPS.

### Task 2 — `app/services/mailer.py`

```python
class EmailEvent:  kind: str; to_email: str; context: dict
                   related_booking_id: str | None = None
                   dedupe_key: str | None = None

enqueue(db, events: list[EmailEvent]) -> list[str]     # returns email_log ids
send_pending(db, *, limit=50) -> SendReport            # retries, honours the cap
run_reminders(db) -> ReminderReport                    # E10, idempotent
run_admin_digest(db) -> int                            # E7, ≤1/admin/hour
```

Kinds are `E1`…`E10`. Cap behaviour (§9.4): when `daily_email_cap` is reached,
`E10` is dropped and logged with status `skipped`; `E1`, `E5`, `E8`, `E9` still
send. Transport is chosen by `Config.email_transport` (`fake` in tests).

### Task 3 — `app/services/rooms.py`

```python
list_rooms(db, *, include_inactive=False) -> list[Room]
create_room(db, actor, **fields) -> Room
update_room(db, actor, room_id, **fields) -> Room
set_active(db, actor, room_id, active: bool, *, cancel_bookings=False)
    -> DeactivationResult      # raises CONFIRMATION_REQUIRED if future bookings exist
availability(db, *, day: date, room_ids=None) -> list[RoomDay]
```

### Task 3 — `app/services/bookings.py`

```python
validate_request(conn, *, requester: User, room: Room, start_at, end_at, title,
                 settings) -> None          # FR-5 steps 1-8, in order, raises AppError
future_confirmed_count(conn, user_id) -> int
cancel_booking(db, *, actor: User, booking_id: str) -> CancelResult
list_for_user(db, user_id) -> tuple[list[Booking], list[Booking]]   # upcoming, past
```

`validate_request` must run the checks in exactly the order of §6.5 so the
first failure reported matches the spec.

### Task 4 — `app/services/preemption.py`

```python
attempt_booking(db, *, requester_id, room_id, start_at, end_at, title,
                confirm_preemption: bool = False,
                dry_run: bool = False) -> BookingAttempt
```

```python
@dataclass
class Victim:      booking: Booking; owner_view: dict     # public_view() only
@dataclass
class BookingAttempt:
    outcome: str                    # AVAILABLE | PREEMPTION_REQUIRED | BLOCKED | CREATED
    booking: Booking | None = None
    victims: list[Victim] = []
    reason: str | None = None       # SELF_OVERLAP | EQUAL_OR_HIGHER_LEVEL | PROTECTED_WINDOW
    blocker: dict | None = None     # room, time range, owner public_view
    emails: list[EmailEvent] = []
```

* `dry_run=True` is **phase 1**: never writes; returns `AVAILABLE`,
  `PREEMPTION_REQUIRED`, or `BLOCKED`.
* `dry_run=False` is **phase 2**: re-runs the entire check inside the
  transaction. A phase-1 result is never trusted.
* Emails are returned, not sent; `attempt_booking` enqueues them itself only
  after the transaction commits.

### Task 0 — `app/services/audit.py`

```python
record(conn, *, actor_id, action, target_type=None, target_id=None, detail=None)
```

Call inside the same transaction as the change being audited (FR-7).

---

## 6. HTTP contract

All HTML responses are zh-TW and mobile-responsive. All JSON errors use
`{"error": "<CODE>", "details": {...}}` with the status from `AppError.status`.

### Pages

| Method | Path | Access | Notes |
|---|---|---|---|
| GET | `/` | any | redirects to `/day` or `/login` |
| GET POST | `/login` | public | |
| POST | `/logout` | member | |
| GET POST | `/register` | public | |
| GET | `/verify` | token | `?token=` |
| GET POST | `/invite` | token | `?token=`, email field locked |
| GET POST | `/forgot` | public | |
| GET POST | `/reset` | token | `?token=` |
| GET POST | `/password` | member | forced after first admin login (§10.3) |
| GET | `/day` | member | `?date=`; all rooms as columns |
| GET | `/week` | member | `?room_id=&date=` |
| GET | `/my` | member | upcoming + past |
| GET | `/admin/...` | admin | approvals, members, invitations, rooms, bookings, preemptions, settings, emails |

Pending members reach `/day` and `/week` read-only; every booking control shows
`error.AWAITING_APPROVAL`.

### JSON API

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/availability?date=&room_id=` | — | `{rooms: [{id, name, open, close, bookings: [...]}]}` |
| POST | `/api/bookings/check` | `{room_id, start_at, end_at, title}` | `{outcome, victims?, reason?, blocker?}` |
| POST | `/api/bookings` | same + `{confirm_preemption: bool}` | `{outcome, booking?, displaced?}` |
| POST | `/api/bookings/{id}/cancel` | — | `{ok: true}` |
| GET | `/api/health` | — | `{version, database, last_reminder_run, time}` |
| POST | `/api/cron/send-reminders` | — | `{sent, skipped, failed}`; header `X-Cron-Secret` |

`start_at` / `end_at` are ISO-8601 with offset. Booking JSON exposes
`owner: {full_name, department}` — never an email address.

`/api/health` returns 200 only when the database is reachable; the deploy
workflow smoke-tests it (§10.4).

---

## 7. Test harness

```
python -m unittest discover -s tests -t .          # whole suite
python -m unittest tests.test_preemption -v        # one module
python -m unittest tests.test_preemption.PreemptionTests.test_c4_partial_overlap
```

`tests/support.py` provides `AppTestCase` (fresh migrated+seeded in-memory
database per test), row factories, `taipei_at()`, and `assertErrorCode`.
scrypt cost is lowered there so the suite stays fast.

CI runs the same suite twice: once on SQLite and once against a real Postgres
service container, so the production backend is exercised before any deploy.

---

## 8. Known environment limitation

The machine this was built on has **no outbound network**: no package installs,
no Postgres, no provider accounts, and therefore no real deploy. Consequences:

* The Postgres backend and the Brevo transport are written but not executed
  locally. CI covers the Postgres path; the Brevo path is covered by a
  contract test against a stub HTTP layer.
* Spec §12 Group E (deployment) cannot be verified here. It is written and
  documented, and must be run once by the owner from the GitHub Actions tab.
