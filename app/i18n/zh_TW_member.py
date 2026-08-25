"""Traditional Chinese strings introduced by Task 5 (member UI and auth pages).

Fragment merged into the main catalogue by :mod:`app.i18n` -- see that
module's docstring for how fragments are folded together. Error codes already
have wording in ``app/i18n/zh_TW.py`` (or another task's fragment) and must
not be duplicated here; this file only adds page copy specific to the member
screens and the auth flows (spec §8, §7.2).
"""

from __future__ import annotations

STRINGS: dict[str, str] = {
    # --- shared form field captions ----------------------------------------
    "auth.field.email": "電子郵件",
    "auth.field.password": "密碼",
    "auth.field.full_name": "姓名",
    "auth.field.department": "部門",
    "auth.field.phone": "電話",
    "auth.field.new_password": "新密碼",
    "auth.field.confirm_password": "確認密碼",
    "auth.field.current_password": "目前密碼",

    # --- login -----------------------------------------------------------------
    "auth.login.title": "登入",
    "auth.login.submit": "登入",
    "auth.login.register_link": "還沒有帳號？註冊",
    "auth.login.forgot_link": "忘記密碼？",

    # --- registration ------------------------------------------------------------
    "auth.register.title": "註冊",
    "auth.register.submit": "註冊",
    "auth.register.success": "若此電子郵件可以註冊，我們已寄出驗證信，請至信箱查收。",
    "auth.register.login_link": "已經有帳號？登入",

    # --- email verification --------------------------------------------------------
    "auth.verify.title": "電子郵件驗證",
    "auth.verify.success": "您的電子郵件已完成驗證，請等待管理員審核帳號。",
    "auth.verify.expired": "此驗證連結已過期。",
    "auth.verify.resend": "重新寄送驗證信",
    "auth.verify.resent": "若此電子郵件需要驗證，我們已重新寄出驗證信，請至信箱查收。",

    # --- invitation acceptance ---------------------------------------------------
    "auth.invite.title": "接受邀請",
    "auth.invite.intro": "請完成以下資料以啟用帳號，電子郵件已由邀請信確認，無法變更。",
    "auth.invite.submit": "建立帳號",

    # --- forgot / reset password --------------------------------------------------
    "auth.forgot.title": "忘記密碼",
    "auth.forgot.submit": "寄送重設密碼連結",
    "auth.forgot.success": "若此電子郵件有對應帳號，我們已寄出密碼重設連結，請至信箱查收。",
    "auth.reset.title": "重設密碼",
    "auth.reset.submit": "重設密碼",
    "auth.reset.success": "密碼已重設，請使用新密碼登入。",
    "auth.reset.mismatch": "兩次輸入的密碼不一致。",

    # --- forced/voluntary password change -------------------------------------------
    "auth.password.title": "變更密碼",
    "auth.password.forced_notice": "為了帳號安全，請先設定一組新密碼才能繼續使用系統。",
    "auth.password.submit": "更新密碼",

    # --- day view --------------------------------------------------------------
    "day.slot_free": "空閒",
    "day.prev": "‹ 前一天",
    "day.next": "下一天 ›",
    "day.legend.mine": "我的預約",
    "day.legend.other": "他人已預約",
    "day.legend.free": "空閒",
    "day.no_rooms": "目前沒有可預約的會議室。",
    "day.book.title": "建立預約",
    "day.book.room": "會議室",
    "day.book.date": "日期",
    "day.book.start": "開始時間",
    "day.book.end": "結束時間",
    "day.book.subject": "會議主題",
    "day.book.submit": "送出預約要求",

    # --- week view -------------------------------------------------------------
    "week.room": "會議室",
    "week.prev": "‹ 前一週",
    "week.next": "下一週 ›",
    "week.view": "切換",

    # --- my bookings -----------------------------------------------------------
    "my.upcoming": "即將到來",
    "my.past": "歷史紀錄",
    "my.none_upcoming": "目前沒有即將到來的預約。",
    "my.none_past": "尚無歷史紀錄。",
    "my.room": "會議室",
    "my.time": "時間",
    "my.title_col": "主題",
    "my.status": "狀態",
    "my.cancel": "取消預約",
    "my.booked_success": "預約已建立成功。",
    "my.cancelled_success": "預約已取消。",

    # --- two-phase preemption confirmation (spec §7.2) --------------------------
    "booking.confirm.title": "需要確認：覆蓋既有預約",
    "booking.confirm.message": "此操作將取消 {count} 筆既有預約，受影響的成員會收到電子郵件通知。是否繼續？",
    "booking.confirm.submit": "確認覆蓋並送出",
    "booking.confirm.cancel": "取消，返回",

    "booking.blocked.title": "無法建立預約",
    "booking.blocked.back": "返回當日總覽",
}
