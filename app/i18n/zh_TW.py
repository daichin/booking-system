"""Traditional Chinese (zh-TW) strings -- the single translation catalogue.

Keys are namespaced by area: ``error.*`` mirrors :mod:`app.errors` codes,
``email.*`` covers the E1-E10 catalogue in spec §9.1, and the rest is UI.
"""

from __future__ import annotations

STRINGS: dict[str, str] = {
    # --- general ----------------------------------------------------------
    "app.name": "會議室預約系統",
    "app.timezone_note": "所有時間均為台北時間",
    "common.yes": "是",
    "common.no": "否",
    "common.save": "儲存",
    "common.cancel": "取消",
    "common.confirm": "確認",
    "common.back": "返回",
    "common.search": "搜尋",
    "common.export_csv": "匯出 CSV",
    "common.loading": "載入中…",
    "common.none": "無",

    # --- navigation and page shell -----------------------------------------
    "nav.login": "登入",
    "nav.register": "註冊",
    "nav.logout": "登出",
    "nav.day": "當日總覽",
    "nav.week": "單室週表",
    "nav.my_bookings": "我的預約",
    "nav.admin": "管理後台",
    "nav.home": "回首頁",
    "nav.skip": "跳到主要內容",
    "error.page_title": "發生問題",

    # --- booking validation, spec §6.5 -------------------------------------
    "error.NOT_ACTIVE": "您的帳號尚未通過管理員審核，目前無法預約。",
    "error.ROOM_INACTIVE": "此會議室已停用，無法預約。",
    "error.ROOM_NOT_FOUND": "找不到指定的會議室。",
    "error.OFF_GRID": "預約時間必須以 {slot_minutes} 分鐘為單位（例如 14:00、14:30）。",
    "error.END_NOT_AFTER_START": "結束時間必須晚於開始時間。",
    "error.TOO_LONG": "單次預約最長為 {max_minutes} 分鐘。",
    "error.START_IN_PAST": "無法預約已經過去的時間。",
    "error.BEYOND_HORIZON": "最多只能預約 {days} 天以內的時段。",
    "error.OUTSIDE_WINDOW": "此會議室的開放時間為 {open_time} 至 {close_time}。",
    "error.CROSSES_MIDNIGHT": "預約不可跨越午夜，請分兩次預約。",
    "error.QUOTA_EXCEEDED": "您目前的等級最多可同時保有 {quota} 筆未來預約，請先取消其他預約。",
    "error.TITLE_REQUIRED": "請填寫會議主題。",

    # --- preemption, spec §7 ------------------------------------------------
    "error.SELF_OVERLAP": "您在這個時段已經有預約了。",
    "error.EQUAL_OR_HIGHER_LEVEL": "此時段已被同等級或更高等級的成員預約，無法覆蓋。",
    "error.PROTECTED_WINDOW": "此預約即將開始，已進入保護時間，無法覆蓋。",

    # --- bookings -----------------------------------------------------------
    "error.BOOKING_NOT_FOUND": "找不到指定的預約。",
    "error.BOOKING_NOT_CONFIRMED": "此預約已不在生效中。",
    "error.BOOKING_ALREADY_ENDED": "此預約已結束，無法取消。",
    "error.NOT_BOOKING_OWNER": "您只能取消自己的預約。",

    # --- accounts and auth, spec §6.1 / §6.2 --------------------------------
    "error.INVALID_CREDENTIALS": "電子郵件或密碼不正確。",
    "error.ACCOUNT_SUSPENDED": "您的帳號已被停權，請聯絡管理員。",
    "error.ACCOUNT_REJECTED": "您的註冊申請未通過，請聯絡管理員。",
    "error.EMAIL_NOT_VERIFIED": "請先完成電子郵件驗證。",
    "error.AWAITING_APPROVAL": "您的帳號正在等待管理員審核。",
    "error.PASSWORD_TOO_SHORT": "密碼至少需要 8 個字元。",
    "error.PASSWORD_TOO_COMMON": "這組密碼過於常見，請改用其他密碼。",
    "error.PASSWORD_CHANGE_REQUIRED": "請先變更您的密碼。",
    "error.MISSING_FIELD": "請填寫「{field}」。",
    "error.INVALID_EMAIL": "電子郵件格式不正確。",
    "error.TOKEN_INVALID": "此連結無效，請重新申請。",
    "error.TOKEN_EXPIRED": "此連結已過期，請重新申請。",
    "error.TOKEN_USED": "此連結已經使用過了。",
    "error.LOGIN_RATE_LIMITED": "登入嘗試次數過多，請於 {minutes} 分鐘後再試。",
    "error.EMAIL_RATE_LIMITED": "寄送次數過多，請稍後再試。",
    "error.NOT_AUTHENTICATED": "請先登入。",
    "error.NOT_ADMIN": "此功能僅限管理員使用。",

    # --- admin --------------------------------------------------------------
    "error.USER_NOT_FOUND": "找不到指定的成員。",
    "error.INVALID_LEVEL": "等級必須介於 1 到 10 之間。",
    "error.INVALID_STATUS_TRANSITION": "無法從目前狀態執行此操作。",
    "error.ROOM_HAS_BOOKINGS": "此會議室仍有預約紀錄，無法刪除，請改為停用。",
    "error.INVALID_SETTING": "設定值不正確。",
    "error.CONFIRMATION_REQUIRED": "此操作需要再次確認。",
    "error.INTERNAL": "系統發生錯誤，請稍後再試。",
    "error.NOT_FOUND": "找不到頁面。",

    # --- user status labels -------------------------------------------------
    "status.pending_email": "待驗證信箱",
    "status.pending_approval": "待審核",
    "status.active": "已啟用",
    "status.rejected": "已拒絕",
    "status.suspended": "已停權",

    # --- booking status labels ---------------------------------------------
    "booking_status.confirmed": "已確認",
    "booking_status.cancelled_by_user": "已由本人取消",
    "booking_status.cancelled_by_admin": "已由管理員取消",
    "booking_status.preempted": "已被較高等級覆蓋",
}
