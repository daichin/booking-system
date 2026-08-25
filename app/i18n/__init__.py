"""Translation lookup.

Spec: all user-facing text is Traditional Chinese and lives in a single file so
it can be swapped later. :mod:`app.i18n.zh_TW` is that file; nothing outside
this package should contain a user-facing sentence.
"""

from __future__ import annotations

from app.i18n import zh_TW

DEFAULT_LOCALE = "zh-TW"

_CATALOGUES = {"zh-TW": zh_TW.STRINGS}


def t(key: str, /, locale: str = DEFAULT_LOCALE, **params: object) -> str:
    """Look up ``key`` and interpolate ``params``.

    An unknown key returns the key itself rather than raising: a missing
    translation should never take down a page, and it is obvious on screen.
    """
    catalogue = _CATALOGUES.get(locale, zh_TW.STRINGS)
    template = catalogue.get(key)
    if template is None:
        return key
    if not params:
        return template
    try:
        return template.format(**params)
    except (KeyError, IndexError):
        return template


def error_message(code: str, /, locale: str = DEFAULT_LOCALE, **params: object) -> str:
    """Human wording for an error code from :mod:`app.errors`."""
    return t(f"error.{code}", locale=locale, **params)


__all__ = ["DEFAULT_LOCALE", "error_message", "t"]
