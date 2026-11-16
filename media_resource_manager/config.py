"""Configuration & path management (pure stdlib).

YAML adapter rules are parsed with PyYAML when available; JSON is always
supported. Heavy dependencies are imported lazily elsewhere so the core
(stdlib-only) can run and be tested without the GUI/AI stack.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

from .rateplan import default_plan as default_rate_plan
from .security.config_secrets import (ConfigSecretError, ConfigSecrets,
                                      restrict_permissions)
# R152-E6：config.json 敏感字段落盘加密。security.config_secrets 本体是
# 纯 stdlib（cryptography 在其内部惰性 import）⇒ 本模块的「stdlib-only
# 可直接跑」性质不变，PyInstaller 也会顺这条 import 链收集到
# security.config_secrets / security.crypto（含函数体内的 cryptography）。

APP_NAME = "media-resource-manager"

# ---- 合规基线（设置 → 合规模板）----
# off      关闭（R186：个人自用，完全不检查合规/授权 —— 不查 robots、
#          不查授权表、不自动插行；引擎两处合规门直接放行）
# loose    宽松：robots.txt 不允许时只记录警告，不阻断（自用站/已获授权站）
# standard 标准：robots.txt 禁止根目录 → 拒绝（默认，跟以前行为一致）
# strict   严格：额外校验入口路径是否被 Disallow，且授权依据不能为空
COMPLIANCE_LEVELS = {
    "off": "关闭（个人自用，完全不检查合规/授权）",
    "loose": "宽松（robots 不允许也只警告，不阻断）",
    "standard": "标准（robots 禁止则拒绝，推荐）",
    "strict": "严格（连路径也校验，且必须填授权依据）",
}
DEFAULT_COMPLIANCE_LEVEL = "off"   # 2026-10-26：个人自用，合规层已整层删除，此值仅兜底

# 敏感图模糊（#35）的默认敏感词；与 security/blur.py 的 SENSITIVE_DEFAULT 一致
_SENSITIVE_DEFAULT_WORDS = ["face", "nude", "naked", "idcard", "id card",
                            "passport", "watermark", "人脸", "身份证", "护照",
                            "裸", "水印"]

# 各基线新建任务时预填的默认排除词（strict 最保守）
COMPLIANCE_DEFAULT_EXCL = {
    "loose": [],
    "standard": [],
    "strict": ["watermark", "logo", "screenshot"],
}


def default_exclusions(level: str) -> list:
    """Return the default AI exclusion words for a compliance level."""
    return list(COMPLIANCE_DEFAULT_EXCL.get(level, []) or [])

# R189：默认 UA 改为真实 Chrome UA。旧默认 "MediaResourceManager/1.0 (...)"
# 是机器人标识 —— danbooru/konachan 直接 403、gelbooru 回 JS 挑战页，站点
# 挡的就是这个 UA。本软件是个人自用工具，采集请求「像浏览器一样」发出即可。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# R189：gallery-dl 快路径是否携带浏览器 Cookie（"chrome"/"edge"/"firefox"/
# ""=不取）。被拦站点（danbooru/gelbooru 等）带登录 Cookie 后可正常取到图。
# 取 Cookie 任何环节失败都只带 UA 继续，绝不让任务因 Cookie 失败而崩。
DEFAULT_COOKIE_BROWSER = "chrome"


LEGACY_DATA_DIR_NAME = APP_NAME   # 旧版数据目录：%APPDATA%\media-resource-manager


def _legacy_user_data_dir() -> Path:
    """旧版（2026-10-01 之前）的数据目录，只用于首次启动时的自动迁移。"""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".local" / "share"
    return base / LEGACY_DATA_DIR_NAME


def _program_dir() -> Optional[Path]:
    """软件所在目录：打包后 = exe 所在目录；源码跑 = 项目根目录。

    源码模式下 `sys.executable` 是 python.exe（其 parent 是 Scripts/ 之类，
    不能当数据目录），所以只有 frozen（PyInstaller）时才用它。
    """
    try:
        import sys
        if getattr(sys, "frozen", False):
            here = Path(getattr(sys, "executable", "")).parent
            if here and here.exists():
                return here
        return Path(__file__).resolve().parents[1]
    except Exception:
        return None


def _writable_dir(candidate: Path) -> bool:
    """候选目录能写才敢把数据放进去（Program Files / 只读盘会静默失败）。

    探针只写不删：全量测试里 user_data_dir() 会被调用上百次，早先每调一次就
    write+unlink 一个 .write_probe —— 删除动作被子系统的安全删除守卫接走，
    撞上批量删除配额直接 SystemExit(1) 把整个 pytest 会话打死。
    现在改成「探针存在就只做校验，不再删」，稳态下一次删除都没有。
    """
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write_probe"
        if probe.exists():
            # 目录要是变得不可写了，探针就读不到内容 → 让调用方退回旧位置
            with open(probe, "r", encoding="utf-8") as fh:
                return fh.read().strip() == "1"
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("1")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        with open(probe, "r", encoding="utf-8") as fh:
            return fh.read().strip() == "1"
    except Exception:
        return False


def ai_device_effective(cfg) -> str:
    """返回**生效**的推理设备：auto / gpu / npu / cpu。

    2026-10-27 加 npu：ARM 设备（高通 QNN）、AMD Ryzen AI（VitisAI/XDNA）、
    英特尔（OpenVINO）都有 NPU，以后要能在这些机器上跑推理就得先有这一档。

    规则（三处必须用同一口径，否则会出现"下拉显示自动、实际在跑 CPU"）：
      · ai_device 显式给了 gpu/npu/cpu → 用它；
      · ai_device 为空/auto：
          - 老字段 ai_gpu=False → **cpu**（老配置里"关掉 GPU 加速"就是 CPU）
          - 否则 → auto
    """
    dev = str(getattr(cfg, "ai_device", "") or "auto").lower().strip()
    if dev not in ("auto", "gpu", "npu", "cpu"):
        dev = "auto"
    if dev == "auto" and not bool(getattr(cfg, "ai_gpu", True)):
        dev = "cpu"
    return dev


def user_data_dir() -> Path:
    """数据目录 = 「软件目录/data」(便携模式，默认) → %APPDATA% (程序目录不可写) 。

    默认改成软件目录内，好处：整个文件夹拷走/拷回来就能带着任务、图、模型、
    偏好继续用，不用再管 %APPDATA%。环境变量 MRM_HOME 或只读的程序目录会退回旧位置。
    """
    env = os.environ.get("MRM_HOME")
    if env:
        return Path(env).expanduser()
    here = _program_dir()
    if here is not None:
        cand = here / "data"
        if _writable_dir(cand):
            return cand
    # 程序目录不可写（或源码目录被只读挂载）→ 退回旧位置，保证程序还能跑
    return _legacy_user_data_dir()


# ---- 旧数据自动迁移（首次在新目录启动，把 %APPDATA% 里的东西搬过来）----
# R152-F2：补 "library"（图库本体）。老用户的图全在旧目录 data\library 下，
# 漏了它 = 搬库不搬图，图库显示"磁盘已缺失"。
_MIGRATE_ITEMS = (
    "mrm.db", "config.json", "cookies.json", "rules", "isolate", "quarantine",
    "review", "thumbs", "models", "backups", "theme_arrows", "ui.json",
    "cache", "library",
)


def migrate_from_legacy() -> list:
    """把旧 %APPDATA% 目录里的内容搬到新数据目录。

    * 只搬新目录里**还没有**的同名文件/夹，绝不覆盖。
    * 搬完在原处留一个空目录 + `_migrated.txt` 说明，旧数据一条都不删。
    * 多实例同时启动也不会重复搬（靠新目录里的 `_migrated_from_appdata` 标记）。
    返回实际搬过的条目列表（供 UI/日志提示）。
    """
    # 守卫：MRM_NO_MIGRATE=1（测试/工具脚本）或数据目录不是「程序目录/data」
    # （即走了 MRM_HOME / 只读回退）时，一律不碰旧目录。
    if os.environ.get("MRM_NO_MIGRATE"):
        return []
    _prog = _program_dir()
    if _prog is None or user_data_dir() != (_prog / "data"):
        return []
    new_dir = user_data_dir()
    marker = new_dir / "_migrated_from_appdata"
    if marker.exists():
        return []
    legacy = _legacy_user_data_dir()
    if not legacy.exists() or not (legacy / "mrm.db").exists():
        marker.write_text(
            f"数据目录已改为软件目录：{new_dir}\n"
            f"（本文件只表示迁移流程已跑过；旧目录 {legacy} 保留未删）\n",
            encoding="utf-8")
        return []
    moved: list = []
    # F2：迁移前给**小文件**（db/config/cookies/ui）留一份快照到
    # new_dir/_migrate_backup/。shutil.move 跨盘 = 先复制后删源，中断时
    # 旧目录可能已删掉一半；目录项（rules/library/models...）体积大，同盘
    # 移动实为原子 rename，跨盘中断也只会两头并存（不会丢），故不复制——
    # 这是空间与安全的取舍，写在这里免得后人「优化」掉。
    import shutil
    plan = [n for n in _MIGRATE_ITEMS
            if (legacy / n).exists() and not (new_dir / n).exists()]
    if plan:
        try:
            bak = new_dir / "_migrate_backup"
            bak.mkdir(parents=True, exist_ok=True)
            for n in plan:
                src = legacy / n
                if src.is_file():
                    shutil.copy2(str(src), str(bak / n))
        except Exception:
            pass
    for name in _MIGRATE_ITEMS:
        src = legacy / name
        dst = new_dir / name
        if not src.exists():
            continue
        if dst.exists():
            continue            # 新目录已有同名内容 → 不动，避免覆盖用户数据
        try:
            if src.is_dir():
                import shutil
                shutil.move(str(src), str(dst))
            else:
                import shutil
                shutil.move(str(src), str(dst))
            moved.append(name)
        except Exception:
            pass
    try:
        marker.write_text(
            f"迁移时间：{__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"来源（旧目录，未删除）：{legacy}\n"
            f"已迁移条目：{', '.join(moved) or '（无，旧目录本来就是空的）'}\n",
            encoding="utf-8")
    except Exception:
        pass
    return moved


# 外观预设表的**世代号**。R59 把classic 拆成「真原生」并新增 violet_look，
# 老配置必须靠它区分「该不该被改名迁移」。写在模块级、且**必须在 AppConfig
# 之前**：dataclass 的字段默认值在类体求值那一刻就要看到这个名字
# （踩过一次：定义在文件后段 ⇒ import config 直接 NameError）。
# 两处各写一个数字也迟早漂 —— 漂了就是「迁移对新版配置也生效」的老 bug。
PRESET_SCHEME = 2


# R106 删掉的死字段。F3「未知键保留」对它们例外——老配置里再出现也不许
# 借 round-trip 复活（test_r106 有锁）。
_DEAD_CONFIG_KEYS = frozenset({
    "ai_slot_auto", "ai_slot_day", "ai_slot_evening",
    "ai_slot_night", "ai_slot_applied",
})


@dataclass
class AppConfig:
    db_path: Path = field(default_factory=lambda: user_data_dir() / "mrm.db")
    library_dir: Path = field(default_factory=lambda: user_data_dir() / "library")
    last_output_dir: Path = field(
        default_factory=lambda: user_data_dir() / "library")
    # V0.0.7：「从文件夹学习」上次选的**源**目录。刻意与 last_output_dir
    # 分开：后者是「下载的新图存哪」，写成用户 F 盘原图库后，新建任务会
    # 把新图直接下进原图库，而且那片目录已被保护闸锁死、以后删都删不掉。
    last_learn_dir: Path = field(
        default_factory=lambda: user_data_dir() / "library")
    rules_dir: Path = field(default_factory=lambda: user_data_dir() / "rules")
    isolate_dir: Path = field(default_factory=lambda: user_data_dir() / "quarantine")
    # 审核（待用户决定）图片的物理文件夹：与图库 output_dir 彻底分开两个文件夹，
    # 下载后判定「待审核」的图直接落在这里，批准后移回图库、不喜欢移入回收站。
    review_dir: Path = field(default_factory=lambda: user_data_dir() / "review")
    log_dir: Path = field(default_factory=lambda: user_data_dir() / "logs")
    # 物理上三个文件夹彻底分开：图库=output_dir/库目录，审核=review_dir，
    # 隔离区(回收站)=isolate_dir。任一处的文件绝不与其他两处混放。
    # 需求 16：「不喜欢」选「全局黑名单」时的落点 —— 影响**所有**任务
    # （含以后新建的，因为 engine 过滤时无条件并上它）。
    # 与 task.blacklist / task.ai_blacklist 的区别：那两个是**单任务**的，
    # 会随任务导出/导入走；这个写在 AppConfig 上、随 config.json 持久化。
    global_blacklist: list[str] = field(default_factory=list)
    # V0.0.6：用户导入过的「原有图库」目录。哪怕数据目录恰好也在同一块盘、
    # 或某个软件目录是指向外部的链接，这些目录里的文件也一律不许删/移/改名。
    # 导入文件夹时自动登记（见 safe_delete.mark_external_root）。
    external_library_roots: list[str] = field(default_factory=list)
    # F3：config.json 里的未知键（旧版本字段、手改键、未来版本的键）加载时
    # 保留在这里，保存时原样带回，不再被静默吞掉；_DEAD_CONFIG_KEYS 例外。
    extra: dict = field(default_factory=dict)
    max_depth: int = 3
    concurrency: int = 4
    delay: float = 1.0
    timeout: int = 30
    retry: int = 3
    # 2026-10-27 用户要求（#10/#11）：
    #   auto_retry      任务失败后自动重试几次（0=不重试），每次等待翻倍；
    #   auto_retry_delay 首次等待秒数（第 n 次 = delay * 2^(n-1)）；
    #   auto_slowdown   失败率偏高时自动把该任务的 delay 调大（降频），避免
    #                   继续被站点当成刷流量。
    auto_retry: int = 2
    auto_retry_delay: int = 60
    auto_slowdown: bool = True
    user_agent: str = DEFAULT_USER_AGENT
    # R189：gallery-dl 快路径从哪个浏览器取 Cookie（见 DEFAULT_COOKIE_BROWSER）
    cookie_browser: str = DEFAULT_COOKIE_BROWSER
    proxy: Optional[str] = None
    min_width: int = 0
    min_height: int = 0
    min_bytes: int = 0
    max_bytes: int = 0      # 文件大小上限（0=不限）
    dark_mode: bool = False
    skin: str = "violet"     # 皮肤（#10）：强调色方案名
    # 外观预设（SKIN_ORDER）：用户真正在选择器里点的那一项。
    # 一个预设 = 一整套外观（强调色 + 明暗），所以「深色模式」不再是一个
    # 独立开关，而是 `night` 这一个预设的固有属性。
    # ⚠ `dark_mode` 字段**故意保留**：自绘控件有十几处读它（图标兜底色、
    #   任务行文字、缩略图底…），全部改成读 ui_preset 会把改动面放大好几倍。
    #   它现在的角色是「由 ui_preset 派生的明暗标志」，由 set_skin 同步写入。
    ui_preset: str = "modern"    # V0.0.19-E 默认改现代风（用户拍板）；classic=原始工程风
    # 2026-10-27 用户要求：**网页皮肤与桌面皮肤分开**。
    # 以前网页顶栏那个下拉写的是 ui_preset（改网页=改桌面端皮肤），
    # 用户明确说"网页端自己独立的皮肤"。两者现在互不影响。
    web_theme: str = "classic"
    # 2026-10-28 用户提供的 Iconify 动画集（svg-spinners / line-md）：
    # 加载、刷新、扫描、传输这些"有事在发生"的位置改用 SMIL 动画图标。
    # 默认开 —— 用户原话是"如果能用带动画特效的图标就都用这种"。
    # 但必须留一个总开关：动画在低配机 / 远程桌面 / 多人同时操作时是纯负担，
    # 而且有人就是觉得动来动去晃眼。关掉只影响观感，功能一字不变。
    ui_animated_icons: bool = True
    # 外观预设表的世代号（见模块级 PRESET_SCHEME）。**别删**：
    # R59 起它是「老配置的 classic 要不要被搬到 violet_look」的唯一判据。
    # 老配置没有这个键 → 0 → 搬；新版写过 2 → 不搬。没有它，
    # 用户在新版里主动选「原始工程风」会被每次启动弹回紫罗兰风。
    ui_preset_scheme: int = PRESET_SCHEME
    # 删除时是否先经过回收站（隔离区，可还原）；关掉=直接永久删除
    use_recycle: bool = True
    # 用键盘快捷键（审核页 ↑不喜欢 / ↓喜欢）判定时，是否弹出「未命中主题词」
    # 选词框。默认关：键盘连续判定时不打断；想让它弹就在这里/设置里打开。
    review_key_ask: bool = False
    # R152-C9：单任务失败率过高时，**是否自动暂停整个队列**。
    # 默认 False = 只告警（给提示 + 建议，队列继续跑）——擅自停下来
    # 违反「网络异常不硬停任务」那条铁律：一个站点抽风就把用户排好的
    # 十几个任务全按住，还得他自己发现队列停了。要自动停的用户自己开。
    auto_pause_on_failure: bool = False
    # ── Web 端（Phase B）────────────────────────────────────────────
    # 这五个字段**原来根本不存在**（#1 生产测试报告）：代码全靠
    # getattr(cfg, "web_xxx", 默认) 硬扛，新用户配置里没有这些键 →
    # 界面无法自动启用 Web、端口显示空白；更危险的是 web_port 为 None 时
    # ThreadingHTTPServer 会绑到**操作系统随机端口**，用户根本不知道网址。
    # 字段名必须与 webui.py / main_window.py 里 getattr 的键名完全一致。
    web_enabled: bool = False
    web_bind: str = "127.0.0.1"      # 默认只监听本机（安全默认）
    web_port: int = 8080             # 与 Web 设置对话框的默认值一致
    web_user: str = "admin"
    web_password: str = ""           # 首次启用 Web 时由对话框强制设置（≥4 位）
    # R52：局域网免密。开启后，来自私有地址（127/10/172.16-31/192.168 段
    # 及 IPv6 链路本地/唯一本地）的请求**不需要密码**即可访问 Web；
    # 公网（非私有地址）访问仍需密码（若未设密码则被拦在登录页）。
    # 默认关（R152-E5 安全默认翻转）：免密是**放宽**，默认敞开等于
    # 「同网段谁都能翻图库」；尤其容器场景对端恒为 docker0 网关
    # （172.17.0.1，私有段），默认开 = 全网免密（见 webui.py `_client_ip`
    # docstring）。要手机免密直连，在 Web 设置里显式打开并确认只在内网。
    # 存量 config.json 里存过 True 的不受影响（from_dict 以存值为准）。
    web_lan_open: bool = False
    # R52：额外始终放行的 IP/CIDR 白名单（与 web_lan_open 叠加）。
    # 例：["203.0.113.5", "10.8.0.0/24"]。留空=只用局域网自动放行。
    web_ip_whitelist: list = field(default_factory=list)
    # 容器 / 反向代理（DOCKER_X86_ORDER E1）：开启后 _client_ip() 才会解析
    # X-Real-IP / X-Forwarded-For 取真实客户端 IP。
    # 默认关（安全）：这两个头**客户端可伪造**，默认信任等于把"局域网免密"
    # 开放给任何人。只有确认容器前面确实有可信反代（Nginx / Caddy /
    # 飞牛自带反代）时才开，且反代必须**覆盖**（而不是追加）这两个头。
    # 环境变量 MRM_TRUST_PROXY=1 可覆盖（见 headless 的 A4 映射表）。
    trust_proxy: bool = False
    # 回收站（隔离区）保留天数：超过则菜单「清理过期回收站」物理删除（#59）
    recycle_keep_days: int = 30
    # 每天首次启动自动备份数据库，只保留最近 N 份（数据安全）
    daily_backup: bool = True
    backup_keep: int = 7
    # 合规基线：loose / standard / strict（见 COMPLIANCE_LEVELS）
    compliance_level: str = DEFAULT_COMPLIANCE_LEVEL
    # 日志脱敏：URL 只留域名、本地路径只留文件名（诊断包可安全外发）
    log_sanitize: bool = True
    # 磁盘剩余空间低于这个数（MB）就暂停队列并提示，0=不检查（#54）
    min_free_mb: int = 200
    # 完整性校验：小于这个字节数的图视为可疑（#55，0=不检查）
    suspicious_bytes: int = 1024
    # AI 推理线程数（0=自动，最多 4 且不超过核心数一半）。
    # 本机纯 CPU，不限制的话 6 并发就能把机器拖卡死。
    ai_threads: int = 0
    # AI 推理是否优先用显卡（CUDA / DirectML）。True=自动挑 GPU 后端，
    # 建不成会话再回退 CPU（不影响功能）；False=强制纯 CPU（显卡驱动抽风、
    # 显存被别的程序占满、或 GPU 推理把界面拖卡时用）。
    # 2026-10-27 用户要求：把「用显卡还是用 CPU」做成**用户可选的三档**——
    # 因为 CPU 有时要跑别的重负载任务，用户想主动把推理挪到显卡去。
    #   auto = 有显卡后端就用显卡（默认）
    #   gpu  = 强制显卡（没有 GPU 后端时回退 CPU 并在设置页明确标出）
    #   cpu  = 强制 CPU（显卡驱动抽风/显存被占/想让显卡干别的时用）
    # ai_gpu 保留为老字段（True/False 等价 auto/cpu），旧配置照常读。
    ai_device: str = "auto"
    ai_gpu: bool = True
    # R50：**全局** AI 总开关（关掉后所有任务都不做 AI 打分，与每个任务自己
    # 的 `ai_enabled` 是两层：任务级管「这个任务用不用 AI」，这个管「整库用不用」。
    # ⚠ 原来 webui 的可写白名单里就有 `ai_enabled`，但全局字段并不存在 ——
    # 而 `ai_enabled` 是 **TaskConfig** 上的字段名，同名不同类，最容易写混。
    # 这里刻意改名 `ai_enabled_global` 就是为了跟任务级区分开，
    # 否则两处都叫 ai_enabled，读代码的人会以为改一处等于改两处。
    # 实际生效点：crawler/engine.py 建分类器前多判一次。
    ai_enabled_global: bool = True
    # 「新建任务默认」最大抓取页数（0=不限）。真正生效的是每个任务自己设的
    # 任务级 max_pages；这里只当新建任务时的预填值，改它不会影响已有任务。
    max_pages: int = 0
    # 敏感图入库前先本地模糊/打码（原图留盘但隐藏，图库只显示模糊版）
    blur_sensitive: bool = False
    sensitive_words: list = field(
        default_factory=lambda: list(_SENSITIVE_DEFAULT_WORDS))
    # AI 主题词 / 排除词 / 二三级细分词的「曾用词」历史：点一下就能填，免得手打。
    # 三个层级各记一份：一级=入门票，二三级=细分，别串味。
    ai_label_history: list = field(default_factory=list)
    ai_label_history_l2: list = field(default_factory=list)
    ai_label_history_l3: list = field(default_factory=list)
    ai_excl_history: list = field(default_factory=list)
    # ══ 2026-10-30 治本：补上**缺失**的两个全局 AI 字段 ══
    # `import_folder._tag_stage` 里写的是 `task.ai_labels or cfg.ai_labels`，
    # 但 AppConfig 里**从来没有** ai_labels 这个字段（全仓 grep 可证）。
    # 后果：网页端「从文件夹学习」永远报「没有配置主题词，本次只入库不打标签」，
    # 一张标签都不打 —— 这就是图库 218 张 tags 全为 [] 的直接原因，
    # 不是部署问题、也不是模型问题。
    # 注意 `ai_label_history` 是**下拉框的历史记录**，不是当前生效的词表，
    # 两者语义完全不同，不能混用。
    ai_labels: list = field(default_factory=list)
    # 全局默认阈值。任务级 ai_threshold 优先（任务存在时永远读任务的）；
    # 这里只是"新建任务/无任务上下文"时的兜底，与 models.py 的默认保持 0.35。
    ai_threshold: float = 0.35
    # r32：审核页判定完「喜欢/不喜欢」后，自动从待审核队列补图把本页填满
    # （每页 50 张：判掉一张就续一张，连着刷不用手动刷新）。
    # 关掉 = 只审本页原有的这些，审完就剩多少算多少，剩下的等手动刷新/换页。
    review_auto_fill: bool = True
    # r37：「保存时转成 JPG」。真机库里 916 张 = 699 webp + 180 avif + 37 jpg，
    # 而 Qt 打包的 imageformats 插件只有 jpeg/webp/gif/bmp/tiff/svg，**没有
    # AVIF** → 那 180 张在灯箱/预览里天生打不开（只能靠灯箱的 Pillow 回退
    # 兜底，缩略图缓存、网格显示还是按后缀判断）。打开这个开关后新采集的图
    # 一律存成 .jpg。只影响**新落的图**，老图走「维护 → 把已有图片转成 JPG」。
    convert_to_jpg: bool = False
    # r32：任务跑完后顺手清一次软件自己的垃圾（0 字节 / .part 孤儿 /
    # 图库里指向已消失文件的残留记录 / 隔离区 _purged）。绝不碰系统回收站。
    tidy_after_task: bool = True
    # 2026-10-26：任务结束后是否自动跳到图库页。默认【关】——用户原话
    # "总是自动跳转到图库"，正在审核/设置页时被拽走。关 = 只发日志/通知，
    # 停在当前页；开 = 仅"真完成"(done)才跳，报错/0图不跳。
    task_done_goto_gallery: bool = False
    # 2026-10-26：Web 默认界面用新版（Vue3）还是老 SPA。
    # 默认 False = 老界面在 `/`，新版在 `/v2`（不改变任何既有行为/测试）。
    # 打开后 `/` 直接进新版，老界面挪到 `/classic`。
    web_ui_v2: bool = False
    live_wall: bool = True   # 右上角「实时抓到」图片墙开关（省资源可关）
    auto_authorize: bool = True   # 所有网站自动授权（用户明确开启的功能）
    parallel_tasks: int = 3       # 同时运行的任务数
    # ---- F24 下载限速计划（分时段带宽上限，口径全在 rateplan.py）----
    # ⚠ 默认**关闭**：升级上来的老用户行为一点不能变（不能让升级悄悄变慢）。
    # 打开后按 rate_plan 逐时段限速；kbps=0 的时段 = 不限速。
    rate_plan_enabled: bool = False
    rate_plan: list = field(default_factory=default_rate_plan)
    # ---- F19 快捷键自定义（口径全在 hotkeys.py）----
    # ⚠ 只存**与默认不同的覆盖**（action_id → 序列串，见 hotkeys.overrides_only）；
    # 默认键位活在 hotkeys.py 的 ACTIONS 注册表里。老配置没有这个键 = 全默认，
    # 行为与之前逐字节一致。手写垃圾值会被 hotkeys.resolve 忽略（回退默认），
    # 保存时过 sanitize_overrides 清洗，不会把非法串留在 config.json 里。
    hotkeys: dict = field(default_factory=dict)
    gallery_refresh_sec: int = 10   # 图库自动刷新间隔秒（0=关）
    # 图库网格列数：r31 起**弃用**（用户要求「取消每行几张，只用预览控制」）。
    # 列数现在完全由缩略图尺寸自适应，这个字段只在老 config.json 里被读回来，
    # 不再参与任何计算；新默认值不再改动它。
    gallery_columns: int = 6
    # 图库缩略图（预览）尺寸（像素）；最大化窗口时会自动放大（#83 用户诉求）
    # 默认值与 review_thumb_size 一致：两处网格是同一套几何，默认尺寸还不一样
    # 就会让人觉得"图库的图比审核小"（r30 起因）。
    gallery_thumb_size: int = 160
    # 审核页缩略图（预览）尺寸（像素）；与图库预览尺寸相互独立（K② 用户诉求）
    review_thumb_size: int = 160
    # 水印不透明度（0~255）。默认跟 gui/watermark.py 的 ALPHA 一致（#42：用户
    # 试色 demo 定的 178＝70%）；设置面板里的临时候选滑条就地调这个值，存这里。
    # R233：水印挪到最左列后不挡内容，用户要实心不透明（原 178/70%）
    watermark_alpha: int = 255
    # R50：Web 端「设置 → 水印」要的开关。⚠ **原来 webui 的白名单里写了
    # `watermark_enabled`，但这个字段在本 dataclass 里根本不存在** ——
    # setattr 进去之后没有任何代码读它，用户在网页上关掉水印，
    # 回桌面端一看水印还在，而接口还返回 ok:true（"改了没反应"最难查）。
    # 桌面端 MainWindow 建浮层时读它（gui/main_window.py）。
    watermark_enabled: bool = True
    review_margin: float = 0.12     # AI 分数离阈值多近算「拿不准」→进审核队列
    # 缩略图尺寸（缓存小图边长，像素）；改大更清晰但更占缓存（#75）
    thumbnail_size: int = 256
    # 外部备份同步目标目录（留空=不自动同步）；启动/手动「备份同步」时把
    # 数据库+规则+缩略图复制到这里（#79）
    backup_sync_dir: str = ""
    # ---- AI 模型自定义 ----
    ai_backend: str = "builtin"     # builtin=内置CLIP | local=自定义本地模型 | api=在线API
    ai_model_path: str = ""         # 自定义本地模型目录（CLIP/open_clip 格式）
    ai_api_url: str = ""            # OpenAI 兼容接口，如 https://api.xx.com/v1
    ai_api_key: str = ""
    ai_api_model: str = ""          # 在线模型名，留空用接口默认
    saved_urls: list = field(default_factory=list)   # 常用网址（管理器里维护）
    # Master password enables DB/library encryption (optional, user choice).
    encryption_enabled: bool = False
    age_gate_ok: bool = False  # set true only after explicit age confirmation
    # R149-B：「学习」独立开关（挂 AI与过滤菜单，不新增顶级菜单）。
    # 关掉时所有 PreferenceLearner.learn 调用都不持久化（只跳过学习，
    # 审核/删除/隔离本身照常）。默认 True；老配置没有这个键 = 兼容视为 True。
    # R154-F22：新手引导（首次运行弹 3 步向导）完成标记。
    # ⚠ 默认 True 的语义 = 「老用户不打扰」：存量 config.json 没有这个键，
    #   from_dict 填默认 True ⇒ 升级上来的用户永远不会被弹向导；
    #   **全新安装**（config.json 不存在）由 load_config 的 fresh 分支显式置
    #   False，向导只弹这一次，完成/跳过后写回 True。
    #   判断「该不该弹」必须读这个字段，不许再猜「库空不空」——空库的老用户
    #   （清空过任务）不该再被弹，全新装的用户库也是空的但该弹。
    onboarding_done: bool = True
    learn_enabled: bool = True

    # V0.0.19 界面双模式："newbie"=新手（精简菜单/表单），
    # "pro"=老鸟（全部入口）。默认老鸟——老用户现状零变化。
    ui_mode: str = "pro"

    # V0.0.19 毛玻璃（Win11 Acrylic / Mica，Win10 半透明近似）。
    # 默认关：系统级背板在远程桌面 / 部分显卡驱动下会黑屏，稳一手。
    glass_enabled: bool = False

    # ===================== 2026-10-27 第 1 批（qBittorrent 对标）=====================
    # ① 界面与启动（编号 5/6/9）
    #: 有任务在跑时，点关闭/退出先弹确认（防手滑把几小时的抓取打断）。
    confirm_exit_running: bool = True
    #: 采集/下载期间阻止系统休眠（Windows SetThreadExecutionState）。
    #: 夜里长任务最怕这个：不阻止的话系统一睡，任务就停在那儿等天亮。
    prevent_sleep: bool = True
    #: 启动画面（低优先，纯观感）。关掉=直接显示主窗口。
    splash_enabled: bool = True
    # V0.0.51：启动后自动恢复上次没做完的工作。用户原话「启动后自动恢复
    # 原有任务，让用户勾选是下载、打标、导入还是怎么」。
    # 默认全关：老用户升级后不会被突然自动跑一堆任务吓到；设置页里
    # 一个总开关 + 三个分类勾选，勾了哪类就只恢复哪类。
    resume_on_start: bool = False
    resume_downloads: bool = True
    resume_tags: bool = True
    resume_imports: bool = True
    #: 启动后直接进托盘，不弹主窗口（qBittorrent「启动时最小化」同款）。
    #: 默认关：新装用户双击图标后桌面什么都没有，会以为没启动成功。
    #: 打开后**只在托盘真的可用时**生效；托盘不可用则照常显示窗口，
    #: 免得程序变成叫不回来的幽灵（判据见 launch() 里的 _resident）。
    tray_start_minimized: bool = False
    #: 日志轮转：单文件超过多少 MB 就切一份（0=不切，退回老的单文件行为）。
    log_max_mb: int = 10
    #: 保留几份历史日志（不含当前文件）。0 与 1 等价于只留当前。
    log_backup_count: int = 5

    #: 适配器自动匹配（R252）：任务的适配器选「自动」且 gallery-dl 不认这个站时，
    #: 按**域名**在规则目录里兜底找一条规则。
    #: 关掉它 = 完全回到旧行为（自动就只走 gallery-dl）。
    adapter_auto_match: bool = True

    #: 浏览器插件推送 Cookie 用的配对码（6 位数字）。
    #: 空 = 还没生成；设置页打开时会自动生成一个并落盘。
    #: ⚠ 它不是密码，是「允许插件跨过登录鉴权」的凭据 —— 不要写进日志。
    cookie_pair_code: str = ""

    #: 整库备份（mrm_full_*.zip）的自动轮换。
    #: 三条规则同时生效，取并集；**始终至少保留最新一份**。
    #: keep=0 或 days=0 或 max_gb=0 表示该条规则不生效。
    full_backup_keep: int = 5
    full_backup_days: int = 30
    full_backup_max_gb: float = 8.0

    # ---- 长期运行的数据保留（R247） ----
    # 24 小时挂着跑，四张表只增不减：审计日志（每张图一条）、页面账本
    # （每抓一页一条）、失败网址、磁盘快照。不清的话库和备份会一直变大。
    # 策略是「按天 + 按行数」双保险，见 data_growth 模块说明。
    #: 审计日志保留天数。
    retain_audit_days: int = 90
    #: 审计日志最多保留行数（防某一晚疯狂刷；按 id 封顶，不动最新数据）。
    retain_audit_rows: int = 200_000
    #: 已抓完（done）的页面记录保留天数。未完成的页**不按时间删**。
    retain_pages_days: int = 30
    #: 失败网址保留天数。
    retain_failed_days: int = 90
    #: 磁盘快照保留天数（每天一行，两年足够看趋势）。
    retain_snap_days: int = 730

    # ② 下载与保存（编号 10/11/12/13/14）
    #: 静默添加：用默认模板直接建任务，不弹表单（适合「粘贴一堆网址就走」）。
    silent_add: bool = False
    #: 取消/删除任务时，一并清掉该任务目录下的 .part 残留与空目录。
    clean_part_on_cancel: bool = True
    #: 输出子目录模式：none=全放输出目录；site=按站点（历史行为）；
    #: task=按任务名；both=任务名/站点。
    subdir_mode: str = "site"
    #: 监视文件夹（编号 13）：把「一行一个网址」的 .txt 丢进去就自动建任务。
    watch_enabled: bool = False
    watch_dir: str = ""
    #: 监视文件夹的扫描间隔（秒）。
    watch_interval_sec: int = 30
    #: 改输出目录时，询问是否把已有文件一起搬过去（编号 14）。
    move_on_path_change: bool = False

    # ③ 连接与代理（编号 15/16/17）
    #: 域名解析也走代理（socks5 → socks5h）。国内直连 DNS 被污染时必开。
    #: ⚠ 默认 **False**：socks5h 要求代理端支持远程解析，老代理可能不认；
    #:   「新键默认不改变既有行为」优先。需要时在设置里打开。
    proxy_dns: bool = False
    #: 仅任务请求走代理：软件自身的在线 API / 更新检查等直连。
    proxy_tasks_only: bool = False
    #: 抓取时屏蔽的 IP/网段（编号 17）：一行一个，支持 1.2.3.4 与 1.2.3.0/24。
    ip_filter_enabled: bool = False
    ip_filter_text: str = ""

    # ④ 速度（编号 18/20）
    #: 全局速率上限（KB/s），0=不限速。与「分时段计划」同时生效时**取更严的那个**
    #: ——两处都配了上限时用户显然想要「都不超过」。
    speed_limit_kbps: int = 0
    #: 内网/本机地址不限速（本地 NAS、127.0.0.1 之类，限速没意义）。
    speed_skip_lan: bool = True

    # ⑤ Web 安全（编号 24/25/26/27/28/29/30/31）
    #: 仅本机（127.0.0.1 / ::1）免密，外部仍要密码。比「整站免密」安全得多。
    web_localhost_free: bool = False
    #: IP 白名单内的来源免密（白名单此前只表示「允许访问」）。
    web_whitelist_nopass: bool = False
    #: 连续登录失败 N 次后封禁该 IP（0=不封禁）。
    web_login_max_fail: int = 5
    #: 封禁时长（分钟）。
    web_login_ban_min: int = 10
    #: 会话有效期（小时）。0 或负数 = 用默认 12 小时。
    web_session_hours: int = 12
    #: 拒绝被 iframe 嵌入（X-Frame-Options: DENY + CSP frame-ancestors none）。
    #: 默认开：网页端没有「被别的站嵌进去」的正当需求。
    web_frame_deny: bool = True
    #: 校验 Host 头（防 DNS rebinding：攻击者用自己的域名解析到你的内网 IP）。
    web_host_check: bool = True
    #: CSRF 双提交 Cookie 校验（写操作要求 X-MRM-CSRF 与 Cookie 一致）。
    web_csrf: bool = True
    #: 自定义响应头：一行一个 `Key: Value`（反代/嵌入场景用）。
    #: 2026-10-28（对标 qBittorrent `web_ui_secure_cookie_enabled`）：
    #: 走 HTTPS 时给会话 cookie 加 Secure —— 防止证书降级/混合内容把会话
    #: 从明文那口漏出去。HTTP 访问时**不能**加（浏览器会直接不保存 cookie，
    #: 表现为「登录了却一直回登录页」），所以按连接是不是 TLS 自动判断。
    web_secure_cookie: bool = True
    web_extra_headers: str = ""
    #: 更新清单地址（编号 7）：留空 = 只能看当前版本，不假装"已是最新"。
    #: 清单是 JSON：{"version": "1.0.1", "url": "...", "notes": "..."}
    update_manifest_url: str = ""

    # ===================== 2026-10-28 第 2 批（qBittorrent 对标）=====================
    # ⑥ HTTPS（编号 23）
    #: 是否同时开一个 HTTPS 监听（自签证书自动生成，不需要用户去搞证书）。
    #: ⚠ 默认 **False**：老用户升级后行为一点不变（还是纯 HTTP 一个口）。
    web_https: bool = False
    #: HTTPS 监听端口。**刻意不复用 web_port** —— 两个协议各占一个口，
    #: 关了 HTTPS 也不用回改端口；反代/书签里已有的 8080 照样能用。
    web_https_port: int = 8443
    #: HTTP 请求自动 301 跳到 HTTPS（"必须走加密"时才开）。
    #: 开了之后 HTTP 那口只做跳转，不再提供服务。
    web_https_redirect: bool = False

    # ⑦ 网络出口与证书（编号 32/33/34）
    #: 绑定源 IP（多网卡/多线机器选从哪张网卡出去）。留空 = 交给系统路由表。
    #: 注意：走代理时源 IP 由系统连代理那条连接决定，本项对代理链路无效。
    bind_ip: str = ""
    #: HTTPS 站点证书校验。关掉 = 全局不校验（**危险**，中间人可换包）。
    #: 只对个别自签证书站点放行请用 ssl_insecure_hosts，别关这个总开关。
    ssl_verify: bool = True
    #: 只为这些域名跳过证书校验（自签/内网站）。逗号/换行分隔，支持子域。
    ssl_insecure_hosts: str = ""
    #: 允许抓内网/本机地址（10./172.16-31./192.168./127.）。
    #: ⚠ 默认 **True**：老用户可能有本地 NAS/局域网图库任务，默认拦掉等于
    #:   升级即失能。想收紧（防 SSRF）在设置里关掉，关掉后这些地址直接跳过。
    allow_private_hosts: bool = True

    # ⑧ 站点订阅 / 新图监控（编号 21/22）
    #: 订阅总开关。关掉 = 后台不再定时查（已有订阅源与历史快照都保留）。
    sub_enabled: bool = True
    #: 新订阅源的默认检查间隔（分钟）；每个源可以单独改。
    sub_interval_min: int = 60

    # ⑨ 慢任务自动让位（编号 35）
    #: 开了之后：排队里有任务在等、而某个正在跑的任务连续 N 分钟毫无进展，
    #: 就把它停下让位给后面的（停下的任务回到队列，下次续跑仍有断点账）。
    #: ⚠ 默认 **False**：这是"抢跑"行为，得用户明确要才开。
    slow_yield_enabled: bool = False
    #: 连续多少分钟没有新页面/新图片就算"慢"（0/负数按 10 分钟算）。
    slow_yield_min: int = 10

    def ensure_dirs(self) -> None:
        for d in (self.db_path.parent, self.library_dir, self.rules_dir,
                  self.isolate_dir, self.review_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict:
        out = asdict(self)
        out = {k: (str(v) if isinstance(v, Path) else v) for k, v in out.items()}
        # F3：加载时保留的未知键原样带回，避免「回写吞键」；
        # 死字段不过滤第二道（from_dict 已拦，这里双保险）。
        for k, v in (self.extra or {}).items():
            if k not in _DEAD_CONFIG_KEYS and k not in out:
                out[k] = v
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "AppConfig":
        for k in ("db_path", "library_dir", "rules_dir", "isolate_dir",
                  "review_dir", "log_dir", "last_output_dir",
                  "last_learn_dir"):
            if k in d and d[k]:
                d[k] = Path(d[k])
        cfg = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        # F3：未知键不再静默丢弃——保留到 cfg.extra 并告警，下次保存带回。
        # R106 死字段除外（不许复活）；其余未知键是用户/未来版本的信息。
        unknown = {k: v for k, v in d.items()
                   if k not in cls.__dataclass_fields__
                   and k not in _DEAD_CONFIG_KEYS}
        dead = [k for k in d
                if k not in cls.__dataclass_fields__ and k in _DEAD_CONFIG_KEYS]
        if unknown or dead:
            try:
                from .logutil import get_logger
                get_logger().warning(
                    "config.json 键兼容：保留未知键 %s；忽略已删除的死字段 %s",
                    sorted(unknown) or "（无）", dead or "（无）")
            except Exception:
                pass
        if unknown:
            cfg.extra = dict(unknown)
        # 2026-11-05：存量黑名单整串拆分（历史 config.json 里可能存过
        # 「颜射，老，黑女，…」这样的一整串）。拆分在**读取路径**做，幂等：
        # 已经是单词的列表再跑一遍结果不变，所以老用户升级后启动即自愈，
        # 下一次 save_config 把拆好的形态写回磁盘。
        try:
            cfg.global_blacklist = split_blacklist_words(
                getattr(cfg, "global_blacklist", []) or [])
        except Exception:
            pass
        return cfg


# ---- R161：运行时数据库主密码持有（不落盘）----
# 主密码只活在进程内存；绝不写进 config.json（否则静态加密失去意义）。
# 用户启动软件输入主密码后由入口点写入；Web / headless 无交互界面时从
# 环境变量 MRM_DB_PASSWORD 读取。开关 `encryption_enabled` 仍走 AppConfig 正常持久化。
_DB_MASTER_PASSWORD = ""
_ACTIVE_CFG = None


def set_db_master_password(pw: str) -> None:
    """设置（或清空）内存中的数据库主密码。空串 = 未解锁。"""
    global _DB_MASTER_PASSWORD
    _DB_MASTER_PASSWORD = pw or ""


def get_db_master_password() -> str:
    return _DB_MASTER_PASSWORD


def set_active_config(cfg) -> None:
    """登记当前进程的生效配置，供 db.Database 在 __init__ 里读取加密开关。"""
    global _ACTIVE_CFG
    _ACTIVE_CFG = cfg


def get_active_config():
    return _ACTIVE_CFG


#: 2026-11-05：黑名单整串拆分用的分隔符（中英文逗号 / 顿号 / 分号 / 换行 /
#: 竖线 / 制表）。**不含空格**——黑名单是 CJK 子串匹配，词里带空格（如
#: "id card"）是合法的，按空格切会把这类词切碎。分隔符一律要求两侧非空。
_BLACKLIST_SPLIT_RE = re.compile(r"[,，、;；\n\r|\t]+")


def split_blacklist_words(words) -> list[str]:
    """把传入的词（或含分隔符的一整串）拆成干净的黑名单词条列表。

    背景（2026-11-05 修复）：用户在设置框里一次性粘贴
    ``"颜射，老，黑女，丑，……"`` 时，早期版本把这个含 13 个词的长串当成
    **一个**元素存进列表，消费侧 `_hit_blacklist` 要求 URL 里出现整个长串
    才命中 ⇒ 13 个词全部空转（只有"黑白照"靠灰度检测歪打正着还生效）。
    这里按中英文逗号 / 顿号 / 分号 / 换行 / 竖线统一拆开，去空白、去重、
    丢空串。**幂等**：对已拆好的列表再跑一遍结果不变。
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in words or []:
        if not isinstance(item, str):
            continue
        for piece in _BLACKLIST_SPLIT_RE.split(item):
            w = piece.strip().strip("\"'“”'、").strip()
            if not w or w in seen:
                continue
            seen.add(w)
            out.append(w)
    return out


