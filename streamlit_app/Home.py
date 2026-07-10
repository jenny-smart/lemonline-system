import os
import sys
from datetime import datetime

import pandas as pd
import streamlit as st

# db/ 可能在 repo 根目錄(本檔上一層),也可能跟本檔同層,兩層都加進 path 保險
_here = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(_here), _here):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from db import client as db  # noqa: E402

st.set_page_config(page_title="檸檬家事 LINE 管理後台", page_icon="🍋", layout="wide")

# secrets 只需要這兩個
for key in ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
    if key not in os.environ and key in st.secrets:
        os.environ[key] = st.secrets[key]

# 讓分頁籤列(st.tabs)在往下捲動時固定在頂部
st.markdown(
    """
    <style>
    div[data-testid="stTabs"] > div[data-baseweb="tab-list"] {
        position: sticky;
        top: 0;
        z-index: 999;
        background-color: var(--background-color, white);
        padding-top: 6px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_client():
    c = db.get_client()
    db.ensure_schema(c)
    return c


client = get_client()

st.title("🍋 檸檬家事 LINE 管理後台")

tab_home, tab_messages, tab_users, tab_tags = st.tabs(["📊 儀表板", "📨 訊息管理", "👤 用戶管理", "🏷️ 標籤管理"])

STATUS_LABEL = {
    "unreplied": "未回覆", "replied": "已回覆", "processing": "處理中",
    "resolved": "已解決", "closed": "已關閉",
}
STATUS_COLOR = {
    "unreplied": "🟡", "replied": "🔵", "processing": "🟠", "resolved": "🟢", "closed": "⚪",
}


def tag_badges_html(tags):
    if not tags:
        return "<span style='color:#999'>無標籤</span>"
    spans = []
    for t in tags:
        spans.append(
            f"<span style='background:{t['color']};color:white;padding:2px 8px;"
            f"border-radius:10px;font-size:0.75rem;margin-right:4px;'>{t['name']}</span>"
        )
    return "".join(spans)


def edit_user_popover(tags, user_id, current_edited_name, current_tag_ids, current_note, key_prefix, current_status=None):
    """共用的「編輯用戶」彈出視窗:改名稱 + 下標籤(含新增標籤) + 備註(訊息頁另外加狀態切換)。
    回傳 True 表示使用者按了儲存。"""
    new_name = st.text_input("改用戶名稱", value=current_edited_name or "", key=f"{key_prefix}_name")

    selected_tags = st.multiselect(
        "標籤", options=[t["id"] for t in tags], default=current_tag_ids,
        format_func=lambda tid: next((t["name"] for t in tags if t["id"] == tid), tid),
        key=f"{key_prefix}_tags",
    )
    new_tag_input = st.text_input(
        "新增標籤(用逗號分隔,存檔時會自動建立並套用)",
        placeholder="例如: VIP, 台北, 重要客戶",
        key=f"{key_prefix}_newtags",
    )

    new_note = st.text_area("備註事項", value=current_note or "", key=f"{key_prefix}_note", height=80)

    new_status = None
    if current_status is not None:
        new_status = st.selectbox(
            "狀態", list(STATUS_LABEL.keys()),
            index=list(STATUS_LABEL.keys()).index(current_status) if current_status in STATUS_LABEL else 0,
            format_func=lambda s: STATUS_LABEL[s], key=f"{key_prefix}_status",
        )

    if st.button("儲存", key=f"{key_prefix}_save"):
        final_tag_ids = list(selected_tags)
        if new_tag_input.strip():
            existing_names = {t["name"]: t["id"] for t in tags}
            for name in [n.strip() for n in new_tag_input.split(",") if n.strip()]:
                if name in existing_names:
                    final_tag_ids.append(existing_names[name])
                else:
                    new_id = db.create_tag(client, name, "#06C755", "")
                    final_tag_ids.append(new_id)

        db.update_user_name(client, user_id, new_name)
        db.set_user_tags(client, user_id, final_tag_ids)
        db.update_user_note(client, user_id, new_note)
        return new_status
    return "NOCLICK"


# ====================================================================
# 儀表板
# ====================================================================
with tab_home:
    stats = db.get_stats(client)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("總訊息數", stats["total_messages"])
    c2.metric("今日訊息", stats["today_messages"])
    c3.metric("未回覆", stats["unreplied_messages"])
    c4.metric("用戶數", stats["total_users"])
    c5.metric("標籤數", stats["total_tags"])

    st.subheader("近 7 天訊息趨勢")
    trend = db.get_message_trend(client, days=7)
    if trend:
        st.bar_chart(pd.DataFrame(trend).set_index("date"))
    else:
        st.info("目前沒有訊息資料")

    st.caption(f"最後更新: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# ====================================================================
# 訊息管理 —— 表格化版面
# ====================================================================
with tab_messages:
    tags = db.get_all_tags(client)
    tag_options = {"all": "全部標籤"} | {t["id"]: t["name"] for t in tags}

    with st.form("message_filters", border=True):
        r1c1, r1c2, r1c3, r1c4 = st.columns(4)
        status = r1c1.selectbox(
            "狀態篩選", ["all"] + list(STATUS_LABEL.keys()),
            format_func=lambda s: "全部狀態" if s == "all" else STATUS_LABEL[s],
        )
        start_date = r1c2.date_input("日期範圍", value=None)
        end_date = r1c3.date_input("至", value=None)
        keyword = r1c4.text_input("關鍵字搜尋", placeholder="搜尋內容、用戶...")

        r2c1, r2c2, r2c3, r2c4 = st.columns(4)
        user_id_filter = r2c1.text_input("用戶ID")
        tag_filter = r2c2.selectbox("標籤篩選", list(tag_options.keys()), format_func=lambda k: tag_options[k])
        r2c3.write("")
        submit_col1, submit_col2 = r2c4.columns(2)
        clear = submit_col1.form_submit_button("✕ 清除篩選")
        apply_ = submit_col2.form_submit_button("🔽 套用篩選", type="primary")

    if clear:
        st.session_state["msg_page"] = 1
        st.rerun()

    page = st.session_state.get("msg_page", 1)

    messages, total = db.get_messages(
        client,
        keyword=(None if clear else (keyword or None)),
        tag_id=(None if clear else tag_filter),
        status=(None if clear else status),
        user_id=(None if clear else (user_id_filter or None)),
        start_date=(None if clear else (str(start_date) if start_date else None)),
        end_date=(None if clear else (str(end_date) if end_date else None)),
        page=page,
        page_size=30,
    )

    with st.container(border=True):
        h1, h2 = st.columns([1, 1])
        h1.markdown("**訊息列表**")
        h2.markdown(f"<div style='text-align:right'>顯示 {len(messages)} / {total} 條訊息</div>", unsafe_allow_html=True)

        header = st.columns([1.2, 1.2, 1.2, 3, 1.3, 1, 1.5, 1])
        for col, label in zip(header, ["ID", "用戶", "編輯名稱", "訊息內容", "時間", "狀態", "標籤", "操作"]):
            col.markdown(f"**{label}**")
        st.divider()

        if not messages:
            st.info("沒有找到任何訊息數據")

        for m in messages:
            row = st.columns([1.2, 1.2, 1.2, 3, 1.3, 1, 1.5, 1])
            row[0].caption(m["id"])
            row[1].write(m["original_name"] or "未知用戶")
            row[2].write(m["edited_name"] or "—")
            content = m["content"] or "無內容"
            row[3].write(content[:20] + ("…" if len(content) > 20 else ""))
            row[4].caption(m["received_at"])
            row[5].markdown(f"{STATUS_COLOR.get(m['status'],'⚪')} {STATUS_LABEL.get(m['status'], m['status'])}")
            row[6].markdown(tag_badges_html(m["tags"]), unsafe_allow_html=True)

            with row[7].popover("編輯"):
                result_status = edit_user_popover(
                    tags, m["user_id"], m["edited_name"],
                    [t["id"] for t in m["tags"]], m.get("note"),
                    key_prefix=f"m_{m['id']}", current_status=m["status"],
                )
                if result_status != "NOCLICK":
                    if result_status and result_status != m["status"]:
                        db.update_message_status(client, m["id"], result_status)
                    st.success("已更新")
                    st.rerun()
            st.divider()

    pc1, pc2, pc3 = st.columns([1, 1, 6])
    if pc1.button("← 上一頁", disabled=page <= 1):
        st.session_state["msg_page"] = page - 1
        st.rerun()
    pc2.write(f"第 {page} 頁")
    if pc3.button("下一頁 →", disabled=len(messages) < 30):
        st.session_state["msg_page"] = page + 1
        st.rerun()

# ====================================================================
# 用戶管理 —— 表格化版面(原名稱/編輯名稱/LINE ID/標籤/備註)
# ====================================================================
with tab_users:
    tags = db.get_all_tags(client)
    tag_options = {"all": "全部標籤"} | {t["id"]: t["name"] for t in tags}

    # 每次開啟用戶頁時,自動把訊息裡出現過但還沒進用戶表的用戶補進來(用原始名稱+line id)
    # 只在本 session 第一次進來時自動跑,避免每次互動都全表掃描
    if not st.session_state.get("_users_synced"):
        with st.spinner("同步用戶資料中..."):
            db.sync_users_from_messages(client)
        st.session_state["_users_synced"] = True

    c1, c2, c3 = st.columns([3, 1, 1])
    u_keyword = c1.text_input("搜尋用戶名稱 / ID", key="u_kw")
    u_tag_filter = c2.selectbox("標籤篩選", list(tag_options.keys()), format_func=lambda k: tag_options[k], key="u_tag")
    c3.write("")
    if c3.button("🔄 重新同步", help="從訊息記錄重新補齊用戶清單"):
        n = db.sync_users_from_messages(client)
        st.success(f"同步完成，新增 {n} 位用戶")
        st.rerun()

    users = db.get_users(client, keyword=u_keyword or None, tag_id=u_tag_filter)

    with st.container(border=True):
        st.markdown(f"**用戶列表**　共 {len(users)} 位用戶　(訊息一進來即以原始名稱+LINE ID 出現在這裡)")
        header = st.columns([1.6, 1.1, 1.1, 1.3, 1.6, 1, 1, 0.8])
        for col, label in zip(header, ["LINE ID", "原用戶名稱", "編輯後名稱", "標籤", "備註事項", "首次互動", "最後互動", "操作"]):
            col.markdown(f"**{label}**")
        st.divider()

        if not users:
            st.info("沒有找到任何用戶數據")

        for u in users:
            row = st.columns([1.6, 1.1, 1.1, 1.3, 1.6, 1, 1, 0.8])
            row[0].caption(u["line_user_id"])
            row[1].write(u["display_name"] or "未知用戶")
            row[2].write(u["edited_name"] or "—")
            row[3].markdown(tag_badges_html(u["tags"]), unsafe_allow_html=True)
            note_preview = (u.get("note") or "—")
            row[4].write(note_preview[:15] + ("…" if len(note_preview) > 15 else ""))
            row[5].caption(u["first_seen_at"] or "—")
            row[6].caption(u["last_seen_at"] or "—")

            with row[7].popover("編輯"):
                result = edit_user_popover(
                    tags, u["line_user_id"], u["edited_name"],
                    [t["id"] for t in u["tags"]], u.get("note"),
                    key_prefix=f"u_{u['line_user_id']}",
                )
                if result != "NOCLICK":
                    st.success("已更新")
                    st.rerun()
            st.divider()

# ====================================================================
# 標籤管理
# ====================================================================
with tab_tags:
    with st.expander("➕ 新增標籤"):
        c1, c2, c3 = st.columns([2, 1, 3])
        name = c1.text_input("標籤名稱", key="new_tag_name")
        color = c2.color_picker("顏色", value="#06C755", key="new_tag_color")
        desc = c3.text_input("描述(選填)", key="new_tag_desc")
        if st.button("建立標籤", key="create_tag_btn"):
            if name.strip():
                try:
                    db.create_tag(client, name.strip(), color, desc)
                    st.success(f"已建立標籤「{name}」")
                    st.rerun()
                except Exception as e:
                    st.error(f"建立標籤失敗：{e}")
            else:
                st.warning("請輸入標籤名稱")

    st.divider()

    tags = db.get_all_tags(client)
    if not tags:
        st.info("尚未建立任何標籤")

    cols = st.columns(3)
    for i, tag in enumerate(tags):
        with cols[i % 3]:
            with st.container(border=True):
                st.markdown(tag_badges_html([tag]), unsafe_allow_html=True)
                if tag.get("description"):
                    st.caption(tag["description"])
                if st.button("刪除", key=f"del_{tag['id']}"):
                    db.delete_tag(client, tag["id"])
                    st.rerun()

    # ---- 資料庫診斷(排查結構問題用,確認正常後可移除) ----
    with st.expander("🔧 資料庫診斷"):
        st.caption("如果建立標籤/存檔失敗,這裡可看出實際的表結構")
        try:
            st.write("**現有資料表：**", db.list_tables(client))
            for tbl in ("tags", "line_users", "user_tags", "message_status"):
                st.write(f"**{tbl} 欄位：**")
                st.json(db.describe_table(client, tbl))
        except Exception as e:
            st.error(f"診斷查詢失敗：{e}")

