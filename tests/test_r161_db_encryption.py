"""R161 数据库静态加密护栏（方案 B，复用 crypt.py）。

不依赖 GUI；通过显式 encrypt=/master_password= 参数驱动，绝不触碰真实 data/。
工作库目录走 MRM_DB_WORK_DIR 环境变量隔离到临时目录。
"""
import os
import shutil
import sqlite3
import tempfile

import pytest

from media_resource_manager import crypt
from media_resource_manager.db import Database, EncryptionLocked


def test_decrypt_at_rest_disables(env):
    atrest_root, _ = env
    at = os.path.join(atrest_root, "mrm.db")
    _seed_plain(at, 9)
    Database(at, encrypt=True, master_password="pw").close()
    assert crypt.is_encrypted(at)
    Database.decrypt_at_rest(at, "pw")
    assert not crypt.is_encrypted(at)
    # 还原后明文可正常打开，行数一致
    c = sqlite3.connect(at)
    n = c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    c.close()
    assert n == 9
    # 错误密码解密应抛错
    Database(at, encrypt=True, master_password="pw").close()  # 重新加密
    import pytest as _pytest
    with _pytest.raises(crypt.DecryptError):
        Database.decrypt_at_rest(at, "bad")


@pytest.fixture
def env():
    work = tempfile.mkdtemp(prefix="r161_work_")
    atrest_root = tempfile.mkdtemp(prefix="r161_atrest_")
    os.environ["MRM_DB_WORK_DIR"] = work
    yield atrest_root, work
    os.environ.pop("MRM_DB_WORK_DIR", None)
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(atrest_root, ignore_errors=True)


def _seed_plain(at_rest: str, n: int = 25):
    conn = sqlite3.connect(at_rest)
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t(v) VALUES(?)",
                     [("val_%d" % i,) for i in range(n)])
    conn.commit()
    conn.close()


def _count(db):
    return db.query("SELECT COUNT(*) AS n FROM t")[0]["n"]


# ---- 1. 默认关：纯明文，向后兼容 ----
def test_default_off_plaintext_roundtrip(env):
    atrest_root, _ = env
    at = os.path.join(atrest_root, "mrm.db")
    _seed_plain(at, 10)
    db = Database(at)  # 无 encrypt 参数、无生效配置 -> 明文
    assert _count(db) == 10
    db.execute("INSERT INTO t(v) VALUES(?)", ("extra",))
    db.close()
    # 重开仍是明文，且 at-rest 不是加密包
    assert not crypt.is_encrypted(at)
    db2 = Database(at)
    assert _count(db2) == 11
    db2.close()


# ---- 2. 开启加密：明文 at-rest 原地迁移为加密 ----
def test_enable_migrates_plaintext_at_rest(env):
    atrest_root, _ = env
    at = os.path.join(atrest_root, "mrm.db")
    _seed_plain(at, 30)
    db = Database(at, encrypt=True, master_password="s3cret")
    assert _count(db) == 30
    db.close()
    # 迁移后 at-rest 是加密包
    assert crypt.is_encrypted(at)
    # 工作库明文文件应已删除
    work_dir = os.environ["MRM_DB_WORK_DIR"]
    remains = [p for p in os.listdir(work_dir)
               if p.endswith(".mrmwork")]
    assert not remains, "close 后明文工作库残留：" + str(remains)


# ---- 3. 正确密码重开：行数一致 ----
def test_reopen_with_correct_password(env):
    atrest_root, _ = env
    at = os.path.join(atrest_root, "mrm.db")
    _seed_plain(at, 30)
    Database(at, encrypt=True, master_password="pw").close()
    db = Database(at, encrypt=True, master_password="pw")
    assert _count(db) == 30
    db.close()


# ---- 4. 错误密码：解密失败应抛错 ----
def test_wrong_password_fails(env):
    atrest_root, _ = env
    at = os.path.join(atrest_root, "mrm.db")
    _seed_plain(at, 12)
    Database(at, encrypt=True, master_password="right").close()
    with pytest.raises(crypt.DecryptError):
        Database(at, encrypt=True, master_password="wrong").close()


# ---- 5. 未提供主密码：锁定 ----
def test_no_password_locked(env):
    atrest_root, _ = env
    at = os.path.join(atrest_root, "mrm.db")
    _seed_plain(at, 5)
    with pytest.raises(EncryptionLocked):
        Database(at, encrypt=True, master_password="").close()


# ---- 6. 关闭回写：at-rest 加密且工作库清理 ----
def test_close_reencrypt_and_clean(env):
    atrest_root, _ = env
    at = os.path.join(atrest_root, "mrm.db")
    db = Database(at, encrypt=True, master_password="pw")
    db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t(v) VALUES(?)", ("x",))
    db.close()
    assert crypt.is_encrypted(at)
    work_dir = os.environ["MRM_DB_WORK_DIR"]
    assert not [p for p in os.listdir(work_dir) if p.endswith(".mrmwork")]