def add_global_blacklist(cfg: "AppConfig", words: list[str]) -> list[str]:
    """需求 16：把词追加进**全局黑名单**，返回本次真正新增的那些词。

    三条硬约束：
    - **去重幂等**：已在列表里的不重复加，重复调用不会把列表撑大；
    - **空词丢弃**：空白串不进列表（否则匹配时可能命中一切）；
    - **主题词永不进排除词**：调用方负责过滤，这里是最后一道兜底
      （空串与非字符串一律不收）。
    - **整串拆分**（2026-11-05）：传入的元素内部若含分隔符（用户一次粘一串），
      先拆成单词再收，避免"整串进列表 → 永不命中"。

    只改内存 + 落 config.json，**不碰**任何任务的 blacklist。
    """
    added: list[str] = []
    cur = split_blacklist_words(getattr(cfg, "global_blacklist", []) or [])
    for w in split_blacklist_words(words or []):
        if w in cur:
            continue
        cur.append(w)
        added.append(w)
    if added or cur != list(getattr(cfg, "global_blacklist", []) or []):
        cfg.global_blacklist = cur
        save_config(cfg)
    return added


def _seed_rules(cfg: "AppConfig") -> None:
    """补种内置站点规则（缺哪个写哪个，用户改过的不动）。失败不影响启动。"""
    try:
        from .crawler.builtin_rules import seed_builtin_rules
        cfg.rules_dir.mkdir(parents=True, exist_ok=True)
        seed_builtin_rules(cfg.rules_dir)
    except Exception:
        pass


