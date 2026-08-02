import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from game_data import EXPORT_SECTIONS, item_name


APP_TZ = ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "coc-reminder.db")))
CHECK_INTERVAL = max(5, int(os.getenv("CHECK_INTERVAL_SECONDS", "30")))
API_KEY = os.getenv("API_KEY", "").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
AUTH_DISABLED = os.getenv("DISABLE_AUTH", "0") == "1"


def load_session_secret():
    configured = os.getenv("SESSION_SECRET", "").strip()
    if configured:
        return configured
    secret_path = Path(
        os.getenv("SESSION_SECRET_FILE", str(DB_PATH.parent / ".session-secret"))
    )
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    if secret_path.exists():
        saved = secret_path.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    generated = secrets.token_urlsafe(48)
    secret_path.write_text(generated, encoding="utf-8")
    secret_path.chmod(0o600)
    return generated


app = Flask(__name__)
app.json.ensure_ascii = False
app.secret_key = load_session_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


def now_utc():
    return datetime.now(timezone.utc)


def iso_utc(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=APP_TZ)
    return dt.astimezone(timezone.utc)


def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS villages (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              player_tag TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS upgrades (
              id TEXT PRIMARY KEY,
              village_id TEXT NOT NULL,
              name TEXT NOT NULL,
              category TEXT NOT NULL DEFAULT '其他',
              level_from INTEGER,
              level_to INTEGER,
              started_at TEXT,
              ends_at TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'upgrading',
              notified_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(village_id) REFERENCES villages(id)
            );
            CREATE INDEX IF NOT EXISTS idx_upgrades_due
              ON upgrades(status, notified_at, ends_at);
            CREATE TABLE IF NOT EXISTS settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )


def require_access():
    if AUTH_DISABLED or session.get("authenticated"):
        return None
    supplied = request.headers.get("X-API-Key") or request.args.get("api_key", "")
    if API_KEY and secrets.compare_digest(supplied, API_KEY):
        return None
    return jsonify({"ok": False, "error": "请先登录后台或提供有效的 API Key"}), 401


def row_to_upgrade(row):
    data = dict(row)
    end = parse_time(data["ends_at"])
    data["remaining_seconds"] = max(0, int((end - now_utc()).total_seconds()))
    data["level_text"] = (
        f"Lv{data['level_from']}→{data['level_to']}"
        if data["level_from"] is not None and data["level_to"] is not None
        else ""
    )
    return data


