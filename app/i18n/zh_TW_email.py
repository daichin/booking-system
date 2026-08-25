"""zh-TW strings for the email catalogue (spec §9.1).

A fragment merged into the main catalogue by :mod:`app.i18n` (see its
``_load_zh_tw``). Kept separate so Task 2 never edits ``zh_TW.py``, which
another task owns.

Every ``email.<KIND>.body`` string is a small paragraph-separated template
(blank line = new paragraph) rendered by
:mod:`app.services.email_templates` into both an HTML and a text part.
"""

from __future__ import annotations

STRINGS: dict[str, str] = {
    # --- shared ------------------------------------------------------------
    "email.footer": "此為會議室預約系統自動發送之通知信件，請勿直接回覆。",

    # --- E1 email verification ----------------------------------------------
    "email.E1.subject": "請驗證您的電子郵件 - {app_name}",
    "email.E1.body": (
        "{full_name} 您好，\n\n"
        "感謝您註冊{app_name}。請點擊下方連結驗證您的電子郵件地址，"
        "此連結將於 {expires_hours} 小時後失效：\n\n{verify_url}\n\n"
        "驗證完成後，您的申請將送交管理員審核，審核通過後即可開始預約會議室。"
    ),

    # --- E1_EXISTS re-registration on an already-active address -------------
    "email.E1_EXISTS.subject": "您已擁有帳號 - {app_name}",
    "email.E1_EXISTS.body": (
        "您好，\n\n"
        "系統偵測到有人使用這個電子郵件地址嘗試註冊，"
        "但這個地址已經有一個啟用中的帳號。如果這是您本人操作，"
        "請直接前往登入頁面登入；如果忘記密碼，可以使用忘記密碼功能重新設定。\n\n"
        "{login_url}\n\n"
        "如果這不是您本人的操作，請忽略此信件，您的帳號不會受到任何影響。"
    ),

    # --- E2 approved ----------------------------------------------------------
    "email.E2.subject": "您的帳號已通過審核 - {app_name}",
    "email.E2.body": (
        "{full_name} 您好，\n\n"
        "您的帳號已經管理員審核通過，現在即可開始預約會議室。\n\n{login_url}"
    ),

    # --- E3 rejected ------------------------------------------------------
    "email.E3.subject": "您的註冊申請結果 - {app_name}",
    "email.E3.body": (
        "{full_name} 您好，\n\n"
        "很抱歉，您的註冊申請未能通過。如有任何疑問，請聯絡系統管理員。"
    ),

    # --- E4 booking created --------------------------------------------------
    "email.E4.subject": "預約成功通知 - {app_name}",
    "email.E4.body": (
        "{full_name} 您好，\n\n"
        "您已成功預約以下會議室：\n\n"
        "會議室：{room_name}\n主題：{title}\n時間：{time_range}\n\n"
        "如需取消，請至「我的預約」頁面操作：\n\n{cancel_url}"
    ),

    # --- E5 cancellations (three reasons share the wording pattern) --------
    "email.E5_preempted.subject": "您的預約已被取消 - {app_name}",
    "email.E5_preempted.body": (
        "{full_name} 您好，\n\n"
        "很抱歉，您原本預約的以下時段，已被更高優先等級的成員取代：\n\n"
        "會議室：{room_name}\n主題：{title}\n時間：{time_range}\n\n"
        "系統不會自動為您安排其他時段，請重新預約：\n\n{book_url}"
    ),

    "email.E5_admin.subject": "您的預約已被管理員取消 - {app_name}",
    "email.E5_admin.body": (
        "{full_name} 您好，\n\n"
        "管理員已取消您原本預約的以下時段：\n\n"
        "會議室：{room_name}\n主題：{title}\n時間：{time_range}\n\n"
        "如需重新預約，請前往：\n\n{book_url}"
    ),

    "email.E5_room.subject": "會議室已停用 - {app_name}",
    "email.E5_room.body": (
        "{full_name} 您好，\n\n"
        "由於會議室「{room_name}」已停用，您原本預約的以下時段已被取消：\n\n"
        "主題：{title}\n時間：{time_range}\n\n"
        "請改為預約其他會議室：\n\n{book_url}"
    ),

    # --- E6 self-cancellation confirmation -----------------------------------
    "email.E6.subject": "取消預約確認 - {app_name}",
    "email.E6.body": (
        "{full_name} 您好，\n\n"
        "您已成功取消以下預約：\n\n"
        "會議室：{room_name}\n主題：{title}\n時間：{time_range}"
    ),

    # --- E7 admin digest of pending registrations ---------------------------
    "email.E7.subject": "有新的註冊申請待審核 - {app_name}",
    "email.E7.body": (
        "{admin_name} 您好，\n\n"
        "目前有 {count} 筆註冊申請正在等待您的審核：\n\n{pending_list}\n\n"
        "請至管理後台處理：\n\n{admin_url}"
    ),
    "email.E7.pending_item": "・{full_name}（{department}，電話：{phone}，信箱：{email}）",

    # --- E8 invitation --------------------------------------------------------
    "email.E8.subject": "會議室預約系統邀請 - {app_name}",
    "email.E8.body": (
        "您好，\n\n"
        "管理員邀請您加入{app_name}。請點擊下方連結完成註冊，"
        "此連結將於 {expires_hours} 小時後失效：\n\n{invite_url}\n\n"
        "完成註冊後即可立即開始預約會議室，不需等待審核。"
    ),

    # --- E9 password reset -----------------------------------------------------
    "email.E9.subject": "重設密碼 - {app_name}",
    "email.E9.body": (
        "{full_name} 您好，\n\n"
        "我們收到重設您密碼的請求。請點擊下方連結設定新密碼，"
        "此連結將於 {expires_hours} 小時後失效：\n\n{reset_url}\n\n"
        "如果這不是您本人的操作，請忽略此信件，您的密碼不會被變更。"
    ),

    # --- E10 reminder -----------------------------------------------------------
    "email.E10.subject": "會議提醒 - {app_name}",
    "email.E10.body": (
        "{full_name} 您好，\n\n"
        "提醒您即將有以下會議：\n\n"
        "會議室：{room_name}\n主題：{title}\n時間：{time_range}"
    ),
}
