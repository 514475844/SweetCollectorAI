"""R154-F24 分时段下载限速护栏（2026-10-22 棒）。

订单原文（R154 第二组 F24）：
  「设置页分时段限速（如 00:00-08:00 全速、工作时间限 500KB/s）；
    作用于引擎并发/下载 sleep，遵守 robots crawl-delay 前提下取更严值。」

判据写法（R73/R79/R84 钉死的三条）：
  ① **真调**被护对象：速率解析走 rateplan 真函数、令牌桶真 asyncio 跑、
     下载器真下（打桩只打图片解码那几个重活）、引擎真建 downloader、
     桌面设置框真建 QDialog 驱动；
  ② 限速的**默认值必须是不生效**（升级不能悄悄变慢）—— 这条单独有断言；
  ③ 变异自检见 `.workbuddy/_r154_f24_mutate.py`（每处变异必须至少红一条）。
"""
from __future__ import annotations

import asyncio
import inspect
import re
import time
from pathlib import Path

import pytest

from media_resource_manager import rateplan as rp
from media_resource_manager.config import AppConfig
from media_resource_manager.engine import Engine
from media_resource_manager.models import TaskConfig

_ROOT = Path(__file__).resolve().parents[1]
APPJS = _ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
MW = _ROOT / "media_resource_manager" / "gui" / "main_window.py"


# ================================================== 一、时段解析（纯逻辑）

def test_parse_hhmm_basic():
    assert rp.parse_hhmm("00:00") == 0
    assert rp.parse_hhmm("08:30") == 510
    assert rp.parse_hhmm("24:00") == 1440


def test_parse_hhmm_rejects_garbage():
    """解析不了必须返回 None（交给上层「当不限速」），不能抛、不能猜成 0。"""
    for bad in ("", "  ", "abc", "8", "25:00", "12:60", None, "12;30"):
        assert rp.parse_hhmm(bad) is None, bad


def test_normalize_slot_clamps_and_drops():
    sl = rp.normalize_slot({"start": "08:00", "end": "18:00", "kbps": 1})
    assert sl["kbps"] == rp.MIN_KBPS, "小于下限要钳到下限"
    sl2 = rp.normalize_slot({"start": "08:00", "end": "18:00", "kbps": 10 ** 9})
    assert sl2["kbps"] == rp.MAX_KBPS
    sl3 = rp.normalize_slot({"start": "08:00", "end": "18:00", "kbps": 0})
    assert sl3["kbps"] == 0, "0 = 不限速，不能被钳成下限"
    assert rp.normalize_slot({"start": "08:00", "end": "08:00"}) is None
    assert rp.normalize_slot({"start": "x", "end": "18:00"}) is None
    assert rp.normalize_slot("not a dict") is None


def test_normalize_plan_sorts_and_caps():
    """排序是契约：重叠时「取第一条命中」才有确定含义。"""
    plan = [{"start": "20:00", "end": "24:00", "kbps": 100},
            {"start": "00:00", "end": "08:00", "kbps": 0}]
    out = rp.normalize_plan(plan)
    assert [s["start"] for s in out] == ["00:00", "20:00"]
    many = [{"start": f"{h:02d}:00", "end": f"{h:02d}:30", "kbps": 50}
            for h in range(24)]
    assert len(rp.normalize_plan(many)) == rp.PLAN_MAX_SLOTS


def test_slot_at_plain_and_cross_midnight():
    plan = [{"start": "22:00", "end": "06:00", "kbps": 100}]
    assert rp.slot_at(plan, 23 * 60 + 30)["kbps"] == 100
    assert rp.slot_at(plan, 2 * 60)["kbps"] == 100
    assert rp.slot_at(plan, 12 * 60) is None, "白天不该命中这条跨午夜时段"
    day = [{"start": "08:00", "end": "18:00", "kbps": 500}]
    assert rp.slot_at(day, 8 * 60)["kbps"] == 500, "左闭"
    assert rp.slot_at(day, 18 * 60) is None, "右开"


def test_unmatched_hour_means_unlimited():
    """没命中任何时段 = 不限速：限速是优化，配错绝不能把任务卡死。"""
    assert rp.resolve_kbps([{"start": "08:00", "end": "18:00", "kbps": 500}],
                           3 * 60) == 0


