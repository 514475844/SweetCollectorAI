<script setup>
// V0.0.17 · 快捷键说明与键位自定义（按 ? 或 F1 打开，侧栏 / 设置页亦有入口）。
// 桌面 F1 说明对应键位由 /api/web/hotkeys 下发，此处只读展示；网页端动作键位
// 可录键或手工填写，保存后立即写入全局状态，让全局 keydown 真正按新键位生效。
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
import Icon from './Icon.vue'
import { eventSeq, setWebHotkeys, recordingHotkey } from '../uiPrefs'

const visible = ref(false)
const loading = ref(false)
const saving = ref(false)
const err = ref('')
const info = ref('')
const actions = ref([])
const desktop = ref([])
const fixed = ref([])
const shell = ref([])
const shellNote = ref('')
const note = ref('')
const draft = reactive({})
const listening = ref('')
// V0.0.23：用户原话「快捷键说明为什么不能更改按键？不是应该说明就只是说明，
// 更改按键就只是更改按键吗？」—— 拆成两个独立入口：
//   view：只读说明（默认，按 ? / F1 打开就是这个）
//   edit：自定义键位（设置页单独的「自定义快捷键」按钮进来）
const mode = ref('view')
const editing = computed(() => mode.value === 'edit')

async function load() {
  loading.value = true
  err.value = ''
  try {
    const d = await api.webHotkeys()
    if (!d.ok) throw new Error(d.error || '载入失败')
    actions.value = d.actions || []
    desktop.value = d.desktop || []
    fixed.value = d.fixed || []
    shell.value = d.shell || []
    shellNote.value = d.shell_note || ''
    note.value = d.note || ''
    for (const a of actions.value) draft[a.id] = a.current || a.default
  } catch (e) {
    err.value = e.message || '载入快捷键说明失败，请稍后重试。'
  } finally { loading.value = false }
}

function open(m = 'view') {
  mode.value = m === 'edit' ? 'edit' : 'view'
  visible.value = true
  info.value = ''
  load()
}
function close() {
  visible.value = false
  listening.value = ''
  recordingHotkey.value = false
}
function startListen(id) {
  listening.value = id
  recordingHotkey.value = true
  info.value = ''
}
function onKey(e) {
  if (!visible.value) return
  if (e.key === 'Escape') { close(); return }
  if (!listening.value) return
  e.preventDefault()
  const seq = eventSeq(e)
  if (!seq || ['Ctrl', 'Alt', 'Shift', 'Meta'].includes(seq)) return
  draft[listening.value] = seq
  listening.value = ''
  recordingHotkey.value = false
}
function resetDefault() {
  for (const a of actions.value) draft[a.id] = a.default
  info.value = '已恢复默认键位，请保存后生效。'
}
async function save() {
  saving.value = true
  err.value = ''
  info.value = ''
  try {
    const r = await api.webHotkeysSave({ ...draft })
    if (!r.ok) throw new Error(r.error || '保存键位失败，请稍后重试。')
    setWebHotkeys({ ...draft })
    info.value = r.note || '键位已保存并生效。'
    await load()
  } catch (e) {
    err.value = e.message || '保存键位失败，请稍后重试。'
  } finally { saving.value = false }
}
onMounted(() => {
  window.addEventListener('mrm-shortcuts', () => open('view'))
  window.addEventListener('mrm-shortcuts-edit', () => open('edit'))
  window.addEventListener('keydown', onKey)
})
onUnmounted(() => {
  window.removeEventListener('mrm-shortcuts', open)
  window.removeEventListener('keydown', onKey)
})
</script>

