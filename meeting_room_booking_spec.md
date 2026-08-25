# Meeting Room Booking System — Build Specification

**Version:** 1.0 (locked)
**Date:** 2026-08-25
**Audience:** Coding agent (orchestrator) + sub-agents
**Status:** All requirements confirmed with the product owner. No open questions remain except the items explicitly listed in §14.

---

## 0. How to use this document

1. Read §1–§3 to understand scope and hard constraints.
2. **Do not start coding until §11 Task 0 (Foundation) is complete.** Task 0 fixes the tech stack, DB schema, and API contract so that all other sub-agents can work in parallel without conflicts.
3. Each task in §11 is written to be handed to a separate sub-agent. Each task lists its inputs, outputs, dependencies, and definition of done.
4. §12 contains the acceptance tests. The build is not done until every scenario passes.
5. Anything not specified here is the implementing agent's choice — but it must not violate §2.

**Rules for all sub-agents:**
- Never change the DB schema or API contract unilaterally. Raise it to the orchestrator.
- Every business rule constant in §5 must be read from the database settings table, never hardcoded.
- All times are stored in UTC and displayed in `Asia/Taipei`.
- All user-facing text is Traditional Chinese (zh-TW). Keep all strings in a single i18n file so they can be swapped later.

---

## 1. Product summary

A web application for booking meeting rooms inside an organisation of roughly 200 users.

The distinguishing feature is **priority-based preemption**: every member has a numeric priority level, and a higher-level member may take over a room slot already booked by a lower-level member. The displaced member is notified by email and must rebook themselves.

Core loops:
- **Member:** register → wait for admin approval (or arrive via an invitation link and skip the wait) → browse room availability → book a slot → receive confirmation → receive a reminder → possibly get displaced and rebook.
- **Admin:** approve or reject registrations → send invitations → set each member's level → manage rooms → tune system rules → view all bookings and the preemption audit log.

---

## 2. Hard constraints

These are non-negotiable. The technology stack is otherwise the implementing agent's choice, but the choice must satisfy all of the following.

| # | Constraint |
|---|---|
| C1 | **Total running cost must be US$0/month.** Only permanent free tiers. No credit card required where avoidable. No trial credits that expire. |
| C2 | Must comfortably serve ~200 registered users with light concurrent usage. |
| C3 | **Deployment must be triggerable from the GitHub web UI with one button** (an Actions workflow with `workflow_dispatch`). The owner must never be required to run a local terminal for a normal deploy. |
| C4 | The app is reachable at a free platform-provided subdomain (e.g. `*.vercel.app`, `*.onrender.com`). No custom domain. The design must not depend on owning a domain. |
| C5 | Email sending must work on a free tier with **at least 300 emails/day**. See §9.4 for the volume budget. |
| C6 | The database free tier must **not delete or expire data** (avoid 90-day-expiry databases). Idle suspension with cold start is acceptable; data loss is not. |
| C7 | Scheduled jobs (reminder emails) must run without a paid scheduler. Use a GitHub Actions `schedule` workflow that calls a protected endpoint. |
| C8 | Timezone `Asia/Taipei`. Language zh-TW. |
| C9 | No paid third-party services of any kind, including analytics and error tracking (free tiers only, or omit). |

### 2.1 Candidate free services (non-binding, verify limits at build time)

The agent picks the stack. These were current as of August 2026 and are offered only as a starting point — the agent **must re-verify each provider's current free tier before committing**, because these numbers change often.

- **Database (Postgres, permanent free tier, no card):** Neon or Supabase are the common defaults; Aiven also offers a free single-node Postgres. Avoid Render's free Postgres — it expires after 90 days, violating C6.
- **App hosting:** Vercel (free hobby tier, serverless), Render (free web service; note it sleeps after ~15 min idle with a 30–60 s cold start), or Koyeb (one free web service). Railway only funds ~US$1/month of non-rollover credit, which is marginal for always-on use.
- **Email:** Brevo's free tier allows 300 emails/day with API and SMTP access; Resend allows 3,000/month; Mailjet allows 6,000/month capped at 200/day. SendGrid retired its free plan in 2025. Amazon SES's free allowance is first-12-months only, which violates C1 in year two.

