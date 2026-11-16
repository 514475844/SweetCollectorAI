"""storage 模块单元测试 —— 规模安全存储分桶。

可直接 `python tests/test_storage.py` 运行（无需 pytest），也兼容 pytest
（函数名以 test_ 开头）。
"""
import os
import sys
import tempfile
from pathlib import Path

# 让 `media_resource_manager` 作为 namespace 包可被导入（cwd = 项目根）。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from media_resource_manager.storage import (  # noqa: E402
    bucket_rel,
    bucketed_dest,
    resolve_existing,
    save_atomic,
    migrate_flat_to_bucketed,
)


def test_bucket_rel_shape():
    rel = bucket_rel("pictoa", "https://x.com/a.jpg", "a.jpg")
    parts = rel.parts
    assert parts[0] == "pictoa", parts
    assert len(parts[1]) == 2 and len(parts[2]) == 2, "需要两级 hex 分桶"
    assert parts[3] == "a.jpg"


def test_bucket_deterministic():
    a = bucket_rel("s", "https://u/1.jpg", "1.jpg")
    b = bucket_rel("s", "https://u/1.jpg", "1.jpg")
    assert a == b
    # 不同 URL → 不同桶
    c = bucket_rel("s", "https://u/2.jpg", "2.jpg")
    assert a != c


def test_save_atomic_and_resolve():
    tmp_path = Path(tempfile.mkdtemp())
    dest = bucketed_dest(tmp_path, "s", "https://u/1.jpg", "1.jpg")
    save_atomic(b"hello", dest)
    assert dest.exists() and dest.read_bytes() == b"hello"
    got = resolve_existing(tmp_path, "s", "https://u/1.jpg", "1.jpg")
    assert got == dest


def test_resolve_falls_back_to_flat():
    tmp_path = Path(tempfile.mkdtemp())
    flat = tmp_path / "s" / "legacy.jpg"
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_bytes(b"x")
    got = resolve_existing(tmp_path, "s", "https://other/legacy.jpg", "legacy.jpg")
    assert got == flat


def test_migrate():
    tmp_path = Path(tempfile.mkdtemp())
    old_p = tmp_path / "s" / "a.jpg"
    old_p.parent.mkdir(parents=True, exist_ok=True)
    old_p.write_bytes(b"data")

    class FakeDB:
        def __init__(self):
            self.rows = [{
                "id": 1, "path": str(old_p),
                "source_url": "https://u/a.jpg", "site": "s", "filename": "a.jpg",
            }]
            self.updates = []

        def query(self, sql, params=None):
            return self.rows

        def execute(self, sql, params=None):
            self.updates.append(params)

    db = FakeDB()
    n = migrate_flat_to_bucketed(db, tmp_path)
    assert n == 1, n
    new = bucketed_dest(tmp_path, "s", "https://u/a.jpg", "a.jpg")
    assert new.exists() and new.read_bytes() == b"data"
    assert not old_p.exists()
    assert db.updates and db.updates[0] == (str(new), 1)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n全部 {len(fns)} 项通过 ✅")


if __name__ == "__main__":
    _run_all()
