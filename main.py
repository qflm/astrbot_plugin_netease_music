"""
astrbot_plugin_netease_music —— 网易云点歌插件

功能：
    /点歌 <歌名>   搜索网易云音乐，返回带序号的歌曲列表
    回复数字       选择对应歌曲，以语音消息发送
    /点歌帮助      查看帮助

歌源说明：
    1. 官方外链  https://music.163.com/song/media/outer/url?id=<id>.mp3  （免登录）
    2. 公开解析  gdstudio / meting 等第三方公开接口，用于补全官方外链取不到的曲目
    全程不需要网易云账号登录。
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import re
import shutil
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Record
from astrbot.api.star import Context, Star, register

try:  # 兼容不同 AstrBot 版本
    from astrbot.api import AstrBotConfig
except Exception:  # pragma: no cover
    AstrBotConfig = dict  # type: ignore


PLUGIN_NAME = "astrbot_plugin_netease_music"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://music.163.com/",
    "Accept": "*/*",
}

SEARCH_HEADERS = dict(
    BASE_HEADERS,
    **{
        "Cookie": "os=pc; appver=8.9.70;",
        "Content-Type": "application/x-www-form-urlencoded",
    },
)

HELP_TEXT = (
    "🎵 网易云点歌 · 使用说明\n"
    "──────────────\n"
    "1️⃣ /点歌 歌名  —— 搜索歌曲\n"
    "      例：/点歌 晴天\n"
    "2️⃣ 回复序号    —— 选择并播放\n"
    "      例：2\n"
    "3️⃣ /选 序号    —— 同上（群聊需要唤醒时用）\n"
    "4️⃣ 取消        —— 放弃本次点歌\n"
    "5️⃣ /点歌帮助   —— 显示本说明\n"
    "──────────────\n"
    "· 歌源为网易云音乐公开资源，无需登录\n"
    "· 选中后将以语音消息发送\n"
    "· 列表 {timeout} 秒内有效"
)

# 纯数字（可带“选/选择”前缀或“号”后缀）
PICK_RE = r"^\s*(?:选|选择)?\s*(\d{1,2})\s*号?\s*$"
CANCEL_RE = r"^\s*(?:取消|放弃|不听了|算了|退出)\s*$"
SELECT_RE = r"(?:" + PICK_RE + r")|(?:" + CANCEL_RE + r")"

MIN_AUDIO_BYTES = 20 * 1024
MAX_AUDIO_BYTES = 80 * 1024 * 1024


def _fmt_duration(ms: int) -> str:
    """毫秒 -> mm:ss"""
    try:
        ms = int(ms or 0)
    except (TypeError, ValueError):
        return "--:--"
    if ms <= 0:
        return "--:--"
    total = ms // 1000
    return f"{total // 60:02d}:{total % 60:02d}"


@register(
    PLUGIN_NAME,
    "qflm",
    "网易云点歌：/点歌 歌名 搜索歌曲，按序号选择后以语音消息发送（免登录）",
    "1.0.2",
)
class NeteaseMusicPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        # ---- 配置 ----
        self.max_results = self._cfg_int("max_results", 8, 1, 20)
        self.select_timeout = self._cfg_int("select_timeout", 120, 10, 3600)
        self.max_duration = self._cfg_int("max_duration", 0, 0, 86400)
        self.send_mode = str(self.config.get("send_mode", "auto") or "auto").lower()
        self.quality = str(self.config.get("quality", "320") or "320")
        self.enable_cache = bool(self.config.get("enable_cache", True))
        self.cache_max_mb = self._cfg_int("cache_max_mb", 200, 0, 4096)
        self.notify_before_send = bool(self.config.get("notify_before_send", True))
        self.max_b64_mb = self._cfg_int("max_base64_mb", 6, 1, 50)

        # ---- 运行时状态 ----
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._http: Optional[aiohttp.ClientSession] = None
        self.cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
        except OSError as e:  # pragma: no cover
            logger.warning(f"[点歌] 缓存目录创建失败，将使用临时目录: {e}")
            self.cache_dir = os.path.join(os.path.abspath("."), "netease_music_cache")
            os.makedirs(self.cache_dir, exist_ok=True)

        if shutil.which("ffmpeg") is None:
            logger.warning(
                "[点歌] 未检测到 ffmpeg。AstrBot 在 aiocqhttp 平台发送语音时会强制把音频"
                "转成 wav（Record.convert_to_base64 -> target_format='wav'），缺少 ffmpeg "
                "会导致语音发送失败，日志报 `ffmpeg not found`。安装：apt install -y ffmpeg"
            )

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _ffmpeg_required(event: AstrMessageEvent) -> bool:
        """aiocqhttp 发送语音必须经过 ffmpeg 转码，这里提前判断避免静默失败"""
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:
            return False
        if platform != "aiocqhttp":
            return False
        return shutil.which("ffmpeg") is None

    def _cfg_int(self, key: str, default: int, lo: int = -2**31, hi: int = 2**31) -> int:
        try:
            val = int(self.config.get(key, default))
        except (TypeError, ValueError):
            val = default
        return max(lo, min(hi, val))

    @staticmethod
    def _sid(event: AstrMessageEvent) -> str:
        for attr in ("unified_msg_origin", "session_id"):
            val = getattr(event, attr, None)
            if val:
                return str(val)
        return "default"

    @staticmethod
    def _norm(text: str) -> str:
        """去掉开头的 / ! 等前缀"""
        t = (text or "").strip()
        t = re.sub(r"^[/!！。．.\s]+", "", t)
        return t.strip()

    def _extract_keyword(self, event: AstrMessageEvent, cmd: str) -> str:
        text = self._norm(getattr(event, "message_str", "") or "")
        if text.startswith(cmd):
            text = text[len(cmd):]
        return text.strip()

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(trust_env=True)
        return self._http

    # ------------------------------------------------------------- 搜索相关

    async def search_songs(self, keyword: str, limit: int) -> List[Dict[str, Any]]:
        """调用网易云公开搜索接口（免登录）"""
        session = await self._session()
        timeout = aiohttp.ClientTimeout(total=15)

        # 接口 1：cloudsearch（信息最全）
        try:
            async with session.post(
                "https://music.163.com/api/cloudsearch/pc",
                data={
                    "s": keyword,
                    "type": "1",
                    "limit": str(limit),
                    "offset": "0",
                    "total": "true",
                },
                headers=SEARCH_HEADERS,
                timeout=timeout,
            ) as resp:
                data = await resp.json(content_type=None)
            songs = self._parse_songs(data)
            if songs:
                return songs[:limit]
        except Exception as e:
            logger.debug(f"[点歌] cloudsearch 失败: {e}")

        # 接口 2：web 搜索（备用）
        try:
            async with session.get(
                "https://music.163.com/api/search/get/web",
                params={"s": keyword, "type": "1", "limit": str(limit), "offset": "0"},
                headers=SEARCH_HEADERS,
                timeout=timeout,
            ) as resp:
                data = await resp.json(content_type=None)
            songs = self._parse_songs(data)
            if songs:
                return songs[:limit]
        except Exception as e:
            logger.debug(f"[点歌] web search 失败: {e}")

        return []

    @staticmethod
    def _parse_songs(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        result = (data or {}).get("result") or {}
        raw = result.get("songs") or []
        out: List[Dict[str, Any]] = []
        for s in raw:
            try:
                sid = int(s.get("id"))
            except (TypeError, ValueError):
                continue
            artists_raw = s.get("artists") or s.get("ar") or []
            names = [
                a.get("name")
                for a in artists_raw
                if isinstance(a, dict) and a.get("name")
            ]
            album = s.get("album") or s.get("al") or {}
            album_name = album.get("name") if isinstance(album, dict) else ""
            try:
                duration = int(s.get("duration") or s.get("dt") or 0)
            except (TypeError, ValueError):
                duration = 0
            try:
                fee = int(s.get("fee") or 0)
            except (TypeError, ValueError):
                fee = 0
            out.append(
                {
                    "id": sid,
                    "name": html.unescape(s.get("name") or "未知歌曲"),
                    "artists": "、".join(names) if names else "未知歌手",
                    "album": html.unescape(album_name or ""),
                    "duration": duration,
                    "fee": fee,
                }
            )
        return out

    # ------------------------------------------------------------- 取音频

    def _cache_path(self, song_id: int) -> str:
        return os.path.join(self.cache_dir, f"{song_id}.mp3")

    async def _download(self, session: aiohttp.ClientSession, url: str, dest: str) -> Tuple[bool, Optional[str]]:
        """下载音频到 dest，返回 (是否成功, 最终 URL)"""
        tmp = dest + ".part"
        try:
            async with session.get(
                url,
                headers=BASE_HEADERS,
                timeout=aiohttp.ClientTimeout(total=180),
            ) as resp:
                if resp.status != 200:
                    return False, None
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/html" in ctype or "json" in ctype:
                    return False, None
                try:
                    clen = int(resp.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    clen = 0
                if clen > MAX_AUDIO_BYTES:
                    return False, None

                total = 0
                with open(tmp, "wb") as fh:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > MAX_AUDIO_BYTES:
                            raise ValueError("文件过大")
                        fh.write(chunk)
                final_url = str(resp.url)

            if total < MIN_AUDIO_BYTES:
                raise ValueError(f"文件过小: {total}")

            with open(tmp, "rb") as fh:
                head = fh.read(4)
            if head[:1] in (b"<", b"{"):
                raise ValueError("返回内容不是音频")

            os.replace(tmp, dest)
            return True, final_url
        except Exception as e:
            logger.debug(f"[点歌] 下载失败 {url[:80]}: {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return False, None

    async def _fetch_by_api(self, session: aiohttp.ClientSession, api_url: str) -> Optional[str]:
        """从返回 JSON 的解析接口里取出 url 字段"""
        try:
            async with session.get(
                api_url,
                headers={"User-Agent": UA, "Referer": "https://music.163.com/"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                text = await resp.text(errors="ignore")
            data = json.loads(text)
            url = (data or {}).get("url") or ""
            if isinstance(url, str) and url.startswith("http"):
                return url
        except Exception as e:
            logger.debug(f"[点歌] 解析接口失败 {api_url[:60]}: {e}")
        return None

    async def fetch_audio(self, song: Dict[str, Any], dest: str) -> Tuple[bool, Optional[str]]:
        """多源尝试获取音频，返回 (是否成功, 可用 URL)"""
        session = await self._session()
        song_id = song["id"]

        # 源 1：网易云官方外链（免登录，免费曲目可用）
        ok, final_url = await self._download(
            session, f"https://music.163.com/song/media/outer/url?id={song_id}.mp3", dest
        )
        if ok:
            return True, final_url

        # 源 2/3：meting 公开接口（直接返回音频流，最稳定）
        for api in (
            f"https://api.injahow.cn/meting/?type=url&id={song_id}",
            f"https://api.qijieya.cn/meting/?type=url&id={song_id}",
        ):
            ok, final_url = await self._download(session, api, dest)
            if ok:
                return True, final_url

        # 源 4：gdstudio 公开解析（返回 JSON，可取高码率，偶有限流）
        api = (
            "https://music-api.gdstudio.xyz/api.php"
            f"?types=url&source=netease&id={song_id}&br={self.quality}"
        )
        url = await self._fetch_by_api(session, api)
        if url:
            ok, final_url = await self._download(session, url, dest)
            if ok:
                return True, final_url

        return False, None

    # ------------------------------------------------------------- 发送相关

    def _build_record(self, path: str, url: Optional[str]):
        """根据配置构造语音消息组件"""
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0

        mode = self.send_mode
        if mode == "url" and url:
            return Record(file=url)
        if mode == "path":
            return Record(file=path)
        if mode == "base64":
            return Record(file=self._to_base64(path))

        # auto：小文件用 base64（跨机器最稳），大文件优先用 URL
        if size <= self.max_b64_mb * 1024 * 1024:
            return Record(file=self._to_base64(path))
        if url:
            return Record(file=url)
        return Record(file=path)

    @staticmethod
    def _to_base64(path: str) -> str:
        with open(path, "rb") as fh:
            return "base64://" + base64.b64encode(fh.read()).decode()

    def _prune_cache(self) -> None:
        if self.cache_max_mb <= 0:
            return
        try:
            files = [
                (os.path.join(self.cache_dir, n), os.path.getmtime(os.path.join(self.cache_dir, n)))
                for n in os.listdir(self.cache_dir)
                if n.endswith(".mp3")
            ]
        except OSError:
            return
        total = sum(os.path.getsize(p) for p, _ in files if os.path.exists(p))
        limit = self.cache_max_mb * 1024 * 1024
        if total <= limit:
            return
        for path, _ in sorted(files, key=lambda x: x[1]):
            try:
                total -= os.path.getsize(path)
                os.remove(path)
            except OSError:
                pass
            if total <= limit:
                break

    # ------------------------------------------------------------- 会话状态

    def _get_pending(self, sid: str) -> Optional[Dict[str, Any]]:
        item = self._pending.get(sid)
        if not item:
            return None
        if time.time() - item.get("ts", 0) > self.select_timeout:
            self._pending.pop(sid, None)
            return None
        return item

    def _set_pending(self, sid: str, songs: List[Dict[str, Any]], keyword: str) -> None:
        now = time.time()
        for key in [k for k, v in self._pending.items() if now - v.get("ts", 0) > self.select_timeout]:
            self._pending.pop(key, None)
        self._pending[sid] = {"songs": songs, "keyword": keyword, "ts": now}

    # --------------------------------------------------------------- 指令

    @filter.command("点歌帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """点歌帮助"""
        yield event.plain_result(HELP_TEXT.format(timeout=self.select_timeout))

    @filter.command("点歌")
    async def cmd_song(self, event: AstrMessageEvent):
        """点歌 <歌名>

        注意：AstrBot 的 CommandFilter 会跳过前两个参数（self、event），把其余参数
        当作指令参数解析。因此这里不能写 *args —— VAR_POSITIONAL 的默认值是
        inspect.Parameter.empty，框架会把它当类型构造函数调用，直接抛
        `_empty() takes no arguments`。歌名统一从 event.message_str 里取。
        """
        keyword = self._extract_keyword(event, "点歌")
        if not keyword or keyword in ("帮助", "help", "?", "？", "h"):
            yield event.plain_result(HELP_TEXT.format(timeout=self.select_timeout))
            return

        try:
            songs = await self.search_songs(keyword, self.max_results)
        except Exception as e:
            logger.error(f"[点歌] 搜索异常: {e}")
            yield event.plain_result("❌ 搜索出错了，请稍后再试～")
            return

        if not songs:
            yield event.plain_result(f"😢 没有找到与「{keyword}」相关的歌曲，换个关键词试试？")
            return

        self._set_pending(self._sid(event), songs, keyword)

        lines = [f"🔍 「{keyword}」搜索结果："]
        has_vip = False
        for idx, song in enumerate(songs, 1):
            tag = ""
            if song.get("fee") == 1:
                tag = " 🔒"
                has_vip = True
            elif song.get("fee") == 4:
                tag = " 💰"
                has_vip = True
            lines.append(
                f"{idx}. {song['name']} - {song['artists']}{tag}  [{_fmt_duration(song['duration'])}]"
            )
        lines.append("")
        if has_vip:
            lines.append("🔒 会员曲目 / 💰 付费曲目 可能无法播放")
        lines.append(f"👉 回复序号选择（{self.select_timeout} 秒内有效），回复「取消」放弃")
        yield event.plain_result("\n".join(lines))

    @filter.command("选")
    async def cmd_pick(self, event: AstrMessageEvent):
        """选 <序号>"""
        text = self._extract_keyword(event, "选")
        async for result in self._handle_select(event, text):
            yield result

    @filter.regex(SELECT_RE)
    async def on_number(self, event: AstrMessageEvent):
        """直接回复数字 / 取消"""
        async for result in self._handle_select(event, getattr(event, "message_str", "") or ""):
            yield result

    # ------------------------------------------------------------ 选择逻辑

    async def _handle_select(self, event: AstrMessageEvent, text: str):
        sid = self._sid(event)
        pending = self._get_pending(sid)
        if not pending:  # 没有待选列表 -> 保持沉默
            return

        raw = self._norm(text)

        if re.match(CANCEL_RE, raw):
            self._pending.pop(sid, None)
            yield event.plain_result("👌 已取消本次点歌")
            return

        m = re.match(PICK_RE, raw)
        if not m:
            return

        songs: List[Dict[str, Any]] = pending["songs"]
        try:
            index = int(m.group(1))
        except (TypeError, ValueError):
            return

        if index < 1 or index > len(songs):
            yield event.plain_result(f"⚠️ 序号超出范围，请输入 1 - {len(songs)} 之间的数字")
            return

        song = songs[index - 1]
        self._pending.pop(sid, None)

        if self.max_duration > 0 and song["duration"] > self.max_duration * 1000:
            yield event.plain_result(
                f"⚠️《{song['name']}》时长 {_fmt_duration(song['duration'])}，"
                f"超过限制 {_fmt_duration(self.max_duration * 1000)}，已取消"
            )
            return

        # aiocqhttp 发语音必须经过 ffmpeg 转 wav，缺了会静默失败，这里提前拦下
        if self._ffmpeg_required(event):
            logger.error("[点歌] 当前平台为 aiocqhttp 且未安装 ffmpeg，语音无法发送")
            yield event.plain_result(
                "⚠️ 机器人服务端未安装 ffmpeg，语音消息无法转码发送。\n"
                "请管理员在 AstrBot 所在机器执行：apt install -y ffmpeg"
            )
            return

        if self.notify_before_send:
            yield event.plain_result(f"🎧 正在准备《{song['name']}》…")

        async for result in self._send_song(event, song):
            yield result

    async def _send_song(self, event: AstrMessageEvent, song: Dict[str, Any]):
        path = self._cache_path(song["id"])
        url: Optional[str] = None

        cached = (
            self.enable_cache
            and os.path.isfile(path)
            and os.path.getsize(path) > MIN_AUDIO_BYTES
        )

        if not cached:
            try:
                ok, url = await self.fetch_audio(song, path)
            except Exception as e:
                logger.error(f"[点歌] 获取音频异常: {e}")
                ok = False
            if not ok:
                yield event.plain_result(
                    f"❌ 《{song['name']}》暂时无法播放（可能是 VIP / 版权限制或音源失效）"
                )
                return
            self._prune_cache()

        try:
            record = self._build_record(path, url)
        except Exception as e:
            logger.error(f"[点歌] 构造语音消息失败: {e}")
            yield event.plain_result("❌ 语音消息生成失败，请查看日志")
            return

        yield event.chain_result([record])

    # --------------------------------------------------------------- 生命周期

    async def terminate(self):
        if self._http and not self._http.closed:
            try:
                await self._http.close()
            except Exception:
                pass
        self._http = None