Constraint interaction to respect: if the chosen host sleeps when idle (Render), the reminder cron in C7 will also wake the app — that is fine, but the reminder job must tolerate a cold start of up to 60 seconds.

---

## 3. Roles and permissions

| Capability | Guest | Pending member | Approved member | Admin |
|---|---|---|---|---|
| Register | ✅ | — | — | — |
| Log in | — | ✅ | ✅ | ✅ |
| View room availability | ❌ | ✅ (read-only) | ✅ | ✅ |
| Create a booking | ❌ | ❌ | ✅ | ✅ |
| Cancel own booking | ❌ | ❌ | ✅ | ✅ |
| Preempt a lower-level booking | ❌ | ❌ | ✅ | ✅ |
| Approve/reject registrations | ❌ | ❌ | ❌ | ✅ |
| Send invitations | ❌ | ❌ | ❌ | ✅ |
| Change any member's level | ❌ | ❌ | ❌ | ✅ |
| Suspend/reactivate a member | ❌ | ❌ | ❌ | ✅ |
| CRUD rooms | ❌ | ❌ | ❌ | ✅ |
| Cancel anyone's booking | ❌ | ❌ | ❌ | ✅ |
| Edit system settings (§5) | ❌ | ❌ | ❌ | ✅ |
| View preemption audit log | ❌ | ❌ | own records only | ✅ (all) |

A pending member can log in and see availability so they understand what they are waiting for, but every booking action returns a clear message: their account is awaiting administrator approval.

---

## 4. Domain model

Field types are indicative; the agent may adapt them to its ORM.

### 4.1 `users`
| Field | Type | Notes |
|---|---|---|
| id | uuid PK | |
| email | string, unique, case-insensitive | login identifier |
| password_hash | string | strong hashing (bcrypt/argon2) |
| full_name | string, required | collected at registration |
| department | string, required | collected at registration |
| phone | string, required | contact/extension, collected at registration |
| level | int 1–10, default 1 | **higher number = higher priority** |
| status | enum: `pending_email`, `pending_approval`, `active`, `rejected`, `suspended` | see §6.1 state machine |
| is_admin | bool, default false | |
| email_verified_at | timestamp, nullable | |
| approved_at | timestamp, nullable | |
| approved_by | uuid FK users, nullable | |
| created_at / updated_at | timestamp | |

### 4.2 `email_tokens`
Used for email verification, invitations, and password resets.
| Field | Type | Notes |
|---|---|---|
| id | uuid PK | |
| user_id | uuid FK, nullable | null for invitations to an email with no account yet |
| email | string | target address |
| type | enum: `verify_email`, `invite`, `password_reset` | |
| token_hash | string | store a hash, never the raw token |
| expires_at | timestamp | see §5 |
| used_at | timestamp, nullable | single use |
| created_at | timestamp | |

### 4.3 `rooms`
| Field | Type | Notes |
|---|---|---|
| id | uuid PK | |
| name | string, required | |
| capacity | int, nullable | |
| location | string, nullable | |
| equipment_note | text, nullable | free text: projector, whiteboard, etc. |
| is_active | bool, default true | inactive rooms are hidden from booking but keep history |
| open_time / close_time | time, nullable | per-room bookable window; falls back to the global default in §5 |
| created_at / updated_at | timestamp | |

### 4.4 `bookings`
| Field | Type | Notes |
|---|---|---|
| id | uuid PK | |
| room_id | uuid FK | |
| user_id | uuid FK | owner |
| title | string, required | short purpose, shown on the calendar |
| start_at / end_at | timestamp (UTC) | aligned to 30-minute boundaries |
| status | enum: `confirmed`, `cancelled_by_user`, `cancelled_by_admin`, `preempted` | |
| level_at_booking | int | snapshot of the owner's level when created — **for the audit log only, never for preemption decisions** (see §7.3) |
| preempted_by_booking_id | uuid FK bookings, nullable | set when status = `preempted` |
| cancelled_at | timestamp, nullable | |
| created_at / updated_at | timestamp | |

Only rows with `status = 'confirmed'` occupy a room. Cancelled and preempted rows are retained forever as history.

Required index: `(room_id, start_at, end_at)` filtered on `status = 'confirmed'` for fast overlap queries.

