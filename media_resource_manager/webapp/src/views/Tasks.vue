<script setup>
import { ref, reactive, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import ContextMenu from '../components/ContextMenu.vue'
import FolderPicker from '../components/FolderPicker.vue'
import Icon from '../components/Icon.vue'
import EmptyState from '../components/EmptyState.vue'
import { useColResize } from '../colResize'
import { uiMode } from '../uiPrefs'
// V0.0.21：内置窗口统一走软件动作（Ctrl+A 全选、F5 刷新），不再靠本页
// 自己的 keydown 抢键；普通浏览器访问仍保留下面的原有键位处理。
import { onShellSelectAll, onShellRefresh, onShellFocusSearch } from '../nativeShell'
let unbindShell = []
// V0.0.19：新手模式默认收起高级字段，单击可临时展开；
// 字段值不随显隐变化，保存时仍按完整表单提交。
const advOpen = ref(false)

// route/router 提前声明：状态筛选直接读 URL（见 statusFilter）
const route = useRoute()
const router = useRouter()

// R243 · 列宽可拖：默认宽度与原 CSS `52px 1fr 86px 124px 1.2fr 250px` 近似
// （1fr 类的那两列给一个像素初值，最后「操作」列按 minmax(w, 1fr) 自动吃掉
// 剩余宽度，所以窗口变宽也不会右边空一大片）。
const { gridStyle, startResize, resetCol } = useColResize('tasks', [
  { k: 'no', w: 52, min: 44, max: 110 },
  { k: 'name', w: 280, min: 140, max: 640 },
  { k: 'status', w: 86, min: 70, max: 160 },
  { k: 'prog', w: 124, min: 96, max: 220 },
  { k: 'entry', w: 300, min: 120, max: 760 },
  { k: 'acts', w: 250, min: 232, max: 520 },
], { compactUnder: 1400 })   // 第 3 条：放不下就交还 CSS，否则操作列被裁
// V0.0.23：阈值从 1180 抬到 1400 —— 实测 1280 宽时行内列宽合计 1149px，
// 而卡片只有 1013px，右端按钮被 overflow 切掉（用户截图就是这个）。

// 任务页：列表 + 启停 + 新建/编辑（与桌面端同一套 /api/task/save 契约）。
const tasks = ref([])
const loading = ref(false)
const err = ref('')
const info = ref('')
const busy = ref(new Set())
// 2026-10-28（对标 qBittorrent 传输列表的多选）：勾若干行 → 批量开始/暂停/停止/
// 重试/删除。Shift 连选、Ctrl 点选、Ctrl+A 全选当前筛选结果。
const sel = ref(new Set())
let anchorIdx = -1                 // Shift 连选的锚点（行下标，不是 id）

function toggleSel(id) {
  const n = new Set(sel.value)
  n.has(id) ? n.delete(id) : n.add(id)
  sel.value = n
}
function onRowClick(e, t, i) {
  // 行里的按钮自己处理点击，别因为选择「开始」顺手把行选上
  if (e.target.closest('button, a, input')) return
  if (e.shiftKey && anchorIdx >= 0) {
    const [a, b] = [Math.min(anchorIdx, i), Math.max(anchorIdx, i)]
    const n = new Set(sel.value)
    for (let k = a; k <= b; k++) n.add(shownTasks.value[k].id)
    sel.value = n
  } else if (e.ctrlKey || e.metaKey) {
    toggleSel(t.id); anchorIdx = i
  } else {
    sel.value = new Set(sel.value.size === 1 && sel.value.has(t.id) ? [] : [t.id])
    anchorIdx = i
  }
}
const allShownSelected = computed(() =>
  shownTasks.value.length > 0 && shownTasks.value.every((t) => sel.value.has(t.id)))
function selectAllShown() {
  sel.value = allShownSelected.value ? new Set()
    : new Set(shownTasks.value.map((t) => t.id))
}
// Ctrl+A 在桌面软件里是「全选」，重复按不清空；表头的复选框仍是切换语义。
function selectAllForce() {
  sel.value = new Set(shownTasks.value.map((t) => t.id))
}
function clearSel() { sel.value = new Set(); anchorIdx = -1 }

// 批量动作：走 /api/task/batch（与「全部开始」同一条链，不另写一套）
async function batchAct(actName) {
  const ids = [...sel.value]
  if (!ids.length) return
  if (actName === 'delete') {
    openDelete(ids)
    return
  }
  busy.value = new Set(ids)
  try {
    const r = await api.taskBatch(actName, ids)
    info.value = `批量${({start:'开始',stop:'停止',pause:'暂停',retry:'重试',delete:'删除'})[actName] || actName}：`
      + `成功 ${(r.done || []).length} 个`
      + ((r.failed || []).length ? `，失败 ${r.failed.length} 个` : '')
    clearSel()
    await load()
  } catch (e) { err.value = e.message } finally { busy.value = new Set() }
}

// ── V0.0.27 · 删除任务对话框（可选「同时删除图片」）────────────────────
// 用户原话：「删除任务的时候能不能给个对勾选项，同时删除图片」。
// 原来只有一句 confirm，且写死「仅删除任务配置，图片不受影响」。
const delDlg = ref(null)   // { ids, title, images, withImages, busy }

function openDelete(ids) {
  const list = (tasks.value || []).filter((t) => ids.includes(t.id))
  const images = list.reduce((n, t) => n + Number(t.images || 0), 0)
  const title = list.length === 1
    ? `删除任务「${list[0].name || ('#' + list[0].id)}」？`
    : `删除选中的 ${list.length} 个任务？`
  delDlg.value = { ids, title, images, withImages: false, busy: false }
}

async function doDelete() {
  const d = delDlg.value
  if (!d || d.busy) return
  d.busy = true
  try {
    if (d.ids.length === 1) {
      const r = await api.taskCtl('delete', d.ids[0], { with_images: d.withImages })
      if (r && r.ok === false) throw new Error(r.error || '删除失败')
      const nImg = Number((r && r.images_deleted) || 0)
      const nProt = Number((r && r.images_protected) || 0)
      info.value = d.withImages
        ? `任务已删除，同时删除图片 ${nImg} 张`
          + (nProt ? `（其中 ${nProt} 张是导入的本地原图，只清了登记，磁盘文件没动）` : '')
        : '任务已删除，图片仍保留在图库中'
    } else {
      const r = await api.taskBatch('delete', d.ids, { with_images: d.withImages })
      info.value = `批量删除：成功 ${(r.done || []).length} 个`
        + ((r.failed || []).length ? `，失败 ${r.failed.length} 个` : '')
        + (d.withImages ? '（图片按选择一并删除）' : '（图片保留）')
      clearSel()
    }
    delDlg.value = null
    await load()
  } catch (e) {
    err.value = e.message
    if (delDlg.value) delDlg.value.busy = false
  }
}
// 2026-10-27：任务一多，找某个任务只能滚 —— 加搜索 + 排序（纯前端，
// 列表本来就已经全量拉下来了，不用再打接口）。
const kw = ref('')
const kwEl = ref(null)          // 过滤框，Ctrl+F 聚焦用
// 2026-10-28（对标 qBittorrent 左侧「状态」树）：点一下只看某一种状态，
// 带实时计数。计数直接用列表算，不额外请求接口。
// 2026-10-29：状态筛选搬去左侧导航「任务」下面了，这个页面只负责
// **跟着 URL 走**（/tasks?status=running）。这样：
//   · 刷新/收藏/分享链接都能落在同一个筛选上；
//   · 浏览器后退能退回上一个筛选（以前是个局部状态，退了没反应）。
const statusFilter = ref(String(route.query.status || ''))
// 侧栏切状态时不重新挂载组件，只改 query ⇒ 必须跟着改，否则筛选看起来没反应
watch(() => route.query.status, (v) => { statusFilter.value = String(v || '') })
const FILTERS = [
  { k: '', label: '全部', icon: 'list-checks' },
  { k: 'running', label: '运行中', icon: 'play' },
  { k: 'queued', label: '排队中', icon: 'clock' },
  { k: 'error', label: '失败', icon: 'triangle-alert' },
  { k: 'idle', label: '空闲', icon: 'inbox' },
  { k: 'stopped', label: '已停止', icon: 'circle-stop' },
  { k: 'done', label: '完成', icon: 'check-check' },
]
const sortBy = ref('id')
const proxies = ref([])
const adapters = ref([])
let timer = null

const stats = computed(() => {
  const s = { running: 0, error: 0, idle: 0, stopped: 0, done: 0 }
  for (const t of tasks.value) s[t.status] = (s[t.status] || 0) + 1
  return s
})
// 2026-10-27：后端状态全集（models.TaskStatus + 队列里可能残留的 queued），
// 全都翻成中文；**任何没列到的值也显示中文**（未知状态），不再漏英文给用户看。
const statusText = {
  running: '运行中', queued: '排队中', error: '失败', idle: '空闲',
  stopped: '已停止', done: '完成', paused: '暂停',
}
const statusClass = {
  running: 'ok', queued: 'warn', error: 'err', done: 'ok',
  idle: 'muted', stopped: 'muted', paused: 'warn',
}
const statusLabel = (s) => statusText[s] || ('未知（' + (s || '空') + '）')

// 搜索命中：任务名 / 入口网址 / #编号 任一含关键词
const shownTasks = computed(() => {
  const k = kw.value.trim().toLowerCase()
  let arr = tasks.value
  if (statusFilter.value) arr = arr.filter((t) => t.status === statusFilter.value)
  if (k) {
    arr = arr.filter((t) =>
      String(t.name || '').toLowerCase().includes(k)
      || String(t.id) === k.replace('#', '')
      || (t.entry_urls || []).some((u) => String(u).toLowerCase().includes(k)))
  }
  const by = sortBy.value
  const desc = by.endsWith('_desc')
  const key = desc ? by.slice(0, -5) : by
  const dir = desc ? -1 : 1
  const copy = arr.slice()
  // 2026-10-28：列头可点（qBittorrent 也是点表头排序）。
  // 「本次张数」默认大的在前，跟原来一致 —— 用 0-1 记默认方向，点一下才反过来。
  const cmp = {
    id: (a, b) => (a.id - b.id),
    name: (a, b) => String(a.name || '').localeCompare(String(b.name || ''), 'zh'),
    status: (a, b) => String(a.status).localeCompare(String(b.status)),
    got: (a, b) => ((b.got || 0) - (a.got || 0)),
  }[key] || ((a, b) => a.id - b.id)
  copy.sort((a, b) => cmp(a, b) * (key === 'got' ? dir : dir))
  return copy
})

// 列头高亮用：sortBy 可能是 name / name_desc，取基名
const sortBase = computed(() => String(sortBy.value || '').replace(/_desc$/, ''))

// 点列头：同一个键再点一下反向；换列就从该列的自然顺序开始
function toggleSort(key) {
  if (sortBy.value === key) sortBy.value = key + '_desc'
  else if (sortBy.value === key + '_desc') sortBy.value = key
  else sortBy.value = key
}

// Ctrl+F 聚焦过滤框；Esc 先清过滤词，再清状态筛选
function onKey(e) {
  if ((e.ctrlKey || e.metaKey) && (e.key === 'f' || e.key === 'F')) {
    e.preventDefault()
    kwEl.value && kwEl.value.focus()
  } else if ((e.ctrlKey || e.metaKey) && (e.key === 'a' || e.key === 'A')) {
    // 只在任务页生效；输入框里按 Ctrl+A 还是选文字，别抢
    const tag = (e.target && e.target.tagName) || ''
    if (tag === 'INPUT' || tag === 'TEXTAREA') return
    e.preventDefault()
    selectAllShown()
  } else if (e.key === 'Escape') {
    if (sel.value.size) clearSel()
    else if (kw.value) kw.value = ''
    else if (statusFilter.value) statusFilter.value = ''
  }
}

// silent=True 用于定时轮询：**不碰 loading**，否则「刷新」按钮
// 每 3 秒闪一下灰（用户以为界面在抽风）。
async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const d = await api.tasks()
    tasks.value = (d.tasks || d || []).map((t) => ({
      ...t,
      // 2026-10-27 走查抓到的真 bug：/api/tasks 列表返回的字段是 `urls`，
      // 而详情接口才叫 `entry_urls`。上一版只读 entry_urls ⇒ 入口列全是「—」。
      entry_urls: Array.isArray(t.urls) ? t.urls
        : (Array.isArray(t.entry_urls) ? t.entry_urls : []),
    }))
    // 选中的任务被删掉/过滤掉后，别让"已选 N"变成幽灵
    if (sel.value.size) {
      const alive = new Set(tasks.value.map((x) => x.id))
      const keep = [...sel.value].filter((id) => alive.has(id))
      if (keep.length !== sel.value.size) sel.value = new Set(keep)
    }
    if (!silent) err.value = ''
  } catch (e) { if (!silent) err.value = e.message } finally {
    if (!silent) loading.value = false
  }
}

