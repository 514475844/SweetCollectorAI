"""内置站点规则（只在缺失时补种，绝不覆盖用户改过的规则）。

站点规则归用户所有（运行时在 data/rules/），但常见站点每次手抄 YAML 太劝退。
这里内置「已实测可用」的站点适配器（15 个），由用户在历史会话中适配：
启动时发现 rules 目录里没有同名文件才写一份；用户已经改过就完全不碰。

新增内置规则 = 往 BUILTIN_RULES 里加一条即可（key=站点名，value=YAML 文本）。
本文件由 tools/gen_builtin_rules.py 从 data/rules/ 烧入，最后更新 2026-10-01。
"""
from __future__ import annotations

from pathlib import Path

BUILTIN_RULES: dict[str, str] = {}

BUILTIN_RULES["cosplaytele"] = """\
# cosplaytele.com 专属适配器（由 tools/validate_adapters.py 实测生成）
# 仅当你对该站点内容拥有合法采集权限时使用。
name: cosplaytele
base_url: https://cosplaytele.com
allowed_domains:
  - cosplaytele.com

# 列表/分类页：/category/ 下是各 cosplay 合集
list_page_selectors:
  albums:
    kind: css
    expr: "a[href*='/category/'], a[href*='/tag/']"
    attr: href

# 图片页：wp-content/uploads 下的 cosplay 图
image_link_selectors:
  - kind: css
    expr: "img[src*='wp-content']"
    attr: src
  - kind: css
    expr: "img[src*='uploads']"
    attr: src

next_page_selectors:
  next:
    kind: css
    expr: "a.next, a[rel='next'], link[rel='next'], a[class*='next'], a[class*='page']"
    attr: href

max_depth: 2
"""

BUILTIN_RULES["erome"] = """\
# erome.com 专属适配器（由 tools/validate_adapters.py 实测生成）
# 结构：/s/<id> 是相册/视频页，内含正文图。已实测首页能抽到图。
# 仅当你对该站点内容拥有合法采集权限时使用。
name: erome
base_url: https://www.erome.com
allowed_domains:
  - erome.com

list_page_selectors:
  albums:
    kind: css
    expr: "a[href*='/s/']"
    attr: href

image_link_selectors:
  - kind: css
    expr: "img[src*='wp-content']"
    attr: src
  - kind: css
    expr: "img[src*='erome']"
    attr: src
  - kind: css
    expr: "img[src]"
    attr: src

next_page_selectors:
  next:
    kind: css
    expr: "a.next, a[rel='next'], link[rel='next'], a[class*='next'], a[class*='page']"
    attr: href

max_depth: 2
"""

BUILTIN_RULES["everia"] = """\
# everia.club 专属适配器（由 tools/validate_adapters.py 实测生成）
# 仅当你对该站点内容拥有合法采集权限时使用。
name: everia
base_url: https://everia.club
allowed_domains:
  - everia.club
  - karubox.top

# 列表/分类页：帖子多为日期型路径（/2026/09/28/slug）
list_page_selectors:
  albums:
    kind: css
    expr: "a[href*='/20'], a[href*='/category/'], a[href*='/tag/']"
    attr: href

# 图片页：karubox.top 的 wp-content/uploads webp 原图
image_link_selectors:
  - kind: css
    expr: "img[src*='wp-content']"
    attr: src
  - kind: css
    expr: "img[src*='uploads']"
    attr: src
  - kind: css
    expr: "img[src*='webp']"
    attr: src

next_page_selectors:
  next:
    kind: css
    expr: "a.next, a[rel='next'], link[rel='next'], a[class*='next'], a[class*='page']"
    attr: href

max_depth: 2
"""

BUILTIN_RULES["gb84791rx"] = """\
# gb84791rx.com 专属适配器（qo31487tw.com 已跳转至此新域名，由 probe 实测生成）
# 仅当你对该站点内容拥有合法采集权限时使用。
name: gb84791rx
base_url: https://www.gb84791rx.com
allowed_domains:
  - gb84791rx.com
  - www.gb84791rx.com

list_page_selectors:
  albums:
    kind: css
    expr: "a[href*='/photos/'], a[href*='/thread/'], a[href*='/forum/'], a[href*='/t/']"
    attr: href

image_link_selectors:
  - kind: css
    expr: "img[src*='wp-content']"
    attr: src
  - kind: css
    expr: "img[src*='uploads']"
    attr: src
  - kind: css
    expr: "a[href$='.jpg'], a[href$='.jpeg'], a[href$='.png'], a[href$='.webp']"
    attr: href

next_page_selectors:
  next:
    kind: css
    expr: "a.next, a[rel='next'], link[rel='next'], a[class*='next'], a[class*='page']"
    attr: href

max_depth: 2
"""

