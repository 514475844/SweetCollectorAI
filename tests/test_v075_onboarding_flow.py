"""V0.0.75 护栏：新手引导重写为 4 步流程（用户拍板的顺序）。

第一步问有没有本地图库 → 让 AI 学偏好（直达学习页自动展开组件）；
第二步索要网站；第三步索要白/黑名单；第四步保存并运行任务。
钉住：
- 4 步齐全、顺序正确（学图库在第一步、保存运行在最后一步）
- 第 1 步有「去学图库」出口且跳 /learning?vocab=1；Learning.vue 认这个参数
- 第 4 步真建任务：taskSave 带白/黑名单字段 + taskCtl start 起跑
- 旧 3 步文案不得复活
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_four_steps_in_order():
    c = _read("media_resource_manager/webapp/src/components/Onboarding.vue")
    tags = [f"第 {i} 步 / 共 4 步" for i in range(1, 5)]
    for t in tags:
        assert t in c, f"缺 {t}"
    # 顺序：图库学习 → 网站 → 黑白名单 → 保存运行
    idx = [c.index(t) for t in tags]
    assert idx == sorted(idx), "四步顺序不对"
    seg = lambda a, b: c[idx[a]:idx[b]]
    assert "本地图片库" in seg(0, 1)
    assert "网站" in seg(1, 2)
    assert "白名单" in seg(2, 3) and "黑名单" in seg(2, 3)
    assert "保存并运行任务" in c[idx[3]:]


def test_step1_jumps_to_vocab_entry():
    c = _read("media_resource_manager/webapp/src/components/Onboarding.vue")
    assert "router.push('/learning?vocab=1')" in c, "第 1 步没有直达图库学习的出口"
    l = _read("media_resource_manager/webapp/src/views/Learning.vue")
    assert "vocab" in l and "showAutoVocab.value = true" in l, \
        "学习页没接 ?vocab=1 自动展开"


def test_step4_creates_task_with_terms():
    c = _read("media_resource_manager/webapp/src/components/Onboarding.vue")
    assert "api.taskSave(" in c and "api.taskCtl('start'" in c
    # 白名单进主题词分级、黑名单进排除词
    assert "ai_label_levels" in c, "白名单没带进任务"
    assert "ai_blacklist" in c, "黑名单没带进任务"
    assert "entry_urls" in c, "网址没带进任务"


def test_old_three_step_copy_gone():
    c = _read("media_resource_manager/webapp/src/components/Onboarding.vue")
    for dead in ("第 1 步 / 共 3 步", "第 2 步 / 共 3 步", "第 3 步 / 共 3 步"):
        assert dead not in c, f"旧三步文案复活：{dead}"


def test_version_and_changelog():
    v = _read("media_resource_manager/__init__.py")
    m = re.search(r'VERSION = "0\.0\.(\d+)"', v)
    assert m and int(m.group(1)) >= 75, "版本号未升到 0.0.75"
    ch = _read("CHANGELOG.md")
    assert "V0.0.75" in ch, "更新日志缺 V0.0.75"