// 2026-10-27：菜单抽成函数 —— 窄屏那 6 个按钮塞不进一行，
// 改成一个「⋯」打开**同一份菜单**（不维护两套动作）。
function rowMenu(t) {
  return [
      { label: t.status === 'stopped' ? '继续' : '开始', icon: 'play',
        disabled: t.status === 'running', onClick: () => act(t, 'start') },
      { label: '暂停', icon: 'pause', disabled: t.status !== 'running',
        onClick: () => act(t, 'pause') },
      { label: '强制停止', icon: 'circle-stop', onClick: () => act(t, 'stop') },
      { label: '编辑…', icon: 'pencil', onClick: () => openEdit(t) },
      { label: '失败网址…', icon: 'triangle-alert', onClick: () => openFailed(t) },
      // 2026-10-29（第 2 条）：用户要求取消「存为模板」入口。行内按钮与这个
      // 窄屏菜单**一起删**，否则宽屏没了、窄屏还能点，等于没取消。
      // saveAsTemplate() 函数本身保留（不占界面、随时可恢复）。
      { label: '在文件夹中打开', icon: 'folder-open',
        // 没设 output_dir 的任务用的是全局默认目录，这里无从得知具体路径 → 置灰
        disabled: !t.output_dir,
        onClick: () => revealTask(t) },
      { label: '复制入口网址', icon: 'copy', disabled: !t.entry_urls.length,
        onClick: () => navigator.clipboard?.writeText(t.entry_urls.join('\n')) },
      { label: `复制任务编号 #${t.id}`, icon: 'hash',
        onClick: () => navigator.clipboard?.writeText(String(t.id)) },
      { label: '仅运行该任务', icon: 'zap', onClick: async () => {
          await api.taskBatch('stop', null); await act(t, 'start') } },
      { label: '删除任务', icon: 'trash', danger: true, onClick: () => delTask(t) },
  ]
}

function onRowContext(e, t) {
  e.preventDefault()
  ctx.value = { x: e.clientX, y: e.clientY, items: rowMenu(t) }
}

function openRowMenu(e, t) {
  e.preventDefault()
  e.stopPropagation()
  // 用按钮当前位置当锚点（手机上右键不存在，只能点）
  const r = e.currentTarget.getBoundingClientRect()
  ctx.value = { x: r.left, y: r.bottom + 4, items: rowMenu(t) }
}

// 2026-10-28（对标 qBittorrent「打开目标文件夹」）：让服务端弹资源管理器。
// 远程访问时后端会明确回「只能在运行软件的那台电脑上打开」，把话转给用户就行。
async function revealTask(t) {
  try {
    const r = await api.taskReveal(t.id)
    info.value = `已打开：${r.path || ''}`
    setTimeout(() => (info.value = ''), 3000)
  } catch (e) { err.value = e.message }
}

async function act(t, actName) {
  busy.value.add(t.id)
  try {
    await api.taskCtl(actName === 'retry' ? 'retry' : actName, t.id)
    await load()
  } catch (e) { err.value = e.message } finally { busy.value.delete(t.id) }
}

async function all(actName) {
  const label = { start: '开始', stop: '停止', retry: '重试' }[actName] || actName
  try {
    const r = await api.taskBatch(actName, null)
    const n = (r && (r.count ?? r.done?.length)) || ''
    info.value = `已提交「全部${label}」${n ? `（${n} 个任务）` : ''}`
    setTimeout(() => (info.value = ''), 2500)
    await load(true)
  } catch (e) { err.value = e.message }
}

// ---------------- 编辑 / 新建 ----------------
// 失败网址 / 模板 / 导入导出
const failed = ref(null)       // { tid, items }
const failedSel = ref(new Set())
const templates = ref([])
const tplOpen = ref(false)
const importMsg = ref('')

async function openFailed(t) {
  try {
    const d = await api.taskFailedUrls(t.id)
    failed.value = { tid: t.id, items: d.items || [] }
    failedSel.value = new Set()
  } catch (e) { err.value = e.message }
}
function toggleFailed(u) {
  const s = new Set(failedSel.value)
  s.has(u) ? s.delete(u) : s.add(u)
  failedSel.value = s
}
async function retryFailed(all) {
  const f = failed.value
  if (!f) return
  const urls = all ? f.items.map((x) => x.url) : [...failedSel.value]
  if (!urls.length) { err.value = '请先勾选需要重试的网址'; return }
  try {
    await api.taskRetryUrls(f.tid, urls)
    info.value = `已提交重试 ${urls.length} 条网址`
    setTimeout(() => (info.value = ''), 2500)
    failed.value = null
  } catch (e) { err.value = e.message }
}
async function clearFailed() {
  const f = failed.value
  if (!f) return
  if (!confirm('确认清空该任务的全部失败网址？')) return
  try {
    await api.post('/api/task/failed-urls/clear', { task_id: f.tid })
    failed.value = { tid: f.tid, items: [] }
  } catch (e) { err.value = e.message }
}

