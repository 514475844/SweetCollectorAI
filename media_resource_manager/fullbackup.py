"""R241 · 整库备份 / 恢复（Web 端对称桌面「数据 → 导出完整备份」）。

订单出处：``docs/r239_proxy_page_20261028.md`` 遗留项 ——
桌面端「数据」菜单里有「导出完整备份（配置+库+规则+缩略图）/ 从完整备份恢复」，
R232 的 Web 版只给了**数据库单库快照**（``/api/backup``），换机时
配置、规则、缩略图三样拿不回来；图库能打开但缩略图全丢、规则全丢。

为什么和 R232 分成两层：
* R232 = 「每天一份的库快照」，目的是**误删之后倒回去**，轻、快、自动；
* 本模块 = 「换电脑 / 重装 / 留档」的一次性整包，重（几百 MB 很正常），
  必须用户手点，且恢复是**重操作**（要有进度、要能中途看状态）。
两者共用同一个 ``backups`` 目录，但文件名前缀不同（``mrm_db_backup_`` vs
``mrm_full_``），所以清理逻辑互不影响。

⚠ 恢复为什么走「就地合并」而不是桌面端那套「关库 → 解压覆盖 → 重开库」：
桌面是**单进程独占**，关库没人再读；Web 端与桌面/引擎**同进程共享一个
Database 对象**（``server.db``、``queue`` 手里的还是同一份引用）。真关库再
重开，桌面 GUI 手上那个引用就永久失效了 —— 网页上点一次恢复，桌面端整个
变成「库已关闭」，直到重启软件。所以这里改成：把备份库 **ATTACH** 进来，
在一本事务里逐表「清空 + 灌入」，连接始终不关，桌面/引擎不受影响。
（副作用：恢复后 ``images.id`` 与备份里一致，``reviews`` 等外键依然对得上。）

⚠ 图片本体**不在**备份包里：图在各任务输出目录（用户自己选的盘），
包里只有「配置 + 数据库 + 规则 + 缩略图」。缩略图带上是为了恢复后图库
立刻有缩略、不用全库重算。桌面端导出的包是同一个口径。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

#: 备份文件名：``mrm_full_20261028_235900.zip``
FULL_PREFIX = "mrm_full_"
FULL_SUFFIX = ".zip"
#: 包内清单文件名（恢复时先读它，认不出就拒绝，别把普通 zip 当备份解）。
MANIFEST_NAME = "MRM_BACKUP.json"
KIND = "mrm_full_backup"
#: 清单版本（以后包结构变了按它分叉）。
VERSION = 1
#: 包内除数据库外固定携带的顶层项（白名单，防止把任意文件解到数据目录）。
FIXED_TOP = (MANIFEST_NAME, "config.json", "rules", "thumbs")
#: 上传上限（4 GB）：整包通常几百 MB，4 GB 是「明显异常」的边界。
MAX_UPLOAD = 4 * 1024 * 1024 * 1024
#: 单个成员的解压上限（防 zip bomb）：比 4 GB 再宽一点。
MAX_MEMBER = 6 * 1024 * 1024 * 1024
#: 成员数量上限（缩略图可能几万张，20 万足够，再多说明不是我们的包）。
MAX_MEMBERS = 200000

#: 恢复时**绝不覆盖**的配置键。
#:
#: 理由分三类，都不是「保守」而是「覆盖了当场出事」：
#:   · ``db_path`` —— 覆盖后内存里的 Database 还连着老路径，重启才发现错位；
#:   · ``web_*`` 监听项 —— 端口/绑定地址一改，当前这个网页**立刻连不上**
#:     （改了端口要重启才生效，而重启是用户的动作，不能由一个恢复按钮代做）；
#:   · ``portable`` 等启动期开关 —— 同属「重启才生效」，静默改掉等于让
#:     用户下次启动时发现数据目录换了个地方。
CONFIG_BLOCKED = frozenset({
    "db_path", "web_port", "web_bind", "web_https", "web_https_port",
    "web_https_redirect", "web_cert_file", "web_key_file", "portable",
    "trust_proxy", "web_trust_lan",
})


class RestoreError(ValueError):
    """备份包不合格（中文消息直接给用户看）。"""


# --------------------------------------------------------------------------
# 命名 / 清单
# --------------------------------------------------------------------------
def full_name(now: Optional[datetime] = None) -> str:
    """整包备份文件名（精确到秒，同一天可以留多份）。"""
    return "%s%s%s" % (FULL_PREFIX, (now or datetime.now()).strftime("%Y%m%d_%H%M%S"),
                       FULL_SUFFIX)


def full_path(base: Any, now: Optional[datetime] = None) -> Path:
    """整包备份的落盘路径（与 R232 的备份目录同一个）。"""
    from .data_backup import backup_dir
    return backup_dir(base) / full_name(now)


def is_full_name(name: str) -> bool:
    """是不是整包备份文件名（``mrm_full_`` + 15 位时间 + ``.zip``）。"""
    n = str(name or "")
    body = n[len(FULL_PREFIX):-len(FULL_SUFFIX)] if (
        n.startswith(FULL_PREFIX) and n.endswith(FULL_SUFFIX)) else ""
    return len(body) == 15 and body[:8].isdigit() and body[8] == "_" \
        and body[9:].isdigit()


def prune_full(names: Iterable[str], keep: Any) -> list:
    """按文件名升序保留最近 ``keep`` 份，返回该删的（``keep<=0`` = 不清理）。"""
    try:
        k = int(keep or 0)
    except (TypeError, ValueError):
        k = 0
    if k <= 0:
        return []
    seq = sorted(str(n) for n in names if is_full_name(str(n)))
    if len(seq) <= k:
        return []
    return seq[:len(seq) - k]


#: 整包备份的自动清理默认值（可在设置页改）。
#:   keep  = 最多留几份；days = 超过几天就清；max_gb = 总量上限（0 = 不限）
PRUNE_DEFAULTS = {"keep": 5, "days": 30, "max_gb": 8}


def _full_ts(name: str) -> Optional[datetime]:
    """从整包备份文件名里取时间；不是整包备份就返回 None。"""
    if not is_full_name(name):
        return None
    body = str(name)[len(FULL_PREFIX):-len(FULL_SUFFIX)]
    try:
        return datetime.strptime(body, "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def prune_plan(entries, keep: int = 5, days: int = 30, max_bytes: int = 0,
               now: Optional[datetime] = None) -> list:
    """算出该删哪些整包备份，返回**文件名列表**（新的保留）。

    ``entries`` 可以是文件名列表，也可以是 ``(name, size)`` 二元组列表
    （有 size 才能算总量）。

    三条规则同时生效，取并集：
      · 份数：只留最近 ``keep`` 份；
      · 天数：早于 ``days`` 天的清掉；
      · 总量：从新到旧累加，超 ``max_bytes`` 的清掉（0 = 不限）。

    ⚠ **永远至少留最新一份**。用户把「保留份数」填成 0 的本意通常是
    「不想自动删」，而不是「一份都别留」；而天数/总量规则如果在磁盘上只有
    一份时也照删，就等于把唯一的退路删了。所以最后统一兜一次底。
    """
    now = now or datetime.now()
    rows = []
    for e in entries or []:
        if isinstance(e, (tuple, list)) and len(e) >= 2:
            name, size = str(e[0]), int(e[1] or 0)
        else:
            name, size = str(e), 0
        ts = _full_ts(name)
        if ts is None:
            continue
        rows.append((name, ts, size))
    rows.sort(key=lambda r: r[1])              # 旧 → 新
    if not rows:
        return []

    drop = set()
    try:
        k = int(keep or 0)
    except (TypeError, ValueError):
        k = 0
    if k > 0 and len(rows) > k:
        for name, _ts, _sz in rows[:len(rows) - k]:
            drop.add(name)

    try:
        d = int(days or 0)
    except (TypeError, ValueError):
        d = 0
    if d > 0:
        cutoff = now - timedelta(days=d)
        for name, ts, _sz in rows:
            if ts < cutoff:
                drop.add(name)

    if max_bytes and max_bytes > 0:
        # 只在「份数/天数」都删完之后剩下的那些里挑，且**从最旧的开始删**。
        # 第一版是从新到旧累加，最新的那份也被算进「该删」再被兜底救回来，
        # 结果总量超出上限（实测 7×1000B 想压到 3000B，实际留了 4000B）。
        left = [r for r in rows if r[0] not in drop]     # 旧 → 新
        total = sum(sz for _n, _t, sz in left)
        for name, _ts, sz in left[:-1]:                  # 最新的那份不参与
            if total <= max_bytes:
                break
            drop.add(name)
            total -= sz

    newest = rows[-1][0]
    drop.discard(newest)
    # 保持「按名字升序」返回，方便调用方按顺序删（日志读起来也顺）
    return sorted(drop)


def prune_and_delete(directory, keep: int = 5, days: int = 30,
                     max_bytes: int = 0) -> dict:
    """按策略删掉旧整包备份，返回 ``{"deleted": [...], "bytes": n}``。

    删除失败逐个吞掉（文件可能正被别的程序打开）；调用方不该因为
    「旧备份没删掉」而让整个备份流程失败。
    """
    d = Path(directory)
    if not d.is_dir():
        return {"deleted": [], "bytes": 0}
    entries = []
    for p in d.iterdir():
        try:
            if p.is_file() and is_full_name(p.name):
                entries.append((p.name, p.stat().st_size))
        except OSError:
            continue
    plan = prune_plan(entries, keep=keep, days=days, max_bytes=max_bytes)
    gone, freed = [], 0
    sizes = dict(entries)
    for name in plan:
        try:
            (d / name).unlink()
            gone.append(name)
            freed += int(sizes.get(name) or 0)
        except OSError:
            continue
    return {"deleted": gone, "bytes": freed}


def manifest_payload(*, db_rel: str, db_encrypted: bool, counts: dict,
                     version: str = "", items: int = 0,
                     include_thumbs: bool = True,
                     now: Optional[datetime] = None) -> dict:
    """包内清单（不放任何密钥/口令）。"""
    return {
        "kind": KIND,
        "version": VERSION,
        "created_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "app_version": str(version or ""),
        "db": db_rel,
        "db_encrypted": bool(db_encrypted),
        "has_config": True,
        "has_rules": True,
        "has_thumbs": bool(include_thumbs),
        "counts": dict(counts or {}),
        "items": int(items or 0),
    }


def read_manifest(zip_path: Any) -> dict:
    """读包内清单；不是本工具的整包备份就抛 :class:`RestoreError`。"""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            try:
                raw = zf.read(MANIFEST_NAME)
            except KeyError:
                raise RestoreError(
                    "这不是完整备份包：包里没有 MRM_BACKUP.json 清单。"
                    "（数据库单库备份要走的不是这个恢复入口）")
    except zipfile.BadZipFile:
        raise RestoreError("文件不是有效的 ZIP 包（可能没上传完或已损坏）")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise RestoreError("备份包里的清单读不出来（文件已损坏）")
    if not isinstance(data, dict) or data.get("kind") != KIND:
        raise RestoreError("这不是本软件的完整备份包")
    try:
        ver = int(data.get("version") or 0)
    except (TypeError, ValueError):
        ver = 0
    if ver > VERSION:
        raise RestoreError(
            "备份包来自更新版本的软件（包版本 %d），当前版本只认到 %d，"
            "请先升级软件再恢复。" % (ver, VERSION))
    return data


def inspect_zip(zip_path: Any) -> dict:
    """给界面做「恢复前预览」：清单 + 包的实际大小/条目数。"""
    p = Path(zip_path)
    man = read_manifest(p)
    with zipfile.ZipFile(p) as zf:
        infos = zf.infolist()
        raw = sum(int(i.file_size or 0) for i in infos)
    man["bytes"] = p.stat().st_size
    man["uncompressed"] = raw
    man["entries"] = len(infos)
    return man


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------
def _rel_db(base: Path, db: Any) -> str:
    """数据库在包内的相对路径（能在数据目录里就按真实相对路径存）。"""
    try:
        p = Path(getattr(db, "db_path", base / "mrm.db"))
    except Exception:
        p = base / "mrm.db"
    try:
        return str(p.resolve().relative_to(base.resolve())).replace("\\", "/")
    except Exception:
        return "mrm.db"


def _iter_files(root: Path) -> list:
    try:
        return sorted(f for f in root.rglob("*") if f.is_file())
    except OSError:
        return []


def build_export(dest: Any, base: Any, db: Any, *,
                 version: str = "", include_thumbs: bool = True,
                 extra: Optional[dict] = None,
                 progress: Optional[Callable[[int, str], None]] = None) -> dict:
    """把「配置 + 库快照 + 规则 + 缩略图」打成一个 zip。

    ``db`` 走 ``Database.backup()``（**sqlite 在线快照**，不是 copy 文件）：
    WAL 模式下没并回主库的事务全在 ``-wal`` 里，直接 copy 出去的是旧数据，
    恢复后表现为「最近几小时白采了」（R152-A6 在桌面端已经踩过一次）。

    加密态：``db.backup()`` 写出来的是加密包，清单里 ``db_encrypted=true``，
    恢复时需要同一个主密码。**包里绝不放明文库、也不放主密码。**

    返回 ``{name, bytes, items, counts, manifest}``；失败抛 ``OSError``/``sqlite3.Error``。
    """
    base = Path(base)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="mrm_full_"))
    snap = tmpdir / "mrm_snapshot.db"
    try:
        if progress:
            progress(2, "正在做数据库在线快照…")
        db.backup(snap)
        db_rel = _rel_db(base, db)
        try:
            counts = {
                "tasks": int(db.query(
                    "SELECT COUNT(*) c FROM tasks WHERE is_template=0")[0]["c"]),
                "images": int(db.query("SELECT COUNT(*) c FROM images")[0]["c"]),
                "preferences": int(db.query(
                    "SELECT COUNT(*) c FROM preferences")[0]["c"]),
            }
        except Exception:
            counts = {}

        rules_dir = base / "rules"
        thumbs_dir = base / "thumbs"
        rules = _iter_files(rules_dir)
        thumbs = _iter_files(thumbs_dir) if include_thumbs else []

        items = 2 + len(rules) + len(thumbs)      # 配置 + 库
        man = manifest_payload(db_rel=db_rel,
                               db_encrypted=bool(getattr(db, "_encrypted", False)),
                               counts=counts, version=version, items=items,
                               include_thumbs=include_thumbs)
        if extra:
            man.update({k: v for k, v in extra.items() if k not in man})

        if progress:
            progress(6, "正在写入配置与数据库…")
        written = 0
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr(MANIFEST_NAME,
                        json.dumps(man, ensure_ascii=False, indent=2))
            cfg_file = base / "config.json"
            if cfg_file.is_file():
                zf.write(cfg_file, "config.json")
                written += 1
            zf.write(snap, db_rel)
            written += 1

            total = len(rules) + len(thumbs)
            done = 0
            for f in rules:
                zf.write(f, "rules/" + str(f.relative_to(rules_dir))
                         .replace("\\", "/"))
                done += 1
                if progress and total and done % 50 == 0:
                    progress(6 + int(88 * done / total), "正在写入规则与缩略图…")
            for f in thumbs:
                zf.write(f, "thumbs/" + str(f.relative_to(thumbs_dir))
                         .replace("\\", "/"))
                done += 1
                if progress and total and done % 200 == 0:
                    progress(6 + int(88 * done / total), "正在写入规则与缩略图…")
        if progress:
            progress(96, "正在收尾…")
        size = dest.stat().st_size
        return {"name": dest.name, "bytes": size, "items": written + len(rules) + len(thumbs),
                "counts": counts, "manifest": man}
    except BaseException:
        # 半截包比没有包更坏：恢复时能读清单、解开一半才发现缺库。
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------
# 读取 / 校验
# --------------------------------------------------------------------------
def _safe_members(zf: zipfile.ZipFile, allowed_top: set) -> list:
    """逐条校验成员路径，返回可安全解压的 Info 列表。

    拒收四类（全部是真实攻击面，不是洁癖）：
      * 绝对路径 / 盘符（``C:\\x``、``/etc/x``）—— ``extract`` 会写到数据目录外；
      * ``..`` 穿越 —— 同上；
      * 顶层项不在白名单 —— 别让手工塞进来的文件散到数据目录；
      * 符号链接（``external_attr`` 高位标记）—— 解出来是指向外面的链接。
    """
    out, skip = [], 0
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if info.is_dir():
            continue
        if name.startswith("/") or ":" in name.split("/")[0]:
            raise RestoreError("备份包里有非法路径（%s），已中止恢复" % name[:60])
        parts = [p for p in name.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise RestoreError("备份包里有越权路径（%s），已中止恢复" % name[:60])
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise RestoreError("备份包里有符号链接（%s），已中止恢复" % name[:60])
        if int(info.file_size or 0) > MAX_MEMBER:
            raise RestoreError("备份包里的单个文件超过 6 GB，已中止恢复")
        top = parts[0] if parts else ""
        if top not in allowed_top:
            skip += 1
            continue
        out.append(info)
    if len(zf.infolist()) > MAX_MEMBERS:
        raise RestoreError("备份包里的文件条目超过 20 万，已中止恢复")
    return out


def extract_parts(zip_path: Any, tmpdir: Any, man: dict) -> dict:
    """把包里的部件解到临时目录，返回各部件的本地路径。

    返回 ``{"config": Path|None, "db": Path|None, "rules": Path|None,
    "thumbs": Path|None, "skipped": int}``。
    """
    tmpdir = Path(tmpdir)
    db_rel = str(man.get("db") or "mrm.db").replace("\\", "/")
    db_top = db_rel.split("/")[0]
    allowed = set(FIXED_TOP) | {db_top}
    with zipfile.ZipFile(zip_path) as zf:
        members = _safe_members(zf, allowed)
        skipped = len([i for i in zf.infolist() if not i.is_dir()]) - len(members)
        for info in members:
            zf.extract(info, tmpdir)
    p_config = tmpdir / "config.json"
    p_db = tmpdir / db_rel
    p_rules = tmpdir / "rules"
    p_thumbs = tmpdir / "thumbs"
    return {"config": p_config if p_config.is_file() else None,
            "db": p_db if p_db.is_file() else None,
            "rules": p_rules if p_rules.is_dir() else None,
            "thumbs": p_thumbs if p_thumbs.is_dir() else None,
            "skipped": max(0, skipped)}


def _decrypt_db_if_needed(src: Path, db: Any, tmpdir: Path) -> Path:
    """备份里的库是加密包 → 用当前主密码解成明文临时库再 ATTACH。

    ⚠ 加密包的密文**不是 sqlite 文件**，``ATTACH`` 会直接报
    「file is not a database」。这里先看魔数，不是明文库就必须解。
    """
    head = b""
    try:
        with open(src, "rb") as fh:
            head = fh.read(16)
    except OSError as e:
        raise RestoreError("备份里的数据库读不出来：%s" % e)
    if head.startswith(b"SQLite format 3\x00"):
        return src
    pw = getattr(db, "_master_password", "") or ""
    if not pw:
        raise RestoreError(
            "备份里的数据库是加密的，但当前进程没有主密码，无法恢复。"
            "请在桌面端用相同主密码恢复，或用 MRM_DB_PASSWORD 启动本服务。")
    try:
        from . import crypt as _crypt
        plain = _crypt.decrypt_bytes(src.read_bytes(), pw)
    except Exception:
        raise RestoreError(
            "备份里的数据库解密失败：主密码与备份时的密码不一致。")
    if not plain.startswith(b"SQLite format 3\x00"):
        raise RestoreError("备份里的数据库解密后仍不是有效数据库（备份已损坏）")
    out = tmpdir / "restore_plain.db"
    out.write_bytes(plain)
    return out


# --------------------------------------------------------------------------
# 应用（文件部分）
# --------------------------------------------------------------------------
def _copy_tree(src: Path, dst: Path, *, replace: bool,
               progress: Optional[Callable[[int, str], None]] = None,
               pct_from: int = 0, pct_to: int = 0,
               label: str = "") -> int:
    """把 ``src`` 下的文件覆盖到 ``dst``；``replace`` 时先把 dst 清空。

    ``replace`` 只用于规则目录：备份里的规则集就是「那一刻的规则集」，
    不清空会留下备份里已删除的旧规则（用户看到「删掉的规则又回来了」）。
    缩略图反过来 —— 用**覆盖**不清空：清空等于把备份之后新采的图的缩略图
    一起删掉，图库瞬间全灰。
    """
    dst.mkdir(parents=True, exist_ok=True)
    if replace:
        for f in dst.glob("*"):
            try:
                if f.is_dir():
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    f.unlink()
            except OSError:
                pass
    files = _iter_files(src)
    n = 0
    for i, f in enumerate(files, 1):
        rel = f.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(f, target)
            n += 1
        except OSError:
            continue
        if progress and files and i % 100 == 0:
            progress(pct_from + int((pct_to - pct_from) * i / len(files)),
                     "%s（%d/%d）…" % (label, i, len(files)))
    return n


def merge_config(cfg: Any, config_file: Path, save: Callable[[], Any],
                 blocked: Iterable[str] = CONFIG_BLOCKED) -> dict:
    """把备份里的配置**逐键合并**进当前 cfg（不动监听/路径类字段）。

    返回 ``{applied:[...], blocked:[...], unknown:[...]}`` 供界面汇报
    「哪些恢复回来了、哪些为了不打断当前服务故意跳过」。
    """
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception:
        return {"applied": [], "blocked": [], "unknown": [], "error": "配置文件读不出来"}
    if not isinstance(data, dict):
        return {"applied": [], "blocked": [], "unknown": [], "error": "配置文件格式不对"}
    blocked = set(blocked)
    applied, skipped, unknown = [], [], []
    for k, v in data.items():
        if k in blocked:
            skipped.append(k)
            continue
        if not hasattr(cfg, k):
            unknown.append(k)
            continue
        try:
            cur = getattr(cfg, k)
            # 路径类字段（db_path/library_dir/...）在原对象里是 Path，
            # 直接塞字符串会让后面 `.mkdir()` 崩，按现有类型转一道。
            if isinstance(cur, Path) and isinstance(v, str):
                v = Path(v)
            setattr(cfg, k, v)
            applied.append(k)
        except Exception:
            unknown.append(k)
    if applied:
        try:
            save()
        except Exception:
            pass
    return {"applied": applied, "blocked": skipped, "unknown": unknown}


def counts_after(db: Any) -> dict:
    """恢复后的库内数量（界面用来确认「确实恢复进来了」）。"""
    out = {}
    for k, sql in (("tasks", "SELECT COUNT(*) c FROM tasks WHERE is_template=0"),
                   ("images", "SELECT COUNT(*) c FROM images"),
                   ("preferences", "SELECT COUNT(*) c FROM preferences"),
                   ("rules", "SELECT COUNT(*) c FROM rules")):
        try:
            out[k] = int(db.query(sql)[0]["c"])
        except Exception:
            out[k] = 0
    return out


def quick_integrity(db: Any) -> str:
    """恢复后自检（``quick_check`` 比 ``integrity_check`` 快一个量级）。

    返回 ``"ok"`` 或第一行异常文本（给用户看的中文提示由调用方拼）。
    """
    try:
        rows = db.query("PRAGMA quick_check")
        vals = [str(r[0]) for r in rows or []]
        return vals[0] if vals else "ok"
    except Exception as e:
        return "自检失败：%s" % e
