-- migration_001_add_users_and_tags.sql
-- 只新增,不修改/不刪除既有的 line_messages 表

-- 用戶主檔:改名 + 標籤都存這裡,以 line_user_id 為 key
CREATE TABLE IF NOT EXISTS line_users (
    line_user_id   TEXT PRIMARY KEY,
    display_name   TEXT,              -- LINE 原始顯示名稱(最後一次同步到的)
    edited_name    TEXT,              -- 客服自訂備註名稱
    picture_url    TEXT,
    status_message TEXT,
    first_seen_at  TEXT,
    last_seen_at   TEXT,
    message_count  INTEGER DEFAULT 0,
    note           TEXT               -- 客服備註事項
);

-- 標籤主檔(id 為 integer 自動遞增,對齊既有表結構)
CREATE TABLE IF NOT EXISTS tags (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    color       TEXT DEFAULT '#06C755',
    description TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- 用戶 <-> 標籤 多對多
CREATE TABLE IF NOT EXISTS user_tags (
    line_user_id TEXT NOT NULL,
    tag_id       TEXT NOT NULL,
    tagged_at    TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (line_user_id, tag_id),
    FOREIGN KEY (line_user_id) REFERENCES line_users(line_user_id),
    FOREIGN KEY (tag_id) REFERENCES tags(id)
);

-- 訊息狀態(未回覆/已回覆/處理中...),用 message id 對應,不動原表結構
CREATE TABLE IF NOT EXISTS message_status (
    message_id TEXT PRIMARY KEY,
    status     TEXT DEFAULT 'unreplied',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_user_tags_user ON user_tags(line_user_id);
CREATE INDEX IF NOT EXISTS idx_user_tags_tag ON user_tags(tag_id);
