"""其乐论坛 (Keylol) 数据抓取与解析。

本模块不依赖 AstrBot，可以脱离插件框架单独导入做单元测试。
解析全部基于标准库（re / html.parser / xml.etree），不引入 bs4、lxml 等
AstrBot 内置环境里没有的依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import ClassVar

import aiohttp

BASE_URL = "https://keylol.com"
RSS_URL = BASE_URL + "/forum.php?mod=rss&fid={fid}&auth=0"
THREAD_URL = BASE_URL + "/t{tid}-1-1"
FORUM_URL = BASE_URL + "/f{fid}-1"
INDEX_URL = BASE_URL + "/forum.php"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

# 北京时间（论坛以 UTC 存储 pubDate，展示时统一换算到东八区）
CST = timezone(timedelta(hours=8))

# 常见版块，作为 fid -> 名称 的内置映射；运行时可用 forum.php 首页刷新。
KNOWN_FORUMS: dict[int, str] = {
    140: "社区公告",
    161: "热点聚焦",
    319: "福利放送",
    234: "购物心得",
    271: "慈善包",
    257: "华语汉化",
    127: "平台工具",
    235: "成就指南",
    129: "游戏互鉴",
    254: "分享互赠",
    301: "平台技术问题",
}


class KeylolError(Exception):
    """抓取/解析其乐过程中的异常。"""


# --------------------------------------------------------------------------- #
# 文本清洗
# --------------------------------------------------------------------------- #


class _TextExtractor(HTMLParser):
    """把帖子正文 HTML 抓成纯文本，同时收集图片地址。"""

    _BLOCK_TAGS: ClassVar[set[str]] = {
        "br",
        "p",
        "div",
        "tr",
        "li",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "table",
        "section",
        "article",
    }
    _SKIP_TAGS: ClassVar[set[str]] = {"script", "style", "iframe", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self.images: list[str] = []

    # -- HTMLParser 回调 ---------------------------------------------------- #
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")
        elif tag == "img":
            self._collect_image(dict(attrs))

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._parts.append(data)

    # -- 内部 --------------------------------------------------------------- #
    def _collect_image(self, attrs: dict[str, str | None]) -> None:
        # 其乐帖子图片是懒加载：真实地址在 file 属性，src 只是占位图。
        for key in ("file", "zoomfile", "data-src", "src"):
            url = attrs.get(key)
            if not url:
                continue
            url = url.strip()
            if not url.startswith("http"):
                continue
            if "/static/image/" in url:
                continue
            if url not in self.images:
                self.images.append(url)
            return

    def text(self) -> str:
        raw = "".join(self._parts)
        # 统一换行，去掉纯空白行，压缩连续空行
        lines = [ln.strip() for ln in raw.replace("\r", "\n").split("\n")]
        out: list[str] = []
        for line in lines:
            if not line:
                if out and out[-1] != "":
                    out.append("")
                continue
            out.append(line)
        return "\n".join(out).strip()


def html_to_text(html: str) -> tuple[str, list[str]]:
    """HTML -> (纯文本, 图片地址列表)。"""
    if not html:
        return "", []
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - 脏 HTML 不应让整个推送失败
        return "", []
    return parser.text(), parser.images


_RE_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_RE_TAG = re.compile(r"<[^>]+>")

# 帖子正文里需要整块丢弃的“噪声”：广告位、Steam 商店控件、脚本、内嵌框架。
_DROP_CLASSES = {"rnd_ai_pr", "rnd_ad", "ad", "advert"}
_DROP_TAGS = {"script", "style", "iframe", "noscript", "object", "embed"}


def _should_drop(tag: str, attrs: dict[str, str]) -> bool:
    if tag in _DROP_TAGS:
        return True
    classes = set((attrs.get("class") or "").split())
    if classes & _DROP_CLASSES:
        return True
    style = (attrs.get("style") or "").replace(" ", "")
    # 其乐正文顶部的 Steam 商店工具条：<span style="font-size: 10px">Steam商店，…</span>
    return tag == "span" and "font-size:10px" in style


class _BlockStripper(HTMLParser):
    """按整块丢弃指定的 HTML 子树，保证嵌套标签不会把外层一起吃掉。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._out: list[str] = []
        self._depth = 0  # >0 表示正处于被丢弃的子树里
        self._stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._depth:
            if tag in _VOID_TAGS:
                return
            self._depth += 1
            self._stack.append(tag)
            return
        if _should_drop(tag, dict(attrs)):
            if tag not in _VOID_TAGS:
                self._depth = 1
                self._stack = [tag]
            return
        self._out.append(self.get_starttag_text() or f"<{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._depth:
            return
        if _should_drop(tag, dict(attrs)):
            return
        self._out.append(self.get_starttag_text() or f"<{tag}/>")

    def handle_endtag(self, tag: str) -> None:
        if self._depth:
            if self._stack and self._stack[-1] == tag:
                self._stack.pop()
            self._depth -= 1
            return
        self._out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._depth:
            self._out.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self._depth:
            self._out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._depth:
            self._out.append(f"&#{name};")

    def result(self) -> str:
        return "".join(self._out)


_VOID_TAGS = {
    "br",
    "img",
    "hr",
    "input",
    "meta",
    "link",
    "area",
    "base",
    "col",
    "source",
    "track",
    "wbr",
}


def strip_noise(html: str) -> str:
    """去掉广告位、Steam 商店工具条、iframe、脚本等无关内容。"""
    if not html:
        return ""
    parser = _BlockStripper()
    try:
        parser.feed(html)
        parser.close()
        return parser.result()
    except Exception:  # noqa: BLE001
        return html


def strip_steam_widgets(text: str) -> str:
    """正文顶部常有一行“Steam商店，Steam评测区｜…”的链接工具条，转成纯文本后单独清理。"""
    lines = []
    for line in text.split("\n"):
        if line.count("｜") + line.count("|") >= 3 and (
            "Steam商店" in line or "SteamDB" in line
        ):
            continue
        if line in {"点击隐藏", "展开", "收起", "隐藏"}:
            continue
        lines.append(line)
    return "\n".join(lines)


def clean_bbcode(text: str) -> str:
    """把 Discuz BBCode 转为可读纯文本（RSS 的 description 里大量存在）。"""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Steam 商店框：[sframe]APPID[/sframe]
    text = re.sub(
        r"\[sframe\]([^\[\]]+)\[/sframe\]",
        lambda m: (
            f"🎮 Steam 商店：https://store.steampowered.com/app/{m.group(1).strip()}/"
        ),
        text,
    )
    # 折叠块：保留折叠标题作为小标题
    text = re.sub(
        r"\[collapse(?:=([^\]]*))?\]",
        lambda m: f"\n【{m.group(1).strip()}】\n" if m.group(1) else "\n",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\[/collapse\]", "\n", text, flags=re.IGNORECASE)
    # 列表项
    text = re.sub(r"\[\*\]", "• ", text)
    # 图片
    text = re.sub(
        r"\[img[^\]]*\].*?\[/img\]", "〔图片〕", text, flags=re.DOTALL | re.IGNORECASE
    )
    # 链接
    text = re.sub(
        r"\[url=([^\]]+)\](.*?)\[/url\]",
        lambda m: f"{m.group(2).strip()}（{m.group(1).strip()}）",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"\[url\](.*?)\[/url\]", r"\1", text, flags=re.DOTALL | re.IGNORECASE)
    # 其余成对/自闭合标签一律去掉
    text = re.sub(
        r"\[/?(?:b|i|u|s|color|size|font|align|quote|code|table|tr|td|"
        r"attach|media|flash|hide|free|indent|float|list|hr|p|br)[^\]]*\]",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # 兜底：任何残留的 [xxx] / [/xxx]
    text = re.sub(r"\[/?[a-zA-Z*][^\[\]]{0,40}\]", "", text)
    text = _RE_TAG.sub("", text)
    lines = [ln.strip() for ln in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(line)
    return "\n".join(out).strip()


def shorten(text: str, limit: int) -> str:
    """按字数截断，超长补省略号。limit <= 0 表示不截断。"""
    text = (text or "").strip()
    if limit and limit > 0 and len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


# --------------------------------------------------------------------------- #
# AI 摘要
# --------------------------------------------------------------------------- #

# 送进模型的正文上限：再长既费 token，也不会让摘要更准
SUMMARY_INPUT_LIMIT = 4000

SUMMARY_SYSTEM_PROMPT = (
    "你是一个论坛帖子摘要助手。请用简体中文，把用户给出的论坛帖子概括成一段话。"
    "要求：只陈述帖子本身讲了什么，不评论、不评价、不寒暄；"
    "保留关键信息（游戏名、版本、价格、时间、活动规则、作者观点等）；"
    "不要使用「本文」「这篇文章」「楼主」这类元叙述；"
    "不要使用 Markdown、列表、标题或代码块；直接输出一段连续的文字。"
    "字数必须严格控制在上限之内，宁可少写也不要超出，超出的部分会被直接截掉。"
)


def build_summary_prompt(
    item: ThreadItem, detail: ThreadDetail | None = None, *, limit: int = 150
) -> str:
    """把帖子内容拼成送给模型的提示词。

    纯函数，不依赖 AstrBot，方便脱离框架做单元测试。
    """
    content = ""
    if detail is not None and detail.visible and detail.content:
        content = detail.content
    elif item.description:
        content = item.description
    content = shorten(content, SUMMARY_INPUT_LIMIT)

    forum = item.forum_name or (detail.forum_name if detail else "")
    parts = [f"版块：{forum}" if forum else "", f"标题：{item.title.strip()}"]
    if content:
        parts.append(f"正文：\n{content}")
    else:
        parts.append("正文：（无正文，仅有标题）")
    target = max(20, int(limit or 150))
    parts.append(f"请用不超过 {target} 个汉字概括这段内容。")
    return "\n".join(part for part in parts if part)


def clean_summary(text: str, limit: int = 0) -> str:
    """清洗模型输出：去掉引号、前缀、「摘要：」这类包装，再按需截断。"""
    text = (text or "").strip()
    if not text:
        return ""
    # 模型常把整段包在引号或「摘要：」前缀里
    text = re.sub(r"^(?:摘要|总结|概括|内容摘要)\s*[:：]\s*", "", text)
    text = text.strip().strip("\"'“”‘’「」『』")
    # 折行压平，避免把模型输出的分点原样推给用户
    text = re.sub(r"\s*\n+\s*", " ", text).strip()
    text = re.sub(r"[ \t]{2,}", " ", text)
    if limit and len(text) > limit:
        # 优先在句末标点处收尾，避免摘要以半句话被切断
        head = text[:limit]
        cut = max(head.rfind(mark) for mark in "。！？；")
        if cut >= limit * 0.6:
            return head[: cut + 1]
        return shorten(text, limit)
    return text


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class ThreadItem:
    """RSS / 列表页里的一条帖子摘要。"""

    tid: str
    title: str
    url: str
    forum_fid: int = 0
    forum_name: str = ""
    author: str = ""
    published: datetime | None = None
    description: str = ""
    category: str = ""

    def keywords_blob(self) -> str:
        return f"{self.title}\n{self.description}\n{self.category}"


@dataclass
class ThreadDetail:
    """帖子详情页解析结果。"""

    tid: str
    url: str
    title: str = ""
    forum_fid: int = 0
    forum_name: str = ""
    author: str = ""
    posted_at: str = ""
    content: str = ""
    images: list[str] = field(default_factory=list)
    visible: bool = True


# --------------------------------------------------------------------------- #
# 解析：RSS
# --------------------------------------------------------------------------- #


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _parse_rfc822(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def extract_tid(text: str) -> str:
    """从 `t1054390-1-1` / `tid=1054390` / 纯数字里取帖子 tid。"""
    text = (text or "").strip()
    m = re.search(r"(?:^|[/=?&])t(\d+)-", text)
    if m:
        return m.group(1)
    m = re.search(r"[?&]tid=(\d+)", text)
    if m:
        return m.group(1)
    m = re.search(r"(?:^|\D)(\d{4,})(?:\D|$)", text)
    return m.group(1) if m else ""


def parse_rss(xml_text: str, fid: int = 0) -> list[ThreadItem]:
    """解析其乐官方 RSS，返回帖子摘要列表（按 RSS 顺序，新帖在前）。"""
    from xml.etree import ElementTree as ET

    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as exc:
        raise KeylolError(f"RSS 解析失败：{exc}") from exc

    channel = None
    for node in root.iter():
        if _strip_ns(node.tag) == "channel":
            channel = node
            break
    if channel is None:
        raise KeylolError("RSS 缺少 channel 节点")

    forum_name = ""
    for child in channel:
        if _strip_ns(child.tag) == "title" and child.text:
            forum_name = child.text.replace("其乐 Keylol - ", "").strip()
            break

    items: list[ThreadItem] = []
    for child in channel:
        if _strip_ns(child.tag) != "item":
            continue
        data: dict[str, str] = {}
        for field_node in child:
            name = _strip_ns(field_node.tag)
            if name in {
                "title",
                "link",
                "description",
                "category",
                "author",
                "pubDate",
                "guid",
            }:
                data[name] = (field_node.text or "").strip()
        tid = extract_tid(data.get("guid") or data.get("link", ""))
        if not tid:
            continue
        items.append(
            ThreadItem(
                tid=tid,
                title=_unescape(data.get("title", "")),
                url=data.get("link") or THREAD_URL.format(tid=tid),
                forum_fid=fid,
                forum_name=forum_name,
                author=data.get("author", ""),
                published=_parse_rfc822(data.get("pubDate", "")),
                description=clean_bbcode(data.get("description", "")),
                category=data.get("category", ""),
            )
        )
    return items


# --------------------------------------------------------------------------- #
# 解析：版块列表页
# --------------------------------------------------------------------------- #

_RE_TBODY = re.compile(r'<tbody id="normalthread_(\d+)"[^>]*>(.*?)</tbody>', re.DOTALL)
_RE_LIST_TITLE = re.compile(
    r'<a href="t\d+-1-1"[^>]*class="s xst"[^>]*>(.*?)</a>', re.DOTALL
)
_RE_LIST_TITLE_ALT = re.compile(r'class="s xst"[^>]*>(.*?)</a>', re.DOTALL)
_RE_LIST_CATEGORY = re.compile(r"<em>\[<a[^>]*>(.*?)</a>\]</em>", re.DOTALL)
_RE_LIST_AUTHOR = re.compile(
    r'<cite class="threadlist-reply-username">\s*<a[^>]*>(.*?)</a>', re.DOTALL
)
_RE_LIST_TIME = re.compile(r'<em><span title="([^"]*)">', re.DOTALL)


def _unescape(text: str) -> str:
    import html as _html

    return _html.unescape(text or "").strip()


def parse_forum_list(html_text: str, fid: int = 0) -> list[ThreadItem]:
    """解析版块列表页（f{fid}-1），置顶帖排在前面。"""
    if not html_text:
        return []
    forum_name = ""
    m = re.search(r'<h1[^>]*class="xs2"[^>]*>(.*?)</h1>', html_text, re.DOTALL)
    if m:
        forum_name = _unescape(_RE_TAG.sub("", m.group(1)))

    items: list[ThreadItem] = []
    for match in _RE_TBODY.finditer(html_text):
        tid, block = match.group(1), match.group(2)
        title_m = _RE_LIST_TITLE.search(block) or _RE_LIST_TITLE_ALT.search(block)
        if not title_m:
            continue
        author_m = _RE_LIST_AUTHOR.search(block)
        category_m = _RE_LIST_CATEGORY.search(block)
        time_m = _RE_LIST_TIME.search(block)
        items.append(
            ThreadItem(
                tid=tid,
                title=_unescape(_RE_TAG.sub("", title_m.group(1))),
                url=THREAD_URL.format(tid=tid),
                forum_fid=fid,
                forum_name=forum_name,
                author=_unescape(author_m.group(1)) if author_m else "",
                category=_unescape(category_m.group(1)) if category_m else "",
                description=f"{(time_m.group(1) if time_m else '').strip()} 发表",
            )
        )
    return items


def parse_forum_index(html_text: str) -> dict[int, str]:
    """从论坛首页解析 fid -> 版块名。"""
    result: dict[int, str] = {}
    if not html_text:
        return result
    for m in re.finditer(r'href="f(\d+)-1"[^>]*>(.*?)</a>', html_text, re.DOTALL):
        fid = int(m.group(1))
        name = _unescape(_RE_TAG.sub("", m.group(2)))
        if name and fid not in result:
            result[fid] = name
    return result


# --------------------------------------------------------------------------- #
# 解析：帖子详情页
# --------------------------------------------------------------------------- #

_RE_POST_BODY = re.compile(
    r'<td class="t_f" id="postmessage_(\d+)"[^>]*>(.*?)</td>\s*</tr>', re.DOTALL
)
_RE_POST_BODY_LOOSE = re.compile(
    r'<td class="t_f" id="postmessage_(\d+)"[^>]*>(.*?)</td>', re.DOTALL
)
_RE_SUBJECT = re.compile(r'id="thread_subject"[^>]*title="([^"]*)"')
_RE_PAGE_TITLE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
# 面包屑里的当前版块：<a href="f161-1">热点聚焦</a><em>›</em>
_RE_CRUMB_FORUM = re.compile(r'href="f(\d+)-1"[^>]*>([^<]+)</a>\s*<em>›</em>')
_RE_AUTHORPOSTON = re.compile(r'id="authorposton\d+">(.*?)</em>', re.DOTALL)
_RE_AUTHI_NAME = re.compile(
    r'<div class="authi">\s*<a[^>]*class="xw1"[^>]*>(.*?)</a>', re.DOTALL
)
_RE_GUEST_HINT = ("请先登录", "绑定Steam", "抱歉，本帖要求阅读权限")


def parse_thread(html_text: str, url: str = "", tid: str = "") -> ThreadDetail:
    """解析帖子详情页首楼。"""
    if not html_text:
        raise KeylolError("帖子页面为空")
    tid = tid or extract_tid(url) or ""

    # 标题
    title = ""
    m = _RE_SUBJECT.search(html_text)
    if m:
        title = _unescape(m.group(1))
    forum_fid = 0
    forum_name = ""
    crumbs = _RE_CRUMB_FORUM.findall(html_text)
    if crumbs:
        forum_fid, forum_name = int(crumbs[-1][0]), _unescape(crumbs[-1][1])
    if not title:
        m = _RE_PAGE_TITLE.search(html_text)
        if m:
            parts = [p.strip() for p in _unescape(m.group(1)).split(" - ")]
            title = parts[0] if parts else ""
            if not forum_name and len(parts) > 1:
                forum_name = parts[1]

    # 首楼正文
    body_html = ""
    m = _RE_POST_BODY.search(html_text) or _RE_POST_BODY_LOOSE.search(html_text)
    if m:
        body_html = m.group(2)

    visible = True
    if not body_html:
        visible = False
    elif any(hint in body_html for hint in _RE_GUEST_HINT):
        text_probe, _ = html_to_text(strip_noise(body_html))
        if len(text_probe) < 60:
            visible = False

    body_html = strip_noise(body_html)
    content, images = html_to_text(body_html)
    content = strip_steam_widgets(content)

    author = ""
    m = _RE_AUTHI_NAME.search(html_text)
    if m:
        author = _unescape(_RE_TAG.sub("", m.group(1)))

    posted_at = ""
    m = _RE_AUTHORPOSTON.search(html_text)
    if m:
        posted_at = _unescape(_RE_TAG.sub("", m.group(1))).strip()
        posted_at = re.sub(r"\s+", " ", posted_at).replace("发表于", "").strip()

    return ThreadDetail(
        tid=tid,
        url=url or THREAD_URL.format(tid=tid),
        title=title,
        forum_fid=forum_fid,
        forum_name=forum_name,
        author=author,
        posted_at=posted_at,
        content=content,
        images=images,
        visible=visible,
    )


# --------------------------------------------------------------------------- #
# 网络请求
# --------------------------------------------------------------------------- #


class KeylolClient:
    """异步 HTTP 客户端。AstrBot 环境自带 aiohttp，不引入 requests。"""

    def __init__(
        self,
        *,
        cookie: str = "",
        user_agent: str = DEFAULT_UA,
        timeout: float = 20.0,
        proxy: str = "",
        retries: int = 2,
    ) -> None:
        self.cookie = (cookie or "").strip()
        self.user_agent = (user_agent or DEFAULT_UA).strip()
        self.timeout = float(timeout or 20.0)
        self.proxy = (proxy or "").strip() or None
        self.retries = max(1, int(retries or 1))
        self._session: aiohttp.ClientSession | None = None
        self._lock = None

    # -- session ------------------------------------------------------------ #
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            import asyncio

            self._lock = self._lock or asyncio.Lock()
            async with self._lock:
                if self._session is None or self._session.closed:
                    headers = {
                        "User-Agent": self.user_agent,
                        "Accept": (
                            "text/html,application/xhtml+xml,application/xml;q=0.9,"
                            "*/*;q=0.8"
                        ),
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                        "Referer": BASE_URL + "/forum.php",
                    }
                    if self.cookie:
                        headers["Cookie"] = self.cookie
                    self._session = aiohttp.ClientSession(
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                    )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # -- 通用请求 ------------------------------------------------------------ #
    async def get_text(self, url: str, *, encoding: str = "utf-8") -> str:
        import asyncio

        session = await self._get_session()
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                async with session.get(url, proxy=self.proxy) as resp:
                    if resp.status != 200:
                        raise KeylolError(f"HTTP {resp.status}: {url}")
                    raw = await resp.read()
                    return raw.decode(encoding, errors="replace")
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt + 1 < self.retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise KeylolError(f"请求失败：{url} ({last_exc})")

    # -- 业务接口 ------------------------------------------------------------ #
    async def fetch_rss(self, fid: int | str) -> list[ThreadItem]:
        fid_int = int(fid)
        text = await self.get_text(RSS_URL.format(fid=fid_int))
        return parse_rss(text, fid_int)

    async def fetch_thread(self, tid: str) -> ThreadDetail:
        url = THREAD_URL.format(tid=tid)
        text = await self.get_text(url)
        return parse_thread(text, url=url, tid=tid)

    async def fetch_forum_list(self, fid: int | str) -> list[ThreadItem]:
        fid_int = int(fid)
        text = await self.get_text(FORUM_URL.format(fid=fid_int))
        return parse_forum_list(text, fid_int)

    async def fetch_forum_index(self) -> dict[int, str]:
        text = await self.get_text(INDEX_URL)
        return parse_forum_index(text)


# --------------------------------------------------------------------------- #
# 消息格式化
# --------------------------------------------------------------------------- #


def format_time(dt: datetime | None) -> str:
    if dt is None:
        return ""
    try:
        return dt.astimezone(CST).strftime("%m-%d %H:%M")
    except (OSError, ValueError):
        return dt.strftime("%m-%d %H:%M")


def format_push_text(
    item: ThreadItem,
    detail: ThreadDetail | None = None,
    *,
    content_limit: int = 300,
    show_category: bool = True,
    summary: str = "",
    show_content: bool = True,
) -> str:
    """拼出一条推送消息的正文。

    传入 `summary` 时只展示 AI 摘要，不再展示帖子全文；
    摘要为空则回落到原来的正文截断逻辑。
    `show_content=False` 时既不展示摘要也不展示正文，只留标题与链接。
    """
    forum = item.forum_name or (detail.forum_name if detail else "")
    header = f"📮 其乐 · {forum}" if forum else "📮 其乐论坛新帖"

    lines: list[str] = [header]
    category = item.category or ""
    if show_category and category and category != forum:
        lines.append(f"🏷 {category}")
    # 论坛标题常自带书名号（如《冰汽时代2》DLC…），或者句中已经引了作品名
    # （如「科幻动作RPG《Zack Bishop…》Steam页面开放」），这两种情况都不再加外层书名号，
    # 否则会出现《《…》》或《…《…》…》这样嵌套的书名号。
    title = item.title.strip()
    has_bracket = any(mark in title for mark in "《》【】「」")
    lines.append(title if has_bracket else f"《{title}》")

    meta: list[str] = []
    author = item.author or (detail.author if detail else "")
    if author:
        meta.append(f"👤 {author}")
    when = format_time(item.published)
    if not when and detail and detail.posted_at:
        when = detail.posted_at
    if when:
        meta.append(f"🕘 {when}")
    if meta:
        lines.append("　".join(meta))

    if not show_content:
        pass
    elif summary:
        lines.append("")
        lines.append(f"🤖 {summary}")
    else:
        body = ""
        if detail is not None and detail.visible and detail.content:
            body = detail.content
        elif item.description:
            body = item.description
        body = shorten(body, content_limit)
        if body:
            lines.append("")
            lines.append(body)

    lines.append("")
    lines.append(f"🔗 {item.url}")
    return "\n".join(lines).strip()


def guess_forum_name(fid: int) -> str:
    return KNOWN_FORUMS.get(int(fid), f"版块{fid}")
