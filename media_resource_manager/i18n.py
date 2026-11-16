# -*- coding: utf-8 -*-
"""i18n：标签/文案的取词层（为后期语言包留接口）。

r40 起因：界面与图库里直接把站点给的英文标签原样显示（NSFW / stockings /
pantyhose / cute …），用户看不懂。现在统一走 `label_text()` 过一道映射，
**没命中的原样返回**（不丢信息，API 与导入导出仍然是原始英文）。

设计约定（后期加语言包不用改调用点）：

* `label_text(tag, lang=None)` = 「该语言下的标签名」，lang 省略则取
  :func:`current_lang`；语言包 > 内置中文映射（仅中文界面）> 原文；
* `tag_key(tag)`    = 「稳定的标签标识」，永远返回归一化后的英文 key，
  存库/比对用途 —— 语言包换了，`tag_key` 不变，历史数据不失效；
* `tr(text)`        = 「界面文案」占位，现阶段直接返回原文，
  以后替换成 gettext / json 语言包即可，调用点不用动。

语言包文件约定：`media_resource_manager/locale/<lang>/LC_MESSAGES/*.json`，
形如 {"NSFW": "不适宜", "cute": "可爱"}，`LOCALES` 里登记可用语言。
"""
from __future__ import annotations

import os
from pathlib import Path

LOCALES = ("zh_CN", "en_US")
_DEFAULT = "zh_CN"

# R152-F7：语言包缓存（load_locale 现在被 label_text 走热路径调用，不能每次读盘）
_LOCALE_CACHE: dict = {}

# ---------------------------------------------------------------- 标签映射 --
# key 一律小写英文；值就是中文界面名。中文词本身也留在表里（原样映射）。
TAG_CN = {
    # 袜 / 腿（本项目高频）
    "nsfw": "不适宜",
    "cute": "可爱",
    "sexy": "性感",
    "beautiful": "漂亮",
    "beauty": "美女",
    "pretty": "好看",
    "legs": "美腿",
    "thighs": "大腿",
    "feet": "脚部",
    "foot": "脚部",
    "sole": "脚底",
    "toes": "脚趾",
    "calves": "小腿",
    "knees": "膝盖",
    "stocks": "丝袜",
    "stocking": "丝袜",
    "stockings": "丝袜",
    "pantyhose": "连裤袜",
    "tights": "连裤袜",
    "socks": "短袜",
    "sock": "短袜",
    "knee socks": "中筒袜",
    "over knee socks": "过膝袜",
    "kneehigh socks": "过膝袜",
    "thigh high socks": "长筒袜",
    "boat socks": "船袜",
    "ankle socks": "船袜",
    "loose socks": "船袜",
    "white stockings": "白丝",
    "black stockings": "黑丝",
    "gray stockings": "灰丝",
    "grey stockings": "灰丝",
    "brown stockings": "棕丝",
    "white socks": "白袜",
    "black socks": "黑袜",
    "white pantyhose": "白连裤袜",
    "black pantyhose": "黑连裤袜",
    "leggings": "打底裤",
    "pants": "裤子",
    "skirt": "裙子",
    "shorts": "短裤",
    "dress": "连衣裙",
    "uniform": "制服",
    "school uniform": "校服",
    "seifuku": "校服",
    "maid": "女仆",
    "cosplay": "角色扮演",
    "costume": "cosplay",

    # 服装 / 部位
    "bra": "文胸",
    "brassiere": "文胸",
    "lingerie": "内衣",
    "naked": "裸体",
    " nude": "裸体",
    "nudity": "裸体",
    "topless": "露乳",
    "covering": "遮点",
    "blurry": "模糊",
    "cleavage": "乳沟",
    "breasts": "胸部",
    "breast": "胸部",
    "ass": "臀部",
    "buttocks": "臀部",
    "hip": "胯",
    "belly": "腹部",
    "armpits": "腋下",
    "armpit": "腋下",
    "hands": "手部",
    "gloves": "手套",
    "hat": "帽子",
    "cap": "帽子",
    "heels": "高跟鞋",
    "high heels": "高跟鞋",
    "shoes": "鞋子",
    "boots": "靴子",
    "sneakers": "运动鞋",
    "hair": "头发",
    "long hair": "长发",
    "short hair": "短发",
    "twintails": "双马尾",
    "ponytail": "马尾",
    "bald": "光头",
    "glasses": "眼镜",
    "makeup": "妆容",

    # 场景 / 动作 / 属性
    "indoor": "室内",
    "outdoor": "室外",
    "bed": "床上",
    "bedroom": "卧室",
    "bath": "浴室",
    "bathroom": "浴室",
    "bathtub": "浴缸",
    "kitchen": "厨房",
    "classroom": "教室",
    "school": "学校",
    "garden": "花园",
    "window": "窗边",
    "mirror": "镜子",
    "car": "车内",
    "sunlight": "日光",
    "night": "夜景",
    "rain": "雨天",
    "snow": "雪景",
    "mountain": "山景",
    "beach": "海边",
    "pool": "泳池",
    "swimming pool": "泳池",
    "water": "水",
    "sky": "天空",
    "city": "城市",

    # 相机 / 画质
    "photo": "照片",
    "portrait": "人像",
    "landscape": "风景",
    "close-up": "特写",
    "closeup": "特写",
    "profile": "侧脸",
    "front": "正面",
    "back": "背面",
    "top-down": "俯拍",
    "high angle": "俯拍",
    "low angle": "仰拍",
    "group": "多人",
    "solo": "单人",
    "1girl": "单人",
    "multiple girls": "多人",
    "waterfall": "瀑布",
    "monochrome": "黑白",
    "grayscale": "黑白",
    "color": "彩色",
    "blurry background": "背景虚化",
    "depth of field": "景深",
    "film grain": "胶片颗粒",
    "vintage": "复古",
    "retro": "复古",

    # 已存在的中文标签（保留，避免重复映射）
    "丝袜": "丝袜", "可爱": "可爱", "白丝": "白丝", "黑丝": "黑丝",
    "短袜": "短袜", "船袜": "船袜", "袜": "袜", "裤袜": "裤袜",
    "连裤袜": "连裤袜", "美腿": "美腿",
}

