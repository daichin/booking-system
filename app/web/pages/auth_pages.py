"""Auth and account pages (spec §8, §6.1, §6.2): Task 5.

Login, registration, email verification, invitation acceptance,
forgot/reset-password, and the forced password-change screen. Everything
here is server-rendered HTML that works with JavaScript disabled -- forms
post back to the same or a sibling route and the server decides what to show
next.

Business rules (rate limiting, the account state machine, token handling)
all live in :mod:`app.services.accounts`; this module only turns its results
into zh-TW markup and never re-implements a rule that service already
enforces.
"""

from __future__ import annotations

from app import models, security
from app.errors import AppError, TOKEN_EXPIRED, TOKEN_INVALID, TOKEN_USED
from app.i18n import error_message, t
from app.services import accounts, mailer, sessions
from app.timeutil import now_utc
from app.web.framework import (
    CSRF_FIELD,
    csrf_token,
    SESSION_COOKIE,
    Request,
    Response,
    Router,
    require_login,
)
from app.web.html import Markup, a, button, div, field, form, h2, hidden, notice, p
from app.web.layout import page

# --- helpers -----------------------------------------------------------------


def _csrf_hidden(request: Request) -> Markup:
    return hidden(CSRF_FIELD, csrf_token(request))


def _safe_next(value: str | None) -> str | None:
    """A same-origin path, or ``None``.

    Rejects anything that is not a path (an absolute URL) and anything that
    looks like a protocol-relative URL (``//evil.example.com``), which a
    browser also treats as off-site.
    """
    if not value:
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    return value


def _actions(*children) -> Markup:
    return div(*children, class_="actions")


def _set_session_cookie(request: Request, response: Response, user: models.User) -> None:
    raw, expires_at = sessions.create_session(request.db, user)
    max_age = max(0, int((expires_at - now_utc()).total_seconds()))
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        max_age=max_age,
        http_only=True,
        secure=request.is_secure,
        same_site="Lax",
    )


def _load_token_row(request: Request, token: str, token_type: str) -> dict | None:
    hashed = security.hash_token(token)
    return request.db.run_in_transaction(
        lambda conn: conn.query_one(
            "SELECT * FROM email_tokens WHERE token_hash = ? AND type = ?",
            (hashed, token_type),
        )
    )


def _token_error(row: dict | None) -> str | None:
    if row is None:
        return TOKEN_INVALID
    if row["used_at"] is not None:
        return TOKEN_USED
    if row["revoked_at"] is not None:
        return TOKEN_INVALID
    if row["expires_at"] <= now_utc():
        return TOKEN_EXPIRED
    return None


# --- login -------------------------------------------------------------------


def _login_form(request: Request, *, email: str = "", next_value: str = "", error: str = "") -> Markup:
    banner = notice(error, kind="error") if error else Markup("")
    return div(
        banner,
        form(
            _csrf_hidden(request),
            hidden("next", next_value),
            field("email", t("auth.field.email"), type="email", value=email),
            field("password", t("auth.field.password"), type="password", value=""),
            _actions(button(t("auth.login.submit"), type="submit")),
            method="post",
            action="/login",
        ),
        p(a(t("auth.login.register_link"), href="/register")),
        p(a(t("auth.login.forgot_link"), href="/forgot")),
        class_="panel stack",
    )


def login_page(request: Request) -> Response:
    if request.user is not None:
        return Response.redirect("/day")
    next_value = _safe_next(request.query.get("next")) or ""
    # Deleting your own account lands here with the session already gone; say
    # so, or it reads as having been logged out for no reason.
    banners = (
        [notice(t("account.deleted_notice"), kind="success")]
        if request.query.get("deleted")
        else None
    )
    return Response.html(
        page(
            request,
            t("auth.login.title"),
            _login_form(request, next_value=next_value),
            banners=banners,
        )
    )


def login_submit(request: Request) -> Response:
    if request.user is not None:
        return Response.redirect("/day")
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    next_value = _safe_next(request.form.get("next")) or ""

    try:
        user = accounts.authenticate(request.db, email, password)
    except AppError as err:
        message = error_message(err.code, **err.details)
        body = _login_form(request, email=email, next_value=next_value, error=message)
        return Response.html(page(request, t("auth.login.title"), body), err.status)

    destination = "/password" if user.must_change_password else (next_value or "/day")
    response = Response.redirect(destination)
    _set_session_cookie(request, response, user)
    return response