def _recompute_data_paths(cfg: "AppConfig") -> None:
    """数据根目录下的子目录一律按 user_data_dir() 重算，抛弃 config.json 里
    可能固化旧机器 / 旧目录（如 %APPDATA%）的绝对路径。

    这是「整个软件目录拷走、解压即用」能成立的根本保证：换机器、换盘符、清掉
    data 重启时，规则 / 库 / 审核 / 隔离区仍自动落到新的软件目录/data 下，不会
    悄悄跑回旧位置。
    * 这 6 个是「数据根」子目录，必须跟着软件走，不可被旧绝对路径覆盖。
    * last_output_dir（用户最后选的下载位置）属于用户偏好，保留原值不强行改写。
    """
    root = user_data_dir()
    cfg.db_path = root / "mrm.db"
    cfg.library_dir = root / "library"
    cfg.rules_dir = root / "rules"
    cfg.isolate_dir = root / "quarantine"
    cfg.review_dir = root / "review"
    cfg.log_dir = root / "logs"


# 外观预设表的**世代号**见模块顶部（AppConfig 之前）。
def migrated_preset_for(raw: dict) -> str:
    """老配置（升级前写下的 config.json）该落到哪个皮肤预设。

    SKIN_ORDER 之前，用户的外观是「`dark_mode` 布尔 + `skin` 强调色」两件事；
    之后统一成 `ui_preset` 一个预设。这里把老配置翻译过去。

    ⚠⚠ **参数必须是「解析出来的原始 dict」，不能是 AppConfig 实例** ——
    这是本函数存在的唯一理由，也是最容易写错的地方：
    `AppConfig.from_dict()` 会把**缺失字段填成 dataclass 默认值**，
    所以拿到 cfg 时 `ui_preset` 永远是 "classic"，**根本读不出
    「这个键压根不存在」**。于是第一版写成 `migrated_preset_for(cfg)` 时
    它对老用户恒返回 "classic" —— 一个特意开了深色的用户升级后
    界面整个变浅，**静默**（不报错、不提示，用户只会觉得「软件更新坏了」）。
    能区分「键不存在」与「用户就选了默认值」的，只有原始 dict。

    规则：
      · `ui_preset` 存在且是合法预设 → 原样保留（正常路径）。
      · `ui_preset` 不存在/ 为空 / 非法 + `dark_mode` 为真 → `night`。
      · 其余（浅色）→ 默认预设。
    """
    presets = ("classic", "violet_look", "modern", "night", "qbittorrent")
    cur = (raw or {}).get("ui_preset") or ""
    # ---- R59（NATIVE_SKIN_ORDER §4）：classic 的**一次性**改名迁移 ----
    # 背景：R59 把「原始工程风」改成真原生 + 钢灰蓝，于是 R58 之前
    # `ui_preset == "classic"` 的老用户，他看到的那套紫罗兰观感被搬到了
    # `violet_look`。不搬的话他们一升级界面整个变样 —— 静默、无提示。
    #
    # ⚠⚠ **必须一次性**，不能每次 load 都搬：
    #   用户在新版里**主动**选「原始工程风」是合法操作，而配置键名字没变
    #   （还是 "classic"）。若无条件搬，他每次启动都被弹回violet_look，
    #   永远选不了这个皮肤 —— 而且不报任何错。
    #   判据用 `ui_preset_scheme`（原始 dict 里的键是否存在/值是否够新），
    #   **不是** AppConfig 实例：from_dict 会把缺失字段填成默认值，
    #   拿到 cfg 就永远读不出「这个键压根不存在」——
    #   那是本函数 docstring 里记着的、这个项目已经踩过一次的坑。
    try:
        scheme = int((raw or {}).get("ui_preset_scheme") or 0)
    except (TypeError, ValueError):
        scheme = 0
    if scheme < PRESET_SCHEME and cur == "classic":
        return "violet_look"
    if cur in presets:
        return cur
    # V0.0.19-E：无键/非法值的老配置回落到现代风（用户拍板默认现代化）。
    # 显式选了某皮肤的用户走上面 `cur in presets` 原样保留，不受影响。
    return "night" if (raw or {}).get("dark_mode") else "modern"


