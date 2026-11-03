# 其乐论坛推送 (astrbot_plugin_keylol_pusher)

<img src="logo.png" width="96" alt="其乐论坛推送">

把[其乐论坛](https://keylol.com)的新帖自动推送到 **QQ 群** 或 **QQ 私聊**。

插件通过论坛自带的 RSS 增量发现新帖，再按需抓取帖子首楼正文与配图，推送前会清理 Steam 小部件、广告位等噪声。群和私聊各自独立订阅，互不影响。

---

## 功能

- **按版块订阅**：订阅任意有 RSS 输出的版块（`fid`），一个会话可订阅多个。
- **关键字过滤**：只推标题/简介里含指定关键字的帖子，例如只推含「DLC」「汉化」的帖子。
- **群 + 私聊**：在群里发指令 = 订阅该群；私聊发指令 = 订阅该私聊，互不干扰。
- **正文与配图**：自动抓取首楼正文（可截断）与前 3 张图片。
- **持久化去重**：已推送的帖子记录在本地，重启不会重复推送。
- **首次不刷屏**：首次运行默认只记录历史帖、不推送，可按需关闭该行为。

---

## 安装

### 方式一：插件市场

在 AstrBot WebUI 的「插件市场」搜索 `其乐` 或 `keylol` 安装。

### 方式二：手动安装

1. 把整个 `astrbot_plugin_keylol_pusher` 目录放到 AstrBot 的 `data/plugins/` 下：

   ```
   AstrBot/
   └── data/
       └── plugins/
           └── astrbot_plugin_keylol_pusher/
               ├── main.py
               ├── kpusher_core.py
               ├── metadata.yaml
               ├── _conf_schema.json
               └── requirements.txt
   ```

2. 重启 AstrBot，或在 WebUI 插件页点击「重载」。

3. 依赖只有一个 `aiohttp`（AstrBot 已内置，通常无需额外安装）。若提示缺依赖，在 WebUI 插件页点「安装依赖」，或手动执行：

   ```bash
   pip install -r requirements.txt
   ```

> **平台说明**：主动推送依赖 `context.send_message()`，支持 **aiocqhttp（OneBot v11 / NapCat / Lagrange 等 QQ 协议端）** 与 **qq_official / qq_official_webhook（QQ 官方机器人）**。
>
> `qq_official` 的主动推送有额外前提（AstrBot v4.29+ 已内置适配）：
> - **群聊**：该群必须先给机器人发过消息（指令本身就能满足），适配器才会记住群场景并允许主动推送；否则日志会打印 `[QQOfficial] No cached msg_id for session: ... skip send_by_session`。
> - **私聊**：无此前置条件，直接可推。
> - 官方机器人受腾讯侧频率策略约束，推送过于频繁可能被限流；建议把 `interval` 设在 600 秒以上。

---

## 指令

所有指令都支持中文名与英文别名。

| 指令 | 别名 | 说明 |
| --- | --- | --- |
| `/其乐添加 <fid或版块名> [关键字...]` | `/kladd` | 为当前会话订阅版块 |
| `/其乐删除 <fid>` | `/kldel` | 取消订阅 |
| `/其乐列表` | `/kllist`、`/其乐订阅` | 查看当前会话的订阅 |
| `/其乐版块` | `/klforum` | 列出可订阅的版块（fid=名称） |
| `/其乐测试 [fid]` | `/kltest` | 立即抓一条最新帖，预览推送效果（只发给自己，不入库） |
| `/其乐状态` | `/klstatus` | 查看服务运行状态 |
| `/其乐帮助` | `/klhelp`、`/其乐` | 指令帮助 |

### 示例

```
/其乐添加 161               订阅「热点聚焦」版块全部新帖
/其乐添加 161 DLC 汉化       只推标题/简介含 DLC 或 汉化的帖子
/其乐添加 热点聚焦           用版块名订阅
/其乐删除 161               取消订阅
/其乐测试                   预览一条推送效果
```

### 常用版块 fid

| fid | 版块 | fid | 版块 |
| --- | --- | --- | --- |
| 140 | 社区公告 | 257 | 华语汉化 |
| 161 | 热点聚焦 | 127 | 平台工具 |
| 319 | 福利放送 | 235 | 成就指南 |
| 234 | 购物心得 | 129 | 游戏互鉴 |
| 271 | 慈善包 | 254 | 分享互赠 |
| 301 | 平台技术问题 | | |

完整列表用 `/其乐版块` 现拉（会访问论坛首页解析，失败时回退到内置列表）。

---

## 配置

在 AstrBot WebUI「插件配置」中修改，或直接编辑 `data/config/astrbot_plugin_keylol_pusher_config.json`。

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `interval` | int | `600` | 轮询间隔（秒），最小 60。太小容易被论坛限流，建议 300~900。 |
| `startup_delay` | int | `15` | 启动后等待多久开始首次轮询（秒）。 |
| `skip_history` | bool | `true` | 首次运行只记录历史帖、不推送。关闭后首次会推最近若干条。 |
| `max_per_run` | int | `5` | 单次轮询最多推送的帖子数（按帖子计数，推给多个会话仍算 1 条）；余下的下次继续。 |
| `fetch_detail` | bool | `true` | 是否抓帖子页拿完整正文与配图。关闭只推 RSS 摘要（请求量更小）。 |
| `send_images` | bool | `true` | 是否附带首楼配图（单次最多 3 张；`.avif`/`.webp` 等 QQ 官方接口不认的格式会自动跳过）。 |
| `show_content` | bool | `true` | 推送里是否显示帖子内容。关闭后只推版块、标题、作者、时间与链接，正文和 AI 摘要都不显示，也不会再调用模型。 |
| `content_limit` | int | `300` | 正文截断长度（字符），`0` 表示不截断。开启 AI 摘要后此项不再生效。 |
| `ai_summary` | bool | `false` | 开启后调用 AstrBot 已配置的对话模型，把正文概括成一段摘要再推送，不再显示全文。 |
| `summary_length` | int | `150` | AI 摘要字数上限（40~500），建议 120~200。 |
| `summary_provider` | string | `""` | 指定摘要用的模型提供商 ID（如 `deepseek/deepseek-flash`），留空用默认对话模型。 |
| `cookie` | string (secret) | `""` | 论坛 Cookie（可选）。部分需登录/绑定 Steam 的版块正文对游客不可见，填入后即可抓取。 |
| `user_agent` | string | `""` | 自定义 UA，留空用内置 Chrome UA。 |
| `proxy` | string | `""` | HTTP 代理，如 `http://127.0.0.1:7890`。 |
| `timeout` | int | `20` | 单次请求超时（秒）。 |
| `retries` | int | `2` | 请求失败重试次数。 |

### 关于 Cookie

其乐部分版块的正文对未登录访客不可见（页面会显示「浏览此历史内容请先登录和绑定 Steam 账号」）。此时插件会推送标题、作者、链接与简介，正文位置标注为不可见。

如需完整正文：

1. 浏览器登录 keylol.com；
2. 开发者工具 → Application → Cookies，复制 `keylol_salt`、`keylol_auth`、`keylol_auth_key` 等键值；
3. 拼成 `keylol_salt=xxx; keylol_auth=xxx; keylol_auth_key=xxx` 填入配置。

> Cookie 属于敏感凭据，配置项已标记为 `secret`，请勿分享配置文件。Cookie 过期后需重新获取。

### 关于 AI 摘要

开启 `ai_summary` 后，插件会把帖子的标题与正文交给 AstrBot 里已配置的对话模型，让模型概括成一段不超过 `summary_length` 个字的摘要，推送时用 `🤖` 前缀显示摘要、**不再显示帖子正文**：

```
📮 其乐 · 热点聚焦
《冰汽时代2》DLC「激流」Steam 页面开放
👤 万狐飞仙  🕘 11-03 01:51
🤖 《冰汽时代2》新 DLC「激流」已开放 Steam 商店页面，预计明年推出，新增剧本与派系机制。
🔗 https://keylol.com/t1054390-1-1
```

几点说明：

- 摘要失败（模型报错、未配置模型、返回空）时**自动回落到正文截断**，推送不会因此中断。
- 每次推送每帖调用一次模型。`max_per_run` 默认 5，即一轮最多 5 次调用。
- 帖子正文对游客不可见时（见上方 Cookie 说明），模型只能看到标题，摘要质量会下降。
- 想把推送压到最短，可关闭 `show_content`：此时既不显示正文也不显示摘要，也不会调用模型，只留标题与链接。

---

## 工作原理

```
 ┌────────────────────────┐
 │  定时轮询（asyncio）    │  每 interval 秒
 └───────────┬────────────┘
             ▼
 ┌────────────────────────────────────┐
 │ RSS 发现新帖                        │
 │ forum.php?mod=rss&fid=161&auth=0   │
 └───────────┬────────────────────────┘
             ▼  按 tid 去重（跳过已推送）
 ┌────────────────────────────────────┐
 │ 命中订阅的会话（按 fid + 关键字）    │
 └───────────┬────────────────────────┘
             ▼  fetch_detail
 ┌────────────────────────────────────┐
 │ 抓帖子页 /t{tid}-1-1                │
 │ td.t_f → 正文；img[file] → 图片     │
 │ 清理广告 / Steam 小部件 / iframe    │
 └───────────┬────────────────────────┘
             ▼
 ┌────────────────────────────────────┐
 │ context.send_message(umo, chain)   │
 │ 推送到群 / 私聊                     │
 └────────────────────────────────────┘
```

要点：

- **发现新帖用 RSS**，因为它是论坛官方输出、结构稳定、不受页面改版影响；
- **正文用帖子页解析**，因为 RSS 的 `description` 被截断到约 300 字且带 BBCode；
- **订阅关系用 `unified_msg_origin` 作为键**（形如 `aiocqhttp:GroupMessage:123456`），所以群与私聊天然隔离；
- **状态双写**：`data/plugin_data/astrbot_plugin_keylol_pusher/state.json` 与 AstrBot KV 存储，任一可用即可恢复。所有持久化数据只落在 AstrBot 的 `data/plugin_data/` 内（或 KV 接口），不会写到用户主目录或工作目录。

---

## 数据文件

```
data/plugin_data/astrbot_plugin_keylol_pusher/state.json
```

插件只使用 AstrBot 规定的数据目录：优先 `data/plugin_data/astrbot_plugin_keylol_pusher/`，同时把订阅关系与去重记录通过 `put_kv_data` / `get_kv_data` 写入 KV 存储。若该目录不可写，插件不会退回到其它位置，而是仅依赖 KV 接口（日志会给出警告）。

```jsonc
{
  "subscriptions": {
    "aiocqhttp:GroupMessage:123456": {
      "platform": "aiocqhttp",
      "type": "GroupMessage",
      "label": "群 123456",
      "sessions": [
        { "fid": 161, "name": "热点聚焦", "keywords": ["DLC"], "only_new": true }
      ]
    }
  },
  "seen": ["1054390", "1054312"],
  "updated_at": 1762000000
}
```

`seen` 最多保留 4000 条 tid，超出后按加入顺序淘汰最旧的。

---

## 常见问题

**Q：装好了但一条都没推？**
依次检查：① 是否已用 `/其乐添加` 订阅；② `skip_history` 是否为 `true` 且这是首次运行（用 `/其乐测试` 可立刻验证抓取链路）；③ `/其乐状态` 里「上次轮询」是否在推进；④ 日志里有没有 `[keylol]` 报错。

**Q：能收到但正文是「浏览此历史内容请先登录」？**
该版块正文对游客隐藏，按上文配置 `cookie`。

**Q：推送的图片加载不出来？**
图片是 `blob.keylol.com` 直链，部分 QQ 客户端/网络环境加载较慢。可把 `send_images` 关掉只推文字。

**Q：图片发不出来 / 日志里有「富媒体文件格式不支持」？**
插件已自动过滤掉 QQ 官方接口不支持的格式（`.avif`、`.webp` 等），只剩文字照常送达。若某帖全部配图都是这类格式，那条推送就是纯文字，属正常现象。

**Q：会不会推重复？**
不会。已推送的 tid 会持久化，重启后继续去重。

**Q：论坛改版了怎么办？**
RSS 通道通常不受影响；正文解析依赖 `td.t_f` 与 `img[file]`，若论坛改版这里失效，表现为正文为空但标题/链接正常 —— 提 issue 或自行改 `kpusher_core.py` 的 `html_to_text` / `parse_thread`。

**Q：请求量会不会太大？**
每个订阅版块每轮询周期 1 次 RSS 请求；仅对新帖才抓帖子页。默认 600 秒一轮，10 个版块/天约 1440 次 RSS 请求，已加入请求间隔（0.8~1.5 秒）与失败重试退避。

---

## 开发

解析层 `kpusher_core.py` 不依赖 AstrBot，可脱离框架单测：

```bash
# 解析层单测：78 项断言，使用真实抓取的页面作为 fixture
python -X utf8 keylol_dev/selftest.py

# 插件集成测试：用最小 astrbot 桩，按真实导入路径加载插件，
# 验证订阅存储、群/私聊推送、去重、关键字过滤
python -X utf8 keylol_dev/integration_test.py

ruff check astrbot_plugin_keylol_pusher/  # 代码规范
ruff format astrbot_plugin_keylol_pusher/
```

代码结构：

- `kpusher_core.py` — HTTP 客户端（aiohttp）、RSS / 列表页 / 帖子页解析、噪声清理、推送文案组装；
- `main.py` — AstrBot 插件壳：订阅存储、轮询调度、发送、指令。

---

## 许可

MIT