def normalize_import(body):
    """Accept our documented shape or the game's raw village export shape."""
    if isinstance(body.get("village"), dict) and isinstance(body.get("upgrades"), list):
        return body, "standard"
    if not body.get("tag") or not isinstance(body.get("timestamp"), (int, float)):
        raise ValueError("无法识别 JSON：需要游戏导出的 tag、timestamp 和升级数组")

    source_time = datetime.fromtimestamp(body["timestamp"], tz=timezone.utc)
    tag = str(body["tag"])
    upgrades = []
    occurrences = {}
    for section, category in EXPORT_SECTIONS.items():
        entries = body.get(section, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("timer") is None:
                continue
            data_id = int(entry.get("data"))
            remaining = int(entry["timer"])
            if remaining < 0:
                continue
            occurrence_key = (section, data_id)
            occurrence = occurrences.get(occurrence_key, 0)
            occurrences[occurrence_key] = occurrence + 1
            level = entry.get("lvl")
            upgrades.append(
                {
                    "id": f"{tag}:{section}:{data_id}:{occurrence}",
                    "name": item_name(data_id, category),
                    "category": category,
                    "level_from": level,
                    "level_to": int(level) + 1 if level is not None else None,
                    "started_at": iso_utc(source_time),
                    "ends_at": iso_utc(source_time + timedelta(seconds=remaining)),
                }
            )
    return {
        "village": {
            "id": tag,
            "name": str(body.get("name") or f"村庄 {tag}"),
            "player_tag": tag,
        },
        "upgrades": upgrades,
    }, "game_export"


class WeComNotifier:
    def __init__(self):
        self.corp_id = ""
        self.agent_id = ""
        self.secret = ""
        self.to_user = "@all"
        self.base_url = "https://qyapi.weixin.qq.com"
        self.proxy_url = ""
        self.proxies = None
        self._token = None
        self._token_expires = 0

    def refresh(self):
        defaults = {
            "wecom_corp_id": os.getenv("WECOM_CORP_ID", "").strip(),
            "wecom_agent_id": os.getenv("WECOM_AGENT_ID", "").strip(),
            "wecom_secret": os.getenv("WECOM_SECRET", "").strip(),
            "wecom_to_user": os.getenv("WECOM_TO_USER", "@all").strip() or "@all",
            "wecom_api_base": os.getenv(
                "WECOM_API_BASE", "https://qyapi.weixin.qq.com"
            ).strip(),
            "outbound_proxy": os.getenv("OUTBOUND_PROXY", "").strip(),
        }
        with get_db() as conn:
            stored = {
                row["key"]: row["value"]
                for row in conn.execute(
                    "SELECT key, value FROM settings WHERE key LIKE 'wecom_%' "
                    "OR key = 'outbound_proxy'"
                )
            }
        config = {**defaults, **stored}
        self.corp_id = config["wecom_corp_id"]
        self.agent_id = config["wecom_agent_id"]
        self.secret = config["wecom_secret"]
        self.to_user = config["wecom_to_user"] or "@all"
        self.base_url = (
            config["wecom_api_base"] or "https://qyapi.weixin.qq.com"
        ).rstrip("/")
        self.proxy_url = config["outbound_proxy"]
        self.proxies = (
            {"http": self.proxy_url, "https": self.proxy_url}
            if self.proxy_url
            else None
        )
        self._token = None
        self._token_expires = 0

    @property
    def configured(self):
        return bool(self.corp_id and self.agent_id and self.secret)

    def token(self):
        if self._token and time.time() < self._token_expires:
            return self._token
        response = requests.get(
            f"{self.base_url}/cgi-bin/gettoken",
            params={"corpid": self.corp_id, "corpsecret": self.secret},
            proxies=self.proxies,
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errcode") != 0:
            raise RuntimeError(f"获取企业微信 token 失败：{payload}")
        self._token = payload["access_token"]
        self._token_expires = time.time() + int(payload.get("expires_in", 7200)) - 300
        return self._token

    def send_text(self, content):
        if not self.configured:
            raise RuntimeError("尚未配置企业微信 WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_SECRET")
        payload = {
            "touser": self.to_user,
            "msgtype": "text",
            "agentid": int(self.agent_id),
            "text": {"content": content},
            "safe": 0,
            "enable_duplicate_check": 1,
            "duplicate_check_interval": 1800,
        }
        response = requests.post(
            f"{self.base_url}/cgi-bin/message/send",
            params={"access_token": self.token()},
            json=payload,
            proxies=self.proxies,
            timeout=15,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("errcode") != 0:
            raise RuntimeError(f"企业微信发送失败：{result}")
        return result


notifier = WeComNotifier()


def completion_message(upgrade):
    finished = parse_time(upgrade["ends_at"]).astimezone(APP_TZ).strftime("%Y-%m-%d %H:%M")
    level = (
        f" Lv{upgrade['level_from']}→{upgrade['level_to']}"
        if upgrade["level_from"] is not None and upgrade["level_to"] is not None
        else ""
    )
    tag = f"（{upgrade['player_tag']}）" if upgrade["player_tag"] else ""
    return (
        "✅ 部落冲突升级完成\n"
        f"村庄：{upgrade['village_name']}{tag}\n"
        f"项目：{upgrade['name']}{level}\n"
        f"分类：{upgrade['category']}\n"
        f"完成时间：{finished}"
    )


def process_due_upgrades():
    if not notifier.configured:
        return 0
    current = iso_utc(now_utc())
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.*, v.name AS village_name, v.player_tag
            FROM upgrades u JOIN villages v ON v.id = u.village_id
            WHERE u.status = 'upgrading' AND u.notified_at IS NULL AND u.ends_at <= ?
            ORDER BY u.ends_at
            """,
            (current,),
        ).fetchall()
    sent = 0
    for row in rows:
        try:
            notifier.send_text(completion_message(row))
            with get_db() as conn:
                conn.execute(
                    "UPDATE upgrades SET status='completed', notified_at=?, updated_at=? "
                    "WHERE id=? AND notified_at IS NULL",
                    (current, current, row["id"]),
                )
            sent += 1
        except Exception:
            app.logger.exception("发送升级完成通知失败：%s", row["id"])
    return sent


def scheduler_loop():
    while True:
        try:
            process_due_upgrades()
        except Exception:
            app.logger.exception("提醒轮询失败")
        time.sleep(CHECK_INTERVAL)


@app.get("/")
def index():
    if not AUTH_DISABLED and not session.get("authenticated"):
        return redirect(url_for("login"))
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if AUTH_DISABLED or session.get("authenticated"):
        return redirect(url_for("index"))
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        valid_user = secrets.compare_digest(username, ADMIN_USERNAME)
        valid_password = secrets.compare_digest(password, ADMIN_PASSWORD)
        if valid_user and valid_password:
            session.clear()
            session["authenticated"] = True
            session["username"] = ADMIN_USERNAME
            session.permanent = True
            return redirect(url_for("index"))
        error = "账号或密码错误"
    return render_template("login.html", error=error)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "time": iso_utc(now_utc()),
            "wecom_configured": notifier.configured,
        }
    )


@app.get("/api/v1/villages")
def list_villages():
    auth = require_access()
    if auth:
        return auth
    with get_db() as conn:
        villages = [dict(row) for row in conn.execute("SELECT * FROM villages ORDER BY name")]
        for village in villages:
            rows = conn.execute(
                "SELECT * FROM upgrades WHERE village_id=? ORDER BY status DESC, ends_at",
                (village["id"],),
            ).fetchall()
            village["upgrades"] = [row_to_upgrade(row) for row in rows]
    return jsonify({"ok": True, "villages": villages})


@app.get("/api/v1/settings/wecom")
def get_wecom_settings():
    auth = require_access()
    if auth:
        return auth
    return jsonify(
        {
            "ok": True,
            "settings": {
                "corp_id": notifier.corp_id,
                "agent_id": notifier.agent_id,
                "secret": "••••••••" if notifier.secret else "",
                "secret_set": bool(notifier.secret),
                "to_user": notifier.to_user,
                "api_base": notifier.base_url,
                "outbound_proxy": notifier.proxy_url,
                "configured": notifier.configured,
            },
        }
    )


@app.put("/api/v1/settings/wecom")
def update_wecom_settings():
    auth = require_access()
    if auth:
        return auth
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400

    corp_id = str(body.get("corp_id", "")).strip()
    agent_id = str(body.get("agent_id", "")).strip()
    secret = str(body.get("secret", "")).strip()
    to_user = str(body.get("to_user", "@all")).strip() or "@all"
    api_base = str(
        body.get("api_base", "https://qyapi.weixin.qq.com")
    ).strip().rstrip("/")
    outbound_proxy = str(body.get("outbound_proxy", "")).strip()
    if not corp_id:
        return jsonify({"ok": False, "error": "企业 ID 不能为空"}), 400
    if not agent_id.isdigit():
        return jsonify({"ok": False, "error": "AgentID 必须是数字"}), 400
    if not secret and not notifier.secret:
        return jsonify({"ok": False, "error": "应用 Secret 不能为空"}), 400
    if not api_base.startswith(("https://", "http://")):
        return jsonify({"ok": False, "error": "API 地址必须以 http:// 或 https:// 开头"}), 400
    if outbound_proxy and not outbound_proxy.startswith(("https://", "http://")):
        return jsonify({"ok": False, "error": "出站代理必须以 http:// 或 https:// 开头"}), 400

    values = {
        "wecom_corp_id": corp_id,
        "wecom_agent_id": agent_id,
        "wecom_to_user": to_user,
        "wecom_api_base": api_base,
        "outbound_proxy": outbound_proxy,
    }
    if secret:
        values["wecom_secret"] = secret
    updated_at = iso_utc(now_utc())
    with get_db() as conn:
        conn.executemany(
            """
            INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value=excluded.value, updated_at=excluded.updated_at
            """,
            [(key, value, updated_at) for key, value in values.items()],
        )
    notifier.refresh()
    return jsonify({"ok": True, "configured": notifier.configured})


@app.post("/api/v1/import")
def import_village():
    auth = require_access()
    if auth:
        return auth
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
    try:
        body, input_format = normalize_import(body)
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    village = body.get("village") or {}
    upgrades = body.get("upgrades")
    if not isinstance(village, dict) or not village.get("name"):
        return jsonify({"ok": False, "error": "village.name 不能为空"}), 400
    if not isinstance(upgrades, list):
        return jsonify({"ok": False, "error": "upgrades 必须是数组"}), 400

    village_id = str(village.get("id") or village.get("player_tag") or uuid.uuid4())
    imported = 0
    imported_ids = []
    current = now_utc()
    current_iso = iso_utc(current)
    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO villages(id, name, player_tag, updated_at) VALUES(?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name=excluded.name, player_tag=excluded.player_tag, updated_at=excluded.updated_at
                """,
                (village_id, str(village["name"]), str(village.get("player_tag", "")), current_iso),
            )
            for item in upgrades:
                if not isinstance(item, dict) or not item.get("name"):
                    raise ValueError("每个升级项目都必须包含 name")
                end = parse_time(item.get("ends_at"))
                if end is None and item.get("duration_seconds") is not None:
                    end = current + timedelta(seconds=int(item["duration_seconds"]))
                if end is None:
                    raise ValueError(f"{item['name']} 缺少 ends_at 或 duration_seconds")
                start = parse_time(item.get("started_at")) or current
                item_id = str(
                    item.get("id")
                    or uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{village_id}:{item['name']}:{iso_utc(end)}",
                    )
                )
                status = "completed" if end <= current and item.get("completed") else "upgrading"
                conn.execute(
                    """
                    INSERT INTO upgrades(
                      id, village_id, name, category, level_from, level_to,
                      started_at, ends_at, status, notified_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      village_id=excluded.village_id, name=excluded.name,
                      category=excluded.category, level_from=excluded.level_from,
                      level_to=excluded.level_to, started_at=excluded.started_at,
                      ends_at=excluded.ends_at, status=excluded.status,
                      notified_at=CASE
                        WHEN upgrades.ends_at != excluded.ends_at THEN NULL
                        ELSE upgrades.notified_at
                      END,
                      updated_at=excluded.updated_at
                    """,
                    (
                        item_id,
                        village_id,
                        str(item["name"]),
                        str(item.get("category", "其他")),
                        item.get("level_from"),
                        item.get("level_to"),
                        iso_utc(start),
                        iso_utc(end),
                        status,
                        current_iso,
                        current_iso,
                    ),
                )
                imported += 1
                imported_ids.append(item_id)
            if input_format == "game_export":
                # A new game snapshot is authoritative for active timers. If an
                # old raw-export item vanished, make it due now so it is
                # announced once instead of lingering until an outdated ETA.
                parameters = [current_iso, current_iso, village_id, f"{village_id}:%"]
                missing_clause = ""
                if imported_ids:
                    placeholders = ",".join("?" for _ in imported_ids)
                    missing_clause = f" AND id NOT IN ({placeholders})"
                    parameters.extend(imported_ids)
                conn.execute(
                    """
                    UPDATE upgrades
                    SET ends_at = MIN(ends_at, ?), updated_at = ?
                    WHERE village_id = ?
                      AND status = 'upgrading'
                      AND notified_at IS NULL
                      AND id LIKE ?
                    """
                    + missing_clause,
                    parameters,
                )
    except (ValueError, TypeError, OverflowError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    sent = process_due_upgrades()
    return jsonify(
        {
            "ok": True,
            "village_id": village_id,
            "imported": imported,
            "format": input_format,
            "notifications_sent": sent,
        }
    ), 201


@app.delete("/api/v1/upgrades/<upgrade_id>")
def delete_upgrade(upgrade_id):
    auth = require_access()
    if auth:
        return auth
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM upgrades WHERE id=?", (upgrade_id,))
    if not cursor.rowcount:
        return jsonify({"ok": False, "error": "升级项目不存在"}), 404
    return jsonify({"ok": True})


@app.post("/api/v1/notifications/test")
def test_notification():
    auth = require_access()
    if auth:
        return auth
    try:
        result = notifier.send_text(
            "🔔 部落冲突提醒测试\n企业微信通知配置成功。\n"
            f"测试时间：{datetime.now(APP_TZ).strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return jsonify({"ok": True, "result": result})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.post("/api/v1/notifications/check")
def check_notifications():
    auth = require_access()
    if auth:
        return auth
    return jsonify({"ok": True, "sent": process_due_upgrades()})


init_db()
notifier.refresh()
if os.getenv("DISABLE_SCHEDULER", "0") != "1":
    threading.Thread(target=scheduler_loop, name="reminder-scheduler", daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False)