def logout(request: Request) -> Response:
    sessions.revoke_session(request.db, request.cookies.get(SESSION_COOKIE))
    response = Response.redirect("/login")
    response.clear_cookie(SESSION_COOKIE)
    return response


# --- the account page (spec §8) ------------------------------------------------
#
# Until now /password was reachable only by the forced-change redirect: a member
# who simply wanted to change their password had no link to it anywhere. This
# page is the one place a member manages their own account, and it is where
# deleting it lives, because deletion has to sit behind the same proof of
# identity as a password change.


def _account_details(user: models.User) -> Markup:
    rows = [
        (t("account.field.name"), user.full_name),
        (t("account.field.email"), user.email),
        (t("account.field.department"), user.department),
        (t("account.field.level"), str(user.level)),
    ]
    return div(
        h2(t("account.details.title")),
        div(
            *[
                div(
                    div(caption, class_="detail-label"),
                    div(value, class_="detail-value"),
                    class_="detail",
                )
                for caption, value in rows
            ],
            class_="detail-list",
        ),
        p(a(t("account.change_password"), href="/password")),
        class_="panel",
    )


def _delete_form(request: Request, *, error: str = "") -> Markup:
    """Deleting your own account, behind your own password.

    The password is the confirmation step -- there is no second "are you
    sure" screen, because a dialog you can click through without knowing
    anything is weaker protection than a secret you have to type.
    """
    return div(
        h2(t("account.delete.title")),
        notice(error, kind="error") if error else Markup(""),
        p(t("account.delete.explain")),
        p(t("account.delete.keeps_history"), class_="muted"),
        form(
            _csrf_hidden(request),
            field(
                "current_password",
                t("account.delete.password_label"),
                type="password",
                help_text=t("account.delete.password_help"),
            ),
            _actions(
                button(t("account.delete.submit"), type="submit", class_="danger")
            ),
            method="post",
            action="/account/delete",
        ),
        class_="panel danger-zone",
    )


def account_page(request: Request, *, error: str = "", status: int = 200) -> Response:
    user = require_login(request)
    return Response.html(
        page(
            request,
            t("account.title"),
            _account_details(user),
            _delete_form(request, error=error),
        ),
        status,
    )


def account_delete_submit(request: Request) -> Response:
    user = require_login(request)
    try:
        accounts.delete_account(
            request.db,
            actor=user,
            user_id=user.id,
            current_password=request.form.get("current_password") or "",
        )
    except AppError as err:
        return account_page(
            request,
            error=error_message(err.code, **err.details),
            status=err.status,
        )

    # The session was revoked inside the transaction; clearing the cookie
    # stops the next request presenting a dead session and being bounced
    # through the login page with no explanation.
    response = Response.redirect("/login?deleted=1")
    response.clear_cookie(SESSION_COOKIE)
    return response


# --- registration --------------------------------------------------------------


def _register_form(request: Request, *, values: dict | None = None, error: str = "") -> Markup:
    values = values or {}
    banner = notice(error, kind="error") if error else Markup("")
    return div(
        banner,
        form(
            _csrf_hidden(request),
            field("email", t("auth.field.email"), type="email", value=values.get("email", "")),
            field("password", t("auth.field.password"), type="password"),
            field("full_name", t("auth.field.full_name"), value=values.get("full_name", "")),
            field("department", t("auth.field.department"), value=values.get("department", "")),
            field("phone", t("auth.field.phone"), value=values.get("phone", "")),
            _actions(button(t("auth.register.submit"), type="submit")),
            method="post",
            action="/register",
        ),
        p(a(t("auth.register.login_link"), href="/login")),
        class_="panel stack",
    )


def register_page(request: Request) -> Response:
    if request.user is not None:
        return Response.redirect("/day")
    return Response.html(page(request, t("auth.register.title"), _register_form(request)))


def register_submit(request: Request) -> Response:
    values = {
        key: (request.form.get(key) or "").strip()
        for key in ("email", "full_name", "department", "phone")
    }
    password = request.form.get("password") or ""

    try:
        result = accounts.register(
            request.db,
            email=values["email"],
            password=password,
            full_name=values["full_name"],
            department=values["department"],
            phone=values["phone"],
        )
    except AppError as err:
        message = error_message(err.code, **err.details)
        body = _register_form(request, values=values, error=message)
        return Response.html(page(request, t("auth.register.title"), body), err.status)

    mailer.enqueue(request.db, result.emails)
    # Spec §6.1: a generic response, whether or not the address already has
    # an account -- never confirm or deny existence.
    body = div(
        notice(t("auth.register.success"), kind="success"),
        p(a(t("auth.login.title"), href="/login")),
        class_="panel",
    )
    return Response.html(page(request, t("auth.register.title"), body))


