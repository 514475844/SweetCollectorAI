"""R169 护栏：网页词表行式排版 + 学习权重防再污染（执行方笔记本棒）。

订单 R169_KEYWORDS_LAYOUT_AND_LEARNING_POLLUTION.md 两个 Bug：

  Bug1  网页任务表单词表排版乱 —— 要求「一行一行」的分组排版，
        白名单三档各自成组、黑名单独立成组，组内行对齐。视觉参照桌面端。
  Bug2  黑名单/学习看板出现用户从未输入的高权重词（AI 误标污染）。
        要求：权重封顶 + 时间衰减 + 碎片词不入统计 + 一键清模型自动词
        + 只信用户动作的负样本。

⚠ 本文件的所有判据都锚在**磁盘上真实那份** app.js / app.css / repo.py，
  不复制副本（同 R146/R174 的做法）；源码一改判据立刻跟着变。
⚠ 静态判据只钉"结构对不对"，真实观感留给台式机真机复测。
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APPJS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
APPCSS = ROOT / "media_resource_manager" / "web" / "static" / "css" / "app.css"
REPO = ROOT / "media_resource_manager" / "repo.py"
DB = ROOT / "media_resource_manager" / "db.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _code(src: str) -> str:
    """去掉行注释后返回（避免注释里的字面量把判据顶红）。

    ⚠ 只去 `#` 行注释，不碰字符串 —— 本项目的护栏多次踩过
      「注释里写了字段名 ⇒ 按字面量判的用例假红」这个坑。
    """
    out = []
    for line in src.splitlines():
        s = line.split("#", 1)[0] if line.lstrip().startswith("#") else line
        out.append(s)
    return "\n".join(out)


def _fn_src(path: Path, name: str) -> str:
    """AST 取函数源码切块（不靠正则猜缩进）。"""
    src = _read(path)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


# ==================================================================
# 一、Bug1：网页词表「一行一行」排版
# ==================================================================
def _task_form_seg() -> str:
    js = _read(APPJS)
    return js.split('id="task-fields"', 1)[1].split("</form>", 1)[0]

def _brace_body(js: str, start: int) -> str:
    """从 `function xxx(` 起，按花括号配平取出函数体（含嵌套）。"""
    i = js.index("{", start)
    depth = 0
    for j in range(i, len(js)):
        c = js[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return js[i:j + 1]
    return js[i:]


# ==================================================================
# 二、Bug2：防再污染
# ==================================================================
def _db(tmp_path, name="r169.db"):
    from media_resource_manager.db import Database
    return Database(tmp_path / name)


def test_fragment_labels_are_recognized():
    """碎片词判定：单拉丁字母/单符号/纯数字 = 碎片；正常类别词不是。"""
    from media_resource_manager.repo import _is_fragment_label
    for frag in ("a", "x", "1", "?", "//", "...", "123", "\U0001f600", ""):
        assert _is_fragment_label(frag), f"{frag!r} 应判为碎片词"
    for ok in ("cat", "金发", "红丝", "blond hair", "a1", "猫", "狗"):
        assert not _is_fragment_label(ok), f"{ok!r} 不该判为碎片词"


def test_fragment_labels_do_not_enter_model_stats(tmp_path):
    """★ 核心：模型学习入口（``learn``）里的碎片词**不入统计**。

    这正是用户投诉里「红丝」那类切词残片的来源 —— 它们不是"类别"，
    进统计只会占着看板 Top-N 的位置把真词挤下去。

    ⚠ 过滤的位置是 **``PreferenceLearner.learn``**（模型标签入库的唯一咽喉），
      **不是** ``repo.set_preference``（低层写入器）。测试必须打在正确那一层：
      打在低层会把"导入偏好 / 权重明细手工改 / 测试造数据"一起改掉行为。
    """
    from media_resource_manager.classify.preference import PreferenceLearner
    db = _db(tmp_path)
    try:
        L = PreferenceLearner(db, 0)
        L.learn("blacklist", ["a"])       # 单拉丁字母 = 碎片
        L.learn("blacklist", ["123"])     # 纯数字 = 碎片
        L.learn("blacklist", ["??"])      # 纯符号 = 碎片
        L.learn("blacklist", ["红丝"])     # 正常词要留下
        got = {r["label"] for r in db.query(
            "SELECT label FROM preferences WHERE task_id=0")}
        assert got == {"红丝"}, f"碎片词没被挡住：{got}"
    finally:
        db.close()


def test_user_typed_fragment_is_kept(tmp_path):
    """★ 反向护栏：**用户手填**的碎片词照记 —— 绝不丢用户填的词。

    过滤看的是**来源**而不是词本身：同一个"看起来像碎片"的词，用户手填
    就要留下（他可能就是只想排除这一个字符），模型自动进来就丢掉。

    ⚠ 用例刻意用**同一个词**（"a"）走两条来源：这样才能真的证明
      "过滤看的是来源、不是词"。用两个不同的词是测不出来的
      （各自成败互不相干，实现里把来源判断删掉也照样过）。
    """
    from media_resource_manager.classify.preference import PreferenceLearner
    db = _db(tmp_path)
    try:
        # 用户手填：走 learn_manual（它内部会桥接，但桥不到也照记原词）
        PreferenceLearner(db, 0).learn_manual("dislike", ["a"])
        # 模型自动：同一个词
        PreferenceLearner(db, 0).learn("dislike", ["a"])
        rows = {r["label"]: str(r["source"]) for r in db.query(
            "SELECT label, source FROM preferences WHERE task_id=0")}
        assert "a" in rows, "用户手填的碎片词被过滤掉了 —— 用户白填"
        assert rows["a"] == "user_typed", \
            f"用户手填的来源被模型自动覆盖了：{rows}"
    finally:
        db.close()


def test_model_fragment_skipped_but_user_fragment_kept_same_word(tmp_path):
    """★ 同一批标签里混着手填与自动：只挡自动那几个。

    `learn_action` 会把整图标签按 model_auto 记；手填词走 learn_manual。
    这条钉住"两条路各按各的来源过滤"，而不是"整次学习一刀切"。
    """
    from media_resource_manager.classify.preference import PreferenceLearner
    db = _db(tmp_path)
    try:
        L = PreferenceLearner(db, 0)
        L.learn("dislike", ["x", "cat"])          # x=碎片(丢), cat=正常(留)
        L.learn_manual("dislike", ["x"])          # 用户手填同一个 x(留)
        rows = {r["label"]: str(r["source"]) for r in db.query(
            "SELECT label, source FROM preferences WHERE task_id=0")}
        assert rows.get("cat"), "正常词被一起挡掉了"
        assert rows.get("x") == "user_typed", \
            f"手填的 x 没留住或来源不对：{rows}"
    finally:
        db.close()


def test_fragment_filter_lives_at_the_model_gateway():
    """★ 过滤位置必须钉在 ``PreferenceLearner.learn``，**不是** repo 低层写入器。

    这是本次实现**踩出来的教训**：初版把过滤放进 ``repo.set_preference``，
    结果 R174 护栏（用 "甲"/"a" 这类短标签造数据）与"权重明细手工改权重"
    一起被改了行为 —— 低层写入器该忠实写下来什么就是什么。
    订单原文要的是"不入**统计**"，那是学习入口的职责。
    """
    import inspect
    from media_resource_manager.classify import preference as P
    from media_resource_manager import repo as R
    learn_src = inspect.getsource(P.PreferenceLearner.learn)
    assert "_is_fragment_label" in learn_src, \
        "模型学习入口没做碎片词过滤（噪声照样进统计）"
    setp_src = inspect.getsource(R.set_preference)
    assert "_is_fragment_label" not in setp_src, \
        "低层写入器被塞进了过滤 —— 会把导入偏好/手工改权重也一起改行为"


def test_single_cjk_char_is_a_valid_category(tmp_path):
    """★ 单一汉字是**合法类别词**，不该被当成碎片。

    长度门槛若按 len>=2 一刀切，"猫"/"狗"/"树"这些完整中文类别词会被
    全部砍掉 —— 中文信息密度高，单字就是完整词。所以门槛只对
    非中日韩字符串生效（这条是实测踩出来的：初版用 len>=2 把「猫」判成了碎片）。
    """
    from media_resource_manager.repo import _is_fragment_label
    from media_resource_manager.classify.preference import PreferenceLearner
    for w in ("猫", "狗", "树", "花"):
        assert not _is_fragment_label(w), f"{w!r} 被误判为碎片词（中文单字是完整词）"
    db = _db(tmp_path, "cjk.db")
    try:
        PreferenceLearner(db, 0).learn("dislike", ["猫"])
        got = {r["label"] for r in db.query(
            "SELECT label FROM preferences WHERE task_id=0")}
        assert "猫" in got, "模型自动的单一汉字类别词被过滤了"
    finally:
        db.close()


def test_decay_lowers_stale_model_weights(tmp_path):
    """★ 时间衰减：长期不再被碰的模型自动词缓慢降权。

    造一条「30 天没碰过」的行，衰减后应落在 0.95^30 ≈ 21% 左右。
    """
    from media_resource_manager.repo import decay_preferences
    db = _db(tmp_path)
    try:
        db.execute(
            "INSERT INTO preferences(task_id,label,weight,seen,source,updated_at)"
            " VALUES(0,'红丝',-10.0,5,'model_auto',"
            " datetime('now','-30 day'))")
        decay_preferences(db, now=None)
        w = float(db.query("SELECT weight FROM preferences "
                           "WHERE label='红丝'")[0]["weight"])
        expect = -10.0 * (0.95 ** 30)
        assert abs(w - expect) < 0.05, \
            f"30 天衰减后应是 {expect:.2f}，实际 {w:.2f}"
    finally:
        db.close()


def test_decay_never_touches_user_typed_or_fresh_rows(tmp_path):
    """★ 两条红线：① 用户手填的词一律不衰减；② 天天在用的词不衰减。

    ① 是硬约束（"用户教过的东西不许被算法悄悄改小"）；
    ② 是设计意图（平时学习结果就该稳稳不动，只有"死了的词"才退场）。
    """
    from media_resource_manager.repo import decay_preferences
    db = _db(tmp_path)
    try:
        db.execute(
            "INSERT INTO preferences(task_id,label,weight,seen,source,updated_at)"
            " VALUES(0,'金发',-10.0,5,'user_typed',"
            " datetime('now','-60 day'))")
        db.execute(
            "INSERT INTO preferences(task_id,label,weight,seen,source,updated_at)"
            " VALUES(0,'猫',-10.0,5,'model_auto', datetime('now'))")
        decay_preferences(db)
        rows = {r["label"]: float(r["weight"]) for r in db.query(
            "SELECT label, weight FROM preferences WHERE task_id=0")}
        assert rows.get("金发") == -10.0, \
            f"用户手填的词被衰减了（权重 {rows.get('金发')}）—— 这是伤用户数据"
        assert rows.get("猫") == -10.0, \
            f"今天刚碰过的词被衰减了（权重 {rows.get('猫')}）"
    finally:
        db.close()


def test_decay_skips_rows_without_timestamp(tmp_path):
    """老库迁移补列后 updated_at 是 NULL —— 那种行**跳过不衰减**。

    没有时间信息就不该凭猜测削用户的词。这是"看起来对、实际伤数据"的典型。
    """
    from media_resource_manager.repo import decay_preferences
    db = _db(tmp_path)
    try:
        db.execute("INSERT INTO preferences(task_id,label,weight,seen,source)"
                   " VALUES(0,'美丽',-9.0,3,'model_auto')")
        decay_preferences(db)
        assert float(db.query("SELECT weight FROM preferences "
                              "WHERE label='美丽'")[0]["weight"]) == -9.0, \
            "没有 updated_at 的老行被衰减了（不该凭猜测动用户的词）"
    finally:
        db.close()


def test_decay_prunes_noise_weight(tmp_path):
    """衰减到 |w| < 0.5 的词整行清掉 —— 否则看板堆一片 0.0x 噪声词。"""
    from media_resource_manager.repo import decay_preferences
    db = _db(tmp_path)
    try:
        db.execute(
            "INSERT INTO preferences(task_id,label,weight,seen,source,updated_at)"
            " VALUES(0,'噪声词',-0.7,2,'model_auto',"
            " datetime('now','-40 day'))")
        decay_preferences(db)
        assert not db.query("SELECT 1 FROM preferences WHERE label='噪声词'"), \
            "衰减到 0.5 以下的噪声词没被清掉（会永远占着看板）"
    finally:
        db.close()


def test_set_preference_refreshes_updated_at(tmp_path):
    """每次真的记一笔都要刷新 updated_at —— 否则衰减会把"天天在用的词"也削掉。"""
    from media_resource_manager.repo import set_preference
    db = _db(tmp_path)
    try:
        db.execute("INSERT INTO preferences(task_id,label,weight,seen,source,"
                   "updated_at) VALUES(0,'猫',1.0,1,'model_auto',"
                   " datetime('now','-30 day'))")
        set_preference(db, 0, "猫", 1.0, kind="pos")
        u = db.query("SELECT updated_at FROM preferences WHERE label='猫'")[0]
        assert u["updated_at"], "没写 updated_at"
        assert "day" not in str(u["updated_at"]), \
            f"updated_at 没刷新（还是那个 30 天前的值）：{u['updated_at']}"
    finally:
        db.close()


def test_weight_time_decay_constants_are_reasonable():
    """常量取值要落在"合理区间"内，别写成 0.0001（一天清零）或 0.9999（等于没做）。"""
    from media_resource_manager import repo as R
    assert 0.5 <= R.PREF_DECAY_FACTOR <= 0.99, \
        f"衰减系数 {R.PREF_DECAY_FACTOR} 不在合理区间"
    assert R.PREF_MIN_LABEL_LEN >= 2, "碎片词门槛小于 2 会把正常短词也砍掉"


def test_maintenance_is_idempotent_and_daily(tmp_path):
    """维护任务：碎片清扫一次性、衰减每天一次（靠 app_meta 日期标记）。"""
    from media_resource_manager.repo import run_learning_maintenance
    db = _db(tmp_path)
    try:
        db.execute("INSERT INTO preferences(task_id,label,weight,seen,source)"
                   " VALUES(0,'1',-3.0,2,'model_auto')")
        first = run_learning_maintenance(db)
        assert first["fragments"] == 1, f"第一次没清碎片词：{first}"
        assert first["decayed"] is True, f"第一次没跑衰减：{first}"
        # 第二次：碎片清扫不重复（标记已写），衰减也不重复（同一天）
        second = run_learning_maintenance(db)
        assert second["fragments"] == 0, "碎片清扫不幂等（每次都重清）"
        assert second["decayed"] is False, "衰减同一天跑了两次"
    finally:
        db.close()


def test_maintenance_never_raises_on_broken_db(tmp_path):
    """库结构不对时维护必须静默返回，不能把程序启动带崩。"""
    from media_resource_manager.repo import run_learning_maintenance
    db = _db(tmp_path)
    try:
        db._conn.execute("DROP TABLE IF EXISTS preferences")
        out = run_learning_maintenance(db)      # 不许抛
        assert isinstance(out, dict)
    finally:
        db.close()


def test_updated_at_column_migrates_into_old_db(tmp_path):
    """老库打开一次就要补上 updated_at 列（不能只有新装才有）。"""
    import sqlite3
    from media_resource_manager.db import Database
    p = tmp_path / "old169.db"
    con = sqlite3.connect(str(p))
    con.executescript(
        "CREATE TABLE preferences (task_id INTEGER NOT NULL, label TEXT NOT NULL,"
        " weight REAL DEFAULT 0.0, seen INTEGER DEFAULT 0,"
        " hit_pos INTEGER DEFAULT 0, hit_neg INTEGER DEFAULT 0,"
        " PRIMARY KEY (task_id, label))")
    con.execute("INSERT INTO preferences(task_id,label,weight,seen)"
                " VALUES(0,'cat',1.0,3)")
    con.commit()
    con.close()
    db = Database(p)
    try:
        cols = {r["name"] for r in
                db._conn.execute("PRAGMA table_info(preferences)")}
        assert "updated_at" in cols, f"老库没补上 updated_at：{cols}"
        assert "source" in cols, f"老库没补上 source：{cols}"
        assert db.query("SELECT COUNT(*) c FROM preferences")[0]["c"] == 1, \
            "迁移把老数据弄没了"
    finally:
        db.close()


# ==================================================================
# 三、重置入口两端可见
# ==================================================================

def test_reset_requires_confirm_on_backend():
    """★ 重置是不可撤销的删除 —— 后端那道 confirm 才是真防线（前端 confirm 只是提示）。"""
    body = _fn_src(ROOT / "media_resource_manager" / "webui.py",
                   "_api_learning_reset")
    assert "confirm" in body, "重置学习没有后端二次确认"
    assert "confirm" in body.split("DELETE", 1)[0], \
        "confirm 检查落在 DELETE 之后了（等于没拦）"


if __name__ == "__main__":      # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
