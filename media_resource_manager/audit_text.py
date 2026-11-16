"""审计日志的中文化（V0.0.23）。

用户反馈原话：「日志里面客户端和网页都会提示是 web 操作，而且动作和对象
这都是什么？看不懂还要按动作统计，为什么这些都是英文呢？详情为什么只有
一条横杠」。

三件事分开修，都在这里统一口径：
  ① 来源（actor）—— 谁动的手：客户端 / 网页 / 采集引擎 / 系统；
  ② 动作（action）—— 库里存的是 `task_save` 这类编码，界面必须给中文；
  ③ 对象（target）—— 只有数字 id 的话补上名字（任务 #12「konachan」）。

⚠ 这里只做**展示层**翻译，不碰写入口径：库里仍然存编码，
   否则老日志与新日志两套值，按动作统计会裂成两堆。
"""
from __future__ import annotations

# 来源
ACTOR_CN = {
    # client＝在 SweetWeb 软件窗口里操作；browser＝用手机/别的电脑打开网页操作。
    # web 是 V0.0.22 及更早写死的历史值（当时不区分来源），不能显示成「网页」
    # —— 那会让用户误以为自己在客户端里的操作被记成了网页。
    "client": "客户端",
    "browser": "浏览器",
    "web": "旧记录",
    "system": "系统",
    "engine": "采集引擎",
    "task": "采集引擎",
    "gui": "客户端",
    "cookie": "站点登录态",
    "gallery": "图库",
    "quarantine": "隔离区",
    "maintenance": "磁盘维护",
    "blacklist": "排除词",
    "filter": "过滤规则",
    "dedup": "去重",
    "ai": "AI 审核",
    "blur": "打码处理",
    "integrity": "完整性校验",
    "migration": "数据迁移",
    "proxy": "代理",
    "rules": "适配器规则",
    "purged": "彻底删除",
}

# 动作
ACTION_CN = {
    # 任务
    "task_save": "保存任务",
    "task_delete": "删除任务",
    "task_start": "开始任务",
    "task_pause": "暂停任务",
    "task_stop": "停止任务",
    "task_retry": "重试任务",
    "failed-urls-clear": "清空失败网址",
    # 设置与系统
    "settings_save": "修改设置",
    "autostart": "开机自启",
    "backfill": "补全缺失记录",
    "cleanup": "清理数据",
    "quarantine-prune": "清理隔离区文件",
    "model-download": "下载 AI 模型",
    "export-training-kit": "导出训练素材集",
    "threshold-apply": "应用合格线",
    # 适配器规则
    "adapter_add": "新增适配器规则",
    "adapter_save": "修改适配器规则",
    "adapter_delete": "删除适配器规则",
    # 备份与恢复
    "full-backup": "整库备份",
    "full-restore": "整库恢复",
    "full-prune": "清理旧备份",
    "backup-sync": "同步备份",
    # 订阅与登录
    "sub_save": "保存站点订阅",
    "sub_delete": "删除站点订阅",
    "qr-login-request": "发起扫码登录",
    "qr-login-confirm": "确认扫码登录",
    # 图库
    "rename": "重命名图片",
    "like": "标记喜欢",
    "dislike-delete": "标记不喜欢并删除",
    "blur": "打码处理",
    "clean-orphan": "清理孤儿记录",
    "cleanup-missing": "清理已丢失文件",
    # 隔离区
    "purge": "彻底删除",
    "purge-all": "清空隔离区",
    "restore": "从隔离区恢复",
    "clean-missing": "清理已丢失文件",
    # 采集与过滤
    "run": "开始采集",
    "run(gallery-dl)": "开始采集（gallery-dl）",
    "retry-failed-urls": "重试失败网址",
    "stage-to-review": "送入审核",
    "pre-collect-skip": "采集前拦下",
    "content-review": "内容审核拦下",
    "retroactive-review": "回溯审核拦下",
    "excluded-review": "排除词拦下",
    "needs-review": "标记待审核",
    "sensitive": "敏感内容处理",
    "ledger-only": "只登记不入库",
    "too-large-review": "超出体积送审",
    # 磁盘维护
    "clean-records": "清理记录",
    "convert-jpg": "转换为 JPG",
    "repair-gallery": "修复图库",
    "clean": "清理",
    "rollback": "回退规则",
    "auto-rotate": "自动轮换代理",
    # 站点登录态
    "import": "导入登录态",
    "push": "推送登录态",
    "delete": "删除",
}

# 对象前缀：动作 → 对象类型
_TARGET_KIND = {
    "task_save": "task",
    "task_delete": "task",
    "task_start": "task",
    "task_pause": "task",
    "task_stop": "task",
    "task_retry": "task",
    "failed-urls-clear": "task",
    "sub_save": "sub",
    "sub_delete": "sub",
}

KIND_CN = {"task": "任务", "sub": "订阅"}

# 清理动作的对象键 → 中文（/api/cleanup 的 plan 键）
CLEAN_CN = {
    "tasks": "任务",
    "reviews": "审核记录",
    "images": "图库记录",
    "quarantine": "隔离标记",
    "thumbs": "缩略图",
    "logs": "日志",
}


def actor_label(code: str) -> str:
    code = (code or "").strip()
    if not code:
        return "未知来源"
    return ACTOR_CN.get(code, code)


def action_label(code: str) -> str:
    code = (code or "").strip()
    if not code:
        return "未知动作"
    if code in ACTION_CN:
        return ACTION_CN[code]
    # 形如 task_save 的编码：至少把下划线拆成人话，别让用户看天书
    return code.replace("_", " ").replace("-", " ")


def target_label(action: str, target: str, names: dict | None = None) -> str:
    """把「12」这种裸 id 补成「任务 #12（konachan）」。

    names = {("task", 12): "konachan"}，由调用方一次查好传进来
    （每条日志查一次库会打出 50 条 SQL）。
    """
    t = (target or "").strip()
    if not t:
        return ""
    if action in ("cleanup", "quarantine-prune") and "," in t:
        # 清理动作的对象是 plan 的键名（reviews,images…），翻成中文
        return "、".join(CLEAN_CN.get(x.strip(), x.strip()) for x in t.split(","))
    kind = _TARGET_KIND.get((action or "").strip())
    if kind and t.isdigit():
        nm = (names or {}).get((kind, int(t)))
        return f"{KIND_CN.get(kind, '对象')} #{t}" + (f"（{nm}）" if nm else "")
    return t
