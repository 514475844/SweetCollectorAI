<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, connectEvents } from './api'
import Icon from './components/Icon.vue'
import ShortcutsDialog from './components/ShortcutsDialog.vue'
import Onboarding from './components/Onboarding.vue'
import {
  setAnimatedIcons, webTheme, readWebTheme, setWebTheme,
  setWebHotkeys, matchWebAction, recordingHotkey,
  uiMode, setUiMode, loadUiMode,
} from './uiPrefs'
// V0.0.21：内置窗口统一接管浏览器键位与指针行为（普通浏览器访问不生效）。
import { installNativeShell, isNativeShell } from './nativeShell'
let uninstallNativeShell = null

const route = useRoute()
const router = useRouter()
// V0.0.27：品牌图分两种用法 ——
//   logo.png    原图（256×256 方图，四周大片透明边，水印/图标用）
//   wordmark.png 裁掉透明边的字标（约 251×94），侧栏与启动页用
// 用户原话「首页上方那个 logo 也太小了吧，啥也看不到」：方图塞进 26×26
// 的框里，字只有 26×9 像素，当然看不清。字标按宽高比显示才认得出。
const logo = '/brand/wordmark.png'
const logoFallback = '/brand/logo.png'
const ready = ref(false)      // 会话检查完成
const needLogin = ref(false)
const password = ref('')
const loginErr = ref('')
// V0.0.42：扫码登录整块撤掉（用户定：「扫码和打码都不要了」）。
// 后端那四个扫码接口保留（老客户端/脚本还在用），只是网页登录页不再给
// 入口 —— 它原来要「桌面窗口弹框同意」，壳里已经没有那个窗口，
// 扫到最后只会卡在「尚未获得授权」。
const summary = ref({ tasks: 0, images: 0, pending: 0, quarantine: 0,
                      running: 0, queued: 0, error: 0 })
