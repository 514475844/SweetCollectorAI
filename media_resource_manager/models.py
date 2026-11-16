"""Data models shared across modules (stdlib dataclasses)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TaskStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    DONE = "done"
    ERROR = "error"


class PageType(str, Enum):
    LIST = "list"
    ALBUM = "album"
    IMAGE = "image"
    TAG = "tag"
    OTHER = "other"


class ReviewDecision(str, Enum):
    LIKE = "like"
    DISLIKE = "dislike"
    SKIP = "skip"
    BLACKLIST = "blacklist"


@dataclass
class CrawlResult:
    url: str
    page_type: PageType = PageType.OTHER
    image_urls: list[str] = field(default_factory=list)
    child_links: list[str] = field(default_factory=list)
    # next-page (continuation) links, kept separate so BFS can enqueue them at
    # the SAME depth (paging one album/list must not consume the depth budget).
    next_links: list[str] = field(default_factory=list)
    depth: int = 0
    title: str = ""
    error: Optional[str] = None


@dataclass
class ImageRecord:
    id: Optional[int]
    source_url: str
    task_id: int
    sha256: Optional[str] = None
    phash: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    bytes: Optional[int] = None
    path: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    score: float = 0.0
    blacklisted: bool = False
    in_quarantine: bool = False
    collected_at: Optional[str] = None
    rating: int = 0
    favorite: bool = False
    needs_review: bool = False      # AI 分数在阈值附近、软件拿不准 → 进审核
    review_reason: str = ""         # V0.0.8：为什么进审核（中文，审核页直接显示）
    fmt: Optional[str] = None       # #68 图片格式（JPEG/PNG/...）
    taken_at: Optional[str] = None   # #68 拍摄时间（EXIF DateTimeOriginal）


@dataclass
class TaskConfig:
    id: Optional[int]
    name: str
    adapter_name: str
    entry_urls: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    proxy: Optional[str] = None
    proxy_id: Optional[int] = None
    use_proxy: bool = False
    concurrency: int = 4
    max_depth: int = 3
    delay: float = 1.0
    output_dir: Optional[str] = None
    auto_run: bool = False
    status: TaskStatus = TaskStatus.IDLE
    is_template: bool = False
    parent_id: Optional[int] = None
    priority: int = 5                      # lower = runs first
    ai_enabled: bool = False                # local AI tagging / theme filter
    ai_labels: list[str] = field(default_factory=list)
    ai_blacklist: list[str] = field(default_factory=list)   # AI 排除词：命中即删
    # #60：默认阈值从 0.5 降到 0.35。本机内置 CLIP 对**正确**匹配的分数最高
    # 也只有 0.5~0.65，0.5 等于把大半正确结果判成「未命中」扔进隔离区。
    # 老库里恰好等于旧默认 0.5 的任务会被 db.py 一次性迁移，用户自己调过的不动。
    ai_threshold: float = 0.35
    # 主题词(白名单)命中语义：any=命中一个就保留 / all=全部命中才保留
    ai_match_mode: str = "any"
    # 排除词(黑名单)命中语义：any=命中一个就删除 / all=全部命中才删除
    ai_exclude_match_mode: str = "any"
    # r29 主题词分级：一级 = 入门票（决定留/丢/送审）；二级、三级 = 细分标签。
    # 只填一级时行为跟以前一模一样；填了二/三级后，命中一级的图会再拿二三级
    # 去筛：命中就带细分标签入库（更深的词当 {keyword} 子目录），一个都没命中
    # 就送审核区让人手点标签（方案2），或者按严格模式直接当不命中（方案1）。
    ai_labels_l2: list[str] = field(default_factory=list)
    ai_labels_l3: list[str] = field(default_factory=list)
    # 严格模式（方案1 的活版）：填了二三级但一个都没命中 → 直接当「不命中」，
    # 走原来的隔离逻辑。默认关（铁律：宁可漏放不可误杀，先送人看一眼）。
    # ⚠ 2026-10-28 起它只是**旧的全局开关**：判定已改走下面两个逐层字段，
    #   它保留是因为老库/老界面/老测试都还在读它（写的时候会同步成
    #   "细分层里是否有严格层"）。
    ai_label_strict: bool = False
    #: 主题词**无限分级**（2026-10-28）：二维数组，[第1级词, 第2级词, …]。
    #: 为空时回退上面的一/二/三级三列（老任务零影响）。见 label_levels.py。
    ai_label_levels: list = field(default_factory=list)
    #: 与 ai_label_levels 逐层对齐的严格开关：True = 这一级必须命中，
    #: 否则整张图判「不命中」（每级可单独设）。
    ai_label_strict_levels: list = field(default_factory=list)
    # 图片过滤（分辨率 + 文件大小）按任务保存：每个任务可以不一样；0=不限
    min_width: int = 0
    min_height: int = 0
    min_bytes: int = 0
    max_bytes: int = 0
    last_error: str = ""
    last_run_at: str = ""
    # 定时运行：留空=不定时；"08:30"=每天 8:30；"2026-10-01 08:30"=一次性。
    # 错过的时间点会在下次软件开着时自动补跑一次（catch-up）。
    schedule_at: str = ""
    # 下载限速（#22）：rate_limit=每分钟最多下载几张（0=不限）；
    # allowed_hours="22-6" 表示只在 22 点到次日 6 点之间抓（留空/首尾相同=全天）。
    rate_limit: int = 0
    allowed_hours: str = ""
    # 站点 Cookie / 登录态（#52）：原样作为 Cookie 请求头下发，留空不发送。
    # 用于需要登录才能看的图。敏感信息仅存本地配置文件。
    # ⚠ R173：这一列以前**只在内存里**，repo.save_task 不写库、_task_from_row
    #   也不读回 ⇒ 保存后重开就是空（engine 的 getattr(task,"cookie","") 常年
    #   读空气）。现在真的落库了，见 db._migrate 的 tasks.cookie。
    cookie: str = ""
    # 任务级 User-Agent（R173）：留空 = 用全局 cfg.user_agent。
    # 为什么必须任务级：实测 xxknit 是「app UA 403 / 浏览器 UA 200」，
    # 而 danbooru 恰好**反过来**（app UA 200 / 浏览器 UA 403）⇒ 全局只有一个
    # UA 永远救不了两边。cf_clearance 又与 UA+IP 绑定，所以 UA 与 Cookie
    # 必须同一任务一起配，缺一个不可用。
    user_agent: str = ""
    # 按主题词（AI 主题词）分子目录（#28）：命中某个主题词的图放进
    # {site}/{主题词}/ 子目录；都没命中则平铺在 {site}/ 下。
    keyword_subdir: bool = False
    # 本任务最多抓多少页面（0=不限）。优先级：任务级 > 规则级 > 全局默认值。
    # 原来只有一个全局「每任务最大抓取页数」，改一次会波及所有任务，还被
    # 同名的规则字段盖住；现在每个任务各自独立，留 0 就是不设上限。
    max_pages: int = 0
