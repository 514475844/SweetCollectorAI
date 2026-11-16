"""Thin CRUD helpers over the Database for tasks, images, reviews, prefs."""
from __future__ import annotations

import json
from typing import Optional

from .db import Database
from .models import ImageRecord, TaskConfig, TaskStatus


def _jdump(x) -> str:
    return json.dumps(x, ensure_ascii=False)


def _num(v, default: int = 0) -> int:
    """把任务字段安全地转成 int。

    TaskConfig 的字段默认值都给齐了，但导入/克隆/测试里很容易凑出半个任务
    （某个字段是 None），`int(None)` 会直接把「保存任务」打成异常。统一兜一层。

    ⚠ **只用于真正的 int 字段**。delay / ai_threshold 是 float，套这里会静默
    丢精度（2.75 → 2、0.42 → 0），用户设的值回读变成另一个值 —— 那叫
    静默丢用户数据。见下面的 `_flt`。
    """
    try:
        return int(v if v is not None else default)
    except (TypeError, ValueError):
        return default


def _flt(v, default: float = 0.0) -> float:
    """float 版转换：delay / ai_threshold 必须走它，别走 `_num`。

    #8 生产测试报告实测：`delay=2.75` 存进去读出来是 `2.0`，
    `ai_threshold=0.42` 直接变 `0.0`（int(0.42) 截断）—— 用户调半天阈值，
    保存后悄悄回到默认值，且 UI 显示正常，极难察觉。
    """
    try:
        return float(v) if v is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def _status_value(t) -> str:
    try:
        return t.status.value
    except Exception:
        return "idle"


def save_task(db: Database, t: TaskConfig) -> int:
    if t.id is None:
        cols = ["name", "adapter_name", "entry_urls", "keywords", "blacklist",
                "proxy", "proxy_id", "use_proxy", "concurrency", "max_depth",
                "delay", "output_dir", "auto_run", "status", "is_template",
                "parent_id", "priority", "ai_enabled", "ai_labels",
                "ai_blacklist", "ai_threshold", "ai_match_mode",
                "ai_exclude_match_mode", "min_width", "min_height",
                "min_bytes", "max_bytes", "schedule_at",
                "rate_limit", "allowed_hours", "max_pages",
                "ai_labels_l2", "ai_labels_l3", "ai_label_strict",
                "ai_label_levels", "ai_label_strict_levels",
                "keyword_subdir", "user_agent", "cookie"]
        vals = (t.name, t.adapter_name, _jdump(t.entry_urls), _jdump(t.keywords),
                _jdump(t.blacklist), t.proxy, t.proxy_id, _num(t.use_proxy),
                _num(t.concurrency), _num(t.max_depth), _flt(t.delay),
                t.output_dir, _num(t.auto_run), _status_value(t),
                _num(t.is_template), t.parent_id, _num(t.priority),
                _num(t.ai_enabled), _jdump(t.ai_labels), _jdump(t.ai_blacklist),
                _flt(t.ai_threshold), t.ai_match_mode, t.ai_exclude_match_mode,
                _num(t.min_width), _num(t.min_height), _num(t.min_bytes),
                _num(t.max_bytes), t.schedule_at or "", _num(t.rate_limit),
                getattr(t, "allowed_hours", "") or "", _num(t.max_pages),
                _jdump(t.ai_labels_l2), _jdump(t.ai_labels_l3),
                int(bool(getattr(t, "ai_label_strict", False))),
                # 2026-10-28：无限分级（层数组 + 每层严格开关）
                _jdump(getattr(t, "ai_label_levels", []) or []),
                _jdump(getattr(t, "ai_label_strict_levels", []) or []),
                int(bool(getattr(t, "keyword_subdir", False))),
                # R173：任务级 UA 与站点 Cookie —— 以前这两项中 cookie 只在
                # 内存里（engine 的 getattr 常年读空气），已按订单正式落库。
                (getattr(t, "user_agent", "") or ""),
                (getattr(t, "cookie", "") or ""))
        cur = db.execute(
            "INSERT INTO tasks ({}) VALUES ({})".format(
                ", ".join(cols), ", ".join("?" * len(vals))), vals)
        return int(cur.lastrowid)
    db.execute(
            """UPDATE tasks SET name=?, adapter_name=?, entry_urls=?, keywords=?,
               blacklist=?, proxy=?, proxy_id=?, use_proxy=?, concurrency=?, max_depth=?,
               delay=?, output_dir=?, auto_run=?, status=?, is_template=?, parent_id=?,
               priority=?, ai_enabled=?, ai_labels=?, ai_blacklist=?, ai_threshold=?,
               ai_match_mode=?, ai_exclude_match_mode=?,
               min_width=?, min_height=?,                min_bytes=?, max_bytes=?, schedule_at=?,
               rate_limit=?, allowed_hours=?, max_pages=?, ai_labels_l2=?,
               ai_labels_l3=?, ai_label_strict=?,
               ai_label_levels=?, ai_label_strict_levels=?,
               keyword_subdir=?, last_error=?, last_run_at=?,
               user_agent=?, cookie=?,
               updated_at=datetime('now') WHERE id=?""",
            (t.name, t.adapter_name, _jdump(t.entry_urls), _jdump(t.keywords),
             _jdump(t.blacklist), t.proxy, t.proxy_id, _num(t.use_proxy),
             _num(t.concurrency), _num(t.max_depth), _flt(t.delay),
             t.output_dir, _num(t.auto_run), _status_value(t),
             _num(t.is_template), t.parent_id, _num(t.priority),
             _num(t.ai_enabled), _jdump(t.ai_labels), _jdump(t.ai_blacklist),
             _flt(t.ai_threshold), t.ai_match_mode, t.ai_exclude_match_mode,
             _num(t.min_width), _num(t.min_height), _num(t.min_bytes),
             _num(t.max_bytes), t.schedule_at or "", _num(t.rate_limit),
             getattr(t, "allowed_hours", "") or "", _num(t.max_pages),
             _jdump(t.ai_labels_l2), _jdump(t.ai_labels_l3),
             int(bool(getattr(t, "ai_label_strict", False))),
             _jdump(getattr(t, "ai_label_levels", []) or []),
             _jdump(getattr(t, "ai_label_strict_levels", []) or []),
             int(bool(getattr(t, "keyword_subdir", False))),
             # r38：以前这两列没进 UPDATE，_set_status 里 `t.last_error = err`
             # 只改了内存对象 → 任务列表的「最后错误 / 最后运行」永远是空；
             # 用户点「全部开始」后 16 站齐刷刷变 error，却一个原因都看不到。
             (t.last_error or "")[:500], (t.last_run_at or ""),
             # R173：任务级 UA / Cookie 正式落库（列位置在 last_run_at 之后、
             # id 之前，参数顺序必须与之逐一对应，别插错位）。
             (getattr(t, "user_agent", "") or ""),
             (getattr(t, "cookie", "") or ""), t.id),
        )
    return t.id


