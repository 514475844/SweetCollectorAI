<script setup>
// 适配器：读 rules 目录（/api/view/adapters，与桌面端同一个 load_adapter 解析），
// 支持删除坏规则。
// V0.0.54：按用户定案移除两处 AI 生成规则的入口（在线生成与外部回复回填）——
// 规则的获取改走「适配器市场」订阅安装，或手动新建 / 导入本地文件。
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import { useShellRefresh } from '../shellVue'
import Icon from '../components/Icon.vue'

const items = ref([])
const err = ref('')
const info = ref('')
const router = useRouter()

async function load() {
  try {
    const d = await api.view('adapters', { limit: 200 })
    items.value = d.items || []
    err.value = ''
  } catch (e) { err.value = e.message }
}
async function del(a) {
  if (!confirm(`确认删除适配器规则「${a.name}」？对应规则文件将被移除。`)) return
  try {
    await api.post('/api/adapter/delete', { name: a.name })
    info.value = `已删除 ${a.name}`
    setTimeout(() => (info.value = ''), 2500)
    await load()
  } catch (e) { err.value = e.message }
}

// ── V0.0.23：手动新建 / 手动编辑 / 导入本地规则文件 ──────────────────
// 规则本质就是一个文本文件，网页端能读能写就应该能做。
const saving = ref(false)
const editor = ref(null)     // { name, text, overwrite, from }
const EDIT_TEMPLATE = `name: my_site
base_url: https://example.com
entry_urls:
  - https://example.com/albums
list_selector: "a.album"
image_selector: "img.photo"
attr: src
`

async function openEdit(a) {
  err.value = ''
  // 列表接口本来就带 rule_text（后端 _api_adapters 一起返回），直接用；
  // 只有老版本/缺字段时才再单独取一次。
  if (a.rule_text) {
    editor.value = { name: a.name, text: a.rule_text, overwrite: true, from: a.file || '' }
    return
  }
  try {
    const r = await api.adapterGet(a.name)
    if (r && r.ok === false) { err.value = r.error || '读取规则失败'; return }
    editor.value = { name: a.name, text: r.text || '', overwrite: true, from: a.file || '' }
  } catch (e) { err.value = e.message }
}

function openNew() {
  err.value = ''
  editor.value = { name: '', text: EDIT_TEMPLATE, overwrite: false, from: '' }
}

async function importRule(e) {
  const f = (e.target.files || [])[0]
  e.target.value = ''
  if (!f) return
  err.value = ''
  try {
    const text = await f.text()
    const stem = f.name.replace(/\.(ya?ml|json)$/i, '')
    editor.value = { name: stem, text, overwrite: false, from: f.name }
  } catch (err2) { err.value = `读取文件失败：${err2.message}` }
}

async function saveEditor() {
  const ed = editor.value
  if (!ed) return
  if (!ed.text.trim()) { err.value = '规则内容不能为空'; return }
  saving.value = true
  err.value = ''
  try {
    const r = await api.adapterSave(ed.text, ed.name.trim(), ed.overwrite)
    if (r && r.ok === false) { err.value = r.error || '保存失败'; return }
    info.value = `已保存规则 ${r.name || ed.name}`
    setTimeout(() => (info.value = ''), 3000)
    editor.value = null
    await load()
  } catch (e) { err.value = e.message }
  finally { saving.value = false }
}
onMounted(load)
useShellRefresh(() => load())
</script>

