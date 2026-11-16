"""SQLite persistence layer.

Tables:
  tasks        - per-task configuration + status
  images       - discovered / downloaded media + metadata + dedup hashes
  reviews      - human review decisions & preference signals
  preferences  - learned GLOBAL preference model (label -> weight); task_id is always 0
  rules        - stored adapter rule files (versioned, rollback-able)
  compliance   - permission-basis log (why a site may be crawled)
  audit_log    - every destructive / sensitive operation for accountability
"""
from __future__ import annotations

import sqlite3
import threading
import atexit
import os
import re
from contextlib import contextmanager
from . import crypt as _mrm_crypt
from pathlib import Path
from typing import Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    adapter_name  TEXT,
    entry_urls    TEXT DEFAULT '[]',
    keywords      TEXT DEFAULT '[]',
    blacklist     TEXT DEFAULT '[]',
    proxy         TEXT,
    use_proxy     INTEGER DEFAULT 0,
    concurrency   INTEGER DEFAULT 4,
    max_depth     INTEGER DEFAULT 3,
    delay         REAL    DEFAULT 1.0,
    output_dir    TEXT,
    auto_run      INTEGER DEFAULT 0,
    status        TEXT    DEFAULT 'idle',
    is_template   INTEGER DEFAULT 0,
    parent_id     INTEGER,
    created_at    TEXT    DEFAULT (datetime('now')),
    updated_at    TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    -- R221/C10：task_id **刻意不加 REFERENCES tasks(id)**。删任务的现存语义是
    -- 「只删任务定义，用户已下载的图库图片全部保留」，而 task_id 是 NOT NULL、
    -- 又不能 ON DELETE SET NULL ⇒ 加外键等于逼用户在「删任务先删图」和
    -- 「任务删不掉」里二选一，两条都不能接受。孤儿 task_id 由现有查询天然
    -- 容忍（图库/审核查询都不 JOIN tasks 存在性）。其余三张表的外键照常强制
    -- （reviews→images、failed_urls→tasks、task_pages→tasks）。
    task_id       INTEGER NOT NULL,
    source_url    TEXT NOT NULL,
    sha256        TEXT,
    phash         TEXT,
    width         INTEGER,
    height        INTEGER,
    bytes         INTEGER,
    path          TEXT,
    tags          TEXT DEFAULT '[]',
    score         REAL DEFAULT 0.0,
    user_score    REAL DEFAULT 0.0,
    blacklisted   INTEGER DEFAULT 0,
    in_quarantine INTEGER DEFAULT 0,
    collected_at  TEXT DEFAULT (datetime('now')),
    rating        INTEGER DEFAULT 0,
    favorite      INTEGER DEFAULT 0,
    UNIQUE(task_id, source_url)
);
CREATE INDEX IF NOT EXISTS idx_images_sha  ON images(sha256);
CREATE INDEX IF NOT EXISTS idx_images_task ON images(task_id);
-- 2026-10-28：网页底栏要算"最近 1 分钟下了多少"，按时间戳过滤。
-- 没有这个索引时每次采样都是全表扫描（图库上万行时肉眼可见卡顿）。
CREATE INDEX IF NOT EXISTS idx_images_collected ON images(collected_at);

CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id   INTEGER NOT NULL REFERENCES images(id),
    decision   TEXT,
    task_id    INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS preferences (
    task_id   INTEGER NOT NULL,
    label     TEXT NOT NULL,
    weight    REAL DEFAULT 0.0,
    seen      INTEGER DEFAULT 0,
    -- #37/D3：把「命中次数」按方向拆开，权重明细里才讲得清
    -- 这条权重是被**主题词（喜欢）**触发过几次、被**排除词（不喜欢/拉黑）**触发过几次
    hit_pos   INTEGER DEFAULT 0,
    hit_neg   INTEGER DEFAULT 0,
    -- R174：来源（user_typed 用户手填 / model_auto 模型自动标签）。
    -- 只允许 user_typed 负权进 tasks.ai_blacklist 硬黑名单。
    -- V0.0.3：默认必须是 model_auto（保守端）。老配置写死 user_typed，
    -- 会让模型自动学到的词被当成「用户手填」流进硬黑名单，反过来屏蔽
    -- 用户真正想要的类目（见 repo._pref_origin 的注释）。
    source    TEXT DEFAULT 'model_auto',
    -- R169 第 3 条：最后一次记这笔的时间，供**时间衰减**判"这个词还活着吗"。
    updated_at TEXT,
    PRIMARY KEY (task_id, label)
);

CREATE TABLE IF NOT EXISTS rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    version    INTEGER DEFAULT 1,
    content    TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_rules_name ON rules(name);

CREATE TABLE IF NOT EXISTS compliance (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER,
    site         TEXT,
    robots_ok    INTEGER,
    terms_ok     INTEGER DEFAULT 1,
    basis        TEXT,
    checked_at   TEXT DEFAULT (datetime('now')),
    detail       TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT DEFAULT (datetime('now')),
    actor      TEXT,
    action     TEXT,
    target     TEXT,
    detail     TEXT
);

-- R166：学习样本账本（**append-only**）。
-- 为什么不能只靠 reviews 表：用户要求「所有判断动作都参与学习」，其中
-- 「隔离区彻底删除」会连带 `DELETE FROM reviews WHERE image_id=?`（R164-Bug1
-- 的真删除语义），样本当场就没了 —— 看板统计不到、来源也分不清。
-- 这张表 **只记来源留痕**（谁、在哪个视图、什么动作、极性与权重），
-- 权重本身仍只走 preferences（不重复计分）；删图也不动这里的历史。
-- source：gallery / review / quarantine / web_gallery / web_quarantine …
-- 判据同 orders：看板能按来源分组区分（R166 第 4 条）。
-- 2026-11-05：本 schema 里 learn_samples 建表只在下方出现一次（同名定义
-- 在基建 schema 段重复过，属冗余拷贝，已删其一）。

-- F7（R154）：失败网址清单 —— 按 URL 粒度记录下载失败，取代原来
-- 「错误采样最多 8 条」的内存截断（采样只当摘要，完整清单在这里）。
-- UNIQUE(task_id,url)：同一网址反复失败只累加 tries，不堆重复行。
CREATE TABLE IF NOT EXISTS failed_urls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id),
    url         TEXT NOT NULL,
    error       TEXT,
    tries       INTEGER DEFAULT 1,
    first_at    TEXT DEFAULT (datetime('now')),
    last_tried  TEXT DEFAULT (datetime('now')),
    UNIQUE(task_id, url)
);
CREATE INDEX IF NOT EXISTS idx_failed_urls_task ON failed_urls(task_id);

-- F9（R154）：页面级断点续采 —— discovery 访问过的页面落库。
-- status 四态：pending=已发现未抓 / done=抓成 / error=抓取失败 /
-- blocked=被 robots 或站点挡了。**只有 done 的页重跑时跳过**，
-- pending/error/blocked 一律带着原深度重试（断点续采的"续"）。
-- UNIQUE(task_id,url)：同一页跨多次运行只一行，done 不会被重新
-- 发现降级回 pending（record_page_seen 走 INSERT OR IGNORE）。
CREATE TABLE IF NOT EXISTS task_pages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   INTEGER NOT NULL REFERENCES tasks(id),
    url       TEXT NOT NULL,
    status    TEXT DEFAULT 'pending',
    depth     INTEGER DEFAULT 0,
    images    INTEGER DEFAULT 0,
    error     TEXT,
    first_at  TEXT DEFAULT (datetime('now')),
    last_at   TEXT DEFAULT (datetime('now')),
    UNIQUE(task_id, url)
);
CREATE INDEX IF NOT EXISTS idx_task_pages_task ON task_pages(task_id);

