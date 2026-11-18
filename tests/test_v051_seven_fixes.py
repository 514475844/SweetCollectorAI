"""V0.0.51 护栏：用户一次反馈的七处修复。

1. 重启后「同时复制一份进软件图库」勾选丢失
2. Web 访问页又多又乱又杂
3. 启动后自动恢复原有任务（下载/打标/导入可勾选）
4. 备份同步页就地配置同步目录
5. 导入结果卡「再导一次」话术
6. 模型库加更多档位（含大显存）
7. 适配器在线 AI 说清来源 + 复制提示词 + 外部 AI 回填
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "media_resource_manager"
WEB = MOD / "webapp" / "src"


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ── ① 表单勾选与作业状态分离 ────────────────────────────────
def test_import_form_persists_after_job_clear():
    s = _read("media_resource_manager/webapp/src/importJob.js")
    assert "FORM_KEY = 'mrm.import.form.v1'" in s
    assert "function _writeForm()" in s
    # 清作业只删 STORE_KEY，不能再把表单一起删掉
    clear = s.split("function _clearJob()", 1)[1].split("\n}", 1)[0]
    assert "removeItem(STORE_KEY)" in clear
    assert "FORM_KEY" not in clear
    resumed = s.split("export function resumeImport()", 1)[1].split("\n}", 1)[0]
    assert "getItem(FORM_KEY)" in resumed
    assert "imp.copyIn = !!s.copyIn" in resumed


def test_import_watch_includes_checkbox():
    s = _read("media_resource_manager/webapp/src/components/ImportFolder.vue")
    seg = s.split("watch(() => [imp.mode", 1)[1].split("\n\n", 1)[0]
    assert "imp.copyIn" in seg


# ── ② Web 访问页收敛 ──────────────────────────────────────
def test_web_access_page_has_collapsible_advanced():
    s = (WEB / "views/Settings.vue").read_text(encoding="utf-8")
    assert "webAdvanced = ref(false)" in s
    assert 'class="btn sm ghost adv-toggle"' in s
    assert 'v-if="webAdvanced" class="web-advanced"' in s
    # 常用项仍在模板里
    tpl = s.split("<template>", 1)[1]
    for label in ("允许局域网访问", "端口", "登录密码", "局域网免密"):
        assert label in tpl


def test_https_no_longer_second_long_card():
    s = (WEB / "views/Settings.vue").read_text(encoding="utf-8")
    assert "!(tab.value === 'web' && s.title === 'HTTPS 加密访问')" in s


# ── ③ 启动恢复 ────────────────────────────────────────────
def test_startup_resume_config_and_backend():
    cfg = _read("media_resource_manager/config.py")
    for k in ("resume_on_start", "resume_downloads", "resume_tags", "resume_imports"):
        assert k in cfg
    ui = _read("media_resource_manager/webui.py")
    for k in ("resume_on_start", "resume_downloads", "resume_tags", "resume_imports"):
        assert f'"{k}"' in ui
    r = _read("media_resource_manager/startup_resume.py")
    for fn in ("restore_downloads", "restore_pending_work", "restore_all"):
        assert f"def {fn}(" in r
    q = _read("media_resource_manager/scheduler/queue.py")
    assert "restore_all(self.cfg, self, stale_ids)" in q
    fn = q.split("def reset_stale_running", 1)[1].split("def ", 1)[0]
    assert "return ids" in fn


def test_startup_resume_ui_checkboxes():
    s = (WEB / "views/Settings.vue").read_text(encoding="utf-8")
    sec = s.split("title: '启动后自动恢复'", 1)[1].split("  },\n  {", 1)[0]
    for k in ("resume_on_start", "resume_downloads", "resume_tags", "resume_imports"):
        assert f"k: '{k}'" in sec


def test_tag_pending_uses_same_running_lock():
    s = _read("media_resource_manager/import_folder.py")
    fn = s.split("def tag_pending(", 1)[1].split("\n\ndef ", 1)[0]
    assert "_RUNNING.acquire(blocking=False)" in fn
    assert "_RUNNING.release()" in fn
    assert "_tag_stage(" in fn


# ── ④ 备份同步页就地配置 ──────────────────────────────────
def test_maintenance_sync_dir_editable_here():
    s = (WEB / "views/Maintenance.vue").read_text(encoding="utf-8")
    assert "backup_sync_dir" in s
    assert "saveSyncDir" in s
    assert "同步目录请在「设置" not in s
    assert "这里改的就是设置里的" in s


# ── ⑤ 话术 ────────────────────────────────────────────────
def test_no_informal_again_copy():
    s = (WEB / "components/ImportFolder.vue").read_text(encoding="utf-8")
    assert "再导一次" not in s
    assert "开始新的导入" in s


# ── ⑥ 模型扩充 ────────────────────────────────────────────
def test_more_models_including_big_vram():
    s = _read("media_resource_manager/models_lib.py")
    for mid in ("chinese-clip-vit-b16", "chinese-clip-vit-l14",
                "chinese-clip-vit-l14-336", "dinov2-large"):
        assert f'"id": "{mid}"' in s
    assert "CHINESE_CLIP_FILES" in s and "vocab.txt" in s
    m = (WEB / "views/Models.vue").read_text(encoding="utf-8")
    assert "DINOv2-L" in m


# ── ⑦ 适配器在线 AI ──────────────────────────────────────
# V0.0.54 用户定案：前端移除「自动生成规则 / 别的 AI 生成」入口，改走
# 适配器市场。后端 /api/adapter/prompt 等接口保留不动（别的调用方还在），
# 但界面必须不再出现这两块。
def test_adapter_ai_ui_removed_routes_kept():
    s = (WEB / "views/Adapters.vue").read_text(encoding="utf-8")
    assert "自动生成规则" not in s and "copyPrompt" not in s and "fromReply" not in s
    assert "适配器市场" in s
    ui = _read("media_resource_manager/webui.py")
    for route in ('"/api/adapter/prompt"', '"/api/adapter/from-reply"'):
        assert route in ui
    a = _read("media_resource_manager/webapp/src/api.js")
    assert "adapterPrompt:" in a and "adapterFromReply:" in a
