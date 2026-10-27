# astrbot_plugin_netease_music · 网易云点歌

在 AstrBot 里用一句话点歌：搜索网易云音乐 → 数字选歌 → 机器人把歌当**语音消息**发给你。

全程**不需要网易云账号登录**，只使用公开可访问的资源。

## 功能

| 指令 | 说明 |
| --- | --- |
| `/点歌 歌名` | 搜索歌曲，返回带序号的候选列表 |
| 回复数字（如 `2`） | 选择对应歌曲，以语音消息发送 |
| `/选 2` | 同上，群聊里需要唤醒机器人时使用 |
| `取消` | 放弃本次点歌 |
| `/点歌帮助` | 查看使用说明 |

## 安装

1. 把整个 `astrbot_plugin_netease_music` 文件夹放进 AstrBot 的 `data/plugins/` 目录：

   ```
   AstrBot/
   └── data/
       └── plugins/
           └── astrbot_plugin_netease_music/
               ├── main.py
               ├── metadata.yaml
               ├── _conf_schema.json
               └── requirements.txt
   ```

2. 在 WebUI「插件管理」里重载插件（或重启 AstrBot）。
3. 依赖只有 `aiohttp`（AstrBot 自带），一般无需额外安装。

## 必装依赖：ffmpeg

**这是发语音的硬性前提。** AstrBot 在 aiocqhttp 平台发送 `Record` 时会强制转码：

```
Record.convert_to_base64() -> MediaResolver(..., media_type="audio").to_base64(target_format="wav")
        -> _resolve_path() -> ensure_wav() -> convert_audio_format() -> ffmpeg
```

所以只要没装 ffmpeg，日志就会报：

```
Failed to send the message chain: ... error = ffmpeg not found
FileNotFoundError: [Errno 2] No such file or directory: 'ffmpeg'
```

而且**机器人侧完全静默**——用户收不到任何回复。装好即可：

```bash
# Debian / Ubuntu
apt update && apt install -y ffmpeg

# CentOS / RHEL
yum install -y epel-release && yum install -y ffmpeg

# 验证
ffmpeg -version
```

- AstrBot 直接跑在宿主机：装完即可，无需重启。
- AstrBot 在 Docker 里：要装进**容器内**，或换用自带 ffmpeg 的镜像。
- 本插件启动时会检测一次 ffmpeg，缺失则在日志里打 WARNING；发歌时也会提前拦截并回复明确提示，而不是静默失败。

## 音源说明

插件按顺序尝试以下公开音源，任意一个成功即返回：

1. **网易云官方外链** `https://music.163.com/song/media/outer/url?id=<id>.mp3` —— 免登录，免费曲目可直接播放。
2. **meting 公开接口**（injahow / qijieya）—— 直接返回音频流，稳定性最好。
3. **gdstudio 公开解析** —— 返回 JSON 直链，可指定码率，偶有限流。

> 说明：官方外链对 VIP / 版权受限曲目会 302 到 404 页面，插件会自动识别并依次回退到后面的公开源。若全部音源都失败，会提示「暂时无法播放」。搜索列表里带 🔒 / 💰 标记的曲目失败概率较高。

## 配置项

在 WebUI 插件配置面板中可调：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `max_results` | 8 | 搜索结果条数（1–20） |
| `select_timeout` | 120 | 选歌列表有效期（秒） |
| `send_mode` | auto | `auto` / `base64` / `path` / `url` |
| `max_base64_mb` | 6 | auto 模式下走 base64 的大小阈值 |
| `quality` | 320 | 补源时请求的码率 |
| `max_duration` | 0 | 单曲时长上限（秒），0 为不限制 |
| `enable_cache` | true | 本地缓存，重复点播秒回 |
| `cache_max_mb` | 200 | 缓存上限，超出按时间清理 |
| `notify_before_send` | true | 发送前提示「正在准备」 |

**send_mode 怎么选：**

- `auto`（默认）：小文件 base64、大文件直链，兼容性最好。
- `path`：AstrBot 与协议端（NapCat / Lagrange）在同一台机器时最快，零拷贝。
- `url`：直接把网易云 CDN 直链交给协议端下载，服务端最省带宽。
- `base64`：始终内联发送，跨机器最稳，但大文件较慢。

## 注意事项

- **语音格式**：OneBot 协议端（NapCat / Lagrange / LLOneBot）发送 `record` 时通常需要把 mp3 转成 silk，请确保协议端所在环境已安装 `ffmpeg`，否则可能发送失败或被当成文件发送。
- **群聊唤醒**：AstrBot 默认需要 @机器人 或唤醒前缀才会响应。直接回复数字能触发，是因为插件用了正则过滤器（AstrBot 允许正则匹配唤醒）；如果你的配置关闭了该行为，请改用 `/选 2`。
- **平台限制**：语音消息能力依赖适配器。QQ（aiocqhttp / OneBot v11）支持最好；其他平台若适配器不支持 `record`，会退化成文件或发送失败。
- **版权**：本插件仅调用公开可访问的接口用于个人学习与娱乐，请勿用于商业分发。歌曲版权归网易云音乐及各版权方所有。

## 常见问题

**Q：日志报 `astrbot_plugin_netease_music: _empty() takes no arguments`？**
A：这是 v1.0.0 的签名 bug，v1.0.1 已修复。原因是 AstrBot 的 `CommandFilter` 会跳过 `self` 和 `event`，把处理函数剩下的参数当作指令参数解析；写成 `(self, event, *args)` 时，`*args` 的默认值是 `inspect.Parameter.empty`，框架会把它当类型构造函数调用，从而抛出该异常。修复方式是处理函数只保留 `(self, event)`，歌名从 `event.message_str` 读取。**升级到 v1.0.1 即可。**

**Q：回复数字没反应？**
A：确认在 `select_timeout` 秒内回复；确认发送的是纯数字（1–8）。群聊里可改用 `/选 2`。

**Q：`/点歌晴天` 没有空格，为什么没反应？**
A：AstrBot 的指令匹配要求指令与参数之间有空格。请写成 `/点歌 晴天`。

**Q：提示「暂时无法播放」？**
A：该曲目所有公开音源都取不到（多为独家版权）。换一首同名翻唱通常可以。

**Q：语音发出来是文件 / 没有声音？**
A：协议端缺少 ffmpeg，安装后重启协议端即可。

**Q：群里两个人同时点歌会怎样？**
A：待选列表按会话（群/私聊）保存，后一次搜索会覆盖前一次。多人同时点歌时建议各自私聊，或依次进行。

**Q：能不能搜索歌单 / 歌手？**
A：当前版本按单曲搜索。若需要，可扩展 `search_songs` 的 `type` 参数（1000=歌单，100=歌手）。