# --- email verification --------------------------------------------------------


def verify_page(request: Request) -> Response:
    token = request.query.get("token", "")
    try:
        accounts.verify_email(request.db, token)
    except AppError as err:
        if err.code == TOKEN_EXPIRED:
            row = _load_token_row(request, token, models.VERIFY_EMAIL)
            email_value = row["email"] if row else ""
            body = div(
                notice(t("auth.verify.expired"), kind="warning"),
                form(
                    _csrf_hidden(request),
                    hidden("email", email_value),
                    _actions(button(t("auth.verify.resend"), type="submit")),
                    method="post",
                    action="/verify",
                ),
                class_="panel stack",
            )
            return Response.html(page(request, t("auth.verify.title"), body), err.status)
        message = error_message(err.code, **err.details)
        body = notice(message, kind="error")
        return Response.html(page(request, t("auth.verify.title"), body), err.status)

    body = div(
        notice(t("auth.verify.success"), kind="success"),
        p(a(t("auth.login.title"), href="/login")),
        class_="panel",
    )
    return Response.html(page(request, t("auth.verify.title"), body))


def verify_resend(request: Request) -> Response:
    email = (request.form.get("email") or "").strip()
    if email:
        accounts.resend_verification(request.db, email)
    body = div(
        notice(t("auth.verify.resent"), kind="success"),
        p(a(t("auth.login.title"), href="/login")),
        class_="panel",
    )
    return Response.html(page(request, t("auth.verify.title"), body))


# --- invitation acceptance ------------------------------------------------------


def _invite_form(request: Request, *, token: str, email: str, values: dict | None = None, error: str = "") -> Markup:
    values = values or {}
    banner = notice(error, kind="error") if error else Markup("")
    return div(
        banner,
        p(t("auth.invite.intro")),
        form(
            _csrf_hidden(request),
            hidden("token", token),
            field("email", t("auth.field.email"), type="email", value=email, readonly=True),
            field("password", t("auth.field.password"), type="password"),
            field("full_name", t("auth.field.full_name"), value=values.get("full_name", "")),
            field("department", t("auth.field.department"), value=values.get("department", "")),
            field("phone", t("auth.field.phone"), value=values.get("phone", "")),
            _actions(button(t("auth.invite.submit"), type="submit")),
            method="post",
            action="/invite",
        ),
        class_="panel stack",
    )


def invite_page(request: Request) -> Response:
    token = request.query.get("token", "")
    row = _load_token_row(request, token, models.INVITE)
    err_code = _token_error(row)
    if err_code:
        return Response.html(
            page(request, t("auth.invite.title"), notice(error_message(err_code), kind="error")),
            400,
        )
    return Response.html(page(request, t("auth.invite.title"), _invite_form(request, token=token, email=row["email"])))


def invite_submit(request: Request) -> Response:
    token = request.form.get("token", "")
    row = _load_token_row(request, token, models.INVITE)
    err_code = _token_error(row)
    if err_code:
        return Response.html(
            page(request, t("auth.invite.title"), notice(error_message(err_code), kind="error")),
            400,
        )

    values = {
        key: (request.form.get(key) or "").strip()
        for key in ("full_name", "department", "phone")
    }
    password = request.form.get("password") or ""

    try:
        user = accounts.accept_invitation(
            request.db,
            token,
            password=password,
            full_name=values["full_name"],
            department=values["department"],
            phone=values["phone"],
        )
    except AppError as err:
        message = error_message(err.code, **err.details)
        body = _invite_form(request, token=token, email=row["email"], values=values, error=message)
        return Response.html(page(request, t("auth.invite.title"), body), err.status)

    # Spec §6.1: the invitation link proves the address, so the account is
    # active immediately -- sign them straight in.
    response = Response.redirect("/day")
    _set_session_cookie(request, response, user)
    return response


# --- forgot / reset password ----------------------------------------------------


def _forgot_form(request: Request, *, email: str = "", error: str = "") -> Markup:
    banner = notice(error, kind="error") if error else Markup("")
    return div(
        banner,
        form(
            _csrf_hidden(request),
            field("email", t("auth.field.email"), type="email", value=email),
            _actions(button(t("auth.forgot.submit"), type="submit")),
            method="post",
            action="/forgot",
        ),
        class_="panel stack",
    )


def forgot_page(request: Request) -> Response:
    return Response.html(page(request, t("auth.forgot.title"), _forgot_form(request)))


