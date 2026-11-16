"""R247：长期运行的两处结构性问题 —— 缩略图分桶 + 增长表保留策略。

这两条都不是「功能」，是**规模一上来就会变糟**的东西：

1. 缩略图原来全部平铺在 ``data/thumbs/`` 一层。10 万张图就是十几万个
   小文件挤在同一目录，备份/完整性检查/rsync 全部退化。改成按文件名
   哈希分两级桶（256×16 = 4096 个），旧平铺文件仍然认、不迁移。

2. 审计日志/页面账本/失败网址/磁盘快照只增不减。10 万张图约 3–8 万条
   审计记录，百万级就是上百万行，库和备份一起变大。改成「按天 + 按行数」
   双保险，并且**未完成的页面记录不能按时间删**（那等于把断点丢了）。
"""
import io
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"


def _src(rel: str) -> str:
    return (PKG / rel).read_text(encoding="utf-8")


# ───────────────────────── 缩略图分桶 ─────────────────────────
def test_bucket_path_is_deterministic_and_two_level():
    from media_resource_manager import thumbpath as tp

    a = tp.cache_path("/x/thumbs", "123_256.jpg")
    b = tp.cache_path("/x/thumbs", "123_256.jpg")
    assert a == b, "同一文件名必须落到同一路径（幂等，否则缓存永远命中不了）"
    assert a.name == "123_256.jpg"
    # <root>/<2 位>/<1 位>/<name>
    assert a.parent.parent.parent == Path("/x/thumbs")
    assert len(a.parent.name) == 1 and len(a.parent.parent.name) == 2


def test_bucket_spreads_files_across_many_dirs():
    """4096 个桶的意义：名字散开，单目录文件数不随总量线性增长。"""
    from media_resource_manager import thumbpath as tp

    dirs = {tp.cache_path("/t", f"{i}_256.jpg").parent for i in range(500)}
    assert len(dirs) > 60, f"500 个文件只落到 {len(dirs)} 个目录，分桶没生效"


def test_find_falls_back_to_legacy_flat_file(tmp_path):
    """旧的平铺文件必须仍然能读到 —— 不迁移是为了不白费一次重新生成。"""
    from media_resource_manager import thumbpath as tp

    legacy = tmp_path / "9_160.jpg"
    legacy.write_bytes(b"x")
    assert tp.find(tmp_path, "9_160.jpg") == legacy
    # 新路径优先
    new = tp.cache_path(tmp_path, "9_160.jpg")
    new.parent.mkdir(parents=True)
    new.write_bytes(b"y")
    assert tp.find(tmp_path, "9_160.jpg") == new


def test_find_has_no_side_effect(tmp_path):
    """查一个不存在的缩略图**不能建目录**（查询不该有副作用）。"""
    from media_resource_manager import thumbpath as tp

    p = tp.find(tmp_path, "404_256.jpg")
    assert not p.exists()
    assert not p.parent.exists(), "find() 建了目录 —— 只该返回路径"
    assert list(tmp_path.iterdir()) == []


def test_web_thumb_writer_uses_bucketed_path():
    src = _src("webui.py")
    seg = src.split("def _thumb_for", 1)[1].split("\ndef ", 1)[0]
    assert "thumbpath" in seg and "cache_path" in seg, \
        "网页端缩略图没走分桶路径，十万张会平铺在一个目录里"
    # 旧路径必须仍然被查（不然已部署的 data/ 里那些文件白瞎）
    assert "legacy_path" in seg


def test_desktop_thumb_writer_uses_bucketed_path():
    src = _src("gui/table_view.py")
    seg = src.split("def square_thumb_pixmap", 1)[1].split("\ndef ", 1)[0]
    assert "thumbpath" in seg and "cache_path" in seg, \
        "桌面端缩略图没走分桶路径"
    assert "find(d, _name)" in seg


