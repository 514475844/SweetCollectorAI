"""V0.0.5 决定清单第 25 项：「复制到软件图库」开关。

用户原话：「开启后所有删/移只碰副本，F 盘彻底隔离。」
定稿：默认关；放选文件夹准备导入的小窗口，跟「只入库先不打标签」挨着；
桌面和网页同一个勾。本文件钉后端语义（两条入口共用），界面入口在
test_v005_ui 断言存在性。
"""
from __future__ import annotations

import json

import pytest

from media_resource_manager import import_folder as imp
from media_resource_manager import learn_folder as lf


@pytest.fixture()
def db(tmp_path):
    from media_resource_manager.db import Database
    home = tmp_path / "home"
    home.mkdir()
    d = Database(home / "mrm.db")
    yield d
    d.close()


@pytest.fixture()
def cfg(tmp_path):
    """数据目录在 tmp/home 下；tmp/F_wai 就是"外部原有图库"。"""
    from media_resource_manager.config import AppConfig
    home = tmp_path / "home"
    c = AppConfig()
    c.db_path = home / "mrm.db"
    c.library_dir = home / "library"
    c.isolate_dir = home / "quarantine"
    c.review_dir = home / "review"
    c.ai_model_path = ""
    c.ai_gpu = False
    return c


def _img(p, color=(200, 30, 30)):
    from PIL import Image
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), color).save(p, quality=90)
    return p


def _external_src(tmp_path):
    """模拟"原有图库"：数据目录之外的文件夹（相当于 F 盘）。
    三张图尺寸各不同 —— 同尺寸纯色 JPEG 编码可能字节一致被去重。"""
    from PIL import Image
    src = tmp_path / "F_wai" / "PIC"
    src.mkdir(parents=True, exist_ok=True)
    for i, (w, h) in enumerate([(64, 64), (80, 64), (64, 96)]):
        Image.new("RGB", (w, h), (200, 30 + i * 60, 30)).save(
            src / f"{i}.jpg", quality=90)
    return src


def test_copy_off_keeps_old_behavior(db, cfg, tmp_path):
    """关着（默认）：登记原地址，不复制。"""
    src = _external_src(tmp_path)
    r = imp.import_folder(db, cfg, str(src), do_tag=False)
    assert r["ok"], r
    assert r.get("copied", 0) == 0
    rows = db.query("SELECT path FROM images")
    assert all(str(x["path"]).startswith(str(src)) for x in rows), rows


def test_copy_on_registers_copy_and_leaves_original(db, cfg, tmp_path):
    """开着：账本登记软件图库里的副本；外部原图一张没动、地址不再入账。"""
    src = _external_src(tmp_path)
    orig_files = sorted(p.name for p in src.iterdir())
    r = imp.import_folder(db, cfg, str(src), do_tag=False,
                          copy_to_library=True)
    assert r["ok"], r
    assert r["copied"] == 3
    lib = str(cfg.library_dir)
    rows = db.query("SELECT path FROM images")
    assert len(rows) == 3
    for x in rows:
        assert str(x["path"]).startswith(lib), "登记的必须是副本地址"
    # 原文件夹完整保留（复制不是移动）
    assert sorted(p.name for p in src.iterdir()) == orig_files


def test_copy_on_then_delete_never_touches_original(db, cfg, tmp_path):
    """开启复制后走删除链路：只碰副本，外部原图安然无恙。"""
    from media_resource_manager.safe_delete import delete_image_file
    src = _external_src(tmp_path)
    r = imp.import_folder(db, cfg, str(src), do_tag=False,
                          copy_to_library=True)
    assert r["copied"] == 3
    rows = db.query("SELECT path FROM images")
    for x in rows:
        # 副本在软件目录 → 正常可处置；原文件不受影响
        res = delete_image_file(x["path"], cfg, recycle=True)
        assert res == "recycled", res
    assert all(p.exists() for p in src.iterdir()), "原图被动了！"


