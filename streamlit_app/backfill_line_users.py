# v1.0
# 一次性回填工具：把 line_messages 現有歷史資料，回填成 line_users 記錄
# 用途：Worker（在 line-lemonsystem 舊系統）補上 upsert 邏輯後，新訊息會自動建檔，
#       但「過去已經傳過訊息」的 8,855 筆歷史資料不會自動補上，需要手動跑一次這支回填腳本
#
# 這支屬於「新系統 lemonline-system」的臨時工具，只需成功執行一次。
#
# 使用方式：
#   1. 放到 lemonline-system repo 底下，例如 streamlit_app/tools/backfill_line_users.py
#   2. 本機執行（需要能連到 Turso，.streamlit/secrets.toml 要有 TURSO_DATABASE_URL /
#      TURSO_AUTH_TOKEN，可直接複製 lemonline-system 既有的 secrets.toml）：
#
#        cd lemonline-system
#        streamlit run streamlit_app/tools/backfill_line_users.py
#
#   3. 瀏覽器會開一個獨立頁面，點「開始回填」執行
#   4. 執行成功、確認 line_users 筆數符合預期後，把這個檔案整個刪掉即可，
#      不需要留在正式系統中（重複執行雖然安全，但沒有留著的必要）

import streamlit as st
from libsql_client import create_client_sync


def get_db_client():
    """
    沿用專案既有的連線方式。
    若 lemonline-system 已有共用的 get_client() / get_session()，
    請直接改用該函式，這裡提供獨立可執行版本作為備援。
    """
    return create_client_sync(
        url=st.secrets["TURSO_DATABASE_URL"],
        auth_token=st.secrets["TURSO_AUTH_TOKEN"],
    )


def backfill_line_users(db):
    """
    從 line_messages 依 line_user_id 分組，回填 line_users。
    - first_seen_at：該用戶最早一筆 received_at
    - last_seen_at：該用戶最晚一筆 received_at
    - message_count：該用戶總訊息數
    - display_name：取最新一筆訊息的 display_name（最新暱稱）

    採 upsert：若 line_users 已有該用戶（例如 Worker 已經開始正常運作後新建的），
    則不覆蓋，只補上原本缺漏的統計欄位；避免蓋掉客服已編輯的 edited_name / note 等資料。
    """
    # 1. 從 line_messages 算出每個用戶的統計資料
    agg_result = db.execute("""
        SELECT
            line_user_id,
            MIN(received_at) AS first_seen_at,
            MAX(received_at) AS last_seen_at,
            COUNT(*) AS message_count
        FROM line_messages
        WHERE line_user_id IS NOT NULL AND line_user_id != ''
        GROUP BY line_user_id
    """)

    # 2. 取得每個用戶「最新一筆」的 display_name
    latest_name_result = db.execute("""
        SELECT lm.line_user_id, lm.display_name
        FROM line_messages lm
        INNER JOIN (
            SELECT line_user_id, MAX(id) AS max_id
            FROM line_messages
            WHERE line_user_id IS NOT NULL AND line_user_id != ''
            GROUP BY line_user_id
        ) latest ON lm.line_user_id = latest.line_user_id AND lm.id = latest.max_id
    """)
    latest_name_map = {row[0]: row[1] for row in latest_name_result.rows}

    # 3. 已存在於 line_users 的用戶（避免覆蓋客服已編輯的資料）
    existing_result = db.execute("SELECT line_user_id FROM line_users")
    existing_ids = {row[0] for row in existing_result.rows}

    inserted = 0
    updated_stats_only = 0

    for row in agg_result.rows:
        user_id, first_seen, last_seen, msg_count = row[0], row[1], row[2], row[3]
        display_name = latest_name_map.get(user_id)

        if user_id in existing_ids:
            # 已存在：只補統計欄位，不動 edited_name / note / phone / area 等客服編輯欄位
            db.execute(
                """
                UPDATE line_users
                SET first_seen_at = ?,
                    last_seen_at = ?,
                    message_count = ?
                WHERE line_user_id = ?
                """,
                [first_seen, last_seen, msg_count, user_id],
            )
            updated_stats_only += 1
        else:
            # 全新用戶：完整建檔
            db.execute(
                """
                INSERT INTO line_users
                (line_user_id, display_name, first_seen_at, last_seen_at, message_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                [user_id, display_name, first_seen, last_seen, msg_count],
            )
            inserted += 1

    return inserted, updated_stats_only


def render_backfill_page():
    st.title("用戶主檔回填工具")
    st.markdown(
        "把 `line_messages` 現有的歷史訊息資料，回填成 `line_users` 客戶主檔。\n\n"
        "適用時機：Worker 補上自動建檔邏輯後，過去已經傳過訊息、但尚未有主檔的客戶，"
        "用這個工具一次補齊。**可重複執行，不會產生重複資料**，"
        "已編輯過的客戶資料（改名、備註、標籤等）不會被覆蓋。"
    )

    if st.button("開始回填", type="primary"):
        db = get_db_client()
        with st.spinner("回填中，請稍候..."):
            inserted, updated_stats_only = backfill_line_users(db)
        st.success(
            f"回填完成！新建立 {inserted} 位客戶主檔，"
            f"{updated_stats_only} 位既有客戶已同步最新互動統計。"
        )


if __name__ == "__main__":
    render_backfill_page()