@pytest.fixture(scope="module")
def _qapp():
    """QPixmap 必须有 QApplication（conftest 已把平台设成 offscreen）。"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_thumb_writer_really_writes_into_bucket(tmp_path, _qapp):
    """真跑一次：写出来的文件必须落在 <root>/xx/y/ 下。"""
    from PIL import Image
    from media_resource_manager.gui.table_view import square_thumb_pixmap

    src = tmp_path / "src.jpg"
    Image.new("RGB", (64, 48), (120, 30, 30)).save(src)
    cache = tmp_path / "thumbs"
    pm = square_thumb_pixmap(src, 96, cache, key="k1", crop=False)
    assert pm is not None and not pm.isNull()
    files = list(cache.rglob("*.png"))
    assert len(files) == 1
    f = files[0]
    assert f.parent.parent.parent == cache, f"缩略图没进桶：{f}"
    assert len(f.parent.name) == 1 and len(f.parent.parent.name) == 2


# ───────────────────────── 增长表保留策略 ─────────────────────────
@pytest.fixture()
def db(tmp_path):
    from media_resource_manager.db import Database

    d = Database(tmp_path / "mrm.db")
    d.execute("INSERT INTO tasks(name) VALUES ('t1')")
    yield d
    d.close()


def _cfg(**kw):
    base = dict(retain_audit_days=90, retain_audit_rows=200_000,
                retain_pages_days=30, retain_failed_days=90,
                retain_snap_days=730)
    base.update(kw)
    return SimpleNamespace(**base)


def test_audit_pruned_by_age(db):
    from media_resource_manager.data_growth import prune_growth

    db.execute("INSERT INTO audit_log(ts,actor,action) VALUES "
               "(datetime('now','-200 days'),'a','old')")
    db.execute("INSERT INTO audit_log(actor,action) VALUES ('a','new')")
    st = prune_growth(db, _cfg())
    rows = db.query("SELECT action FROM audit_log")
    assert [r["action"] for r in rows] == ["new"]
    assert st["audit_log"] >= 1


def test_audit_capped_by_row_count(db):
    """某一晚疯狂刷的尖峰：时间没到，但行数已经几十万。"""
    from media_resource_manager.data_growth import prune_growth

    for i in range(300):
        db.execute("INSERT INTO audit_log(actor,action,target) "
                   "VALUES ('a','x',?)", (str(i),))
    prune_growth(db, _cfg(retain_audit_rows=100))
    n = db.query("SELECT COUNT(*) c FROM audit_log")[0]["c"]
    assert 0 < n <= 100, f"行数封顶没生效，还剩 {n} 行"
    # 留下的是**最新**那批（按 id 封顶，不是随机删）
    last = db.query("SELECT target FROM audit_log ORDER BY id DESC LIMIT 1")[0]
    assert last["target"] == "299"


def test_unfinished_pages_never_pruned_by_age(db):
    """未抓完的页面是断点续采的命根子，不能按时间清。"""
    from media_resource_manager.data_growth import prune_growth

    tid = db.query("SELECT id FROM tasks")[0]["id"]
    db.execute("INSERT INTO task_pages(task_id,url,status,last_at) VALUES "
               "(?, 'u1','done', datetime('now','-60 days'))", (tid,))
    db.execute("INSERT INTO task_pages(task_id,url,status,last_at) VALUES "
               "(?, 'u2','pending', datetime('now','-60 days'))", (tid,))
    db.execute("INSERT INTO task_pages(task_id,url,status,last_at) VALUES "
               "(?, 'u3','error', datetime('now','-60 days'))", (tid,))
    db.execute("INSERT INTO task_pages(task_id,url,status,last_at) VALUES "
               "(?, 'u4','done', datetime('now','-1 days'))", (tid,))
    prune_growth(db, _cfg())
    left = {r["url"] for r in db.query("SELECT url FROM task_pages")}
    assert left == {"u2", "u3", "u4"}, f"页面账本清错了：{left}"


def test_failed_urls_and_snapshots_pruned(db):
    from media_resource_manager.data_growth import prune_growth

    tid = db.query("SELECT id FROM tasks")[0]["id"]
    db.execute("INSERT INTO failed_urls(task_id,url,last_tried) VALUES "
               "(?, 'old', datetime('now','-200 days'))", (tid,))
    db.execute("INSERT INTO failed_urls(task_id,url) VALUES (?, 'new')", (tid,))
    db.execute("INSERT INTO disk_snapshots(day) VALUES (date('now','-800 days'))")
    db.execute("INSERT INTO disk_snapshots(day) VALUES (date('now'))")
    prune_growth(db, _cfg())
    assert {r["url"] for r in db.query("SELECT url FROM failed_urls")} == {"new"}
    assert len(db.query("SELECT day FROM disk_snapshots")) == 1


def test_bad_config_values_fall_back_to_defaults(db):
    """配置被改成 0 或乱字符串时，不能把表清空。"""
    from media_resource_manager.data_growth import prune_growth

    db.execute("INSERT INTO audit_log(actor,action) VALUES ('a','keep')")
    st = prune_growth(db, _cfg(retain_audit_days=0, retain_audit_rows="abc"))
    assert db.query("SELECT COUNT(*) c FROM audit_log")[0]["c"] == 1
    assert st["audit_log"] == 0


def test_prune_never_raises_on_broken_db(tmp_path):
    """库坏了也必须安静返回 —— 清理线程不能把服务拖死。"""
    from media_resource_manager.data_growth import prune_growth

    class Dead:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("db is gone")

        def query(self, *a, **k):
            raise sqlite3.OperationalError("db is gone")

    st = prune_growth(Dead(), None)
    assert isinstance(st, dict)
    assert sum(st.values()) == 0


def test_config_defaults_exist():
    from media_resource_manager.config import AppConfig

    c = AppConfig()
    assert c.retain_audit_days == 90
    assert c.retain_audit_rows == 200_000
    assert c.retain_pages_days == 30
    assert c.retain_failed_days == 90
    assert c.retain_snap_days == 730


def test_web_server_starts_growth_maintenance():
    src = _src("webui.py")
    assert "_start_growth_maintenance()" in src
    seg = src.split("def _start_growth_maintenance", 1)[1].split("\n    def ", 1)[0]
    assert "prune_growth" in seg
    assert "daemon=True" in seg, "维护线程必须是 daemon，否则退出时挂住"


# ───────────────────────── 备份瘦身 ─────────────────────────
def test_backup_defaults_without_thumbs():
    """缩略图可重建，默认不打包：百万级时它占备份体积的绝大部分。"""
    web = _src("webui.py")
    assert "include_thumbs = False" in web
    api = (ROOT / "media_resource_manager/webapp/src/api.js").read_text(encoding="utf-8")
    assert "fullExport: (includeThumbs = false)" in api
    st = (ROOT / "media_resource_manager/webapp/src/views/Settings.vue").read_text(
        encoding="utf-8")
    assert "const fullIncludeThumbs = ref(false)" in st
    # 恢复前自动备份同样不带缩略图（退路只要数据）
    seg = web.split("恢复前的自动备份失败", 1)[0]
    assert "include_thumbs=False" in seg


# ───────────────────── 前端构建产物不能越堆越多 ─────────────────────
def test_build_script_cleans_old_assets():
    """构建前必须清旧产物。

    踩到的实况：vite 的 `outDir` 是整个 `web/`（那里还有 sw.js、static/img、
    index.v2.html，不能整体清空），`emptyOutDir` 只能是 false —— 于是
    `static/app/assets` 只增不删，实测堆到 **53 个 js、13 MB**，全部被
    PyInstaller 打进安装包。清完是 336 KB。
    """
    import json

    pkg = json.loads((ROOT / "media_resource_manager/webapp/package.json")
                     .read_text(encoding="utf-8"))
    assert "clean-assets" in pkg["scripts"]["build"], \
        "build 脚本没挂清理步骤，旧产物会一直往安装包里堆"
    assert (ROOT / "media_resource_manager/webapp/scripts/clean-assets.mjs").is_file()


def test_web_assets_contain_no_stale_chunks():
    """产物目录里只该有「这一次构建」的东西，且入口引用的那个必须在。"""
    d = PKG / "web" / "static" / "app" / "assets"
    files = [p for p in d.iterdir() if p.is_file()]
    assert len(files) <= 8, (
        f"前端产物堆了 {len(files)} 个文件（旧构建没清）："
        + ", ".join(sorted(p.name for p in files)[:10]))
    html = (PKG / "web" / "index.v2.html").read_text(encoding="utf-8")
    m = re.search(r"assets/([A-Za-z0-9._-]+\.js)", html)
    assert m, "index.v2.html 里没引用 js 产物"
    assert (d / m.group(1)).is_file(), f"入口引用的 {m.group(1)} 不存在（构建没落地）"