### 4.5 `preemption_log`
| Field | Type | Notes |
|---|---|---|
| id | uuid PK | |
| victim_booking_id | uuid FK | |
| winner_booking_id | uuid FK | |
| victim_user_id / winner_user_id | uuid FK | |
| victim_level / winner_level | int | levels at the moment of preemption |
| room_id | uuid FK | |
| occurred_at | timestamp | |
| notification_sent_at | timestamp, nullable | |

### 4.6 `settings`
Single-row (or key/value) table holding every tunable in §5. Seeded on first deploy. Editable only by admins.

### 4.7 `email_log`
| Field | Type | Notes |
|---|---|---|
| id, to_email, type, subject, status (`sent`/`failed`), provider_message_id, error, related_booking_id, created_at | | used for retries, debugging, and the daily quota guard in §9.4 |

---

## 5. System settings (admin-editable, seeded with these defaults)

| Key | Default | Meaning |
|---|---|---|
| `slot_minutes` | 30 | Booking grid granularity. Every start and end must land on a 30-minute boundary. |
| `max_booking_minutes` | 240 | Maximum length of a single booking. |
| `booking_horizon_days` | 60 | How far into the future a booking may be made. |
| `default_open_time` | 08:00 | Default daily bookable window start (per-room override allowed). |
| `default_close_time` | 22:00 | Default daily bookable window end. |
| `preemption_protection_minutes` | 120 | **A confirmed booking becomes immune to preemption once `now >= start_at − this value`.** Admin-tunable. |
| `quota_by_level` | JSON map, e.g. `{"1":3,"2":3,"3":5,...,"10":20}` | Max simultaneous *future confirmed* bookings per level. Must cover levels 1–10. `0` or `null` means unlimited. |
| `reminder_lead_minutes` | 60 | How long before start the reminder email is sent. |
| `reminders_enabled` | true | Kill switch if the email quota gets tight. |
| `verify_token_hours` | 24 | Email verification link lifetime. |
| `invite_token_hours` | 168 | Invitation link lifetime (7 days). |
| `reset_token_hours` | 2 | Password reset link lifetime. |
| `daily_email_cap` | 280 | Safety margin below the provider's free-tier daily limit. See §9.4. |

---

## 6. Functional requirements

### 6.1 FR-1 — Registration and account lifecycle

**Self-registration path**
1. Guest submits: email, password, full name, department, phone. All required.
2. Account is created with `status = 'pending_email'`, `level = 1`, `is_admin = false`.
3. System sends **E1 verification email** containing a single-use link valid for `verify_token_hours` (24h).
4. Clicking the link sets `email_verified_at` and moves the account to `status = 'pending_approval'`.
5. Admins receive **E7 new-registration notice** (batched — see §9.4).
6. An admin approves → `status = 'active'`, and the user receives **E2 approval notice**. Or the admin rejects → `status = 'rejected'` and the user receives **E3 rejection notice**.
7. Only `active` users may create bookings.

**Invitation path**
1. Admin enters one or more email addresses and (optionally) a starting level, then sends invitations.
2. System sends **E8 invitation email** with a single-use link valid for `invite_token_hours` (7 days).
3. Clicking the link opens a registration form pre-filled with the invited email (email field is locked). The invitee sets a password and fills in name, department, phone.
4. On submit the account is created directly as **`status = 'active'` with `email_verified_at` set** — clicking the invitation link proves the address, so there is no second verification email and no approval step. The user can book immediately.

**State machine**

```
pending_email --verify--> pending_approval --approve--> active
                                           --reject---> rejected
(invite link)  ------------------------------------> active
active --admin suspend--> suspended --admin reactivate--> active
```

**Rules**
- Re-registering with an email that is already `pending_email`: resend the verification email, do not create a duplicate row.
- Re-registering with an email that already has an `active` account: respond with a generic "check your email" message and send a "you already have an account" email instead. Do not confirm or deny account existence in the UI.
- Expired verification link: show a page with a "resend" button.
- Unverified accounts older than 30 days may be purged by a cleanup job.
- Suspending a user does **not** auto-cancel their existing bookings; the admin cancels them explicitly if wanted.

**Definition of done:** all of §12 Scenario Group A passes.