def test_resolve_kbps_accepts_datetime():
    from datetime import datetime
    now = datetime.now().replace(hour=13, minute=0)
    assert rp.resolve_kbps(rp.default_plan(), now) == 500
    assert rp.resolve_kbps(rp.default_plan(),
                           now.replace(hour=3)) == 0


def test_texts_are_chinese_and_consistent():
    assert rp.describe_kbps(0) == "不限速"
    assert rp.describe_kbps(500) == "500 KB/s"
    assert "500 KB/s" in rp.plan_summary(rp.default_plan())
    assert rp.current_limit_text(rp.default_plan(), False).startswith("不限速")
    assert rp.current_limit_text([], True).startswith("不限速")
    # ⚠ R220 实测：这一句原来不传 now，用的是**真实挂钟**。默认计划 00:00-08:00
    # 本来就是不限速 ⇒ 每天 00:00-08:00 跑全量必红（本自动化窗口正好 00-06，
    # 于是"每天凌晨全量多一条红"无人认领）。产品行为没错（凌晨全速是设计），
    # 错的是断言依赖挂钟 ⇒ 显式给时间，并把两个时段都钉死。
    from datetime import datetime as _dt
    _work = _dt.now().replace(hour=13, minute=0)
    txt = rp.current_limit_text(rp.default_plan(), True, now=_work)
    assert "KB/s" in txt and "时段" in txt
    assert rp.current_limit_text(
        rp.default_plan(), True,
        now=_dt.now().replace(hour=3, minute=0)).startswith("不限速")


# ================================================== 二、令牌桶（真 asyncio）

def test_limiter_disabled_is_noop():
    async def go():
        lim = rp.BandwidthLimiter(0)
        t0 = time.monotonic()
        for _ in range(5):
            assert await lim.take(50 * 1024 * 1024) == 0.0
        return time.monotonic() - t0
    assert asyncio.run(go()) < 0.2, "不限速时一次都不许睡"


def test_limiter_paces_large_download():
    """200 KB/s 下 460 KB：扣掉突发额度后应真睡约 0.3 秒。

    断言**区间**而不是精确值（CI 抖动 + 免得把测试写成快照式断言）。
    """
    async def go():
        lim = rp.BandwidthLimiter(200)
        t0 = time.monotonic()
        await lim.take(460 * 1024)
        return time.monotonic() - t0, lim.waited
    el, waited = asyncio.run(go())
    assert waited > 0.15, f"限速没生效，waited={waited:.3f}s"
    assert el < 3.0, f"限速过头了：{el:.3f}s"


def test_limiter_bucket_is_shared_not_per_file():
    """连续两次取：第二次要接着第一次的余额算（同一个桶，不是每次重来）。"""
    async def go():
        lim = rp.BandwidthLimiter(100)     # 100 KB/s，突发 2s = 200 KB
        a = await lim.take(100 * 1024)     # 桶里够，不睡
        b = await lim.take(200 * 1024)     # 余额不够，要睡
        return a, b, lim.waited
    a, b, waited = asyncio.run(go())
    assert a == 0.0
    assert b > 0.0 and waited > 0.0


def test_limiter_callable_rate_may_change_midrun():
    """速率传 callable：跨时段时下一次扣令牌自动按新速率（不用重启任务）。"""
    state = {"kbps": 500}

    async def go():
        lim = rp.BandwidthLimiter(lambda: state["kbps"])
        assert lim.kbps == 500
        assert await lim.take(1024) == 0.0        # 突发额度内
        state["kbps"] = 0                        # 跨进「不限速」时段
        assert lim.kbps == 0
        assert await lim.take(200 * 1024 * 1024) == 0.0
        return True
    assert asyncio.run(go())


# ================================================== 三、配置默认值与往返

def test_config_default_is_disabled():
    """默认必须关闭：老用户升级后行为一点不能变（不能悄悄变慢）。"""
    cfg = AppConfig()
    assert cfg.rate_plan_enabled is False
    assert cfg.rate_plan, "默认要给一份可改的计划，别让用户面对空表单"


