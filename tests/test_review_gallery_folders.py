"""L：审核与图库物理分开文件夹。

- 下载后判定『待审核』(in_quarantine & needs_review) 的图，落盘时移入
  review_dir/<task_id>/，与图库 output_dir 彻底是两个文件夹；
- 审核批准(LIKE/SKIP)：文件从 review_dir 移回图库 output_dir（按域名放回
  output_dir/<site>/，保留『一个网站一个文件夹』约定）；
- 不喜欢(DISLIKE/BLACKLIST)：文件移入 isolate_dir（回收站，可还原）。
"""
from pathlib import Path

from media_resource_manager.db import Database
from media_resource_manager.config import AppConfig
from media_resource_manager.models import ImageRecord, ReviewDecision
from media_resource_manager.repo import upsert_image, get_image
from media_resource_manager.review.queue import ReviewQueue
from media_resource_manager.engine import Engine


def _mk_file(d, name):
    p = d / name
    p.write_bytes(b"x" * 100)
    return p


def test_stage_to_review_moves_file_out_of_output_dir(tmp_config):
    db = Database(tmp_config.db_path)
    eng = Engine(tmp_config, db)
    out = tmp_config.library_dir / "site"
    out.mkdir(parents=True, exist_ok=True)
    f = _mk_file(out, "a.jpg")
    rec = ImageRecord(id=None, source_url="https://site.com/a.jpg", task_id=1,
                      path=str(f), in_quarantine=True, needs_review=True)
    eng._stage_to_review(rec)
    assert not f.exists(), "待审核图应从下载目录移走"
    target = tmp_config.review_dir / "1" / "a.jpg"
    assert target.exists(), "待审核图应落在 review_dir/<task_id>/"
    assert rec.path == str(target)
    # 图库目录（output_dir）下不应出现这张待审核图
    assert not (out / "a.jpg").exists()


def test_review_like_releases_back_to_output_dir(tmp_config):
    db = Database(tmp_config.db_path)
    out = tmp_config.library_dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    db.execute(
        "INSERT INTO tasks(id,name,adapter_name,entry_urls,output_dir,status) "
        "VALUES(?,?,?,?,?,?)",
        (1, "t", "x", "https://site.com", str(out), "idle"))
    rev = tmp_config.review_dir / "1"
    rev.mkdir(parents=True, exist_ok=True)
    f = _mk_file(rev, "a.jpg")
    rec = ImageRecord(id=None, source_url="https://site.com/a.jpg", task_id=1,
                      path=str(f), in_quarantine=True, needs_review=True)
    iid = upsert_image(db, rec)
    q = ReviewQueue(db, tmp_config.isolate_dir, tmp_config)
    q.apply(iid, ReviewDecision.LIKE)
    r = get_image(db, iid)
    assert not f.exists(), "批准后原 review 文件应被移走"
    assert r.in_quarantine is False and r.blacklisted is False
    assert Path(r.path).parent.name == "site.com", "应放回 output_dir/<域名>/"
    assert Path(r.path).exists(), "批准后的图应在图库目录里"


def test_review_dislike_is_a_delete_following_the_recycle_switch(tmp_config):
    """r33 定版：『不喜欢』= 删除这张图，删法跟随「删除时先进回收站」开关。

    开 → 文件进隔离区(回收站)、行保留（可还原）；关 → 文件+行一起直删。
    """
    db = Database(tmp_config.db_path)
    rev = tmp_config.review_dir / "1"
    rev.mkdir(parents=True, exist_ok=True)
    # 两分支各造一条，互不影响
    results = {}
    for i, use_recycle in enumerate((True, False)):
        db2 = Database(tmp_config.db_path)
        tmp_config.use_recycle = use_recycle
        f = _mk_file(rev, f"b{i}.jpg")
        rec = ImageRecord(id=None, source_url=f"https://site.com/b{i}.jpg",
                          task_id=1, path=str(f),
                          in_quarantine=True, needs_review=True)
        iid = upsert_image(db2, rec)
        ReviewQueue(db2, tmp_config.isolate_dir, tmp_config).apply(
            iid, ReviewDecision.DISLIKE)
        results[use_recycle] = (iid, f, get_image(db2, iid))

    # 1) 回收站开关开：进隔离区，行还在
    iid, f, r = results[True]
    assert not f.exists(), "原文件应被搬进回收站"
    assert r is not None, "开关开着时行要保留（可还原）"
    assert r.in_quarantine is True
    assert r.needs_review == 0
    assert Path(r.path).parent == tmp_config.isolate_dir
    assert Path(r.path).exists()

    # 2) 回收站开关关：文件+行一起没了
    iid2, f2, r2 = results[False]
    assert not f2.exists(), "开关关着时应从磁盘直删"
    assert r2 is None, "开关关着时行要一起删掉"


def test_keep_images_stay_in_output_dir(tmp_config):
    """非待审核(直接入库)的图不应被移进 review_dir。"""
    db = Database(tmp_config.db_path)
    eng = Engine(tmp_config, db)
    out = tmp_config.library_dir / "site"
    out.mkdir(parents=True, exist_ok=True)
    f = _mk_file(out, "keep.jpg")
    rec = ImageRecord(id=None, source_url="https://site.com/keep.jpg", task_id=1,
                      path=str(f))  # 默认 in_quarantine=False
    eng._stage_to_review(rec)
    assert f.exists(), "已入库(非待审核)的图应留在原下载目录"
    assert rec.path == str(f)
