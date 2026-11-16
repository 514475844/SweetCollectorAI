"""Review queue: pending images + decision application + learning hook.

In fully-automatic mode most images are auto-classified; only uncertain or
blacklist-hit items land here. Every destructive action goes to the isolation
dir (undoable) and is written to the audit log.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from ..db import Database
from ..models import ReviewDecision
from ..repo import get_image, upsert_image
from . import reasons as _reasons

# 撤销栈上限（#12：最近 50 步）
UNDO_LIMIT = 50
LOG = logging.getLogger("mrm.review.queue")


class ReviewQueue:
    def __init__(self, db: Database, isolate_dir: Path, cfg=None,
                 undo_limit: int = UNDO_LIMIT):
        self.db = db
        self.isolate_dir = Path(isolate_dir)
        self.cfg = cfg
        self.undo_limit = max(1, int(undo_limit or UNDO_LIMIT))
        self._undo: list[dict] = []

    def pending(self, task_id: Optional[int] = None, limit: int = 50):
        # 审核队列放所有「待用户决定」的图（needs_review=1）：
        # AI 拿不准 / AI 不命中主题 / AI 排除词命中 / 黑名单命中（含黑白照内容识别）
        # —— 用审核替代隔离区，凡是软件没把握的都先留文件让用户确认。
        # 注意：不再用 blacklisted=0 过滤，否则黑名单命中的图会被藏起来看不到。
        # V0.0.5：不再要求 in_quarantine=1 —— 网页「移动」送进待审核的图
        # 是 needs_review=1 / in_quarantine=0，桌面以前看不到（决定清单
        # 圈定的两边口径统一）。隔离区视图本身按 needs_review=0 排除，
        # 两张清单依旧互斥。
        sql = "SELECT i.* FROM images i WHERE i.needs_review=1"
        params = []
        if task_id is not None:
            sql += " AND i.task_id=?"
            params.append(task_id)
        sql += " ORDER BY i.id DESC LIMIT ?"
        params.append(limit)
        rows = self.db.query(sql, params)
        return [get_image(self.db, r["id"]) for r in rows]

    def count_pending(self, task_id: Optional[int] = None) -> int:
        """待审核**总数量**（不受 limit / 分页影响）。

        `pending()` 只取当前页，拿它的 len 当"总共有多少张待审核"会骗人
        （#122：审核区标题只显示本页数，用户以为那是全部 → 像没总数）。
        这里独立 COUNT 一次，专供界面显示"待审核 N 张"。
        """
        # V0.0.6：与 pending() / 网页待审核列表逐字同口径。以前这里还多
        # 要求 in_quarantine=1，网页「移动」送进来的图（needs_review=1、
        # in_quarantine=0）列表里看得见、标题数字却不算它，两边永远对不上。
        sql = "SELECT COUNT(*) AS c FROM images i WHERE i.needs_review=1"
        params = []
        if task_id is not None:
            sql += " AND i.task_id=?"
            params.append(task_id)
        rows = self.db.query(sql, params) or []
        try:
            return int(rows[0]["c"])
        except Exception:
            return 0

    def apply(self, image_id: int, decision: ReviewDecision,
              on_learn=None):
        rec = get_image(self.db, image_id)
        if rec is None:
            return
        snap = self._snapshot(rec)
        self._do_apply(rec, decision)
        if on_learn:
            on_learn(decision.value, rec.tags)
        snap["prefs_after"] = self._prefs_of(rec.task_id)
        snap["fb_after"] = self._fb_now()
        self._attach_dropped(snap, rec)
        self._push({"kind": "single", "decision": decision.value,
                    "items": [snap]})

    def apply_batch(self, image_ids: list[int], decision: ReviewDecision,
                    on_learn_factory=None) -> int:
        """批量决定（#12）：整批作为一个撤销事务，Ctrl+Z 一次全部回滚。

        `on_learn_factory(rec)` 返回该图的 learn 回调，或 None。
        """
        items = []
        n = 0
        for image_id in image_ids:
            rec = get_image(self.db, image_id)
            if rec is None:
                continue
            snap = self._snapshot(rec)
            self._do_apply(rec, decision)
            cb = on_learn_factory(rec) if on_learn_factory else None
            if cb:
                cb(decision.value, rec.tags)
            snap["prefs_after"] = self._prefs_of(rec.task_id)
            snap["fb_after"] = self._fb_now()
            self._attach_dropped(snap, rec)
            items.append(snap)
            n += 1
        if items:
            self._push({"kind": "batch", "decision": decision.value,
                        "items": items})
        return n

    def _do_apply(self, rec, decision: ReviewDecision):
        if decision == ReviewDecision.BLACKLIST:
            rec.blacklisted = True
            self._isolate(rec)
        elif decision == ReviewDecision.DISLIKE:
            # r33 定版：「不喜欢」= 把这张图**删掉**，删法跟随「删除时先进回收站」开关
            # （cfg.use_recycle，菜单 隔离区 → 删除时先进回收站，默认开）。
            #   * 开关开 → 文件 shutil.move 进隔离区（=回收站，可还原），
            #     记录保留 in_quarantine=1；tail 里会把 needs_review 置 0，
            #     于是它同时退出审核队列、出现在「隔离区管理」里可还原。
            #   * 开关关 → 文件直接从磁盘删掉 + 删 DB 行（只能靠撤销栈回滚）。
            # 学习（负向偏好）与撤销快照由 apply/apply_batch 统一负责，这里只管文件与记录。
            if not bool(getattr(self.cfg or object(), "use_recycle", True)):
                self._delete_image_file(getattr(rec, "path", None))
                self._delete_image_row(rec.id)
                self.db.audit("review", "dislike-delete", str(rec.id))
                return                      # 记录已删，别再走下面的公共收尾
            self._isolate(rec)
        elif decision in (ReviewDecision.LIKE, ReviewDecision.SKIP):
            # 【修复】原这里 LIKE 和 SKIP 一起清 blacklisted，等于"点一下跳过"
            # 也能把一张命中黑名单的图放回图库里——但「跳过」的语义只是「这页先不看」，
            # 不该改变这张图的归属。改成只有「喜欢」才否决黑名单。
            # 两者都退出审核队列（in_quarantine=False 就是移出队列），
            # 只有 SKIP 保留 blacklisted=1 标记，之后仍可在隔离区/黑名单里看到它。
            rec.in_quarantine = False
            if decision == ReviewDecision.LIKE:
                rec.blacklisted = False   # 用户明确保留 → 否决黑名单
                rec.favorite = True
            try:
                new_path = self._release_to_gallery(rec)
            except Exception:
                new_path = getattr(rec, "path", None)
            # V0.0.6：重复图片功能**彻底**取消 —— 连「审核通过时顺手查一下
            # 图库里有没有同图」这一刀也撤掉。上一版还留着 _gallery_dup_of
            # （里面甚至用 phash 汉明距离把「长得像」的不同图判成重复），
            # 命中就把这张图的文件搬走、账本行删掉：用户点一下「喜欢」，
            # 自己的一张图反而凭空消失，正是明令禁止的聚类去重。
            # 现在不比较、不搬、不删，按用户决定正常入库。
            rec.path = new_path
        if rec is not None:
            rec.needs_review = False
            self.db.execute("INSERT INTO reviews(image_id, decision, task_id) "
                            "VALUES (?,?,?)", (rec.id, decision.value, rec.task_id))
            upsert_image(self.db, rec)
            self.db.audit("review", decision.value, str(rec.id))

    # V0.0.6：`_gallery_dup_of`（按 sha / 感知哈希查「相似重复」）已删除。
    # 重复图片专项彻底不做：软件里不再有任何找重复、比相似、挪走或删掉
    # 某一张的动作。撤销快照里若还留着老数据，_restore_dropped 仍能还原。

    def _may_touch(self, path) -> bool:
        """能不能对这个磁盘文件动手（搬走 / 删除 / 改名 / 写回）。

        V0.0.6：判不清就一律**不动**。以前写的是「cfg 为空时沿用老语义
        直接处置」，等于少传一个参数整道闸就失效，照样可能删到 F 盘原图。
        """
        if self.cfg is None or not path:
            return False
        try:
            from ..safe_delete import is_software_file
            return bool(is_software_file(path, self.cfg))
        except Exception:
            return False

    def _delete_image_file(self, p):
        """删除/转移一个图片文件；返回文件最终落点（隔离时）或 None。

        V0.0.5：先过安全删除闸 —— 用户原有图库（F 盘等软件目录之外）的
        文件**一律不动磁盘**，调用方只清登记记录。软件自己的文件才按
        ``cfg.use_recycle`` 移隔离区或真删（沿用 R152-B1 语义）。
        """
        try:
            if not p or not Path(p).exists():
                return None
            if not self._may_touch(p):
                return None          # 外部原图 / 判不清：没碰文件，只应清登记
            src = Path(p)
            if bool(getattr(self.cfg or object(), "use_recycle", True)):
                self.isolate_dir.mkdir(parents=True, exist_ok=True)
                dest = self.isolate_dir / src.name
                n = 1
                while dest.exists():
                    dest = self.isolate_dir / f"{src.stem}({n}){src.suffix}"
                    n += 1
                # 跨卷用 shutil.move（os.replace 会 WinError 17，见 _isolate 坑 A）
                shutil.move(str(src), str(dest))
                return str(dest)
            src.unlink(missing_ok=True)
            return None
        except Exception:
            return None

    def _delete_image_row(self, iid):
        self.db.execute("DELETE FROM reviews WHERE image_id=?", (iid,))
        self.db.execute("DELETE FROM images WHERE id=?", (iid,))

    @staticmethod
    def _attach_dropped(snap: dict, rec):
        """R152-B1：把 _do_apply 里「删旧留新 / 删新留旧」去掉的行挂进撤销快照。

        `_do_apply` 会在被丢掉的 rec 上留两个类型外属性（本地传递用）：
        ``_dropped_dup``（图库里被顶掉的旧行）/ ``_dropped_self``（本项被丢的行）。
        快照在 _do_apply **之前**取，所以只能在这里补挂。
        """
        d_dup = getattr(rec, "_dropped_dup", None)
        if d_dup:
            snap["dropped"] = dict(d_dup)
        d_self = getattr(rec, "_dropped_self", None)
        if d_self:
            snap["dropped_self"] = dict(d_self)

    def _full_row(self, iid) -> Optional[dict]:
        """整行快照（SELECT *）——R152-B1 撤销「删旧留新」时重插被删的旧行用。"""
        try:
            rows = self.db.query("SELECT * FROM images WHERE id=?", (iid,))
            return dict(rows[0]) if rows else None
        except Exception:
            return None

    def _release_to_gallery(self, rec) -> str:
        """审核批准(LIKE/SKIP)：把文件从 review_dir 移回图库(output_dir)。

        保留『一个网站一个文件夹』约定：http(s) 来源按域名放回 output_dir/<site>/，
        file:// (磁盘回填) 来源则平铺回 output_dir。文件名冲突加 (1) 序号。
        移动失败（源已不存在等）不抛错，原样返回当前路径。
        """
        p = getattr(rec, "path", None)
        if not p or not Path(p).exists():
            return p
        try:
            from ..config import user_data_dir
            from ..repo import get_task
            t = get_task(self.db, rec.task_id)
            out = Path(t.output_dir) if (t and getattr(t, "output_dir", None)) \
                else Path(getattr(self.cfg, "library_dir", user_data_dir()))
        except Exception:
            from ..config import user_data_dir
            out = Path(getattr(self.cfg, "library_dir", user_data_dir()))
        src = Path(p)
        su = getattr(rec, "source_url", "") or ""
        sub = ""
        if su.startswith("http://") or su.startswith("https://"):
            try:
                from ..utils.url import domain_of
                sub = domain_of(su)
            except Exception:
                sub = ""
        dest_dir = out / sub if sub else out
        dest_dir.mkdir(parents=True, exist_ok=True)
        # V0.0.5：用户原有图库的文件「放回头像」时原地不动 ——
        # 挪进图库目录同样是对原图动手脚。只更新账本归属。
        # V0.0.6：cfg 缺省同样不搬（判不清就不动）。
        if not self._may_touch(src):
            return str(src)
        # r33：这张图本来就在图库里（审核里点到的大多是已经进图库的），
        # 不能"移回去"再落一份 xxx(1).jpg —— 那等于在图库里凭空多一张副本，
        # 也不能因为按域名多套一层子目录就把原图搬走。同属图库就原地不动。
        try:
            out_r = out.resolve()
            src_r = src.parent.resolve()
            if out_r == src_r or out_r in src_r.parents:
                return str(src)
        except Exception:
            pass
        dest = dest_dir / src.name
        n = 1
        while dest.exists():
            dest = dest_dir / f"{src.stem}({n}){src.suffix}"
            n += 1
        try:
            shutil.move(str(src), str(dest))
        except Exception as e:
            # V0.0.6：以前搬失败会「复制一份 + 把原文件 unlink」——等于
            # 悄悄真删。现在搬不动就原样留着，只回报原路径，绝不删。
            LOG.warning("放回图库失败，文件原样保留：%s（原因：%s）", src, e)
            return p
        self.db.audit("review", "release-to-gallery", str(dest), str(src))
        return str(dest)

    def _isolate(self, rec):
        if not rec.path:
            rec.in_quarantine = True
            return
        # V0.0.5：用户原有图库的文件不搬 —— 只在账本上打「隔离」标记，
        # 文件留在原处。软件自己的文件才真的移进隔离区目录。
        # V0.0.6：cfg 缺省同样不搬（判不清就不动）。
        if not self._may_touch(rec.path):
            rec.in_quarantine = True
            return
        src = Path(rec.path)
        if src.exists():
            self.isolate_dir.mkdir(parents=True, exist_ok=True)
            dest = self.isolate_dir / src.name
            n = 1
            while dest.exists():
                dest = self.isolate_dir / f"{src.stem}({n}){src.suffix}"
                n += 1
            # 坑 A：os.replace / Path.replace 不能跨卷（output_dir 在别的盘时
            # 会抛 WinError 17）。必须走 shutil.move（跨盘自动复制+删除）。
            shutil.move(str(src), str(dest))
            rec.path = str(dest)
        rec.in_quarantine = True

    # ------------------------------------------------------------ undo (#12)
    def _snapshot(self, rec) -> dict:
        # V0.0.8：纠错反馈账本（「别找」词）也进快照 —— 撤销一张判退时，
        # 学到的词必须一起退回，否则 Ctrl+Z 声称全撤了、账本里还留着。
        try:
            from .. import feedback as _fb
            fb_before = _fb.snapshot(self.db)
        except Exception:
            fb_before = None
        return {
            "image_id": rec.id,
            "path": rec.path,
            "in_quarantine": bool(rec.in_quarantine),
            "needs_review": bool(rec.needs_review),
            "blacklisted": bool(rec.blacklisted),
            "favorite": bool(rec.favorite),
            "task_id": rec.task_id,
            "prefs": self._prefs_of(rec.task_id),
            "reviews_max": self._reviews_max(rec.id),
            "fb_before": fb_before,
        }

    def _fb_now(self):
        """纠错反馈账本当前快照（apply 里学完后取，撤销时按差量回滚）。"""
        try:
            from .. import feedback as _fb
            return _fb.snapshot(self.db)
        except Exception:
            return None

    def _prefs_of(self, task_id) -> dict:
        """{label: (weight, seen, hit_pos, hit_neg)} —— 学习回滚快照（全局偏好）。

        R152-B2：旧版只存 (weight, seen)，撤销后 hit_pos/hit_neg 残留
        （学习写入时 hit_pos/hit_neg 各 +1，学习看板的命中数就虚高）。
        """
        try:
            rows = self.db.query(
                "SELECT label, weight, seen, hit_pos, hit_neg "
                "FROM preferences WHERE task_id=0", ())
            return {r["label"]: (r["weight"], r["seen"],
                                 int(r["hit_pos"] or 0), int(r["hit_neg"] or 0))
                    for r in rows}
        except Exception:
            return {}

    def _reviews_max(self, image_id) -> int:
        try:
            rows = self.db.query(
                "SELECT MAX(rowid) AS m FROM reviews WHERE image_id=?",
                (image_id,))
            return int(rows[0]["m"] or 0) if rows else 0
        except Exception:
            return 0

    # ------------------------------------------------------------ 网页端快照
    def snapshot_web(self, iid) -> Optional[dict]:
        """V0.0.12：网页动作接口不走 `_do_apply`（它只翻标志位、物理搬运
        交给独立路径），但撤销必须能回滚**同样的东西**。本方法产出的快照
        与 `_snapshot` 逐字段同形，交给 `_undo_one` 原样消费——两处格式
        分叉的话撤销会「撤了个寂寞」（桌面 R152 系列踩过）。

        拿不到图片行返回 None（调用方不压栈）。
        """
        rec = get_image(self.db, iid)
        if rec is None:
            return None
        return self._snapshot(rec)

    def finalize_web(self, item: dict) -> None:
        """网页侧学习发生在动作循环里（快照之后），补齐 `_do_apply` 路径
        由 apply() 负责的两样：学后权重快照 + 当前反馈账本。"""
        item["prefs_after"] = self._prefs_of(item.get("task_id"))
        item["fb_after"] = self._fb_now()

    def push_web(self, act: str, items: list) -> None:
        """把一批网页动作打包压入撤销栈（与桌面批量同构：一条栈目、
        一次 Ctrl+Z/按钮撤全批；`_undo_one` 倒序回滚的理由同 apply_batch）。"""
        if items:
            self._push({"kind": "web", "decision": act, "items": items})

    def undo_info(self) -> dict:
        """前端按钮态：能不能撤、撤的是哪一步、栈深。"""
        return {"can_undo": self.can_undo(),
                "label": self.undo_label(),
                "depth": self.undo_depth()}

    def _push(self, entry: dict):
        self._undo.append(entry)
        if len(self._undo) > self.undo_limit:
            del self._undo[0]

    def can_undo(self) -> bool:
        return bool(self._undo)

    def undo_depth(self) -> int:
        return len(self._undo)

    def undo_label(self) -> str:
        """下一步撤销会做什么（按钮 tooltip / 日志用）。"""
        if not self._undo:
            return ""
        e = self._undo[-1]
        n = len(e.get("items") or [])
        return f"撤销「{e.get('decision', '')}」" + (f" ×{n}" if n > 1 else "")

    def undo(self) -> Optional[str]:
        """回滚最近一步审核操作：图片状态 + 文件位置 + 学习权重。

        返回人类可读的结果描述；没有可撤销的操作时返回 None。

        R152-B1：行已不存在的项分两种——撤销栈带了完整快照的（去重删除）
        重插恢复；``use_recycle`` 关着的真永久删除，如实报「不可恢复」，
        不许假装撤成了。
        """
        if not self._undo:
            return None
        entry = self._undo.pop()
        items = entry.get("items") or []
        done = 0
        restored = 0
        gone = 0
        # R152-B2：批量条目必须**倒序**恢复——apply_batch 循环里后项快照
        # 是在前项学习之后才取的（后项 prefs 含前项学习结果），正序恢复
        # 会让「后项恢复」最后落笔，把批前已存在的词留在前项学习后的值上
        # （探针实证：新建词被 DELETE+UPDATE落空碰巧清掉，已有词才暴露残留）。
        # 倒序后最后一笔是首项恢复 = 批前状态；单条条目 reversed 无影响。
        for snap in reversed(items):
            try:
                st = self._undo_one(snap)
                if st == "restored":
                    restored += 1
                    done += 1
                elif st == "gone":
                    gone += 1
                else:
                    done += 1
            except Exception as e:      # 单条失败不影响其余
                self.db.audit("review", "undo-failed",
                              str(snap.get("image_id")), str(e))
        n = len(items)
        desc = f"已撤销「{entry.get('decision', '')}」{done}/{n} 项"
        if restored:
            desc += f"（含恢复 {restored} 项被去重删掉的图）"
        if gone:
            desc += f"（{gone} 项文件已永久删除，不可恢复）"
        try:
            self.db.audit("review", "undo", "", desc)
        except Exception:
            pass
        return desc

    def _restore_dropped(self, d) -> bool:
        """R152-B1：把快照里被删掉的行重插回去，文件从隔离区搬回原路径。

        返回是否真恢复了。行还在（不该发生）或文件已不可寻回时不插——
        插一条「磁盘已缺失」的幽灵行比不插更糟。
        """
        if not d:
            return False
        iid = d.get("id")
        if iid is None:
            return False
        try:
            if get_image(self.db, iid) is not None:
                return False
            iso = d.get("_isolated_as")
            orig = d.get("path") or ""
            have_file = (iso and Path(iso).exists()) or (orig and Path(orig).exists())
            if not have_file:
                return False           # 文件真没了（use_recycle 关）→ 不可恢复
            full = {k: v for k, v in d.items() if not k.startswith("_")}
            if full:
                cols = list(full.keys())
                self.db.execute(
                    "INSERT INTO images (" + ",".join(cols) + ") VALUES ("
                    + ",".join("?" * len(cols)) + ")",
                    [full[c] for c in cols])
            # V0.0.6：撤销也是「搬文件」，同样得过闸。老快照里的原路径
            # 可能就是用户 F 盘原图，往那儿写回等于动用户文件。
            if (iso and orig and Path(iso).exists()
                    and self._may_touch(iso) and self._may_touch(orig)):
                dst = Path(orig)
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    stem, dot, ext = dst.name.rpartition(".")
                    k = 1
                    while dst.exists():
                        dst = dst.with_name(f"{stem}({k}){dot}{ext}")
                        k += 1
                shutil.move(str(iso), str(dst))
                self.db.execute("UPDATE images SET path=? WHERE id=?",
                                (str(dst), iid))
            self.db.audit("review", "undo-restore-dropped", str(iid))
            return True
        except Exception:
            return False

    def _undo_one(self, snap: dict) -> str:
        iid = snap["image_id"]
        rec = get_image(self.db, iid)
        if rec is None:
            # R152-B1：行没了——带完整快照的（去重删除）重插恢复；
            # 否则就是 use_recycle=关 的永久删除，如实上报不可恢复。
            if (self._restore_dropped(snap.get("dropped"))
                    or self._restore_dropped(snap.get("dropped_self"))):
                return "restored"
            return "gone"
        # 1) 文件位置：若当初被移进了隔离区，搬回原路径（跨盘用 shutil.move）
        old_path = snap.get("path")
        cur_path = rec.path
        if (old_path and cur_path and Path(old_path) != Path(cur_path)
                and self._may_touch(old_path) and self._may_touch(cur_path)):
            src = Path(cur_path)
            if src.exists():
                dst = Path(old_path)
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    stem, dot, ext = dst.name.rpartition(".")
                    k = 1
                    while dst.exists():
                        dst = (dst.with_name(f"{stem}({k}).{ext}") if dot
                               else dst.with_name(f"{stem}({k})"))
                        k += 1
                shutil.move(str(src), str(dst))
                rec.path = str(dst)
        # 2) 数据库字段回滚
        rec.in_quarantine = bool(snap.get("in_quarantine"))
        rec.needs_review = bool(snap.get("needs_review"))
        rec.blacklisted = bool(snap.get("blacklisted"))
        rec.favorite = bool(snap.get("favorite"))
        if not rec.path and old_path:
            rec.path = old_path
        upsert_image(self.db, rec)
        # 3) 删掉这次操作写入的 reviews 记录
        try:
            self.db.execute(
                "DELETE FROM reviews WHERE image_id=? AND rowid>?",
                (iid, int(snap.get("reviews_max") or 0)))
        except Exception:
            pass
        # 4) 学习权重回滚到操作前（含 seen 计数与命中计数）
        # R152-B2：hit_pos/hit_neg 也要回滚——学习写入时它们各 +1，
        # 只回 weight/seen 会让学习看板的命中数越撤越虚高。
        before = snap.get("prefs") or {}
        after = snap.get("prefs_after") or {}
        for label in set(list(before.keys()) + list(after.keys())):
            try:
                if label in before:
                    w, seen, hp, hn = before[label]
                    self.db.execute(
                        "UPDATE preferences SET weight=?, seen=?, "
                        "hit_pos=?, hit_neg=? "
                        "WHERE task_id=0 AND label=?",
                        (w, seen, hp, hn, label))
                else:
                    self.db.execute(
                        "DELETE FROM preferences WHERE task_id=0 AND label=?",
                        (label,))
            except Exception:
                pass
        # 5) V0.0.8：纠错反馈账本按差量回滚 —— 这次判退学到/加计数的词退回
        #    操作前；快照之后别的入口新加的词不碰（并发安全）。
        try:
            from .. import feedback as _fb
            _fb.rollback(self.db, snap.get("fb_before"), snap.get("fb_after"))
        except Exception:
            pass
        return "ok"

    def sample_quarantine(self, n: int = 12) -> list[int]:
        """误杀抽检：随机把 N 张「AI 自动隔离」的图（needs_review=0）标记为待审核，
        拉进审核队列让人过一遍——防止阈值/排除词把好图误杀了没人发现。

        只抽 blacklisted=0 的（AI 未命中/拿不准 隔离的），排除词明确命中
        的那种不抽（那是用户主动设的）。返回被抽中的图片 id 列表。
        """
        rows = self.db.query(
            "SELECT id FROM images WHERE in_quarantine=1 AND needs_review=0 "
            "AND blacklisted=0 ORDER BY RANDOM() LIMIT ?", (n,))
        ids = [r["id"] for r in rows]
        for iid in ids:
            # V0.0.8：抽检进审核的图也要有「为什么」，审核页才说得清
            self.db.execute(
                "UPDATE images SET needs_review=1, review_reason=? WHERE id=?",
                (_reasons.SAMPLE, iid))
        return ids

    def smart_sample(self, n: int = 20, task_id: Optional[int] = None) -> list[int]:
        """主动学习：抽 N 张模型「最拿不准」的图（|score-阈值| 最小）给人审。

        比随机抽信息量大得多——专挑边界样本，标几张就能把决策边界学准。
        只从「AI 自动隔离、还没进审核」的图里抽（needs_review=0）。
        """
        sql = ("SELECT id, score FROM images WHERE in_quarantine=1 "
               "AND blacklisted=0 AND needs_review=0 AND score IS NOT NULL "
               "AND score > 0")
        params: list = []
        if task_id is not None:
            sql += " AND task_id=?"
            params.append(task_id)
        rows = self.db.query(sql, params)
        # 【修复】原先写死 0.5 当"阈值"来排"最拿不准"的图，但真正参与判定的是
        # 任务自己的 ai_threshold（有人调到 0.8，排序就全错了）。这里取真实阈值。
        # #60：兜底值也跟着新默认 0.35 走（查不到任务行时别又拿 0.5 排一遍）。
        thr = 0.35
        try:
            if task_id is not None:
                r = self.db.query(
                    "SELECT ai_threshold FROM tasks WHERE id=?", (task_id,))
                key = "ai_threshold"
            else:
                r = self.db.query("SELECT AVG(ai_threshold) AS avg_thr "
                                  "FROM tasks WHERE is_template=0")
                key = "avg_thr"
            if r and r[0] is not None:
                try:
                    val = r[0][key]
                except Exception:
                    val = r[0].get(key) if hasattr(r[0], "get") else None
                if val is not None:
                    thr = float(val)
        except Exception:
            # 查库失败同样退回新默认——两处兜底不一致的话，查库失败时排序
            # 会按旧默认 0.5 排，跟正常路径对不上。
            thr = 0.35
        # 离阈值越近越拿不准；没有分数或分数为 0 的排最后
        rows.sort(key=lambda r: abs((r["score"] or 0) - thr))
        ids = [r["id"] for r in rows[:n]]
        for iid in ids:
            self.db.execute(
                "UPDATE images SET needs_review=1, review_reason=? WHERE id=?",
                (_reasons.SMART, iid))
        return ids
