"""Translation lookup.

Spec: all user-facing text is Traditional Chinese and lives in a single file so
it can be swapped later. :mod:`app.i18n.zh_TW` is that file; nothing outside
this package should contain a user-facing sentence.
"""

from __future__ import annotations

import importlib
import pkgutil

from app.i18n import zh_TW

DEFAULT_LOCALE = "zh-TW"


def _load_zh_tw() -> dict[str, str]:
    """Merge the main catalogue with any ``zh_TW_<area>`` fragments.

    Fragments exist so that tasks built in parallel do not all edit one file.
    They are folded back into ``zh_TW.py`` before release, leaving the single
    catalogue the spec asks for.
    """
    merged = dict(zh_TW.STRINGS)
    for module in pkgutil.iter_modules(__path__):
        if module.name.startswith("zh_TW_"):
            fragment = importlib.import_module(f"{__name__}.{module.name}")
            merged.update(getattr(fragment, "STRINGS", {}))
    return merged


_CATALOGUES = {"zh-TW": _load_zh_tw()}


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