def test_learn_folder_copy_same_semantics(db, cfg, tmp_path):
    """桌面「从文件夹学习」的同一个勾：语义必须一致。"""
    src = _external_src(tmp_path)
    r = lf.learn_folder(db, cfg, str(src), copy_to_library=True)
    assert r.get("copied") == 3, r
    rows = db.query("SELECT path FROM images")
    assert all(str(x["path"]).startswith(str(cfg.library_dir)) for x in rows)
    assert all(p.exists() for p in src.iterdir())


def test_copy_dedupe_ledger_only(db, cfg, tmp_path):
    """同一批图重复导入：开着复制也不产生第二行（账本只记一条）。"""
    src = _external_src(tmp_path)
    r1 = imp.import_folder(db, cfg, str(src), do_tag=False,
                           copy_to_library=True)
    r2 = imp.import_folder(db, cfg, str(src), do_tag=False,
                           copy_to_library=True)
    assert r1["indexed"] == 3
    assert r2["indexed"] == 0 and r2["skipped_dup"] == 3, r2
    assert db.query("SELECT COUNT(*) c FROM images")[0]["c"] == 3


def test_zero_byte_never_registered_any_mode(db, cfg, tmp_path):
    """0 字节图：无论开关状态、无论哪条入口，都不登记（用户定稿）。"""
    src = _external_src(tmp_path)
    (src / "empty.jpg").write_bytes(b"")
    r = imp.import_folder(db, cfg, str(src), do_tag=False)
    assert r["indexed"] == 3 and r["errors"] == 1, r
    paths = [x["path"] for x in db.query("SELECT path FROM images")]
    assert not any(str(p).endswith("empty.jpg") for p in paths)
    # 复制模式同样不收（换一批不同内容的图，否则被 sha 去重跳过）
    src2 = tmp_path / "F_wai2"
    for j, (w, h) in enumerate([(70, 50), (90, 70)]):
        from PIL import Image
        src2.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (w, h), (10 + j * 90, 200, 60 + j * 30)).save(
            src2 / f"n{j}.jpg", quality=90)
    (src2 / "empty2.jpg").write_bytes(b"")
    r2 = imp.import_folder(db, cfg, str(src2), do_tag=False,
                           copy_to_library=True)
    assert r2["indexed"] == 2 and r2["copied"] == 2, r2
    paths2 = [x["path"] for x in db.query("SELECT path FROM images")]
    assert not any(str(p).endswith("empty2.jpg") for p in paths2)


def test_desktop_entry_has_copy_checkbox_source():
    """桌面导入小窗口要有这个勾（源码断言，打包后界面一定在）。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1]
           / "media_resource_manager" / "gui" / "main_window.py")
    text = src.read_text(encoding="utf-8")
    assert "同时复制一份进软件图库" in text
    assert "copy_to_library=_copy" in text


def test_web_entry_has_copy_checkbox_source():
    """网页导入区同样有这个勾，并把 copy 参数传给后端。

    V0.0.27 改判（不是放宽）：导入区从设置页搬到「导入与导出」页的
    `ImportFolder.vue`（V0.0.23 搬图库、V0.0.26 搬导出页），文件名与变量名
    随之变了；判据从 Settings.vue 的 impCopy 改成真实所在文件里的 copyIn。
    V0.0.35 再改判：作业状态搬到共享模块 importJob.js（切页不丢进度），
    勾选框仍在组件里、发请求在模块里，两处一起钉才拦得住真回归。
    """
    from pathlib import Path
    base = (Path(__file__).resolve().parents[1]
            / "media_resource_manager" / "webapp" / "src")
    text = (base / "components" / "ImportFolder.vue").read_text(encoding="utf-8")
    assert "imp.copyIn" in text and "复制一份进软件图库" in text
    job = (base / "importJob.js").read_text(encoding="utf-8")
    assert "copy: imp.copyIn" in job, "共享模块发请求时没带上复制开关"