### 6.2 FR-2 — Authentication
- Email + password login. Session cookie: `httpOnly`, `secure`, `sameSite=lax`.
- Forgot-password flow using **E9 reset email**, token valid `reset_token_hours` (2h), single use, invalidates all sessions on success.
- Rate limiting: max 5 failed logins per email per 15 minutes; max 3 verification/reset emails per address per hour (protects the email quota).
- Password minimum 8 characters. Do not impose composition rules; do reject the top-1000 common passwords if a list is easy to bundle.

### 6.3 FR-3 — Levels
- Integer 1–10. **10 is the most privileged.** New self-registrations default to 1.
- Only admins change levels; changes take effect immediately.
- A level change does **not** retroactively cancel or protect existing bookings. Preemption is always evaluated against the users' *current* levels at the moment of the attempt (§7.3).
- Every level change is written to the audit log (who, whom, from, to, when).

### 6.4 FR-4 — Room management (admin)
- Create, edit, deactivate, reactivate rooms with the fields in §4.3.
- Deactivating a room with future confirmed bookings requires an explicit confirmation and offers to cancel those bookings (which sends **E5 cancellation notices** to the owners).
- Rooms cannot be hard-deleted while bookings reference them.

### 6.5 FR-5 — Booking
- Views: (a) day view showing all rooms side by side as columns of 30-minute slots; (b) week view for a single selected room; (c) "my bookings" list split into upcoming and past.
- To create a booking the user picks room, date, start slot, end slot, and enters a title.
- Validation, in this order, with a specific error message for each failure:
  1. User is `active`.
  2. Room is active.
  3. Start and end are on 30-minute boundaries, end > start.
  4. Duration ≤ `max_booking_minutes`.
  5. Start is in the future.
  6. Start date within `booking_horizon_days`.
  7. Booking falls inside the room's open/close window and does not cross midnight.
  8. The user's future confirmed booking count is below their level's quota in `quota_by_level`.
  9. Conflict resolution per §7.
- Cancelling: an owner may cancel their own confirmed booking at any time before `end_at`. Sends **E6 cancellation confirmation** to the owner. Admins may cancel anyone's booking; that sends **E5 cancelled-by-admin notice** to the owner.
- A cancelled or preempted booking immediately frees the slot for anyone.

### 6.6 FR-6 — Admin console
Screens: pending approvals queue, all members (search, filter by status/level, edit level, suspend), invitations (send, list outstanding, revoke), rooms, all bookings (filter by room/user/date, cancel), preemption log, settings (§5), email log.

### 6.7 FR-7 — Audit and history
Persist forever: preemption events, level changes, approvals/rejections, admin cancellations, room deactivations. Admin-visible, exportable to CSV.

---

## 7. Preemption engine — the core algorithm

This is the highest-risk part of the system. Implement it once, in one service function, covered by unit tests.

### 7.1 Rules (final, confirmed)

| Rule | Decision |
|---|---|
| Who can preempt whom | Strictly **higher current level only**. Equal level can never preempt. |
| Time window | Any time from now until the target booking starts, **except** inside the protection window. |
| Protection window | A booking cannot be preempted once `now >= start_at − preemption_protection_minutes` (default 120 min, admin-tunable). |
| Partial overlap | If the new booking overlaps an existing one **at all**, the existing booking is cancelled **in its entirety**. No splitting, no trimming. |
| Multiple victims | A single new booking may displace several existing bookings, but **only if every one of them is individually preemptible**. It is all-or-nothing. |
| Victim compensation | **Email notification only.** The system does not auto-rebook and does not suggest alternative slots. The email tells them to rebook themselves and links to the booking page. |
| Chain reactions | The displaced user rebooking may itself preempt someone lower. This is allowed and expected. Log it normally. |
| Preempting yourself | If the overlapping booking belongs to the requester, reject with "you already have a booking at this time" — never preempt your own. |
| Admin bookings | Admins follow the same level rules as everyone else. Being an admin grants no preemption privilege; only their `level` matters. |

### 7.2 Two-phase UX (required)

Preemption must never be silent.

