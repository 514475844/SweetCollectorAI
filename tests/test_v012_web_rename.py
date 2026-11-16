"""V0.0.12 护栏：批量重命名抽成两端共享实现 + 网页入口。

钉三件事：
  1. 红线：用户原有图库（external_library_roots，如 F 盘）的文件**一律不改名**，
     跳过且计数回报——不许静默吞掉（用户会以为全改成功了）；
  2. 磁盘改名与 images.path 更新必须同一实现（os.replace 成功才 UPDATE，
     反序在改名失败时留死记录）；桌面转调共享模块，网页不许另写一份；
  3. 模板序号只给「真会改名」的图编号（原有图库跳过不占号）。
"""
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_OPS = (_ROOT / "media_resource_manager/rename_ops.py").read_text(encoding="utf-8")
_MW = (_ROOT / "media_resource_manager/gui/main_window.py").read_text(
    encoding="utf-8")
_WEB = (_ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")


@pytest.fixture()
def env(tmp_path):
    import os
    os.environ["MRM_HOME"] = str(tmp_path)
    from media_resource_manager.db import Database
    from media_resource_manager.config import AppConfig
    from media_resource_manager.repo import upsert_image, save_task
    from media_resource_manager.models import (ImageRecord, TaskConfig,
                                                TaskStatus)
    from media_resource_manager.safe_delete import mark_external_root
    cfg = AppConfig(db_path=tmp_path/'m.db', library_dir=tmp_path/'lib',
                    rules_dir=tmp_path/'r', isolate_dir=tmp_path/'q',
                    log_dir=tmp_path/'l')
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    t = TaskConfig(id=None, name="美图", adapter_name="",
                   entry_urls=["https://konachan.com/post"])
    t.status = TaskStatus.IDLE
    tid = save_task(db, t)
    ext = tmp_path/'F盘'
    ext.mkdir()
    mark_external_root(cfg, ext)

    def add(dirp, name, url):
        p = dirp / name
        p.write_bytes(b"x" * 10)
        return upsert_image(db, ImageRecord(id=None, source_url=url,
                                            task_id=tid, path=str(p),
                                            tags=["猫"]))
    ids = [add(tmp_path/'lib', "a.jpg", "https://k/1"),
           add(tmp_path/'lib', "b.jpg", "https://k/2")]
    eid = add(ext, "keep.jpg", "https://k/3")
    yield db, cfg, ids, eid, ext
    db.close()


def _path_of(db, iid):
    return Path(db.query("SELECT path FROM images WHERE id=?", (iid,))[0]["path"])


def test_prefix_skips_external(env):
    from media_resource_manager import rename_ops
    db, cfg, ids, eid, ext = env
    r = rename_ops.rename_by_prefix(db, cfg, ids + [eid], "精选_")
    assert r["renamed"] == 2
    assert r["skipped_external"] == 1, "原有图库必须跳过并计数回报"
    assert (ext / "keep.jpg").exists(), "红线：原有图库文件被改名了"
    assert _path_of(db, eid).name == "keep.jpg"
    assert _path_of(db, ids[0]).name == "精选_a.jpg"
    assert _path_of(db, ids[0]).exists(), "磁盘改名与 path 更新必须同步"


def test_template_numbering_skips_external(env):
    from media_resource_manager import rename_ops
    db, cfg, ids, eid, ext = env
    r = rename_ops.rename_by_template(db, cfg, [eid] + ids,
                                      "x_{n}_{orig}")
    assert r["renamed"] == 2 and r["skipped_external"] == 1
    # 跳过不占号：两张真改名的编号必须 001、002 连号
    names = sorted(_path_of(db, i).name for i in ids)
    assert any("001" in n for n in names) and any("002" in n for n in names), names
    assert not any("003" in n for n in names), "原有图库占号导致断档"


def test_template_requires_placeholder(env):
    from media_resource_manager import rename_ops
    db, cfg, ids, eid, ext = env
    r = rename_ops.rename_by_template(db, cfg, ids, "纯文字没花括号")
    assert r.get("error") and "占位符" in r["error"]


def test_desktop_delegates_to_shared_ops():
    # 桌面两个菜单函数不许再自己写循环（两份实现早晚分叉）
    seg1 = _MW[_MW.index("def batch_rename_selected"):]
    seg1 = seg1[:seg1.index("def gallery_rename_by_template")]
    seg2 = _MW[_MW.index("def gallery_rename_by_template"):]
    seg2 = seg2[:seg2.index("\n    def ", 40)]
    assert "rename_by_prefix" in seg1 and "is_software_file" not in seg1
    assert "rename_by_template" in seg2 and "os.replace" not in seg2


def test_web_route_and_no_second_impl():
    assert 'path == "/api/image/rename"' in _WEB
    seg = _WEB[_WEB.index("def _api_image_rename"):]
    seg = seg[:seg.index("def _api_review_sample")]
    assert "rename_ops.rename_by_prefix" in seg
    assert "rename_ops.rename_by_template" in seg
    assert "os.replace" not in seg, "网页不许另写改名循环——必须走共享实现"