<template>
  <div class="page">
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="info" class="ok">{{ info }}</p>

    <!-- V0.0.54：手动新建 / 导入本地规则文件 / 适配器市场 -->
    <div class="card tools">
      <div class="inline">
        <button class="btn sm" @click="openNew"><Icon name="plus" :size="13" />新建规则</button>
        <label class="btn sm file">
          <Icon name="upload" :size="13" />导入本地规则文件
          <input type="file" accept=".yaml,.yml,.json" hidden @change="importRule" />
        </label>
        <button class="btn sm primary" @click="router.push('/market')">
          <Icon name="globe" :size="13" />适配器市场</button>
        <span class="small muted">可导入 .yaml / .yml / .json；导入后先在本页核对，确认无误再保存。</span>
      </div>
    </div>

    <div v-if="editor" class="card ai">
      <div class="ai-head">
        <b>{{ editor.overwrite ? `编辑规则：${editor.name}` : '新建规则' }}</b>
        <span class="muted small">
          {{ editor.from ? `来源文件：${editor.from}。` : '' }}
          规则名只能用字母、数字、下划线与短横线；保存前会校验解析与选择器类型。
        </span>
      </div>
      <div class="ai-in">
        <label class="small">规则名</label>
        <input v-model="editor.name" class="ai-name" :disabled="editor.overwrite"
               placeholder="例如 my_site" />
        <span v-if="editor.overwrite" class="small muted">已存在的规则名不可修改</span>
      </div>
      <textarea v-model="editor.text" class="ai-text" rows="14" spellcheck="false" />
      <div class="ai-in">
        <button class="btn sm primary" :disabled="saving" @click="saveEditor">
          <Icon name="save" :size="13" />{{ saving ? '保存中…' : (editor.overwrite ? '保存修改' : '保存为新规则') }}</button>
        <button class="btn sm" :disabled="saving" @click="editor = null">取消</button>
      </div>
    </div>

    <div class="card table">
      <div class="th row"><span>规则名</span><span>站点</span><span>状态</span><span>操作</span></div>
      <div v-for="a in items" :key="a.name" class="row tr">
        <span class="mono">{{ a.name }}</span>
        <span class="mono dim">{{ a.base_url || (a.domains || []).join(', ') }}</span>
        <span class="st">
          <i class="pill" :class="a.ok ? 'ok' : 'err'">{{ a.ok ? '正常' : '有问题' }}</i>
          <!-- R252：规则的登录态与 UA 只显示「有没有」，值不回显 -->
          <i v-if="a.has_cookie" class="pill soft" title="规则里带 Cookie：这个站的任务自动带上登录态">登录态</i>
          <i v-if="a.has_ua" class="pill soft" title="规则里固定了 User-Agent">UA</i>
          <em v-if="!a.ok" class="why">{{ (a.problems || []).join('；') }}</em>
        </span>
        <span class="acts">
          <button class="btn sm" @click="openEdit(a)"><Icon name="pencil" :size="13" />编辑</button>
          <button class="btn sm danger" @click="del(a)"><Icon name="trash" :size="13" />删除</button>
        </span>
      </div>
      <p v-if="!items.length" class="empty muted">暂无适配器规则</p>
    </div>
    <p class="muted small">
      任务里适配器选「自动」时，会按域名自动使用这里的规则（gallery-dl 认得的大站仍走快路径）；
      需要「只粘网址」就在设置里保持「按域名自动用适配器」开启。
    </p>
    <p class="muted small">
      规则里可以写 <code>cookie:</code> 与 <code>user_agent:</code>：这个站需要登录态时填一次，
      以后该站所有任务自动带上。
    </p>
  </div>
</template>

<style scoped>
/* V0.0.27：块与块之间统一 14px 缝（这页以前没有 page 间距，卡片全贴在一起） */
.page { display: grid; gap: 14px; align-items: start; }
.table { overflow-x: auto; overflow-y: hidden; }
.row { display: grid; grid-template-columns: 180px minmax(0, 1fr) minmax(0, 1fr) 176px;
  gap: 10px; align-items: center; padding: 10px 14px; }
.acts { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.tools { padding: 10px 14px; }
.ai-name { width: 220px; }
.th { background: #fafbff; border-bottom: 1px solid var(--line); font-size: 12px; color: var(--muted); }
.ai { padding: 12px 14px; display: flex; flex-direction: column; gap: 8px; }
.ai-head { display: flex; flex-direction: column; gap: 2px; }
.ai-cfg { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 6px 0 0; }
.ai-in { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ai-url { flex: 1; min-width: 240px; }
.ai-text { width: 100%; box-sizing: border-box; font: 12px/1.5 Consolas, monospace;
  padding: 8px 10px; border-radius: 8px; border: 1px solid var(--line);
  background: #fbfaff; color: var(--text); resize: vertical; }
.ai-notes { display: flex; flex-direction: column; gap: 2px; }
.ai-notes .warn { color: var(--warn, #b7791f); }
.chk { display: inline-flex; align-items: center; gap: 5px; }
.st { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.pill.soft { background: #f1edff; color: var(--brand); border-color: #ded4ff; }
.tr { border-bottom: 1px solid #f1f2f8; }
.dim { color: var(--muted); }
.why { font-style: normal; color: var(--err); font-size: 12px; margin-left: 6px; }
.err { color: var(--err); margin: 0; }
.ok { color: var(--ok); margin: 0; }
.small { font-size: 12px; }
.empty { padding: 24px; text-align: center; margin: 0; }
/* V0.0.59：按钮尺寸已统一到全局 style.css，此处局部覆盖删除 */
.btn.danger { color: var(--err); border-color: #f3c9c9; }
</style>
