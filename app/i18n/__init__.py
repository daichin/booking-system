"""Translation lookup.

Each locale is one module exporting ``STRINGS``. ``zh_TW`` is the reference
catalogue: it defines which keys exist, and every other locale mirrors it.

Spec §2 C8 fixed the language as zh-TW and §13 put other languages outside
v1. English was added afterwards at the owner's request, which is why the
architecture already allowed for it -- the original rule was "keep all
strings in a single file so they can be swapped later".
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from app.i18n import zh_TW

#: English is the primary language: most use of this deployment is in
#: English, so it is what an unrecognised visitor gets. zh-TW remains a
#: first-class locale and every string exists in both.
DEFAULT_LOCALE = "en"

#: Offered in the language switcher, in display order.
AVAILABLE_LOCALES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("zh-TW", "中文"),
)

SUPPORTED = tuple(code for code, _ in AVAILABLE_LOCALES)


def _load(name: str) -> dict[str, str]:
    try:
        module = __import__(f"app.i18n.{name}", fromlist=["STRINGS"])
    except ImportError:  # a locale that has not been written yet
        return {}
    return dict(getattr(module, "STRINGS", {}))


#: English falls back to the zh-TW entry for any key it is missing, so a
#: partially translated catalogue degrades to a readable page rather than
#: showing raw keys.
_CATALOGUES: dict[str, dict[str, str]] = {
    "zh-TW": dict(zh_TW.STRINGS),
    "en": {**zh_TW.STRINGS, **_load("en")},
}


def normalise(locale: str | None) -> str:
    """Map anything user- or browser-supplied onto a locale we actually have."""
    if not locale:
        return DEFAULT_LOCALE
    code = locale.strip()
    if code in _CATALOGUES:
        return code
    # "en-GB" -> "en", "zh-Hant-TW" -> "zh-TW"
    lowered = code.lower()
    if lowered.startswith("zh"):
        return "zh-TW"
    if lowered.startswith("en"):
        return "en"
    return DEFAULT_LOCALE


def from_accept_header(header: str) -> str | None:
    """Best supported match from an ``Accept-Language`` header, or ``None``.

    Used only as an initial guess for a visitor who has never chosen; an
    explicit choice always wins over this.
    """
    best: tuple[float, str] | None = None
    for part in header.split(","):
        piece, _, params = part.strip().partition(";")
        if not piece:
            continue
        quality = 1.0
        if params.startswith("q="):
            try:
                quality = float(params[2:])
            except ValueError:
                quality = 0.0
        lowered = piece.strip().lower()
        if lowered.startswith("zh"):
            candidate = "zh-TW"
        elif lowered.startswith("en"):
            candidate = "en"
        else:
            continue
        if best is None or quality > best[0]:
            best = (quality, candidate)
    return best[1] if best else None


#: The locale of the request being handled. Set once per request, so the
#: hundreds of existing ``t("key")`` calls in the page modules translate
#: without every one of them having to thread a locale argument through.
#: Context variables are per-thread, so concurrent requests cannot see
#: each other's value.
_current: ContextVar[str] = ContextVar("locale", default=DEFAULT_LOCALE)


def set_locale(code: str) -> None:
    """Set the locale for the request being handled on this thread."""
    _current.set(normalise(code))


def current_locale() -> str:
    return _current.get()


def t(key: str, /, locale: str | None = None, **params: Any) -> str:
    """Look up ``key`` and interpolate ``params``.

    Defaults to the locale of the request being handled. An unknown key
    returns the key itself rather than raising: a missing translation
    should never take down a page, and it is obvious on screen.
    """
    catalogue = _CATALOGUES.get(locale or _current.get()) or _CATALOGUES[DEFAULT_LOCALE]
    template = catalogue.get(key)
    if template is None:
        return key
    if not params:
        return template
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return template


def error_message(code: str, /, locale: str | None = None, **params: Any) -> str:
    """Human wording for an error code from :mod:`app.errors`."""
    return t(f"error.{code}", locale=locale, **params)


def missing_keys(locale: str) -> list[str]:
    """Keys ``locale`` has not translated yet. Used by the test suite."""
    translated = _load(locale)
    return sorted(key for key in zh_TW.STRINGS if key not in translated)


__all__ = [
    "AVAILABLE_LOCALES",
    "DEFAULT_LOCALE",
    "SUPPORTED",
    "current_locale",
    "error_message",
    "from_accept_header",
    "missing_keys",
    "normalise",
    "set_locale",
    "t",
]