<template>
<!-- V0.0.58：挂到 body，避免 fixed 遮罩被 .page/过渡的 transform 困住盖不住顶栏标题 -->
    <Teleport to="body">
  <div v-if="visible" class="mask" @click.self="close">
    <div class="dlg card">
      <div class="head">
        <h3><Icon name="key-round" :size="16" />{{ editing ? '自定义快捷键' : '快捷键说明' }}</h3>
        <button class="ibtn" title="关闭弹窗" @click="close"><Icon name="x" :size="15" /></button>
      </div>
      <p class="muted small" v-if="!editing">
        本窗口只用于查看当前生效的键位，不能修改。要改键位请关闭本窗口，
        在「设置 → 外观与启动 → 界面辅助」里点「自定义快捷键」。
      </p>
      <p class="muted small" v-else>
        在这里改键位：单击「录键」后按下新的组合键，按 Esc 取消本次录入；也可在输入框中手工填写。
      </p>
      <p v-if="err" class="err small">{{ err }}</p>
      <p v-if="info" class="ok small">{{ info }}</p>

      <div class="scroll">
        <h4>桌面端键位（与 F1 说明对应）</h4>
        <p class="muted tiny">下列键位取自桌面端当前生效配置，仅供查看，不在网页端修改。</p>
        <table class="tbl">
          <thead><tr><th>场景</th><th>动作</th><th>键位</th></tr></thead>
          <tbody>
            <tr v-for="(d, i) in desktop" :key="i">
              <td class="muted small">{{ d.group }}</td>
              <td>{{ d.label }}</td>
              <td class="mono">{{ d.key }}</td>
            </tr>
            <tr v-if="!desktop.length"><td colspan="3" class="muted">暂无桌面端键位数据</td></tr>
          </tbody>
        </table>
        <div class="chips">
          <span v-for="(f, i) in fixed" :key="i" class="chip"><b>{{ f.key }}</b>{{ f.label }}</span>
        </div>

        <h4>网页端键位{{ editing ? '（编辑中）' : '' }}</h4>
        <p class="muted tiny" v-if="editing">
          单击「录键」后按下新的组合键，按 Esc 取消本次录入；也可在输入框中手工填写键位。
        </p>
        <p class="muted tiny" v-else>
          这些是当前生效的网页端键位。要修改请用设置页的「自定义快捷键」。
        </p>
        <table class="tbl">
          <thead><tr><th>动作</th><th>键位</th><th v-if="editing">操作</th></tr></thead>
          <tbody>
            <tr v-for="a in actions" :key="a.id">
              <td>{{ a.label }}</td>
              <td>
                <input v-if="editing" v-model="draft[a.id]" class="keyin" type="text" />
                <span v-else class="mono">{{ draft[a.id] || a.default }}</span>
              </td>
              <td v-if="editing">
                <button class="btn sm" :disabled="saving" @click="startListen(a.id)">
                  <Icon name="pencil" :size="12" />{{ listening === a.id ? '正在录入…' : '录键' }}
                </button>
              </td>
            </tr>
            <tr v-if="!actions.length"><td :colspan="editing ? 3 : 2" class="muted">暂无可自定义的网页端动作</td></tr>
          </tbody>
        </table>
        <h4>内置窗口固定键位</h4>
        <p class="muted tiny">{{ shellNote }}</p>
        <table class="tbl">
          <thead><tr><th>键位</th><th>动作</th></tr></thead>
          <tbody>
            <tr v-for="(s, i) in shell" :key="i">
              <td class="mono">{{ s.key }}</td>
              <td>{{ s.label }}</td>
            </tr>
            <tr v-if="!shell.length"><td colspan="2" class="muted">暂无内置窗口键位数据</td></tr>
          </tbody>
        </table>
        <p class="muted tiny">{{ note }}</p>
        <p class="muted tiny">桌面端快捷键对话框保存时会清理网页专用键位，请勿在两端交替修改。</p>
      </div>

      <div class="acts">
        <button v-if="editing" class="btn sm" :disabled="saving || loading" @click="resetDefault">
          <Icon name="refresh-cw" :size="13" />恢复默认</button>
        <span class="spacer"></span>
        <button class="btn sm" @click="close">关闭</button>
        <button v-if="editing" class="btn primary sm" :disabled="saving || loading" @click="save">
          <Icon name="save" :size="13" />{{ saving ? '正在保存…' : '保存键位' }}</button>
      </div>
    </div>
  </div>
  </Teleport>
</template>

<style scoped>
.mask { position: fixed; inset: 0; z-index: 80; background: rgba(16, 24, 40, .46);
        display: grid; place-items: center; padding: 18px; }
.dlg { width: min(680px, 94vw); max-height: 86vh; display: flex; flex-direction: column;
       padding: 18px 20px; gap: 8px; }
.head { display: flex; align-items: center; justify-content: space-between; }
.head h3 { margin: 0; font-size: 17px; display: inline-flex; align-items: center; gap: 8px; }
.ibtn { border: 0; background: transparent; color: var(--muted); cursor: pointer;
        width: 28px; height: 28px; border-radius: 7px; display: grid; place-items: center; }
.ibtn:hover { background: #f2f3f8; color: var(--brand); }
.scroll { overflow: auto; min-height: 0; display: grid; gap: 8px; }
h4 { margin: 8px 0 0; font-size: 14px; }
.tiny { font-size: 12px; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { padding: 6px 8px; border-bottom: 1px solid var(--line, rgba(0,0,0,.08));
                   text-align: left; }
.tbl th { color: var(--muted); font-weight: 600; }
.keyin { width: 130px; padding: 4px 8px; font-family: monospace; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 9px;
        border-radius: 999px; background: rgba(0,0,0,.04); font-size: 12px; }
.acts { display: flex; align-items: center; gap: 8px; padding-top: 6px;
        border-top: 1px solid var(--line, rgba(0,0,0,.08)); }
.spacer { flex: 1; }
.err { color: var(--err, #b91c1c); }
.ok { color: var(--brand, #6d4bd6); }
</style>