def _task_from_row(r) -> TaskConfig:
    def maybe(key, cast):
        try:
            return cast(r[key])
        except (IndexError, KeyError, TypeError):
            return cast({}.get(key, ""))
    return TaskConfig(
        id=r["id"], name=r["name"], adapter_name=r["adapter_name"],
        entry_urls=json.loads(r["entry_urls"]), keywords=json.loads(r["keywords"]),
        blacklist=json.loads(r["blacklist"]), proxy=r["proxy"],
        use_proxy=bool(r["use_proxy"]), concurrency=r["concurrency"],
        max_depth=r["max_depth"], delay=r["delay"], output_dir=r["output_dir"],
        auto_run=bool(r["auto_run"]), status=TaskStatus(r["status"]),
        is_template=bool(r["is_template"]), parent_id=r["parent_id"],
        proxy_id=r["proxy_id"] if "proxy_id" in r.keys() else None,
        priority=r["priority"] if "priority" in r.keys() else 5,
        ai_enabled=bool(r["ai_enabled"]) if "ai_enabled" in r.keys() else False,
        ai_labels=json.loads(r["ai_labels"]) if "ai_labels" in r.keys() else [],
        ai_blacklist=(json.loads(r["ai_blacklist"])
                      if "ai_blacklist" in r.keys() and r["ai_blacklist"] else []),
        ai_threshold=(r["ai_threshold"] if "ai_threshold" in r.keys()
                      and r["ai_threshold"] is not None else 0.35),
        ai_match_mode=(r["ai_match_mode"] if "ai_match_mode" in r.keys()
                       and r["ai_match_mode"] else "any"),
        ai_exclude_match_mode=(r["ai_exclude_match_mode"]
                               if "ai_exclude_match_mode" in r.keys()
                               and r["ai_exclude_match_mode"] else "any"),
        min_width=r["min_width"] if "min_width" in r.keys() else 0,
        min_height=r["min_height"] if "min_height" in r.keys() else 0,
        min_bytes=r["min_bytes"] if "min_bytes" in r.keys() else 0,
        max_bytes=r["max_bytes"] if "max_bytes" in r.keys() else 0,
        schedule_at=r["schedule_at"] if "schedule_at" in r.keys() else "",
        rate_limit=r["rate_limit"] if "rate_limit" in r.keys() else 0,
        allowed_hours=r["allowed_hours"] if "allowed_hours" in r.keys() else "",
        # 老库（还没有这一列）按 0 = 不限 处理，行为与「不设上限」一致
        max_pages=r["max_pages"] if "max_pages" in r.keys() else 0,
        ai_labels_l2=(json.loads(r["ai_labels_l2"])
                      if "ai_labels_l2" in r.keys() and r["ai_labels_l2"]
                      else []),
        ai_labels_l3=(json.loads(r["ai_labels_l3"])
                      if "ai_labels_l3" in r.keys() and r["ai_labels_l3"]
                      else []),
        ai_label_strict=(bool(r["ai_label_strict"])
                         if "ai_label_strict" in r.keys() else False),
        # 2026-10-28：无限分级。老库没有这两列 → 空数组，回退旧三列。
        ai_label_levels=(json.loads(r["ai_label_levels"])
                         if "ai_label_levels" in r.keys()
                         and r["ai_label_levels"] else []),
        ai_label_strict_levels=(json.loads(r["ai_label_strict_levels"])
                                if "ai_label_strict_levels" in r.keys()
                                and r["ai_label_strict_levels"] else []),
        # 老库还没这一列时按 False（= 站内扁平，跟以前一致）
        keyword_subdir=(bool(r["keyword_subdir"])
                         if "keyword_subdir" in r.keys() else False),
        # R173：任务级 UA / Cookie 读回（老库无列时按 ""，行为与之前一致）。
        #   user_agent 空 = 用全局 cfg.user_agent；cookie 空 = 不额外发 Cookie。
        user_agent=((r["user_agent"] or "")
                    if "user_agent" in r.keys() else ""),
        cookie=((r["cookie"] or "") if "cookie" in r.keys() else ""),
        # r38：存进去了就得读回来，否则任务列表那一列还是空的
        # R154-F16：老库/新插入行的这两列可能是 NULL（INSERT 列里没有它们），
        #   读出来统一 coalesce 成 ""，别把 None 泄给 GUI/日志格式化。
        last_error=(r["last_error"] or "") if "last_error" in r.keys() else "",
        last_run_at=(r["last_run_at"] or "") if "last_run_at" in r.keys() else "",
    )