BUILTIN_RULES["generic-gallery"] = """\
# 通用图库适配器：覆盖一批版式相似的图库/相册站（懒加载缩略图 + og:image 原图）。
# 这些站的具体版式如有出入，可复制本文件改名后按站点微调选择器。
# 仅当你对目标内容拥有合法采集权限时使用。
name: generic-gallery
base_url: https://www.pornpics.com
allowed_domains:
  - pornpics.com
  - pornpics.center
  - xx.knit.bid
  - knit.bid
  - legs-a-hadaka.jp
  - hadaka.jp
  - pimpandhost.com
  - viralporn.com
  - ososedki.com
  - everia.club
  - girlstop.info
  - balbums.st
  - xchina.co
  - suuss.uk
  - naiquan05.com
  - xsijishe.com
  - erome.com
  - qo314877tw.com
  - pichunter.com
  - spankbang.com
  - cosplaytele.com

# 列表页：相册/图集链接（通用模式）
list_page_selectors:
  albums:
    kind: css
    expr: "a[href*='gallery'], a[href*='album'], a[href*='photos'], a[href*='pict']"
    attr: href

# 图片：懒加载属性优先，og:image 自动转原图（引擎内置提取）
image_link_selectors:
  - kind: css
    expr: "img[data-src]"
    attr: data-src
  - kind: css
    expr: "img[data-lazy-src]"
    attr: data-lazy-src
  - kind: css
    expr: "img[data-original]"
    attr: data-original
  - kind: css
    expr: "img[src*='thumb']"
    attr: src
  - kind: css
    expr: "img[src*='/medium'], img[src*='/small']"
    attr: src

# 已知缩略图→原图主机替换（常见规律，命中才生效）
image_url_replace:
  - from: "/thumbs/"
    to: "/"

# 翻页链接（与内置各专属适配器一致的通用写法）：
# link[rel='next'] / a.next / a[class*='next'] / a[class*='page'] 都算续页。
# 缺 next_page_selectors 会导致列表翻页完全不跟随（与 pictoa 第二根因同类）；
# 实测 balbums.st 列表页被识别为 LIST 但 next=0，翻页全丢。补上后 BFS 走
# 「翻页同深度」分支，翻一整本相册/翻完列表都不消耗抓取深度。
next_page_selectors:
  next:
    kind: css
    expr: "a.next, a[rel='next'], link[rel='next'], a[class*='next'], a[class*='page']"
    attr: href

max_depth: 2
"""

BUILTIN_RULES["girlstop"] = """\
# en.girlstop.info 专属适配器（实测该站走 Cloudflare，裸 httpx 请求会被 403 JS 挑战拦截）
# 注意：本软件 fetcher 基于 httpx（无 JS 引擎），若你的代理不是 Cloudflare 绕过型，
# 仍可能 403。选择器按常见图库结构预置，等代理能过 CF 时即可用。
# 仅当你对该站点内容拥有合法采集权限时使用。
name: girlstop
base_url: https://en.girlstop.info
allowed_domains:
  - girlstop.info
  - en.girlstop.info

list_page_selectors:
  albums:
    kind: css
    expr: "a[href*='/gallery/'], a[href*='/album/'], a[href*='/photos/']"
    attr: href

image_link_selectors:
  - kind: css
    expr: "img[data-src]"
    attr: data-src
  - kind: css
    expr: "img[src*='wp-content']"
    attr: src
  - kind: css
    expr: "a[href$='.jpg'], a[href$='.jpeg'], a[href$='.png'], a[href$='.webp']"
    attr: href

next_page_selectors:
  next:
    kind: css
    expr: "a.next, a[rel='next'], link[rel='next'], a[class*='next'], a[class*='page']"
    attr: href

max_depth: 2
"""