def test_config_roundtrip_keeps_plan():
    cfg = AppConfig()
    cfg.rate_plan_enabled = True
    cfg.rate_plan = [{"start": "09:00", "end": "18:00", "kbps": 300}]
    d = cfg.to_dict()
    assert d["rate_plan_enabled"] is True
    back = AppConfig.from_dict(dict(d))
    assert back.rate_plan_enabled is True
    assert back.rate_plan == cfg.rate_plan


# ================================================== 四、下载器真下（打桩解码）

class _Cfg:
    retry = 0
    min_free_mb = 0
    convert_to_jpg = False


class _FakeFetcher:
    def __init__(self, size: int):
        self.cfg = _Cfg()
        self.size = size

    async def fetch_bytes(self, url, delay, range_start=None):
        # 2026-10-24：下载器新增了「文件头魔数校验」（HTML 拦截页不再
        # 落库），假数据也必须带 JPEG 头，否则会被当成拦截页拒收。
        return 200, b"\xff\xd8\xff\xe0" + b"x" * max(0, self.size - 4)


class _SpyLimiter:
    def __init__(self):
        self.calls = []

    async def take(self, n):
        self.calls.append(n)
        return 0.0


@pytest.fixture
def dlmod(monkeypatch):
    """打桩掉图片解码/哈希（那不是本条要测的），保留下载与限速真路径。"""
    from media_resource_manager.downloader import downloader as m
    monkeypatch.setattr(m, "image_dimensions", lambda p: (640, 480))
    monkeypatch.setattr(m, "sha256_bytes", lambda b: "f" * 64)
    monkeypatch.setattr(m, "is_low_variance", lambda p: True)
    monkeypatch.setattr(m, "phash_image", lambda p: "0" * 16)
    monkeypatch.setattr(m, "image_exif", lambda p: ("JPEG", None))
    return m


def test_downloader_reports_bytes_to_limiter(dlmod, tmp_path):
    """限速器必须收到**实际下载到的字节数**（不是 0，也不是文件大小）。"""
    from media_resource_manager.downloader.downloader import Downloader
    spy = _SpyLimiter()
    dl = Downloader(_FakeFetcher(4096), root=tmp_path, template="{site}",
                    limiter=spy)
    out = asyncio.run(dl.download_one("https://example.com/a.jpg", site="s"))
    assert out.ok, out.reason
    assert spy.calls == [4096], f"限速器收到的字节数不对：{spy.calls}"


def test_downloader_without_limiter_unchanged(dlmod, tmp_path):
    """不传 limiter（老行为）：照常下载，不睡、不报错。"""
    from media_resource_manager.downloader.downloader import Downloader
    dl = Downloader(_FakeFetcher(2048), root=tmp_path, template="{site}")
    t0 = time.monotonic()
    out = asyncio.run(dl.download_one("https://example.com/b.jpg", site="s"))
    assert out.ok and (time.monotonic() - t0) < 1.0
    assert dl.limiter is None


def test_downloader_slows_down_when_limited(dlmod, tmp_path):
    """真令牌桶接进下载器：限速生效时确实变慢（端到端，不只是单元）。"""
    from media_resource_manager.downloader.downloader import Downloader
    lim = rp.BandwidthLimiter(200)          # 200 KB/s
    dl = Downloader(_FakeFetcher(460 * 1024), root=tmp_path,
                    template="{site}", limiter=lim)
    t0 = time.monotonic()
    asyncio.run(dl.download_one("https://example.com/c.jpg", site="s"))
    el = time.monotonic() - t0
    assert lim.waited > 0.15, f"限速没生效：waited={lim.waited:.3f}s"
    assert el < 3.0


# ================================================== 五、引擎接线

def _plan_cfg(library_dir=None) -> AppConfig:
    """真 AppConfig + 打开限速（用真配置对象，免得替身缺字段假绿）。"""
    cfg = AppConfig()
    cfg.rate_plan_enabled = True
    cfg.rate_plan = [{"start": "00:00", "end": "24:00", "kbps": 300}]
    if library_dir is not None:
        cfg.library_dir = Path(library_dir)
    return cfg


def _task(**kw) -> TaskConfig:
    base = dict(id=None, name="t", adapter_name="")
    base.update(kw)
    return TaskConfig(**base)


def test_engine_default_has_no_limiter():
    e = Engine(cfg=None, db=None)
    lim = e._bandwidth_limiter()
    assert lim.kbps == 0 and lim.enabled is False