def get_task(db: Database, task_id: int) -> Optional[TaskConfig]:
    row = db.query("SELECT * FROM tasks WHERE id=?", (task_id,))
    return _task_from_row(row[0]) if row else None


def list_tasks(db: Database, templates: bool = False) -> list[TaskConfig]:
    rows = db.query("SELECT * FROM tasks WHERE is_template=?",
                    (0 if not templates else 1,))
    return [_task_from_row(r) for r in rows]


# ---------------------------------------------------------------- L1 keyset 分页
# 图库列表默认要读的列（列表模式要显示文件名/状态/大小/AI分/任务/网站/入库时间）。
GALLERY_COLUMNS = ("id, path, source_url, tags, score, width, height, "
                   "favorite, sha256, fmt, taken_at, bytes, collected_at")

# 排序键 → SQL 表达式。keyset 分页要求「排序列 + id」构成**全序**：
# 只按 score 排时同分行之间的先后在两次查询间可能变，游标就会漏行或重行。
#
# ⚠ 排序列必须包 COALESCE。score/width/height 都可为 NULL，而 SQL 里
# `NULL < 3` 得到的是 NULL（不是 true）→ 带 NULL 的行会被游标条件整批吞掉，
# 表现为「翻到某页突然少了一大片」。这里统一把 NULL 归一成该类型的最小值，
# 语义上也对：未知分数/未知尺寸就该排在最后（倒序）或最前（正序）。
_SORT_EXPR = {
    "id": "id",
    "site": "source_url",
    "score": "COALESCE(score, 0)",
    "res": "(COALESCE(width, 0) * COALESCE(height, 0))",
}

# 与 _SORT_EXPR 一一对应的 Python 侧取值器：**两边必须同义**，
# 否则算出来的游标和库里排序用的不是同一个键，深翻页就会漏/重。
def _sort_py_id(r):
    return int(r["id"])


def _sort_py_site(r):
    return r["source_url"]


def _sort_py_score(r):
    return float(r["score"] or 0.0)


def _sort_py_res(r):
    return int(r["width"] or 0) * int(r["height"] or 0)


_SORT_PY = {
    "id": _sort_py_id,
    "site": _sort_py_site,
    "score": _sort_py_score,
    "res": _sort_py_res,
}


def list_images_page(db: Database, *, where: str = "1=1", params=(),
                     sort: str = "id", desc: bool = True,
                     cursor=None, limit: int = 200, backward: bool = False,
                     columns: str = GALLERY_COLUMNS):
    """keyset（游标）分页取一页图片。返回 ``(rows, next_cursor, has_more)``。

    **为什么不用 OFFSET**（L1 作业单）：``LIMIT n OFFSET m`` 要求 SQLite 先产出
    并丢弃前 m 行，耗时随 m 线性增长 —— 翻到第 1000 页就是把整张表扫一遍。
    keyset 只用「排序列 + id > 游标」这一个可走索引的条件，第 1000 页与第 1 页
    同量级；有新图入库时也不会像 OFFSET 那样漏图/重图。

    ``cursor`` 是上一页最后一行的 ``(排序键值, id)``，首页传 ``None``。
    ``backward=True`` 取游标**之前**的一页（往前翻），返回的行仍按显示顺序排好。

    ``next_cursor`` 为 ``None`` 表示这一侧到头了；``has_more`` 供翻页按钮定态。
    """
    expr = _SORT_EXPR.get(sort, _SORT_EXPR["id"])
    pykey = _SORT_PY.get(sort, _SORT_PY["id"])
    lim = max(1, int(limit))
    sql = [f"SELECT {columns} FROM images WHERE {where}"]
    args = list(params)
    # 游标取「排序键排在游标那一侧」的行：
    #   倒序显示（新的在前）时，下一页是排序键**更小**的那些；
    #   正序显示时反过来。往前翻则取另一侧。
    if desc:
        fwd_op, back_op = "<", ">"
    else:
        fwd_op, back_op = ">", "<"
    op = back_op if backward else fwd_op
    if cursor is not None:
        if sort == "id":
            # 排序列就是主键时不必凑复合键，直接单列比较走主键索引。
            sql.append(f"AND id {op} ?")
            args.append(int(cursor[1]))
        else:
            # ⚠ 复合游标的两个值必须**摊平成两个独立绑定**。
            # 传成 `args.append([v1, v2])`（一个 list）时 sqlite3 会当成
            # 单个绑定值，语句要 3 个绑定只给了 2 个 → ProgrammingError。
            sql.append(f"AND ({expr}, id) {op} (?, ?)")
            args.extend([cursor[0], int(cursor[1])])
    # 往前翻时按**反序**取再翻回来 —— 正序取会拿到离游标最远的一批（也就是首页），
    # 看上去有数据、其实一次跳掉了中间所有页。
    if backward:
        dir_ = "ASC" if desc else "DESC"
    else:
        dir_ = "DESC" if desc else "ASC"
    sql.append((f"ORDER BY {expr} {dir_}, id {dir_}" if sort != "id"
                else f"ORDER BY id {dir_}") + " LIMIT ?")
    args.append(lim + 1)
    # 多取一条判断还有没有下一页，不返回给调用方（省一次 COUNT）。
    rows = list(db.query(" ".join(sql), args))
    has_more = len(rows) > lim
    raw = rows[:lim]
    next_cursor = None
    if has_more and raw:
        # 游标取「查询顺序里的最后一行」——反向翻页时它就是最靠前的那行，
        # 再往前翻一次才该继续用它，所以这里必须在 reverse 之前算。
        tail = raw[-1]
        next_cursor = (pykey(tail), int(tail["id"]))
    if backward:
        raw.reverse()
    return raw, next_cursor, has_more


