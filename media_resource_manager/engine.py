"""Automatic crawl pipeline orchestrator (the "fully automatic" engine).

Flow per task:
  multi-level discovery -> concurrent download
  -> exact(sha256)/perceptual(phash) dedup -> blacklist filter
  -> local classifier tagging + preference scoring -> report.

2026-10-26：合规门禁整层删除（个人自用软件，用户多次明确要求）——
不再有授权检查/robots 阻断/permission basis；R186 当时只加了 off 分支
没删干净，本次连根拔。

Every fetch honours robots.txt and pauses on 401/403/429 (no bypass).
Destructive moves go to the isolation dir and are audit-logged.
"""
from __future__ import annotations

import asyncio
import json
import os
import concurrent.futures
import re
import shutil
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .config import AppConfig, load_config
from .crawler.adapter import AdapterRunner
from .crawler.discovery import Discovery
from .crawler.fetcher import Fetcher
from .crawler.rules import ADAPTER_NAME_OK, load_adapter
from .crawler.site_entries import ua_override_allowed
from .cookies import cookie_for_task, merge_cookies
from .db import Database
from .dedup.hash import hamming
from .downloader.downloader import Downloader
from .models import CrawlResult, ImageRecord, PageType
from .repo import find_duplicate_sha, get_preferences, upsert_image
from .utils.url import domain_of, normalize_url
from .logutil import get_logger
from . import label_levels as _lvl
from . import find_terms as _find
from .review import reasons as _reasons

LOG = get_logger("mrm.engine")


def _level_labels(task) -> tuple[list[str], list[str], list[str]]:
    """把任务里的主题词按 一级 / 二级 / 三级 拆出来（过滤空词）。

    老任务没有 l2/l3 字段 → 只有一级，行为跟 r29 之前完全一致。
    2026-10-28：主题词已支持无限分级，这里只取前三层 —— 保留它是因为
    老测试与少量老调用点按三元组解包；真正判定走 ``label_levels.walk_levels``。
    """
    lv = _lvl.levels_of(task)

    def at(i):
        return list(lv[i]) if len(lv) > i else []

    return at(0), at(1), at(2)


def _best_score(score_of: dict, raw_words) -> tuple[str, float]:
    """一个主题词的「最好的分数」：原词 + 它的中英翻译候选里取最大。

    候选词不在模型 top-K（score_of）里就是「没分」，不是「不匹配」——所以这里
    返回 0.0，让调用方按「不知道」处理（送人工看一眼），绝不据此判不命中。
    """
    best_word, best = "", 0.0
    try:
        from .classify.labels_zh import expand_labels
    except Exception:
        def expand_labels(ws):
            return list(ws)
    for w in raw_words or []:
        for cand in expand_labels([w]):
            s = score_of.get(cand)
            if s is not None:
                try:
                    s = float(s)
                except Exception:
                    continue
                if s > best:
                    best, best_word = s, w
    return best_word, best


def resolve_max_pages(task=None, rule=None, cfg=None) -> int:
    """本任务最多抓多少页面（0=不限），按 任务级 > 规则级 > 全局默认 取值。

    r29 前只有一个全局「每任务最大抓取页数」，改一次影响所有任务，还被
    同名的规则字段（`rule.max_pages`）盖住，用户以为设了没生效。现在任务
    级优先；写成纯函数是为了能脱离引擎单测优先级。
    """
    for src in (task, rule, cfg):
        try:
            v = int(getattr(src, "max_pages", 0) or 0)
        except Exception:
            v = 0
        if v > 0:
            return v
    return 0


def _task_size_limit(task_val, cfg_val) -> int:
    """【R117 P1-4】图片最小宽/高/字节：任务级 > 全局（0=不限），取正整数。

    纯函数（同 resolve_max_pages 的理由）：脱离引擎就能单测优先级。
    ⚠ 与 ``resolve_max_pages`` 不同的是这里**只比任务级与全局两级** ——
      规则级没有对应字段（crawler/rules.py 里没定义），不要臆造第三级。
    ⚠ 非正数一律当「不限」：用户填 0 / 负数 / 脏数据都不能变成
      "把所有图都过滤掉"或"传给下游一个垃圾阈值"。
    """
    for v in (task_val, cfg_val):
        try:
            n = int(v or 0)
        except Exception:
            n = 0
        if n > 0:
            return n
    return 0


@dataclass
class RunReport:
    task_id: int
    pages: int = 0
    images_found: int = 0
    downloaded: int = 0
    skipped_exists: int = 0
    failed: int = 0
    dedup: int = 0
    blacklist_hit: int = 0
    keyword_filtered: int = 0
    quarantined: int = 0
    ai_filtered: int = 0
    ai_excluded: int = 0
    ai_tagged: int = 0
    ai_review: int = 0            # 软件置信度不足、进了审核队列的
    hours_skipped: int = 0        # #22 因不在允许时段而挂起未抓的
    blurred: int = 0              # #35 入库前做了本地模糊/打码的
    low_disk: bool = False        # #54 因磁盘剩余空间不足而中止
    low_disk_detail: str = ""     # #54 具体剩余/阈值信息
    pages_resumed: int = 0        # F9：本轮因上轮已抓过而跳过的页数（断点续采账）
    bytes_downloaded: int = 0
    elapsed: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def speed_bytes_per_sec(self) -> float:
        return self.bytes_downloaded / self.elapsed if self.elapsed > 0 else 0.0


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".tiff"}


def _source_from_sidecar(path: Path, fallback: str) -> str:
    """Recover the original remote URL from gallery-dl's metadata sidecar."""
    sidecar = Path(str(path) + ".json")
    if not sidecar.exists():
        return f"{fallback}#{path.name}"
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception:
        return f"{fallback}#{path.name}"

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and v.startswith(("http://", "https://")):
                    return v
                r = walk(v)
                if r:
                    return r
        elif isinstance(node, list):
            for x in node:
                r = walk(x)
                if r:
                    return r
        return None

    return walk(data) or f"{fallback}#{path.name}"


def _sidecar_words(path: Path) -> list[str]:
    """读 gallery-dl 留下的说明文件，把页面的标题 / 标签 / 描述文字取出来。

    V0.0.6：以前快路径只拿图片网址去跟黑名单比，而图站的文件名是一串
    数字哈希，写满「灌篮高手」的是页面标签和标题 —— 结果黑名单一个都
    拦不住。现在把这些文字也并进下载前的比对。读不到就返回空列表，
    绝不影响采集。
    """
    try:
        data = json.loads(Path(str(path) + ".json").read_text(encoding="utf-8"))
    except Exception:
        return []
    keys = {"tags", "title", "description", "caption", "category",
            "artist", "character", "copyright", "name", "subject",
            "tag_string", "content"}
    out: list[str] = []

    def walk(node, key: str = ""):
        if len(out) >= 200:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k))
        elif isinstance(node, list):
            for x in node:
                walk(x, key)
        elif isinstance(node, str) and node and len(node) < 200:
            if not key or key.lower() in keys:
                out.append(node)

    try:
        walk(data)
    except Exception:
        pass
    return out


def _album_of(url: str) -> str:
    """单个文件夹名：取 URL 路径里文件名前的那一段（不再层层嵌套）。"""
    parts = [p for p in Path(urlsplit(url).path).parts if p]
    if len(parts) >= 2:
        return parts[-2]
    return "root"


# 黑名单里表示「黑白照/灰度」的关键词（用于内容级检测，而非文字匹配）
_GRAYSCALE_KEYWORDS = (
    "黑白照", "黑白照片", "黑白图片", "黑白", "灰度", "灰阶",
    "bw", "b&w", "black and white", "blackwhite", "monochrome",
    "grayscale", "gray scale", "grey", "gray",
)


def _any_grayscale_keyword(blacklist: list[str]) -> bool:
    """黑名单里是否含有「灰度」类关键词（决定要不要对图片做内容级检测）。"""
    for rule in blacklist:
        rl = (rule or "").strip().lower()
        if not rl:
            continue
        if rl in _GRAYSCALE_KEYWORDS:
            return True
        if any(tok in rl for tok in ("黑白", "灰度", "灰阶", "gray", "grey",
                                     "mono", "bw")):
            return True
    return False


# ── R152-C6：黑名单「任务词 ∪ 全局词」的唯一合并点 ───────────────────
#
# 需求 16 的「不喜欢 → 全局黑名单」要真生效，靠的就是这个合并以前**只在
# Engine._blacklist_rules 里存在**，而快路径（gallery-dl 落盘那一支）、
# 追溯应用、本地回填这三处各拿 `task.blacklist` 自己单干 ⇒
# 用户勾了"全局"，下一任务照样把这类图全下回来（三个过滤点漏了两个半）。
def merged_blacklist(task, cfg=None) -> list[str]:
    """「别找什么」全量并表：任务黑名单 + 全局黑名单 + AI 排除词 + 拉黑学到的词。

    V0.0.6 补两处：
      · 以前漏了 task.ai_blacklist，用户选择「不喜欢 → 加排除」学到的词
        在这几个过滤点一个都不生效（只有下载前那两处靠 find_terms 兜住了）；
      · 用户在任务里一次粘贴「词1，词2，词3」这种长串时，整串被当成
        一个词，永远不可能出现在网址里 —— 和全局黑名单一样先拆开。
    """
    _cfg = cfg
    if _cfg is None:
        try:
            from .config import get_active_config as _gac
            _cfg = _gac()
        except Exception:
            _cfg = None
    raw = (list(getattr(task, "blacklist", None) or [])
           + list(getattr(_cfg, "global_blacklist", None) or [])
           + list(getattr(task, "ai_blacklist", None) or []))
    try:
        from .config import split_blacklist_words
        out: list[str] = []
        for x in raw:
            s = str(x or "").strip()
            if not s:
                continue
            # 正则形态 /xxx/ 里可能带逗号，别被拆坏
            pieces = ([s] if s.startswith("/") and s.endswith("/")
                      else split_blacklist_words([s]))
            for w in pieces:
                if w and w not in out:
                    out.append(w)
        return out
    except Exception:
        return [str(x).strip() for x in raw if str(x).strip()]


# ── R152-C3：worker 的收工哨兵 ────────────────────────────────────
#
# discovery 无论怎么死（被取消 / 自己抛异常），都必须给每个 worker 发一条
# None 哨兵并等它收工 —— 原代码只捕 CancelledError，别的异常直接穿出去，
# 没人发哨兵 ⇒ worker 永远卡在 `queue.get()` 上； asyncio 里这叫泄漏的
# task：线程不能退出、队列也没人消费，下一次再跑就是两套 worker 抢同一批图。
async def drain_workers(workers, queue) -> None:
    """给每个 worker 发停工哨兵，并等它们全部收工（**任何情况下**都要走）。"""
    for _ in workers:
        try:
            queue.put_nowait(None)
        except Exception:
            pass
    try:
        await asyncio.gather(*workers, return_exceptions=True)
    except asyncio.CancelledError:
        # 已经在取消流程里（用户的「停止」）：哨兵发到了，worker 自己会退，
        # 这里不能把取消信号吞掉，也不许把它抛回去打乱外层状态机。
        pass


def _hit_blacklist(url: str, tags: list[str], blacklist: list[str],
                   path: Optional[str] = None) -> bool:
    """黑名单**精确**匹配（精度优先，宁可漏放不可误杀）：

    - ``/正则/`` 或含 ``*`` 的当正则处理；
    - 纯 ASCII 词要求**整词边界**（避免 ``cat`` 命中 ``category``）；
    - 含 CJK / 非字母数字的当**字符子串**（CJK 没有空格，子串即词义）；
    - 匹配对象 = URL + 关键字标签。下载前(省流量)和下载后(入库前)都会用。
    - **内容级**（path 给定时）：黑名单含「黑白照/灰度」类词且图片确为灰度 →
      命中。文字黑名单对 B&W 图无效（URL/标签里没"黑白照"字样），必须看图本身。
    """
    blob = (url + " " + " ".join(tags)).lower()
    for rule in blacklist:
        r = rule.strip()
        if not r:
            continue
        rlow = r.lower()
        if (r.startswith("/") and r.endswith("/")) or ("*" in r):
            import re
            try:
                # V0.0.6：以前拿**没小写**的原规则去搜已经全小写的文字，
                # 用户填 Doraemon* 或 /SlamDunk/ 永远不命中、静默失效。
                pat = rlow.strip("/").replace("*", ".*")
                if not pat:
                    # V0.0.78：规则 "//" strip 后是空串，re.search("", blob)
                    # 恒真 → 全部图片被判黑名单命中。空正则直接跳过。
                    continue
                if re.search(pat, blob, re.IGNORECASE):
                    return True
            except re.error as e:
                LOG.warning("黑名单正则写错了，这条规则已跳过：%s（%s）", r, e)
            continue
        if rlow.isascii() and any(c.isalnum() for c in rlow):
            # ASCII 词：整词边界，避免 cat 误中 category
            import re
            if re.search(rf"(?<![a-z0-9]){re.escape(rlow)}(?![a-z0-9])", blob):
                return True
        else:
            # CJK 或非字母数字：直接子串
            if rlow in blob:
                return True
    # 内容级：灰度关键词 + 图片本身确为黑白
    if path and _any_grayscale_keyword(blacklist):
        try:
            from .dedup.hash import is_grayscale
            if is_grayscale(Path(path)):
                return True
        except Exception:
            pass
    return False


