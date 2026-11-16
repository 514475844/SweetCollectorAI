"""#63 图库搜索语法（轻量、纯 SQLite，不依赖外部搜索引擎）。

支持：
    tag:白丝            命中标签（模糊，含）
    site:pictoa         来自某站点（任务名）
    score>0.5           分数高于阈值
    score<0.3           分数低于阈值
    w>=800 / h>=600     最小宽/高
    fav                 只看收藏                 black        只看黑名单
    quar                只看隔离区               review       只看待审核
    -黑丝                负号排除（标签里不含"黑丝"）
    裸词 "猫"            在标签/来源 URL 里模糊匹配

多个条件之间是 AND。空格分隔；引号不是必须的（标签本身无空格）。
"""
from __future__ import annotations

import json
import re
from typing import Optional

from .db import Database

# key 后跟 : 或 > / < / >= / <= / = 再跟值的语法
_OP = re.compile(r'^(?P<key>[A-Za-z]+)(?P<op>[:<>]=?|=)(?P<val>.*)$')
_KNOWN_KEYS = {"tag", "site", "score", "w", "width", "minw", "h", "height", "minh"}


def parse_query(q: str) -> dict:
    """把搜索串解析成结构化条件。

    支持 token 形如：tag:白丝 / site:pictoa / score>0.5 / score<0.3 /
    w>=800 / h>=600 / -黑丝 / fav / black / quar / review / 裸词。
    """
    out = {
        "tags_include": [], "tags_exclude": [], "site": None,
        "score_min": None, "score_max": None, "w_min": None,
        "h_min": None, "fav": False, "black": False, "quar": False,
        "review": False, "free": [],
    }
    for tok in (q or "").split():
        if tok.startswith("-") and len(tok) > 1:
            out["tags_exclude"].append(tok[1:])
            continue
        m = _OP.match(tok)
        if m and m.group("key").lower() in _KNOWN_KEYS:
            k = m.group("key").lower()
            op = m.group("op")
            v = m.group("val")
            if k == "tag":
                if v:
                    out["tags_include"].append(v)
            elif k == "site":
                if v:
                    out["site"] = v
            elif k == "score":
                val = _f(v)
                if op == ">":
                    out["score_min"] = val
                elif op == "<":
                    out["score_max"] = val
                else:  # ":" 或 "=" 当作精确范围两端
                    out["score_min"] = out["score_max"] = val
            elif k in ("w", "width", "minw"):
                # R152-C8：用户把 w 写成 `w>=abc` 时原来 int() 直接
                # ValueError 冒到 GUI（图库搜索框一敲回车就崩）。
                # 非法数值**忽略该条件**（其余条件照常生效），不抛异常。
                try:
                    out["w_min"] = int(v.lstrip(">=") or 0)
                except ValueError:
                    pass
            elif k in ("h", "height", "minh"):
                try:
                    out["h_min"] = int(v.lstrip(">=") or 0)
                except ValueError:
                    pass
            continue
        if tok in ("fav", "favorite"):
            out["fav"] = True
        elif tok == "black":
            out["black"] = True
        elif tok == "quar":
            out["quar"] = True
        elif tok == "review":
            out["review"] = True
        else:
            out["free"].append(tok)
    return out


def _f(s: str) -> Optional[float]:
    try:
        return float(s)
    except Exception:
        return None


def orient_res_where(orient: str = "", res: str = "") -> str:
    """图库方向 / 分辨率筛选 → SQL 片段（**桌面端与 Web 共用**）。

    orient: all/"" (不筛) / land(横图) / port(竖图) / square(正方形)
    res   : all/"" (不筛) / s(<1280) / m(1280–1920) / l(1920–2560) / xl(≥2560)
            分辨率按**长边** max(width, height) 分档。

    ⚠ 只对**已有尺寸**的记录生效（width/height 为空的行不会命中具体档位）：
    这与桌面端 `main_window._gallery_orient_res_where` 的语义逐字一致，
    两处必须同步 —— 所以这里只有一份实现，GUI 那边改成转发到本函数。
    """
    o = str(orient or "").strip().lower()
    r = str(res or "").strip().lower()
    frags = []
    if o == "land":
        frags.append("width IS NOT NULL AND width>height")
    elif o == "port":
        frags.append("width IS NOT NULL AND height>width")
    elif o == "square":
        frags.append("width IS NOT NULL AND width=height AND width>0")
    mk = "(CASE WHEN width>=height THEN width ELSE height END)"
    if r == "s":
        frags.append(f"{mk} < 1280")
    elif r == "m":
        frags.append(f"{mk} >= 1280 AND {mk} < 1920")
    elif r == "l":
        frags.append(f"{mk} >= 1920 AND {mk} < 2560")
    elif r == "xl":
        frags.append(f"{mk} >= 2560")
    return (" AND " + " AND ".join(frags)) if frags else ""


def search_images(db: Database, q: str, limit: int = 500) -> list[dict]:
    """执行搜索，返回 images 行（含 task name 作为 site）。"""
    cond = ["1=1"]
    params: list = []
    c = parse_query(q)

    # 可见性：默认只看「未拉黑、未隔离、有文件路径」的图（与图库一致）；
    # 显式 black / quar 时放开对应限制，方便从图库直接搜隔离区/黑名单。
    if not c["black"] and not c["quar"]:
        cond.append("i.path IS NOT NULL AND i.blacklisted=0 AND i.in_quarantine=0")

    # 标签筛选：tags 是 JSON 数组，用 LIKE 近似匹配（够用且零依赖）
    for t in c["tags_include"]:
        cond.append("(tags LIKE ?)")
        params.append(f'%{json.dumps(t, ensure_ascii=False).strip(chr(34))}%')
    for t in c["tags_exclude"]:
        cond.append("(tags NOT LIKE ?)")
        params.append(f'%{json.dumps(t, ensure_ascii=False).strip(chr(34))}%')

    if c["site"]:
        cond.append("t.name LIKE ?")
        params.append(f"%{c['site']}%")
    if c["score_min"] is not None:
        cond.append("i.score >= ?")
        params.append(c["score_min"])
    if c["score_max"] is not None:
        cond.append("i.score <= ?")
        params.append(c["score_max"])
    if c["w_min"]:
        cond.append("i.width >= ?")
        params.append(c["w_min"])
    if c["h_min"]:
        cond.append("i.height >= ?")
        params.append(c["h_min"])
    if c["fav"]:
        cond.append("i.favorite=1")
    if c["black"]:
        cond.append("i.blacklisted=1")
    if c["quar"]:
        cond.append("i.in_quarantine=1")
    if c["review"]:
        cond.append("i.needs_review=1")
    for f in c["free"]:
        cond.append("(i.tags LIKE ? OR i.source_url LIKE ?)")
        params.append(f"%{f}%")
        params.append(f"%{f}%")

    sql = ("SELECT i.*, t.name AS site FROM images i "
           "LEFT JOIN tasks t ON t.id=i.task_id "
           f"WHERE {' AND '.join(cond)} ORDER BY i.id DESC LIMIT {int(limit)}")
    return [dict(r) for r in db.query(sql, params)]
