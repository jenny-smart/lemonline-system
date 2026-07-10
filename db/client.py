"""
db/client.py
Turso 連線與資料存取層。

⚠️ 改版說明:
   原本用 libsql_client 套件,但它跟 Streamlit Cloud 目前預設的 Python 3.14
   不相容(WebSocket 握手失敗 / HTTP 回應解析 KeyError 都是這套件的 bug)。
   而 Streamlit Cloud 的 Python 版本只能在第一次部署時選,runtime.txt 目前
   會被平台忽略,所以與其等套件修復或砍掉重建 app,不如直接改用最基本的
   requests 呼叫 Turso 的 HTTP Pipeline API,完全不依賴 aiohttp/asyncio,
   跟 Python 版本無關,穩定很多。

環境變數:
  TURSO_DATABASE_URL   例如 https://line-lemon-db-jenny-smart.turso.io
                       (libsql:// 開頭也可以,程式會自動轉成 https://)
  TURSO_AUTH_TOKEN

安裝: pip install requests

⚠️ 注意:MESSAGES_TABLE 底下的欄位名稱是「假設值」,
   請對照你 line_messages 的實際結構修改。
"""

import os
import uuid
from datetime import datetime, timezone

import requests

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


class ResultSet:
    def __init__(self, columns, rows):
        self.columns = columns
        self.rows = rows