// 2026-10-28（对标 qBittorrent「视图 → 显示侧栏」）：侧栏可收起，选择记在
// localStorage —— 笔记本小屏用户收起来一次就别再自己弹回来。
const sideHide = ref(false)
// 主区容器（滚动容器）：底栏的「回到顶部」要滚的是它，不是 window
const mainEl = ref(null)
// 底栏展示的地址：用真实访问地址，不是写死的 127.0.0.1（手机连过来时要看着对）
const where = location.origin.replace(/^https?:\/\//, '')

// 2026-10-28（对标 qBittorrent 状态栏）：实时速率单独轮询 /api/stats。
// 不塞进 summary —— summary 跟着每个事件刷新（可能一秒好几次），
// 而速率只需要 5 秒一格，混在一起纯属浪费。
const st = ref({ rate_bps: 0, per_min: [], today: 0, per_min_avg: 0, last_min: 0 })
let statsTimer = null

// V0.0.19：新手 / 老鸟一键切换，全局生效（桌面端共用同一配置项）。
function toggleUiMode() {
  setUiMode(uiMode.value === 'newbie' ? 'pro' : 'newbie')
}

function fmtRate(bps) {
  const n = Number(bps) || 0
  if (n <= 0) return '0'
  if (n < 1024) return `${Math.round(n)} B/s`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB/s`
  return `${(n / 1024 / 1024).toFixed(2)} MB/s`
}

/** 最近 10 分钟每分钟新增张数 → 柱高(0..14px)。零值给 0，样式里画成一条浅线。 */
const spark = computed(() => {
  const arr = (st.value.per_min || []).slice(-10)
  if (!arr.length) return []
  const max = Math.max(1, ...arr)
  // 有数就至少 1.5px，否则"本分钟写入 1 张"和"没有"看起来一模一样
  return arr.map((n) => (n > 0 ? Math.max(1.5, Math.round((n / max) * 14)) : 0))
})
const sparkMax = computed(() => Math.max(1, ...(st.value.per_min || [0])))

async function refreshStats() {
  // 页面在后台就别刷了：手机/笔记本挂着网页时不该白耗电
  if (document.hidden) return
  try { st.value = await api.stats() } catch (e) { /* 静默：底栏坏了不影响主流程 */ }
}
let closeEvents = null

// 2026-10-28：图标统一走 Lucide（内联 SVG，见 icons.js），不再用 ▶ ▦ ✓ 这类字符充数。
// 隔离区从图库的页签里拆出来，成为左边独立一项（用户要的）。
// V0.0.9：学习看板升级成首页（打开第一眼=学到什么、正按什么找），挪到第一项。
const navs = [
  // sub: 有没有子菜单（目前只有「任务」有 —— 状态筛选挂在它下面）
  { path: '/learning', label: '首页', icon: 'graduation-cap' },
  { path: '/tasks', label: '任务', icon: 'list-checks', sub: true },
  { path: '/gallery', label: '图库', icon: 'images' },
  { path: '/quarantine', label: '隔离区', icon: 'shield-alert' },
  { path: '/review', label: '审核', icon: 'clipboard-check' },
  { path: '/adapters', label: '适配器', icon: 'plug' },
  { path: '/models', label: 'AI 模型', icon: 'brain' },
  { path: '/urls', label: '常用网址', icon: 'link' },
  { path: '/subs', label: '站点订阅', icon: 'rss' },
  { path: '/proxies', label: '代理管理', icon: 'globe' },
  { path: '/logs', label: '日志', icon: 'scroll-text' },
  { path: '/report', label: '报表', icon: 'pie-chart' },
  { path: '/export', label: '导入与导出', icon: 'download' },
  { path: '/maintenance', label: '维护', icon: 'wrench' },
  { path: '/settings', label: '设置', icon: 'settings' },
]
// 2026-10-28（对标 qBittorrent 侧栏每项后面的计数）：图库/隔离区/待审/任务
// 直接显示条数。全部来自 /api/summary（已按各视图同一口径算好，别在这另算）。
const NAV_COUNT = {
  '/tasks': () => summary.value.tasks,
  '/gallery': () => summary.value.gallery != null ? summary.value.gallery : summary.value.images,
  '/quarantine': () => summary.value.quarantine,
  '/review': () => summary.value.pending,
}
const navCount = (path) => {
  const fn = NAV_COUNT[path]
  const n = fn ? fn() : 0
  return (n && Number(n) > 0) ? Number(n) : 0
}

// 2026-10-29 用户要求（第 4 条）：任务页那排「全部/运行中/…」挪进左侧导航，
// 挂在「任务」下面，点任务这个条目才展开。理由：那排胶囊占着任务列表顶上
// 一整行，任务一多就挤；而左侧本来就是导航，状态筛选本质上就是「任务的子视图」。
const TASK_VIEWS = [
  { k: '', label: '全部', icon: 'list-checks' },
  { k: 'running', label: '运行中', icon: 'play' },
  { k: 'queued', label: '排队中', icon: 'clock' },
  { k: 'error', label: '失败', icon: 'triangle-alert' },
  { k: 'idle', label: '空闲', icon: 'inbox' },
  { k: 'stopped', label: '已停止', icon: 'circle-stop' },
  { k: 'done', label: '完成', icon: 'check-check' },
]
// 展开状态记在 localStorage：默认展开（任务页是主战场），收起后别再自己弹开
const taskOpen = ref(true)
const taskCount = (k) => {
  if (!k) return Number(summary.value.tasks || 0)
  // 状态计数由 /api/summary 提供（running/queued/error 三个是现成的；
  // 其余状态没有单独字段，就不显示数字，别在这瞎猜一个）
  const m = { running: summary.value.running, queued: summary.value.queued,
              error: summary.value.error }
  return Number(m[k] || 0)
}
const taskView = computed(() => String(route.query.status || ''))
// 任务页本身才认这条子导航；别处的 status 参数与它无关
const onTasksPage = computed(() => route.path === '/tasks' || route.path === '/tasks/')
function goTask(k) {
  router.push({ path: '/tasks', query: k ? { status: k } : {} })
}
function toggleTaskOpen() {
  taskOpen.value = !taskOpen.value
  try { localStorage.setItem('mrm.taskopen', taskOpen.value ? '1' : '0') } catch (e) { /* 隐私模式 */ }
}

function openShortcuts() {
  window.dispatchEvent(new Event('mrm-shortcuts'))
}

// V0.0.17：全局键位按后端下发的覆盖真正生效；F1 固定打开说明（对齐桌面 F1）。
// 录键态与输入框内不分发，避免误触发页面跳转。
function onKey(e) {
  // 录键态优先：此刻任何按键都归快捷键弹窗采集，不分发页面动作。
  if (recordingHotkey.value) return
  if (e.key === 'F1') {
    e.preventDefault()
    openShortcuts()
    return
  }
  const act = matchWebAction(e)
  if (!act) return
  const map = {
    'web.shortcuts': () => openShortcuts(),
    'web.side': () => toggleSide(),
    'web.tasks': () => router.push('/tasks'),
    'web.gallery': () => router.push('/gallery'),
    'web.top': () => backToTop(),
  }
  const fn = map[act]
  if (fn) { e.preventDefault(); fn() }
}

async function boot() {
  try {
    await api.session()
    needLogin.value = false
    ready.value = true
    loadTheme()
    refreshSummary()
    // 动态图标开关存在后端配置里（多设备一致）。这里单独拉一次设置：
    // 失败不能让整个界面进不去，所以吞掉异常 —— 大不了先用默认值（开）。
    api.settings().then((d) => {
      if (d && 'ui_animated_icons' in d) setAnimatedIcons(d.ui_animated_icons)
    }).catch(() => { /* 忽略 */ })
    // V0.0.17：网页键位覆盖从后端配置载入，全局 keydown 立即按新键位生效。
    api.webHotkeys().then((d) => {
      if (d && d.ok && Array.isArray(d.actions)) {
        const m = {}
        for (const a of d.actions) m[a.id] = a.current || a.default
        setWebHotkeys(m)
      }
    }).catch(() => { /* 忽略：取不到就保持默认键位 */ })
    closeEvents = connectEvents(() => refreshSummary())
    try { sideHide.value = localStorage.getItem('mrm.side') === '1' } catch (e) { /* 忽略 */ }
    // 任务子菜单的展开状态（同上：记不住的话每次刷新都回到展开，等于没做）
    try {
      const t = localStorage.getItem('mrm.taskopen')
      if (t !== null) taskOpen.value = t === '1'
    } catch (e) { /* 忽略 */ }
    refreshStats()
    statsTimer = setInterval(refreshStats, 5000)
    // V0.0.18：通知未读数 60 秒轮询一次
    refreshNotif()
    notifTimer = setInterval(refreshNotif, 60000)
    // 回到顶部的滚动监听改在模板上绑（@scroll）。这里原来是
    // mainEl.value.addEventListener(...)，但 ready=true 只是改了数据，
    // 此刻 <main> 还没渲染（Vue 的 DOM 更新在下一个 tick），mainEl 为 null，
    // 监听器一次都没挂上 —— 于是按钮永远不出现。
  } catch (e) {
    needLogin.value = true
    ready.value = true
  }
}

// 主区滚动监听：超过一屏才显示「回到顶部」，否则一进页面就挂个按钮很碍眼
const showTop = ref(false)
function onMainScroll() {
  const el = mainEl.value
  // scroll 事件不冒泡，但**捕获阶段**能收到任意后代的滚动 —— 所以除了
  // <main> 自己的 @scroll，还在 window 上挂一个 capture 监听兜底。
  // 好处是：万一某个页面把滚动容器换成了别的元素（或整页滚 document），
  // 按钮依然会出来，不会再出现"按钮写了但永远不显示"。
  const top = el ? el.scrollTop : (document.scrollingElement?.scrollTop || 0)
  const max = el ? el.scrollHeight - el.clientHeight : 0
  showTop.value = top > 240 && max > 300
}
function backToTop() {
  const el = mainEl.value
  if (!el) { window.scrollTo({ top: 0, behavior: 'smooth' }); return }
  el.scrollTo({ top: 0, behavior: 'smooth' })
}
// 换页面回到顶部并在下一帧重新判定按钮（否则从长页面跳到短页面，
// 残留的 showTop=true 会留一个点了没反应的按钮）
watch(() => route.fullPath, () => {
  const el = mainEl.value
  if (el) el.scrollTop = 0
  requestAnimationFrame(() => onMainScroll())
})

function toggleSide() {
  sideHide.value = !sideHide.value
  try { localStorage.setItem('mrm.side', sideHide.value ? '1' : '0') } catch (e) { /* 隐私模式 */ }
}

// V0.0.39：状态栏「完全退出」。两步：先进入待确认（3 秒），再点才发请求。
// 后端只在本机壳窗口里接得住（浏览器访问会返回中文说明）。
const quitArmed = ref(false)
const quitting = ref(false)
const quitMsg = ref('')
let quitTimer = 0
async function askQuit() {
  if (!quitArmed.value) {
    quitArmed.value = true
    quitMsg.value = ''
    clearTimeout(quitTimer)
    quitTimer = setTimeout(() => { quitArmed.value = false }, 3000)
    return
  }
  clearTimeout(quitTimer)
  quitArmed.value = false
  try {
    const r = await api.appQuit()
    if (r && r.ok === false) {
      quitMsg.value = r.error || '退出失败'
      setTimeout(() => { quitMsg.value = '' }, 5000)
      return
    }
    quitting.value = true
  } catch (e) {
    quitMsg.value = '退出失败：' + (e && e.message ? e.message : e)
    setTimeout(() => { quitMsg.value = '' }, 5000)
  }
}

async function doLogin() {
  loginErr.value = ''
  try {
    await api.login(password.value || '')
    needLogin.value = false
    boot()
  } catch (e) {
    loginErr.value = e.message || '登录失败'
  }
}

// 2026-10-27：主题（与桌面端 ui_preset 同一份配置）
// 2026-10-29 用户要求：网页端**删掉「暗夜」**（与「深色」重复，两个深色皮肤
// 让选择变复杂），并把「qBittorrent」改名叫「深色」—— 用户认的是深浅，不是出处。
const THEMES = [
  { key: 'classic', label: '原始' },
  { key: 'violet_look', label: '紫罗兰' },
  { key: 'modern', label: '现代' },
  { key: 'qbittorrent', label: '深色' },
]
const theme = ref('classic')

function applyTheme(key) {
  // 老用户缓存里可能还是 'night'（已删）→ 平滑迁到「深色」，
  // 别让人一觉醒来从深色变回白色（那看起来就像设置没保存住）。
  const LEGACY = { night: 'qbittorrent' }
  const k0 = LEGACY[key] || key
  const k = THEMES.some((x) => x.key === k0) ? k0 : 'classic'
  theme.value = k
  webTheme.value = k
  if (k === 'classic') document.documentElement.removeAttribute('data-theme')
  else document.documentElement.setAttribute('data-theme', k)
}

// V0.0.17：自定义皮肤样式由服务端 CSS 文件注入，选择本身只存本机浏览器。
async function applySkin(key) {
  let el = document.getElementById('mrm-custom-skin')
  if (!key || !key.startsWith('custom:')) {
    if (el) el.remove()
    return
  }
  try {
    const r = await api.skinCss(key)
    if (!r.ok) return
    if (!el) {
      el = document.createElement('style')
      el.id = 'mrm-custom-skin'
      document.head.appendChild(el)
    }
    el.textContent = r.css || ''
  } catch { /* 忽略：样式读不到就保持现状 */ }
}

function onSkin(e) {
  applySkin(String((e && e.detail) || ''))
}
// 品牌图加载失败（老版本后端没有字标路由）→ 退回原图，不留碎图。
let logoFellBack = false
function onLogoError(e) {
  if (logoFellBack) {
    try { e.target.style.display = 'none' } catch { /* 忽略 */ }
    return
  }
  logoFellBack = true
  try { e.target.src = logoFallback } catch { /* 忽略 */ }
}

// V0.0.27：自定义皮肤也进左下角那个下拉 —— 用户反馈「设置里两个外观选择，
// 加上左下角一共三个，这对吗？」。现在全软件只有**这一个**换皮肤入口，
// 内建与自定义皮肤在同一个列表里，设置页只管上传/恢复文件。
const customSkins = ref([])
async function loadSkins() {
  try {
    const r = await api.skins()
    if (r && r.ok) customSkins.value = (r.items || []).filter(
      (s) => s && String(s.key || '').startsWith('custom:'))
  } catch { /* 读不到自定义皮肤不影响内建皮肤 */ }
}
// V0.0.23：设置页「自定义皮肤管理」里点内建皮肤以前没反应 —— 那边只发
// mrm-skin（只处理 custom:xxx 的自定义 CSS），内建皮肤走不到 applyTheme。
// 现在内建皮肤发 mrm-theme，这里走与左下角选择器同一条链路。
function onThemePick(e) {
  const k = String((e && e.detail) || '')
  if (!k) return
  changeTheme(k)
}

async function loadTheme() {
  // 先用本地缓存（避免闪一下），再以服务端设置为准。
  // 自定义皮肤的选择仅在本机生效，服务端 web_theme 不覆盖它。
  const cached = readWebTheme()
  const custom = cached.startsWith('custom:')
  if (!custom && cached) applyTheme(cached)
  if (custom) {
    // 自定义皮肤：下拉框也要显示成它，否则用户看到的是「原始」但界面是自定义
    theme.value = cached
    webTheme.value = cached
    applySkin(cached)
  }
  try {
    const s = await api.settings()
    if (s && s.web_theme && !custom) applyTheme(s.web_theme)
  } catch { /* 忽略：拿不到就保持缓存 */ }
}

function changeTheme(k) {
  const key = String(k || '')
  if (key.startsWith('custom:')) {
    // 自定义皮肤：只切本机选择 + 注入服务端那份 CSS，不写服务端 web_theme
    theme.value = key
    webTheme.value = key
    document.documentElement.removeAttribute('data-theme')
    setWebTheme(key)
    applySkin(key)
    return
  }
  applyTheme(key)
  setWebTheme(key)
  applySkin('')
}

async function refreshSummary() {
  try { summary.value = await api.summary() } catch { /* 忽略：页面会自己拉 */ }
}

// ── V0.0.18 · 通知中心（顶栏铃铛）────────────────────────────────────
// 60 秒拉一次未读数；打开面板时再拉完整列表。任何失败都静默——
// 顶栏多一个入口，坏也不能拖累主界面。
const notif = ref({ items: [], unread: 0 })
const notifOpen = ref(false)
const notifBusy = ref(false)
let notifTimer = null

async function refreshNotif() {
  try {
    notif.value = await api.notifications({ limit: 100, onlyUnread: false })
  } catch (e) { /* 静默 */ }
}
async function toggleNotif() {
  notifOpen.value = !notifOpen.value
  if (notifOpen.value) refreshNotif()
}
function closeNotif() {
  // focusout 时若新焦点仍在面板内（点了里面的按钮）则不收起
  requestAnimationFrame(() => {
    const el = document.activeElement
    if (el && el.closest && el.closest('.notif')) return
    notifOpen.value = false
  })
}
async function onNotifClick(n) {
  // 单击通知：标记已读，带 link 的跳转对应页面
  try { await api.notificationsRead([n.id]) } catch (e) { /* 静默 */ }
  refreshNotif()
  if (n.link) gotoLink(n.link)
}
async function markNotifRead(ids, all = false) {
  notifBusy.value = true
  try {
    await api.notificationsRead(ids, all)
    await refreshNotif()
  } catch (e) { /* 静默 */ } finally { notifBusy.value = false }
}
async function clearNotif() {
  if (!window.confirm('确定清空全部通知吗？此操作不可撤销。')) return
  notifBusy.value = true
  try {
    await api.notificationsClear()
    await refreshNotif()
    notifOpen.value = false
  } catch (e) { /* 静默 */ } finally { notifBusy.value = false }
}
function gotoLink(link) {
  notifOpen.value = false
  if (link) router.push(link)
}
function npIcIcon(n) {
  // 通知级别 → 图标（名字全部来自 icons.js 白名单）
  return n.level === 'error' ? 'triangle-alert' : (n.level === 'warn' ? 'shield-alert' : 'info')
}
function npIcClass(n) {
  return n.level === 'warn' ? 'warn' : (n.level === 'error' ? 'error' : 'info')
}

// ── V0.0.18 · 全局搜索（顶栏搜索框）──────────────────────────────────
// 输入停顿 400ms 或回车触发；结果分组下拉。图库语法沿用后端 parse_query 口径。
const search = ref('')
const searchRes = ref(null)
const searchOpen = ref(false)
const searchBusy = ref(false)
let searchTimer = null
async function runSearch() {
  const q = search.value.trim()
  if (!q) { searchRes.value = null; searchOpen.value = false; return }
  searchBusy.value = true
  try {
    searchRes.value = await api.searchAll(q)
    searchOpen.value = true
  } catch (e) { searchRes.value = null } finally { searchBusy.value = false }
}
function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(runSearch, 400)
}
function closeSearch() {
  searchOpen.value = false
}
function pickTask(id) {
  searchOpen.value = false
  router.push({ path: '/tasks', query: { focus: id } })
}
function pickImage(id) {
  searchOpen.value = false
  // 图库灯箱跨页定位无现成机制，这里只跳转（欠账：可后续在 ImageGrid 接路由参数）
  router.push('/gallery')
}
function pickLog() {
  searchOpen.value = false
  router.push('/logs')
}

// 会话过期 → 任何页面收到 401 都会广播，这里统一弹登录框
function onAuthNeeded() {
  needLogin.value = true
  loginErr.value = '会话已过期，请重新登录'
}

// V0.0.21：桌面窗口模式的浏览器行为接管收敛到 nativeShell.js（键位、
// 缩放、拖放、右键、文本选择统一一张决策表）。这里只负责把「软件动作」
// 落到具体界面：定位搜索框、刷新数据、全选当前列表、保存当前表单。
// 普通浏览器访问不安装这一层，浏览器原有编辑与查找行为不受影响。
const searchEl = ref(null)
function focusGlobalSearch() {
  const el = searchEl.value || document.querySelector('.gs-input')
  if (!el) return
  el.focus()
  try { el.select() } catch (_e) { /* 忽略 */ }
}
function refreshCurrentView() {
  refreshSummary()
  refreshNotif()
  // 广播给当前页，由各页自行重拉数据（不重载整页，任务与表单状态不丢）。
  try { window.dispatchEvent(new Event('mrm-refresh-view')) } catch (_e) { /* 忽略 */ }
}
function onShellAction(e) {
  const name = e && e.type
  // 当前视图先收到（先注册），可自行处理并取消全局兜底。
  if (e && e.defaultPrevented) return
  if (name === 'mrm-shell-focus-search') focusGlobalSearch()
  else if (name === 'mrm-shell-refresh-view') refreshCurrentView()
  // 全选 / 保存由当前视图订阅，无视图时静默，不产生浏览器行为。
}
const SHELL_EVENTS = [
  'mrm-shell-select-all', 'mrm-shell-focus-search',
  'mrm-shell-save-current', 'mrm-shell-refresh-view',
]

onMounted(() => {
  window.addEventListener('mrm-auth-needed', onAuthNeeded)
  window.addEventListener('keydown', onKey)
  // 让内核转发的键也能按用户自定义键位 / 录键态判定，口径与网页一致。
  window.__mrmMatchWebAction = matchWebAction
  window.__mrmRecordingHotkey = false
  uninstallNativeShell = installNativeShell()
  for (const name of SHELL_EVENTS) window.addEventListener(name, onShellAction)
  window.addEventListener('mrm-skin', onSkin)
  window.addEventListener('mrm-theme', onThemePick)
  // 设置页传了新皮肤文件 ⇒ 左下角下拉立刻能选到（不用刷新页面）
  window.addEventListener('mrm-skins-changed', loadSkins)
  // 捕获阶段收所有滚动（scroll 不冒泡，capture 才能收到 <main> 里的滚动）
  window.addEventListener('scroll', onMainScroll, { capture: true, passive: true })
  boot()
  loadUiMode()
  loadSkins()
})
// 录键态必须同步到内核转发的键也能读到的位置，否则按 F5 录键会被误拦。
watch(recordingHotkey, (v) => { window.__mrmRecordingHotkey = !!v }, { flush: 'sync' })
onUnmounted(() => {
  if (statsTimer) clearInterval(statsTimer)
  if (notifTimer) clearInterval(notifTimer)
  if (searchTimer) clearTimeout(searchTimer)
  window.removeEventListener('keydown', onKey)
  if (uninstallNativeShell) uninstallNativeShell()
  for (const name of SHELL_EVENTS) window.removeEventListener(name, onShellAction)
  window.removeEventListener('mrm-skin', onSkin)
  window.removeEventListener('mrm-theme', onThemePick)
  window.removeEventListener('mrm-skins-changed', loadSkins)
  window.removeEventListener('scroll', onMainScroll, { capture: true })
  if (closeEvents) closeEvents()
  window.removeEventListener('mrm-auth-needed', onAuthNeeded)
})
</script>

<template>
  <!-- 启动页（2026-10-28 用户要求）：文字换成 logo，配一个转圈动画。
       为什么不用文字：第一次打开时后端要读库/建表，"正在连接…"这行字
       在快机器上一闪而过（像闪屏），在慢机器上又干等；logo + 转圈
       两头都成立 —— 快的时候看得出"这是这个软件"，慢的时候看得出"还在动"。
       动画图标关掉时（或系统要求减少动效）自动退回静态圆环，不是空白。 -->
  <div v-if="!ready" class="boot">
    <img class="boot-logo" :src="logo" alt="Sweet 采集AI版"
         @error="onLogoError" />
    <Icon anim="spin" name="loader" :size="24" class="boot-spin" />
  </div>

  <!-- 登录门：密码可留空（后端免密时直接放行） -->
  <div v-else-if="needLogin" class="login">
    <form class="card login-card" @submit.prevent="doLogin">
      <h1>Sweet采集AI版</h1>
      <p class="muted">请输入软件「设置 → Web 访问」里设置的密码（没设密码时可直接进入）</p>
      <input v-model="password" type="password" placeholder="密码（可留空）" autocomplete="current-password" />
      <p v-if="loginErr" class="err">{{ loginErr }}</p>
      <button class="btn primary" type="submit"><Icon name="check" :size="15" />进入</button>
    </form>
  </div>

  <div v-else class="shell" :class="{ 'side-off': sideHide }">
    <aside class="side">
      <!-- 2026-10-29（第 13/15 条）：去掉了「Sweet」文字只留 logo 并放大；
           右边加一个侧栏收起按钮，位置照 WorkBuddy 左上角那个。 -->
      <div class="brand">
        <img :src="logo" alt="Sweet采集AI版" title="Sweet采集AI版"
             @error="onLogoError" />
        <button v-if="!sideHide" class="side-btn" :title="sideHide ? '展开侧栏' : '收起侧栏（Ctrl+B）'"
                @click="toggleSide">
          <Icon name="panel-left" :size="16" />
        </button>
      </div>
      <nav>
        <template v-for="n in navs" :key="n.path">
          <router-link
            v-if="!n.sub"
            :to="n.path" class="nav" :class="{ active: route.path === n.path }">
            <span class="ic"><Icon :name="n.icon" :size="16" /></span>
            <span class="nlabel">{{ n.label }}</span>
            <span v-if="navCount(n.path)" class="nbadge">{{ navCount(n.path) }}</span>
          </router-link>
          <!-- 「任务」是父级：点一下既进任务页，也展开/收起下面的状态子项 -->
          <div v-else class="navwrap">
            <router-link
              :to="n.path" class="nav" :class="{ active: route.path === n.path }"
              @click="taskOpen = true">
              <span class="ic"><Icon :name="n.icon" :size="16" /></span>
              <span class="nlabel">{{ n.label }}</span>
              <span v-if="navCount(n.path)" class="nbadge">{{ navCount(n.path) }}</span>
              <span class="ncar" role="button" tabindex="0"
                    :title="taskOpen ? '收起状态筛选' : '展开状态筛选'"
                    @click.prevent.stop="toggleTaskOpen"
                    @keydown.enter.prevent.stop="toggleTaskOpen">
                <Icon :name="'chevron-right'" :size="12"
                      :style="{ transform: taskOpen ? 'rotate(90deg)' : 'none' }" />
              </span>
            </router-link>
            <div v-if="taskOpen" class="subnav">
              <!-- V0.0.38：子项的选中态必须**同时**认页面。
                   原来只比 query.status，「全部」等于 status 为空 —— 站在
                   设置/图库任何一页时，任务子项里的「全部」也会跟着亮，
                   看起来像两个地方同时被选中（用户截图里的那条）。 -->
              <button v-for="v in TASK_VIEWS" :key="v.k || 'all'" class="subitem"
                      :class="{ on: onTasksPage && taskView === v.k }"
                      @click="goTask(v.k)">
                <Icon :name="v.icon" :size="12" />
                <span class="nlabel">{{ v.label }}</span>
                <span v-if="taskCount(v.k)" class="nbadge">{{ taskCount(v.k) }}</span>
              </button>
              <!-- 2026-10-29（第 4 条）：这里原来还有一个「清空筛选」，点它执行的是
                   goTask('')，与上面的「全部」完全同一件事 —— 两个入口一个动作，
                   用户要问「这不是重复的吗」。删掉这个，只留「全部」。 -->
            </div>
          </div>
        </template>
      </nav>
      <div class="stats">
        <div><b>{{ summary.tasks }}</b><span>任务</span></div>
        <div><b>{{ summary.images }}</b><span>图库</span></div>
        <div><b>{{ summary.pending }}</b><span>待审</span></div>
      </div>
      <!-- 2026-10-29（用户要求）：皮肤选择从右上角挪到侧栏底部 ——
           紧接在「任务/图库/待审」这组数字下面，再往下就是页面状态栏。
           右上角那块留给页面标题，视线更干净。 -->
      <div class="theme-box">
        <select class="theme" :value="theme" title="界面皮肤（全软件只有这一处）"
                @change="changeTheme($event.target.value)">
          <option v-for="t in THEMES" :key="t.key" :value="t.key">{{ t.label }}</option>
          <!-- V0.0.27：上传的自定义皮肤也在这个下拉里，不再单独做一套选择器 -->
          <optgroup v-if="customSkins.length" label="自定义皮肤">
            <option v-for="s in customSkins" :key="s.key" :value="s.key">{{ s.label }}</option>
          </optgroup>
        </select>
        <!-- V0.0.24：侧栏高度是硬约束，这里收成图标按钮（悬停有说明） -->
        <button class="side-tool" title="快捷键说明（? 或 F1）" @click="openShortcuts">
          <Icon name="key-round" :size="14" />
        </button>
      </div>
    </aside>
    <main ref="mainEl" class="main" @scroll.passive="onMainScroll">
      <header class="top">
        <h2>{{ route.meta.title }}</h2>
        <div class="top-right">
          <!-- V0.0.19：界面模式切换（新手 / 老鸟，与桌面端共用配置） -->
          <button class="mode-btn" type="button" @click="toggleUiMode"
                  :title="uiMode === 'newbie' ? '当前为新手模式，单击切换为老鸟模式' : '当前为老鸟模式，单击切换为新手模式'">
            <Icon name="panel-left" :size="14" />
            {{ uiMode === 'newbie' ? '新手模式' : '老鸟模式' }}
          </button>
          <!-- V0.0.18：全局搜索框（输入停顿 400ms 或回车触发，结果分组下拉） -->
          <div class="gsearch" @focusout="closeSearch">
            <span class="gs-ic"><Icon name="search" :size="14" /></span>
            <input ref="searchEl" v-model="search" type="text" class="gs-input"
                   placeholder="搜索任务 / 图库 / 日志" autocomplete="off"
                   aria-label="全局搜索"
                   @input="onSearchInput" @keyup.enter="runSearch" />
            <span v-if="searchBusy" class="gs-spin"><Icon anim="spin" name="loader-circle" :size="14" /></span>
            <Transition name="pop">
            <div v-if="searchOpen && searchRes" class="gs-panel">
              <div v-if="searchRes.tasks && searchRes.tasks.length" class="gs-grp">
                <div class="gs-title">任务</div>
                <button v-for="t in searchRes.tasks" :key="'t' + t.id" class="gs-item"
                        @mousedown.prevent="pickTask(t.id)">
                  <Icon name="list-checks" :size="13" />
                  <span class="gs-label">{{ t.name || ('任务 #' + t.id) }}</span>
                  <span class="gs-meta">{{ t.status }}</span>
                </button>
              </div>
              <div v-if="searchRes.images && searchRes.images.length" class="gs-grp">
                <div class="gs-title">图库</div>
                <button v-for="im in searchRes.images" :key="'i' + im.id" class="gs-item"
                        @mousedown.prevent="pickImage(im.id)">
                  <img class="gs-thumb" :src="im.thumb_url" alt="" loading="lazy" />
                  <span class="gs-label">{{ im.name }}</span>
                  <span class="gs-meta">{{ im.score }}</span>
                </button>
              </div>
              <div v-if="searchRes.logs && searchRes.logs.length" class="gs-grp">
                <div class="gs-title">日志</div>
                <button v-for="lg in searchRes.logs" :key="'l' + lg.id" class="gs-item"
                        @mousedown.prevent="pickLog">
                  <Icon name="scroll-text" :size="13" />
                  <span class="gs-label">{{ lg.action }}</span>
                  <span class="gs-meta">{{ lg.ts }}</span>
                </button>
              </div>
              <div v-if="!(searchRes.tasks && searchRes.tasks.length)
                        && !(searchRes.images && searchRes.images.length)
                        && !(searchRes.logs && searchRes.logs.length)" class="gs-empty">
                暂无匹配结果
              </div>
            </div>
            </Transition>
          </div>
          <!-- V0.0.18：通知中心铃铛（未读数角标，面板可标已读 / 全部已读 / 清空） -->
          <div class="notif" @focusout="closeNotif">
            <button class="bell-btn" :class="{ on: notifOpen }"
                    title="通知中心" aria-label="通知中心" @click="toggleNotif">
              <Icon :name="notif.unread ? 'bell' : 'bell-off'" :size="16" />
              <span v-if="notif.unread" class="bell-badge">{{ notif.unread > 99 ? '99+' : notif.unread }}</span>
            </button>
            <Transition name="pop">
            <div v-if="notifOpen" class="notif-panel">
              <div class="np-hd">
                <b>通知中心</b>
                <span class="muted small">未读 {{ notif.unread }}</span>
                <span class="np-acts">
                  <button class="btn sm ghost" :disabled="notifBusy || !notif.unread"
                          @click="markNotifRead(null, true)">全部已读</button>
                  <button class="btn sm ghost" :disabled="notifBusy || !notif.items.length"
                          @click="clearNotif">清空</button>
                </span>
              </div>
              <div v-if="!notif.items.length" class="np-empty">暂无通知</div>
              <ul v-else class="np-list">
                <li v-for="n in notif.items" :key="n.id" class="np-item" :class="{ unread: !n.read }">
                  <button class="np-main" @click="onNotifClick(n)">
                    <span class="np-ic" :class="npIcClass(n)">
                      <Icon :name="npIcIcon(n)" :size="14" /></span>
                    <span class="np-body">
                      <span class="np-title">{{ n.title }}</span>
                      <span v-if="n.body" class="np-text">{{ n.body }}</span>
                      <span class="np-ts">{{ n.ts }}</span>
                    </span>
                  </button>
                  <button v-if="!n.read" class="np-mark" title="标记已读" aria-label="标记已读"
                          @click="markNotifRead([n.id])"><Icon name="check" :size="13" /></button>
                </li>
              </ul>
            </div>
            </Transition>
          </div>
        </div>
      </header>
      <router-view />
    </main>

    <!-- 2026-10-29（第 2 条）：侧栏收起后，在**原来侧栏所在的那条左边**留一个
         竖排把手。原来的做法是只在底栏最右侧塞一个灰色小箭头 —— 收起后
         整个左边变成空白，用户根本不知道回哪儿点，体感就是"缩回去就出不来了"。
         把手贴着左边缘、颜色用主色，一眼能看见；Ctrl+B 依然可用。 -->
    <button v-if="sideHide" class="side-rail" title="展开侧栏（Ctrl+B）" @click="toggleSide">
      <Icon name="panel-left" :size="16" />
      <span>展开</span>
    </button>

    <!-- 2026-10-29（第 1 条）：右下角「回到顶部」，滚过一屏才出现。 -->
    <button v-if="showTop" class="totop" title="回到顶部" aria-label="回到顶部" @click="backToTop">
      <Icon name="arrow-up" :size="18" />
    </button>

    <!-- 2026-10-28：底栏对标 qBittorrent 的状态栏 —— 一眼看到在跑几个、
         排队几个、失败几个，不用点进页面。
         2026-10-29（第 12 条）：访问地址挪到**左边**，任务/图/待审/隔离
         这组「今日进度」挪到**右下角**（用户按 qB 的习惯认右边那处）。 -->
    <footer class="statusbar">
      <div class="sb-left">
        <span class="mono sb-host" :title="'当前访问地址'">{{ where }}</span>
        <span class="sb-sep"></span>
        <span class="sb-item" title="正在运行的任务">
          <Icon :anim="(summary.running || 0) > 0 ? 'pulse' : ''" name="play" :size="12" />{{ summary.running || 0 }} 运行</span>
        <span class="sb-item" title="排队等着的任务"><Icon name="clock" :size="12" />{{ summary.queued || 0 }} 排队</span>
        <span class="sb-item" :class="{ bad: summary.error }" title="失败的任务">
          <Icon name="triangle-alert" :size="12" />{{ summary.error || 0 }} 失败</span>
        <span class="sb-sep"></span>
        <!-- 速率 + 最近 10 分钟小柱图（qBittorrent 右下角那处清爽版） -->
        <span class="sb-item rate" :class="{ live: st.rate_bps > 0 }"
              :title="`最近 1 分钟写入 ${fmtRate(st.rate_bps)}；最近 10 分钟平均 ${st.per_min_avg || 0} 张/分`">
          <!-- 有流量时换成动画柱，静止时用普通箭头（0 速率还动会骗人） -->
          <Icon v-if="st.rate_bps > 0" anim="bars" :size="12" />
          <Icon v-else name="arrow-down" :size="12" />{{ fmtRate(st.rate_bps) }}
        </span>
        <span v-if="spark.length" class="spark"
              :title="`最近 10 分钟每分钟新增（峰值 ${sparkMax} 张/分）`">
          <i v-for="(h, k) in spark" :key="k" :style="{ height: h + 'px' }" :class="{ zero: !h }"></i>
        </span>
        <span class="sb-item" :title="`今天新增 ${st.today || 0} 张`">
          <Icon name="image" :size="12" />今日 {{ st.today || 0 }}
        </span>
        <!-- 2026-10-28（对标 qB 状态栏右侧剩余空间）：采集器最常见的翻车是
             「下到一半盘满」。测的是**存图那个盘**，低于预警值标红。 -->
        <span v-if="st.disk_text" class="sb-item" :class="{ bad: st.disk_low }"
              :title="`存图盘剩余 ${st.disk_text}（${st.disk_path}）`
                + (st.disk_low ? `；已低于预警值 ${st.disk_warn_mb} MB，新任务会自动暂停` : '')">
          <Icon name="hard-drive" :size="12" />剩余 {{ st.disk_text }}
        </span>
      </div>
      <div class="sb-right">
        <span class="sb-item" title="任务总数">{{ summary.tasks }} 个任务</span>
        <span class="sb-item" title="图库总张数">{{ summary.images }} 张图</span>
        <span class="sb-item" title="待审核">{{ summary.pending }} 待审</span>
        <span class="sb-item" title="隔离区">{{ summary.quarantine }} 隔离</span>
        <span v-if="quitMsg" class="sb-item bad">{{ quitMsg }}</span>
        <!-- V0.0.45：用户原话「右下角的退出太不明显了」。原来是纯图标、
             悬停才出提示，放在状态栏最右边跟一排灰字完全融在一起。
             现在是一颗常显文字的红色胶囊：平时淡红底 + 「退出软件」，
             点一下变实心红 + 「确认退出」，3 秒内再点才真退（状态栏最容易误点，
             一步直退迟早会关掉正在跑的采集）。窗口右上角的 ✕ 已改回收进托盘，
             所以这里是「彻底退出」唯一常显的入口。 -->
        <button class="quit-btn" :class="{ armed: quitArmed }"
                :title="quitArmed
                  ? '3 秒内再点一次：彻底退出软件'
                  : '彻底退出软件（后台采集一并停止）；只想收进托盘请点窗口的关闭按钮'"
                @click="askQuit">
          <Icon name="log-out" :size="13" />
          <span>{{ quitArmed ? '确认退出' : '退出软件' }}</span>
        </button>
      </div>
    </footer>
    <div v-if="quitting" class="quit-mask">
      <div class="quit-card">
        <Icon name="log-out" :size="18" />
        <span>正在退出…</span>
      </div>
    </div>
  </div>

  <!-- V0.0.17：全局弹窗（登录后才挂载，避免登录页先弹出引导） -->
  <ShortcutsDialog v-if="ready && !needLogin" />
  <Onboarding v-if="ready && !needLogin" />
</template>

<style scoped>
.boot {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 18px; height: 100%; color: var(--muted);
}
.boot-logo {
  /* V0.0.27：启动页改用裁好边的字标，按原始宽高比显示 —— 以前 104×104 的方框
     套一张带大片透明边的方图，字只有 104×35，糊成一团。 */
  width: 208px; height: auto; max-width: 62vw;
  filter: drop-shadow(0 6px 18px rgba(16, 24, 40, .10));
}
.boot-spin { color: var(--brand); opacity: .9; }
.login { display: grid; place-items: center; height: 100%; }
.login-card { width: min(380px, 92vw); padding: 28px 26px; display: grid; gap: 12px; }
.login-card h1 { margin: 0; font-size: 20px; }
.login-card .btn { width: 100%; }
.err { color: var(--err); margin: 0; font-size: 13px; }
/* V0.0.42：扫码登录撤掉后，二维码那几条样式也一并删了（已无引用）。 */

.shell {
  display: grid; grid-template-columns: 208px 1fr; grid-template-rows: 1fr auto;
  height: 100%;
}
/* 收起侧栏：列宽归零 + 不画那个 div（display:none 才不会留边框） */
.shell.side-off { grid-template-columns: 0 1fr; }
.shell.side-off .side { display: none; }
.shell.side-off .main { grid-column: 1 / -1; }

.statusbar {
  grid-column: 1 / -1; display: flex; align-items: center; justify-content: space-between;
  gap: 10px; padding: 4px 12px; font-size: 12px; color: var(--muted);
  background: #fbfbfe; border-top: 1px solid var(--line); min-height: 30px;
}
.sb-left, .sb-right { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.sb-item { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }
.sb-item.bad { color: var(--err); }
.sb-item.rate { color: var(--muted); font-variant-numeric: tabular-nums; min-width: 66px; }
.sb-item.rate.live { color: #2e9e5b; font-weight: 600; }
/* 迷你柱图：10 根，底对齐；零值只留一条浅线（让人知道"这分钟确实是 0"） */
.spark {
  display: inline-flex; align-items: flex-end; gap: 2px; height: 14px;
  padding: 0 2px; border-left: 1px solid var(--line); margin-left: 2px;
}
.spark i { width: 3px; background: var(--brand); border-radius: 1px; opacity: .75; }
.spark i.zero { height: 1px !important; background: var(--line); opacity: 1; }
.sb-sep { width: 1px; height: 12px; background: var(--line); display: inline-block; }
.sb-host { opacity: .75; }
/* 第 1 条：回到顶部。固定定位在右下角、底栏之上
   （底栏 min-height 30px，46px 刚好压在上面不留缝；手机上底栏会换行变高，
   用 max() 兜一层，最矮也给 46px） */
.totop {
  position: fixed; right: 18px; bottom: max(46px, calc(38px + env(safe-area-inset-bottom)));
  z-index: 45;
  width: 38px; height: 38px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  background: #fff; color: var(--brand); border: 1px solid var(--line);
  box-shadow: 0 6px 18px rgba(16, 24, 40, .16); cursor: pointer;
  transition: background .12s, color .12s, transform .12s, box-shadow .12s;
}
.totop:hover { background: var(--brand); color: #fff; border-color: var(--brand); transform: translateY(-2px); }
.totop:active { transform: translateY(0); }
/* 第 2 条：侧栏收起后的「展开」把手，贴在左边缘竖排 */
.side-rail {
  position: fixed; left: 0; top: 86px; z-index: 45;
  display: inline-flex; flex-direction: column; align-items: center; gap: 3px;
  padding: 10px 5px; border: 1px solid var(--brand); border-left: 0;
  border-radius: 0 10px 10px 0; cursor: pointer;
  background: var(--brand); color: #fff; font-size: 11px; line-height: 1;
  box-shadow: 2px 4px 14px rgba(16, 24, 40, .18);
}
.side-rail span { writing-mode: vertical-rl; letter-spacing: 2px; }
.side-rail:hover { filter: brightness(1.06); }
.sb-btn {
  border: 1px solid transparent; background: transparent; color: var(--muted);
  padding: 2px 6px; border-radius: 6px; display: inline-flex; align-items: center; gap: 4px;
}
.sb-btn:hover { border-color: var(--line); background: #fff; color: var(--brand); }
/* 收起状态：底栏按钮也点亮一下（侧栏没了，这里得有第二个入口可点） */
.sb-btn.on { border-color: var(--brand); color: var(--brand); background: #efeaff; }
/* V0.0.39/V0.0.45：状态栏「退出软件」—— 常显文字的红色胶囊。
   以前只有一个灰图标，跟右边那排「N 个任务 / N 张图」融成一片，用户根本
   找不到；关闭按钮改回「收进托盘」之后，这里是彻底退出唯一常显的入口，
   必须一眼能看见。两步确认：点一下变实心红「确认退出」，3 秒内再点才真退。 */
.quit-btn {
  display: inline-flex; align-items: center; gap: 5px;
  height: 22px; padding: 0 10px; border-radius: 999px; flex: none;
  border: 1px solid rgba(217, 48, 37, .42);
  background: rgba(217, 48, 37, .10);
  color: #d93025; font-size: 12px; font-weight: 500; line-height: 1;
  cursor: pointer; white-space: nowrap;
  transition: background var(--dur-fast) var(--ease),
              border-color var(--dur-fast) var(--ease),
              color var(--dur-fast) var(--ease),
              box-shadow var(--dur-fast) var(--ease);
}
.quit-btn:hover {
  background: rgba(217, 48, 37, .18);
  border-color: rgba(217, 48, 37, .62);
}
.quit-btn.armed, .quit-btn.armed:hover {
  background: #e81123; border-color: #e81123; color: #fff;
  box-shadow: 0 0 0 3px rgba(232, 17, 35, .20);
  animation: mrm-armed-pulse 1.1s ease-in-out infinite;
}
@keyframes mrm-armed-pulse {
  0%, 100% { box-shadow: 0 0 0 3px rgba(232, 17, 35, .20) }
  50%      { box-shadow: 0 0 0 5px rgba(232, 17, 35, .06) }
}
/* 深色皮肤：暗红底上的深红字（#d93025）几乎看不见，换成亮一档的红。 */
html[data-theme="night"] .quit-btn,
html[data-theme="qbittorrent"] .quit-btn {
  color: #ff8177; border-color: rgba(255, 129, 119, .45);
  background: rgba(255, 129, 119, .12);
}
html[data-theme="night"] .quit-btn:hover,
html[data-theme="qbittorrent"] .quit-btn:hover {
  background: rgba(255, 129, 119, .22); border-color: rgba(255, 129, 119, .7);
}
html[data-theme="night"] .quit-btn.armed,
html[data-theme="qbittorrent"] .quit-btn.armed {
  background: #e81123; border-color: #e81123; color: #fff;
}
/* 退出遮罩：请求已发出，页面马上会断 —— 别让用户以为卡住了 */
.quit-mask { position: fixed; inset: 0; z-index: 99; display: flex;
  align-items: center; justify-content: center; background: rgba(24, 26, 36, .42); }
.quit-card { display: flex; align-items: center; gap: 10px; background: #fff;
  color: var(--text); border-radius: 12px; padding: 16px 22px; font-size: 14px;
  box-shadow: 0 12px 32px rgba(16, 24, 40, .18); }
/* 2026-10-27：手机上 208px 侧栏把内容压得只剩一条 —— 窄屏改成顶栏抽屉式 */
@media (max-width: 760px) {
  .shell { grid-template-columns: 1fr; grid-template-rows: auto 1fr auto; }
  .shell.side-off .main { grid-column: auto; }
  .sb-host { display: none; }
  .side { flex-direction: row; align-items: center; padding: 8px 10px; gap: 8px;
          border-right: 0; border-bottom: 1px solid var(--line); overflow-x: auto; }
  .side nav { display: flex; gap: 4px; }
  .nav { padding: 7px 10px; white-space: nowrap; }
  .stats { margin-top: 0; padding: 0 8px; border-top: 0; gap: 10px; }
  .brand { display: none; }
  .main > :deep(.page) { padding: 0 10px 14px; }
  .top { padding: 10px 12px 8px; }
  /* 手机上侧栏在顶部，收起后把手往下挪一点，别压住页面标题 */
  .side-rail { top: 56px; }
  .totop { right: 12px; }
}
.side {
  background: #fff; border-right: 1px solid var(--line);
  display: flex; flex-direction: column; padding: 12px 10px; gap: 8px;
  min-height: 0; overflow: auto;
}
.brand { display: flex; align-items: center; gap: 7px; height: 36px; padding: 0 6px; }
/* V0.0.27：品牌区改用「裁掉透明边的字标」，按 92×~34 显示。
   以前是 26×26 的方框套带大片透明边的方图，字只剩 26×9 像素 ——
   用户原话「首页上方那个 logo 也太小了吧，啥也看不到」。 */
.brand img { width: 92px; height: auto; max-height: 34px; flex: none; }
.bname { flex: 1; font-size: 11.5px; font-weight: 500; color: var(--muted); letter-spacing: .3px; }
/* 第 15 条：侧栏收起按钮，贴在 logo 右边（WorkBuddy 左上角那个位置） */
.side-btn {
  margin-left: auto; display: inline-flex; align-items: center; justify-content: center;
  width: 26px; height: 26px; border: 1px solid transparent; border-radius: 7px;
  background: transparent; color: var(--muted); cursor: pointer;
}
.side-btn:hover { background: #f5f3ff; color: var(--brand); border-color: var(--line); }
nav { display: grid; gap: 2px; }
.navwrap { display: grid; gap: 2px; }
.nav {
  display: flex; align-items: center; gap: 7px; height: 25px; padding: 0 9px;
  border-radius: 7px; color: var(--text); font-size: 12.5px;
  position: relative;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease),
              transform var(--dur-fast) var(--ease);
}
/* V0.0.33：当前所在页左边一条指示线，切换时从中间展开。
   伪元素是绝对定位，不占布局 —— 侧栏 753px 一屏全显示是硬约束，
   任何多占一像素的写法都不能用。 */
.nav::before {
  content: ""; position: absolute; left: 0; top: 4px; bottom: 4px; width: 3px;
  border-radius: 999px; background: var(--brand);
  transform: scaleY(0); transform-origin: center;
  transition: transform var(--dur) var(--ease);
  pointer-events: none;
}
.nav.active::before { transform: scaleY(1); }
.nav:hover { transform: translateX(2px); }
.nav.active:hover { transform: none; }
.nav:hover { background: #f5f3ff; }
.nav.active { background: #efeaff; color: var(--brand); font-weight: 600; }
.nav .ic { width: 15px; flex: none; display: inline-flex; align-items: center; justify-content: center; opacity: .75; }
.nlabel { flex: 1; }
/* 父项上的展开箭头：只有 14px，点它只切展开、不跳转（点整行才跳） */
.ncar {
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; padding: 0; border: 0; background: transparent;
  color: var(--muted); cursor: pointer; border-radius: 5px;
}
.ncar:hover { background: #fff; color: var(--brand); }
.ncar :deep(svg) { transition: transform .12s; }
/* 任务状态子项：比父项缩进一级，字号小一号，避免和一级导航抢视线 */
.subnav { display: grid; gap: 1px; margin: 1px 0 2px 10px; padding-left: 7px;
  border-left: 1px solid var(--line); }
.subitem {
  display: flex; align-items: center; gap: 6px; height: 20px; padding: 0 7px;
  border: 0; background: transparent; border-radius: 6px; cursor: pointer;
  color: var(--muted); font-size: 11.5px; text-align: left; width: 100%;
}
.subitem:hover { background: #f5f3ff; color: var(--brand); }
.subitem.on { background: #efeaff; color: var(--brand); font-weight: 600; }
.subitem :deep(svg) { opacity: .8; }
.nbadge {
  font-size: 10.5px; color: var(--muted); font-variant-numeric: tabular-nums;
  background: #f2f3f8; border-radius: 999px; padding: 0 5px; line-height: 15px;
}
.nav.active .nbadge { background: #fff; color: var(--brand); }
.nav.active .ic { opacity: 1; }
.switch {
  display: flex; align-items: center; gap: 5px;
  margin-top: auto; padding: 6px 10px; border-radius: 8px;
  color: var(--muted); font-size: 12px;
}
.switch:hover { background: #f5f3ff; color: var(--brand); }
.stats { display: flex; justify-content: center; align-items: baseline; gap: 5px;
  margin-top: auto; padding: 7px 0 1px; border-top: 1px solid var(--line);
  font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums;
}
.stats div { display: inline-flex; align-items: baseline; gap: 3px; }
.stats div + div::before { content: "·"; margin-right: 3px; color: var(--line); }
.stats b { font-size: 11px; font-weight: 500; color: var(--text); }
.stats span { font-size: 11px; }
/* 2026-10-29（用户要求）：皮肤选择挪到侧栏最下方（统计数字下面、页面底栏上面）。
   margin-top:auto 把它顶到侧栏底部，与上面的导航列表分开。 */
.theme-box {
  padding: 6px 0 0; display: flex; align-items: center; gap: 4px;
}
.theme-box .theme { flex: 1 1 0; width: auto; min-width: 0;
  height: 24px; padding: 0 4px; font-size: 11.5px; }
/* V0.0.17：侧栏快捷键说明入口 */
.side-tool { flex: none; width: 24px; height: 24px; padding: 0;
             display: inline-flex; align-items: center; justify-content: center;
             border: 1px solid var(--line); border-radius: 6px;
             background: #fff; color: var(--muted); cursor: pointer; }
.side-tool:hover { border-color: var(--brand); color: var(--brand); }

/* 2026-10-28：主区必须自己滚。
   ⚠ 不加 min-height:0 + overflow，grid 的 1fr 行会被内容撑高，
   底栏（状态栏）就被推到视口外面 —— 表现是"底栏时有时无"，
   任务多的时候整条消失，任务少的时候又冒出来，极难复现。 */
.main { display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: auto; }
.top {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 22px 10px;
  position: sticky; top: 0; z-index: 20; background: var(--bg);
}
.top h2 { margin: 0; font-size: 18px; }
.top-right { display: flex; align-items: center; gap: 8px; }
.theme { width: 128px; padding: 5px 8px; font-size: 12px; }
.main > :deep(.page) { padding: 0 22px 22px; }

/* V0.0.18 · 全局搜索框（顶栏） */
.gsearch { position: relative; display: flex; align-items: center; }
.gs-ic { position: absolute; left: 8px; display: inline-flex; color: var(--muted); pointer-events: none; }
.gs-input {
  width: 190px; padding: 5px 26px 5px 28px; font-size: 12.5px;
  border: 1px solid var(--line); border-radius: 9px; background: #fff; color: var(--text);
}
.gs-input:focus { outline: none; border-color: var(--brand); }
.gs-spin { position: absolute; right: 7px; display: inline-flex; color: var(--muted); }
.gs-panel {
  position: absolute; top: calc(100% + 6px); right: 0; z-index: 60;
  width: 340px; max-height: 420px; overflow: auto;
  background: #fff; border: 1px solid var(--line); border-radius: 12px;
  box-shadow: 0 12px 32px rgba(16, 24, 40, .18); padding: 6px;
}
.gs-grp { margin-bottom: 4px; }
.gs-title { font-size: 11px; color: var(--muted); padding: 4px 8px 2px; letter-spacing: .4px; }
.gs-item {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 5px 8px; border: 0; background: transparent; border-radius: 8px;
  cursor: pointer; text-align: left; font-size: 13px; color: var(--text);
}
.gs-item:hover { background: #f5f3ff; }
.gs-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.gs-meta { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.gs-thumb { width: 26px; height: 26px; border-radius: 5px; object-fit: cover; background: #eee; flex: none; }
.gs-empty { padding: 10px; font-size: 12.5px; color: var(--muted); text-align: center; }

/* V0.0.18 · 通知中心铃铛 */
.notif { position: relative; }
.mode-btn {
  display: inline-flex; align-items: center; gap: 6px;
  height: 30px; padding: 0 10px; border: 1px solid var(--line);
  border-radius: 8px; background: transparent; color: var(--text);
  font-size: 12px; cursor: pointer; white-space: nowrap;
}
.mode-btn:hover { border-color: var(--brand); color: var(--brand); }
.bell-btn {
  position: relative; display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; border: 1px solid transparent; border-radius: 8px;
  background: transparent; color: var(--muted); cursor: pointer;
}
.bell-btn:hover { background: #f5f3ff; color: var(--brand); }
.bell-btn.on { background: #efeaff; color: var(--brand); border-color: var(--line); }
.bell-badge {
  position: absolute; top: -3px; right: -3px; min-width: 15px; height: 15px;
  padding: 0 3px; border-radius: 999px; background: var(--err); color: #fff;
  font-size: 10px; line-height: 15px; text-align: center; font-variant-numeric: tabular-nums;
}
.notif-panel {
  position: absolute; top: calc(100% + 6px); right: 0; z-index: 60;
  width: 380px; max-height: 460px; overflow: auto;
  background: #fff; border: 1px solid var(--line); border-radius: 12px;
  box-shadow: 0 12px 32px rgba(16, 24, 40, .18);
}
.np-hd { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-bottom: 1px solid var(--line); position: sticky; top: 0; background: #fff; }
.np-hd b { font-size: 14px; }
.np-acts { margin-left: auto; display: flex; gap: 6px; }
.np-empty { padding: 20px; text-align: center; color: var(--muted); font-size: 12.5px; }
.np-list { list-style: none; margin: 0; padding: 4px; }
.np-item { display: flex; align-items: flex-start; gap: 6px; border-radius: 9px; padding: 2px; }
.np-item.unread { background: #f7f5ff; }
.np-main { flex: 1; display: flex; gap: 8px; border: 0; background: transparent; cursor: pointer; text-align: left; padding: 6px 4px; border-radius: 8px; }
.np-main:hover { background: #efeaff; }
.np-ic { flex: none; margin-top: 2px; color: var(--muted); }
.np-ic.warn { color: #c98a00; }
.np-ic.error { color: var(--err); }
.np-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.np-title { font-size: 13px; color: var(--text); }
.np-text { font-size: 12px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.np-ts { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.np-mark { flex: none; align-self: center; width: 24px; height: 24px; border: 1px solid var(--line); border-radius: 6px; background: #fff; color: var(--brand); cursor: pointer; display: inline-flex; align-items: center; justify-content: center; }
.np-mark:hover { border-color: var(--brand); }
.np-hd .small { font-size: 12px; }
.np-hd .btn.sm { padding: 4px 10px; font-size: 12px; border-radius: 8px; }

/* ── V0.0.33 微交互：所有可点区域统一过渡节奏，避免有的秒变、有的滞后 ── */
.subitem, .side-btn, .side-tool, .mode-btn, .bell-btn, .sb-btn, .ncar,
.gs-item, .np-main, .np-mark, .nbadge, .theme-box .theme {
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease),
              border-color var(--dur-fast) var(--ease), transform var(--dur-fast) var(--ease);
}
.side-btn:active, .side-tool:active, .bell-btn:active, .mode-btn:active { transform: scale(.94); }
@keyframes mrm-rail-in {
  from { opacity: 0; transform: translateX(-8px) }
  to   { opacity: 1; transform: none }
}
.side-rail { animation: mrm-rail-in var(--dur) var(--ease) both; }
.totop { animation: mrm-pop-in var(--dur) var(--ease); }
/* 有未读通知时角标轻微呼吸，比静态红点更容易被余光捕捉到 */
@keyframes mrm-badge-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(220, 38, 38, .32) }
  50%      { box-shadow: 0 0 0 4px rgba(220, 38, 38, 0) }
}
.bell-badge { animation: mrm-badge-pulse 2.4s var(--ease) infinite; }
/* 窄屏（手机）：顶栏搜索框收窄，结果面板与通知面板改成占满可用宽度 */
@media (max-width: 760px) {
  .gs-input { width: 120px; }
  .gs-panel { width: min(340px, 88vw); right: auto; left: 0; }
  .notif-panel { width: min(380px, 92vw); right: 0; }
}
</style>
