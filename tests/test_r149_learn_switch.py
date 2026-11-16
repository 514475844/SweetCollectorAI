"""R149-B：「学习」独立开关护栏。

验证：
- 关掉时 PreferenceLearner.learn / learn_manual 不持久化（set_preference 不被调用）；
- 打开时正常调用；
- 老配置没有 learn_enabled 键 = 默认开（AppConfig 默认值 + from_dict 兼容）。
"""
import pytest

from media_resource_manager.classify.preference import (
    PreferenceLearner,
    is_learning_enabled,
    set_learning_enabled,
)
from media_resource_manager.config import AppConfig


class _FakeDB:
    """只拦 execute，便于断言 set_preference 是否真的写了库。"""

    def __init__(self):
        self.calls = 0

    def execute(self, *a, **k):
        self.calls += 1

    def query(self, *a, **k):
        return []


@pytest.fixture
def db():
    return _FakeDB()


@pytest.fixture(autouse=True)
def _reset_gate():
    # 每个用例前后都复位成开，避免影响别的用例/其他测试套件。
    prev = is_learning_enabled()
    set_learning_enabled(True)
    yield
    set_learning_enabled(prev)


def test_default_on(db):
    pl = PreferenceLearner(db, 0)
    pl.learn("like", ["cat"])
    assert db.calls == 1, "默认（开）时 learn 应写入偏好"


def test_off_skips_learn(db):
    set_learning_enabled(False)
    pl = PreferenceLearner(db, 0)
    pl.learn("like", ["cat"])
    assert db.calls == 0, "关掉后 learn 不应持久化任何偏好"


def test_on_learn_manual_persists(db):
    set_learning_enabled(True)
    pl = PreferenceLearner(db, 0)
    # learn_manual 内部会走 self.learn → 应落到 set_preference。
    pl.learn_manual("dislike", ["dog"])
    assert db.calls >= 1, "打开时 learn_manual 应通过 learn 写入偏好"


def test_off_learn_manual_skips(db):
    set_learning_enabled(False)
    pl = PreferenceLearner(db, 0)
    pl.learn_manual("dislike", ["dog"])
    assert db.calls == 0, "关掉后 learn_manual 也不应持久化"


def test_old_config_default_on():
    # 老配置 JSON 里没有 learn_enabled 键 → from_dict 应填默认 True。
    cfg = AppConfig.from_dict({"web_port": 9999})  # 故意不含 learn_enabled
    assert cfg.learn_enabled is True


def test_explicit_off_config():
    cfg = AppConfig.from_dict({"learn_enabled": False})
    assert cfg.learn_enabled is False
    # 序列化往返后仍为 False。
    cfg2 = AppConfig.from_dict(cfg.to_dict())
    assert cfg2.learn_enabled is False
