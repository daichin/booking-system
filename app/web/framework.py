"""A small WSGI request/response layer.

Hand-rolled rather than pulled from PyPI so the app has no web-framework
dependency: it runs under :mod:`wsgiref` locally and gunicorn in production
with the same code.

Scope is deliberately narrow -- routing, form/JSON parsing, cookies, CSRF, and
error rendering. Anything resembling business logic belongs in
:mod:`app.services`.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote

from app.errors import (
    AppError,
    AuthError,
    ForbiddenError,
    NOT_ADMIN,
    NOT_AUTHENTICATED,
)
from app.i18n import error_message, t

#: Name of the session cookie (CONTRACT.md §5).
SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf"
CSRF_FIELD = "_csrf"
CSRF_HEADER = "HTTP_X_CSRF_TOKEN"

log = logging.getLogger("app.web")

_MAX_BODY = 1 << 20  # 1 MB is ample for these forms and refuses silly payloads


class Request:
    """One HTTP request, with lazily parsed body."""

    def __init__(self, environ: dict[str, Any]) -> None:
        self.environ = environ
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        self.path = environ.get("PATH_INFO", "/") or "/"
        self.query = _flatten(parse_qs(environ.get("QUERY_STRING", "")))
        self.params: dict[str, str] = {}
        self.user = None          # set by the application after session lookup
        self.db = None
        self.config = None
        self._body: bytes | None = None
        self._form: dict[str, str] | None = None
        self._json: Any = None

    # --- body ------------------------------------------------------------

    @property
    def body(self) -> bytes:
        if self._body is None:
            try:
                length = int(self.environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            length = max(0, min(length, _MAX_BODY))
            stream = self.environ.get("wsgi.input")
            self._body = stream.read(length) if stream and length else b""
        return self._body

    @property
    def content_type(self) -> str:
        return (self.environ.get("CONTENT_TYPE") or "").split(";")[0].strip().lower()

    @property
    def is_json(self) -> bool:
        return self.content_type == "application/json"

    @property
    def form(self) -> dict[str, str]:
        if self._form is None:
            if self.is_json:
                data = self.json if isinstance(self.json, dict) else {}
                self._form = {k: "" if v is None else str(v) for k, v in data.items()}
            else:
                self._form = _flatten(
                    parse_qs(self.body.decode("utf-8", "replace"), keep_blank_values=True)
                )
        return self._form

    @property
    def json(self) -> Any:
        if self._json is None:
            try:
                self._json = json.loads(self.body or b"{}")
            except (ValueError, UnicodeDecodeError):
                self._json = {}
        return self._json

    # --- headers and cookies ---------------------------------------------

    @property
    def cookies(self) -> dict[str, str]:
        jar = SimpleCookie()
        jar.load(self.environ.get("HTTP_COOKIE", ""))
        return {key: morsel.value for key, morsel in jar.items()}

    @property
    def is_secure(self) -> bool:
        if self.environ.get("wsgi.url_scheme") == "https":
            return True
        # Render terminates TLS in front of the app.
        return self.environ.get("HTTP_X_FORWARDED_PROTO", "").split(",")[0] == "https"

    @property
    def wants_json(self) -> bool:
        if self.path.startswith("/api/"):
            return True
        accept = self.environ.get("HTTP_ACCEPT", "")
        return "application/json" in accept and "text/html" not in accept

    def header(self, name: str) -> str:
        return self.environ.get(name, "")


@dataclass
class Response:
    body: bytes = b""
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def html(cls, markup: str, status: int = 200) -> "Response":
        return cls(markup.encode("utf-8"), status)

    @classmethod
    def json(cls, payload: Any, status: int = 200) -> "Response":
        return cls(
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            status,
            "application/json; charset=utf-8",
        )

    @classmethod
    def redirect(cls, location: str, status: int = 303) -> "Response":
        response = cls(b"", status)
        response.headers.append(("Location", location))
        return response

    @classmethod
    def text(cls, body: str, status: int = 200) -> "Response":
        return cls(body.encode("utf-8"), status, "text/plain; charset=utf-8")

    def set_cookie(
        self,
        name: str,
        value: str,
        *,
        max_age: int | None = None,
        http_only: bool = True,
        secure: bool = False,
        same_site: str = "Lax",
        path: str = "/",
    ) -> "Response":
        parts = [f"{name}={quote(value)}", f"Path={path}", f"SameSite={same_site}"]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        if http_only:
            parts.append("HttpOnly")
        if secure:
            parts.append("Secure")
        self.headers.append(("Set-Cookie", "; ".join(parts)))
        return self

    def clear_cookie(self, name: str, *, path: str = "/") -> "Response":
        self.headers.append(
            ("Set-Cookie", f"{name}=; Path={path}; Max-Age=0; HttpOnly; SameSite=Lax")
        )
        return self


Handler = Callable[[Request], Response]

_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class Route:
    def __init__(self, method: str, pattern: str, handler: Handler, **flags: Any):
        self.method = method.upper()
        self.pattern = pattern
        self.handler = handler
        self.flags = flags
        regex = _PARAM.sub(lambda m: f"(?P<{m.group(1)}>[^/]+)", pattern)
        self.regex = re.compile(f"^{regex}$")


class Router:
    def __init__(self) -> None:
        self.routes: list[Route] = []

    def add(self, method: str, pattern: str, handler: Handler, **flags: Any) -> None:
        self.routes.append(Route(method, pattern, handler, **flags))

    def get(self, pattern: str, **flags: Any):
        def decorate(handler: Handler) -> Handler:
            self.add("GET", pattern, handler, **flags)
            return handler

        return decorate

    def post(self, pattern: str, **flags: Any):
        def decorate(handler: Handler) -> Handler:
            self.add("POST", pattern, handler, **flags)
            return handler

        return decorate

    def resolve(self, method: str, path: str) -> tuple[Route | None, bool]:
        """Return ``(route, path_matched)``.

        ``path_matched`` distinguishes 404 from 405, so a form posted to a
        GET-only page reports the right thing.
        """
        matched_path = False
        for route in self.routes:
            match = route.regex.match(path)
            if not match:
                continue
            matched_path = True
            if route.method == method.upper():
                return route, True
        return None, matched_path


def _flatten(values: dict[str, list[str]]) -> dict[str, str]:
    """First value wins; these forms have no repeated fields."""
    return {key: value[0] for key, value in values.items() if value}


def issue_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def csrf_ok(request: Request) -> bool:
    """Double-submit cookie check.

    The token is delivered in a readable cookie and must be echoed back in the
    form body or the ``X-CSRF-Token`` header. An attacker's site can cause a
    request but cannot read our cookie, so it cannot echo the value.
    """
    expected = request.cookies.get(CSRF_COOKIE, "")
    if not expected:
        return False
    supplied = request.form.get(CSRF_FIELD) or request.header(CSRF_HEADER)
    return bool(supplied) and secrets.compare_digest(supplied, expected)


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Applied to every response. The CSP is strict because the UI ships no
#: third-party assets; inline styles are allowed for the single embedded
#: stylesheet, inline scripts are not.
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "same-origin"),
    (
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "
        "base-uri 'none'",
    ),
)


class WSGIApp:
    """Ties the router to the WSGI protocol."""

    def __init__(
        self,
        router: Router,
        *,
        on_request: Callable | None = None,
        after_request: Callable | None = None,
    ) -> None:
        self.router = router
        self.on_request = on_request
        # Runs for every response, successful or not -- used to issue the CSRF
        # cookie so that even an error page can host a working form.
        self.after_request = after_request

    def __call__(self, environ: dict[str, Any], start_response) -> Iterable[bytes]:
        request = Request(environ)
        started = time.monotonic()
        failure: str = ""
        try:
            response = self.dispatch(request)
        except AppError as error:
            # A rejected request is the single most useful thing to see in a
            # production log: without the code and details, a 400 in the
            # access log says only "something was wrong somewhere".
            failure = _describe_error(error)
            response = self.render_error(request, error)
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace to a user
            failure = f"{type(exc).__name__}: {exc}"
            log.exception(
                "unhandled error on %s %s", request.method, request.path
            )
            response = self.render_error(request, AppError("INTERNAL"), status=500)

        self._log_request(request, response, started, failure)

        if self.after_request is not None:
            response = self.after_request(request, response) or response

        headers = [("Content-Type", response.content_type)]
        headers.extend(SECURITY_HEADERS)
        headers.extend(response.headers)
        headers.append(("Content-Length", str(len(response.body))))
        start_response(f"{response.status} {_reason(response.status)}", headers)
        return [response.body]

    def dispatch(self, request: Request) -> Response:
        route, path_matched = self.router.resolve(request.method, request.path)
        if route is None:
            if path_matched:
                return self.render_error(
                    request, AppError("METHOD_NOT_ALLOWED"), status=405
                )
            return self.render_error(request, AppError("NOT_FOUND"), status=404)

        match = route.regex.match(request.path)
        request.params = match.groupdict() if match else {}

        if self.on_request is not None:
            early = self.on_request(request, route)
            if early is not None:
                return early

        if request.method not in SAFE_METHODS and not route.flags.get("csrf_exempt"):
            if not csrf_ok(request):
                return self.render_error(request, ForbiddenError("CSRF_FAILED"), 403)

        return route.handler(request)

    def _log_request(
        self, request: Request, response: Response, started: float, failure: str
    ) -> None:
        """One line per request, with the reason when it was refused."""
        duration_ms = (time.monotonic() - started) * 1000
        who = getattr(request.user, "id", None)
        line = "%s %s -> %d (%.0fms)"
        args: list[Any] = [request.method, request.path, response.status, duration_ms]
        if who:
            line += " user=%s"
            args.append(who[:8])
        if failure:
            line += " reason=%s"
            args.append(failure)
        if response.status >= 500:
            log.error(line, *args)
        elif response.status >= 400:
            log.warning(line, *args)
        else:
            log.info(line, *args)

    def render_error(
        self, request: Request, error: AppError, status: int | None = None
    ) -> Response:
        code = status or getattr(error, "status", 400)
        if request.wants_json:
            return Response.json(error.to_dict(), code)
        if error.code == NOT_AUTHENTICATED:
            target = quote(request.path, safe="/")
            return Response.redirect(f"/login?next={target}")
        from app.web.layout import error_page

        return Response.html(
            error_page(request, code, error_message(error.code, **_humanise(error.details))),
            code,
        )


def _describe_error(error: AppError) -> str:
    """One-line summary of a rejected request, safe to write to a log.

    Details are included because they carry the *why* -- which field, which
    quota, which blocking rule. Anything that could identify a person is
    dropped: logs are read by whoever runs the server, not just admins.
    """
    redacted = {
        key: value
        for key, value in error.details.items()
        if key not in _SENSITIVE_DETAIL_KEYS
    }
    return f"{error.code} {redacted}" if redacted else error.code


#: Never write these to the log, even though the app may use them internally.
_SENSITIVE_DETAIL_KEYS = frozenset({"email", "to_email", "owner", "blocker", "token"})


def _humanise(details: dict[str, Any]) -> dict[str, Any]:
    """Translate machine field names before they reach a person.

    Services raise ``MISSING_FIELD`` with ``field="start_time"``; showing a
    member the literal column name is not an error message. Anything without
    a translation falls through unchanged.
    """
    if "field" not in details:
        return details
    readable = dict(details)
    key = f"field.{details['field']}"
    label = t(key)
    readable["field"] = details["field"] if label == key else label
    return readable


_REASONS = {
    200: "OK", 201: "Created", 204: "No Content", 303: "See Other",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 409: "Conflict", 429: "Too Many Requests",
    500: "Internal Server Error",
}


def _reason(status: int) -> str:
    return _REASONS.get(status, "OK")


def require_login(request: Request):
    """Return the signed-in user, or raise.

    HTML callers are bounced to the login page by :meth:`WSGIApp.render_error`;
    API callers get a 401 with the error code.
    """
    if request.user is None:
        raise AuthError(NOT_AUTHENTICATED)
    return request.user


def require_admin(request: Request) -> None:
    if request.user is None or not request.user.is_admin:
        raise ForbiddenError(NOT_ADMIN)
