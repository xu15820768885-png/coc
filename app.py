import base64
import hashlib
import json
import os
import secrets
import sqlite3
import struct
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from Crypto.Cipher import AES
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from game_data import EXPORT_SECTIONS, item_name
from werkzeug.middleware.proxy_fix import ProxyFix


APP_TZ = ZoneInfo(os.getenv("TZ", "Asia/Shanghai"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "coc-reminder.db")))
RETRY_INTERVAL = max(
    5,
    int(
        os.getenv(
            "NOTIFICATION_RETRY_SECONDS",
            os.getenv("CHECK_INTERVAL_SECONDS", "30"),
        )
    ),
)
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
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
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
            CREATE TABLE IF NOT EXISTS wecom_messages (
              msg_id TEXT PRIMARY KEY,
              from_user TEXT NOT NULL,
              received_at TEXT NOT NULL
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

    def send_text(self, content, to_user=None):
        if not self.configured:
            raise RuntimeError("尚未配置企业微信 WECOM_CORP_ID / WECOM_AGENT_ID / WECOM_SECRET")
        payload = {
            "touser": to_user or self.to_user,
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

    def download_media(self, media_id):
        response = requests.get(
            f"{self.base_url}/cgi-bin/media/get",
            params={"access_token": self.token(), "media_id": media_id},
            proxies=self.proxies,
            timeout=30,
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            payload = response.json()
            raise RuntimeError(f"下载企业微信文件失败：{payload}")
        return response.content


class WeComCallbackCrypto:
    def __init__(self, token, encoding_aes_key, receive_id):
        if not token or len(token) > 32:
            raise ValueError("回调 Token 必须为不超过 32 位的英文或数字")
        if len(encoding_aes_key) != 43:
            raise ValueError("EncodingAESKey 必须是 43 位")
        try:
            self.key = base64.b64decode(encoding_aes_key + "=", validate=True)
        except Exception as exc:
            raise ValueError("EncodingAESKey 格式无效") from exc
        if len(self.key) != 32:
            raise ValueError("EncodingAESKey 解码后长度无效")
        self.token = token
        self.receive_id = receive_id

    def verify_signature(self, signature, timestamp, nonce, encrypted):
        parts = sorted([self.token, str(timestamp), str(nonce), encrypted])
        expected = hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()
        if not secrets.compare_digest(expected, signature or ""):
            raise ValueError("企业微信回调签名验证失败")

    def decrypt(self, encrypted):
        try:
            ciphertext = base64.b64decode(encrypted)
            padded = AES.new(self.key, AES.MODE_CBC, self.key[:16]).decrypt(ciphertext)
        except Exception as exc:
            raise ValueError("企业微信消息解密失败") from exc
        padding = padded[-1]
        if padding < 1 or padding > 32 or padded[-padding:] != bytes([padding]) * padding:
            raise ValueError("企业微信消息填充无效")
        plain = padded[:-padding]
        if len(plain) < 20:
            raise ValueError("企业微信消息长度无效")
        message_length = struct.unpack(">I", plain[16:20])[0]
        message_end = 20 + message_length
        message = plain[20:message_end]
        receive_id = plain[message_end:].decode("utf-8")
        if self.receive_id and not secrets.compare_digest(receive_id, self.receive_id):
            raise ValueError("企业微信回调 CorpID 不匹配")
        return message.decode("utf-8")


notifier = WeComNotifier()
scheduler_wakeup = threading.Event()


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


def seconds_until_next_upgrade():
    if not notifier.configured:
        return None
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT MIN(ends_at) AS next_ends_at
            FROM upgrades
            WHERE status = 'upgrading' AND notified_at IS NULL
            """
        ).fetchone()
    if not row or not row["next_ends_at"]:
        return None
    delay = (parse_time(row["next_ends_at"]) - now_utc()).total_seconds()
    # Past-due means the previous send failed. Back off before retrying.
    return delay if delay > 0 else RETRY_INTERVAL


def scheduler_loop():
    while True:
        try:
            process_due_upgrades()
            delay = seconds_until_next_upgrade()
        except Exception:
            app.logger.exception("提醒轮询失败")
            delay = RETRY_INTERVAL
        scheduler_wakeup.wait(timeout=delay)
        scheduler_wakeup.clear()


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
    with get_db() as conn:
        callback_values = {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('wecom_callback_token', 'wecom_callback_aes_key')"
            )
        }
    callback_token_set = bool(callback_values.get("wecom_callback_token"))
    callback_aes_key_set = bool(callback_values.get("wecom_callback_aes_key"))
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
                "callback_url": url_for("wecom_callback", _external=True),
                "callback_token": "••••••••" if callback_token_set else "",
                "callback_token_set": callback_token_set,
                "callback_aes_key": "••••••••" if callback_aes_key_set else "",
                "callback_aes_key_set": callback_aes_key_set,
                "callback_configured": callback_token_set and callback_aes_key_set,
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
    callback_token = str(body.get("callback_token", "")).strip()
    callback_aes_key = str(body.get("callback_aes_key", "")).strip()
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
    with get_db() as conn:
        existing_callback = {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('wecom_callback_token', 'wecom_callback_aes_key')"
            )
        }
    effective_token = callback_token or existing_callback.get("wecom_callback_token", "")
    effective_aes_key = callback_aes_key or existing_callback.get(
        "wecom_callback_aes_key", ""
    )
    if bool(effective_token) != bool(effective_aes_key):
        return jsonify({"ok": False, "error": "回调 Token 和 EncodingAESKey 必须同时填写"}), 400
    if effective_token:
        if not effective_token.isalnum() or len(effective_token) > 32:
            return jsonify(
                {"ok": False, "error": "回调 Token 必须为不超过 32 位的英文或数字"}
            ), 400
        try:
            WeComCallbackCrypto(effective_token, effective_aes_key, corp_id)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    values = {
        "wecom_corp_id": corp_id,
        "wecom_agent_id": agent_id,
        "wecom_to_user": to_user,
        "wecom_api_base": api_base,
        "outbound_proxy": outbound_proxy,
    }
    if secret:
        values["wecom_secret"] = secret
    if callback_token:
        values["wecom_callback_token"] = callback_token
    if callback_aes_key:
        values["wecom_callback_aes_key"] = callback_aes_key
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
    scheduler_wakeup.set()
    return jsonify({"ok": True, "configured": notifier.configured})


def save_import_payload(body):
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    body, input_format = normalize_import(body)
    village = body.get("village") or {}
    upgrades = body.get("upgrades")
    if not isinstance(village, dict) or not village.get("name"):
        raise ValueError("village.name 不能为空")
    if not isinstance(upgrades, list):
        raise ValueError("upgrades 必须是数组")

    village_id = str(village.get("id") or village.get("player_tag") or uuid.uuid4())
    imported = 0
    imported_ids = []
    current = now_utc()
    current_iso = iso_utc(current)
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
    sent = process_due_upgrades()
    scheduler_wakeup.set()
    return {
        "ok": True,
        "village_id": village_id,
        "imported": imported,
        "format": input_format,
        "notifications_sent": sent,
    }


def get_wecom_callback_crypto():
    with get_db() as conn:
        values = {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('wecom_callback_token', 'wecom_callback_aes_key')"
            )
        }
    token = values.get("wecom_callback_token", "")
    aes_key = values.get("wecom_callback_aes_key", "")
    if not token or not aes_key:
        raise ValueError("尚未在网页后台配置回调 Token 和 EncodingAESKey")
    return WeComCallbackCrypto(token, aes_key, notifier.corp_id)


def send_wecom_import_result(from_user, content):
    try:
        notifier.send_text(content, to_user=from_user.split("/")[-1])
    except Exception:
        app.logger.exception("发送企业微信 JSON 导入结果失败")


def process_wecom_incoming_message(message):
    from_user = message.get("FromUserName", "")
    try:
        msg_type = message.get("MsgType", "")
        if msg_type == "text":
            raw_json = message.get("Content", "").strip()
        elif msg_type == "file":
            file_name = message.get("FileName", "village.json")
            media_id = message.get("MediaId", "")
            if not media_id:
                raise ValueError("企业微信文件消息缺少 MediaId")
            raw_json = notifier.download_media(media_id).decode("utf-8-sig")
            if not file_name.lower().endswith((".json", ".txt")):
                raise ValueError("请发送 .json 或 .txt 文件")
        else:
            raise ValueError("请发送村庄 JSON 文本，或发送 .json 文件")
        payload = json.loads(raw_json)
        result = save_import_payload(payload)
        send_wecom_import_result(
            from_user,
            "✅ 村庄 JSON 导入成功\n"
            f"村庄：{result['village_id']}\n"
            f"进行中项目：{result['imported']} 个\n"
            "升级完成后会自动通知。",
        )
    except Exception as exc:
        app.logger.exception("处理企业微信村庄 JSON 失败")
        send_wecom_import_result(from_user, f"❌ 村庄 JSON 导入失败\n原因：{exc}")


@app.route("/api/v1/wecom/callback", methods=["GET", "POST"])
def wecom_callback():
    try:
        crypto = get_wecom_callback_crypto()
        signature = request.args.get("msg_signature", "")
        timestamp = request.args.get("timestamp", "")
        nonce = request.args.get("nonce", "")
        if request.method == "GET":
            encrypted_echo = request.args.get("echostr", "")
            crypto.verify_signature(signature, timestamp, nonce, encrypted_echo)
            return Response(crypto.decrypt(encrypted_echo), mimetype="text/plain")

        outer_root = ET.fromstring(request.get_data(cache=False))
        encrypted = outer_root.findtext("Encrypt", "")
        if not encrypted:
            raise ValueError("企业微信回调缺少 Encrypt")
        crypto.verify_signature(signature, timestamp, nonce, encrypted)
        inner_xml = crypto.decrypt(encrypted)
        inner_root = ET.fromstring(inner_xml)
        message = {child.tag: child.text or "" for child in inner_root}
        msg_id = message.get("MsgId") or hashlib.sha1(
            (
                message.get("FromUserName", "")
                + ":"
                + message.get("CreateTime", "")
                + ":"
                + message.get("MsgType", "")
            ).encode("utf-8")
        ).hexdigest()
        with get_db() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO wecom_messages(msg_id, from_user, received_at)
                VALUES(?, ?, ?)
                """,
                (msg_id, message.get("FromUserName", ""), iso_utc(now_utc())),
            )
        if cursor.rowcount:
            threading.Thread(
                target=process_wecom_incoming_message,
                args=(message,),
                name=f"wecom-message-{msg_id}",
                daemon=True,
            ).start()
        return Response("", status=200)
    except (ValueError, ET.ParseError) as exc:
        app.logger.warning("拒绝企业微信回调：%s", exc)
        return Response(str(exc), status=400, mimetype="text/plain")


@app.post("/api/v1/import")
def import_village():
    auth = require_access()
    if auth:
        return auth
    try:
        result = save_import_payload(request.get_json(silent=True))
        return jsonify(result), 201
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.delete("/api/v1/upgrades/<upgrade_id>")
def delete_upgrade(upgrade_id):
    auth = require_access()
    if auth:
        return auth
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM upgrades WHERE id=?", (upgrade_id,))
    if not cursor.rowcount:
        return jsonify({"ok": False, "error": "升级项目不存在"}), 404
    scheduler_wakeup.set()
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
