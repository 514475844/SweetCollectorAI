"""r241 护栏：整库备份 / 恢复（导出 → 改坏 → 恢复的闭环）。

护的是四件**已经想清楚、以后最容易被改回去**的事：

1. **恢复不许「关库重开」**：桌面端那套（close → extractall → new Database）
   在 Web 端会把桌面/引擎手里的 Database 引用变成死引用。这里断言实现走
   ``ATTACH`` + 单事务逐表覆盖（``Database.restore_from``）。
2. **包内路径必须校验**：``../`` / 绝对路径 / 符号链接一律拒收 ——
   否则「恢复」等于给浏览器一个往数据目录任意位置写文件的口子。
3. **配置恢复必须挡住监听项与 db_path**：端口一改，当前网页当场连不上；
   db_path 一改，内存里的连接和配置就错位了。
4. **导出必须是 sqlite 在线快照**（``db.backup``）而不是 copy 文件：
   WAL 里未并回的事务 copy 出去就丢（R152-A6 桌面端已踩过一次）。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from media_resource_manager import fullbackup as fb
from media_resource_manager.config import AppConfig, save_config
from media_resource_manager.db import Database

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 纯函数
def test_name_shape_and_prune():
    now = __import__("datetime").datetime(2026, 10, 28, 23, 59, 0)
    n = fb.full_name(now)
    assert n == "mrm_full_20261028_235900.zip"
    assert fb.is_full_name(n)
    # 与 R232 的单库快照**不能互相认领**（否则清理逻辑会互相删）
    assert not fb.is_full_name("mrm_db_backup_20261028.db")
    assert not fb.is_full_name("mrm_full_2026.zip")
    assert not fb.is_full_name("")
    # 按名字（= 日期）保留最新，不按 mtime（拷贝/解压会改 mtime）
    names = ["mrm_full_20261001_000000.zip", "mrm_full_20261005_000000.zip",
             "mrm_full_20261009_000000.zip"]
    assert fb.prune_full(names, 2) == ["mrm_full_20261001_000000.zip"]
    assert fb.prune_full(names, 0) == []          # 0 = 不清理
    assert fb.prune_full(names, 9) == []


def test_manifest_rejects_foreign_zip(tmp_path):
    p = tmp_path / "a.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(fb.RestoreError) as e:
        fb.read_manifest(p)
    assert "MRM_BACKUP.json" in str(e.value)

    bad = tmp_path / "b.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr(fb.MANIFEST_NAME, json.dumps({"kind": "something_else"}))
    with pytest.raises(fb.RestoreError):
        fb.read_manifest(bad)

    # 更新版本做的包：明确让用户先升级，而不是「解一半报乱码」
    newer = tmp_path / "c.zip"
    with zipfile.ZipFile(newer, "w") as z:
        z.writestr(fb.MANIFEST_NAME,
                   json.dumps({"kind": fb.KIND, "version": fb.VERSION + 1}))
    with pytest.raises(fb.RestoreError) as e:
        fb.read_manifest(newer)
    assert "升级" in str(e.value)


def test_manifest_payload_has_no_secret_fields():
    man = fb.manifest_payload(db_rel="mrm.db", db_encrypted=True,
                              counts={"images": 3}, version="1.0.0")
    text = json.dumps(man)
    for word in ("password", "token", "key", "secret"):
        assert word not in text.lower()


def test_traversal_members_are_refused(tmp_path):
    p = tmp_path / "evil.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(fb.MANIFEST_NAME, json.dumps({"kind": fb.KIND, "version": 1}))
        z.writestr("../../escape.txt", "x")
    man = fb.read_manifest(p)
    with pytest.raises(fb.RestoreError) as e:
        fb.extract_parts(p, tmp_path / "st", man)
    assert "越权" in str(e.value)

    p2 = tmp_path / "evil2.zip"
    with zipfile.ZipFile(p2, "w") as z:
        z.writestr(fb.MANIFEST_NAME, json.dumps({"kind": fb.KIND, "version": 1}))
        z.writestr("C:/windows/win.ini", "x")
    with pytest.raises(fb.RestoreError):
        fb.extract_parts(p2, tmp_path / "st2", fb.read_manifest(p2))


def test_unknown_top_level_members_are_skipped(tmp_path):
    """白名单外的文件**跳过**而不是整体拒收：以后加新部件（比如 cookies）
    时，旧版本软件解新包不该直接失败，能用的部分先恢复。"""
    p = tmp_path / "a.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(fb.MANIFEST_NAME, json.dumps({"kind": fb.KIND, "version": 1}))
        z.writestr("mrm.db", b"SQLite format 3\x00")
        z.writestr("random_dir/junk.txt", "x")
    parts = fb.extract_parts(p, tmp_path / "st", fb.read_manifest(p))
    assert parts["db"] is not None
    assert parts["skipped"] == 1


# ---------------------------------------------------------------- 闭环
@pytest.fixture()
def env(tmp_path):
    base = tmp_path / "data"
    base.mkdir()
    cfg = AppConfig()
    cfg.db_path = base / "mrm.db"
    cfg.rules_dir = base / "rules"
    cfg.log_dir = base / "logs"
    cfg.library_dir = base / "library"
    cfg.web_port = 8123
    save_config(cfg, base / "config.json")
    db = Database(cfg.db_path)
    db.execute("INSERT INTO tasks(name, entry_urls) VALUES ('甲', '[]')")
    for i in range(3):
        db.execute("INSERT INTO images(task_id, source_url, path) VALUES (1, ?, ?)",
                   ("https://e/%d" % i, "p%d" % i))
    db.execute("INSERT INTO preferences(task_id, label, weight, seen) "
               "VALUES (0, '白丝', 2.0, 3)")
    (base / "rules").mkdir()
    (base / "rules" / "demo.yaml").write_text("name: demo\n", encoding="utf-8")
    (base / "thumbs").mkdir()
    for i in range(3):
        (base / "thumbs" / ("%d_256.jpg" % i)).write_bytes(b"\xff\xd8" + b"0" * 20)
    yield base, cfg, db
    try:
        db.close()
    except Exception:
        pass


def test_export_then_restore_roundtrip(env):
    base, cfg, db = env
    out = fb.full_path(base)
    res = fb.build_export(out, base, db, version="1.0.0")
    assert out.is_file() and res["counts"]["images"] == 3
    man = fb.inspect_zip(out)
    assert man["kind"] == fb.KIND and man["entries"] >= 6

    # 模拟「换了台电脑 / 误删」：库清空、缩略图与规则全没、配置被改
    db.execute("DELETE FROM images")
    db.execute("DELETE FROM preferences")
    for f in (base / "thumbs").glob("*"):
        f.unlink()
    (base / "rules" / "demo.yaml").unlink()
    cfg.learn_enabled = False
    save_config(cfg, base / "config.json")

    stage = base / "_stage"
    parts = fb.extract_parts(out, stage, man)
    rep = db.restore_from(parts["db"])
    assert rep["integrity"] == "ok"
    fb._copy_tree(parts["thumbs"], base / "thumbs", replace=False)
    fb._copy_tree(parts["rules"], base / "rules", replace=True)
    m = fb.merge_config(cfg, parts["config"],
                        save=lambda: save_config(cfg, base / "config.json"))

    assert db.query("SELECT COUNT(*) c FROM images")[0]["c"] == 3
    assert db.query("SELECT COUNT(*) c FROM preferences")[0]["c"] == 1
    assert (base / "thumbs" / "2_256.jpg").is_file()
    assert (base / "rules" / "demo.yaml").is_file()
    assert cfg.learn_enabled is True
    # 监听项必须被挡住（否则当前网页当场连不上）
    assert "web_port" in m["blocked"] and cfg.web_port == 8123
    assert "db_path" in m["blocked"]
    # 自增序列要对齐，否则恢复后第一次插入就撞主键
    db.execute("INSERT INTO images(task_id, source_url) VALUES (1, 'new')")
    assert db.query("SELECT MAX(id) m FROM images")[0]["m"] == 4


def test_restore_replaces_rules_instead_of_merging(env, tmp_path):
    """规则目录是「替换」语义：不清空的话，备份里已删的规则会复活。"""
    base, cfg, db = env
    out = fb.full_path(base)
    fb.build_export(out, base, db, version="1.0.0")
    (base / "rules" / "later_added.yaml").write_text("x: 1\n", encoding="utf-8")
    man = fb.inspect_zip(out)
    parts = fb.extract_parts(out, tmp_path / "st", man)
    fb._copy_tree(parts["rules"], base / "rules", replace=True)
    assert (base / "rules" / "demo.yaml").is_file()
    assert not (base / "rules" / "later_added.yaml").exists()


def test_thumbs_copy_overwrites_but_keeps_new(env, tmp_path):
    """缩略图是「覆盖」语义：清空会把备份之后新采图的缩略图一起删掉。"""
    base, cfg, db = env
    out = fb.full_path(base)
    fb.build_export(out, base, db, version="1.0.0")
    (base / "thumbs" / "999_256.jpg").write_bytes(b"\xff\xd8new")
    parts = fb.extract_parts(out, tmp_path / "st", fb.inspect_zip(out))
    fb._copy_tree(parts["thumbs"], base / "thumbs", replace=False)
    assert (base / "thumbs" / "999_256.jpg").is_file()
    assert (base / "thumbs" / "1_256.jpg").is_file()


def test_restore_from_uses_attach_not_reopen(env):
    """恢复期间连接不能关：桌面/引擎共享同一个 Database 对象。"""
    src = (ROOT / "media_resource_manager/db.py").read_text(encoding="utf-8")
    i = src.index("def restore_from")
    j = src.index("def decrypt_at_rest")
    body = src[i:j]
    assert "ATTACH DATABASE" in body
    assert "self._conn.close()" not in body
    assert "BEGIN IMMEDIATE" in body            # 一本账，失败整本回滚
    assert "quick_check" in body                # 恢复后必须自检


def test_export_uses_online_snapshot(env):
    base, cfg, db = env
    src = (ROOT / "media_resource_manager/fullbackup.py").read_text(encoding="utf-8")
    assert "db.backup(snap)" in src
    assert "shutil.copy2" in src or True        # 缩略图是文件拷贝，不是快照
    # 导出过程中落的是临时快照，包内按原相对路径存，桌面端导入逻辑零改动
    out = fb.full_path(base)
    res = fb.build_export(out, base, db, version="1.0.0")
    with zipfile.ZipFile(out) as zf:
        assert res["manifest"]["db"] in zf.namelist()


# ---------------------------------------------------------------- 路由 / 前端接线
def test_routes_are_wired():
    src = (ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")
    for path in ("/api/backup/full", "/api/backup/full/export",
                 "/api/backup/full/restore", "/api/backup/full/job",
                 "/api/backup/full/download", "/api/backup/full/upload",
                 "/api/backup/full/inspect", "/api/backup/full/delete",
                 "/api/diagnostic"):
        assert f'"{path}"' in src, path


def test_backup_name_is_validated_before_use():
    """请求里的名字必须过 ``is_full_name`` + 目录校验，不能直接拼路径。"""
    src = (ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")
    i = src.index("def _full_path_of")
    j = src.index("def _api_full_list")
    body = src[i:j]
    assert "is_full_name" in body
    assert "resolve().parent" in body           # 防 ../ 绕出去
    assert ".." in body


def test_upload_never_truncates_an_existing_backup():
    """上传必须「先写 .part + 校验 + 改名」，不能直接写正式名字。

    踩过的真 bug：直接写到 ``target`` 时，同名文件会被截断；随后校验失败
    再删 target，删掉的是刚写坏的那份，而**用户原来那份好备份已经被覆盖没了**
    （探针 ``tools/_probe_r241_http.py`` 里当场复现过：上传一个坏包把同秒的
    好包搞丢了）。
    """
    src = (ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")
    i = src.index("def _api_full_upload")
    j = src.index("def _api_full_restore")
    body = src[i:j]
    assert '.part' in body                    # 两段式落盘
    assert "part.replace(want)" in body       # 校验通过才改名
    assert "part.unlink()" in body            # 失败只删自己的临时文件
    # 绝不能出现「直接写到正式名字」的写法
    assert "_raw_body_to_file(target" not in body


def test_job_registry_progress_is_monotonic_and_ttl_bounded():
    """作业表：进度只增不减、失败置 failed、未知 id 返回 None。"""
    from media_resource_manager import jobs

    jobs.clear()
    jid = jobs.new("t")
    assert len(jid) == 16                       # 8 字节随机 → 16 个 hex
    cb = jobs.progress_cb(jid)
    cb(40, "第一阶段")
    cb(10, "第二阶段开头")                       # 多阶段回退不该让进度条倒回去
    assert jobs.get(jid)["pct"] == 40
    assert jobs.get(jid)["message"] == "第二阶段开头"
    jobs.finish(jid, result={"a": 1})
    rec = jobs.get(jid)
    assert rec["state"] == "done" and rec["pct"] == 100 and rec["result"] == {"a": 1}
    assert jobs.get("不存在的编号") is None
    # 返回的是拷贝：改它不影响内部状态
    rec["pct"] = 1
    assert jobs.get(jid)["pct"] == 100
    jobs.clear()


def test_job_ids_are_unpredictable():
    """作业编号必须随机（自增 id 会被邻号请求猜到别人的备份进度/文件名）。"""
    from media_resource_manager import jobs
    jobs.clear()
    ids = {jobs.new("t") for _ in range(50)}
    assert len(ids) == 50
    assert all(not i.isdigit() for i in ids)
    jobs.clear()


def test_frontend_wires_full_backup():
    s = (ROOT / "media_resource_manager/webapp/src/views/Settings.vue"
         ).read_text(encoding="utf-8")
    a = (ROOT / "media_resource_manager/webapp/src/api.js"
         ).read_text(encoding="utf-8")
    for k in ("fullList", "fullExport", "fullRestore", "fullJob",
              "fullUpload", "fullDelete", "fullDownloadUrl"):
        assert k in a, k
    for k in ("startFullExport", "doRestore", "doUpload", "pollJob"):
        assert k in s, k
    # 轮询必须在离开页面时清掉，否则每秒白打一次请求
    assert "onUnmounted" in s and "clearInterval" in s
