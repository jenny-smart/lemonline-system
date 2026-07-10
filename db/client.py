"""
db/client.py
Turso (libSQL) 連線與資料存取層。

環境變數:
  TURSO_DATABASE_URL   例如 libsql://line-lemon-db-jenny-smart.turso.io
  TURSO_AUTH_TOKEN

安裝: pip install libsql-client

⚠️ 注意:MESSAGES_TABLE 底下的欄位名稱是「假設值」,
   因為我沒辦法直接讀取你 Turso 裡 line_messages 的實際結構。
   部署前請對照你現有的表,把下面 COLUMN MAP 改成正確欄位名稱。
"""

import os
import uuid
from datetime import datetime, timezone

import libsql_client

# ------------------------------------------------------------------
# 現有 line_messages 表的欄位對應 —— 請依實際結構修改
# ------------------------------------------------------------------
MESSAGES_TABLE = "line_messages"
COL = {
    "id": "id",
    "user_id": "line_user_id",
    "display_name": "display_name",
    "content": "message_text",
    "message_type": "message_type",
    "received_at": "received_at",
}


def get_client():
    url = os.environ["TURSO_DATABASE_URL"]
    token = os.environ["TURSO_AUTH_TOKEN"]
    return libsql_client.create_client_sync(url=url, auth_token=token)


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(client):
    """執行 migration,只會新增表,不動 line_messages"""
    with open(os.path.join(os.path.dirname(__file__), "migration_001_add_users_and_tags.sql")) as f:
        sql = f.read()
    for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
        client.execute(stmt)

    # 舊表可能沒有 note 欄位(CREATE TABLE IF NOT EXISTS 不會補欄位),補一次
    try:
        client.execute("ALTER TABLE line_users ADD COLUMN note TEXT")
    except Exception:
        pass  # 欄位已存在時會噴錯,忽略即可