# 常见复合写法：先拆分再逐词翻译，拼不回来就退回整串查表
_COMPOSITE = (
    ("over knee", "过膝"), ("knee high", "过膝"), ("thigh high", "长筒"),
    ("white ", "白"), ("black ", "黑"), ("blue ", "蓝"),
    ("red ", "红"), ("pink ", "粉"), ("green ", "绿"),
    ("high heels", "高跟鞋"),
)


def tag_key(tag: str) -> str:
    """稳定标识：归一化为「小写 + 折叠空格」，永远给英文 key 用。"""
    return " ".join(str(tag).strip().lower().split())


def _wants_cn(lang: str) -> bool:
    """R152-F7：只有中文界面才套中文映射，英文界面一律回原文。

    旧实现收下 ``lang`` 却从不使用它（``en_US`` 也照样返回"丝袜"），
    语言参数形同虚设。判断按前缀，``zh`` / ``zh_CN`` / ``zh_TW`` 都算中文。
    """
    return str(lang or _DEFAULT).lower().startswith("zh")


def label_text(tag: str, lang: str = None) -> str:
    """界面上要显示的标签名。

    取词优先级（R152-F7，改之前 ``lang`` 与语言包都是死参数）：
      1. 语言包 ``locale/<lang>/LC_MESSAGES/*.json``（用户可覆盖，最高优先）；
      2. 中文界面才套内置 :data:`TAG_CN` 映射；
      3. 都没命中 → 原样返回（不丢信息，存库/比对仍是原始英文）。
    """
    t = str(tag).strip()
    if not t:
        return t
    lang = lang or current_lang() or _DEFAULT
    pack = load_locale(lang)
    if pack:
        if t in pack:
            return str(pack[t])
        k = tag_key(t)
        if k in pack:
            return str(pack[k])
    if not _wants_cn(lang):
        return t
    if t in TAG_CN:
        return TAG_CN[t]
    k = tag_key(t)
    if k in TAG_CN:
        return TAG_CN[k]
    # 复合写法：white stockings / black pantyhose 这类
    for en, cn in _COMPOSITE:
        if k.startswith(en + " "):
            rest = k[len(en) + 1:]
            return _COMPOSITE_FIX.get(en, cn) + label_text(rest, lang)
    return t


# 组合词里「颜色前缀」单独成词的显示（白 + 丝袜 = 白丝）
_COMPOSITE_FIX = {
    "white": "白", "black": "黑", "blue": "蓝",
    "red": "红", "pink": "粉", "green": "绿",
}


def tr(text: str, *args) -> str:
    """界面文案占位：现阶段返回原文；以后接语言包时只改这里。"""
    if not args:
        return text
    try:
        return text % args
    except Exception:
        return text


def translate_tags(tags, lang: str = None) -> list:
    """一批标签 → 界面显示名（去重保序）。"""
    out, seen = [], set()
    for t in tags or []:
        v = label_text(t, lang)
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def locale_dir() -> Path:
    return Path(__file__).resolve().parent / "locale"


def load_locale(lang: str = _DEFAULT) -> dict:
    """读语言包（不存在就空），返回 {key: 显示名}。

    R152-F7：以前没有任何调用方（死代码），现在由 :func:`label_text` 每次取词
    时调用 —— 因为进了热路径（图库几千个标签），结果按语言缓存；
    改语言包后调 :func:`clear_locale_cache` 生效。
    """
    lang = str(lang or _DEFAULT)
    hit = _LOCALE_CACHE.get(lang)
    if hit is not None:
        return hit
    data: dict = {}
    p = locale_dir() / lang / "LC_MESSAGES"
    if p.exists():
        import json
        for f in sorted(p.glob("*.json")):
            try:
                got = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(got, dict):
                data = got
                break
    _LOCALE_CACHE[lang] = data
    return data


def clear_locale_cache(lang: str = None) -> None:
    """R152-F7：语言包缓存失效（换语言 / 改包后调）。"""
    if lang is None:
        _LOCALE_CACHE.clear()
    else:
        _LOCALE_CACHE.pop(str(lang), None)


def current_lang() -> str:
    """临时：从环境变量取；以后接设置里的「界面语言」下拉。"""
    return os.environ.get("MRM_LANG", _DEFAULT)