# ---- R152-E6：config.json 敏感字段落盘加密 -----------------------------
#: 需要加密落盘的敏感字段（web_password=Web 登录密码；ai_api_key=AI 服务密钥）。
_SECRET_CONFIG_FIELDS = ("web_password", "ai_api_key")

#: 按目录缓存的加密盒（进程内每目录一个实例，密钥状态不挂类属性）。
_SECRET_BOX_CACHE: dict = {}


def _secret_box(config_dir: Path) -> ConfigSecrets:
    """取（或建）某数据目录的加密盒。密钥文件 = <目录>/config.key。"""
    key = str(Path(config_dir).resolve())
    box = _SECRET_BOX_CACHE.get(key)
    if box is None:
        box = ConfigSecrets(Path(config_dir) / "config.key")
        _SECRET_BOX_CACHE[key] = box
    return box


def load_config(path: Optional[Path] = None) -> AppConfig:
    # 先跑一次旧数据迁移（把 %APPDATA% 里的库/规则/模型搬到软件目录/data），
    # 再按新目录读配置，保证任务、图、模型、偏好全部跟着软件走。
    try:
        migrate_from_legacy()
    except Exception:
        pass
    path = path or (user_data_dir() / "config.json")
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            cfg = AppConfig.from_dict(raw)
            # R152-E6：敏感字段（web_password / ai_api_key）解密。
            # 旧明文自动透传；有标记却解不开（密钥文件缺失/损坏）→
            # 置空 + 告警，**绝不**静默返回乱码、也不拖垮整个配置加载。
            for k in _SECRET_CONFIG_FIELDS:
                try:
                    setattr(cfg, k, _secret_box(path.parent).decrypt(
                        getattr(cfg, k, "")))
                except ConfigSecretError as e:
                    try:
                        from .logutil import get_logger
                        get_logger().warning(
                            "config.json 敏感字段 %s 解密失败，已置空：%s", k, e)
                    except Exception:
                        pass
                    setattr(cfg, k, "")
            # 老配置没有 ui_preset（SKIN_ORDER 之前的老用户）→ 翻译过来，
            # 开着深色的落到 night，不静默重置成浅色。
            # ⚠ 必须传**原始 dict**（`raw`）而不是 cfg：from_dict 已把缺失字段
            # 填成默认值，传 cfg 的话「键不存在」这件事就丢了，迁移恒不生效。
            # 详见 migrated_preset_for 的docstring。
            try:
                _mig = migrated_preset_for(raw)
                if getattr(cfg, "ui_preset", "") != _mig:
                    cfg.ui_preset = _mig
                # 世代号一并抬到当前值：下面的 save_config 会把它写回磁盘，
                # 于是「一次性」成立。不写这一句的话迁移会在每次启动重跑。
                cfg.ui_preset_scheme = PRESET_SCHEME
            except Exception:
                pass
            # 关键：读回配置后强制把数据根子目录重算到软件目录/data，
            # 否则 config.json 里固化的旧绝对路径会把规则/库拽回 %APPDATA%。
            _recompute_data_paths(cfg)
            _seed_rules(cfg)
            # 顺手把重算后的正确路径写回磁盘，自愈 config.json 里的旧绝对路径，
            # 让「解压即用 / 换机器」时磁盘与内存一致，不再残留 %APPDATA%。
            try:
                save_config(cfg, path)
            except Exception:
                pass
            return cfg
        except Exception as e:
            # V0.0.6：以前这里一句 `pass`，接着就用默认值把用户那份
            # config.json **整个覆盖**——库路径、全局黑名单、网页密码、
            # 所有开关一次性全没，还不报错。现在先把坏文件留一份，
            # 再把话说清楚，最后才自愈写默认值。
            try:
                bak = path.with_name(
                    f"{path.name}.broken-{time.strftime('%Y%m%d_%H%M%S')}.bak")
                shutil.copy2(path, bak)
                from .logutil import get_logger
                get_logger().error(
                    "config.json 读取/迁移失败（%s: %s），已原样备份到 %s；"
                    "本次先用默认设置启动，原文件可从备份取回",
                    type(e).__name__, e, bak)
            except Exception:
                pass
    cfg = AppConfig()
    cfg.ensure_dirs()
    _recompute_data_paths(cfg)
    _seed_rules(cfg)
    # R154-F22：全新安装（config.json 此前不存在）→ 新手引导只弹这一次。
    # 存在分支（上面 from_dict）不写这一行：老配置缺键 = 兼容 True，不打扰。
    cfg.onboarding_done = False
    save_config(cfg, path)
    return cfg


def save_config(cfg: AppConfig, path: Optional[Path] = None) -> None:
    path = path or (user_data_dir() / "config.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.to_dict()
    # R152-E6：敏感字段加密后落盘（web_password / ai_api_key）。
    # 空值/旧明文语义不变；cryptography 缺席时 box.encrypt 原样返回（降级）。
    box = _secret_box(path.parent)
    for k in _SECRET_CONFIG_FIELDS:
        if k in data:
            data[k] = box.encrypt(data[k])
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    # 订单 E6 的「尽力」口径：POSIX 真收紧，Windows 只影响只读位。
    restrict_permissions(path)


def load_rule_file(path: Path) -> dict:
    """Load an adapter rule file (.json or .yaml/.yml)."""
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "PyYAML is required to read YAML rules. Install it or use JSON."
            ) from e
        return yaml.safe_load(text)
    return json.loads(text)


def dump_rule_file(rules: dict, path: Path) -> None:
    text = ""
    if path.suffix in (".yaml", ".yml"):
        import yaml  # type: ignore
        text = yaml.safe_dump(rules, allow_unicode=True, sort_keys=False)
    else:
        text = json.dumps(rules, indent=2, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