# ------------------------------------------------------------------
# 訊息
# ------------------------------------------------------------------
def get_messages(client, keyword=None, tag_id=None, status=None, user_id=None,
                  start_date=None, end_date=None, page=1, page_size=30):
    where = []
    args = []

    if user_id:
        where.append(f"m.{COL['user_id']} = ?")
        args.append(user_id)
    if keyword:
        where.append(f"(m.{COL['content']} LIKE ? OR u.edited_name LIKE ? OR m.{COL['display_name']} LIKE ?)")
        args += [f"%{keyword}%"] * 3
    if start_date:
        where.append(f"m.{COL['received_at']} >= ?")
        args.append(start_date)
    if end_date:
        where.append(f"m.{COL['received_at']} <= ?")
        args.append(end_date)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT
            m.{COL['id']}            AS id,
            m.{COL['user_id']}       AS user_id,
            m.{COL['display_name']}  AS original_name,
            u.edited_name            AS edited_name,
            u.note                   AS note,
            m.{COL['content']}       AS content,
            m.{COL['message_type']}  AS message_type,
            m.{COL['received_at']}   AS received_at,
            COALESCE(s.status, 'unreplied') AS status
        FROM {MESSAGES_TABLE} m
        LEFT JOIN line_users u ON u.line_user_id = m.{COL['user_id']}
        LEFT JOIN message_status s ON s.message_id = m.{COL['id']}
        {where_sql}
        ORDER BY m.{COL['received_at']} DESC
        LIMIT ? OFFSET ?
    """
    args_paged = args + [page_size, (page - 1) * page_size]
    rs = client.execute(sql, args_paged)

    messages = [dict(zip(rs.columns, row)) for row in rs.rows]

    # 補標籤(逐用戶查,量大時可再優化成一次 IN 查詢)
    for m in messages:
        m["tags"] = get_user_tags(client, m["user_id"])

    # 套用標籤篩選(在應用層過濾,因為標籤是多對多)
    if tag_id and tag_id != "all":
        messages = [m for m in messages if any(t["id"] == tag_id for t in m["tags"])]
    if status and status != "all":
        messages = [m for m in messages if m["status"] == status]

    count_sql = f"SELECT COUNT(*) FROM {MESSAGES_TABLE} m LEFT JOIN line_users u ON u.line_user_id = m.{COL['user_id']} {where_sql}"
    total = client.execute(count_sql, args).rows[0][0]

    return messages, total


def update_message_status(client, message_id, status):
    client.execute(
        "INSERT INTO message_status (message_id, status, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(message_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
        [message_id, status, _now()],
    )


# ------------------------------------------------------------------
# 用戶(改名 / 標籤 —— 核心功能)
# ------------------------------------------------------------------
def upsert_user_seen(client, user_id, display_name):
    """訊息進來時呼叫,確保 line_users 有這個用戶,並更新原始名稱/最後互動時間"""
    now = _now()
    existing = client.execute("SELECT line_user_id FROM line_users WHERE line_user_id = ?", [user_id]).rows
    if existing:
        client.execute(
            "UPDATE line_users SET display_name = ?, last_seen_at = ?, "
            "message_count = message_count + 1 WHERE line_user_id = ?",
            [display_name, now, user_id],
        )
    else:
        client.execute(
            "INSERT INTO line_users (line_user_id, display_name, first_seen_at, last_seen_at, message_count) "
            "VALUES (?, ?, ?, ?, 1)",
            [user_id, display_name, now, now],
        )


def get_users(client, keyword=None, tag_id=None):
    where = []
    args = []
    if keyword:
        where.append("(display_name LIKE ? OR edited_name LIKE ? OR line_user_id LIKE ?)")
        args += [f"%{keyword}%"] * 3
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"SELECT * FROM line_users {where_sql} ORDER BY last_seen_at DESC"
    rs = client.execute(sql, args)
    users = [dict(zip(rs.columns, row)) for row in rs.rows]

    for u in users:
        u["tags"] = get_user_tags(client, u["line_user_id"])

    if tag_id and tag_id != "all":
        users = [u for u in users if any(t["id"] == tag_id for t in u["tags"])]

    return users


def update_user_name(client, user_id, edited_name):
    """改用戶名稱 —— 核心功能"""
    client.execute(
        "UPDATE line_users SET edited_name = ? WHERE line_user_id = ?",
        [edited_name, user_id],
    )


def update_user_note(client, user_id, note):
    """更新用戶備註事項"""
    client.execute(
        "UPDATE line_users SET note = ? WHERE line_user_id = ?",
        [note, user_id],
    )


def get_user_tags(client, user_id):
    rs = client.execute(
        "SELECT t.id, t.name, t.color FROM tags t "
        "JOIN user_tags ut ON ut.tag_id = t.id WHERE ut.line_user_id = ?",
        [user_id],
    )
    return [dict(zip(rs.columns, row)) for row in rs.rows]


def set_user_tags(client, user_id, tag_ids):
    """下標籤 —— 核心功能。整批覆蓋該用戶的標籤"""
    client.execute("DELETE FROM user_tags WHERE line_user_id = ?", [user_id])
    for tag_id in tag_ids:
        client.execute(
            "INSERT OR IGNORE INTO user_tags (line_user_id, tag_id) VALUES (?, ?)",
            [user_id, tag_id],
        )


# ------------------------------------------------------------------
# 標籤管理
# ------------------------------------------------------------------
def get_all_tags(client):
    rs = client.execute("SELECT * FROM tags ORDER BY name")
    return [dict(zip(rs.columns, row)) for row in rs.rows]


def create_tag(client, name, color, description=""):
    tag_id = "tag_" + uuid.uuid4().hex[:8]
    client.execute(
        "INSERT INTO tags (id, name, color, description) VALUES (?, ?, ?, ?)",
        [tag_id, name, color, description],
    )
    return tag_id


def update_tag(client, tag_id, name, color, description):
    client.execute(
        "UPDATE tags SET name = ?, color = ?, description = ? WHERE id = ?",
        [name, color, description, tag_id],
    )


def delete_tag(client, tag_id):
    client.execute("DELETE FROM user_tags WHERE tag_id = ?", [tag_id])
    client.execute("DELETE FROM tags WHERE id = ?", [tag_id])


# ------------------------------------------------------------------
# 儀表板統計
# ------------------------------------------------------------------
def get_stats(client):
    total_messages = client.execute(f"SELECT COUNT(*) FROM {MESSAGES_TABLE}").rows[0][0]
    total_users = client.execute("SELECT COUNT(*) FROM line_users").rows[0][0]
    unreplied = client.execute(
        "SELECT COUNT(*) FROM message_status WHERE status = 'unreplied'"
    ).rows[0][0]
    total_tags = client.execute("SELECT COUNT(*) FROM tags").rows[0][0]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_messages = client.execute(
        f"SELECT COUNT(*) FROM {MESSAGES_TABLE} WHERE {COL['received_at']} LIKE ?",
        [f"{today}%"],
    ).rows[0][0]

    return {
        "total_messages": total_messages,
        "today_messages": today_messages,
        "unreplied_messages": unreplied,
        "total_users": total_users,
        "total_tags": total_tags,
    }


def get_message_trend(client, days=7):
    rs = client.execute(
        f"SELECT substr({COL['received_at']}, 1, 10) AS d, COUNT(*) "
        f"FROM {MESSAGES_TABLE} GROUP BY d ORDER BY d DESC LIMIT ?",
        [days],
    )
    return list(reversed([{"date": row[0], "count": row[1]} for row in rs.rows]))
