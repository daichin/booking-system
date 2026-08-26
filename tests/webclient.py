"""An in-process WSGI test client.

Real sockets are deliberately avoided: the suite stays fast and works on a
machine with no network. Cookies are preserved between requests, so a test can
log in and then behave like a signed-in browser.
"""

from __future__ import annotations

import io
import json as jsonlib
from typing import Any
from urllib.parse import unquote, urlencode


class Response:
    """The outcome of one request through the WSGI stack."""

    def __init__(self, status: int, headers: list, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def location(self) -> str:
        for name, value in self.headers:
            if name.lower() == "location":
                return value
        return ""

    def header(self, name: str) -> str:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return ""

    def json(self) -> Any:
        return jsonlib.loads(self.body or b"{}")

    def __repr__(self) -> str:
        return f"<Response {self.status} {len(self.body)}b>"


class Client:
    """Drives a WSGI application in-process, keeping cookies."""

    def __init__(self, app) -> None:
        self.app = app
        self.cookies: dict[str, str] = {}

    def request(
        self,
        method: str,
        path: str,
        *,
        form: dict | None = None,
        json_body: Any = None,
        headers: dict | None = None,
        csrf: bool = True,
    ) -> Response:
        # A browser never sends the fragment to the server, and links in this
        # app carry one so the page lands where the member was looking. Drop
        # it here or it ends up glued to the last query parameter.
        path, _, _fragment = path.partition("#")

        query = ""
        if "?" in path:
            path, _, query = path.partition("?")

        body = b""
        content_type = ""
        if json_body is not None:
            body = jsonlib.dumps(json_body).encode("utf-8")
            content_type = "application/json"
        elif form is not None:
            payload = dict(form)
            if csrf and method.upper() != "GET":
                payload.setdefault("_csrf", self.cookies.get("csrf", ""))
            body = urlencode(payload).encode("utf-8")
            content_type = "application/x-www-form-urlencoded"

        environ: dict[str, Any] = {
            "REQUEST_METHOD": method.upper(),
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "SERVER_NAME": "testserver",
            "SERVER_PORT": "80",
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(body),
            "CONTENT_LENGTH": str(len(body)),
        }
        if content_type:
            environ["CONTENT_TYPE"] = content_type
        if self.cookies:
            environ["HTTP_COOKIE"] = "; ".join(
                f"{key}={value}" for key, value in self.cookies.items()
            )
        if csrf and method.upper() != "GET" and json_body is not None:
            environ["HTTP_X_CSRF_TOKEN"] = self.cookies.get("csrf", "")
        environ.update(headers or {})

        captured: dict[str, Any] = {}

        def start_response(status: str, response_headers: list) -> None:
            captured["status"] = int(status.split(" ", 1)[0])
            captured["headers"] = response_headers

        payload = b"".join(self.app(environ, start_response))
        self._absorb_cookies(captured["headers"])
        return Response(captured["status"], captured["headers"], payload)

    def _absorb_cookies(self, headers: list) -> None:
        for name, value in headers:
            if name.lower() != "set-cookie":
                continue
            morsel = value.split(";", 1)[0]
            key, _, cookie_value = morsel.partition("=")
            if "Max-Age=0" in value:
                self.cookies.pop(key, None)
            else:
                self.cookies[key] = unquote(cookie_value)

    def get(self, path: str, **kwargs) -> Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Response:
        return self.request("POST", path, **kwargs)

    def follow(self, response: Response) -> Response:
        """Follow a single redirect, if there is one."""
        return self.get(response.location) if response.location else response
