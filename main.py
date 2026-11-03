"""其乐论坛 (Keylol) 帖子推送插件。

把其乐论坛的新帖按订阅关系主动推送到 QQ 群 / QQ 私聊。
推送链路：
    RSS（forum.php?mod=rss&fid=X） -> 发现新帖 -> 抓帖子详情补全正文/图片 -> 发送

指令：
    /其乐添加 <fid 或版块名> [...关键字]   为当前会话订阅版块（可多个，空格分隔）
    /其乐删除 <fid>                        取消订阅
    /其乐列表                              查看当前会话订阅
    /其乐测试 [fid]                        立即拉取一条最新帖子做推送预览
    /其乐状态                              查看全局推送状态
    /其乐帮助                              指令帮助

作者: keylol-pusher
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .kpusher_core import (
    KNOWN_FORUMS,
    KeylolClient,
    KeylolError,
    ThreadDetail,
    ThreadItem,
    format_push_text,
    guess_forum_name,
    shorten,
)

PLUGIN_NAME = "astrbot_plugin_keylol_pusher"
KV_SUBSCRIPTIONS = "keylol_subscriptions"
KV_SEEN = "keylol_seen_tids"

# 单次推送的图片数量上限，避免刷屏
MAX_IMAGES_PER_PUSH = 3
# 已推送 tid 的保留条数
MAX_SEEN = 4000
# 可安全推送的图片扩展名。QQ 官方 API 的富媒体接口只认常见位图，
# 碰到 .avif / .webp / .svg 等会报「富媒体文件格式不支持」并重试到失败，
# 所以直接跳过这类图，宁可不发图也不让整条推送卡在重试上。
SAFE_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".bmp")


# --------------------------------------------------------------------------- #
# 订阅存储
# --------------------------------------------------------------------------- #


class SubscriptionStore:
    """按会话（unified_msg_origin）保存订阅关系。

    结构: {"<umo>": {"platform": "aiocqhttp", "type": "GroupMessage",
                    "sessions": [{"fid": 161, "name": "热点聚焦",
                                  "keywords": ["DLC"], "only_new": true}]}}
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data: dict[str, dict[str, Any]] = data or {}

    # -- 序列化 -------------------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return self.data

    # -- 查询 ---------------------------------------------------------------- #
    def targets(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self.data.items())

    def get(self, umo: str) -> dict[str, Any]:
        return self.data.setdefault(umo, {"sessions": []})

    def subs_of(self, umo: str) -> list[dict[str, Any]]:
        return self.get(umo).setdefault("sessions", [])

    def all_forums(self) -> set[int]:
        fids: set[int] = set()
        for meta in self.data.values():
            for sub in meta.get("sessions", []):
                try:
                    fids.add(int(sub.get("fid")))
                except (TypeError, ValueError):
                    continue
        return fids

    # -- 修改 ---------------------------------------------------------------- #
    def add(
        self, umo: str, fid: int, name: str, keywords: list[str]
    ) -> tuple[bool, str]:
        meta = self.get(umo)
        subs = meta.setdefault("sessions", [])
        for sub in subs:
            if int(sub.get("fid", 0)) == fid:
                # 已订阅：合并关键字
                merged = list(dict.fromkeys(list(sub.get("keywords", [])) + keywords))
                sub["keywords"] = merged
                if name:
                    sub["name"] = name
                return (
                    True,
                    f"已更新订阅：{name or fid}（关键字：{'、'.join(merged) or '无'}）",
                )
        subs.append(
            {
                "fid": fid,
                "name": name or guess_forum_name(fid),
                "keywords": keywords,
                "only_new": True,
            }
        )
        return True, f"已订阅：{name or guess_forum_name(fid)}（fid={fid}）"

    def remove(self, umo: str, fid: int) -> bool:
        subs = self.subs_of(umo)
        before = len(subs)
        self.data[umo]["sessions"] = [s for s in subs if int(s.get("fid", 0)) != fid]
        return len(self.data[umo]["sessions"]) != before

    def merge_legacy(self, other: SubscriptionStore) -> None:
        """兼容早期只按 fid 存的列表结构。"""
        for umo, meta in other.data.items():
            mine = self.get(umo)
            for sub in meta.get("sessions", []):
                if not any(
                    int(x.get("fid", 0)) == int(sub.get("fid", 0))
                    for x in mine["sessions"]
                ):
                    mine["sessions"].append(sub)