BUILTIN_RULES["naiquan05"] = """\
# naiquan05.com 专属适配器（由 tools/validate_adapters.py 实测生成）
# 仅当你对该站点内容拥有合法采集权限时使用。
name: naiquan05
base_url: https://www.naiquan05.com
allowed_domains:
  - naiquan05.com
  - nqtu.234669.xyz

# 列表/分类页：帖子多为日期型路径（/2023/09/...）或 ?p= 形式
list_page_selectors:
  albums:
    kind: css
    expr: "a[href*='/20'], a[href*='?p=']"
    attr: href

# 图片页：封面/正文图多为 data-src 懒加载或 wp-content/uploads
image_link_selectors:
  - kind: css
    expr: "img[data-src]"
    attr: data-src
  - kind: css
    expr: "img[src*='wp-content']"
    attr: src
  - kind: css
    expr: "img[src*='uploads']"
    attr: src

next_page_selectors:
  next:
    kind: css
    expr: "a.next, a[rel='next'], link[rel='next'], a[class*='next'], a[class*='page']"
    attr: href

max_depth: 2
"""

BUILTIN_RULES["ososedki"] = """\
# ososedki.com 专属适配器（实测该站走 Cloudflare，裸 httpx 请求会被 403 JS 挑战拦截）
# 注意：本软件 fetcher 基于 httpx（无 JS 引擎），若你的代理不是 Cloudflare 绕过型，
# 仍可能 403。选择器按常见图库结构预置，等代理能过 CF 时即可用。
# 仅当你对该站点内容拥有合法采集权限时使用。
name: ososedki
base_url: https://ososedki.com
allowed_domains:
  - ososedki.com

list_page_selectors:
  albums:
    kind: css
    expr: "a[href*='/album/'], a[href*='/gallery/'], a[href*='/post/']"
    attr: href

image_link_selectors:
  - kind: css
    expr: "img[data-src]"
    attr: data-src
  - kind: css
    expr: "img[src*='wp-content']"
    attr: src
  - kind: css
    expr: "a[href$='.jpg'], a[href$='.jpeg'], a[href$='.png'], a[href$='.webp']"
    attr: href

next_page_selectors:
  next:
    kind: css
    expr: "a.next, a[rel='next'], link[rel='next'], a[class*='next'], a[class*='page']"
    attr: href

max_depth: 2
"""

BUILTIN_RULES["pichunter"] = """\
# pichunter.com 专属适配器（由 tools/validate_adapters.py 实测生成）
# 仅当你对该站点内容拥有合法采集权限时使用。
name: pichunter
base_url: https://www.pichunter.com
allowed_domains:
  - pichunter.com
  - cdn1337.pichunter.com

# 列表/分类页：gallery / photos / tags 都是合集入口
list_page_selectors:
  albums:
    kind: css
    expr: "a[href*='/gallery/'], a[href*='/photos/'], a[href*='/tags/']"
    attr: href

# 图片页：正文图多为 data-src 懒加载，CDN 在 cdn1337.pichunter.com/media/posts
# 注意：URL 里的 /conversions/ 是站点的缩放版，仍是可下载的有效图片
image_link_selectors:
  - kind: css
    expr: "img[data-src]"
    attr: data-src
  - kind: css
    expr: "img[src*='media/posts']"
    attr: src

next_page_selectors:
  next:
    kind: css
    expr: "a.next, a[rel='next'], link[rel='next'], a[class*='next'], a[class*='page']"
    attr: href

max_depth: 2
"""

BUILTIN_RULES["pictoa"] = """\
# pictoa.com 适配器规则
# 仅当你对目标内容拥有合法采集权限时使用（GUI 的「授权站点」会记录依据）。
# 本规则不会绕过任何访问控制。
name: pictoa
base_url: https://www.pictoa.com
allowed_domains: [pictoa.com, t1.pictoa.com, s1.pictoa.com]

# 列表页 + 相册页都靠这个发现相册链接：
# 首页/翻页页 → 同类相册封面；相册页 → 同相册下一页(…/<id>/N.html) 与“相关相册”。
list_page_selectors:
  albums:
    kind: css
    expr: "a[href*='/albums/']"
    attr: href

# 相册页：图片懒加载，真实地址在 data-src（src 只是占位图 /img/1.png）。
# 早期规则误写成 data-lazy-src → 整页 0 命中，每页只靠 og:image 兜 1 张，
# 这是“只抓到 100 来张”的头号根因。data-lazy-src 作为兜底保留（部分页面可能用它）。
image_link_selectors:
  - kind: css
    expr: "img[data-src*='/media/galleries/']"
    attr: data-src
  - kind: css
    expr: "img[data-lazy-src*='/media/galleries/']"
    attr: data-lazy-src
  - kind: css
    expr: "img[src*='/media/galleries/']"
    attr: src

# 翻页链接（对所有页面类型生效）：
#  list_relnext → 列表页 <link rel="next">（page2.html … page21.html）
#  list_pager   → 列表页 /pageN.html 分页锚点（冗余兜底）
#  album_next   → 相册页 <a id="next" class="gallery-link"> 进同相册下一页
# 缺 next_page_selectors 会导致列表翻页 / 相册内翻页完全不跟随（第二个根因）。
next_page_selectors:
  list_relnext:
    kind: css
    expr: "link[rel='next']"
    attr: href
  list_pager:
    kind: css
    expr: "a[href*='/page']"
    attr: href
  album_next:
    kind: css
    expr: "a#next.gallery-link"
    attr: href

# 关键：t1. 是缩略图(320x400)；s1. 才是原图。s2. 也已失效，统一映射到 s1.。
image_url_replace:
  - from: "t1.pictoa.com"
    to: "s1.pictoa.com"
  - from: "s2.pictoa.com"
    to: "s1.pictoa.com"

# 抓取深度由“任务设置 → 抓取深度”控制（引擎不读这里的 max_depth），
# 默认任务深度 3 已够 list 翻 3 页 + 相册翻 3 页；想抓更全请在任务里调大。
max_depth: 5
"""