async function saveAsTemplate(t) {
  try {
    const r = await api.post('/api/task/template/save', { id: t.id })
    info.value = `已存为模板 #${r.id || ''}`
    setTimeout(() => (info.value = ''), 2500)
  } catch (e) { err.value = e.message }
}
async function openTemplates() {
  try {
    const d = await api.get('/api/task/templates')
    templates.value = d.templates || []
    tplOpen.value = true
  } catch (e) { err.value = e.message }
}
function useTemplate(tp) {
  Object.assign(form, blank(), {
    task_id: null,
    name: tp.name || '',
    entry_urls: (tp.entry_urls || []).join('\n'),
    adapter_name: tp.adapter_name || '',
    max_depth: tp.max_depth ?? 3,
    concurrency: tp.concurrency ?? 4,
    delay: tp.delay ?? 1,
    priority: tp.priority ?? 0,
    ai_enabled: !!(tp.ai_labels || []).length,
    ...levelFields(tp),
    ai_blacklist: (tp.ai_blacklist || []).join(' '),
  })
  tplOpen.value = false
  dlg.value = { title: '用模板新建任务' }
}
async function delTemplate(tp) {
  if (!confirm(`删除模板「${tp.name}」？`)) return
  try {
    await api.post('/api/task/template/delete', { id: tp.id })
    await openTemplates()
  } catch (e) { err.value = e.message }
}

function exportTasks() {
  window.location.href = '/api/tasks/export'
}
async function importTasks(ev) {
  const f = ev.target.files && ev.target.files[0]
  if (!f) return
  try {
    const text = await f.text()
    const data = JSON.parse(text)
    const r = await api.post('/api/tasks/import', data)
    importMsg.value = `导入完成：新建 ${r.created || 0}，重复跳过 ${r.skipped_dup || 0}`
      + (r.missing_adapter && r.missing_adapter.length
          ? `，缺适配器跳过 ${r.missing_adapter.length}` : '')
    setTimeout(() => (importMsg.value = ''), 5000)
    await load()
  } catch (e) { err.value = e.message } finally { ev.target.value = '' }
}

const ctx = ref(null)     // 任务行右键菜单
const dlg = ref(null)          // 当前编辑中的任务对象（null = 关闭）
// 2026-10-29（新需求）：保存目录原来只能手打路径。浏览器拿不到本机路径，
// 所以统一走服务端的目录浏览（FolderPicker → /api/fs/list）：
// 本机打开就是本机磁盘，Docker/NAS 打开就是容器里挂载的那些卷。
const pickerOpen = ref(false)
const pickerTarget = ref('')
const pickerStart = ref('')
function pickDir(field) {
  pickerTarget.value = field
  pickerStart.value = String(form[field] || '')
  pickerOpen.value = true
}
function onPickedDir(p) {
  if (pickerTarget.value) form[pickerTarget.value] = p
  pickerOpen.value = false
}
// 2026-10-27（编号 10）：快速添加 —— 粘贴网址，用默认配置直接建任务。
const quick = ref(null)        // { text, name, start } | null
const quickBusy = ref(false)
const quickErr = ref('')

function openQuick() {
  quick.value = { text: '', name: '', start: true }
  quickErr.value = ''
}

async function submitQuick() {
  const text = String(quick.value?.text || '').trim()
  if (!text) { quickErr.value = '请先粘贴网址（每行一个）'; return }
  quickBusy.value = true
  quickErr.value = ''
  try {
    const r = await api.post('/api/task/quick', {
      urls: text, name: String(quick.value?.name || '').trim(),
      start: !!quick.value?.start,
    })
    if (!r.ok) throw new Error(r.error || '建任务失败')
    quick.value = null
    await load()
  } catch (e) { quickErr.value = e.message } finally { quickBusy.value = false }
}

// V0.0.17：从剪贴板读取网址（navigator.clipboard.readText）。
// 无权限或失败给完整中文提示，并保留下方文本框作为手工粘贴入口。
async function readClipboard() {
  quickErr.value = ''
  try {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      quickErr.value = '当前浏览器不支持剪贴板读取，请手工粘贴网址后继续。'
      return
    }
    const text = await navigator.clipboard.readText()
    if (!text || !text.trim()) {
      quickErr.value = '剪贴板内容为空，请手工粘贴网址后继续。'
      return
    }
    if (!quick.value) quick.value = { text: '', name: '', start: true }
    quick.value.text = text
  } catch (e) {
    quickErr.value = '剪贴板读取失败：浏览器未授予读取权限或当前环境不支持，请手工粘贴网址后继续。'
  }
}
const form = reactive({})
const saving = ref(false)

// V0.0.6：建任务时把「这回按什么词去找图」摆在最上面 —— 与桌面端同一张词表。
// 手填的词从表单实时算，学来的词从后端统一词表接口取（导入图库后自动出现）。
const findTerms = ref({ want: [], avoid: [], learned: [], soft_avoid: [] })
async function loadFindTerms() {
  try {
    const r = await api.get('/api/find-terms')
    if (r && r.ok) findTerms.value = r
  } catch (e) { /* 拿不到就只显示手填的词，不影响建任务 */ }
}
const splitWords = (s) => String(s || '').split(/[\s,，、;；]+/).filter(Boolean)
const termsHint = computed(() => {
  const typed = []
  for (const lv of (form.ai_levels || [])) {
    for (const w of splitWords(lv)) {
      if (!typed.some((x) => x.toLowerCase() === w.toLowerCase())) typed.push(w)
    }
  }
  const avoid = []
  for (const w of splitWords(form.ai_blacklist).concat(findTerms.value.avoid || [])) {
    if (!avoid.some((x) => x.toLowerCase() === w.toLowerCase())) avoid.push(w)
  }
  const learned = (findTerms.value.learned || []).filter(
    (w) => !typed.some((x) => x.toLowerCase() === String(w).toLowerCase()))
  const want = typed.concat(learned).filter(
    (w) => !avoid.some((x) => x.toLowerCase() === String(w).toLowerCase()))
  const fmt = (xs) => xs.length ? xs.slice(0, 12).join('、') + (xs.length > 12 ? '…' : '') : '（无）'
  // V0.0.7：判退 1 次的词处于观察期，仅进入待审核，不下载前拦截
  const soft = (findTerms.value.soft_avoid || []).filter(
    (w) => !avoid.some((x) => x.toLowerCase() === String(w).toLowerCase()))
  return { want: fmt(want), learned: fmt(learned), avoid: fmt(avoid), soft: fmt(soft) }
})

const blank = () => ({
  task_id: null, name: '', entry_urls: '', adapter_name: '', output_dir: '',
  max_depth: 3, concurrency: 4, delay: 1, priority: 5,
  use_proxy: false, proxy_id: null,
  ai_enabled: false, ai_labels: '', ai_labels_l2: '', ai_labels_l3: '',
  // 2026-10-28：主题词无限分级 —— 每层一个字符串（多个词空格分隔），
  // 每层一个「严格」开关。提交时转成 ai_label_levels / ai_label_strict_levels。
  ai_levels: [''], ai_stricts: [false],
  ai_blacklist: '', ai_threshold: 0.35, auto_run: false,
  user_agent: '', cookie: '',
  // V0.0.12：桌面已有、网页补齐的字段（口径与桌面表单一致）。
  // 图片大小过滤表单用 KB，提交时 ×1024 转字节（与桌面同一换算）。
  schedule_at: '', allowed_hours: '', rate_limit: 0, max_pages: 0,
  min_width: 0, min_height: 0, min_kb: 0, max_kb: 0,
  keyword_subdir: false,
  ai_match_mode: 'any', ai_exclude_match_mode: 'any',
})

// 后端详情里的 ai_label_levels（二维数组）→ 表单用的「每层一个字符串」。
// 老任务/老后端没有这个字段时回退用一/二/三级三格，行为与以前一致。
function levelFields(row) {
  let lv = Array.isArray(row.ai_label_levels) ? row.ai_label_levels : []
  let st = Array.isArray(row.ai_label_strict_levels) ? row.ai_label_strict_levels : []
  if (!lv.length) {
    lv = [row.ai_labels || [], row.ai_labels_l2 || [], row.ai_labels_l3 || []]
    const one = !!row.ai_label_strict
    st = [false, one, one]
  }
  lv = lv.map((x) => (Array.isArray(x) ? x.join(' ') : String(x || '')))
  st = lv.map((_, i) => !!st[i])
  // 尾部空层不显示（后端保存时也会裁掉），中间空层要保留位置
  while (lv.length > 1 && !String(lv[lv.length - 1]).trim()) { lv.pop(); st.pop() }
  if (!lv.length) { lv = ['']; st = [false] }
  return { ai_levels: lv, ai_stricts: st }
}

