import base64
import hashlib
import importlib
import json
import struct
from datetime import datetime, timedelta, timezone

import pytest
from Crypto.Cipher import AES


@pytest.fixture()
def module(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("DISABLE_AUTH", "1")
    monkeypatch.delenv("API_KEY", raising=False)
    import app

    importlib.reload(app)
    return app


def payload(seconds=3600):
    return {
        "village": {"id": "v1", "name": "测试村庄", "player_tag": "#TEST"},
        "upgrades": [
            {
                "id": "u1",
                "name": "大本营",
                "category": "建筑",
                "level_from": 14,
                "level_to": 15,
                "duration_seconds": seconds,
            }
        ],
    }


def encrypt_wecom_message(aes_key, receive_id, message):
    key = base64.b64decode(aes_key + "=")
    plain = (
        b"0123456789abcdef"
        + struct.pack(">I", len(message.encode("utf-8")))
        + message.encode("utf-8")
        + receive_id.encode("utf-8")
    )
    padding = 32 - len(plain) % 32
    padded = plain + bytes([padding]) * padding
    return base64.b64encode(
        AES.new(key, AES.MODE_CBC, key[:16]).encrypt(padded)
    ).decode()


def wecom_signature(token, timestamp, nonce, encrypted):
    return hashlib.sha1(
        "".join(sorted([token, timestamp, nonce, encrypted])).encode()
    ).hexdigest()


def test_import_and_list(module):
    client = module.app.test_client()
    response = client.post("/api/v1/import", json=payload())
    assert response.status_code == 201
    assert response.json["imported"] == 1
    assert response.json["upgrades"][0]["name"] == "大本营"

    data = client.get("/api/v1/villages").json
    assert data["villages"][0]["name"] == "测试村庄"
    assert data["villages"][0]["upgrades"][0]["level_text"] == "Lv14→15"
    assert 3500 <= data["villages"][0]["upgrades"][0]["remaining_seconds"] <= 3600


def test_reimport_updates_without_duplicate(module):
    client = module.app.test_client()
    client.post("/api/v1/import", json=payload(3600))
    client.post("/api/v1/import", json=payload(7200))
    upgrades = client.get("/api/v1/villages").json["villages"][0]["upgrades"]
    assert len(upgrades) == 1
    assert upgrades[0]["remaining_seconds"] > 7100


def test_due_upgrade_sends_once(module, monkeypatch):
    client = module.app.test_client()
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    body = payload()
    body["upgrades"][0].pop("duration_seconds")
    body["upgrades"][0]["ends_at"] = past
    client.post("/api/v1/import", json=body)

    sent = []
    monkeypatch.setattr(module.WeComNotifier, "configured", property(lambda self: True))
    monkeypatch.setattr(module.notifier, "send_text", lambda message: sent.append(message) or {"errcode": 0})
    assert module.process_due_upgrades() == 1
    assert module.process_due_upgrades() == 0
    assert "建筑：大本营" in sent[0]
    assert "等级：Lv14 → Lv15" in sent[0]


def test_only_sends_at_completion(module, monkeypatch):
    client = module.app.test_client()
    client.post("/api/v1/import", json=payload(7200))
    upgrade = client.get("/api/v1/villages").json["villages"][0]["upgrades"][0]
    end = module.parse_time(upgrade["ends_at"])
    clock = [end - timedelta(hours=1)]
    sent = []
    monkeypatch.setattr(module, "now_utc", lambda: clock[0])
    monkeypatch.setattr(
        module.WeComNotifier, "configured", property(lambda self: True)
    )
    monkeypatch.setattr(
        module.notifier,
        "send_text",
        lambda message: sent.append(message) or {"errcode": 0},
    )

    assert module.process_due_upgrades() == 0
    assert sent == []

    clock[0] = end - timedelta(minutes=30)
    assert module.process_due_upgrades() == 0
    assert sent == []

    clock[0] = end
    assert module.process_due_upgrades() == 1
    assert "✅ 建筑升级完成" in sent[-1]
    assert "村庄：测试村庄" in sent[-1]
    assert "建筑：大本营" in sent[-1]
    assert "等级：Lv14 → Lv15" in sent[-1]
    assert "完成时间：" in sent[-1]
    assert "（北京时间）" in sent[-1]
    assert module.process_due_upgrades() == 0


def test_invalid_payload(module):
    response = module.app.test_client().post(
        "/api/v1/import",
        json={"village": {"name": "测试"}, "upgrades": [{"name": "无时间"}]},
    )
    assert response.status_code == 400
    assert "ends_at" in response.json["error"]


def test_raw_game_export(module):
    raw = {
        "tag": "#GG8QVU9UL",
        "timestamp": 1785646279,
        "helpers": [{"data": 93000000, "lvl": 8, "helper_cooldown": 71731}],
        "buildings": [{"data": 1000085, "lvl": 1, "timer": 274236}],
        "units": [{"data": 4000005, "lvl": 11, "timer": 761222}],
        "heroes": [{"data": 28000001, "lvl": 46, "timer": 57985}],
        "pets": [{"data": 73000007, "lvl": 2, "timer": 93399}],
    }
    client = module.app.test_client()
    response = client.post("/api/v1/import", json=raw)
    assert response.status_code == 201
    assert response.json["format"] == "game_export"
    assert response.json["imported"] == 4
    assert response.json["slot"] == "A"
    village = client.get("/api/v1/villages").json["villages"][0]
    assert village["name"] == "sakura"
    assert village["player_tag"] == "#GG8QVU9UL"
    names = {upgrade["name"] for upgrade in village["upgrades"]}
    assert names == {"弹射加农炮", "气球兵", "弓箭女皇", "毒蜥"}
    assert all(upgrade["level_to"] == upgrade["level_from"] + 1 for upgrade in village["upgrades"])
    assert all("助手" not in upgrade["name"] for upgrade in village["upgrades"])

    second = {**raw, "tag": "#SECOND", "buildings": [], "units": []}
    second_response = client.post("/api/v1/import", json=second)
    assert second_response.json["slot"] == "B"
    repeat_response = client.post("/api/v1/import", json=raw)
    assert repeat_response.json["slot"] == "A"
    assert repeat_response.json["slot_name"] == "sakura"
    assert repeat_response.json["slot_new"] is False
    villages = client.get("/api/v1/villages").json["villages"]
    assert {village["name"]: village["player_tag"] for village in villages} == {
        "sakura": "#GG8QVU9UL",
        "shine": "#SECOND",
    }

    menu = module.build_wecom_menu()
    village_menu = menu["button"][0]["sub_button"]
    assert [button["name"] for button in village_menu] == ["sakura", "shine"]
    assert [button["key"] for button in village_menu] == [
        "COC_VILLAGE_A",
        "COC_VILLAGE_B",
    ]


def test_raw_game_export_includes_crafted_defense_module_timers(module):
    raw = {
        "tag": "#CRAFTED",
        "timestamp": 1785660674,
        "buildings": [
            {
                "data": 1000097,
                "types": [
                    {
                        "data": 103000011,
                        "modules": [
                            {"data": 102000033, "lvl": 3},
                            {"data": 102000034, "lvl": 1},
                            {
                                "data": 102000035,
                                "lvl": 4,
                                "timer": 20505,
                            },
                        ],
                    },
                    {
                        "data": 103000012,
                        "modules": [
                            {
                                "data": 102000036,
                                "lvl": 4,
                                "timer": 702,
                            }
                        ],
                    },
                        {
                            "data": 103000013,
                            "modules": [
                                {"data": 102000039, "lvl": 3},
                                {"data": 102000040, "lvl": 4},
                                {
                                    "data": 102000041,
                                "lvl": 1,
                                "timer": 23332,
                            }
                        ],
                    },
                ],
            }
        ],
    }
    response = module.app.test_client().post("/api/v1/import", json=raw)
    assert response.status_code == 201
    assert response.json["imported"] == 3
    assert [item["name"] for item in response.json["upgrades"]] == [
        "英雄猎台",
        "火热蜡烛",
        "蛋糕投掷器",
    ]
    assert all(
        item["category"] == "精工防御"
        for item in module.app.test_client()
        .get("/api/v1/villages")
        .json["villages"][0]["upgrades"]
    )


def test_new_raw_snapshot_makes_missing_upgrade_due(module):
    client = module.app.test_client()
    first = {
        "tag": "#SYNC",
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
        "buildings": [{"data": 1000085, "lvl": 1, "timer": 86400}],
    }
    client.post("/api/v1/import", json=first)
    client.post(
        "/api/v1/import",
        json={
            "tag": "#SYNC",
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "buildings": [],
        },
    )
    upgrade = client.get("/api/v1/villages").json["villages"][0]["upgrades"][0]
    assert upgrade["remaining_seconds"] == 0


def test_wecom_settings_are_persisted_and_secret_is_masked(module):
    client = module.app.test_client()
    response = client.put(
        "/api/v1/settings/wecom",
        json={
            "corp_id": "ww-test",
            "agent_id": "1000002",
            "secret": "top-secret-value",
            "to_user": "zhangsan|lisi",
            "api_base": "https://qyapi.weixin.qq.com",
            "outbound_proxy": "",
        },
    )
    assert response.status_code == 200
    assert response.json["configured"] is True

    settings = client.get("/api/v1/settings/wecom").json["settings"]
    assert settings["secret"] == "••••••••"
    assert settings["secret_set"] is True
    assert "top-secret-value" not in str(settings)
    assert settings["to_user"] == "zhangsan|lisi"

    client.put(
        "/api/v1/settings/wecom",
        json={
            "corp_id": "ww-updated",
            "agent_id": "1000003",
            "secret": "",
            "to_user": "@all",
            "api_base": "https://qyapi.weixin.qq.com",
            "outbound_proxy": "",
        },
    )
    assert module.notifier.secret == "top-secret-value"
    assert module.notifier.corp_id == "ww-updated"


def test_admin_login_and_api_key_access(module):
    module.AUTH_DISABLED = False
    module.ADMIN_USERNAME = "admin-test"
    module.ADMIN_PASSWORD = "password-test"
    module.API_KEY = "api-key-test"
    client = module.app.test_client()

    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    assert client.get("/api/v1/villages").status_code == 401

    wrong = client.post(
        "/login",
        data={"username": "admin-test", "password": "wrong"},
    )
    assert wrong.status_code == 200
    assert "账号或密码错误" in wrong.get_data(as_text=True)

    logged_in = client.post(
        "/login",
        data={"username": "admin-test", "password": "password-test"},
    )
    assert logged_in.status_code == 302
    assert client.get("/api/v1/villages").status_code == 200

    external_client = module.app.test_client()
    imported = external_client.post(
        "/api/v1/import",
        json=payload(),
        headers={"X-API-Key": "api-key-test"},
    )
    assert imported.status_code == 201


def test_session_secret_is_generated_and_persisted(module):
    secret_path = module.DB_PATH.parent / ".session-secret"
    assert secret_path.exists()
    first_secret = secret_path.read_text().strip()
    assert len(first_secret) >= 48
    assert module.load_session_secret() == first_secret


def test_scheduler_waits_until_exact_reminder_time(module, monkeypatch):
    client = module.app.test_client()
    client.post("/api/v1/import", json=payload(7200))
    monkeypatch.setattr(module.WeComNotifier, "configured", property(lambda self: True))

    delay = module.seconds_until_next_upgrade()
    assert 7100 <= delay <= 7200

    with module.get_db() as conn:
        conn.execute(
            "UPDATE upgrades SET ends_at=? WHERE id='u1'",
            (
                (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                ).isoformat().replace("+00:00", "Z"),
            ),
        )
    assert module.seconds_until_next_upgrade() == module.RETRY_INTERVAL


def test_wecom_callback_verification_and_text_json_import(module, monkeypatch):
    token = "callbackToken123"
    aes_key = base64.b64encode(b"k" * 32).decode().rstrip("=")
    corp_id = "ww-callback-test"
    client = module.app.test_client()
    saved = client.put(
        "/api/v1/settings/wecom",
        json={
            "corp_id": corp_id,
            "agent_id": "1000002",
            "secret": "outgoing-secret",
            "to_user": "@all",
            "api_base": "https://qyapi.weixin.qq.com",
            "outbound_proxy": "",
            "callback_token": token,
            "callback_aes_key": aes_key,
        },
    )
    assert saved.status_code == 200
    settings = client.get(
        "/api/v1/settings/wecom",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "coc.weige1999.xin",
        },
    ).json["settings"]
    assert (
        settings["callback_url"]
        == "https://coc.weige1999.xin/api/v1/wecom/callback"
    )

    monkeypatch.setattr(module, "PUBLIC_BASE_URL", "https://public.example.com")
    settings = client.get("/api/v1/settings/wecom").json["settings"]
    assert (
        settings["callback_url"]
        == "https://public.example.com/api/v1/wecom/callback"
    )

    timestamp = "1785650000"
    nonce = "123456"
    encrypted_echo = encrypt_wecom_message(aes_key, corp_id, "verified-echo")
    response = client.get(
        "/api/v1/wecom/callback",
        query_string={
            "msg_signature": wecom_signature(
                token, timestamp, nonce, encrypted_echo
            ),
            "timestamp": timestamp,
            "nonce": nonce,
            "echostr": encrypted_echo,
        },
    )
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "verified-echo"

    replies = []
    monkeypatch.setattr(
        module.notifier,
        "send_text",
        lambda content, to_user=None: replies.append((to_user, content))
        or {"errcode": 0},
    )
    monkeypatch.setattr(
        module.notifier,
        "create_menu",
        lambda menu: {"errcode": 0, "menu": menu},
    )

    class ImmediateThread:
        def __init__(self, target, args=(), **kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(module.threading, "Thread", ImmediateThread)
    json_text = json.dumps(payload(), ensure_ascii=False)
    inner_xml = (
        "<xml>"
        f"<ToUserName><![CDATA[{corp_id}]]></ToUserName>"
        "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
        "<CreateTime>1785650000</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{json_text}]]></Content>"
        "<MsgId>987654321</MsgId>"
        "<AgentID>1000002</AgentID>"
        "</xml>"
    )
    encrypted = encrypt_wecom_message(aes_key, corp_id, inner_xml)
    signature = wecom_signature(token, timestamp, nonce, encrypted)
    outer_xml = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"
    callback = client.post(
        "/api/v1/wecom/callback",
        query_string={
            "msg_signature": signature,
            "timestamp": timestamp,
            "nonce": nonce,
        },
        data=outer_xml,
        content_type="application/xml",
    )
    assert callback.status_code == 200
    assert client.get("/api/v1/villages").json["villages"][0]["id"] == "v1"
    import_reply = next(reply for reply in replies if reply[0] == "zhangsan")
    assert "导入成功" in import_reply[1]
    assert "1. 大本营 Lv14→15" in import_reply[1]
    assert "剩余" in import_reply[1]
    assert "北京时间" in import_reply[1]
    assert "建筑升级完成时会自动通知" in import_reply[1]
    assert "完成前" not in import_reply[1]

    module.process_wecom_incoming_message(
        {
            "FromUserName": "zhangsan",
            "MsgType": "event",
            "Event": "click",
            "EventKey": "COC_VILLAGE_A",
        }
    )
    assert "sakura 升级进度" in replies[-1][1]
    assert "大本营 Lv14→15" in replies[-1][1]

    reply_count = len(replies)
    duplicate = client.post(
        "/api/v1/wecom/callback",
        query_string={
            "msg_signature": signature,
            "timestamp": timestamp,
            "nonce": nonce,
        },
        data=outer_xml,
        content_type="application/xml",
    )
    assert duplicate.status_code == 200
    assert len(replies) == reply_count

    monkeypatch.setattr(
        module.notifier,
        "download_media",
        lambda media_id: json.dumps(payload(7200), ensure_ascii=False).encode(),
    )
    module.process_wecom_incoming_message(
        {
            "FromUserName": "zhangsan",
            "MsgType": "file",
            "FileName": "village.json",
            "MediaId": "media-123",
        }
    )
    assert "导入成功" in replies[-1][1]


def test_wecom_crypto_accepts_large_json_text(module):
    token = "callbackToken123"
    aes_key = base64.b64encode(b"z" * 32).decode().rstrip("=")
    corp_id = "ww-large-json"
    large_json = json.dumps(
        {
            "tag": "#LARGE",
            "timestamp": 1785650000,
            "buildings": [{"data": 1000085, "lvl": 1, "timer": 3600}],
            "padding": "x" * 12000,
        }
    )
    encrypted = encrypt_wecom_message(aes_key, corp_id, large_json)
    crypto = module.WeComCallbackCrypto(token, aes_key, corp_id)
    assert crypto.decrypt(encrypted) == large_json