- **Phase 1 — check.** When the user submits the form, the server returns one of:
  - `AVAILABLE` — no overlap; proceed to create.
  - `PREEMPTION_REQUIRED` — overlaps exist and **all** are preemptible. Return the list (room, time range, owner's display name, whether it is the requester's own — which is already excluded) so the UI can show a confirmation dialog: "This will cancel N existing booking(s). The affected members will be notified by email. Continue?"
  - `BLOCKED` — overlaps exist and at least one is **not** preemptible. Return the specific reason for the first blocker: equal-or-higher level, or inside the protection window. Do not reveal the blocking user's email; show name and department only.
- **Phase 2 — commit.** Only on explicit confirmation does the server re-run the entire check inside a transaction and write the changes. Never trust the phase-1 result.

### 7.3 Reference algorithm

```
function attemptBooking(requester, roomId, startAt, endAt, title):
    assert requester.status == 'active'
    runStaticValidations()                       # FR-5 steps 1-8

    BEGIN TRANSACTION  (isolation: SERIALIZABLE, or SELECT ... FOR UPDATE
                        on the room's confirmed bookings in the range)

        overlaps = SELECT * FROM bookings
                   WHERE room_id = roomId
                     AND status = 'confirmed'
                     AND start_at < endAt
                     AND end_at   > startAt
                   FOR UPDATE

        if overlaps is empty:
            booking = insert(status='confirmed', level_at_booking=requester.level)
            COMMIT
            enqueue(E4_booking_confirmed, booking)
            return CREATED(booking)

        victims = []
        for b in overlaps:
            if b.user_id == requester.id:
                ROLLBACK; return BLOCKED("SELF_OVERLAP", b)

            owner = getUser(b.user_id)           # CURRENT level, not level_at_booking

            if owner.level >= requester.level:
                ROLLBACK; return BLOCKED("EQUAL_OR_HIGHER_LEVEL", b)

            if now() >= b.start_at - settings.preemption_protection_minutes:
                ROLLBACK; return BLOCKED("PROTECTED_WINDOW", b)

            victims.append((b, owner))

        # all-or-nothing: reaching here means every overlap is preemptible
        if not requesterConfirmedPreemption:
            ROLLBACK; return PREEMPTION_REQUIRED(victims)

        newBooking = insert(status='confirmed', level_at_booking=requester.level)

        for (b, owner) in victims:
            update b SET status='preempted',
                         preempted_by_booking_id=newBooking.id,
                         cancelled_at=now()
            insert preemption_log(victim=b, winner=newBooking,
                                  victim_level=owner.level,
                                  winner_level=requester.level)

    COMMIT

    enqueue(E4_booking_confirmed, newBooking)
    for (b, owner) in victims:
        enqueue(E5_preempted_notice, b, owner, newBooking)   # after commit only
    cancelPendingReminderFor(victims)
    return CREATED(newBooking, displaced=victims)
```

**Critical implementation notes**
- Emails are enqueued **after** the transaction commits. Never send inside a transaction — a rollback would produce a "your booking was cancelled" email for a booking that still exists.
- Two simultaneous requests for the same slot must not both succeed. Use `SELECT ... FOR UPDATE` on the overlap set, or `SERIALIZABLE` isolation plus a retry-on-conflict wrapper. A DB-level exclusion constraint on `(room_id, tstzrange(start_at, end_at))` where `status='confirmed'` is the strongest guarantee if the chosen database supports it — but note it must be dropped/re-added around a preemption commit, or the preempted rows must be updated before the insert within the same statement order.
- The protection window is evaluated against the **victim's** start time, not the new booking's.

---

## 8. Screens

| Screen | Access | Notes |
|---|---|---|
| Login | public | link to register and forgot-password |
| Register | public | email, password, name, department, phone |
| Register via invitation | token link | email locked |
| Verify-email result | token link | success / expired + resend |
| Pending-approval landing | pending users | explains the wait, shows read-only availability |
| Room day view | logged in | all rooms as columns, 30-min rows, colour by status; own bookings highlighted |
| Room week view | logged in | one room, 7 days |
| Booking form / dialog | active members | includes the §7.2 preemption confirmation dialog |
| My bookings | active members | upcoming + past, cancel action, shows preempted history with the reason |
| Admin: approvals | admin | name, department, phone, email, registered-at; approve/reject |
| Admin: members | admin | level editor, suspend/reactivate |
| Admin: invitations | admin | send, list, revoke |
| Admin: rooms | admin | CRUD |
| Admin: all bookings | admin | filter, cancel |
| Admin: preemption log | admin | who displaced whom, when |
| Admin: settings | admin | every key in §5, with inline explanations in zh-TW |

