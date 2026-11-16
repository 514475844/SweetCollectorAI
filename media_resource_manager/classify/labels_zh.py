"""本地 AI 主题词的中英自动互译（离线内置词典）。

CLIP 对英文理解最好，所以中文主题词会自动补上英文；反之英文也会补中文，
让排除词/主题词不管用户填哪种语言都能匹配。

用法:  expand_labels(["白丝", "sunset"]) -> ["白丝", "white stockings", ...]
"""
from __future__ import annotations

import re

# 常用视觉词（小写）。值可以是单个词或多个候选（空格分隔的多个词组用 | 分隔）。
ZH2EN: dict[str, str] = {
    "白丝": "white stockings|white pantyhose",
    "黑丝": "black stockings|black pantyhose",
    "丝袜": "stockings|pantyhose",
    # #64：下面这组是「词表桥接」的主战场 —— 用户在「不喜欢」弹窗里手填的是
    # 中文，CLIP 打出来的标签是英文，中间必须有词典这一层，否则中文手填词
    # 学进权重却永远匹配不上英文标签（学习闭环断掉）。文档与测试里反复拿
    # 「金发 → blond hair」当例子，所以这几条一定要在。
    "金发": "blond hair|blonde hair",
    "银发": "silver hair|white hair",
    "黑发": "black hair|dark hair",
    "棕发": "brown hair",
    "卷发": "curly hair",
    "长发": "long hair",
    "短发": "short hair",
    "JK": "japanese school uniform|jk uniform",
    "制服": "uniform",
    "泳装": "swimsuit|swimwear",
    "比基尼": "bikini",
    "婚纱": "wedding dress",
    "和服": "kimono",
    "旗袍": "cheongsam|qipao",
    "女仆": "maid",
    "护士": "nurse",
    "学生": "student",
    "女孩": "girl",
    "少女": "young girl",
    "女人": "woman",
    "美女": "beautiful woman",
    "男人": "man",
    "男孩": "boy",
    "儿童": "child",
    "情侣": "couple",
    "猫": "cat",
    "狗": "dog",
    "风景": "landscape|scenery",
    "日落": "sunset",
    "日出": "sunrise",
    "夜景": "night view|city night",
    "海边": "beach|seaside",
    "雪山": "snow mountain",
    "花": "flower",
    "樱花": "cherry blossom",
    "天空": "sky",
    "云": "clouds",
    "雨": "rain",
    "雪": "snow",
    "汽车": "car",
    "摩托车": "motorcycle",
    "自行车": "bicycle",
    "飞机": "airplane",
    "食物": "food",
    "蛋糕": "cake",
    "咖啡": "coffee",
    "建筑": "architecture|building",
    "城市": "city",
    "动漫": "anime",
    "游戏": "video game|gaming",
    "自拍": "selfie",
    "肖像": "portrait",
    "特写": "close-up",
    "全身": "full body",
    "黑白": "black and white|monochrome",
    "复古": "vintage|retro",
    "可爱": "cute",
    "性感": "sexy",
    "唯美": "aesthetic|beautiful",
    "高清": "high resolution|hd",
    "壁纸": "wallpaper",
    "白色": "white",
    "黑色": "black",
    "红色": "red",
    "蓝色": "blue",
    "粉色": "pink",
    "紫色": "purple",
    "绿色": "green",
    "黄色": "yellow",
    "灰色": "gray|grey",
    "长腿": "long legs",
    "长发": "long hair",
    "短发": "short hair",
    "眼镜": "glasses",
    "帽子": "hat",
    "鞋子": "shoes",
    "高跟鞋": "high heels",
    "运动鞋": "sneakers",
    "裙子": "skirt|dress",
    "短裙": "miniskirt",
    "牛仔裤": "jeans",
    "内衣": "lingerie",
    "睡衣": "pajamas|sleepwear",
    "运动": "sports|fitness",
    "瑜伽": "yoga",
    "跳舞": "dancing",
    "唱歌": "singing",
    "睡觉": "sleeping",
    "微笑": "smiling",
    "哭": "crying",
    "雨伞": "umbrella",
    "手机": "smartphone|phone",
    "电脑": "computer|laptop",
    "书": "book",
    "花束": "bouquet",
    "月亮": "moon",
    "星星": "stars",
    "森林": "forest",
    "河流": "river",
    "湖泊": "lake",
    "瀑布": "waterfall",
    "沙漠": "desert",
    "草原": "grassland|meadow",
}

# 英文 -> 中文（反向自动生成 + 少量补充）
EN2ZH: dict[str, str] = {v: k for k, v in ZH2EN.items()}
EN2ZH.update({
    "stockings": "丝袜",
    "pantyhose": "丝袜",
    "sunset": "日落",
    "sunrise": "日出",
    "landscape": "风景",
    "scenery": "风景",
    "beach": "海边",
    "seaside": "海边",
    "girl": "女孩",
    "woman": "女人",
    "man": "男人",
    "boy": "男孩",
    "cat": "猫",
    "dog": "狗",
    "car": "汽车",
    "flower": "花",
    "city": "城市",
    "anime": "动漫",
    "cute": "可爱",
    "sexy": "性感",
    "portrait": "肖像",
    "selfie": "自拍",
    "white": "白色",
    "black": "黑色",
    "red": "红色",
    "blue": "蓝色",
    "pink": "粉色",
    "purple": "紫色",
    "green": "绿色",
    "yellow": "黄色",
})

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _has_cjk(s: str) -> bool:
    return bool(_CJK.search(s))


def translate_one(label: str) -> list[str]:
    """单个词的中->英 或 英->中 候选（可能多个，用 | 拆开）。"""
    key = label.strip()
    if not key:
        return []
    table = ZH2EN if _has_cjk(key) else EN2ZH
    raw = table.get(key) or table.get(key.lower()) or ""
    return [x.strip() for x in raw.split("|") if x.strip()]


def expand_labels(labels: list[str]) -> list[str]:
    """主题词/排除词双语扩展：中文词补英文候选，英文词补中文。

    顺序保持：原词在前，翻译在后，去重。
    """
    out: list[str] = []
    for lab in labels or []:
        lab = (lab or "").strip()
        if not lab:
            continue
        if lab not in out:
            out.append(lab)
        for t in translate_one(lab):
            if t not in out:
                out.append(t)
    return out
