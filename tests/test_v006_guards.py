"""V0.0.6：把这一轮「找出来的真毛病」逐条钉住。

重点全是红线：用户原有图库（F 盘）里的文件，任何情况下都不许删 / 移 /
改名；其次是「填的词别白填」「填的黑名单真的拦得住」。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from media_resource_manager import import_folder as imp
from media_resource_manager import maintenance as mt
from media_resource_manager.find_terms import collect, search_entries
from media_resource_manager.safe_delete import (is_software_file,
                                                mark_external_root)


@pytest.fixture()
def home(tmp_path):
    d = tmp_path / "home"
    d.mkdir()
    return d


@pytest.fixture()
def db(home):
    from media_resource_manager.db import Database
    d = Database(home / "mrm.db")
    yield d
    d.close()


@pytest.fixture()
def cfg(home):
    from media_resource_manager.config import AppConfig
    c = AppConfig()
    c.db_path = home / "mrm.db"
    c.library_dir = home / "library"
    c.isolate_dir = home / "quarantine"
    c.review_dir = home / "review"
    c.ai_model_path = ""
    c.ai_gpu = False
    c.library_dir.mkdir(parents=True, exist_ok=True)
    return c


def _img(p: Path, color=(200, 30, 30), fmt="PNG"):
    from PIL import Image
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (48, 48), color).save(p, format=fmt)
    return p


def _add(db, path, **kw):
    from media_resource_manager.models import ImageRecord
    from media_resource_manager.repo import upsert_image
    rec = ImageRecord(id=None, source_url="file://" + str(path),
                      task_id=1, path=str(path), **kw)
    return upsert_image(db, rec)


# ---------- 红线：外部原图一个字节都不动 ----------

def test_convert_to_jpg_never_touches_external_original(db, cfg, tmp_path):
    """批量转 JPG 会删原文件、改扩展名 —— 外部原图必须整行跳过。"""
    ext = _img(tmp_path / "F_wai" / "outside.png")
    own = _img(cfg.library_dir / "inside.png")
    _add(db, ext)
    _add(db, own)

    rows = mt.convertible_rows(db, 0, cfg)
    assert [r["path"] for r in rows] == [str(own)], "外部原图不该出现在可转清单里"

    res = mt.convert_to_jpg(db, cfg)
    assert res["scanned"] == 1
    assert ext.exists(), "外部原图被删/改名了"
    assert ext.suffix == ".png", "外部原图扩展名被改了"
    assert (cfg.library_dir / "inside.jpg").exists(), "软件自己的图没转成 jpg"


def test_external_root_stays_protected_even_if_data_dir_is_inside_it(db, tmp_path):
    """数据目录恰好落在原图库里时，导入过的目录仍不许判定成「可删」。"""
    from media_resource_manager.config import AppConfig
    ext = tmp_path / "F_wai" / "PIC"
    _img(ext / "a.jpg", fmt="JPEG")
    c = AppConfig()
    c.db_path = ext / "mrm.db"           # 故意把数据目录放原图库里
    c.library_dir = ext / "library"
    c.isolate_dir = ext / "quarantine"
    c.review_dir = ext / "review"
    mark_external_root(c, ext)
    assert not is_software_file(ext / "a.jpg", c), \
        "导入过的原图库目录必须永远受保护"


def test_delete_gate_is_conservative_without_cfg(db, tmp_path, cfg):
    """少传配置时一律不动磁盘，不能悄悄退回「照删不误」。"""
    from media_resource_manager.models import ReviewDecision
    from media_resource_manager.repo import get_image
    from media_resource_manager.review.queue import ReviewQueue
    f = _img(tmp_path / "outside" / "a.jpg", fmt="JPEG")
    iid = _add(db, f, needs_review=True, in_quarantine=True)
    q = ReviewQueue(db, tmp_path / "quarantine")     # 故意不传 cfg
    q.apply(iid, ReviewDecision.DISLIKE)
    assert f.exists(), "cfg 缺省时把文件删了"
    assert get_image(db, iid) is not None


def test_review_no_longer_does_similarity_dedup(db, cfg):
    """点「喜欢」不该让一张图凭空消失：相似判重的整套代码已经删掉。"""
    from media_resource_manager.models import ReviewDecision
    from media_resource_manager.review.queue import ReviewQueue
    a = _img(cfg.library_dir / "a.jpg", fmt="JPEG")
    b = _img(cfg.library_dir / "b.jpg", fmt="JPEG")
    ia, ib = _add(db, a, needs_review=True, in_quarantine=True), _add(
        db, b, needs_review=True, in_quarantine=True)
    q = ReviewQueue(db, cfg.isolate_dir, cfg=cfg)
    assert not hasattr(q, "_gallery_dup_of")
    q.apply(ia, ReviewDecision.LIKE)
    q.apply(ib, ReviewDecision.LIKE)
    assert a.exists() and b.exists(), "审核通过把某张图搬走/删了"
    assert len(db.query("SELECT id FROM images")) == 2, "账本行被删了"


# ---------- 导入与回填：坏图不收、重导不翻倍 ----------

def test_backfill_never_deletes_zero_byte_file(db, cfg, tmp_path):
    """回填是只读补登记，0 字节文件留在原地、也不进账本。"""
    from media_resource_manager.engine import backfill_local_files
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task
    ext = tmp_path / "F_wai" / "PIC"
    _img(ext / "good.jpg", fmt="JPEG")
    (ext / "empty.jpg").write_bytes(b"")
    tid = save_task(db, TaskConfig(id=None, name="原有图库", adapter_name="",
                                   output_dir=str(ext), entry_urls=[]))
    backfill_local_files(db, int(tid))
    assert (ext / "empty.jpg").exists(), "回填把用户盘上的文件删了"
    paths = [r["path"] for r in db.query("SELECT path FROM images") or []]
    assert not any(str(p).endswith("empty.jpg") for p in paths if p), \
        "0 字节坏图不该登记"


def test_reimport_with_copy_does_not_create_second_row_or_copy(db, cfg, tmp_path):
    """开了复制开关、再强制导一次，还是一行一份副本。"""
    ext = tmp_path / "F_wai" / "PIC"
    for i in range(3):
        _img(ext / f"{i}.jpg", color=(30 + i * 60, 40, 90), fmt="JPEG")
    imp.import_folder(db, cfg, str(ext), copy_to_library=True, do_tag=False)
    first_rows = len(db.query("SELECT id FROM images"))
    first_copies = len(list((cfg.library_dir / "imported").rglob("*.jpg")))
    imp.import_folder(db, cfg, str(ext), copy_to_library=True,
                      do_tag=False, force=True)
    assert len(db.query("SELECT id FROM images")) == first_rows, "重导插了新行"
    assert len(list((cfg.library_dir / "imported").rglob("*.jpg"))) \
        == first_copies, "重导又造了一批副本"


# ---------- 词真的生效 ----------

def test_keyword_blocked_no_longer_drops_whole_site(db, cfg):
    """中文主题词不会出现在图片网址里，不能因此把整站图全判掉。"""
    from media_resource_manager.engine import Engine
    from media_resource_manager.models import TaskConfig
    t = TaskConfig(id=None, name="t", adapter_name="", entry_urls=[], keywords=["中文主题词"])
    eng = Engine(cfg, db)
    assert eng._keyword_blocked(t, "https://site.example/12345.jpg") is False


def test_blacklist_regex_is_case_insensitive():
    from media_resource_manager.engine import _hit_blacklist
    assert _hit_blacklist("https://x/SlamDunk/1.jpg", [], ["/slamdunk/"])
    assert _hit_blacklist("https://x/Doraemon_1.jpg", [], ["Doraemon*"])


def test_long_blacklist_string_is_split_and_excluded_from_search(db, cfg):
    from media_resource_manager.models import TaskConfig
    long_blob = "、".join([f"不要词{i}" for i in range(20)])
    t = TaskConfig(id=None, name="t", adapter_name="", entry_urls=[],
                   blacklist=[long_blob], ai_labels=["猫"])
    terms = collect(db, t, cfg)
    assert "不要词0" in terms.avoid and "不要词19" in terms.avoid, \
        "整串黑名单被丢掉了"
    entries, note = search_entries(["https://danbooru.donmai.us/"],
                                   ["cat"], avoid=["dog"])
    assert "-dog" in entries[0], "别找什么没拼进站内找图条件"


def test_count_pending_matches_list(db, cfg):
    """网页移进来的待审核图，桌面标题数字也要算上。"""
    from media_resource_manager.review.queue import ReviewQueue
    a = _img(cfg.library_dir / "a.jpg", fmt="JPEG")
    b = _img(cfg.library_dir / "b.jpg", fmt="JPEG")
    _add(db, a, needs_review=True, in_quarantine=0)
    _add(db, b, needs_review=True, in_quarantine=1)
    q = ReviewQueue(db, cfg.isolate_dir, cfg=cfg)
    assert q.count_pending() == 2
    assert len(q.pending()) == 2


def test_web_bool_parsing_does_not_treat_false_string_as_true():
    from media_resource_manager.webui import _as_bool
    assert _as_bool("false") is False
    assert _as_bool("FALSE") is False
    assert _as_bool("true") is True
    assert _as_bool(None, True) is True
    assert _as_bool("说不清", False) is False


# ---------- 配置坏了不许悄悄清空 ----------

def test_task_form_shows_the_word_list_on_both_sides():
    """建任务表单最上面要能看见词表，桌面和网页都得有。"""
    import inspect
    from media_resource_manager.gui import main_window as MW
    from media_resource_manager import webui
    assert "_lbl_task_terms" in inspect.getsource(
        MW.MainWindow._build_task_form_panel)
    assert "def _refresh_task_terms_label" in inspect.getsource(MW.MainWindow)
    backend = Path(webui.__file__).read_text(encoding="utf-8")
    assert '"/api/find-terms"' in backend and "def _api_find_terms" in backend
    front = (Path(webui.__file__).parent / "webapp" / "src" / "views"
             / "Tasks.vue").read_text(encoding="utf-8")
    assert "termsHint" in front and "loadFindTerms" in front


def test_broken_config_is_backed_up_before_self_heal(tmp_path, monkeypatch):
    import media_resource_manager.config as C
    monkeypatch.setattr(C, "migrate_from_legacy", lambda: None)
    p = tmp_path / "config.json"
    broken = "{ 这不是 json"
    p.write_text(broken, encoding="utf-8")
    C.load_config(p)
    baks = list(tmp_path.glob("config.json.broken-*.bak"))
    assert baks, "坏配置没留备份就被覆盖了"
    assert baks[0].read_text(encoding="utf-8") == broken