def _match_keywords(
    item: ThreadItem, detail: ThreadDetail | None, keywords: list[str]
) -> bool:
    """关键字过滤：命中任一关键字即可；未配置关键字时全部通过。"""
    if not keywords:
        return True
    blob = item.keywords_blob()
    if detail is not None and detail.content:
        blob += "\n" + detail.content
    blob = blob.lower()
    return any(kw.lower() in blob for kw in keywords if kw)


def _pick_sendable_images(images: list[str]) -> list[str]:
    """挑出可以安全推送的图片，最多 MAX_IMAGES_PER_PUSH 张。

    部分图床会返回 .avif/.webp 等格式，QQ 官方富媒体接口不认，会一直重试到
    失败并拖慢整条推送，因此这里按扩展名先过滤一遍。
    """
    picked: list[str] = []
    for url in images:
        path = url.split("?", 1)[0].lower()
        if not path.endswith(SAFE_IMAGE_EXTS):
            logger.debug(f"[keylol] 跳过不支持的图片格式: {url}")
            continue
        picked.append(url)
        if len(picked) >= MAX_IMAGES_PER_PUSH:
            break
    return picked


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #


class KeylolPusherPlugin(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.store = SubscriptionStore()
        self.seen: set[str] = set()
        self._seen_order: list[str] = []
        self._client: KeylolClient | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_run: float = 0.0
        self._last_error: str = ""
        self._push_count: int = 0
        self._data_path = ""
        self._forum_names: dict[int, str] = dict(KNOWN_FORUMS)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self) -> None:
        """插件启用时由 AstrBot 调用（实例方法），启动后台轮询。"""
        try:
            await self._startup()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[keylol] 初始化失败: {exc}")

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except AttributeError:
            value = default
        return default if value is None else value

    def _build_client(self) -> KeylolClient:
        # user_agent 留空时由 KeylolClient 回落到内置 Chrome UA
        return KeylolClient(
            cookie=str(self._cfg("cookie", "") or ""),
            user_agent=str(self._cfg("user_agent", "") or ""),
            timeout=float(self._cfg("timeout", 20) or 20),
            proxy=str(self._cfg("proxy", "") or ""),
            retries=int(self._cfg("retries", 2) or 2),
        )

    async def _startup(self) -> None:
        if self._running:
            return
        if not self._data_path:
            self._data_path = self._resolve_data_path()
        await self._load_state()
        self._restore_qq_scenes()
        self._client = self._build_client()
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"[keylol] 推送服务已启动，订阅会话 {len(self.store.data)} 个，"
            f"轮询间隔 {self._interval()} 秒"
        )

    def _restore_qq_scenes(self, umos: list[str] | None = None) -> int:
        """为会话补回 QQ 官方适配器的会话场景记录。

        QQ 官方 API 的主动推送要求适配器内存里有该会话的 scene（"group"/"friend"），
        而这份记录只在收到该会话的消息时写入、重启即丢。订阅本身就是由会话主动
        发指令建立的，所以按订阅里的会话类型补登记是安全的；否则群里一条都推不
        出去（适配器会静默丢弃且不报错）。

        Args:
            umos: 只处理这些会话；为 None 时处理全部已订阅会话。

        Returns:
            成功补登记的 (会话, 平台) 数量。
        """
        restored = 0
        try:
            platforms = list(self.context.platform_manager.platform_insts)
        except Exception as exc:  # noqa: BLE001 - 拿不到平台列表就跳过
            logger.debug(f"[keylol] 无法枚举平台实例: {exc}")
            return 0
        targets = self.store.targets()
        if umos is not None:
            wanted = set(umos)
            targets = [(u, m) for u, m in targets if u in wanted]
        for umo, _meta in targets:
            parts = umo.split(":", 2)
            if len(parts) != 3:
                continue
            platform_id, msg_type, session_id = parts
            if msg_type == "GroupMessage":
                scene = "group"
            elif msg_type == "FriendMessage":
                scene = "friend"
            else:
                continue
            for platform in platforms:
                try:
                    if str(platform.meta().id) != platform_id:
                        continue
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[keylol] 读取平台 ID 失败: {exc}")
                    continue
                remember = getattr(platform, "remember_session_scene", None)
                if remember is None:
                    continue
                for sid in {session_id, session_id.rsplit("_", 1)[-1]}:
                    try:
                        remember(sid, scene)
                        restored += 1
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"[keylol] 补登记会话场景失败 {sid}: {exc}")
        if restored and umos is None:
            logger.info(f"[keylol] 已为 {restored} 个已订阅会话补回推送场景")
        return restored

    def _resolve_data_path(self) -> str:
        """返回插件专属的持久化目录（AstrBot 规定的 data/plugin_data/<name>）。

        只认 AstrBot 的 data 目录，绝不回退到用户主目录或当前工作目录，
        以免订阅关系与去重记录散落在数据目录之外、影响备份与迁移。
        取不到路径或目录不可写时返回空串，此时全部状态改由 KV 接口承载。
        """
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            path = os.path.join(get_astrbot_data_path(), "plugin_data", PLUGIN_NAME)
        except Exception as exc:  # noqa: BLE001 - 非 AstrBot 环境下没有这个模块
            logger.debug(f"[keylol] 无法定位 AstrBot data 目录，改用 KV 存储: {exc}")
            return ""

        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".write_test")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
        except Exception as exc:  # noqa: BLE001 - 目录不可写则只用 KV
            logger.warning(f"[keylol] 数据目录不可写，改用 KV 存储: {path} ({exc})")
            return ""
        return path

    @property
    def _state_file(self) -> str:
        return os.path.join(self._data_path, "state.json")

    def _read_state_file(self) -> dict[str, Any]:
        """同步读取 state.json（由 asyncio.to_thread 调用）。"""
        try:
            if not os.path.exists(self._state_file):
                return {}
            with open(self._state_file, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001 - 状态坏了就当空状态重来
            logger.warning(f"[keylol] 读取本地状态失败: {exc}")
            return {}

    def _write_state_file(self, payload: dict[str, Any]) -> None:
        """同步写入 state.json（由 asyncio.to_thread 调用）。"""
        if not self._data_path:
            return
        os.makedirs(self._data_path, exist_ok=True)
        tmp = self._state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self._state_file)

    async def _load_state(self) -> None:
        if not self._data_path:
            self._data_path = self._resolve_data_path()
        raw: dict[str, Any] = {}
        if self._data_path:
            raw = await asyncio.to_thread(self._read_state_file)

        # 优先读 KV 存储（AstrBot >= 4.9.2），失败则退回本地文件
        subs_data = None
        try:
            stored = await self.get_kv_data(KV_SUBSCRIPTIONS, None)
            if stored:
                subs_data = json.loads(stored) if isinstance(stored, str) else stored
        except Exception:  # noqa: BLE001
            subs_data = None
        if not subs_data:
            subs_data = raw.get("subscriptions") or {}
        self.store = SubscriptionStore(subs_data if isinstance(subs_data, dict) else {})

        seen_raw: Any = None
        try:
            seen_raw = await self.get_kv_data(KV_SEEN, None)
        except Exception:  # noqa: BLE001
            seen_raw = None
        if not seen_raw:
            seen_raw = raw.get("seen") or []
        if isinstance(seen_raw, list):
            self.seen = {str(x) for x in seen_raw}
            self._seen_order = [str(x) for x in seen_raw]

    async def _save_state(self) -> None:
        if not self._data_path:
            self._data_path = self._resolve_data_path()
        seen_tail = self._seen_order[-MAX_SEEN:]
        payload = {
            "subscriptions": self.store.to_dict(),
            "seen": seen_tail,
            "updated_at": int(time.time()),
        }
        # 本地缓存只在拿到合规数据目录时写，绝不写到别处
        if self._data_path:
            try:
                await asyncio.to_thread(self._write_state_file, payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[keylol] 写入本地状态失败: {exc}")
        try:
            await self.put_kv_data(
                KV_SUBSCRIPTIONS,
                json.dumps(self.store.to_dict(), ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001 - KV 不可用时本地文件已写入
            logger.debug(f"[keylol] 写入订阅 KV 失败: {exc}")
        try:
            await self.put_kv_data(KV_SEEN, seen_tail)
        except Exception as exc:  # noqa: BLE001 - 同上
            logger.debug(f"[keylol] 写入去重记录 KV 失败: {exc}")

    async def terminate(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.debug("[keylol] 轮询任务已取消")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[keylol] 轮询任务异常退出: {exc}")
        if self._client is not None:
            await self._client.close()
            self._client = None
        await self._save_state()
        logger.info("[keylol] 推送服务已停止")

    # ------------------------------------------------------------------ #
    # 轮询主循环
    # ------------------------------------------------------------------ #
    def _interval(self) -> int:
        try:
            return max(60, int(self._cfg("interval", 600) or 600))
        except (TypeError, ValueError):
            return 600

    async def _poll_loop(self) -> None:
        # 启动后先等一会，避免和 AstrBot 自身启动抢资源
        await asyncio.sleep(float(self._cfg("startup_delay", 15) or 15))
        while self._running:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.error(f"[keylol] 轮询异常: {exc}")
            await asyncio.sleep(self._interval())

    async def poll_once(self, *, startup: bool = False) -> int:
        """轮询一次所有被订阅的版块，返回推送条数。"""
        if self._client is None:
            self._client = self._build_client()
        self._last_run = time.time()
        self._last_error = ""
        pushed = 0
        marked = 0
        threads_sent = 0
        fids = sorted(self.store.all_forums())
        if not fids:
            return 0
        max_per_run = int(self._cfg("max_per_run", 5) or 5)
        first_run = startup or not self.seen

        for fid in fids:
            if threads_sent >= max_per_run:
                break
            try:
                items = await self._client.fetch_rss(fid)
            except KeylolError as exc:
                logger.warning(f"[keylol] 拉取 RSS 失败 fid={fid}: {exc}")
                self._last_error = str(exc)
                continue
            # RSS 新帖在前；从旧到新逐条处理，保证推送顺序自然
            for item in reversed(items):
                if item.tid in self.seen:
                    continue
                targets = self._targets_for(fid, item)
                if not targets:
                    if self._mark_seen(item.tid):
                        marked += 1
                    continue
                if first_run and bool(self._cfg("skip_history", True)):
                    # 首次运行只记录不推送，避免一次性刷屏历史帖
                    if self._mark_seen(item.tid):
                        marked += 1
                    continue
                if threads_sent >= max_per_run:
                    break
                detail: ThreadDetail | None = None
                if bool(self._cfg("fetch_detail", True)):
                    try:
                        detail = await self._client.fetch_thread(item.tid)
                        await asyncio.sleep(0.8)
                    except KeylolError as exc:
                        logger.debug(f"[keylol] 抓详情失败 tid={item.tid}: {exc}")
                sent = await self._push(item, detail, targets)
                if sent:
                    pushed += sent
                    self._push_count += sent
                    threads_sent += 1
                if self._mark_seen(item.tid):
                    marked += 1
            await asyncio.sleep(1.0)
        # 即使一条都没推送（首轮只记录 / 关键字全不匹配），只要 seen 有新增就要落盘，
        # 否则重启后这批帖子会被当成历史帖重新吞一次。
        if pushed or marked:
            await self._save_state()
            if marked and not pushed:
                logger.debug(f"[keylol] 本轮仅记录 {marked} 条已见帖子，未推送")
        return pushed

    def _targets_for(
        self, fid: int, item: ThreadItem
    ) -> list[tuple[str, dict[str, Any]]]:
        result: list[tuple[str, dict[str, Any]]] = []
        for umo, meta in self.store.targets():
            for sub in meta.get("sessions", []):
                try:
                    if int(sub.get("fid")) != fid:
                        continue
                except (TypeError, ValueError):
                    continue
                if _match_keywords(item, None, list(sub.get("keywords") or [])):
                    result.append((umo, sub))
                    break
        return result

    def _mark_seen(self, tid: str) -> bool:
        """记录一条已处理帖子，返回是否为新记录。"""
        if tid in self.seen:
            return False
        self.seen.add(tid)
        self._seen_order.append(tid)
        if len(self._seen_order) > MAX_SEEN * 2:
            drop = len(self._seen_order) - MAX_SEEN
            for old in self._seen_order[:drop]:
                self.seen.discard(old)
            self._seen_order = self._seen_order[drop:]
        return True

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    async def _push(
        self,
        item: ThreadItem,
        detail: ThreadDetail | None,
        targets: list[tuple[str, dict[str, Any]]],
    ) -> int:
        limit = int(self._cfg("content_limit", 300) or 300)
        text = format_push_text(item, detail, content_limit=limit)
        images: list[str] = []
        if (
            detail is not None
            and detail.visible
            and bool(self._cfg("send_images", True))
        ):
            images = _pick_sendable_images(detail.images)

        chain = self._build_chain(text, images)
        ok = 0
        for umo, _sub in targets:
            # 有些平台（QQ 官方 API）在缺少会话场景记录时会静默丢弃消息但返回成功，
            # 所以每次推送前都补一次登记，避免“日志说已推送、实际没发出去”。
            self._restore_qq_scenes([umo])
            try:
                sent = await self.context.send_message(umo, chain)
                if sent:
                    ok += 1
                else:
                    logger.warning(f"[keylol] 推送失败（会话不可用）: {umo}")
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[keylol] 推送到 {umo} 出错: {exc}")
            await asyncio.sleep(0.6)
        if ok:
            logger.info(f"[keylol] 已推送《{item.title}》到 {ok} 个会话")
        return ok

    def _build_chain(self, text: str, images: list[str]):
        from astrbot.api.event import MessageChain

        chain = MessageChain()
        chain.message(text)
        for url in images:
            try:
                chain.chain.append(Comp.Image.fromURL(url))
            except Exception as exc:  # noqa: BLE001 - 单张图挂了不影响整条推送
                logger.debug(f"[keylol] 跳过无效图片 {url}: {exc}")
        return chain

    # ------------------------------------------------------------------ #
    # 指令
    # ------------------------------------------------------------------ #
    @filter.command("其乐添加", alias={"kladd"})
    async def cmd_add(self, event: AstrMessageEvent):
        """订阅其乐版块：/其乐添加 161 [关键字...]"""
        await self._ensure_started()
        args = self._split_args(event)
        if not args:
            yield event.plain_result(
                "用法：/其乐添加 <版块ID或名称> [关键字...]\n"
                "例如：/其乐添加 161\n"
                "　　　/其乐添加 热点聚焦 DLC\n"
                "常用版块："
                + "、".join(f"{fid}={n}" for fid, n in list(KNOWN_FORUMS.items())[:6])
            )
            event.stop_event()
            return
        raw = args[0]
        keywords = [a for a in args[1:] if a]
        fid, name = self._resolve_forum(raw)
        if fid is None:
            yield event.plain_result(
                f"找不到版块「{raw}」。可以用数字 fid，或 /其乐版块 查看全部。"
            )
            event.stop_event()
            return
        umo = event.unified_msg_origin
        meta = self.store.get(umo)
        meta["platform"] = event.get_platform_id()
        meta["type"] = "GroupMessage" if event.get_group_id() else "FriendMessage"
        meta["label"] = self._session_label(event)
        _ok, msg = self.store.add(umo, fid, name or "", keywords)
        await self._save_state()
        yield event.plain_result(
            f"✅ {msg}\n关键字：{'、'.join(keywords) or '无（推送全部新帖）'}"
        )
        event.stop_event()

    @filter.command("其乐删除", alias={"kldel"})
    async def cmd_remove(self, event: AstrMessageEvent):
        """取消订阅：/其乐删除 <fid>"""
        await self._ensure_started()
        args = self._split_args(event)
        if not args:
            subs = self.store.subs_of(event.unified_msg_origin)
            if not subs:
                yield event.plain_result("当前会话还没有订阅任何版块。")
            else:
                yield event.plain_result(
                    "当前订阅："
                    + "、".join(str(s.get("fid")) for s in subs)
                    + "\n用法：/其乐删除 <fid>"
                )
            event.stop_event()
            return
        fid, _name = self._resolve_forum(args[0])
        if fid is None:
            yield event.plain_result(f"无效的版块「{args[0]}」。")
            event.stop_event()
            return
        if self.store.remove(event.unified_msg_origin, fid):
            await self._save_state()
            yield event.plain_result(
                f"✅ 已取消订阅 {guess_forum_name(fid)}（fid={fid}）"
            )
        else:
            yield event.plain_result(f"当前会话没有订阅 fid={fid}。")
        event.stop_event()

    @filter.command("其乐列表", alias={"kllist", "其乐订阅"})
    async def cmd_list(self, event: AstrMessageEvent):
        """查看当前会话的订阅。"""
        await self._ensure_started()
        subs = self.store.subs_of(event.unified_msg_origin)
        if not subs:
            yield event.plain_result(
                "当前会话还没有订阅。\n用 /其乐添加 161 订阅热点聚焦，/其乐帮助 看全部指令。"
            )
            event.stop_event()
            return
        lines = [f"📋 当前会话订阅（{len(subs)} 个版块）"]
        for sub in subs:
            fid = sub.get("fid")
            name = sub.get("name") or guess_forum_name(int(fid))
            kws = sub.get("keywords") or []
            lines.append(
                f"· {name}（fid={fid}）"
                + (f"　关键字：{'、'.join(kws)}" if kws else "")
            )
        lines.append("")
        lines.append(f"⏱ 轮询间隔 {self._interval()} 秒")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    @filter.command("其乐测试", alias={"kltest"})
    async def cmd_test(self, event: AstrMessageEvent):
        """立刻拉一条帖子预览推送效果：/其乐测试 [fid]"""
        await self._ensure_started()
        args = self._split_args(event)
        fid: int | None = None
        if args:
            fid, _ = self._resolve_forum(args[0])
        else:
            subs = self.store.subs_of(event.unified_msg_origin)
            if subs:
                try:
                    fid = int(subs[0].get("fid"))
                except (TypeError, ValueError):
                    fid = None
        if fid is None:
            fid = 161
        assert self._client is not None
        try:
            items = await self._client.fetch_rss(fid)
        except KeylolError as exc:
            yield event.plain_result(f"❌ 拉取版块 {fid} 失败：{exc}")
            event.stop_event()
            return
        if not items:
            yield event.plain_result(
                f"版块 {fid} 暂时没有可用的帖子（可能 RSS 未开放）。"
            )
            event.stop_event()
            return
        item = items[0]
        detail: ThreadDetail | None = None
        try:
            detail = await self._client.fetch_thread(item.tid)
        except KeylolError as exc:
            logger.debug(f"[keylol] 测试抓详情失败: {exc}")
        text = format_push_text(
            item, detail, content_limit=int(self._cfg("content_limit", 300) or 300)
        )
        images = (
            _pick_sendable_images(detail.images)
            if detail is not None and detail.visible
            else []
        )
        chain = self._build_chain("🧪 推送效果预览\n\n" + text, images)
        yield event.chain_result(chain.chain)
        event.stop_event()

    @filter.command("其乐状态", alias={"klstatus"})
    async def cmd_status(self, event: AstrMessageEvent):
        """查看推送服务状态。"""
        await self._ensure_started()
        subs = self.store.subs_of(event.unified_msg_origin)
        last = (
            time.strftime("%m-%d %H:%M", time.localtime(self._last_run))
            if self._last_run
            else "尚未运行"
        )
        lines = [
            "📡 其乐推送服务状态",
            f"· 运行中：{'是' if self._running else '否'}",
            f"· 轮询间隔：{self._interval()} 秒",
            f"· 上次轮询：{last}",
            f"· 订阅会话：{len(self.store.data)} 个",
            f"· 已记录帖子：{len(self.seen)} 条",
            f"· 本次启动已推送：{self._push_count} 条",
            f"· 当前会话订阅：{len(subs)} 个版块",
        ]
        if self._last_error:
            lines.append(f"· 最近错误：{shorten(self._last_error, 120)}")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    @filter.command("其乐版块", alias={"klforum"})
    async def cmd_forums(self, event: AstrMessageEvent):
        """列出可订阅的版块。"""
        await self._ensure_started()
        yield event.plain_result("⏳ 正在拉取版块列表…")
        event.stop_event()
        names = dict(KNOWN_FORUMS)
        try:
            fetched = await self._client.fetch_forum_index()
            if fetched:
                names.update(fetched)
                self._forum_names.update(fetched)
        except KeylolError as exc:
            logger.debug(f"[keylol] 拉取版块列表失败: {exc}")
        ordered = sorted(names.items(), key=lambda kv: kv[0])
        lines = ["📚 其乐版块列表（fid=名称）"]
        for fid, name in ordered:
            lines.append(f"{fid}={name}")
        lines.append("")
        lines.append("订阅：/其乐添加 <fid>；带关键字：/其乐添加 161 DLC")
        yield event.plain_result("\n".join(lines))

    @filter.command("其乐帮助", alias={"klhelp", "其乐"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看帮助。"""
        await self._ensure_started()
        text = (
            "📮 其乐论坛推送插件\n"
            "\n"
            "· /其乐添加 <fid或版块名> [关键字...]　订阅版块\n"
            "· /其乐删除 <fid>　　取消订阅\n"
            "· /其乐列表　　查看当前会话订阅\n"
            "· /其乐版块　　列出全部版块\n"
            "· /其乐测试 [fid]　　立即预览一条推送\n"
            "· /其乐状态　　查看服务状态\n"
            "\n"
            "群里和私聊都可以订阅，订阅后由机器人主动推送新帖。"
        )
        yield event.plain_result(text)
        event.stop_event()

    # ------------------------------------------------------------------ #
    # 指令工具
    # ------------------------------------------------------------------ #
    async def _ensure_started(self) -> None:
        if not self._running:
            await self._startup()

    def _split_args(self, event: AstrMessageEvent) -> list[str]:
        text = (event.message_str or "").strip()
        if not text:
            return []
        # 去掉开头的指令名本身
        parts = text.split()
        if parts and parts[0].lstrip("/") in {
            "其乐添加",
            "kladd",
            "其乐删除",
            "kldel",
            "其乐列表",
            "kllist",
            "其乐订阅",
            "其乐测试",
            "kltest",
            "其乐状态",
            "klstatus",
            "其乐版块",
            "klforum",
            "其乐帮助",
            "klhelp",
            "其乐",
        }:
            parts = parts[1:]
        return parts

    def _session_label(self, event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if group_id:
            return f"群 {group_id}"
        return f"私聊 {event.get_sender_id()}"

    def _resolve_forum(self, raw: str) -> tuple[int | None, str]:
        raw = (raw or "").strip()
        if not raw:
            return None, ""
        if raw.isdigit():
            fid = int(raw)
            return fid, self._forum_names.get(fid) or guess_forum_name(fid)
        for fid, name in self._forum_names.items():
            if name == raw:
                return fid, name
        for fid, name in self._forum_names.items():
            if raw in name:
                return fid, name
        return None, ""