BUILTIN_RULES["pimpandhost"] = """\
# pimpandhost.com 专属适配器（由 tools/validate_adapters.py 实测生成）
# 图床型站点：/post/<id> 或 /g/<id> 是相册页，内含原图。已实测首页可访问（含图需进相册页）。
# 仅当你对该站点内容拥有合法采集权限时使用。
name: pimpandhost
base_url: https://www.pimpandhost.com
allowed_domains:
  - pimpandhost.com

list_page_selectors:
  albums:
    kind: css
    expr: "a[href*='/post/'], a[href*='/g/'], a[href*='/s/']"
    attr: href

image_link_selectors:
  - kind: css
    expr: "img[src*='pimpandhost']"
    attr: src
  - kind: css
    expr: "a[href$='.jpg'], a[href$='.jpeg'], a[href$='.png'], a[href$='.webp']"
    attr: href
  - kind: css
    expr: "img[src]"
    attr: src

next_page_selectors:
  next:
    kind: css
    expr: "a.next, a[rel='next'], link[rel='next'], a[class*='next'], a[class*='page']"
    attr: href

max_depth: 2
"""

BUILTIN_RULES["pornpics"] = """\
# pornpics.center / pornpics.com 站点规则（已实测）
# 首页 = 列表页：/a/<id>/ 是相册入口，翻页用 <link rel="next">
# 相册页 = 原图：<a href="https://cdn.pornpics.center/galleries/xx/yy/0_716.jpg">
# 缩略图（.../0_t.jpg）只有 225x300，所以用锚点 href 拿原图，不拿 img[src]。
# 仅当你对该站点内容拥有合法采集权限时使用。
name: pornpics
base_url: https://www.pornpics.center
allowed_domains:
  - pornpics.center
  - www.pornpics.center
  - cdn.pornpics.center
  - pornpics.com
  - www.pornpics.com

list_page_selectors:
  albums:
    kind: css
    # 首页用相对路径 /a/<id>/，相册页里的"相关推荐"用绝对地址，所以都匹配
    expr: "a[href*='/a/']"
    attr: href

album_link_selectors:
  full:
    kind: css
    expr: "a[href$='.jpg'], a[href$='.jpeg'], a[href$='.png'], a[href$='.webp']"
    attr: href

image_link_selectors:
  - kind: css
    expr: "a[href$='.jpg'], a[href$='.jpeg'], a[href$='.png'], a[href$='.webp']"
    attr: href

next_page_selectors:
  next:
    kind: css
    expr: "link[rel='next']"
    attr: href

max_depth: 1
delay: 1.0
concurrency: 4
"""

BUILTIN_RULES["spankbang"] = """\
# it.spankbang.com 专属适配器（实测该站走 Cloudflare，裸 httpx 请求会被 403 JS 挑战拦截）
# 注意：本软件 fetcher 基于 httpx（无 JS 引擎），若你的代理不是 Cloudflare 绕过型，
# 仍可能 403。选择器按常见图库/视频站结构预置，等代理能过 CF 时即可用。
# 仅当你对该站点内容拥有合法采集权限时使用。
name: spankbang
base_url: https://it.spankbang.com
allowed_domains:
  - spankbang.com
  - it.spankbang.com

list_page_selectors:
  albums:
    kind: css
    expr: "a[href*='/video/'], a[href*='/gallery/'], a[href*='/album/']"
    attr: href

image_link_selectors:
  - kind: css
    expr: "img[data-src]"
    attr: data-src
  - kind: css
    expr: "img[src*='wp-content']"
    attr: src
  - kind: css
    expr: "a[href$='.jpg'], a[href$='.jpeg'], a[href$='.png'], a[href$='.webp']"
    attr: href

next_page_selectors:
  next:
    kind: css
    expr: "a.next, a[rel='next'], link[rel='next'], a[class*='next'], a[class*='page']"
    attr: href

max_depth: 2
"""