def images_cursor_for(row, sort: str = "id"):
    """从一行记录算出它的 keyset 游标（供调用方回退/前进时复用）。"""
    pykey = _SORT_PY.get(sort, _SORT_PY["id"])
    return (pykey(row), int(row["id"]))


def upsert_image(db: Database, rec: ImageRecord) -> int:
    # R221/C10：整本账包进 BEGIN IMMEDIATE。「先查有没有 → 再 UPDATE/INSERT」
    # 是多步写，并发下两个线程可能都查到「没有」然后各写各的（后一个撞
    # UNIQUE(task_id, source_url) 抛异常）。包成事务后第二个线程等锁，提交后
    # 它自然看到已有行走 UPDATE 分支；中途异常整体 ROLLBACK，不留半行。
    with db.transaction():
        existing = db.query(
            "SELECT id FROM images WHERE task_id=? AND source_url=?",
            (rec.task_id, rec.source_url),
        )
        if not existing and rec.path:
            # 同一磁盘文件只允许一行：下载行的 source_url 是 http 地址，
            # 回填等按路径入库的调用若不查 path，会把同一文件灌成第二行（图库重复）。
            existing = db.query(
                "SELECT id FROM images WHERE path=?", (rec.path,))
        tags = _jdump(rec.tags)
        if existing:
            iid = int(existing[0]["id"])
            # V0.0.8：送审原因是「为什么进审核」的用户可读说明，只有
            # 送审路径会写；其他路径（回填刷新、打标等）upsert 同一行时
            # 传的是空串 ⇒ 空串**不覆盖**已有原因，否则图会被反复抹掉理由。
            db.execute(
                """UPDATE images SET sha256=?, phash=?, width=?, height=?, bytes=?,
                   path=?, tags=?, score=?, blacklisted=?, in_quarantine=?, rating=?,
                   favorite=?, needs_review=?, fmt=?, taken_at=?,
                   review_reason=CASE WHEN ?='' THEN COALESCE(review_reason,'') ELSE ? END
                   WHERE id=?""",
                (rec.sha256, rec.phash, rec.width, rec.height, rec.bytes,
                 rec.path, tags, rec.score, int(rec.blacklisted),
                 int(rec.in_quarantine), rec.rating, int(rec.favorite),
                 int(rec.needs_review), rec.fmt, rec.taken_at,
                 rec.review_reason or "", rec.review_reason or "", iid),
            )
            return iid
        cur = db.execute(
            """INSERT INTO images
               (task_id, source_url, sha256, phash, width, height, bytes, path,
                tags, score, blacklisted, in_quarantine, rating, favorite,
                needs_review, fmt, taken_at, review_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec.task_id, rec.source_url, rec.sha256, rec.phash, rec.width,
             rec.height, rec.bytes, rec.path, tags, rec.score,
             int(rec.blacklisted), int(rec.in_quarantine), rec.rating,
             int(rec.favorite), int(rec.needs_review), rec.fmt, rec.taken_at,
             rec.review_reason or ""),
        )
        return int(cur.lastrowid)


def get_image(db: Database, image_id: int) -> Optional[ImageRecord]:
    row = db.query("SELECT * FROM images WHERE id=?", (image_id,))
    if not row:
        return None
    r = row[0]
    return ImageRecord(
        id=r["id"], source_url=r["source_url"], task_id=r["task_id"],
        sha256=r["sha256"], phash=r["phash"], width=r["width"], height=r["height"],
        bytes=r["bytes"], path=r["path"], tags=json.loads(r["tags"]),
        score=r["score"],         blacklisted=bool(r["blacklisted"]),
        in_quarantine=bool(r["in_quarantine"]), collected_at=r["collected_at"],
        rating=r["rating"], favorite=bool(r["favorite"]),
        needs_review=bool(r["needs_review"]),
        review_reason=(r["review_reason"]
                       if "review_reason" in r.keys() else "") or "",
        fmt=r["fmt"], taken_at=r["taken_at"],
    )


def find_duplicate_sha(db: Database, sha256: str, exclude_id: int) -> Optional[int]:
    row = db.query(
        "SELECT id FROM images WHERE sha256=? AND id<>? LIMIT 1",
        (sha256, exclude_id),
    )
    return int(row[0]["id"]) if row else None


# 学习记录统一存到全局任务（task_id=0）：清空任务后新建的任务也能继承，
# 真正做到「全面清理后免得重新学习」。调用方传的 task_id 仅作兼容，实际都归到 0。
GLOBAL_TASK_ID = 0

# ===================== R174：学习权重的「来源」 =====================
# 为什么必须有来源（用户原话：「这难道不是一个 bug 吗？」）：
#   模型给一张图打的标签只是**假设**，不是事实。旧逻辑把「用户不喜欢这张图」
#   直接等价于「用户不要这个标签」，于是模型一误标（红丝/美丽/可爱），
#   用户真正想要的类目就被写进 ``tasks.ai_blacklist`` 硬排除（engine 命中即删）
#   —— 软件反过来屏蔽了用户想要的东西。
#   分流之后：**只有用户亲手输入过的词**才配进硬黑名单；模型自动标签只进
#   软学习（preferences 权重），并且可以被一键清掉。
PREF_ORIGIN_USER = "user_typed"    # 用户亲手输入（不喜欢弹窗手填 / 词表桥接的原词）
PREF_ORIGIN_MODEL = "model_auto"   # 模型自动标签（含用户只是勾选、没手写的主题词）
# V0.0.4：用户把自己原有的本地图库导进来教软件的词。这批图是用户一张张
# 留下来的，等于用户亲口说「我喜欢这个」，不是机器自己猜的 —— 所以
# 「清除模型自动词」不许把它一起清掉。单独一个来源，看板上也分得清。
PREF_ORIGIN_IMPORT = "user_imported"
#: 单标签权重上限（R169 第 3 条并入本单）：误标不该一次霸榜。
#: 取 50 = 25 次「拉黑」级别的反复表态，正常学习根本到不了，
#: 只有"每张图都误标成同一个词"这种系统性错误才会顶到它。
PREF_WEIGHT_CAP = 50.0

# ===================== R169 第 3 条：防再污染 =====================
# 用户投诉的形态是「黑名单里多了我从没写过的词、权重还极高」。三道闸一起上：
#   ① 权重封顶（PREF_WEIGHT_CAP，已在上）；
#   ② 碎片词不入统计（_is_fragment_label）—— 切词残片（单字/纯符号/纯数字）
#      本来就不是"一个类别"，让它们进统计只会占着 Top-N 的位置；
#   ③ 时间衰减（decay_preferences）—— 一次误标不该永久霸榜，长期不再出现的
#      词缓慢降权，用户新近的表态自然压过它。
# ⚠ 三道闸**都只作用在模型自动词这条路上**。用户亲手填的词一条都不动：
#   "用户教过的东西不许被算法悄悄改小"是比"防污染"更高优先级的约定。

#: 碎片词：拉丁字母序列至少 2 个字符才算一个"类别"。
#: ⚠ **汉字例外**：中文信息密度高，「猫」「狗」「树」都是完整类别词，
#:   按长度砍掉它们是错的。所以门槛只对"非中日韩"的字符串生效。
PREF_MIN_LABEL_LEN = 2

#: 时间衰减：每闲置一天，权重乘这个系数。
#: 0.95 的含义 —— 第 7 天约降到 70%，第 30 天约 21%，第 60 天约 5%。
#: 取 0.95（不是 0.5 那种"一天腰斩"）是因为学习结果**平时不该被动**：
#: 用户每天在用的词 updated_at 每天刷新、永远不衰减，
#: 只有"再也没被碰过"的词才会慢慢降下来。
PREF_DECAY_FACTOR = 0.95


def _has_cjk(s: str) -> bool:
    """字符串里有没有中日韩文字（汉字/假名/谚文）。"""
    for ch in s:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF        # 中日韩统一表意文字
                or 0x3400 <= cp <= 0x4DBF  # 扩展 A
                or 0x3040 <= cp <= 0x30FF  # 平假名 / 片假名
                or 0xAC00 <= cp <= 0xD7AF):  # 谚文音节
            return True
    return False


def _is_fragment_label(label: str) -> bool:
    """碎片词判定：这东西**不像一个类别**，不该占学习统计的位置。

    真实形态（都是用户实际遇到的）：
      · 单个拉丁字母 —— "a"、"x"，CLIP 切词残片
      · 纯符号       —— "??"、"..."、"//"
      · 纯数字       —— "123"（分辨率、编号被当成标签）

    返回 True = 碎片（模型自动词遇到它就跳过）。

    ⚠ 两条刻意的"放过"：
      ① **单个汉字放行**（"猫"/"狗"/"树"都是完整类别词）。长度门槛只
         对非中日韩字符串生效 —— 用 len>=2 一刀切会把中文词全砍掉。
      ② **只用于模型自动词**（``PREF_ORIGIN_MODEL``）。用户手填的词
         （哪怕只有一两个字符）照样要学 ——"绝不丢用户填的词"是硬约束，
         优先级高于这里的过滤。
    """
    s = str(label or "").strip()
    if not s:
        return True
    # 纯符号：一个字母数字都没有（含全角标点、emoji）
    if not any(ch.isalnum() for ch in s):
        return True
    # 纯数字：没有任何字母/汉字
    if all(ch.isdigit() or ch.isspace() for ch in s):
        return True
    # 含中日韩文字：长度不设门槛（"猫"是合法类别词）
    if _has_cjk(s):
        return False
    # 非中日韩：单个字符（"a"、"x"）算碎片
    return len(s) < PREF_MIN_LABEL_LEN


def _pref_origin(row) -> str:
    """取一行的来源。**取不到列时按 model_auto 处理**（保守端）。

    保守的含义：宁可让这个词暂时不该进硬黑名单，也不能让来路不明的词
    变成"用户手填"流进 ai_blacklist —— 那正是本单要堵的洞。
    """
    try:
        v = row["source"]
    except Exception:
        return PREF_ORIGIN_MODEL
    v = str(v or "").strip()
    return v or PREF_ORIGIN_MODEL


def set_preference(db: Database, task_id: int, label: str, weight: float,
                   seen: int = 1, kind: str | None = None,
                   origin: str = PREF_ORIGIN_MODEL):
    """记一条学习权重。

    kind : "pos" = 主题词/喜欢方向命中，"neg" = 排除词/不喜欢（拉黑）方向命中，
           None = 只记权重、不算命中（比如导入偏好）。#37/D3：权重明细要把
           「主题词命中 / 排除词命中」分成两列，所以这里必须知道方向。

    origin : R174 起每条权重都带来源（``PREF_ORIGIN_USER`` / ``PREF_ORIGIN_MODEL``）。
           ⚠ **默认取 model_auto，不是 user_typed**：默认落在保守那一端。
           若默认是"用户手填"，以后任何忘了传来源的新调用点都会悄悄把模型
           标签升级成用户词、合法地流进硬黑名单 —— 本单堵的洞会换个地方继续漏。

    ⚠ R169 的**碎片词过滤故意不在这里**：本函数是**低层写入器**（导入偏好、
      权重明细手工改、测试造数据都走它），它该忠实写下来什么就是什么。
      "别让模型噪声进统计"这条规则属于**模型学习入口**，落在
      ``PreferenceLearner.learn`` 里（见那里的注释）—— 放这儿会把
      上层所有调用方一起改掉行为，连"用户手动置零权重"这种正当写入都受影响。
    """
    # V0.0.3：写入侧统一归一，保证打分时查得到（见 get_preferences）
    label = _norm_label(label)
    if not label:
        return
    # 老库可能留着大小写不同的同义词行，先合并进来再 UPSERT，避免一个词
    # 被拆成两行、权重各算各的。
    try:
        dup = db.query(
            "SELECT label, weight FROM preferences "
            "WHERE task_id=? AND label<>? AND lower(label)=?",
            (GLOBAL_TASK_ID, label, label))
        for r in dup:
            w_extra = float(r["weight"] or 0.0)
            weight = float(weight or 0.0) + w_extra
            db.execute("DELETE FROM preferences WHERE task_id=? AND label=?",
                       (GLOBAL_TASK_ID, r["label"]))
    except Exception:
        pass
    if origin not in (PREF_ORIGIN_USER, PREF_ORIGIN_MODEL,
                      PREF_ORIGIN_IMPORT):
        origin = PREF_ORIGIN_MODEL
    w = float(weight or 0.0)
    w = max(-PREF_WEIGHT_CAP, min(PREF_WEIGHT_CAP, w))
    col = {"pos": "hit_pos", "neg": "hit_neg"}.get(kind or "")
    # 来源「粘」在最强的那一端：用户亲手填的、以及从用户原有图库导入学来的，
    # 都不许被后来模型样本再记一次时降级成「机器自己猜的」——否则一键
    # 「清掉机器自动词」会连用户亲口教的东西一起清掉（V0.0.6 修）。
    _src_sql = ("source=(CASE "
                "WHEN preferences.source IN (?,?) THEN preferences.source "
                "WHEN excluded.source IN (?,?) THEN excluded.source "
                "ELSE excluded.source END)")
    _src_args = (PREF_ORIGIN_USER, PREF_ORIGIN_IMPORT,
                 PREF_ORIGIN_USER, PREF_ORIGIN_IMPORT)
    # R169：每次真的记一笔就刷新 updated_at —— 时间衰减靠它判"这个词还活着吗"。
    # ⚠ 必须与 weight 在**同一条 UPSERT** 里更新：分成两条语句会出现
    #   "权重涨了但时间戳没动"的中间态（衰减下次就把新权重给削了）。
    _now_sql = "updated_at=datetime('now')"
    if col:
        db.execute(
            f"""INSERT INTO preferences(task_id, label, weight, seen, {col},
                                       source, updated_at)
                VALUES (?,?,?,?,1,?, datetime('now'))
                ON CONFLICT(task_id, label) DO UPDATE SET
                  weight=MAX(-?, MIN(?, weight+excluded.weight)),
                  seen=seen+excluded.seen,
                  {col}={col}+1, {_src_sql}, {_now_sql}""",
            (GLOBAL_TASK_ID, label, w, seen, origin,
             PREF_WEIGHT_CAP, PREF_WEIGHT_CAP, *_src_args),
        )
        return
    db.execute(
        f"""INSERT INTO preferences(task_id, label, weight, seen, source,
                                   updated_at)
            VALUES (?,?,?,?,?, datetime('now'))
            ON CONFLICT(task_id, label) DO UPDATE SET
              weight=MAX(-?, MIN(?, weight+excluded.weight)),
              seen=seen+excluded.seen, {_src_sql}, {_now_sql}""",
        (GLOBAL_TASK_ID, label, w, seen, origin,
         PREF_WEIGHT_CAP, PREF_WEIGHT_CAP, *_src_args),
    )


def negative_user_typed_labels(db: Database) -> list[str]:
    """负权 + **用户手填**的标签 —— 这是**唯一**允许进硬黑名单的来源。

    R174：模型自动标签（哪怕权重已经很负）一律不在这里返回。调用方
    ``review_tab.apply_learned_exclusions`` 靠它决定写什么进 ``ai_blacklist``。
    """
    rows = db.query("SELECT label, source FROM preferences "
                    "WHERE task_id=? AND weight<0", (GLOBAL_TASK_ID,))
    out = []
    for r in rows:
        lab = str(r["label"] or "").strip()
        if lab and _pref_origin(r) == PREF_ORIGIN_USER and lab not in out:
            out.append(lab)
    return out


def clear_model_auto_negatives(db: Database) -> int:
    """一键清「模型自动 + 负权」的学习行，并把这些词从任务硬黑名单摘掉。

    ⚠ 只清**负权**：正权那头是"用户喜欢什么"，清掉等于把用户教了几个月
    的成果一起抹了，代价远大于收益；而污染黑名单的恰恰只有负权那一半。

    ⚠ 为什么必须连 ``tasks.ai_blacklist`` 一起摘：只删 preferences 的话，
    已经写进去的词仍在引擎的排除名单里（``engine.py`` 命中即删），
    用户会看到"清完了，可它还是不给我下这类图"。

    返回清掉的条数（0 = 没有可清的）。
    """
    rows = db.query("SELECT label, source FROM preferences "
                    "WHERE task_id=? AND weight<0", (GLOBAL_TASK_ID,))
    victims = [str(r["label"]).strip() for r in rows
               if str(r["label"] or "").strip()
               and _pref_origin(r) == PREF_ORIGIN_MODEL]
    if not victims:
        return 0
    # 用户手填过的词不许顺手清掉（哪怕它同时也被模型自动记过 —— 那种行
    # 的来源已被"粘"成 user_typed，本来就不在 victims 里，这里再兜一层）
    keep = set(negative_user_typed_labels(db))
    drop = [v for v in dict.fromkeys(victims) if v not in keep]
    if not drop:
        return 0
    for lab in drop:
        db.execute("DELETE FROM preferences WHERE task_id=? AND label=?",
                   (GLOBAL_TASK_ID, lab))
    try:
        for r in db.query("SELECT id, ai_blacklist FROM tasks"):
            try:
                cur = json.loads(r["ai_blacklist"] or "[]")
            except Exception:
                continue
            if not isinstance(cur, list):
                continue
            new = [w for w in cur if str(w) not in drop]
            if len(new) != len(cur):
                db.execute("UPDATE tasks SET ai_blacklist=? WHERE id=?",
                           (json.dumps(new, ensure_ascii=False), r["id"]))
    except Exception:
        pass        # 摘黑名单失败不影响"权重已清掉"这个主要结果
    return len(drop)


def _norm_label(label) -> str:
    """偏好词的统一键：去空白 + 小写。

    V0.0.3 修「学了但不生效」：写入侧（split_tags / 学习入口）会把标签转
    小写，打分侧却拿原始大小写去字典里查，中文或含大写的词永远匹配不上，
    权重写了等于没写。现在读写两侧共用这一个归一函数。
    """
    return str(label or "").strip().lower()


def get_preferences(db: Database, task_id: int = GLOBAL_TASK_ID) -> dict[str, float]:
    rows = db.query("SELECT label, weight FROM preferences WHERE task_id=?",
                    (GLOBAL_TASK_ID,))
    out: dict[str, float] = {}
    for r in rows:
        k = _norm_label(r["label"])
        if k:
            # 老库里可能同时存在 'White' 和 'white' 两行，合并而不是互相覆盖
            out[k] = out.get(k, 0.0) + float(r["weight"] or 0.0)
    return out


def set_preference_weight(db: Database, label: str, weight: float) -> bool:
    """#60：手工改写某条学习权重（**直接设为**，不是累加）。

    权重明细面板以前只有「删除选中」一条路：用户学歪了只能整条删，删完这条词
    的全部学习成果（weight/seen/hit_pos/hit_neg）一起没了，没法「调小一点」。
    这里给一条「改成多少」的精细修法——命中次数原样保留（那是历史事实，
    不该因为用户改了个数值就被抹掉）。

    返回 True=改到了；False=没这条权重（面板与库不同步时的静默失败）。
    """
    label = _norm_label(label)
    db.execute("UPDATE preferences SET weight=? WHERE task_id=? AND label=?",
               (float(weight), GLOBAL_TASK_ID, label))
    r = db.query("SELECT weight FROM preferences "
                 "WHERE task_id=? AND label=?",
                 (GLOBAL_TASK_ID, label))
    return bool(r)


def delete_preference(db: Database, label: str) -> bool:
    """#60：删单条学习权重（面板与逻辑分离，好测）。返回是否真删掉了。"""
    label = _norm_label(label)
    existed = bool(db.query("SELECT 1 FROM preferences "
                            "WHERE task_id=? AND label=?",
                            (GLOBAL_TASK_ID, label)))
    db.execute("DELETE FROM preferences WHERE task_id=? AND label=?",
               (GLOBAL_TASK_ID, label))
    return existed


# ===================== R169 第 3 条：时间衰减 + 碎片词清扫 =====================

def decay_preferences(db: Database, *, factor: float = PREF_DECAY_FACTOR,
                      now: str | None = None) -> int:
    """对**模型自动词**做时间衰减：长期不再被碰的词缓慢降权。

    为什么需要它：用户投诉「一次误标就永久霸榜」。权重封顶挡住了"冲高"，
    但没挡住"一次误标之后永远待在高位" —— 那个词再没被碰过，权重却一直
    影响打分。衰减让"还在用的词"（updated_at 天天刷新）稳稳不动，
    而"死了的词"慢慢退场。

    参数
    ----
    factor : 每闲置一天乘的系数（默认 0.95）。
    now    : 当前时间（``YYYY-MM-DD HH:MM:SS`` UTC），省略取数据库当前时间。
            显式传是为了可测（测试要造"30 天没碰过"的行）。

    返回被衰减的行数。

    ⚠ 三条铁律：
      ① **只动 model_auto**。用户亲手填的词一律不衰减 —— "用户教过的
         东西不许被算法悄悄改小"是硬约束（与 clear_model_auto_negatives
         只清负权同理：动用户的东西代价远大于收益）。
      ② ``updated_at`` 为 NULL 的老行**跳过**（不衰减）。那是迁移补列造成的
         "没有时间信息"，拿一个猜出来的时间削用户积累的词，是典型的
         "看起来对、实际伤数据"。
      ③ 权重衰减到 ``|w| < 0.5`` 时**删掉整行** —— 留着会让看板堆积一大片
         0.01 级别的噪声词，用户反而找不到真正需要处理的。
    """
    f = float(factor or PREF_DECAY_FACTOR)
    if not (0.0 < f < 1.0):
        f = PREF_DECAY_FACTOR
    if now:
        db.execute(
            "UPDATE preferences SET weight = weight * POWER(?, "
            "  CAST(julianday(?) - julianday(updated_at) AS INTEGER)) "
            "WHERE task_id=? AND source=? AND updated_at IS NOT NULL "
            "  AND julianday(?) - julianday(updated_at) >= 1",
            (f, now, GLOBAL_TASK_ID, PREF_ORIGIN_MODEL, now))
    else:
        db.execute(
            "UPDATE preferences SET weight = weight * POWER(?, "
            "  CAST(julianday('now') - julianday(updated_at) AS INTEGER)) "
            "WHERE task_id=? AND source=? AND updated_at IS NOT NULL "
            "  AND julianday('now') - julianday(updated_at) >= 1",
            (f, GLOBAL_TASK_ID, PREF_ORIGIN_MODEL))
    # 衰减后过小的行直接清掉（噪声词不该占着看板）
    db.execute("DELETE FROM preferences WHERE task_id=? AND source=? "
               "AND ABS(weight) < 0.5", (GLOBAL_TASK_ID, PREF_ORIGIN_MODEL))
    r = db.query("SELECT COUNT(*) c FROM preferences WHERE task_id=?",
                 (GLOBAL_TASK_ID,))
    return int(r[0]["c"]) if r else 0


def purge_fragment_preferences(db: Database) -> int:
    """清掉历史遗留的**模型自动碎片词**行（单字/纯符号/纯数字）。

    新写的行已由 :func:`set_preference` 挡在门外，但**老库里已经有**一批
    （用户投诉的「红丝」「美丽」那一代就是这条路上进来的）。迁移/启动时
    清一次，避免"新代码不写了、老数据还在看板上"。

    ⚠ 只清 model_auto：用户手填的单字词照留（"绝不丢用户填的词"）。
    返回清掉的条数。
    """
    rows = db.query("SELECT label, source FROM preferences WHERE task_id=?",
                    (GLOBAL_TASK_ID,))
    victims = [str(r["label"]) for r in rows
               if _pref_origin(r) == PREF_ORIGIN_MODEL
               and _is_fragment_label(r["label"])]
    for lab in dict.fromkeys(victims):
        db.execute("DELETE FROM preferences WHERE task_id=? AND label=?",
                   (GLOBAL_TASK_ID, lab))
    return len(dict.fromkeys(victims))


#: 维护任务的 app_meta 标记键。**按日期**打标记 ⇒ "每天最多跑一次"，
#: 而不是"一辈子只跑一次"（衰减本身就是要反复跑的）。
_PREF_FRAG_KEY = "pref_fragments_purged_v1"
_PREF_DECAY_KEY = "pref_decayed_on"


def reset_learning(db: Database) -> int:
    """清空**全局**学习权重（``task_id=0``）。返回清掉的条数。

    与 :func:`clear_model_auto_negatives` 的区别（**两者代价差一个量级**，
    调用方必须在文案上说清，不能混成一个按钮）：
      · 本函数 = **全清**，连用户亲手填的词与"喜欢"方向一起没，
        回到"它什么都没学过"的状态；
      · 那个函数 = 只清"机器认错攒下来的负权词"，用户教的成果都在。

    ⚠ **只清 task_id=0**：按任务存的行（task_id<>0）语义完全不同，
      一并删掉属于误伤（清理功能里这两类也是各归各的）。

    抽成 repo 函数的理由（不只是"好测"）：桌面看板与网页端用的是**同一张表**，
    两边各写一句 DELETE 就有了两个可能走偏的实现 —— 收在一处，
    口径只有一份（同 clear_model_auto_negatives 的做法）。
    """
    try:
        before = db.query(
            "SELECT COUNT(*) c FROM preferences WHERE task_id=?",
            (GLOBAL_TASK_ID,))[0]["c"]
    except Exception:
        return 0
    db.execute("DELETE FROM preferences WHERE task_id=?", (GLOBAL_TASK_ID,))
    return int(before)


def run_learning_maintenance(db: Database) -> dict:
    """R169 第 3 条：启动时跑一次学习数据维护（幂等，可反复调）。

    做两件事：
      ① **碎片词清扫**（一次性）：丑数据的存量清理，靠 app_meta 打标记；
      ② **时间衰减**（每天一次）：长期不再被碰的模型自动词缓慢降权。

    为什么挂在**启动**而不是"定时器"：本项目的两种启动形态（桌面 GUI /
    Web 服务）都必然经过各自的初始化，挂在这里覆盖最全；而衰减是"一天一次
    就够"的慢活，不需要秒级精度，用日期标记判定即可。

    ⚠ 全程兜异常：维护失败绝不能让程序起不来（学习数据坏了是小事，
      软件打不开是大事）。返回 ``{"fragments": n, "decayed": bool}`` 供日志用。
    """
    out = {"fragments": 0, "decayed": False}
    import datetime as _dt
    try:
        if not db.meta_get(_PREF_FRAG_KEY):
            out["fragments"] = purge_fragment_preferences(db)
            db.meta_set(_PREF_FRAG_KEY, "1")
    except Exception:
        pass
    try:
        today = _dt.date.today().isoformat()
        if db.meta_get(_PREF_DECAY_KEY) != today:
            decay_preferences(db)
            # ⚠ 标记**在衰减成功之后**才写：先写标记再衰减的话，衰减中途
            #   抛异常会让这个标记把今天剩下的机会全堵死。
            db.meta_set(_PREF_DECAY_KEY, today)
            out["decayed"] = True
    except Exception:
        pass
    return out
