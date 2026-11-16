"""r36：锁死「按主题词分子目录」开关必须真的落库。

以前 GUI 表单上有这个复选框（_fill_form 读、_collect_form 写），但 `tasks` 表
根本没有这一列，`repo.save_task` 也不写它 —— 用户勾上保存、关掉重开又变回没勾。
它不是纯显示开关：engine.py 里 `keyword_subdir=True` 会把落盘模板从 `{site}`
换成 `{site}/{keyword}`，所以开关静默丢失＝下载目录结构悄悄变了。
"""
import pytest


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    home = tmp_path / "mrm_home"
    home.mkdir()
    monkeypatch.setenv("MRM_HOME", str(home))
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    from media_resource_manager.db import Database
    db = Database(str(home / "mrm.db"))
    yield db
    try:
        db.close()
    except Exception:
        pass


def _task(name="K", kwsub=False):
    from media_resource_manager.models import TaskConfig, TaskStatus
    return TaskConfig(id=None, name=name, adapter_name="pictoa",
                      entry_urls=["https://www.pictoa.com/"],
                      keywords=[], blacklist=[], status=TaskStatus.IDLE,
                      ai_labels=[], ai_blacklist=[],
                      keyword_subdir=kwsub, max_pages=3)


def test_keyword_subdir_is_persisted(db_env):
    """勾上 → 保存 → 读回来还是勾上。"""
    from media_resource_manager.repo import get_task, save_task
    tid = save_task(db_env, _task(kwsub=True))
    assert get_task(db_env, tid).keyword_subdir is True


def test_keyword_subdir_update_persists(db_env):
    """改了再保存（走 UPDATE 分支）也要落库。"""
    from media_resource_manager.repo import get_task, save_task
    tid = save_task(db_env, _task(kwsub=False))
    t = get_task(db_env, tid)
    t.keyword_subdir = True
    save_task(db_env, t)
    assert get_task(db_env, tid).keyword_subdir is True


def test_legacy_db_without_column_defaults_false(db_env):
    """老库没有这一列时（还没跑到自动迁移）读回来要按 False 兜底，别炸。"""
    from media_resource_manager.repo import get_task, save_task
    tid = save_task(db_env, _task(kwsub=True))
    db_env.execute("ALTER TABLE tasks DROP COLUMN keyword_subdir")
    assert get_task(db_env, tid).keyword_subdir is False