-- F18（R154）：磁盘空间看板的每日快照 —— **一天一行**（day 主键，
-- 同日记录走先 UPDATE 后 INSERT 的便携 upsert，见 diskspace.record_snapshot）。
-- 为什么落库：7 日增长趋势要跨天看，只在内存里攒的话，用户昨天没开软件
-- 那天就是个洞。detail 存 {目录key: 字节} 的 JSON；free_bytes 单独一列
-- （剩余空间也是趋势的一部分：目录没涨但盘满了，那是别的东西在吃）。
CREATE TABLE IF NOT EXISTS disk_snapshots (
    day         TEXT PRIMARY KEY,
    total_bytes INTEGER DEFAULT 0,
    detail      TEXT DEFAULT '{}',
    free_bytes  INTEGER,
    recorded_at TEXT DEFAULT (datetime('now'))
);

-- #60：一次性数据迁移的标记表（幂等的靠山）。
-- 迁移本身会改用户数据（阈值 0.5 -> 0.35），必须**只跑一次**：
-- 跑第二次就会把用户后来手动调好的阈值又改回去。
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- R166：学习样本账本（**append-only**）。
-- 为什么不能只靠 reviews 表：用户要求「所有判断动作都参与学习」，其中
-- 「隔离区彻底删除」会连带 `DELETE FROM reviews WHERE image_id=?`（R164-Bug1
-- 的真删除语义），样本当场就没了 —— 看板统计不到、来源也分不清。
-- 这张表 **只记来源留痕**（谁、在哪个视图、什么动作、极性与权重），
-- 权重本身仍只走 preferences（不重复计分）；删图也不动这里的历史。
-- source：gallery / review / quarantine / web_gallery / web_quarantine …
-- 判据同订单：看板能按来源分组区分（R166 第 4 条）。
CREATE TABLE IF NOT EXISTS learn_samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT DEFAULT (datetime('now')),
    image_id   INTEGER,
    task_id    INTEGER,
    act        TEXT NOT NULL,
    polarity   TEXT,
    weight     REAL DEFAULT 0.0,
    source     TEXT,
    labels     TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_learn_samples_act ON learn_samples(act);
CREATE INDEX IF NOT EXISTS idx_learn_samples_ts ON learn_samples(ts);

-- V0.0.18：训练素材集导出历史账本（append-only）。
-- 「导出过哪几批、什么时候、去了哪个目录、多少张」必须留痕：训练数据
-- 讲究可追溯，用户要能回看每一批的筛选口径。删图不动这里的历史。
CREATE TABLE IF NOT EXISTS export_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT DEFAULT (datetime('now')),
    dest          TEXT NOT NULL,
    copied        INTEGER DEFAULT 0,
    captions      INTEGER DEFAULT 0,
    skipped       INTEGER DEFAULT 0,
    min_score     REAL DEFAULT 0,
    filters       TEXT DEFAULT '{}',
    note          TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_export_history_ts ON export_history(ts);

-- V0.0.18：通知中心（累积历史）。
-- 为什么不能只靠 SSE：`/api/events` 只推「正在看页面的人」，关掉浏览器
-- 消息就没了。任务完成/失败/备份异常等事件必须落库，页面重开仍可回看。
-- kind：task_done / task_failed / backup / quarantine / update / info …
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT DEFAULT (datetime('now')),
    kind       TEXT NOT NULL,
    level      TEXT DEFAULT 'info',      -- info / warn / error
    title      TEXT NOT NULL,
    body       TEXT DEFAULT '',
    link       TEXT DEFAULT '',          -- 前端路由（如 /tasks），可空
    read       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notifications_ts ON notifications(ts);
