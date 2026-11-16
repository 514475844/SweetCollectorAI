"""图片标签的**纯函数层**（R228 · R154-F17 Web 对称端点）。

为什么单独开这一层
------------------
这三个纯函数原本只挂在 ``gui.main_window.MainWindow`` 上。Web 端要做
「加标签 / 删标签 / 标签筛选」的对称功能时，只有两条路：

  ① import main_window —— 会把 PySide6 一起拖进无头进程（Docker/容器
     那套 headless 直接起不来，R74 已有先例）；
  ② 在 webui 里另写一份 —— 那就是**第二套口径**，迟早变成「网页加的
     标签桌面端读不出来」。

现在两边同源：main_window 的三个静态方法只是转发到这里的同名函数，
webui 直接用这里。改这一处 ⇒ 两端一起变。

语义（与 R215 落地时逐字一致，改这里等于改两端）
-----------------------------------------------
read_tags
    读 ``images.tags`` → list，**保留原始大小写**（写回用途）。
    ⚠ ``simsearch.split_tags`` 的小写口径只归学习/相似度 —— 用户手敲的
    "White Stockings" 不能被打成小写再落库（界面显示会变）。
    老库的「逗号分隔」裸串也认（与 split_tags 同款兼容）。
merge_tags
    批量**加**：去重不覆盖（已有该标签的行不返回）、保序、新标签追加末尾。
strip_tags
    批量**删**：只给**确实少了一个标签**的行返回（没命中的不写库）。
count_tags
    全库标签计数（筛选下拉用）：取前 ``top`` 个、次数相同按标签名排、
    **含空格的跳过** —— ``tag:`` 语法按空格切 token，带空格的标签拼进
    查询串会被切成两个词（筛错东西），要筛它请直接敲搜索框（走 LIKE）。
"""

from __future__ import annotations

import json
import re
from collections import Counter

_SPLIT = re.compile(r"[,\s;，、]+")
_WS = re.compile(r"\s")

# 筛选下拉最多给多少个标签（与桌面端 g_tag_filter 同一个数字）。
TAG_TOP_N = 40


def read_tags(raw) -> list:
    """把 ``images.tags`` 的原始值读成标签列表（保留原始大小写）。"""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(t) for t in raw]
    t = str(raw).strip()
    if t.startswith("["):
        try:
            v = json.loads(t)
            if isinstance(v, list):
                return [str(x) for x in v]
        except Exception:
            pass
    return [x for x in _SPLIT.split(t) if x]


def merge_tags(rows, tag: str) -> list:
    """批量**加**一个标签：返回 ``[(id, 新 tags JSON), …]``。"""
    tag = (tag or "").strip()
    if not tag:
        return []
    out = []
    for r in rows or ():
        iid = r.get("id")
        if iid is None:
            continue
        cur = read_tags(r.get("tags"))
        if tag in [t.strip() for t in cur]:
            continue
        out.append((iid, json.dumps(cur + [tag], ensure_ascii=False)))
    return out


def strip_tags(rows, remove) -> list:
    """批量**删**标签：返回 ``[(id, 新 tags JSON), …]``。"""
    rm = {str(t).strip() for t in (remove or ()) if str(t).strip()}
    if not rm:
        return []
    out = []
    for r in rows or ():
        iid = r.get("id")
        if iid is None:
            continue
        cur = read_tags(r.get("tags"))
        if not cur:
            continue
        left = [t for t in cur if t.strip() not in rm]
        if len(left) == len(cur):
            continue
        out.append((iid, json.dumps(left, ensure_ascii=False)))
    return out


def union_tags(rows) -> list:
    """多张图的标签**并集**（保序去重）—— 删标签弹窗的勾选清单。

    与桌面端 ``gallery_remove_tag`` 的清单同口径：按图片顺序、按标签
    出现顺序，第一次出现的位置为准；空标签跳过。
    """
    out = []
    for r in rows or ():
        for t in read_tags(r.get("tags")):
            t = t.strip()
            if t and t not in out:
                out.append(t)
    return out


def count_tags(rows, top: int = TAG_TOP_N) -> list:
    """统计 ``rows``（含 ``tags`` 键的行）里各标签出现次数。

    返回 ``[(tag, n), …]``：次数倒序、同次数按标签名排（顺序稳定不跳），
    含空格的标签跳过（理由见模块 docstring）。
    """
    cnt: Counter = Counter()
    for r in rows or ():
        try:
            raw = r["tags"]
        except Exception:
            continue            # 缺列的行跳过，不该让整个下拉挂掉
        for t in read_tags(raw):
            t = t.strip()
            if t and not _WS.search(t):
                cnt[t] += 1
    return sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


def row_tags(rows) -> list:
    """把库行归一成 ``[{"id":…, "tags":[...]}, …]``（给 API 下发用）。

    ⚠ 只带 id 与 tags 两个键：调用方（加/删标签）不需要别的字段，
    少一个字段就少一处"两端字段漂移"的机会。
    """
    return [{"id": r.get("id"), "tags": read_tags(r.get("tags"))}
            for r in rows or () if r.get("id") is not None]
