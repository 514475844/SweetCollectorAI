"""V0.0.41：限速时段表「看着像没生效」的真实原因 —— 界面只说了一句「不限速」。

用户原话：「自己看，都修改了几次了，还是不行？？」

截图里表配得对、开关也开着，但那会儿正是 00:00–08:00 这个**不限速**时段，
界面只写「当前：不限速（时段 00:00-08:00）」，于是看起来像又坏了。
修法不是改表，而是把状态说完整：现在几点、现在怎么限、下次什么时候变。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "media_resource_manager" / "webapp" / "src"


def _web(rel: str) -> str:
    p = WEB / rel
    assert p.is_file(), f"缺文件 {rel}"
    return p.read_text(encoding="utf-8")


PLAN = [
    {"start": "00:00", "end": "08:00", "kbps": 0},
    {"start": "08:00", "end": "24:00", "kbps": 500},
]


def test_status_says_now_and_next_when_unlimited():
    from media_resource_manager.rateplan import plan_status

    # 00:30 —— 落在夜里那条不限速时段
    st = plan_status(PLAN, True, now=30)
    assert st["now_kbps"] == 0
    assert "现在不限速" in st["now_text"]
    assert "00:00" in st["now_text"] and "08:00" in st["now_text"], \
        "没说清是落在哪个时段里不限速的"
    assert st["next_clock"] == "08:00" and st["next_kbps"] == 500
    assert "08:00" in st["next_text"] and "500" in st["next_text"]


def test_status_says_limited_during_day_slot():
    from media_resource_manager.rateplan import plan_status

    st = plan_status(PLAN, True, now=10 * 60 + 5)      # 10:05
    assert st["now_kbps"] == 500
    assert "现在限速 500" in st["now_text"]
    # 今天的边界都过了 → 下次变化在明天 00:00（不限速）
    assert st["next_day"] is True and st["next_clock"] == "00:00"
    assert st["next_kbps"] == 0 and "不限速" in st["next_text"]


def test_status_handles_disabled_and_empty_plan():
    from media_resource_manager.rateplan import plan_status

    st = plan_status(PLAN, False, now=600)
    assert st["now_text"].startswith("现在不限速")
    assert "没有打开" in st["now_text"]
    assert st["next_text"] == ""

    st2 = plan_status([], True, now=600)
    assert "还没有配置" in st2["now_text"]


def test_status_without_any_slot_covers_now():
    """表里只有 09:00-10:00 限速，现在是 12:00 ⇒ 现在不限速、下次明天 09:00。"""
    from media_resource_manager.rateplan import plan_status

    st = plan_status([{"start": "09:00", "end": "10:00", "kbps": 800}],
                     True, now=12 * 60)
    assert st["now_kbps"] == 0 and "不在任何时段" in st["now_text"]
    assert st["next_clock"] == "09:00" and st["next_kbps"] == 800
    assert st["next_day"] is True


def test_settings_api_exposes_status():
    w = (ROOT / "media_resource_manager" / "webui.py").read_text(encoding="utf-8")
    assert 'out["rate_plan_status"] = plan_status(' in w, \
        "设置接口没把结构化状态给前端"


def test_settings_page_uses_status_block():
    vue = _web("views/Settings.vue")
    assert "planStatus" in vue and "planStatus.now_text" in vue
    assert "planStatus.next_text" in vue, "没写「下次什么时候变」"
    # 旧的「当前：限速/不限速」一句话写法不许回来（皮肤那处「当前：xx」不算）
    assert "当前：{{ s.rate_plan_now }}" not in vue
    assert "s.rate_plan_now" not in vue.split("<template>", 1)[1]
    # 每行右侧的 0 要写成「本时段不限速」，别跟"整体不限速"混
    assert "本时段不限速" in vue


def test_version_bumped():
    import re as _re
    from media_resource_manager import VERSION
    m = _re.fullmatch(r"0\.0\.(\d+)", VERSION)
    assert m and int(m.group(1)) >= 41, f"版本回退：{VERSION}"