class Engine:
    def __init__(self, cfg: AppConfig, db: Database,
                 fetcher_factory=None, model_dir=None):
        self.cfg = cfg
        self.db = db
        # fetcher_factory(cfg, proxy) -> Fetcher; allows injecting a fake in tests.
        self.fetcher_factory = fetcher_factory
        # model_dir lets tests / portable installs point at a specific CLIP dir
        self.model_dir = Path(model_dir) if model_dir else None
        # 限速状态（#22）：下一次允许下载的时刻，跨 worker 共享；0=还没开始
        self._rate_next = 0.0
        self._rate_lock = None      # asyncio.Lock 必须在事件循环里创建
        # ---- 并发去重（r41 修）----
        # worker 是并发的（n_workers 个 worker 抢同一个 queue），而
        # 「查库看有没有重复」和「把这张入库」之间**没有临界区** —— 同一批里
        # 两张内容相同的图会**同时**查到「库里没有」，双双入库，dedup 计数
        # 还会是 0。实测证据（tests/test_engine.py 稳定复现 + 只读探针）：
        # 两张 sha256 同为 f5a5fd42…，find_duplicate_sha 两次都返回 None，
        # 库里却躺了 id=1、id=2 两条一模一样 SHA 的记录。
        # 修法：锁内查库 + 记「本批已见」，同批撞车直接判重复。锁同样**不能**
        # 在 __init__ 里建（见上面 _rate_lock 那条 r36 教训），懒建在 loop 里。
        self._dedup_lock = None
        self._seen_sha = set()
        # r36：engine 自持的小线程池。别用 asyncio.to_thread()——它内部是
        # loop.run_in_executor(None, ...)，走的是解释器「全局默认 executor」，
        # 一旦那个 executor 已经 shutdown（进程退出阶段、或 asyncio.run 收尾），
        # 再调就抛 RuntimeError: cannot schedule new futures after shutdown。
        # 实测 r36 真机跑 30 分钟，engine worker 正好卡在这坑上刷了 6 条
        # Traceback 并把 worker 协程打断。自持一个池子就没这个耦合。
        # 池里的线程是 daemon，不 shutdown 也不会拖住解释器退出。
        self._ex = concurrent.futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="mrm-engine")
        self._ex_closed = False
        # r40：AI 分类器槽位必须在 __init__ 就建好。
        # 原来这两句只写在 run() 里、且位置在「gallery-dl 快路径提前 return」
        # 之后 —— 走快路径（installed + supports 命中）时它们根本没执行，
        # 落库阶段 _ai_exclude_state() 读 self._excl_clf 就是
        # AttributeError: 'Engine' object has no attribute '_excl_clf'，
        # 整条任务直接判失败（r40 真机 30 分钟扫描抓到 1 例，erome 任务）。
        self._clf = None
        self._excl_clf = None
        # V0.0.4：这次任务「要找什么 / 别找什么」的词表（手填 + 学到的）。
        # run() 一开始就算好，两条采集路径共用，避免快路径把词丢了。
        self._terms = None
        # V0.0.77：口味库指纹，运行中热更新词表用（None = 还没记初始版本）
        self._taste_ver = None
        # V0.0.7：纠错反馈的软排除词 + 中文词→模型标签反查表。放在这里
        # 初始化，保证 _prepare_ai 没跑过（例如老测试直接调 _apply_ai）时
        # 也不会 AttributeError。
        self._soft_excl_labels: set[str] = set()
        self._pref_alias: dict[str, list[str]] = {}
        # 分类器只按任务初始化一次（快路径与自研爬虫都会调 _prepare_ai）
        self._ai_ready_for = None

    def _prepare_ai(self, task, rep: RunReport) -> None:
        """把看图模型准备好：快路径和自研爬虫两条路共用，按任务只准备一次。

        V0.0.4 修两个让词白填的毛病：
          1. 这段原来只长在「自研爬虫」那条路上。粘个网址就建任务走的是
             gallery-dl 快路径，前面提前 return，看图模型根本没准备好 ——
             用户填的主题词、排除词一个都没跑，等于白填；
          2. 候选标签集原来只有手填的主题词，从本地图库学到的「哆啦A梦」
             从来没拿去认过图。现在统一用 find_terms 合成的那张词表，
             学到的东西才真的决定「收不收这张图」。
        """
        if self._terms is None:
            self._terms = _find.collect(self.db, task, self.cfg)
        # V0.0.7：软反馈词也进缓存键。以前只看「要找/别找」，用户中途
        # 拉黑一张图、账本多了个观察期的词时，这里会误判「没变化」直接
        # return，排除分类器还是上一批候选 —— 反馈等于没生效。
        marker = (getattr(task, "id", None), tuple(self._terms.want),
                  tuple(self._terms.avoid),
                  tuple(getattr(self._terms, "soft_avoid", [])),
                  bool(task.ai_enabled),
                  str(getattr(self.cfg, "ai_backend", "builtin")))
        if self._ai_ready_for == marker:
            return
        self._ai_ready_for = marker
        self._clf = None
        self._excl_clf = None
        # 全局关了 AI 就不跑，这是硬门禁。
        if not bool(getattr(self.cfg, "ai_enabled_global", True)):
            return
        # 下面这个判据必须同时写着「全局开关」和「任务级开关」——
        # 全仓有一条护栏专门盯着它，防止以后有人把全局开关摘掉。
        # 后半句：只要「要找什么」里有词（包括从本地图库学到的），
        # 也按用户的初衷跑一遍看图判定，不再要求用户另外勾开关 ——
        # 导入过图，软件就该已经知道要找什么了。
        if not (bool(getattr(self.cfg, "ai_enabled_global", True))
                and task.ai_enabled) and not self._terms.want:
            return
        from .classify.labels_zh import expand_labels
        # 学到的词可以很多，全塞进模型候选集会稀释分数、还拖慢推理；
        # 取权重最高的前 24 个（手填词排在前面，永远优先）。
        want = expand_labels(self._terms.want[:24])
        excl = expand_labels(self._terms.avoid)
        # V0.0.7：被判掉过一次的图，它身上的词也进看图排除候选。命中只送
        # 审核让人再看一眼，绝不静默删、也不写进硬黑名单 —— R174 那道
        # 「模型认错不能封死用户想要的题材」的防线原样保留。
        soft_excl = expand_labels(getattr(self._terms, "soft_avoid", []))
        excl = list(dict.fromkeys(list(excl) + soft_excl))
        _hard_keys = {str(x).strip().lower() for x in expand_labels(self._terms.avoid)}
        self._soft_excl_labels = ({str(x).strip().lower() for x in soft_excl}
                                  - _hard_keys)
        # V0.0.7：中文手填词会被展开成英文标签交给模型，可偏好表里存的
        # 键还是中文。打分时拿英文标签查权重永远查不到，用户选择「不喜欢」
        # 等于白点。留一张「模型标签 → 用户原词」反查表把两头对上。
        alias: dict[str, list[str]] = {}
        for w in self._terms.want[:24]:
            src = str(w).strip().lower()
            if not src:
                continue
            for e in expand_labels([w]):
                k = str(e).strip().lower()
                if k and src not in alias.setdefault(k, []):
                    alias[k].append(src)
        self._pref_alias = alias
        backend = getattr(self.cfg, "ai_backend", "builtin")
        try:
            if backend == "api" and getattr(self.cfg, "ai_api_url", ""):
                from .classify.api_model import ApiClassifier
                kw = dict(base_url=self.cfg.ai_api_url,
                          api_key=getattr(self.cfg, "ai_api_key", ""),
                          model=getattr(self.cfg, "ai_api_model", ""))
                if want:
                    self._clf = ApiClassifier(labels=want, **kw)
                if excl:
                    self._excl_clf = ApiClassifier(labels=excl, **kw)
            else:
                from .classify.clip_model import LocalClassifier
                model_dir = (getattr(self.cfg, "ai_model_path", "")
                             or self.model_dir)
                _ai_threads = int(getattr(self.cfg, "ai_threads", 0) or 0)
                _ai_gpu = bool(getattr(self.cfg, "ai_gpu", True))
                from .config import ai_device_effective
                _ai_dev = ai_device_effective(self.cfg)
                if want:
                    self._clf = LocalClassifier(
                        labels=want, model_dir=model_dir,
                        threads=_ai_threads, gpu=_ai_gpu, device=_ai_dev)
                    if getattr(self._clf, "backend", "") != "clip":
                        rep.errors.append(
                            f"本地 AI 处于降级模式（后端={self._clf.backend}）；"
                            f"标签将为空")
                if excl:
                    self._excl_clf = LocalClassifier(
                        labels=excl, model_dir=model_dir,
                        threads=_ai_threads, gpu=_ai_gpu, device=_ai_dev)
                    # 非 CLIP 后端对排除词一律返回满分，会把整库拖进待审核
                    if getattr(self._excl_clf, "backend", "") != "clip":
                        _b = getattr(self._excl_clf, "backend", "?")
                        self._excl_clf = None
                        rep.errors.append(
                            f"本地 AI 处于降级模式（排除词分类后端={_b}）；"
                            f"排除词分类已跳过")
        except Exception as e:
            rep.errors.append(f"local AI unavailable: {e}")

    async def _to_thread(self, fn, *args):
        """把 CPU/IO 重的同步函数丢到 engine 自己的线程池（r36）。

        见 __init__ 里 self._ex 的注释：不能用 asyncio.to_thread()，
        它依赖解释器全局默认 executor，那个 executor 被 shutdown 之后
        每次调用都会 RuntimeError。
        """
        if self._ex_closed:
            raise RuntimeError("engine 线程池已关闭")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._ex, fn, *args)

    def shutdown(self, wait: bool = False):
        """销毁 engine 线程池（daemon 线程，不调也能退出，显式调更干净）。

        r39d：默认 wait=False —— 调用方多半站在队列的事件循环里，wait=True 会
        把整个循环堵死；wait=False 时每个 worker 手上的活干完就退出。
        收尾顺序很重要：调用方必须**先**退池子、**再**关 Database，
        反过来会出现 worker 正用着 conn 而库已关（退出期实测 0xC0000409）。
        """
        self._ex_closed = True
        try:
            self._ex.shutdown(wait=wait)
        except Exception:
            pass

    def _taste_version(self) -> tuple:
        """口味库 + 反馈账本的轻量指纹。用来判断运行中词表是否变了。

        V0.0.77：用户定稿"白名单/黑名单增量加入后立即生效，任务中也生效"。
        以前词表在 run() 开头 collect 一次就冻结，运行中用户加的词要等下一
        任务。这里用一次极轻的聚合查询（preferences 只有几十行）当版本号，
        _process_image 每张图比一次——变了就重读词表并重建分类器。成本可忽略。
        """
        try:
            r = self.db.query(
                "SELECT COUNT(*) AS n, COALESCE(SUM(weight),0) AS w "
                "FROM preferences WHERE task_id=0")
            pref = (int(r[0]["n"]), round(float(r[0]["w"]), 4)) if r else (0, 0.0)
        except Exception:  # noqa: BLE001
            pref = (-1, 0.0)
        try:
            fb = self.db.meta_get("feedback_avoid.v1", "") or ""
        except Exception:  # noqa: BLE001
            fb = ""
        return pref + (hash(fb),)

    def _refresh_terms(self, task) -> bool:
        """词表脏了就重读。返回是否真的刷新了。"""
        try:
            ver = self._taste_version()
        except Exception:  # noqa: BLE001
            return False
        if getattr(self, "_taste_ver", None) == ver:
            return False
        # 第一次：run() 已 collect 过，只记版本号，不重复 collect
        if getattr(self, "_taste_ver", None) is None:
            self._taste_ver = ver
            return False
        try:
            fresh = _find.collect(self.db, task, self.cfg)
            fresh.avoid = list(dict.fromkeys(
                list(fresh.avoid) + self._blacklist_rules(task)))
            self._terms = fresh
            self._taste_ver = ver
            # 强制 _prepare_ai 重建分类器（marker 会因新词表变化）
            LOG.info("口味库已变化，运行中热更新词表: 要找=%s 别找=%s",
                     fresh.want[:8], fresh.avoid[:8])
            return True
        except Exception as e:  # noqa: BLE001
            LOG.warning("热更新词表失败（沿用旧词表）: %s", e)
            return False

    def _in_allowed_hours(self, task) -> bool:
        """#22 允许时段：留空 / 首尾相同 = 全天；支持跨午夜如 "22-6"。"""
        h = (getattr(task, "allowed_hours", "") or "").strip()
        if not h:
            return True
        m = re.match(r"^(\d{1,2})\s*[-~]\s*(\d{1,2})$", h)
        if not m:
            return True                      # 填得不对就当不限，别把任务卡死
        a, b = int(m.group(1)), int(m.group(2))
        if a > 23 or b > 23 or a == b:
            return True
        cur = datetime.now().hour
        return (a <= cur < b) if a < b else (cur >= a or cur < b)

    async def _rate_wait(self, task):
        """#22 限速：按 rate_limit(张/分) 在下载之间插入间隔（0=不限）。"""
        lim = int(getattr(task, "rate_limit", 0) or 0)
        if lim <= 0:
            return
        if self._rate_lock is None:
            self._rate_lock = asyncio.Lock()
        interval = 60.0 / max(1, lim)
        async with self._rate_lock:
            now = time.monotonic()
            if self._rate_next <= 0:
                self._rate_next = now + interval
                wait = 0.0
            else:
                wait = self._rate_next - now
                self._rate_next = max(now, self._rate_next) + interval
        if wait > 0:
            await asyncio.sleep(wait)

    # ---------------- F7：run/retry 共用的构造助手 ----------------
    #
    # 为什么要抽出来：F7 的「按 URL 重试」要走与 run() **逐字节相同**的
    # fetcher/downloader 构造（代理解析、Cookie 合并、目录模板、尺寸阈值）。
    # 在 retry 里另抄一份 = 两套口径迟早对不上（这个仓里
    # 「两个张数口径不一致」类的 bug 反复踩过），所以抽成三个助手，
    # run() 与 retry_failed_urls() 都调它们。
    def _resolve_proxy(self, task) -> str:
        """代理解析：config.proxy 优先；为空时回退到系统环境变量
        （HTTP_PROXY/HTTPS_PROXY），避免"没配代理→外网全 502→0 张图"。"""
        from .proxy import ProxyPool
        pool = ProxyPool(self.db)
        env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        return pool.resolve(
            task.proxy_id,
            task.proxy if task.use_proxy else None,
            self.cfg.proxy or env_proxy)

    def _make_fetcher(self, task, rule, proxy_url: str):
        """注入的测试 fetcher 只接受 (cfg, proxy)，不传 cookie；
        真实 Fetcher 才带站点 Cookie（#52）。"""
        # R173：任务级 User-Agent。留空 = 沿用全局（一个字都不改）；
        # 非空则做**浅拷贝覆盖**，让 fetcher 发请求、robots.txt 取 agent、
        # 合规检查器读 agent 三处自动同口径 —— 全局 cfg 本身绝不被改，
        # 否则同进程里其他任务/后续批次会被串味。
        effective_cfg = self.cfg
        # 起跑前兜底再清洗一次：库里的值可能来自旧版本 UI 或手工 SQL，
        # 换行进了请求头就是**请求头注入**，这里必须挡住（与两端同一个函数）。
        from .task_headers import clean_cookie, clean_user_agent
        # R252：适配器里也可以固定一个 UA（很多站对 UA 很挑）。
        # 优先级：任务 > 适配器 > 全局 —— 任务是用户对"这一次"的明确指定，
        # 不该被规则里的默认值盖掉。
        task_ua = clean_user_agent(getattr(task, "user_agent", "")) or \
            clean_user_agent(getattr(rule, "user_agent", "") if rule else "")
        if task_ua:
            try:
                effective_cfg = replace(self.cfg, user_agent=task_ua)
            except Exception:  # pragma: no cover - cfg 非 dataclass 时的兜底
                effective_cfg = self.cfg
        if self.fetcher_factory is not None:
            return self.fetcher_factory(effective_cfg, proxy_url)
        # 任务级 Cookie 同样兜底清洗（与 UA 同理，防旧数据/手工 SQL 注入换行）
        # R252：适配器自带的 Cookie 与任务 Cookie 合并（任务优先，同名覆盖）。
        task_cookie = merge_cookies(
            clean_cookie(getattr(rule, "cookie", "") if rule else ""),
            clean_cookie(getattr(task, "cookie", "")))
        # 同域名已存的 Cookie（如 Cloudflare 的 cf_clearance）自动合并进来，
        # 这样 3 个 CF 站只需用「取站点 Cookie」抓一次，以后同域名任务全复用。
        _domain = domain_of(
            (rule.base_url if rule else "")
            or (task.entry_urls[0] if task.entry_urls else ""))
        return Fetcher(
            effective_cfg, proxy=proxy_url,
            cookie=cookie_for_task(task_cookie, _domain))

    def _bandwidth_limiter(self):
        """F24：本轮的**共享**带宽令牌桶（run 与 F7 按 URL 重试各建一个，
        两者读同一份 cfg ⇒ 同口径）。

        速率传的是 **callable**：任务跑几小时很常见，跨过时段边界时
        下一次扣令牌自动按新时段算，不用重启任务、也不用另起定时器。
        """
        # 2026-10-27（编号 18）：不再只认 rate_plan —— 全局上限
        # `speed_limit_kbps` 也算数，两者由 rateplan.effective_kbps 合成
        # 一个口径（两处都配时取更严的）。引擎/fetcher/界面都调那一个函数，
        # 避免"设置里写 500、实际跑 2000"这类对不上。
        from .rateplan import BandwidthLimiter, effective_kbps
        cfg = self.cfg
        return BandwidthLimiter(lambda: effective_kbps(cfg))

    @staticmethod
    def _safe_dirname(name: str) -> str:
        """把任务名洗成合法目录名（与图片文件名同一套 sanitize，口径统一）。"""
        try:
            from .downloader.naming import sanitize
            out = sanitize(str(name or "").strip()) or "task"
        except Exception:
            out = "".join(ch for ch in str(name or "") if ch not in '\\/:*?"<>|').strip() or "task"
        return out[:60]

    def _make_downloader(self, task, fetcher, limiter=None) -> "Downloader":
        """下载器构造（run 与 F7 重试共用；参数口径见 run() 内注释）。

        limiter：F24 带宽令牌桶；不传就按当前 cfg 现建一个（共享给本轮
        所有并发下载，见 _bandwidth_limiter 注释）。
        """
        # 2026-10-27（编号 12）：输出子目录三档，由 cfg.subdir_mode 决定。
        #   site = 按站点（历史行为，默认）
        #   task = 按任务名
        #   both = 任务名/站点
        #   none = 全放输出目录（想"一锅端"时用）
        # ⚠ 不用 {task} 占位符：naming.build_path 的 token 是固定白名单
        #   （site/album/date/theme/resolution/keyword/filename），
        #   塞未知 token 会 KeyError。所以「任务名」这一层直接做在 root 上，
        #   模板只负责站点与主题词。
        _mode = str(getattr(self.cfg, "subdir_mode", "site") or "site").lower()
        _base_root = Path(task.output_dir or self.cfg.library_dir)
        _tpl = "{site}"
        if _mode == "none":
            _tpl = ""
        elif _mode == "task":
            _tpl = ""
            _base_root = _base_root / self._safe_dirname(getattr(task, "name", "") or f"task{task.id}")
        elif _mode == "both":
            _base_root = _base_root / self._safe_dirname(getattr(task, "name", "") or f"task{task.id}")
        if _tpl and getattr(task, "keyword_subdir", False) and (
                task.ai_labels or task.ai_labels_l2 or task.ai_labels_l3):
            _tpl = _tpl + "/{keyword}"
        return Downloader(fetcher, root=_base_root,
                          template=_tpl,
                          # 一个网站一个文件夹；开「按主题词分子目录」时站内再按主题词分
                          # 【R117 P1-4】任务级优先（>0 才算数，0=该任务不设限），
                          # 否则回退全局 —— 与 resolve_max_pages 同口径。
                          min_width=_task_size_limit(task.min_width, self.cfg.min_width),
                          min_height=_task_size_limit(task.min_height, self.cfg.min_height),
                          min_bytes=task.min_bytes or self.cfg.min_bytes,
                          concurrency=task.concurrency, delay=task.delay,
                          limiter=(limiter if limiter is not None
                                   else self._bandwidth_limiter()))

    async def run(self, task_id: int, basis: str | None = None) -> RunReport:
        from .repo import get_task
        task = get_task(self.db, task_id)
        if task is None:
            raise ValueError(f"no such task {task_id}")
        rep = RunReport(task_id=task_id)
        # V0.0.4：先把「这回要找什么 / 别找什么」算出来。
        # 手填的主题词 + 从本地图库学到的词合成要找的；黑名单 + 拉黑过的词
        # 合成别找的。以前学到的词只进口味账、从不参与采集，等于白学。
        self._terms = _find.collect(self.db, task, self.cfg)
        # 再把引擎既有的「任务黑名单 + 全局黑名单」合并口径并进来，
        # 保证任何一条采集路径都不会漏掉用户刚加的全局黑名单。
        self._terms.avoid = list(dict.fromkeys(
            list(self._terms.avoid) + self._blacklist_rules(task)))
        # V0.0.77：记下本任务起始的口味库版本，之后每张图比一次实现热更新
        try:
            self._taste_ver = self._taste_version()
        except Exception:  # noqa: BLE001
            self._taste_ver = None
        LOG.info("▶ 开始任务 #%s 入口=%s 适配器=%s | %s",
                  task_id, task.entry_urls, task.adapter_name or "(自动)",
                  self._terms.describe())
        # 本批「已见 sha256」要按任务清空：上一批见过的图，这一批可能库里已经
        # 被清掉了（全面清理后），留着会把新图误判成重复而错删。
        self._seen_sha.clear()
        t0 = time.monotonic()

        # 1) adapter — OPTIONAL. Empty / "(自动)" means "just use gallery-dl",
        # so a beginner only needs to paste a URL.
        adapter_name = (task.adapter_name or "").strip()
        needs_adapter = bool(adapter_name) and "自动" not in adapter_name
        adapter_path = self._resolve_adapter(adapter_name) if needs_adapter else None
        if needs_adapter and adapter_path is None:
            if not ADAPTER_NAME_OK.match(adapter_name):
                # R152-C1：名字不合法（含路径字符等）——与"不存在"分开说，
                # 用户才知道要去改名字而不是去找文件。
                rep.errors.append(
                    f"适配器名 '{adapter_name}' 不合法：只能用字母、数字、下划线"
                    f"和短横线（1-64 字符）。请改个名字，或把适配器下拉框选回"
                    f"「(自动 - gallery-dl)」")
            else:
                rep.errors.append(
                    f"适配器 '{adapter_name}' 不存在。不需要它的话，请把适配器下拉框"
                    f"选回「(自动 - gallery-dl)」；或者先导入规则文件 (mrm add-rule)")
            return rep
        rule = load_adapter(adapter_path) if adapter_path else None

        # 2) 代理 / fetcher 构造（合规门禁已于 2026-10-26 整层删除）
        from .proxy import ProxyPool
        pool = ProxyPool(self.db)
        # #53 记住这次任务用的是哪个代理条目，失败时自动切到下一个健康节点
        proxy_entry = None
        if task.use_proxy:
            if task.proxy_id:
                proxy_entry = pool.get(task.proxy_id)
            elif len(pool.list()) > 0:
                proxy_entry = pool.pick()
        # 代理解析走共用助手（与 F7 重试同口径，见 _resolve_proxy 注释）
        proxy_url = self._resolve_proxy(task)
        # fetcher 构造同样走共用助手（Cookie 合并逻辑见 _make_fetcher）
        fetcher = self._make_fetcher(task, rule, proxy_url)

        entries = task.entry_urls or (rule.entry_urls if rule else [])
        from .backends.gallery_dl import GalleryDLBackend, supports, installed
        from .crawler.site_entries import map_entry, supported_shapes
        # V0.0.4：拿「要找什么」的词去站内找图。以前用户填了网址，软件就把
        # 站上能翻到的图全抓回来再挑，「学到的哆啦A梦」从没变成去找图的词。
        # 只在已知支持按词找图的站点 + 用户填的是整站入口时才换；
        # 用户给了具体图集/帖子网址一律尊重原网址，绝不擅改。
        if self._terms.want:
            _searched, _note = _find.search_entries(
                entries, self._terms.want, self._terms.avoid)
            if _searched != entries:
                if any(supports(u) for u in _searched):
                    LOG.info("按词找图：%s", _note or "已生成站内按词入口")
                    entries = _searched
                else:
                    LOG.info("按词生成的入口本站不认，保持原网址（改由下载前/后按词挑）")
        # 2026-10-26：裸首页/裸列表 → 该站全站入口。
        # 用户填 https://www.pornpics.com/ 是表达「我要整站」，这是最自然的
        # 输入；gallery-dl 只认特定 URL 形状，以前这里静默退回自研爬虫 →
        # 站点回 JS 空壳 → 0 图 + 误导性报错。现在先尝试映射，映射成功走快路径。
        _mapped: list[str] = []
        for _u in entries:
            _m = map_entry(_u)
            _mapped.append(_m or _u)
        if _mapped != list(entries):
            LOG.info("入口映射: %s → %s（全站入口）", entries, _mapped)
            entries = _mapped
        supported_here = installed() and any(supports(u) for u in entries)
        LOG.info("路由判定: gallery-dl 快路径=%s (installed=%s) 入口数=%d",
                  supported_here, installed(), len(entries))
        if not supported_here and installed():
            # 形状不匹配时把「本站到底支持哪些形状」直接说出来，别让用户猜；
            # 只写日志不塞 rep.errors —— 后续自研爬虫若成功，不该被判失败。
            _shapes = supported_shapes(entries[0] if entries else "")
            if _shapes:
                LOG.warning("gallery-dl 认这个站、但不认这个网址形状；"
                            "本站支持的形状：%s", " / ".join(_shapes))
            else:
                LOG.info("该站 gallery-dl 无提取器，走自研爬虫（适配器规则）")

        # ---- FAST PATH: gallery-dl knows this site -> delegate to it. -------
        # It already implements per-site multi-level discovery, original-image
        # picking, retries, rate limits and proxies. We only orchestrate.
        if supported_here:
            # V0.0.4 修「粘个网址建任务，填的词全白填」：这段原来在
            # 快路径的提前 return **之后**，走快路径时看图模型根本没准备好，
            # 主题词、排除词一个都没跑。两条路现在共用同一套准备动作。
            self._prepare_ai(task, rep)
            return await self._run_via_gallery_dl(task, rule, entries, rep)

        # R252：用户把适配器选成「自动」时，按**域名**兜底匹配一条规则。
        # 为什么放在 gallery-dl 判定**之后**：快路径优先是既定契约
        # （danbooru/gelbooru 这类站改 YAML 反而更差），自动匹配只该补
        # 「gallery-dl 完全不认这个站」的空档 —— 那正是用户粘一个冷门网址
        # 就报「暂无可用抓取规则」的场景。
        if rule is None and bool(getattr(self.cfg, "adapter_auto_match", True)):
            try:
                from .crawler.rules import match_adapter
                _auto = match_adapter(self.cfg.rules_dir, entries)
            except Exception:
                _auto = None
            if _auto is not None:
                rule = _auto
                LOG.info("自动匹配适配器：%s（域名 %s）",
                         rule.name, ",".join(rule.allowed_domains))
        if rule is None:
            # P1-3：报错不许说黑话。原文案把 gallery-dl / 适配器规则 /
            # mrm check / examples/*.yaml 全丢给用户，而新用户一个都不认识，
            # 看到只会以为软件坏了。改成「说人话 + 给下一步」。
            # V0.0.55：去掉写死的站点数（用户能从规则市场自装，数字必腐坏），
            # 并把「自动生成规则」改成真实存在的「适配器市场」（V0.0.54 起
            # 该 AI 生成入口已移除，旧引导会把用户指向一个不存在的功能）。
            rep.errors.append(
                "该网址暂无可用抓取规则。"
                "可能原因：该站点需要专用抓取规则，或页面结构已变更。"
                "建议操作："
                "① 更换为其他网址；"
                "② 打开「适配器」页，进入「适配器市场」订阅并安装对应站点的规则；"
                "③ 或在「适配器」页手动新建规则。")
            await fetcher.close()
            return rep

        # 看图模型统一在 _prepare_ai 里准备（快路径也会在那之前调用一次）。
        # 这里补一次是幂等的：marker 没变就直接返回，不会重复加载模型。
        self._prepare_ai(task, rep)

        # ---- FALLBACK: our own generic discovery for unsupported sites. -----

        # Streaming discovery: pages are crawled and images downloaded AS
        # THEY ARE FOUND, so files appear immediately (no full-site wait).
        runner = AdapterRunner(rule)
        warnings: list[str] = []

        # F9（R154）：页面级断点续采 —— 把上次的页面账本搬来当种子：
        # 抓过的页不再重爬（省流量，也少惹站点），没抓完的接着抓。
        # 账本读不出来不能拦住抓取：退化成全量重爬（老行为）。
        known_pages: dict = {}
        try:
            known_pages = self.db.task_pages_resume(task_id) or {}
        except Exception:
            known_pages = {}

        # 4) download — STREAMING: images are processed as pages are found,
        # so the user sees files appearing while discovery is still running.
        # 构造走共用助手 _make_downloader（与 F7 的按 URL 重试同一套参数口径；
        # R117 P1-4 的任务级优先/回退全局语义见该助手内注释）。
        # F24：把「这轮到底限不限速、限多少」写进日志 —— 限速最怕的就是
        # 用户设了却看不出生效（界面那行状态在设置框里，跑起来时看不到）。
        try:
            from .rateplan import current_limit_text
            LOG.info("限速计划：%s",
                     current_limit_text(getattr(self.cfg, "rate_plan", None),
                                        bool(getattr(self.cfg,
                                                     "rate_plan_enabled", False))))
        except Exception:
            pass
        dl = self._make_downloader(task, fetcher)

        queue: asyncio.Queue = asyncio.Queue()
        seen: set[str] = set()

        def on_page(res: CrawlResult):
            rep.pages += 1
            # F9：抓完一页**立刻**落账（done/error/blocked 三类都记，
            # 不等任务结束）—— 中途停掉/崩掉进度也不丢；重跑时 done
            # 跳过、其余带着原深度重试。记账失败只吞掉，绝不让它
            # 打断抓取主流程（断点续采是优化，不是新故障点）。
            try:
                if res.error == "blocked":
                    _pst = "blocked"
                elif res.error:
                    _pst = "error"
                else:
                    _pst = "done"
                self.db.record_page_result(
                    task_id, res.url, _pst,
                    images=len(res.image_urls or []),
                    error=res.error or "", depth=res.depth)
            except Exception:
                LOG.debug("F9 页面账本落账失败（忽略）", exc_info=True)
            LOG.info("▶ 发现页面: %s (图片=%d)", res.url, len(res.image_urls))
            for img in res.image_urls:
                if img in seen:
                    continue
                seen.add(img)
                queue.put_nowait((img, _album_of(img), res.url))

        def on_discover(link: str, depth: int):
            # F9：这轮第一次见到的新链接 → 落 pending（"知道了还没抓"）。
            # INSERT OR IGNORE：已抓过（done）的页不会被降级。
            try:
                self.db.record_page_seen(task_id, link, depth)
            except Exception:
                LOG.debug("F9 页面账本记 pending 失败（忽略）", exc_info=True)

        disc = Discovery(fetcher, runner, base_domain=domain_of(rule.base_url),
                         max_depth=task.max_depth, strategy="bfs",
                         on_page=on_page, on_warning=warnings.append,
                         on_discover=on_discover, known_pages=known_pages,
                         # 页数上限：任务级优先（每个任务自己设，0=不限）→
                         # 规则可单独收紧（如 uuss4khd 首页翻页有 3000+ 页）→
                         # 最后才回退全局「新建任务默认页数」。
                         max_pages=resolve_max_pages(task, rule, self.cfg))
        discovery_run = asyncio.create_task(
            disc.run(task.entry_urls or rule.entry_urls))

        n_workers = max(1, task.concurrency)

        # r35：本次 run 的停止信号。选择「停止」/ 关窗口时外层任务被 cancel，这里
        # 置位，worker 最多 0.5s 内自己收工。旧版 worker 死等 `await queue.get()`，
        # 外层任务被 cancel 后它照样活着继续消费队列；一旦库的连接被 close，
        # 每张图都会撞 `Cannot operate on a closed database`（实测一次刷 280 条
        # ERROR，图还白下载了、没入库）。
        stopped = asyncio.Event()

        async def worker():
            while not stopped.is_set():
                # 0.5s 轮询取图：既能及时看到 stopped，也不会空转烧 CPU。
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    return
                if stopped.is_set():
                    return
                # 兜底：库已经被关（关窗口流程 closeEvent 关在 worker 之前）→
                # 立刻收工，别再白下载。
                if self.db.is_closed():
                    stopped.set()
                    rep.errors.append("数据库已关闭，任务提前结束")
                    LOG.warning("⚠ 数据库已关闭，停止处理剩余图片")
                    return
                try:
                    await self._process_image(task, rule, dl, *item, rep)
                except asyncio.CancelledError:
                    stopped.set()
                    raise
                except Exception as e:
                    if self.db.is_closed():
                        # 库已关，再逐条打 Traceback 就是刷屏：记一条汇总后收工
                        stopped.set()
                        rep.errors.append("数据库已关闭，任务提前结束")
                        LOG.warning("⚠ 数据库已关闭，停止处理剩余图片")
                        continue
                    rep.failed += 1
                    LOG.exception("⚠ 处理图片失败: %r", item)
                    rep.errors.append(f"{type(e).__name__}: {e}"[:120])
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(n_workers)]
        _disc_cancelled = False
        try:
            await discovery_run
        except asyncio.CancelledError:
            # 停止信号没传到 worker（它卡在下载里）→ 这里置位兜底
            stopped.set()
            _disc_cancelled = True
        except Exception as _de:
            # R152-C3：以前这里**只**捕 CancelledError —— discovery 自己炸了
            # （域名解析失败 / 适配器 rule.node 表达式抛异常 / 编码问题…）
            # 就直接穿出本函数，worker 拿不到停工哨兵，永久挂在 queue.get()
            # 上。异常本身照样记进报告给用户看（不断Errors链），但人必须先撤。
            stopped.set()
            rep.errors.append(f"发现页面失败：{type(_de).__name__}: {_de}"[:200])
            LOG.warning("⚠ 发现页面失败，已通知 worker 收工：%r", _de)
        finally:
            # F9：把「跳过多少已抓页」写进报告（断点续采的账：日志/UI 用）。
            rep.pages_resumed = int(getattr(disc, "skipped_done", 0) or 0)
            await drain_workers(workers, queue)
        if _disc_cancelled:
            raise asyncio.CancelledError
        rep.images_found = len(seen)

        # r39f：warnings 里其实混着两类，别一股脑当失败原因。
        #   · "image selector '…' matched 0" —— 只是**提示**：某页某选择器空了
        #   · "所有 image selector 落空，通用兜底也没找到图片：页面可能是…或入口页本身不含图"
        #     —— 这才是能照着做的那句（JS 渲染 / 要登录 / 入口指错）
        # 任务列表的 last_error 只取第一条，原来永远是 "matched 0" 打头，
        # 用户看到只剩一句没头没尾的提示，根本不知道该改入口还是改选择器。
        # 把最有指导性的那条提到最前；其余提示一条不丢，照旧全留。
        def _hint_rank(w: str) -> int:
            if any(k in w for k in ("落空", "也没找到", "可能是", "未产出图")):
                return 0
            return 1

        rep.errors.extend(sorted(warnings[:20], key=_hint_rank))
        rep.elapsed = time.monotonic() - t0
        try:
            await fetcher.close()
        except Exception:
            LOG.debug("fetcher 关闭异常（忽略）", exc_info=True)
        # 关窗口流程是「先 stop 队列 → 再 db.close()」，worker 早退后 run 才走到
        # 这里；此时库已关，再写 `UPDATE tasks SET status='done'` 就是旧版刷屏的
        # 那 280 条 `Cannot operate on a closed database` 之一。直接收工。
        if self.db.is_closed():
            rep.errors.append("数据库已关闭，本次任务未能正常收尾")
            LOG.warning("⚠ 数据库已关闭，跳过任务收尾写库")
            return rep
        self.db.execute("UPDATE tasks SET status='done', updated_at=datetime('now') "
                        "WHERE id=?", (task_id,))
        # #53 代理健康：一张没下下来且全失败 → 这次用的代理大概率挂了，
        # 自动把它记一次失败（失败满 3 次才会被停用），下次任务挑下一个健康节点
        if proxy_entry is not None and rep.downloaded == 0 and rep.failed > 0:
            try:
                if len(pool.list()) > 1:
                    pool.report_failure(proxy_entry)
                    rep.errors.append(
                        f"代理 {proxy_entry.name} 本次全失败，已自动切到下一个")
                    self.db.audit("proxy", "auto-rotate", proxy_entry.name,
                                  "task failed with this proxy")
            except Exception:
                pass
        self.db.audit("engine", "run", str(task_id),
                      f"dl={rep.downloaded} dup={rep.dedup} bl={rep.blacklist_hit}")
        return rep

    # ---------------- F7（R154）：按 URL 粒度重试失败网址 ----------------
    async def retry_failed_urls(self, task_id: int,
                                urls: Optional[list[str]] = None) -> dict:
        """重试 failed_urls 清单里的网址（全部或指定子集）。

        **复用现有单 URL 下载+管线路径**（`_process_image`：下载 → 去重 →
        黑名单 → AI → 入库），不另写一套下载逻辑；构造走 run() 同款助手
        （_resolve_proxy / _make_fetcher / _make_downloader）。

        返回摘要 dict：
          ok              bool   是否跑完（False 时看 error，如任务不存在）
          retried         int    实际重试了几条
          succeeded       list   这次下载成功（已自动从失败清单移除）
          still_failed    list   仍失败（tries 已 +1，原因已刷新）
          filtered        list   被关键字/黑名单/允许时段跳过（没下载，也不算失败）
          error           str    仅 !ok 时给出（中文）
        """
        from .repo import get_task

        task = get_task(self.db, task_id)
        if task is None:
            return {"ok": False, "error": f"任务 #{task_id} 不存在，可能已被删除"}
        rows = self.db.list_failed_urls(task_id)
        if urls:
            want = {(u or "").strip() for u in urls if (u or "").strip()}
            rows = [r for r in rows if r["url"] in want]
        if not rows:
            return {"ok": True, "retried": 0, "succeeded": [],
                    "still_failed": [], "filtered": [],
                    "note": "失败清单里没有这些网址"}

        # 合规门（与 run() 同口径）：R186 —— off 档整层跳过；不再自动插
        # 授权行。这里防的只是「非 off 档下清单还在、授权被撤」。
        adapter_name = (task.adapter_name or "").strip()
        needs_adapter = bool(adapter_name) and "自动" not in adapter_name
        adapter_path = self._resolve_adapter(adapter_name) if needs_adapter else None
        if needs_adapter and adapter_path is None:
            return {"ok": False,
                    "error": "任务的适配器不存在或名字不合法，先修好任务再重试"}
        rule = load_adapter(adapter_path) if adapter_path else None
        # 2026-10-26：retry 路径的第二道合规门禁也删了（同 run() 主门禁）。

        fetcher = self._make_fetcher(task, rule, self._resolve_proxy(task))
        dl = self._make_downloader(task, fetcher)
        rep = RunReport(task_id=task_id)
        # V0.0.78：重试路径每次新建 Engine，_clf/_excl_clf/_pref_alias 全空；
        # _process_image 里的 _refresh_terms 首次只记口味库版本就返回 False，
        # 不会补建分类器 —— 结果 _ai_excluded 恒 pass、_apply_ai 恒 keep，
        # 重试的 AI 过滤整条失效。这里显式准备一次（marker 幂等，不会重复
        # 加载模型），与 run() 的快路径 / 自研路径口径对齐。
        self._prepare_ai(task, rep)
        succeeded: list[str] = []
        still_failed: list[str] = []
        filtered: list[str] = []

        def _snap() -> tuple:
            return (rep.downloaded, rep.skipped_exists, rep.failed,
                    rep.keyword_filtered, rep.blacklist_hit, rep.hours_skipped)

        try:
            for r in rows:
                url = r["url"]
                if self.db.is_closed():
                    break
                before = _snap()
                try:
                    # album/page_url 都用 URL 本身：失败清单里只存了图片 URL，
                    # 相册名与来源页按 URL 现算（_album_of/page_url 只影响
                    # 落盘目录与关键字匹配，不影响下载成败）。
                    await self._process_image(task, rule, dl, url,
                                              _album_of(url), url, rep)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # _process_image 内部异常（非下载失败）：按失败记账，
                    # 保证这条不会从清单里悄悄消失。
                    rep.failed += 1
                    try:
                        self.db.record_failed_url(
                            task_id, url, f"{type(e).__name__}: {e}"[:300])
                    except Exception:
                        pass
                    still_failed.append(url)
                    continue
                after = _snap()
                if after[0] > before[0] or after[1] > before[1]:
                    # 下载成功 / 库里已有（幂等跳过）→ 从失败清单划掉
                    succeeded.append(url)
                    try:
                        self.db.delete_failed_urls(task_id, [url])
                    except Exception:
                        pass
                elif after[2] > before[2]:
                    # 仍失败：record_failed_url 已在 _process_image 里记账
                    # （tries+1、原因刷新），这里只归类。
                    still_failed.append(url)
                else:
                    # 关键字/黑名单/允许时段/尺寸过滤：没下载，但也不是下载失败
                    filtered.append(url)
        finally:
            try:
                await fetcher.close()
            except Exception:
                pass
        self.db.audit("engine", "retry-failed-urls", str(task_id),
                      f"n={len(rows)} ok={len(succeeded)} "
                      f"bad={len(still_failed)} skip={len(filtered)}")
        LOG.info("↻ 失败网址重试 #%s：重试 %d，成功 %d，仍失败 %d，过滤 %d",
                 task_id, len(rows), len(succeeded), len(still_failed),
                 len(filtered))
        return {"ok": True, "retried": len(rows), "succeeded": succeeded,
                "still_failed": still_failed, "filtered": filtered}

    def _keyword_blocked(self, task, *texts: str) -> bool:
        """下载前按词拦一道：链接文字 / 标题 / 相册里命中「别找什么」就不抓。

        V0.0.6 修掉一个会让词白填的硬伤：以前 prefilter 判完「看不出来、
        放行交给看图判定」之后，又追加了一道「手填的主题词必须出现在
        网址文字里，一个都不含就拦掉」。图站文件名基本是一串数字哈希、
        生成的入口是英文标签，用户填中文词时必然一个都不命中 —— 结果
        整站的图全被判掉，任务跑完 0 张。按产品初衷，手填的词是「要找
        什么」，不是「网址里必须写什么」，这一刀彻底删除。
        """
        terms = self._terms or _find.collect(self.db, task, self.cfg)
        verdict, _why = _find.prefilter(terms, *texts)
        return verdict == "skip"

    def _matched_keyword(self, task, *texts: str) -> str:
        """#28 返回在 URL/相册/来源页里命中的主题词（用于分子目录）。

        2026-10-28：主题词支持无限分级，这里改成**由最深一层往浅找**，
        哪个词命中就用它当 ``{keyword}`` 子目录名（越深越具体）。全都没命中
        返回空（文件平铺在 {site}/ 下，行为不变）。
        """
        levels = _lvl.levels_of(task)
        for words in reversed(levels):
            kws = [k for k in (words or []) if (k or "").strip()]
            if not kws:
                continue
            hit = self._first_in_text(kws, *texts)
            if hit:
                return hit
        return ""

    @staticmethod
    def _first_in_text(kws, *texts: str) -> str:
        blob = " ".join(t for t in texts if t).lower()
        for k in kws:
            if k.lower() in blob:
                return k
        return ""
        # R152-A10：这里原本跟着一段与上面完全相同的重复实现（return 之后
        # 永不可达的死代码），已删 —— 全仓 AST 探针证实仅此一处成对出现。

    def _url_seen(self, task_id: int, url: str) -> bool:
        """该任务是否已经下载过这个 URL（幂等重跑的依据，与文件名无关）。"""
        try:
            return bool(self.db.query(
                "SELECT 1 FROM images WHERE task_id=? AND source_url=? LIMIT 1",
                (task_id, url)))
        except Exception:
            return False

    def _blacklist_rules(self, task) -> list[str]:
        """需求 16：任务黑名单 **并上** 全局黑名单。

        这是「不喜欢 → 全局黑名单」能真正生效的唯一支点：光把词写进
        config 而不在这里并上，用户勾了「全局」却看到下一任务照样下这类图
        —— 那就是"ok 但做了另一件事"。**所有**过滤点都走这个合并点
        （R152-C6 以前快路径/追溯/回填四处各自拿 task.blacklist 单干）。
        """
        return merged_blacklist(task, self.cfg)

    async def _process_image(self, task, rule, dl, img_url: str,
                             album: str, page_url: str, rep) -> None:
        """Download one image and run the full pipeline (dedup/blacklist/AI)."""
        # 限速 / 允许时段（#22）：两者都是"留空即不启用"，默认行为完全不变
        if not self._in_allowed_hours(task):
            rep.hours_skipped += 1
            return
        # V0.0.77：口味库/黑名单运行中增量加入立即生效——每张图比一次指纹，
        # 变了就热更新词表 + 重建分类器；没变就零开销跳过。
        if self._refresh_terms(task):
            self._ai_ready_for = None
            self._prepare_ai(task, rep)
        await self._rate_wait(task)
        # 下载前先过滤：把能看到的文字（图片网址、相册名、来源页标题）
        # 一起跟「别找什么」比一遍。以前只认图片网址里有没有那几个字，
        # 于是标题写着「灌篮高手」的图照样被抓回来 —— 用户要的是不下载。
        terms = self._terms or _find.collect(self.db, task, self.cfg)
        # 防御性再合一次：就算将来有人改了 run()，这条路径也不许漏掉
        # 全局黑名单（全仓护栏盯着每个过滤点都走合并口径）。
        avoid = list(dict.fromkeys(
            list(terms.avoid) + self._blacklist_rules(task)))
        if _hit_blacklist(img_url, [t for t in (album, page_url) if t],
                          avoid):
            rep.blacklist_hit += 1
            return
        if self._keyword_blocked(task, img_url, album, page_url):
            rep.keyword_filtered += 1
            return
        # rule 为 None（自动适配器 / gallery-dl 任务）时用站点域名兜底：
        # F7 的 retry_failed_urls 会带 rule=None 进来（run() 的 fallback
        # 路径永不为 None —— 见 run() 里 `if rule is None: return` 的提前
        # 返回），直接 rule.name 会 AttributeError，把每条重试都误判成
        # "仍失败"（R170 护栏测试抓到）。
        _site = rule.name if rule else (domain_of(img_url) or "site")
        out = await dl.download_one(
            img_url, site=_site, album=album, date=_today(),
            keyword=self._matched_keyword(task, img_url, album, page_url),
            meta={"tags": [t for t in task.keywords if t]},
            already=lambda u, _tid=task.id: self._url_seen(_tid, u))
        if out.skipped:
            rep.skipped_exists += 1
            return
        if not out.ok:
            rep.failed += 1
            # #54 磁盘空间不足：单独标记，交给队列层暂停全部任务并提示
            if out.reason == "low_disk":
                rep.low_disk = True
                if not rep.low_disk_detail:
                    rep.low_disk_detail = (out.detail or "磁盘剩余空间低于阈值")
            # 失败原因采样（去重，最多8条），让用户在日志里能看到"为什么0张"
            reason = (out.reason or "unknown")[:120]
            if reason not in rep.errors and len(rep.errors) < 8:
                rep.errors.append(f"download failed: {reason}")
            # F7（R154）：**完整**失败网址落库（采样只是摘要，这里才是清单）。
            # 落库失败绝不影响采集主流程（与 _purge_to_trash 同款防御）。
            try:
                self.db.record_failed_url(task.id, img_url, reason)
            except Exception:
                LOG.debug("failed_urls 落库失败（忽略）: %s", img_url,
                          exc_info=True)
            return
        rep.downloaded += 1
        rep.bytes_downloaded += out.bytes or 0
        # #68 解析下载器附带的格式/拍摄时间元数据，落到图库记录上
        fmt, taken_at = None, None
        if out.detail:
            try:
                _m = json.loads(out.detail)
                fmt, taken_at = _m.get("fmt"), _m.get("taken_at")
            except Exception:
                pass
        # 内容级黑名单（如「黑白照」按图片是否真灰度判断）——早期拦截，进审核
        # （用审核替代隔离区：命中黑名单也先留文件给用户确认，不再静默删除/隔离）
        if _hit_blacklist(img_url, [t for t in task.keywords if t],
                         self._blacklist_rules(task), path=out.path):
            rep.blacklist_hit += 1
            self.db.audit("blacklist", "content-review", img_url,
                          "grayscale/keyword")
            _rec = ImageRecord(
                id=None, source_url=img_url, task_id=task.id,
                sha256=out.sha256, phash=out.phash, width=out.width,
                height=out.height, bytes=out.bytes, path=str(out.path),
                tags=[t for t in task.keywords if t],
                blacklisted=True, in_quarantine=True, needs_review=True,
                review_reason=_reasons.HIT_BLACKLIST,
                fmt=fmt, taken_at=taken_at)
            upsert_image(self.db, self._stage_to_review(_rec))
            return
        # 敏感图本地模糊（#35，可选）：命中敏感词 → 另存模糊版入库，
        # 原图留在磁盘（登记一条隔离记录隐藏，避免被回填重新灌进图库）
        if getattr(self.cfg, "blur_sensitive", False):
            try:
                from .security.blur import blur_file, hits_sensitive
                words = list(getattr(self.cfg, "sensitive_words", []) or [])
                _tag_list = [t for t in task.keywords if t]
                if hits_sensitive([img_url, album or "", " ".join(_tag_list)],
                                  words):
                    dst = await self._to_thread(blur_file, Path(out.path))
                    if dst is not None:
                        upsert_image(self.db, ImageRecord(
                            id=None, source_url=img_url, task_id=task.id,
                            sha256=out.sha256, phash=out.phash,
                            width=out.width, height=out.height,
                            bytes=out.bytes, path=str(out.path),
                            tags=_tag_list, in_quarantine=True,
                            fmt=fmt, taken_at=taken_at))
                        out.path = dst
                        rep.blurred += 1
                        self.db.audit("blur", "sensitive", img_url, str(dst))
            except Exception as e:
                LOG.warning("sensitive blur failed: %s", e)
        # 文件大小过滤器（max_bytes，0=不限）：超大全进审核，不直接丢弃
        if getattr(self.cfg, "max_bytes", 0) and out.bytes and \
                out.bytes > self.cfg.max_bytes:
            self.db.audit("filter", "too-large-review", img_url,
                          f"{out.bytes} > {self.cfg.max_bytes}")
            _rec = ImageRecord(
                id=None, source_url=img_url, task_id=task.id,
                sha256=out.sha256, phash=out.phash, width=out.width,
                height=out.height, bytes=out.bytes, path=str(out.path),
                tags=[t for t in task.keywords if t],
                in_quarantine=True, needs_review=True,
                review_reason=_reasons.humanize(
                    f"{out.bytes} > {self.cfg.max_bytes}"),
                fmt=fmt, taken_at=taken_at)
            upsert_image(self.db, self._stage_to_review(_rec))
            return
        # AI 排除词：命中 → 确信则判"不命中"(drop)，文件会搬到待审核目录
        # 等你判断（不会静默删除）；置信度不足 → 同样进待审核。
        # 注意：这里叫 drop 是"AI 判定没命中"，不是"删文件"。真正删文件只发生在
        # 去重输家 / 聚类去重输家 / 你在隔离区点彻底删除这三种情况。
        excl, note = await self._to_thread(self._ai_excluded, task, out.path)
        if excl in ("drop", "review"):
            if excl == "drop":
                rep.ai_excluded += 1
            else:
                rep.ai_review += 1
            self.db.audit("ai", "excluded-review", img_url, note)
            _rec = ImageRecord(
                id=None, source_url=img_url, task_id=task.id,
                sha256=out.sha256, phash=out.phash, width=out.width,
                height=out.height, bytes=out.bytes, path=str(out.path),
                tags=[t for t in task.keywords if t],
                in_quarantine=True, needs_review=True,
                review_reason=_reasons.humanize(note),
                fmt=fmt, taken_at=taken_at)
            upsert_image(self.db, self._stage_to_review(_rec))
            return
        # 重复内容处理（V0.0.5 定稿口径）：
        #   「同一张图账本只记一条」保留 —— 用户自己选的例外，不算去重功能；
        #   「找重复、比大小、留大删小」彻底取消 —— 有更专业的软件做，不造轮子。
        # 所以撞车时**一律不动库里那张旧图**：新落盘的这份进隔离区（可找回），
        # 账本不再加行。并发撞车同样处理。
        if self._dedup_lock is None:
            self._dedup_lock = asyncio.Lock()
        async with self._dedup_lock:
            dup_id = None
            batch_clash = False
            if out.sha256:
                if out.sha256 in self._seen_sha:
                    dup_id, batch_clash = -1, True
                else:
                    dup_id = find_duplicate_sha(self.db, out.sha256, -1)
                    if dup_id is None:
                        self._seen_sha.add(out.sha256)
            # V0.0.6：不再用感知哈希「找长得像的图」。用户定稿重复图片
            # 彻底不做，只有 sha256 完全相同才算「同一张图，账本只记一条」。
        if dup_id is not None:
            rep.dedup += 1
            self._purge_to_trash(out.path)
            self.db.audit("dedup", "ledger-only", img_url,
                          "同批并发撞车" if batch_clash else f"账本已有 #{dup_id}")
            return
        # 链接文字里命中的「要找什么」词也记到图上，用户能看见这张图
        # 是因为哪个词被收进来的（以前只有手填的关键词）。
        tags = _find.matched_want(terms, img_url, album, page_url) + [
            t for t in task.keywords if t]
        rec = ImageRecord(id=None, source_url=img_url, task_id=task.id,
                          sha256=out.sha256, phash=out.phash,
                          width=out.width, height=out.height,
                          bytes=out.bytes, path=str(out.path), tags=tags,
                          fmt=fmt, taken_at=taken_at)
        decision, note = await self._to_thread(
            self._apply_ai, task, rec, out.path, rep)
        if decision == "keep":
            pass  # 入库（in_quarantine 默认 False）
        elif decision == "drop":
            rep.ai_filtered += 1
            # 用审核替代隔离：不命中主题也进审核队列，文件保留等用户决定
            rec.in_quarantine = True
            rec.needs_review = True
            rec.review_reason = _reasons.humanize(note)
            self.db.audit("ai", "needs-review", img_url, note)
        else:  # "review"：置信度不足，留文件等用户决定
            rep.ai_review += 1
            rec.in_quarantine = True
            rec.needs_review = True
            rec.review_reason = _reasons.humanize(note)
            self.db.audit("ai", "needs-review", img_url, note)
        upsert_image(self.db, self._stage_to_review(rec))

    def _insecure_entry(self, entries) -> bool:
        """编号 33：入口 URL 里有没有"跳过证书校验"的域名（快路径用）。

        与 Fetcher._is_insecure_host 同一套匹配规则（含子域），只是这里
        一次看整批入口 —— gallery-dl 一个进程只吃一份配置，没法按 URL 分档，
        所以"只要有一个入口要不校验，这一批都不校验"。宁松不严：用户既然
        把这个域列进来，就是知道它是自签的。
        """
        try:
            rules = [x.strip().lower().lstrip("*").lstrip(".")
                     for x in str(getattr(self.cfg, "ssl_insecure_hosts", "") or "")
                     .replace("\n", ",").replace(" ", ",").split(",")
                     if x.strip()]
            if not rules:
                return False
            for u in entries or []:
                host = (urlsplit(u).hostname or "").lower()
                if not host:
                    continue
                for rule in rules:
                    if host == rule or host.endswith("." + rule):
                        return True
        except Exception:
            return False
        return False

    async def _run_via_gallery_dl(self, task, rule, entries, rep: RunReport):
        from .backends.gallery_dl import (GalleryDLBackend, is_bare_homepage)
        from .dedup.hash import sha256_file, phash_image, image_dimensions, is_low_variance

        folder = (rule.name if rule
                  else (domain_of(entries[0]) if entries else "downloads"))
        dest = Path(task.output_dir or self.cfg.library_dir) / folder
        # r36：这里别再直接用 task.proxy 原文。ProxyPool.resolve() 才做统一解析；
        # 直接把任务里存的 socks5:// 塞给 gallery-dl，本机没装 PySocks 会
        # 直接 InvalidSchema（"Missing dependencies for SOCKS support"），
        # 现象是「快路径=True、一张图都没下、任务却只写着 error」。
        from .proxy import ProxyPool
        _pool = ProxyPool(self.db)
        proxy_url = _pool.resolve(
            task.proxy_id,
            task.proxy if task.use_proxy else None,
            self.cfg.proxy
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY"))
        be = GalleryDLBackend(dest_root=dest,
                              proxy=proxy_url,
                              retries=self.cfg.retry, timeout=self.cfg.timeout,
                              sleep=max(task.delay, 0.5),
                              directory=[],          # 全部图片直接放这个文件夹里
                              write_metadata=False,   # 不再生成 .json 边车
                              # 2026-10-26：per-site UA 例外。danbooru 站方明确
                              # 允许 gallery-dl 自带 UA，反而把「浏览器 UA」当
                              # 可疑请求 → Cloudflare 403（真机实测）。这类站
                              # 一律不给 UA 覆盖，用 gallery-dl 默认值。
                              user_agent=(""
                                          if not ua_override_allowed(
                                              entries[0] if entries else "")
                                          else getattr(self.cfg, "user_agent", "")),
                              cookie_browser=getattr(self.cfg, "cookie_browser", ""),
                              # 2026-10-28（编号 33）：证书校验交给同一个口径
                              # —— 全局关了校验、或该入口域在"跳过校验"名单里，
                              # 快路径也别去校验（否则自签站永远 0 图）。
                              verify=(bool(getattr(self.cfg, "ssl_verify", True))
                                      and not self._insecure_entry(entries)))
        t0 = time.monotonic()
        # R184：先把「站点裸首页」这类入口挑出来 —— 它们喂给 gallery-dl
        # 基本无图可下，每喂一次就弹一次黑控制台窗口（抢键盘焦点）。
        skipped_home = 0
        real_entries = []
        for url in entries:
            try:
                if is_bare_homepage(url):
                    skipped_home += 1
                    LOG.info("⏭ 跳过裸首页入口（无图可下，还会弹黑窗）: %s", url)
                    continue
            except Exception:
                pass
            real_entries.append(url)
        if skipped_home and not real_entries:
            rep.errors.append(
                f"入口为站点首页（{skipped_home} 个），gallery-dl 无法从首页"
                f"直接获取图片，请改用具体图集或帖子网址")
        for url in real_entries:
            try:
                await self._consume_gallery_dl(task, be, url, rep)
            except asyncio.CancelledError:
                # 用户停止任务：终止 gallery-dl 子进程后再传播取消
                be.stop()
                # V0.0.78：关窗口流程是「先停队列 → 再 db.close()」，走到这里
                # 库可能已关；再写 status='stopped' 会抛 "closed database"，
                # 被 queue.py 的宽 except 吞掉后任务被误记 ERROR 而非 STOPPED。
                # 照搬 BFS 路径（run() 收尾）的 is_closed 自查写法。
                if self.db.is_closed():
                    LOG.warning("⚠ 数据库已关闭，跳过停止状态写库")
                else:
                    self.db.execute(
                        "UPDATE tasks SET status='stopped', "
                        "updated_at=datetime('now') WHERE id=?", (task.id,))
                raise
        rep.elapsed = time.monotonic() - t0
        # r37：快路径是 gallery-dl 自己落盘的（不走 Downloader），设置里的
        # 「保存时转成 JPG」在这儿补一次 —— 否则开了开关以后 BFS 路径转了、
        # 快路径还在落一堆 webp/avif，图库里两种后缀混着，灯箱照样看不了 avif。
        # 放在循环外只扫一次 dest（gallery-dl 已经下载完），成本低。
        try:
            if getattr(self.cfg, "convert_to_jpg", False):
                from .convert_img import convert_tree_to_jpg
                _n, _bad = convert_tree_to_jpg(dest)
                if _n:
                    rep.downloaded += _n
                    LOG.info("convert_to_jpg: 目录 %s 转换 %d 张（失败 %d）",
                             dest, _n, _bad)
        except Exception as e:
            LOG.warning("convert_to_jpg 扫描失败（不影响采集）: %s", e)
        # V0.0.78：同 BFS 路径收尾的 is_closed 自查（关窗口时库先关）。
        # 库已关就别再写 status='done'，否则抛错被 queue.py 宽 except 吞成
        # ERROR，用户看到的是「任务失败」而不是干净的提前收工。
        if self.db.is_closed():
            rep.errors.append("数据库已关闭，本次任务未能正常收尾")
            LOG.warning("⚠ 数据库已关闭，跳过任务收尾写库")
            return rep
        self.db.execute("UPDATE tasks SET status='done', updated_at=datetime('now') "
                        "WHERE id=?", (task.id,))
        self.db.audit("engine", "run(gallery-dl)", str(task.id),
                      f"dl={rep.downloaded} dup={rep.dedup} bl={rep.blacklist_hit}")
        return rep

    async def _consume_gallery_dl(self, task, be, url: str, rep: RunReport):
        """gallery-dl 流式消费：每落一个文件立即走 去重/AI/入库 管线。

        旧行为是等 gallery-dl 全部跑完才返回文件列表（一个首页会翻几百页，
        期间图库/统计/实时墙全是空的）；现在边下边入库。
        """
        from .dedup.hash import sha256_file, phash_image, image_dimensions, is_low_variance
        q: asyncio.Queue = asyncio.Queue()
        LOG.info("▶ 正在浏览网页/下载入口: %s", url)
        # V0.0.6：produce 跑在线程池里，直接在别的线程对 asyncio.Queue
        # 调 put_nowait 不是线程安全的（唤醒逻辑可能丢失，主协程卡在
        # q.get() 不动、图明明在下却像死住）。统一排回事件循环里投递。
        loop = asyncio.get_running_loop()

        def _emit(item):
            try:
                loop.call_soon_threadsafe(q.put_nowait, item)
            except RuntimeError:
                pass          # 循环已关（用户点了停止），没必要再投递

        def produce():
            try:
                for f in be.download_iter(url):
                    _emit(f)
            except Exception as e:
                _emit(e)
            finally:
                _emit(None)

        prod = asyncio.create_task(self._to_thread(produce))
        try:
            while True:
                f = await q.get()
                if f is None:
                    break
                if isinstance(f, Exception):
                    rep.errors.append(f"gallery-dl: {f}"[:120])
                    # F7（R154）：快路径的失败粒度是**入口 URL**（gallery-dl
                    # 内部重试/翻页全在子进程里，拿不到单图 URL）—— 按入口
                    # 记一条，用户至少能对着这个网址单条重试。
                    try:
                        self.db.record_failed_url(
                            task.id, url, f"gallery-dl: {f}"[:300])
                    except Exception:
                        LOG.debug("failed_urls 落库失败（忽略）: %s", url,
                                  exc_info=True)
                    continue
                if f.suffix.lower() not in IMAGE_EXTS:
                    continue
                sz = f.stat().st_size
                # 0 字节/损坏文件（gallery-dl 偶发留下空文件）→ 当失败处理并删除，
                # 绝不进图库，避免「0 KB 空白图」
                if sz == 0:
                    rep.failed += 1
                    LOG.warning("⚠ 跳过 0 字节空文件(防盗链/中断): %s", f.name)
                    try:
                        f.unlink(missing_ok=True)
                    except Exception:
                        pass
                    continue
                # 2026-10-24 白图根治：站点反爬返回 HTML 拦截页/详情页时，
                # gallery-dl 按 .jpg 后缀落盘 → 图库白图+尺寸未知。
                # 按文件头魔数校验，不是真图片的一律当失败、删文件、记 failed_urls。
                from .dedup.hash import looks_like_image as _looks_img
                if not _looks_img(f):
                    rep.failed += 1
                    LOG.warning("⚠ 下载内容不是图片(疑似拦截页/详情页)，已删除: %s", f.name)
                    try:
                        f.unlink(missing_ok=True)
                    except Exception:
                        pass
                    try:
                        self.db.record_failed_url(
                            task.id, url,
                            "下载到的不是图片（站点返回了网页/拦截页，"
                            "可能需要 Cookie 或代理）"[:300])
                    except Exception:
                        pass
                    if "下载内容非图片" not in rep.errors:
                        rep.errors.append(
                            "下载内容非图片：站点返回了网页/拦截页（需 Cookie 或代理）")
                    continue
                rep.downloaded += 1
                rep.bytes_downloaded += sz
                LOG.info("↓ 已下载图片: %s (%d KB)", f.name, sz // 1024)
                src = _source_from_sidecar(f, url)
                try:
                    sha = sha256_file(f)
                except Exception:
                    sha = None
                ph = None if is_low_variance(f) else phash_image(f)
                w, h = image_dimensions(f)
                # V0.0.6 定稿：零字节 / 读不出尺寸的坏图一律不收、不登记。
                # 以前这里 PIL 打不开只是留 (None, None)，照样写一行「尺寸未知」。
                if not w or not h or not sz:
                    rep.errors.append(f"坏图已跳过：{f.name}"[:120])
                    continue
                # 页面标题 / 标签文字（下载器留下的说明文件），下载前比对用
                extra_texts = _sidecar_words(f)
                # 文件大小过滤器（max_bytes，0=不限）：超大全进审核，不直接丢弃
                if getattr(self.cfg, "max_bytes", 0) and sz > self.cfg.max_bytes:
                    self.db.audit("filter", "too-large-review", str(f),
                                  f"{sz} > {self.cfg.max_bytes}")
                    _rec = ImageRecord(
                        id=None, source_url=src, task_id=task.id,
                        sha256=sha, phash=ph, width=w, height=h,
                        bytes=sz, path=str(f),
                        tags=[t for t in task.keywords if t],
                        in_quarantine=True, needs_review=True,
                        review_reason=_reasons.humanize(f"{sz} > {self.cfg.max_bytes}"))
                    upsert_image(self.db, self._stage_to_review(_rec))
                    continue
                # 重复内容（V0.0.5 口径）：账本已有同图 → 新落盘这份进隔离区，
                # 库里那张原样不动、不比较大小、不删任何旧文件。
                dup_id = None
                if sha:
                    dup_id = find_duplicate_sha(self.db, sha, -1)
                # V0.0.6：感知哈希「相似判重」已彻底移除，只认 sha 完全相同。
                if dup_id is not None:
                    rep.dedup += 1
                    self._purge_to_trash(f)
                    self.db.audit("dedup", "ledger-only", str(f),
                                  f"账本已有 #{dup_id}")
                    continue
                # V0.0.78：快路径也要像 _process_image 那样做运行中词表热更新。
                # V0.0.77 的「白/黑名单增量加入后立即生效」原来只长在
                # _process_image（自研爬虫），粘网址建任务走的 gallery-dl
                # 快路径完全没接，用户运行中加的词要等下一任务才生效。
                # _refresh_terms 内部只有一次轻量指纹查询；只有词表真变了才
                # 重建分类器，没变就零开销跳过 —— 不会每张图重算昂贵模型。
                if self._refresh_terms(task):
                    self._ai_ready_for = None
                    self._prepare_ai(task, rep)
                # 内容级黑名单（黑白照等）→ 早期拦截进审核
                # R152-C6：这里以前只拿 task.blacklist ⇒ 全局黑名单对快路径
                # （gallery-dl 这一支）完全无效，用户勾了"全局"照样下回来。
                terms = self._terms or _find.collect(self.db, task, self.cfg)
                # 快路径同样走引擎的黑名单合并口径，别让「全局黑名单」
                # 只在一半路径上生效。
                avoid = list(dict.fromkeys(
                    list(terms.avoid) + self._blacklist_rules(task)))
                # V0.0.6：以前把「要找的词」(task.keywords) 当标签塞进黑名单
                # 比对文字里，还把用户原有图库那套规则漏在标题之外 —— 现在
                # 只拿网址 + 页面标题/标签文字比，且命中就**不入库**：
                # 用户填黑名单要的是「这类图别下回来」，不是「下回来再等审」。
                # 文件是刚落盘的新图，搬进回收区找得回，绝不真删。
                if _hit_blacklist(src, [url] + extra_texts, avoid, path=f):
                    rep.blacklist_hit += 1
                    self.db.audit("blacklist", "pre-collect-skip", src,
                                  "网址、标题或标签命中排除词")
                    self._purge_to_trash(f, freshly_downloaded=True)
                    LOG.info("⛔ 黑名单命中→不收（已存进回收区）: %s", src)
                    continue
                # AI 排除词：用审核替代隔离/删除——确信命中也先进审核队列
                excl, note = await self._to_thread(self._ai_excluded, task, f)
                if excl in ("drop", "review"):
                    if excl == "drop":
                        rep.ai_excluded += 1
                    else:
                        rep.ai_review += 1
                    self.db.audit("ai", "excluded-review", src, note)
                    _rec = ImageRecord(
                        id=None, source_url=src, task_id=task.id,
                        sha256=sha, phash=ph, width=w, height=h,
                        bytes=sz, path=str(f),
                        tags=[t for t in task.keywords if t],
                        in_quarantine=True, needs_review=True,
                        review_reason=_reasons.humanize(note))
                    upsert_image(self.db, self._stage_to_review(_rec))
                    LOG.info("⚠ AI排除→进审核: %s (%s)", src, note)
                    continue
                # V0.0.6：以前把本地文件路径 str(f) 也当「链接文字」传进去
                # （毫无意义），现在只传网址 + 页面标题/标签。命中就不入库。
                if self._keyword_blocked(task, src, url, *extra_texts):
                    rep.keyword_filtered += 1
                    self.db.audit("filter", "pre-collect-skip", src,
                                  "命中排除词")
                    self._purge_to_trash(f, freshly_downloaded=True)
                    continue
                rec = ImageRecord(
                    id=None, source_url=src, task_id=task.id,
                    sha256=sha, phash=ph, width=w, height=h,
                    bytes=sz, path=str(f),
                    tags=_find.matched_want(terms, src, str(f), url)
                         + [t for t in (task.keywords or []) if t])
                decision, note = await self._to_thread(
                    self._apply_ai, task, rec, f, rep)
                if decision == "keep":
                    LOG.info("✓ AI命中→保留 score=%.2f tags=%s: %s",
                              rec.score, ",".join(rec.tags), src)
                elif decision == "drop":
                    rep.ai_filtered += 1
                    # 用审核替代隔离：不命中主题也进审核队列，文件保留等用户决定
                    rec.in_quarantine = True
                    rec.needs_review = True
                    rec.review_reason = _reasons.humanize(note)
                    self.db.audit("ai", "needs-review", src, note)
                    LOG.info("✗ AI未命中→进审核 score=%.2f: %s (%s)", rec.score, src, note)
                else:  # "review"
                    rep.ai_review += 1
                    rec.in_quarantine = True
                    rec.needs_review = True
                    rec.review_reason = _reasons.humanize(note)
                    self.db.audit("ai", "needs-review", src, note)
                    LOG.info("⚠ AI置信度不足→进审核 score=%.2f: %s (%s)", rec.score, src, note)
                upsert_image(self.db, self._stage_to_review(rec))
        finally:
            # 生产者收尾（停止时终止子进程）
            be.stop()
            if not prod.done():
                try:
                    await asyncio.wait_for(prod, timeout=10)
                except Exception:
                    prod.cancel()

    def _ai_excluded(self, task, path) -> tuple[str, str]:
        """AI 排除词：返回三态

          "drop"   : 确信命中排除词（分数达标、且离阈值够远）→ 判"不命中"，文件
                     搬去待审核目录交给人看，本程序不会自己把文件删掉
          "review" : 置信度不足（分数在阈值附近）→ 进审核队列，先不删、留待用户决定
          "pass"   : 没命中排除词

        精度保护：只有当「排除词分数 > 主题词分数」时才删。否则一张明明命中
        主题（如白丝）又顺带带点排除概念（如微胖）的图会被误删——主题优先。
        """
        if not self._excl_clf:
            return "pass", ""
        try:
            pairs = self._excl_clf.classify(Path(path))
        except Exception:
            return "pass", f"ai error"
        if not pairs:
            return "pass", ""
        best_label, best_score = pairs[0]
        thr = float(task.ai_threshold or 0.0)
        # V0.0.7：只被判掉过一次的词还在观察期，要求「明显更像」才送审核。
        # CLIP 在候选很少时总能把每张图算成「最像的那个」，不抬高门槛会
        # 把整批新图全推进待审核 —— 比多下几张更烦人。反复判掉 ≥2 次的
        # 词已经是硬反馈，仍用原来的阈值。
        _soft = getattr(self, "_soft_excl_labels", set()) or set()
        thr_excl = (max(thr, 0.55)
                    if str(best_label).strip().lower() in _soft else thr)
        margin = getattr(self.cfg, "review_margin", 0.12)
        # 精度：若图片同时强命中主题词，主题优先，不误删
        if self._clf:
            try:
                incl = self._clf.classify(Path(path))
                if incl and incl[0][1] >= best_score:
                    return "pass", (f"ai-exclude skipped: theme "
                                    f"{incl[0][0]}={incl[0][1]:.2f} >= "
                                    f"{best_label}={best_score:.2f}")
            except Exception:
                pass
        mode = getattr(task, "ai_exclude_match_mode", "any") or "any"
        if mode == "all":
            # 全部排除词都达到阈值才删除（AND）
            drop = len(pairs) > 0 and all(s >= thr_excl for l, s in pairs)
        else:
            # 命中任意一个排除词即删除（OR，默认行为）
            drop = best_score >= thr_excl
        if not drop:
            return "pass", ""
        # 置信度不足（分数离阈值很近）→ 进审核，不删除
        if abs(best_score - thr_excl) <= margin:
            return "review", f"ai-exclude uncertain={best_label} score={best_score:.2f}"
        return "drop", f"ai-exclude hit={best_label} score={best_score:.2f}"

    def _apply_ai(self, task, rec: ImageRecord, path, rep=None) -> tuple[str, str]:
        """本地模型打标 + 按主题筛选，返回三态

          "keep"   : 确信命中主题词 → 进图库
          "review" : 置信度不足（分数在阈值附近）→ 进审核队列，文件保留、等用户决定
          "drop"   : 确信不命中 → 隔离（可恢复，不删除）

        若未安装模型，则不做什么决定（直接 "keep"），绝不静默丢弃用户已下载的图。

        主题词分级（r29）：
          一级 = 入门票，决定「留 / 送审 / 丢」，逻辑跟以前一模一样；
          二级 + 三级 = 细分标签，一级命中了才去筛：
            * 有细分词命中  → 入库，命中的细分词写进标签，最深的那个当
              {keyword} 子目录名；
            * 一个都没命中  → 默认**送审核区**让人手点两级/三级标签（方案2）；
              ai_label_strict（严格模式）打开时改成当「不命中」直接隔离（方案1）。
          一级为空时整套分级不参与，行为完全退回老逻辑。
        """
        if not self._clf:
            return "keep", ""
        try:
            pairs = self._clf.classify(Path(path))
        except Exception as e:
            return "keep", f"ai error: {type(e).__name__}: {e}"
        if not pairs:
            return "keep", ""
        best_label, best_score = pairs[0]
        # 防御（#全隔离根因）：模型返回全 0（加载/推理失败，并非"真·不命中"）
        # 时，绝不应据此把用户已下载的真图隔离掉——保留进图库（或进审核），
        # 否则会出现"阈值没问题、图却全进隔离区"的诡异现象。
        if best_score <= 0.0:
            return "keep", "ai degenerate-zero score (模型可能异常); 保留进图库"
        thr = task.ai_threshold
        margin = getattr(self.cfg, "review_margin", 0.12)
        mode = getattr(task, "ai_match_mode", "any") or "any"
        any_match = [l for l, s in pairs if s >= thr]
        if mode == "all":
            # 全部主题词都达到阈值才保留（AND）；标签=全部主题词
            keep = len(pairs) > 0 and all(s >= thr for l, s in pairs)
            matched = [l for l, s in pairs] if keep else []
        else:
            # 命中任意一个主题词即保留（OR，默认行为）
            keep = len(any_match) > 0
            matched = any_match
        # 注入学习偏好：被「不喜欢」过的主题词会降低其有效分数，被「喜欢」过则抬高，
        # 让偏好真正参与"保留/隔离"决策（之前权重只写库不消费，等于白学）。
        eff = best_score
        if matched:
            try:
                tid = getattr(task, "id", None)
                if tid is not None:
                    pw = get_preferences(self.db, tid)
                    if pw:
                        # V0.0.3：偏好键统一小写，这里也要归一后再查。
                        # V0.0.7：中文手填词经 expand_labels 变成英文标签
                        # 交给模型后，偏好表里存的键仍是中文 —— 只拿英文
                        # 标签查会永远查到 0，用户选择「不喜欢」等于白点。
                        # 借 _prepare_ai 留下的反查表把两种写法都对上。
                        alias = getattr(self, "_pref_alias", {}) or {}
                        ws = []
                        for l in matched:
                            k = str(l).strip().lower()
                            vals = [pw.get(k, 0.0)]
                            for a in alias.get(k, []):
                                vals.append(pw.get(a, 0.0))
                            # 绝对值最大的那个才是用户真正表过的态
                            ws.append(max(vals, key=abs))
                        if ws:
                            avg = sum(ws) / len(ws)        # 平均偏好，约 [-2, +2]
                            eff = best_score + 0.12 * max(-2.0, min(2.0, avg))
            except Exception:
                eff = best_score
        # 偏好强烈反对（有效分已低于阈值）却仍被模型判命中：不放行，转「待审核」
        # 让人确认——绝不静默丢弃已下载的真图，也不静默保留，交给审核队列。
        if keep and eff < thr:
            return "review", (f"ai preference-down for "
                              f"{task.ai_labels or task.keywords} "
                              f"(best={best_label} {best_score:.2f}, pref_eff={eff:.2f})")
        rec.score = float(best_score)
        # ---- 第 2..N 级：只负责「细分」（打标签 + 分子目录），不负责留/扔 ----
        # 2026-10-28：从「固定二三级 + 一个全局严格」扩成「无限分级 + 每级独立严格」。
        # 判定规则集中在 label_levels.walk_levels（纯函数，好单测）：
        #   · 这一层有词且命中 → 记下最深命中词，继续下一层；
        #   · 这一层有词但没命中 → 勾了严格就整图判「不命中」，
        #                          没勾就停在上一层（保留，不送审）；
        #   · 细分层一个都没命中 → 维持老行为：送审核区（除非严格层已判丢）。
        levels = _lvl.levels_of(task)
        stricts = _lvl.stricts_of(task)
        score_of = {l: float(s) for l, s in pairs}
        deep_hit: list[str] = []
        # 只有「填了一级」且「填了细分层」才请出分级判定：一级留空时细分词只当
        # 标签用，绝不允许严格模式在没有入门票的情况下把图一隔离了事。
        if keep and levels and levels[0] and len(levels) > 1:
            def _hit(w: str) -> bool:
                return _best_score(score_of, [w])[1] >= thr

            kind, _deepest, deep_hit, why = _lvl.walk_levels(levels, stricts, _hit)
            if kind == "drop":
                keep = False          # 方案1 活版：严格层没命中 → 当「不命中」
            elif kind == "review":
                # 方案2：只命中一级 → 送审核区，让人手点细分标签。
                # 千万别在这直接 drop：宁可漏放不可误杀（r29 铁律）。
                return ("review",
                        f"ai {why} for "
                        f"{task.ai_labels or task.keywords} "
                        f"(levels={len(levels)}, "
                        f"best={best_label} {best_score:.2f}, thr={thr:.2f})")
        if keep:
            tags = list(dict.fromkeys(
                list(rec.tags or []) + list(matched) + deep_hit))
            rec.tags = tags
            if rep is not None:
                rep.ai_tagged += 1
            # 拿得准 → 入库；分数在阈值附近 → 进审核
            if thr > 0 and abs(eff - thr) <= margin:
                return "review", (f"ai uncertain for "
                                  f"{task.ai_labels or task.keywords} "
                                  f"(best={best_label} {best_score:.2f}, mode={mode})")
            return "keep", ("ai deep tag: " + ",".join(deep_hit)
                            if deep_hit else "")
        # 不命中主题词（或严格模式下细分没命中）：拿得准 → 隔离；置信度不足 → 审核
        if abs(eff - thr) <= margin:
            return "review", (f"ai uncertain for "
                              f"{task.ai_labels or task.keywords} "
                              f"(best={best_label} {best_score:.2f}, mode={mode})")
        return "drop", (f"ai: no match for {task.ai_labels or task.keywords} "
                        f"(best={best_label} {best_score:.2f}, mode={mode})")

    # ---- helpers ----
    def _resolve_adapter(self, name: str) -> Path | None:
        # R152-C1：name 来自任务表单（用户可编辑），直接拼路径等于路径穿越。
        # 先过白名单（与 Web 端导入闸同源），不合法按"不存在"处理，
        # 由调用方给出中文提示——宁可任务起不来，也不能把文件写到图库外。
        n = (name or "").strip()
        if not ADAPTER_NAME_OK.match(n):
            return None
        for ext in (".yaml", ".yml", ".json"):
            p = self.cfg.rules_dir / (n + ext)
            if p.exists():
                return p
        return None

    # V0.0.6：`_phash_dup_id`（感知哈希相似判重）已彻底删除 —— 用户定稿
    # 「重复图片不做了，有更专业的软件」，软件里不再找相似、不再聚类。

    # V0.0.5：`cluster_dedupe`（相似图聚类去重全库）已彻底删除 ——
    # 用户拍板「重复图片功能不做了，有专业软件，不重复造轮子」。
    # 软件内不再找重复、不再比大小、不再整库搬图；菜单入口同批撤掉。

    def _purge_to_trash(self, path, *, freshly_downloaded: bool = False) -> None:
        """重复兜底：把这份文件移进 隔离区/_purged（可找回），不直接消失。

        V0.0.5：本程序已不做「找重复比大小」。这里只在「账本已有同图、
        这份新落盘的多余文件要清走」时用，且只动刚下载的这份。
        use_recycle=True（默认）→ shutil.move 进 _purged；
        移动失败不静默直删 —— 改成留在原地并报日志，由用户决定。
        """
        p = Path(path) if path else None
        if p is None or not p.exists():
            return
        # V0.0.6 红线：这里**永远不删文件**。以前 use_recycle=False 会
        # unlink、移动失败也会回退 unlink —— 都是「悄悄真删」。现在一律
        # 只往软件自己的 _purged 搬（找得回），搬不动就留在原地并记日志。
        # 注意：只动刚下载进软件目录的这份新文件，绝不碰用户原有图库。
        if not freshly_downloaded:
            # 只有调用点明确说「这是刚下载进任务目录的新文件」时才跳过
            # 这道闸；其它情况一律先认目录，判不清就不动。
            try:
                from .safe_delete import is_software_file
                if not is_software_file(p, self.cfg):
                    LOG.info("外部原图不动，仅账本记一条：%s", p)
                    return
            except Exception:
                return
        try:
            trash = Path(self.cfg.isolate_dir) / "_purged"
            trash.mkdir(parents=True, exist_ok=True)
            tgt = trash / p.name
            i = 1
            while tgt.exists():
                tgt = trash / f"{p.stem}({i}){p.suffix}"
                i += 1
            shutil.move(str(p), str(tgt))
        except Exception as e:
            LOG.warning("重复落盘文件搬进回收站失败，已原样保留：%s（原因：%s）",
                        p, e)

    # V0.0.5：`_keep_largest`（留大删小）已删除。重复图片处理专项取消，
    # 两条下载路径的「账本只记一条」改在调用点直接完成（输家新文件进
    # 隔离区，库里旧图一律不动）。

    # R152-A10：原 ``_to_quarantine()`` 已删 —— 黑名单追溯/去重路径改用
    # 「审核替代隔离」（apply_blacklist_existing 只标记 needs_review，
    # 不再搬文件）后，这个旧「搬文件进隔离区」的方法全仓零调用点
    # （tools/audit_dupes.py docstring 里留有它当年吞 WinError 17 的历史注记）。

    def _stage_to_review(self, rec) -> "ImageRecord":
        """把『待审核』图片(in_quarantine 且 needs_review)从下载目录物理移到
        review_dir/<task_id>/，让『审核中』与『图库』在文件层面彻底分开两个文件夹。

        跨盘安全（shutil.move 自动回退为复制+删除）；目标重名加序号；移动失败
        （如源已被删）不抛错，原样返回 rec（文件本就不在，不影响入库）。
        """
        if not (getattr(rec, "in_quarantine", False)
                and getattr(rec, "needs_review", False)):
            return rec
        p = getattr(rec, "path", None)
        if not p:
            return rec
        src = Path(p)
        if not src.exists():
            return rec
        self.cfg.review_dir.mkdir(parents=True, exist_ok=True)
        dest_dir = self.cfg.review_dir / str(getattr(rec, "task_id", 0) or 0)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        n = 1
        while dest.exists():
            dest = dest_dir / f"{src.stem}({n}){src.suffix}"
            n += 1
        try:
            shutil.move(str(src), str(dest))
        except Exception:
            try:
                shutil.copy2(str(src), str(dest))
                src.unlink(missing_ok=True)
            except Exception:
                return rec
        rec.path = str(dest)
        self.db.audit("engine", "stage-to-review", str(dest), str(src))
        return rec


def _today() -> str:
    import datetime
    return datetime.date.today().strftime("%Y-%m-%d")


def apply_blacklist_existing(db: Database, task, isolate_dir=None,
                             cfg=None) -> int:
    """把黑名单追应用到「已经下载进图库」的图片。

    命中的图片：文件移入隔离区（不删除），DB 记录标记 blacklisted 并清空
    path（图库不再显示）。返回处理的张数，供 UI 提示。
    """
    moved = 0
    rows = db.query(
        "SELECT id, path, source_url, tags FROM images "
        "WHERE task_id=? AND path IS NOT NULL AND blacklisted=0",
        (task.id,),
    )
    for r in rows:
        try:
            tags = json.loads(r["tags"] or "[]")
        except Exception:
            tags = []
        # R152-C6：追溯也走合并点（以前只对单个任务自己的黑名单生效，
        # 用户在另一个任务里加的全局词对已入库的历史图一条都拦不住）。
        if not _hit_blacklist(r["source_url"], tags, merged_blacklist(task, cfg),
                              path=r["path"]):
            continue
        # 用审核替代隔离：命中黑名单也只标记待审核（文件保留），不移动/不删
        db.execute(
            "UPDATE images SET blacklisted=1, in_quarantine=1, needs_review=1, "
            "review_reason=? WHERE id=?",
            (_reasons.RETROACTIVE, r["id"]))
        db.audit("blacklist", "retroactive-review", str(r["id"]),
                 r["source_url"][:120])
        moved += 1
    return moved


def backfill_local_files(db: Database, task_id: int | None = None, *,
                         on_progress=None, stop=None,
                         re_filter_ai: bool = False) -> int:
    """把磁盘上已下载、但没进数据库的图片补写进 images 表，且**与实时管线同样的过滤**
    ——跳过空/缺失/损坏文件，命中黑名单的隔离，配了主题词但内容不匹配的也隔离，
    杜绝再次污染图库（之前的版本无脑全量灌入，导致图库被无关图淹没）。

    典型场景：用户已在磁盘看到图、界面却空着。扫描每个任务的 output_dir，按绝对路径
    当稳定 source_url 幂等补写。只读扫描 + 插入，绝不动磁盘文件。返回新增张数。

    为支持大目录不卡死 GUI：
    - ``on_progress(scanned, added, current_path)`` 每处理若干文件回调一次；
    - ``stop`` 传 ``threading.Event``，回调间检查，置位即中止（已写入的保留）；
    - ``re_filter_ai`` 默认 False：跳过昂贵的逐图 CLIP 分类（回填核心是"补链接"，
      不强制重跑主题过滤），也跳过 PIL 开图取尺寸——只校验后缀+非空字节，速度极快；
      仅当命中黑名单含"黑白照"等需要内容识别的关键词时，才按需开图判灰度。
    """
    from .models import ImageRecord
    from .repo import get_task, list_tasks, upsert_image
    try:
        from PIL import Image
    except Exception:
        Image = None

    if task_id is not None:
        tasks = [get_task(db, task_id)]
    else:
        tasks = list_tasks(db)
    # 2026-11-05（与 tidy._is_local_folder_task 同源收窄，R152-B4 补漏）：
    # learn_folder / import_folder 建的**本地文件夹任务**，其 output_dir 就是
    # 用户自选的相册/文档目录。下面 0 字节清理那段会对 rglob 出来的 sz==0
    # 文件直接 unlink —— 扫到用户相册 = 越界删用户文件。整棵排除这类任务，
    # 与 tidy.clean_scan_dirs 的白名单口径对齐（同一个越界问题在两个入口只
    # 修过一个）。用户显式指定 task_id 视为知情同意，仍然允许回填该任务，
    # 否则「回填整个库」会把用户相册里的 0 字节文件静默清掉。
    if task_id is None:
        try:
            from .tidy import _is_local_folder_task as _is_local
            _before = len(tasks)
            tasks = [t for t in tasks if t and not _is_local(t)]
            _skipped = _before - len(tasks)
            if _skipped:
                LOG.info("回填跳过 %d 个本地文件夹任务（不碰用户相册目录）",
                         _skipped)
        except Exception:
            pass
    tasks = [t for t in tasks if t and t.output_dir]
    if not tasks:
        return 0

    def build_theme_clf(task):
        ai_labels = list(task.ai_labels or [])
        if not ai_labels or not re_filter_ai:
            return None
        try:
            from .classify.labels_zh import expand_labels
            from .classify.clip_model import LocalClassifier
            from .config import user_data_dir
            md = getattr(task, "ai_model_path", "") or (user_data_dir() / "models" / "clip")
            return LocalClassifier(labels=expand_labels(ai_labels), model_dir=md)
        except Exception:
            return None

    exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".jxl"}
    added = 0
    scanned = 0
    for t in tasks:
        root = Path(t.output_dir)
        if not root.exists():
            continue
        LOG.info("回填扫描 任务 #%s 目录=%s", t.id, root)
        tags = [k for k in (t.keywords or []) if k]
        # R152-C6：回填同样并上全局词（老用户历史目录里的图也一样要拦）。
        black = merged_blacklist(t)
        ai_labels = list(t.ai_labels or [])
        ai_thr = float(getattr(t, "ai_threshold", 0.0) or 0.0)
        clf = build_theme_clf(t)
        # 仅当黑名单含"黑白照"等灰度关键词时，才需要逐图开图判灰度
        need_gray = bool(black) and _any_grayscale_keyword(black)
        n_keep = n_black = n_quar = 0
        for p in root.rglob("*"):
            if stop is not None and stop.is_set():
                LOG.info("回填被用户取消，已写入 %d 张保留", added)
                return added
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            source = f"file://{p.resolve()}"
            rpath = str(p.resolve())
            # 幂等：同 source_url **或同磁盘路径**已存在就跳过。
            # 只查 source_url 不够：正常下载行的 source_url 是 http 地址，
            # 回填的合成 URL 永远查不到它 → 同一文件被灌进第二行 → 图库重复。
            if db.query("SELECT 1 FROM images WHERE source_url=? OR path=?",
                        (source, rpath)):
                continue
            try:
                sz = p.stat().st_size
            except Exception:
                continue
            if sz == 0:
                # V0.0.6 红线：回填是「只读扫描 + 补登记」，**绝不动磁盘文件**。
                # 以前这里会 unlink 掉 0 字节文件；如果回填的是用户原有图库
                # （F 盘，任务 output_dir 就是用户自选目录），等于删了用户盘上
                # 的文件。0 字节图一律不收、不登记，但也不删 —— 留在原地，
                # 由用户自己决定怎么处理。
                LOG.info("回填·跳过 0 字节空文件（不删不改、不登记）: %s", source)
                continue
            scanned += 1
            # 快速路径：只读图片头拿尺寸（Image.open 只读 header，开销极小）。
            # 以前把尺寸留空 → 回填行全是「尺寸未知」；现在直接补齐。
            w = h = None
            if Image is not None:
                try:
                    with Image.open(p) as _im:
                        w, h = _im.size
                except Exception:
                    w = h = None
            # 读不出尺寸仍照常登记：回填是「磁盘上已有文件补登记」，不能因为
            # 本机没装 HEIC/AVIF 看图插件、或文件头能认但内容损坏，就把用户
            # 已有的图从图库里排除。「坏图一律不收」是**导入**那两处的口径，
            # 0 字节也已经在上面跳过，两者不要混为一谈（V0.0.6 复核）。
            # V0.0.6 方向修正：用户原有图库（本地文件夹任务）是「教机器喜欢
            # 什么」的输入，不该再拿黑名单/主题词考一次试、把用户的图丢进
            # 审核区。这类任务只补登记，一律进图库；下载任务目录维持原口径。
            local_origin = False
            try:
                from .tidy import _is_local_folder_task as _is_local
                if _is_local(t):
                    # 只有「output_dir 在软件目录之外」才算用户原有图库。
                    # 下载任务把图存进软件自己的 library 时，哪怕入口写的是
                    # 本地路径，也照常按全局黑名单过滤（C6 护栏）。
                    try:
                        from .config import get_active_config
                        from .safe_delete import is_software_file
                        _cfg = get_active_config()
                        local_origin = not (
                            _cfg is not None and is_software_file(root, _cfg))
                    except Exception:
                        local_origin = True
            except Exception:
                local_origin = False
            # 黑名单（含内容级灰度识别）→ 进审核（用审核替代隔离，文件保留）
            if not local_origin and black and _hit_blacklist(
                    source, tags, black, path=rpath if need_gray else None):
                rec = ImageRecord(
                    id=None, source_url=source, task_id=t.id,
                    sha256=None, phash=None, width=w, height=h, bytes=sz,
                    path=rpath, tags=tags,
                    blacklisted=True, in_quarantine=True, needs_review=True,
                    review_reason=_reasons.BACKFILL_BLACKLIST)
                upsert_image(db, rec)
                added += 1; n_black += 1
                LOG.info("⛔ 回填·黑名单→审核: %s", source)
                continue
            # 主题词过滤（仅 re_filter_ai=True 时启用，开销大）
            if not local_origin and re_filter_ai and ai_labels and clf is not None:
                try:
                    pairs = clf.classify(rpath)
                except Exception:
                    pairs = []
                mode = getattr(t, "ai_match_mode", "any") or "any"
                if mode == "all":
                    matched = bool(pairs) and all(
                        s >= ai_thr for l, s in pairs)
                else:
                    matched = bool(pairs) and pairs[0][1] >= ai_thr
                if not matched:
                    best = pairs[0][1] if pairs else 0.0
                    rec = ImageRecord(
                        id=None, source_url=source, task_id=t.id,
                        sha256=None, phash=None, width=w, height=h, bytes=sz,
                        path=rpath, tags=tags,
                        blacklisted=False, in_quarantine=True,
                        needs_review=True,
                        review_reason=(f"回填本地图片库时，AI 判定没匹配上主题词"
                                       f"（最高 {best:.2f} 分）"))
                    upsert_image(db, rec)
                    added += 1; n_quar += 1
                    LOG.info("✗ 回填·AI未命中→审核 score=%.2f mode=%s: %s",
                             best, mode, source)
                    continue
            # 通过所有过滤 → 进图库
            rec = ImageRecord(
                id=None, source_url=source, task_id=t.id,
                sha256=None, phash=None, width=w, height=h, bytes=sz,
                path=rpath, tags=tags,
                blacklisted=False, in_quarantine=False)
            upsert_image(db, rec)
            added += 1; n_keep += 1
            LOG.info("✓ 回填·进图库: %s", source)
            # 每 200 个文件回报一次进度，避免回调太频繁
            if on_progress is not None and (added % 200 == 0):
                try:
                    on_progress(scanned, added, source)
                except Exception:
                    pass
        LOG.info("回填 任务 #%s 完成: 新增=%d (进图库=%d 隔离=%d 黑名单=%d)",
                 t.id, n_keep + n_quar + n_black, n_keep, n_quar, n_black)
        if on_progress is not None:
            try:
                on_progress(scanned, added, str(root))
            except Exception:
                pass
    if on_progress is not None:
        try:
            on_progress(scanned, added, "")
        except Exception:
            pass
    return added