function addLevel() {
  if (form.ai_levels.length >= 20) return
  form.ai_levels.push('')
  form.ai_stricts.push(false)
}

function removeLevel(i) {
  if (i <= 0) return          // 一级是入门票，不许删
  form.ai_levels.splice(i, 1)
  form.ai_stricts.splice(i, 1)
}

function openNew() {
  Object.assign(form, blank())
  adapterTouched.value = false     // 新建时允许按网址自动选适配器
  loadFindTerms()
  dlg.value = { title: '新建任务' }
}
async function openEdit(t) {
  try {
    const d = await api.task(t.id)
    const row = d.task || d
    Object.assign(form, blank(), {
      task_id: t.id,
      name: row.name || '',
      entry_urls: (row.entry_urls || []).join('\n'),
      adapter_name: row.adapter_name || '',
      output_dir: row.output_dir || '',
      max_depth: row.max_depth ?? 3,
      concurrency: row.concurrency ?? 4,
      delay: row.delay ?? 1,
      priority: row.priority ?? 0,
      use_proxy: !!row.use_proxy,
      proxy_id: row.proxy_id || null,
      ai_enabled: !!row.ai_enabled,
      ...levelFields(row),
      ai_blacklist: (row.ai_blacklist || []).join(' '),
      ai_threshold: row.ai_threshold ?? 0.35,
      auto_run: !!row.auto_run,
      user_agent: row.user_agent || '',
      cookie: row.cookie || '',
      // V0.0.12：补齐桌面已有字段回填（图片大小回填成 KB，与桌面同一口径）
      schedule_at: row.schedule_at || '',
      allowed_hours: row.allowed_hours || '',
      rate_limit: row.rate_limit ?? 0,
      max_pages: row.max_pages ?? 0,
      min_width: row.min_width ?? 0,
      min_height: row.min_height ?? 0,
      min_kb: Math.round((row.min_bytes ?? 0) / 1024),
      max_kb: Math.round((row.max_bytes ?? 0) / 1024),
      keyword_subdir: !!row.keyword_subdir,
      ai_match_mode: row.ai_match_mode || 'any',
      ai_exclude_match_mode: row.ai_exclude_match_mode || 'any',
    })
    loadFindTerms()
    dlg.value = { title: `编辑任务 #${t.id}` }
  } catch (e) { err.value = e.message }
}

async function saveTask() {
  if (!form.entry_urls.trim()) { err.value = '请填写入口网址'; return }
  saving.value = true
  try {
    const body = {
      ...form,
      // 多入口：换行/空格/逗号都能分（后端 _as_words 统一处理）
      entry_urls: form.entry_urls.split(/[\s,，]+/).filter(Boolean),
      proxy_id: form.use_proxy ? (form.proxy_id || null) : null,
      // 2026-10-28：主题词分级按「层数组 + 每层严格」提交（层数不限）
      ai_label_levels: form.ai_levels.map((s) => String(s || '').trim()),
      ai_label_strict_levels: form.ai_levels.map((_, i) => !!form.ai_stricts[i]),
      // V0.0.12：图片大小表单填 KB，提交转字节（与桌面 *1024 同一口径）
      min_bytes: Math.round((Number(form.min_kb) || 0) * 1024),
      max_bytes: Math.round((Number(form.max_kb) || 0) * 1024),
    }
    // 这几个只是表单内部状态，别发给后端（后端不认，白占带宽）
    delete body.ai_levels
    delete body.ai_stricts
    delete body.min_kb
    delete body.max_kb
    if (!body.task_id) delete body.task_id
    const r = await api.taskSave(body)
    info.value = r.created ? `已创建任务 #${r.task_id}` : `已保存任务 #${r.task_id}`
    setTimeout(() => (info.value = ''), 2600)
    dlg.value = null
    await load()
  } catch (e) { err.value = e.message } finally { saving.value = false }
}

async function delTask(t) {
  // V0.0.27：改成带「同时删除图片」勾选的对话框（原来是写死「图片不受影响」
  // 的 confirm，用户明确要求给个对勾）
  openDelete([t.id])
}

async function loadMeta() {
  try { proxies.value = (await api.proxies()).items || [] } catch { /* 忽略 */ }
  try {
    const d = await api.view('adapters', { limit: 200 })
    // 2026-10-29（第 5 条）：原来只留 name，把 domains 丢掉了，结果「从常用网址
    // 建任务」永远不知道该选哪个适配器，每次都停在「（自动 / gallery-dl）」。
    // 后端 /api/view/adapters 本来就下发 domains（与 rules.match_adapter 同源），
    // 这里留住它，按域名自己匹配一次即可。
    adapters.value = (d.items || [])
      .filter((x) => x && x.name)
      .map((x) => ({
        name: x.name,
        domains: Array.isArray(x.domains) ? x.domains : [],
        ok: x.ok !== false,
      }))
  } catch { /* 忽略 */ }
  autoPickAdapter()
}

// ── 2026-10-29（第 5 条）：按入口网址自动选适配器 ────────────────────
// 口径与后端 crawler.rules.match_adapter 保持一致：只看**健康规则**（ok），
// 域名匹配 host === d 或 host 以 "."+d 结尾，多条命中取**最长域名**
// （www.example.com 同时命中 example.com 和 www.example.com 时选后者）。
// 只在用户没手动改过适配器时自动填 —— 用户选过的值不许被悄悄覆盖。
const adapterTouched = ref(false)

function hostOf(raw) {
  const s = String(raw || '').trim()
  if (!s) return ''
  try {
    return new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(s) ? s : 'https://' + s)
      .hostname.toLowerCase()
  } catch { return '' }
}

function pickAdapterByUrls(text) {
  const urls = String(text || '').split(/[\s,，]+/).filter(Boolean)
  if (!urls.length) return ''
  let best = ''
  let bestLen = 0
  for (const u of urls) {
    const host = hostOf(u)
    if (!host) continue
    for (const a of adapters.value) {
      if (!a.ok) continue
      for (const d0 of a.domains) {
        const d = String(d0 || '').toLowerCase().trim().replace(/^\*\./, '')
        if (!d) continue
        const hit = host === d || host.endsWith('.' + d)
        if (hit && d.length > bestLen) { bestLen = d.length; best = a.name }
      }
    }
  }
  return best
}

function autoPickAdapter() {
  if (adapterTouched.value) return
  const n = pickAdapterByUrls(form.entry_urls)
  if (n && n !== form.adapter_name) form.adapter_name = n
}

watch(() => form.entry_urls, autoPickAdapter)


onMounted(async () => {
  load()
  // ⚠ 必须先拿到适配器清单再处理 ?entry=，否则自动选适配器时清单还是空的
  //（第 5 条「从常用网址建任务不自动选适配器」就出在这个时序上）。
  await loadMeta()
  timer = setInterval(() => load(true), 4000)
  // 2026-10-27：「常用网址」页选择「用它建任务」→ /tasks?entry=<url>
  // 直接打开新建表单并预填入口，省得用户再复制一遍。
  const entry = String(route.query.entry || '').trim()
  if (entry) {
    openNew()
    form.entry_urls = entry
    adapterTouched.value = false
    autoPickAdapter()          // watch 是异步的，这里显式补一刀
    router.replace({ path: '/tasks' })
  }
})
onMounted(() => {
  window.addEventListener('keydown', onKey)
  unbindShell = [
    onShellSelectAll(() => selectAllForce()),
    onShellRefresh(() => load(true)),
    // 任务页有自己的过滤框：Ctrl+F 优先定位它，并取消顶栏全局搜索的兜底。
    onShellFocusSearch((e) => {
      if (!kwEl.value) return
      e.preventDefault()
      kwEl.value.focus()
      try { kwEl.value.select() } catch (_err) { /* 忽略 */ }
    }),
  ]
})
onUnmounted(() => {
  clearInterval(timer)
  window.removeEventListener('keydown', onKey)
  unbindShell.forEach((fn) => fn())
  unbindShell = []
})
</script>

