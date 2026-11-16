"""全软件唯一的一张「这回要找什么 / 别找什么」词表。

用户当初设计这个软件时的原话意思：

    想下载很多哆啦A梦的图，关键词得写「日本动漫」这个大类，
    但不想搜到灌篮高手，于是把「灌篮高手」填进黑名单让它别下载；
    本地存的哆啦A梦图随时导进来，软件从这些图里学会「我喜欢哆啦A梦」；
    然后拿学到的结果 + 手填的词 + 给的网站，去全站找对味的图下载；
    偶尔下错了（动漫哆啦A梦变成 Cosplay 真人），把那张放进黑名单，
    软件再学一次，越用越准。

以前这条链是断的：手填的词只拿去给**已经下载回来**的图打分；
从本地图库学到的词只进了一本口味账，从来没变成「去找什么」。
于是「学」跟「没学」一个样，用户填的词也拦不住同类里不想要的那部分。

本模块把两边并成一张表，采集前后共用：
  · want  = 手填的主题词/关键词 + 从本地图库学到的正偏好词
  · avoid = 任务/全局黑名单 + 排除词 + 用户明确拉黑过的词

三个消费点（缺一不可）：
  1. 下载前：链接文字/标题/相册命中 avoid → 根本不抓；
  2. 站内找图：把 want 变成已知站点的按词找图入口；
  3. 看图判定：want 全部进模型候选标签，机器真的拿「哆啦A梦」去认图。

容错原则：读不到偏好表、表还没建、模型没装，都退化成「只用手填的词」，
绝不让采集任务因为这些辅助信息崩掉。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote_plus, urlsplit

# 学到的词最多取多少个进「要找什么」。太多会把模型候选集撑爆、也会让
# 站内入口一次开几十个，反而更慢更不准。按权重从高到低取。
MAX_LEARNED = 40
# 低于这个权重不认为「用户明确喜欢」。导入学习给最高频词 2.0，
# 一次误标只有 0.x，不该把整个采集方向带偏。
MIN_POS_WEIGHT = 0.5
# 一次最多按几个词生成站内找图入口
MAX_SEARCH_ENTRIES = 6

# 已知支持「按词/标签找图」的站点模板。只在这些站点、且用户填的是
# 裸首页或裸列表时才生成 —— 用户给了具体图集/帖子网址一律不动。
# {q} 是 URL 编码后的词。
SEARCH_TEMPLATES: dict[str, str] = {
    "danbooru.donmai.us": "https://danbooru.donmai.us/posts?tags={q}",
    "gelbooru.com": "https://gelbooru.com/index.php?page=post&s=list&tags={q}",
    "konachan.com": "https://konachan.com/post?tags={q}",
    "yande.re": "https://yande.re/post?tags={q}",
    "safebooru.org": "https://safebooru.org/index.php?page=post&s=list&tags={q}",
}

# 这些形状算「整站/列表」，可以替换成按词找图；带 query 的一律不算。
_BARE_PATHS = {"", "/", "/posts", "/post", "/index.php", "/recent", "/galleries"}


@dataclass
class FindTerms:
    """一次任务用的词表。"""

    want: list[str] = field(default_factory=list)          # 要找什么
    avoid: list[str] = field(default_factory=list)         # 别找什么
    typed: list[str] = field(default_factory=list)         # 手填的（含主题词分层）
    learned: list[str] = field(default_factory=list)       # 从本地图库学到的
    avoid_learned: list[str] = field(default_factory=list)  # 用户拉黑后学到的
    # V0.0.7：「这张图下错了」教出来的词。判掉 2 次以上已经并进 avoid
    # 硬拦；判掉 1 次的先放这里，只进看图排除候选、命中送审核，不封死。
    soft_avoid: list[str] = field(default_factory=list)
    avoid_feedback: list[str] = field(default_factory=list)  # 已升级成硬拦的

    def describe(self) -> str:
        return (f"要找={self.want[:12]}{'…' if len(self.want) > 12 else ''} "
                f"别找={self.avoid[:12]}{'…' if len(self.avoid) > 12 else ''}")


def _clean(words) -> list[str]:
    """去空、去重复（大小写不敏感）、保持原顺序与原样（中文不转小写）。

    V0.0.6：先把「一整串」拆开。用户在任务黑名单里一次粘贴
    「词1，词2，词3……」时，以前整串超过 60 字被直接丢掉，等于这个任务
    的黑名单一栏白填。全局黑名单早就有这套拆分，任务级漏了。
    """
    try:
        from .config import split_blacklist_words as _split
    except Exception:
        _split = None
    out: list[str] = []
    seen: set[str] = set()
    for w in words or []:
        s = str(w or "").strip()
        if not s:
            continue
        # 正则形态 /xxx/ 里可能带逗号，别拆坏
        if s.startswith("/") and s.endswith("/"):
            pieces = [s]
        else:
            pieces = _split([s]) if _split else [s]
        for piece in pieces:
            piece = str(piece or "").strip()
            if not piece or len(piece) > 60:
                continue
            k = piece.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(piece)
    return out


def _typed_words(task) -> list[str]:
    """用户手填的：主题词（含所有分层）+ 关键词。"""
    out: list[str] = []
    try:
        from . import label_levels as lvl
        out.extend(lvl.flat_words(task))
    except Exception:
        out.extend(list(getattr(task, "ai_labels", None) or [])
                   + list(getattr(task, "ai_labels_l2", None) or [])
                   + list(getattr(task, "ai_labels_l3", None) or []))
    out.extend(list(getattr(task, "keywords", None) or []))
    return _clean(out)


def _useless(label: str) -> bool:
    """太泛、太碎、不像一个「要找什么」的词，别拿去搜站。"""
    k = str(label or "").strip().lower()
    if not k or len(k) > 40:
        return True
    try:
        from .simsearch import GENERIC
        if k in GENERIC:
            return True
    except Exception:
        pass
    try:
        from .repo import _is_fragment_label
        if _is_fragment_label(k):
            return True
    except Exception:
        pass
    return False


def _learned_words(db) -> tuple[list[str], list[str]]:
    """从口味账里取：正权 = 喜欢什么，负权且用户亲手填过 = 明确不要什么。

    模型自己猜出来的负权词**不进**硬黑名单（避免一次误判把整个方向封死），
    这条界线沿用 repo.negative_user_typed_labels 的既定口径。
    """
    pos: list[tuple[str, float]] = []
    neg: list[str] = []
    try:
        from .repo import get_preferences
        prefs = get_preferences(db)
    except Exception:
        return [], []
    for lab, w in (prefs or {}).items():
        k = str(lab or "").strip().lower()
        if _useless(k):
            continue
        try:
            v = float(w or 0.0)
        except Exception:
            continue
        if v >= MIN_POS_WEIGHT:
            pos.append((str(lab).strip(), v))
    pos.sort(key=lambda x: -x[1])
    try:
        from .repo import negative_user_typed_labels
        neg = [str(x).strip() for x in negative_user_typed_labels(db)
               if not _useless(str(x))]
    except Exception:
        neg = []
    return [w for w, _ in pos[:MAX_LEARNED]], _clean(neg)


def _feedback_words(db) -> tuple[list[str], list[str]]:
    """「这张图下错了」教出来的词：(已经硬拦的, 还在观察的)。

    V0.0.7。读不到账本就当没有，绝不让采集任务因为辅助信息崩掉。
    """
    if db is None:
        return [], []
    try:
        from .feedback import hard_words, soft_words
        return hard_words(db), soft_words(db)
    except Exception:
        return [], []


def _clashes_with_theme(word: str, theme_keys) -> bool:
    """反馈词与「要找」的词**部分重叠**也算冲突。

    只比完全相等会漏：主题是「风景」，被拒收的图可能带「风景 插画」这种
    组合标签；判退两次就把「风景 插画」硬拦，站内搜索拼出 `-风景 插画`
    反而把整个主题带没了。所以只要互相包含，一律降级成观察词（送审核、
    不硬拦）—— 用户真正想排除的，交给弹窗里手填排除词那条硬通道。
    """
    k = str(word or "").strip().lower()
    if not k:
        return True
    for t in theme_keys:
        t = str(t or "").strip().lower()
        if not t:
            continue
        if k == t or k in t or t in k:
            return True
    return False


def global_terms(db, cfg=None) -> FindTerms:
    """「不带任何具体任务」的全局词表：学到的词 + 全局黑名单。

    学习看板用它回答「软件整体上现在喜欢/排斥什么」。手填任务级词不在
    这里 —— 那是每个任务自己的采集配置，看板上不混显。
    """
    learned, avoid_learned = _learned_words(db)
    avoid: list[str] = []
    if cfg is None:
        try:
            from .config import get_active_config
            cfg = get_active_config()
        except Exception:
            cfg = None
    avoid.extend(list(getattr(cfg, "global_blacklist", None) or []))
    avoid.extend(avoid_learned)
    fb_hard, fb_soft = _feedback_words(db)
    # 与「学到的要找词」重叠的反馈词整条作废（既不硬拦也不送审核）：
    # 模型把主题词本身打在拒收图上时，让它去拉审核等于把用户想要的图
    # 一张张拦下来。词仍在账本里看得见、可删，只是不参与采集判定。
    fb_hard = [w for w in fb_hard if not _clashes_with_theme(w, learned)]
    fb_soft = [w for w in fb_soft if not _clashes_with_theme(w, learned)]
    avoid.extend(fb_hard)
    avoid = _clean(avoid)
    learned = [w for w in learned if w.lower() not in {a.lower() for a in avoid}]
    avoid_keys = {a.lower() for a in avoid}
    soft = [w for w in _clean(fb_soft) if w.lower() not in avoid_keys]
    return FindTerms(want=list(learned), avoid=avoid,
                     typed=[], learned=list(learned),
                     avoid_learned=list(avoid_learned),
                     soft_avoid=soft,
                     avoid_feedback=[w for w in _clean(fb_hard)
                                     if w.lower() in avoid_keys])


def collect(db, task, cfg=None) -> FindTerms:
    """合成这次任务的词表。任何一步读不到都退化，不抛异常。"""
    typed = _typed_words(task)
    learned, avoid_learned = _learned_words(db) if db is not None else ([], [])

    # 学到的词排在手填词后面：手填的是用户当下最明确的意图。
    typed_keys = {w.lower() for w in typed}
    learned = [w for w in learned if w.lower() not in typed_keys]
    want = _clean(typed + learned)

    avoid: list[str] = []
    # 跟 engine.merged_blacklist 同一口径：任务黑名单并上全局黑名单。
    # 这里不反过来导 engine（避免循环引用，也方便单独测试本模块）。
    if cfg is None:
        try:
            from .config import get_active_config
            cfg = get_active_config()
        except Exception:
            cfg = None
    avoid.extend(list(getattr(task, "blacklist", None) or []))
    avoid.extend(list(getattr(cfg, "global_blacklist", None) or []))
    avoid.extend(list(getattr(task, "ai_blacklist", None) or []))
    avoid.extend(avoid_learned)
    fb_hard, fb_soft = _feedback_words(db)
    # ⚠ 用户亲手填的「要找什么」不许被模型标签封死。R174 那次投诉的根子
    #   就是模型认错标签 → 用户想要的类目被写进硬排除 → 整个方向下不到图。
    #   真有冲突时，把这条反馈降级成「先送审核看看」，绝不直接拦掉。
    _typed_keys = {w.lower() for w in typed}
    # 真撞上了就整条忽略：既不硬拦，也不送审核。用户亲手填的「要找什么」
    # 优先级最高，不该被模型一次误标反复拉去审核。
    fb_hard = [w for w in fb_hard if w.lower() not in _typed_keys]
    fb_soft = [w for w in fb_soft if w.lower() not in _typed_keys]
    avoid.extend(fb_hard)
    avoid = _clean(avoid)
    # 同一个词既在「要找」又在「别找」时，以「别找」为准（用户明确拉黑优先）
    avoid_keys = {w.lower() for w in avoid}
    want = [w for w in want if w.lower() not in avoid_keys]
    # 还在观察期的词只进看图排除候选，命中先送审核，不进下载前硬拦。
    soft = [w for w in _clean(fb_soft)
            if w.lower() not in avoid_keys and w.lower() not in _typed_keys]

    return FindTerms(want=want, avoid=avoid, typed=typed,
                     learned=learned, avoid_learned=avoid_learned,
                     soft_avoid=soft,
                     avoid_feedback=[w for w in _clean(fb_hard)
                                     if w.lower() in avoid_keys])


def _word_hit(word: str, blob: str) -> bool:
    """跟黑名单同一套口径：英文整词、中文子串。"""
    import re
    r = str(word or "").strip().lower()
    if not r:
        return False
    if (r.startswith("/") and r.endswith("/")) or "*" in r:
        try:
            # V0.0.7：blob 已经转小写，用户写的正则里带大写字母时原来永远
            # 匹配不到（例如 /Cosplay/）。补上忽略大小写，跟文字比对同口径。
            return bool(re.search(r.strip("/").replace("*", ".*"), blob,
                                  re.IGNORECASE))
        except re.error:
            return False
    if r.isascii() and any(c.isalnum() for c in r):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(r)}(?![a-z0-9])", blob))
    return r in blob


def prefilter(terms: FindTerms, *texts: str) -> tuple[str, str]:
    """下载前判定，返回 (结论, 说明)。

    "skip"    : 命中「别找什么」→ 根本不抓
    "match"   : 命中「要找什么」→ 明确对味，优先抓
    "unknown" : 链接文字里看不出来 → 放行，交给下载后的看图判定

    ⚠ 为什么 unknown 不拦：很多图站的文件名就是一串数字，标题里也不带
      任何词。若「没提到词就跳过」，会把整站的图全判掉，用户一张都收不到
      —— 那比多下几张更糟。看图模型会在下载后补上这一刀。
    """
    blob = " ".join(str(t or "") for t in texts).lower()
    for w in terms.avoid:
        if _word_hit(w, blob):
            return "skip", f"命中不要的词：{w}"
    for w in terms.want:
        if _word_hit(w, blob):
            return "match", f"命中要找的词：{w}"
    return "unknown", ""


def matched_want(terms: FindTerms, *texts: str) -> list[str]:
    """链接文字里命中的「要找什么」词，用来给这张图补标签。"""
    blob = " ".join(str(t or "") for t in texts).lower()
    return [w for w in terms.want if _word_hit(w, blob)]


def _host(url: str) -> str:
    try:
        h = (urlsplit(url).netloc or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _bare(url: str) -> bool:
    """是不是「整站/列表」入口。带查询参数、带具体编号的一律不算。"""
    try:
        sp = urlsplit(url)
    except Exception:
        return False
    if sp.query:
        return False
    p = (sp.path or "/").rstrip("/")
    return p in _BARE_PATHS


def _search_word(word: str) -> str:
    """中文词尽量换成站点认得的英文标签；换不了就用原词。"""
    try:
        from .classify.labels_zh import expand_labels
        cands = expand_labels([word])
    except Exception:
        cands = [word]
    for c in cands:
        c = str(c).strip()
        if c and c.isascii() and " " not in c:
            return c
    return str(word).strip()


def search_entries(urls: list[str], want: list[str],
                   avoid: list[str] | None = None) -> tuple[list[str], str]:
    """把「整站入口」变成「按词找图的入口」，返回 (新入口, 说明文字)。

    只在已知支持按词找图的站点、且用户填的是裸首页/裸列表时生效。
    其它情况原样返回 —— 用户给的网址永远优先，绝不擅改。

    V0.0.6：顺手把「别找什么」也拼进站内找图条件（这些站的标签语法
    支持 ``-标签`` 排除）。以前只在链接文字层拦，站内搜索这一层完全
    没参与，用户填的黑名单等于少挡了一道。
    """
    if not urls or not want:
        return list(urls), ""
    # 只把能换成站点认得的英文标签的排除词拼进去；中文原词塞进 URL
    # 多半搜不到东西，不如留在下载前的文字比对那一道拦。
    excl: list[str] = []
    for w in (avoid or [])[:MAX_SEARCH_ENTRIES]:
        s = str(w or "").strip()
        if not s or s.startswith("/") or "*" in s:
            continue
        e = _search_word(s)
        if e and e.isascii() and " " not in e and not e.startswith("-"):
            tag = "-" + e
            if tag not in excl:
                excl.append(tag)
    made: list[str] = []
    note = ""
    for u in urls:
        host = _host(u)
        tpl = SEARCH_TEMPLATES.get(host)
        if tpl and _bare(u):
            words = [w for w in want[:MAX_SEARCH_ENTRIES] if str(w).strip()]
            for w in words:
                q = quote_plus(" ".join([_search_word(w)] + excl))
                made.append(tpl.format(q=q))
            if words:
                note = (f"已按「{'、'.join(words[:3])}」等 {len(words)} 个词去站内找图"
                        + (f"，并排除 {len(excl)} 类" if excl else ""))
        else:
            made.append(u)
    # 去重，保持顺序
    out: list[str] = []
    seen: set[str] = set()
    for x in made:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out, note
