"""Web 端（需求 28 + Phase B #46~#53 / #17）：设置里一个开关就能开，仿qBittorrent。

用户原话：「目前仍然保留 win 端的 GUI，但是在设置中加一个『是否开启 Web 功能』
的开关，可以仿照 qBittorrent」。

qBittorrent Web UI 的要点，这里照搬：
- 默认**关闭**，要在设置里显式打开；
- 独立端口 + Web 账号密码，未登录一律 401；
- 局域网可访问（照片要能在手机上看），但**默认只绑本机**，
  用户主动改监听地址才对外；
- 绑定的是**同一个 Database / Config**，所以网页上看到的就是 GUI 里的实时数据，
  不需要任何同步。

实现刻意只用标准库 http.server：这个软件要在别人机器上跑，
再引入 Flask/FastAPI 就是给打包体积和依赖树加负担，而 Web 端只要
「看任务 / 看图 / 开关任务」这几个页面，标准库完全够。

=====================================================================
Phase B（B1~B9）把内联 HTML/JS 拆成了真前端资产
=====================================================================
以前整个界面是一段 `PAGE = '''…'''` 内联字符串 + 服务端拼 HTML。Phase B 之后：

- 前端资产在 :mod:`media_resource_manager.web`（index.v2.html / static/app
  / static/js/qr-login.js / manifest.webmanifest / sw.js / static/img），
  **已加入打包 datas**。旧版 SPA（index.html + static/css/app.css +
  static/js/app.js）已于 2026-10-29 物理删除，不再保留任何入口；
- 本模块只负责路由 + JSON API + 静态文件服务，不再拼 HTML。

这么拆的三个理由（都不是"更现代"这种虚话）：
1. **内联字符串没法做 PWA**：service worker 必须能被浏览器按 URL 取到，
   塞在 Python 字符串里就没法注册。
2. **内联 HTML 每加一个页面就复制一遍 CSS/JS**，改一处要改 N 处；
   分成文件后浏览器还能缓存（SW 缓存外壳，离线可开）。
3. **服务端拼 HTML 必须转义每个字段**，漏一个就是 XSS。
   现在页面是静态的、数据靠 JSON 过去，转义只发生在 JS 一处。

⚠ **打包铁律**：`web/` 目录必须写进 `build/pyinstaller.spec` 的 `datas`
（用 `os.path.isdir` 条件化，见 C8 的坑：不存在的 datas 源会让PyInstaller
直接中断打包）。少了这一步就是「代码里有、界面上 404」—— 正是
本项目反复出现的那类 bug。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import select
import shutil
import socket
import ssl
import sys
import tempfile
import datetime
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urlparse, urlsplit

# 2026-10-27：本模块此前**完全没有 logger** —— 我在安全校验的兜底分支里
# 写了 LOG.warning，结果 NameError 又被内层 except 吞掉，等于什么都没记。
# 统一给一个模块级 logger（与全项目同一个 mrm.* 命名空间）。
import logging as _logging

LOG = _logging.getLogger("mrm.webui")

from . import i18n
from . import zhsort
from .review import reasons as _reasons

DEFAULT_PORT = 8080
DEFAULT_USER = "admin"

# R144：给**外部 AI** 用的「网址 → 适配器规则」生成 Prompt 模板。
_ADAPTER_PROMPT_TEXT = """你是「Sweet 采集器」的适配器规则生成助手。

【目标网址】
【在这里粘贴你要采集的网站网址】
（我会把要采集的网址填在这里。若我另外给了栏目页/图册页网址，请一并参考。）

【输出格式】
只输出一个 JSON 代码块，不要解释、不要寒暄。字段写法如下：
{
  "name": "my_site",
  "base_url": "https://example.com/",
  "entry_urls": ["https://example.com/latest"],
  "allowed_domains": ["example.com"],
  "list_page_selectors": {"kind": "css", "value": "div.list"},
  "album_link_selectors": {"kind": "css", "value": "a.album"},
  "image_page_selectors": {"kind": "css", "value": "div.gallery"},
  "image_link_selectors": [{"kind": "css", "value": "img.photo"}],
  "original_image_attrs": ["src", "data-src", "data-original"],
  "image_url_replace": [{"from": "t.example.com", "to": "s.example.com"}],
  "next_page_selectors": {"kind": "css", "value": "a.next"},
  "paging": {"mode": "offset_json", "url": "{base}?offset={offset}&limit={limit}",
             "offset_start": 20, "limit": 20, "step": 20, "link_key": "g_url", "max_pages": 500},
  "max_depth": 3,
  "max_pages": 0,
  "delay": 1.0,
  "concurrency": 4,
  "proxy": null,
  "cookie": "",
  "user_agent": "",
  "note": "备注，可留空"
}

【选择器规范】
- 每个选择器的 kind 只能是 css / xpath / regex 三种之一，写别的会报错。
- list_page_selectors：列表页里圈住"这一页有哪些条目"的区域。
- album_link_selectors：列表页里指向图册详情页的链接；网站没有图册页就留空。
- image_page_selectors：图册详情页里装图的容器。
- image_link_selectors：最终下载用的图片链接或图片元素，可以写多条，最重要。
- next_page_selectors：翻到下一页的按钮；没有翻页就留空。
- paging：**站点没有可见翻页链接、但列表接口支持 offset 分页**时用（无限滚动站常见）。
  形如 {"mode": "offset_json", "url": "{base}?offset={offset}&limit={limit}",
  "offset_start": 20, "limit": 20, "step": 20, "link_key": "g_url"}：
  引擎会按 offset 递增去请求接口，把 link_key 指定的链接并入下一页队列。
  没有这种接口就整段删掉（不要留空对象）。
- original_image_attrs：从图片元素上取链接时依次尝试的属性名。
- image_url_replace：把缩略图域名换成原图域名的替换规则，可以留空。
- cookie：需要登录才能看到图时，整串 Cookie（`k=v; k2=v2`）。**不知道就留空**，
  不要编造 —— 乱填会让所有请求带上无效登录态，反而更容易被拦。
- user_agent：站点只认特定浏览器时填该 UA。**不确定就留空**（沿用全局设置）。

【必备字段（缺一不可）】
1. base_url 与 entry_urls 至少要有其中一个；
2. image_link_selectors（图片选择器），没有它采不到图；
3. next_page_selectors（翻页选择器）；
4. allowed_domains 留空也没关系，系统会自动拿 base_url 的域名填上。

【输出要求】
只输出规则本身（一个 JSON 代码块），不要解释，不要寒暄。
"""


def _as_bool(value, default: bool = False) -> bool:
    """把请求体里的开关值读成真/假。

    V0.0.6：裸 HTTP 接口不只自带网页在用。以前写 `bool(body.get("copy"))`
    或 `body.get("tag") is not False`，客户端只要传字符串 "false"，
    Python 里就是真 —— 明明没勾却复制双份、明明只想索引却强制打标。
    这里统一认常见写法，认不出来就用默认值。
    """
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "on", "y", "t"):
        return True
    if s in ("false", "0", "no", "off", "n", "f", ""):
        return False
    return bool(default)


def adapter_prompt_template() -> str:
    """R144：网页端「一键复制 Prompt」用的模板文本。

    ⚠ 为什么单点定义在后端、不让前端写死一份：桌面端「自动编写适配器」
    （``crawler/adapter_proposer.open_auto_gen``）和网页端这个按钮说的是
    同一件事 —— 把目标网址变成一条 ``AdapterRule``。话术写两份的话，
    字段一改就一边新一边旧，用户拿着过期模板只会白跑一趟。

    ⚠ 字段清单必须跟 ``crawler.rules.AdapterRule`` **逐字段对齐**：
    少写一个字段，AI 就不知道还能给；多写一个，``load_adapter`` 会静默
    丢掉，用户以为存进去了其实没有。
    """
    return _ADAPTER_PROMPT_TEXT

# B2 #47：服务端对分页参数的**硬上限**。前端传再大的 limit 也会被夹到这里。
# 为什么必须有上限：无限加载是"忘了加 limit"的最后一道防线，
# 一个手滑的 LIMIT 1000000 会把几万行塞进 JSON，手机直接卡死。
PAGE_LIMIT_MAX = 200
PAGE_LIMIT_DEFAULT = 100

# ==========================================================================
# 2026-10-29：标签计数缓存（10 万张库的必需件，不是优化）
# ==========================================================================
# 标签树是**每进一次图库就会问一次**，而全库聚合在 10 万行上要几十到几百毫秒。
# 缓存 30 秒，写标签时立刻清（见 _tag_cache_clear），所以「刚加的标签马上能看到」
# 与「翻页不卡」两头都占。桌面端不走这里（它有自己的内存索引）。
_TAG_CACHE: dict = {"ts": 0.0, "rows": None}
_TAG_CACHE_LOCK = threading.Lock()

#: 底栏概览缓存（见 _api_summary）。3 秒的陈旧度用户看不出来，
#: 但把「每 5 秒一次全表扫」降到「三分之一频率」。
# ⚠ 必须按「哪个数据库」分开缓存：测试里会在同一进程内起多个 WebServer、
# 各自挂不同的临时库，共用一个槽位会让后一个服务读到前一个库的数字
# （实测：test_summary_counts 拿到上一个库的 0，而它刚插了 3 张图）。
# 用 id(db) 当 key —— Database 实例在服务生命周期内不变。
_SUMMARY_CACHE: dict = {"ts": 0.0, "data": None, "key": None}
_SUMMARY_CACHE_LOCK = threading.Lock()


def _tag_cache_clear() -> None:
    """标签写入后必须清缓存 —— 否则用户加了标签、树里要等半分钟才出现。"""
    with _TAG_CACHE_LOCK:
        _TAG_CACHE["ts"] = 0.0
        _TAG_CACHE["rows"] = None


# B4 #49：桌面端 8 个顶级菜单 → Web 的 8 个视图。
# 顺序与桌面端一致（任务不在Web 里，所以从图库开始）。
#: #36 排序白名单：key → ORDER BY 子句。
#: time 是默认，与以前的 `id DESC` + `id<cursor` 键集分页**逐字节等价**
#: （老前端不带 sort 参数 ⇒ 行为一字不变）；name / size 走 OFFSET 分页 ——
#: 键集分页只能挂在排序键上，而 path / bytes 可空可重复，塞进单个 int
#: cursor 又脆又难验；本地单用户图库「翻页中途插入新图导致偏移」是可接受
#: 代价，且能被测试证明（静态数据集翻完不重不漏）。
_SORTS = {
    "time": "id DESC",
    "name": "COALESCE(path,'') ASC, id ASC",
    "size": "bytes DESC, id DESC",
    # V0.0.14：网页图库补齐桌面端早就有的「AI 分」「站点」两种排序。
    # 两者都走 OFFSET 分页（只有 time 走 id 游标），与 name/size 同理：
    # score 可空可重复、站点名来自另一张表，都塞不进单个 int cursor。
    # 末尾一律补 id 保证**全序**，否则同分/同站点的行翻页会重排。
    "score": "COALESCE(score,0) DESC, id DESC",
    "site": ("COALESCE((SELECT name FROM tasks WHERE tasks.id=images.task_id),'') "
             "ASC, id ASC"),
}


def _norm_sort(raw) -> str:
    """排序键白名单收窄：不认识的（含空）一律回落 time，绝不 500。"""
    s = str(raw or "").strip().lower()
    return s if s in _SORTS else "time"


def _ai_dev_eff(cfg) -> str:
    """生效的推理设备（auto/gpu/cpu）——与 config.ai_device_effective 同一口径。"""
    try:
        from .config import ai_device_effective
        return ai_device_effective(cfg)
    except Exception:
        return str(getattr(cfg, "ai_device", "auto") or "auto")


VIEWS = ("gallery", "pending", "quarantine", "dedup",
         "adapters", "cleanup", "settings", "logstats")


def image_action_sets(act: str, was_quarantined: bool) -> str:
    """R158：图片动作落到 `images` 两个归属标志上的 SET 子句（纯函数）。

    ⚠ 与桌面端 `review.queue.ReviewQueue._do_apply` 逐条对齐，两端任一处
    改口径都要同步改另一侧，否则同一张图在两端归属不同（R143 的老伤）：
      · like（审核通过）→ 出隔离区**回图库**：in_quarantine=0；
        桌面端 LIKE/SKIP 就是 `rec.in_quarantine = False`。
      · dislike（不喜欢）→ 移进隔离区：in_quarantine=1；
        桌面端走 `_isolate()`（文件 move + 置 in_quarantine=1）。
      · blacklist（拉黑）→ 既 blacklisted=1 也进隔离区；
        桌面端 ReviewDecision.BLACKLIST 就是 `blacklisted=True` + `_isolate`。

    ⚠ `needs_review` 一律清 0：三个动作都是"这件事已经定了"，不能再留在
    待审核队列里（`pending()` 的口径是 in_quarantine=1 AND needs_review=1，
    留着就会"点了通过还占着待审核"）。

    ⚠ `quarantined_at` 只在本来不在隔离区时写：已在隔离区的图再点一次
    不喜欢，把隔离时间刷新成现在属于伪造历史（隔离区清理/排序靠它）。

    ⚠ 返回的是 **SQL 片段**，不是参数化占位符 —— 值全是本函数内部的
    常量字面量，没有一处来自用户输入（`act` 已由调用方白名单收窄）。
    """
    q = bool(was_quarantined)
    if act == "like":
        return "needs_review=0, in_quarantine=0"
    if act == "blacklist":
        if q:
            return "blacklisted=1, needs_review=0"
        return ("blacklisted=1, needs_review=0, in_quarantine=1, "
                "quarantined_at=datetime('now')")
    # dislike（以及任何未来的"拒绝"语义动作）
    if q:
        return "needs_review=0"
    return ("needs_review=0, in_quarantine=1, "
            "quarantined_at=datetime('now')")

# B1 #46：水印/logo 的呈现规格（与桌面端一致：右下角小块，绝不铺满）。
WATERMARK_MAX_PX = 120
WATERMARK_OPACITY = 0.70      # ≈ 178/255，与 watermark.py 的 ALPHA=178 同口径


# ---------------------------------------------------------------- auth

def _token(user: str, pw: str) -> str:
    """Basic Auth 的比对值。存哈希不比明文，日志里也不会漏出口令。"""
    raw = f"{user}:{pw}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def check_auth(header: str, user: str, pw: str) -> bool:
    if not header:
        return False
    if not hmac.compare_digest(header.strip(), _token(user, pw)):
        return False
    return True


# B3 #48：会话 cookie 的签名与校验。
# 为什么要 cookie 会话而**继续沿用 Basic Auth** 做底层校验：
# 移动端浏览器弹的 Basic Auth 对话框体验很差（没法自定义文案、
# 登出后浏览器还会缓存凭据）。所以：
#   登录一次 → 拿一个 HMAC 签名的会话 token 存 cookie → 之后靠 cookie。
#   Basic Auth 仍然接受（老书签/脚本/curl 不用改）。
#
# token 载荷 = 用户名 + 过期时间戳 + HMAC，用配置里的密码当密钥。
# 改密码 → 密钥变 → 旧 cookie 全部失效（这正是我们要的）。
_COOKIE_NAME = "mrm_session"
_SESSION_TTL = 12 * 3600        # 12 小时：够用一天，跨夜自动失效

# ---- R154-F20：手机扫码登录 ----
# 订单原文：Web 登录页加二维码（内容=一次性 token 登录 URL，token 5 分钟
# 有效、一次性）；桌面端扫码授权确认（防恶意扫码需用户在看到请求后点同意）；
# token 走现有 session 机制；无手机场景不影响密码登录。
# 流程：登录页 POST /api/login/qrcode 签发 token → 前端显示二维码（内容=
# /qr-login?token=xxx 完整 URL，后端 segno 生成 SVG）→ 手机扫码开确认页 →
# 桌面端 GUI 弹窗授权（同意/拒绝，见 main_window 接线）→ 确认页轮询到
# approved 后 POST /api/login/qrcode/confirm → 复用 make_session 发 cookie
# → 跳转首页。一次性：confirm 后标记 used，再 confirm 一律拒绝。
_QR_TOKEN_TTL = 300.0        # 5 分钟
_QR_ISSUE_WINDOW = 60.0      # 签发限流窗口
_QR_ISSUE_MAX = 5            # 窗口内每 IP 最多签发次数（弹窗轰炸防线）

_QR_TOKENS: dict[str, dict] = {}
_QR_LOCK = threading.Lock()

# R152-E10：≥此大小的文件走分块流式 + Range，避免 read_bytes 整张读进内存。
_STREAM_THRESHOLD = 1024 * 1024   # 1MB
_FILE_CHUNK = 65536                # 流式分块大小

# ---------------------------------------------------------------------------
# R241/R242 · 重操作的后台作业表
# ---------------------------------------------------------------------------
# 表本身搬到了 :mod:`media_resource_manager.jobs`（与 HTTP 无关，单独可测）。
# 这里只留一层薄封装，让本文件里的调用点读起来还是 _job_*。
from . import jobs as _jobs_mod


def _job_new(kind: str) -> str:
    return _jobs_mod.new(kind)


def _job_progress(jid: str):
    return _jobs_mod.progress_cb(jid)


def _job_get(jid: str):
    return _jobs_mod.get(jid)


def _job_finish(jid: str, result=None, error: str = "", message: str = "") -> None:
    _jobs_mod.finish(jid, result=result, error=error, message=message)


# ---- R248：批量导入的运行态 ----
# 同一时刻只允许一个导入（两个线程同时写库 + 抢 CPU 只会互相拖慢，
# 这一点在 import_folder 里也有锁，这里是给 HTTP 层读的）。
# 取消用 Event：导入线程每一张都检查它，所以「停止」是**优雅停**，
# 不是杀进程 —— 已入库的保留、已打标的保留。
_IMPORT_LOCK = threading.Lock()
_IMPORT_STATE: dict = {"job": "", "cancel": None}
# V0.0.46：语义特征补算作业（独立于导入，但同样「一次只跑一个」）
_SEM_BACKFILL: dict = {"job": "", "cancel": None}

# R249：磁盘维护作业。同样「一次只跑一个」—— 转换/扫描/修复都要大量读盘，
# 同时跑两个只会互相拖慢，而且转 JPG 与修复图库同时改同一个库更危险。
# R251：插件推送 Cookie 的限速（同一 IP 的推送时刻 / 配对码错误计数）
_INGEST_LOCK = threading.Lock()
_INGEST_HITS: dict = {}
_INGEST_FAIL: dict = {}

_MAINT_LOCK = threading.Lock()
_MAINT_STATE: dict = {"job": "", "cancel": None}


def _qr_gc(now=None):
    """惰性清理过期 token。**必须在锁内遍历**（签发/确认并发）。"""
    now = time.time() if now is None else now
    with _QR_LOCK:
        for tok in [t for t, v in _QR_TOKENS.items()
                    if now - v["created_at"] > _QR_TOKEN_TTL]:
            _QR_TOKENS.pop(tok, None)


def approve_qr_token(token: str) -> bool:
    """桌面端 GUI 选择「同意」：token → approved。不存在/已用/已拒返回 False。"""
    _qr_gc()
    with _QR_LOCK:
        rec = _QR_TOKENS.get(token)
        if rec is None or rec["status"] in ("used", "rejected"):
            return False
        if time.time() - rec["created_at"] > _QR_TOKEN_TTL:
            _QR_TOKENS.pop(token, None)
            return False
        rec["status"] = "approved"
        return True


def reject_qr_token(token: str) -> bool:
    """桌面端 GUI 选择「拒绝」：token → rejected。"""
    _qr_gc()
    with _QR_LOCK:
        rec = _QR_TOKENS.get(token)
        if rec is None or rec["status"] == "used":
            return False
        rec["status"] = "rejected"
        return True


# 扫码登录确认页（/qr-login）。脚本走同源外链 /static/js/qr-login.js
# （CSP script-src 'self' 不允许内联脚本）。
_QR_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>扫码登录确认 - Sweet采集AI版</title>
<style>
body { font-family: system-ui, "PingFang SC", "Microsoft YaHei", sans-serif;
       margin: 0;
       display: flex; min-height: 100vh; align-items: center; justify-content: center; }
.card { border-radius: 12px; padding: 28px 32px; max-width: 420px;
        width: 92%; text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,.08); }
h1 { font-size: 18px; margin: 0 0 10px; }
.muted { font-size: 14px; }
.dot { width: 10px; height: 10px; border-radius: 50%; border: 2px solid currentColor;
       display: inline-block; margin-right: 6px; vertical-align: middle; }
</style>
</head>
<body>
<div class="card">
  <h1>扫码登录确认</h1>
  <p class="muted">设备正在请求登录本服务。</p>
  <p id="qr-msg"><span class="dot"></span>等待桌面端确认…</p>
  <p class="muted" id="qr-sub"></p>
</div>
<script src="/static/js/qr-login.js"></script>
</body>
</html>"""

# R152-E3：登录限流。原来 _api_login 对错误密码不设防——公网暴露时
# 匀速爆破没有任何成本。现在按 IP 记失败次数，达阈后指数退避锁定，
# 锁定期间直接 429（带 Retry-After），不再比对密码。
# ⚠ 状态挂 **WebServer 实例**（见 _WebHTTPServer.__init__），不挂类属性/
#   模块级——否则同一进程里多个用例共用一个计数器会互相锁死（假红），
#   且重启服务后计数不重置也不合理。
_LOGIN_MAX_FAILS = 5            # 连续失败多少次开始锁定
_LOGIN_BACKOFF_BASE = 30.0      # 首次锁定 30s，之后每失败一次翻倍
_LOGIN_BACKOFF_CAP = 900.0      # 单次锁定上限 15 分钟

# ---- R154-F6：SSE 统一推送通道 ----
#
# 订单原文：单 `/api/events`（SSE，带数据版本号），任务进度/图库计数/审核数
# 变化时推送；前端用事件源替代「每页各开轮询」；Docker 友好（反代关缓冲）；
# 事件**只送版本号/摘要**，详情仍走原 API。
#
# 三个定死的取舍（改之前先读）：
# ① **服务端轮询版本，不在写入点埋钩子**。要真埋钩子得改动 engine/db/
#    queue 十几处写入路径，任何一处漏埋就是「界面不刷」的幽灵 bug；
#   而这里每 2s 查一次 4 个 COUNT(*)，代价可以忽略。代价是**最多 2s 延迟**，
#   对本场景（下载进度）无感。
# ② **只送计数摘要，不送明细**。明细走 /api/tasks 等原接口，前端收到事件
#   自己决定要不要拉——这样 SSE 不会因为某个视图要的字段多就变成另一个
#   大 JSON 接口（也避免「SSE 里塞了列表，前端两份渲染逻辑」）。
# ③ **心跳 15s**：`_Handler.timeout = 30`（R152-E1 的 idle 闸）会在 30s
#   无数据时断连接；心跳必须比它密，否则浏览器每 30s 重连一次。
SSE_POLL_SEC = 2.0              # 版本检测间隔（也是推送延迟上界）
SSE_HEARTBEAT_SEC = 15.0        # 心跳间隔，必须 < _Handler.timeout
SSE_RETRY_MS = 3000             # 断线后浏览器重连间隔（写进 retry: 帧）
SSE_MAX_CLIENTS = 8             # 同时挂着的 SSE 连接上限（见并发闸注释）
SSE_MAX_SEC = 1800.0            # 单条连接最长 30 分钟，之后主动关、浏览器重连


def make_session(user: str, pw: str, now: float | None = None) -> str:
    ts = int(now if now is not None else time.time())
    payload = f"{user}:{ts}"
    sig = hmac.new(pw.encode("utf-8"), payload.encode("utf-8"),
                   hashlib.sha256).hexdigest()[:32]
    return f"{payload}:{sig}"


def verify_session(token: str, user: str, pw: str,
                   now: float | None = None,
                   ttl: float | None = None) -> bool:
    """校验会话 token。**任何一步不过都返回 False，绝不抛异常** ——
    它跑在 HTTP 处理器的路径上，抛异常会变成 500 而不是 401。"""
    try:
        u, ts, sig = token.split(":", 2)
        if u != user:
            return False
        exp = int(ts)
        cur = int(now if now is not None else time.time())
        # 编号 27：ttl 由调用方按配置传入；不传则用模块默认（12 小时）。
        _life = float(ttl) if ttl and ttl > 0 else _SESSION_TTL
        if cur > exp + _life:                 # 过期
            return False
        payload = f"{u}:{ts}"
        want = hmac.new(pw.encode("utf-8"), payload.encode("utf-8"),
                        hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(sig, want)
    except Exception:
        return False


def event_snapshot(db, queue=None) -> dict:
    """R154-F6：数据摘要 —— SSE 推送的**全部**内容。

    ⚠ 四条硬规矩：
    1. **绝不抛异常**。它跑在 SSE 长连接线程里，抛了就是整条流断掉
       （浏览器表现为「事件源一直重连」），而调用方拿不到堆栈。
       任何一步出错都退化成上一个可用值 / 0。
    2. **SQL 与 `_api_summary` 逐字一致**。口径分叉的话，顶栏徽标与
       SSE 推的计数会互相打架，用户看到「推了变化但数字没变」。
       `tests/test_r172_guards.py` 里有条护栏真跑两边比对，别手改一边。
    3. **不发明新统计**。要新指标先去 stats/learning_stats 找，没有才加
       （订单纪律：复用现有口径）。
    4. **不带任何明细**（任务名/网址/图片路径一概不出）——SSE 是常驻连接，
       泄露面比一次性接口大；详情请前端自己打 /api/*。
    """
    out = {"tasks": 0, "images": 0, "pending": 0, "quarantine": 0,
           "gallery": 0, "failed_urls": 0, "progress": []}
    if db is not None:
        try:
            # R154-F16：模板（is_template=1）不是任务，顶栏计数不带它。
            # ⚠ 与 _api_summary 的任务计数 SQL **逐字一致**（R172 护栏比口径）。
            out["tasks"] = int(db.query(
                "SELECT COUNT(*) c FROM tasks WHERE is_template=0")[0]["c"])
            out["images"] = int(db.query(
                "SELECT COUNT(*) c FROM images "
                "WHERE in_quarantine=0 AND blacklisted=0")[0]["c"])
            out["pending"] = int(db.query(
                "SELECT COUNT(*) c FROM images WHERE needs_review=1")[0]["c"])
            out["quarantine"] = int(db.query(
                "SELECT COUNT(*) c FROM images "
                "WHERE in_quarantine=1 AND needs_review=0")[0]["c"])
            # R167：导航「图库」角标 = **图库视图同款 WHERE**
            # （与 `_api_view` 的 gallery 映射逐字一致，也与桌面端
            #  `_nav_badge_counts()['gallery']` 一致）—— 角标数字与点进去
            # 看到的张数必须是同一个数。
            out["gallery"] = int(db.query(
                "SELECT COUNT(*) c FROM images "
                "WHERE in_quarantine=0 AND blacklisted=0 "
                "AND path IS NOT NULL")[0]["c"])
        except Exception:
            pass
    # F7 累计失败 URL 数（R170 建表）。老库/异常一律当 0，不影响其余字段。
    try:
        if hasattr(db, "failed_url_count"):
            out["failed_urls"] = int(db.failed_url_count() or 0)
    except Exception:
        pass
    # V0.0.18：通知最大 id —— 新通知一落库版本号就变，顶栏铃铛实时亮。
    # 老库没这张表时退化成 0，不影响其余字段（同上面的失败 URL 纪律）。
    try:
        from . import notify as _notify

        out["notify_id"] = _notify.since_id(db)
    except Exception:
        out["notify_id"] = 0
    # 运行中任务的进度：**只存在于内存**（queue.reports），DB 里没有进度列。
    # 少了这一段，任务跑着跑着界面就不动了 —— 恰恰是 SSE 最该覆盖的场景。
    # 排序后拼字符串：dict 遍历顺序若不稳定，同一状态会算出不同版本号
    # ⇒ 前端收到「变了」但其实没变，白拉一次接口。
    try:
        reports = getattr(queue, "reports", None) or {}
        prog = []
        for tid in sorted(reports, key=lambda x: str(x)):
            r = reports[tid] or {}
            prog.append([
                str(tid),
                int(r.get("pages") or 0),
                int(r.get("downloaded") or 0),
                int(r.get("failed") or 0),
            ])
        out["progress"] = prog
    except Exception:
        out["progress"] = []
    return out


def event_version(db, queue=None) -> str:
    """R154-F6：数据版本号 —— 摘要相同则版本号相同（内容寻址）。

    用内容寻址而不是自增序号，是因为**服务端可能重启**：自增号重启归零，
    前端 `since` 反而变成「未来版本」⇒ 永远等不到变化。内容寻址天然幂等，
    也让 `?since=` 可以直接比对。

    ⚠ 不做 hash：明文版本号在浏览器 DevTools 里一眼能看出「是哪一项变了」，
    排查问题时值千金；hash 只能告诉你「变了」。
    """
    s = event_snapshot(db, queue)
    try:
        prog = ";".join(f"{p[0]}:{p[1]}/{p[2]}/{p[3]}" for p in s["progress"])
    except Exception:
        prog = ""
    return (f"t{s['tasks']}.i{s['images']}.p{s['pending']}"
            f".q{s['quarantine']}.f{s['failed_urls']}"
            f".n{s.get('notify_id', 0)}.g[{prog}]")


def _sse_slot_acquire(server, limit: int = SSE_MAX_CLIENTS) -> bool:
    """占一个 SSE 连接槽位。**拿不到返回 False，绝不阻塞等待** ——
    等待会让请求线程挂住，反而更占资源（与 R152-E1 的线程闸同思路）。

    状态挂 **server 实例**：同一进程里可能起多个 WebServer（测试就是），
    挂模块级会让它们互相挤掉（假红）。
    """
    lock = getattr(server, "_sse_lock", None)
    if lock is None:
        lock = threading.Lock()
        try:
            server._sse_lock = lock
        except Exception:
            return True                       # 挂不上去就不限流，别把功能搞没
    with lock:
        cur = int(getattr(server, "_sse_count", 0) or 0)
        if cur >= limit:
            return False
        server._sse_count = cur + 1
        return True


def _sse_slot_release(server) -> None:
    lock = getattr(server, "_sse_lock", None)
    if lock is None:
        return
    with lock:
        server._sse_count = max(0, int(getattr(server, "_sse_count", 0) or 0) - 1)


def _fallback_accent() -> str:
    """兜底强调色 —— 从**调色板**取，不写死 hex。

    ⚠ 这里原来直接写了 `#8b7cf8`，被 P1-4 的静态护栏
    （`test_no_hardcoded_hex_outside_theme`）当场拦下 —— 那条护栏是对的：
    theme.py 之外出现裸 hex，就意味着换皮肤时这个色不会跟着变。
    兜底路径尤其不能写死：它正是「取不到色」时走的分支，写死等于
    「在最需要它的场景下恰好失效」。
    """
    try:
        from .gui import theme as _theme
        return _theme.SKINS[_theme.DEFAULT_ACCENT][0]
    except Exception:
        return ""          # 空串：前端 --brand 空值时用自己的 CSS 默认


def web_theme_payload(preset: str) -> dict:
    """桌面端当前皮肤 → 网页端要用的 theme 参数（SKIN_ORDER 第 3 条）。

    「切到暗夜风时，探探窗口 + Web 审图页 + 所有自绘控件一起换」——
    这一条以前只有桌面端换，网页端是**独立**的 `mrm-theme` localStorage，
    两端会各看各的。这里把桌面端的预设翻译成网页端认识的形状：
      · `data_theme`  ← dark / light（网页 CSS 的 `html[data-theme=...]`）；
      · `accent`      ← 强调色，网页用它盖住 --brand 那一档变量；
      · `preset`      ← 皮肤名，给前端做展示与调试。

    ⚠ 未知预设不许抛异常：这里在**请求路径**上，一个坏配置不该让
    整页 500（用户看到的是「网页打不开」，排查成本极高）。
    """
    try:
        from .gui import theme as _theme
        # R160：自定义键同样合法（resolve_preset_key 认它；preset_dark 会
        # 落到它叠加的那个内建预设上），否则网页端会把用户的自定义皮肤
        # 静默降级成默认皮肤 —— 两头看到的不是一个皮肤。
        key = _theme.resolve_preset_key(preset)
        pal = _theme.palette(_theme.preset_dark(key))
        return {"preset": key,
                "label": _theme.preset_label(key),
                "data_theme": "dark" if _theme.preset_dark(key) else "light",
                "accent": pal.get("accent") or _fallback_accent()}
    except Exception:
        return {"preset": "classic", "label": "原始工程风",
                "data_theme": "light", "accent": _fallback_accent()}


def _inject_web_theme(raw: bytes, web_theme: str) -> bytes:
    """把**网页端皮肤**写进 index.v2.html 的 ``<html data-theme=…>``。

    为什么不复用 :func:`inject_theme_into_index`：那个函数吃的是桌面端
    ``ui_preset``，而网页皮肤是独立的一份（用户 2026-10-27 明确要求）。
    两者取值域不同，硬套会把桌面皮肤画到网页上。

    ``classic`` 就是默认样式，直接把属性去掉；``night`` 已删（第 8 条），
    老配置里还留着就映射到「深色」。
    """
    try:
        key = str(web_theme or "classic").strip().lower()
        if key == "night":
            key = "qbittorrent"
        if not re.fullmatch(r"[a-z_]{1,32}", key or ""):
            key = "classic"
        txt = raw.decode("utf-8")
        if key == "classic":
            new = txt.replace('data-theme="light"', "", 1)
        else:
            new = txt.replace('data-theme="light"', 'data-theme="%s"' % key, 1)
        return new.encode("utf-8")
    except Exception:
        return raw          # 装饰性属性，注入失败绝不能把首页打成 500


def _html_escape(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _clamp_limit(raw) -> int:
    """把请求里的 limit 参数夹到 [1, PAGE_LIMIT_MAX]。

    非法输入（非数字/负数）一律回落到默认值，**不报错** ——
    前端只是传错参数，不该让它收到 500。
    """
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return PAGE_LIMIT_DEFAULT
    if n < 1:
        return 1
    return min(n, PAGE_LIMIT_MAX)


def _json_list(raw) -> list:
    """把 tasks 表里存成 TEXT 的词表解回 list。

    ⚠ 老数据有可能是空串、NULL，甚至被桌面端写成空格分隔的裸串
    （ai_labels 字段是 `TEXT DEFAULT '[]'`），三种都要能读。
    解析失败**返回空列表而不是抛异常** —— 一条脏数据不该让整个
    「不喜欢理由」接口 500（前端会静默退化成「单纯不喜欢」）。
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    s = str(raw).strip()
    if not s:
        return []
    try:
        v = json.loads(s)
    except (TypeError, ValueError):
        return [w for w in s.split() if w]
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [w for w in v.split() if w]
    return []


#: R146-D：规则原文下发上限（字符）。一条规则通常 1~4 KB，
#: 取 4000 足够看清结构；超了只截前面并给出省略提示。
RULE_TEXT_LIMIT = 4000

#: R146-C11：相似检索的候选上限。逐张解码提特征（桌面端实测 600 张
#: ≈ 15s），网页端再压一半 —— 手机上一转几十秒等于功能不存在。
SIMILAR_CANDIDATE_CAP = 300


def _human_bytes(n) -> str:
    """字节数说人话（与桌面端模型页那个 _human 同口径）。"""
    try:
        v = float(n)
    except (TypeError, ValueError):
        v = 0.0
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024:
            return f"{v:.0f} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


def _rule_text(path) -> str:
    """读规则原文给网页「规则原文」折叠区看（**只读**，不做任何解析）。

    ⚠ 两个必须：① 截断（几百条规则一起下发会把首屏请求拖成几十 MB）；
    ② 读失败返回空串而不是抛异常 —— 一条读不了的 yaml 不该让整个适配器
    页 500（解析失败那行本来就已经有「解析失败」的说明了）。
    """
    try:
        s = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if len(s) > RULE_TEXT_LIMIT:
        return s[:RULE_TEXT_LIMIT] + f"\n…（已省略 {len(s) - RULE_TEXT_LIMIT} 字）"
    return s


def web_assets_dir() -> Path:
    """前端资产目录。兼容源码运行与 PyInstaller 打包两种形态。

    打包后 `media_resource_manager/web/` 会整体躺在 `_internal` 里
    （spec 的 datas 目标目录），所以优先找 ``sys._MEIPASS``。
    ⚠ 冻结态必须**先查 _MEIPASS**：冻结后 ``__file__`` 指向解包后的临时目录，
    直接用它可能在某些 PyInstaller 模式下拿到不完整路径。
    """
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        cand = Path(frozen) / "media_resource_manager" / "web"
        if cand.is_dir():
            return cand
    cand = Path(__file__).resolve().parent / "web"
    return cand if cand.is_dir() else Path(__file__).resolve().parent


# R48/I：Web 端品牌水印图的 URL 与字节。
#
# 作业单 I 要求「用真实水印图，不要自绘」。前端因此一律引这个常量，
# 不写字面量 —— 写死字符串的话，改名时前端静默 404，右上角变成碎图
# 而没有任何报错（这类"拼错了但没人告诉我"最费时间）。
BRAND_LOGO_URL = "/brand/logo.png"


# R50：Web「设置 → 皮肤」的合法预设与中文标签。
#
# ⚠ 取色引擎有两套键（见 MEMORY 第 7 条）：`SKIN_LABELS` 是**取色用的**
# （violet/blue/…），用户看到的**选择器**是 `PRESETS` + `preset_label()`。
# 这里要的是后者 —— 拿 SKIN_LABELS 去渲染菜单，用户会看到「紫罗兰/青蓝/海蓝」
# 而不是三个皮肤名。所以下面一律走 theme.preset_label，不碰 SKIN_LABELS。
#: AI 来源的合法值（与 cfg.ai_backend 的注释保持一致）。
_AI_BACKENDS = ("builtin", "local", "api")


def _compliance_levels() -> tuple:
    """合法合规级别（来自 config.COMPLIANCE_LEVELS，不在此复写一份）。"""
    try:
        from .config import COMPLIANCE_LEVELS
        return tuple(COMPLIANCE_LEVELS)
    except Exception:
        return ("off", "loose", "standard", "strict")


_SETTINGS_LABELS = {
    "ui_preset": "皮肤",
    # 2026-10-28：动态图标总开关（缺这行时设置页会显示英文键名）
    "ui_animated_icons": "动态图标",
    "watermark_enabled": "水印",
    "watermark_alpha": "水印不透明度",
    "ai_enabled_global": "AI 总开关",
    "rate_plan_enabled": "分时段下载限速",
    "rate_plan": "限速时段表",
    "speed_limit_kbps": "全局速率上限（KB/s）",
    "speed_skip_lan": "内网地址不限速",
    "confirm_exit_running": "退出前确认（有任务在跑）",
    "prevent_sleep": "采集时阻止系统休眠",
    "splash_enabled": "显示启动画面",
    "resume_on_start": "启动后自动恢复上次未完成的工作",
    "resume_downloads": "恢复未完成的下载任务",
    "resume_tags": "恢复未打完的标签",
    "resume_imports": "恢复未完成的导入",
    "silent_add": "静默添加任务（不弹表单）",
    "clean_part_on_cancel": "停止时清理临时文件",
    "subdir_mode": "输出子目录方式",
    "watch_enabled": "监视文件夹",
    "watch_dir": "监视文件夹路径",
    "watch_interval_sec": "监视扫描间隔（秒）",
    "move_on_path_change": "改目录时移动已有文件",
    "proxy_dns": "域名解析也走代理",
    "proxy_tasks_only": "仅任务请求走代理",
    "ip_filter_enabled": "启用 IP 过滤",
    "ip_filter_text": "IP 过滤规则",
    "log_max_mb": "日志单文件上限（MB）",
    "log_backup_count": "日志保留份数",
    "update_manifest_url": "更新清单地址",
    "web_localhost_free": "仅本机免密",
    "web_whitelist_nopass": "白名单内免密",
    "web_login_max_fail": "登录失败锁定阈值",
    "web_login_ban_min": "锁定时长（分钟）",
    "web_session_hours": "会话有效期（小时）",
    "web_frame_deny": "禁止被网页嵌入（防点击劫持）",
    "web_host_check": "校验 Host 头（防 DNS 重绑定）",
    "web_csrf": "校验请求来源（防 CSRF）",
    "web_extra_headers": "自定义响应头",
    # #18 新增项（只在枚举校验报错时用到，但缺了会显示内部键名）
    "ai_backend": "AI 来源",
    "compliance_level": "合规检查",
    "ai_device": "推理设备",
}


# 网页皮肤名单（前端 App.vue 的 THEMES 与它一一对应）
_WEB_THEMES = ("classic", "violet_look", "modern", "night", "qbittorrent")


# ── V0.0.17 · 网页端键位 / 自定义皮肤的共用口径 ─────────────────────────
# 桌面 hotkeys.py 的 ACTIONS 是审核页 / 灯箱 / 主窗口那套上下文，网页端没有
# 这些窗口，所以网页动作单独注册在同一份 cfg.hotkeys 覆盖字典里（以 web. 开头，
# 桌面 resolve() 对未知动作一律忽略，互不干扰）。键位序列串仍走 hotkeys 的规范。
WEB_ACTIONS = (
    {"id": "web.shortcuts", "label": "打开快捷键说明", "default": "?"},
    {"id": "web.side", "label": "收起或展开侧栏", "default": "Ctrl+B"},
    {"id": "web.tasks", "label": "前往任务页", "default": "Alt+1"},
    {"id": "web.gallery", "label": "前往图库页", "default": "Alt+2"},
    {"id": "web.top", "label": "回到顶部", "default": "Alt+T"},
)
WEB_ACTION_IDS = frozenset(a["id"] for a in WEB_ACTIONS)
WEB_HOTKEY_DEFAULTS = {a["id"]: a["default"] for a in WEB_ACTIONS}
#: 内建网页皮肤的中文名（自定义皮肤在 /api/skins 里另列）
_WEB_THEME_LABELS = {
    "classic": "原始", "violet_look": "紫罗兰",
    "modern": "现代", "qbittorrent": "深色",
}


def _web_hotkey_seq_ok(seq) -> bool:
    """网页键位序列串是否合法（与 hotkeys.normalize_sequence 同一口径）。

    ``?`` 在键盘上天然带 Shift，桌面注册表没有这个键，这里单独放行；
    其余一律交给 hotkeys 解析，手写垃圾值不许把界面搞失效。
    """
    if not isinstance(seq, str) or not seq.strip():
        return False
    s = seq.strip()
    if s == "?":
        return True
    try:
        from .hotkeys import normalize_sequence
        return normalize_sequence(s) is not None
    except Exception:
        return False


def _skin_helpers():
    """优先复用 gui/theme.py 的自定义皮肤口径（目录 / 文件名校验 / CSS 净化）。

    gui/__init__.py 是惰性导入，正常情况下这里不会把 PySide6 拖进进程；
    极简纯 Web 环境导入失败时，调用方退回本文件内的兜底实现。
    """
    try:
        from .gui import theme as _t
        return _t
    except Exception:
        return None


_SKIN_BAD_NAME = re.compile(r"[/\\\x00-\x1f:*?\"<>|]")


def _skin_name_ok(stem) -> bool:
    """兜底用的皮肤名校验，与 theme.custom_name_ok 同一条正则。"""
    if not isinstance(stem, str):
        return False
    n = stem.strip()
    if not n or len(n) > 64 or n in (".", ".."):
        return False
    return not _SKIN_BAD_NAME.search(n)


def _skins_dir() -> Path:
    from .config import user_data_dir
    return Path(user_data_dir()) / "skins"


def _skins_local_list() -> list[tuple]:
    """兜底：直接扫 skins 目录（.qss 与同名 .css 算同一个皮肤）。"""
    found = {}
    try:
        for p in _skins_dir().iterdir():
            try:
                if not p.is_file():
                    continue
                suf = p.suffix.lower()
                if suf not in (".qss", ".css"):
                    continue
                stem = p.name[: -len(suf)]
                if _skin_name_ok(stem):
                    found[stem] = True
            except OSError:
                continue
    except OSError:
        return []
    return [("custom:" + s, s) for s in sorted(found)]


def _skins_local_css(key) -> str:
    """兜底：读同名 .css 并抹掉 ``</style``（与 theme 的安全闸同口径）。"""
    if not isinstance(key, str) or not key.startswith("custom:"):
        return ""
    stem = key[len("custom:"):].strip()
    if not _skin_name_ok(stem):
        return ""
    p = _skins_dir() / (stem + ".css")
    try:
        if not p.is_file():
            return ""
        data = p.read_bytes()[:256 * 1024].decode("utf-8", errors="ignore")
    except OSError:
        return ""
    return re.sub(r"<\s*/\s*style", "/* mrm */ /style", data, flags=re.I)


def _split_word_list(value) -> list:
    """设置页 CJK 词表字段的拆分器（global_blacklist / sensitive_words）。

    复用 config.split_blacklist_words：按中英文逗号/顿号/分号/换行/竖线拆，
    去空白去重去引号。Web 前端常整串粘贴（中文逗号），老逻辑只按空白+英文
    逗号拆会把"词A，词B，词C"当**一个**词存进去，消费侧永不命中。
    导入失败（纯 Web 环境异常）时退回本地拆分，不拖垮保存。
    """
    try:
        from .config import split_blacklist_words
        return split_blacklist_words(value or [])
    except Exception:
        out, seen = [], set()
        for item in (value or []):
            if not isinstance(item, str):
                continue
            import re as _re
            for piece in _re.split(r"[,，、;；\n\r|\t]+", item):
                w = piece.strip().strip("\"'“”'、").strip()
                if w and w not in seen:
                    seen.add(w)
                    out.append(w)
        return out
# 推理设备四档：auto / gpu / npu / cpu
_AI_DEVICES = ("auto", "gpu", "npu", "cpu")


def _theme_presets() -> tuple:
    """合法的 ui_preset 取值（来自 theme.PRESETS，不在此处复写一份）。

    ⚠ 刻意**不在导入时**就把值取成模块级常量：那会把「预设列表」在 import
    那一刻钉死，将来加第四个皮肤时 Web 端会静默只认旧的三个
    （与 MEMORY 第 6 条「色值不许做成模块级常量」同型 —— 取值时机要对）。
    另外 theme 住在 gui 包里、导入会拉 PySide6，所以包在函数里：
    纯 Web 环境（无 Qt）也不该因为读设置列表而崩。

    ⚠ R160：自定义皮肤也要在合法值里 —— 否则用户在网页设置页选了自定义
      皮肤会被当成「非法值」静默丢回默认，而桌面端那边是生效的。
    """
    try:
        from .gui.theme import PRESETS, list_custom_skins
        return tuple(PRESETS.keys()) + tuple(k for k, _ in list_custom_skins())
    except Exception:
        return ("classic", "modern", "night")


def _preset_label_safe(key: str) -> str:
    """皮肤 key → 中文名。取不到就退回 key 本身，**绝不抛**。

    与 `_theme_presets` 同理：theme 住在 gui 包、导入会拉 PySide6，
    Web 端渲染设置页不该因为「起不来 Qt」而整页 500。
    """
    try:
        from .gui.theme import preset_label
        return preset_label(key)
    except Exception:
        return str(key)


def brand_logo_bytes() -> bytes | None:
    """读桌面端那张水印原图（:func:`resources.logo_path`）。

    ⚠ **刻意复用 resources.logo_path 而不是拷一份进 web/static/img**：
    桌面端水印控件（gui/watermark.py 的 `_load_png`）读的也是它，
    这样两端天然同图。用户换图只换一处，两边一起变。

    返回 None 表示「用户没放图」—— 那是合法状态（logo_path 的契约），
    调用方要当404 处理，**不能抛**：抛了会让整页500。
    """
    try:
        from .resources import logo_path
        p = logo_path()
    except Exception:
        p = None
    if p is None:
        return None
    try:
        data = Path(p).read_bytes()
    except OSError:
        return None
    # 只要 PNG —— 后缀对但内容不是图的话，<img> 会碎。
    # ⚠ 这不是过度防御：logo_path 是按**文件名**找的，用户放个
    #   logo.jpg 进去它照样返回，浏览器拿到非 PNG 直接碎图。
    return data if data[:8] == b"\x89PNG\r\n\x1a\n" else None


_WORDMARK_CACHE: bytes | None = None


def brand_wordmark_bytes() -> bytes | None:
    """V0.0.27：品牌「字标」——裁掉 logo 四周大片透明边后再给网页用。

    为什么需要它：``logo.png`` 是 256×256 的方图，真正有内容的只有中间
    y=85..171 那一条（约 244×86 的「Sweet」字标）。塞进侧栏 26×26 的方框里，
    字只剩 26×9 像素 —— 用户原话「那个 logo 也太小了吧，啥也看不到」。
    裁掉透明边以后可以按宽约 96px 自然显示，字标才认得出。

    优先读预裁好的 ``resources/logo_wordmark.png``（随包发布、结果稳定）；
    没有就用 PIL 现场裁一次并缓存（用户自己换了 logo.png 也能立刻生效）。
    """
    global _WORDMARK_CACHE
    try:
        from .resources import resources_dir, logo_path
        pre = Path(resources_dir()) / "logo_wordmark.png"
        if pre.exists():
            data = pre.read_bytes()
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                return data
    except Exception:                                  # noqa: BLE001
        pass
    if _WORDMARK_CACHE is not None:
        return _WORDMARK_CACHE
    raw = brand_logo_bytes()
    if raw is None:
        return None
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        bb = im.getbbox()
        if bb:
            pad = 4
            box = (max(0, bb[0] - pad), max(0, bb[1] - pad),
                   min(im.width, bb[2] + pad), min(im.height, bb[3] + pad))
            im = im.crop(box)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        _WORDMARK_CACHE = buf.getvalue()
        return _WORDMARK_CACHE
    except Exception:                                  # noqa: BLE001
        return raw          # 裁不了就退回原图，至少不碎图


# ---------------------------------------------------------------- handler

_CJK = re.compile(r"[\u4e00-\u9fff]")


def plain_reason(err: str) -> str:
    """#38：把原始报错压成一句**白话中文**（认不出来也必须是中文）。

    ⚠ 判据**直接用桌面端那个** `stats.short_reason` —— 任务行的失败原因
    在 exe 和网页上必须是同一句话，各写一套就会出现「桌面说超时、网页说
    未知错误」这种自相矛盾的界面。

    `short_reason` 认不出来时原样返回 ``s[:60]``（宁可长而准，也不猜）。
    那一手在桌面端没问题（列宽够、用户看得懂上下文），但网页端是**手机
    竖屏**，一行英文 traceback 等于没说。所以这里只加一层兜底：认不出来
    就加中文前缀，**原文照带**（用户要反馈时还得靠它），不删不改写。
    """
    from .stats import short_reason      # 延迟导入：webui 启动路径不该被它拖住

    s = (err or "").strip()
    if not s:
        return ""
    zh = short_reason(s)
    if zh != s[:60]:
        return zh                      # 认出来了 —— 桌面同一句
    if _CJK.search(zh):
        return zh                      # 原文就是中文（比如「未产出图…」）
    return "任务出错（详情见原始报错）：" + zh[:60]


class _Handler(BaseHTTPRequestHandler):
    """只处理 GET/POST 的极简处理器。服务端的 db/cfg 由 WebServer 注入。"""

    server_version = "mrm-web/2.0"
    protocol_version = "HTTP/1.1"
    # R152-E1：连接超时。StreamRequestHandler.setup() 会把它设到 socket 上，
    # idle 挂着不关的连接到时间自动断，不再无限期占着一条线程。
    timeout = 30

    # ---- 基础工具 ----
    def log_message(self, fmt, *args):
        # 默认实现往 stderr 打，PyInstaller 窗口程序里没人看得到；
        # 改成静默，需要排查时再打开。
        pass

    @property
    def cfg(self):
        return self.server.cfg                     # type: ignore[attr-defined]

    @property
    def db(self):
        return self.server.db                      # type: ignore[attr-defined]

    def _is_https(self) -> bool:
        """本请求是不是走在 HTTPS 上（判断依据是 socket 类型，不是配置）。"""
        try:
            return isinstance(getattr(self, "connection", None), ssl.SSLSocket)
        except Exception:
            return False

    def _redirect_to_https(self) -> bool:
        """编号 23：强制 HTTPS 时把 HTTP 请求 301 到 HTTPS。

        返回 True 表示已经回过响应（调用方直接 return）。
        ⚠ 只在**HTTP 那口**上跳；HTTPS 那口自己绝不跳自己（会死循环）。
        ⚠ 目标端口取 `web_https_port`，不是当前端口 —— 两个协议各占一个口。
        """
        try:
            if self._is_https():
                return False
            if not bool(getattr(self.cfg, "web_https", False)):
                return False
            if not bool(getattr(self.cfg, "web_https_redirect", False)):
                return False
            host = (self.headers.get("Host") or "").strip()
            if not host:
                host = f"127.0.0.1:{self.server.server_address[1]}"
            host = host.split(":")[0].split("]")[0].lstrip("[")
            port = int(getattr(self.cfg, "web_https_port", 8443) or 8443)
            target = f"https://{host}:{port}{self.path or '/'}"
            body = ("已改用加密连接访问。<a href=\"%s\">继续</a>" % target).encode("utf-8")
            self._send(301, body, "text/html; charset=utf-8",
                       extra={"Location": target})
            return True
        except Exception:
            return False

    def _send_security_headers(self, cache: str = "no-store"):
        """R152-E4 安全响应头（_send 与 _send_file 共用，防漂移）。

        CSP 取「宽松但存在」——本前端全部资产都是同源静态文件、无内联脚本/
        事件属性（新版 Vue 应用的产物已核实），style 允许 unsafe-inline
        是因为大量行内样式来自 _tstyle/主题机制。
        """
        # 默认不缓存（外壳由 SW 管离线；API 绝不能缓存）。
        # 2026-10-27：缩略图例外 —— 缩略图是**按 id+w 生成的不可变文件**，
        # 允许长缓存，否则每次滚屏都重新下载（这就是"浏览很卡"的一半原因）。
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        # 2026-10-27（编号 28）：默认**拒绝被任何页面嵌入**。
        # 之前是 SAMEORIGIN（同源可嵌），但本软件没有任何"被嵌"的正当用途，
        # 而点击劫持正是靠 iframe 套一层透明层骗点击。
        try:
            _deny = bool(getattr(self.cfg, "web_frame_deny", True))
        except Exception:
            _deny = True
        self.send_header("X-Frame-Options", "DENY" if _deny else "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors "
            + ("'none'" if _deny else "'self'") + "; "
            "base-uri 'self'; form-action 'self'")
        # 2026-10-27（编号 31）：自定义响应头。反代/嵌入场景需要加
        # `X-Robots-Tag`、`Permissions-Policy` 之类，但绝不允许覆盖
        # 已经算好的内容协商头（否则页面直接坏，且现象极难查）。
        for _k, _v in self._custom_headers():
            self.send_header(_k, _v)

    #: 自定义响应头**禁止**触碰的名单：这些头决定了响应怎么被解析，
    #: 覆盖它们等于自毁（例如把 Content-Type 改成 text/html 会让图片全挂）。
    _HDR_FORBIDDEN = {
        "content-length", "content-type", "transfer-encoding", "connection",
        "cache-control", "content-encoding", "set-cookie", "location",
    }

    def _custom_headers(self) -> list:
        """解析 `cfg.web_extra_headers`（一行一个 `Key: Value`）。坏行直接跳过。"""
        try:
            raw = str(getattr(self.cfg, "web_extra_headers", "") or "")
        except Exception:
            return []
        out = []
        for line in raw.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            k, sep, v = s.partition(":")
            if not sep:
                continue
            k, v = k.strip(), v.strip()
            # 头名只能是 token 字符；值里禁止 CR/LF（防响应头注入）
            if not k or any(ch in k for ch in " \t\r\n") or "\r" in v or "\n" in v:
                continue
            if k.lower() in self._HDR_FORBIDDEN:
                continue
            out.append((k, v))
        return out

    def _host_header_ok(self) -> bool:
        """编号 29：Host 头校验，防 DNS 重绑定。

        攻击场景：恶意站把自己的域名解析到 `192.168.1.5`（你的机器），
        再用 `http://evil.com:8080/` 让受害者浏览器去访问 —— 请求会打到我们
        的 Web 服务上，而**浏览器的同源判断认的是 evil.com**，等于绕过了同源
        保护。校验 Host 是不是"我们认得的地址"就能挡住。

        放行规则（宁松勿紧，否则会把正常用户锁在门外）：
          * 反向代理已启用 → 整条跳过（代理会改写 Host，校验必然误杀）；
          * 回环 / 内网 IP 字面量 → 放行（局域网直接访问是常态）；
          * `localhost` / `*.local` / `*.lan` → 放行；
          * 等于绑定地址（bind 是主机名时）→ 放行；
          * 其余（公网域名）→ 拒绝。
        """
        try:
            if not bool(getattr(self.cfg, "web_host_check", True)):
                return True
            if bool(getattr(self.cfg, "trust_proxy", False)):
                return True
            from .ipfilter import is_local_host
            raw = str(self.headers.get("Host", "") or "").strip()
            if not raw:
                return True                      # 无 Host（HTTP/1.0 客户端）→ 老行为
            host = raw
            if host.startswith("["):             # [::1]:8080
                host = host[1:].split("]")[0]
            else:
                host = host.split(":")[0]
            if not host:
                return True
            bind = str(getattr(self.cfg, "web_bind", "127.0.0.1") or "").strip()
            if bind and host.lower() == bind.lower():
                return True
            return bool(is_local_host(host))
        except Exception:
            return True

    def _origin_ok(self) -> bool:
        """编号 30：同源校验（CSRF）。

        为什么不用双提交令牌：本项目两套前端都是自己的，加令牌要同步改两套 JS
        和几十个测试；而**浏览器发起的跨站请求一定带 Origin**，校验同源就能
        挡住真实威胁。非浏览器客户端（脚本/curl）通常不带 Origin，
        此时按老行为放行 —— 这类客户端本来就能直接带 Cookie，谈不上 CSRF。

        允许的情况：
          * 没开这个开关；
          * 没有 Origin 且没有 Referer（非浏览器）；
          * Origin 与 Host 同源（协议无关，端口要比）；
          * 反向代理启用（Origin 可能是外部域名，Host 已被改写）。
        """
        try:
            if not bool(getattr(self.cfg, "web_csrf", True)):
                return True
            if bool(getattr(self.cfg, "trust_proxy", False)):
                return True
            origin = str(self.headers.get("Origin", "") or "").strip()
            referer = str(self.headers.get("Referer", "") or "").strip()
            host = str(self.headers.get("Host", "") or "").strip()
            if not host:
                return True
            if not origin:
                if not referer:
                    return True
                try:
                    origin = f"{urlsplit(referer).scheme}://{urlsplit(referer).netloc}"
                except Exception:
                    return True
            if origin == "null":
                return False                     # 沙箱 iframe / file:// 发起的请求
            try:
                o = urlsplit(origin)
                o_host = o.netloc or o.path
            except Exception:
                return True
            return o_host.lower() == host.lower()
        except Exception as ex:                       # noqa: BLE001
            # ⚠ 第一次实现就栽在这：`urlsplit` 忘了 import → NameError 被这里
            # 吞掉 → 校验**静默失效**（探针显示跨站 Origin 竟然 200）。
            # 现在异常不再完全静默，至少日志里能看到。
            try:
                LOG.warning("同源校验异常（已按放行处理）：%s", ex)
            except Exception:
                pass
            return True

    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        _extra = dict(extra or {})
        self._send_security_headers(_extra.pop("Cache-Control", "no-store"))
        for k, v in _extra.items():
            self.send_header(k, v)
        # R251：插件推送接口要回 CORS 头（见 _cors_extension）。
        # 走实例上的暂存列表而不是 extra 参数：那条路径的返回语句有十几处，
        # 每处都记得传 extra 是不可能的 —— 漏一处就是「插件报跨域、软件
        # 日志里一切正常」，最难查的那种。
        for k, v in getattr(self, "_extra_headers", []) or []:
            self.send_header(k, v)
        self._extra_headers = []
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _send_file(self, path: Path, ctype: str,
                   extra: dict | None = None) -> bool:
        """流式发送文件，支持 Range（206 Partial Content）。

        R152-E10：原来 `_serve_image`/`_serve_original` 对任意大小的图都
        `read_bytes()` 整张读进内存，大图（几十 MB 原图）并发几个就把内存
        吃满。这里 ≥_STREAM_THRESHOLD(1MB) 走分块流式 + Range，小图仍走
        read_bytes（简单路径行为不变）。

        支持单区间 `Range: bytes=start-end` / `bytes=start-` / `bytes=-suffix`。
        返回 True 表示已发送响应；False 表示文件读不了（调用方应已处理
        404，这里只兜底 stat/open 的 OSError）。
        """
        try:
            size = path.stat().st_size
        except OSError:
            return False
        # 小文件：保持 read_bytes 简单路径（与原来行为完全一致）
        if size < _STREAM_THRESHOLD:
            try:
                self._send(200, path.read_bytes(), ctype, extra)
            except OSError:
                return False
            return True

        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes="):].split(",")[0].strip()
            if "-" in spec:
                a, _, b = spec.partition("-")
                try:
                    if not a:
                        # suffix：最后 b 字节
                        n = int(b) if b else 0
                        start = max(0, size - n)
                    else:
                        start = int(a)
                        end = int(b) if b else size - 1
                        if end >= size:
                            end = size - 1
                    if 0 <= start <= end < size:
                        partial = True
                except ValueError:
                    partial = False

        # 先打开文件（失败返回 False，调用方发 404），再发头再流式 ——
        # 避免「头已发、文件却读不了」的半截响应。
        try:
            fh = open(path, "rb")
        except OSError:
            return False
        try:
            if partial:
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header(
                    "Content-Range", "bytes %d-%d/%d" % (start, end, size))
                _ex = dict(extra or {})
                self._send_security_headers(_ex.pop("Cache-Control", "no-store"))
                for k, v in _ex.items():
                    self.send_header(k, v)
                self.end_headers()
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(_FILE_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            else:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                _ex = dict(extra or {})
                self._send_security_headers(_ex.pop("Cache-Control", "no-store"))
                for k, v in _ex.items():
                    self.send_header(k, v)
                self.end_headers()
                while True:
                    chunk = fh.read(_FILE_CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        finally:
            fh.close()
        return True

    def _html(self, code: int, text: str):
        self._send(code, text.encode("utf-8"), "text/html; charset=utf-8")

    def _json(self, obj, code: int = 200, extra: dict | None = None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    # ---- B3 #48 鉴权 ----
    # ---- V0.0.23：日志的「来源」 ----
    def _actor(self) -> str:
        """这次请求是谁发起的：内置客户端 / 网页浏览器。

        前端（api.js）在每个请求上带 `X-MRM-Surface: client|browser`；
        内置壳窗口里打开时是 client，手机 / 别的浏览器打开是 browser。
        用户抱怨「客户端和网页都记成 web，分不清是谁点的」，就是这里补的。
        """
        try:
            s = str(self.headers.get("X-MRM-Surface") or "").strip().lower()
        except Exception:                                  # noqa: BLE001
            s = ""
        return "client" if s == "client" else "browser"

    # ---- R52：局域网 / IP 白名单免密 ----
    def _client_ip(self) -> str:
        """取请求方 IP。

        默认**只认直连地址**（``self.client_address``），不解析任何代理头：
        X-Forwarded-For / X-Real-IP 是**客户端可伪造**的，一旦默认信任，
        任何人带上 `X-Forwarded-For: 127.0.0.1` 就能骗到局域网免密（安全级）。

        只有显式开 ``cfg.trust_proxy``（容器 / 反向代理场景，见订单 E1）时
        才解析，顺序：X-Real-IP（单值，最可信）→ X-Forwarded-For **最左一跳**
        （原始客户端）→ 都取不到退回直连地址。

        ⚠ 容器场景的**另一半**问题不在这里：即使完全不挂反代，容器内直连
        地址也是 docker0 网关 172.17.0.1，它本身就是私有地址（_ip_is_private
        专判 172.16-31 段）⇒ 叠加 web_lan_open 依然是"全网免密"。所以容器
        部署必须**同时** MRM_WEB_LAN_OPEN=0 +
        显式设 MRM_WEB_PASSWORD，光加这个开关救不了。
        """
        try:
            direct = self.client_address[0] or ""
        except Exception:
            direct = ""

        if not getattr(self.cfg, "trust_proxy", False):
            return direct

        try:
            real = (self.headers.get("X-Real-IP") or "").strip()
            if real:
                return real
            xff = (self.headers.get("X-Forwarded-For") or "").strip()
            if xff:
                # 最左一跳 = 原始客户端（后续是链路上的各层代理）
                first = xff.split(",")[0].strip()
                if first:
                    return first
        except Exception:
            # 头解析炸了绝不 500：退回直连（宁严勿松）
            return direct
        return direct

    @staticmethod
    def _ip_is_private(ip: str) -> bool:
        """是不是私有/本机地址（RFC1918 + 回环 + 链路本地 + IPv6 本地）。

        ⚠ 手搓前缀匹配而不用 ipaddress：跑在请求热路径上，一处解析异常就是
        500；这里只用最朴素的「前缀/段」覆盖 LAN 常见范围即可，绝不抛。
        """
        if not ip:
            return False
        if ip in ("127.0.0.1", "::1", "localhost"):
            return True
        # IPv6 链路本地(fe80:)/唯一本地(fc/fd)
        if ip.startswith("fe80:") or ip.startswith("fc") or ip.startswith("fd"):
            return True
        if ip.startswith("10."):
            return True
        if ip.startswith("192.168."):
            return True
        if ip.startswith("172."):
            try:                # 172.16.0.0/12
                if 16 <= int(ip.split(".")[1]) <= 31:
                    return True
            except (ValueError, IndexError):
                pass
        if ip.startswith("169.254."):
            return True          # 链路本地
        return False

    @staticmethod
    def _ip_in_cidr(ip: str, cidr: str) -> bool:
        """极简 IPv4 CIDR 匹配（a.b.c.d/n）。任何异常一律返回 False 不抛。"""
        try:
            net, _, bits = cidr.strip().partition("/")
            if not bits:
                return ip == net
            mask = int(bits)
            a = [int(x) for x in ip.split(".")]
            b = [int(x) for x in net.split(".")]
            if len(a) != 4 or len(b) != 4 or not (0 <= mask <= 32):
                return False
            nbits = mask
            for i in range(4):
                if nbits <= 0:
                    break
                take = min(8, nbits)
                shift = 8 - take
                if ((a[i] >> shift) << shift) != ((b[i] >> shift) << shift):
                    return False
                nbits -= 8
            return True
        except (ValueError, IndexError):
            return False

    def _client_allowed_by_ip(self) -> bool:
        """R52：本请求是否因「局域网」或「显式白名单」而免密。

        返回 True=放行（不要求密码）；False=仍需密码。
        ⚠ 任何异常都回落 False（宁严勿松：免密是放宽，失败必须回到要密码）。
        """
        try:
            if getattr(self.cfg, "web_lan_open", False) and \
                    self._ip_is_private(self._client_ip()):
                return True
            ip = self._client_ip()
            # 2026-10-27（编号 25）：白名单**是否免密**可配。
            # 默认 True = 保持历史行为（白名单里的来源直接放行）。
            # 置 False 后白名单只表示"允许访问登录页"，进数据仍要密码 ——
            # 适合"想让家里人看，但不想给密码"以外的场景（例如只开放给
            # 反代所在网段，而反代自己有认证）。
            if not bool(getattr(self.cfg, "web_whitelist_nopass", True)):
                return False
            for entry in (getattr(self.cfg, "web_ip_whitelist", None) or []):
                if entry and self._ip_in_cidr(ip, str(entry)):
                    return True
        except Exception:
            return False
        return False

    # ---- R152-E3：登录限流（按 IP 失败计数 + 指数退避）----
    def _login_gate(self) -> float:
        """返回该 IP 还需等待的秒数；0 = 放行。

        任何异常都回落 0（宁松勿紧：限流是加固，坏了不能把人锁在外面）。
        """
        try:
            st = getattr(self.server, "_login_fails", None)
            lk = getattr(self.server, "_login_lock", None)
            if st is None or lk is None:
                return 0.0
            ip = self._client_ip()
            now = time.monotonic()
            with lk:
                rec = st.get(ip)
                if not rec:
                    return 0.0
                until = float(rec.get("until", 0.0) or 0.0)
                if until <= now:
                    return 0.0
                return until - now
        except Exception:
            return 0.0

    def _login_failed(self) -> int:
        """记一次密码失败，返回该 IP 的连续失败次数（供审计日志用）。"""
        try:
            st = getattr(self.server, "_login_fails", None)
            lk = getattr(self.server, "_login_lock", None)
            if st is None or lk is None:
                return 0
            ip = self._client_ip()
            now = time.monotonic()
            with lk:
                rec = st.setdefault(ip, {"count": 0, "last": 0.0, "until": 0.0})
                rec["count"] = int(rec.get("count", 0) or 0) + 1
                rec["last"] = now
                # 2026-10-27（编号 26）：阈值与锁定时长可配。
                # 0 = 不锁定（自用机器嫌麻烦时关掉）；默认 5 次 / 10 分钟。
                try:
                    _max = int(getattr(self.cfg, "web_login_max_fail", 5) or 0)
                except Exception:
                    _max = 5
                try:
                    _ban = float(getattr(self.cfg, "web_login_ban_min", 10) or 10) * 60.0
                except Exception:
                    _ban = 600.0
                if _max <= 0:
                    rec["until"] = 0.0
                    return rec["count"]
                if rec["count"] >= _max:
                    over = rec["count"] - _max
                    rec["until"] = now + min(
                        max(_ban, _LOGIN_BACKOFF_BASE) * (2 ** over), _LOGIN_BACKOFF_CAP)
                return rec["count"]
        except Exception:
            return 0

    def _login_succeeded(self) -> None:
        """登录成功即清零该 IP 的失败计数（正当用户不该被历史失败连坐）。"""
        try:
            st = getattr(self.server, "_login_fails", None)
            lk = getattr(self.server, "_login_lock", None)
            if st is None or lk is None:
                return
            with lk:
                st.pop(self._client_ip(), None)
        except Exception:
            pass

    def _auth_ok(self) -> bool:
        """免密策略（R52 起，R157 微调）。

        - 空密码 + 绑本机（127.0.0.1/::1/localhost）→ **无条件放行**
          （R220：绑本机时物理上只有本机能连上，不再依赖首启动时序下
          可能取空的 client_ip —— 那正是「重新开关 web 才能进」的成因）；
        - 空密码 + 其他绑定 → 本机/私有网段客户端放行，公网仍拦（R58 不放开）；
        - 设了密码 + 局域网/白名单 → 免密放行（R52 契约，设密码路径不变）；
        - 设了密码 + 无凭据 → 仍要密码（Basic Auth 或会话 cookie）。

        ⚠ 用户名仍不要求：后端一律用 `DEFAULT_USER`。

        ⚠⚠ R157 与 R58/R141 的关系（改之前先读这段）：
          旧代码此处写 `if not want: return False`，即「没设密码反而一律拦」。
          这与 `/api/session` 报给前端的 `password_required=False`、以及
          前端 `app.js` 的「web_password 为空即免密」**直接矛盾** ⇒ 用户
          （本机、`web_lan_open=False` 时）打开网页，前端以为不用密码，
          后端却对每个接口回 401 ⇒ 「空密码反而进不去」。
          改法**只放开「本机/局域网」这一档**（把 `_client_allowed_by_ip`
          里被 `web_lan_open` 关掉的那部分补回来），**不放开公网**：
          R58 的安全侧（公网 + 空密码必须拦）与 R141 的边界（只放宽本机/局域网）
          两条护栏因此都仍然成立，无需改动任何既有护栏。
          若日后用户明确要求「公网也免密」，必须连 R58/R141 的 3 条护栏一起改
          （R141 订单原话），不能只改这一行 —— 那是一次真实的暴露面扩大。
        """
        # R220：空密码分支**提前**。原顺序先查 `_client_allowed_by_ip`，
        # 首启动自动拉起 Web 的时序里 client_ip 可能取到 ""，
        # `_ip_is_private("")=False` ⇒ 空密码也被判「要密码」，前端弹密码页，
        # 用户「重新开关一下 web 才能进」。现在绑本机时无条件放行 ——
        # bind=127.0.0.1 时本来就只有本机能连上，client_ip 取没取到无关紧要；
        # 仅当绑定可能暴露公网（0.0.0.0/:: 等）时仍走 R58 护栏。
        want = str(self.cfg.web_password or "")
        # 2026-10-27（编号 24）：仅本机免密。比「整站免密」安全得多 ——
        # 127.0.0.1 只可能是本机进程，而 0.0.0.0 上来的可能是任何人。
        try:
            if want and bool(getattr(self.cfg, "web_localhost_free", False)):
                if self._client_ip() in ("127.0.0.1", "::1"):
                    return True
        except Exception:
            pass
        if not want:
            # R157：空密码 = 免密模式（R220 收窄了它的判定路径）。
            # ⚠ 权衡（拍板留档）：非本机绑定 + 空密码时**不再查**
            # `_client_allowed_by_ip`（白名单/lan_open 对公网客户端的放行
            # 一并失效）—— R58 的「空密码不对公网敞开」优先级更高，
            # 宁严勿松；要公网可达就必须设密码。
            bind = str(getattr(self.cfg, "web_bind", "127.0.0.1") or "127.0.0.1")
            if bind in ("127.0.0.1", "::1", "localhost"):
                return True
            return self._ip_is_private(self._client_ip())
        if self._client_allowed_by_ip():
            return True
        # R105：Web 只认密码，不再区分用户。会话/鉴权统一用常量 DEFAULT_USER，
        # 不再读取 cfg.web_user（该字段保留为兼容占位、不再参与鉴权）。
        user = DEFAULT_USER
        # 1) Basic Auth（兼容老书签/脚本）
        if check_auth(self.headers.get("Authorization", ""), user, want):
            return True
        # 2) 会话 cookie（浏览器正常路径）
        tok = self._cookie(_COOKIE_NAME)
        # 2026-10-27（编号 27）：会话有效期可配（默认 12 小时）。
        try:
            _h = float(getattr(self.cfg, "web_session_hours", 12) or 12)
        except Exception:
            _h = 12.0
        _ttl = _h * 3600 if _h > 0 else _SESSION_TTL
        return bool(tok) and verify_session(tok, user, want, ttl=_ttl)

    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie", "") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return unquote(v)
        return ""

    def _unauthorized(self):
        """API 请求回 401 JSON；页面请求回 200 + 登录页外壳。

        ⚠ 不能对页面也回 401：那会让浏览器弹原生 Basic Auth 对话框，
        在手机上体验极差且没法登出（B3 要的就是自己的密码页）。
        所以 /api/* 之外的未认证请求，直接把登录壳送过去。
        """
        if urlparse(self.path).path.startswith("/api/"):
            return self._json({"ok": False, "error": "未登录"}, 401)
        # ⚠ _login_shell 返回的是**bytes**（直接来自 _asset），所以这里必须
        # 走 _send 而不是 _html —— _html 会再 .encode() 一次，
        # 报 'bytes' object has no attribute 'encode'，未登录访问整站直接 500。
        data = self._login_shell()
        if isinstance(data, bytes):
            return self._send(200, data, "text/html; charset=utf-8")
        return self._html(200, data)

    def _login_shell(self):
        """未登录时返回的 HTML —— 就是 index.v2.html 本身。

        前端应用启动时打 ``/api/session``，拿到 401 就显示密码页。
        所以这里不需要服务端另写一套登录 HTML（少一份重复模板）。
        ⚠ 读不到资产时回退到一句内联提示（**必须是 str**）：
        返回 None 会让 _html(None) 抛 AttributeError → 整站 500。
        """
        data = self._asset("index.v2.html")
        if data:
            return data
        return ("<!doctype html><meta charset='utf-8'>"
                "<title>Sweet采集AI版</title>"
                "<p>Web 前端资源缺失（未找到 web/index.v2.html）。"
                "请从桌面端重新打包，或确认 media_resource_manager/web/ 存在。</p>")

    # ---- 静态资源 ----
    def _asset(self, rel: str):
        """按相对路径读 web/ 下的文件。**必须防目录穿越**。"""
        base = web_assets_dir().resolve()
        try:
            p = (base / rel).resolve()
        except Exception:
            return None
        # 关键安全检查：`..` 穿越。少了这一行，任何人都能
        # 用 /static/../../mrm.db 把整个数据库读走。
        #
        # ⚠ 判定必须比到**目录分隔符**为止，不能只 startswith(base)：
        #   base = C:/.../web，若同级另有 C:/.../web_backup/，
        #   `C:/.../web_backup/x.js`.startswith('C:/.../web') 为 **True**，
        #   于是 ../web_backup/x.js 能读出来 —— 看似堵了、实际没堵。
        # 用 `p.is_relative_to(base)`（Py3.9+）或比较 parent 才严密。
        try:
            inside = p.is_relative_to(base)
        except AttributeError:      # 老Python 兜底
            inside = str(p).startswith(str(base) + os.sep)
        if not inside or p == base:
            return None
        if not p.is_file():
            return None
        try:
            return p.read_bytes()
        except OSError:
            return None

    def _serve_brand_logo(self):
        """R48/I：把**桌面端那张水印原图**作为静态资源发给浏览器。

        作业单 I 的硬要求是「桌面端与 Web 端**共用同一张图**，不要各用各的」。
        所以这里**不复制**一份图片进 `web/static/img/`，而是直接读
        :func:`resources.logo_path` —— 桌面端水印控件读的也是它。
        复制的话两边迟早会漂移（用户换了图只更新一处，另一处仍是旧的），
        而「两端同图」正是这条要验的东西。

        ⚠ 找不到图时**不能 500**：那会让整个页面的 `<img>` 变成碎图，
        而 logo_path() 在用户没放图时会返回 None（合法状态）。
        此时回 404，前端 CSS 会退回到自绘 SVG（见 brand_logo_url 的注释），
        页面其余部分照常工作。
        """
        data = brand_logo_bytes()
        if data is None:
            return self._html(404, "<h1>404</h1>")
        # 品牌图变了要能刷出来：与 sw.js 同理，不给强缓存
        # （否则用户换了 logo.png 刷新几次都还是旧的）。
        return self._send(200, data, "image/png",
                          {"Cache-Control": "no-cache"})

    def _serve_brand_wordmark(self):
        """V0.0.27：品牌字标（裁掉透明边），侧栏与启动页用它。"""
        data = brand_wordmark_bytes()
        if data is None:
            return self._html(404, "<h1>404</h1>")
        return self._send(200, data, "image/png",
                          {"Cache-Control": "no-cache"})

    def _serve_asset(self, rel: str, ctype: str | None = None):
        data = self._asset(rel)
        if data is None:
            return self._html(404, "<h1>404</h1>")
        # SKIN_ORDER：首屏就按**网页端当前皮肤**渲染（暗色用户不会先白闪一下）。
        # 2026-10-29（第 11 条）：旧版下线后只服务 index.v2.html，注入目标
        # 跟着换到它；同时改用 `web_theme`（网页端自己的皮肤）而不是桌面端
        # `ui_preset` —— 网页皮肤从 2026-10-27 起就是独立的，再拿桌面皮肤
        # 去定首屏，会和 App.vue 里的设置打架（看起来像"皮肤没保存住"）。
        if rel == "index.v2.html":
            data = _inject_web_theme(data, getattr(self.cfg, "web_theme", ""))
        if ctype is None:
            ctype = mimetypes.guess_type(rel)[0] or "application/octet-stream"
        extra = {}
        # service worker 与 manifest 必须不被 HTTP 缓存住：
        # 旧 sw.js 会一直控制着旧页面，用户更新了前端却看不到变化。
        if rel in ("sw.js", "manifest.webmanifest"):
            extra["Service-Worker-Allowed"] = "/"
        return self._send(200, data, ctype, extra)

    # ==================================================================
    # R154-F6：SSE 统一推送（GET /api/events）
    # ==================================================================
    # ⚠⚠ **并发闸是必须的，不是优化**。服务是 ThreadingHTTPServer +
    # ``max_threads=64``（R152-E1）：一条 SSE 就是一条**常驻线程**，
    # 8 个标签页开着 8 条流就占掉 1/8 的线程额度，来者不拒的话
    # 几十个标签页能把 64 个槽位吃干 ⇒ 连普通接口都开始 503。
    # 所以这里单独立一道闸（默认 8），超了回 503 + 中文原因，
    # 前端收到就降级回轮询（不会卡住界面）。
    def _serve_events(self, u):
        server = self.server
        if not _sse_slot_acquire(server):
            return self._json(
                {"ok": False,
                 "error": "同时打开的事件流过多（上限 %d），请关闭几个页面后重试"
                          % SSE_MAX_CLIENTS},
                503)
        try:
            return self._events_loop(u)
        finally:
            # ⚠ 必须 finally：循环里任何一条 return/异常都要还槽位，
            # 泄漏几次之后新连接永远 503（症状极难排查）。
            _sse_slot_release(server)

    def _sse_write(self, payload: bytes) -> bool:
        """写一帧（**手写 chunked**）。返回 False = 客户端已断开。

        为什么手写 chunked 而不用 `_send`：
          · `_send` 会带 Content-Length，而 SSE 是**长度未知的长流**；
          · 不带 Content-Length 又不 chunked 的话，nginx 之流的反代会
            把响应**缓冲到连接关闭**才转发 ⇒ 前端一条事件都收不到
            （正是订单点名要避免的「反代吃 SSE」）。
        """
        try:
            self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
            self.wfile.flush()
            return True
        except Exception:
            # 断开/复位/超时，一律当作「这条流结束了」，交给上层收尾。
            return False

    def _sse_peer_alive(self) -> bool:
        """对端还活着吗（**只用 peek，绝不真读**）。

        ⚠ 这不是洁癖：SSE 客户端关掉标签页时，服务端**写**往往不会立刻失败
        （数据先落进内核发送缓冲，要等下一次写/ACK 才报错）。于是旧实现里
        槽位最长要等一个心跳周期（15s）才还回去 —— 用户连关几个标签页后
        再开新页面就会吃 503，白白降级成轮询。
        这里每次循环用 select + MSG_PEEK 探一下：对端 FIN 时 select 报「可读」
        （EOF 也是一种可读），peek 回空 ⇒ 立刻收摊。
        """
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            if not r:
                return True
            return bool(self.connection.recv(1, socket.MSG_PEEK))
        except Exception:
            return False

    def _events_loop(self, u):
        since = (parse_qs(u.query).get("since") or [""])[0]
        queue = getattr(self.server, "queue", None)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        # ⚠ 这两行是 Docker/反代场景的命门：nginx 默认开 proxy_buffering，
        #   会把 SSE 攒着不发；X-Accel-Buffering: no 是 nginx 的关闭开关。
        #   （compose/README 里也写了 proxy_buffering off，双保险。）
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

        alive = True
        started = time.time()
        last_beat = started
        cur = None
        try:
            # 首帧先给 retry 与当前版本：前端拿到基线，下次断连可带 ?since=。
            alive = self._sse_write(b"retry: %d\n\n" % SSE_RETRY_MS)
            while alive and (time.time() - started) < SSE_MAX_SEC:
                try:
                    v = event_version(self.db, queue)
                except Exception:
                    v = "error"          # 版本号算不出来也不能断流
                if v != cur:
                    cur = v
                    snap = event_snapshot(self.db, queue)
                    payload = {
                        "ok": True, "version": v, "since_matched": (v == since),
                        "counts": {
                            "tasks": snap["tasks"], "images": snap["images"],
                            "pending": snap["pending"],
                            "quarantine": snap["quarantine"],
                            "failed_urls": snap["failed_urls"],
                            "running": len(snap["progress"]),
                        },
                    }
                    ev = "snapshot" if (v == since) else "change"
                    body = ("event: %s\ndata: %s\n\n"
                            % (ev, json.dumps(payload, ensure_ascii=False))
                            ).encode("utf-8")
                    alive = self._sse_write(body)
                if time.time() - last_beat >= SSE_HEARTBEAT_SEC:
                    last_beat = time.time()
                    # 注释帧（`: ` 开头）：浏览器不当事件派发，只用于保活。
                    alive = self._sse_write(b": ping\n\n")
                if alive:
                    # 分段睡：每段都探一次对端，断开能在 0.25s 内收摊
                    # （整段 sleep 的话，最长要等一个心跳周期才释放槽位）。
                    dl = time.time() + SSE_POLL_SEC
                    while alive and time.time() < dl:
                        time.sleep(min(0.25, max(0.0, dl - time.time())))
                        alive = self._sse_peer_alive()
        except Exception:
            alive = False
        try:
            # chunked 终止块。客户端早断了会抛，忽略即可。
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass
        self.close_connection = True

    # ---- 路由 ----
    def do_GET(self):                               # noqa: N802
        u = urlparse(self.path)
        path = u.path
        # 2026-10-28（编号 23）：开了"强制 HTTPS"时，HTTP 那口只做跳转。
        # ⚠ 放在 Host 校验之前：跳转本身不泄露任何数据，而如果用 http://
        #   公网名访问（DNS 重绑定场景），跳转比 400 更能让用户看懂。
        if self._redirect_to_https():
            return
        # 2026-10-27（编号 29）：Host 头校验放在**最前面**（连静态资源也拦）。
        # DNS 重绑定攻击的目的是拿到页面/接口，只拦 API 而不拦页面等于没拦。
        if not self._host_header_ok():
            return self._send(400, "拒绝访问：Host 头不是本机/内网地址".encode("utf-8"),
                              "text/plain; charset=utf-8")
        # ⚠⚠ R47/B7：**静态资源必须放在鉴权之前**。
        # 未登录时 `_unauthorized()` 送的是 `_login_shell()`，而登录壳
        # **就是 index.v2.html 本身**（它引用 /static/app 下的产物与
        # /static/js/app.js）。所以一旦把 `/static/*` 放在鉴权后面，
        # 浏览器就会拿到「一段 HTML 当成 CSS/JS」→ 样式与脚本全失效 →
        # **页面一片空白**（真机现象）。
        # 实测（修复前）：`/`、`/sw.js`、`/manifest.webmanifest`、
        # `/static/css/app.css`、`/static/js/app.js` 五个路径返回的
        # **字节数完全一样**（都是 5249B 的登录壳），坐实了这条。
        #
        # 安全性不变：静态资源本来就不含任何用户数据（壳/样式/脚本/图标），
        # 而**数据与操作**全在 `/api/*` 与 `/thumb/`，那些仍然在鉴权之后。
        try:
            # 外壳与静态资源（/ 不再是服务端拼的页面，直接送 index.v2.html）
            # 2026-10-29（用户第 11 条）：**旧版网页彻底下线**，只剩新版。
            #   · `/`、`/v2`、`/index.html` 全部落到同一个 Vue3 应用；
            #   · `/classic` 不再给旧界面，302 回首页 —— 老书签不至于变成 404；
            #   · `web_ui_v2` 这个开关随之作废（见 _SETTINGS_WRITABLE 注释）。
            if path in ("/classic", "/classic/"):
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self._send_security_headers("no-store")
                self.end_headers()
                return None
            if path in ("/v2", "/v2/", "/index.v2.html", "/index.html"):
                return self._serve_asset("index.v2.html")
            if path == "/":
                return self._serve_asset("index.v2.html")
            if path.startswith("/static/"):
                # ⚠ 注意：这里传的是带 `static/` 前缀的完整相对路径，
                # 因为 web_assets_dir() 本身就是 web/ 目录。
                # 若把 `/static/` 剥掉就变成找 `css/app.css` → **404**。
                return self._serve_asset(path[len("/"):])
            if path == "/manifest.webmanifest":
                return self._serve_asset(
                    "manifest.webmanifest",
                    "application/manifest+json; charset=utf-8")
            if path == "/sw.js":
                return self._serve_asset("sw.js",
                                         "text/javascript; charset=utf-8")
            if path == "/favicon.ico":
                return self._serve_asset("static/img/logo.svg",
                                         "image/svg+xml")
            # R48/I：品牌水印原图。**必须排在鉴权之前** —— 与上面
            # /static/* 同一个理由（B7）：index.v2.html 就是登录壳，它引用
            # 这张图；一旦放到 `_auth_ok()` 后面，未登录时浏览器拿到的
            # 是 HTML 冒充 PNG，右上角品牌标识直接碎掉。
            if path == "/brand/logo.png":
                return self._serve_brand_logo()
            # V0.0.27：裁掉透明边的品牌字标（侧栏与启动页用，字才看得清）
            if path == "/brand/wordmark.png":
                return self._serve_brand_wordmark()
            # ---- R154-F20：扫码登录的公开端点（鉴权前；不泄露数据，
            #      confirm 才建立登录态） ----
            if path == "/api/login/qrcode/image":
                return self._do_qr_image(u)
            if path == "/api/login/qrcode/status":
                return self._do_qr_status(u)
            if path == "/qr-login":
                return self._do_qr_page(u)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)

        # ---- 以下是需要登录的数据面 ----
        # ⚠ R50：`/healthz` **必须在鉴权之前**。
        # 它是给外部探活（路由器脚本、监控、桌面端自己判断「Web 起没起」）
        # 用的，探活方**不可能**带着会话 cookie。排在鉴权后面时它有两种坏结果：
        # 设了密码 → 401 JSON；没设密码时又可能被 `_unauthorized()` 的登录壳
        # 顶掉（返回 200 HTML）。两种探活脚本都会判成"服务不正常"。
        # 它只回 `{"ok": true}`，不泄露任何数据，放行是安全的。
        if path == "/healthz":
            return self._json({"ok": True})
        if not self._auth_ok():
            return self._unauthorized()
        # ---- R154-F6：SSE 事件流 ----
        # ⚠ **必须排在 `try: … except → 500 JSON` 之外**：SSE 响应头一旦发出，
        # 再回 JSON 就是把两种协议混在一帧里，浏览器只会报解析错误。
        # 同样逐个显式列出，不改成循环（路由字面量有护栏盯着）。
        if path == "/api/events":
            return self._serve_events(u)
        try:
            if path == "/api/session":
                # R52：password_required 改成「**对本客户端**是否要密码」——
                # 局域网/白名单客户端即便没设密码也免密，登录页就不该拦它。
                want = bool(str(self.cfg.web_password or ""))
                return self._json({
                    "ok": True,
                    "user": DEFAULT_USER,
                    "password_required": want and not self._client_allowed_by_ip(),
                    "lan_open": bool(getattr(self.cfg, "web_lan_open", False)),
                })
            if path == "/api/summary":
                return self._json(self._api_summary())
            if path == "/api/settings":
                return self._json(self._api_settings())
            # V0.0.55 批1：定时关机的**实时计划**（配置里的字段只记每日
            # 定点；on_idle/一次性只活在内存里，前端要显示"今晚安排了什么"
            # 必须查这个，不能读配置——读了永远空）
            if path == "/api/powerdown":
                try:
                    from . import powerdown
                    return self._json({"ok": True, "plan": powerdown.status()})
                except Exception as e:                   # noqa: BLE001
                    return self._json({"ok": False, "error": f"读取计划失败：{e}"})
            # ---- 2026-10-28：站点订阅（编号 21/22）+ 本机网卡（编号 32）----
            if path == "/api/subs":
                return self._json(self._api_subs())
            if path == "/api/sub/items":
                return self._json(self._api_sub_items(u))
            if path == "/api/net/ips":
                return self._json(self._api_net_ips())
            # 2026-10-28：底栏实时速率（qBittorrent 状态栏那一行）
            if path == "/api/stats":
                return self._json(self._api_stats())
            if path.startswith("/api/view/"):
                return self._json(self._api_view(path[len("/api/view/"):], u))
            if path.startswith("/thumb/"):
                return self._serve_image(u)
            if path.startswith("/orig/"):
                return self._serve_original(u)
            if path == "/api/tasks":
                return self._json(self._api_tasks())
            # V0.0.12：网页审核「撤销」按钮态查询（撤销栈在内存，刷新后
            # 前端要靠这个恢复按钮亮不亮）。逐字面量列出，理由同 tasks/export。
            if path == "/api/review/undo-info":
                rq = self._web_review_queue()
                return self._json({"ok": True,
                                   **(rq.undo_info() if rq else
                                      {"can_undo": False, "label": "",
                                       "depth": 0})})
            # ---- R154-F14：任务批量导出（下载 JSON，与桌面端同一份格式） ----
            # ⚠ 同样**逐字面量列出**，不改成 f-string 循环：
            #   tests/test_r48_web_features.py::test_new_endpoints_are_routed
            #   按源码字面量判「接没接路由」，循环展开后字面量消失 ⇒ 护栏假红。
            if path == "/api/tasks/export":
                return self._api_tasks_export(u)
            # ---- R154-F16：模板列表（新建任务一键套用） ----
            # ⚠ 逐字面量列出，理由同上一行。
            if path == "/api/task/templates":
                return self._json(self._api_task_templates())
            if path == "/api/proxies":
                return self._json(self._api_proxy_list())
            if path == "/api/learning":
                return self._json(self._api_learning())
            # V0.0.6：网页建任务表单顶部要显示「这回按什么词去找图」，
            # 与桌面端同一张词表（手填的 + 从导入图库学到的）。
            if path == "/api/find-terms":
                return self._json(self._api_find_terms())
            # V0.0.7：学习看板要显示「被你判掉过的词」，并允许删 / 清空。
            if path == "/api/feedback":
                return self._json(self._api_feedback())
            # ---- R230 · 学习开关（R149-B 补全：桌面能勾，Web 之前只能看）----
            # ⚠ 逐字面量列出，理由同上面几组（test_r48_web_features 按字面量判路由）。
            if path == "/api/learn/enabled":
                return self._json(self._api_learn_enabled())
            # ---- R154-F25：采集周报 / 月报（按周/月的采集量、通过率、任务排行） ----
            # ⚠ 与上面几组一样**逐个显式列出**，不改成 f-string 循环：
            #   tests/test_r48_web_features.py::test_new_endpoints_are_routed
            #   按源码字面量判「接没接路由」，循环展开后字面量消失 ⇒ 护栏假红。
            if path == "/api/report":
                return self._json(self._api_report(u))
            # ---- V0.0.14：合格线自动校准（只读建议）与加密导出下载 ----
            # ⚠ 逐字面量列出，理由同上（护栏按源码字面量判路由）。
            if path == "/api/threshold/suggest":
                return self._json(self._api_threshold_suggest(u))
            if path == "/api/export/file":
                return self._api_export_file(u)
            # ---- V0.0.16：网页补齐桌面「数据 / 维护」菜单的对称能力 ----
            if path == "/api/site/data-location":
                return self._json(self._api_site_data_location())
            if path == "/api/site/cleanup-items":
                return self._json(self._api_site_cleanup_items())
            if path == "/api/site/logs/export":
                return self._api_site_logs_export()
            # ---- R154-F18：磁盘空间看板（目录占用 + 7 日趋势 + 磁盘预警） ----
            # ⚠ 与上面几组一样**逐个显式列出**，不改成 f-string 循环：
            #   tests/test_r48_web_features.py::test_new_endpoints_are_routed
            #   按源码字面量判「接没接路由」，循环展开后字面量消失 ⇒ 护栏假红。
            if path == "/api/diskspace":
                return self._json(self._api_diskspace(u))
            # ---- V0.0.17：网页端补齐的新端点（逐字面量列出，护栏按源码判路由）----
            if path == "/api/diskspace/trend":
                return self._json(self._api_diskspace_trend(u))
            # 二进制 .mrmx 下载不走 _json（方法内部自己 _send，同 _api_diagnostic）
            if path == "/api/logs/stats/export":
                return self._api_logs_stats_export()
            if path == "/api/skins":
                return self._json(self._api_skins())
            if path == "/api/skins/css":
                return self._json(self._api_skins_css(u))
            if path == "/api/web/hotkeys":
                return self._json(self._api_web_hotkeys_get())
            if path == "/api/db/master":
                return self._json(self._api_db_master_get())
            # ---- R232：数据备份 / 偏好导入导出（桌面「数据」菜单的 Web 对称） ----
            # ⚠ 与上面几组一样**逐个显式列出**，不改成 f-string 循环：
            #   tests/test_r48_web_features.py::test_new_endpoints_are_routed
            #   按源码字面量判「接没接路由」，循环展开后字面量消失 ⇒ 护栏假红。
            if path == "/api/backup":
                return self._json(self._api_backup_list())
            # ---- R241/R242：整库备份（导出/恢复/上传/下载）+ 诊断包 ----
            # ⚠ 与上面几组一样**逐个显式列出**，不改成 f-string 循环：
            #   tests/test_r48_web_features.py::test_new_endpoints_are_routed
            #   按源码字面量判「接没接路由」，循环展开后字面量消失 ⇒ 护栏假红。
            if path == "/api/backup/full":
                return self._json(self._api_full_list())
            if path == "/api/backup/full/job":
                return self._json(self._api_full_job(u))
            # R248：批量导入（从文件夹）。目录浏览 2026-10-29 起**远程也开**
            # （Docker/NAS 用户在别的机器上要选 NAS 上的目录，拒绝等于锁死功能）；
            # 只读、只列目录、系统目录挡掉，响应带 where/remote 供界面标注。
            if path == "/api/fs/list":
                return self._json(self._api_fs_list(u))
            if path == "/api/import/job":
                return self._json(self._api_import_job(u))
            # V0.0.44：断点续导 —— 「上次导到哪儿了、还剩多少张没打标」
            if path == "/api/import/last":
                return self._json(self._api_import_last())
            # V0.0.46：语义搜索状态（已存多少向量 / 还差多少 / 当前模型）
            if path == "/api/semantic/status":
                return self._json(self._api_semantic_status())
            # R254：桌面系统集成（版本 / 开机自启 / 检查更新）—— 网页端也能看
            if path == "/api/system/integration":
                return self._json(self._api_sys_integration())
            if path == "/api/system/update":
                return self._json(self._api_sys_update())
            # ---- V0.0.18：导出历史 / 站点统计 / Cookie 提醒 / 通知中心 / 全局搜索 / 更新检查 ----
            # ⚠ 逐字面量列出，理由同护栏（test_r48_web_features 按源码字面量判路由）。
            if path == "/api/export/history":
                return self._json(self._api_export_history(u))
            if path == "/api/site-stats":
                return self._json(self._api_site_stats(u))
            if path == "/api/cookies/hints":
                return self._json(self._api_cookies_hints())
            if path == "/api/notifications":
                return self._json(self._api_notifications_list(u))
            if path == "/api/search/all":
                return self._json(self._api_search_all(u))
            if path == "/api/ui/mode":
                return self._json(self._api_ui_mode_get())
            if path == "/api/update/check":
                return self._json(self._api_update_check())
            # R253：AI 写适配器（后台作业：抓页面 + 模型 + 本地验证可能几十秒）
            if path == "/api/adapter/ai/job":
                return self._json(self._api_adapter_ai_job(u))
            # V0.0.23：读一条已有规则的原文（网页端「编辑」用；列表里也带了
            # rule_text，这里是给「只拿到名字」的场景兜底）
            if path == "/api/adapter/get":
                return self._json(self._api_adapter_get(u))
            # V0.0.54：适配器市场 —— 订阅源列表 / 拉源目录（GET 侧）
            if path == "/api/market/sources":
                return self._json(self._api_market_sources())
            if path == "/api/market/index":
                return self._json(self._api_market_index(u))
            # R251：站点 Cookie（按域名存档，插件推送 / JSON 导入 / 手动删）
            if path == "/api/cookies":
                return self._json(self._api_cookies())
            # R249：磁盘级维护（完整性 / 孤儿文件 / 转 JPG / 修复图库）
            if path == "/api/maintenance/job":
                return self._json(self._api_maint_job(u))
            if path == "/api/backup/full/download":
                return self._api_full_download(u)
            if path == "/api/backup/full/inspect":
                return self._json(self._api_full_inspect(u))
            if path == "/api/diagnostic":
                return self._api_diagnostic(u)
            if path == "/api/preferences/export":
                return self._api_prefs_export()
            # R79 #41：常用网址（与桌面端共用 cfg.saved_urls）
            if path == "/api/urls":
                return self._json(self._api_urls())
            if path == "/api/task":
                q0 = parse_qs(u.query).get("id") or ["0"]
                try:
                    tid = int(q0[0])
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "任务编号不对"})
                return self._json(self._api_task_detail(tid))
            if path == "/api/gallery":
                return self._json(self._api_gallery())
            # ---- R228 / R154-F17：图库标签的 Web 对称端点 ----
            # ⚠ 与上面几组一样**逐个显式列出**，不改成 f-string 循环：
            #   tests/test_r48_web_features.py::test_new_endpoints_are_routed
            #   按源码字面量判「接没接路由」，循环展开后字面量消失 ⇒ 护栏假红。
            #   · GET  /api/tags        全库标签计数（筛选下拉）
            #   · GET  /api/image/tags  选中图的标签 + 并集（删标签勾选清单）
            #   · POST /api/image/tags  批量加 / 删（见 do_POST）
            if path == "/api/tags":
                return self._json(self._api_tags())
            if path == "/api/image/tags":
                return self._json(self._api_image_tags(u))
            # R146-B5/C4：Web 端的「曾用词」与「不喜欢理由」都走桌面端
            # 同一份数据（cfg 历史池 / tasks 词表），不另建数据源。
            if path == "/api/words/history":
                return self._json(self._api_words_history())
            if path == "/api/dislike/options":
                return self._json(self._api_dislike_options(u))
            # ---- R154-F7：失败网址清单（按 URL 粒度重试的读取侧） ----
            # ⚠ 与上面几组一样**逐个显式列出**，不改成 f-string 循环：
            #   tests/test_r48_web_features.py::test_new_endpoints_are_routed
            #   按源码字面量判「接没接路由」，循环展开后字面量消失 ⇒ 护栏假红。
            if path == "/api/task/failed-urls":
                q0 = parse_qs(u.query).get("id") or ["0"]
                try:
                    tid = int(q0[0])
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "任务编号不对"})
                return self._json(self._api_task_failed_urls(tid))
            # ---- R146-C9 / C11：模型目录 与 相似图片（网页端只展示） ----
            # ⚠ 与上面几组一样**逐个显式列出**，不改成 f-string 循环：
            #   tests/test_r48_web_features.py::test_new_endpoints_are_routed
            #   按源码字面量判「接没接路由」，循环展开后字面量消失 ⇒ 护栏假红。
            if path == "/api/models":
                return self._json(self._api_models())
            # ---- R245：机器信息（AI 模型页显示 CPU/内存/GPU/硬盘/剩余空间） ----
            # ⚠ 逐字面量列出（理由同本函数其他几组）。
            if path == "/api/sysinfo":
                return self._json(self._api_sysinfo())
            # 模型下载进度：**必须放 GET 区**（POST 那一大块只处理 POST，
            # 放进去就是 404 —— 第一版实测就是这么错的）。
            if path == "/api/model/download/status":
                return self._json(self._api_model_download_status())
            if path == "/api/similar":
                return self._json(self._api_similar(u))
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)
        # ⚠ R67：未知 `/api/*` 原来落进下面这行，吐出的是 **HTML**（`<h1>404</h1>`）。
        # 前端 fetch 拿到 HTML 会解析失败 ⇒ 只能看到「出错了」，看不到原因。
        # /api/* 一律回 JSON（与 do_POST 末尾对齐），非 /api 页面仍走原 HTML 404
        # （tests/...::test_unknown_page_is_404 依赖它，本轮刻意不动）。
        if path.startswith("/api/"):
            return self._json({"ok": False, "error": "未知接口"}, 404)
        return self._html(404, "<h1>404</h1>")

    def do_OPTIONS(self):                           # noqa: N802
        """CORS 预检。只对插件推送接口放行，其余一律 501（保持原行为）。

        浏览器扩展发跨域 POST 前会先发 OPTIONS；不处理的话插件直接用不了，
        而现象只是「fetch 失败」，没有任何可读的错误。
        """
        u = urlparse(self.path)
        if u.path != "/api/cookie/ingest":
            return self._json({"ok": False, "error": "不支持 OPTIONS"}, 501)
        try:
            origin = str(self.headers.get("Origin", "") or "")
        except Exception:
            origin = ""
        if not origin.startswith("chrome-extension://"):
            return self._json({"ok": False, "error": "只接受浏览器扩展的预检"}, 403)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):                              # noqa: N802
        u = urlparse(self.path)
        path = u.path

        if self._redirect_to_https():
            return

        # 2026-10-27（编号 29/30）：Host 校验 + 同源校验，都在鉴权之前。
        # 顺序有意为之：先挡"根本不是给我们发的"请求（重绑定/跨站），
        # 再谈"你有没有密码"。这样攻击者连密码都不需要猜就已被拒绝。
        if not self._host_header_ok():
            return self._json({"ok": False,
                               "error": "拒绝访问：Host 头不是本机/内网地址"}, 400)

        # R251：浏览器插件直接推 Cookie。**必须排在同源校验与鉴权之前**：
        #   · 插件的 Origin 是 `chrome-extension://<id>`，与 Host 不同源，
        #     走同源校验必然 403 —— 而它恰恰是我们自己要用的客户端；
        #   · 插件也拿不到网页会话，不该让用户把网页密码填进插件里。
        # 所以这条路径的安全由**配对码**承担（`/api/cookie/ingest` 内校验），
        # 并且 Host 校验（上面那步）仍然生效 —— 防 DNS rebinding。
        if path == "/api/cookie/ingest":
            # ⚠ 必须包 _json()：这里返回的是**字典**，不写响应的话客户端
            # 会一直挂着等（探针实测：20 秒超时，日志里一行错都没有）。
            # `_status` 让「配对码错/太频繁」回 403 而不是 200 —— 插件能据此
            # 区分「连上了但没授权」和「软件根本没开」。
            _res = self._api_cookie_ingest(self._body())
            return self._json(_res, int(_res.pop("_status", 200)))

        if not self._origin_ok():
            return self._json({"ok": False,
                               "error": "拒绝访问：请求来源不是本机页面（防跨站请求伪造）"}, 403)

        # 登录/登出**必须在鉴权之前**：它们就是鉴权本身。
        if path == "/api/login":
            return self._do_login()
        if path == "/api/logout":
            return self._do_logout()
        # ---- R154-F20：扫码登录签发/确认（鉴权前：登录页未登录也要能用） ----
        if path == "/api/login/qrcode":
            return self._do_qr_request()
        if path == "/api/login/qrcode/confirm":
            return self._do_qr_confirm()

        if not self._auth_ok():
            return self._unauthorized()
        try:
            # V0.0.39：右下角状态栏的「完全退出」。只在**壳窗口**里有意义
            # （壳注入 quit_hook）；浏览器远程访问拿不到钩子，返回中文说明。
            if path == "/api/app/quit":
                return self._json(self._api_app_quit())
            # ---- V0.0.16：网页端维护动作（破坏性动作必须带 confirm）----
            if path == "/api/site/backfill":
                return self._json(self._api_site_backfill(self._body()))
            if path == "/api/site/backup-sync":
                return self._json(self._api_site_backup_sync(self._body()))
            if path == "/api/site/logs/clear":
                return self._json(self._api_site_logs_clear(self._body()))
            if path == "/api/site/quarantine/prune":
                return self._json(self._api_site_quarantine_prune(self._body()))
            if path == "/api/site/cleanup":
                return self._json(self._api_site_cleanup(self._body()))
            # ---- V0.0.14：应用校准后的合格线 / 导出训练素材集 ----
            if path == "/api/threshold/apply":
                return self._json(self._api_threshold_apply(self._body()))
            if path == "/api/export/training-kit":
                return self._json(self._api_training_kit(self._body()))
            # ---- V0.0.18：素材集导出前预览 / 通知中心已读与清空（逐字面量列出）----
            if path == "/api/export/kit/preview":
                return self._json(self._api_export_kit_preview(self._body()))
            if path == "/api/notifications/read":
                return self._json(self._api_notifications_read(self._body()))
            if path == "/api/notifications/clear":
                return self._json(self._api_notifications_clear(self._body()))
            if path == "/api/ui/mode":
                return self._json(self._api_ui_mode_set(self._body()))
            if path == "/api/image/action":
                return self._json(self._api_image_action(self._body()))
            # ---- V0.0.17：网页端补齐的新端点（逐字面量列出，护栏按源码判路由）----
            if path == "/api/image/reveal":
                return self._json(self._api_image_reveal(self._body()))
            if path == "/api/web/hotkeys":
                return self._json(self._api_web_hotkeys_set(self._body()))
            if path == "/api/db/master":
                return self._json(self._api_db_master_set(self._body()))
            if path == "/api/skins/upload":
                return self._json(self._api_skins_upload(self._body()))
            # V0.0.12：网页审核补桌面已有的三个能力——撤销上一步、随机抽检、
            # 智能抽样。撤销栈挂在 server 上常驻（handler 每请求新建，
            # 栈放 handler 上等于永远撤不了——R5050 那条 self.queue 的坑）；
            # 回滚逻辑与桌面 Ctrl+Z **同一个 ReviewQueue._undo_one**
            # （标志位 + reviews 行 + 学习权重 + 反馈账本 + 文件回搬，
            # 两处各写一份早晚分叉）。
            if path == "/api/review/undo":
                return self._json(self._api_review_undo())
            if path == "/api/review/sample":
                return self._json(self._api_review_sample(self._body()))
            if path == "/api/review/smart":
                return self._json(self._api_review_smart(self._body()))
            # V0.0.12：批量重命名（桌面图库菜单同款）。逻辑在 rename_ops
            # 共享模块——红线（用户原有图库不改名）只有一处实现。
            if path == "/api/image/rename":
                return self._json(self._api_image_rename(self._body()))
            # V0.0.12：手填词的词表桥接预演（桌面 dislike_dialog 同款）。
            # 用户在网页判退框里手填「金发」，得看见这会记到哪个词表标签上
            # ——没这一步，网页端的学习闭环对普通用户仍然是黑盒。
            if path == "/api/label-bridge/preview":
                return self._json(self._api_label_bridge_preview(self._body()))
            # ---- R228 / R154-F17：批量加 / 删标签 ----
            # ⚠ 刻意**独立路由**，不并进 `/api/image/action` 的四态机白名单：
            #   加/删标签不是审核决定，不该走 like/dislike/… 那张语义表，
            #   也不该碰 needs_review / in_quarantine（R81/R82 契约）。
            #   塞进白名单 ≈ 让「加个标签」变成一次审核动作。
            if path == "/api/image/tags":
                return self._json(self._api_image_tags_post(self._body()))
            if path == "/api/cleanup":
                return self._json(self._api_cleanup(self._body()))
            # R50：⚠ 必须用 `_task_id_of` 取 id，**不能**在这里直接
            # `int(parse_qs(u.query).get("id"))`。
            # 起因：适配器页的「启停」按钮（app.js 的 renderTable）走的是
            # **JSON body** `{task_id: N}`，而任务页按钮走 **query** `?id=N`。
            # 原来只认query → body 那条恒为 0，于是选择「启停」返回
            # `{"ok": true, "task_id": 0}`（**看起来成功了**）却根本没停。
            # 实测（tools/probe_r50_prod_http.py）：body 风格四个动作
            # 全部 task_id=0，query 风格才拿到真 id。
            # ⚠ 五个端点**逐个显式列出**，不要图省事改成 f-string 循环：
            #   tests/test_r48_web_features.py::test_new_endpoints_are_routed
            #   是按**源码里有没有这个字面量**判「接没接路由」的，
            #   循环展开成 `/api/task/{act}` 后字面量消失 → 护栏假红。
            #   （这类"静态护栏查不出语义、只能查字面量"的坑与 MEMORY
            #   第 8 条同型：护栏全绿 ≠ 功能对，反过来也一样。）
            if path == "/api/task/start":
                return self._json(self._api_task_ctl(
                    self._task_id_of(u), "start"))
            if path == "/api/task/stop":
                return self._json(self._api_task_ctl(
                    self._task_id_of(u), "stop"))
            if path == "/api/task/pause":
                return self._json(self._api_task_ctl(
                    self._task_id_of(u), "pause"))
            if path == "/api/task/delete":
                # V0.0.27：删除任务可以「同时删除图片」。
                # ⚠ body 只读一次（`_task_id_of` 内部也会读 body，重复读会拿到空），
                #   所以先读 body、再取 id：query 优先，body 兜底，与 _task_id_of
                #   同一口径（R50 那条不能破）。
                _dbody = self._body()
                _dqs = parse_qs(u.query)
                _draw = (_dqs.get("id") or [""])[0]
                if not _draw:
                    _draw = _dbody.get("task_id") or _dbody.get("id") or ""
                try:
                    _did = int(_draw)
                except (TypeError, ValueError):
                    _did = 0
                _dwi = bool(_dbody.get("with_images"))
                if not _dwi:
                    _dwi = str((_dqs.get("with_images") or [""])[0]).lower() in (
                        "1", "true", "yes", "on")
                return self._json(self._api_task_ctl(
                    _did, "delete", with_images=_dwi))
            if path == "/api/task/retry":
                return self._json(self._api_task_ctl(
                    self._task_id_of(u), "retry"))
            # ---- R154-F7：按 URL 粒度重试失败网址 / 清空失败清单 ----
            # ⚠ 与上面几个一样**逐个显式列出**，不改成 f-string 循环：
            #   tests/test_r48_web_features.py::test_new_endpoints_are_routed
            #   按源码字面量判「接没接路由」，循环展开后字面量消失 ⇒ 护栏假红。
            if path == "/api/task/retry-urls":
                return self._json(self._api_task_retry_urls(self._body()))
            if path == "/api/task/failed-urls/clear":
                return self._json(self._api_task_failed_urls_clear(self._body()))
            # ---- R81 #39：任务批量操作 ----
            # ⚠ 与上面五个一样**逐个显式列出**，不改成 f-string 循环：
            #   tests/test_r48_web_features.py::test_new_endpoints_are_routed
            #   按源码字面量判「接没接路由」，循环展开后字面量消失 ⇒ 护栏假红。
            if path == "/api/task/batch":
                return self._json(self._api_task_batch(self._body()))
            # ---- R154-F14：任务批量导入（上传 JSON，校验适配器后建任务） ----
            # ⚠ 同样**逐字面量列出**，不改成 f-string 循环：
            #   tests/test_r48_web_features.py::test_new_endpoints_are_routed
            #   按源码字面量判「接没接路由」，循环展开后字面量消失 ⇒ 护栏假红。
            if path == "/api/tasks/import":
                return self._json(self._api_task_import(self._body_json()))
            # ---- R154-F16：任务模板（R170 起「克隆」已移除）----
            # ⚠ 同上：**逐个显式字面量列出**，不改成 f-string 循环
            #   （test_r48_web_features::test_new_endpoints_are_routed 按字面量判）。
            if path == "/api/task/template/save":
                return self._json(
                    self._api_task_template_save(self._body_json()))
            if path == "/api/task/template/delete":
                return self._json(
                    self._api_task_template_delete(self._body_json()))
            # ---- R48/E：新增接口 ----
            if path == "/api/task/save":
                return self._json(self._api_task_save(self._body()))
            # 2026-10-27（编号 10）：静默添加 —— 用默认配置直接建任务，
            # 不弹表单。qB 的「不要自动开始下载」在网页端的等价物：
            # 用户粘贴一堆网址就想直接开工，不想过一遍 20 个字段。
            if path == "/api/task/quick":
                return self._json(self._api_task_quick(self._body()))
            # V0.0.55 批1：手机收件口 —— 安卓壳把分享来的链接 POST 到这里，
            # 自动匹配适配器建任务并立即运行。鉴权复用本管道（壳登录一次存 Cookie）。
            if path == "/api/inbox/link":
                return self._json(self._api_inbox_link(self._body()))
            # V0.0.55 批1：定时自动关机（任务跑完关 / 每日定点关 / 取消）
            if path == "/api/powerdown":
                return self._json(self._api_powerdown(self._body()))
            # ---- 2026-10-28：站点订阅的写操作（编号 21/22）----
            if path == "/api/sub/save":
                return self._json(self._api_sub_save(self._body()))
            if path == "/api/sub/delete":
                return self._json(self._api_sub_delete(self._body()))
            if path == "/api/sub/check":
                return self._json(self._api_sub_check(self._body()))
            if path == "/api/sub/preview":
                return self._json(self._api_sub_preview(self._body()))
            # 2026-10-28：在资源管理器里打开任务输出目录（qB「打开目标文件夹」）
            if path == "/api/task/reveal":
                return self._json(self._api_task_reveal(self._body()))
            if path == "/api/proxy/save":
                return self._json(self._api_proxy_save(self._body()))
            if path == "/api/proxy/delete":
                return self._json(self._api_proxy_delete(self._body()))
            # 2026-10-28：测代理延迟（桌面端「代理健康检查」的网页版）
            if path == "/api/proxy/check":
                return self._json(self._api_proxy_check(self._body()))
            if path == "/api/settings/save":
                return self._json(self._api_settings_save(self._body()))
            # ---- R144 #41：网页端添加 / 删除适配器（与桌面端同一个 rules_dir）----
            # ⚠ 与上面几组一样**逐个显式列出**，不改成 f-string 循环：
            #   tests/test_r48_web_features.py::test_new_endpoints_are_routed
            #   按源码字面量判「接没接路由」，循环展开后字面量消失 ⇒ 护栏假红。
            if path == "/api/system/autostart":
                return self._json(self._api_sys_autostart(self._body()))
            if path == "/api/adapter/ai":
                return self._json(self._api_adapter_ai(self._body()))
            if path == "/api/adapter/prompt":
                return self._json(self._api_adapter_prompt(self._body()))
            if path == "/api/adapter/from-reply":
                return self._json(self._api_adapter_from_reply(self._body()))
            if path == "/api/adapter/save":
                return self._json(self._api_adapter_save(self._body()))
            # V0.0.23：读一条已有规则的原文（网页端「编辑」「本地导入后核对」用）
            if path == "/api/adapter/get":
                return self._json(self._api_adapter_get(u))
            # V0.0.54：适配器市场 —— 改订阅源 / 安装规则（POST 侧）
            if path == "/api/market/manage":
                return self._json(self._api_market_manage(self._body()))
            if path == "/api/market/install":
                return self._json(self._api_market_install(self._body()))
            if path == "/api/adapter/delete":
                return self._json(self._api_adapter_delete(self._body()))
            # ---- R79 #41 / #44 ----
            # ⚠ 五个端点一样是**逐个显式列出**，不改成 f-string 循环：
            #   tests/test_r48_web_features.py::test_new_endpoints_are_routed
            #   按源码字面量判「接没接路由」，循环展开后字面量消失 ⇒ 护栏假红。
            if path == "/api/url/save":
                return self._json(self._api_url_save(self._body()))
            if path == "/api/url/delete":
                return self._json(self._api_url_delete(self._body()))
            # ⚠ 刻意叫 /api/learn/*，不给学习看板那个端点挂子路径：
            #   tests/test_r48_h_feature_smoke.py::test_meta_red_green_evidence_is_real
            #   按「把该端点名全文替换一次后不该再出现」做元变异，
            #   挂子路径会让它顶红（改那条绿着的测试，不如这里换个名字）。
            #   ⚠ 注释里也**不许**写那个端点全名 —— 写进来一样会被它扫到。
            if path == "/api/learn/delete":
                return self._json(self._api_learning_delete(self._body()))
            if path == "/api/learn/reset":
                return self._json(self._api_learning_reset(self._body()))
            # R174：只清「模型自动 + 负权」那批（不是全量重置，别混）
            if path == "/api/learn/clear-model-auto":
                return self._json(
                    self._api_learning_clear_model_auto(self._body()))
            # ---- R230 · 学习开关写入 ----
            # ⚠ 刻意**不进** /api/settings/save 的那套白名单：那边装的都是
            #   「改完要重启才生效」的字段，而这个开关是**立即生效**的，
            #   混在一起会让人以为「勾了没反应」。
            if path == "/api/learn/enabled":
                return self._json(self._api_learn_enabled_set(self._body()))
            # V0.0.7：纠错反馈词账本的删单条 / 清空（写操作走 POST）
            if path == "/api/feedback/remove":
                return self._json(self._api_feedback_remove(self._body()))
            if path == "/api/feedback/clear":
                return self._json(self._api_feedback_clear(self._body()))
            # V0.0.7：手动加一个「要找」的词（与桌面看板「加词」同一口径）
            if path == "/api/learn/add-term":
                return self._json(self._api_learn_add_term(self._body()))
            # ---- R232：立即备份 / 每日备份开关 / 导入偏好 ----
            # ⚠ 同样**逐个显式列出**（理由见本函数上面几组）。
            # ⚠ 「每日自动备份」开关刻意**不进** /api/settings/save 的白名单：
            #   那边装的都是「改完要重启才生效、值由启动流程读」的字段，
            #   而这个开关与桌面端读同一个 cfg 键，走独立路由更好查也更好测。
            if path == "/api/backup":
                return self._json(self._api_backup_now())
            # ---- R241：整库备份导出 / 上传 / 恢复（都是后台作业） ----
            # ⚠ 同样**逐个显式列出**（理由见本函数上面几组）。
            # ⚠ 导出与恢复都不在本线程跑完：几百 MB 缩略图压缩 + 恢复要等
            #   运行中任务收尾，放请求线程里必然撞 fetch 超时。
            if path == "/api/backup/full/prune":
                return self._json(self._api_full_prune())
            if path == "/api/backup/full/export":
                return self._json(self._api_full_export(self._body()))
            # R248：批量导入（从文件夹）—— 起后台作业 + 取消
            if path == "/api/import/folder":
                return self._json(self._api_import_folder(self._body()))
            if path == "/api/import/cancel":
                return self._json(self._api_import_cancel())
            # V0.0.44：从 WebDAV 目录导入（NAS / 网盘的 WebDAV 入口）
            if path == "/api/import/webdav":
                return self._json(self._api_import_webdav(self._body()))
            # V0.0.49：FTP / SFTP / SMB 网络协议导入
            if path == "/api/import/remote":
                return self._json(self._api_import_remote(self._body()))
            # V0.0.46：语义搜索 —— 按画面内容找图 / 补算历史图的特征向量
            if path == "/api/semantic/search":
                return self._json(self._api_semantic_search(self._body()))
            if path == "/api/semantic/backfill":
                return self._json(self._api_semantic_backfill(self._body()))
            if path == "/api/semantic/backfill/cancel":
                return self._json(self._api_semantic_backfill_cancel())
            # R251：站点 Cookie 管理（保存 / 删除 / 导入 / 换配对码）
            if path == "/api/cookies":
                return self._json(self._api_cookies_post(self._body()))
            # R249：磁盘级维护四件套。扫描/转换/修复都是**后台作业**
            # （10 万张逐张解码要几分钟），清理记录是同步的（毫秒级）。
            if path == "/api/maintenance/scan":
                return self._json(self._api_maint_start("scan", self._body()))
            if path == "/api/maintenance/orphans":
                return self._json(self._api_maint_start("orphans", self._body()))
            if path == "/api/maintenance/convert":
                return self._json(self._api_maint_start("convert", self._body()))
            if path == "/api/maintenance/repair":
                return self._json(self._api_maint_start("repair", self._body()))
            if path == "/api/maintenance/clean":
                return self._json(self._api_maint_clean(self._body()))
            if path == "/api/maintenance/cancel":
                return self._json(self._api_maint_cancel())
            if path == "/api/backup/full/upload":
                return self._json(self._api_full_upload(u))
            if path == "/api/backup/full/restore":
                return self._json(self._api_full_restore(self._body()))
            if path == "/api/backup/full/delete":
                return self._json(self._api_full_delete(self._body()))
            if path == "/api/backup/daily":
                return self._json(self._api_backup_daily(self._body()))
            if path == "/api/preferences/import":
                return self._json(self._api_prefs_import(self._body_json()))
            # ---- R146-C9：选用模型（写 cfg.ai_model_path 并落盘）----
            # ⚠ 同样**显式列出**（理由见上面的 /api/task/* 一组）。
            if path == "/api/model/use":
                return self._json(self._api_model_use(self._body()))
            if path == "/api/ai/gpu":
                return self._json(self._api_ai_gpu(self._body()))
            # ---- 2026-10-27：网页遥控下载模型（用户要求"网页上直接遥控软件下载"）----
            if path == "/api/model/download":
                return self._json(self._api_model_download(self._body()))
            if path == "/api/model/download/cancel":
                return self._json(self._api_model_download_cancel())
            # ---- 2026-10-27：在线模型列表（从 OpenAI 兼容接口拉 /models）----
            if path == "/api/ai/online/models":
                return self._json(self._api_online_models(self._body()))
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)
        # ⚠ R67：原来这里是英文 "unknown endpoint"，违反铁律「用户可见错误必须中文」。
        return self._json({"ok": False, "error": "未知接口"}, 404)

    def _body_json(self):
        """读请求体并解析为 JSON（数组或对象皆可），解析失败/为空返回 ``{}``。

        R154-F14：导入任务是 JSON **数组**，而 :meth:`_body` 只认 dict
        （数组会被它强制转成 ``{}``），所以导入接口必须走这个不强制 dict 的版本。
        共享与 :meth:`_body` 同一套 Content-Length / 1MB 上限 / 超限读净断连逻辑。
        """
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        _MAX = 1024 * 1024
        if n > _MAX:
            left = n
            try:
                while left > 0:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    left -= len(chunk)
            except Exception:
                pass
            self.close_connection = True
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---- B3 #48 登录 / 登出 ----
    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        # 上限 1MB：清理/批量动作的请求体再大也不该超过这个量级，
        # 防止一个畸形 Content-Length 把内存吃光。
        # R152-E2：超限时**必须把剩余 body 读干净并断连** —— 原来只读前
        # 1MB 不断连接，残留字节留在缓冲里会被下一条请求当成请求行解析
        # （keep-alive 协议错乱，轻则 400 重试、重则连接挂死）。
        _MAX = 1024 * 1024
        if n > _MAX:
            left = n
            try:
                while left > 0:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    left -= len(chunk)
            except Exception:
                pass
            self.close_connection = True
            return {}
        raw = self.rfile.read(n)
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def _task_id_of(self, u) -> int:
        """R50：取任务控制类接口的任务编号，**query 与 body 两个来源都认**。

        ⚠ 为什么要两个都认（这是实测抓到的真缺陷，不是防御性编程）：
        前端有**两条**风格不同的调用路径，各自只发一半：
          · 任务页按钮  → `/api/task/start?id=7`      （query）
          · 适配器页按钮 → `/api/task/start` + body `{task_id: 7}`（JSON）
        原来只读 query，body 那条就恒为 0，而返回值还是 `ok: true`
        （**看起来点了没反应才是最难查的那种**）。

        query 优先于 body：同一请求两者都带时以 URL 为准，
        与「显式路径参数优先」的直觉一致。
        """
        raw = (parse_qs(u.query).get("id") or [""])[0]
        if not raw:
            body = self._body()
            raw = body.get("task_id") or body.get("id") or ""
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def _do_login(self):
        """R48/F：登录只认**密码**，用户名不再要求输入。

        - 没设密码 → 直接放行（返回 ok，前端直接进主界面）；
        - 设了密码 → 密码对就发会话 cookie。

        ⚠ 之前 `if not want or not hmac.compare_digest(...)` 把
          「没设密码」判成「密码不对」→ 用户不设密码就永远登不进去，
          正是作业单 F 要去掉的那个强制校验。
        ⚠ R52：不在免密范围（非局域网/白名单）的客户端，即便没设密码也
          不允许"登录成功"——否则会出现"登录返回 ok，但首页数据全 401"的
          诡异状态。这里直接 401，让前端如实提示。
        """
        body = self._body()
        # R152-E3：锁定期间直接 429，不再比对密码（爆破成本从 0 变成要等）。
        # ⚠ 只在"真的在试密码"时计数（见下方失败分支），免密/未设密码的
        # 客户端走别的分支，不会被误锁。
        wait = self._login_gate()
        if wait > 0:
            secs = int(wait) + 1
            return self._json(
                {"ok": False,
                 "error": f"密码错误次数太多，请 {secs} 秒后再试"},
                429, extra={"Retry-After": str(secs)})
        pw = str(body.get("password") or "")
        # R105：Web 只认密码，不再区分用户（见 _auth_ok 注释）。
        user = DEFAULT_USER
        want = str(self.cfg.web_password or "")
        # ⚠⚠ R68：`hmac.compare_digest` 对**非 ASCII 的 str 直接抛 TypeError**
        #   （CPython 只支持 ASCII str 或 bytes）。原代码直接拿用户输入的 str
        #   去比，于是：
        #     · 密码含中文/emoji ⇒ **敲对了也抛** ⇒ 登录处理器崩 ⇒ 连接被掐断
        #       （客户端看到 RemoteDisconnected，不是 401）⇒ 拿不到 cookie
        #       ⇒ **Web 端被永久锁在门外**，只能回桌面端改密码。而桌面端那个
        #       密码框只校验长度 ≥4、**没有任何 ASCII 限制**（main_window.py
        #       的 Web 设置对话框），中文密码是完全可达的正常输入。
        #     · 反向：密码是 ASCII，用户输入法没切回英文打了中文 ⇒ 同样掐断
        #       连接，而不是回一句「密码不对」。
        #   实测证据：tools/r68_repro_nonascii_pw.py（6 组输入 4 组抛）。
        #   正解：两边都 encode 成 bytes 再比 —— ASCII 情况下逐字节相同，
        #   语义不变；中文/emoji 也能安全比较（False 而不是抛异常）。
        #   ⚠ 同文件另两处比密码的地方**本来就是安全的**，别顺手改：
        #     · `check_auth` 比的是 base64（纯 ASCII）；
        #     · `verify_session` 比的是 hexdigest（纯 ASCII）；
        #     · crypt.py 那处比的是 bytes。
        #   ⇒ 全仓唯一的暴露点就是这一行，改这一处即可。
        if want and not hmac.compare_digest(
                pw.encode("utf-8"), want.encode("utf-8")):
            # R152-E3：记失败 + 审计。审计写 db.audit_log，桌面端"安全"页
            # 能翻到；db 不在（极少数测试替身）时静默跳过，不影响限流本身。
            fails = self._login_failed()
            try:
                self.db.audit(
                    "web", "login-failed", self._client_ip(),
                    f"consecutive_fails={fails}")
            except Exception:
                pass
            return self._json({"ok": False, "error": "密码不对"}, 401)
        # ⚠⚠ R58 修复：这里原来只判`if not self._client_allowed_by_ip():`，
        #   **漏了 `not want`**（设了密码的情形）。后果不是「多拒一点」，
        #   而是把整个密码机制变成死路：
        #     · 公网客户端 + 设了正确密码 → 登录被拒，拿不到 cookie；
        #     · 而 `_auth_ok()`（449-450 行 R52 自己的注释）明写
        #       「不在免密范围 + **设了密码** → 仍要密码（Basic Auth 或会话 cookie）」
        #       并且认Basic Auth 与 cookie；
        #     · 于是「能生成的东西不被自己承认」——用户在外面设了密码
        #       却永远登不进来，且没有任何办法绕过（重设密码也没用）。
        #   正确条件与上面那句注释一一对应：**只有「没设密码」且「不在免密范围」
        #   才拒**（那种情况下前端不该拿到"登录成功"却满屏 401）。
        #   判据用探针跑出来，不是读代码猜的：tools/_r58_probe_login_deadlock.py
        if not want and not self._client_allowed_by_ip():
            return self._json(
                {"ok": False,
                 "error": "当前网络不在免密范围，请在桌面端「设置→Web」里设置 Web 密码后再登录"},
                401)
        # 免密也发一个会话 cookie：前端状态一致，且日后设了密码不用改前端
        # R152-E3：登录成功清零失败计数（正当用户不被历史失败连坐）。
        self._login_succeeded()
        tok = make_session(user, want)
        cookie = self._session_cookie(tok)
        return self._json({"ok": True, "user": user, "password_required": bool(want)},
                           extra={"Set-Cookie": cookie})

    def _session_cookie(self, tok: str, max_age: int = 0) -> str:
        """拼会话 cookie。2026-10-28：HTTPS 下自动加 Secure（qBittorrent 同款开关）。

        ⚠ 只有真 TLS 连接才加 Secure：HTTP 上带 Secure 的 cookie 浏览器会丢弃，
        症状是「输入密码点登录，页面闪一下又回登录页」，排查起来极其费时。
        """
        age = max_age or _SESSION_TTL
        tls = False
        try:
            tls = hasattr(self.connection, "cipher")   # SSLSocket 才有 cipher()
        except Exception:
            tls = False
        secure = "; Secure" if (tls and bool(getattr(self.cfg, "web_secure_cookie", True))) else ""
        return (f"{_COOKIE_NAME}={tok}; Path=/; HttpOnly; SameSite=Strict; "
                f"Max-Age={age}{secure}")

    def _do_logout(self):
        # Max-Age=0 = 立刻删 cookie。HttpOnly/SameSite 与登录时一致。
        cookie = self._session_cookie("", max_age=1).replace(
            "Max-Age=1", "Max-Age=0")
        return self._json({"ok": True}, extra={"Set-Cookie": cookie})

    # ---- R154-F20：扫码登录端点（路由在鉴权前：登录页未登录也要能用；
    #      它们不泄露任何数据，confirm 才建立登录态） ----
    def _do_qr_request(self):
        """签发一次性扫码登录 token（5 分钟有效）。"""
        # 签发限流：每 IP 每分钟最多 _QR_ISSUE_MAX 次（防弹窗轰炸）。
        st = getattr(self.server, "_qr_issued", None)
        lk = getattr(self.server, "_qr_lock", None)
        if st is not None and lk is not None:
            ip = self._client_ip()
            now = time.time()
            with lk:
                lst = [t for t in st.get(ip, [])
                       if now - t < _QR_ISSUE_WINDOW]
                if len(lst) >= _QR_ISSUE_MAX:
                    st[ip] = lst
                    return self._json(
                        {"ok": False, "error": "签发太频繁，请稍后再试"}, 429)
                lst.append(now)
                st[ip] = lst
        _qr_gc()
        tok = secrets.token_urlsafe(24)
        ip = self._client_ip()
        # 二维码内容 = 确认页完整 URL（Host 头优先，保证手机同网段可达）
        host = self.headers.get("Host") or ""
        if not host:
            bind = str(getattr(self.cfg, "web_bind", "127.0.0.1") or "127.0.0.1")
            host = "127.0.0.1" if bind in ("0.0.0.0", "::") else bind
            try:
                host = f"{host}:{self.server.server_address[1]}"
            except Exception:
                host = f"{host}:8080"
        url = f"http://{host}/qr-login?token={tok}"
        with _QR_LOCK:
            _QR_TOKENS[tok] = {"created_at": time.time(), "status": "pending",
                               "ip": ip, "url": url}
        # 通知桌面端 GUI 弹授权框（hook 由 main_window 注入；headless 无
        # hook 时照常签发，确认页会等到过期——不阻塞密码登录）。
        hook = getattr(self.server, "qr_request_hook", None)
        if hook is not None:
            try:
                hook(tok, ip)
            except Exception:
                pass
        try:
            self.db.audit(self._actor(), "qr-login-request", ip,
                          f"token_len={len(tok)}")
        except Exception:
            pass
        return self._json({"ok": True, "token": tok, "url": url,
                           "expires_in": int(_QR_TOKEN_TTL)})

    def _qr_rec(self, tok):
        """取 token 记录；不存在/已过期返回 None（过期顺手清除）。"""
        if not tok:
            return None
        _qr_gc()
        with _QR_LOCK:
            rec = _QR_TOKENS.get(tok)
            if rec is None:
                return None
            if time.time() - rec["created_at"] > _QR_TOKEN_TTL:
                _QR_TOKENS.pop(tok, None)
                return None
            return rec

    def _do_qr_image(self, u):
        """二维码 SVG（segno 生成，内容 = 签发时存的确认页 URL）。"""
        tok = (parse_qs(u.query).get("token") or [""])[0]
        rec = self._qr_rec(tok)
        if rec is None:
            return self._json({"ok": False, "error": "二维码已失效"}, 410)
        try:
            from segno import make
            svg = make(rec["url"], error="m").svg_inline(scale=4, border=2)
        except Exception as e:
            return self._json(
                {"ok": False, "error": f"二维码生成失败：{e}"}, 500)
        return self._send(200, svg.encode("utf-8"), "image/svg+xml")

    def _do_qr_status(self, u):
        """确认页轮询：token 状态 + 剩余有效秒数。"""
        tok = (parse_qs(u.query).get("token") or [""])[0]
        rec = self._qr_rec(tok)
        if rec is None:
            return self._json({"ok": False, "status": "expired"})
        left = max(0, int(_QR_TOKEN_TTL
                          - (time.time() - rec["created_at"])))
        return self._json({"ok": True, "status": rec["status"],
                           "expires_in": left})

    def _do_qr_confirm(self):
        """确认页自动提交：approved 且未用 → 发会话 cookie。
        **复用 make_session**（与密码登录同一机制：改密码即失效）。"""
        body = self._body()
        tok = str(body.get("token") or "")
        rec = self._qr_rec(tok)
        if rec is None:
            return self._json({"ok": False, "error": "二维码无效或已过期"}, 401)
        if rec["status"] == "used":
            return self._json({"ok": False, "error": "该二维码已使用"}, 401)
        if rec["status"] != "approved":
            return self._json({"ok": False, "error": "尚未获得桌面端授权"}, 403)
        with _QR_LOCK:
            rec["status"] = "used"          # 一次性
        want = str(self.cfg.web_password or "")
        self._login_succeeded()             # R152-E3：登录成功清零失败计数
        session = make_session(DEFAULT_USER, want)
        cookie = self._session_cookie(session)
        try:
            self.db.audit(self._actor(), "qr-login-confirm", rec["ip"],
                          f"user={DEFAULT_USER}")
        except Exception:
            pass
        return self._json({"ok": True, "user": DEFAULT_USER},
                          extra={"Set-Cookie": cookie})

    def _do_qr_page(self, u):
        """/qr-login?token=xxx：手机扫码后的确认页。"""
        tok = (parse_qs(u.query).get("token") or [""])[0]
        if not tok:
            return self._html(400, "<h1>缺少 token</h1>")
        if self._qr_rec(tok) is None:
            return self._html(410, "<h1>二维码已过期</h1>"
                                    "<p>请回登录页重新获取。</p>")
        return self._html(200, _QR_PAGE_HTML)

    # ---- 图片 ----
    def _serve_image(self, u):
        """R50：id **在路径里**（`/thumb/<id>`），query 只是兼容。

        ⚠ 这是一个**整页都崩**的缺陷，而且业务方法单测完全测不到：
        后端自己发给前端的 URL 就是 `/thumb/{id}`（见 `_img_item`），
        而这里却只从 **query string** 取 `?id=` → 恒为 0 → 恒 404。
        后果：Web 端图库/待审核页**每一张图都是碎图**，
        而任何直接 new handler 调 `_api_images()` 的单测都看不到
        （它们只检查 JSON 里的 url 字段长对不对，不去真的请求那个 url）。

        两个来源都认：query 优先（老书签），path 兜底（真实前端路径）。
        """
        iid = 0
        m = re.match(r"^/thumb/(\d+)", u.path or "")
        if m:
            iid = int(m.group(1))
        if not iid:
            q = parse_qs(u.query)
            try:
                iid = int((q.get("id") or ["0"])[0])
            except ValueError:
                iid = 0
        if not iid:
            return self._html(404, "not found")
        row = self.db.query("SELECT path FROM images WHERE id=?", (iid,))
        if not row or not row[0]["path"]:
            return self._html(404, "not found")
        p = Path(row[0]["path"])
        if not p.exists():
            return self._html(404, "文件已不在磁盘")
        # 2026-10-27 性能修复：`/thumb/` 以前**直接发原图**（1~20MB/张）。
        # 图库一屏 60 格 = 下载几百 MB，浏览器解码也扛不住 → 用户实测
        # 「正常浏览都卡，加标签筛选更卡」（其实就是同一件事：原图太大）。
        # 现在：按 ?w=（默认 360）用 Pillow 生成真缩略图，落盘缓存
        # data/thumbs/<id>_<w>.jpg，并给浏览器长缓存。
        try:
            _w = int((parse_qs(u.query).get("w") or ["360"])[0])
        except ValueError:
            _w = 360
        _w = max(96, min(1280, _w))
        thumb = self._thumb_for(p, iid, _w)
        if thumb is not None:
            ok = self._send_file(
                thumb, "image/jpeg",
                {"Cache-Control": "public, max-age=604800, immutable"})
            if ok:
                return
        # 生成失败（缺 Pillow / 坏图）→ 退回原图，功能优先于性能
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        # R152-E10：走流式 + Range（≥1MB 分块），不再整张 read_bytes
        if not self._send_file(p, ctype):
            return self._html(404, "读取失败")

    def _ai_model_is_small(self) -> bool:
        """当前 AI 模型是不是"小模型"（<200MB）——决定 auto 档走 CPU 还是显卡。

        与 ``classify/clip_onnx.ClipONNX`` 里的判定同一条线（那边按
        model_path.stat().st_size），两处口径必须一致。
        """
        try:
            p = str(getattr(self.cfg, "ai_model_path", "") or "").strip()
            from pathlib import Path as _P
            if p:
                cand = _P(p)
                if cand.is_dir():
                    cand = cand / "model_quantized.onnx"
                if cand.exists():
                    return cand.stat().st_size < 200 * 1024 * 1024
            # 内置模型：走产品自己的解析函数（打包/源码都能找到）
            from .models_lib import resolve_default_model_dir
            from pathlib import Path as _P2
            d = resolve_default_model_dir(None)
            if d:
                b = _P2(d) / "model_quantized.onnx"
                if b.exists():
                    return b.stat().st_size < 200 * 1024 * 1024
        except Exception:
            pass
        return True

    def _thumb_for(self, src: Path, iid: int, w: int) -> Optional[Path]:
        """缩略图（**幂等、带磁盘缓存**）：data/thumbs/<id>_<w>.jpg。

        · 源文件比缓存旧 ⇒ 直接用缓存（毫秒级）；
        · 否则用 Pillow 按最长边 w 缩放、JPEG q=82 落盘；
        · 任何异常返回 None，调用方退回原图（图片坏了也要能看见）。
        """
        try:
            from .config import user_data_dir
            cdir = user_data_dir() / "thumbs"
            cdir.mkdir(parents=True, exist_ok=True)
            # R247：缩略图改为**分桶存放**（thumbs/a3/f/<id>_<w>.jpg）。
            # 原来全部平铺在 thumbs/ 一层，十万张就是十几万个小文件挤在
            # 一个目录里 —— 备份、完整性检查、rsync 都会退化成龟速。
            # 旧路径仍然认（老文件不白瞎），新生成的一律落新路径。
            from . import thumbpath as _tp
            name = f"{iid}_{w}.jpg"
            out = _tp.cache_path(cdir, name)
            for _cand in (out, _tp.legacy_path(cdir, name)):
                try:
                    if (_cand.exists()
                            and _cand.stat().st_mtime >= src.stat().st_mtime):
                        return _cand
                except OSError:
                    pass
            out.parent.mkdir(parents=True, exist_ok=True)
            from PIL import Image
            with Image.open(src) as im:
                # draft 让 JPEG 解码时就直接降采样，大图快一个数量级
                try:
                    im.draft("RGB", (w * 2, w * 2))
                except Exception:
                    pass
                im = im.convert("RGB")
                im.thumbnail((w, w), Image.LANCZOS)
                # ⚠ 2026-10-27 走查：临时文件名原来是固定的 <id>_<w>.tmp，
                # 两个请求同时生成同一张时会互踩（一个读到半个文件）。
                # 改成带 pid+线程 id 的唯一名，再原子 rename。
                import os as _os
                import threading as _th
                tmp = out.with_suffix(f".{_os.getpid()}_{_th.get_ident()}.tmp")
                im.save(tmp, "JPEG", quality=82, optimize=True)
                tmp.replace(out)
            return out
        except Exception:
            return None

    def _serve_original(self, u):
        """#46 第 29 项：下载**原图**（`/orig/<id>`）。

        与 `/thumb/` 的区别只有一个 —— 带 `Content-Disposition: attachment`
        让浏览器**存盘**而不是内嵌显示。手机上尤其重要：不带这个头，
        选择「下载原图」只会把图片在当前页打开一下，用户以为没反应。

        ⚠ 文件名走 RFC 5987 的 ``filename*=UTF-8''``：中文文件名在
        老浏览器上会变成乱码（`filename="?.jpg"`）。ascii 那个是给
        老客户端兜底的 fallback。
        """
        m = re.match(r"^/orig/(\d+)", u.path or "")
        if not m:
            return self._html(404, "not found")
        iid = int(m.group(1))
        row = self.db.query("SELECT path FROM images WHERE id=?", (iid,))
        if not row or not row[0]["path"]:
            return self._html(404, "not found")
        p = Path(row[0]["path"])
        if not p.exists():
            return self._html(404, "文件已不在磁盘")
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        name = p.name
        ascii_name = re.sub(r'[^\x20-\x7e]', "_", name).replace('"', "_")
        disp = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
            ascii_name, quote(name, safe=""))
        # R152-E10：走流式 + Range（≥1MB 分块），不再整张 read_bytes
        if not self._send_file(p, ctype, {"Content-Disposition": disp}):
            return self._html(404, "读取失败")

    # ---------------- JSON API ----------------

    def _api_summary(self):
        """导航角标 + 底栏用的计数（7 个 COUNT）。

        ⚠ 2026-10-29：底栏每 5 秒轮询一次，而这里 7 条 COUNT 里 4 条走的是
        「in_quarantine=0 AND blacklisted=0」这种**没有索引**的扫表条件 ——
        10 万行约几十毫秒、100 万行就是几百毫秒，等于底栏每秒吃掉一次扫表。
        加 3 秒缓存：底栏本来 5 秒刷一次，3 秒的陈旧度用户看不出来，
        但扫表频率从「每次轮询」降到「三分之一次」。

        ⚠ **拿不到就整组不返回**（宁可不显示，也不回 0）—— 回 0 会让用户
        以为图库空了，这比慢半拍严重得多。
        """
        now = time.time()
        cache_key = id(self.db)
        with _SUMMARY_CACHE_LOCK:
            if (_SUMMARY_CACHE["data"] is not None
                    and _SUMMARY_CACHE["key"] == cache_key
                    and now - _SUMMARY_CACHE["ts"] < 3.0):
                return dict(_SUMMARY_CACHE["data"], cached=True)
        try:
            q = lambda s: self.db.query(s)[0]["c"]      # noqa: E731
            data = {
                "ok": True,
                "tasks": q("SELECT COUNT(*) c FROM tasks WHERE is_template=0"),
                "images": q("SELECT COUNT(*) c FROM images "
                            "WHERE in_quarantine=0 AND blacklisted=0"),
                "pending": q("SELECT COUNT(*) c FROM images "
                             "WHERE needs_review=1"),
                # R143：隔离区计数用与列表**同一口径**（见 _api_view 的
                # where 映射注释），不再另写一份 SQL。
                "quarantine": q("SELECT COUNT(*) c FROM images "
                                "WHERE in_quarantine=1 AND needs_review=0"),
                # R167：导航「图库」角标口径（= 图库视图同款 WHERE），
                # 与桌面端 `_nav_badge_counts()['gallery']` 及
                # event_snapshot 三方逐字一致。
                "gallery": q("SELECT COUNT(*) c FROM images "
                             "WHERE in_quarantine=0 AND blacklisted=0 "
                             "AND path IS NOT NULL"),
                # 2026-10-28：网页底栏要像 qBittorrent 那样显示"在跑几个/排队几个"。
                # 口径直接用 tasks.status，与任务页状态树同一份数据，不另算。
                "running": q("SELECT COUNT(*) c FROM tasks "
                             "WHERE status='running'"),
                "queued": q("SELECT COUNT(*) c FROM tasks "
                            "WHERE status='queued'"),
                "error": q("SELECT COUNT(*) c FROM tasks WHERE status='error'"),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}
        with _SUMMARY_CACHE_LOCK:
            _SUMMARY_CACHE["ts"] = now
            _SUMMARY_CACHE["data"] = data
            _SUMMARY_CACHE["key"] = cache_key
        return dict(data, cached=False)

    def _summary_cache_clear(self):
        with _SUMMARY_CACHE_LOCK:
            _SUMMARY_CACHE["ts"] = 0.0
            _SUMMARY_CACHE["data"] = None
            _SUMMARY_CACHE["key"] = None

    def _api_stats(self):
        """网页底栏的实时速率（对标 qBittorrent 状态栏那一行 ↓ 12.3 MB/s）。

        为什么不用"下载器内部的字节计数器"：采集器的并发是「每任务一个引擎 +
        gallery-dl 子进程」，字节数散落在十几处，汇总必然漂移。而**图片落库那一刻
        是唯一可信的收敛点** —— `images.collected_at` + `images.bytes` 天然带
        时间戳和大小，算出来的就是「真写进库的速率」，不会被重试/去重重复计数。

        口径：
        - `rate_bps`：最近 60 秒新增图片的字节数 ÷ 60
        - `per_min`：最近 10 分钟**每分钟新增张数**（画清空前的小柱图）
        - `today` / `today_bytes`：当天（UTC 日界，与 collected_at 同口径）
        """
        try:
            q = lambda s, a=(): self.db.query(s, a)          # noqa: E731
            last = q("SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS b "
                     "FROM images WHERE collected_at >= datetime('now','-60 seconds')")[0]
            today = q("SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS b "
                      "FROM images WHERE collected_at >= date('now')")[0]
            rows = q("SELECT strftime('%Y-%m-%d %H:%M', collected_at) AS m, "
                     "COUNT(*) AS n FROM images "
                     "WHERE collected_at >= datetime('now','-11 minutes') "
                     "GROUP BY m")
            have = {r["m"]: int(r["n"]) for r in rows}
            # 补齐空分钟：前端要按固定 10 格画柱子，缺格会让柱子"跳位"。
            import datetime as _dt
            now = _dt.datetime.utcnow().replace(second=0, microsecond=0)
            per_min = []
            for k in range(9, -1, -1):
                key = (now - _dt.timedelta(minutes=k)).strftime("%Y-%m-%d %H:%M")
                per_min.append(have.get(key, 0))
            # 2026-10-28（对标 qBittorrent 状态栏右侧的剩余空间）：
            # 采集器最容易踩的坑是"下到一半盘满了"，而网页端此前完全看不到。
            # 测的是**存图那个盘**（last_output_dir 所在卷），不是软件安装盘 ——
            # 用户把图存到 F: 时，报 C: 的剩余空间毫无意义。
            disk = {}
            try:
                from .diskspace import free_space, format_size
                root = Path(str(getattr(self.cfg, "last_output_dir", "") or "") or ".")
                free = free_space(root)
                if free is not None:
                    import shutil as _sh
                    total = int(_sh.disk_usage(str(root)).total)
                    warn_mb = int(getattr(self.cfg, "min_free_mb", 0) or 0)
                    disk = {
                        "disk_free": int(free),
                        "disk_total": total,
                        "disk_low": bool(warn_mb > 0
                                         and free < warn_mb * 1024 * 1024),
                        "disk_warn_mb": warn_mb,
                        "disk_text": format_size(free),
                        # 盘符不好从 Path 里可靠取出（网络盘/UNC），交给前端显示路径
                        "disk_path": str(root),
                    }
            except Exception:                       # noqa: BLE001
                disk = {}                            # 测不到就当没有，底栏不显示
            return {
                "ok": True,
                **disk,
                "rate_bps": round(float(last["b"] or 0) / 60.0, 1),
                "last_min": int(last["n"] or 0),
                "per_min": per_min,
                "today": int(today["n"] or 0),
                "today_bytes": int(today["b"] or 0),
                # 让前端能算"每分钟多少张"而不用自己等 60 秒
                "per_min_avg": round(sum(per_min) / 10.0, 1),
            }
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def _api_task_reveal(self, body):
        """在资源管理器里打开任务的输出目录（对标 qBittorrent「打开目标文件夹」）。

        ⚠ 两个必须说清的前提，否则会变成"点了没反应"或"打错地方"：
        1. **打开的是运行服务那台机器**的资源管理器。手机/别的电脑访问网页时，
           弹窗会出现在电脑上，手机上什么都不发生 —— 所以远程访问时明确提示。
        2. 目录可能已被用户删掉/挪走 → 必须报中文错，不能静默。
        """
        try:
            tid = int(body.get("id") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "任务编号不对"}
        from .repo import get_task
        t = get_task(self.db, tid)
        if t is None:
            return {"ok": False, "error": f"任务 #{tid} 不存在"}
        raw = str(getattr(t, "output_dir", "") or "").strip()
        if not raw:
            return {"ok": False, "error": "这个任务没设输出目录（用的是全局默认目录）"}
        d = Path(raw).expanduser()
        try:
            if not d.is_dir():
                return {"ok": False, "error": f"目录不存在：{d}"}
        except OSError as e:
            return {"ok": False, "error": f"目录打不开：{e}"}
        # 远程访问就别假装能打开了：告诉用户"在哪台机器上"比静默失败有用
        try:
            host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        except Exception:
            host = ""
        if host and host not in ("127.0.0.1", "localhost", "::1"):
            return {"ok": False, "error":
                    "这是远程访问：目录只能在运行软件的那台电脑上打开"
                    f"（路径：{d}）"}
        try:
            os.startfile(str(d))          # 仅 Windows；只在服务端执行
        except AttributeError:
            return {"ok": False, "error": "当前系统不支持自动打开目录，路径是：" + str(d)}
        except OSError as e:
            return {"ok": False, "error": f"打开失败：{e}"}
        return {"ok": True, "path": str(d)}

    def _api_settings(self):
        """设置页（B4）：**部分可写**。

        R50：作业单 Web 段点名要「皮肤三选、水印开关与透明度、AI 开关」
        在网页上可用，所以这三个（加上 `ui_preset`）从"只读"改成"可改"，
        由 `_SETTINGS_WRITABLE` 白名单 + `/api/settings/save` 承接。

        仍然**只读**的：web_bind / web_port / web_password / web_user ——
        那三项改了要重启服务才生效，在网页上改只会给人「改了没反应」，
        甚至把自己关在门外。分工写在 `_note` 里，别让用户猜。
        """
        out = {}
        for k in ("web_bind", "web_port", "web_user", "web_password",
                  "ui_theme", "ai_gpu", "blur_sensitive", "review_key_ask"):
            if not hasattr(self.cfg, k):
                continue
            v = getattr(self.cfg, k)
            # ⚠ 密码**绝不回传给网页**，哪怕它是明文存的也不行：
            # 这页在局域网里是裸奔的，回显密码等于把它摊给每个连上来的人。
            # 密码仍然**不回显明文**（局域网裸奔，回显=把密码摊给每个连上来的人）。
            # 但前端要能编辑，所以给空串 + 有无密码的标记。
            if k == "web_password":
                out["web_password_set"] = bool(v)
                out[k] = "（已设置）" if v else "（未设置）"
            else:
                out[k] = v
        # ---- R50：可改项的当前值（表单要靠它预选/预填） ----
        # ⚠ 必须走 `self._SETTINGS_WRITABLE`，不能写模块级裸名：
        #   白名单是**类属性且定义在文件更下方**，类体里前向引用不到，
        #   裸名会被当全局去找 → NameError → /api/settings 整个 500。
        #   （这坑只在真起服务时才暴露：单测直接 new handler 也会炸，
        #     但如果只用 mock 就完全看不见。）
        for k in sorted(self._SETTINGS_WRITABLE):
            if not hasattr(self.cfg, k):
                continue
            if k in self._SETTINGS_SECRETS:
                # ⚠ 这个循环在白名单里会**再写一遍**，把上面脱敏成
                # "（已设置）" 的值覆盖回明文 —— 2026-10-27 实测泄漏。
                # 密钥永远不在这里回显，另给一个 *_set 标记表明有没有。
                _has = bool(getattr(self.cfg, k))
                out[f"{k}_set"] = _has
                out[k] = "（已设置）" if _has else "（未设置）"
                continue
            _v = getattr(self.cfg, k)
            # ⚠ Path 不能直接进 JSON（TypeError → 整个 /api/settings 500）。
            # 2026-10-27 新增 library_dir 等 Path 字段后就踩到了。
            out[k] = str(_v) if isinstance(_v, Path) else _v
        # 皮肤可选项：前端**不要**自己写死三个名字，一律用这份列表渲染。
        out["_presets"] = [
            {"key": k, "label": _preset_label_safe(k)}
            for k in _theme_presets()
        ]
        # F24：限速计划的**只读摘要**（时段表只能在桌面端改，见白名单注释）。
        # 网页上给一句人话，免得用户以为「开关开了但什么都没配」。
        try:
            from .rateplan import current_limit_text, plan_status, plan_summary
            out["rate_plan_text"] = plan_summary(getattr(self.cfg, "rate_plan", None))
            out["rate_plan_now"] = current_limit_text(
                getattr(self.cfg, "rate_plan", None),
                bool(getattr(self.cfg, "rate_plan_enabled", False)))
            # V0.0.41：界面状态条要「现在几点、现在怎么限、下次什么时候变」。
            # 只给一句「不限速」，用户会以为计划没生效（实际是夜里那档本来就不限）。
            out["rate_plan_status"] = plan_status(
                getattr(self.cfg, "rate_plan", None),
                bool(getattr(self.cfg, "rate_plan_enabled", False)))
        except Exception:
            pass
        out["_writable"] = sorted(self._SETTINGS_WRITABLE)
        # 2026-10-28（编号 23/32）：HTTPS 实况 + 可选源地址。
        # ⚠ 这里给的是**服务端实际状态**（https_port>0 才是真起来了），
        #   不是"配置里勾了"—— 勾了但证书起不来时界面要能看出区别。
        try:
            srv = getattr(self.server, "_mrm_webserver", None)
            if srv is not None:
                out["_https_url"] = srv.https_url
                out["_https_error"] = srv.https_error
        except Exception:
            pass
        try:
            from .tls import cert_info
            out["_cert"] = cert_info(
                Path(str(getattr(self.cfg, "db_path", ""))).parent / "tls")
        except Exception:
            pass
        try:
            from .nets import list_local_ips
            out["_local_ips"] = list_local_ips()
        except Exception:
            out["_local_ips"] = []
        out["_note"] = ("皮肤 / 水印 / AI 开关 / 限速 / 安全项都能在网页上改；"
                        "监听地址、端口、HTTPS 开关改完需要重启 Web 服务才生效")
        return out

    # ---- B2 #47 + B4 #49：分批视图 ----
    def _api_view(self, view: str, u):
        if view not in VIEWS:
            return {"ok": False, "error": f"未知视图 {view}"}
        q = parse_qs(u.query)
        limit = _clamp_limit((q.get("limit") or [""])[0])
        try:
            cursor = int((q.get("cursor") or [""])[0])
        except ValueError:
            cursor = 0
        # #36 排序 + #23 搜索：两个参数对非图片视图被忽略（那些视图自己
        # 渲染，不走 _api_images），只有图库类四视图会真用上。
        sort = _norm_sort((q.get("sort") or [""])[0])
        kw = (q.get("q") or [""])[0].strip()[:80]
        # 2026-10-27 用户要求：图库/待审核可按**任务**筛选。
        task_flt = 0
        try:
            task_flt = int((q.get("task") or ["0"])[0] or 0)
        except ValueError:
            task_flt = 0
        # 2026-10-27：图库方向 / 分辨率筛选（桌面端早就有，Web 一直没有）。
        orient = (q.get("orient") or [""])[0].strip().lower()
        res = (q.get("res") or [""])[0].strip().lower()
        # V0.0.14：图库快捷筛选。**白名单收窄**，不认识的值一律当「不筛选」，
        # 绝不让前端传来的字符串进 SQL（与 _norm_sort 同一条纪律）。
        flag = (q.get("flag") or [""])[0].strip().lower()
        if flag not in ("fav", "unseen", "missing"):
            flag = ""
        if view == "settings":
            return {"ok": True, "items": list(self._api_settings().items()),
                    "next_cursor": None, "has_more": False}
        if view == "cleanup":
            # 清理页不需要分页，前端自己渲染复选框
            return {"ok": True, "items": [], "next_cursor": None,
                    "has_more": False}
        if view == "logstats":
            return self._api_logstats()
        if view == "adapters":
            # R50：适配器**不是图片流**，绝不能落到 `_api_images` 的
            # `where="1=1"` 去 —— 那样返回的是 images 表的行，
            # 前端按 name/ok/task_id 取值全拿到 undefined，
            # 页面渲染成一片「undefined / 异常」，看着像软件坏了。
            # （实测：修复前 /api/view/adapters 返回的是图片行。）
            return self._api_adapters()
        return self._api_images(view, cursor, limit, sort=sort, q=kw,
                               task_id=task_flt, orient=orient, res=res,
                               flag=flag)

    def _api_adapters(self) -> dict:
        """R50：适配器视图 = 真读 rules 目录，复用桌面端同一个 `load_adapter`。

        ⚠ **不另写一套解析**（作业单 E 的铁律）：能否解析、缺哪些字段，
        判据全在 `crawler.rules.load_adapter` 与 `AdapterRule` 上，
        与桌面端「适配器巡检」用的是同一个函数。判「OK / 缺啥」的口径
        也照 `gui/adapter_checkup.checkup_rules_offline` 的三项必备字段。

        每行字段沿用前端 `_taskRow` 之外的适配器表所需：
        `name` / `ok` / `file` / `domains` / `problems` / `base_url`。
        `task_id` 刻意**不给**：适配器不是任务，没有可启停的对象 ——
        给一个空 task_id 会让前端的「启停」按钮变成"点了没反应"。
        """
        try:
            from .crawler.rules import load_adapter
        except Exception as e:
            return {"ok": False, "error": f"适配器模块不可用：{e}",
                    "items": [], "next_cursor": None, "has_more": False}
        try:
            rdir = Path(self.cfg.rules_dir)
        except Exception:
            rdir = Path(".")
        files = sorted([*rdir.glob("*.yaml"), *rdir.glob("*.yml")])
        items = []
        for f in files:
            problems: list[str] = []
            ok = True
            base = ""
            doms: list[str] = []
            try:
                rule = load_adapter(f)
            except Exception as e:
                ok = False
                problems.append(f"解析失败：{type(e).__name__}")
                items.append({"name": f.stem, "ok": False, "file": f.name,
                              "domains": [], "problems": problems,
                              "base_url": "", "rule_text": _rule_text(f)})
                continue
            # 必备三项：与 adapter_checkup.checkup_rules_offline 同口径
            if not rule.base_url and not rule.entry_urls:
                problems.append("缺 base_url/entry_urls")
            if not rule.allowed_domains:
                problems.append("缺 allowed_domains")
            if not rule.image_link_selectors:
                problems.append("缺图片选择器")
            ok = not problems
            base = rule.base_url or (rule.entry_urls[0] if rule.entry_urls
                                     else "")
            doms = list(rule.allowed_domains or [])
            items.append({"name": rule.name or f.stem, "ok": ok,
                          "file": f.name, "domains": doms,
                          "problems": problems, "base_url": base,
                          # R252：规则自带的登录态与 UA（只报"有没有"，不回值）
                          "has_cookie": bool(getattr(rule, "cookie", "")),
                          "has_ua": bool(getattr(rule, "user_agent", "")),
                          # R146-D：规则原文随清单下发（网页「规则原文」折叠
                          # 区用）。⚠ 必须**截断**：一条规则几 KB，几百条
                          # 一起下发会把首屏请求拖成几十 MB。
                          "rule_text": _rule_text(f)})
        if not files:
            # ⚠ R144：空目录这条**早退分支也必须带 prompt** —— 第一次装好、
            # 一条规则都没有时，用户最需要的恰恰是「一键复制 Prompt」。
            # 少了这个键，前端按钮点下去是空的，看着像功能坏了。
            return {"ok": True, "items": [],
                    "next_cursor": None, "has_more": False,
                    "prompt": adapter_prompt_template(),
                    "note": f"规则目录里没有 yaml：{rdir}"}
        return {"ok": True, "items": items,
                "next_cursor": None, "has_more": False,
                # R144：给外部 AI 的生成话术随清单一起下发，前端照显示。
                "prompt": adapter_prompt_template(),
                "stats": {"total": len(items),
                          "ok": sum(1 for i in items if i["ok"])}}

    # R144：规则名白名单 —— 只允许字母/数字/下划线/短横线。
    # ⚠ 为什么必须有：**规则名直接拼进文件路径**（`<rules_dir>/<name>.yaml`）。
    # 不做白名单的话，一条 `../../Desktop/evil` 就能把文件写到图库外面去
    # （路径穿越）。宁可让用户改个名字，也不能让他写错一次就丢数据。
    _ADAPTER_NAME_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

    def _bad_selector_kinds(self, rule, kinds) -> list[str]:
        """R144：挑出写错 kind 的选择器，返回「哪一块写错了」的清单。

        ⚠ 为什么后端还要再查一遍：`load_adapter` 只管"字段在不在",
        不管 kind 的取值合法与否 —— 写 `{"kind":"CSS"}` 或 `{"kind":"jq"}`
        它照样收下，等到真跑任务时才解不出选择器，用户看到的是"采到 0 张"，
        根本联想不到是几个月前粘规则时大小写写错了。
        """
        bad = []
        groups = [
            ("list_page_selectors", rule.list_page_selectors),
            ("album_link_selectors", rule.album_link_selectors),
            ("image_page_selectors", rule.image_page_selectors),
            ("next_page_selectors", rule.next_page_selectors),
        ]
        for gname, val in groups:
            if isinstance(val, dict) and val:
                k = str(val.get("kind") or "")
                if k and k not in kinds:
                    bad.append(f"{gname}={k}")
        for i, val in enumerate(rule.image_link_selectors or []):
            if isinstance(val, dict) and val:
                k = str(val.get("kind") or "")
                if k and k not in kinds:
                    bad.append(f"image_link_selectors[{i}]={k}")
        return bad

    # ───────────── R254 · 桌面系统集成（网页端可看、可切） ─────────────
    #
    # 这三项原来是桌面端菜单独占：托盘/热键**只能**桌面端做（浏览器碰不到
    # 窗口系统），但「开机自启」与「检查更新」本质上都是**服务端动作**
    # （写启动项、抓一份清单），网页端完全能做 —— 尤其是装在 NAS/小主机上
    # 以后，人根本不在那台机器前，只能靠网页开。
    def _api_sys_integration(self):
        """GET /api/system/integration → 版本 + 开机自启 + 启动行为。"""
        out = {"ok": True, "version": "", "built": "", "autostart": False,
               "autostart_supported": False, "platform": sys.platform}
        # V0.0.27：托盘相关。托盘是**窗口端**能力（浏览器里没有），
        # 这里只报「配置值 + 这台机器上能不能用」，由前端决定怎么讲。
        try:
            out["tray_start_minimized"] = bool(
                getattr(self.cfg, "tray_start_minimized", False))
        except Exception:                              # noqa: BLE001
            out["tray_start_minimized"] = False
        out["tray_supported"] = bool(sys.platform.startswith("win"))
        try:
            from .update_check import build_info
            info = build_info()
            out["version"] = info.get("version", "")
            out["built"] = info.get("built", "")
        except Exception:
            pass
        try:
            from . import autostart as A
            out["autostart_supported"] = bool(A.supported())
            if out["autostart_supported"]:
                on, detail = A.status()
                out["autostart"] = bool(on)
                out["autostart_detail"] = str(detail or "")[:200]
        except Exception as e:
            out["autostart_detail"] = f"读取失败：{e}"
        return out

    def _api_sys_autostart(self, body):
        """POST /api/system/autostart {enabled} → 开/关开机自启。

        ⚠ 只在 Windows 有效（写「启动」文件夹里的一个 .bat）。
        非 Windows 直接回中文说明，**不假装成功** —— 用户会以为设上了。
        """
        want = bool((body or {}).get("enabled"))
        try:
            from . import autostart as A
            if not A.supported():
                return {"ok": False,
                        "error": "当前系统不支持开机自启（仅 Windows）。"
                                 "Linux/Docker 部署请用系统服务或容器 restart 策略。"}
            ok, detail = (A.install() if want else A.remove())
            if not ok:
                return {"ok": False, "error": str(detail)[:300]}
            on, _ = A.status()
            try:
                self.db.audit("system", "autostart", "on" if on else "off")
            except Exception:
                pass
            return {"ok": True, "autostart": bool(on),
                    "message": ("已开启开机自启：登录 Windows 后自动在后台运行"
                                if want else "已关闭开机自启")}
        except Exception as e:
            return {"ok": False, "error": f"操作失败：{e}"}

    def _api_sys_update(self):
        """GET /api/system/update → 版本比对（失败一律中文说明，不抛）。"""
        try:
            from .update_check import check
            r = check(self.cfg)
            return {"ok": True, **{k: r.get(k) for k in
                                   ("has_update", "local", "remote", "url",
                                    "notes", "built", "text")}}
        except Exception as e:
            return {"ok": False, "error": f"检查更新失败：{type(e).__name__}: {e}"}

    # ─────────────── R253 · AI 写适配器（先本地验证，再让用户确认） ───────────────
    def _api_adapter_ai(self, body):
        """POST /api/adapter/ai {url} → 起作业生成候选规则。

        **不落盘**：结果回给前端预览，用户选择「保存」才走 `/api/adapter/save`
        （那条通道已经有完整校验）。模型编选择器是常态，所以生成完立刻拿
        真页面跑一遍选择器，匹配 0 张就判定不可用并退回纯启发式。
        """
        try:
            url = str((body or {}).get("url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                return {"ok": False, "error": "请填写完整的网址（以 http:// 或 https:// 开头）"}
            use_ai = bool((body or {}).get("use_ai", True))
            jid = _job_new("adapter_ai")
            cfg = self.cfg
            progress = _job_progress(jid)

            def _run():
                try:
                    from . import adapter_ai as A
                    from .crawler.adapter_proposer import dump_rule_yaml  # noqa: F401
                    progress(10, "抓取页面…")
                    html = A.fetch_page(cfg, url)
                    if not html or len(html) < 200:
                        _job_finish(jid, error=(
                            "页面内容过短（可能被拦或被要求登录）。"
                            "先在浏览器里打开该网址确认能正常看到图，"
                            "必要时用浏览器插件把 Cookie 推给软件再重试。"))
                        return
                    progress(45, "生成规则…")
                    res = A.propose(cfg, url, html,
                                    template=adapter_prompt_template(),
                                    use_ai=use_ai)
                    rule = res.get("rule")
                    out = {
                        "url": url,
                        "ok": bool(res.get("ok")),
                        "source": res.get("source"),
                        "count": res.get("count"),
                        "notes": res.get("notes") or [],
                        "rule_text": res.get("rule_text") or "",
                        "name": getattr(rule, "name", "") if rule else "",
                        "domains": list(getattr(rule, "allowed_domains", []) or []),
                    }
                    progress(100, "完成")
                    _job_finish(jid, result=out,
                                message=("规则可用，本页匹配到 %d 张图"
                                         % int(res.get("count") or 0))
                                if out["ok"] else "未能生成可用规则（见说明）")
                except Exception as e:      # noqa: BLE001 —— 作业线程必须兜住
                    _job_finish(jid, error=f"生成失败：{type(e).__name__}: {e}")

            threading.Thread(target=_run, name="mrm-adapter-ai",
                             daemon=True).start()
            return {"ok": True, "job": jid}
        except Exception as e:
            return {"ok": False, "error": f"无法开始：{e}"}

    def _api_adapter_prompt(self, body):
        """POST /api/adapter/prompt {url} → 给外部 AI 用的完整提示词。

        用户要「复制一份提示词，其他 AI 自己做」。这里把**目标网址 +
        真实页面结构**一起拼好，粘到任意 AI 对话框就能出规则；
        页面抓取复用适配器那条链（带 Cookie / UA / 代理），不是空模板。
        """
        try:
            url = str((body or {}).get("url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                return {"ok": False, "error": "请填写完整的网址（以 http:// 或 https:// 开头）"}
            from . import adapter_ai as A
            html = A.fetch_page(self.cfg, url)
            if not html or len(html) < 200:
                return {"ok": False, "error":
                        "页面内容过短（可能被拦或需要登录）。先在浏览器里打开确认。"}
            return {"ok": True, "prompt": A.build_prompt(
                url, html, template=adapter_prompt_template()),
                "note": "已带上页面结构；粘到任意 AI 对话框即可。用别的 AI 生成后，"
                        "把回复粘到下面的输入框，再选择「检查并填入」。"}
        except Exception as e:                          # noqa: BLE001
            return {"ok": False, "error": f"准备提示词失败：{type(e).__name__}: {e}"}

    def _api_adapter_from_reply(self, body):
        """POST /api/adapter/from-reply {url, reply} → 解析别的 AI 回复。

        同样走「本地真页面验证」这道闸：别的 AI 编出来的选择器匹配 0 张图
        照样不入库。返回候选规则文本供前端预览，点保存才落盘。
        """
        try:
            url = str((body or {}).get("url") or "").strip()
            reply = str((body or {}).get("reply") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                return {"ok": False, "error": "请先填写目标网址"}
            if not reply:
                return {"ok": False, "error": "请把别的 AI 的回复粘贴进来"}
            from . import adapter_ai as A
            obj = A.extract_json(reply)
            if obj is None:
                return {"ok": False, "error":
                        "没在回复里找到 JSON 对象。请让 AI 只输出规则 JSON。"}
            html = A.fetch_page(self.cfg, url)
            if not html or len(html) < 200:
                return {"ok": False, "error": "目标页面打不开或内容过短，无法验证规则"}
            rule = A.rule_from_dict(obj, fallback_name="external_ai")
            v = A.validate(rule, html, url)
            notes = [f"别的 AI 回复已解析；本页匹配到 {int(v.get('count') or 0)} 张图"]
            if not v.get("ok"):
                notes.append(v.get("reason") or "规则未通过本地验证")
            from .crawler.adapter_proposer import dump_rule_yaml
            return {"ok": bool(v.get("ok")), "rule_text": dump_rule_yaml(rule),
                    "name": getattr(rule, "name", "") or "external_ai",
                    "count": int(v.get("count") or 0), "notes": notes}
        except Exception as e:                          # noqa: BLE001
            return {"ok": False, "error": f"解析失败：{type(e).__name__}: {e}"}

    def _api_adapter_ai_job(self, u):
        """GET /api/adapter/ai/job?id=… → 进度与候选规则。"""
        jid = (parse_qs(u.query).get("id") or [""])[0]
        rec = _job_get(jid)
        if rec is None:
            return {"ok": False, "error": "作业已过期或不存在，请重新发起"}
        return {"ok": True, **rec}

    def _api_adapter_get(self, u) -> dict:
        """GET /api/adapter/get?name=… → 一条规则的原文。

        V0.0.23：网页端要能「手动编辑已有规则」和「导入本地文件后核对」，
        前提就是把规则原文读出来。只认白名单规则名、只读 rules_dir 下
        这三种后缀，不跟随目录外路径。
        """
        name = str((parse_qs(u.query).get("name") or [""])[0]).strip()
        if not self._ADAPTER_NAME_OK.match(name):
            return {"ok": False, "error": "规则名只能用字母、数字、下划线和短横线"}
        try:
            rdir = Path(self.cfg.rules_dir)
        except Exception as e:                        # noqa: BLE001
            return {"ok": False, "error": f"规则目录打不开：{e}"}
        for ext in (".yaml", ".yml", ".json"):
            f = rdir / f"{name}{ext}"
            try:
                if not f.is_file():
                    continue
                if f.stat().st_size > 512 * 1024:
                    return {"ok": False,
                            "error": "规则文件超过 512 KB，网页端不提供编辑"}
                return {"ok": True, "name": name, "file": f.name,
                        "text": f.read_text(encoding="utf-8", errors="replace")}
            except OSError as e:                      # noqa: BLE001
                return {"ok": False, "error": f"读取规则失败：{e}"}
        return {"ok": False, "error": f"未找到规则「{name}」"}

    def _api_adapter_save(self, body) -> dict:
        """R144：把用户贴进来的规则原文（JSON 或 YAML）存进 `cfg.rules_dir`。

        ⚠ 三件事必须和桌面端「网址与适配器管理器」完全一致，否则用户会遇到
        「网页上加了，桌面端却看不到」——同一件事两个答案比没有功能更糟：
          ① 目录 = `cfg.rules_dir`（两边同一个）；
          ② 解析 = `config.load_rule_file`（同一份，**不自己再写一遍**）；
          ③ 校验 = `crawler.rules.load_adapter`（同一份，含 base_url /
             entry_urls 二选一、allowed_domains 自动补全等桌面端语义）。
        ⚠ **先校验后落盘**：不合法的规则一个字节都不写。半截文件比报错糟得多
        ——它会让适配器页多一行「解析失败」，用户还以为是别的地方坏了。
        """
        try:
            from .config import dump_rule_file, load_rule_file
            from .crawler.rules import SELECTOR_KINDS, load_adapter
        except Exception as e:
            return {"ok": False, "error": f"适配器模块不可用：{e}"}

        raw = str(body.get("rule") or "").strip()
        if not raw:
            return {"ok": False, "error": "规则内容不能为空"}
        try:
            rdir = Path(self.cfg.rules_dir)
            rdir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"ok": False, "error": f"规则目录打不开：{e}"}

        # 后缀只是给 load_rule_file 选解析器用的：JSON 解得出就当 JSON，
        # 否则当 YAML。**先写成临时文件**是因为 load_rule_file 只吃路径 ——
        # 复用它，比为网页端再写一份「文本→dict」强（多一份就多一处漂移）。
        # ⚠ 临时文件放系统临时目录，**不放 rules_dir**：放规则目录里万一
        # 崩在中途，就会在适配器列表里留下一条半截规则的幽灵行。
        suffix = ".json"
        try:
            json.loads(raw)
        except Exception:
            suffix = ".yaml"
        tmp = Path(tempfile.gettempdir()) / f"mrm_rule_{os.getpid()}_{time.time_ns()}{suffix}"
        try:
            tmp.write_text(raw, encoding="utf-8")
            try:
                data = load_rule_file(tmp)
                rule = load_adapter(tmp)
            except Exception as e:
                msg = str(e)
                if "base_url" in msg or "entry_urls" in msg:
                    msg = "规则必须写 base_url 或 entry_urls 中的至少一个"
                elif "must be a mapping" in msg or "object" in msg.lower():
                    msg = "规则内容得是一个对象（不能是列表或一段纯文字）"
                else:
                    msg = f"规则解析失败：{msg}"
                return {"ok": False, "error": msg}
            if not isinstance(data, dict):
                return {"ok": False, "error": "规则内容得是一个对象（不能是列表或一段纯文字）"}
            # R152-C1 + R144：**规则正文里的 name 也要过白名单**。
            # load_adapter 现在会把坏名收敛成安全名（crawler/rules.py
            # sanitize_adapter_name）——那是给引擎/落盘兜底的；但网页保存是
            # 用户主动粘贴的场景，必须明说"这个名字不合法"让他改，而不是
            # 悄悄改名存进去（R144 原语义：拒）。少了这一条，坏名会被
            # sanitize 掩掉，用户以为存的是自己写的名字。
            inner_name = str(data.get("name") or "").strip()
            if inner_name and not self._ADAPTER_NAME_OK.match(inner_name):
                return {"ok": False,
                        "error": "规则名只能用字母、数字、下划线和短横线，且不能为空"}
            bad = self._bad_selector_kinds(rule, SELECTOR_KINDS)
            if bad:
                return {"ok": False,
                        "error": "选择器类型只能是 css / xpath / regex，"
                                 + "这几处写错了：" + "、".join(bad)}
            name = str(body.get("name") or "").strip() or str(rule.name or "")
            if not self._ADAPTER_NAME_OK.match(name):
                return {"ok": False,
                        "error": "规则名只能用字母、数字、下划线和短横线，且不能为空"}
            final = rdir / f"{name}.yaml"
            # V0.0.23：允许覆盖 —— 网页端「编辑已有规则」必须能写回同一个文件。
            # 只有显式带 overwrite 才覆盖；不带时仍然拒绝，避免手滑拿新规则
            # 冲掉同名的旧规则（用户自己写的规则被静默替换是最糟的形态）。
            overwrite = bool(body.get("overwrite"))
            if final.exists() and not overwrite:
                return {"ok": False,
                        "error": f"已经有一条叫「{name}」的规则了，请换个名字"}
            data["name"] = name
            # 统一按 YAML 落盘：`_api_adapters` 与桌面端都按 *.yaml 列举，
            # 存成 .json 的话两边都会漏掉它。
            dump_rule_file(data, final)
            load_adapter(final)          # 写回自检：读不出来就别报成功
            # V0.0.23：规则的增删也要留痕（用户排查「这条规则谁改的」时要有线索）
            try:
                self.db.audit(self._actor(),
                              "adapter_save" if overwrite else "adapter_add",
                              name, "覆盖保存" if overwrite else "新增规则")
            except Exception:                          # noqa: BLE001
                pass
            return {"ok": True, "name": name, "file": final.name}
        except Exception as e:
            return {"ok": False, "error": f"保存适配器失败：{e}"}
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _api_adapter_delete(self, body) -> dict:
        """R144：删掉一条自己加的规则。**只认白名单规则名**，用户数据目录之外的一律不碰。"""
        name = str(body.get("name") or "").strip()
        if not self._ADAPTER_NAME_OK.match(name):
            return {"ok": False, "error": "规则名不合法"}
        try:
            rdir = Path(self.cfg.rules_dir)
        except Exception as e:
            return {"ok": False, "error": f"规则目录打不开：{e}"}
        for ext in (".yaml", ".yml", ".json"):
            p = rdir / f"{name}{ext}"
            if p.exists():
                p.unlink()
                return {"ok": True, "name": name, "file": p.name}
        return {"ok": False, "error": f"没有找到名为「{name}」的规则"}

    # ───────────────── V0.0.54 · 适配器市场（规则订阅源） ─────────────────
    #
    # 三个接口：列订阅源 / 拉某个源目录 / 安装一条规则。
    # 安装**不另写落盘路径**——下载原文后直接走 _api_adapter_save 那道
    # 与手动新建完全相同的校验链（解析、名字、选择器类型、重名保护），
    # 多写一份就会漂移一份，市场来的规则没有资格享受特判。

    def _api_market_sources(self):
        """GET /api/market/sources → 已订阅源 + 官方市场默认项。"""
        from . import rule_market as rm
        return {"ok": True, "sources": rm.load_sources(self.cfg),
                "default": rm.DEFAULT_SOURCE_URL}

    def _api_market_index(self, u):
        """GET /api/market/index?url=<源地址> → 拉清单（浏览市场目录）。"""
        from . import rule_market as rm
        url = str((parse_qs(u.query).get("url") or [""])[0]).strip()
        if not url:
            return {"ok": False, "error": "缺少订阅源地址"}
        try:
            return rm.fetch_index(url, cfg=self.cfg)
        except rm.MarketError as e:
            return {"ok": False, "error": str(e)}

    def _api_market_manage(self, body):
        """POST /api/market/manage {action: add|remove, url} → 管订阅源。"""
        from . import rule_market as rm
        body = body if isinstance(body, dict) else {}
        action = str(body.get("action") or "").strip().lower()
        url = str(body.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "缺少订阅源地址"}
        urls = rm.load_sources(self.cfg)
        if action == "add":
            if not url.lower().startswith(("http://", "https://")):
                return {"ok": False, "error": "订阅源地址必须以 http(s):// 开头"}
            if url in urls:
                return {"ok": False, "error": "这个订阅源已经添加过了"}
            # 先验证能拉到再存 —— 存一个坏源不如当场报错
            try:
                info = rm.fetch_index(url, cfg=self.cfg)
            except rm.MarketError as e:
                return {"ok": False, "error": str(e)}
            rm.save_sources(self.cfg, urls + [url])
            return {"ok": True, "sources": urls + [url], "name": info.get("name")}
        if action == "remove":
            rest = [x for x in urls if x != url]
            if len(rest) == len(urls):
                return {"ok": False, "error": "这个订阅源不在列表里"}
            rm.save_sources(self.cfg, rest)
            return {"ok": True, "sources": rest}
        return {"ok": False, "error": "action 仅支持 add / remove"}

    def _api_market_install(self, body):
        """POST /api/market/install {url, overwrite?} → 下载并安装一条规则。

        下载 → 复用 _api_adapter_save（校验 + 落盘 + 审计一步不少）。
        """
        from . import rule_market as rm
        body = body if isinstance(body, dict) else {}
        url = str(body.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "缺少规则文件地址"}
        try:
            text = rm.fetch_rule_preview(url, cfg=self.cfg)
        except rm.MarketError as e:
            return {"ok": False, "error": str(e)}
        name = str(body.get("name") or "").strip()
        r = self._api_adapter_save({"rule": text, "name": name,
                                    "overwrite": bool(body.get("overwrite"))})
        if r.get("ok"):
            try:
                self.db.audit(self._actor(), "market_install",
                              str(r.get("name") or name or url)[:64], url[:200])
            except Exception:
                pass
        return r

    def _api_images(self, view: str, cursor: int, limit: int,
                    sort: str = "time", q: str = "",
                    task_id: int = 0,
                    orient: str = "", res: str = "",
                    flag: str = "") -> dict:
        """图库类视图的统一分页实现（#36 排序 / #23 搜索）。

        ⚠ **time 排序一律带 ``id<cursor``**：无限加载靠的是"上次最后一张的 id"，
        单纯 LIMIT/OFFSET 在有新图入库时会漏图也会重图。
        ``id DESC`` + ``id < cursor`` 天然稳定，也是本方法的默认行为
        （老前端不带 sort ⇒ 与以前逐字节等价）。

        name / size 两种排序**刻意走 OFFSET**：键集分页只能挂在排序键上，
        而 path / bytes 可空可重复，编码进单个 int cursor 会又脆又难验；
        本地单用户图库在翻页中途插入新图导致偏移，是这里可接受的代价
        （测试用静态数据集证明「翻完不重不漏」）。

        #23 的 ``q`` 搜路径 / 标签 / 来源网址三处：SQLite 的 LIKE 对
        ASCII 大小写不敏感，中文是子串匹配 —— 与「搜索」的直觉一致。
        """
        # R143：⚠⚠ 隔离区口径必须与桌面端 `main_window.quarantine_manager`
        # 的取数**逐字一致**。桌面端注释写明「隔离区只列用户主动操作进来的
        # 回收站（needs_review=0）；审核队列里的待定图走审核页，不在这里混」。
        # 这里少了 AND needs_review=0 的后果（用户实测反馈）：
        #   · Web 隔离区比桌面端多出一整批 in_quarantine=1 AND needs_review=1；
        #   · 那批图在 Web 隔离区与 Web 待审核页**同时可见**（用户据此怀疑
        #     "web 端隔离区和待审核是同一个东西"——不是同一份，是有交集）。
        # 两边任一侧改口径都必须同步改另一侧，别再各写一份。
        where = {
            "gallery": "in_quarantine=0 AND blacklisted=0 AND path IS NOT NULL",
            # V0.0.5：待审核口径与桌面端 review.queue.pending 逐字一致
            # （needs_review=1）。以前桌面端多要求 in_quarantine=1，网页
            # 「移动」送进待审核的图（needs_review=1、in_quarantine=0）
            # 桌面审核页看不到——决定清单圈定的"两边口径统一"就在这里落地。
            "pending": "needs_review=1",
            "quarantine": "in_quarantine=1 AND needs_review=0",
            # 「去重输家」＝被判为重复而被丢的那张（phash 非空 + 未入库）。
            # 去重输家没有单独标记列，用「有 phash 但没进图库」这个既有事实表示。
            "dedup": ("blacklisted=0 AND in_quarantine=0 AND path IS NULL "
                      "AND phash IS NOT NULL AND phash<>''"),
        }.get(view, "1=1")
        args: list = []
        if task_id:
            where += " AND task_id=?"
            args.append(int(task_id))
        # ── 2026-10-27：#63 高级搜索语法（**复用 search.parse_query**）──
        # 以前这里把 q 当成一个整体 LIKE 串 ⇒ `tag:白丝` 变成
        # LIKE '%tag:白丝%'，永远 0 行。老界面的「标签」下拉拼的正是这个
        # 语法，所以「点标签 → 图库空」不是没图，是查询压根没被理解。
        # 语法与桌面端共用一份实现，别在这里另写一套（两套必然对不上）。
        from .search import parse_query

        cq = parse_query(q)
        # 显式 black / quar / review 时**切换口径**：用户是主动要看去重/隔离/
        # 待审的图，继续套 gallery 的 in_quarantine=0 只会得到空集。
        if view == "gallery" and (cq["black"] or cq["quar"] or cq["review"]):
            if cq["black"]:
                where = "blacklisted=1 AND path IS NOT NULL"
            elif cq["quar"]:
                where = "in_quarantine=1"
            else:
                where = "needs_review=1"
        # ---- V0.0.14：快捷筛选（只看收藏 / 尚未查看 / 文件缺失）----
        # ⚠ 必须放在上面「切换口径」之后：那段会把 where 整体替换掉，
        #   放前面追加的 AND 会被一起冲掉，筛选看起来「点了没反应」。
        _flag = str(flag or "").strip().lower()
        if _flag == "fav":
            where += " AND favorite=1"
        elif _flag == "unseen" and self._has_column("images", "viewed"):
            where += " AND COALESCE(viewed,0)=0"
        elif _flag == "missing":
            where += " AND path IS NOT NULL AND mrm_file_exists(path)=0"
        for _tg in (cq["tags_include"] or [])[:8]:
            where += " AND tags LIKE ?"
            args.append("%" + str(_tg) + "%")
        for _tg in (cq["tags_exclude"] or [])[:8]:
            where += " AND (tags IS NULL OR tags NOT LIKE ?)"
            args.append("%" + str(_tg) + "%")
        if cq["site"]:
            where += (" AND task_id IN "
                      "(SELECT id FROM tasks WHERE name LIKE ?)")
            args.append("%" + str(cq["site"]) + "%")
        if cq["score_min"] is not None:
            where += " AND score >= ?"
            args.append(float(cq["score_min"]))
        if cq["score_max"] is not None:
            where += " AND (score IS NULL OR score <= ?)"
            args.append(float(cq["score_max"]))
        if cq["w_min"]:
            where += " AND width >= ?"
            args.append(int(cq["w_min"]))
        if cq["h_min"]:
            where += " AND height >= ?"
            args.append(int(cq["h_min"]))
        if cq["fav"]:
            where += " AND favorite=1"
        # 裸词：仍然走「路径 / 标签 / 来源网址」三处模糊（与老行为一致）
        for _w in (cq["free"] or [])[:6]:
            where += " AND (path LIKE ? OR tags LIKE ? OR source_url LIKE ?)"
            like = "%" + str(_w) + "%"
            args.extend([like, like, like])
        # 方向 / 分辨率：与桌面端同一个纯函数，不在这里另写 SQL
        from .search import orient_res_where
        where += orient_res_where(orient, res)
        order = _SORTS[_norm_sort(sort)]
        # 多取一条用来判断"还有没有下一页"，不多返回给前端。
        if _norm_sort(sort) == "time":
            where += " AND id<?"
            args.append(cursor if cursor > 0 else 1 << 62)
            args.append(limit + 1)
            sql = (
                "SELECT id, path, tags, score, blacklisted, in_quarantine, "
                "needs_review, favorite, source_url, width, height, bytes, "
                "collected_at, review_reason FROM images "
                f"WHERE {where} ORDER BY {order} LIMIT ?")
        else:
            off = cursor if cursor > 0 else 0
            args.extend([limit + 1, off])
            sql = (
                "SELECT id, path, tags, score, blacklisted, in_quarantine, "
                "needs_review, favorite, source_url, width, height, bytes, "
                "collected_at, review_reason FROM images "
                f"WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?")
        rows = self.db.query(sql, tuple(args))
        more = len(rows) > limit
        rows = rows[:limit]
        items = [self._img_item(r) for r in rows]
        # ⚠⚠ next_cursor 必须是**本页最后一行**（裁片后算），绝不是那条
        # 多取出来的 peek 行：拿 peek 行当 cursor，下一页从它下面开始
        # ⇒ 每页边界稳定漏一张（实测 200 张图翻两页只拿到 199）。
        # time 排序 = 最后一行的 id；OFFSET 排序 = 下一页的偏移量。
        if _norm_sort(sort) == "time":
            nxt = rows[-1]["id"] if (more and rows) else None
        else:
            nxt = (off + limit) if more else None
        return {"ok": True, "items": items, "next_cursor": nxt,
                "has_more": more}

    def _img_item(self, r) -> dict:
        tags = r["tags"] or ""
        try:
            from .simsearch import split_tags
            shown = " · ".join(i18n.translate_tags(split_tags(tags))[:6])
        except Exception:
            shown = ""
        flag = ""
        if r["blacklisted"]:
            flag = "已拉黑"
        elif r["in_quarantine"]:
            flag = "隔离中"
        elif r["needs_review"]:
            flag = "待审核"
        elif r["favorite"]:
            flag = "★"
        meta = shown or ("#%s" % r["id"])
        return {
            "id": r["id"],
            # B1 #46：logo 水印**不走服务端烧进图片**，而是前端卡片右下角
            # 叠一个小块（见 app.css .card .flag）。理由：烧进原图会破坏
            # 原文件、没法撤销；前端叠加随时可关、随时可改样式。
            "url": f"/thumb/{r['id']}",
            # ⚠ #46：灯箱要的是**原图**。以前 full_url 和 url 一样都指
            # /thumb/，于是「灯箱里放大」放大的是缩略图 —— 用户看到的
            # 就是一片糊，还以为图源坏了。orig_url 才是下载/大图用的地址。
            "full_url": f"/orig/{r['id']}",
            "thumb_url": f"/thumb/{r['id']}",
            "orig_url": f"/orig/{r['id']}",
            # 第 30/37 项：来源网址 + 尺寸 + 采集时间（灯箱与信息面板要用）。
            "source_url": r["source_url"] if "source_url" in r.keys() else "",
            "width": r["width"] if "width" in r.keys() else None,
            "height": r["height"] if "height" in r.keys() else None,
            "bytes": r["bytes"] if "bytes" in r.keys() else None,
            "collected_at": (r["collected_at"]
                             if "collected_at" in r.keys() else None),
            "meta": meta[:60],
            "flag": flag,
            "score": r["score"],
            "favorite": bool(r["favorite"]),
            # V0.0.8：待审核卡片显示「为什么被送进来」（写库时已翻成中文）。
            "reason": (str(r["review_reason"] or "")
                       if r["needs_review"]
                       and "review_reason" in r.keys() else ""),
        }

    def _api_logstats(self):
        try:
            from .audit_text import action_label, actor_label, target_label
            rows = self.db.query(
                "SELECT COUNT(*) c FROM audit_log")
            total = rows[0]["c"] if rows else 0
            items = [{"name": "日志条数", "value": total}]
            # 有几个最近动作，让「日志统计」页不是只有一行
            # V0.0.23：名字改成中文（原来直接把 task_save 这类编码摆给用户看）
            for r in self.db.query(
                    "SELECT action, COUNT(*) c FROM audit_log "
                    "GROUP BY action ORDER BY c DESC LIMIT 8"):
                items.append({"name": f"动作：{action_label(r['action'])}",
                              "code": str(r["action"] or ""),
                              "value": r["c"]})
            # 2026-10-27：新 Web「日志」页需要明细 —— 原来只有计数，页面
            # 只能显示两行数字，等于没有信息。`items` 形状保持不变（老前端
            # 与护栏都在用），这里**只追加** `recent`。
            recent = []
            try:
                raw = list(self.db.query(
                    "SELECT id, ts, actor, action, target, detail "
                    "FROM audit_log ORDER BY id DESC LIMIT 50"))
                # 一次把用到的任务 / 订阅名字查出来（每条查一次会打出 50 条 SQL）
                names = {}
                tids = {int(r["target"]) for r in raw
                        if str(r["target"] or "").isdigit()
                        and str(r["action"] or "").startswith("task_")}
                if tids:
                    ph = ",".join("?" * len(tids))
                    for t in self.db.query(
                            f"SELECT id, name FROM tasks WHERE id IN ({ph})",
                            tuple(tids)):
                        names[("task", int(t["id"]))] = str(t["name"] or "")
                for r in raw:
                    act = str(r["action"] or "")
                    tgt = str(r["target"] or "")
                    recent.append({
                        "id": int(r["id"] or 0),
                        "ts": str(r["ts"] or ""),
                        "actor": str(r["actor"] or ""),
                        "actor_label": actor_label(str(r["actor"] or "")),
                        "action": act,
                        "action_label": action_label(act),
                        "target": tgt,
                        "target_label": target_label(act, tgt, names),
                        "detail": str(r["detail"] or "")[:300],
                    })
            except Exception:                              # noqa: BLE001
                recent = []
            return {"ok": True, "items": items, "recent": recent,
                    "next_cursor": None, "has_more": False}
        except Exception as e:                             # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def _api_tasks(self):
        """#26 + #38：任务列表带上**两个张数**与**白话中文**失败原因。

        ⚠⚠ 「本次」和「累计」是**两个不同口径**，谁也代替不了谁：
          · 本次 = 这一轮运行真存下来的张数（队列报告 `downloaded`）。
            没跑过的任务它就是 0 —— 不是 bug，是这个任务还没跑过。
          · 累计 = 这个任务名下**现在还在库里**的张数（`taskio.task_progress`，
            跟 exe「批量导出」里那个「已下载」是同一个函数、同一套判定：
            隔离 / 拉黑 / 没落盘的都不算）。
        ⇒ 拿一个当另一个用，用户会看到「跑完 30 张，累计还是 0」或者
          「累计 500 张，本次却显示 500」这种自相矛盾的数字。

        ⚠ `error` 现在给的是**翻译后**的中文，`error_raw` 才是库里的原文
        （挂在单元格 title 上，用户要反馈时能抄走）。
        """
        from . import taskio

        q = getattr(self.server, "queue", None)          # type: ignore[attr-defined]
        # 队列没挂上（纯 API 模式）也要能出列表：reports 取空 dict，
        # 「本次」一律 0，累计照常从库里算。
        reports = getattr(q, "reports", None) or {}
        # V0.0.27：任务名下的图片总数（删除任务时「同时删除图片」要显示真实张数，
        # 一次 GROUP BY 拿全表，别在循环里对 25 个任务各查一次）。
        try:
            img_cnt = {int(r["task_id"]): int(r["c"])
                       for r in self.db.query(
                           "SELECT task_id, COUNT(*) AS c FROM images "
                           "GROUP BY task_id")}
        except Exception:                              # noqa: BLE001
            img_cnt = {}
        rows = []
        for r in self.db.query(
                "SELECT id, name, status, entry_urls, last_error "
                # R154-F16：模板（is_template=1）不进任务列表
                "FROM tasks WHERE is_template=0 ORDER BY id"):
            try:
                tid = int(r["id"])
            except (TypeError, ValueError):
                continue
            rep = reports.get(tid) or {}
            prog = taskio.task_progress(self.db, tid)
            raw_err = r["last_error"] or ""
            # R154-F7：失败清单条数（落库的完整清单；与「本次失败」不同口径：
            # 那是最近一次运行的采样，这个跨运行累积，直到重试成功或被清空）。
            try:
                n_failed_urls = self.db.failed_url_count(tid)
            except Exception:
                n_failed_urls = 0
            # R154-F9：页面级断点续采账本（"已抓 x/y 页"）。
            # 口径 = 累计（跨运行），与 task_pages 表逐字一致，别处不另算。
            try:
                pstat = self.db.task_page_stats(tid)
            except Exception:
                pstat = {"done": 0, "total": 0}
            rows.append({
                "id": tid, "name": r["name"], "status": r["status"],
                "urls": r["entry_urls"],
                # ---- #26 两个张数 ----
                "got": int(rep.get("downloaded", 0) or 0),
                "total": int(prog.get("downloaded", 0) or 0),
                # V0.0.27：这个任务名下**登记在库**的图片总张数（含隔离/拉黑），
                # 与上面的「累计在库」不是同一口径：那个是筛过可用的成品张数。
                "images": int(img_cnt.get(tid, 0)),
                "found": int(rep.get("images_found", 0) or 0),
                "failed": int(rep.get("failed", 0) or 0),
                # ---- R154-F7：失败网址条数 ----
                "failed_urls": n_failed_urls,
                # ---- R154-F9：已抓页面 x/y ----
                "pages_done": int(pstat.get("done", 0) or 0),
                "pages_total": int(pstat.get("total", 0) or 0),
                # ---- #38 白话中文 ----
                "error": plain_reason(raw_err),
                "error_raw": raw_err[:200],
                "errors": [plain_reason(e)
                           for e in (rep.get("errors") or [])][:5],
            })
        return rows

    def _api_gallery(self):
        """旧接口保留（老书签/脚本可能还在用），但**也加上了分页上限**。"""
        return [{"id": r["id"], "tags": r["tags"], "score": r["score"],
                 "url": f"/thumb/{r['id']}"}
                for r in self.db.query(
                    "SELECT id, tags, score FROM images "
                    "WHERE in_quarantine=0 AND path IS NOT NULL "
                    "ORDER BY id DESC LIMIT ?", (PAGE_LIMIT_DEFAULT,))]

    # ---- R228 / R154-F17：图库标签（Web 与桌面同一套纯函数） ----
    #
    # ⚠ 三个端点全部走 ``tags_util``，本文件**不自己解析 tags 字符串**：
    #   桌面端 MainWindow 的 _read_tags / _merge_tags / _strip_tags 也已
    #   转发到同一层（见 gui/main_window.py），两端口径只有一处实现。
    #   Web 自己再写一遍 = 第二套口径，迟早「网页加的标签桌面端读不出来」。

    @staticmethod
    def _tag_ids(u) -> list:
        """``?ids=1,2,3`` → ``[1, 2, 3]``（非数字跳过，上限 500）。

        与 ``_api_dislike_options`` 的 ids 解析同口径（逗号分隔、容错），
        上限与 ``_api_image_action`` 一致（防超大请求）。
        """
        raw = (parse_qs(u.query).get("ids") or [""])[0]
        out = []
        for p in str(raw).split(","):
            p = p.strip()
            if not p:
                continue
            try:
                out.append(int(p))
            except (TypeError, ValueError):
                continue
        return out[:500]

    def _tag_rows(self, ids) -> list:
        """取这几张图的 ``id`` / ``tags``（按 ids 原顺序，缺行跳过）。"""
        out = []
        for iid in ids:
            try:
                r = self.db.query(
                    "SELECT id, tags FROM images WHERE id=?", (iid,))
            except Exception:
                r = []
            if r:
                out.append({"id": r[0]["id"], "tags": r[0]["tags"]})
        return out

    def _api_tags(self) -> dict:
        """全库标签计数 —— Web 图库「标签筛选」树的数据源。

        ⚠ 2026-10-29 修：原来 `SELECT tags ... LIMIT 5000`，只数库里**前 5000 行**。
        几千张时看不出来，10 万张的库里标签旁边的数字只有真实值的 5% ——
        用户会以为导入坏了。现在改成 SQL 侧用 `json_each` 一次扫描聚合
        （C 层展开数组，不在 Python 里逐行 json.loads），并把结果缓存 30 秒
        （标签树是每进一次图库就问一次，10 万行每次都重算扛不住）。

        老库里有「逗号分隔裸串」的历史数据，`json_each` 遇到会抛错 ——
        退回 Python 侧 `count_tags`，同样**不再截断行数**。
        标签写操作会清缓存（见 `_tag_cache_clear`），所以加完标签立刻能看到。
        """
        from .tags_util import TAG_TOP_N, count_tags
        now = time.time()
        with _TAG_CACHE_LOCK:
            if (_TAG_CACHE["rows"] is not None
                    and now - _TAG_CACHE["ts"] < 30):
                return {"ok": True, "items": list(_TAG_CACHE["rows"])}
        items = []
        try:
            # LIMIT 放宽到 top*3：带空格的标签下面会被滤掉，留点余量免得取不满
            rows = self.db.query(
                "SELECT j.value AS tag, COUNT(*) AS n "
                "FROM images, json_each(images.tags) AS j "
                "WHERE images.tags LIKE '[%' "
                "GROUP BY j.value ORDER BY n DESC, j.value ASC LIMIT ?",
                (int(TAG_TOP_N) * 3,)) or []
            for r in rows:
                t = str(r["tag"] or "")
                # tag: 语法按空格切词，带空格的标签拼进查询串会筛错东西
                if not t or " " in t:
                    continue
                items.append({"tag": t, "n": int(r["n"] or 0)})
            items = items[:TAG_TOP_N]
        except Exception:
            items = []
        if not items:
            try:
                rows = self.db.query(
                    "SELECT tags FROM images "
                    "WHERE tags IS NOT NULL AND tags NOT IN ('', '[]')") or []
                items = [{"tag": t, "n": n} for t, n in count_tags(rows, TAG_TOP_N)]
            except Exception:
                return {"ok": True, "items": []}
        with _TAG_CACHE_LOCK:
            _TAG_CACHE["ts"] = now
            _TAG_CACHE["rows"] = items
        return {"ok": True, "items": items}

    def _api_image_tags(self, u) -> dict:
        """GET：选中图各自的标签 + **并集**（删标签弹窗的勾选清单）。

        ⚠ 并集**保序去重**（``tags_util.union_tags``），与桌面端
        ``gallery_remove_tag`` 的清单同口径 —— 顺序不同用户就得重找一遍。
        """
        ids = self._tag_ids(u)
        if not ids:
            return {"ok": True, "items": [], "tags": []}
        rows = self._tag_rows(ids)
        from .tags_util import row_tags, union_tags
        return {"ok": True, "items": row_tags(rows),
                "tags": union_tags(rows)}

    def _api_image_tags_post(self, body) -> dict:
        """POST：批量**加** / **删**标签（``act=add`` / ``act=remove``）。

        与桌面端 ``gallery_add_tag`` / ``gallery_remove_tag`` 同一套纯函数：
          · add    去重不覆盖、保序追加；已有该标签的行不写、不计数；
          · remove 只删勾中的，没勾的原样留；没命中的行不写；
          · 多标签按顺序一次算完再**统一写库**（每行只 UPDATE 一次）；
          · 只改 ``images.tags``，**不碰四态机标志位**（加标签不是审核
            决定，图不该因此离开当前视图 —— 与 move/delete 的分工）；
          · done = 真正被改的行数（缺 id / 无变化的都不冒充满分）。
        """
        ids = body.get("ids") or []
        act = str(body.get("act") or "")
        if not isinstance(ids, list):
            return {"ok": False, "error": "ids 必须是数组"}
        try:
            ids = [int(i) for i in ids][:500]
        except (TypeError, ValueError):
            return {"ok": False, "error": "ids 必须是数字"}
        if not ids:
            return {"ok": False, "error": "没选任何图片"}
        tags = [str(t).strip() for t in (body.get("tags") or [])
                if str(t).strip()][:50]
        if not tags:
            return {"ok": False, "error": "没给标签"}
        if act not in ("add", "remove"):
            return {"ok": False, "error": f"不支持的动作 {act}"}
        rows = self._tag_rows(ids)
        if not rows:
            return {"ok": True, "done": 0, "act": act}
        from .tags_util import merge_tags, strip_tags
        changed = {}
        if act == "add":
            cur = list(rows)
            for t in tags:
                plan = merge_tags(cur, t)
                if not plan:
                    continue
                new = {iid: js for iid, js in plan}
                changed.update(new)
                # 下一轮的起点 = 本轮写出的新值，其余行沿用原值
                cur = [{"id": r.get("id"),
                        "tags": new.get(r.get("id"), r.get("tags"))}
                       for r in cur]
        else:
            changed = {iid: js for iid, js in strip_tags(rows, tags)}
        for iid, js in changed.items():
            try:
                self.db.execute(
                    "UPDATE images SET tags=? WHERE id=?", (js, iid))
            except Exception:
                continue
        if changed:
            _tag_cache_clear()      # 加了/删了标签，树里的计数必须立刻跟着变
        return {"ok": True, "done": len(changed), "act": act}

    # ---- R146-B5：AI 词「曾用词」历史（Web 与桌面同一份池子） ----
    def _api_words_history(self) -> dict:
        """把桌面端「曾用词」历史下发给网页。

        ⚠ **不另建数据源**：直接读 cfg 里那四个历史键 —— 桌面端
        `main_window._FILL_ALL_MAP` 用的就是它们（`_remember_web_words`
        也往这里写）。另存一份就会出现「桌面看得到、网页看不到」。
        键名刻意与**任务表单字段名**一致（ai_labels / ai_labels_l2 /
        ai_labels_l3 / ai_blacklist），前端不必再查一张映射表。
        """
        return {"ok": True, "items": {
            "ai_labels": list(getattr(self.cfg, "ai_label_history", []) or []),
            "ai_labels_l2": list(
                getattr(self.cfg, "ai_label_history_l2", []) or []),
            "ai_labels_l3": list(
                getattr(self.cfg, "ai_label_history_l3", []) or []),
            "ai_blacklist": list(
                getattr(self.cfg, "ai_excl_history", []) or []),
        }}

    # ---- R146-C4：不喜欢/拉黑的理由选项（与桌面端 dislike_dialog 同源） ----
    def _api_dislike_options(self, u) -> dict:
        """给 Web 的「为什么不喜欢」模态下发候选词。

        语义照抄桌面端 `ask_dislike_reason`（左栏=主题词未命中，只调权重；
        右栏=排除词，才写 ai_blacklist）。层级（一/二/三级）按桌面端
        `labels_by_tier` 的口径分段下发，网页才能像桌面那样分段显示。
        """
        raw = (parse_qs(u.query).get("ids") or [""])[0]
        ids = []
        for p in str(raw).split(","):
            p = p.strip()
            if not p:
                continue
            try:
                ids.append(int(p))
            except (TypeError, ValueError):
                continue
        ids = ids[:500]
        if not ids:
            return {"ok": False, "error": "没给图片编号"}
        qmarks = ",".join("?" * len(ids))
        rows = self.db.query(
            f"SELECT DISTINCT task_id FROM images WHERE id IN ({qmarks})",
            tuple(ids))
        if not rows:
            return {"ok": True, "labels": [], "blacklist": [],
                    "blacklist_groups": {}, "tiers": {}}
        labels: list[str] = []
        blacklist: list[str] = []
        # R171：右栏黑名单要分组显示，两组各自留一份（去重后按拼音排）。
        #   本任务排除词 = 该任务 ai_blacklist 的并集（用户在这个任务里学过的）
        #   已学进库     = 全局曾用排除词池 cfg.ai_excl_history（跨任务学习成果）
        # 语义照桌面 dislike_dialog 的「含已学进库 + 可手填」口径。
        task_excl: list[str] = []
        learned: list[str] = []
        tiers: dict = {}

        def _merge(bucket: list, words) -> None:
            for w in words or []:
                w = str(w).strip()
                if w and w not in bucket:
                    bucket.append(w)

        for r in rows:
            tid = r["task_id"]
            row = self.db.query(
                "SELECT ai_labels, ai_labels_l2, ai_labels_l3, ai_blacklist "
                "FROM tasks WHERE id=?", (tid,))
            if not row:
                continue
            rec = row[0]
            # 与桌面端同口径：白名单优先，同一词同时出现在黑名单时只留白名单
            _merge(labels, _json_list(rec["ai_labels"]))
            _merge(labels, _json_list(rec["ai_labels_l2"]))
            _merge(labels, _json_list(rec["ai_labels_l3"]))
            _merge(blacklist, _json_list(rec["ai_blacklist"]))
            _merge(task_excl, _json_list(rec["ai_blacklist"]))
            for key, name in (("ai_labels", "一级主题词"),
                              ("ai_labels_l2", "二级细分词"),
                              ("ai_labels_l3", "三级细分词")):
                _merge(tiers.setdefault(name, []), _json_list(rec[key]))
        _merge(learned, getattr(self.cfg, "ai_excl_history", []) or [])
        lset = set(labels)
        blacklist = [b for b in blacklist if b not in lset]
        task_excl = [b for b in task_excl if b not in lset]
        learned = [b for b in learned if b not in lset]
        # 去重口径：一个词只出现在一组 —— 本任务排除词优先（同词既是任务排除词
        # 又在全局池里时，归到「本任务排除词」，否则用户会看到重复两条）。
        tset = set(task_excl)
        learned = [w for w in learned if w not in tset]
        return {"ok": True, "labels": zhsort.sort_words(labels),
                "blacklist": zhsort.sort_words(blacklist),
                "blacklist_groups": {
                    "learned": zhsort.sort_words(learned),
                    "task": zhsort.sort_words(task_excl)},
                "tiers": {k: zhsort.sort_words(v)
                          for k, v in tiers.items() if v}}

    # ---- R146-C9：AI 模型目录（与桌面端同一个 models_lib 清单） ----
    def _models_base_dir(self) -> Path:
        """AI 模型根目录 = 数据目录/models（与桌面端 `_models_base_dir` 同口径）。

        ⚠ 必须走 `user_data_dir()`：便携模式下 models 目录跟着软件目录走，
        自己拼路径会在「装到 D 盘」或「绿色版」上落到错误的地方。
        """
        from .config import user_data_dir

        try:
            override = str(getattr(self.cfg, "models_dir", "") or "").strip()
            if override:
                return Path(override)
        except Exception:
            pass
        return user_data_dir() / "models"

    def _api_sysinfo(self) -> dict:
        """R245：机器信息（CPU / 内存 / GPU / 各盘容量 / 当前下载目录剩余空间）。

        取不到的项返回 ``None`` 并保留字段（页面显示「—」），**不抛异常**：
        这是给人看的信息面板，显卡驱动没装不该让整页 500。
        """
        try:
            from . import sysinfo
            d = sysinfo.collect(self.cfg)
            d["ok"] = True
            d["headline"] = sysinfo.headline(d)
            return d
        except Exception as e:
            return {"ok": False, "error": f"读取机器信息失败：{e}"}

    def _api_models(self) -> dict:
        """模型清单 + 每档状态（使用中 / 就绪 / 已内置 / 未下载）。

        ⚠ 与桌面端**同一份数据源**（`models_lib.sorted_catalog` / `is_ready`
        / `bundled_ready`）—— 另写一份清单必然出现「桌面看得到、网页看不到」。
        ⚠ 下载**不在这里做**：几百 MB，网页端不跑下载任务（按钮在桌面端），
        网页只负责「选用已经能用的那一档」。
        """
        try:
            from . import models_lib
        except Exception as e:
            return {"ok": False, "error": f"模型模块不可用：{e}", "items": []}
        base = self._models_base_dir()
        cur = str(getattr(self.cfg, "ai_model_path", "") or "").strip()
        items = []
        for m in models_lib.sorted_catalog():
            mid = str(m.get("id") or "")
            try:
                ready = bool(models_lib.is_ready(base, mid))
            except Exception:
                ready = False
            builtin = (not ready) and bool(models_lib.bundled_ready(mid))
            try:
                path = Path(models_lib.model_dir(base, mid))
            except Exception:
                path = Path()
            is_cur = False
            if cur and str(path):
                try:
                    is_cur = Path(cur).resolve() == path.resolve()
                except Exception:
                    is_cur = False
            items.append({
                "id": mid, "name": m.get("name") or mid,
                "size_hint": m.get("size_hint") or "",
                "pros": m.get("pros") or "", "note": m.get("note") or "",
                "hardware": models_lib.hardware_line(m),
                "input": m.get("input"),
                "ready": ready, "builtin": builtin, "current": is_cur,
                "path": str(path),
            })
        try:
            usage = models_lib.disk_usage(base)
        except Exception:
            usage = 0
        # R172-1：网页端也要能看到「GPU 开关 + 实际生效的后端」。
        # 与桌面端**同一份** cfg.ai_gpu 与同一个 models_lib 判定函数，
        # 措辞也共用 —— 两端各写一套必然出现「桌面说在用 GPU、网页说 CPU」。
        gpu_want = bool(getattr(self.cfg, "ai_gpu", True))
        try:
            dev = models_lib.device_report(
                gpu_want,
                device=_ai_dev_eff(self.cfg),
                small_model=self._ai_model_is_small())
        except Exception as e:                              # pragma: no cover
            dev = {"want": "gpu" if gpu_want else "cpu", "have": "cpu",
                   "providers": [], "degraded": False,
                   "npu_have": False, "npu_providers": [],
                   "text": f"读不到本机推理后端：{e}"}
        return {"ok": True, "items": items, "base": str(base),
                "usage": _human_bytes(usage), "current_path": cur,
                "gpu_want": gpu_want, "gpu_state": dev.get("text") or "",
                "gpu_have": dev.get("have") or "cpu",
                "npu_have": bool(dev.get("npu_have")),
                "npu_providers": list(dev.get("npu_providers") or []),
                "ai_device": _ai_dev_eff(self.cfg),
                "gpu_degraded": bool(dev.get("degraded")),
                "gpu_providers": list(dev.get("providers") or [])}

    # ---- 网页遥控下载模型 ----
    # 只允许**同时一个**下载：几百 MB 的东西并行会把带宽/磁盘搅乱（桌面端
    # 也是这个规矩），而且进度只有一个，界面才好画。
    _MODEL_DL: dict = {}
    _MODEL_DL_LOCK = __import__("threading").Lock()

    def _api_model_download(self, body) -> dict:
        from . import models_lib

        mid = str(body.get("id") or "").strip()
        if not mid:
            return {"ok": False, "error": "没给模型 id"}
        if not any(x["id"] == mid for x in models_lib.CATALOG):
            return {"ok": False, "error": f"清单里没有这个模型：{mid}"}
        with self._MODEL_DL_LOCK:
            cur = dict(self._MODEL_DL)
            if cur.get("running"):
                return {"ok": False,
                        "error": f"已经在下载「{cur.get('name') or cur.get('id')}」，"
                                 "等它完成或先取消。"}
            name = next((x.get("name") or mid for x in models_lib.CATALOG
                         if x["id"] == mid), mid)
            self._MODEL_DL.clear()
            self._MODEL_DL.update({"running": True, "id": mid, "name": name,
                                   "pct": 0, "text": "正在准备…",
                                   "ok": None, "msg": "", "tried": []})
        base = self._models_base_dir()
        st = self._MODEL_DL

        def on_prog(rel, done, total, src=None):   # noqa: ANN001
            # ⚠ 桌面端踩过：回调是**四参**（含来源），早期写成 0 参会 TypeError
            # 被 except 吞掉 → 进度永远不动。这里明确接收。
            try:
                with self._MODEL_DL_LOCK:
                    st["pct"] = int(done * 100 / total) if total else 0
                    st["text"] = (f"{rel}　{done / 1048576:.1f}/"
                                  f"{(total or 0) / 1048576:.1f} MB")
                    if src and src not in st["tried"]:
                        st["tried"].append(src)
                    st["source"] = src or ""
            except Exception:
                pass

        def run():
            from .models_lib import DownloadWorker

            try:
                wk = DownloadWorker(base, mid, on_prog)
                st["worker"] = wk
                wk.start()
                wk.join()
                ok, msg = wk.result
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
            with self._MODEL_DL_LOCK:
                st.update({"running": False, "ok": bool(ok), "msg": str(msg),
                           "pct": 100 if ok else st.get("pct", 0)})
            try:
                self.db.audit(self._actor(), "model-download", mid,
                              f"{'ok' if ok else 'fail'} {str(msg)[:120]}")
            except Exception:
                pass

        import threading as _th
        _th.Thread(target=run, daemon=True, name=f"mrm-web-dl-{mid}").start()
        return {"ok": True, "id": mid, "name": name}

    def _api_model_download_status(self) -> dict:
        with self._MODEL_DL_LOCK:
            d = dict(self._MODEL_DL)
        d.pop("worker", None)
        if not d:
            return {"ok": True, "running": False}
        return {"ok": True, **d}

    def _api_model_download_cancel(self) -> dict:
        with self._MODEL_DL_LOCK:
            wk = self._MODEL_DL.get("worker")
            if wk is not None:
                # 取消是**协作式**的：worker 每个 256KB 块检查一次标志，
                # 若正卡在连接阶段要等这次请求超时（最多 30 秒）。
                # 所以这里先标「正在取消」，别让界面看起来没反应。
                self._MODEL_DL["canceling"] = True
                self._MODEL_DL["text"] = "正在取消…（等当前请求收尾）"
        if wk is not None:
            try:
                wk.cancel()
            except Exception:
                pass
            return {"ok": True, "canceled": True}
        return {"ok": True, "canceled": False}

    def _api_online_models(self, body) -> dict:
        """拉在线接口的模型列表（OpenAI 兼容 ``GET {base}/models``）。

        body 里可以临时给 url/key（还没保存也能先请尝试）；没给就用配置里的。
        ⚠ 只读、超时 20 秒、错误一律中文：在线接口连不上是常态
        （代理没开 / key 错 / 不是 OpenAI 兼容），不能把异常抛给界面。
        """
        import json as _json
        import urllib.error as _ue
        import urllib.request as _ur
        from urllib.parse import urlsplit

        base = str(body.get("url") or getattr(self.cfg, "ai_api_url", "") or "").strip()
        key = str(body.get("key") or getattr(self.cfg, "ai_api_key", "") or "").strip()
        if not base:
            return {"ok": False, "error": "尚未填写在线 API 地址（例如 https://api.xx.com/v1）"}
        url = base.rstrip("/") + "/models"
        req = _ur.Request(url, headers={"Accept": "application/json"})
        if key:
            req.add_header("Authorization", "Bearer " + key)
        # 与本机其它出站请求同口径：用配置里的代理（留空=直连）
        opener = _ur.build_opener()
        try:
            proxy = str(getattr(self.cfg, "proxy_url", "") or "").strip()
            if proxy:
                opener = _ur.build_opener(_ur.ProxyHandler(
                    {"http": proxy, "https": proxy}))
        except Exception:
            pass
        try:
            with opener.open(req, timeout=20) as r:
                raw = r.read(2 * 1024 * 1024)
        except _ue.HTTPError as e:
            hint = {401: "Key 不对或没权限", 404: "这个地址没有 /models 接口",
                    403: "被拒（可能需要换地区或代理）"}.get(e.code, "接口返回错误")
            return {"ok": False, "error": f"{hint}（HTTP {e.code}）",
                    "url": url, "host": urlsplit(url).hostname}
        except Exception as e:
            # 把 urllib 的英文壳剥掉，只给用户能懂的一句话
            raw = str(e)
            low = raw.lower()
            if "tunnel" in low or "proxy" in low or "502" in low:
                why = "代理连不通（检查代理是否在运行）"
            elif "timed out" in low or "timeout" in low:
                why = "连接超时（网络或代理有问题）"
            elif "name or service" in low or "getaddrinfo" in low:
                why = "域名解析失败（地址写错了？）"
            elif "refused" in low:
                why = "对方拒绝连接（地址/端口不对）"
            else:
                why = raw[:80]
            return {"ok": False, "error": f"连不上：{why}", "url": url}
        try:
            data = _json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return {"ok": False, "error": "返回的不是 JSON（可能不是 OpenAI 兼容接口）",
                    "url": url}
        items = data.get("data") if isinstance(data, dict) else data
        ids = []
        for it in (items or []):
            mid = it.get("id") if isinstance(it, dict) else it
            if mid:
                ids.append(str(mid))
        return {"ok": True, "url": url, "items": sorted(set(ids)),
                "current": str(getattr(self.cfg, "ai_api_model", "") or "")}

    def _api_model_use(self, body) -> dict:
        """把某档模型设为当前使用的本地模型（写 cfg.ai_model_path 并落盘）。

        与桌面端 `_use_model` 同口径：已就绪 → 模型目录；随软件内置 → 包内
        目录；两者都不是 → **拒绝**（选一个没下载的模型等于把 AI 关掉，
        还让用户以为是 AI 坏了）。
        """
        try:
            from . import models_lib
        except Exception as e:
            return {"ok": False, "error": f"模型模块不可用：{e}"}
        mid = str((body or {}).get("id") or "").strip()
        if not mid:
            return {"ok": False, "error": "没给模型编号"}
        base = self._models_base_dir()
        try:
            if models_lib.is_ready(base, mid):
                p = Path(models_lib.model_dir(base, mid))
            elif models_lib.bundled_ready(mid):
                p = Path(models_lib.bundled_models_dir()) / mid
            else:
                return {"ok": False, "error": "该模型尚未下载，请先在桌面端完成下载"}
        except Exception as e:
            return {"ok": False, "error": f"读模型目录失败：{e}"}
        self.cfg.ai_model_path = str(p)
        # V0.0.40：不再把 ai_backend 改写成 local —— builtin 与 local 在引擎里
        # 走的是同一条路（都读 ai_model_path），界面上也已经合并成一个选项，
        # 这里再改一次只会让配置文件里留一个用户没选过的值。
        try:
            from .config import save_config

            save_config(self.cfg)
        except Exception as e:
            return {"ok": False, "error": f"保存设置失败：{e}"}
        return {"ok": True, "id": mid, "path": str(p), "name": mid,
                "note": "已保存，下一次跑任务时生效"}

    def _api_ai_gpu(self, body) -> dict:
        """R172-1：网页端切换「用显卡加速」（写 cfg.ai_gpu 并落盘）。

        与桌面端对话框顶部那个复选框、以及桌面菜单项**同一份配置**；
        生效时机同桌面端：下次建推理会话（= 下次跑任务）时读它。
        ⚠ 这里**不复用** `_api_settings_save`：那个接口一次收一整张设置表单，
        而 GPU 开关在模型页里是独立控件，走单独接口才能做到「点一下立刻落盘」。
        """
        raw = (body or {}).get("on")
        if isinstance(raw, str):
            on = raw.strip().lower() not in ("0", "false", "no", "off", "")
        else:
            on = bool(raw)
        try:
            self.cfg.ai_gpu = on
            from .config import save_config

            save_config(self.cfg)
        except Exception as e:
            return {"ok": False, "error": f"保存设置失败：{e}"}
        try:
            from . import models_lib

            dev = models_lib.device_report(
                on,
                device=_ai_dev_eff(self.cfg),
                small_model=self._ai_model_is_small())
        except Exception:                                    # pragma: no cover
            dev = {"text": "", "have": "cpu", "degraded": False,
                   "providers": []}
        return {"ok": True, "gpu_want": on,
                "gpu_state": dev.get("text") or "",
                "gpu_have": dev.get("have") or "cpu",
                "gpu_degraded": bool(dev.get("degraded")),
                "note": "已保存，下一次跑任务时生效"}

    # ---- R146-C11：相似图片（三路加权，与桌面端 simsearch 同源） ----
    def _api_similar(self, u) -> dict:
        """给一张图找「同一类画面、道具可以不同」的相似图。

        ⚠ 解码与比对跑在**这个请求线程**里：服务端是 ThreadingHTTPServer
        （一条连接一个线程），不会卡住主线程，也不会卡住其它请求 ——
        订单点名「别在主线程跑推理」说的正是这件事。
        ⚠ 候选集**必须设上限**：逐张解码，桌面端实测 600 张 ≈ 15s，
        不设上限会让手机端一直转圈（用户只会以为软件坏了）。
        """
        from .simsearch import features_for_image, rank_similar

        q = parse_qs(u.query)
        try:
            iid = int((q.get("id") or ["0"])[0])
        except (TypeError, ValueError):
            return {"ok": False, "error": "图片编号不对"}
        try:
            limit = int((q.get("limit") or ["60"])[0])
        except (TypeError, ValueError):
            limit = 60
        limit = max(1, min(limit, 200))
        cap = SIMILAR_CANDIDATE_CAP
        rows = self.db.query(
            "SELECT id, path, tags, phash FROM images "
            "WHERE path IS NOT NULL AND in_quarantine=0 "
            "AND tags IS NOT NULL AND tags NOT IN ('', '[]') "
            "ORDER BY id DESC LIMIT ?", (cap,))
        cands = []
        for r in rows:
            f = features_for_image(r["path"])
            if f is None:
                continue
            cands.append({"id": r["id"], "path": r["path"],
                          "tags": r["tags"], "phash": r["phash"],
                          "color": f})
        seed_row = None
        for c in cands:
            if c["id"] == iid:
                seed_row = c
                break
        if seed_row is None:
            # 种子自己没提到特征（文件没了/太小）⇒ 退化成只按标签与哈希，
            # 与桌面端同一个分支，别在这里另发明一种退化方式。
            sr = self.db.query(
                "SELECT id, path, tags, phash FROM images WHERE id=?", (iid,))
            if not sr:
                return {"ok": False, "error": "这张图已不在库里"}
            s = sr[0]
            seed_row = {"id": s["id"], "path": s["path"],
                        "tags": s["tags"], "phash": s["phash"],
                        "color": None}
        hits = rank_similar(seed_row, cands, limit=limit)
        items = []
        for h in hits:
            items.append({
                "id": h.get("id"),
                "score": round(float(h.get("score") or 0.0), 4),
                "tags": _json_list(h.get("tags"))[:12],
                "url": f"/thumb/{h.get('id')}",
            })
        return {"ok": True, "items": items, "candidates": len(cands),
                "seed": iid}

    # ---- B5 #50：单按钮动作 ----
    def _api_image_action(self, body):
        ids = body.get("ids") or []
        act = str(body.get("act") or "")
        if not isinstance(ids, list):
            return {"ok": False, "error": "ids 必须是数组"}
        try:
            ids = [int(i) for i in ids][:500]      # 上限 500，防超大请求
        except (TypeError, ValueError):
            return {"ok": False, "error": "ids 必须是数字"}
        if not ids:
            return {"ok": False, "error": "没选任何图片"}
        allowed = ("like", "dislike", "blacklist", "favorite", "delete",
                   "move")
        if act not in allowed:
            return {"ok": False, "error": f"不支持的动作 {act}"}
        # R146-C4：Web「为什么不喜欢」模态回传的两栏结果。
        # 语义与桌面端 ask_dislike_reason 完全一致，别自创：
        #   unmatched  → 只是**这张图没匹配上该主题词**，做负向权重，
        #                **绝不写进 ai_blacklist**（否则整个主题被误杀）；
        #   exclusions → 才写进任务 ai_blacklist，下次自动跳过。
        # 两者都只在 dislike / blacklist 时有效，其它动作忽略（传了也不学）。
        unmatched = [str(w).strip() for w in (body.get("unmatched") or [])
                     if str(w).strip()][:50]
        exclusions = [str(w).strip() for w in (body.get("exclusions") or [])
                      if str(w).strip()][:50]
        # V0.0.12：桌面 dislike_dialog 有、网页模态以前缺的两样——
        #   manual  = 用户**手填**的词：排除词里只有这个来源允许走词表桥接
        #             学习（learn_manual / user_typed，R174 底线）；
        #   correct = 「模型误判」勾选后的**正确标签**：正向学习信号，
        #             绝不写黑名单（用户原话场景：机器认错了、这其实是我想要的）。
        manual = [str(w).strip() for w in (body.get("manual") or [])
                  if str(w).strip()][:50]
        correct = [str(w).strip() for w in (body.get("correct") or [])
                   if str(w).strip()][:50]
        done = 0
        tids = set()
        # V0.0.8：判退动作学到的「别找」词汇总（次数 + 升级为下载前拦截的词），
        # 随响应返回，网页审核页 toast 给用户看——让「点一下 → 学到什么」当场可见。
        learned_total = 0
        learned_hard = []
        # V0.0.12：网页动作的可撤销快照收集（栈常驻在 server 上）。
        # 拿不到队列对象（纯 API 测试模式）就整个静默跳过——动作照常做，
        # 只是不可撤，绝不能因为撤销功能把动作本身弄失败。
        # ⚠ 快照必须在**改动前**取（撤销恢复到的是动作前状态）；每条在
        # 自己那轮循环顶部取、动作确认生效（done 计数处）才登记。
        _rq = self._web_review_queue()
        snaps = []
        for iid in ids:
            snap = None
            if _rq is not None:
                try:
                    snap = _rq.snapshot_web(iid)
                except Exception:
                    snap = None
            try:
                if act == "favorite":
                    cur = self.db.query(
                        "SELECT favorite, task_id, tags FROM images WHERE id=?",
                        (iid,))
                    if not cur:
                        continue
                    self.db.execute(
                        "UPDATE images SET favorite=? WHERE id=?",
                        (0 if cur[0]["favorite"] else 1, iid))
                    done += 1
                    if snap is not None: snaps.append(snap)
                    # R166：收藏 = 正样本（用户明确想留），与桌面端同口径。
                    # ⚠ 只在**收**（0→1）时学，取消收藏不学 —— 「取消收藏」
                    #   只是收回一个主观标记，不是"这张不要"，学成负样本会把
                    #   一张用户其实没意见的图打成负权重（过度解读）。
                    if not int(cur[0]["favorite"] or 0):
                        self._learn_web_action(
                            "favorite", self._tags_of(cur[0]),
                            "web_gallery", iid, cur[0]["task_id"])
                    continue

                if act == "move":
                    # #32「移动」：把选中的图**送到待审核队列**。
                    # 与另外三个批量动作的分工（四态机，缺一不可）：
                    #   like    审核通过 → 出待审核，留在图库
                    #   dislike 拒绝     → 出待审核，留在图库（+reviews 行）
                    #   delete  删除     → 进隔离区（软删，可还原）
                    #   move    移动     → 进待审核（needs_review=1）
                    # ⚠ 必须同时清 in_quarantine：隔离区与待审核**互斥** ——
                    #   桌面端隔离区管理的取数口径明确写着「needs_review=1 的
                    #   走审核页，不在这里混」，两边都置位会让这张图在两个
                    #   视图里都看不见（ok 但做了另一件事）。
                    # ⚠ 刻意**不写 reviews 行、不喂学习器**：移动不是审核
                    #   决定，只是改排队位置；喂了学习器等于替用户做判断。
                    # ⚠ 先查存在再 UPDATE：与其它动作同口径，
                    #   done 只数真正作用到的行（缺 id 不冒充满分）。
                    row = self.db.query(
                        "SELECT id, in_quarantine FROM images WHERE id=?", (iid,))
                    if not row:
                        continue
                    # V0.0.6：从隔离区「移入待审核」以前只改两个标志，
                    # 文件还留在隔离目录里，于是图库/待审核里出现一张
                    # 「住在隔离区的图」，桌面整理时按路径就找不着它。
                    # 和「审核通过」一样把文件搬回图库再回写路径；
                    # 搬不动（外部原图 / 被占用）就维持原标志改法并留日志。
                    if int(row[0]["in_quarantine"] or 0) == 1:
                        newp = self._web_release_to_gallery(iid)
                        if newp:
                            self.db.execute(
                                "UPDATE images SET path=? WHERE id=?",
                                (newp, iid))
                    self.db.execute(
                        "UPDATE images SET needs_review=1, in_quarantine=0, "
                        "review_reason=? WHERE id=?",
                        (_reasons.MOVE_MANUAL, iid))
                    done += 1
                    if snap is not None: snaps.append(snap)
                    continue

                dec = {"like": "like", "dislike": "dislike",
                       "blacklist": "blacklist", "delete": "dislike"}[act]
                # R158-Bug2/Bug3：这里顺带把 in_quarantine 一起查出来 ——
                # 「点在哪个视图上」前端不传，只能由当前标志位反推，
                # 而 dislike/like 的最终归属**完全取决于**这张图现在在哪。
                # R166：tags 一并取出（下面要喂学习器），顺手省一条查询。
                row = self.db.query(
                    "SELECT task_id, in_quarantine, tags FROM images WHERE id=?",
                    (iid,))
                if not row:
                    continue
                if row[0]["task_id"] is not None:
                    tids.add(int(row[0]["task_id"]))
                was_q = int(row[0]["in_quarantine"] or 0) == 1
                self.db.execute(
                    "INSERT INTO reviews(image_id,decision,task_id) "
                    "VALUES (?,?,?)", (iid, dec, row[0]["task_id"]))
                if act == "delete":
                    if was_q:
                        # R164-Bug1：隔离区内删除 = 真删除（删文件 + 记录，
                        # 不可还原），与桌面端隔离区删除语义一致。图库/待审核
                        # 里删除仍走"进隔离区（软删可还原）"—— 由 was_q 区分。
                        # R166：**必须先学再删** —— `_hard_delete_image` 会把
                        #   reviews 行一起清掉，放它后面学等于没学；而学习本身
                        #   写的是 preferences（全局权重）与 learn_samples
                        #   （append-only 留痕），都不受图片行消失影响。
                        _fb = self._learn_web_action(
                            "purge", self._tags_of(row[0]),
                            "web_quarantine", iid, row[0]["task_id"])
                        if _fb:
                            learned_total += int(_fb.get("added") or 0)
                            learned_hard += list(_fb.get("hardened") or [])
                        self._hard_delete_image(iid)
                        done += 1
                        if snap is not None: snaps.append(snap)
                        continue
                    # R143：进隔离区必须**同时清 needs_review** ——
                    # 四态机要求「待审核 ↔ 隔离区互斥」（见 move 分支注释）。
                    # 少了 needs_review=0，这张图会同时出现在隔离区与待审核，
                    # 正是用户实测"web 隔离区和待审核像同一个东西"的成因。
                    self.db.execute(
                        "UPDATE images SET in_quarantine=1, needs_review=0, "
                        "quarantined_at=datetime('now') WHERE id=?", (iid,))
                else:
                    if act == "like" and was_q:
                        # E13：隔离区页「审核通过」的真实语义 = 桌面端隔离区
                        # 管理的「还原到图库」（gui/main_window restore()），
                        # 不是普通 LIKE，两处必须对齐：
                        #   ① blacklisted=0 —— 拉黑+隔离的图选择「通过」后若只清
                        #      in_quarantine，blacklisted 仍是 1 ⇒ 图库
                        #      (blacklisted=0) 与隔离区 (in_quarantine=1)
                        #      **两个视图都看不见**，"永困"换个形态复发；
                        #   ② 文件物理搬回图库（桌面走 ReviewQueue.
                        #      _release_to_gallery：按域名子目录、冲突加序号、
                        #      本来就在图库则原地不动）+ path 回写 —— 不搬的话
                        #      文件留在隔离区目录，图库里是一张"住在隔离区的图"。
                        # ⚠ image_action_sets 保持不动：普通 LIKE（待审核页
                        #   点通过）语义不变，R179 语义表护栏照常成立。
                        self.db.execute(
                            "UPDATE images SET needs_review=0, "
                            "in_quarantine=0, blacklisted=0 WHERE id=?",
                            (iid,))
                        newp = self._web_release_to_gallery(iid)
                        if newp:
                            self.db.execute(
                                "UPDATE images SET path=? WHERE id=?",
                                (newp, iid))
                    else:
                        # R158-Bug2：图库选择「不喜欢/拉黑」原来只清 needs_review，
                        # 而图库里的图本来就是 needs_review=0 ⇒ 那条 UPDATE 是
                        # **空操作**，图一动不动地留在图库（"点了没效果"）。
                        # R158-Bug3：隔离区选择「审核通过」原来只清 needs_review，
                        # 从不碰隔离区真正的标志 in_quarantine ⇒ 永远出不去。
                        # 两个方向现在都走同一张语义表 `image_action_sets`，
                        # 与桌面端 `_do_apply` 同口径（改一处不会再漏另一端）。
                        self.db.execute(
                            "UPDATE images SET "
                            + image_action_sets(act, was_q)
                            + " WHERE id=?", (iid,))
                # R166：网页端每个判断动作都参与学习，且**来源可区分**：
                #   was_q=True  这张图本来在隔离区 → 「还原回图库」= restore
                #   was_q=False 图库/待审核里判定   → 原语义（like/dislike/…）
                # 之前这里一律 learn(dec) 且写死 task_id=0，导致隔离区点
                # 「审核通过」（实为"还原"）在看板上与图库点通过混成一条。
                _src = "web_quarantine" if was_q else "web_gallery"
                _act = ("restore" if (act == "like" and was_q) else dec)
                _fb = self._learn_web_action(_act, self._tags_of(row[0]),
                                             _src, iid, row[0]["task_id"])
                if _fb:
                    learned_total += int(_fb.get("added") or 0)
                    learned_hard += list(_fb.get("hardened") or [])
                done += 1
                if snap is not None: snaps.append(snap)
            except Exception:
                continue
        if act in ("dislike", "blacklist") and (exclusions or unmatched
                                                or manual or correct):
            self._apply_web_dislike_reasons(tids, exclusions, unmatched,
                                            manual=manual, correct=correct)
        # V0.0.12：本批全部生效后才压栈（学后权重/反馈账本在 finalize 里补取，
        # 循环内学的东西撤销时才能一并退回）。部分成功也压：撤销只退已生效的。
        if snaps and _rq is not None:
            try:
                for _s in snaps:
                    _rq.finalize_web(_s)
                _rq.push_web(act, snaps)
            except Exception:
                pass
        out = {"ok": True, "done": done, "act": act}
        if snaps:
            out["undoable"] = True
        if learned_total or learned_hard:
            out["learned"] = {"total": learned_total,
                              "hardened": sorted(set(learned_hard))}
        return out

    # ------------------------------------------------------------ V0.0.12
    _WEB_RQ_ATTR = "_web_review_queue_obj"

    def _web_review_queue(self):
        """网页端常驻 ReviewQueue（撤销栈所在）。

        挂在 **server** 上跨请求存活——handler 每请求新建（`self.queue`
        那个历史坑，见任务快速添加处注释）。纯 API 测试模式（server 是
        SimpleNamespace 桩）没有隔离目录配置时返回 None，撤销功能自动隐身。
        """
        srv = getattr(self, "server", None)
        rq = getattr(srv, self._WEB_RQ_ATTR, None)
        if rq is not None:
            return rq
        try:
            from .review.queue import ReviewQueue
            rq = ReviewQueue(self.db,
                             getattr(self.cfg, "isolate_dir", ""), self.cfg)
            setattr(srv, self._WEB_RQ_ATTR, rq)
            return rq
        except Exception:
            return None

    def _api_review_undo(self):
        rq = self._web_review_queue()
        if rq is None:
            return {"ok": False, "error": "当前环境不支持撤销"}
        if not rq.can_undo():
            return {"ok": False, "error": "没有可撤销的操作",
                    **rq.undo_info()}
        desc = rq.undo()
        return {"ok": True, "message": desc or "已撤销", **rq.undo_info()}

    def _api_label_bridge_preview(self, body):
        """V0.0.12：手填词 → 词表标签 预演（桌面 _bridge_preview 同一实现）。"""
        words = [str(w).strip() for w in ((body or {}).get("words") or [])
                 if str(w).strip()][:10]
        if not words:
            return {"ok": True, "hints": []}
        try:
            from .classify.label_bridge import get_bridge
            br = get_bridge(self.db)
            hints = []
            for w in words:
                hit = br.resolve(w) if br is not None else None
                hints.append({"word": w,
                              "hint": (br.hint(hit) if (br and hit) else
                                       f"{w} → 不在词表里，只按原词记")})
            return {"ok": True, "hints": hints}
        except Exception as e:
            return {"ok": False, "error": str(e), "hints": []}

    def _api_image_rename(self, body):
        """V0.0.12：网页批量重命名。body = {ids, mode: 'prefix'|'template', value}。

        与桌面图库菜单走 rename_ops 同一份实现：原有图库文件跳过并计数
        回报（红线）、改名与 path 更新同步、撞名顺延编号。
        """
        ids = (body or {}).get("ids") or []
        mode = str((body or {}).get("mode") or "")
        value = str((body or {}).get("value") or "")
        if not isinstance(ids, list) or not ids:
            return {"ok": False, "error": "没选任何图片"}
        try:
            ids = [int(i) for i in ids][:500]
        except (TypeError, ValueError):
            return {"ok": False, "error": "ids 必须是数字"}
        from . import rename_ops
        if mode == "prefix":
            r = rename_ops.rename_by_prefix(self.db, self.cfg, ids, value)
        elif mode == "template":
            r = rename_ops.rename_by_template(self.db, self.cfg, ids, value)
        else:
            return {"ok": False, "error": "mode 应为 prefix 或 template"}
        if r.get("error"):
            return {"ok": False, "error": r["error"]}
        self.db.audit("gallery", "rename", "",
                      f"web {mode} {r['renamed']} 张")
        return {"ok": True, **r}

    def _api_review_sample(self, body):
        """随机抽检：把 N 张「AI 自动隔离、还没进审核」的图拉进待审核。

        复用桌面同款 ReviewQueue.sample_quarantine（含送审原因写法）——
        两端逻辑必须同一份，桌面改了网页自动跟上。
        """
        rq = self._web_review_queue()
        if rq is None:
            return {"ok": False, "error": "当前环境不支持抽检"}
        try:
            n = int((body or {}).get("n") or 12)
        except (TypeError, ValueError):
            n = 12
        n = max(1, min(100, n))
        ids = rq.sample_quarantine(n)
        return {"ok": True, "picked": len(ids), "ids": ids}

    def _api_review_smart(self, body):
        """智能抽样：专挑模型最置信度不足（离阈值最近）的 N 张进待审核。"""
        rq = self._web_review_queue()
        if rq is None:
            return {"ok": False, "error": "当前环境不支持抽检"}
        try:
            n = int((body or {}).get("n") or 20)
        except (TypeError, ValueError):
            n = 20
        n = max(1, min(100, n))
        tid = (body or {}).get("task_id")
        try:
            tid = int(tid) if tid not in (None, "") else None
        except (TypeError, ValueError):
            tid = None
        ids = rq.smart_sample(n, tid)
        return {"ok": True, "picked": len(ids), "ids": ids}

    @staticmethod
    def _tags_of(row) -> list:
        """从一行 images 记录里取出标签列表（R166）。

        ``row`` 是 sqlite3.Row 或 dict；缺列/空值一律给空列表 —— 上游
        ``learn_action`` 对空标签会直接返回不记，不会产生脏样本。
        ⚠ 走 ``simsearch.split_tags`` 而不是裸 ``json.loads``：标签在库里
        可能以「逗号分隔」的老格式存着（历史遗留），统一口径才不会漏学。
        """
        try:
            raw = row["tags"] if row is not None else None
        except Exception:
            raw = None
        if not raw:
            return []
        try:
            from .simsearch import split_tags
            return split_tags(raw)
        except Exception:
            return []

    def _web_release_to_gallery(self, iid: int) -> str:
        """E13：把隔离区文件的物理位置还原回图库（桌面端同款逻辑）。

        复用 ``ReviewQueue._release_to_gallery``（按域名子目录、冲突加序号、
        源文件已在图库内则原地不动返回原路径；文件不存在也原样返回），
        不另写一份搬运逻辑 —— 两处口径分叉就是 R143 的老伤。
        任何异常返回 ""（调用方跳过 path 回写，标志位照常清）。
        """
        try:
            from .repo import get_image
            from .review.queue import ReviewQueue
            rec = get_image(self.db, iid)
            if not rec:
                return ""
            rq = ReviewQueue(self.db,
                             getattr(self.cfg, "isolate_dir", ""), self.cfg)
            return rq._release_to_gallery(rec)
        except Exception:
            return ""

    def _learn_web_action(self, act: str, tags: list, source: str,
                          image_id=None, task_id=None) -> None:
        """网页端的统一学习入口（R166）：一律走 ``learn_action``。

        ⚠ **任务号一律传 0**：学习权重是**全局的**（``repo.GLOBAL_TASK_ID``，
        见 ``learning_stats`` 的模块说明），``set_preference`` 内部本来就
        钉死 task_id=0。这里保留 ``task_id`` 参数只为写进 learn_samples
        的来源留痕（"这张图当年属于哪个任务"），**不影响权重归属**。
        """
        try:
            from .classify.preference import PreferenceLearner
            lr = PreferenceLearner(self.db, 0)
            lr.learn_action(act, tags, source=source, image_id=image_id,
                            sample_task_id=task_id)
            # V0.0.8：把「这次判退学到了哪些别找词」带回去给前端显示。
            fb = getattr(lr, "last_feedback", None)
            if fb and fb.get("ok"):
                return fb
        except Exception:
            pass          # 学习失败不该让审核/删除本身失败
        return None

    def _hard_delete_image(self, iid: int) -> None:
        """R164-Bug1：隔离区内删除 = 真删除。删原图文件 + 评价记录 + 图片行。

        ⚠ 与桌面端 `_isolate` 反向；**不可还原**，故只在本单 quarantine 视图的
           删除路径调用（was_q 已确认这张图当前就在隔离区）。
        ⚠ 不动其它任务/数据；文件删除失败（已不在盘上/路径为空）静默跳过，
           不影响 DB 清理 —— 删库记录才是"彻底删除"的权威动作。
        """
        row = self.db.query("SELECT path FROM images WHERE id=?", (iid,))
        path = row[0]["path"] if row else None
        # V0.0.3：网页端也走统一安全入口 —— 用户原有图库（F 盘）的文件
        # 只清登记记录，绝不删磁盘原文件。
        try:
            from .safe_delete import delete_image_file
            delete_image_file(path, self.cfg,
                              recycle=bool(getattr(self.cfg, "use_recycle", True)))
        except Exception:
            pass
        # ⚠ SQLite 未开外键强制（PRAGMA 仅 journal/busy/synchronous），手动先清
        #   reviews 再删 images，避免悬空引用；顺序固定不依赖外键级联。
        self.db.execute("DELETE FROM reviews WHERE image_id=?", (iid,))
        self.db.execute("DELETE FROM images WHERE id=?", (iid,))

    def _apply_web_dislike_reasons(self, tids, exclusions, unmatched,
                                   manual=(), correct=()) -> None:
        """把 Web 模态勾出来的理由落到任务与学习器上。

        ⚠ 与桌面端 review_tab 的同款处理**逐条对齐**：
          · exclusions → 追加进每个相关任务的 ai_blacklist（去重、不改其它字段）；
          · unmatched  → PreferenceLearner 负向学习，**不碰 ai_blacklist**；
          · manual（V0.0.12）→ 手填词走 learn_manual（词表桥接 + user_typed），
            同时并入排除词写 ai_blacklist——桌面 do_exclude 就是这个组合：
            `excl.append(custom)` 之后走桥接学习，两头都不缺；
          · correct（V0.0.12）→ 「模型误判」的正确标签走 learn_manual **like 方向**，
            **绝不写黑名单**（R174 §4 底线：这是纠正误标，不是排除内容）。
        ⚠ 整段包 try：学习/写词失败不该让「不喜欢」这个动作本身变成失败。
        """
        try:
            manual = [str(w).strip() for w in (manual or []) if str(w).strip()]
            correct = [str(w).strip() for w in (correct or []) if str(w).strip()]
            excl_all = list(exclusions or [])
            for w in manual:
                if w not in excl_all:
                    excl_all.append(w)
            for tid in sorted(tids or []):
                if excl_all:
                    row = self.db.query(
                        "SELECT ai_blacklist FROM tasks WHERE id=?", (tid,))
                    cur = _json_list(row[0]["ai_blacklist"]) if row else []
                    add = [w for w in excl_all if w not in cur]
                    if add:
                        self.db.execute(
                            "UPDATE tasks SET ai_blacklist=? WHERE id=?",
                            (json.dumps(cur + add, ensure_ascii=False), tid))
                from .classify.preference import PreferenceLearner
                if unmatched:
                    PreferenceLearner(self.db, tid).learn("dislike", unmatched)
                # #64：手填的词走词表桥接 —— 「金发」要记到词表标签
                # 「blond hair」上，否则学进去了也永远打不着地。
                if manual:
                    PreferenceLearner(self.db, tid).learn_manual(
                        "dislike", manual)
                # R174 §4：「正确标签」→ 正向学习信号（learn_manual /
                # user_typed / like 方向），永远不会写进硬黑名单。
                if correct:
                    PreferenceLearner(self.db, tid).learn_manual(
                        "like", correct)
        except Exception:
            pass

    # ---- B7 #52 全面清理（保守默认：前端默认不勾任何一项） ----
    def _api_cleanup(self, body):
        plan = body.get("plan") or {}
        if not isinstance(plan, dict):
            return {"ok": False, "error": "plan 必须是对象"}
        # 允许的清理项白名单。**刻意不含** 适配器 / 模型 / 设置 / 学习记录
        # —— 桌面端的「全面清理」明确保留这四样（用户教过的偏好最贵）。
        allowed = {
            # ⚠⚠ R67：这两个键原来都写着 `DELETE FROM reviews`（**键名与SQL 对不上**）。
            # 用户勾「清理任务」⇒ 真删的是 reviews 表（审核记录），tasks 表一根没动，
            # 返回体还报 done:["tasks"] ⇒ 「ok 但做了另一件事」。
            # 审核记录是学习器的输入，删掉等于静默损坏用户教过的偏好。
            # 前端 app.js 的 6 个键与这里逐个对应，改一侧必须同步另一侧
            # （tests/test_r67_web_cleanup_mapping.py 会钉住这个对应关系）。
            "tasks": "DELETE FROM tasks",
            "reviews": "DELETE FROM reviews",
            "quarantine": "UPDATE images SET in_quarantine=0",
            # ⚠ R67：`images` 原来是 None —— 勾了只往 done 里append 一句就返回成功，
            # images 表一行没删，用户却看到「已清理：images」。同样是「ok 但什么都没发生」。
            # 对应桌面端 cleanup._ITEM_TABLES["library"] = ("images",)。
            "images": "DELETE FROM images",
            # 这两项是**物理目录**（缩略图/日志），网页端不动硬盘，只回报，
            # 真删由桌面端「全面清理」负责（那是 risky 级操作）。
            "thumbs": None,
            "logs": None,
        }
        done = []
        # R221/C10：FK 开启后必须「子表先于父表」—— reviews 引用 images，
        # failed_urls/task_pages 引用 tasks。plan 来自前端 JSON，键序不定 ⇒
        # 固定按 reviews → images → tasks（连同两张子表一起清）→ 其余 执行。
        # （原来先删 images 会撞 FK，异常还被 except 吞掉 ⇒「ok 但没清」。）
        # R154-F9 的 task_pages 兜底清理并入下面的 tasks 分支。
        _fk_order = ("reviews", "images", "tasks")
        keys = [k for k in _fk_order if k in plan] + \
               [k for k in plan.keys() if k not in _fk_order]
        for k in keys:
            if k not in allowed:
                continue
            sql = allowed[k]
            if sql is None:
                done.append(k)
                continue
            try:
                if k == "tasks":
                    # 父表前先清两张引用 tasks 的子表（与删任务同口径）
                    for _child in ("failed_urls", "task_pages"):
                        try:
                            self.db.execute(f"DELETE FROM {_child}")
                        except Exception:
                            pass
                if k == "images":
                    # R221/C10：reviews 是 images 的子表 —— 勾「已落库图」
                    # 时必须级联清掉 reviews 再删 images，否则 FK 拦下 +
                    # except 吞掉 ⇒「ok 但没清」（R67 同族，回归实测抓到）。
                    # 与所有删图路径（先删 reviews 再删图）同口径；学习样本
                    # 自 R166 起在 learn_samples，不依赖 reviews 表。
                    try:
                        self.db.execute("DELETE FROM reviews")
                    except Exception:
                        pass
                self.db.execute(sql)
                done.append(k)
            except Exception:
                continue
        try:
            self.db.audit(self._actor(), "cleanup", ",".join(done))
        except Exception:
            pass
        return {"ok": True, "done": done}

    def _api_task_ctl(self, task_id: int, act: str, with_images: bool = False):
        # Web 端控任务：只改内存里的队列状态，不碰任务定义。
        q = getattr(self.server, "queue", None)       # type: ignore[attr-defined]
        if act == "delete":
            return self._api_task_delete(task_id, with_images=with_images)
        if act == "retry":
            return self._api_task_retry(task_id)
        if q is None:
            return {"ok": False, "error": "队列未就绪"}
        if act == "start":
            q.start_task(task_id)
        elif act == "pause":
            # pause 与 stop 语义不同：pause = 暂停（可续），stop = 停。
            # 队列层只有 start/stop，暂停复用 stop 但**保留任务定义**。
            q.stop_task(task_id)
        else:
            q.stop_task(task_id)
        # V0.0.23：启停也要留痕（原来日志里只有「保存任务」，用户看不出
        # 谁在什么时候把任务停了 —— 排查「怎么自己停了」时完全没线索）。
        try:
            _nm = ""
            for _r in self.db.query("SELECT name FROM tasks WHERE id=?",
                                    (int(task_id),)):
                _nm = str(_r["name"] or "")
            self.db.audit(self._actor(), f"task_{act}", str(task_id),
                          (_nm or "")[:80])
        except Exception:                                  # noqa: BLE001
            pass
        return {"ok": True, "task_id": task_id, "act": act}

    # ---------- R48/E：Web 功能完整化 ----------
    #
    # 作业单 E 明确要求「**共用同一套业务逻辑/引擎，不要另写一套**」。
    # 所以下面每个接口都走 `repo.save_task` / `ProxyPool` / `get_preferences`
    # 这些桌面端已经在用的函数，绝不在 webui 里复刻一份任务字段映射。

    #: 表单字段 → TaskConfig 属性名。**刻意不写 ai_labels_l2/l3 全量**，
    #: 只放 Web 表单真正提供的字段；缺字段就走 dataclass 默认值。
    _TASK_FORM_FIELDS = {
        "name": "name",
        "adapter_name": "adapter_name",
        "entry_urls": "entry_urls",
        "keywords": "keywords",
        "blacklist": "blacklist",
        "proxy": "proxy",
        "proxy_id": "proxy_id",
        "use_proxy": "use_proxy",
        "concurrency": "concurrency",
        "max_depth": "max_depth",
        "delay": "delay",
        "output_dir": "output_dir",
        "auto_run": "auto_run",
        "priority": "priority",
        "ai_enabled": "ai_enabled",
        "ai_labels": "ai_labels",
        "ai_blacklist": "ai_blacklist",
        "ai_threshold": "ai_threshold",
        "ai_labels_l2": "ai_labels_l2",
        "ai_labels_l3": "ai_labels_l3",
        # 2026-10-28：老的全局严格开关也要能收（Web 表单以前压根没这个控件，
        # 所以它一直只存在于桌面端）。新前端发的是逐层数组，见下面单独处理。
        "ai_label_strict": "ai_label_strict",
        # R173：任务级 UA / 站点 Cookie（web 表单同步提供）
        "user_agent": "user_agent",
        "cookie": "cookie",
        # V0.0.12：桌面已有、网页补齐的 9 个任务字段（两端对齐硬约定）。
        #  定时/允许时段是文本（格式与桌面 _parse_schedule 同一口径）；
        #  限速/页数/图片过滤是整数；匹配模式是 "any"/"all" 文本；
        #  关键词子目录是布尔。
        "schedule_at": "schedule_at",
        "allowed_hours": "allowed_hours",
        "rate_limit": "rate_limit",
        "max_pages": "max_pages",
        "min_width": "min_width",
        "min_height": "min_height",
        "min_bytes": "min_bytes",
        "max_bytes": "max_bytes",
        "keyword_subdir": "keyword_subdir",
        "ai_match_mode": "ai_match_mode",
        "ai_exclude_match_mode": "ai_exclude_match_mode",
    }
    _TASK_INT_FIELDS = ("concurrency", "max_depth", "priority", "proxy_id",
                        "rate_limit", "max_pages",
                        "min_width", "min_height", "min_bytes", "max_bytes")
    _TASK_FLOAT_FIELDS = ("delay", "ai_threshold")
    _TASK_LIST_FIELDS = ("entry_urls", "keywords", "blacklist",
                         "ai_labels", "ai_blacklist", "ai_labels_l2",
                         "ai_labels_l3")
    _TASK_BOOL_FIELDS = ("auto_run", "use_proxy", "ai_enabled",
                         "ai_label_strict", "keyword_subdir")

    @staticmethod
    def _as_words(v) -> list[str]:
        """把「a b c」或 ["a","b"] 或 "a,b" 统一成词列表（表单两种都收）。"""
        if v is None:
            return []
        if isinstance(v, str):
            parts = [p for p in v.replace(",", " ").replace("\n", " ").split()]
        elif isinstance(v, (list, tuple)):
            parts = []
            for item in v:
                parts += str(item).replace(",", " ").split()
        else:
            parts = [str(v)]
        out, seen = [], set()
        for p in parts:
            if p and p not in seen:          # 去重但**保序**
                seen.add(p)
                out.append(p)
        return out

    def _task_from_form(self, body: dict, base=None):
        """把 Web 表单 dict 转成 TaskConfig（新建/编辑共用）。

        ⚠ 编辑时必须**基于原任务**改（`base`），不能从空白开始 ——
           那样没填的字段会被清成默认值，等于「改个名字把整份配置清了」。
        """
        from .models import TaskConfig, TaskStatus

        if base is None:
            base = TaskConfig(id=None, name="", adapter_name="")
        for form_key, attr in self._TASK_FORM_FIELDS.items():
            if form_key not in body:
                continue                      # 没传的字段保持原值
            v = body[form_key]
            if form_key in self._TASK_LIST_FIELDS:
                setattr(base, attr, self._as_words(v))
            elif form_key in self._TASK_INT_FIELDS:
                try:
                    setattr(base, attr, int(v))
                except (TypeError, ValueError):
                    pass                      # 用户填了非数字 = 忽略，不崩
            elif form_key in self._TASK_FLOAT_FIELDS:
                try:
                    setattr(base, attr, float(v))
                except (TypeError, ValueError):
                    pass
            elif form_key in self._TASK_BOOL_FIELDS:
                setattr(base, attr, bool(v))
            else:
                setattr(base, attr, "" if v is None else str(v))
        # R173：任务级 UA / Cookie 统一清洗（去换行防请求头注入 + 限长）。
        #   与桌面 GUI 走**同一个函数**，不在这里另写一套：
        #   两端口径一旦漂移，同一份配置从 Web 存和从桌面存行为会不一样。
        # 2026-10-28：主题词**无限分级**（1..N 层）+ 每层独立严格开关。
        #   这两个字段是**嵌套结构**（[[词…], …] + [bool, …]），不能走上面的
        #   _as_words 扁平化，单独接；而且必须放在字段循环**之后**，
        #   否则被 ai_labels / l2 / l3 三个老字段覆盖回去（它们是前 3 层的镜像）。
        if ("ai_label_levels" in body or "ai_label_strict_levels" in body
                or "ai_label_strict" in body):
            try:
                from .label_levels import apply_levels
                raw_lv = body.get("ai_label_levels")
                if raw_lv is None:
                    # 老前端：只发了一/二/三级三格 → 当三层处理，行为不变
                    raw_lv = [self._as_words(body.get(k, getattr(base, k, [])))
                              for k in ("ai_labels", "ai_labels_l2",
                                        "ai_labels_l3")]
                lv = []
                if isinstance(raw_lv, (list, tuple)):
                    for item in raw_lv:
                        if isinstance(item, str):
                            lv.append(self._as_words(item))
                        elif isinstance(item, (list, tuple)):
                            lv.append(self._as_words(item))
                        else:
                            lv.append([])
                raw_st = body.get("ai_label_strict_levels") or []
                if isinstance(raw_st, str):
                    raw_st = [x.strip().lower() in ("1", "true", "on", "是")
                              for x in raw_st.replace(",", " ").split()]
                st_list = [bool(x) for x in raw_st]
                # 老前端只发 ai_label_strict=True（当年的含义就是「二三级都没
                # 命中就丢」）→ 展开成「第 2 层起全部严格」，语义完全等价。
                if not any(st_list) and body.get("ai_label_strict"):
                    st_list = [False] + [True] * max(0, len(lv) - 1)
                apply_levels(base, lv, st_list)
            except Exception as ex:                    # noqa: BLE001
                return None, f"主题词分级设置有误：{ex}"
        from .task_headers import resolve_task_headers
        base.user_agent, base.cookie = resolve_task_headers(
            getattr(base, "user_agent", ""), getattr(base, "cookie", ""))
        # V0.0.12：定时格式校验 —— 与桌面同一个解析函数。
        # 以前网页表单压根没这个字段，现在有了却不校验的话，填错的文本
        # 会原样存库：调度器扫表解析失败 ⇒ **静默永不触发**，用户以为
        # 设上了定时。桌面是弹警告后按「不定时」保存，这里对齐成
        # 明确拒绝（表单报错比静默降级好，网页没有弹窗层）。
        if "schedule_at" in body:
            sched = str(body.get("schedule_at") or "").strip()
            if sched:
                from .schedule import parse_schedule
                _k, _w, err = parse_schedule(sched)
                if err not in (None, "empty"):
                    return None, (f"定时格式不对：{sched}"
                                  "（应为 HH:MM 或 日期 HH:MM，例如 08:30）")
                base.schedule_at = sched
        # 允许时段同样校验（桌面 tooltip 口径：22-6 这种小时区间）。
        # 不校验 = 填 "晚上" 也存进去，运行时判据 regex 匹配不上就全天照跑，
        # 用户以为限制了时段。空串合法（= 全天）。
        if "allowed_hours" in body:
            ah = str(body.get("allowed_hours") or "").strip()
            if ah:
                import re as _re
                if not _re.match(r"^\d{1,2}-\d{1,2}$", ah) or \
                        any(int(x) > 23 for x in ah.split("-")):
                    return None, ("允许时段格式不对：" + ah +
                                  "（应为 开始-结束 的小时数，例如 22-6）")
            base.allowed_hours = ah
        # 入口地址为空 = 没有可跑的任务，直接拒绝（否则建出一个空任务）
        if not base.entry_urls:
            return None, "请填写入口网址"
        if not base.name:
            base.name = base.entry_urls[0][:40]
        return base, ""

    def _api_task_quick(self, body) -> dict:
        """编号 10：静默添加（默认配置建任务）。

        入参：{"urls": "一行一个" 或 ["…"], "name": "可选", "start": true/false}
        · 只接受 http/https；没有可用网址时**明确报错**，不建空任务；
        · 任务名默认取第一个网址的域名（用户一眼能认出是哪个站）；
        · start 省略时按 cfg.auto_run 决定是否立即入队。
        """
        try:
            from .models import TaskConfig
            from .repo import save_task
            from .watchfolder import parse_urls
        except Exception as ex:                       # noqa: BLE001
            return {"ok": False, "error": f"内部依赖缺失：{ex}"}
        raw = (body or {}).get("urls")
        if isinstance(raw, (list, tuple)):
            urls = parse_urls("\n".join(str(x) for x in raw))
        else:
            urls = parse_urls(str(raw or ""))
        if not urls:
            return {"ok": False,
                    "error": "没有可用网址（每行一个，必须是 http:// 或 https:// 开头）"}
        name = str((body or {}).get("name") or "").strip()
        if not name:
            try:
                name = urlsplit(urls[0]).netloc or "快速任务"
            except Exception:
                name = "快速任务"
        tcfg = TaskConfig(
            id=None, name=name[:80], adapter_name="", entry_urls=urls,
            max_depth=int(getattr(self.cfg, "max_depth", 3) or 3),
            concurrency=int(getattr(self.cfg, "concurrency", 4) or 4),
            delay=float(getattr(self.cfg, "delay", 0.5) or 0.5),
        )
        try:
            tid = save_task(self.db, tcfg)
        except Exception as ex:                       # noqa: BLE001
            return {"ok": False, "error": f"建任务失败：{type(ex).__name__}: {ex}"}
        want_start = (body or {}).get("start")
        if want_start is None:
            want_start = bool(getattr(self.cfg, "auto_run", False))
        started = False
        if want_start:
            # ⚠ 队列挂在 **server** 上，不是 handler 上（handler 每条请求新建）。
            # 旧写法 `self.queue` 一直是 AttributeError，又被 except 吞掉 ——
            # 「快速添加」勾了立即开始却一动不动，接口还回 started=true。
            q = getattr(self.server, "queue", None)
            if q is not None:
                try:
                    q.submit(int(tid))
                    started = True
                except Exception:
                    started = False
        return {"ok": True, "task_id": int(tid), "name": tcfg.name,
                "count": len(urls), "started": started,
                "note": ("" if started or not want_start else
                         "任务已创建，但队列未启动（Web 端未启用调度），请手工启动")}

    def _api_inbox_link(self, body) -> dict:
        """V0.0.55 批1：手机收件口。安卓壳分享来的链接 → 建任务并立即运行。

        入参：{"link": "网页链接或含链接的分享文本"}
        流程：抠链接 → 校验 → 同链接未结束任务去重 → 匹配适配器 →
        建任务（adapter_name 留空 = 运行期自动匹配，与桌面/网页同一条引擎链）
        → 入队。返回给壳一个可提示的结果。
        """
        from .inbox import clean_link, is_reachable_site, find_open_task_for
        link = clean_link((body or {}).get("link"))
        if not link:
            return {"ok": False, "error": "没有识别到网页链接"}
        good, why = is_reachable_site(link)
        if not good:
            return {"ok": False, "error": why}
        dup = find_open_task_for(self.db, link)
        if dup is not None:
            return {"ok": True, "task_id": dup, "deduped": True,
                    "name": "已有同一链接的任务在跑", "started": True}
        # 匹配一条域名对得上的规则，纯粹为了把任务名起得更认得出；
        # 匹配不到也照建（引擎运行期会走通用采集兜底）。
        rule = None
        try:
            from .crawler.rules import match_adapter
            rule = match_adapter(self.cfg.rules_dir, [link])
        except Exception:                                  # noqa: BLE001
            rule = None
        name = (getattr(rule, "name", "") if rule else "") or \
            (urlsplit(link).netloc or "收件任务")
        try:
            from .models import TaskConfig
            from .repo import save_task
            tcfg = TaskConfig(
                id=None, name=("收 " + name)[:80], adapter_name="",
                entry_urls=[link],
                max_depth=int(getattr(self.cfg, "max_depth", 3) or 3),
                concurrency=int(getattr(self.cfg, "concurrency", 4) or 4),
                delay=float(getattr(self.cfg, "delay", 0.5) or 0.5),
            )
            tid = save_task(self.db, tcfg)
        except Exception as ex:                            # noqa: BLE001
            return {"ok": False,
                    "error": f"建任务失败：{type(ex).__name__}: {ex}"}
        q = getattr(self.server, "queue", None)
        started = False
        if q is not None:
            try:
                q.submit(int(tid))
                started = True
            except Exception:                              # noqa: BLE001
                started = False
        try:
            self.db.audit(self._actor(), "inbox_link", str(tid), name[:80])
        except Exception:                                  # noqa: BLE001
            pass
        return {"ok": True, "task_id": int(tid), "name": tcfg.name,
                "adapter": (getattr(rule, "name", "") if rule else ""),
                "started": started}

    def _api_powerdown(self, body) -> dict:
        """V0.0.55 批1：定时自动关机设置入口（网页/壳共用）。

        入参：{"mode": "off"|"on_idle"|"at_time",
               "at": "HH:MM"(每日定点, 可空=只这一次用 target 字段),
               "target": epoch秒(一次性定点), "daily": true/false,
               "countdown": 秒, "idle_grace": 秒}
        校验链与真关机执行都在 powerdown 模块，这里只做参数搬运 +
        配置持久化（重启后每日计划能自动续上）。
        """
        from . import powerdown
        from .config import save_config
        mode = str((body or {}).get("mode") or "").strip()
        if mode not in ("off", "on_idle", "at_time"):
            return {"ok": False, "error": "未知的关机模式"}
        daily = bool((body or {}).get("daily"))
        at_hhmm = str((body or {}).get("at") or "").strip()
        target = (body or {}).get("target")
        try:
            target = float(target or 0)
        except (TypeError, ValueError):
            target = 0.0
        q = getattr(self.server, "queue", None)
        if mode == "off":
            r = powerdown.cancel()
        elif mode == "at_time" and daily and at_hhmm:
            r = powerdown.schedule("at_time", at_hhmm=at_hhmm, daily=True,
                                   countdown=int((body or {}).get("countdown") or 60),
                                   queue=q)
        elif mode == "at_time":
            r = powerdown.schedule("at_time", target_ts=target,
                                   countdown=int((body or {}).get("countdown") or 60),
                                   queue=q)
        else:
            r = powerdown.schedule(
                "on_idle", countdown=int((body or {}).get("countdown") or 60),
                idle_grace=int((body or {}).get("idle_grace") or 180), queue=q)
        if not r.get("ok"):
            return r
        # 持久化只记「每日定点」计划：重启后 resume_from_cfg 续跑。
        # on_idle / 一次性 at_time 不落盘 —— 那属于"今晚"的安排，机器都
        # 重启过了还复活，等于把用户下次开机也安排了关机，是事故。
        try:
            persist = (mode == "at_time" and daily and at_hhmm)
            self.cfg.auto_shutdown_mode = "at_time" if persist else "off"
            self.cfg.auto_shutdown_countdown = int(
                (body or {}).get("countdown") or 60)
            self.cfg.auto_shutdown_at = at_hhmm if persist else ""
            self.cfg.auto_shutdown_daily = 1 if persist else 0
            save_config(self.cfg)
        except Exception as e:                           # noqa: BLE001
            return {"ok": True,
                    "warning": f"计划已生效，但配置未能保存（重启后不续）：{e}",
                    "plan": powerdown.status()}
        return {"ok": True, "plan": powerdown.status()}

    # ---------------- 2026-10-28：站点订阅 / 新图监控（编号 21、22）----------
    def _subs_payload(self) -> list[dict]:
        try:
            from . import subscribe as sub
        except Exception as ex:                          # noqa: BLE001
            raise RuntimeError(f"订阅模块不可用：{ex}")
        out = []
        for s in sub.list_subs(self.db):
            s["seen"] = sub.snapshot_count(self.db, int(s["id"]))
            out.append(s)
        return out

    def _api_subs(self) -> dict:
        """订阅源列表（附带快照条数 = "认识多少条历史条目"）。"""
        try:
            return {"ok": True, "subs": self._subs_payload(),
                    "enabled": bool(getattr(self.cfg, "sub_enabled", True)),
                    "default_interval": int(
                        getattr(self.cfg, "sub_interval_min", 60) or 60)}
        except Exception as ex:                          # noqa: BLE001
            return {"ok": False, "error": f"读取订阅失败：{ex}"}

    def _api_sub_save(self, body) -> dict:
        from . import subscribe as sub
        ok, msg, sid = sub.save_sub(self.db, body or {})
        if not ok:
            return {"ok": False, "error": msg}
        try:
            self.db.audit(self._actor(), "sub_save", str(sid))
        except Exception:
            pass
        return {"ok": True, "id": sid, "note": msg}

    def _api_sub_delete(self, body) -> dict:
        from . import subscribe as sub
        try:
            sid = int((body or {}).get("id") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "订阅编号不对"}
        if not sid:
            return {"ok": False, "error": "缺少订阅编号"}
        n = sub.delete_sub(self.db, sid)
        return {"ok": True, "deleted": n,
                "note": "已删除该订阅源与它的历史快照" if n else "这条订阅源已经不在了"}

    def _api_sub_check(self, body) -> dict:
        """立即检查。

        `run=false`（默认）→ 只比对不建任务（"先让我看看能认出什么"）；
        `run=true` → 真建任务并按订阅的自动开始设置入队。
        """
        from . import subscribe as sub
        try:
            sid = int((body or {}).get("id") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "订阅编号不对"}
        s = sub.get_sub(self.db, sid)
        if not s:
            return {"ok": False, "error": "未找到该订阅源"}
        run = bool((body or {}).get("run"))
        q = getattr(self.server, "queue", None)
        r = sub.check_sub(self.db, self.cfg, s,
                          queue=(q if run else None), dry_run=not run)
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error") or "检查失败",
                    "id": sid, "name": s.get("name")}
        # 提示文案要**说清到底发生了什么**（"有新条目"和"建了任务"是两件事）。
        if r.get("task_id"):
            _note = ("已建任务并开始下载" if run else "已建任务")
        elif r.get("new"):
            _note = (f"有 {r['new']} 条新条目"
                     + ("，但没建任务（队列未就绪）" if run else "（只预览，未建任务）"))
        else:
            _note = "没有发现新条目"
        return {"ok": True, "id": sid, "name": s.get("name"),
                "found": r.get("found", 0), "new": r.get("new", 0),
                "items": r.get("items", []), "task_id": r.get("task_id", 0),
                "ran": run, "note": _note}

    def _api_sub_preview(self, body) -> dict:
        """按地址 + 链接规则试抓一页，返回它认出的条目（不写库）。"""
        from . import subscribe as sub
        url = str((body or {}).get("url") or "").strip()
        if not re.match(r"^https?://", url, re.I):
            return {"ok": False, "error": "请先填订阅地址（http:// 或 https:// 开头）"}
        r = sub.preview(self.cfg, url, str((body or {}).get("pattern") or ""),
                        limit=int((body or {}).get("limit") or 40))
        return r

    def _api_sub_items(self, u) -> dict:
        """看某条订阅的"已认识"条目（最近 100 条，用于确认快照是否正常）。"""
        from . import subscribe as sub
        q = parse_qs(u.query)
        try:
            sid = int((q.get("id") or ["0"])[0] or 0)
        except ValueError:
            sid = 0
        if not sid:
            return {"ok": False, "error": "缺少订阅编号"}
        sub.ensure_tables(self.db)
        rows = self.db.query(
            "SELECT url, first_seen, task_id FROM sub_items WHERE sub_id=? "
            "ORDER BY rowid DESC LIMIT 100", (sid,))
        return {"ok": True, "count": sub.snapshot_count(self.db, sid),
                "items": [{"url": r["url"], "first_seen": r["first_seen"],
                           "task_id": r["task_id"]} for r in rows]}

    def _api_net_ips(self) -> dict:
        """编号 32：本机可选源地址（设置页那排单选/下拉靠它）。"""
        try:
            from .nets import list_local_ips
            ips = list_local_ips()
        except Exception as ex:                          # noqa: BLE001
            return {"ok": False, "error": f"读不到本机网卡：{ex}"}
        return {"ok": True, "ips": ips,
                "current": str(getattr(self.cfg, "bind_ip", "") or "")}

    def _api_task_save(self, body):
        """E-1：新建 / 编辑任务（`task_id` 为空=新建）。"""
        from .repo import save_task, get_task

        raw_id = body.get("task_id") or body.get("id")
        base = None
        if raw_id:
            try:
                tid = int(raw_id)
            except (TypeError, ValueError):
                return {"ok": False, "error": "任务编号不对"}
            base = get_task(self.db, tid)
            if base is None:
                return {"ok": False, "error": "未找到该任务"}
        task, err = self._task_from_form(body, base)
        if task is None:
            return {"ok": False, "error": err or "任务内容不完整"}
        try:
            tid = save_task(self.db, task)
        except Exception as e:
            return {"ok": False, "error": f"保存任务失败：{e}"}
        # 记住用户用过的词（与桌面端同一个历史池，Web/桌面互通）
        self._remember_web_words(task)
        try:
            # V0.0.23：详情写清是哪条任务、改了哪些字段 —— 原来只有一串 id，
            # 用户在日志里看到「保存任务 / 12 / —」等于没信息。
            _detail = str(getattr(task, "name", "") or "")[:80]
            if raw_id:
                _detail += "（修改）"
            else:
                _detail += "（新建）"
            self.db.audit(self._actor(), "task_save", str(tid), _detail)
        except Exception:
            pass
        return {"ok": True, "task_id": tid,
                "created": not bool(raw_id)}

    def _remember_web_words(self, task) -> None:
        """把任务里的 AI 词记进**桌面端同一个** cfg 历史池。

        ⚠ 走 `cfg` 而不是另存一份 —— 用户在 Web 建的词，桌面端「曾用词」
        里也要能看到，反之亦然。
        """
        try:
            for words, key in ((task.ai_labels, "ai_label_history"),
                               (task.ai_labels_l2, "ai_label_history_l2"),
                               (task.ai_labels_l3, "ai_label_history_l3"),
                               (task.ai_blacklist, "ai_excl_history")):
                if not words:
                    continue
                hist = list(getattr(self.cfg, key, []) or [])
                for w in words:
                    if w not in hist:
                        hist.insert(0, w)
                setattr(self.cfg, key, hist[:30])
            from .config import save_config
            save_config(self.cfg)
        except Exception:
            pass          # 记历史失败不该让建任务失败

    def _task_images_of(self, task_id: int) -> list:
        """这个任务名下的图片登记（id / path），供「同时删除图片」用。"""
        try:
            return self.db.query(
                "SELECT id, path FROM images WHERE task_id=?", (int(task_id),))
        except Exception:
            return []

    def _purge_task_images(self, task_id: int) -> dict:
        """V0.0.27：删掉任务名下的图片登记，并按其处置结果处理磁盘文件。

        ⚠ 文件处置一律走 ``safe_delete.delete_image_file``（不在这里写 unlink）：
          导入进来的本地图库原图（F 盘那些）只会掉登记、**磁盘文件一个不动**，
          软件自己下载目录里的按「删除进回收站」开关处理。
        ⚠ 行删除分批（SQLite 变量上限 999）：一个任务下几万张时不至于一条 SQL
          塞爆；`reviews` 先删，避免留下指向已删图片的悬空引用。
        """
        rows = self._task_images_of(task_id)
        out = {"images": 0, "protected": 0, "failed": 0}
        if not rows:
            return out
        recycle = bool(getattr(self.cfg, "use_recycle", True))
        from .safe_delete import delete_image_file
        ids = []
        for r in rows:
            try:
                res = delete_image_file(r["path"], self.cfg, recycle=recycle)
            except Exception:                          # noqa: BLE001
                res = "failed"
            if res == "protected":
                out["protected"] += 1
            elif res == "failed":
                out["failed"] += 1
            try:
                ids.append(int(r["id"]))
            except (TypeError, ValueError):
                continue
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            ph = ",".join("?" * len(chunk))
            try:
                self.db.execute(
                    f"DELETE FROM reviews WHERE image_id IN ({ph})", tuple(chunk))
            except Exception:                          # noqa: BLE001
                pass
            try:
                self.db.execute(
                    f"DELETE FROM images WHERE id IN ({ph})", tuple(chunk))
                out["images"] += len(chunk)
            except Exception:                          # noqa: BLE001
                pass
        return out

    def _api_task_delete(self, task_id: int, with_images: bool = False):
        """E-2：删除任务（与桌面端同一句 SQL，不另写逻辑）。

        V0.0.27：``with_images=True`` 时连这个任务名下的图片一起删。
        默认仍是「只删任务定义，图片全留」—— 老调用方（脚本/书签）行为不变。
        """
        if not task_id:
            return {"ok": False, "error": "任务编号不对"}
        row = self.db.query("SELECT id FROM tasks WHERE id=?", (task_id,))
        if not row:
            return {"ok": False, "error": "未找到该任务"}
        # 先把图片张数数出来（删完再数就是 0，日志里就看不出删了多少）
        n_images = len(self._task_images_of(task_id))
        purged = {"images": 0, "protected": 0, "failed": 0}
        if with_images and n_images:
            purged = self._purge_task_images(task_id)
        # R221/C10：外键开启 ⇒ 先清子表（failed_urls / task_pages 都
        # REFERENCES tasks）再删父行；R154-F9 的页面账本清理从「删后」提前。
        for _clr in (self.db.clear_failed_urls, self.db.clear_task_pages):
            try:
                _clr(task_id)
            except Exception:
                pass
        try:
            self.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        except Exception as e:
            return {"ok": False, "error": f"删除任务失败：{e}"}
        try:
            detail = str(task_id)
            if with_images:
                detail = (f"{task_id}（同时删除图片 {purged['images']} 张"
                          f"，本地原图仅清登记 {purged['protected']} 张）")
            self.db.audit(self._actor(), "task_delete", str(task_id), detail)
        except Exception:
            pass
        return {"ok": True, "task_id": task_id,
                "images_total": n_images,
                "images_deleted": int(purged.get("images", 0)),
                "images_protected": int(purged.get("protected", 0)),
                "images_failed": int(purged.get("failed", 0))}

    def _api_task_retry(self, task_id: int):
        """E-2：重试任务 = 清错误 + 重新入队启动。"""
        from .repo import get_task

        t = get_task(self.db, task_id)
        if t is None:
            return {"ok": False, "error": "未找到该任务"}
        try:
            self.db.execute(
                "UPDATE tasks SET status='idle', last_error='' WHERE id=?",
                (task_id,))
        except Exception as e:
            return {"ok": False, "error": f"重置任务失败：{e}"}
        q = getattr(self.server, "queue", None)       # type: ignore[attr-defined]
        started = False
        if q is not None:
            try:
                q.start_task(task_id)
                started = True
            except Exception:
                started = False
        return {"ok": True, "task_id": task_id, "started": started}

    # ---------- R154-F7：失败网址清单（读 / 重试 / 清空） ----------
    #
    # 与「重试任务」的区别：那个是整任务重新入队（重新发现页面）；这里是
    # **只跑失败清单里的单条 URL**（engine.retry_failed_urls →
    # _process_image 单 URL 下载+管线），不重新爬页面。
    def _api_task_failed_urls(self, task_id: int):
        """失败清单读取。items 里带 tries/原因/时间，前端直接渲染。"""
        try:
            rows = self.db.list_failed_urls(task_id)
        except Exception as e:
            return {"ok": False, "error": f"读取失败清单失败：{e}"}
        return {"ok": True, "task_id": task_id, "items": [
            {"id": r["id"], "url": r["url"], "error": r["error"] or "",
             "tries": int(r["tries"] or 1),
             "first_at": r["first_at"] or "", "last_tried": r["last_tried"] or ""}
            for r in rows]}

    def _api_task_retry_urls(self, body):
        """按 URL 粒度重试：body = {task_id, urls:[...]}（urls 缺省/空 = 全部）。

        ⚠ 会**阻塞等待重试结果**（上限 300s）：失败清单通常几十条，
        下载完就有准数。超时的话返回 pending=True，前端稍后刷新清单即可
        （清单本身随重试进度实时变短，就是进度条）。
        """
        import concurrent.futures as _cf

        try:
            task_id = int(body.get("task_id") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "任务编号不对"}
        if task_id <= 0:
            return {"ok": False, "error": "任务编号不对"}
        raw = body.get("urls")
        urls = None
        if raw is not None:
            if not isinstance(raw, (list, tuple)):
                return {"ok": False, "error": "urls 要是网址列表"}
            urls = [str(u) for u in raw if u]
        q = getattr(self.server, "queue", None)       # type: ignore[attr-defined]
        if q is None:
            return {"ok": False, "error": "队列未就绪（纯 API 模式没有队列）"}
        n_list = len(urls) if urls is not None else \
            self.db.failed_url_count(task_id)
        if not n_list:
            return {"ok": False, "error": "失败清单是空的，没有可重试的网址"}
        try:
            fut = q.retry_failed_urls(task_id, urls)
        except Exception as e:
            return {"ok": False, "error": f"提交重试失败：{e}"}
        if fut is None:
            # 任务正在运行（两个 Engine 叠在一个任务上会互相踩）
            return {"ok": False, "error": "这个任务正在运行，先停止再重试失败网址"}
        try:
            result = fut.result(timeout=300)
        except _cf.TimeoutError:
            return {"ok": True, "task_id": task_id, "submitted": n_list,
                    "pending": True,
                    "note": "重试还在后台跑，稍后刷新失败清单看结果"}
        except Exception as e:
            return {"ok": False, "error": f"重试出错：{e}"}
        if not isinstance(result, dict):
            return {"ok": False, "error": "重试没有返回结果"}
        out = {"ok": bool(result.get("ok")), "task_id": task_id}
        out.update({k: v for k, v in result.items() if k != "ok"})
        return out

    def _api_task_failed_urls_clear(self, body):
        """清空某任务的失败清单（用户显式操作；不影响任何图片数据）。"""
        try:
            task_id = int(body.get("task_id") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "任务编号不对"}
        if task_id <= 0:
            return {"ok": False, "error": "任务编号不对"}
        try:
            n = self.db.clear_failed_urls(task_id)
        except Exception as e:
            return {"ok": False, "error": f"清空失败：{e}"}
        try:
            self.db.audit(self._actor(), "failed-urls-clear", str(task_id), f"n={n}")
        except Exception:
            pass
        return {"ok": True, "task_id": task_id, "cleared": n}

    def _api_task_batch(self, body):
        """#39：任务批量操作 —— 全部开始 / 全部停止 / 重试失败。

        **复用单任务那条链**（`_api_task_ctl` / `_api_task_retry`），
        不另写一套启停逻辑（作业单 E：共用同一套业务逻辑）。
        「重试失败」只挑 status='error' 的任务；开始 / 停止对全体生效。
        """
        act = str(body.get("act") or "")
        # 2026-10-28：网页任务表支持「勾选若干行 → 批量操作」后，这里要能吃
        # pause / delete。**delete 必须带 ids**：不带 ids 的老语义是"全体"，
        # 对启停是合理的，对删除是灾难（一次点错清空所有任务配置）。
        if act not in ("start", "stop", "pause", "retry", "delete"):
            return {"ok": False, "error": f"不支持的批量动作 {act}"}
        if act == "delete" and body.get("ids") is None:
            return {"ok": False, "error": "批量删除必须明确给出要删的任务编号"}
        if act == "retry":
            rows = self.db.query(
                "SELECT id FROM tasks WHERE status='error' "
                "AND is_template=0 ORDER BY id")
            ids = [int(r["id"]) for r in rows]
        else:
            raw = body.get("ids")
            if raw is None:
                rows = self.db.query(
                    "SELECT id FROM tasks WHERE is_template=0 ORDER BY id")
                ids = [int(r["id"]) for r in rows]
            else:
                try:
                    ids = [int(x) for x in raw]
                except (TypeError, ValueError):
                    return {"ok": False, "error": "任务 id 不对"}
        done, failed = [], []
        _with_images = bool(body.get("with_images"))
        for tid in ids:
            if act == "retry":
                r = self._api_task_retry(tid)
            elif act == "delete" and _with_images:
                # 只有「删除且勾了同时删图片」才多传一个参数；其余动作保持
                # 原来的调用形状 —— 共用同一条链是老契约，别为了新参数把它改样。
                r = self._api_task_ctl(tid, act, with_images=True)
            else:
                r = self._api_task_ctl(tid, act)
            (done if r.get("ok") else failed).append(tid)
        # V0.0.18：批量动作完成后记一条通知（失败不影响主流程）
        try:
            from . import notify
            _labels = {"start": "批量开始", "stop": "批量停止", "pause": "批量暂停",
                       "retry": "批量重试失败", "delete": "批量删除"}
            notify.add(
                self.db, "task-batch",
                f"任务{_labels.get(act, '批量操作')}完成"
                f"（成功 {len(done)} 个，失败 {len(failed)} 个）",
                link="/tasks")
        except Exception:
            pass
        return {"ok": True, "act": act, "total": len(ids),
                "done": done, "failed": failed}

    # ---- R154-F14：任务批量导出 / 导入（与桌面端共用 taskio 同一套契约） ----
    def _adapter_name_set(self) -> set:
        """本地已存在的适配器名集合（规则目录下的 yaml/yml 文件名 stem）。

        Web 导入时用它校验 ``adapter_name`` 是否存在 —— 缺适配器的任务建出来
        也是坏的，必须跳过并提示（F14 红线），不能静默建一个跑不起来的任务。
        """
        try:
            from .crawler.rules import load_adapter
        except Exception:
            load_adapter = None  # noqa: F811
        try:
            rdir = Path(self.cfg.rules_dir)
        except Exception:
            rdir = Path(".")
        names: set = set()
        for f in sorted([*rdir.glob("*.yaml"), *rdir.glob("*.yml")]):
            # 只收**能解析**的，解析不了的算"坏规则"，不算可用适配器名，
            # 否则会误把缺字段的文件当成"存在"而放行一个建不出任务的导入。
            if load_adapter is not None:
                try:
                    rule = load_adapter(f)
                    names.add(rule.name or f.stem)
                    continue
                except Exception:
                    pass
            names.add(f.stem)
        return names

    def _api_tasks_export(self, u):
        """GET /api/tasks/export?ids=1,2,3 → 下载任务 JSON（含进度统计）。

        直接复用桌面端 :func:`taskio.build_export_data` 构造数据，保证
        桌面 / Web 导出的文件**同一份格式**（Web 导出的文件能回桌面导入）。
        ``ids`` 不传 = 全部非模板任务。
        """
        from . import taskio

        ids = None
        q = parse_qs(u.query).get("ids")
        if q and q[0]:
            try:
                ids = [int(x) for x in str(q[0]).split(",") if x.strip()]
            except (TypeError, ValueError):
                ids = None
        data = taskio.build_export_data(self.db, ids=ids, with_progress=True)
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return self._send(
            200, body, "application/json; charset=utf-8",
            {"Content-Disposition":
             f'attachment; filename="tasks_export_{ts}.json"'})

    def _api_task_import(self, body):
        """POST /api/tasks/import → 从 JSON 批量建任务。

        请求体可以是任务数组，或 ``{"tasks": [...]}`` / ``{"data": [...]}``。
        校验适配器是否存在：缺失的归入 ``missing_adapter`` 并跳过（中文提示）。
        返回 ``{created, skipped_dup, missing_adapter}`` 供前端汇报。
        """
        from . import taskio

        if isinstance(body, dict):
            raw = body.get("tasks") or body.get("data") or []
        elif isinstance(body, list):
            raw = body
        else:
            return {"ok": False, "error": "请求体必须是任务 JSON 数组"}
        if not isinstance(raw, list):
            return {"ok": False, "error": "任务数据不是数组"}
        valid = self._adapter_name_set()
        try:
            res = taskio.import_tasks_data(
                self.db, raw, skip_duplicates=True, valid_adapters=valid)
        except ValueError as e:
            return {"ok": False, "error": f"导入失败：{e}"}
        return {"ok": True, **res}

    # ---- R154-F16：任务模板（桌面右键菜单 + Web 对称）----
    # R170：`_api_task_clone` 与 /api/task/clone 路由已按用户拍板移除。
    # 库层 taskio.clone_task 保留（模板功能依赖它），但不再对外暴露"克隆"。
    def _api_task_templates(self):
        """GET /api/task/templates → 模板列表（新建任务时一键套用）。"""
        from . import taskio
        return {"ok": True,
                "templates": [taskio.template_payload(t)
                              for t in taskio.list_templates(self.db)]}

    def _api_task_template_save(self, body):
        """POST /api/task/template/save {id, name?} → 把任务存为模板。"""
        from . import taskio
        try:
            tid = int((body or {}).get("id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "缺少任务 id"}
        new_id = taskio.save_as_template(
            self.db, tid, (body or {}).get("name"))
        if new_id is None:
            return {"ok": False, "error": "未找到该任务"}
        return {"ok": True, "id": new_id}

    def _api_task_template_delete(self, body):
        """POST /api/task/template/delete {id} → 删模板（真任务删不动）。"""
        from . import taskio
        try:
            tid = int((body or {}).get("id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "缺少模板 id"}
        ok = taskio.delete_template(self.db, tid)
        if not ok:
            return {"ok": False, "error": "模板不存在，或它不是模板"}
        return {"ok": True}

    def _api_task_detail(self, task_id: int):
        """E-1：编辑表单要拿到任务的全部字段。"""
        from .repo import get_task
        from .label_levels import levels_of as _lv_of, stricts_of as _st_of

        t = get_task(self.db, task_id)
        if t is None:
            return {"ok": False, "error": "未找到该任务"}
        out = {"ok": True, "task": {
            "id": t.id, "name": t.name, "adapter_name": t.adapter_name,
            "entry_urls": list(t.entry_urls or []),
            "keywords": list(t.keywords or []),
            "blacklist": list(t.blacklist or []),
            "proxy": t.proxy, "proxy_id": t.proxy_id,
            "use_proxy": bool(t.use_proxy),
            "concurrency": t.concurrency, "max_depth": t.max_depth,
            "delay": t.delay, "output_dir": t.output_dir,
            "auto_run": bool(t.auto_run), "priority": t.priority,
            "ai_enabled": bool(t.ai_enabled),
            "ai_labels": list(t.ai_labels or []),
            "ai_blacklist": list(t.ai_blacklist or []),
            "ai_labels_l2": list(t.ai_labels_l2 or []),
            "ai_labels_l3": list(t.ai_labels_l3 or []),
            "ai_threshold": t.ai_threshold,
            # 2026-10-28：主题词无限分级 + 每层严格（编辑页据此动态画 N 层输入框）。
            # 老任务这里是空/一层，前端会自动回退用上面的一/二/三级三格。
            # ⚠ 用函数内 import：模块级的名字一旦忘了导入就是 NameError 被 except
            #   吞掉（本仓踩过两次），函数内导入能保证「用不到就不出事」。
            "ai_label_levels": _lv_of(t),
            "ai_label_strict_levels": _st_of(t),
            # 老字段也要吐：它现在同时是 Web 表单字段（r48 那条「详情必须返回
            # 全部表单字段」的护栏就是靠比对这两边集合来抓漏的）。
            "ai_label_strict": bool(getattr(t, "ai_label_strict", False)),
            # R173：任务级 UA / Cookie —— 少这两个 = 编辑页一打开是空的，
            # 用户一保存就把已存的值抹掉（r48 那条「详情要返回全部表单字段」
            # 的护栏正是为此存在，本棒补映射表时就靠它抓到了）。
            "user_agent": getattr(t, "user_agent", "") or "",
            "cookie": getattr(t, "cookie", "") or "",
            # V0.0.12：桌面已有、网页补齐的任务字段。详情必须一并吐回，
            # 否则编辑页一打开是空的，用户一保存就把已存的值抹掉
            # （r48「详情要返回全部表单字段」护栏正是抓这个的）。
            "schedule_at": getattr(t, "schedule_at", "") or "",
            "allowed_hours": getattr(t, "allowed_hours", "") or "",
            "rate_limit": int(getattr(t, "rate_limit", 0) or 0),
            "max_pages": int(getattr(t, "max_pages", 0) or 0),
            "min_width": int(getattr(t, "min_width", 0) or 0),
            "min_height": int(getattr(t, "min_height", 0) or 0),
            "min_bytes": int(getattr(t, "min_bytes", 0) or 0),
            "max_bytes": int(getattr(t, "max_bytes", 0) or 0),
            "keyword_subdir": bool(getattr(t, "keyword_subdir", False)),
            "ai_match_mode": getattr(t, "ai_match_mode", "any") or "any",
            "ai_exclude_match_mode":
                getattr(t, "ai_exclude_match_mode", "any") or "any",
        }}
        # ---- R146-C5：详情模态还要「状态 / 两个张数 / 最近报错」 ----
        # ⚠ 张数口径与 /api/tasks **逐字一致**（taskio.task_progress = 累计、
        #   queue.reports = 本次），别在详情里另算一套 —— 两处数字对不上
        #   是最难解释的那类 bug（用户会以为是数据丢了）。
        try:
            from . import taskio

            prog = taskio.task_progress(self.db, task_id) or {}
        except Exception:
            prog = {}
        # ⚠ 纯 API 模式（测试/无队列）下 server 可能没有 queue，
        #   必须 getattr 兜底：没有队列 ⇒ 「本次」一律 0，累计照常出。
        q = getattr(getattr(self, "server", None), "queue", None)
        rep = (getattr(q, "reports", None) or {}).get(task_id) or {}
        row = self.db.query("SELECT status, last_error FROM tasks WHERE id=?",
                            (task_id,))
        out["task"]["status"] = (row[0]["status"] or "") if row else ""
        out["task"]["last_error"] = plain_reason(
            (row[0]["last_error"] or "") if row else "")
        out["task"]["progress"] = {
            "got": int(rep.get("downloaded", 0) or 0),
            "total": int(prog.get("downloaded", 0) or 0),
        }
        out["task"]["errors"] = [plain_reason(e)
                                 for e in (rep.get("errors") or [])][:5]
        # R154-F7：失败网址条数（详情模态的「失败网址」标签用）
        try:
            out["task"]["failed_urls"] = self.db.failed_url_count(task_id)
        except Exception:
            out["task"]["failed_urls"] = 0
        # R154-F9：已抓页面 x/y（页面账本累计口径，与 /api/tasks 一致）
        try:
            pstat = self.db.task_page_stats(task_id)
        except Exception:
            pstat = {"done": 0, "total": 0}
        out["task"]["pages_done"] = int(pstat.get("done", 0) or 0)
        out["task"]["pages_total"] = int(pstat.get("total", 0) or 0)
        return out

    # ---------- E-5 代理管理（复用 ProxyPool） ----------

    def _pool(self):
        from .proxy import ProxyPool

        return ProxyPool(self.db)

    def _api_proxy_list(self):
        try:
            pool = self._pool()
            items = []
            # ⚠ R50：**必须 only_enabled=False**。
            # `ProxyPool.list()` 默认只回启用的（那是给"选一个能用的代理"
            # 用的），而 Web 代理页要给用户看**全量**：它按 enabled 渲染
            # 「停用/启用」两个按钮。隐藏停用项的话，那排"启用"按钮
            # 永远点不到任何东西 —— 用户停用一个代理就等于**再也找不回它**
            # （实测：toggle 之后列表里就没了，IndexError）。
            # 桌面端同款用法见 main_window.py 的 list(only_enabled=False)。
            for e in pool.list(only_enabled=False):
                items.append({
                    "id": e.id, "name": e.name, "url": e.url(hide_secret=True),
                    "scheme": e.scheme, "host": e.host, "port": e.port,
                    # R245：编辑表单要回填「账号」（不回填密码）。
                    # 只给用户名不算泄密：它本来就明文显示在 hide_secret 的
                    # `user:***@` 里；密码永远不出这个接口。
                    "username": e.username or "",
                    "enabled": bool(e.enabled), "priority": e.priority,
                    "failures": e.failures, "last_error": e.last_error,
                    "latency_ms": e.latency_ms,
                })
            return {"ok": True, "items": items}
        except Exception as e:
            return {"ok": False, "error": f"读取代理失败：{e}"}

    def _api_proxy_save(self, body):
        """E-5：单条代理「确定 / 关闭」。

        ⚠ 关闭（enabled=False）只是停用，**不删条目** —— 用户下次还能打开。
        """
        from .proxy import ProxyEntry

        try:
            pool = self._pool()
            pid = body.get("id")
            entry = pool.get(int(pid)) if pid else None
            if entry is None:
                url = str(body.get("url") or "").strip()
                if not url:
                    return {"ok": False, "error": "请填写代理地址"}
                from urllib.parse import urlsplit
                u = urlsplit(url if "://" in url else f"http://{url}")
                if not u.hostname:
                    return {"ok": False, "error": "代理地址格式不对"}
                entry = ProxyEntry(
                    name=str(body.get("name") or u.hostname),
                    scheme=u.scheme or "http", host=u.hostname,
                    port=u.port or (1080 if u.scheme.startswith("socks") else 8080),
                    username=u.username or "", password=u.password or "",
                    enabled=bool(body.get("enabled", True)),
                    priority=int(body.get("priority") or 0),
                )
            else:
                if "enabled" in body:
                    entry.enabled = bool(body["enabled"])
                if body.get("name"):
                    entry.name = str(body["name"])
                if "priority" in body:
                    entry.priority = int(body["priority"] or 0)
                if body.get("url"):
                    from urllib.parse import urlsplit
                    u = urlsplit(str(body["url"]))
                    entry.scheme = u.scheme or entry.scheme
                    entry.host = u.hostname or entry.host
                    entry.port = u.port or entry.port
                    # 2026-10-28：原来改地址时**不动账号密码**，于是把
                    # `socks5://user:pass@host:1080` 改成别的写法后，旧密码
                    # 会悄悄留在条目里继续用 —— 表现为"地址是对的但一直 407"。
                    if u.username is not None:
                        entry.username = u.username
                    if u.password is not None:
                        entry.password = u.password
            new_id = pool.save(entry)
            return {"ok": True, "id": new_id, "enabled": entry.enabled}
        except Exception as e:
            return {"ok": False, "error": f"保存代理失败：{e}"}

    def _api_proxy_delete(self, body):
        try:
            pool = self._pool()
            pid = int(body.get("id") or 0)
            if not pid:
                return {"ok": False, "error": "代理编号不对"}
            pool.remove(pid)
            return {"ok": True, "id": pid}
        except Exception as e:
            return {"ok": False, "error": f"删除代理失败：{e}"}

    def _api_proxy_check(self, body):
        """测代理延迟（桌面端「代理健康检查」的网页版）。

        为什么必须限制数量：`ProxyPool.check()` 是**同步阻塞**的 httpx 请求，
        一个不通的代理要等满 timeout 才返回。全都测的话，用户会遇到
        「点一下转半分钟」，还会以为网页卡死。所以：单个不限、批量最多 8 个，
        并且把实际用掉的条数写进 note 告诉用户。
        """
        from .proxy import ProxyPool
        try:
            pool = ProxyPool(self.db)
        except Exception as e:                        # noqa: BLE001
            return {"ok": False, "error": f"代理池不可用：{e}"}
        try:
            timeout = float(body.get("timeout") or 5.0)
        except (TypeError, ValueError):
            timeout = 5.0
        timeout = max(1.0, min(timeout, 10.0))
        pid = body.get("id")
        if pid:
            try:
                e = pool.get(int(pid))
            except (TypeError, ValueError):
                return {"ok": False, "error": "代理编号不对"}
            if e is None:
                return {"ok": False, "error": f"代理 #{pid} 不存在"}
            targets = [e]
        else:
            targets = pool.list(only_enabled=False)[:8]
        out = []
        for e in targets:
            ok, lat, err = pool.check(e, timeout=timeout)
            out.append({"id": e.id, "name": e.name, "ok": bool(ok),
                        "latency_ms": int(lat), "error": err})
        note = ""
        if not pid and len(pool.list(only_enabled=False)) > 8:
            note = "只测了前 8 个（代理太多，一次全测要等很久）"
        return {"ok": True, "items": out, "note": note}

    def _api_find_terms(self):
        """V0.0.6：给网页建任务表单用的全局词表（学到的词 + 全局别找的词）。

        和桌面端 ``find_terms.global_terms`` 同一个函数，不另写一套口径。
        """
        try:
            from . import find_terms as _ft
            t = _ft.global_terms(self.db, self.cfg)
            return {"ok": True, "want": list(t.want),
                    "avoid": list(t.avoid), "learned": list(t.learned),
                    "soft_avoid": list(getattr(t, "soft_avoid", [])),
                    "avoid_feedback": list(getattr(t, "avoid_feedback", []))}
        except Exception as e:
            return {"ok": False, "error": str(e), "want": [], "avoid": [],
                    "learned": [], "soft_avoid": [], "avoid_feedback": []}

    # ---- V0.0.7：「这张图下错了」教出来的词（看板可见、可删、可清空） ----
    def _api_feedback(self):
        try:
            from . import feedback as fb
            return {"ok": True, "items": fb.list_all(self.db),
                    "hard_hits": fb.HARD_HITS}
        except Exception as e:
            return {"ok": False, "error": str(e), "items": [], "hard_hits": 2}

    def _api_feedback_remove(self, body):
        try:
            from . import feedback as fb
            word = str((body or {}).get("word") or "").strip()
            if not word:
                return {"ok": False, "error": "没给词"}
            return {"ok": fb.remove(self.db, word)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _api_feedback_clear(self, body):
        try:
            from . import feedback as fb
            return {"ok": True, "removed": fb.clear(self.db)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _api_learn_add_term(self, body):
        """手动加一个「要找」的词 —— 与桌面看板 ``learning_board._add_term``
        **逐条同口径**：权重 2.0、方向 pos、来源 = 用户亲手输入。

        ⚠ 为什么权重取 2.0 而不是 1.0：``find_terms.MIN_POS_WEIGHT`` 是 0.5，
        而导入图库学到的最高频词也是 2.0；填小了用户手写的词会被一堆
        模型自动词压过去，等于白填。已 ≥2.0 就不重复累加（防越点越大）。
        """
        word = str((body or {}).get("word") or "").strip()
        if not word:
            return {"ok": False, "error": "请先输入一个词"}
        if len(word) > 40:
            return {"ok": False, "error": "词太长了，最多 40 个字符"}
        try:
            from .repo import (PREF_ORIGIN_USER, get_preferences,
                               set_preference)
            prefs = get_preferences(self.db)
            added = float(prefs.get(word.lower(), 0.0) or 0.0) < 2.0
            if added:
                set_preference(self.db, 0, word, 2.0, kind="pos",
                               origin=PREF_ORIGIN_USER)
            t = self._api_find_terms()
            return {"ok": True, "added": added, **t}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _api_learning(self):
        """#44 学习看板：读**桌面端同一份** preference，并把看板口径也搬过来。

        ⚠ 为什么统计不用自己写 SQL：桌面端看板（``gui/learning_board.py``）
        读的是 ``classify.learning_stats.collect``。网页端若另写一套口径，
        用户就会看到「桌面说 12 条、手机说 9 条」——同一个事实两个数字，
        比没有数字更糟。所以这里**直接调同一个函数**。

        ``items`` / ``stats`` 两个键是 R48 起既有前端在用的，形状保持不变；
        新增 ``samples``（全部样本，带命中次数）与 ``summary``（一行中文摘要）。
        """
        try:
            from .classify import learning_stats as LS
            from .i18n import label_text
            from .learn_switch import read_enabled
            from .repo import get_preferences

            prefs = get_preferences(self.db)
            items = [{"name": k, "value": round(float(v), 4)}
                     for k, v in sorted(prefs.items(),
                                        key=lambda kv: -abs(kv[1]))]
            st = LS.collect(self.db)
            # 兼容旧前端：这两个键 R48 起就在用，不能改名
            st["count"] = st["labels_total"]
            st["reviews"] = st["reviews_total"]

            # 全部样本（Top-N 之外的也要看得到，否则「管理」无从下手）
            samples = []
            for r in self.db.query(
                    "SELECT label, weight, seen, hit_pos, hit_neg, source "
                    "FROM preferences WHERE task_id=0"):
                w = float(r["weight"] or 0.0)
                # R174：来源取不到列时按 model_auto（保守端，见 repo._pref_origin）
                try:
                    src = str(r["source"] or "").strip() or "model_auto"
                except Exception:
                    src = "model_auto"
                if src not in ("user_typed", "model_auto"):
                    src = "model_auto"
                samples.append({
                    "label": str(r["label"]),
                    "text": label_text(str(r["label"])),
                    "weight": round(w, 4),
                    "seen": int(r["seen"] or 0),
                    "hit_pos": int(r["hit_pos"] or 0),
                    "hit_neg": int(r["hit_neg"] or 0),
                    "sign": "pos" if w > 0 else ("neg" if w < 0 else "neu"),
                    "source": src,
                })
            samples.sort(key=lambda s: -abs(s["weight"]))
            st["origins"] = LS.preference_origins(self.db)
            return {"ok": True, "items": items, "stats": st,
                    "samples": samples, "summary": LS.summary_line(st),
                    "origin_line": LS.origin_line(st["origins"]),
                    # R166：样本来源一行（订单第 4 条）。与桌面看板**同一个
                    # 函数**，两端文案不会漂。前端若还没接这个字段，
                    # 只是不显示，不影响旧渲染。
                    "sample_line": LS.sample_line(st),
                    # R230：顺带把开关状态带出去，前端**一次请求**就能画勾选框，
                    # 不必再单独打一次（弱网下少一次「转圈」）。
                    "enabled": read_enabled(self.cfg),
                    "next_cursor": None, "has_more": False}
        except Exception as e:
            return {"ok": False, "error": f"读取学习数据失败：{e}"}

    # ---------- R230 · 学习开关（读 / 写） ----------

    def _api_learn_enabled(self):
        """R230：读「学习」开关（桌面端同一个 cfg 字段 + 同一份默认值）。"""
        try:
            from .learn_switch import read_enabled
            return {"ok": True, "enabled": read_enabled(self.cfg),
                    "next_cursor": None, "has_more": False}
        except Exception as e:
            return {"ok": False, "error": f"读取学习开关失败：{e}"}

    def _api_learn_enabled_set(self, body):
        """R230：写「学习」开关，**立即**同步全局 gate 并落盘。

        ⚠ 必须落盘：只改内存的话重启就回到旧值，用户会以为「我关了它又
        自己开了」。落盘走 config.save_config —— 与 /api/url/save 同一条链路。
        """
        try:
            from .config import save_config
            from .learn_switch import apply_enabled
            if not isinstance(body, dict) or "enabled" not in body:
                return {"ok": False,
                        "error": "缺少参数：enabled（true 开 / false 关）"}
            v = apply_enabled(self.cfg, body.get("enabled"),
                              save=lambda: save_config(self.cfg))
            return {"ok": True, "enabled": v}
        except Exception as e:
            return {"ok": False, "error": f"保存学习开关失败：{e}"}

    # ---------- R232 · 数据备份 / 偏好导入导出（桌面「数据」菜单的 Web 对称） ----------

    def _api_backup_list(self):
        """GET /api/backup → 备份清单（新的在前）+ 「每日自动备份」开关状态。"""
        try:
            from .config import user_data_dir
            from .data_backup import (KEEP_DEFAULT, KEEP_FIELD, backup_dir,
                                      daily_enabled, human_size, is_backup_name)
            d = backup_dir(user_data_dir())
            items = []
            if d.is_dir():
                for f in sorted(d.iterdir(), key=lambda p: p.name, reverse=True):
                    if not (f.is_file() and is_backup_name(f.name)):
                        continue
                    st = f.stat()
                    items.append({
                        "name": f.name,
                        "size": st.st_size,
                        "size_text": human_size(st.st_size),
                        "mtime": datetime.datetime.fromtimestamp(
                            st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    })
            return {"ok": True, "dir": str(d), "count": len(items),
                    "items": items, "daily": daily_enabled(self.cfg),
                    "keep": int(getattr(self.cfg, KEEP_FIELD, KEEP_DEFAULT)
                                or KEEP_DEFAULT),
                    "next_cursor": None, "has_more": False}
        except Exception as e:
            return {"ok": False, "error": f"读取备份失败：{e}"}

    def _api_backup_now(self):
        """POST /api/backup → 立刻备份一次数据库（**走 sqlite 在线快照**）。

        ⚠ **不许**直接 copy .db 文件：WAL 模式下还没并回主库的事务全在
        -wal 里，copy 出去的是旧数据，等真要恢复才发现最近几小时白采了
        （R152-A6 已经踩过一次，桌面端导出完整备份同一条理由）。
        """
        try:
            from .config import user_data_dir
            from .data_backup import (KEEP_DEFAULT, KEEP_FIELD, backup_dir,
                                      backup_name, human_size, is_backup_name,
                                      prune)
            d = backup_dir(user_data_dir())
            d.mkdir(parents=True, exist_ok=True)
            dest = d / backup_name()
            self.db.backup(dest)
            keep = int(getattr(self.cfg, KEEP_FIELD, KEEP_DEFAULT)
                       or KEEP_DEFAULT)
            names = [f.name for f in d.iterdir()
                     if f.is_file() and is_backup_name(f.name)]
            removed = 0
            for name in prune(names, keep):
                try:
                    (d / name).unlink()
                    removed += 1
                except Exception:
                    pass
            return {"ok": True, "name": dest.name, "size": dest.stat().st_size,
                    "size_text": human_size(dest.stat().st_size),
                    "removed": removed}
        except Exception as e:
            # V0.0.34：备份失败原先只把错误返回给当时点按钮的那个人，
            # 关掉页面就再也看不到 —— 落一条错误级通知（缺口之三）。
            try:
                from . import notify
                notify.add(self.db, "backup-failed", "数据库备份失败",
                           str(e)[:200], link="/maintenance", level="error")
            except Exception:
                pass
            return {"ok": False, "error": f"备份失败：{e}"}

    def _api_backup_daily(self, body):
        """POST /api/backup/daily → 开 / 关「每日自动备份」（落盘，与桌面同键）。"""
        try:
            from .config import save_config
            from .data_backup import apply_daily
            if not isinstance(body, dict) or "enabled" not in body:
                return {"ok": False,
                        "error": "缺少参数：enabled（true 开 / false 关）"}
            v = apply_daily(self.cfg, body.get("enabled"),
                            save=lambda: save_config(self.cfg))
            return {"ok": True, "daily": v, "enabled": v}
        except Exception as e:
            return {"ok": False, "error": f"保存备份开关失败：{e}"}

    # ---------- R241 · 整库备份 / 恢复（桌面「数据」菜单的 Web 对称） ----------
    #
    # 与 R232 的分工：R232 是「每日一份的库快照」（自动、轻、误删回滚用），
    # 这里是「换电脑 / 重装的一次性整包」（配置 + 库 + 规则 + 缩略图，重）。
    # 两者共用 ``backups`` 目录，前缀不同（``mrm_db_backup_`` / ``mrm_full_``）。
    #
    # ⚠ 三个共同点都在这里守着，别在下游各写一遍：
    #   1. 只认**目录内**的文件名（``is_full_name`` + 路径规范化），
    #      绝不受理请求里带的任意路径 —— 否则等于给浏览器一个任意文件读/删口；
    #   2. 重活（压缩几百 MB / 恢复）一律后台作业 + 轮询，不占请求线程；
    #   3. 一切失败给中文人话（用户看到的不是 traceback）。
    def _prune_full_backups(self) -> dict:
        """按配置轮换旧整库备份。**只在备份成功之后调用**。

        为什么不在设置页改数值时立刻清：用户把「保留 5 份」改成「3 份」
        是为了**以后**少留，不是要你当场删掉两份历史 —— 万一他正打算用
        其中一份恢复呢。真正删旧备份的时机只有两个：新备份刚生成完、
        或用户手动选择「清理旧备份」。
        """
        try:
            from . import fullbackup as fb
            gb = float(getattr(self.cfg, "full_backup_max_gb", 0) or 0)
            return fb.prune_and_delete(
                self._full_dir(),
                keep=int(getattr(self.cfg, "full_backup_keep", 5) or 0),
                days=int(getattr(self.cfg, "full_backup_days", 30) or 0),
                max_bytes=int(gb * 1024 ** 3) if gb > 0 else 0)
        except Exception as e:      # noqa: BLE001 —— 清理失败不该让备份失败
            return {"deleted": [], "bytes": 0, "error": str(e)}

    def _full_dir(self) -> Path:
        from .data_backup import backup_dir
        from .config import user_data_dir
        d = backup_dir(user_data_dir())
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _full_path_of(self, name: str) -> Optional[Path]:
        """把请求里的名字解析成备份目录内的路径；不合法返回 None。

        三道闸：必须 ``is_full_name``（前缀+时间戳形状）→ 不允许分隔符
        → 解析后父目录必须就是备份目录（防 ``../`` 绕出去）。
        """
        try:
            from .fullbackup import is_full_name
        except Exception:
            return None
        n = str(name or "").strip()
        if not n or not is_full_name(n):
            return None
        if "/" in n or "\\" in n or ":" in n:
            return None
        p = (self._full_dir() / n)
        try:
            if p.resolve().parent != self._full_dir().resolve():
                return None
        except OSError:
            return None
        return p

    # ───────── R248 · 从文件夹批量导入（网页端） ─────────
    def _api_fs_list(self, u):
        """GET /api/fs/list?path= → 列子目录（只读）。

        为什么需要它：浏览器出于安全拿不到真实文件路径，网页端「选一个文件夹」
        只能由服务端列、用户点选。桌面端是原生对话框，网页端没这条路。

        2026-10-29 改口径（用户新需求）：**远程访问也开放**。
        原来这里是「远程一律拒绝」，理由是"列出服务器的文件系统帮不上忙"。
        Docker/NAS 部署之后这个理由反了 —— 用户在另一台电脑上打开网页，
        要选的恰恰是服务器（NAS）上的目录，拒绝等于把「从文件夹学习」这个
        功能整个锁死。现在：远程可读，但
          · 只列**目录**（不列文件名，也不返回文件大小/内容）；
          · 系统目录（/proc、/sys、/etc…）直接挡掉；
          · 响应里带 ``where``（机器名）与 ``remote``，前端必须把"这是哪台
            机器"显示出来，否则用户会在自己的 D 盘里找 NAS 的目录。
        安全边界：本接口在 ``_auth_ok()`` 之后，未登录拿不到。
        """
        try:
            host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        except Exception:
            host = ""
        remote = bool(host) and host not in ("127.0.0.1", "localhost", "::1")
        try:
            import socket
            where = socket.gethostname()
        except Exception:
            where = ""

        raw = str((parse_qs(u.query).get("path") or [""])[0]).strip()
        try:
            if not raw:
                # 空路径 = 列"入口位置"：Windows 给盘符，Linux 给常见挂载点。
                # 容器里没有盘符，直接给 /data、/mnt 这些才点得动。
                if os.name == "nt":
                    import string
                    items = []
                    for ch in string.ascii_uppercase:
                        d = Path(f"{ch}:\\")
                        try:
                            if d.exists():
                                items.append({"name": f"{ch}:", "path": str(d)})
                        except OSError:
                            continue
                    return {"ok": True, "path": "", "parent": "", "items": items,
                            "where": where, "remote": remote}
                items = []
                for cand in ("/data", "/library", "/mnt", "/media", "/volume1",
                             "/vol1", "/vol2", "/home", "/"):
                    try:
                        if Path(cand).is_dir():
                            items.append({"name": cand, "path": cand})
                    except OSError:
                        continue
                return {"ok": True, "path": "", "parent": "", "items": items,
                        "where": where, "remote": remote, "roots": True}
            p = Path(raw).expanduser().resolve()
            sp = str(p).replace("\\", "/")
            blocked = ("/proc", "/sys", "/dev", "/etc", "/root", "/boot",
                       "/var/lib/docker", "/run")
            if os.name == "nt":
                blocked = ()        # Windows 盘符结构不同，不套 Linux 前缀
            for bad in blocked:
                if sp == bad or sp.startswith(bad + "/"):
                    return {"ok": False, "error": "系统目录不支持浏览，请选择数据目录或挂载卷"}
            if not p.is_dir():
                return {"ok": False, "error": f"目录不存在：{p}"}
            items = []
            # 上限 500：真到那种「一个目录下几万个文件夹」的怪结构，
            # 全列出来也点不动，截断并说明比卡住强。
            truncated = False
            with os.scandir(p) as it:
                for e in it:
                    if len(items) >= 500:
                        truncated = True
                        break
                    try:
                        if not e.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if e.name.startswith("."):
                        continue
                    items.append({"name": e.name, "path": e.path})
            items.sort(key=lambda x: x["name"].lower())
            parent = str(p.parent) if p.parent != p else ""
            return {"ok": True, "path": str(p), "parent": parent,
                    "items": items, "truncated": truncated,
                    "where": where, "remote": remote}
        except PermissionError:
            return {"ok": False, "error": "没有权限读取这个目录"}
        except Exception as e:
            return {"ok": False, "error": f"列目录失败：{e}"}

    def _api_import_folder(self, body):
        """POST /api/import/folder {folder, force, tag} → 起后台导入作业。

        两阶段（先索引后可浏览、再后台打标）见 :mod:`import_folder`。
        这里只负责「起线程 + 把进度写进作业表」，进度由前端轮询。
        """
        from . import import_folder as imp
        if not isinstance(body, dict):
            return {"ok": False, "error": "请求格式不对"}
        folder = str(body.get("folder") or "").strip()
        if not folder:
            return {"ok": False, "error": "请先选择一个文件夹"}
        try:
            if not Path(folder).expanduser().is_dir():
                return {"ok": False, "error": f"目录不存在：{folder}"}
        except OSError as e:
            return {"ok": False, "error": f"目录打不开：{e}"}
        if imp.is_running():
            return {"ok": False, "error": "已有一个导入任务在跑，等它结束再开始"}
        force = _as_bool(body.get("force"), False)
        # 默认打标；显式传 tag:false 才只索引（用户想先把几万张翻一遍）
        do_tag = _as_bool(body.get("tag"), True)
        # V0.0.5：「复制到软件图库」开关（网页与桌面同一语义，默认关）
        # V0.0.6：字符串 "false" 不许被当成开（裸 HTTP 接口谁都能调）。
        copy_to_library = _as_bool(body.get("copy"), False)
        # V0.0.35：「是否导入子文件夹」，默认开 = 保持老行为（全递归）。
        # 显式传 false（含字符串 "false"）才只看所选这一层。
        recursive = _as_bool(body.get("recursive"), True)
        jid = _job_new("import")
        evt = threading.Event()
        with _IMPORT_LOCK:
            _IMPORT_STATE["job"] = jid
            _IMPORT_STATE["cancel"] = evt
        cfg, db = self.cfg, self.db

        def _cb(phase, done, total, name, eta, pct):
            label = "正在索引" if phase == "index" else "正在打标"
            msg = f"{label} {done:,} / {total:,}"
            if phase == "index" and name:
                msg += f" · {name[:36]}"
            if eta and total and done < total:
                msg += f" · 约剩 {eta}"
            # V0.0.45：界面自己拼「阶段 + 张数 + 剩余时间 + 当前文件」，
            # 这一串 message 保留给还在读它的老调用方（手机端/脚本）。
            _jobs_mod.update(jid, pct=pct, message=msg, phase=phase,
                             done=done, total=total, eta=eta,
                             name=str(name or "")[:120])

        def _run():
            # V0.0.34：导入动辄几万张、跑几十分钟，人不可能一直盯着进度条。
            # 完成 / 停止 / 失败都落一条通知，回来点铃铛就能看到结果。
            def _notice(title, body, level="info"):
                try:
                    from . import notify
                    notify.add(db, "import-folder", title, str(body)[:200],
                               link="/export?tab=imp", level=level)
                except Exception:
                    pass

            try:
                res = imp.import_folder(db, cfg, folder, force=force,
                                        do_tag=do_tag, stop=evt.is_set,
                                        copy_to_library=copy_to_library,
                                        recursive=recursive,
                                        progress=_cb)
                if not res.get("ok"):
                    _notice("导入失败", res.get("error") or "导入失败", "error")
                    _job_finish(jid, error=str(res.get("error") or "导入失败"))
                    return
                _counts = (f"入库 {int(res.get('indexed') or 0):,} 张"
                           f" · 打标 {int(res.get('tagged') or 0):,} 张"
                           f" · 跳过重复 {int(res.get('skipped_dup') or 0):,} 张")
                if res.get("stopped"):
                    _notice("导入已停止", _counts + "；可再次导入接着打")
                    _job_finish(jid, result=res,
                                message="已停止（可再次导入接着打）")
                else:
                    _notice("导入完成", _counts)
                    _job_finish(jid, result=res, message="导入完成")
            except Exception as e:      # noqa: BLE001 —— 作业线程必须兜住
                _notice("导入失败", f"{type(e).__name__}: {e}", "error")
                _job_finish(jid, error=f"{type(e).__name__}: {e}")
            finally:
                with _IMPORT_LOCK:
                    if _IMPORT_STATE.get("job") == jid:
                        _IMPORT_STATE["job"] = ""
                        _IMPORT_STATE["cancel"] = None

        threading.Thread(target=_run, daemon=True, name="mrm-import").start()
        return {"ok": True, "job": jid}

    def _api_import_job(self, u):
        """GET /api/import/job?id= → 进度快照（前端每 1–2 秒轮询）。"""
        jid = str((parse_qs(u.query).get("id") or [""])[0]).strip()
        rec = _job_get(jid) if jid else None
        if rec is None:
            return {"ok": False, "error":
                    "作业不存在或已过期（软件重启后旧作业会失效，请重新开始）"}
        from . import import_folder as imp
        out = dict(rec)
        out["ok"] = True
        out["running"] = imp.is_running()
        return out

    def _api_import_cancel(self):
        """POST /api/import/cancel → 请求停止（优雅停，不是杀进程）。"""
        with _IMPORT_LOCK:
            evt = _IMPORT_STATE.get("cancel")
        if evt is None:
            return {"ok": False, "error": "当前没有正在跑的导入"}
        evt.set()
        return {"ok": True, "message":
                "已请求停止：已入库的图片保留，下次导入同一文件夹会从剩下的继续"}

    def _api_import_last(self):
        """GET /api/import/last → 上次导入到哪儿了（断点续导入口）。

        真正的「断点」不在断点文件里，而在库里的 `images.ai_state`：
        索引阶段每张图落一行（0=待打标 / 1=已打标）。中途点停止、软件崩了、
        关机了都不影响 —— 下一次对同一目录发起导入时，已登记的直接跳过、
        没打标的接着打。这个接口只是把这件事算出来给界面看，避免用户
        「不知道上次停在哪儿、只能从头再点一遍」。
        """
        from . import import_folder as imp
        try:
            rows = self.db.query(
                """SELECT t.id AS tid, t.name AS name, t.output_dir AS folder,
                          COUNT(i.id) AS total,
                          SUM(CASE WHEN i.ai_state = 0 THEN 1 ELSE 0 END) AS pending
                     FROM tasks t JOIN images i ON i.task_id = t.id
                    WHERE i.source_url LIKE 'file://%'
                    GROUP BY t.id
                    ORDER BY t.id DESC
                    LIMIT 20""")
        except Exception as e:                          # noqa: BLE001
            return {"ok": False, "error": f"读取导入记录失败：{e}"}
        items = []
        for r in rows:
            folder = str(r["folder"] or "")
            try:
                exists = bool(folder) and Path(folder).is_dir()
            except OSError:
                exists = False
            name = str(r["name"] or "")
            is_webdav = bool(getattr(self.cfg, "library_dir", "")) and                 folder.startswith(str(getattr(self.cfg, "library_dir", "")))
            items.append({
                "task_id": int(r["tid"]),
                # WebDAV 导入的「任务名」其实是图库里那层目录的标识（webdav_xxx），
                # 直接显示出来等于给用户看一串哈希，所以换成能读的名字。
                "name": ("WebDAV 导入" if is_webdav else name),
                "raw_name": name,
                "folder": folder,
                "total": int(r["total"] or 0),
                "pending": int(r["pending"] or 0),
                "exists": exists,
                # WebDAV 的图片落在软件图库的 webdav/ 下，续导时走同一条本地链
                "webdav": str(getattr(self.cfg, "library_dir", "")) and
                          folder.startswith(str(getattr(self.cfg, "library_dir", ""))),
            })
        return {"ok": True, "items": items, "running": imp.is_running()}

    # ---- V0.0.46 · 语义搜索（按画面内容找图） ----
    def _api_semantic_status(self):
        """GET /api/semantic/status → 有多少图已存特征向量、还差多少。"""
        from . import semantic
        try:
            clf = semantic.make_classifier(self.cfg)
        except Exception as e:                          # noqa: BLE001
            return {"ok": False, "error": f"模型加载失败：{e}"}
        if getattr(clf, "backend", "") != "clip":
            return {"ok": True, "available": False,
                    "reason": "本地 AI 模型不可用，按意思找需要 CLIP 模型"}
        mid = clf.model_id
        have = int(self.db.query(
            "SELECT COUNT(*) c FROM images WHERE clip_vec IS NOT NULL "
            "AND clip_model=?", (mid,))[0]["c"] or 0)
        pend = semantic.pending_count(self.db, mid)
        return {"ok": True, "available": True, "model": mid,
                "have": have, "pending": pend}

    def _api_semantic_search(self, body):
        """POST /api/semantic/search {q, task, limit} → 相似度排序的图。

        semantic.search 只回 (id, 相似度)；这里按 id 取行、复用 _img_item
        拼成前端现成的卡片字段（灯箱大图 / flag / 标签），再把相似度
        折成 0~100 的 sem_score 附上。
        """
        from . import semantic
        if not isinstance(body, dict):
            return {"ok": False, "error": "请求格式不对"}
        q = str(body.get("q") or "").strip()
        try:
            task_id = int(body.get("task") or 0)
        except (TypeError, ValueError):
            task_id = 0
        try:
            limit = int(body.get("limit") or 200)
        except (TypeError, ValueError):
            limit = 200
        r = semantic.search(self.db, self.cfg, q,
                            task_id=task_id, limit=limit)
        if not r.get("ok"):
            return r
        hits = r.get("hits") or []
        if not hits:
            return {"ok": True, "items": [], "total": r.get("total", 0),
                    "note": r.get("note", ""), "empty": True,
                    "message": "没有找到足够像的图。换个说法"
                               "（如「比基尼」而不是整套描述）请尝试。"}
        got = ",".join(str(i) for i, _ in hits)
        rows = self.db.query(
            f"SELECT * FROM images WHERE id IN ({got})")
        by_id = {int(x["id"]): x for x in rows}
        items = []
        for iid, sim in hits:
            x = by_id.get(iid)
            if x is None:
                continue
            d = self._img_item(x)
            d["sem_score"] = round(max(0.0, min(1.0, sim)) * 100)
            items.append(d)
        return {"ok": True, "items": items, "total": r.get("total", 0),
                "note": r.get("note", ""), "model": r.get("model", "")}

    def _api_semantic_backfill(self, body):
        """POST /api/semantic/backfill → 起后台补算作业（可停）。"""
        from . import semantic
        jid = _job_new("semantic-backfill")
        with _IMPORT_LOCK:
            if _IMPORT_STATE.get("job"):
                _job_finish(jid, error="有导入任务在跑，先等它结束")
                return {"ok": False, "error": "有导入任务在跑，先等它结束"}
            if _SEM_BACKFILL.get("job"):
                _job_finish(jid, error="补算已在进行中")
                return {"ok": False, "error": "补算已在进行中",
                        "job": _SEM_BACKFILL["job"]}
            _SEM_BACKFILL["job"] = jid
        evt = threading.Event()
        cfg, db = self.cfg, self.db

        def _stop():
            return evt.is_set()

        def _run():
            try:
                # backfill 自己把 done/stopped 终态写进作业表；这里只兜异常
                semantic.backfill(db, cfg, _jobs_mod, jid, stop_flag=_stop)
            except Exception as e:                      # noqa: BLE001
                _job_finish(jid, error=f"{type(e).__name__}: {e}")
            finally:
                with _IMPORT_LOCK:
                    if _SEM_BACKFILL.get("job") == jid:
                        _SEM_BACKFILL["job"] = ""
                        _SEM_BACKFILL["cancel"] = None

        _SEM_BACKFILL["cancel"] = evt
        threading.Thread(target=_run, daemon=True,
                         name="mrm-sem-backfill").start()
        return {"ok": True, "job": jid}

    def _api_semantic_backfill_cancel(self):
        """POST /api/semantic/backfill/cancel → 请求停止补算（优雅停）。"""
        with _IMPORT_LOCK:
            evt = _SEM_BACKFILL.get("cancel")
        if evt is None:
            return {"ok": False, "error": "当前没有正在跑的补算"}
        evt.set()
        return {"ok": True, "message": "已请求停止：已算好的画面特征保留"}

    def _api_import_webdav(self, body):
        """POST /api/import/webdav {url,user,password,tag,copy,recursive} → 起作业。

        枚举（PROPFIND）→ 下载到 `library/webdav/<标识>/` → 交给本地导入那条链。
        枚举与下载都可能几分钟，所以和文件夹导入一样是**后台作业 + 轮询**。
        账号密码只用于本次请求的 Basic 头，不落盘。
        """
        from . import import_folder as imp
        from . import webdav_import as wd
        if not isinstance(body, dict):
            return {"ok": False, "error": "请求格式不对"}
        url = str(body.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "请先填写 WebDAV 地址"}
        if not url.lower().startswith(("http://", "https://")):
            return {"ok": False, "error":
                    "地址要以 http:// 或 https:// 开头（NAS 的 WebDAV 入口）"}
        user = str(body.get("user") or "")
        password = str(body.get("password") or "")
        do_tag = _as_bool(body.get("tag"), True)
        copy_to_library = _as_bool(body.get("copy"), False)
        recursive = _as_bool(body.get("recursive"), True)
        if imp.is_running():
            return {"ok": False, "error": "已有一个导入任务在跑，等它结束再开始"}
        jid = _job_new("import")
        evt = threading.Event()
        with _IMPORT_LOCK:
            _IMPORT_STATE["job"] = jid
            _IMPORT_STATE["cancel"] = evt
        cfg, db = self.cfg, self.db

        def _cb(phase, done, total, name, eta, pct):
            label = {"list": "正在列目录", "download": "正在下载",
                     "index": "正在索引", "tag": "正在打标"}.get(phase, "正在处理")
            msg = f"{label} {done:,} / {total:,}"
            if name and phase != "index":
                msg += f" · {name[:36]}"
            elif phase == "index" and name:
                msg += f" · {name[:36]}"
            if eta and total and done < total:
                msg += f" · 约剩 {eta}"
            _jobs_mod.update(jid, pct=max(0, min(100, int(pct))), message=msg,
                             phase=phase, done=done, total=total, eta=eta,
                             name=str(name or "")[:120])

        def _run():
            def _notice(title, text, level="info"):
                try:
                    from . import notify
                    notify.add(db, "import-webdav", title, str(text)[:200],
                               link="/export?tab=imp", level=level)
                except Exception:
                    pass

            try:
                res = wd.import_webdav(
                    db, cfg, url, user=user, password=password,
                    recursive=recursive, do_tag=do_tag,
                    copy_to_library=copy_to_library,
                    stop=evt.is_set, progress=_cb)
                if not res.get("ok"):
                    _notice("WebDAV 导入失败", res.get("error") or "导入失败", "error")
                    _job_finish(jid, error=str(res.get("error") or "导入失败"))
                    return
                _counts = (f"下载 {int(res.get('downloaded') or 0):,} 张"
                           f" · 入库 {int(res.get('indexed') or 0):,} 张"
                           f" · 打标 {int(res.get('tagged') or 0):,} 张")
                if res.get("stopped"):
                    _notice("WebDAV 导入已停止", _counts + "；可再次导入接着来")
                    _job_finish(jid, result=res, message="已停止（可再次导入接着来）")
                else:
                    _notice("WebDAV 导入完成", _counts)
                    _job_finish(jid, result=res, message="导入完成")
            except Exception as e:      # noqa: BLE001 —— 作业线程必须兜住
                _notice("WebDAV 导入失败", f"{type(e).__name__}: {e}", "error")
                _job_finish(jid, error=f"{type(e).__name__}: {e}")
            finally:
                with _IMPORT_LOCK:
                    if _IMPORT_STATE.get("job") == jid:
                        _IMPORT_STATE["job"] = ""
                        _IMPORT_STATE["cancel"] = None

        threading.Thread(target=_run, daemon=True, name="mrm-import-webdav").start()
        return {"ok": True, "job": jid}

    def _api_import_remote(self, body):
        """POST /api/import/remote {protocol,host,port,user,password,path,tag,copy,recursive}
        → 起 FTP / SFTP / SMB 导入作业（后台 + 轮询）。密码只用于本次连接。
        """
        from . import import_folder as imp
        from . import remote_import as ri
        if not isinstance(body, dict):
            return {"ok": False, "error": "请求格式不对"}
        protocol = str(body.get("protocol") or "").lower().strip()
        host = str(body.get("host") or "").strip()
        if protocol not in ri.DEFAULT_PORT:
            return {"ok": False, "error": "协议仅支持 FTP / SFTP / SMB"}
        if not host:
            return {"ok": False, "error": "请先填写服务器地址"}
        try:
            port = int(body.get("port") or ri.DEFAULT_PORT[protocol])
        except (TypeError, ValueError):
            port = ri.DEFAULT_PORT[protocol]
        user = str(body.get("user") or "")
        password = str(body.get("password") or "")
        remote_path = str(body.get("path") or "").strip()
        do_tag = _as_bool(body.get("tag"), True)
        copy_to_library = _as_bool(body.get("copy"), False)
        recursive = _as_bool(body.get("recursive"), True)
        if imp.is_running():
            return {"ok": False, "error": "已有一个导入任务在跑，等它结束再开始"}
        jid = _job_new("import")
        evt = threading.Event()
        with _IMPORT_LOCK:
            _IMPORT_STATE["job"] = jid
            _IMPORT_STATE["cancel"] = evt
        cfg, db = self.cfg, self.db

        def _cb(phase, done, total, name, eta, pct):
            label = {"list": "正在列目录", "download": "正在下载",
                     "index": "正在索引", "tag": "正在打标"}.get(phase, "正在处理")
            msg = f"{label} {done:,} / {total:,}"
            if name:
                msg += f" · {str(name)[:36]}"
            if eta and total and done < total:
                msg += f" · 约剩 {eta}"
            _jobs_mod.update(jid, pct=max(0, min(100, int(pct))), message=msg,
                             phase=phase, done=done, total=total, eta=eta,
                             name=str(name or "")[:120])

        def _run():
            def _notice(title, text, level="info"):
                try:
                    from . import notify
                    notify.add(db, "import-remote", title, str(text)[:200],
                               link="/export?tab=imp", level=level)
                except Exception:
                    pass
            try:
                res = ri.import_remote(
                    db, cfg, protocol, host, port=port, user=user,
                    password=password, remote_path=remote_path,
                    recursive=recursive, do_tag=do_tag,
                    copy_to_library=copy_to_library, stop=evt.is_set,
                    progress=_cb)
                if not res.get("ok"):
                    _notice(f"{protocol.upper()} 导入失败",
                            res.get("error") or "导入失败", "error")
                    _job_finish(jid, error=str(res.get("error") or "导入失败"))
                    return
                _counts = (f"入库 {int(res.get('indexed') or 0):,} 张"
                           f" · 打标 {int(res.get('tagged') or 0):,} 张")
                if res.get("stopped"):
                    _notice(f"{protocol.upper()} 导入已停止",
                            _counts + "；可再次导入接着来")
                    _job_finish(jid, result=res, message="已停止（可再次导入接着来）")
                else:
                    _notice(f"{protocol.upper()} 导入完成", _counts)
                    _job_finish(jid, result=res, message="导入完成")
            except Exception as e:      # noqa: BLE001
                _notice(f"{protocol.upper()} 导入失败", f"{type(e).__name__}: {e}", "error")
                _job_finish(jid, error=f"{type(e).__name__}: {e}")
            finally:
                with _IMPORT_LOCK:
                    if _IMPORT_STATE.get("job") == jid:
                        _IMPORT_STATE["job"] = ""
                        _IMPORT_STATE["cancel"] = None

        threading.Thread(target=_run, daemon=True, name="mrm-import-remote").start()
        return {"ok": True, "job": jid}

    # ───────────────── R251 · 站点 Cookie（按域名存档） ─────────────────
    #
    # 为什么要有这一块：sites 上 Cloudflare 的 `cf_clearance` 之类的登录态
    # 只能在真实浏览器里拿到。插件把 Cookie 推过来（或用户粘贴 JSON），
    # 软件按**域名**存一份，之后这个域名的所有任务都自动带上 ——
    # 不用每个任务手填一次，也不用为了拿 Cookie 在桌面端弹一个浏览器窗口。
    #
    # ⚠ 三条安全红线：
    #   1. 列表接口**只给名字，绝不回值**（值和密码同级）；
    #   2. 推送接口（`/api/cookie/ingest`）必须带配对码，且限速；
    #   3. 任何日志/审计只记「域名 + 条数」，不记内容。
    def _api_cookies(self):
        """GET /api/cookies → 已存 Cookie 的域名清单 + 插件配对码。"""
        try:
            from . import cookies as ck
            from .config import save_config

            def _save():
                try:
                    save_config(self.cfg)
                except Exception:
                    pass

            code = ck.pair_code(self.cfg, save=_save)
            return {"ok": True, "items": ck.summary(), "pair_code": code,
                    "extension": "extension/Sweet采集助手（插件目录，需在浏览器里加载）"}
        except Exception as e:
            return {"ok": False, "error": f"读取失败：{e}"}

    def _api_cookies_post(self, body):
        """POST /api/cookies → save / delete / import / rotate。

        ``save`` 收「域名 + Cookie 头或导出 JSON」，是手填与插件失败时的兜底。
        """
        from . import cookies as ck
        body = body if isinstance(body, dict) else {}
        act = str(body.get("action") or "").strip().lower()

        if act == "rotate":
            try:
                from .config import save_config
                self.cfg.cookie_pair_code = ck.new_pair_code()
                save_config(self.cfg)
                return {"ok": True, "pair_code": self.cfg.cookie_pair_code,
                        "message": "配对码已更换：插件里要重新填一次"}
            except Exception as e:
                return {"ok": False, "error": f"更换配对码失败：{e}"}

        if act == "delete":
            dom = ck.normalize_domain(body.get("domain"))
            if not dom:
                return {"ok": False, "error": "缺少域名"}
            ck.del_cookie(dom)
            try:
                self.db.audit("cookie", "delete", dom)
            except Exception:
                pass
            return {"ok": True, "domain": dom}

        if act in ("save", "import"):
            raw = body.get("payload")
            if raw is None:
                raw = body.get("text")
            dom = ck.normalize_domain(body.get("domain"))
            # 纯字符串 + 有域名 ⇒ 当成 Cookie 头；其余一律按导出 JSON 解析
            if isinstance(raw, str):
                txt = raw.strip()
                if not txt:
                    return {"ok": False, "error": "内容为空"}
                if dom and "=" in txt and not txt.lstrip().startswith(("[", "{")):
                    res = ck.import_payload(
                        [{"name": k, "value": v, "domain": dom}
                         for k, v in ck._parse(txt).items()],
                        merge=bool(body.get("merge", True)))
                else:
                    try:
                        res = ck.import_payload(__import__("json").loads(txt),
                                                merge=bool(body.get("merge", True)))
                    except Exception:
                        return {"ok": False,
                                "error": "不是合法的 Cookie 内容（既不是 k=v 串，也不是导出 JSON）"}
            else:
                res = ck.import_payload(raw,
                                        merge=bool(body.get("merge", True)))
            if not res.get("domains"):
                return {"ok": False, "error": "没有解析出可用的 Cookie（检查是否包含域名与名字）"}
            try:
                for d, info in res["domains"].items():
                    self.db.audit("cookie", "import", d,
                                  f"{info.get('count')} 条")
            except Exception:
                pass
            return {"ok": True, **res}

        return {"ok": False, "error": f"未知操作：{act or '（空）'}"}

    def _api_cookie_ingest(self, body):
        """POST /api/cookie/ingest → 浏览器插件推送过来的 Cookie（**凭配对码**）。

        这条路径在鉴权与同源校验之前，所以这里自己把关：
          · 配对码（恒定时间比较）；
          · 限速：同一 IP 每分钟最多 8 次，连错 20 次锁 5 分钟 ——
            6 位数字虽然难猜，但不该允许无限次尝试；
          · 只接受「本机/内网来源」的 Host（外层已校验过 Host 头）。
        """
        body = body if isinstance(body, dict) else {}
        self._cors_extension()
        try:
            from . import cookies as ck
            if not ck.check_pair_code(self.cfg, body.get("code")):
                self._ingest_throttle_fail()
                return {"ok": False, "_status": 403,
                        "error": "配对码不对（在设置 → 站点 Cookie 里看 6 位数字），"
                                 "或插件尚未填写配对码"}
            if not self._ingest_throttle_ok():
                return {"ok": False, "_status": 429,
                        "error": "尝试过于频繁，请等几分钟再试"}

            dom = ck.normalize_domain(body.get("domain") or body.get("url"))
            raw = body.get("cookies")
            if raw is None:
                raw = body.get("payload")
            if raw is None and body.get("header"):
                raw = [{"name": k, "value": v, "domain": dom}
                       for k, v in ck._parse(str(body["header"])).items()]
            if isinstance(raw, list):
                for it in raw:
                    if isinstance(it, dict) and not it.get("domain"):
                        it["domain"] = dom
            if not raw:
                return {"ok": False, "error": "没有收到 Cookie"}
            res = ck.import_payload(raw, merge=True)
            if not res.get("domains"):
                return {"ok": False, "error": "Cookie 里没有可识别的域名或名字"}
            # UA 顺带存下来（CF 的 cf_clearance 与 UA 绑，UA 变了就失效）
            ua = str(body.get("ua") or "").strip()
            if ua and len(ua) < 512:
                try:
                    from .config import save_config
                    if str(getattr(self.cfg, "user_agent", "") or "") != ua:
                        self.cfg.user_agent = ua
                        save_config(self.cfg)
                except Exception:
                    pass
            for d, info in res["domains"].items():
                try:
                    self.db.audit("cookie", "push", d, f"{info.get('count')} 条")
                except Exception:
                    pass
            return {"ok": True, "domains": res["domains"],
                    "imported": res.get("imported", 0),
                    "message": "已保存到软件"}
        except Exception as e:
            return {"ok": False, "error": f"接收失败：{e}"}

    def _cors_extension(self):
        """给插件回 CORS 头：只认 ``chrome-extension://`` 来源。

        为什么不是 ``*``：这条接口虽然靠配对码守，但没必要让**任意网页**
        读到它的响应（写 Cookie 的接口给了读权限，等于把「你存了哪些域名」
        告诉所有网页）。扩展的来源固定是 ``chrome-extension://<id>``。
        """
        try:
            origin = str(self.headers.get("Origin", "") or "")
        except Exception:
            origin = ""
        if not origin.startswith("chrome-extension://"):
            return
        # 头必须在 send_response 之后、end_headers 之前塞进去；
        # 这里只登记到实例上，由 _json 统一写。
        self._extra_headers = getattr(self, "_extra_headers", [])
        self._extra_headers.extend([
            ("Access-Control-Allow-Origin", origin),
            ("Access-Control-Allow-Methods", "POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "content-type"),
            ("Access-Control-Max-Age", "600"),
        ])

    def _ingest_throttle_ok(self) -> bool:
        """同一 IP 每分钟最多 8 次推送（插件连点也不会超过）。"""
        now = time.monotonic()
        ip = self._client_ip()
        with _INGEST_LOCK:
            hits = [t for t in _INGEST_HITS.get(ip, []) if now - t < 60]
            _INGEST_HITS[ip] = hits
            if len(hits) >= 8:
                return False
            hits.append(now)
            _INGEST_HITS[ip] = hits
            return True

    def _ingest_throttle_fail(self) -> None:
        """配对码错 20 次锁 5 分钟（防逐位爆破）。"""
        now = time.monotonic()
        ip = self._client_ip()
        with _INGEST_LOCK:
            row = _INGEST_FAIL.get(ip) or [0, 0.0]
            row[0] += 1
            if now - row[1] > 300:
                row[0], row[1] = 1, now      # 5 分钟窗口外重新计数
            _INGEST_FAIL[ip] = row

    # ─────────── R249 · 磁盘级维护四件套（网页端与桌面端共用逻辑） ───────────
    def _maint_busy(self):
        with _MAINT_LOCK:
            jid = str(_MAINT_STATE.get("job") or "")
        if not jid:
            return None
        rec = _job_get(jid)
        if rec is None or rec.get("state") != "running":
            with _MAINT_LOCK:
                if _MAINT_STATE.get("job") == jid:
                    _MAINT_STATE["job"] = ""
            return None
        return jid

    def _api_maint_start(self, kind: str, body):
        """POST /api/maintenance/{scan,orphans,convert,repair} → 起后台作业。

        诊断（只读）与修复（改数据）共用一条通道，但**参数默认值刻意不同**：
        `repair` 默认「不降阈值、不删死链」，必须显式勾。
        """
        busy = self._maint_busy()
        if busy:
            return {"ok": False, "error": "已有维护作业在跑，等它结束再发起"}
        body = body if isinstance(body, dict) else {}
        jid = _job_new(f"maint_{kind}")
        evt = threading.Event()
        with _MAINT_LOCK:
            _MAINT_STATE["job"] = jid
            _MAINT_STATE["cancel"] = evt
        cfg, db, progress = self.cfg, self.db, _job_progress(jid)

        def _run():
            try:
                from . import maintenance as mt
                if kind == "scan":
                    res = mt.scan(db, cfg, deep=bool(body.get("deep")),
                                  progress=progress, stop=evt.is_set)
                    msg = ("发现 %d 条异常" % sum(res.get("kinds", {}).values())
                           if res.get("kinds") else "全部正常")
                elif kind == "orphans":
                    res = mt.clean_orphans(db, cfg, progress=progress,
                                           stop=evt.is_set)
                    msg = "清理 %d 个 0 字节/残片文件" % res.get("moved", 0)
                elif kind == "convert":
                    res = mt.convert_to_jpg(
                        db, cfg, dry_run=bool(body.get("dry_run")),
                        limit=int(body.get("limit") or 0),
                        progress=progress, stop=evt.is_set)
                    msg = ("可以转换 %d 张" % res.get("to_convert", 0)
                           if res.get("dry_run")
                           else "转换 %d 张" % res.get("converted", 0))
                elif kind == "repair":
                    res = mt.repair(
                        db, cfg,
                        fix_threshold=bool(body.get("fix_threshold")),
                        restore_quarantine=bool(
                            body.get("restore_quarantine", True)),
                        drop_ghosts=bool(body.get("drop_ghosts", False)),
                        progress=progress, stop=evt.is_set)
                    msg = ("恢复 %d 张、清掉 %d 条死链"
                           % (res.get("restored", 0), res.get("ghosts", 0)))
                else:
                    raise ValueError(f"未知的维护类型：{kind}")
                if not res.get("ok", True):
                    _job_finish(jid, error=res.get("error") or "执行失败")
                    return
                _job_finish(jid, result=res, message=msg)
            except Exception as e:      # noqa: BLE001 —— 作业线程必须兜住
                _job_finish(jid, error=f"执行失败：{e}")
            finally:
                with _MAINT_LOCK:
                    if _MAINT_STATE.get("job") == jid:
                        _MAINT_STATE["job"] = ""
                        _MAINT_STATE["cancel"] = None

        threading.Thread(target=_run, name=f"mrm-maint-{kind}",
                         daemon=True).start()
        return {"ok": True, "job": jid, "kind": kind}

    def _api_maint_job(self, u):
        """GET /api/maintenance/job?id=… → 进度与结果（前端 1 秒一轮）。"""
        jid = (parse_qs(u.query).get("id") or [""])[0]
        rec = _job_get(jid)
        if rec is None:
            return {"ok": False, "error": "作业已过期或不存在，请重新发起"}
        return {"ok": True, **rec}

    def _api_maint_cancel(self):
        """POST /api/maintenance/cancel → 请求停止（优雅停，不杀线程）。"""
        with _MAINT_LOCK:
            evt = _MAINT_STATE.get("cancel")
        if evt is None:
            return {"ok": False, "error": "当前没有正在跑的维护作业"}
        evt.set()
        return {"ok": True, "message": "已请求停止：已处理完的部分保留"}

    def _api_maint_clean(self, body):
        """POST /api/maintenance/clean → 删掉勾选的异常库记录（同步）。

        ⚠ **只删库记录，不动磁盘文件**：0 字节/坏图要清就单独选择「清理孤儿文件」，
        那边的动作是「移到 trash」而不是删 —— 两个动作分开才对得上用户的直觉。
        """
        ids = (body or {}).get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not ids:
            return {"ok": False, "error": "没有勾选任何记录"}
        # 上限：一次删几万条会把库锁住很久，让用户分批
        if len(ids) > 5000:
            return {"ok": False,
                    "error": "一次最多清理 5000 条，请分批勾选"}
        try:
            from . import maintenance as mt
            res = mt.clean_records(self.db, ids)
        except Exception as e:
            return {"ok": False, "error": f"清理失败：{e}"}
        return {"ok": True, **res}

    def _api_full_list(self):
        """GET /api/backup/full → 盘点整包备份（新的在前）+ 空间占用。"""
        try:
            from .data_backup import human_size
            from .fullbackup import is_full_name, read_manifest
            d = self._full_dir()
            items = []
            for f in sorted(d.glob("*.zip"), reverse=True):
                if not (f.is_file() and is_full_name(f.name)):
                    continue
                rec = {"name": f.name, "bytes": f.stat().st_size,
                       "bytes_text": human_size(f.stat().st_size),
                       "mtime": datetime.datetime.fromtimestamp(
                           f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                       "counts": {}, "app_version": "", "readable": True,
                       "error": ""}
                try:
                    man = read_manifest(f)
                    rec["counts"] = man.get("counts") or {}
                    rec["app_version"] = man.get("app_version") or ""
                    rec["has_thumbs"] = bool(man.get("has_thumbs"))
                    rec["created_at"] = man.get("created_at") or ""
                except Exception as e:
                    # 坏包也列出来：用户要能看到「这份坏了」并删掉，
                    # 悄悄隐藏只会让他在恢复时才踩到。
                    rec["readable"] = False
                    rec["error"] = str(e)
                items.append(rec)
            total = sum(i["bytes"] for i in items)
            return {"ok": True, "items": items, "count": len(items),
                    "total_text": human_size(total),
                    "dir": str(d)}
        except Exception as e:
            return {"ok": False, "error": f"读取备份清单失败：{e}"}

    def _api_full_prune(self):
        """POST /api/backup/full/prune → 按当前配置清一次旧整包备份。

        同步执行（删几个文件是毫秒级）。**最新一份永远保留**，即便它已经
        超过天数/总量上限 —— 那是用户唯一的退路。
        """
        try:
            res = self._prune_full_backups()
            if res.get("error"):
                return {"ok": False, "error": f"清理失败：{res['error']}"}
            n = len(res.get("deleted") or [])
            try:
                self.db.audit(self._actor(), "full-prune", str(n),
                              f"{res.get('bytes', 0)} bytes")
            except Exception:
                pass
            return {"ok": True, "deleted": res.get("deleted") or [],
                    "bytes": int(res.get("bytes") or 0),
                    "message": (f"已清理 {n} 份旧备份" if n else "没有需要清理的备份")}
        except Exception as e:
            return {"ok": False, "error": f"清理失败：{e}"}

    def _api_full_export(self, body):
        """POST /api/backup/full/export → 起一个后台作业打包整库备份。"""
        try:
            from .fullbackup import full_name
            from . import __version__
            # R247：默认**不带缩略图**。缩略图可从原图重建，百万级时它
            # 占备份体积的绝大部分；默认带上会让「备份一次」从秒级变半小时。
            include_thumbs = False
            if isinstance(body, dict) and "include_thumbs" in body:
                include_thumbs = bool(body.get("include_thumbs"))
            dest = self._full_dir() / full_name()
            jid = _job_new("full_export")
            cfg, db, progress = self.cfg, self.db, _job_progress(jid)
            base = Path(self._full_dir()).parent

            def _run():
                try:
                    from . import fullbackup
                    res = fullbackup.build_export(
                        dest, base, db,
                        version=getattr(cfg, "app_version", "") or __version__,
                        include_thumbs=include_thumbs, progress=progress)
                    try:
                        db.audit(self._actor(), "full-backup", res["name"],
                                 f"{res['bytes']} bytes")
                    except Exception:
                        pass
                    pruned = self._prune_full_backups()
                    if pruned.get("deleted"):
                        res["pruned"] = pruned["deleted"]
                        res["pruned_bytes"] = pruned.get("bytes", 0)
                    _job_finish(jid, result=res, message="备份完成")
                except Exception as e:      # noqa: BLE001 —— 作业线程必须兜住
                    _job_finish(jid, error=f"导出失败：{e}")

            threading.Thread(target=_run, name="mrm-full-export",
                             daemon=True).start()
            return {"ok": True, "job": jid, "name": dest.name}
        except Exception as e:
            return {"ok": False, "error": f"无法开始导出：{e}"}

    def _api_full_job(self, u):
        """GET /api/backup/full/job?id=… → 作业进度（前端 1 秒一轮）。"""
        jid = (parse_qs(u.query).get("id") or [""])[0]
        rec = _job_get(jid)
        if rec is None:
            return {"ok": False, "error": "作业已过期或不存在，请重新发起"}
        return {"ok": True, **rec}

    def _api_full_inspect(self, u):
        """GET /api/backup/full/inspect?name=… → 恢复前预览包内容。"""
        name = (parse_qs(u.query).get("name") or [""])[0]
        p = self._full_path_of(name)
        if p is None or not p.is_file():
            return {"ok": False, "error": "未找到该备份（名称有误或已删除）"}
        try:
            from .fullbackup import inspect_zip
            man = inspect_zip(p)
            return {"ok": True, "name": p.name, **man}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _api_full_download(self, u):
        """GET /api/backup/full/download?name=… → 流式下载备份包。"""
        name = (parse_qs(u.query).get("name") or [""])[0]
        p = self._full_path_of(name)
        if p is None or not p.is_file():
            return self._json({"ok": False, "error": "未找到该备份"}, 404)
        ok = self._send_file(p, "application/zip", {
            "Content-Disposition": f'attachment; filename="{p.name}"'})
        if not ok:
            return self._json({"ok": False, "error": "备份文件读不出来"}, 404)
        return None

    def _api_full_delete(self, body):
        """POST /api/backup/full/delete → 删一份整包备份（用户手点才算）。

        ⚠ 只删 ``backups/`` 下、名字完全合法的那一个文件；不递归、不按通配。
        """
        try:
            name = str((body or {}).get("name") or "")
            p = self._full_path_of(name)
            if p is None:
                return {"ok": False, "error": "备份名字不合法"}
            if not p.is_file():
                return {"ok": False, "error": "这份备份已经不在了"}
            p.unlink()
            return {"ok": True, "deleted": p.name}
        except Exception as e:
            return {"ok": False, "error": f"删除失败：{e}"}

    def _raw_body_to_file(self, dest: Path, cap: int) -> int:
        """把请求体（``application/octet-stream``）流式落到 ``dest``。

        为什么不用 multipart：前端 ``fetch(url, {body: file})`` 直接发二进制
        更简单也更省内存（multipart 解析器要自己写边界扫描，写错就是
        「上传到 99% 才失败」）。代价是文件名走 query 参数 —— 但文件名本来
        就要在服务端重新生成（不能信客户端给的路径），所以没损失。
        """
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise ValueError("上传内容为空")
        if n > cap:
            raise ValueError("文件太大（上限 %d GB）" % (cap // (1024 ** 3)))
        left, written = n, 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            while left > 0:
                chunk = self.rfile.read(min(left, _FILE_CHUNK * 8))
                if not chunk:
                    break
                fh.write(chunk)
                left -= len(chunk)
                written += len(chunk)
        if written != n:
            # 半截文件绝不能留在 backups/ 里让用户以为「上传成功了」
            try:
                dest.unlink()
            except OSError:
                pass
            raise ValueError("上传中断：只收到 %d / %d 字节" % (written, n))
        return written

    def _api_full_upload(self, u):
        """POST /api/backup/full/upload?name=xxx.zip → 接收用户上传的备份包。

        **两段式落盘**（踩过的真 bug，别改回去）：
        先写到 ``<时间戳>.zip.part``，读一遍清单确认是本工具的整包备份，
        通过了才 ``replace`` 成正式名字。

        为什么不能直接写正式名字：
          · 同名文件会被**截断覆盖**，用户手上那份好备份在他上传一个坏包
            的瞬间就没了（哪怕随后校验失败删掉，删掉的是刚写坏的这份，
            原来那份已经被覆盖掉了）；
          · 校验失败时删 ``target`` 会把「同名的另一份好包」一起删掉。
        第二段起名字也留了余量：客户端名字合法且没占用就沿用（用户看得懂），
        否则按当前时间起名，撞了就往后顺延几秒，绝不覆盖已有备份。
        """
        from .fullbackup import MAX_UPLOAD, full_name, is_full_name, read_manifest
        d = self._full_dir()
        part = d / (full_name() + ".part")
        try:
            name = str((parse_qs(u.query).get("name") or [""])[0]).strip()
            got = self._raw_body_to_file(part, MAX_UPLOAD)
            try:
                man = read_manifest(part)
            except Exception:
                raise
            want = d / name if (is_full_name(name) and not (d / name).exists()) else None
            if want is None:
                _now = datetime.datetime.now()
                for i in range(120):
                    cand = d / full_name(_now + datetime.timedelta(seconds=i))
                    if not cand.exists():
                        want = cand
                        break
                if want is None:
                    raise ValueError("备份目录里同名文件过多，请先清理旧备份")
            part.replace(want)
            return {"ok": True, "name": want.name, "bytes": got,
                    "counts": man.get("counts") or {},
                    "created_at": man.get("created_at") or ""}
        except ValueError as e:
            try:
                part.unlink()
            except OSError:
                pass
            return {"ok": False, "error": str(e)}
        except Exception as e:
            # 清单不合格（RestoreError 等）：只删自己刚写的 .part
            try:
                part.unlink()
            except OSError:
                pass
            return {"ok": False, "error": str(e)}

    def _api_full_restore(self, body):
        """POST /api/backup/full/restore → 后台把一份备份恢复进当前库。

        步骤（每一步失败都会整段回滚或保持原状，见各步注释）：
          1. 校验包 + 解到临时目录（路径穿越/符号链接/超大成员一律拒）；
          2. **先自动留一份当前状态的整包备份**（``restore_safety=True`` 默认开）：
             恢复错了还能倒回去，这一步花的时间远小于「恢复完发现包拿错了」；
          3. 暂停队列 + 有界等运行中任务收尾（避免边写边被覆盖）；
          4. 库：``ATTACH`` 就地逐表覆盖（连接不关，桌面/引擎不受影响）；
          5. 缩略图/规则/配置：按各自语义覆盖（见 ``fullbackup._copy_tree``）;
          6. 续跑队列 + 自检 + 汇报「恢复进来多少条」。
        """
        try:
            from . import fullbackup as fb
            name = str((body or {}).get("name") or "")
            p = self._full_path_of(name)
            if p is None or not p.is_file():
                return {"ok": False, "error": "未找到该备份（名称有误或已删除）"}
            safety = (body or {}).get("restore_safety", True)
            jid = _job_new("full_restore")
            cfg, db, progress = self.cfg, self.db, _job_progress(jid)
            queue = getattr(self.server, "queue", None)   # type: ignore[attr-defined]
            base = Path(self._full_dir()).parent
            fdir = self._full_dir()

            def _run():
                tmpdir = Path(tempfile.mkdtemp(prefix="mrm_restore_"))
                paused = False
                try:
                    man = fb.read_manifest(p)
                    progress(3, "正在校验备份包…")
                    safety_path = None
                    if safety:
                        progress(6, "正在为当前状态留一份备份…")
                        try:
                            from . import __version__
                            safety_path = fb.build_export(
                                fdir / fb.full_name(), base, db,
                                version=getattr(cfg, "app_version", "") or __version__,
                                # R247：退路只要「能回到恢复前的数据」，
                                # 缩略图可重建，不必拖慢这一次关键备份。
                                include_thumbs=False,
                                progress=lambda pct, msg: progress(
                                    6 + int(pct * 0.22), "恢复前自动备份：" + msg))
                        except Exception as e:
                            # 留不出退路就**不恢复** —— 这是本功能的底线
                            raise fb.RestoreError(
                                "恢复前的自动备份失败（%s），为防数据丢失已中止。"
                                "可先关闭「恢复前自动备份」重试。" % e)

                    progress(30, "正在解包…")
                    parts = fb.extract_parts(p, tmpdir, man)
                    if parts.get("db") is None and man.get("db"):
                        raise fb.RestoreError(
                            "备份包里缺少数据库文件，无法恢复（包可能不完整）")

                    if queue is not None:
                        try:
                            queue.pause(reason="正在恢复备份")
                            paused = True
                        except Exception:
                            pass
                        deadline = time.monotonic() + 20.0
                        while time.monotonic() < deadline:
                            try:
                                if not getattr(queue, "running", None):
                                    break
                            except Exception:
                                break
                            time.sleep(0.2)

                    db_res = {}
                    # 20 秒还没收工（大图 / 慢盘）不硬等：照样恢复，但**如实**
                    # 在结果里挂一句警告 —— 边写边被覆盖，那一轮的数据可能丢。
                    busy_left = False
                    try:
                        busy_left = bool(getattr(queue, "running", None))
                    except Exception:
                        busy_left = False
                    if parts.get("db") is not None:
                        progress(40, "正在恢复数据库…")
                        src = fb._decrypt_db_if_needed(parts["db"], db, tmpdir)
                        # sqlite ATTACH 不接受与目标相同的文件（同 inode 会报
                        # "database is already attached"），包里存的是快照副本，
                        # 不会撞；真撞了就是有人在原地覆盖，让它报错更安全。
                        db_res = db.restore_from(src)
                        if str(db_res.get("integrity") or "ok").lower() != "ok":
                            # 注意：这时数据已经换进来了，**不能谎称回滚**。
                            # 如实告诉用户 + 指向刚才那份自动备份。
                            raise fb.RestoreError(
                                "恢复已执行，但数据库自检未通过：%s。"
                                "建议用「%s」恢复回恢复前的状态。"
                                % (db_res.get("integrity"),
                                   (safety_path.name if safety_path else "上一份备份")))

                    if parts.get("thumbs") is not None:
                        n = fb._copy_tree(parts["thumbs"], base / "thumbs",
                                          replace=False, progress=progress,
                                          pct_from=62, pct_to=80, label="恢复缩略图")
                        db_res["thumbs"] = n
                    if parts.get("rules") is not None:
                        n = fb._copy_tree(parts["rules"], base / "rules",
                                          replace=True, progress=progress,
                                          pct_from=80, pct_to=88, label="恢复规则")
                        db_res["rules"] = n
                    if parts.get("config") is not None:
                        progress(90, "正在合并配置…")
                        try:
                            from .config import save_config
                            db_res["config"] = fb.merge_config(
                                cfg, parts["config"],
                                save=lambda: save_config(cfg))
                        except Exception as e:
                            db_res["config"] = {"error": str(e)}

                    progress(96, "正在自检…")
                    db_res["counts"] = fb.counts_after(db)
                    db_res["backup"] = p.name
                    db_res["safety_backup"] = safety_path.name if safety_path else ""
                    db_res["skipped_members"] = parts.get("skipped") or 0
                    if busy_left:
                        db_res["warning"] = (
                            "恢复时仍有任务在运行（等待 20 秒未收工），"
                            "该任务这一轮新写入的图片可能已被覆盖，请在任务页确认后重跑。")
                    try:
                        db.audit(self._actor(), "full-restore", p.name,
                                 json.dumps(db_res.get("counts") or {},
                                            ensure_ascii=False))
                    except Exception:
                        pass
                    _job_finish(jid, result=db_res, message="恢复完成")
                except Exception as e:      # noqa: BLE001 —— 作业线程必须兜住
                    _job_finish(jid, error=str(e) or "恢复失败")
                finally:
                    if paused:
                        try:
                            queue.resume()
                        except Exception:
                            pass
                    try:
                        shutil.rmtree(tmpdir, ignore_errors=True)
                    except Exception:
                        pass

            threading.Thread(target=_run, name="mrm-full-restore",
                             daemon=True).start()
            return {"ok": True, "job": jid, "name": p.name}
        except Exception as e:
            return {"ok": False, "error": f"无法开始恢复：{e}"}

    # ---------- R242 · 诊断包（桌面「维护 → 导出诊断包」的 Web 对称） ----------
    def _api_diagnostic(self, u):
        """GET /api/diagnostic[?db=0] → 直接下载脱敏诊断包 zip。

        同步生成（只读聚合 + 日志尾部 + 库快照，通常 1~2 秒）；库很大时
        快照会慢一点，前端给的超时是 180 秒。
        """
        tmpdir = Path(tempfile.mkdtemp(prefix="mrm_diag_out_"))
        try:
            from . import __version__, diagnostics
            include_db = (parse_qs(u.query).get("db") or ["1"])[0] not in ("0", "false")
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            out = tmpdir / ("mrm_diagnostic_%s.zip" % ts)
            diagnostics.build_bundle(
                out, cfg=self.cfg, db=self.db, include_db=include_db,
                extra_env={"app_version": __version__,
                           "web": True,
                           "web_port": getattr(self.cfg, "web_port", "")})
            # V0.0.15：需求 19「所有导出的任务和所有导出的信息必须加密」。
            # 桌面端 export_diagnostic_bundle 早就把整包加密成 .mrmx，
            # 网页端以前却直接送明文 zip —— 含库版本里带图片路径与来源网址，
            # 明文外发等于绕过红线。这里补上同一份 crypt 加密，两端格式一致。
            from . import export_kit as ek

            data = out.read_bytes()
            blob = ek.encrypted(
                data, str(getattr(self.cfg, "export_passphrase", "") or ""))
            fname = out.stem + ".mrmx"
            return self._send(
                200, blob, "application/octet-stream",
                {"Content-Disposition":
                 f'attachment; filename="{fname}"'})
        except Exception as e:
            return self._json({"ok": False, "error": f"生成诊断包失败：{e}"}, 500)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _api_prefs_export(self):
        """GET /api/preferences/export → 下载偏好 JSON（与桌面端同一份格式）。"""
        try:
            from .data_backup import prefs_payload
            rows = self.db.query(
                "SELECT task_id, label, weight, seen FROM preferences")
            data = prefs_payload(rows)
            body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            return self._send(
                200, body, "application/json; charset=utf-8",
                {"Content-Disposition":
                 f'attachment; filename="mrm_preferences_{ts}.json"'})
        except Exception as e:
            return {"ok": False, "error": f"导出偏好失败：{e}"}

    def _api_prefs_import(self, body):
        """POST /api/preferences/import → 合并偏好进全局学习记录。

        ⚠ 与桌面端 ``import_preferences`` **同一条 UPSERT、同样的 task_id=0**
        （学习记录是全局的）。两端各写一份 ⇒ 同一份文件导入后存成两套数据。

        ⚠ 请求体走 :meth:`_body_json`（不是 :meth:`_body`）：导入文件既可能是
        ``{"preferences": [...]}`` 那个对象，也可能是裸数组，而 ``_body`` 只
        认 dict（数组会被强制转成 ``{}`` ⇒ 「导入成功 0 条」这种假象）。
        1MB 上限沿用 _body 的口径（见该方法注释）；超了会拿到空对象，
        由 normalize_prefs 走「没有可用偏好」的中文报错，不静默。
        """
        try:
            from .data_backup import PREFS_UPSERT_SQL, normalize_prefs
            items, skipped = normalize_prefs(body)
            if not items:
                return {"ok": False,
                        "error": "导入内容里没有可用的偏好（需要 preferences 数组）"}
            merged = 0
            for it in items:
                try:
                    self.db.execute(PREFS_UPSERT_SQL,
                                    (0, it["label"], it["weight"], it["seen"]))
                    merged += 1
                except Exception:
                    continue
            return {"ok": True, "merged": merged,
                    "skipped": skipped + (len(items) - merged)}
        except Exception as e:
            return {"ok": False, "error": f"导入偏好失败：{e}"}

    # ---------- R154-F25 采集周报 / 月报（与桌面端同一个 reporting 模块） ----------

    def _api_report(self, u):
        """R154-F25：按周 / 按月汇总这一期采得怎么样。

        ⚠ 统计**一行 SQL 都不自己写**，全走 ``reporting.collect_period``
        —— 桌面端「数据 → 采集周报 / 月报…」用的是同一个函数。两端各写
        一份口径，用户就会看到「桌面说 12、手机说 9」。

        查询参数：
          · ``kind``：week（默认） / month；
          · ``offset``：往前几期，**0 = 当期**（负数一律钳回 0，未来没有数据）。

        顺带把 ``markdown`` / ``csv`` 一起返回：导出是同一份报告的两个格式，
        让前端再拼一次就会和界面上看到的对不上。
        """
        try:
            from urllib.parse import parse_qs

            from .reporting import (
                PERIOD_WEEK, collect_period, headline, period_range,
                to_csv, to_markdown,
            )
            q = parse_qs(u.query or "")
            kind = (q.get("kind") or [PERIOD_WEEK])[0]
            try:
                offset = int((q.get("offset") or ["0"])[0])
            except (TypeError, ValueError):
                offset = 0
            p = period_range(kind, None, offset)
            rep = collect_period(self.db, p["start"], p["end"])
            rep["kind"] = p["kind"]
            rep["label"] = p["label"]
            return {"ok": True, "report": rep,
                    "headline": headline(rep),
                    "markdown": to_markdown(rep, p["kind"]),
                    "csv": to_csv(rep),
                    "next_cursor": None, "has_more": False}
        except Exception as e:
            return {"ok": False, "error": f"读取采集报告失败：{e}"}

    # ---------- V0.0.14 · 合格线校准 / 加密导出 / 训练素材集 ----------
    # 三个能力共用 ``threshold_tune`` 与 ``export_kit`` 两个纯逻辑模块：
    # 桌面端菜单与网页端点都调同一份实现，不另写一套 SQL 或统计口径。
    def _has_column(self, table: str, column: str) -> bool:
        """老库可能尚未跑到迁移；缺列时把对应筛选退化成「不筛选」。

        ``table`` 走白名单，绝不把外部字符串拼进 PRAGMA。
        """
        if table not in ("images", "tasks"):
            return False
        try:
            return any(str(r[1]) == column
                       for r in self.db.query(f"PRAGMA table_info({table})"))
        except Exception:
            return False

    def _api_threshold_suggest(self, u):
        """GET /api/threshold/suggest → 只读建议，不写任何配置。"""
        from . import threshold_tune as tt

        q = parse_qs(u.query)
        default = getattr(self.cfg, "ai_threshold", 0.35)
        try:
            cur = float((q.get("current") or [default])[0])
        except (TypeError, ValueError):
            try:
                cur = float(default)
            except (TypeError, ValueError):
                cur = 0.35
        try:
            return tt.suggest(self.db, cur)
        except Exception as e:
            return {"ok": False, "note": f"计算合格线失败：{e}"}

    def _api_threshold_apply(self, body):
        """POST /api/threshold/apply → 写全局合格线。

        默认**只改全局默认值**，不动用户已经配好的任务；仅当请求显式带
        ``apply_tasks=true`` 才同步覆盖全部任务。任务级阈值优先级高于全局，
        这一点要写进界面说明，避免用户以为改了却没生效。
        """
        from .config import save_config

        b = body if isinstance(body, dict) else {}
        try:
            val = float(b.get("value"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "合格线数值不合法"}
        if not (0.0 <= val <= 1.0):
            return {"ok": False, "error": "合格线须在 0 到 1 之间"}
        val = round(val, 3)
        try:
            old = float(getattr(self.cfg, "ai_threshold", 0.35) or 0.35)
        except (TypeError, ValueError):
            old = 0.35
        self.cfg.ai_threshold = val
        try:
            save_config(self.cfg)
        except Exception as e:
            self.cfg.ai_threshold = old
            return {"ok": False, "error": f"保存合格线失败：{e}"}
        applied_tasks = False
        if bool(b.get("apply_tasks")) and self._has_column("tasks", "ai_threshold"):
            try:
                self.db.execute("UPDATE tasks SET ai_threshold=?", (val,))
                applied_tasks = True
            except Exception:
                applied_tasks = False
        try:
            self.db.audit(self._actor(), "threshold-apply", f"{old}->{val}",
                          str(int(applied_tasks)))
        except Exception:
            pass
        note = (f"全局合格线已由 {round(old, 3)} 设置为 {val}。"
                + ("该数值已同步覆盖全部任务。"
                   if applied_tasks else
                   "任务中单独填写过合格线的，仍以任务设置为准。"))
        return {"ok": True, "value": val, "previous": round(old, 3),
                "applied_tasks": applied_tasks, "note": note}

    def _api_export_file(self, u):
        """GET /api/export/file?kind=report|index → 下载 .mrmx 加密包。

        图库索引与采集报告都含「图片 → 标签 / 路径」这类信息，按用户定的
        红线一律加密（与桌面端 ``_write_encrypted`` 同一个 ``crypt`` 模块）。
        """
        from . import export_kit as ek

        q = parse_qs(u.query)
        kind = (q.get("kind") or ["index"])[0].strip().lower()
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            if kind == "report":
                try:
                    off = int((q.get("offset") or ["0"])[0])
                except (TypeError, ValueError):
                    off = 0
                period = (q.get("period") or ["week"])[0]
                data = ek.report_bytes(self.db, period, off)
                name = f"mrm_report_{ts}.mrmx"
            elif kind == "index":
                data = ek.gallery_index_bytes(self.db)
                name = f"mrm_gallery_index_{ts}.mrmx"
            else:
                return self._json({"ok": False, "error": "未知的导出类型"}, 400)
            blob = ek.encrypted(
                data, str(getattr(self.cfg, "export_passphrase", "") or ""))
            return self._send(200, blob, "application/octet-stream",
                              {"Content-Disposition":
                               f'attachment; filename="{name}"'})
        except Exception as e:
            return self._json({"ok": False, "error": f"导出失败：{e}"}, 500)

    def _api_training_kit(self, body):
        """POST /api/export/training-kit → 复制合格图成一份可训练的素材集。

        同步执行（复制 500 张通常数秒）；前端给足超时。原图全程只读。
        """
        from . import export_kit as ek

        b = body if isinstance(body, dict) else {}
        dest = str(b.get("dest") or "").strip()
        if not dest:
            return {"ok": False, "error": "请先选择导出目录"}
        keys = ("min_score", "only_favorite", "task_id", "tag", "limit",
                "group_by", "extra_terms", "ids", "caption_template")
        opts = {k: b[k] for k in keys if k in b}
        if opts.get("min_score") in (None, ""):
            opts["min_score"] = getattr(self.cfg, "ai_threshold", 0.35)
        # V0.0.18：标注模板留空时取上次保存的默认值（app_meta，不改 config.py）
        if not str(opts.get("caption_template") or "").strip():
            try:
                saved = self.db.meta_get(ek.CAPTION_TEMPLATE_KEY)
                if saved:
                    opts["caption_template"] = saved
            except Exception:
                pass
        try:
            res = ek.export_training_kit(self.db, self.cfg, dest, opts)
        except Exception as e:
            return {"ok": False, "error": f"导出训练素材集失败：{e}"}
        if res.get("ok"):
            try:
                self.db.audit(self._actor(), "export-training-kit",
                              str(res.get("dest") or ""),
                              str(res.get("copied") or 0))
            except Exception:
                pass
            # 本次用到的模板存为默认值（app_meta，不动 config.py）
            if str(opts.get("caption_template") or "").strip():
                try:
                    self.db.meta_set(ek.CAPTION_TEMPLATE_KEY,
                                     str(opts["caption_template"]).strip())
                except Exception:
                    pass
            try:
                from . import notify
                notify.add(
                    self.db, "export-training-kit",
                    f"训练素材集导出完成（{res.get('copied') or 0} 张）",
                    str(res.get("note") or "")[:200], link="/export")
            except Exception:
                pass
        return res

    # ---------- V0.0.18 · 网页端补齐（导出预览 / 历史 / 站点统计 / 通知 / 全局搜索 / 更新检查） ----------

    def _api_export_kit_preview(self, body):
        """POST /api/export/kit/preview → 素材集导出前预览（不落任何盘）。

        参数与 /api/export/training-kit 相同（min_score/only_favorite/
        task_id/tag/limit/group_by/extra_terms/caption_template）。
        口径复用 export_kit.preview_training_kit，与真导出同一份筛选 SQL。
        """
        from . import export_kit as ek

        b = body if isinstance(body, dict) else {}
        keys = ("min_score", "only_favorite", "task_id", "tag", "limit",
                "group_by", "extra_terms", "caption_template")
        opts = {k: b[k] for k in keys if k in b}
        if opts.get("min_score") in (None, ""):
            opts["min_score"] = getattr(self.cfg, "ai_threshold", 0.35)
        if not str(opts.get("caption_template") or "").strip():
            try:
                saved = self.db.meta_get(ek.CAPTION_TEMPLATE_KEY)
                if saved:
                    opts["caption_template"] = saved
            except Exception:
                pass
        try:
            return ek.preview_training_kit(self.db, self.cfg, opts)
        except Exception as e:
            return {"ok": False, "error": f"生成导出预览失败：{e}"}

    def _api_export_history(self, u):
        """GET /api/export/history?limit=N → 素材集导出历史（只读）。"""
        from . import export_kit as ek

        try:
            limit = int((parse_qs(u.query or "").get("limit") or ["50"])[0])
        except (TypeError, ValueError):
            limit = 50
        try:
            return ek.export_history(self.db, limit)
        except Exception as e:
            return {"ok": False, "error": f"读取导出历史失败：{e}"}

    def _api_site_stats(self, u):
        """GET /api/site-stats?days=0|7|30 → 每任务采集统计（只读聚合）。"""
        from . import site_stats

        try:
            days = int((parse_qs(u.query or "").get("days") or ["0"])[0])
        except (TypeError, ValueError):
            days = 0
        if days not in (0, 7, 30):
            days = 0
        try:
            return site_stats.task_stats(self.db, days)
        except Exception as e:
            return {"ok": False, "error": f"读取站点统计失败：{e}"}

    def _api_cookies_hints(self):
        """GET /api/cookies/hints → 疑似登录失效的任务清单（Cookie 过期提醒）。"""
        from . import site_stats

        try:
            return {"ok": True, "items": site_stats.cookie_expiry_hints(self.db)}
        except Exception as e:
            return {"ok": False, "error": f"读取 Cookie 提醒失败：{e}"}

    def _api_notifications_list(self, u):
        """GET /api/notifications?limit&only_unread → 通知列表 + 未读数。"""
        from . import notify

        q = parse_qs(u.query or "")
        try:
            limit = int((q.get("limit") or ["100"])[0])
        except (TypeError, ValueError):
            limit = 100
        only_unread = str((q.get("only_unread") or [""])[0]).lower() in ("1", "true")
        try:
            return notify.list_all(self.db, limit, only_unread)
        except Exception as e:
            return {"ok": False, "error": f"读取通知失败：{e}"}

    def _api_notifications_read(self, body):
        """POST /api/notifications/read {ids:[...]} 或 {all:true} → 标记已读。"""
        from . import notify

        b = body if isinstance(body, dict) else {}
        all_read = bool(b.get("all"))
        ids = b.get("ids")
        if not isinstance(ids, (list, tuple)):
            ids = None
        if not all_read and not ids:
            return {"ok": False, "error": "请给出要标记已读的通知编号"}
        try:
            return notify.mark_read(self.db, ids, all_read)
        except Exception as e:
            return {"ok": False, "error": f"标记已读失败：{e}"}

    # ---------- V0.0.39：右下角「完全退出」----------
    def _api_app_quit(self):
        """POST /api/app/quit → 请壳程序退出（右下角状态栏那个按钮）。

        ⚠ 只设一个线程安全的事件，**不在 HTTP 线程里碰 Qt**：
          壳（web_shell）用 QTimer 轮询这个事件再走自己的退出链。
          直接在 HTTP 线程里调 app.quit()/close() 会跨线程操作控件，
          实测表现是偶发崩溃或窗口关了进程不退。
        浏览器远程访问没有钩子（进程不在本机界面上），返回中文说明，
        别让用户点了没反应。
        """
        srv = getattr(self.server, "_mrm_webserver", None)
        hook = getattr(srv, "quit_hook", None)
        if hook is None:
            return {"ok": False,
                    "error": "当前是浏览器访问，没有可退出的软件窗口；"
                             "请直接关闭软件窗口或在托盘图标上退出"}
        try:
            hook()
            return {"ok": True}
        except Exception as e:                             # noqa: BLE001
            return {"ok": False, "error": f"退出请求没发出去：{e}"}

    def _api_notifications_clear(self, body):
        """POST /api/notifications/clear {confirm:true} → 清空全部通知。"""
        from . import notify

        if not self._confirmed(body):
            return {"ok": False, "error": "请先确认清空全部通知。"}
        try:
            return notify.clear(self.db)
        except Exception as e:
            return {"ok": False, "error": f"清空通知失败：{e}"}

    # ---------- V0.0.19：界面模式（桌面 / 网页共用 cfg.ui_mode）----------
    def _api_ui_mode_get(self):
        """GET /api/ui/mode → 返回当前生效的界面模式。"""
        mode = str(getattr(self.cfg, "ui_mode", "pro") or "pro")
        return {"ok": True, "mode": "newbie" if mode == "newbie" else "pro"}

    def _api_ui_mode_set(self, body):
        """POST /api/ui/mode → 切换新手 / 老鸟模式并落盘。

        与桌面端读写同一个 ``cfg.ui_mode`` 字段：网页上切换后，桌面
        设置中心的模式勾选项与表单分层同步生效，不另立一套口径。
        """
        mode = str((body or {}).get("mode") or "").strip().lower()
        if mode not in ("newbie", "pro"):
            return {"ok": False,
                    "error": "界面模式仅支持「新手模式」或「老鸟模式」两种取值。"}
        try:
            self.cfg.ui_mode = mode
            from .config import save_config
            save_config(self.cfg)
        except Exception as e:
            return {"ok": False, "error": f"保存界面模式失败：{e}"}
        return {"ok": True, "mode": mode}

    def _api_search_all(self, u):
        """GET /api/search/all?q= → 全局搜索（任务 / 图库 / 日志，只读 LIKE）。

        V0.0.18：口径抽到 ``searchall.search_all``，桌面全局搜索共用同一份，
        两端结果必须一致。只读、参数化、无副作用。
        """
        from . import searchall

        raw = (parse_qs(u.query or "").get("q") or [""])[0]
        try:
            return searchall.search_all(self.db, raw)
        except Exception as e:
            return {"ok": False, "error": f"全局搜索失败：{e}",
                    "tasks": [], "images": [], "logs": []}

    def _api_update_check(self):
        """GET /api/update/check → 程序版本比对（update_check.check 原样透传）。"""
        try:
            from .update_check import check
            r = check(self.cfg)
            return {"ok": True, **{k: r.get(k) for k in
                                   ("has_update", "local", "remote", "url",
                                    "notes", "built", "text", "path")}}
        except Exception as e:
            return {"ok": False, "error": f"检查更新失败：{type(e).__name__}: {e}"}

    # ---------- V0.0.16 · 网页端维护动作（纯逻辑统一在 site_ops） ----------
    @staticmethod
    def _confirmed(body) -> bool:
        """破坏性动作的双重保险：前端 confirm + 请求体必须显式带 true。"""
        return isinstance(body, dict) and body.get("confirm") is True

    def _api_site_data_location(self):
        from . import site_ops as so

        try:
            return so.data_location(self.cfg)
        except Exception as e:
            return {"ok": False, "error": f"读取数据存放位置失败：{e}"}

    def _api_site_cleanup_items(self):
        from . import site_ops as so

        try:
            return {"ok": True, "items": so.cleanup_items()}
        except Exception as e:
            return {"ok": False, "error": f"读取清理清单失败：{e}"}

    def _api_site_logs_export(self):
        """GET /api/site/logs/export → 加密 .mrmx（需求 19：日志属导出信息）。"""
        from . import export_kit as ek
        from . import site_ops as so

        try:
            data = so.audit_log_csv(self.db)
            blob = ek.encrypted(
                data, str(getattr(self.cfg, "export_passphrase", "") or ""))
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            return self._send(200, blob, "application/octet-stream",
                              {"Content-Disposition":
                               f'attachment; filename="mrm_logs_{ts}.mrmx"'})
        except Exception as e:
            return self._json({"ok": False, "error": f"导出日志失败：{e}"}, 500)

    def _api_site_backfill(self, body):
        from . import site_ops as so

        b = body if isinstance(body, dict) else {}
        try:
            res = so.backfill(self.db, self.cfg,
                              b.get("task_id", 0), bool(b.get("re_filter_ai")))
        except Exception as e:
            return {"ok": False, "error": f"回填本地图片失败：{e}"}
        if res.get("ok"):
            try:
                self.db.audit(self._actor(), "backfill", "", str(res.get("added", 0)))
            except Exception:
                pass
        return res

    def _api_site_backup_sync(self, body):
        from . import site_ops as so

        b = body if isinstance(body, dict) else {}
        try:
            res = so.sync_backup(self.db, self.cfg, str(b.get("dest") or ""))
        except Exception as e:
            try:
                from . import notify
                notify.add(self.db, "backup-failed", "备份同步失败",
                           str(e)[:200], link="/maintenance", level="error")
            except Exception:
                pass
            return {"ok": False, "error": f"备份同步失败：{e}"}
        if res.get("ok"):
            try:
                self.db.audit(self._actor(), "backup-sync", res.get("dest", ""))
            except Exception:
                pass
            # V0.0.18：备份同步成功后记通知（失败不影响主流程）
            try:
                from . import notify
                notify.add(self.db, "backup-sync", "备份同步完成",
                           str(res.get("note") or res.get("dest") or "")[:200],
                           link="/maintenance")
            except Exception:
                pass
        else:
            # 同步也可能用返回值报失败（不抛异常），同样要留痕
            try:
                from . import notify
                notify.add(self.db, "backup-failed", "备份同步失败",
                           str(res.get("error") or "")[:200],
                           link="/maintenance", level="error")
            except Exception:
                pass
        return res

    def _api_site_logs_clear(self, body):
        from . import site_ops as so

        if not self._confirmed(body):
            return {"ok": False, "error": "请先确认清空操作日志。"}
        try:
            # 清完不写 audit：否则列表里又多一条，看起来像没清干净。
            res = so.clear_audit_logs(self.db)
        except Exception as e:
            return {"ok": False, "error": f"清空日志失败：{e}"}
        # V0.0.18：清空日志成功后记通知（失败不影响主流程）
        if isinstance(res, dict) and res.get("ok"):
            try:
                from . import notify
                notify.add(self.db, "site-logs-clear", "操作日志已清空",
                           link="/logs")
            except Exception:
                pass
        return res

    def _api_site_quarantine_prune(self, body):
        from . import site_ops as so

        if not self._confirmed(body):
            return {"ok": False, "error": "请先确认清空隔离区。"}
        try:
            res = so.prune_quarantine(self.db)
        except Exception as e:
            return {"ok": False, "error": f"清空隔离区失败：{e}"}
        if res.get("ok") and res.get("deleted"):
            try:
                self.db.audit(self._actor(), "quarantine-prune", "",
                              str(res.get("deleted")))
            except Exception:
                pass
        return res

    def _api_site_cleanup(self, body):
        from . import site_ops as so

        b = body if isinstance(body, dict) else {}
        if not self._confirmed(b):
            return {"ok": False, "error": "请先确认全面清理。"}
        plan = b.get("plan")
        if not isinstance(plan, list) or not plan:
            return {"ok": False, "error": "请至少勾选一项要清理的内容。"}
        try:
            # 清完不写 audit：勾了 audit 项时表已被清空，写回去等于留一条脏行。
            res = so.full_cleanup(self.db, self.cfg, plan)
        except Exception as e:
            return {"ok": False, "error": f"全面清理失败：{e}"}
        # V0.0.18：全面清理成功后记通知（失败不影响主流程）
        if isinstance(res, dict) and res.get("ok"):
            try:
                from . import notify
                notify.add(self.db, "site-cleanup", "全面清理完成",
                           str(res.get("note") or "")[:200], link="/maintenance")
            except Exception:
                pass
        return res

    # ---------- R154-F18 磁盘空间看板（与桌面端同一个 diskspace 模块） ----------

    def _api_diskspace(self, u):
        """R154-F18：各目录占用 + 7 日增长趋势 + 磁盘预警（超阈值中文告警）。

        ⚠ 目录清单与统计口径**一行都不自己写**，全走 ``diskspace`` ——
        桌面端「数据 → 磁盘空间看板…」用的是同一个模块。两端各扫一遍、
        各写一份目录表，用户就会看到「手机上没有 models 这一项」。

        查询参数：``days``：趋势天数（默认 7，钳 1~30）。

        顺带履行「每日一快照」：今天还没记录就扫一遍记上（幂等，同一天
        只有一行）—— 只靠桌面端打开才记的话，只用手机的用户永远只有
        打开过桌面端那几天的柱子。
        """
        try:
            from urllib.parse import parse_qs

            from .diskspace import (
                collect_usage, format_size, headline, load_history,
                snapshot_today_if_missing,
            )
            q = parse_qs(u.query or "")
            try:
                days = int((q.get("days") or ["7"])[0])
            except (TypeError, ValueError):
                days = 7
            usage = snapshot_today_if_missing(self.db, self.cfg)
            free = usage.get("free_bytes")
            return {"ok": True,
                    "dirs": [{"key": d["key"], "name": d["name"],
                              "bytes": d["bytes"], "text": d["text"],
                              "fraction": d["fraction"],
                              "path": str(d["path"] or "")}
                             for d in usage["dirs"]],
                    "total_bytes": usage["total_bytes"],
                    "total_text": usage["total_text"],
                    "free_bytes": free,
                    "free_text": format_size(free) if free is not None else "未知",
                    "threshold_mb": usage["threshold_mb"],
                    "alert": usage["alert"],
                    "headline": headline(usage),
                    "history": load_history(self.db, days)}
        except Exception as e:
            return {"ok": False, "error": f"读取磁盘空间失败：{e}"}

    # ---------- V0.0.17 · 网页端补齐（8 项功能的后端动作） ----------
    # 纪律与上文一致：错误一律完整中文句子，不把英文异常拼给用户；所有导出
    # 走 crypt.encrypt_bytes 加密成 .mrmx；破坏性动作不在本轮新增。

    def _api_image_reveal(self, body):
        """POST /api/image/reveal → 在文件夹中显示图片（安全口径在 site_ops）。"""
        from . import site_ops as so

        b = body if isinstance(body, dict) else {}
        res = so.reveal_image_path(self.db, self.cfg, b.get("id"))
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "无法打开该图片所在文件夹。"}
        # 远程访问就别假装能打开：与 _api_task_reveal 同一句中文提示。
        try:
            host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        except Exception:
            host = ""
        if host and host.strip("[]") not in ("127.0.0.1", "localhost", "::1"):
            return {"ok": False,
                    "error": "这是远程访问：文件夹只能在运行软件的那台电脑上打开。"}
        d = Path(res["dir"])
        try:
            if os.name == "nt":
                os.startfile(str(d))          # 仅 Windows；只在服务端执行
            else:
                return {"ok": False, "error": "当前系统不支持自动打开文件夹。"}
        except OSError:
            return {"ok": False, "error": "打开文件夹失败，请稍后重试。"}
        return {"ok": True, "dir": str(d),
                "note": "已在文件夹中显示图片所在位置。"}

    def _api_diskspace_trend(self, u):
        """GET /api/diskspace/trend → 近 7 日增长数据（与 /api/diskspace 同一口径）。

        没有历史快照时返回明确空态（empty=True + 中文说明），不编造数字。
        """
        from urllib.parse import parse_qs
        from .diskspace import load_history

        try:
            days = int((parse_qs(u.query or "").get("days") or ["7"])[0])
        except (TypeError, ValueError):
            days = 7
        days = max(1, min(30, days))
        try:
            items = load_history(self.db, days)
        except Exception:
            return {"ok": False, "error": "读取增长趋势失败，请稍后重试。"}
        recorded = [x for x in items if x.get("recorded")]
        empty = not recorded
        return {"ok": True, "days": days, "items": items, "empty": empty,
                "note": ("暂无历史快照数据，趋势将在每日记录后呈现。"
                         if empty else "")}

    def _api_logs_stats_export(self):
        """GET /api/logs/stats/export → 日志统计报告，加密为 .mrmx 后下载。

        红线（需求 19）：导出的信息一律加密，绝不出明文 CSV / zip。
        """
        from .crypt import encrypt_bytes
        from . import site_ops as so

        try:
            data = so.audit_log_stats_csv(self.db)
            blob = encrypt_bytes(
                data, str(getattr(self.cfg, "export_passphrase", "") or ""))
        except Exception:
            return self._json(
                {"ok": False, "error": "导出统计报告失败，请稍后重试。"}, 500)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return self._send(200, blob, "application/octet-stream",
                          {"Content-Disposition":
                           f'attachment; filename="mrm_log_stats_{ts}.mrmx"'})

    def _api_web_hotkeys_get(self):
        """GET /api/web/hotkeys → 网页动作键位 + 桌面 F1 说明对应键位。"""
        from .hotkeys import ACTIONS, display, effective

        ov = getattr(self.cfg, "hotkeys", None)
        if not isinstance(ov, dict):
            ov = {}
        eff = effective(ov)
        actions = []
        for a in WEB_ACTIONS:
            cur = str(ov.get(a["id"]) or "")
            if not _web_hotkey_seq_ok(cur):
                cur = a["default"]
            actions.append({"id": a["id"], "label": a["label"],
                            "default": a["default"], "current": cur})
        desktop = [{"group": a["context"], "label": a["label"],
                    "key": display(eff.get(a["id"], a["default"]))}
                   for a in ACTIONS]
        fixed = [
            {"key": "Ctrl+Z", "label": "撤销上一步审核"},
            {"key": "Esc", "label": "灯箱关闭或退出全屏"},
            {"key": "↑↓", "label": "灯箱固定翻页别名"},
            {"key": "F1", "label": "打开桌面端快捷键说明"},
        ]
        # V0.0.21：内置窗口固定键位与桌面 F1 说明同源，逐字一致。
        from .hotkeys import SHELL_FIXED_NOTES, SHELL_MODE_NOTE
        return {"ok": True, "actions": actions, "desktop": desktop,
                "fixed": fixed,
                "shell": [dict(x) for x in SHELL_FIXED_NOTES],
                "shell_note": SHELL_MODE_NOTE,
                "note": "键位配置保存在后端配置中，同一服务的所有浏览器共用。"}

    def _api_web_hotkeys_set(self, body):
        """POST /api/web/hotkeys → 保存网页动作键位覆盖到 cfg.hotkeys。"""
        b = body if isinstance(body, dict) else {}
        hot = b.get("hotkeys")
        if not isinstance(hot, dict):
            return {"ok": False, "error": "键位配置格式不对。"}
        cur = getattr(self.cfg, "hotkeys", None)
        cur = dict(cur) if isinstance(cur, dict) else {}
        for aid, seq in hot.items():
            if aid not in WEB_ACTION_IDS:
                continue
            s = str(seq or "").strip()
            if not _web_hotkey_seq_ok(s):
                return {"ok": False, "error": "键位值不合法，请改用有效组合键。"}
            if s == WEB_HOTKEY_DEFAULTS[aid]:
                cur.pop(aid, None)             # 与默认相同 = 不落覆盖
            else:
                cur[aid] = s
        self.cfg.hotkeys = cur
        try:
            from .config import save_config
            save_config(self.cfg)
        except Exception:
            return {"ok": False, "error": "保存键位失败，请稍后重试。"}
        return {"ok": True, "note": "键位已保存并生效。"}

    def _api_db_master_get(self):
        """GET /api/db/master → 仅返回是否已设置主密码，绝不回显。"""
        try:
            from .config import get_db_master_password
            has = bool(get_db_master_password())
        except Exception:
            has = False
        return {"ok": True, "set": has,
                "encryption_enabled": bool(
                    getattr(self.cfg, "encryption_enabled", False)),
                "note": "主密码仅保存在运行内存中，界面不会回显已保存的密码。"}

    def _api_db_master_set(self, body):
        """POST /api/db/master → 设置数据库主密码（复用 crypt.py 口径）。

        主密码即 .mrmx 加密容器的 passphrase：db.py 的静态加密正是以它调用
        crypt.encrypt_bytes / decrypt_bytes。这里做一次往返自检确认口令可用，
        再注入进程内存（绝不写进 config.json、绝不回显）。
        """
        from . import crypt
        from .config import set_db_master_password

        b = body if isinstance(body, dict) else {}
        pw = str(b.get("password") or "")
        cf = str(b.get("confirm") or "")
        if not pw.strip():
            return {"ok": False, "error": "请填写主密码。"}
        if pw != cf:
            return {"ok": False, "error": "两次输入的主密码不一致，请重新填写。"}
        try:
            probe = crypt.encrypt_bytes(b"mrm-master-check", pw)
            if crypt.decrypt_bytes(probe, pw) != b"mrm-master-check":
                raise ValueError("校验失败")
        except Exception:
            return {"ok": False, "error": "主密码校验失败，请重新填写。"}
        set_db_master_password(pw)
        # 运行中的数据库若已开启静态加密，后续落盘也要用新口令。
        try:
            if getattr(self.db, "_encrypted", False):
                self.db._master_password = pw
        except Exception:
            pass
        return {"ok": True,
                "note": "主密码已设置并在本次运行生效；加密开关与存量数据重新加密将在重启后完成。"}

    # ---- 自定义皮肤（复用 gui/theme.py 的目录与文件名口径）----

    def _api_skins(self):
        """GET /api/skins → 列出内建与已有自定义皮肤。"""
        t = _skin_helpers()
        if t is not None:
            pairs = list(t.list_custom_skins())
            has_css = {k: bool(t.custom_skin_web_css(k)) for k, _ in pairs}
        else:
            pairs = _skins_local_list()
            has_css = {k: bool(_skins_local_css(k)) for k, _ in pairs}
        items = [{"key": k, "label": l, "has_web_css": has_css.get(k, False)}
                 for k, l in pairs]
        builtin = [{"key": k, "label": _WEB_THEME_LABELS[k]}
                   for k in _WEB_THEMES if k in _WEB_THEME_LABELS]
        return {"ok": True, "items": items, "builtin": builtin,
                "note": "自定义皮肤样式文件保存在服务端皮肤目录。"}

    def _api_skins_css(self, u):
        """GET /api/skins/css?key=custom:xxx → 网页端 CSS 正文（已净化）。"""
        from urllib.parse import parse_qs

        key = (parse_qs(u.query or "").get("key") or [""])[0].strip()
        if not key:
            return {"ok": False, "error": "未提供皮肤键名。"}
        t = _skin_helpers()
        if t is not None:
            known = any(k == key for k, _ in t.list_custom_skins())
            css = t.custom_skin_web_css(key)
        else:
            known = any(k == key for k, _ in _skins_local_list())
            css = _skins_local_css(key)
        if not known:
            return {"ok": False, "error": "未找到该自定义皮肤。"}
        return {"ok": True, "key": key, "css": css}

    def _api_skins_upload(self, body):
        """POST /api/skins/upload → 保存自定义 CSS 到皮肤目录。

        三道闸：文件名后缀 .css、名字通过 custom_name_ok（禁路径分隔符）、
        内容无闭合 style / 脚本且不超过 256 KB；最后再比对 resolve 后的父目录，
        杜绝路径穿越。
        """
        b = body if isinstance(body, dict) else {}
        name = str(b.get("name") or "").strip()
        content = b.get("content")
        if not name:
            return {"ok": False, "error": "未提供皮肤名称。"}
        if not name.lower().endswith(".css"):
            return {"ok": False, "error": "皮肤文件名须以 .css 结尾。"}
        stem = name[:-4]
        t = _skin_helpers()
        ok = t.custom_name_ok(stem) if t is not None else _skin_name_ok(stem)
        if not ok:
            return {"ok": False,
                    "error": "皮肤文件名不合法，请仅使用字母、数字、下划线、连字符或中文，并避免路径分隔符。"}
        if not isinstance(content, str) or not content.strip():
            return {"ok": False, "error": "皮肤内容为空，请提供 CSS 文本。"}
        raw = content.encode("utf-8")
        if len(raw) > 256 * 1024:
            return {"ok": False, "error": "皮肤内容超过大小上限（256 KB）。"}
        low = content.lower().replace(" ", "")
        if "</style" in low or "<script" in low or "javascript:" in low:
            return {"ok": False,
                    "error": "皮肤内容包含不允许的标签或脚本，已拒绝保存。"}
        d = Path(t.custom_skins_dir()) if t is not None else _skins_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
            dest = d / (stem + ".css")
            if dest.resolve().parent != d.resolve():
                return {"ok": False, "error": "皮肤路径不合法，已拒绝保存。"}
            dest.write_bytes(raw)
        except OSError:
            return {"ok": False, "error": "保存皮肤失败，请稍后重试。"}
        return {"ok": True, "key": "custom:" + stem,
                "note": "已保存自定义皮肤，可在列表中切换。"}

    # ---------- R79 #41 常用网址（与桌面端同一份配置） ----------

    #: 常用网址的配置字段。⚠ 与桌面端「网址与适配器管理器 → 常用网址」
    #: 用**同一个字段**（AppConfig.saved_urls）：网页上加的桌面端能看见，
    #: 两边各存一份就会互相看不见（「ok 但做了另一件事」的老毛病）。
    _URLS_FIELD = "saved_urls"

    @staticmethod
    def _normalize_saved_url(raw) -> tuple:
        """把用户粘进来的一段文字变成可收藏的网址。

        返回 ``(网址, 错误)``，成功时错误为空串。

        ⚠ 只做**两件**宽容处理，其余一律报中文错、不静默猜：
          ① 首尾空白去掉；② 没写协议时补 ``https://``（用户多半只粘域名）。
        协议不是 http/https（比如 ``javascript:``、``file:``）必须拒：
        存进列表后点开就是在本机上下文里执行，属于自己给自己开门。
        """
        from urllib.parse import urlsplit

        s = str(raw or "").strip()
        if not s:
            return "", "请填写网址"
        # ⚠ 判断「有没有自带协议」必须看 **scheme:** 这个形态，
        #   不能只看有没有 "://"：
        #   `javascript:alert(1)` 没有 "://"，按老写法会被补成
        #   `https://javascript:alert(1)` —— urlsplit 认 scheme=https、
        #   hostname=javascript，于是**一路放行**，
        #   「只支持 http/https」这条防线等于没写。
        #   自带协议的一律原样往下走，交给下面的白名单去拒。
        # ⚠ scheme 字符集里**不含点**：`a.com:8080/x` 是合法的域名带端口，
        #   含点的话它会被误判成 scheme=`a.com` 而遭错杀。
        if not (re.match(r"^[A-Za-z][A-Za-z0-9+_-]*:", s) or s.startswith("//")):
            s = "https://" + s
        u = urlsplit(s)
        if u.scheme not in ("http", "https"):
            return "", "只支持 http 或 https 开头的网址"
        if not u.hostname:
            return "", "网址格式不对：缺少域名"
        return s, ""

    def _api_urls(self):
        try:
            from urllib.parse import urlsplit

            urls = [str(u) for u in
                    (getattr(self.cfg, self._URLS_FIELD, None) or [])]
            items = [{"url": u, "host": urlsplit(u).hostname or u,
                      "idx": i} for i, u in enumerate(urls)]
            return {"ok": True, "items": items,
                    "stats": {"count": len(items)},
                    "next_cursor": None, "has_more": False}
        except Exception as e:
            return {"ok": False, "error": f"读取常用网址失败：{e}"}

    def _api_url_save(self, body):
        url, err = self._normalize_saved_url(body.get("url"))
        if err:
            return {"ok": False, "error": err}
        try:
            from .config import save_config

            urls = [str(u) for u in
                    (getattr(self.cfg, self._URLS_FIELD, None) or [])]
            # ⚠ 去重放在**规范化之后**：`example.com` 与 `https://example.com`
            # 是同一个网址，规范化前比就会存进两条，列表里看着像两个站。
            if url in urls:
                return {"ok": False, "error": "这个网址已经在常用列表里了"}
            urls.append(url)
            setattr(self.cfg, self._URLS_FIELD, urls)
            save_config(self.cfg)
            return {"ok": True, "url": url, "count": len(urls)}
        except Exception as e:
            return {"ok": False, "error": f"保存常用网址失败：{e}"}

    def _api_url_delete(self, body):
        try:
            from .config import save_config

            urls = [str(u) for u in
                    (getattr(self.cfg, self._URLS_FIELD, None) or [])]
            target = str(body.get("url") or "").strip()
            if target:
                if target not in urls:
                    return {"ok": False, "error": "这个网址不在常用列表里"}
                urls = [u for u in urls if u != target]
            elif body.get("idx") is not None:
                i = int(body["idx"])
                if i < 0 or i >= len(urls):
                    return {"ok": False, "error": "这个网址不在常用列表里"}
                urls = [u for j, u in enumerate(urls) if j != i]
            else:
                return {"ok": False, "error": "请选择要删除的网址"}
            setattr(self.cfg, self._URLS_FIELD, urls)
            save_config(self.cfg)
            return {"ok": True, "count": len(urls)}
        except Exception as e:
            return {"ok": False, "error": f"删除常用网址失败：{e}"}

    # ---------- R79 #44 学习样本管理 / 重置 ----------

    def _api_learning_delete(self, body):
        """删单条学习权重（与桌面端「权重明细」用的是同一个 repo 函数）。"""
        try:
            from .repo import delete_preference

            label = str(body.get("label") or "").strip()
            if not label:
                return {"ok": False, "error": "请选择要删除的学习记录"}
            if not delete_preference(self.db, label):
                return {"ok": False, "error": "这条学习记录已经不在了"}
            return {"ok": True, "label": label}
        except Exception as e:
            return {"ok": False, "error": f"删除学习记录失败：{e}"}

    def _api_learning_clear_model_auto(self, body):
        """R174：清掉「模型自动 + 负权」的那一批（网页端与桌面看板同一函数）。

        与「重置学习」的区别（别混成一个按钮）：
          · 重置 = 全清（连用户教了几个月的"喜欢"一起没），代价最大；
          · 本接口 = 只清**机器自己认错攒下来的负权词**，用户手填的排除词
            和正权学习**一条都不动** —— 这是"误标污染黑名单"的专用出口。

        ⚠ 同样是**不可撤销**的删除 ⇒ 必须带 ``confirm``（与 reset 同口径：
        前端那道 confirm 只是提示，后端这道才是真防线）。
        """
        try:
            from .repo import clear_model_auto_negatives

            if not body.get("confirm"):
                return {"ok": False,
                        "error": "清除模型自动词要二次确认，请确认后再点"}
            done = clear_model_auto_negatives(self.db)
            return {"ok": True, "deleted": int(done),
                    "hint": ("没有可清的模型自动负权记录" if not done
                             else "已清除，相关词也已从任务 AI 排除词中摘除")}
        except Exception as e:
            return {"ok": False, "error": f"清除模型自动词失败：{e}"}

    def _api_learning_reset(self, body):
        """重置学习：清空**全局**权重（task_id=0）。

        ⚠ 两道闸，缺一不可：
          ① 必须显式带 ``confirm``：这是**不可撤销**的删除。前端那个
             ``window.confirm`` 只是提示，后端这道才是真防线 —— 少点一次
             就把用户教了几个月的东西清光，是「ok 但做了另一件事」的最坏形态。
          ② 只清 ``task_id=0``：按任务学的行（task_id<>0）语义完全不同，
             一并删掉属于误伤（桌面端 cleanup 对这两类也是各归各的）。
        """
        try:
            if not body.get("confirm"):
                return {"ok": False, "error": "重置学习要二次确认，请确认后再点"}
            before = self.db.query(
                "SELECT COUNT(*) c FROM preferences WHERE task_id=0")[0]["c"]
            self.db.execute("DELETE FROM preferences WHERE task_id=0")
            return {"ok": True, "deleted": int(before)}
        except Exception as e:
            return {"ok": False, "error": f"重置学习失败：{e}"}

    # ---------- E-6 设置写入 ----------
    # ---------- E-6 设置写入 ----------

    #: Web 端允许改的设置项白名单。**刻意不含** web_bind/web_port/password ——
    #: 那三项要重启服务才生效，Web 里改了会给人「改了没反应」的错觉。
    #:
    #: R50 ⚠ **白名单里的每个键都必须在 AppConfig 里真实存在**。
    #: 原来这里混着四个根本不存在的键：
    #:   · `watermark_enabled` —— R50 才补上真字段（之前 setattr 进去没人读）
    #:   · `ai_enabled`—— **同名陷阱**：它是 *TaskConfig* 上的字段，
    #:     不是全局字段。全局那个 R50 起叫 `ai_enabled_global`。
    #:     写 `ai_enabled` 会「改成功」但对全局毫无作用。
    #:   · `auto_organize` / `similarity_threshold` —— 全项目无人读写，纯虚构。
    #: 删掉虚构键 + 补真字段，是同一件事：让"改了有反应"这件事为真。
    #: ⚠ 这条判据必须用「cfg 上有没有这个属性」验证，光看白名单非空没有意义。
    _SETTINGS_WRITABLE = {
        # 皮肤三选（作业单 Web 设置点名项）
        "ui_preset",
        # 2026-10-27：网页皮肤独立于桌面皮肤（用户明确要求）。
        "web_theme",
        # 2026-10-28：静态 / 动态（SMIL 动画）图标总开关
        "ui_animated_icons",
        # 水印：开关 + 不透明度（作业单 Web 设置点名项）
        "watermark_enabled", "watermark_alpha",
        # AI 总开关（作业单 Web 设置点名项）
        "ai_enabled_global",
        "min_width", "min_height", "min_bytes", "max_bytes",
        "ai_gpu", "ai_device",
        # 2026-10-27 设置页扩充（Web 对标桌面）：这些都是"改了不用重启"的项
        "ai_threads", "convert_to_jpg", "rate_plan_enabled",
        "learn_enabled", "blur_sensitive", "review_key_ask",
        "task_done_goto_gallery",
        # #10/#11：失败自动重试 + 自动降频
        "auto_retry", "auto_retry_delay", "auto_slowdown",
        # ===== 2026-10-27 第 1 批（qBittorrent 对标）=====
        # 界面与启动
        "confirm_exit_running", "prevent_sleep", "splash_enabled",
        "resume_on_start", "resume_downloads", "resume_tags", "resume_imports",
        # V0.0.55 批1：定时自动关机（前端初始显示用；改计划走 POST /api/powerdown）
        "auto_shutdown_mode", "auto_shutdown_countdown",
        "auto_shutdown_idle_grace", "auto_shutdown_at", "auto_shutdown_daily",
        # V0.0.27：启动后直接收进托盘（网页端可改，下次启动生效；
        # 桌面版与 SweetWeb 客户端共用同一份配置）
        "tray_start_minimized",
        # 下载与保存
        "silent_add", "clean_part_on_cancel", "subdir_mode",
        "watch_enabled", "watch_dir", "watch_interval_sec",
        "move_on_path_change",
        # 连接与代理
        "proxy_dns", "proxy_tasks_only",
        "ip_filter_enabled", "ip_filter_text",
        # 速度（全局上限 + 内网豁免 + 时段表）
        "speed_limit_kbps", "speed_skip_lan", "rate_plan",
        # 日志
        "log_max_mb", "log_backup_count", "update_manifest_url",
        # Web 安全
        "web_localhost_free", "web_whitelist_nopass",
        "web_login_max_fail", "web_login_ban_min", "web_session_hours",
        "web_frame_deny", "web_host_check", "web_csrf", "web_extra_headers",
        # ===== 2026-10-28 第 2 批 =====
        # 23：HTTPS（三个字段改完要重启 Web 服务才生效，保存时会返回 note）
        "web_https", "web_https_port", "web_https_redirect",
        # 2026-10-28：HTTPS 下给会话 cookie 加 Secure（qBittorrent 同款）
        "web_secure_cookie",
        # 32/33/34：源地址、证书校验、内网地址限制
        "bind_ip", "ssl_verify", "ssl_insecure_hosts", "allow_private_hosts",
        # 21/22：订阅总开关与默认间隔（订阅源本身走 /api/sub/* 那组端点）
        "sub_enabled", "sub_interval_min",
        # 35：慢任务让位
        "slow_yield_enabled", "slow_yield_min",
        # R52：Web 访问策略（作业单：局域网不要密码 + 给 IP 白名单选项）
        # 2026-10-27 用户要求：Web 端也要能改监听端口/账号/密码
        # （以前只有桌面端能改，网页上只能看到"（已设置）"）。
        # ⚠ 端口/监听地址改完要重启服务才生效 —— 保存时返回 note 明确告知，
        #   不假装"已生效"。密码改完**立即**生效（鉴权每次读 cfg）。
        "web_bind", "web_port", "web_user", "web_password",
        "web_lan_open", "web_ip_whitelist",
        # 2026-10-29（第 11 条）：`web_ui_v2` **不再可写** —— 旧版网页已下线，
        # 只剩新版，这个开关点了也不会有任何变化（留着就是骗人）。
        "encryption_enabled",
        # F24：分时段下载限速的**总开关**（时段表本身只在桌面端编辑 ——
        # 网页上排三列表格在手机上没法用，所以这里只给开关 + 只读摘要）。
        "rate_plan_enabled",

        # ── 2026-10-27：#18 设置全面对标桌面端（一次铺开）────────────
        # 采集与下载
        "concurrency", "max_depth", "delay", "timeout", "retry",
        "parallel_tasks", "max_pages",
        # R252：适配器按域名自动匹配（关掉 = 完全回到旧行为）
        "adapter_auto_match",
        "user_agent", "cookie_browser",
        # 保存与整理
        "tidy_after_task",
        "library_dir", "isolate_dir", "review_dir",
        # AI 与审核
        "ai_backend", "ai_api_url", "ai_api_key", "ai_api_model",
        "review_margin", "review_auto_fill",
        "sensitive_words",
        # 主题词
        "global_blacklist",
        # 存储与清理
        "use_recycle", "recycle_keep_days",
        "daily_backup", "backup_keep", "backup_sync_dir",
        # R250：整库备份自动轮换（份数 / 天数 / 总量上限）
        "full_backup_keep", "full_backup_days", "full_backup_max_gb",
        "min_free_mb", "suspicious_bytes", "log_sanitize",
        "compliance_level",
        # Web 访问（反向代理场景）
        "trust_proxy",
    }

    #: **永不回显**的键（读接口一律脱敏成「（已设置）」，写接口照常接收）。
    #: ⚠ 2026-10-27：ai_api_key 与 web_password 同样敏感 —— 在线 API 的 key
    #: 一旦泄漏，别人可以拿你的额度跑；Web 页在局域网是裸奔的，绝不能回显。
    _SETTINGS_SECRETS = {"web_password", "ai_api_key"}

    #: 列表型设置键（前端传「逗号/空格/换行分隔的字符串」，后端拆成 list）。
    _SETTINGS_LIST_FIELDS = {"web_ip_whitelist"}

    #: 白名单里**枚举型**的键 → 合法取值。⚠ 必须在写进去之前校验：
    #: `ui_preset` 是字符串，照原来的"原样 setattr"能把任意文本写进
    #: config.json，而 gui/theme.py 取色时 `preset_info` 只回落不抛 ——
    #: 于是皮肤名变成一个谁都不认识的值，界面配色悄悄退回默认（用户视角：
    #: "我选了皮肤但没变化"）。非法值直接拒收并报中文错。
    # 网页皮肤名单（与前端 THEMES 一一对应；前端另有 data-theme 属性）
    _SETTINGS_ENUMS = {
        "ui_preset": lambda: tuple(_theme_presets()),
        # ⚠ 这里**不能**写 `lambda: tuple(self._WEB_THEMES)`：这个 dict 是在
        # 类体里求值的，lambda 里根本没有 self（实测 500 NameError）。
        # 用模块级常量，类属性也指向它，只有一处定义。
        "web_theme": lambda: tuple(_WEB_THEMES),
        "ai_device": lambda: tuple(_AI_DEVICES),
        # 输出子目录模式：none=平铺 / site=按站点 / task=按任务名 / both=两者
        "subdir_mode": lambda: ("none", "site", "task", "both"),
        # #18：枚举值必须校验 —— 写进一个不存在的后端名，AI 会静默不工作，
        # 用户只看到"AI 好像没反应"，比直接报错难查得多。
        "ai_backend": lambda: tuple(_AI_BACKENDS),
        "compliance_level": lambda: tuple(_compliance_levels()),
    }

    def _api_settings_save(self, body):
        """E-6：设置页改设置（白名单制 + 落盘）。

        R50：白名单里的键**逐个校验在cfg 上真实存在**，且枚举值要合法。
        这两步都必要：缺了会得到「返回 ok 但什么都没变」的结果，
        而这正是 Web 设置页之前的状态。
        """
        from dataclasses import fields as _dc_fields

        real = {f.name for f in _dc_fields(self.cfg)}
        changed = {}
        for k, v in body.items():
            if k not in self._SETTINGS_WRITABLE:
                continue
            if k not in real:
                # 白名单与配置不同步 = 维护漏洞，必须让测试看得见。
                continue
            # 2026-10-27（编号 19）：限速时段表是**结构化的 list[dict]**，
            # 不能走下面「按空白拆成字符串数组」的通用逻辑（那会得到
            # ['[{"start"...}'] 这种垃圾）。统一交给 rateplan.normalize_plan
            # 洗一遍：丢废条、按开始时间排序、条数截断 —— 与桌面端同一口径。
            if k == "rate_plan":
                try:
                    from .rateplan import normalize_plan
                    nv = normalize_plan(v if isinstance(v, (list, tuple)) else [])
                except Exception:
                    return {"ok": False, "error": "限速时段表格式不对"}
                setattr(self.cfg, k, nv)
                changed[k] = nv
                continue
            # 2026-10-28（编号 32）：源地址必须是本机现有地址（填错的表现是
            # 「所有请求都超时」，而错误信息完全看不出是源地址写错了）。
            if k == "bind_ip":
                try:
                    from .nets import validate_bind_ip
                    ok_ip, why = validate_bind_ip(str(v or ""))
                except Exception:
                    ok_ip, why = True, ""
                if not ok_ip:
                    return {"ok": False, "error": why}
                setattr(self.cfg, k, str(v or "").strip())
                changed[k] = str(v or "").strip()
                continue
            # 2026-10-28（编号 23）：HTTPS 端口范围校验。8080 之类已被 HTTP
            # 占用的口在这里拦不住（要等起服务才知道），但"填成 0 或 70000"
            # 这种明显错的必须先挡住。
            if k == "web_https_port":
                try:
                    pv = int(v)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "HTTPS 端口必须是数字"}
                if not (1 <= pv <= 65535):
                    return {"ok": False, "error": "HTTPS 端口要在 1–65535 之间"}
                setattr(self.cfg, k, pv)
                changed[k] = pv
                continue
            if k in ("sub_interval_min", "slow_yield_min"):
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    return {"ok": False, "error": f"{_SETTINGS_LABELS.get(k, k)}必须是整数"}
                if k == "sub_interval_min":
                    iv = max(5, min(iv, 7 * 24 * 60))
                else:
                    iv = max(1, min(iv, 600))
                setattr(self.cfg, k, iv)
                changed[k] = iv
                continue
            # V0.0.40：这两项原来没有钳制 —— 界面给了 0–8 / 0–1 的限制，
            # 但裸 HTTP 接口谁都能塞 999 进去（线程数开成 999 会把机器拖死，
            # 余量给成 5 会让所有图都进人工审核）。后端自己也得拦住。
            if k == "ai_threads":
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "核心数必须是整数"}
                iv = max(0, min(iv, 32))
                setattr(self.cfg, k, iv)
                changed[k] = iv
                continue
            if k == "review_margin":
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "置信度不足的范围必须是数字"}
                fv = max(0.0, min(fv, 1.0))
                setattr(self.cfg, k, fv)
                changed[k] = fv
                continue
            enum_fn = self._SETTINGS_ENUMS.get(k)
            if enum_fn is not None:
                allowed = enum_fn()
                sv = str(v or "")
                if sv not in allowed:
                    return {"ok": False,
                            "error": f"{_SETTINGS_LABELS.get(k, k)}只能是："
                                     f"{'、'.join(allowed)}"}
                setattr(self.cfg, k, sv)
                changed[k] = sv
                continue
            cur = getattr(self.cfg, k, None)
            # 目录类字段是 Path：直接塞字符串会让后面 `cfg.x / "y"` 炸掉。
            # 空串一律拒绝（空目录比"没改"危险得多）。
            if isinstance(cur, Path):
                sv = str(v or "").strip()
                if not sv:
                    return {"ok": False,
                            "error": f"{_SETTINGS_LABELS.get(k, k)}不能是空路径"}
                try:
                    nv = Path(sv)
                except Exception:
                    return {"ok": False,
                            "error": f"{_SETTINGS_LABELS.get(k, k)}不是合法路径"}
                setattr(self.cfg, k, nv)
                changed[k] = str(nv)
                continue
            # R52：列表型（如 web_ip_whitelist）前端传的是分隔字符串，
            # 先拆成 list 再落盘；否则写入的会是一个字符串而非数组。
            # 2026-11-05：global_blacklist / sensitive_words 用专用拆分器——
            # 这两个字段是 CJK 子串匹配，前端常整串粘贴（中文逗号分隔），
            # 按空白+英文逗号拆会留下"词A，词B，词C"这种整串，永不命中。
            if k in ("global_blacklist", "sensitive_words"):
                if isinstance(v, str):
                    nv = _split_word_list(v)
                elif isinstance(v, (list, tuple)):
                    nv = _split_word_list(list(v))
                else:
                    continue
                setattr(self.cfg, k, nv)
                changed[k] = nv
                continue
            if isinstance(cur, list) or k in self._SETTINGS_LIST_FIELDS:
                if isinstance(v, str):
                    nv = [x.strip() for x in re.split(r"[\s,]+", v) if x.strip()]
                elif isinstance(v, (list, tuple)):
                    nv = [str(x).strip() for x in v if str(x).strip()]
                else:
                    continue
                setattr(self.cfg, k, nv)
                changed[k] = nv
                continue
            try:
                if isinstance(cur, bool):
                    nv = _as_bool(v, cur)
                elif isinstance(cur, int):
                    nv = int(v)
                elif isinstance(cur, float):
                    nv = float(v)
                else:
                    nv = v
            except (TypeError, ValueError):
                continue
            setattr(self.cfg, k, nv)
            changed[k] = nv
        # V0.0.40：**设备档位是唯一权威**。老字段 ai_gpu 只是个布尔，
        # 以前界面上它和「推理设备」下拉并排，勾了 / 取消都不一定有用
        # （引擎只在 device=auto 时看它）—— 用户当然觉得"逻辑有问题"。
        # 现在界面只留档位，写配置时把 ai_gpu 同步成与之一致的值，
        # 这样读 ai_gpu 的老代码（引擎、日志、旧配置）不会和界面对不上。
        if "ai_device" in changed:
            _dev = str(getattr(self.cfg, "ai_device", "auto") or "auto").lower()
            try:
                self.cfg.ai_gpu = _dev != "cpu"
            except Exception:                          # noqa: BLE001
                pass
        if not changed:
            return {"ok": False, "error": "没有可保存的设置项"}
        # 2026-11-05 安全修复（P0，与桌面端对话框同一道闸）：非回环绑定 +
        # 空密码 = 同网段免密接管全部数据接口。改 web_bind / web_password /
        # web_lan_open 时校验，命中就**整体拒存**（不写盘、不半保存），返回
        # 中文错误让前端弹出来。loopback = 127.x / localhost / ::1 / ::。
        def _is_loopback(h: str) -> bool:
            h = (h or "").strip().lower()
            if h in ("localhost", "::1", "::"):
                return True
            return h.startswith("127.")
        _nb = str(getattr(self.cfg, "web_bind", "127.0.0.1") or "127.0.0.1")
        _np = str(getattr(self.cfg, "web_password", "") or "")
        if _nb and not _is_loopback(_nb) and not _np.strip() \
                and bool(getattr(self.cfg, "web_lan_open", False)):
            # 只回滚本轮改动的这两个键，其余已 setattr 的项照旧落盘
            if "web_bind" in changed:
                setattr(self.cfg, "web_bind", "127.0.0.1")
            return {"ok": False,
                    "error": "监听地址 " + _nb + " 对网络内所有设备开放，但未设置"
                             "密码 —— 请先设置一个非空密码，或把监听地址改回 "
                             "127.0.0.1（仅本机可访问）。"}
        # 皮肤改了要立刻落盘，且**告知调用方需要重开才全生效**：
        # 桌面端的主窗口已经建好了控件，网页上换皮肤不可能即时改它。
        try:
            from .config import save_config
            save_config(self.cfg)
        except Exception as e:
            return {"ok": False, "error": f"保存设置失败：{e}"}
        # V0.0.55 批1：改动里有关机设置 → 让内存计划跟着配置走。
        # 配置是每日定点的唯一事实源（前端直接改下拉也能生效）。
        if any(str(k).startswith("auto_shutdown_") for k in changed):
            try:
                from . import powerdown
                powerdown.apply_cfg(self.cfg, getattr(self.server, "queue", None))
            except Exception:                            # noqa: BLE001
                pass
        out = {"ok": True, "changed": changed}
        # V0.0.23：设置改动以前**完全不记日志** —— 用户翻日志只能看到「保存任务」
        # 那几条，自然会问「我改的设置去哪了」。这里记一条，详情写清改了哪几项。
        try:
            _cn = [_SETTINGS_LABELS.get(k, k) for k in changed]
            self.db.audit(self._actor(), "settings_save",
                          "、".join(_cn[:3]) + ("…" if len(_cn) > 3 else ""),
                          f"共 {len(_cn)} 项：" + "、".join(_cn)[:220])
        except Exception:                                  # noqa: BLE001
            pass
        _notes = []
        if "ui_preset" in changed:
            _notes.append("皮肤已保存，重开软件后桌面端生效；网页刷新即可看到")
        # 2026-10-27：Web 端现在能改监听地址/端口/密码了，必须说清哪些要重启，
        # 否则用户改完端口发现没生效会以为坏了。
        if {"web_bind", "web_port"} & set(changed):
            _notes.append("监听地址/端口已保存，**重启软件（或容器）后生效**")
        if "web_password" in changed:
            _notes.append("密码已保存，立即生效（当前会话不受影响）")
        if {"library_dir", "isolate_dir", "review_dir"} & set(changed):
            _notes.append("目录已保存；对**之后新下载**的图生效，已有文件不会自动搬")
        if "ai_api_key" in changed:
            _notes.append("API Key 已保存，下次推理生效")
        if "compliance_level" in changed:
            _notes.append("合规级别已保存，下一个任务生效")
        # 2026-10-28（编号 23）：HTTPS 三个字段改完必须重启 Web 服务才生效
        # —— 不能在网页上"顺手重启自己"，那会把当前连接一起掐断。
        if {"web_https", "web_https_port", "web_https_redirect"} & set(changed):
            _notes.append("HTTPS 相关设置已保存，**重启软件后生效**"
                          "（第一次启用会自动生成本机自签证书）")
        if "bind_ip" in changed:
            _notes.append("源地址已保存，下一个任务生效（走代理时该设置不生效）")
        if "ssl_verify" in changed:
            _notes.append("证书校验已保存，下一个任务生效")
        if "sub_enabled" in changed:
            _notes.append("订阅开关已保存，订阅服务按新状态运行")
        if "slow_yield_enabled" in changed:
            _notes.append("慢任务让位已保存，队列下一分钟起按新设置判断")
        if _notes:
            out["note"] = "；".join(_notes)
        return out


class _WebHTTPServer(ThreadingHTTPServer):
    """R152-E1：Web 服务的线程闸。

    ThreadingHTTPServer 默认「一条连接一条线程、来者不拒」：idle 连接
    （挂着不关的 socket）能把线程数堆到上千，把机器拖死。两道闸：
      · ``max_threads``——并发处理线程上限，超载的连接礼貌回 503 再关，
        不挂着占线程；
      · ``timeout = 30``（见 _Handler）——单连接 30s 无数据自动断。
    """

    daemon_threads = True
    max_threads = 64

    def __init__(self, *a, **kw):
        self._slots = threading.BoundedSemaphore(self.max_threads)
        # R152-E3：登录限流状态（每个服务实例一份，重启即忘）。
        self._login_fails: dict[str, dict] = {}
        self._login_lock = threading.Lock()
        # R154-F20：扫码登录签发限流（每 IP 每分钟最多 _QR_ISSUE_MAX 次，
        # 防「刷 token → 桌面端弹窗轰炸」）。状态挂实例（R152-E3 口径）。
        self._qr_issued: dict[str, list] = {}
        self._qr_lock = threading.Lock()
        super().__init__(*a, **kw)

    def process_request(self, request, client_address):
        # 拿不到槽位就别起线程：回一个 503 并关连接（BaseHTTPRequestHandler
        # 的 close_connection 语义这里用不上——连接还没进处理器）。
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n"
                    b"Content-Length: 0\r\n"
                    b"Connection: close\r\n\r\n")
            except Exception:
                pass
            try:
                request.close()
            except Exception:
                pass
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            # 线程没起来（例如系统线程数耗尽）——槽位得还回去，
            # 否则泄漏几个之后服务就永远满负载。
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class WebServer:
    """把 Web 端跑在后台线程。开关关着就完全不启动（qBittorrent 的做法）。"""

    def __init__(self, cfg, db, queue=None):
        self.cfg = cfg
        # R161：登记生效配置 + 把环境变量主密码注入内存（Web 无交互界面）。
        # 加密开启但无主密码 -> 调用方建库会抛 EncryptionLocked，由 headless 拦截。
        from .config import set_active_config, set_db_master_password
        set_active_config(cfg)
        set_db_master_password(os.environ.get("MRM_DB_PASSWORD", "") or "")
        self.db = db
        self.queue = queue
        # R149-B：用 cfg 校准全局「学习」开关（桌面与 Web 同进程线程，共用同一
        # 份模块级标志；这里再校准一次，保证 Web 侧 learn 调用也走同一开关）。
        try:
            from .classify.preference import set_learning_enabled
            set_learning_enabled(bool(getattr(cfg, "learn_enabled", True)))
        except Exception:
            pass
        # R149-A：用 cfg 校准下载代理（空=走系统/环境变量代理或直连）。
        try:
            from .models_lib import set_download_proxy
            set_download_proxy(getattr(cfg, "proxy", None))
        except Exception:
            pass
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # 编号 23：HTTPS 是**第二个监听**（与 HTTP 并存），单独一份句柄/线程。
        self._httpsd: ThreadingHTTPServer | None = None
        self._https_thread: threading.Thread | None = None
        self.https_port = 0
        self.https_error = ""
        # R154-F20：扫码登录授权钩子（HTTP 线程 → 主线程信号）。
        # main_window 注入 self.qr_login_requested.emit；headless 不注入。
        self.qr_hook = None
        # V0.0.39：壳窗口注入「请退出」钩子（只 set 一个 threading.Event，
        # Qt 侧自己轮询后走退出链）。浏览器远程访问时保持 None。
        self.quit_hook = None
        # R169 第 3 条：学习数据维护（老管线下进来的碎片词清扫 + 时间衰减）。
        # 挂在这里而不是建库时：Web 与桌面各自都会走一遍本构造，覆盖面最全；
        # 且它是"每天一次"的慢活，不需要秒级精度（详细口径见
        # repo.run_learning_maintenance）。
        try:
            from .repo import run_learning_maintenance
            run_learning_maintenance(self.db)
        except Exception:
            pass        # 维护失败绝不能让服务起不来

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def port(self) -> int:
        """配置端口。**0 表示「让OS 选一个空闲端口」**。

        ⚠ 这里的坑：以前写的是 ``int(cfg.web_port or DEFAULT_PORT)``，
        而 ``0 or8080`` = 8080 —— 于是「配 0 让系统自动分配」会被悄悄变成
        固定 8080，测试就会去抢用户真实服务在用的端口。
        所以必须先判 None/空，再单独把 0 当「自动」的哨兵值。
        """
        raw = getattr(self.cfg, "web_port", None)
        if raw is None or raw == "":
            return DEFAULT_PORT
        try:
            return int(raw)
        except (TypeError, ValueError):
            return DEFAULT_PORT

    @property
    def url(self) -> str:
        host = str(getattr(self.cfg, "web_bind", "127.0.0.1") or "127.0.0.1")
        shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        return f"http://{shown}:{self.port}/"

    @property
    def https_url(self) -> str:
        """编号 23：HTTPS 地址（没启用时返回空串，界面据此判断显不显示）。"""
        if not self.https_port:
            return ""
        host = str(getattr(self.cfg, "web_bind", "127.0.0.1") or "127.0.0.1")
        shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        return f"https://{shown}:{self.https_port}/"

    def _start_https(self) -> str:
        """编号 23：起第二个监听（HTTPS）。返回**给用户看的中文说明**。

        ⚠ 这里的失败**绝不连累 HTTP**：证书生成不了、端口被占、SSL 握手配错，
        最坏结果都只是"加密那口没起来，原来的 HTTP 还能用"。用户明确要求过
        「网络/代理异常绝不硬停任务」——Web 服务同理。
        """
        try:
            from .tls import make_ssl_context
        except Exception as ex:                          # noqa: BLE001
            self.https_error = f"证书组件不可用：{ex}"
            return f"  （HTTPS 未启用：{self.https_error}；HTTP 照常可用）"
        try:
            data_dir = Path(str(getattr(self.cfg, "db_path", ""))).parent / "tls"
        except Exception:
            data_dir = Path("tls")
        extra = []
        host0 = str(getattr(self.cfg, "web_bind", "") or "")
        if host0 and host0 not in ("0.0.0.0", "::"):
            extra.append(host0)
        ctx, msg = make_ssl_context(data_dir, extra)
        if ctx is None:
            self.https_error = msg
            return f"  （HTTPS 未启用：{msg}；HTTP 照常可用）"
        try:
            port = int(getattr(self.cfg, "web_https_port", 8443) or 8443)
        except (TypeError, ValueError):
            port = 8443
        if port == self.port:
            # ⚠ 两个协议不能共用一个端口（HTTP 与 TLS 在同一 socket 上没法区分）。
            # Windows 的 SO_REUSEADDR 还会让第二次 bind"看起来成功"，结果是
            # 两个监听抢连接、握手随机失败 —— 必须在起之前就拦住。
            self.https_error = f"HTTPS 端口不能和 HTTP 端口相同（都是 {port}）"
            return f"  （HTTPS 未启用：{self.https_error}；请换一个端口，例如 8443）"
        host = str(getattr(self.cfg, "web_bind", "127.0.0.1") or "127.0.0.1")
        try:
            httpd = _WebHTTPServer((host, port), _Handler)
        except OSError as e:
            self.https_error = f"HTTPS 端口 {port} 起不来：{e}"
            return (f"  （HTTPS 未启用：{self.https_error}；"
                    f"换一个端口或先关掉占用它的程序）")
        try:
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        except Exception as e:                           # noqa: BLE001
            # 证书能加载但握手配置不对：关掉这个监听，别留个半死的口
            try:
                httpd.server_close()
            except Exception:
                pass
            self.https_error = f"SSL 配置失败：{type(e).__name__}: {e}"
            return f"  （HTTPS 未启用：{self.https_error}；HTTP 照常可用）"
        httpd.cfg = self.cfg        # type: ignore[attr-defined]
        httpd.db = self.db          # type: ignore[attr-defined]
        httpd.queue = self.queue    # type: ignore[attr-defined]
        httpd.qr_request_hook = self.qr_hook
        httpd._mrm_webserver = self
        httpd.daemon_threads = True
        self._httpsd = httpd
        self.https_port = int(httpd.server_address[1])
        self._https_thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.5},
            daemon=True, name="mrm-web-tls")
        self._https_thread.start()
        self.https_error = ""
        return (f"  （HTTPS 已启用：{self.https_url}"
                f" —— 自签证书浏览器会提示「不安全」，选择「继续访问/高级→继续」即可）")

    def start(self) -> tuple[bool, str]:
        auto_pw = ""
        if self._httpd is not None:
            return True, self.url + auto_pw
        pw = str(getattr(self.cfg, "web_password", "") or "")
        lan_open = bool(getattr(self.cfg, "web_lan_open", False))
        if not pw:
            # ---- R157：空密码 = 免密模式，**不生成、不保存**任何密码 ----
            # 旧行为（2026-10-07 起）：没密码且没开局域网免密时，
            # `secrets.token_hex(16)` 生成一个随机密码并 `save_config` 持久化。
            # 用户从不知情，重启后配置里已经躺着一个随机密码 ⇒ 再进网页被
            # 要密码，而用户从没设过、也不知道是多少 ⇒ **永久锁在门外**
            # （用户真机报告原话：「开启 Web 功能、没设密码 → 关软件 → 重开 →
            # 再进网页就要密码了，但关软件前根本没设过密码」）。
            # 用户 2026-10-19 明确裁决：空密码就是要免密，不自动生成。
            # ⚠ 免密范围仍受 `_auth_ok()` 的 R52/R141/R58 契约约束
            #   （本机/局域网/白名单放行；公网客户端仍拦，见该函数注释）。
            if not lan_open:
                auto_pw = ("  （免密模式：密码为空时直接访问；"
                           "局域网免密已关闭，当前仅本机及 IP 白名单可直接访问）")
            else:
                auto_pw = "  （免密模式：密码为空时直接访问，同网段设备无需密码）"
        # 缺前端资产就别起了：起一个只能返回 404 的 Web 服务毫无意义，
        # 还会让用户以为功能坏了。报错直接说清是打包漏了资源。
        if not (web_assets_dir() / "index.v2.html").is_file():
            return False, ("Web 前端资源缺失（web/index.v2.html 找不到）。"
                           "从源码运行请确认 media_resource_manager/web/ 存在；"
                           "打包版请确认 spec 的 datas 里带了 web/。")
        host = str(getattr(self.cfg, "web_bind", "127.0.0.1") or "127.0.0.1")
        try:
            httpd = _WebHTTPServer((host, self.port), _Handler)
        except OSError as e:
            return False, f"端口 {self.port} 起不来：{e}"
        # 端口写 0（让 OS 选）时，把**实际绑到的**端口回写给 cfg，
        # 这样测试和「自动分配」场景都能从 cfg.web_port 读到真实端口。
        # 不回写的话 port 属性永远返回 0，调用方拿到的 URL 会是 http://host:0/。
        try:
            self.cfg.web_port = httpd.server_address[1]
        except Exception:
            pass
        httpd.cfg = self.cfg        # type: ignore[attr-defined]
        httpd.db = self.db          # type: ignore[attr-defined]
        httpd.queue = self.queue    # type: ignore[attr-defined]
        httpd.qr_request_hook = self.qr_hook  # R154-F20
        httpd._mrm_webserver = self           # 编号 23：处理器要读 HTTPS 实况
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        kwargs={"poll_interval": 0.5},
                                        daemon=True, name="mrm-web")
        self._thread.start()
        self._start_growth_maintenance()
        # 编号 23：HTTPS（失败只影响加密那口，不影响 HTTP）
        if bool(getattr(self.cfg, "web_https", False)):
            auto_pw += self._start_https()
        return True, self.url + auto_pw

    def _start_growth_maintenance(self, interval: float = 86400.0) -> None:
        """R247：长期运行的数据增长控制（起来跑一次，之后每 24 小时一次）。

        为什么挂在 ``start()`` 而不是构造里：测试会建很多 WebServer，
        构造函数里做 DELETE 会拖慢整套用例；而且服务没起来时清理没有意义。

        清理目标见 :mod:`media_resource_manager.data_growth` —— 审计日志、
        页面账本、失败网址、磁盘快照四张只增不减的表。

        ⚠ 线程是 daemon 且异常全吞：清不掉旧记录是小事，清理线程把服务
          拖死是大事。``stop()`` 会置事件，最多等一个 poll 周期就退出。
        """
        if getattr(self, "_growth_thread", None) is not None:
            return
        self._growth_stop = threading.Event()

        def _loop():
            while True:
                try:
                    from .data_growth import prune_growth
                    stat = prune_growth(self.db, self.cfg)
                    if any(stat.values()):
                        try:
                            print("[mrm] 数据清理：%s" % stat, flush=True)
                        except Exception:
                            pass
                except Exception:
                    pass
                if self._growth_stop.wait(interval):
                    break

        self._growth_thread = threading.Thread(
            target=_loop, daemon=True, name="mrm-growth")
        self._growth_thread.start()

    def stop(self):
        try:
            ev = getattr(self, "_growth_stop", None)
            if ev is not None:
                ev.set()
        except Exception:
            pass
        # 编号 23：HTTPS 那口一起停（只停一路会留下半个服务，端口占着不放）
        if self._httpsd is not None:
            try:
                self._httpsd.shutdown()
                self._httpsd.server_close()
            except Exception:
                pass
            t, self._https_thread = self._https_thread, None
            if t is not None and t.is_alive():
                t.join(timeout=3.0)
            self._httpsd = None
            self.https_port = 0
        if self._httpd is None:
            return
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass
        # R50：`shutdown()` 只让serve_forever 循环退出，**不保证线程已经
        # 真的结束**。原来这里直接把 `_thread` 丢掉引用就算完事，于是
        # 「服务已停」的此刻线程可能还活着 —— 正是 MEMORY 里 R37 那类
        # 崩溃的成因之一（活线程跨阶段持有 loop/控件，堆损坏、junit 不生成）。
        # 所以这里显式 join，给一个上限免得在异常态下永久卡死。
        t, self._thread = self._thread, None
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
        self._httpd = None