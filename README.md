# 部落冲突升级提醒

部署在 NAS 上的村庄升级倒计时服务。提交村庄 JSON 后，服务会列出所有进行中的升级及其北京时间，并在完成前 1 小时、完成前 30 分钟和完成时通过企业微信自建应用发送消息。

调度器不会固定轮询村庄数据。导入 JSON 后会直接计算最近的提醒时刻并休眠，到达该时刻立即发送通知；收到新 JSON 时会自动唤醒并重新安排。只有企业微信发送失败时，才会按默认 30 秒间隔重试。

## 工作方式

```text
村庄数据来源 ──HTTP JSON──> 云服务器 IPv4/域名 ──穿透──> NAS Docker :8080
                                                       │
                                                       └──> 企业微信 API ──> 手机通知
```

既支持到点主动通知，也支持接收企业微信消息。配置加密回调后，可以在企业微信里直接向 COC 自建应用发送完整的村庄 JSON 文本。

## NAS 部署

在绿联 NAS 的 Docker“项目”中创建项目，把仓库内的 `docker-compose.yml` 整段粘贴进去即可。默认配置为：

- 镜像：`ghcr.mirrorify.net/xu15820768885-png/coc:latest`
- 访问端口：`802`
- 数据目录：`/volume1/docker/coc/data`

部署前必须修改 `ADMIN_PASSWORD`，不能保留示例值：

- `ADMIN_PASSWORD`：网页后台登录密码；
- `PUBLIC_BASE_URL`：对外 HTTPS 地址，用于生成企业微信回调 URL；本项目填写 `https://coc.weige1999.xin`。

`ADMIN_USERNAME` 默认为 `admin`，也可以修改。登录会话密钥会由程序首次启动时自动生成并保存在 `/data/.session-secret`，无需手工设置。`API_KEY` 也是可选项，只有其他程序需要自动调用接口提交 JSON 时才添加；登录后台后粘贴 JSON 不需要它。

如果 NAS 的共享文件夹路径不同，也要一并修改。默认不使用代理；只有企业微信要求特定出口 IP 时，才需要在网页后台填写 API 转发地址或 HTTP 出站代理。

命令行部署也可以直接执行：

```bash
docker compose pull
docker compose up -d
```

浏览器打开 `http://NAS-IP:802`，使用 Compose 中的 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 登录。然后在“企业微信通知”中填写 CorpID、AgentID、Secret 和接收成员，最后点击“发送测试通知”。

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

外部程序提交 JSON 时不需要登录网页，通过请求头传入 Compose 中单独设置的 `API_KEY`：

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

## 通过企业微信发送村庄 JSON

服务支持企业微信加密回调，可以直接把游戏复制出的整段 JSON 文本粘贴发送给自建应用；`.json` 文件仅作为备用方式。

回调地址固定为：

```text
https://coc.weige1999.xin/api/v1/wecom/callback
```

配置顺序：

1. 在企业微信自建应用的“API 接收消息”页面生成 Token 和 EncodingAESKey，先不要点击保存。
2. 登录 COC 网页后台，把相同的 Token 和 EncodingAESKey 填入“企业微信通知”区域并保存。
3. 回到企业微信管理后台，URL 填上面的回调地址，Token 和 EncodingAESKey 保持完全一致，然后保存。
4. 在企业微信里打开该应用，直接粘贴并发送游戏导出的完整 JSON 文本。
5. Docker 解密消息并导入后，会回复建筑名称、等级、剩余时间和预计完成的北京时间；随后在完成前 1 小时、30 分钟及完成时自动通知。
6. 在 COC 网页后台点击“创建/刷新微信菜单”，重新进入企业微信中的 COC 应用后即可看到“查看村庄、全部进度、使用帮助”菜单。

最多四个常用村庄会按首次导入顺序自动显示为 `sakura`、`shine`、`dizzy`、`erii`。发送 JSON 前不需要选择村庄：服务直接读取 JSON 中的唯一 `tag`，相同标签自动更新原村庄，新标签自动绑定到下一个空位。点击菜单中的村庄名字只用于查询对应进度，不会改变后续 JSON 的归属。

企业微信回调会重试消息，服务使用 `MsgId` 去重，同一条消息不会重复导入。

注意：同一个企业微信自建应用只能配置一个接收消息 URL。如果 MoviePilot 已经使用该应用接收消息，请为 COC 新建一个独立自建应用；可以继续使用同一个企业 ID 和 `https://wx.weige1999.xin` 转发服务，但 COC 应使用新的 AgentID 和 Secret，避免覆盖 MoviePilot 的回调。

## 游戏原始 JSON（推荐）

可以直接粘贴《部落冲突》设置中“导出村庄数据”复制出的整段 JSON，不需要转换。服务会：

- 使用 `tag` 作为村庄唯一标识；
- 使用 `timestamp + timer` 计算准确完成时间；
- 只导入 `buildings`、`traps`、`heroes`、`units`、`spells`、`siege_machines`、`pets` 等数组中带 `timer` 的进行中项目；
- 识别 `buildings → types → modules → timer` 中嵌套的精工防御模组升级；
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
| `POST` | `/api/v1/settings/wecom/menu` | 创建或刷新企业微信应用菜单 |
| `DELETE` | `/api/v1/upgrades/{id}` | 删除升级项目 |
| `POST` | `/api/v1/notifications/test` | 发送企业微信测试消息 |
| `POST` | `/api/v1/notifications/check` | 立即检查到期项目 |
| `GET` | `/health` | 健康检查 |

网页后台通过账号密码和会话 Cookie 鉴权。外部程序可以在请求中发送 `X-API-Key`，因此自动提交村庄 JSON 不受网页登录状态影响。`/health` 保持公开，供 Docker 健康检查使用。