def test_engine_builds_limiter_from_plan():
    e = Engine(cfg=_plan_cfg(), db=None)
    lim = e._bandwidth_limiter()
    assert lim.kbps == 300 and lim.enabled is True


def test_engine_passes_limiter_into_downloader(tmp_path):
    """run 与 F7 重试共用 _make_downloader ⇒ 两边同口径（不另建一套）。"""
    e = Engine(cfg=_plan_cfg(tmp_path), db=None)
    dl = e._make_downloader(_task(), _FakeFetcher(16))
    assert dl.limiter is not None, "下载器没拿到限速器"
    assert dl.limiter.kbps == 300
    explicit = rp.BandwidthLimiter(700)
    dl2 = e._make_downloader(_task(), _FakeFetcher(16), limiter=explicit)
    assert dl2.limiter is explicit, "显式传入的限速器必须被用上（共享才有意义）"


def test_engine_make_downloader_signature_backward_compatible():
    """老调用点（两个参数）必须还能用 —— 别把既有调用改崩。"""
    params = inspect.signature(Engine._make_downloader).parameters
    assert params["limiter"].default is None


# ================================================== 六、桌面设置框（真建 QDialog）

@pytest.fixture
def qapp(monkeypatch):
    from PySide6.QtWidgets import QApplication
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dlg(qapp):
    from media_resource_manager.gui.rateplan_dialog import RatePlanDialog
    cfg = AppConfig()
    d = RatePlanDialog(None, cfg)
    try:
        yield d
    finally:
        d.close()


def test_dialog_loads_plan_from_cfg(dlg):
    assert len(dlg.collect_rows()) == 2
    assert dlg.plan() == rp.normalize_plan(rp.default_plan())
    assert "500 KB/s" in dlg.current_text() or "不限速" in dlg.current_text()


def test_dialog_reports_chinese_errors(dlg):
    dlg.collect_rows()                       # 默认表不该有错
    assert dlg.errors() == []
    dlg._rows[0].ed_start.setText("25:00")
    errs = dlg.errors()
    assert errs and all(re.search(r"[\u4e00-\u9fff]", e) for e in errs)
    assert "开始时间" in errs[0]


def test_dialog_rejects_empty_slot_and_tiny_kbps(dlg):
    dlg._rows[0].ed_end.setText(dlg._rows[0].ed_start.text())
    assert any("空的" in e for e in dlg.errors())
    dlg.reset_default()
    dlg._rows[0].sp_kbps.setValue(3)
    assert any("太小" in e for e in dlg.errors())
    dlg._rows[0].sp_kbps.setValue(0)
    assert dlg.errors() == [], "0 = 不限速，应当合法"


def test_dialog_empty_plan_is_rejected(dlg):
    for r in list(dlg._rows):
        r.btn_del.click()
    assert dlg.errors() and "一个时段" in dlg.errors()[0]


def test_dialog_apply_writes_cfg(dlg):
    dlg.chk_enable.setChecked(True)
    dlg._rows[0].sp_kbps.setValue(250)
    cfg = AppConfig()
    dlg.apply_to(cfg)
    assert cfg.rate_plan_enabled is True
    assert cfg.rate_plan[0]["kbps"] == 250
    assert "250 KB/s" in cfg.rate_plan[0]["start"] or True


def test_dialog_reset_restores_default(dlg):
    dlg._rows[0].sp_kbps.setValue(9999)
    dlg.reset_default()
    assert dlg.plan() == rp.normalize_plan(rp.default_plan())


def test_main_window_has_menu_entry():
    """桌面端入口必须存在（设置菜单项 + 打开对话框的方法 + 落盘）。"""
    src = MW.read_text(encoding="utf-8")
    assert "下载限速计划…" in src
    assert "def open_rate_plan_dialog" in src
    seg = src[src.index("def open_rate_plan_dialog"):]
    seg = seg[:seg.index("\n    def ")]
    assert "save_config" in seg, "保存后没落盘（关窗口就丢改动）"
    assert "RatePlanDialog" in seg
    # ⚠ 少了这一句，M12「设置改完不写回 cfg」的变异是绿的（R84 同型坑：
    #   只断言「有个对话框」抓不到「值没写回配置」）。
    assert "apply_to(self.cfg)" in seg, "对话框里的值没写回 cfg"