# ---- 7. 备份也加密：解密备份得到完整 sqlite ----
def test_backup_is_encrypted(env):
    atrest_root, _ = env
    at = os.path.join(atrest_root, "mrm.db")
    _seed_plain(at, 15)  # 已含 table t + 15 行
    db = Database(at, encrypt=True, master_password="pw")
    db.execute("INSERT INTO t(v) VALUES(?)", ("b_extra",))
    dest = os.path.join(atrest_root, "backup.db")
    db.backup(dest)
    db.close()
    # 备份文件是加密包，不能直接当 sqlite 打开
    assert crypt.is_encrypted(dest)
    # 解密备份 -> 明文 sqlite -> 行数一致
    pt = crypt.decrypt_bytes(open(dest, "rb").read(), "pw")
    tmpd = tempfile.mkdtemp()
    tmpp = os.path.join(tmpd, "dec.db")
    open(tmpp, "wb").write(pt)
    c = sqlite3.connect(tmpp)
    n = c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    c.close()
    os.unlink(tmpp)
    shutil.rmtree(tmpd, ignore_errors=True)
    assert n == 16


# ---- 8. 崩溃残留：遗留明文工作库被重新加密收编 ----
def test_crash_residual_reencrypted(env):
    atrest_root, work = env
    at = os.path.join(atrest_root, "mrm.db")
    db = Database(at, encrypt=True, master_password="pw")
    db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t(v) VALUES(?)", ("persist",))
    db.close()
    # 模拟崩溃：重新把一份明文工作库丢回工作目录（不 close）
    work_file = os.path.join(work, "mrm.mrmwork")
    shutil.copyfile(at, work_file)  # at 此时是加密包，先解密出明文再伪造
    pt = crypt.decrypt_bytes(open(at, "rb").read(), "pw")
    open(work_file, "wb").write(pt)
    # 重新构造（模拟新进程）：应把残留工作库收编回 at-rest 并删除
    db2 = Database(at, encrypt=True, master_password="pw")
    assert _count(db2) == 1
    db2.close()
    assert not os.path.exists(work_file), "崩溃残留工作库未被收编删除"


# ---- 9. 数据零丢失：整轮加解密后行数不变 ----
def test_no_data_loss_full_cycle(env):
    atrest_root, _ = env
    at = os.path.join(atrest_root, "mrm.db")
    _seed_plain(at, 40)
    db = Database(at, encrypt=True, master_password="pw")
    assert _count(db) == 40
    db.execute("INSERT INTO t(v) VALUES(?)", ("new1",))
    db.execute("INSERT INTO t(v) VALUES(?)", ("new2",))
    assert _count(db) == 42
    db.close()
    db2 = Database(at, encrypt=True, master_password="pw")
    assert _count(db2) == 42
    # 直接解密 at-rest 校验物理文件里的行数
    pt = crypt.decrypt_bytes(open(at, "rb").read(), "pw")
    tmpd = tempfile.mkdtemp()
    tmpp = os.path.join(tmpd, "dec.db")
    open(tmpp, "wb").write(pt)
    c = sqlite3.connect(tmpp)
    n = c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    c.close()
    os.unlink(tmpp)
    shutil.rmtree(tmpd, ignore_errors=True)
    assert n == 42
    db2.close()


# ---- 10. 迁移原子性 + 密文完整性校验 ----
def test_migration_atomic_and_integrity(env):
    atrest_root, _ = env
    at = os.path.join(atrest_root, "mrm.db")
    _seed_plain(at, 8)
    Database(at, encrypt=True, master_password="pw").close()
    # at-rest 已是加密包
    assert crypt.is_encrypted(at)
    pt = crypt.decrypt_bytes(open(at, "rb").read(), "pw")
    tmpd = tempfile.mkdtemp()
    tmpp = os.path.join(tmpd, "dec.db")
    open(tmpp, "wb").write(pt)
    c = sqlite3.connect(tmpp)
    res = c.execute("PRAGMA integrity_check").fetchone()[0]
    c.close()
    os.unlink(tmpp)
    shutil.rmtree(tmpd, ignore_errors=True)
    assert res == "ok", "解密出的库未通过 integrity_check"


# ---- 11. 密文不可读：at-rest 不含明文 SQL 串 ----
def test_ciphertext_not_plaintext(env):
    atrest_root, _ = env
    at = os.path.join(atrest_root, "mrm.db")
    _seed_plain(at, 7)
    # 先做一个未加密的参照，确认明文里含特征串
    plain_bytes = open(at, "rb").read()
    assert b"val_3" in plain_bytes
    Database(at, encrypt=True, master_password="pw").close()
    enc_bytes = open(at, "rb").read()
    assert b"val_3" not in enc_bytes, "加密 at-rest 仍含明文特征串"


# ---- 12. 显式参数优先于生效配置 ----
def test_explicit_param_overrides_active_config(env):
    from media_resource_manager import config as cfgmod
    # 生效配置关，但显式 encrypt=True -> 走加密
    cfgmod.set_active_config(cfgmod.AppConfig(encryption_enabled=False))
    atrest_root, _ = env
    at = os.path.join(atrest_root, "mrm_a.db")
    _seed_plain(at, 3)
    Database(at, encrypt=True, master_password="pw").close()
    assert crypt.is_encrypted(at)
    # 显式不传 encrypt（配置关）-> 明文
    at2 = os.path.join(atrest_root, "mrm_b.db")
    _seed_plain(at2, 3)
    Database(at2).close()
    assert not crypt.is_encrypted(at2)
    cfgmod.set_active_config(None)