Mobile-responsive is required — the owner and many users will access this from a phone.

---

## 9. Email

### 9.1 Catalogue

| ID | Trigger | Recipient | Contents |
|---|---|---|---|
| E1 | Self-registration | registrant | Verification link, 24h validity, note that admin approval follows |
| E2 | Admin approves | member | Account active, link to book |
| E3 | Admin rejects | applicant | Neutral wording, contact the administrator |
| E4 | Booking created | owner | Room, date, time, title, cancel link |
| E5 | Booking preempted, or cancelled by admin, or room deactivated | owner | **Which booking was cancelled and why**, plus a prominent "book another slot" link. For preemption: state that a higher-priority booking took the slot. **Do not disclose who preempted them** — name the room and time only. |
| E6 | Owner cancels their own booking | owner | Confirmation of what was cancelled |
| E7 | New registration awaiting approval | all admins | Batched, see §9.4 |
| E8 | Admin sends invitation | invitee | Registration link, 7-day validity |
| E9 | Forgot password | member | Reset link, 2h validity |
| E10 | `reminder_lead_minutes` before start | booking owner | Room, time, title. **Suppressed if the booking is no longer `confirmed`.** |

### 9.2 Template requirements
- zh-TW, plain and readable, with a text-only fallback part.
- Every email states the local time explicitly with the timezone label (e.g. `2026-09-03 (三) 14:00–15:00 (台北時間)`).
- Include an unsubscribe-style footer only where legally sensible; these are transactional, so a short "this is an automated notification from the meeting room system" line is enough.

### 9.3 Reminder job (C7)
- A GitHub Actions workflow on `schedule: '*/15 * * * *'` calls `POST /api/cron/send-reminders` with a shared secret header.
- The endpoint finds confirmed bookings starting within the next `reminder_lead_minutes` that have no reminder logged, sends E10, and records it in `email_log`. It must be idempotent — a double-invocation must not double-send.
- **Known limitation to document in SETUP.md:** GitHub Actions scheduled workflows can be delayed by several minutes under load, and are automatically disabled after 60 days of repository inactivity. The setup guide must tell the owner to re-enable them if reminders stop, and the admin dashboard should show "last reminder job ran at ___" so the problem is visible.

### 9.4 Email volume budget
Estimated steady state for 200 users: ~60 bookings/day × (1 confirmation + 1 reminder) = ~120, plus preemption notices, registrations, and resets. Budget ~200/day against a 300/day free cap.

Required protections:
- A `daily_email_cap` counter (default 280). When exceeded, transactional emails critical to access (E1, E8, E9, E5) still send; E10 reminders are dropped first and the event is logged.
- E7 admin notices are **batched**: at most one digest email per admin per hour listing all pending registrations.
- Failed sends are retried up to 3 times with backoff, then marked `failed` and surfaced in the admin email log.

---

## 10. Deployment (C3)

The owner is not a developer and has only occasional access to a computer. Deployment must be operable entirely from the GitHub website on a phone if necessary.

### 10.1 Deliverables
1. **`.github/workflows/deploy.yml`** — `workflow_dispatch` trigger. Steps: install, lint, test, build, run DB migrations, deploy to the chosen host, run a post-deploy smoke test against `/api/health`, and print the live URL in the job summary. It must fail loudly if any secret is missing, with a message naming the missing secret.
2. **`.github/workflows/reminders.yml`** — the scheduled job in §9.3.
3. **`SETUP.md`** — a numbered, zero-jargon guide in **Traditional Chinese** covering: creating the accounts at each provider, where to find each API key/connection string, where to paste them in GitHub (Settings → Secrets and variables → Actions), and how to press the deploy button (Actions tab → Deploy → Run workflow). Every step must be phrased for someone who has never used GitHub.
4. **`ROLLBACK.md`** — how to redeploy a previous commit from the web UI.
5. **First-run seeding** — on first successful deploy, migrations create the settings row and the admin account from secrets, and seed 3 example rooms. Seeding must be idempotent.

