"""Traditional Chinese strings introduced by Task 1 (accounts and auth).

Fragment merged into the main catalogue by :mod:`app.i18n` -- see that
module's docstring for how fragments are folded together. Only the two new
error codes Task 1 added to :mod:`app.errors` live here; every other code
this task raises (``INVALID_CREDENTIALS``, ``TOKEN_EXPIRED``, ``NOT_ADMIN``,
...) already has wording in ``app/i18n/zh_TW.py`` and must not be duplicated.
"""

from __future__ import annotations

STRINGS: dict[str, str] = {
    "error.ACCOUNT_EXISTS": "此電子郵件已經有帳號了。",
    "error.INVITATION_NOT_FOUND": "找不到指定的邀請。",
}