CREATE INDEX IF NOT EXISTS idx_notifications_read ON notifications(read);
"""


def _json_dumps_list(value) -> str:
    """把标签列表序列化成 JSON 文本（``None``/异常一律退化成 ``[]``）。

    ⚠ 与 ``repo`` 里的同名逻辑同口径但**故意不跨层 import**：db 是底层，
    被 repo 依赖；反向 import 会成环。记样本失败绝不能影响主线动作，
    所以这里任何异常都吞成空列表。
    """
    import json as _json
    try:
        return _json.dumps(list(value or []), ensure_ascii=False)
    except Exception:
        return "[]"


# #60：AI 阈值的新默认。0.5 太严——本机内置 CLIP 对正确匹配的分数最高也就
# 0.5~0.65，等于「大部分正确结果被当成未命中扔进隔离区」。
DEFAULT_AI_THRESHOLD = 0.35
# 迁移前的旧默认。只迁移**恰好等于旧默认**的行，用户自己调过的（0.2/0.8…）不动。
LEGACY_AI_THRESHOLD = 0.5
# 迁移标记键：写进 app_meta 后永不再跑。
THRESHOLD_MIGRATION_KEY = "migrated_ai_threshold_035"


class EncryptionLocked(Exception):
    """数据库已开启加密但未提供主密码时抛出（入口点应改提示用户输入）。"""


class Database:
    def __init__(self, db_path: Path, encrypt: Optional[bool] = None,
                 master_password: Optional[str] = None):
        self.db_path = Path(db_path)
        self._encrypted, self._master_password = self._resolve_encryption(
            encrypt, master_password)
        self._work_path = None
        if self._encrypted:
            if not self._master_password:
                raise EncryptionLocked(
                    "数据库已开启加密，但未提供主密码：请在启动界面输入主密码，"
                    "或设置环境变量 MRM_DB_PASSWORD 后重启。")
            self._prepare_encrypted()
            _conn_path = self._work_path
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            _conn_path = self.db_path
            # 反向降级：加密开关已关，但 at-rest 仍是加密包 -> 一次性解密为明文。
            # 这样「关闭加密后重启」能正常打开，不必依赖遗留的明文工作库。
            if _mrm_crypt.is_encrypted(self.db_path):
                from . import config as _mc
                _pw = _mc.get_db_master_password()
                if not _pw:
                    raise EncryptionLocked(
                        "数据库 at-rest 仍是加密态，但当前未开启加密且未提供主密码，"
                        "无法解密降级。请重新开启加密并输入主密码后重启，"
                        "或恢复加密开关。")
                _pt = _mrm_crypt.decrypt_bytes(
                    self.db_path.read_bytes(), _pw)
                _tmp = self.db_path.with_suffix(self.db_path.suffix + ".dec.part")
                _tmp.write_bytes(_pt)
                _tmp.replace(self.db_path)
        self._conn = sqlite3.connect(str(_conn_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # V0.0.14：图库「文件缺失」筛选用。SQL 层没有「磁盘上这个文件还在吗」
        # 这种谓词，注册一个只读自定义函数，让筛选与分页（id 游标 / OFFSET）
        # 共用同一条查询，不必先把整库拉进 Python 再切页。
        #   · 零字节文件按「缺失」处理（用户明确零字节图一律不要）；
        #   · 不标 deterministic：文件会增删，标了 SQLite 可能缓存旧结果；
        #   · 只在 WHERE 里出现 mrm_file_exists 时才会被调用，常规查询零开销。
        def _mrm_file_exists(value) -> int:
            try:
                if not value:
                    return 0
                p = Path(str(value))
                return 1 if (p.is_file() and p.stat().st_size > 0) else 0
            except (OSError, ValueError):
                return 0

        self._conn.create_function("mrm_file_exists", 1, _mrm_file_exists)
        # r40：同一个连接被多线程共用（GUI 主线程 + 抓取线程 + QThread 里的
        # 聚类去重 / 清理回收站）。WAL + busy_timeout 只解决「多连接」之间的锁，
        # 解决不了「同一个连接对象」被两个线程同时 execute/commit —— Python 的
        # sqlite3 连接不是线程安全的，一个线程 commit 的瞬间另一个线程正好在
        # execute 就会 sqlite3.OperationalError: database is locked。
        # r40 真机 30 分钟扫描实录：「相似图聚类去重」QThread 与「清理过期回收站」
        # 各自撞了这条，QThread 整个 run() 抛异常被 Qt 吞掉。
        # 对策：进程内一把可重入锁把所有 SQL 串起来。SQLite 本地查询都是微秒级，
        # 串行的代价远小于「偶发 database is locked 把整个操作掀翻」。
        self._lock = threading.RLock()
        # R221/C10：显式事务嵌套深度。execute/executemany 在事务内**不**自作
        # 主张 commit（否则 transaction() 的第一条语句就把账本结了）。
        self._tx_depth = 0
        # 关键修复：开启 WAL + 长 busy_timeout。否则抓取线程（调度器）持续写库时，
        # GUI 线程的图库读取会撞 "database is locked" → load_gallery 抛异常 →
        # 图库整段时间显示空白。WAL 允许读写并发，busy_timeout 让读等待而非立即失败。
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            # R221/C10（订单原话「synchronous 评估，WAL 下 NORMAL 可接受则在
            # 注释写明」）：NORMAL 在 WAL 模式下可接受 —— WAL 下 NORMAL 只在
            # checkpoint 时刻可能丢「最近若干笔已提交事务」，且仅限断电/掉电；
            # 进程崩溃、软件异常退出、强杀**都不会**损坏库（WAL 已落盘，重放即可）。
            # 本软件是本地单机图库，断电丢一个 checkpoint 窗口的代价，远小于
            # FULL 每次提交都 fsync 的卡顿（批量写库/断点续审时尤其明显）。
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()
        # R221/C10：外键强制。先跑 images→tasks 外键的退役迁移（老库建表时带
        # REFERENCES tasks(id)，与「删任务保留图片」语义冲突，见 SCHEMA 注释），
        # 迁移成功才开 PRAGMA foreign_keys=ON；失败则保持关闭（绝不半开）。
        if self._migrate_drop_images_task_fk():
            try:
                self._conn.execute("PRAGMA foreign_keys=ON")
            except Exception:
                pass
        if self._encrypted:
            atexit.register(self.close)
        self._closed = False

    # ---------------- R161 静态加密（方案 B，复用 crypt.py） ----------------
    def _resolve_encryption(self, encrypt, master_password):
        """解析生效的加密开关与主密码：显式参数优先，否则读当前进程生效配置。"""
        if encrypt is not None:
            return bool(encrypt), (master_password or "")
        try:
            from . import config as _mc
            _ac = _mc.get_active_config()
            if _ac is not None and getattr(_ac, "encryption_enabled", False):
                return True, _mc.get_db_master_password()
        except Exception:
            pass
        return False, ""

    def _prepare_encrypted(self):
        """解密/迁移到内存外的工作库，并设置 self._work_path。

        - 已加密态：at-rest 文件是 .mrmx 密文 -> 解密到工作库。
        - 明文态（开启后未迁移 / 迁移中途崩溃）：原地迁移为加密，再解密到工作库。
        - 全新库：工作库由 connect 创建，close 时才加密落盘。
        - 崩溃残留：发现上次遗留的明文工作库 -> 直接重新加密收编，不弹框。
        """
        _env = os.environ.get("MRM_DB_WORK_DIR")
        if _env:
            _work_dir = Path(_env)
        else:
            from . import config as _mc
            try:
                _ud = _mc.user_data_dir()
            except Exception:
                _ud = self.db_path.parent
            _work_dir = Path(_ud) / ".mrm_work"
        _work_dir.mkdir(parents=True, exist_ok=True)
        self._work_path = _work_dir / (self.db_path.stem + ".mrmwork")
        if self._work_path.exists():
            # 上次进程被杀留下的明文工作库：收编回 at-rest，再删工作库
            self._reencrypt_work_to_at_rest()
            try:
                self._work_path.unlink(missing_ok=True)
            except Exception:
                pass
        _at = self.db_path
        if _at.exists():
            if _mrm_crypt.is_encrypted(_at):
                _pt = _mrm_crypt.decrypt_bytes(_at.read_bytes(), self._master_password)
                self._work_path.write_bytes(_pt)
            else:
                self._migrate_plaintext_at_rest_to_encrypted()
                _pt = _mrm_crypt.decrypt_bytes(_at.read_bytes(), self._master_password)
                self._work_path.write_bytes(_pt)

    def _migrate_plaintext_at_rest_to_encrypted(self):
        """开启加密时的原地迁移：明文库 -> 加密库（原子替换，失败可回滚）。"""
        _at = self.db_path
        _pt = _at.read_bytes()
        if not _pt[:16].lower().startswith(b"sqlite format"):
            raise RuntimeError("at-rest 文件不是预期的明文 SQLite 库，跳过加密迁移")
        _enc = _mrm_crypt.encrypt_bytes(_pt, self._master_password)
        if _mrm_crypt.decrypt_bytes(_enc, self._master_password) != _pt:
            raise RuntimeError("加密迁移自校验失败：解密结果与原明文不一致")
        _tmp = _at.with_suffix(_at.suffix + ".enc.part")
        _tmp.write_bytes(_enc)
        _tmp.replace(_at)  # 原子替换；此前原明文始终未被改动

    def _reencrypt_work_to_at_rest(self):
        """把工作库（明文）加密回写 at-rest，并自校验。

        供 close / atexit / 崩溃残留复用。任一步失败即抛错，绝不留下半份。
        """
        if not self._work_path or not self._work_path.exists():
            return
        # 正常 close 路径 self._conn 已关闭；崩溃残留路径 self._conn 尚未打开。
        # 两种情况下都自行打开工作库做 checkpoint，再读文件加密，避免依赖未就绪连接。
        try:
            _c = sqlite3.connect(str(self._work_path))
            _c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _c.commit()
            _c.close()
        except Exception:
            pass
        _pt = self._work_path.read_bytes()
        _enc = _mrm_crypt.encrypt_bytes(_pt, self._master_password)
        if _mrm_crypt.decrypt_bytes(_enc, self._master_password) != _pt:
            raise RuntimeError("退出加密自校验失败：解密结果与工作库不一致")
        _tmp = self.db_path.with_suffix(self.db_path.suffix + ".enc.part")
        _tmp.write_bytes(_enc)
        _tmp.replace(self.db_path)

    def _migrate_drop_images_task_fk(self) -> bool:
        """R221/C10：退役 images→tasks 外键（老库），返回能否开 FK 强制。

        背景见 SCHEMA 里 images 建表注释：老库的 images 带
        ``task_id ... REFERENCES tasks(id)``，与「删任务保留图片」冲突。
        新库建表已不带该外键；老库在这里做一次表重建，全部包在一笔事务里，
        中断即整体回滚，老表一根毛不少。

        ⚠ 三个必须踩到的坑：
        ① ``PRAGMA foreign_keys`` 在事务内是 no-op ⇒ 先 commit + 关外键，
           重建完再交给调用方开；
        ② DROP/RENAME 父表前必须 foreign_keys=OFF，否则 reviews 引用着
           images，DROP 会被 FK 拦下；
        ③ AUTOINCREMENT 的 sqlite_sequence：实测 RENAME 会把 images_new 的
           序列条目一起改名（拷贝时 INSERT 显式 id 已把序列推到 MAX(id)），
           所以下一条 INSERT 不会撞 id —— 下面那句 INSERT OR REPLACE 只是
           显式兜底（防的是「将来有人改成不带 id 的拷贝」），不是必需件。
        返回 False = 迁移没做成（调用方保持 foreign_keys 关闭，绝不半开）。
        """
        try:
            fk = self._conn.execute(
                "PRAGMA foreign_key_list(images)").fetchall()
            if not any((r[2] == "tasks") for r in fk):
                return True                      # 新库 / 已迁移过
            self._conn.commit()                  # 坑①：让 PRAGMA 能生效
            self._conn.execute("PRAGMA foreign_keys=OFF")
            orig = self._conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='images'").fetchone()
            create_sql = orig[0] if orig else ""
            if not create_sql.strip():
                return False
            # 从原建表语句派生 images_new：改名 + 摘掉 tasks 外键子句，
            # 其余列/约束/UNIQUE(task_id, source_url) 原样保留（比手搓
            # CREATE TABLE 稳 —— _migrate 加过的列也都在原 sql 里）。
            new_sql = re.sub(
                r"(?i)(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)"
                r"(\[images\]|\"images\"|`images`|images)",
                r"\1images_new", create_sql, count=1)
            new_sql = re.sub(r"(?i)REFERENCES\s+tasks\s*\(\s*id\s*\)",
                             "", new_sql)
            self._conn.execute("DROP TABLE IF EXISTS images_new")
            self._conn.execute(new_sql)
            # 列清单动态取（_migrate 加过的列一个都不能丢）
            cols = [r[1] for r in self._conn.execute(
                "PRAGMA table_info(images)").fetchall()]
            collist = ", ".join('"%s"' % c for c in cols)
            self._conn.execute(
                "INSERT INTO images_new (%s) SELECT %s FROM images"
                % (collist, collist))
            # 老索引（idx_images_* 及 _migrate_gallery_indexes 建的）先快照
            idx_rows = self._conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name='images' AND sql IS NOT NULL").fetchall()
            self._conn.execute("DROP TABLE images")     # 坑②：FK 已 OFF
            self._conn.execute("ALTER TABLE images_new RENAME TO images")
            for _name, isql in idx_rows:
                try:
                    self._conn.execute(isql)             # 原 sql 自带 IF NOT EXISTS
                except Exception:
                    pass
            # 坑③：补回 AUTOINCREMENT 计数，防新插入撞已存在的 id
            self._conn.execute(
                "INSERT OR REPLACE INTO sqlite_sequence(name, seq) "
                "SELECT 'images', COALESCE(MAX(id), 0) FROM images")
            self._conn.commit()
            left = self._conn.execute(
                "PRAGMA foreign_key_list(images)").fetchall()
            if any((r[2] == "tasks") for r in left):
                return False                     # 没摘干净：不冒险开 FK
            return True
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception:
                pass
            return False

    def _migrate(self):
        """Add columns introduced after the first release (idempotent)."""
        wanted = {
            "tasks": {
                "proxy_id": "INTEGER",
                "ai_enabled": "INTEGER DEFAULT 0",
                "ai_labels": "TEXT DEFAULT '[]'",
                "ai_blacklist": "TEXT DEFAULT '[]'",
                "ai_threshold": "REAL DEFAULT 0.35",
                "ai_match_mode": "TEXT DEFAULT 'any'",
                "ai_exclude_match_mode": "TEXT DEFAULT 'any'",
                "priority": "INTEGER DEFAULT 5",
                "last_error": "TEXT",
                "last_run_at": "TEXT",
                "min_width": "INTEGER DEFAULT 0",
                "min_height": "INTEGER DEFAULT 0",
                "min_bytes": "INTEGER DEFAULT 0",
                "max_bytes": "INTEGER DEFAULT 0",
                "schedule_at": "TEXT",
                "rate_limit": "INTEGER DEFAULT 0",
                "allowed_hours": "TEXT",
                # 每任务最大抓取页数（任务级；0=不限）
                "max_pages": "INTEGER DEFAULT 0",
                # r29 主题词分级（二级 / 三级细分词 / 严格模式）
                "ai_labels_l2": "TEXT DEFAULT '[]'",
                "ai_labels_l3": "TEXT DEFAULT '[]'",
                "ai_label_strict": "INTEGER DEFAULT 0",
                # 2026-10-28：主题词**无限分级**（1..N 层）+ 每层独立严格开关。
                # 空 '[]' = 回退上面的一/二/三级三列（老任务行为零变化）。
                "ai_label_levels": "TEXT DEFAULT '[]'",
                "ai_label_strict_levels": "TEXT DEFAULT '[]'",
                # r36：GUI 表单上有「按主题词分子目录」开关，但 tasks 表以前没这列
                # → 勾了保存、重开又回退；它不是纯显示开关，还会改 engine 的落盘
                # 模板（{site} vs {site}/{keyword}），所以必须真的落库。
                "keyword_subdir": "INTEGER DEFAULT 0",
                # R173：任务级 UA 与站点 Cookie（幂等，老库补列，默认空串 = 不覆盖）。
                #   · user_agent 空 = 沿用全局 cfg.user_agent（不填就不会有行为变化）
                #   · cookie 空 = 不额外发 Cookie（同域名 cookie.json 那条老路径不受影响）
                #   两者一起落库是因为 cf_clearance 与 UA+IP 绑定：只有 UA 没有
                #   Cookie（或反过来）都救不了被 Cloudflare 挡住的站。
                "user_agent": "TEXT DEFAULT ''",
                "cookie": "TEXT DEFAULT ''",
            },
            "images": {
                # 软件「拿不准」(AI 分数在阈值附近的)图片进审核队列，拿得准的直接入库。
                "needs_review": "INTEGER DEFAULT 0",
                "viewed": "INTEGER DEFAULT 0",
                # #82「最近浏览」：最后一次看图的时间戳（UTC，与 collected_at 同口径）。
                # 只加列不改任何既有读写路径 —— 老库升级后该列为 NULL，
                # 排序时按「NULL 排最后」处理，不会出现「没看过的图霸占最近浏览」。
                "last_viewed_at": "TEXT",
                # #68 元数据：格式与拍摄时间（仅展示用，不依赖外部库）
                "fmt": "TEXT",
                "taken_at": "TEXT",
                # #59 隔离时间：用于按天数自动清理回收站
                "quarantined_at": "TEXT",
                # 评分语义拆分：score=AI 打分(0~1)，user_score=用户手动评分(0~5)
                "user_score": "REAL DEFAULT 0.0",
                # R248：批量导入的「待打标」标记。0=还没打标、1=已打标。
                # 这是「可停可续」的唯一依据 —— 打标中途关软件，重开只要
                # 查 ai_state=0 就能接着打，不需要另存断点文件。
                # 默认 1：**老库里的行一律视为已打标**（否则升级后会把几万张
                # 早就打好标签的图当成待办，重打一遍纯浪费）。
                "ai_state": "INTEGER DEFAULT 1",
                # V0.0.8：送审原因（中文，用户可读）。图片**为什么**进待审核，
                # 以前只有审计日志里的英文备注知道，审核页上一个字看不到。
                # 写库那一刻就翻成中文存这列，桌面/网页审核页直接显示。
                "review_reason": "TEXT DEFAULT ''",
                # V0.0.46：语义搜索 —— CLIP 图片向量（float16 字节串）。
                # 打标/回填时顺手存下来，搜索时把查询文本也编码成向量，
                # 全库一次 numpy 矩阵乘法排序，3.5 万张毫秒级。
                #   · 512 维（B32/B16）× 2 字节 = 1KB/张，768 维（L14）= 1.5KB/张
                #   · clip_model 记这条向量是哪个模型算的：换了模型档位，
                #     旧向量不能混用（维度都可能不同），搜索只用对得上的。
                "clip_vec": "BLOB",
                "clip_model": "TEXT DEFAULT ''",
            },
            # #37/D3：权重明细要「主题词命中 / 排除词命中」两列
            "preferences": {
                "hit_pos": "INTEGER DEFAULT 0",
                "hit_neg": "INTEGER DEFAULT 0",
                # R174：来源（user_typed 用户手填 / model_auto 模型自动标签）。
                # 列的 DEFAULT 取 'user_typed' 是**列**语义（没写就是用户写的），
                # 但下面的存量回灌会立刻把老行改成 model_auto —— 见那段注释。
                "source": "TEXT DEFAULT 'user_typed'",
                # R169 第 3 条：最后一次被"记一笔"的时间，供**时间衰减**判
                # 「这个词还活着吗」。老行补列后是 NULL —— 衰减函数把 NULL
                # 当成"没有时间信息"**跳过不衰减**（宁可不动，也不能凭一个
                # 猜出来的时间把用户积累的词削掉）。
                "updated_at": "TEXT",
            },
        }
        _fresh_pref_source = False
        for table, cols in wanted.items():
            have = {r["name"] for r in
                    self._conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                    if table == "preferences" and col == "source":
                        _fresh_pref_source = True

        # ★ R174 存量回灌：这一列是**刚建出来**的 ⇒ 库里所有行都早于它，
        #   全部来自"还没有来源概念"的旧代码 —— 那批行绝大多数正是模型标签
        #   （用户投诉的「红丝 / 美丽 / 可爱」就是这类）。
        #   若不回灌，它们会被列的 DEFAULT 标成 user_typed，于是
        #   「一键清模型自动词」对**用户手上现有的脏数据一条都清不掉** ——
        #   本单等于没做。回灌成 model_auto 才对症。
        #   ⚠ 只在这一列新建的那一次执行：以后新写的行各有显式来源，
        #     再洗一遍会把用户新填的词错标成模型词。
        if _fresh_pref_source:
            try:
                self._conn.execute(
                    "UPDATE preferences SET source='model_auto' "
                    "WHERE source IS NULL OR source='' OR source='user_typed'")
            except Exception:
                pass

        # 学习记录改为全局偏好（task_id=0）：把历史上按任务存的学习权重合并到 0，
        # 这样「全面清理」清空任务后，新建的任务仍能继承已学的喜欢/不喜欢，免得重新学习。
        try:
            self._conn.execute(
                "UPDATE preferences SET task_id=0 WHERE task_id<>0")
        except Exception:
            pass

        self._migrate_gallery_indexes()
        self._migrate_threshold_default()

    # ---------------- L1 keyset 分页配套索引 ----------------
    def _migrate_gallery_indexes(self):
        """给图库列表补复合索引（L1）。

        **keyset 分页本身不建索引也能跑，但一样是全表扫描** —— 游标只是把
        「丢弃前 m 行」换成了「扫到第一个满足游标的行为止」，扫描量没变。
        真正让深翻页变快的是下面这些索引：它们让 WHERE 里的等值条件
        （blacklisted/in_quarantine/task_id）先定位到一个连续区间，
        再在这个区间里按 id/score 有序取 limit 行。

        单独用 CREATE INDEX IF NOT EXISTS 而不是塞进 SCHEMA：
        SCHEMA 只对**新库**生效，老库升级后拿不到 —— 而实际用户的库都是老库。
        """
        for stmt in (
            # 图库默认视图的 WHERE 固定是 path IS NOT NULL + 两个 =0。
            # 部分索引（partial index）只收录满足该条件的行，体积和扫描都更小。
            "CREATE INDEX IF NOT EXISTS idx_images_gallery_id "
            "ON images(id) WHERE path IS NOT NULL AND blacklisted=0 "
            "AND in_quarantine=0",
            # 按任务筛 + 按 id 翻页（图库「只看某任务」+ 默认排序）
            "CREATE INDEX IF NOT EXISTS idx_images_task_id "
            "ON images(task_id, id)",
            # 按分数翻页：keyset 游标是 (score, id)，这个索引正好是它的全序
            "CREATE INDEX IF NOT EXISTS idx_images_score_id "
            "ON images(score, id)",
            # 待审核页固定查 needs_review=1
            "CREATE INDEX IF NOT EXISTS idx_images_review_id "
            "ON images(id) WHERE needs_review=1",
            # R248：批量导入的「待打标」队列。打标阶段每批都查
            # `WHERE task_id=? AND ai_state=0 ORDER BY id LIMIT N`，
            # 没有这个部分索引就是每批一次全表扫描（10 万行时肉眼可见）。
            # 只收录 ai_state=0 的行，索引本身很小（打完就出队）。
            "CREATE INDEX IF NOT EXISTS idx_images_tag_queue "
            "ON images(task_id, id) WHERE ai_state=0",
        ):
            try:
                self._conn.execute(stmt)
            except Exception:
                # 老库里缺列（如 needs_review 还没 ALTER 出来）时索引建不上，
                # 不能因此把整个迁移炸掉 —— 慢只是慢，不建索引功能仍正确。
                pass

    # ---------------- #60 一次性数据迁移 ----------------
    def meta_get(self, key: str, default: str = "") -> str:
        """读 app_meta。表不存在的老库自动补一张，绝不让读标记本身炸掉。"""
        # V0.0.6：全类靠 self._lock 串行同一个连接（check_same_thread=False），
        # 原先只有这里不加锁还直接 commit —— 导入线程写学习水位、界面线程
        # 写维护标记时会跟别的事务撞在一起，轻则 database is locked，
        # 重则把别人半截事务提前结掉。
        try:
            with self._lock:
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS app_meta "
                    "(key TEXT PRIMARY KEY, value TEXT)")
                r = self._conn.execute(
                    "SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
                return (r["value"] if r else "") or default
        except Exception:
            return default

    def meta_set(self, key: str, value: str = "1"):
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS app_meta "
                "(key TEXT PRIMARY KEY, value TEXT)")
            self._conn.execute(
                "INSERT INTO app_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))
            self._conn.commit()

    def _migrate_threshold_default(self) -> int:
        """#60：把还停在旧默认 0.5 的任务阈值降到 0.35。**只跑一次**。

        为什么必须一次性而不是每次开库都跑：用户手动把某个任务调到 0.5 是
        他自己的决定；若无脑反复迁移，他改一次、重开软件就被改回去，永远调不动。
        所以用 app_meta 打标记，标记写成功后本进程内也不再重复。

        只动**恰好等于旧默认 0.5** 的行（浮点容差 1e-9），用户调过的 0.2/0.8 一律不碰。
        返回被改的行数（0 表示无需迁移/已迁过）。
        """
        if self.meta_get(THRESHOLD_MIGRATION_KEY):
            return 0
        n = 0
        try:
            cur = self._conn.execute(
                "UPDATE tasks SET ai_threshold=? "
                "WHERE ai_threshold IS NOT NULL "
                "AND ABS(ai_threshold - ?) < 1e-9",
                (DEFAULT_AI_THRESHOLD, LEGACY_AI_THRESHOLD))
            n = cur.rowcount or 0
        except Exception:
            n = 0
        self.meta_set(THRESHOLD_MIGRATION_KEY,
                      f"migrated={n} at 2026-10-06")
        if n:
            try:
                self.audit("migration", "ai_threshold_default",
                           f"{LEGACY_AI_THRESHOLD}->{DEFAULT_AI_THRESHOLD}",
                           f"{n} 个任务")
            except Exception:
                pass
        return n

    @contextmanager
    def connect(self):
        # 直接拿连接自己写 SQL 的老代码路径也得排队，否则绕过上面那把锁
        with self._lock:
            yield self._conn

    @contextmanager
    def transaction(self):
        """R221/C10：把多步「先读后写」包成一本账（BEGIN IMMEDIATE）。

        为什么需要它：``db.execute`` 每条语句各自 commit，「SELECT 看没有 →
        INSERT」这种多步写在并发下，两个线程可能都查到「没有」然后各写各的
        （后一个撞 UNIQUE(task_id, source_url) 直接抛异常）——upsert_image
        正是这个形态。BEGIN IMMEDIATE 一开始就占写锁，第二个线程的 BEGIN 按
        busy_timeout 排队，等前一本账提交后它再读，看到的就是已提交的行 ⇒
        自然走 UPDATE 分支而不是重复 INSERT。异常即 ROLLBACK，不留半笔。
        嵌套调用直接加入外层账本（RLock 可重入，不会自锁）。
        """
        with self._lock:
            conn = self._conn
            # ⚠ 顺序要紧：先看在不在事务里。Python sqlite3 对 DML 会隐开
            # 事务，若这里不管三七二十一先 commit，外层 transaction() 的
            # BEGIN IMMEDIATE 账本会被提前结掉（嵌套调用直接 GG）。
            if conn.in_transaction:
                # 已在外层账本里（嵌套）⇒ 直接加入，不另开、不负责提交
                self._tx_depth += 1
                try:
                    yield conn
                finally:
                    self._tx_depth -= 1
                return
            # PRAGMA/BEGIN 在事务内是 no-op ⇒ 非嵌套时先 commit 掉可能的
            # 隐事务，保证 BEGIN 在事务外发出。
            try:
                conn.commit()
            except Exception:
                pass
            conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            finally:
                self._tx_depth -= 1
            try:
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            # R221/C10：事务内不抢跑 commit，交给 transaction() 收口
            if not self._tx_depth:
                self._conn.commit()
            return cur

    def executemany(self, sql: str, rows: Iterable) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.executemany(sql, list(rows))
            if not self._tx_depth:
                self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def audit(self, actor: str, action: str, target: str = "", detail: str = ""):
        self.execute(
            "INSERT INTO audit_log(actor, action, target, detail) VALUES (?,?,?,?)",
            (actor, action, target, detail),
        )

    # ---------------- R166：学习样本账本（append-only 来源留痕） ----------------
    def record_learn_sample(self, act: str, polarity: str = "", weight: float = 0.0,
                            image_id=None, task_id=None, source: str = "",
                            labels=None) -> None:
        """记一条「用户做了什么判断」的样本来源。

        ⚠ 与 ``PreferenceLearner.learn`` 的分工**必须分清**，不许重复算分：
          · 本方法只写 ``learn_samples``（来源留痕，供看板按来源分组）；
          · 权重累加仍只在 ``preferences``（``set_preference``）里发生。
        这么切的原因见 SCHEMA 里 ``learn_samples`` 的注释：隔离区「彻底删除」
        会连带删掉 reviews 行，样本来源只能另存。**append-only、失败静默**：
        记样本失败绝不能让用户的审核/删除动作本身失败。
        """
        try:
            self.execute(
                "INSERT INTO learn_samples"
                "(image_id, task_id, act, polarity, weight, source, labels) "
                "VALUES (?,?,?,?,?,?,?)",
                (image_id, task_id, act, polarity, float(weight or 0.0),
                 source, _json_dumps_list(labels)))
        except Exception:
            pass

    def learn_sample_counts(self) -> dict:
        """按动作聚合样本数（看板「样本来源」用）。空表返回 {}。"""
        try:
            return {r["act"]: int(r["c"] or 0) for r in self.query(
                "SELECT act, COUNT(*) c FROM learn_samples GROUP BY act")}
        except Exception:
            return {}

    def learn_sample_sources(self) -> dict:
        """按来源视图聚合样本数：{source: {act: n}}。空表返回 {}。"""
        out: dict = {}
        try:
            for r in self.query(
                    "SELECT source, act, COUNT(*) c FROM learn_samples "
                    "GROUP BY source, act"):
                out.setdefault(str(r["source"] or ""), {})[str(r["act"])] = \
                    int(r["c"] or 0)
        except Exception:
            pass
        return out

    # ---------------- F7：失败网址清单（task_id + url 粒度） ----------------
    #
    # 设计口径（与 R154-F7 订单逐条对齐）：
    # · 落点 = **下载失败**那一瞬间（engine._process_image 的 `not out.ok`
    #   分支、gallery-dl 入口级异常分支），不是任务结束后的统计；
    # · 同一 (task_id, url) 反复失败 → tries+1、error/last_tried 更新，
    #   绝不堆行（UNIQUE 约束 + 先 UPDATE 后 INSERT 的便携写法，
    #   不依赖 SQLite 3.24+ 的 ON CONFLICT DO UPDATE）；
    # · 重试成功 → delete_failed_urls 划掉该行；「清空清单」是用户显式操作，
    #   与「重试成功自动移除」是两条路，别混。
    def record_failed_url(self, task_id: int, url: str, error: str = "") -> None:
        """记一条失败网址（已存在则累加尝试次数、刷新原因与时间）。"""
        url = (url or "").strip()
        if not url:
            return
        err = (error or "")[:300]
        cur = self.execute(
            "UPDATE failed_urls SET error=?, tries=tries+1, "
            "last_tried=datetime('now') WHERE task_id=? AND url=?",
            (err, task_id, url))
        if not cur.rowcount:
            self.execute(
                "INSERT INTO failed_urls(task_id, url, error) VALUES (?,?,?)",
                (task_id, url, err))

    def list_failed_urls(self, task_id: int) -> list[sqlite3.Row]:
        """某任务的失败网址清单（按记录顺序；tries=反复失败次数）。"""
        return self.query(
            "SELECT id, task_id, url, error, tries, first_at, last_tried "
            "FROM failed_urls WHERE task_id=? ORDER BY id", (task_id,))

    def failed_url_count(self, task_id: int) -> int:
        row = self.query(
            "SELECT COUNT(*) AS n FROM failed_urls WHERE task_id=?",
            (task_id,))
        return int(row[0]["n"]) if row else 0

    def delete_failed_urls(self, task_id: int, urls) -> int:
        """重试成功后把对应网址划掉。返回真实删除行数。"""
        targets = [u for u in (urls or []) if u]
        if not targets:
            return 0
        n = 0
        for u in targets:
            n += self.execute(
                "DELETE FROM failed_urls WHERE task_id=? AND url=?",
                (task_id, u)).rowcount or 0
        return n

    def clear_failed_urls(self, task_id: int) -> int:
        """用户显式清空某任务的失败清单（不动任何图片/任务数据）。"""
        return self.execute(
            "DELETE FROM failed_urls WHERE task_id=?", (task_id,)).rowcount or 0

    # ---------------- F9：页面级断点续采（task_id + url 粒度） ----------------
    #
    # 设计口径（与 R154-F9 订单逐条对齐）：
    # · 落点 = discovery **发现链接的瞬间**（pending）与**抓完一页的瞬间**
    #   （done/error/blocked），不等任务结束 —— 中途停掉/崩掉进度不丢；
    # · 重跑时 **done 跳过**（不重fetch、省流量），pending/error/blocked
    #   带着原深度重试 —— 这就是"断点续采"的"续"；
    # · record_page_seen 走 INSERT OR IGNORE：已抓过的页不会被"重新
    #   发现"降级成 pending；record_page_result 走先 UPDATE 后 INSERT 的
    #   便携 upsert（同 F7，不依赖 SQLite 3.24+ 的 ON CONFLICT DO UPDATE）。
    def record_page_seen(self, task_id: int, url: str, depth: int = 0) -> None:
        """发现一个新链接：落一条 pending（已存在则不动，绝不降级）。"""
        url = (url or "").strip()
        if not url:
            return
        self.execute(
            "INSERT OR IGNORE INTO task_pages(task_id, url, status, depth) "
            "VALUES (?,?, 'pending', ?)",
            (task_id, url, int(depth or 0)))

    def record_page_result(self, task_id: int, url: str, status: str,
                           images: int = 0, error: str = "",
                           depth: int = 0) -> None:
        """抓完一页按结果落账（done/error/blocked；行不存在则补建）。"""
        url = (url or "").strip()
        if not url or status not in ("done", "error", "blocked"):
            return
        err = (error or "")[:200]
        cur = self.execute(
            "UPDATE task_pages SET status=?, images=?, error=?, depth=?, "
            "last_at=datetime('now') WHERE task_id=? AND url=?",
            (status, int(images or 0), err, int(depth or 0), task_id, url))
        if not cur.rowcount:
            self.execute(
                "INSERT INTO task_pages"
                "(task_id, url, status, depth, images, error) "
                "VALUES (?,?,?,?,?,?)",
                (task_id, url, status, int(depth or 0),
                 int(images or 0), err))

    def task_pages_resume(self, task_id: int) -> dict:
        """续跑种子：{url: (status, depth)}，喂给 Discovery。"""
        rows = self.query(
            "SELECT url, status, depth FROM task_pages WHERE task_id=?",
            (task_id,))
        return {r["url"]: (r["status"] or "pending", int(r["depth"] or 0))
                for r in rows}

    def task_page_stats(self, task_id: int) -> dict:
        """页面账本摘要（"已抓 x/y 页"的两个数都从这里出，别处不另算）。"""
        rows = self.query(
            "SELECT status, COUNT(*) AS n FROM task_pages WHERE task_id=? "
            "GROUP BY status", (task_id,))
        out = {"done": 0, "pending": 0, "error": 0, "blocked": 0, "total": 0}
        for r in rows:
            st = r["status"] or "pending"
            out[st if st in out else "pending"] = int(r["n"])
            out["total"] += int(r["n"])
        return out

    def clear_task_pages(self, task_id: int) -> int:
        """删任务时连带清掉它的页面账本（防孤行堆积）。"""
        return self.execute(
            "DELETE FROM task_pages WHERE task_id=?", (task_id,)).rowcount or 0

    def mark_viewed(self, image_id: int) -> None:
        """#82「最近浏览」：标记看图并盖时间戳。

        与老的 `UPDATE images SET viewed=1` 唯一差别是多写一个 last_viewed_at；
        **重复看会覆盖成最新时间** ——「最近浏览」的语义就是「最后一次」，
        不是「第一次」。时间戳由 SQLite 的 datetime('now') 生成（UTC），
        与 collected_at 同一口径，免得 Python 侧时区不一致导致排序错乱。
        """
        if image_id is None:
            return
        self.execute(
            "UPDATE images SET viewed=1, last_viewed_at=datetime('now') "
            "WHERE id=?", (int(image_id),))

    def close(self):
        """关闭连接。**幂等语义靠 `_closed` 标记**，见 `is_closed()`。"""
        # 走锁：否则 close 正好落在别的线程 execute 中间会直接抛 ProgrammingError
        with self._lock:
            if self._closed:
                return
            if self._encrypted and self._work_path is not None:
                # 加密态：先把工作库落盘并加密回写 at-rest，再删明文工作库
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    self._conn.commit()
                except Exception:
                    pass
                try:
                    self._conn.close()
                except Exception:
                    pass
                # 先置「已关闭」，re-encrypt 失败也视为关闭（失败不抛到调用方，
                # 也不让 atexit 重复进入）
                self._closed = True
                try:
                    self._reencrypt_work_to_at_rest()
                    self._work_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return
            self._closed = True
            try:
                self._conn.close()
            except Exception:
                pass

    def is_closed(self) -> bool:
        """库是否已关闭。

        r34-r35 修复：关窗口会 `closeEvent → self.db.close()`，而引擎的 worker
        协程如果不自查就会拿着已关闭的库继续跑，每张图都撞
        ``sqlite3.ProgrammingError: Cannot operate on a closed database``
        刷屏（实测一次刷了 280 条、图还白下载）。引擎 worker 每轮取图前都会
        用这个判断提前收工。
        """
        return bool(getattr(self, "_closed", False))

    def backup(self, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        b = sqlite3.connect(str(dest))
        with self._lock:
            self._conn.backup(b)
        b.close()
        if self._encrypted:
            # 加密态：备份副本同样加密（与 at-rest 同口径），不留明文泄露口
            _pt = Path(dest).read_bytes()
            _enc = _mrm_crypt.encrypt_bytes(_pt, self._master_password)
            Path(dest).write_bytes(_enc)
        return Path(dest)

    # ---------------- R241 · 整库备份的「就地恢复」 ----------------
    #: 恢复时**先删子表再删父表**（外键 ON 时的安全顺序）。
    RESTORE_DELETE_ORDER = (
        "reviews", "failed_urls", "task_pages", "learn_samples",
        "compliance", "audit_log", "disk_snapshots", "preferences",
        "rules", "images", "tasks",
    )
    #: 恢复时**先灌父表再灌子表**（同上）。
    RESTORE_INSERT_ORDER = (
        "tasks", "images", "rules", "preferences", "disk_snapshots",
        "audit_log", "compliance", "learn_samples", "task_pages",
        "failed_urls", "reviews",
    )

    def restore_from(self, other: Path, progress=None) -> dict:
        """用另一个库文件**就地覆盖**本库内容（连接不关、事务一本账）。

        为什么是「就地覆盖」——见 ``fullbackup`` 模块头：Web 端与桌面端/引擎
        共享同一个 Database 对象，关库重开会把别人手里的引用变成死引用。

        做法：``ATTACH`` 备份库 → 一本 ``BEGIN IMMEDIATE`` 事务里逐表
        ``DELETE`` 再 ``INSERT ... SELECT``（按主键原样搬，``reviews`` 等外键
        依然对得上）→ 重置 ``sqlite_sequence``（否则新插入会和恢复出来的
        最大 id 撞主键）→ 跑一遍 ``_migrate``（备份可能来自旧版本，缺新列）
        → ``quick_check``。

        失败整本回滚：宁可「什么都没变」，绝不能留半份数据（比恢复前更糟）。
        """
        other = Path(other)
        res = {"tables": {}, "integrity": "ok", "fk_bad": 0}
        with self._lock:
            if self._closed:
                raise RuntimeError("数据库已关闭，无法恢复")
            conn = self._conn
            conn.commit()
            try:
                conn.execute("ATTACH DATABASE ? AS bk", (str(other),))
            except sqlite3.Error as e:
                raise RuntimeError("备份里的数据库打不开：%s" % e)
            try:
                main_tables = [str(r[0]) for r in conn.execute(
                    "SELECT name FROM main.sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                bk_tables = {str(r[0]) for r in conn.execute(
                    "SELECT name FROM bk.sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                known = set(self.RESTORE_DELETE_ORDER)
                # 清单之外的表（以后新增的）也照搬，免得「恢复完新表还是空的」
                extra = [t for t in main_tables if t not in known]
                del_order = [t for t in self.RESTORE_DELETE_ORDER
                             if t in main_tables] + extra
                ins_order = [t for t in self.RESTORE_INSERT_ORDER
                             if t in main_tables] + extra
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for t in del_order:
                        conn.execute('DELETE FROM main."%s"' % t)
                    for t in ins_order:
                        if t not in bk_tables:
                            res["tables"][t] = 0
                            continue
                        cols = [str(r[1]) for r in conn.execute(
                            'PRAGMA main.table_info("%s")' % t)]
                        bk_cols = {str(r[1]) for r in conn.execute(
                            'PRAGMA bk.table_info("%s")' % t)}
                        use = [c for c in cols if c in bk_cols]
                        if not use:
                            res["tables"][t] = 0
                            continue
                        col_sql = ", ".join('"%s"' % c for c in use)
                        conn.execute(
                            'INSERT INTO main."%s"(%s) SELECT %s FROM bk."%s"'
                            % (t, col_sql, col_sql, t))
                        res["tables"][t] = int(conn.execute(
                            'SELECT COUNT(*) FROM main."%s"' % t).fetchone()[0])
                    # 自增序列对齐到恢复后的最大 rowid
                    try:
                        for (nm,) in conn.execute(
                                "SELECT name FROM main.sqlite_sequence").fetchall():
                            if nm in main_tables:
                                conn.execute(
                                    'UPDATE main.sqlite_sequence SET seq='
                                    'IFNULL((SELECT MAX(rowid) FROM main."%s"),0)'
                                    ' WHERE name=?' % nm, (nm,))
                    except sqlite3.Error:
                        pass
                    # 恢复出来的 'running' 是上一次运行留下的幽灵状态：
                    # 引擎根本没在跑它，界面却永远显示"运行中"。
                    conn.execute(
                        "UPDATE main.tasks SET status='idle' WHERE status='running'")
                    conn.commit()
                except BaseException:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            finally:
                try:
                    conn.execute("DETACH DATABASE bk")
                except sqlite3.Error:
                    pass
            # 备份可能来自旧版本：补齐当前版本新增的列（幂等 ALTER）
            try:
                self._migrate()
                conn.commit()
            except sqlite3.Error:
                pass
            res["integrity"] = "ok"
            try:
                rows = conn.execute("PRAGMA quick_check").fetchall()
                if rows:
                    res["integrity"] = str(rows[0][0])
            except sqlite3.Error:
                pass
            try:
                res["fk_bad"] = len(conn.execute(
                    "PRAGMA foreign_key_check").fetchall())
            except sqlite3.Error:
                res["fk_bad"] = 0
        return res

    @classmethod
    def decrypt_at_rest(cls, db_path, master_password) -> None:
        """关闭加密：把加密 at-rest 原子还原为明文，并清理明文工作库。

        解密结果不是合法 SQLite（主密码错）则抛错，绝不留半份。
        """
        from . import crypt as _mc
        p = Path(db_path)
        cls._clean_work(p)
        if not _mc.is_encrypted(p):
            return  # 本来就是明文
        _pt = _mc.decrypt_bytes(p.read_bytes(), master_password)
        if not _pt[:16].lower().startswith(b"sqlite format"):
            raise RuntimeError("解密结果不是合法 SQLite（主密码可能错误）")
        _tmp = p.with_suffix(p.suffix + ".dec.part")
        _tmp.write_bytes(_pt)
        _tmp.replace(p)

    @staticmethod
    def _clean_work(db_path) -> None:
        """清掉该库可能残留的明文工作库（崩溃残留 / 关闭后漏删）。"""
        p = Path(db_path)
        _env = os.environ.get("MRM_DB_WORK_DIR")
        if _env:
            _wd = Path(_env)
        else:
            try:
                from . import config as _mc
                _ud = _mc.user_data_dir()
            except Exception:
                _ud = p.parent
            _wd = Path(_ud) / ".mrm_work"
        try:
            for _f in _wd.glob(p.stem + ".mrmwork*"):
                try:
                    _f.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass

    # ---------------- dedup / hygiene helpers ----------------
    def image_sha_exists(self, sha: str, exclude_task: Optional[int] = None) -> bool:
        """#57 入库前内容去重：sha256 是否已在库（同图不同 URL 也不重复入库）。"""
        if not sha:
            return False
        if exclude_task is not None:
            rows = self.query(
                "SELECT 1 FROM images WHERE sha256=? AND task_id<>? LIMIT 1",
                (sha, exclude_task))
        else:
            rows = self.query("SELECT 1 FROM images WHERE sha256=? LIMIT 1",
                              (sha,))
        return bool(rows)

    def quarantine_prune(self, keep_days: int) -> int:
        """#59 回收站自动清理：隔离超过 keep_days 天的记录，返回清理条数。

        只清「既不在审核队列、也没被收藏」的孤立隔离项，且同步删除磁盘文件
        （文件不存在则仅清 DB 行）。保留期=0 表示永不清。
        """
        if keep_days <= 0:
            return 0
        import time
        # 2026-11-05：清空隔离区时 GUI 传 keep_days=10**6（"传极大值=全部删除"），
        # 旧算法 time.localtime(time.time() - 10**6*86400) 是个约 -8.5e10 的负
        # epoch，Windows 的 C runtime 直接 OSError: [Errno 22]，点击必崩、一张
        # 都删不掉。改为：cutoff 时刻早于 epoch（keep_days*86400 已超当前时刻）
        # 时用最小时间戳字符串占位 —— SQLite 按文本比较，正常时间戳都大于它，
        # 等价于"全部选中"，且 Linux/Windows 行为一致。
        cutoff_s = time.time() - keep_days * 86400
        if cutoff_s < 0:
            cutoff = "0000-00-00 00:00:00"
        else:
            cutoff = time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(cutoff_s))
        rows = self.query(
            "SELECT id, path FROM images WHERE in_quarantine=1 "
            "AND (quarantined_at IS NULL OR quarantined_at < ?) "
            "AND needs_review=0 AND favorite=0 AND blacklisted=0",
            (cutoff,))
        n = 0
        from .safe_delete import is_software_file
        _cfg = None
        try:
            from .config import get_active_config
            _cfg = get_active_config()
        except Exception:
            _cfg = None
        for r in rows:
            p = r["path"]
            if p:
                try:
                    from pathlib import Path as _P
                    # V0.0.5：F 盘等原有图库的文件**一律不物理删** ——
                    # 「不喜欢」在保护口径下只打隔离标记、文件留在原地，
                    # 这里 unlink 会正好删到原图。清 DB 行就够了。
                    # 拿不到配置（_cfg=None）时也保守处理：只清登记。
                    if _cfg is not None and is_software_file(p, _cfg):
                        _P(p).unlink(missing_ok=True)
                except Exception:
                    pass
            # R221/C10：FK 开启后子表先清（reviews 引用 images），
            # 否则隔离区自动清理会整批撞外键。
            self.execute("DELETE FROM reviews WHERE image_id=?", (r["id"],))
            self.execute("DELETE FROM images WHERE id=?", (r["id"],))
            n += 1
        return n