<template>
  <div class="page">
   <div class="layout">
    <div class="content">
    <div class="bar card">
      <!-- 2026-10-28：这排计数胶囊挪进左侧「状态」树了（那边还能点着筛选），
           这里只保留一句「当前显示几条」，避免两处数字互相对不上。 -->
      <div class="counts">
        <span class="pill muted">显示 {{ shownTasks.length }} / {{ tasks.length }}</span>
        <!-- 选中后出现的批量工具条（qBittorrent 那种：先选，再对选区动手） -->
        <span v-if="sel.size" class="selbar">
          <b>已选 {{ sel.size }}</b>
          <button class="btn sm" @click="batchAct('start')">
            <Icon name="play" :size="13" />开始</button>
          <button class="btn sm" @click="batchAct('pause')">
            <Icon name="pause" :size="13" />暂停</button>
          <button class="btn sm" @click="batchAct('stop')">
            <Icon name="circle-stop" :size="13" />停止</button>
          <button class="btn sm" @click="batchAct('retry')">
            <Icon name="refresh-cw" :size="13" />重试</button>
          <button class="btn sm danger" @click="batchAct('delete')">
            <Icon name="trash" :size="13" />删除</button>
          <button class="btn sm ghost" @click="clearSel">
            <Icon name="x" :size="13" />取消选中</button>
        </span>
        <span v-if="statusFilter" class="pill warn">
          {{ (FILTERS.find((f) => f.k === statusFilter) || {}).label }}
          <button class="pill-x" title="取消状态筛选" @click="statusFilter = ''">×</button>
        </span>
      </div>
      <div class="ops">
        <input ref="kwEl" v-model="kw" class="search" type="text"
               title="过滤任务列表（名称 / 网址 / #编号）；按 Ctrl+F 可直接定位到这里"
               placeholder="过滤任务列表…（名称 / 网址 / #编号）" />
        <select v-model="sortBy" class="sortsel" title="排序">
          <option value="id">按编号 ↑</option>
          <option value="id_desc">按编号 ↓</option>
          <option value="name">按名称</option>
          <option value="status">按状态</option>
          <option value="got">按本次张数</option>
        </select>
        <button class="btn primary" @click="all('start')">
          <Icon name="play" :size="14" />全部开始</button>
        <button class="btn" @click="all('stop')">
          <Icon name="circle-stop" :size="14" />全部停止</button>
        <button class="btn" @click="openQuick">
          <Icon name="zap" :size="14" />快速添加</button>
        <button class="btn" @click="openNew">
          <Icon name="plus" :size="14" />新建任务</button>
        <button class="btn" @click="openTemplates">
          <Icon name="layers" :size="14" />从模板新建</button>
        <button class="btn" title="导出任务配置（不含图片）" @click="exportTasks">
          <Icon name="download" :size="14" />导出</button>
        <label class="btn file" title="从 JSON 导入任务配置">
          <Icon name="upload" :size="14" />导入
          <input type="file" accept=".json,application/json" hidden @change="importTasks" />
        </label>
        <button class="btn ghost" :disabled="loading" title="刷新列表" @click="load()">
          <Icon :anim="loading ? 'spin' : ''" name="refresh-cw" :size="14" />刷新</button>
      </div>
    </div>

    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="info" class="ok">{{ info }}</p>
    <p v-if="importMsg" class="ok">{{ importMsg }}</p>

    <div class="card table">
      <div class="th row" :style="gridStyle">
        <!-- 2026-10-28：原来「序」和「#」并排两列，用户问“这不是重复的吗”。
             按用户要求：**只显示序**（行号），任务编号留在悬停提示/弹窗里。 -->
        <span title="行号（按当前排序）">
          <input type="checkbox" class="pick" :checked="allShownSelected"
                 :title="allShownSelected ? '取消全选' : '全选当前筛选结果'"
                 @change="selectAllShown" />
          <i class="rz" title="拖动调整列宽（双击复位）" @pointerdown="startResize(0, $event)"
             @dblclick.stop.prevent="resetCol(0)" @click.stop />
        </span>
        <button class="hsort" :class="{ on: sortBase === 'name' }"
                title="点击按名称排序，再次点击反向" @click="toggleSort('name')">
          任务<Icon v-if="sortBase === 'name'" name="arrow-up-down" :size="11" />
          <i class="rz" title="拖动调整列宽（双击复位）" @pointerdown="startResize(1, $event)"
             @dblclick.stop.prevent="resetCol(1)" @click.stop /></button>
        <button class="hsort" :class="{ on: sortBase === 'status' }"
                title="点击按状态排序，再次点击反向" @click="toggleSort('status')">
          状态<Icon v-if="sortBase === 'status'" name="arrow-up-down" :size="11" />
          <i class="rz" title="拖动调整列宽（双击复位）" @pointerdown="startResize(2, $event)"
             @dblclick.stop.prevent="resetCol(2)" @click.stop /></button>
        <button class="hsort" :class="{ on: sortBase === 'got' }"
                title="点击按本次新增张数排序，再次点击反向" @click="toggleSort('got')">
          进度<Icon v-if="sortBase === 'got'" name="arrow-up-down" :size="11" />
          <i class="rz" title="拖动调整列宽（双击复位）" @pointerdown="startResize(3, $event)"
             @dblclick.stop.prevent="resetCol(3)" @click.stop /></button>
        <span>入口
          <i class="rz" title="拖动调整列宽（双击复位）" @pointerdown="startResize(4, $event)"
             @dblclick.stop.prevent="resetCol(4)" @click.stop /></span>
        <span>操作
          <i class="rz" title="拖动调整列宽（双击复位）" @pointerdown="startResize(5, $event)"
             @dblclick.stop.prevent="resetCol(5)" @click.stop /></span>
      </div>
      <div v-for="(t, i) in shownTasks" :key="t.id" class="row tr"
           :style="gridStyle"
           :class="{ sel: sel.has(t.id) }"
           title="单击选中（Shift 连选 / Ctrl 加选），双击编辑"
           @click="onRowClick($event, t, i)"
           @dblclick="openEdit(t)"
           @contextmenu="onRowContext($event, t)">
        <span class="mono dim no-sel" :title="`任务编号 #${t.id}`">
          <input type="checkbox" class="pick" :checked="sel.has(t.id)"
                 :title="sel.has(t.id) ? '取消选中' : '选中这一行'"
                 @click.stop="toggleSel(t.id)" />
          {{ i + 1 }}
        </span>
        <span class="name" :title="t.name">{{ t.name }}</span>
        <span><i class="pill" :class="statusClass[t.status] || 'muted'"
                 :title="t.error || ''">
          <!-- 运行中的任务在状态前面加个脉冲点：一屏 20 行时，
               "哪个在跑"靠颜色不够，动一下立刻能扫到 -->
          <Icon v-if="t.status === 'running'" anim="pulse" :size="11" class="pill-dot" />
          {{ statusLabel(t.status) }}</i></span>
        <!-- 2026-10-27 口径修正：got = **本次**这一轮新下，total = **累计**在库张数，
             两者不是分子分母（后端注释专门警告过）。原来写「已下 X / 共 Y」
             会让人以为跑完 30 张、累计 0 是 bug。 -->
        <span class="prog mono"
              :title="`本次新下 ${t.got || 0} 张 · 累计在库 ${t.total || 0} 张 · 累计失败网址 ${t.failed_urls || 0} 条 · 页面 ${t.pages_done || 0}/${t.pages_total || 0}`">
          本 {{ t.got || 0 }} · 累 {{ t.total || 0 }}
          <em v-if="t.failed_urls" title="累计失败网址条数">✗{{ t.failed_urls }}</em>
        </span>
        <span class="mono entry" :title="t.entry_urls.join(' ')">
          {{ t.entry_urls[0] || '—' }}<em v-if="t.entry_urls.length > 1"> +{{ t.entry_urls.length - 1 }}</em>
        </span>
        <span class="acts" @click.stop>
          <!-- 2026-10-29（第 2 条）：这一列原来「图标 + 文字」并排，列一窄文字就
               被压成竖排（一个字一行），非常难看。现在**只留图标**，
               文案全进 title：鼠标悬停看得到，屏幕也不用再挤。 -->
          <button class="btn sm ico" :disabled="busy.has(t.id) || t.status === 'running'"
                  :title="t.status === 'running' ? '正在运行'
                    : (t.status === 'stopped' ? '从断点继续' : '开始这一轮')"
                  :aria-label="t.status === 'stopped' ? '继续' : '开始'"
                  @click="act(t, 'start')">
            <Icon name="play" :size="14" /></button>
          <button class="btn sm ico" :disabled="busy.has(t.id) || t.status !== 'running'"
                  title="暂停（仅停止当前轮次；选择「继续」从断点续传，已下载图片不重复下载）"
                  aria-label="暂停" @click="act(t, 'pause')">
            <Icon name="pause" :size="14" /></button>
          <button class="btn sm ico" title="编辑任务" aria-label="编辑" @click="openEdit(t)">
            <Icon name="pencil" :size="14" /></button>
          <button class="btn sm ico only-sm" title="更多操作" aria-label="更多操作"
                  @click="openRowMenu($event, t)">
            <Icon name="ellipsis" :size="14" /></button>
          <button class="btn sm ico hide-sm"
                  title="查看该任务的失败网址，可单选重试" aria-label="失败网址"
                  @click="openFailed(t)">
            <Icon name="triangle-alert" :size="14" /></button>
          <button class="btn sm ico danger hide-sm" title="删除任务（可选同时删除图片）"
                  aria-label="删除" @click="delTask(t)">
            <Icon name="trash" :size="14" /></button>
        </span>
      </div>
      <!-- V0.0.33：读取中先给三行骨架，不要让用户对着一片空白等 -->
      <div v-if="loading && !tasks.length" class="skeleton-box">
        <div class="skeleton skeleton-line"></div>
        <div class="skeleton skeleton-line"></div>
        <div class="skeleton skeleton-line"></div>
      </div>
      <EmptyState v-else-if="!tasks.length" icon="list-checks"
                  title="暂无任务" desc="点击右上角「新建任务」创建第一个采集任务。"
                  action-text="新建任务" @action="openNew" />
      <p v-else-if="!shownTasks.length" class="empty">
        没有匹配当前筛选的任务<span v-if="kw">（关键词「{{ kw }}」）</span>
      </p>
    </div>
    </div>
   </div>

    <ContextMenu :menu="ctx" @close="ctx = null" />

    <!-- 保存目录选择：与「从文件夹导入」同一个选择器，服务端列目录 -->
    <FolderPicker :open="pickerOpen" :start="pickerStart" title="选择任务保存目录"
                  @close="pickerOpen = false" @picked="onPickedDir" />

    <!-- 失败网址 -->
    <!-- V0.0.58：弹窗必须挂到 body —— dlg 过渡给 .modal 加 transform，就地渲染会让 fixed 遮罩以 .page 为包含块，盖不住 sticky 顶栏，顶栏标题透出高亮（灯箱同类 bug）。 -->
    <Teleport to="body">
    <Transition name="dlg">
    <div v-if="failed" class="mask" @click.self="failed = null">
      <div class="modal card">
        <h3>任务 #{{ failed.tid }} 的失败网址（{{ failed.items.length }}）</h3>
        <p class="muted small">仅重试所选单条 URL（不重新发现页面）。勾选后点击「重试选中」</p>
        <div class="flist">
          <label v-for="it in failed.items" :key="it.url" class="fitem">
            <input type="checkbox" :checked="failedSel.has(it.url)" @change="toggleFailed(it.url)" />
            <span class="mono url">{{ it.url }}</span>
            <span class="muted small">{{ it.error }}</span>
          </label>
          <p v-if="!failed.items.length" class="muted" style="padding:12px">该任务无失败网址</p>
        </div>
        <div class="foot">
          <button class="btn sm" @click="clearFailed">清空清单</button>
          <span style="flex:1"></span>
          <button class="btn sm" @click="failed = null">关闭</button>
          <button class="btn sm" :disabled="!failedSel.size" @click="retryFailed(false)">重试选中（{{ failedSel.size }}）</button>
          <button class="btn primary sm" :disabled="!failed.items.length" @click="retryFailed(true)">全部重试</button>
        </div>
      </div>
    </div>
    </Transition>
    </Teleport>

    <!-- V0.0.27：删除任务（可选「同时删除图片」） -->
    <!-- V0.0.58：弹窗必须挂到 body —— dlg 过渡给 .modal 加 transform，就地渲染会让 fixed 遮罩以 .page 为包含块，盖不住 sticky 顶栏，顶栏标题透出高亮（灯箱同类 bug）。 -->
    <Teleport to="body">
    <Transition name="dlg">
    <div v-if="delDlg" class="mask" @click.self="delDlg.busy ? null : (delDlg = null)">
      <div class="modal card deldlg">
        <h3>{{ delDlg.title }}</h3>
        <p class="muted small">
          任务配置会被删除；这个任务没跑完的进度、失败网址清单一并清掉。
        </p>
        <label class="delchk" :class="{ off: !delDlg.images }">
          <input type="checkbox" v-model="delDlg.withImages"
                 :disabled="!delDlg.images || delDlg.busy" />
          <span>同时删除这个任务名下的 <b>{{ delDlg.images }}</b> 张图片</span>
        </label>
        <p v-if="delDlg.images" class="muted delnote">
          勾上后：软件自己下载的图片进回收站（可还原）；从本地文件夹导入的原图
          只清掉库里的登记，磁盘上的文件一个都不动。
        </p>
        <p v-else class="muted delnote">这个任务名下没有图片，勾不勾结果一样。</p>
        <div class="foot">
          <span style="flex:1"></span>
          <button class="btn sm" :disabled="delDlg.busy" @click="delDlg = null">取消</button>
          <button class="btn danger sm" :disabled="delDlg.busy" @click="doDelete">
            <Icon :anim="delDlg.busy ? 'spin' : ''"
                  :name="delDlg.busy ? 'loader-circle' : 'trash'" :size="13" />
            {{ delDlg.busy ? '正在删除…' : '删除' }}</button>
        </div>
      </div>
    </div>
    </Transition>
    </Teleport>

    <!-- 模板 -->
    <!-- V0.0.58：弹窗必须挂到 body —— dlg 过渡给 .modal 加 transform，就地渲染会让 fixed 遮罩以 .page 为包含块，盖不住 sticky 顶栏，顶栏标题透出高亮（灯箱同类 bug）。 -->
    <Teleport to="body">
    <Transition name="dlg">
    <div v-if="tplOpen" class="mask" @click.self="tplOpen = false">
      <div class="modal card">
        <h3>任务模板</h3>
        <p class="muted small">模板仅保存任务配置（不含 Cookie）。点击「套用」填入新建表单</p>
        <div class="flist">
          <div v-for="tp in templates" :key="tp.id" class="fitem tpl">
            <div>
              <b>{{ tp.name }}</b>
              <div class="muted small">{{ (tp.entry_urls || [])[0] || '—' }} · {{ tp.adapter_name || '自动' }}</div>
            </div>
            <span style="flex:1"></span>
            <button class="btn sm" @click="useTemplate(tp)">套用</button>
            <button class="btn sm danger" @click="delTemplate(tp)">删除</button>
          </div>
          <p v-if="!templates.length" class="muted" style="padding:12px">暂无模板（在任务行点击「存模板」创建）</p>
        </div>
        <div class="foot"><button class="btn sm" @click="tplOpen = false">关闭</button></div>
      </div>
    </div>
    </Transition>
    </Teleport>

    <!-- 编辑 / 新建 -->
    <!-- 快速添加：一行一个网址，用默认配置建任务 -->
    <!-- V0.0.58：弹窗必须挂到 body —— dlg 过渡给 .modal 加 transform，就地渲染会让 fixed 遮罩以 .page 为包含块，盖不住 sticky 顶栏，顶栏标题透出高亮（灯箱同类 bug）。 -->
    <Teleport to="body">
    <Transition name="dlg">
    <div v-if="quick" class="mask" @click.self="quick = null">
      <div class="modal card">
        <h3>快速添加</h3>
        <p class="tiny muted">
          一行一个网址（http/https 开头），用<b>默认配置</b>直接建任务，不填表单。
          深度/并发/间隔取「设置 → 采集与下载」里的默认值。
        </p>
        <div class="inline">
          <button class="btn sm" type="button" @click="readClipboard">
            <Icon name="clipboard-check" :size="13" />从剪贴板读取</button>
          <span class="muted small">读取失败时请在下方文本框手工粘贴</span>
        </div>
        <textarea rows="7" v-model="quick.text"
                  placeholder="https://example.com/gallery/1&#10;https://example.com/gallery/2"></textarea>
        <div class="row">
          <label>任务名</label>
          <input type="text" v-model="quick.name" placeholder="留空 = 用第一个网址的域名" />
        </div>
        <div class="inline">
          <input type="checkbox" v-model="quick.start" />
          <span class="muted small">创建后立即开始</span>
        </div>
        <p v-if="quickErr" class="err small">{{ quickErr }}</p>
        <div class="acts">
          <button class="btn" @click="quick = null">取消</button>
          <button class="btn primary" :disabled="quickBusy" @click="submitQuick">
            {{ quickBusy ? '正在建…' : '建任务' }}
          </button>
        </div>
      </div>
    </div>
    </Transition>
    </Teleport>

    <!-- V0.0.58：弹窗必须挂到 body —— dlg 过渡给 .modal 加 transform，就地渲染会让 fixed 遮罩以 .page 为包含块，盖不住 sticky 顶栏，顶栏标题透出高亮（灯箱同类 bug）。 -->
    <Teleport to="body">
    <Transition name="dlg">
    <div v-if="dlg" class="mask" @click.self="dlg = null">
      <div class="modal card task-form" :class="{ newbie: uiMode === 'newbie', 'adv-open': advOpen }">
        <h3>{{ dlg.title }}</h3>
        <div v-if="uiMode === 'newbie'" class="adv-toggle">
          <button class="btn sm" type="button" @click="advOpen = !advOpen">
            <Icon name="panel-left" :size="13" />
            {{ advOpen ? '收起高级选项' : '展开高级选项' }}
          </button>
          <span class="muted small">新手模式仅显示基础项；其余配置仍按现有值生效。</span>
        </div>
        <div class="muted small" style="padding:6px 8px;margin-bottom:8px;border:1px solid var(--border, #444);border-radius:6px;line-height:1.6">
          本轮目标词：<b>{{ termsHint.want }}</b>
          <span v-if="(findTerms.learned || []).length">　［学到的：{{ termsHint.learned }}］</span><br />
          排除词：<b>{{ termsHint.avoid }}</b>
          <div v-if="(findTerms.soft_avoid || []).length" class="tiny">观察期（判退 1 次，仅进入待审核）：{{ termsHint.soft }}</div>
          <div class="tiny">手工填写的词随输入实时更新；导入原有图库学习到的词自动并入。采集时于下载前按此筛选。</div>
        </div>
        <div class="f">
          <label>入口网址（每行一个）</label>
          <textarea v-model="form.entry_urls" rows="4" placeholder="https://example.com/"></textarea>
        </div>
        <div class="f2">
          <div class="f">
            <label>适配器</label>
            <select v-model="form.adapter_name" @change="adapterTouched = true">
              <option value="">（自动 / gallery-dl）</option>
              <option v-for="a in adapters" :key="a.name" :value="a.name">
                {{ a.name }}{{ a.ok ? '' : '（规则不完整）' }}
              </option>
            </select>
            <span v-if="!adapterTouched && form.adapter_name" class="muted tiny">
              已按入口网址自动匹配，可手动更改
            </span>
          </div>
          <div class="f">
            <label>保存目录</label>
            <div class="inline">
              <input v-model="form.output_dir" type="text" placeholder="留空则使用默认图库目录" />
              <button class="btn sm" type="button" title="浏览运行软件那台机器上的目录"
                      @click="pickDir('output_dir')">
                <Icon name="folder" :size="13" />浏览
              </button>
            </div>
          </div>
        </div>
        <div class="f4 adv">
          <div class="f adv"><label>深度</label><input v-model.number="form.max_depth" type="number" /></div>
          <div class="f adv"><label>并发</label><input v-model.number="form.concurrency" type="number" /></div>
          <div class="f adv"><label>间隔(秒)</label><input v-model.number="form.delay" type="number" step="0.1" /></div>
          <div class="f adv"><label>优先级</label><input v-model.number="form.priority" type="number" min="1" max="9" title="数值越小越先执行" /></div>
        </div>
        <!-- V0.0.12：桌面已有、网页补齐的运行控制项 -->
        <div class="f4 adv">
          <div class="f adv"><label>最大页数</label><input v-model.number="form.max_pages" type="number" min="0" title="每个入口最多翻多少页，0=用全局默认" /></div>
          <div class="f adv"><label>限速(张/分)</label><input v-model.number="form.rate_limit" type="number" min="0" max="600" title="每分钟最多下载几张，0=不限。抓大站调慢可显著降低被封概率" /></div>
          <div class="f adv"><label>定时运行</label><input v-model="form.schedule_at" type="text" placeholder="08:30 或 2026-10-01 08:30" title="HH:MM=每天定时；日期 HH:MM=一次性；留空不定时" /></div>
          <div class="f adv"><label>允许时段</label><input v-model="form.allowed_hours" type="text" placeholder="22-6" title="只在开始-结束的小时数内抓取（0-23），留空=全天" /></div>
        </div>
        <div class="f4 adv">
          <div class="f adv"><label>最小宽(px)</label><input v-model.number="form.min_width" type="number" min="0" title="宽小于此值的图片不收，0=不限" /></div>
          <div class="f adv"><label>最小高(px)</label><input v-model.number="form.min_height" type="number" min="0" title="高小于此值的图片不收，0=不限" /></div>
          <div class="f adv"><label>文件≥(KB)</label><input v-model.number="form.min_kb" type="number" min="0" title="小于此大小(KB)的图片不收，0=不限" /></div>
          <div class="f"><label>文件≤(KB)</label><input v-model.number="form.max_kb" type="number" min="0" title="大于此大小(KB)的图片不收，0=不限" /></div>
        </div>
        <div class="f">
          <label>代理</label>
          <div class="inline">
            <input type="checkbox" v-model="form.use_proxy" />
            <span class="muted">使用代理</span>
            <select v-model="form.proxy_id" :disabled="!form.use_proxy">
              <option :value="null">（自动挑健康节点）</option>
              <option v-for="p in proxies" :key="p.id" :value="p.id">{{ p.name }} · {{ p.scheme }}://{{ p.host }}:{{ p.port }}</option>
            </select>
          </div>
        </div>
        <div class="f adv">
          <label>AI 主题词（白名单，可无限分级）</label>
          <div v-for="(w, i) in form.ai_levels" :key="i" class="lv-row">
            <span class="lv-no">{{ i === 0 ? '一级 入门票' : (i + 1) + ' 级' }}</span>
            <input v-model="form.ai_levels[i]" type="text"
                   :placeholder="i === 0 ? '多个词以空格分隔' : '这一级的细分词（留空 = 这级不设条件）'" />
            <label v-if="i > 0" class="lv-strict"
                   title="勾选后该级必须命中，否则整张图判定为不命中（可逐级设置）">
              <input type="checkbox" v-model="form.ai_stricts[i]" /> 严格
            </label>
            <button v-if="i > 0" class="btn xs" title="删除该级" @click="removeLevel(i)">−</button>
          </div>
          <div class="lv-actions">
            <button class="btn xs" @click="addLevel">+ 加一级</button>
            <span class="muted small">共 {{ form.ai_levels.length }} 级</span>
          </div>
          <p class="tip">
            一级命中才往下走；某一级没命中时 —— 勾了「严格」整张图判不命中，没勾就停在上一级（保留）。
            多个词请用【空格】分隔。
          </p>
        </div>
        <div class="f adv">
          <label>AI 排除词（黑名单）</label>
          <input v-model="form.ai_blacklist" type="text" placeholder="多个词用空格分隔" />
        </div>
        <div class="f2 adv">
          <div class="f adv"><label>AI 阈值（0–1）</label>
            <input v-model.number="form.ai_threshold" type="number" step="0.05" min="0" max="1" /></div>
          <div class="f adv"><label>保存后立即运行</label>
            <input type="checkbox" v-model="form.auto_run" /></div>
        </div>
        <!-- V0.0.12：匹配模式 + 关键词子目录（桌面表单已有） -->
        <div class="f2 adv">
          <div class="f adv"><label>主题词匹配</label>
            <select v-model="form.ai_match_mode">
              <option value="any">命中任一即保留</option>
              <option value="all">须全部命中</option>
            </select></div>
          <div class="f adv"><label>排除词匹配</label>
            <select v-model="form.ai_exclude_match_mode">
              <option value="any">命中任一即排除</option>
              <option value="all">须全部命中才排除</option>
            </select></div>
        </div>
        <div class="f adv">
          <label class="lv-strict" title="入库文件按命中的主题词分目录存放">
            <input type="checkbox" v-model="form.keyword_subdir" /> 按关键词建子目录
          </label>
        </div>
        <div class="f2 adv">
          <div class="f adv"><label>User-Agent</label><input v-model="form.user_agent" type="text" placeholder="留空=用全局" /></div>
          <div class="f adv"><label>站点 Cookie</label><input v-model="form.cookie" type="text" placeholder="k=v; k2=v2" /></div>
        </div>
        <div class="foot">
          <button class="btn" @click="dlg = null">取消</button>
          <button class="btn primary" :disabled="saving" @click="saveTask">{{ saving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>
    </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.page { display: grid; gap: 14px; }
/* 左筛选栏 + 右列表（qBittorrent 那种两栏） */
/* 2026-10-29：状态筛选已搬进左侧导航，这里不再需要左栏，单列铺满 */
.layout { display: grid; gap: 12px; align-items: start; }
.content { display: grid; gap: 14px; min-width: 0; }
/* 可点列头：看着像表头，鼠标移上去有反馈 */
.hsort {
  display: inline-flex; align-items: center; gap: 3px; width: 100%;
  border: 0; background: transparent; color: var(--muted); font-size: 12px;
  padding: 0; text-align: left; font-family: inherit;
}
.hsort:hover, .hsort.on { color: var(--brand); }
@media (max-width: 820px) {
  .layout { grid-template-columns: 1fr; }
}
.bar { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px; flex-wrap: wrap; gap: 8px; }
.counts { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.pill-dot { margin-right: 3px; vertical-align: -1px; }
.pill-x {
  border: 0; background: transparent; color: inherit; cursor: pointer;
  font-size: 13px; line-height: 1; padding: 0 0 0 4px;
}
.search { width: 190px; }
.sortsel { width: 108px; }
.ops { display: flex; gap: 8px; flex-wrap: wrap; }
.err { color: var(--err); margin: 0; }
.ok { color: var(--ok); margin: 0; }
.table { overflow-x: auto; overflow-y: hidden; }
/* V0.0.23：右端「操作」列被裁的根治。
   老模板 `52px 1fr 86px 124px 1.2fr 250px` 里两个 1fr 的**最小宽度是内容宽度**
   （长网址不可断行），窗口一窄整行就比卡片宽，`.table` 的 overflow:hidden
   直接把最右边的按钮切掉。现在：可伸缩列用 minmax(0, …) 允许收缩，
   操作列按内容取宽（minmax(180px, max-content)），任何宽度都不会再切按钮。 */
.row { display: grid;
  grid-template-columns: 52px minmax(0, 1fr) 86px 124px minmax(0, 1.2fr) minmax(180px, max-content);
  gap: 8px; align-items: center; padding: 9px 12px; }
.name, .entry, .prog { min-width: 0; }
/* R243 列宽可拖：手柄贴在单元格右边缘（8px 命中区，光标变 col-resize）。
   刻意做成绝对定位的透明竖条而不是「有宽度的边框」：不能让手柄占列宽，
   否则拖动时列宽会被自己撑大，越拖越飘。 */
.row > span, .row > button { position: relative; }
.rz { position: absolute; top: 50%; right: -5px; transform: translateY(-50%);
  width: 9px; height: 22px; cursor: col-resize; z-index: 3; }
.rz::after { content: ""; position: absolute; left: 4px; top: 2px; bottom: 2px;
  width: 1px; background: transparent; transition: background .12s; }
.rz:hover::after { background: var(--brand, #6d5efc); }
.th { background: #fafbff; border-bottom: 1px solid var(--line); font-size: 12px; color: var(--muted); }
.tr { border-bottom: 1px solid #f1f2f8; }
.tr:last-child { border-bottom: 0; }
.tr:hover { background: #fcfcff; }
/* 选中行（qBittorrent 也是整行变色，不只是一个勾） */
.tr.sel { background: #f5f2ff; }
.tr.sel:hover { background: #efeaff; }
.pick {
  width: 13px; height: 13px; margin: 0 3px 0 0;
  vertical-align: -2px; accent-color: var(--brand); cursor: pointer;
}
.no-sel { display: inline-flex; align-items: center; gap: 3px; }
/* 批量工具条：选中后才出现，贴在同一行里，不遮挡表格 */
.selbar {
  display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap;
  padding: 3px 8px; border: 1px solid #ded4ff; border-radius: 9px;
  background: #faf7ff; color: var(--brand);
}
.name { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.entry { color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.entry em { font-style: normal; color: var(--brand); }
.prog { color: var(--muted); font-size: 12px; }
.prog em { font-style: normal; color: var(--err); }
/* V0.0.35：整行操作按钮原来 5px 缝，图标挨着图标像连成一排；统一到 8px。
   换行时行距 6px，避免窄列里两排按钮又贴在一起。 */
.acts { display: flex; gap: 8px; flex-wrap: wrap; row-gap: 6px; justify-content: flex-end; }
.btn.sm { padding: 4px 9px; font-size: 12px; border-radius: 8px; }
/* 图标按钮：正方形 + 不换行。文字一旦换成图标，就必须给死宽高，
   否则按钮会随 flex 压缩成一条缝（连图标都被挤没）。 */
.btn.ico {
  width: 28px; height: 26px; padding: 0; display: inline-flex;
  align-items: center; justify-content: center; flex: 0 0 auto;
}
/* 全站按钮都不许把中文一个字一行地竖排（第 2 条的病根） */
.btn, .hsort, .pill { white-space: nowrap; }
.btn.danger { color: var(--err); border-color: #f3c9c9; }
.empty { text-align: center; color: var(--muted); padding: 28px; margin: 0; }

.mask {
  position: fixed; inset: 0; background: rgba(14,16,24,.55);
  display: grid; place-items: center; z-index: 70; padding: 18px;
}
.modal { width: min(720px, 96vw); max-height: 92vh; overflow: auto; padding: 18px 20px; display: grid; gap: 12px; }
.modal h3 { margin: 0; }
.f { display: grid; gap: 4px; }
.f2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.f4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
textarea {
  width: 100%; padding: 8px 10px; border: 1px solid var(--line);
  border-radius: 9px; font: inherit; resize: vertical;
}
.tip { margin: 0; font-size: 12px; color: var(--warn); }
.inline { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.inline select { flex: 1; min-width: 180px; }
.foot { display: flex; justify-content: flex-end; gap: 8px; align-items: center; }
.deldlg { width: min(520px, 94vw); }
.delchk { display: flex; align-items: center; gap: 8px; padding: 10px 12px;
          border: 1px solid var(--line); border-radius: 9px; cursor: pointer;
          background: #faf9ff; font-size: 13px; }
.delchk.off { background: transparent; color: var(--muted); cursor: default; }
.delchk input { accent-color: var(--brand); width: 15px; height: 15px; }
.delnote { font-size: 12px; line-height: 1.7; margin: 0; }
.file { display: inline-flex; align-items: center; }
.flist { max-height: 46vh; overflow: auto; display: grid; gap: 4px; }
.fitem {
  display: flex; gap: 8px; align-items: center; padding: 6px 8px;
  border-radius: 8px; border: 1px solid #f1f2f8;
}
.fitem .url { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fitem.tpl { border-color: var(--line); background: #fcfcff; }
/* 2026-10-29（第 3 条）：窗口一窄，操作列里最右边的按钮就被 .table 的
   overflow:hidden 裁掉半截 —— 断点原来是 820px，可实际 900–1200px 之间
   六个按钮 + 250px 固定操作列已经塞不下了。现在多一道 1180px 断点：
   先去掉次要按钮（失败网址 / 删除）改成「⋯」菜单，再窄才切手机布局；
   同时 .acts 允许换行兜底（列宽被手动拖窄也不会裁）。
   注意 .only-sm 的 display:none 必须写在断点**之前**，否则同特异性下
   后定义的规则会把 @media 里的 inline-flex 又盖回去。 */
.only-sm { display: none; }
@media (max-width: 1180px) {
  .row { grid-template-columns: 44px minmax(0, 1fr) 74px 100px minmax(0, .8fr) minmax(176px, max-content); }
  .hide-sm { display: none !important; }
  .only-sm { display: inline-flex; }
}
@media (max-width: 820px) {
  /* 2026-10-27：手机上原来一行塞 6 个按钮 + 入口列，直接溢出到屏幕外，
     删除键根本点不到。现在：入口列隐藏、只留 开始/编辑/⋯，其余进菜单。 */
  .row { grid-template-columns: 24px 1fr 74px 96px 108px; }
  .row .entry { display: none; }
  /* 手机上列宽由 CSS 定，拖拽手柄没有意义（而且会挡住点击） */
  .rz { display: none; }
  .f2, .f4 { grid-template-columns: 1fr; }
}

/* 2026-10-28：主题词无限分级 —— 每级一行：级别名 + 输入框 + 严格勾选 + 删级 */
.lv-row { display: flex; gap: 8px; align-items: center; margin-bottom: 6px; }
.lv-no {
  flex: 0 0 auto; min-width: 76px; font-size: 12px; color: var(--muted, #6b7280);
  white-space: nowrap;
}
.lv-row input[type="text"] { flex: 1; min-width: 120px; }
.lv-strict {
  flex: 0 0 auto; display: inline-flex; gap: 4px; align-items: center;
  font-size: 12px; white-space: nowrap; cursor: pointer;
}
.lv-actions { display: flex; gap: 10px; align-items: center; }
.btn.xs { padding: 2px 8px; font-size: 12px; }
@media (max-width: 820px) {
  /* 手机上「级别名 + 输入 + 严格 + 删」挤不下，改成两行 */
  .lv-row { flex-wrap: wrap; }
  .lv-no { min-width: 0; }
  .lv-row input[type="text"] { flex: 1 1 100%; }
}
/* V0.0.19：新手模式收起高级字段（仅隐藏显示，数据照常提交） */
.task-form.newbie:not(.adv-open) .adv { display: none; }
.adv-toggle {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin: 0 0 10px; padding: 7px 10px;
  border: 1px solid var(--border, #444); border-radius: 8px;
}
</style>