class TursoHTTPClient:
    """用 Turso HTTP Pipeline API (/v2/pipeline) 取代 libsql_client。"""

    def __init__(self, url, token):
        # libsql:// 或 http:// 開頭都轉成 https://
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        elif url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        self.base_url = url.rstrip("/")
        self.token = token

    @staticmethod
    def _to_turso_value(v):
        if v is None:
            return {"type": "null"}
        if isinstance(v, bool):
            return {"type": "integer", "value": "1" if v else "0"}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": v}
        return {"type": "text", "value": str(v)}

    @staticmethod
    def _from_turso_value(v):
        if v is None:
            return None
        t = v.get("type")
        val = v.get("value")
        if t == "null":
            return None
        if t == "integer":
            return int(val)
        if t == "float":
            return float(val)
        return val  # text / blob(base64 字串) 原樣回傳

    def execute(self, sql, args=None):
        args = args or []
        payload = {
            "requests": [
                {
                    "type": "execute",
                    "stmt": {
                        "sql": sql,
                        "args": [self._to_turso_value(a) for a in args],
                    },
                },
                {"type": "close"},
            ]
        }
        resp = requests.post(
            f"{self.base_url}/v2/pipeline",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        result_item = data["results"][0]
        if result_item.get("type") == "error":
            err = result_item.get("error", {})
            msg = err.get("message", err)
            raise RuntimeError(f"Turso error: {msg}\nSQL: {sql}")

        result = result_item["response"]["result"]
        columns = [c["name"] for c in result.get("cols", [])]
        rows = [
            [self._from_turso_value(cell) for cell in row]
            for row in result.get("rows", [])
        ]
        return ResultSet(columns, rows)


def get_client():
    url = os.environ["TURSO_DATABASE_URL"]
    token = os.environ["TURSO_AUTH_TOKEN"]
    return TursoHTTPClient(url, token)


def _now():
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(client):
    """執行 migration,只會新增表,不動 line_messages"""
    with open(os.path.join(os.path.dirname(__file__), "migration_001_add_users_and_tags.sql")) as f:
        sql = f.read()
    for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
        client.execute(stmt)

    # 既有的表可能欄位不齊(CREATE TABLE IF NOT EXISTS 遇到同名舊表不會補欄位),
    # 逐一嘗試把缺的欄位補上。欄位已存在時 ALTER 會噴錯,忽略即可。
    alters = [
        "ALTER TABLE line_users ADD COLUMN note TEXT",
        "ALTER TABLE line_users ADD COLUMN edited_name TEXT",
        "ALTER TABLE line_users ADD COLUMN picture_url TEXT",
        "ALTER TABLE line_users ADD COLUMN status_message TEXT",
        "ALTER TABLE line_users ADD COLUMN first_seen_at TEXT",
        "ALTER TABLE line_users ADD COLUMN last_seen_at TEXT",
        "ALTER TABLE line_users ADD COLUMN message_count INTEGER DEFAULT 0",
        "ALTER TABLE tags ADD COLUMN color TEXT DEFAULT '#06C755'",
        "ALTER TABLE tags ADD COLUMN description TEXT",
        "ALTER TABLE tags ADD COLUMN created_at TEXT",
    ]
    for a in alters:
        try:
            client.execute(a)
        except Exception:
            pass


def describe_table(client, table):
    """回傳某張表的欄位定義,用於診斷結構不符的問題。"""
    try:
        rs = client.execute(f"PRAGMA table_info({table})")
        return [dict(zip(rs.columns, row)) for row in rs.rows]
    except Exception as e:
        return [{"error": str(e)}]


def list_tables(client):
    rs = client.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [row[0] for row in rs.rows]


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


def sync_users_from_messages(client):
    """
    從 line_messages 一次補齊 line_users:
    凡是在訊息表出現過、但還沒進 line_users 的用戶,用其原始名稱 + line id 建進來,
    並回填首次/最後互動時間與訊息數。已存在的用戶只更新原始名稱與統計,不動 edited_name/標籤/備註。

    這讓「訊息一進來,用戶就出現在用戶列表」不必依賴 Cloudflare Worker,
    每次開啟後台時自動對齊。
    """
    uid = COL["user_id"]
    name = COL["display_name"]
    ts = COL["received_at"]

    # 以每個 user 為單位,算出原始名稱(取最新一筆的名稱)、首次/最後互動、訊息數
    rs = client.execute(
        f"""
        SELECT
            m.{uid} AS user_id,
            MIN(m.{ts}) AS first_seen,
            MAX(m.{ts}) AS last_seen,
            COUNT(*) AS cnt
        FROM {MESSAGES_TABLE} m
        WHERE m.{uid} IS NOT NULL AND m.{uid} != ''
        GROUP BY m.{uid}
        """
    )
    agg = {row[0]: {"first": row[1], "last": row[2], "cnt": row[3]} for row in rs.rows}
    if not agg:
        return 0

    # 每個 user 最新一筆訊息的原始名稱
    name_rs = client.execute(
        f"""
        SELECT m.{uid}, m.{name}
        FROM {MESSAGES_TABLE} m
        JOIN (
            SELECT {uid} AS u, MAX({ts}) AS mx
            FROM {MESSAGES_TABLE}
            WHERE {uid} IS NOT NULL AND {uid} != ''
            GROUP BY {uid}
        ) latest ON m.{uid} = latest.u AND m.{ts} = latest.mx
        """
    )
    latest_name = {row[0]: row[1] for row in name_rs.rows}

    # 已存在的用戶
    existing_rs = client.execute("SELECT line_user_id FROM line_users")
    existing_ids = {row[0] for row in existing_rs.rows}

    inserted = 0
    for user_id, stat in agg.items():
        disp = latest_name.get(user_id) or "未知用戶"
        if user_id in existing_ids:
            # 已存在:只更新原始名稱與統計,保留後台編輯過的資料
            client.execute(
                "UPDATE line_users SET display_name = ?, first_seen_at = ?, "
                "last_seen_at = ?, message_count = ? WHERE line_user_id = ?",
                [disp, stat["first"], stat["last"], stat["cnt"], user_id],
            )
        else:
            client.execute(
                "INSERT INTO line_users (line_user_id, display_name, first_seen_at, last_seen_at, message_count) "
                "VALUES (?, ?, ?, ?, ?)",
                [user_id, disp, stat["first"], stat["last"], stat["cnt"]],
            )
            inserted += 1

    return inserted


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