def forgot_submit(request: Request) -> Response:
    email = (request.form.get("email") or "").strip()
    if email:
        try:
            accounts.request_password_reset(request.db, email)
        except AppError:
            # Privacy: a rate-limit (or any other) failure still gets the
            # same generic response -- never a signal about the address.
            pass
    body = div(
        notice(t("auth.forgot.success"), kind="success"),
        p(a(t("auth.login.title"), href="/login")),
        class_="panel",
    )
    return Response.html(page(request, t("auth.forgot.title"), body))


def _reset_form(request: Request, *, token: str, error: str = "") -> Markup:
    banner = notice(error, kind="error") if error else Markup("")
    return div(
        banner,
        form(
            _csrf_hidden(request),
            hidden("token", token),
            field("password", t("auth.field.new_password"), type="password"),
            field("confirm_password", t("auth.field.confirm_password"), type="password"),
            _actions(button(t("auth.reset.submit"), type="submit")),
            method="post",
            action="/reset",
        ),
        class_="panel stack",
    )


def reset_page(request: Request) -> Response:
    token = request.query.get("token", "")
    row = _load_token_row(request, token, models.PASSWORD_RESET)
    err_code = _token_error(row)
    if err_code:
        return Response.html(
            page(request, t("auth.reset.title"), notice(error_message(err_code), kind="error")),
            400,
        )
    return Response.html(page(request, t("auth.reset.title"), _reset_form(request, token=token)))


def reset_submit(request: Request) -> Response:
    token = request.form.get("token", "")
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm_password") or ""

    if password != confirm:
        body = _reset_form(request, token=token, error=t("auth.reset.mismatch"))
        return Response.html(page(request, t("auth.reset.title"), body), 400)

    try:
        accounts.reset_password(request.db, token, password)
    except AppError as err:
        message = error_message(err.code, **err.details)
        body = _reset_form(request, token=token, error=message)
        return Response.html(page(request, t("auth.reset.title"), body), err.status)

    body = div(
        notice(t("auth.reset.success"), kind="success"),
        p(a(t("auth.login.title"), href="/login")),
        class_="panel",
    )
    return Response.html(page(request, t("auth.reset.title"), body))


# --- change password (spec §10.3 forced first-login change) --------------------


def _password_form(request: Request, *, error: str = "") -> Markup:
    banner = notice(error, kind="error") if error else Markup("")
    return div(
        banner,
        form(
            _csrf_hidden(request),
            field("current_password", t("auth.field.current_password"), type="password"),
            field("new_password", t("auth.field.new_password"), type="password"),
            field("confirm_new_password", t("auth.field.confirm_password"), type="password"),
            _actions(button(t("auth.password.submit"), type="submit")),
            method="post",
            action="/password",
        ),
        class_="panel stack",
    )


def password_page(request: Request) -> Response:
    user = require_login(request)
    parts = []
    if user.must_change_password:
        parts.append(notice(t("auth.password.forced_notice"), kind="warning"))
    parts.append(_password_form(request))
    return Response.html(page(request, t("auth.password.title"), *parts))


def password_submit(request: Request) -> Response:
    user = require_login(request)
    current = request.form.get("current_password") or ""
    new_password = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_new_password") or ""

    if new_password != confirm:
        body = _password_form(request, error=t("auth.reset.mismatch"))
        return Response.html(page(request, t("auth.password.title"), body), 400)

    try:
        accounts.change_password(request.db, user, current, new_password)
    except AppError as err:
        message = error_message(err.code, **err.details)
        body = _password_form(request, error=message)
        return Response.html(page(request, t("auth.password.title"), body), err.status)

    return Response.redirect("/day")


# --- registration --------------------------------------------------------------


def register(router: Router) -> None:
    router.add("GET", "/login", login_page)
    router.add("POST", "/login", login_submit)
    router.add("POST", "/logout", logout)
    router.add("GET", "/register", register_page)
    router.add("POST", "/register", register_submit)
    router.add("GET", "/verify", verify_page)
    router.add("POST", "/verify", verify_resend)
    router.add("GET", "/invite", invite_page)
    router.add("POST", "/invite", invite_submit)
    router.add("GET", "/forgot", forgot_page)
    router.add("POST", "/forgot", forgot_submit)
    router.add("GET", "/reset", reset_page)
    router.add("POST", "/reset", reset_submit)
    router.add("GET", "/account", account_page)
    router.add("POST", "/account/delete", account_delete_submit)
    router.add("GET", "/password", password_page)
    router.add("POST", "/password", password_submit)