### 10.2 Required secrets (name them exactly; list them in SETUP.md)
`DATABASE_URL`, `EMAIL_PROVIDER_API_KEY`, `EMAIL_FROM_ADDRESS`, `APP_BASE_URL`, `SESSION_SECRET`, `ADMIN_EMAIL`, `ADMIN_INITIAL_PASSWORD`, `CRON_SECRET`, plus whatever deploy token the chosen host requires.

### 10.3 Security requirement
The admin account is created from `ADMIN_EMAIL` / `ADMIN_INITIAL_PASSWORD` on first deploy. **The app must force a password change on that account's first login** and SETUP.md must say so.

### 10.4 Health and observability
- `GET /api/health` returns app version, DB connectivity, and the timestamp of the last successful reminder run.
- Admin dashboard surfaces: pending approvals count, emails sent today vs cap, last cron run, recent email failures.

---

## 11. Task breakdown for sub-agents

Dependencies are strict. Tasks at the same stage may run in parallel.

### Stage 0 — must complete before anything else

**Task 0 — Foundation (orchestrator or a single senior sub-agent)**
- Choose and justify the stack against every constraint in §2, including re-verifying current free-tier limits.
- Produce: repository skeleton, DB migrations implementing §4, the seeded settings from §5, a typed API contract document (every endpoint, request, response, error code), the i18n string file skeleton, and a working local test harness.
- **Output artifact:** `CONTRACT.md`. No other task may begin until it exists.

### Stage 1 — parallel

**Task 1 — Auth & accounts** — FR-1, FR-2. Registration, verification, invitation redemption, approval state machine, login, sessions, password reset, rate limiting. Emits email events; does not implement the email transport.

**Task 2 — Email service** — §9. Provider adapter behind an interface, all ten templates, `email_log`, retry, daily cap, batching, the cron endpoint and its idempotency. Must ship with a fake transport for tests.

**Task 3 — Rooms & bookings core** — FR-4, FR-5. Room CRUD, availability queries, slot validation, quota enforcement, cancellation.

**Task 4 — Preemption engine** — §7. **Highest priority for review.** Delivered as a standalone, transaction-safe service function with an exhaustive unit test suite including concurrency tests. Depends on Task 3's schema but must be independently testable with fixtures.

### Stage 2 — after Stage 1

**Task 5 — Member UI** — §8 member screens, mobile-responsive, including the two-phase preemption confirmation dialog.

**Task 6 — Admin console** — §6.6 and §8 admin screens, including settings editing and the audit views.

### Stage 3

**Task 7 — Deployment pipeline** — §10. Both workflows, `SETUP.md`, `ROLLBACK.md`, seeding, health endpoint. This task owns the free-tier verification and must actually perform a real deploy to prove the button works.

**Task 8 — QA** — implement §12 as automated tests, plus a manual pass on a real phone. Reports failures back to the owning task.

### Cross-cutting rules
- Task 4 must not be merged without a reviewing agent independently checking the concurrency behaviour.
- Any agent that discovers a contradiction between this document and reality stops and reports it rather than improvising.

---

## 12. Acceptance tests

### Group A — accounts
- A1 Self-registration sends E1; the link activates verification exactly once; a second click shows "already used".
- A2 A link older than 24h is rejected and the resend button issues a fresh one.
- A3 A verified user cannot book; the UI explains they are awaiting approval.
- A4 After admin approval the user receives E2 and can book.
- A5 An invited user clicking E8 registers and can book immediately — no verification email, no approval step.
- A6 A revoked or expired invitation link is rejected.
- A7 Password reset invalidates existing sessions.
- A8 Six failed logins in ten minutes triggers rate limiting.

### Group B — booking basics
- B1 A booking at 14:00–15:30 succeeds; 14:10–15:00 is rejected as off-grid.
- B2 A booking exceeding `max_booking_minutes` is rejected.
- B3 A booking beyond `booking_horizon_days` is rejected.
- B4 A booking outside the room's open/close window is rejected.
- B5 A level-1 user with a quota of 3 is blocked on the fourth future booking, and unblocked after cancelling one.
- B6 Cancelling frees the slot immediately for another user.

