import importlib
from datetime import datetime, timedelta, timezone

import pytest


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


def test_import_and_list(module):
    client = module.app.test_client()
    response = client.post("/api/v1/import", json=payload())
    assert response.status_code == 201
    assert response.json["imported"] == 1

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
    assert "大本营 Lv14→15" in sent[0]


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
    village = client.get("/api/v1/villages").json["villages"][0]
    assert village["player_tag"] == "#GG8QVU9UL"
    names = {upgrade["name"] for upgrade in village["upgrades"]}
    assert names == {"弹射加农炮", "气球兵", "弓箭女皇", "毒蜥"}
    assert all(upgrade["level_to"] == upgrade["level_from"] + 1 for upgrade in village["upgrades"])
    assert all("助手" not in upgrade["name"] for upgrade in village["upgrades"])


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
