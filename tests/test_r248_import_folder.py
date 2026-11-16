"""R248：从文件夹批量导入（两阶段、可停可续、限核、坏文件不静默）。

这些用例**不加载真模型**：打标那一层换成一个假分类器，只验流程、阈值、
黑名单、断点与去重。真模型的精度与速度由 `tools/_probe_r248_import.py`
在真实图片上验（探针 20 张约 4 秒）。
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _img(path: Path, w: int = 64, h: int = 48, color=None):
    """造一张图。**默认颜色按路径变化** —— 否则一批图内容相同，
    会被 sha256 去重成一张，测出来的行数全错（第一版就这么踩了）。"""
    from PIL import Image
    if color is None:
        n = abs(hash(str(path)))
        color = (60 + n % 60, 40 + (n // 7) % 60, 40 + (n // 11) % 60)
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
    c.ai_model_path = ""
    c.ai_labels = ["person", "sky"]
    c.ai_threshold = 0.5
    c.ai_gpu = False
    return c


@pytest.fixture()
def fake_clf(monkeypatch):
    """假分类器：person 恒 0.9、sky 恒 0.2 —— 阈值 0.5 下只有 person 该命中。"""
    from media_resource_manager.classify import clip_model

    class _Fake:
        backend = "clip"
        model_note = ""
        calls = 0
        last = None

        def __init__(self, labels=None, model_dir=None, threads=0, gpu=True):
            self.labels = list(labels or [])
            self.threads = threads
            _Fake.last = self

        def classify(self, path):
            _Fake.calls += 1
            return [("person", 0.9), ("sky", 0.2)]

    monkeypatch.setattr(clip_model, "LocalClassifier", _Fake)
    return _Fake


# ───────────────────────── 索引阶段 ─────────────────────────
def test_index_only_puts_rows_in_library(db, cfg, tmp_path):
    from media_resource_manager import import_folder as imp

    lib = tmp_path / "lib"
    for i in range(5):
        _img(lib / f"{i}.jpg")
    r = imp.import_folder(db, cfg, str(lib), do_tag=False)
    assert r["ok"] and r["indexed"] == 5
    assert r["tagged"] == 0 and r["pending"] == 0
    assert "只做索引" in r["ai_note"], "只索引时没说明「标签为空是正常的」"
    rows = db.query("SELECT ai_state, width, height FROM images")
    assert len(rows) == 5
    assert all(x["ai_state"] == 1 for x in rows), "只索引时应标记为已处理，不留待办"
    assert all(x["width"] == 64 and x["height"] == 48 for x in rows), "宽高没读出来"


def test_hidden_and_system_dirs_are_skipped(db, cfg, tmp_path):
    """`.git` / `@eaDir`（群晖缩略图）里的同名图不该污染图库。"""
    from media_resource_manager import import_folder as imp

    lib = tmp_path / "lib"
    _img(lib / "keep.jpg")
    _img(lib / ".git" / "x.jpg")
    _img(lib / "@eaDir" / "y.jpg")
    r = imp.import_folder(db, cfg, str(lib), do_tag=False)
    assert r["indexed"] == 1, f"隐藏目录没跳过：indexed={r['indexed']}"
    names = {Path(x["path"]).name for x in db.query("SELECT path FROM images")}
    assert names == {"keep.jpg"}


def test_rescan_same_folder_is_idempotent(db, cfg, tmp_path):
    from media_resource_manager import import_folder as imp

    lib = tmp_path / "lib"
    for i in range(4):
        _img(lib / f"{i}.jpg")
    imp.import_folder(db, cfg, str(lib), do_tag=False)
    r2 = imp.import_folder(db, cfg, str(lib), do_tag=False)
    assert r2["indexed"] == 0 and r2["skipped_dup"] == 4
    assert db.query("SELECT COUNT(*) c FROM images")[0]["c"] == 4


def test_same_content_different_path_is_deduped(db, cfg, tmp_path):
    """同一张图放在两个目录：按 sha256 只留一行（否则图库翻倍）。"""
    from media_resource_manager import import_folder as imp

    lib = tmp_path / "lib"
    _img(lib / "a" / "one.jpg", color=(10, 200, 10))
    (_img(lib / "b" / "two.jpg", color=(10, 200, 10)))
    r = imp.import_folder(db, cfg, str(lib), do_tag=False)
    assert r["indexed"] == 1 and r["skipped_dup"] == 1
    assert db.query("SELECT COUNT(*) c FROM images")[0]["c"] == 1


def test_broken_file_is_counted_and_reported(db, cfg, tmp_path):
    """V0.0.5 用户定稿：0 字节 / 解不开的坏图**一律不收、不登记**，
    只在统计里报出来（errors + broken 清单），图库里不许出现空白图。"""
    from media_resource_manager import import_folder as imp

    lib = tmp_path / "lib"
    _img(lib / "ok.jpg")
    (lib / "zero.jpg").write_bytes(b"")
    r = imp.import_folder(db, cfg, str(lib), do_tag=False)
    assert r["indexed"] == 1, "坏图不该入库"
    assert r["errors"] == 1
    assert any(p.endswith("zero.jpg") for p in r["broken"]), r["broken"]
    assert db.query("SELECT COUNT(*) c FROM images")[0]["c"] == 1


# ───────────────────────── 打标阶段 ─────────────────────────
def test_tagging_applies_threshold_but_never_filters_own_library(
        db, cfg, tmp_path, fake_clf):
    """导入本地图库只打标签、学口味，绝不拿排除词筛掉用户自己的图。

    V0.0.3 起用户把产品逻辑讲死了：「导入我原有的图库 = 教机器我喜欢什么」，
    不是「机器先有口味再来筛我的图」。所以导入阶段黑名单恒为空，
    哪怕任务里填了 ai_blacklist，自己的图也不许被拉黑或进隔离区。
    排除词只在**去网站找新图**那条链路上生效。
    """
    from media_resource_manager import import_folder as imp
    from media_resource_manager.repo import get_task

    lib = tmp_path / "lib"
    for i in range(3):
        _img(lib / f"{i}.jpg")
    r = imp.import_folder(db, cfg, str(lib))
    assert r["tagged"] == 3 and r["pending"] == 0
    t = get_task(db, r["task_id"])
    db.execute("UPDATE tasks SET ai_blacklist=? WHERE id=?",
               (json.dumps(["person"]), r["task_id"]))
    db.execute("UPDATE images SET ai_state=0")
    r2 = imp.import_folder(db, cfg, str(lib), force=True)
    assert r2["tagged"] == 3
    rows = db.query("SELECT tags, score, blacklisted, in_quarantine FROM images")
    for x in rows:
        tags = json.loads(x["tags"])
        assert "person" in tags and "sky" not in tags, f"阈值 0.5 下有 sky：{tags}"
        assert abs(float(x["score"]) - 0.9) < 1e-6
        assert x["blacklisted"] == 0 and x["in_quarantine"] == 0, \
            "导入的是用户自己的图，不该被黑名单筛掉"


def test_stop_midway_then_resume(db, cfg, tmp_path, fake_clf):
    """打标停一半 → 结果标 stopped、剩下的进待办 → 再跑一次打完。

    V0.0.22 起打标改成「并行推理 + 每批一次写回」，进度也按批上报，
    所以停止的落点从「每张之间」变成「每批之间」——同一个契约（能停、
    剩下的进待办、再跑能续上）不变，只是粒度变粗（一批最多 50 张）。
    """
    from media_resource_manager import import_folder as imp

    lib = tmp_path / "lib"
    for i in range(160):
        _img(lib / f"{i}.jpg", w=16 + i % 30, h=16 + i // 30)
    imp.import_folder(db, cfg, str(lib), do_tag=False)

    cur = {"phase": ""}

    def _prog(phase, *a):
        cur["phase"] = phase

    def _stop():
        # 第一批打标上报进度之后立刻喊停 = 只允许跑完当前这一批
        return cur["phase"] == "tag"

    # ⚠ 必须 force=True：do_tag=False 那一轮已经把行标成「已处理」，
    #   不带 force 再跑就是「待打标 0 张」，根本进不了打标阶段（第一版这么踩的）。
    r = imp.import_folder(db, cfg, str(lib), force=True,
                          stop=_stop, progress=_prog)
    assert r["stopped"] is True, "打标阶段被停却没标成已停止（界面会显示成完成）"
    assert 0 < r["tagged"] < 160
    assert r["pending"] == 160 - r["tagged"]
    left = db.query("SELECT COUNT(*) c FROM images WHERE ai_state=0")[0]["c"]
    assert left == r["pending"], "待办数和库里对不上"

    r2 = imp.import_folder(db, cfg, str(lib))
    assert r2["tagged"] == left and r2["pending"] == 0
    assert db.query("SELECT COUNT(*) c FROM images WHERE ai_state=0")[0]["c"] == 0


def test_force_reindexes_and_retags(db, cfg, tmp_path, fake_clf):
    from media_resource_manager import import_folder as imp

    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    imp.import_folder(db, cfg, str(lib))
    r = imp.import_folder(db, cfg, str(lib), force=True)
    assert r["indexed"] == 1 and r["tagged"] == 1
    assert db.query("SELECT COUNT(*) c FROM images")[0]["c"] == 1, "force 写成了两行"


def test_ai_backend_down_does_not_write_fake_tags(db, cfg, tmp_path, monkeypatch):
    """模型不可用：只入库，**绝不写假标签**，且不留待办队列。"""
    from media_resource_manager.classify import clip_model
    from media_resource_manager import import_folder as imp

    class _Stub:
        backend = "stub"
        model_note = ""

        def __init__(self, *a, **k):
            pass

        def classify(self, path):
            return [("person", 1.0)]

    monkeypatch.setattr(clip_model, "LocalClassifier", _Stub)
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    r = imp.import_folder(db, cfg, str(lib))
    assert r["tagged"] == 0
    assert "降级" in r["ai_note"], r["ai_note"]
    row = db.query("SELECT tags, ai_state FROM images")[0]
    assert json.loads(row["tags"]) == [], "降级时写了假标签"
    assert row["ai_state"] == 1, "降级后还留着待办队列（每次进来都会显示还有 N 张）"


def test_thread_cap_is_half_of_cores(db, cfg, tmp_path, fake_clf):
    """V0.0.22 起：并发数 = 一半核，单次推理线程数按并发除下来。

    老口径（串行打标）是「推理线程 = 一半核」；现在打标改成多线程并行，
    再让每个线程都开一半核的线程数 = 并发 × 核数，实测比串行还慢（抢核）。
    护栏钉的是**关系**：并发 × 单次线程数 ≈ 核数，且都不为 0。
    """
    from media_resource_manager import import_folder as imp

    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg.ai_threads = 0
    imp.import_folder(db, cfg, str(lib))
    cores = os.cpu_count() or 4
    workers = max(1, cores // 2)
    expect = max(1, min(workers, cores // workers))
    assert fake_clf.last is not None, "分类器没被构造"
    assert fake_clf.last.threads == expect, (
        f"限核没生效：传了 {fake_clf.last.threads}，应为 {expect}"
        f"（并发 {workers} × 线程 {expect} ≈ {cores} 核）")
    assert fake_clf.last.threads * workers <= cores * 1.5, "并发 × 线程数超过物理核太多"


def test_missing_folder_reports_clearly(db, cfg, tmp_path):
    from media_resource_manager import import_folder as imp

    r = imp.import_folder(db, cfg, str(tmp_path / "nope"))
    assert r["ok"] is False and "不存在" in r["error"]


def test_only_one_import_at_a_time(db, cfg, tmp_path):
    from media_resource_manager import import_folder as imp

    assert imp.is_running() is False
    imp._RUNNING.acquire()
    try:
        assert imp.is_running() is True
        r = imp.import_folder(db, cfg, str(tmp_path), do_tag=False)
        assert r["ok"] is False and "在跑" in r["error"]
    finally:
        imp._RUNNING.release()
    assert imp.is_running() is False


# ───────────────────────── 目录浏览（网页端选目录） ─────────────────────────
def _fs(host: str, q: str = ""):
    from media_resource_manager import webui

    class _Req:
        def __init__(self, query):
            self.query = query

    o = SimpleNamespace(headers={"Host": host})
    return webui._Handler._api_fs_list(o, _Req(q))


def test_fs_list_allows_remote_but_marks_it():
    """2026-10-29 改口径：远程**不再拒绝**列目录。

    原因：Docker/NAS 部署后，用户在另一台电脑上打开网页，要选的恰恰是
    服务器（NAS）上的目录；拒绝等于把「从文件夹学习」整个锁死。
    代价用两个标记补偿：``remote`` 告诉界面"这不是你面前这台机器"，
    ``where`` 给出机器名。仍然只读、只列目录、系统目录挡掉。
    """
    r = _fs("192.168.1.100:8080")
    assert r["ok"] is True, r
    assert r.get("remote") is True
    assert "where" in r
    for h in ("127.0.0.1:8080", "localhost:8080", "127.0.0.1"):
        rr = _fs(h, "path=")
        assert rr["ok"] is True, h
        assert rr.get("remote") is False, h


def test_fs_list_blocks_system_dirs():
    """系统目录不许浏览：点了也没用，还会把系统结构摊给远程用户。"""
    if os.name == "nt":
        return
    for bad in ("/proc", "/sys", "/etc"):
        r = _fs("127.0.0.1:8080", f"path={bad}")
        assert r["ok"] is False and "系统目录" in r["error"], bad
    r = _fs("127.0.0.1:8080", "path=/proc/1")
    assert r["ok"] is False


def test_fs_list_lists_only_dirs(tmp_path):
    _img(tmp_path / "a.jpg")
    (tmp_path / "sub").mkdir()
    r = _fs("127.0.0.1:8080", f"path={tmp_path}")
    assert r["ok"]
    names = [x["name"] for x in r["items"]]
    assert "sub" in names and "a.jpg" not in names, f"列了文件：{names}"
    assert r["path"].endswith(str(tmp_path.name))


def test_fs_list_reports_missing_dir(tmp_path):
    r = _fs("127.0.0.1:8080", f"path={tmp_path / 'nope'}")
    assert r["ok"] is False and "不存在" in r["error"]


def test_import_routes_are_wired():
    src = (ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")
    for p in ('"/api/fs/list"', '"/api/import/folder"',
              '"/api/import/job"', '"/api/import/cancel"'):
        assert p in src, f"缺路由 {p}"
    assert "_IMPORT_STATE" in src and "_IMPORT_LOCK" in src


# ───────────────────────── 网页端接线 ─────────────────────────
def _web(rel: str) -> str:
    return (ROOT / "media_resource_manager/webapp/src" / rel).read_text(encoding="utf-8")


def test_import_block_lives_on_export_page():
    """导入入口的位置变迁：设置页 → 图库页（V0.0.23）→ 导入与导出页（V0.0.26）。

    用户原话：「把『导出』改为『导入与导出』，然后把『导入学习』功能放在这里」。
    口径：
      · 组件 = components/ImportFolder.vue（自带目录选择与轮询）
      · 入口 = views/Export.vue 的「导入 · 导入图片」（V0.0.45 起还收 WebDAV）
      · 图库页只留一句指路，不许再挂一份（一个功能两个入口必然长出两套说法）
      · 设置页也不许有
    """
    comp = _web("components/ImportFolder.vue")
    for k in ("只入库、先不打标签", "包含子文件夹", "async function start",
              "function poll", "async function cancel", "imp-result"):
        assert k in comp, f"导入组件缺「{k}」"
    assert "<FolderPicker" in comp and '@picked="pick"' in comp

    # V0.0.35：进度与轮询已经搬到模块级单例。切页会销毁组件，组件里
    # 不许再在 onUnmounted 停定时器 —— 否则「切到图库再切回来就不导入了」。
    job = _web("importJob.js")
    for k in ("clearInterval(timer)", "setInterval(_tick", "localStorage",
              "resumeImport", "startImport", "cancelImport",
              "recursive: imp.recursive"):
        assert k in job, f"导入作业模块缺「{k}」"
    assert "onUnmounted(stopTimer)" not in comp, \
        "组件卸载时又把轮询停了，切页进度会断"

    ex = _web("views/Export.vue")
    assert "ImportFolder" in ex, "导入与导出页没挂导入组件"
    assert "导入图片" in ex, "导入与导出页缺导入入口标题"
    # V0.0.32：折叠面板改成标签页（用户要求「和设置页一样」）
    assert "k: 'imp'" in ex and "k: 'exp'" in ex, "导入/导出标签页定义缺失"
    assert "v-show=\"tab === 'imp'\"" in ex, "导入卡片没挂在导入标签下"

    gal = _web("views/Gallery.vue")
    assert "ImportFolder" not in gal, "图库页又挂了一份导入组件（应该只有指路）"
    # V0.0.32：指路直达导入标签
    assert "path: '/export'" in gal and "tab: 'imp'" in gal, \
        "图库页链接没带上导入标签参数"

    st = _web("views/Settings.vue")
    assert "async function startImport" not in st and "导入文件夹" not in st, \
        "设置页还留着导入块"


def test_folder_picker_only_lists_dirs():
    s = _web("components/FolderPicker.vue")
    assert "api.fsList" in s
    # 只列目录是服务端保证的；前端不该自己去过滤 files 字段
    assert "d.path" in s and "上一级" in s
    assert "就用这个文件夹" in s


def test_api_has_import_endpoints():
    s = _web("api.js")
    for k in ("fsList:", "importFolder:", "importJob:", "importCancel:"):
        assert k in s, f"api.js 缺 {k}"


def test_built_bundle_contains_import_ui():
    """构建产物里必须真有这些字 —— 防「源码改了但忘了 npm run build」。"""
    d = ROOT / "media_resource_manager/web/static/app/assets"
    js = "".join(p.read_text(encoding="utf-8", errors="replace")
                 for p in d.glob("*.js"))
    assert "导入图片" in js, "打包产物里没有导入界面，前端没重新构建"
    assert "选择文件夹" in js, "打包产物里没有导入组件的按钮文案"
    assert "/api/import/folder" in js