### Group C — preemption (critical)
- C1 Level 5 preempts level 3 → victim's booking becomes `preempted`, E5 is sent, the log records both levels, the winner's booking is confirmed.
- C2 Level 3 attempts to preempt level 3 → `BLOCKED: EQUAL_OR_HIGHER_LEVEL`. Nothing changes.
- C3 Level 5 attempts to preempt level 7 → blocked.
- C4 **Partial overlap:** victim holds 14:00–16:00, winner requests 15:30–16:30 → the **entire** 14:00–16:00 booking is cancelled.
- C5 **All-or-nothing:** the requested range overlaps a level-2 booking and a level-9 booking, requester is level 5 → the whole request is rejected, and the level-2 booking is untouched.
- C6 **Protection window:** with the default 120 minutes, a booking starting in 90 minutes cannot be preempted; one starting in 150 minutes can. Setting the value to 0 makes preemption possible right up to the start time.
- C7 A booking that has already started can never be preempted.
- C8 **Current level, not booking-time level:** a user books at level 8, an admin demotes them to level 2, then a level 5 user preempts them successfully.
- C9 **Concurrency:** two simultaneous preemption attempts on the same victim by two different higher-level users result in exactly one winner, one victim record, and one E5 email.
- C10 Preemption cancels the victim's pending E10 reminder.
- C11 The E5 email names the room and time but not the preempting user.
- C12 A user cannot preempt their own booking.

### Group D — email
- D1 Reminders fire once and only once per booking, within 15 minutes of the target lead time.
- D2 A cancelled booking's reminder is not sent.
- D3 Exceeding `daily_email_cap` drops reminders but still sends E1/E5/E8/E9.
- D4 Two registrations within an hour produce one batched admin digest, not two.
- D5 A provider failure is retried and then surfaced in the admin email log.

### Group E — deployment
- E1 Pressing "Run workflow" on a clean fork with all secrets set produces a live, reachable site.
- E2 A missing secret fails the workflow with a message naming it.
- E3 First deploy creates the admin account, which is forced to change its password at first login.
- E4 Re-running the deploy is safe and does not duplicate seed data.
- E5 A non-technical reader can follow SETUP.md end to end without outside help.

---

## 13. Out of scope (v1)

Calendar (ICS/Google) integration, recurring bookings, room check-in/no-show tracking, attendee invitations, SSO/LDAP, custom domain, multi-language UI beyond zh-TW, mobile apps, waiting lists, automatic rebooking suggestions for displaced users, approval workflows for individual bookings.

---

## 14. Decision log (locked with the product owner)

| Question | Decision |
|---|---|
| Level model | Integer 1–10, higher number wins |
| Same-level preemption | Not permitted |
| Preemption timing | Allowed until the meeting starts, minus an admin-set protection window |
| Protection window default | 120 minutes, admin-editable |
| Partial overlap | Entire victim booking is cancelled |
| Victim compensation | Email notification only; they rebook themselves |
| Slot granularity | 30 minutes |
| Per-person booking quota | Per level, admin-configurable |
| Who may register | Anyone may register, but an admin must approve before booking |
| Registration fields for review | Name, department, phone |
| Invitation path | Admin-issued link; registers and activates in one step |
| Email verification validity | 24 hours |
| Emails required | Verification, preemption notice, booking confirmation, pre-meeting reminder, self-cancellation confirmation, password reset (plus approval/rejection/invitation/admin digest as derived requirements) |
| First admin | Created automatically at deploy time from configuration |
| Domain | Free platform subdomain |
| One-click deploy | GitHub Actions button, no local terminal |
| Tech stack | Implementing agent's choice, subject to §2 |
| Budget | US$0/month |
| Language / timezone | zh-TW / Asia/Taipei |

### Assumptions the agent may proceed on unless the owner objects
1. Rooms are managed by the admin in-app; roughly 5–20 rooms.
2. Max booking length defaults to 4 hours; bookable window 08:00–22:00; horizon 60 days.
3. Reminder lead time is 60 minutes.
4. Admins receive a batched digest of pending registrations (derived from the approval requirement).
5. Approval and rejection notices to applicants (derived from the approval requirement).
6. Bookings may not cross midnight.
7. A booking's title is required and visible to all logged-in users.
