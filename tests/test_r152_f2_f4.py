"""R152-F2/F3/F4 护栏（2026-10-17 下午棒续）。

F2 旧数据迁移漏 library：config.py:_MIGRATE_ITEMS 无 "library"（图库本体在
   data\\library）→ 老用户搬库不搬图，图库显「磁盘已缺失」。修：补 library +
   迁移前给小文件留快照（shutil.move 跨盘 = 复制后删源，中断有丢数据窗口）。

F3 配置回写吞键：from_dict 静默丢弃未知键。修：未知键进 cfg.extra + 告警，
   to_dict 原样带回；R106 删掉的 ai_slot_* 死字段不许借道复活（有锁）。

F4 robots 空 Allow 误判 + 末条匹配：空 Allow 编译成 ^$；allowed() 取末条
   匹配而非最长匹配（RFC 9309 最具体规则胜出）。修：空 Allow 跳过 + 最长
   匹配优先、同长 Allow 胜出。
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from media_resource_manager import config as cfgmod
from media_resource_manager.crawler.robots import RobotsPolicy


# --------------------------------------------------------------------- F2

def test_f2_migrate_items_include_library():
    assert "library" in cfgmod._MIGRATE_ITEMS, \
        "漏了 library：老用户搬库不搬图，图库全显磁盘已缺失"


def test_f2_library_actually_migrated(tmp_path, monkeypatch):
    prog = tmp_path / "prog"
    new = prog / "data"
    legacy = tmp_path / "legacy"
    (legacy / "library" / "sub").mkdir(parents=True)
    (legacy / "library" / "sub" / "a.jpg").write_bytes(b"img")
    (legacy / "mrm.db").write_bytes(b"db-bytes")
    monkeypatch.delenv("MRM_NO_MIGRATE", raising=False)
    monkeypatch.setattr(cfgmod, "_program_dir", lambda: prog)
    monkeypatch.setattr(cfgmod, "user_data_dir", lambda: new)
    monkeypatch.setattr(cfgmod, "_legacy_user_data_dir", lambda: legacy)

    moved = cfgmod.migrate_from_legacy()

    assert "library" in moved
    assert "mrm.db" in moved
    assert (new / "library" / "sub" / "a.jpg").read_bytes() == b"img"
    assert (new / "mrm.db").read_bytes() == b"db-bytes"
    # 迁移前快照：只复制小文件；目录项不复制（同盘 rename 原子、跨盘中断
    # 两头并存都不会丢）——这是写明的取舍，不是漏写。
    bak = new / "_migrate_backup"
    assert (bak / "mrm.db").read_bytes() == b"db-bytes"
    assert not (bak / "library").exists()
    # 幂等：标记已写，第二次不搬
    assert cfgmod.migrate_from_legacy() == []


def test_f2_migrate_never_overwrites_new_dir(tmp_path, monkeypatch):
    prog = tmp_path / "prog"
    new = prog / "data"
    legacy = tmp_path / "legacy"
    (legacy / "library").mkdir(parents=True)
    (legacy / "library" / "old.jpg").write_bytes(b"old")
    (legacy / "mrm.db").write_bytes(b"db")
    (new / "library").mkdir(parents=True)
    (new / "library" / "new.jpg").write_bytes(b"new")
    monkeypatch.delenv("MRM_NO_MIGRATE", raising=False)
    monkeypatch.setattr(cfgmod, "_program_dir", lambda: prog)
    monkeypatch.setattr(cfgmod, "user_data_dir", lambda: new)
    monkeypatch.setattr(cfgmod, "_legacy_user_data_dir", lambda: legacy)

    moved = cfgmod.migrate_from_legacy()

    assert "library" not in moved, "新目录已有 library，不许覆盖"
    assert (new / "library" / "new.jpg").read_bytes() == b"new"
    assert not (new / "library" / "old.jpg").exists()


# --------------------------------------------------------------------- F3

def test_f3_unknown_keys_preserved():
    cfg = cfgmod.AppConfig.from_dict({"web_port": 9090, "future_key": "v"})
    assert cfg.web_port == 9090
    assert cfg.extra.get("future_key") == "v", "未知键被静默吞了"
    out = cfg.to_dict()
    assert out.get("future_key") == "v", "保存时未知键又丢了（回写吞键）"


def test_f3_unknown_key_survives_roundtrip():
    cfg = cfgmod.AppConfig.from_dict({"my_own_setting": [1, 2]})
    again = cfgmod.AppConfig.from_dict(cfg.to_dict())
    assert again.extra.get("my_own_setting") == [1, 2]


def test_f3_dead_ai_slot_keys_not_revived():
    """R106 锁：ai_slot_* 是已删除死字段，未知键保留机制不得让它们复活。"""
    d = {"ai_slot_auto": True, "ai_slot_day": "Step-5-Preview",
         "ai_slot_evening": "HY3", "ai_slot_night": "HY4",
         "ai_slot_applied": "HY3"}
    cfg = cfgmod.AppConfig.from_dict(d)
    for k in d:
        assert k not in cfg.extra
        assert k not in cfg.to_dict()


def test_f3_known_fields_still_work():
    cfg = cfgmod.AppConfig.from_dict({"dark_mode": True, "skin": "violet"})
    assert cfg.dark_mode is True and cfg.skin == "violet"
    assert cfg.to_dict()["skin"] == "violet"


# --------------------------------------------------------------------- F4

def test_f4_empty_allow_is_noop():
    # 空 Allow 不是「只放行空路径」；Disallow: / 之后跟空 Allow 不该解禁
    p = RobotsPolicy("User-agent: *\nDisallow: /\nAllow: \n")
    assert not p.allowed("/gallery/x.jpg")
    # 只有空 Allow 时一切放行
    p2 = RobotsPolicy("User-agent: *\nAllow: \n")
    assert p2.allowed("/anything")


def test_f4_longest_match_wins_regardless_of_order():
    forward = RobotsPolicy("User-agent: *\nDisallow: /a\nAllow: /a/b\n")
    backward = RobotsPolicy("User-agent: *\nAllow: /a/b\nDisallow: /a\n")
    for p in (forward, backward):
        assert not p.allowed("/a/c"), "更具体的 /a/b 不该放行 /a/c"
        assert p.allowed("/a/b/c.jpg"), "最长匹配 Allow 必须胜出"


def test_f4_tie_goes_to_allow():
    p = RobotsPolicy("User-agent: *\nDisallow: /ab\nAllow: /ab\n")
    assert p.allowed("/ab"), "同长冲突时 Allow 优先（主流实现口径）"


def test_f4_plain_disallow_still_blocks():
    p = RobotsPolicy("User-agent: *\nDisallow: /private\n")
    assert not p.allowed("/private/x")
    assert p.allowed("/public/x")
