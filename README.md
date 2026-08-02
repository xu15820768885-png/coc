# 部落冲突升级提醒

部署在 NAS 上的村庄升级倒计时服务。外部程序通过 JSON API 提交村庄数据，服务保存升级项目并在完成时间到达后，通过企业微信自建应用发送消息。

## 工作方式

```text
村庄数据来源 ──HTTP JSON──> 云服务器 IPv4/域名 ──穿透──> NAS Docker :8080
                                                       │
                                                       └──> 企业微信 API ──> 手机通知
```

这是单向主动通知，不接收企业微信消息，因此不需要设置企业微信的回调 URL、Token 或 EncodingAESKey。

## NAS 部署

1. 复制配置：

   ```bash
   cp .env.example .env
   ```

2. 编辑 `.env`。强烈建议设置 `API_KEY`，防止公网接口和后台设置被他人修改。企业微信参数可以留空，启动后在网页后台填写：

   - `API_KEY`：建议设置一个随机长密码，防止公网接口被他人写入

3. 启动：

   ```bash
   docker compose up -d --build
   ```

4. 浏览器打开 `http://NAS-IP:802`，在“企业微信通知”中填写 CorpID、AgentID、Secret 和接收成员，然后点击“发送测试通知”。

网页中保存的通知配置和村庄数据都持久化在项目的 `data/` 目录，更新或重建容器不会丢失。`.env` 中的企业微信参数仍然可作为初始默认值；网页保存的设置优先。

## 云服务器穿透

把云服务器的 IPv4 端口或 HTTPS 域名反向代理到 `NAS-IP:802`。推荐最终使用 HTTPS，例如：

```text
https://coc.example.com/api/v1/import
```

如果只能使用 IPv4，也可以是：

```text
http://云服务器IPv4:映射端口/api/v1/import
```

调用时通过请求头传入 `.env` 中的密钥：

```bash
curl -X POST 'https://coc.example.com/api/v1/import' \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: change-me' \
  --data-binary @village.json
```

普通 FRP、Lucky、Cloudflare Tunnel 或反向代理只负责“外部访问 NAS”的入站流量，不会改变 Docker 主动请求企业微信时的出口 IP。

企业微信如果提示“不可信 IP”，有三种处理方式：

1. 在企业微信自建应用的可信 IP 中添加 NAS 宽带的实际公网出口 IP。
2. 在云服务器部署标准 HTTP/HTTPS 正向代理，并把地址填入 `OUTBOUND_PROXY`，例如 `http://user:pass@云服务器IPv4:3128`。
3. 使用与 MoviePilot 相同、且确实兼容企业微信 `/cgi-bin/gettoken` 和 `/cgi-bin/message/send` 的 API 转发服务，将根地址填入 `WECOM_API_BASE`。

不要把普通穿透地址直接填到 `OUTBOUND_PROXY`，除非它本身就是标准 HTTP 代理。

## 游戏原始 JSON（推荐）

可以直接粘贴《部落冲突》设置中“导出村庄数据”复制出的整段 JSON，不需要转换。服务会：

- 使用 `tag` 作为村庄唯一标识；
- 使用 `timestamp + timer` 计算准确完成时间；
- 只导入 `buildings`、`traps`、`heroes`、`units`、`spells`、`siege_machines`、`pets` 等数组中带 `timer` 的进行中项目；
- 自动把游戏数据 ID 转为中文名称；
- 忽略 `helper_cooldown`，因为它是助手工作日冷却，不是升级完成时间。
- 后续提交的新快照视为该村庄的最新状态；旧快照中存在、但新快照已经消失的升级会立即作为完成项目处理并只通知一次。

例如，下面这份游戏原始结构可以直接提交：

```json
{
  "tag": "#GG8QVU9UL",
  "timestamp": 1785646279,
  "buildings": [{"data": 1000085, "lvl": 1, "timer": 274236}],
  "heroes": [{"data": 28000001, "lvl": 46, "timer": 57985}]
}
```

也仍然支持下面的通用 JSON 格式，适合其他程序自行生成提醒。

## 通用 JSON 格式

明确完成时间（推荐使用带时区的 ISO 8601）：

```json
{
  "village": {
    "id": "home-1",
    "name": "我的家乡",
    "player_tag": "#ABC123"
  },
  "upgrades": [
    {
      "id": "builder-1",
      "name": "大本营",
      "category": "建筑",
      "level_from": 14,
      "level_to": 15,
      "started_at": "2026-08-02T12:00:00+08:00",
      "ends_at": "2026-08-04T18:30:00+08:00"
    }
  ]
}
```

如果数据源只有剩余秒数，也可以使用：

```json
{
  "village": {"id": "home-1", "name": "我的家乡"},
  "upgrades": [
    {
      "id": "hero-queen-79",
      "name": "弓箭女皇",
      "category": "英雄",
      "level_from": 78,
      "level_to": 79,
      "duration_seconds": 7200
    }
  ]
}
```

注意：

- 村庄 `id` 和升级项目 `id` 应保持稳定。再次提交同一 ID 会更新原记录。
- 更新同一升级项目的完成时间后，会重新建立提醒。
- 没有时区的时间按容器的 `TZ`（默认 `Asia/Shanghai`）解释。
- 当前支持的分类图标：建筑、英雄、军队、法术、陷阱、宠物、其他。

## API

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/v1/import` | 导入或更新村庄升级数据 |
| `GET` | `/api/v1/villages` | 查询所有村庄和升级状态 |
| `GET/PUT` | `/api/v1/settings/wecom` | 查看或保存企业微信通知设置 |
| `DELETE` | `/api/v1/upgrades/{id}` | 删除升级项目 |
| `POST` | `/api/v1/notifications/test` | 发送企业微信测试消息 |
| `POST` | `/api/v1/notifications/check` | 立即检查到期项目 |
| `GET` | `/health` | 健康检查 |

除 `/health` 外，配置 `API_KEY` 后所有接口都必须发送 `X-API-Key` 请求头。