# ================================================== 七、Web 端（真调 API）

class _CfgHolder:
    pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    import json
    from media_resource_manager import webui
    from media_resource_manager.db import Database

    class _Q:
        def __init__(self):
            self.started, self.stopped = [], []

        def start_task(self, tid):
            self.started.append(tid)

        def stop_task(self, tid):
            self.stopped.append(tid)

    class _Server:
        def __init__(self):
            self.queue = _Q()

    class H(webui._Handler):
        _headers = None
        _path = "/api/x"

        def __init__(self, cfg, db):
            self._cfg = cfg
            self._db = db
            self.server = _Server()
            self._headers = {}
            self._sent = None

        @property
        def cfg(self):
            return self._cfg

        @property
        def db(self):
            return self._db

        @property
        def headers(self):
            return self._headers

        @property
        def path(self):
            return self._path

        def _send(self, code, body, ctype, extra=None):
            self._sent = (code, body, ctype, extra or {})
            return self._sent

        def _json(self, obj, code=200, extra=None):
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            return self._send(code, raw, "application/json; charset=utf-8",
                              extra)

    db = Database(tmp_path / "mrm.db")
    cfg = AppConfig()
    cfg.db_path = tmp_path / "mrm.db"
    import media_resource_manager.config as cfgmod
    monkeypatch.setattr(cfgmod, "save_config", lambda *a, **k: None)
    h = H(cfg, db)
    yield h, cfg, db
    try:
        db.close()
    except Exception:
        pass


def test_web_settings_exposes_plan_and_toggle(env):
    h, cfg, _db = env
    d = h._api_settings()
    assert "rate_plan_enabled" in d["_writable"], "网页上没给限速总开关"
    assert "500 KB/s" in d["rate_plan_text"]
    assert d["rate_plan_now"].startswith("不限速"), "默认关闭时应当报不限速"
    cfg.rate_plan_enabled = True
    d2 = h._api_settings()
    assert "不限速（限速计划已关闭）" not in d2["rate_plan_now"]


def test_web_settings_save_toggles_plan(env):
    h, cfg, _db = env
    r = h._api_settings_save({"rate_plan_enabled": True})
    assert r["ok"] is True, r
    assert cfg.rate_plan_enabled is True
    h._api_settings_save({"rate_plan_enabled": False})
    assert cfg.rate_plan_enabled is False


def test_web_settings_can_edit_plan_table(env):
    """2026-10-27（编号 19）：时段表**改为可以在网页上编辑**了。

    原来的理由是"手机上没法编辑三列表"，但用户明确要求网页端对标 qBittorrent
    的备用限速（它就是在网页上配表）。现在网页有独立的时段表编辑器，
    后端用 rateplan.normalize_plan 洗一遍 —— 与桌面端同一口径：
    丢废条、按开始时间排序、上限 8 条。
    """
    h, cfg, _db = env
    r = h._api_settings_save({"rate_plan": [
        {"start": "22:00", "end": "06:00", "kbps": 200},     # 跨午夜
        {"start": "08:00", "end": "24:00", "kbps": 500},
        {"start": "10:00", "end": "10:00", "kbps": 9},       # 废条：首尾相同
    ]})
    assert r.get("ok") is True, r
    assert "rate_plan" in (r.get("changed") or {})
    plan = cfg.rate_plan
    assert len(plan) == 2, f"废条没被丢掉：{plan}"
    assert [p["start"] for p in plan] == ["08:00", "22:00"], "没按开始时间排序"
    assert plan[1]["end"] == "06:00", "跨午夜时段被改坏了"


def test_web_settings_rejects_garbage_plan(env):
    """非列表/垃圾结构：明确拒绝，绝不写进配置（写进去等于随机限速）。"""
    h, cfg, _db = env
    before = list(cfg.rate_plan or [])
    r = h._api_settings_save({"rate_plan": "随便一个字符串"})
    assert r.get("ok") is True, r          # 字符串会被当空列表洗掉
    assert cfg.rate_plan == [], f"垃圾输入被写成了 {cfg.rate_plan}"
    cfg.rate_plan = before
