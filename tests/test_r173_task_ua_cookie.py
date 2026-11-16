"""R173 护栏：任务级 User-Agent 与站点 Cookie（执行方笔记本棒）。

订单要点（R173_TASK_UA_AND_COOKIE.md）：
  ① `tasks` 表加 `user_agent` / `cookie` 两列（幂等 migration，老库补列）；
  ② 桌面 + Web 双端任务表单都能填；
  ③ `_make_fetcher` 单处接线：task.user_agent 非空则覆盖 cfg.user_agent，
     对 fetcher 与 robots 取用同一口径；task.cookie 合进请求头；
  ④ 清洗：去换行、限长 8KB、支持 cookies.txt 路径；日志不走全文。

★ 为什么这些护栏必须存在（本棒交接时的真实状态）：
  上一棒已经把 schema 列加进 db.py/models.py（03:28），**但接线全没做** ——
  repo 不写不读、engine 不覆盖 UA、两端表单没入口。也就是说：列加了，
  值永远存不进也读不出，cookies 依旧是 engine 里的 `getattr` 读空气。
  本文件的判据全部落在「值能不能真的走完全程」，而不是「源码里有没有这个词」。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = ROOT / "media_resource_manager"
APPJS = SRC / "web" / "static" / "js" / "app.js"
MAIN = SRC / "gui" / "main_window.py"
WEBUI = SRC / "webui.py"
DBPY = SRC / "db.py"
MODELS = SRC / "models.py"
REPO = SRC / "repo.py"
ENGINE = SRC / "engine.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ==================================================================
# 一、清洗单元（纯函数，最便宜也最该先钉住）
# ==================================================================
def test_clean_ua_strips_crlf_and_controls():
    """★ 安全底线：换行进了请求头 = 请求头注入，必须挡住。

    判据用**真 httpx.Headers 行为**，不用字符串比对 —— 字符串比对本轮就
    差点写成空壳/假红：换行若被实现换成空格，"X-Injected: 1" 仍会作为
    普通文本留在 UA 里（那不是漏洞），只有「多出一个头」才是漏洞。
    """
    import httpx

    from media_resource_manager.task_headers import clean_user_agent

    for bad in ("Evil\r\nX-Injected: 1", "Evil\nX-Injected: 1",
                "Evil\rX-Injected: 1", "Evil\x00Null", "Evil\x1b[31m"):
        out = clean_user_agent(bad)
        assert "\r" not in out and "\n" not in out, repr(out)
        assert not any(ord(c) < 0x20 for c in out), repr(out)
        assert "\x7f" not in out

        # 行为级：拿清洗结果去建真请求头，不得冒出计划外的头。
        # （不清洗的话 httpx 会直接抛 LocalProtocolError；两种都算拦住了。）
        try:
            h = httpx.Headers({"User-Agent": out})
        except Exception:
            continue
        assert len(h) == 1, f"清洗后仍分裂出额外请求头：{dict(h)}"
        assert "x-injected" not in {k.lower() for k in h.keys()}


def test_clean_ua_empty_means_global():
    """空/空白 → ""，空串是「用全局」的契约值，不是「空 UA」。"""
    from media_resource_manager.task_headers import clean_user_agent

    for v in (None, "", "   ", "\r\n", "\t"):
        assert clean_user_agent(v) == "", repr(v)


def test_clean_cookie_normalizes_and_caps():
    """Cookie 归一化成单行 `k=v; k2=v2`，且 8KB 截断（订单点名）。"""
    from media_resource_manager.task_headers import COOKIE_MAX, clean_cookie

    assert clean_cookie("a=1;b=2") == "a=1; b=2"
    assert clean_cookie("  a=1 ;;  b=2  ") == "a=1; b=2"
    assert clean_cookie("a=1\nb=2") == "a=1; b=2"       # 换行粘贴
    assert clean_cookie(None) == ""
    assert clean_cookie("   ") == ""
    # 限长：超长必须被截到 <= COOKIE_MAX，且不得把控制字符带出来
    long_ck = "k=" + "v" * 40000
    out = clean_cookie(long_ck)
    assert len(out) <= COOKIE_MAX, len(out)
    assert "\n" not in out and "\r" not in out


def test_clean_cookie_rejects_header_injection():
    """Cookie 里的换行同样必须剁掉（往 Cookie 值里塞新头是同一类攻击）。"""
    from media_resource_manager.task_headers import clean_cookie

    out = clean_cookie("a=1\r\nX-Injected: yes\r\nb=2")
    assert "\r" not in out and "\n" not in out, repr(out)
    assert "X-Injected: yes" not in out.split("; "), repr(out)


def test_clean_cookie_accepts_netscape_file(tmp_path):
    """订单 §2 明确要求：允许直接填一个 cookies.txt 文件路径。"""
    from media_resource_manager.task_headers import clean_cookie

    f = tmp_path / "cookies.txt"
    f.write_text(
        "# Netscape HTTP Cookie File\n"
        ".example.com\tTRUE\t/\tTRUE\t0\tcf_clearance\tabc123\n"
        ".example.com\tTRUE\t/\tFALSE\t0\tsession\txyz\n",
        encoding="utf-8")
    out = clean_cookie(str(f))
    assert "cf_clearance=abc123" in out
    assert "session=xyz" in out
    assert "\t" not in out and "\n" not in out


def test_clean_cookie_accepts_inline_netscape_text():
    """用户直接把 cookies.txt 的**内容**粘进来也要能认（不只有路径）。"""
    from media_resource_manager.task_headers import clean_cookie

    text = ("# Netscape HTTP Cookie File\n"
            ".site.com\tTRUE\t/\tTRUE\t0\tcf_clearance\tzzz\n")
    out = clean_cookie(text)
    assert out == "cf_clearance=zzz", repr(out)


def test_describe_never_leaks_cookie_value():
    """★ 订单点名：日志不走全文（cf_clearance 是会话凭据）。

    判据 = 说明串里**不得出现任何 value**，只准有名字与长度。
    """
    from media_resource_manager.task_headers import describe_cookie

    secret = "SECRET-VALUE-1234567890"
    s = describe_cookie(f"cf_clearance={secret}; session=abcdef", "x.com")
    assert secret not in s
    assert "abcdef" not in s
    assert "cf_clearance" in s          # 名字可以出现（排障需要）
    assert "x.com" in s


# ==================================================================
# 二、schema 与 repo 往返（值必须真的存得进、读得出）
# ==================================================================
@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MRM_HOME", str(tmp_path))
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    from media_resource_manager.db import Database
    return Database(tmp_path / "mrm.db")


def test_tasks_table_has_both_columns(fresh_db):
    cols = [r["name"] for r in fresh_db.query("PRAGMA table_info(tasks)")]
    assert "user_agent" in cols, "tasks 缺 user_agent 列（R173 §1）"
    assert "cookie" in cols, "tasks 缺 cookie 列（R173 §2）"


def test_migration_is_idempotent_on_existing_db(tmp_path, monkeypatch):
    """★ 老库升级路径：先建一张**没有这两列**的 tasks，再用 Database 打开。

    这条比「新库有列」重要得多 —— 用户手上升级的是老库。
    """
    monkeypatch.setenv("MRM_HOME", str(tmp_path))
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    dbp = tmp_path / "old.db"
    con = sqlite3.connect(dbp)
    con.execute("""CREATE TABLE tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, adapter_name TEXT, entry_urls TEXT, keywords TEXT,
        blacklist TEXT, proxy TEXT, proxy_id INTEGER, use_proxy INTEGER,
        concurrency INTEGER, max_depth INTEGER, delay REAL, output_dir TEXT,
        auto_run INTEGER, status TEXT, is_template INTEGER, parent_id INTEGER,
        priority INTEGER, ai_enabled INTEGER, ai_labels TEXT,
        ai_blacklist TEXT, ai_threshold REAL, ai_match_mode TEXT,
        ai_exclude_match_mode TEXT, min_width INTEGER, min_height INTEGER,
        min_bytes INTEGER, max_bytes INTEGER, schedule_at TEXT,
        rate_limit INTEGER, allowed_hours TEXT, max_pages INTEGER,
        ai_labels_l2 TEXT, ai_labels_l3 TEXT, ai_label_strict INTEGER,
        keyword_subdir INTEGER, last_error TEXT, last_run_at TEXT)""")
    # ⚠ 老库的 JSON 列在真实环境里是有默认值的（建表时 DEFAULT '[]'），
    #   这里必须照写 —— 我第一版只填了 entry_urls，其余为 NULL，
    #   读行时 json.loads(None) 直接炸，那是 fixture 不真实，不是产品问题。
    con.execute(
        "INSERT INTO tasks (name, adapter_name, entry_urls, keywords, "
        "blacklist, ai_labels, ai_blacklist, ai_labels_l2, ai_labels_l3, "
        "status) VALUES ('old','', ?, '[]','[]','[]','[]','[]','[]','idle')",
        ('["https://old.com"]',))
    con.commit()
    con.close()

    from media_resource_manager.db import Database
    db = Database(dbp)
    cols = [r["name"] for r in db.query("PRAGMA table_info(tasks)")]
    assert "user_agent" in cols and "cookie" in cols, "老库没补列"
    # 老行读出来必须是空串（= 用全局 / 不发 cookie），而不是 None 或异常
    from media_resource_manager.repo import get_task
    t = get_task(db, 1)
    assert t is not None
    assert t.user_agent == ""
    assert t.cookie == ""
    # 幂等：再开一次不炸、列不重复
    db2 = Database(dbp)
    cols2 = [r["name"] for r in db2.query("PRAGMA table_info(tasks)")]
    assert cols2 == cols


def test_repo_roundtrip_ua_and_cookie(fresh_db):
    """★ 本棒最关键的判据：save_task 写进去、get_task 读回来。

    上一棒的半截状态就是「列在、值不进不出」—— 只判列存在会**全绿但没用**。
    """
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import get_task, save_task

    t = TaskConfig(id=None, name="rt", adapter_name="",
                   entry_urls=["https://x.com/a"])
    t.user_agent = "RoundTrip/1.0"
    t.cookie = "a=1; b=2"
    tid = save_task(fresh_db, t)

    back = get_task(fresh_db, tid)
    assert back.user_agent == "RoundTrip/1.0"
    assert back.cookie == "a=1; b=2"

    # 编辑路径（UPDATE 分支）也必须带上这两列 —— INSERT 补了不等于 UPDATE 补了
    back.user_agent = "Second/2.0"
    back.cookie = "c=3"
    save_task(fresh_db, back)
    again = get_task(fresh_db, tid)
    assert again.user_agent == "Second/2.0", "UPDATE 分支漏了 user_agent"
    assert again.cookie == "c=3", "UPDATE 分支漏了 cookie"


def test_repo_roundtrip_empty_is_empty(fresh_db):
    """空值往返仍是空串（不是 None），否则 GUI/引擎侧 getattr 会拿到 None。"""
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import get_task, save_task

    t = TaskConfig(id=None, name="empty", adapter_name="",
                   entry_urls=["https://x.com/a"])
    tid = save_task(fresh_db, t)
    back = get_task(fresh_db, tid)
    assert back.user_agent == ""
    assert back.cookie == ""


# ==================================================================
# 三、engine 接线（UA 覆盖 + Cookie 合并，且不许污染全局 cfg）
# ==================================================================
class _CapturingEngine:
    """复用真 Engine，只把 fetcher_factory 换成能记下 cfg 的替身。"""

    @staticmethod
    def make(cfg, db, sink: list):
        from media_resource_manager.engine import Engine

        def factory(effective_cfg, proxy):
            sink.append(effective_cfg)
            return None

        return Engine(cfg, db, fetcher_factory=factory)


def test_task_ua_overrides_cfg_without_mutating_global(tmp_config, tmp_db):
    """★ 任务级 UA 必须生效，且**全局 cfg 一个字都不能被改**。

    为什么单列一条：本项目的 engine 是长驻单例，若在此直接
    `self.cfg.user_agent = task.user_agent`，同进程里其他任务与后续批次
    会被上一个任务的 UA 串味 —— 那是最难查的一类 bug。
    """
    from media_resource_manager.models import TaskConfig

    tmp_config.user_agent = "Global/1.0"
    sink: list = []
    eng = _CapturingEngine.make(tmp_config, tmp_db, sink)

    task = TaskConfig(id=1, name="t", adapter_name="",
                      entry_urls=["https://x.com/a"])
    task.user_agent = "Task/2.0"
    eng._make_fetcher(task, None, None)

    assert sink[-1].user_agent == "Task/2.0", "任务级 UA 没覆盖生效"
    assert tmp_config.user_agent == "Global/1.0", \
        "★ 全局 cfg.user_agent 被就地改了（会串味其他任务）"


def test_empty_task_ua_keeps_global(tmp_config, tmp_db):
    """留空 = 沿用全局，**行为零变化**（这是「不填就不会有变化」的承诺）。"""
    from media_resource_manager.models import TaskConfig

    tmp_config.user_agent = "Global/1.0"
    sink: list = []
    eng = _CapturingEngine.make(tmp_config, tmp_db, sink)

    task = TaskConfig(id=1, name="t", adapter_name="",
                      entry_urls=["https://x.com/a"])
    task.user_agent = ""
    eng._make_fetcher(task, None, None)
    assert sink[-1] is tmp_config, \
        "留空时不该另建 cfg 对象（应原样传全局 cfg）"
    assert sink[-1].user_agent == "Global/1.0"


def test_task_ua_is_backstop_cleaned_in_engine(tmp_config, tmp_db):
    """库里存着脏 UA（手工 SQL / 旧版本）时，engine 起跑前必须自己再洗一次。

    只在 UI 层清洗是不够的：SQL 直写、旧库迁移、第三方脚本都能绕过 UI。
    """
    from media_resource_manager.models import TaskConfig

    sink: list = []
    eng = _CapturingEngine.make(tmp_config, tmp_db, sink)
    task = TaskConfig(id=1, name="t", adapter_name="",
                      entry_urls=["https://x.com/a"])
    task.user_agent = "Evil\r\nX-Injected: 1"
    eng._make_fetcher(task, None, None)
    got = sink[-1].user_agent
    assert "\r" not in got and "\n" not in got, repr(got)


def test_task_cookie_reaches_real_fetcher(tmp_config, tmp_db):
    """Cookie 走真 Fetcher 时必须落到 Fetcher.cookie（含同域名自动合并）。"""
    from media_resource_manager.engine import Engine
    from media_resource_manager.models import TaskConfig

    eng = Engine(tmp_config, tmp_db)          # 不注入 factory = 走真 Fetcher
    task = TaskConfig(id=1, name="t", adapter_name="",
                      entry_urls=["https://cookie-site.com/a"])
    task.cookie = "cf_clearance=token123"
    f = eng._make_fetcher(task, None, None)
    assert "cf_clearance=token123" in f.cookie, f.cookie


def test_make_fetcher_is_the_single_wiring_point():
    """订单 §1 要求「单处接线」：Fetcher 的真构造点只允许在 _make_fetcher。

    GUI 里的那个 Fetcher(...) 是「取站点 Cookie」的独立小请求，不跑任务，
    不在这条链上；这里只钉 engine.py 自身不得出现第二处真构造。
    """
    src = _read(ENGINE)
    assert src.count("return Fetcher(") == 1, \
        "engine.py 出现了第二处 Fetcher 构造 ⇒ UA/Cookie 口径会分裂"


# ==================================================================
# 四、双端表单入口（存不进 UI = 用户根本用不上）
# ==================================================================
def test_desktop_form_has_both_inputs_and_reads_them_back():
    src = _read(MAIN)
    assert "self.f_ua = QLineEdit()" in src, "桌面任务表单缺 UA 输入框"
    assert "self.f_ua.text()" in src, "桌面表单没读 UA"
    # ★ 保存路径必须**把清洗结果真的赋给 t**，不是「调了函数就算接线」。
    #   本条判据是被变异自检逼出来的：M10 注入「调 resolve_task_headers 但
    #   结果丢弃、t.user_agent 硬置 ""」时，只搜函数名的版本照样全绿。
    assert "t.user_agent, t.cookie = resolve_task_headers(" in src, \
        "桌面保存没把清洗结果赋给任务对象（UA/Cookie 存不进库）"
    # 回填（编辑任务时要把已存的值显示出来，否则一编辑就清空）
    assert 'self.f_ua.setText(getattr(t, "user_agent", "")' in src, \
        "编辑态没回填 UA ⇒ 一保存就把用户填的值抹掉"
    # 新建态必须清空（表单是复用的，见 R47/B9）
    assert '"f_ua"' in src, "新建态重置列表漏了 f_ua（会把上个任务的值带过来）"

def test_web_backend_maps_both_fields_and_cleans():
    src = _read(WEBUI)
    # _TASK_FORM_FIELDS 映射（没这条前端传了也丢）
    seg = src.split("_TASK_FORM_FIELDS = {", 1)[1].split("}", 1)[0]
    assert '"user_agent": "user_agent"' in seg
    assert '"cookie": "cookie"' in seg
    # ★ 出口清洗必须**写回 base**（M13 变体：调了函数但结果丢弃照样过）
    assert "base.user_agent, base.cookie = resolve_task_headers(" in src, \
        "Web 保存没把清洗结果写回任务对象"

# ==================================================================
# 五、变异自检用锚点必须真实存在（防止判据将来变成空壳）
# ==================================================================
def test_mutation_anchors_exist():
    """本文件的判据都锚在具体实现上；锚点没了要立刻发现，别静默变空壳。"""
    assert "clean_user_agent" in _read(SRC / "task_headers.py")
    assert "user_agent=?, cookie=?" in _read(REPO), "UPDATE 分支锚点丢了"
    assert "task_ua = clean_user_agent(" in _read(ENGINE), "engine 兜底锚点丢了"


# ==================================================================
# 六、§3 gallery-dl 快路径代理稳健化（socks 缺 socksio 不许让任务死）
# ==================================================================
GD = SRC / "backends" / "gallery_dl.py"


def test_socks_url_recognised():
    """识别口径必须与 fetcher.py 一致（漏一个前缀就漏一次降级）。"""
    from media_resource_manager.backends.gallery_dl import is_socks_url
    for u in ("socks5://127.0.0.1:1080", "SOCKS5://h:1080",
              "socks5h://h:1080", "socks4://h:1080", "socks4a://h:1080"):
        assert is_socks_url(u) is True, f"{u} 没被识别成 SOCKS"
    for u in ("http://127.0.0.1:8080", "https://h:8443", "", None):
        assert is_socks_url(u) is False, f"{u} 被误判成 SOCKS"


def test_socks_to_http_requires_live_port():
    """★ 只在同 host:port 的 http 端口**真的能连**时才转（否则不如不转）。

    换成一个没人听的端口 = 让用户看一堆连接超时，比降级更难查。
    """
    import socket

    from media_resource_manager.backends.gallery_dl import socks_to_http

    # 起一个真 socket 当「同端口 http 可用」的现场
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        out = socks_to_http(f"socks5://127.0.0.1:{port}")
        assert out == f"http://127.0.0.1:{port}", f"没转成 http：{out!r}"
    finally:
        srv.close()
    # 没人听的端口 → 必须返回 None（交给降级分支）
    assert socks_to_http("socks5://127.0.0.1:1") is None, \
        "端口不通还转，任务会变成一堆连接超时"
    # 非 socks → 原样返回（http 代理不许被动）
    assert socks_to_http("http://h:8080") == "http://h:8080"


def test_backend_degrades_when_socks_unconvertible(monkeypatch):
    """★ 核心：socks 换不成 http 时，backend **降级为不用代理**且给中文提示。

    红→绿：改码前把 socks5 原样塞给 gallery-dl ⇒ 子进程
    `InvalidSchema: Missing dependencies for SOCKS support` ⇒ 一张图都没下、
    任务只写 error。这里钉住「不崩 + 中文 + 真的不带代理」。
    """
    from media_resource_manager.backends import gallery_dl as G

    monkeypatch.setattr(G, "socks_to_http", lambda u: None)
    be = G.GalleryDLBackend(dest_root=Path(tempfile.mkdtemp()), proxy="socks5://h:1080")
    assert be.degraded is True, "socks 换不成 http 却没降级"
    assert be.proxy == "socks5://h:1080", "用户填的原值要留着（UI 要显示）"
    assert be.effective_proxy is None, "降级了却还在下发代理"
    assert be.notice, "没有中文提示，用户只看到「一张图都没下」"
    assert "代理" in be.notice and "socksio" in be.notice.lower()
    assert not be.notice.strip().startswith(("Traceback", "ImportError"))
    # 配置里不能带 socks 代理（带了就必炸）
    assert "proxy" not in be._make_config()["extractor"], \
        "降级后配置里还留着 socks 代理"


def test_backend_converts_socks_when_http_port_live(monkeypatch):
    """socks 能换成 http 时：下发的是 http 代理，且不算降级。"""
    from media_resource_manager.backends import gallery_dl as G

    monkeypatch.setattr(G, "socks_to_http", lambda u: "http://h:1080")
    be = G.GalleryDLBackend(dest_root=Path(tempfile.mkdtemp()), proxy="socks5://h:1080")
    assert be.degraded is False
    assert be.effective_proxy == "http://h:1080"
    assert be._make_config()["extractor"]["proxy"] == "http://h:1080", \
        "转换后没把 http 代理真的交给 gallery-dl"


def test_backend_http_proxy_untouched():
    """HTTP 代理不许被误降级/误转换（那会让用户以为走了代理其实直连）。"""
    from media_resource_manager.backends.gallery_dl import GalleryDLBackend
    be = GalleryDLBackend(dest_root=Path(tempfile.mkdtemp()),
                          proxy="http://127.0.0.1:8080")
    assert be.degraded is False
    assert be.effective_proxy == "http://127.0.0.1:8080"
    assert be.notice == ""
    assert be._make_config()["extractor"]["proxy"] == "http://127.0.0.1:8080"


def test_backend_no_proxy_is_silent():
    """没填代理时不许冒提示、不许写 proxy 键（否则行为变化）。"""
    from media_resource_manager.backends.gallery_dl import GalleryDLBackend
    be = GalleryDLBackend(dest_root=Path(tempfile.mkdtemp()), proxy=None)
    assert be.degraded is False and be.notice == ""
    assert "proxy" not in be._make_config()["extractor"]


def test_make_config_is_the_single_proxy_wiring_point():
    """★ 单处接线：`_make_config` 只能读 `effective_proxy`。

    变异自检逼出来的判据 —— 若有人在 `_make_config` 里改回 `self.proxy`，
    「降级了但配置里还带 socks」这个致命组合会立刻复活，而上面那些
    「be.effective_proxy is None」的断言**照样全绿**（假通过）。
    """
    src = _read(GD)
    seg = src.split("def _make_config", 1)[1].split("def download", 1)[0]
    assert 'ex["proxy"] = self.effective_proxy' in seg, \
        "_make_config 没把 effective_proxy 交给 gallery-dl（降级会失效）"
    assert 'ex["proxy"] = self.proxy' not in seg, \
        "_make_config 又回到直接下发 self.proxy（socks 会必炸）"

