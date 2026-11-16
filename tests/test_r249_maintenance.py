"""R249：磁盘级维护四件套（完整性 / 0KB 残片 / 转 JPG / 修复图库）。

四件事原来只有桌面端有，而且其中两件在安装包里**根本 import 不到**
（``tools/`` 不在打包清单里）—— 所以除了测功能，这里还钉死「不许再依赖
``tools.``」这条，否则打包版会静默退化。

真模型的精度/速度不在这里测：``tools/_probe_r249_maint.py`` 用真文件走完
HTTP 全链路（扫描→删记录→清残片→试算→转换→同名冲突→修复→并发）。
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"


def _img(path: Path, w: int = 64, h: int = 48, color=(120, 40, 40)):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), color).save(path)
    return path


@pytest.fixture()
def db(tmp_path):
    from media_resource_manager.db import Database
    d = Database(tmp_path / "mrm.db")
    yield d
    d.close()


@pytest.fixture()
def cfg(tmp_path):
    from media_resource_manager.config import AppConfig
    c = AppConfig()
    c.db_path = tmp_path / "mrm.db"
    c.library_dir = tmp_path / "library"
    c.isolate_dir = tmp_path / "isolate"
    c.review_dir = tmp_path / "review"
    c.log_dir = tmp_path / "logs"
    for d in (c.library_dir, c.isolate_dir, c.review_dir, c.log_dir):
        Path(d).mkdir(parents=True, exist_ok=True)
    c.ai_model_path = ""
    c.ai_labels = ["person"]
    c.suspicious_bytes = 0
    return c


def _add(db, tid, path, **kw):
    from pathlib import Path as P
    p = P(path)
    db.execute(
        "INSERT INTO images(task_id, source_url, path, width, height, bytes,"
        " tags, ai_state, in_quarantine, needs_review, score) "
        "VALUES (?,?,?,?,?,?,'[]',1,?,?,?)",
        (tid, kw.pop("url", str(p)), str(p), 64, 48,
         p.stat().st_size if p.exists() else 0,
         int(kw.pop("q", 0)), int(kw.pop("rev", 0)), float(kw.pop("score", 0))))


@pytest.fixture()
def tid(db):
    db.execute("INSERT INTO tasks(name) VALUES ('t')")
    return db.query("SELECT id FROM tasks")[0]["id"]


# ───────────────────── ① 完整性检查 ─────────────────────
def test_scan_finds_missing_zero_oversize_and_broken(db, cfg, tid, tmp_path):
    from media_resource_manager import maintenance as mt

    ok = _img(cfg.library_dir / "ok.jpg")
    zero = cfg.library_dir / "zero.jpg"
    zero.write_bytes(b"")
    broken = _img(cfg.library_dir / "broken.jpg")
    broken.write_bytes(broken.read_bytes()[:40])       # 截断
    small = _img(cfg.library_dir / "small.jpg", 8, 8)
    _add(db, tid, ok, url="a")
    _add(db, tid, zero, url="b")
    _add(db, tid, broken, url="c")
    _add(db, tid, tmp_path / "gone.jpg", url="d")
    _add(db, tid, small, url="e")

    cfg.suspicious_bytes = 0
    res = mt.scan(db, cfg, deep=True)
    kinds = res["kinds"]
    assert kinds.get("文件缺失") == 1, kinds
    assert kinds.get("0 字节") == 1, kinds
    assert kinds.get("无法解码") == 1, kinds
    assert res["checked"] == 5

    # 「过小」是独立开关：打开它，小图才计入
    cfg.suspicious_bytes = 1024
    res2 = mt.scan(db, cfg, deep=False)
    assert any("过小" in k for k in res2["kinds"]), res2["kinds"]


def test_scan_is_read_only(db, cfg, tid):
    """检查类动作绝不能改数据 —— 用户被删怕过。"""
    from media_resource_manager import maintenance as mt

    p = _img(cfg.library_dir / "ok.jpg")
    _add(db, tid, p)
    before = db.query("SELECT id, path, score FROM images")
    mt.scan(db, cfg, deep=True)
    after = db.query("SELECT id, path, score FROM images")
    assert [dict(r) for r in before] == [dict(r) for r in after]
    assert p.exists()


def test_scan_caps_items_but_keeps_count(db, cfg, tid, monkeypatch):
    from media_resource_manager import maintenance as mt

    monkeypatch.setattr(mt, "MAX_ITEMS", 2)
    for i in range(5):
        _add(db, tid, cfg.library_dir / f"gone{i}.jpg", url=f"g{i}")
    res = mt.scan(db, cfg)
    assert len(res["items"]) == 2
    assert sum(res["kinds"].values()) == 5
    assert res["truncated"] is True


def test_scan_honours_stop(db, cfg, tid):
    from media_resource_manager import maintenance as mt

    for i in range(50):
        _add(db, tid, cfg.library_dir / f"g{i}.jpg", url=f"s{i}")
    res = mt.scan(db, cfg, stop=lambda: True)
    assert res["stopped"] is True
    assert res["checked"] == 0


# ─────────────────── ② 删异常记录（只删库） ───────────────────
def test_clean_records_removes_rows_and_children(db, cfg, tid):
    from media_resource_manager import maintenance as mt

    p = _img(cfg.library_dir / "ok.jpg")
    _add(db, tid, p)
    iid = db.query("SELECT id FROM images")[0]["id"]
    db.execute("INSERT INTO reviews(image_id) VALUES (?)", (iid,))
    res = mt.clean_records(db, [iid])
    assert res["deleted"] == 1
    assert db.query("SELECT COUNT(*) c FROM images")[0]["c"] == 0
    assert db.query("SELECT COUNT(*) c FROM reviews")[0]["c"] == 0
    # ⚠ 文件名里带「只删库记录」：磁盘文件必须还在
    assert p.exists(), "只该删记录，不该动磁盘文件"


def test_clean_records_ignores_bad_ids(db):
    from media_resource_manager import maintenance as mt
    assert mt.clean_records(db, ["abc", None, {}])["deleted"] == 0


# ─────────────────── ③ 清理 0KB / .part ───────────────────
def test_move_orphan_files_moves_not_deletes(cfg, tmp_path):
    from media_resource_manager import maintenance as mt

    d = tmp_path / "dl"
    d.mkdir()
    (d / "a.jpg").write_bytes(b"")
    (d / "b.part").write_bytes(b"xyz")
    (d / "keep.jpg").write_bytes(b"real-data")
    trash = tmp_path / "trash"
    res = mt.move_orphan_files([d], trash)
    assert res["moved"] == 2
    assert not (d / "a.jpg").exists() and not (d / "b.part").exists()
    assert (d / "keep.jpg").exists()
    # 是「移动」不是「删除」—— trash 里能找回内容
    names = [p.name for p in trash.iterdir()]
    assert len(names) == 2
    assert any((trash / n).read_bytes() == b"xyz" for n in names)


def test_orphan_dirs_never_includes_user_album(db, cfg, tmp_path):
    """R152-B4 的越界问题不能被我这一轮又写回来。

    「从文件夹学习」建的任务，它的 output_dir 就是用户自己选的相册目录。
    那个目录一旦进了清理白名单，用户相册里的 0 字节文件会被当垃圾收走。
    """
    from media_resource_manager import maintenance as mt

    album = tmp_path / "我的相册"
    album.mkdir()
    db.execute(
        "INSERT INTO tasks(name, entry_urls) VALUES ('学', ?)",
        (json.dumps([str(album)]),))
    dirs = [str(d) for d in mt.orphan_dirs(db, cfg)]
    assert str(album) not in dirs
    assert not any(d.startswith(str(album)) for d in dirs)


def test_clean_orphans_uses_whitelist_dirs(db, cfg):
    from media_resource_manager import maintenance as mt

    (cfg.library_dir / "zero.jpg").write_bytes(b"")
    res = mt.clean_orphans(db, cfg)
    assert res["moved"] >= 1
    assert not (cfg.library_dir / "zero.jpg").exists()
    assert Path(res["trash"]).is_dir()


# ─────────────────── ④ 批量转 JPG ───────────────────
def test_convert_updates_db_path(db, cfg, tid):
    from media_resource_manager import maintenance as mt

    p = _img(cfg.library_dir / "a.png")
    _add(db, tid, p)
    res = mt.convert_to_jpg(db, cfg)
    assert res["converted"] == 1
    newp = cfg.library_dir / "a.jpg"
    assert newp.exists() and not p.exists()
    row = db.query("SELECT path, bytes FROM images")[0]
    assert row["path"].endswith("a.jpg")
    assert row["bytes"] == newp.stat().st_size, "字节数没跟着更新"


def test_convert_dry_run_touches_nothing(db, cfg, tid):
    from media_resource_manager import maintenance as mt

    p = _img(cfg.library_dir / "a.png")
    _add(db, tid, p)
    res = mt.convert_to_jpg(db, cfg, dry_run=True)
    assert res["to_convert"] == 1 and res["converted"] == 0
    assert p.exists() and not (cfg.library_dir / "a.jpg").exists()
    assert db.query("SELECT path FROM images")[0]["path"].endswith(".png")


def test_convert_skips_when_target_exists(db, cfg, tid):
    """a.webp 与 a.jpg 可能是两张不同的图 —— 转过去会覆盖掉另一张。"""
    from media_resource_manager import maintenance as mt

    src = cfg.library_dir / "dup.webp"
    from PIL import Image
    Image.new("RGB", (64, 48), (10, 200, 200)).save(src)
    other = _img(cfg.library_dir / "dup.jpg", color=(250, 250, 0))
    before = other.read_bytes()
    _add(db, tid, src)
    res = mt.convert_to_jpg(db, cfg)
    assert res["skipped"] == 1 and res["converted"] == 0
    assert other.read_bytes() == before, "同名 jpg 被覆盖了"
    assert src.exists()


def test_convert_repoints_second_row_of_same_file(db, cfg, tid):
    """同一文件被两条记录引用时，第二条要改指 .jpg，不能算「文件缺失」。"""
    from media_resource_manager import maintenance as mt

    p = _img(cfg.library_dir / "same.png")
    _add(db, tid, p, url="u1")
    _add(db, tid, p, url="u2")
    res = mt.convert_to_jpg(db, cfg)
    assert res["converted"] == 1 and res["repointed"] == 1
    assert res["missing"] == 0
    paths = [r["path"] for r in db.query("SELECT path FROM images")]
    assert all(x.endswith("same.jpg") for x in paths), paths


# ─────────────────── ⑤ 修复图库 ───────────────────
def test_repair_drops_ghosts_and_empty_paths(db, cfg, tid):
    from media_resource_manager import maintenance as mt

    alive = _img(cfg.library_dir / "alive.jpg")
    _add(db, tid, alive, url="a")
    _add(db, tid, cfg.library_dir / "gone.jpg", url="b")
    db.execute("INSERT INTO images(task_id, source_url, path, tags, ai_state) "
               "VALUES (?,?,NULL,'[]',1)", (tid, "null-path"))
    res = mt.repair(db, cfg, restore_quarantine=False, drop_ghosts=True)
    assert res["ghosts"] == 2
    left = [r["path"] for r in db.query("SELECT path FROM images")]
    assert left == [str(alive)], left


def test_repair_threshold_only_when_asked(db, cfg, tid):
    from media_resource_manager import maintenance as mt

    db.execute("UPDATE tasks SET ai_enabled=1, ai_threshold=0.9, "
               "ai_labels='[\"person\"]'")
    mt.repair(db, cfg, fix_threshold=False, restore_quarantine=False,
              drop_ghosts=False)
    assert db.query("SELECT ai_threshold FROM tasks")[0]["ai_threshold"] == 0.9
    res = mt.repair(db, cfg, fix_threshold=True, restore_quarantine=False,
                    drop_ghosts=False)
    assert res["threshold_changed"] == 1
    assert db.query("SELECT ai_threshold FROM tasks")[0]["ai_threshold"] == mt.SAFE_THRESHOLD


def test_repair_restores_quarantined_with_model(db, cfg, tid, monkeypatch):
    from media_resource_manager import maintenance as mt
    from media_resource_manager.classify import clip_model

    p = _img(cfg.library_dir / "q.jpg")
    _add(db, tid, p, q=1, score=0.0)
    db.execute("UPDATE tasks SET ai_enabled=1, ai_labels='[\"person\"]'")

    class _Fake:
        def __init__(self, *a, **k):
            pass

        def classify(self, path):
            return [("person", 0.8)]

    monkeypatch.setattr(clip_model, "LocalClassifier", _Fake)
    res = mt.repair(db, cfg, restore_quarantine=True, drop_ghosts=False)
    assert res["restored"] == 1, res
    row = db.query("SELECT in_quarantine, score FROM images")[0]
    assert row["in_quarantine"] == 0 and row["score"] > 0.5


# ─────────────────── 打包相关性（真 bug 的护栏） ───────────────────
def test_runtime_code_never_imports_tools():
    """``tools/`` 不在打包清单里，运行时 import 它就是安装版必崩。

    实测踩到：桌面端「修复图库」「清理 0kb」两处 + ``tidy`` 的自动清理
    都是 ``from tools.repair_gallery import ...``，而 ``tidy`` 那句
    ``except: return 0`` 把异常吞了 —— 安装版里每个任务跑完的自动清理
    一直是空跑，看起来一切正常。
    """
    bad = []
    for p in PKG.rglob("*.py"):
        src = p.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"^\s*(?:from|import)\s+tools[.\s]", src, re.M):
            line = src[:m.start()].count("\n") + 1
            bad.append(f"{p.relative_to(ROOT)}:{line}")
    assert not bad, "运行时模块仍在 import tools（打包版会崩）：\n" + "\n".join(bad)


def test_build_does_not_package_tools():
    """反向确认上面那条判断的前提仍然成立（哪天打包了 tools 就该改口径）。"""
    src = (ROOT / "build_nodel.py").read_text(encoding="utf-8", errors="replace")
    assert "tools" not in src, "打包清单里出现了 tools —— 上面那条护栏的口径要复核"


# ─────────────────── 网页端接线 ───────────────────
def test_backend_has_all_maintenance_routes():
    src = (PKG / "webui.py").read_text(encoding="utf-8")
    for k in ("/api/maintenance/scan", "/api/maintenance/orphans",
              "/api/maintenance/convert", "/api/maintenance/repair",
              "/api/maintenance/clean", "/api/maintenance/cancel",
              "/api/maintenance/job"):
        assert f'"{k}"' in src, f"缺路由 {k}"


def test_clean_endpoint_rejects_empty_and_huge():
    src = (PKG / "webui.py").read_text(encoding="utf-8")
    seg = src.split("def _api_maint_clean", 1)[1].split("def _api_full_list", 1)[0]
    assert "没有勾选任何记录" in seg
    assert "5000" in seg, "缺批量上限：一次删几万条会把库锁死"


def test_frontend_wires_maintenance():
    s = (PKG / "webapp/src/components/DiskMaint.vue").read_text(encoding="utf-8")
    for k in ("文件完整性检查", "清理 0 字节与下载残片", "批量转成 JPG", "修复图库"):
        assert k in s, f"维护面板缺「{k}」"
    assert "maintStart" in s and "maintJob" in s
    api = (PKG / "webapp/src/api.js").read_text(encoding="utf-8")
    for k in ("maintStart:", "maintJob:", "maintCancel:", "maintClean:"):
        assert k in api, f"api.js 缺 {k}"
    st = (PKG / "webapp/src/views/Settings.vue").read_text(encoding="utf-8")
    assert "<DiskMaint" in st, "设置页没挂维护面板"


def test_icons_used_by_maintenance_exist():
    """图标名写错只会退化成灰点、不报错 —— 必须静态兜住。"""
    s = (PKG / "webapp/src/components/DiskMaint.vue").read_text(encoding="utf-8")
    icons = (PKG / "webapp/src/icons.js").read_text(encoding="utf-8")
    names = set(re.findall(r'<Icon\s+name="([a-z0-9-]+)"', s))
    assert names, "面板里一个图标都没用到？"
    missing = [n for n in names if f'"{n}":' not in icons]
    assert not missing, f"icons.js 里没有：{missing}"


def test_built_bundle_contains_maintenance():
    d = PKG / "web/static/app/assets"
    js = "".join(p.read_text(encoding="utf-8", errors="replace")
                 for p in d.glob("*.js"))
    assert "磁盘维护" in js or "文件完整性检查" in js, "前端没重新构建"
    assert "/api/maintenance/" in js