BUILTIN_RULES["xchina"] = """\
# xchina.co 专属适配器（由 tools/validate_adapters.py 实测生成）
# 注意：该站每个「系列页」只展示 1 张封面图，更多图在 /videos/ 系列页更深处；
# 已实测系列页能抽到图（封面 + 少量内容图），比 generic-gallery 更精准。
# 仅当你对该站点内容拥有合法采集权限时使用。
name: xchina
base_url: https://www.xchina.co
allowed_domains:
  - xchina.co
  - tw.xchina.co
  - huangguo.video
  - upload.xchina.io

list_page_selectors:
  albums:
    kind: css
    expr: "a[href*='/photos/'], a[href*='/videos/']"
    attr: href

image_link_selectors:
  - kind: css
    expr: "img[src*='upload.xchina.io']"
    attr: src
  - kind: css
    expr: "img[src*='huangguo']"
    attr: src
  - kind: css
    expr: "img[src*='wp-content']"
    attr: src
  - kind: css
    expr: "img[src*='uploads']"
    attr: src

next_page_selectors:
  next:
    kind: css
    expr: "a.next, a[rel='next'], link[rel='next'], a[class*='next'], a[class*='page']"
    attr: href

max_depth: 3
"""

BUILTIN_RULES["xxknit"] = """\
# xx.knit.bid 专属适配器（由 tools/validate_adapters.py 实测生成）
# 仅当你对该站点内容拥有合法采集权限时使用。
name: xxknit
base_url: https://xx.knit.bid
allowed_domains:
  - xx.knit.bid

# 列表/分类页：/topic/ 下是各写真/cosplay 图集
list_page_selectors:
  albums:
    kind: css
    expr: "a[href*='/topic/']"
    attr: href

# 图片页：static/images 下的图集原图
image_link_selectors:
  - kind: css
    expr: "img[src*='static/images']"
    attr: src
  - kind: css
    expr: "img[src*='uploads']"
    attr: src

next_page_selectors:
  next:
    kind: css
    expr: "a.next, a[rel='next'], link[rel='next'], a[class*='next'], a[class*='page']"
    attr: href

max_depth: 2
"""


BUILTIN_RULES["uuss4khd"] = """\
# qhrzv.uuss.uk / 4khd.com 专属适配器（实测生成 2026-10-01）
# 直连 httpx 即可 200（Cloudflare 未拦普通浏览器 UA）；图片走 i0.wp.com/pic.4khd.com
# Photon CDN，直出 <img src>（非懒加载）；专辑翻页 = <slug>.html/2、/3 …
# 仅当你对该站点内容拥有合法采集权限时使用。
name: uuss4khd
base_url: https://qhrzv.uuss.uk
allowed_domains:
  - qhrzv.uuss.uk
  - uuss.uk
  - 4khd.com
  - www.4khd.com
  - pic.4khd.com
  - i0.wp.com
  - i1.wp.com
  - i2.wp.com

list_page_selectors:
  albums:
    kind: css
    expr: "a[href*='/content/']"
    attr: href

image_link_selectors:
  - kind: css
    expr: "img[src*='4khd.com']"
    attr: src

next_page_selectors:
  next:
    kind: css
    expr: "a[href*='.html/'], a[href*='?p='], a[href*='query-3-page=']"
    attr: href

max_depth: 2
max_pages: 50
delay: 1.0
concurrency: 4
"""


def seed_builtin_rules(rules_dir: Path) -> list[str]:
    """把缺失的内置规则写进 rules 目录，返回新写入的名字列表。"""
    written: list[str] = []
    try:
        rules_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return written
    for name, text in BUILTIN_RULES.items():
        for p in (rules_dir / f"{name}.yaml", rules_dir / f"{name}.yml",
                  rules_dir / f"{name}.json"):
            if p.exists():
                break
        else:
            try:
                (rules_dir / f"{name}.yaml").write_text(text, encoding="utf-8")
                written.append(name)
            except Exception:
                pass
    return written
