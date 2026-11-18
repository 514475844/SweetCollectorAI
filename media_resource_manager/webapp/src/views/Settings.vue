<script setup>
// 设置页（2026-10-27 重构）：从「一张小表单」改成**按桌面端全量配置铺开**的分组设置。
//
// 设计要点：
//   1. **数据驱动**：所有简单项走 SECTIONS 声明（类型/范围/提示），模板只渲染一遍。
//      这样加一个新配置项 = 在数组里加一行，不会漏样式、不会漏保存逻辑。
//   2. **白名单制**：能不能改由后端 `_writable` 说了算，前端不做二次猜测
//      （桌面端专属项后端不认，界面自动变灰并给一句说明）。
//   3. 危险/需重启项（监听端口、目录、密码）都带解释文案，不假装"已生效"。
import { ref, reactive, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'
import { uiMode, setUiMode, readWebTheme } from '../uiPrefs'
import { onShellSave } from '../nativeShell'
let unbindShell = null
import Icon from '../components/Icon.vue'
import FolderPicker from '../components/FolderPicker.vue'
import DiskMaint from '../components/DiskMaint.vue'
import CookieMgr from '../components/CookieMgr.vue'
import { setAnimatedIcons } from '../uiPrefs'
// V0.0.17：网页端补齐的设置页分区（主密码 / 磁盘趋势 / 自定义皮肤）
import DbMaster from '../components/DbMaster.vue'
import DiskTrend from '../components/DiskTrend.vue'
import SkinManager from '../components/SkinManager.vue'

/** 当前皮肤的中文名（下拉里显示「现代」，这里不能显示裸键名 modern）。 */
const THEME_CN = { classic: '原始', violet_look: '紫罗兰', modern: '现代', qbittorrent: '深色' }
const themeNow = computed(() => {
  const k = String(readWebTheme() || 'classic')
  if (k.startsWith('custom:')) return `自定义（${k.slice(7)}）`
  return THEME_CN[k] || k
})

/** 打开全局快捷键说明弹窗（App.vue 挂载的 ShortcutsDialog 监听此事件）。 */
function openShortcuts() {
  window.dispatchEvent(new Event('mrm-shortcuts'))
}
/** V0.0.23：改键单独入口（说明窗口只读，不进编辑态）。 */
function openShortcutsEdit() {
  window.dispatchEvent(new Event('mrm-shortcuts-edit'))
}
/** 重新运行 3 步新手引导（App.vue 挂载的 Onboarding 监听此事件）。 */
function rerunOnboarding() {
  window.dispatchEvent(new Event('mrm-onboarding'))
}

const s = ref({})            // 原始设置（含 _presets/_writable/_note）
const form = reactive({})    // 可编辑副本
const err = ref('')
const info = ref('')

const proxies = ref([])
const disk = ref(null)
const backup = ref(null)
const learn = ref(null)
const prefsInput = ref(null)
const importing = ref(false)

// ── R241 整库备份（配置 + 库 + 规则 + 缩略图）─────────────────────────
// 与上面「立即备份」的区别：那是每天一份的**数据库快照**（几 MB，误删回滚用），
// 这里是**换电脑/重装**用的整包（几百 MB，带配置/规则/缩略图）。
// 导出与恢复都必须走「起作业 → 轮询进度」：同步请求会撞浏览器超时。
const fullBk = ref([])
const fullTotal = ref('')
const fullBusy = ref('')        // '' | 'export' | 'restore' | 'upload'
const fullPct = ref(0)
const fullMsg = ref('')
// R247：默认**不打包缩略图**。缩略图能按需重建，但百万级时它是几百 MB
// 到几 GB —— 每次备份都带上，备份会从秒级变成半小时。
// ── R254：系统集成 ──
const sys = ref({})
const updBusy = ref(false)
const updText = ref('')
const updHas = ref(false)
const updUrl = ref('')

async function loadSys() {
  try { sys.value = await api.sysIntegration() } catch { /* 读不到就不显示 */ }
}

async function checkUpdate() {
  updBusy.value = true
  updText.value = ''
  updUrl.value = ''
  try {
    const r = await api.sysUpdate()
    if (!r.ok) { updText.value = r.error || '检查失败'; updHas.value = false; return }
    updHas.value = !!r.has_update
    updText.value = r.text || (r.has_update ? '发现新版本' : '已是最新版本')
    updUrl.value = r.has_update ? (r.url || '') : ''
  } catch (e) { updText.value = e.message }
  finally { updBusy.value = false }
}

// ── V0.0.18：关于区「检查程序更新」（GET /api/update/check）──
const aboutBusy = ref(false)
const aboutText = ref('')
const aboutHas = ref(false)
const aboutUrl = ref('')
async function checkAppUpdate() {
  aboutBusy.value = true
  aboutText.value = ''
  aboutUrl.value = ''
  try {
    const r = await api.updateCheck()
    if (!r.ok) { aboutText.value = r.error || '检查更新失败'; return }
    aboutHas.value = !!r.has_update
    aboutText.value = r.text || (r.has_update
      ? `发现新版本 ${r.remote}（当前 ${r.local}）`
      : `已是最新版本（${r.local}）`)
    aboutUrl.value = r.has_update ? (r.url || '') : ''
  } catch (e) { aboutText.value = e.message || '检查更新失败' }
  finally { aboutBusy.value = false }
}

// ── V0.0.55 批1：定时自动关机（动作式：即时 POST /api/powerdown，不进草稿）──
const pdBusy = ref(false)
const pdMode = ref('')            // '' | on_idle | daily
const pdAt = ref('23:30')
const pdGraceMin = ref(3)
const pdCountdown = ref(60)
const pdPlan = ref({})            // 后端 GET /api/powerdown 的当前计划
async function pdRefresh() {
  try {
    const r = await api.powerdown()
    if (!r || r.ok === false) return
    pdPlan.value = r.plan || {}
    pdCountdown.value = Number(r.plan?.countdown || 60)
    // 内存里有每日/空闲计划时回填下拉，避免界面和实际计划两张皮
    if (r.plan?.mode === 'at_time' && r.plan?.daily) {
      pdMode.value = 'daily'
      if (r.plan.at_hhmm) pdAt.value = r.plan.at_hhmm
    } else if (r.plan?.mode === 'on_idle') {
      pdMode.value = 'on_idle'
    }
  } catch { /* 读不到不挡页面 */ }
}
const pdPlanText = computed(() => {
  const p = pdPlan.value || {}
  if (p.mode === 'at_time') {
    const t = p.target_ts ? new Date(p.target_ts * 1000) : null
    const when = p.daily ? `每日 ${p.at_hhmm || (t ? t.toTimeString().slice(0, 5) : '?')}`
      : (t ? t.toLocaleString('zh-CN', { hour12: false }) : '定点')
    return `${when} · 关机前倒计时 ${p.countdown || 60} 秒`
  }
  if (p.mode === 'on_idle') return `任务全部跑完（队列连续空闲）后关机 · 倒计时 ${p.countdown || 60} 秒`
  return ''
})
async function pdApply() {
  pdBusy.value = true
  try {
    let body = null
    if (pdMode.value === 'on_idle') {
      body = { mode: 'on_idle', countdown: pdCountdown.value,
               idle_grace: Math.max(60, (pdGraceMin.value || 3) * 60) }
    } else if (pdMode.value === 'daily') {
      body = { mode: 'at_time', at: pdAt.value, daily: true, countdown: pdCountdown.value }
    } else {
      body = { mode: 'off' }
    }
    const r = await api.post('/api/powerdown', body)
    if (r && r.ok === false) { err.value = r.error || '安排失败'; return }
    pdPlan.value = (r && r.plan) || {}
    info.value = pdMode.value === 'on_idle' ? '已安排：任务跑完后自动关机'
      : pdMode.value === 'daily' ? `已安排：每日 ${pdAt.value} 自动关机`
      : '已取消自动关机'
    setTimeout(() => (info.value = ''), 3500)
  } catch (e) { err.value = e.message || '安排失败' }
  finally { pdBusy.value = false }
}
function pdSetMode(v) {
  pdMode.value = v
  pdApply()
}
async function pdCancel() {
  try {
    const r = await api.post('/api/powerdown', { mode: 'off' })
    if (r && r.ok === false) { err.value = r.error || '取消失败'; return }
    pdMode.value = ''
    pdPlan.value = (r && r.plan) || {}
    info.value = '已取消自动关机'
    setTimeout(() => (info.value = ''), 3000)
  } catch (e) { err.value = e.message }
}

async function toggleAutostart(on) {
  try {
    const r = await api.sysAutostart(on)
    if (!r.ok) { err.value = r.error; return }
    sys.value.autostart = !!r.autostart
    info.value = r.message || '已更新'
    setTimeout(() => (info.value = ''), 4000)
  } catch (e) { err.value = e.message }
}

// ── 目录选择器（设置项里的「浏览」按钮用）──
const pickerOpen = ref(false)
const dirTarget = ref(null)

function onPicked(p) {
  const it = dirTarget.value
  if (!it) { pickerOpen.value = false; return }
  form[it.k] = p
  draft[it.k] = p          // 进草稿，仍要用户选择「保存本页」才落盘
  info.value = `已选择 ${p}，选择「保存本页」后生效`
  setTimeout(() => (info.value = ''), 4000)
  dirTarget.value = null
  pickerOpen.value = false
}

/** 设置项里目录「浏览」按钮：选中后回填到该项（走草稿，不立刻落盘）。 */
function pickSettingDir(it) {
  dirTarget.value = it
  pickerOpen.value = true
}

// ── 整库备份 ──
/** 按当前配置立刻清一次旧备份（不等下一次导出）。 */
async function pruneFull() {
  if (!confirm('按「份数 / 天数 / 总量」清理旧备份？\n最新一份永远保留。')) return
  fullBusy.value = 'prune'
  try {
    const r = await api.fullPrune()
    if (!r.ok) { err.value = r.error || '清理失败'; return }
    const mb = Math.round((r.bytes || 0) / 1048576)
    info.value = r.deleted && r.deleted.length
      ? `已清理 ${r.deleted.length} 份旧备份（腾出 ${mb} MB）`
      : '没有需要清理的备份'
    await loadFull()
  } catch (e) { err.value = e.message }
  finally { fullBusy.value = '' }
}

const fullIncludeThumbs = ref(false)
const fullSafety = ref(true)
const zipInput = ref(null)
let fullTimer = null

const writable = computed(() => new Set(s.value._writable || []))
const can = (k) => writable.value.has(k)
const writableCount = computed(() => (s.value._writable || []).length)
// V0.0.51：Web 访问页「又多又乱又杂」—— 只把最常用的四项摊开：
// 局域网开关 / 端口 / 登录密码 / 局域网免密。监听地址、白名单、锁定策略、
// 安全加固、响应头、反向代理、数据库加密这些**高级项收进折叠区**，
// 需要的人点开就能找到，不需要的人一眼看完整页。
const webAdvanced = ref(false)

// V0.0.47：「允许局域网访问」人话开关。监听地址填了具体 IP 也算开放局域网。
const lanOpen = computed(() => {
  const b = String(form.web_bind || '127.0.0.1')
  return b !== '127.0.0.1' && b !== 'localhost' && b !== ''
})

// ── 分组声明 ────────────────────────────────────────────────────────
// type: bool | int | float | text | password | select | preset | list | kb
const SECTIONS = [
  {
    title: '外观与启动',
    items: [
      // V0.0.27：这里原来还有一个「网页皮肤」下拉，加上设置页的皮肤管理、
      // 左下角那个下拉，同一个功能一共三个入口（用户原话「这对吗？」）。
      // 现在换皮肤**只在左下角**一处（内建 + 自定义都在那个列表里），
      // 这一组只留「动效」与「启动行为」。
      // 2026-10-28：图标静态 / 动态总开关。
      // 默认开（用户提供的 svg-spinners + line-md 动画集），关掉只是不动，功能不变。
      { k: 'ui_animated_icons', label: '使用动态图标', type: 'bool',
        hint: '加载、刷新、扫描、下载中这些位置用带动画的图标（转圈 / 脉冲 / 数据流）；'
              + '关掉换成同语义的静态图标。系统开了「减少动效」时自动不动' },
      // V0.0.27：用户实测反馈「启动后最小化到托盘功能呢？」
      { k: 'tray_start_minimized', label: '启动后最小化到托盘', type: 'bool',
        hint: '客户端（SweetWeb）与桌面版共用这一项：勾上后启动不弹窗口，'
              + '只在系统托盘留一个图标，采集照常跑；双击托盘图标打开界面，'
              + '右键可退出。系统托盘不可用时自动照常显示窗口，不会叫不回来' },
    ],
  },
  {
    title: '启动后自动恢复',
    items: [
      { k: 'resume_on_start', label: '启动后自动接着干', type: 'bool',
        hint: '上次关软件时没做完的活，下次启动自动继续；默认关，勾上后下面三类可单独选' },
      { k: 'resume_downloads', label: '· 未完成的下载', type: 'bool',
        when: (f) => !!f.resume_on_start,
        hint: '上次退出时还在采集的任务，启动后重新排队下载' },
      { k: 'resume_tags', label: '· 没打完的标签', type: 'bool',
        when: (f) => !!f.resume_on_start,
        hint: '已入库但标签还没打完的图片，启动后接着打' },
      { k: 'resume_imports', label: '· 没导完的文件夹', type: 'bool',
        when: (f) => !!f.resume_on_start,
        hint: '上次中断的本地文件夹导入，启动后从剩下的接着导（已入库的会跳过）' },
    ],
  },
  {
    title: '采集与下载',
    items: [
      { k: 'concurrency', label: '并发数', type: 'int', min: 1, max: 32,
        hint: '并发抓取页面数。数值过高易触发站点访问限制' },
      { k: 'max_depth', label: '抓取深度', type: 'int', min: 0, max: 10,
        hint: '从入口向下跟随的层数（0 表示仅抓取入口页）' },
      { k: 'delay', label: '请求间隔（秒）', type: 'float', min: 0, max: 60, step: 0.1,
        hint: '相邻请求的间隔时长，用于降低被站点限制的风险' },
      { k: 'timeout', label: '单请求超时（秒）', type: 'int', min: 5, max: 300 },
      { k: 'retry', label: '单页重试次数', type: 'int', min: 0, max: 10 },
      { k: 'parallel_tasks', label: '同时运行任务数', type: 'int', min: 1, max: 10,
        hint: '同时运行的任务数。超出设备承载能力时应下调' },
      { k: 'max_pages', label: '单任务最多抓页数', type: 'int', min: 0, max: 100000,
        hint: '0 表示不限' },
      { k: 'user_agent', label: 'User-Agent', type: 'text',
        hint: '留空时使用内置默认值' },
      { k: 'cookie_browser', label: '读取 Cookie 的浏览器', type: 'text',
        hint: '如 chrome / edge；留空则不读取' },
      { k: 'adapter_auto_match', label: '按域名自动用适配器', type: 'bool',
        hint: '任务适配器为「自动」且 gallery-dl 无法识别站点时，按域名在规则目录中匹配现成规则。'
              + 'gallery-dl 认得的站仍然优先走快路径（这条不会改变它们的行为）' },
    ],
  },
  {
    title: '失败重试与限速',
    items: [
      { k: 'auto_retry', label: '自动重试次数', type: 'int', min: 0, max: 10,
        hint: '0 表示不重试；失败任务保持失败状态，不标记为完成' },
      { k: 'auto_retry_delay', label: '首次重试等待（秒）', type: 'int', min: 5, max: 3600,
        hint: '第 n 次等待 = 首次 × 2^(n-1)' },
      { k: 'auto_slowdown', label: '自动降频', type: 'bool',
        hint: '失败率 ≥30% 时自动增大该任务的请求间隔（上限 10 秒）' },
      // V0.0.38：「分时段限速」的开关**只留在「速度与网络」的时段表旁边**。
      // 同一项在「采集与保存」里还有一份，两处一个即时保存一个要按保存本页，
      // 用户在 A 处勾了、到 B 处看见是空的，就会说「我明明选了为什么没生效」。
    ],
  },
  {
    title: '图片过滤',
    items: [
      { k: 'min_width', label: '最小宽度', type: 'int', min: 0, max: 20000, unit: 'px' },
      { k: 'min_height', label: '最小高度', type: 'int', min: 0, max: 20000, unit: 'px' },
      { k: 'min_bytes', label: '最小体积', type: 'kb', min: 0, max: 102400,
        hint: '小于该体积的图片跳过' },
      { k: 'max_bytes', label: '最大体积', type: 'kb', min: 0, max: 102400,
        hint: '0 表示不限' },
      { k: 'suspicious_bytes', label: '可疑图阈值', type: 'kb', min: 0, max: 10240,
        hint: '小于该值的文件按占位图或损坏图处理' },
    ],
  },
  {
    title: '保存与整理',
    items: [
      { k: 'convert_to_jpg', label: '转成 JPG', type: 'bool',
        hint: 'webp/avif 转 JPG：兼容性更好，体积略增' },
      { k: 'tidy_after_task', label: '任务结束整理目录', type: 'bool' },
      { k: 'task_done_goto_gallery', label: '完成后跳到图库', type: 'bool' },
      { k: 'library_dir', label: '图库目录', type: 'text', dir: true, full: true,
        hint: '修改后仅对后续下载的图片生效，已有文件不迁移' },
      { k: 'isolate_dir', label: '隔离区目录', type: 'text', dir: true, full: true },
      { k: 'review_dir', label: '审核暂存目录', type: 'text', dir: true, full: true },
    ],
  },
  {
    title: 'AI 与审核',
    items: [
      // V0.0.40：整组按「用户能不能看懂、这一项到底管不管用」重写。
      // 删掉的三项都有实证：ai_gpu 只在 ai_device=auto 时才生效（和设备下拉
      // 打架，勾了不一定有用）；review_auto_fill / review_key_ask 只被旧桌面
      // 端审核窗口读，网页端和现有壳里没有任何代码用它 —— 摆着就是骗人。
      { k: 'ai_enabled_global', label: '识别图片内容', type: 'bool',
        hint: '打开后，软件会"看"每一张图，判断与主题词的接近程度；关掉后只按网址和关键词采集，省 CPU，但「不喜欢」教出来的偏好也不再生效' },
      { k: 'ai_device', label: '推理设备', type: 'select', options: [
        { v: 'auto', t: '自动（建议）' },
        { v: 'gpu', t: '显卡' },
        { v: 'cpu', t: 'CPU（速度较慢，兼容性最佳）' },
        { v: 'npu', t: 'NPU（部分笔记本自带）' },
      ], hint: '指定图像识别所使用的计算设备。常规场景保持「自动」即可：轻量模型采用 CPU 推理效率更优，大型模型优先使用显卡。设置对后续图像生效。' },
      { k: 'ai_threads', label: '同时用几个核心', type: 'int', min: 0, max: 32,
        hint: '0 = 自动（使用一半处理器核心）。识别耗时较长时可适当调大；数值过高会与下载任务争抢处理器资源。' },
      { k: 'ai_backend', label: '识别方式', type: 'select', options: [
        { v: 'builtin', t: '本地模型（无需联网，无使用费用）' },
        { v: 'api', t: '在线接口（需自备账号，按调用量计费）' },
      ], hint: '本地模型就是「AI 模型」页里下载的那些；只有使用外部服务商的视觉模型时才选在线接口' },
      { k: 'ai_api_url', label: '接口地址', type: 'text', full: true,
        when: (f) => f.ai_backend === 'api',
        hint: '填到 /v1 为止，例如 https://api.example.com/v1' },
      { k: 'ai_api_key', label: '接口密钥', type: 'password', full: true,
        when: (f) => f.ai_backend === 'api',
        hint: '只存在本机配置里，不会显示出来；留空表示不改动已保存的密钥' },
      { k: 'ai_api_model', label: '模型名称', type: 'text',
        when: (f) => f.ai_backend === 'api',
        hint: '填服务商文档里的模型名，例如 gpt-4o-mini；留空表示用接口的默认模型' },
      { k: 'review_margin', label: '待审核区间', type: 'float', min: 0, max: 1,
        step: 0.01,
        hint: '系统对每张图像给出 0–1 分，高于合格线则收录，低于则隔离；分数接近合格线的图像进入人工审核。0.12 表示与合格线相差 0.12 以内需人工复核；数值越大，送审图像越多。' },
      { k: 'sensitive_words', label: '敏感词', type: 'list', full: true,
        hint: '逗号分隔。命中这些词的图会被标成敏感，审核时排在最前面，便于优先处理' },
    ],
  },
  {
    title: '主题词（全局）',
    items: [
      { k: 'global_blacklist', label: '全局排除词', type: 'list', full: true,
        hint: '逗号分隔；任何任务均不采集包含这些词的内容' },
    ],
  },
  {
    title: '启动与退出',
    items: [
      { k: 'confirm_exit_running', label: '任务运行期间退出前二次确认', type: 'bool',
        hint: '避免误操作中断长时间运行的任务；任务数量较多时建议启用' },
      { k: 'prevent_sleep', label: '采集时阻止系统休眠', type: 'bool',
        hint: '系统休眠会中断长时间任务；启用后可阻止休眠' },
      { k: 'splash_enabled', label: '显示启动画面', type: 'bool',
        hint: '关闭后启动直接进主界面，不再显示那张启动图' },
      { k: 'log_max_mb', label: '日志单文件上限（MB）', type: 'int', min: 0, max: 200,
        hint: '超过该值则分卷；0 表示不分卷（输出为单个文件）' },
      { k: 'log_backup_count', label: '日志保留份数', type: 'int', min: 1, max: 50,
        hint: '含当前文件最多占用「上限 × (份数+1)」空间' },
      { k: 'update_manifest_url', label: '更新清单地址（高级）', type: 'text', full: true,
        hint: '留空时默认从本项目 GitHub 的最新版本检查更新（无需配置）。' +
              '只有自己托管了更新源才需要填这里，填了以这里为准。' },
    ],
  },
  {
    title: '保存与命名',
    items: [
      { k: 'subdir_mode', label: '输出子目录方式', type: 'select', options: [
        { v: 'site', t: '按站点分（默认）' },
        { v: 'task', t: '按任务名分' },
        { v: 'both', t: '任务名 / 站点' },
        { v: 'none', t: '不分子目录（全放一起）' },
      ], hint: '仅影响后续下载的图片；已有文件不迁移' },
      { k: 'silent_add', label: '静默添加任务', type: 'bool',
        hint: '使用默认配置直接创建任务，不打开表单（网页端「快速添加」同此行为）' },
      { k: 'clean_part_on_cancel', label: '停止时清理临时文件', type: 'bool',
        hint: '清理 .part 未完成文件；仅删除临时文件，不影响已完成的图片' },
      { k: 'move_on_path_change', label: '改目录时移动已有文件', type: 'bool',
        hint: '修改任务输出目录时询问是否迁移旧文件（大目录耗时较长）' },
      { k: 'watch_enabled', label: '监视文件夹', type: 'bool',
        hint: '将每行一个网址的 .txt 文件放入监视目录后自动创建任务' },
      { k: 'watch_dir', label: '监视文件夹路径', type: 'text', full: true,
        hint: '处理完成的清单移至「已处理/」目录，不删除原文件' },
      { k: 'watch_interval_sec', label: '监视扫描间隔（秒）', type: 'int', min: 5, max: 600 },
    ],
  },
  {
    title: '速度限制',
    items: [
      { k: 'speed_limit_kbps', label: '全局速率上限（KB/s）', type: 'int', min: 0, max: 100000,
        hint: '0 表示不限速；与时段表同时配置时取较严格者' },
      { k: 'speed_skip_lan', label: '内网地址不限速', type: 'bool',
        hint: '本地 NAS、127.0.0.1 等地址不限速（对其限速无实际意义）' },
      // V0.0.38：「启用分时段限速」不在这里重复出现 —— 它和时段表是一个整体，
      // 开关就在「速度与网络 → 限速时段表」下面（同一项两处，必有一处是假的）。
    ],
  },
  {
    title: '网络与代理',
    items: [
      { k: 'proxy_dns', label: '域名解析也走代理', type: 'bool',
        hint: 'socks5 → socks5h；DNS 污染环境下需启用（否则代理可用但无法访问）' },
      { k: 'proxy_tasks_only', label: '仅任务请求走代理', type: 'bool',
        hint: '软件更新检查与在线模型接口直连' },
      { k: 'ip_filter_enabled', label: '启用 IP 过滤', type: 'bool',
        hint: '命中规则的主机跳过，不再发起请求' },
      { k: 'ip_filter_text', label: 'IP 过滤规则', type: 'textarea', full: true,
        hint: '每行一条：1.2.3.4 / 1.2.3.0/24 / example.com（域名含子域）；# 开头为注释' },
      // ---- 2026-10-28 第 2 批（编号 32/33/34）----
      { k: 'bind_ip', label: '绑定网卡 / 源地址', type: 'select-ip',
        hint: '多网卡或多线路机器可指定出口网卡；留空表示系统自动选择。使用代理时该项不生效' },
      { k: 'ssl_verify', label: '校验 HTTPS 证书', type: 'bool',
        hint: '关闭后全局不校验证书（风险：可被中间人替换内容）。仅对个别自签名站点放行请使用下方选项' },
      { k: 'ssl_insecure_hosts', label: '跳过证书校验的域名', type: 'list', full: true,
        hint: '逗号分隔，支持子域（用于自签名证书或内网站点，较关闭全局校验更安全）' },
      { k: 'allow_private_hosts', label: '允许抓内网地址', type: 'bool',
        hint: '关闭后 10.x / 192.168.x / 127.x 一律跳过（防止被网页诱导抓取内网地址）。抓取本地 NAS 时请保持开启' },
    ],
  },
  {
    title: 'HTTPS 加密访问',
    items: [
      { k: 'web_https', label: '启用 HTTPS', type: 'bool',
        hint: '首次启用时自动生成本机自签名证书（有效期 10 年），无需自行配置证书。修改后需重启软件' },
      { k: 'web_https_port', label: 'HTTPS 端口', type: 'int', min: 1, max: 65535,
        hint: '与 HTTP 端口分开（默认 8443），HTTP 端口照常可用' },
      { k: 'web_https_redirect', label: '强制走 HTTPS', type: 'bool',
        hint: 'HTTP 请求自动跳转至 HTTPS；移动端浏览器会提示证书不受信任，选择继续访问即可' },
      { k: 'web_secure_cookie', label: '会话 Cookie 加 Secure', type: 'bool',
        hint: '只在 HTTPS 访问时生效（HTTP 下加了会导致登录后立刻掉线）；'
              + '防证书降级把登录凭证从明文那口漏出去' },
    ],
  },
  {
    title: '站点订阅 / 慢任务',
    items: [
      { k: 'sub_enabled', label: '启用站点订阅监控', type: 'bool',
        hint: '关闭后停止定时检查（订阅源与历史快照保留）。订阅源在「站点订阅」页维护' },
      { k: 'sub_interval_min', label: '新订阅的默认检查间隔（分钟）', type: 'int', min: 5, max: 10080,
        hint: '每个订阅源可单独设置；最小 5 分钟，避免定时请求过密' },
      { k: 'slow_yield_enabled', label: '慢任务自动让位', type: 'bool',
        hint: '存在排队任务时，连续无进展的任务先暂停并让出并发位（暂停的任务返回队列继续运行）' },
      { k: 'slow_yield_min', label: '多久没进展算「慢」（分钟）', type: 'int', min: 1, max: 600,
        hint: '连续未发现新页面与新图片超过该时长才让出；新启动的任务不参与判定' },
    ],
  },
  {
    title: '存储与清理',
    items: [
      { k: 'use_recycle', label: '删除进回收站', type: 'bool',
        hint: '关闭后直接删除，不可恢复' },
      { k: 'recycle_keep_days', label: '回收站保留天数', type: 'int', min: 0, max: 365 },
      { k: 'daily_backup', label: '每日自动备份', type: 'bool' },
      { k: 'backup_keep', label: '备份保留份数', type: 'int', min: 1, max: 60 },
      { k: 'backup_sync_dir', label: '备份同步目录', type: 'text', dir: true, full: true,
        hint: '留空仅保存到本地；填写后将额外复制一份到该目录' },
      { k: 'min_free_mb', label: '最低剩余空间（MB）', type: 'int', min: 0, max: 102400,
        hint: '低于该值时暂停下载并告警' },
      { k: 'log_sanitize', label: '日志脱敏', type: 'bool',
        hint: '日志中隐去用户名、路径等敏感信息' },
      { k: 'compliance_level', label: '合规检查', type: 'select', options: [
        { v: 'off', t: '关闭（个人自用，不检查）' },
        { v: 'loose', t: '宽松（robots 不允许也只警告）' },
        { v: 'standard', t: '标准（robots 禁止则拒绝）' },
        { v: 'strict', t: '严格（连路径也校验，需填授权依据）' },
      ] },
    ],
  },
]

// 2026-10-27：设置项铺到 60 个以后，找一项要滚很久 —— 加搜索。
// 命中范围 = 标签 / 键名 / 提示文案，用户记得哪个词都能搜到。
const search = ref('')
// V0.0.5（决定清单·照 QB 第②条）：后端判为「不能在线改」的项**直接藏掉**，
// 不再灰着摆在那儿骗人手痒（托盘、快捷键这类只能在桌面干的，网页上不出现）。
// ⚠ 判据必须等后端数据到位（_writable 列表拿到）后才生效 —— 第一帧
// s 还是空对象，若此时就过滤会把整页瞬间清空再闪回来。
// V0.0.6：白名单是**空数组**（后端没给全 / 数据没到位）时不能把整页
// 设置项全藏了 —— 那等于打开设置一片空白。空列表按「还没有过滤依据」
// 处理，全部照常显示，等数据来了再收。
const hideReadonly = computed(() => Array.isArray(s.value._writable)
  && s.value._writable.length > 0)
const isShown = (it) => !hideReadonly.value || can(it.k)
const shownSections = computed(() => {
  const k = search.value.trim().toLowerCase()
  const visible = (sec) => ({
    ...sec,
    items: sec.items.filter((it) =>
      isShown(it) &&
      [it.label, it.k, it.hint].some((v) => String(v || '').toLowerCase().includes(k))),
  })
  if (!k) return SECTIONS.map(visible).filter((sec) => sec.items.length)
  return SECTIONS.map(visible).filter((sec) => sec.items.length)
})
const hitCount = computed(() => shownSections.value.reduce((n, s) => n + s.items.length, 0))

// ── 2026-10-29（第 8 条）：分页 + 每页「保存本页」 ──────────────────────
// 原来 60+ 项铺成一长条：① 找一项要滚很久；② 改完立刻生效、但界面只在
// 顶部闪一句「已保存」，用户根本不知道刚才那一下存没存上。
// 现在：按用途分 6 页，改动先进草稿（draft），选择「保存本页」才提交；
// 未保存的页在标签上打点，关窗口前有提示。
// 搜索不受分页限制 —— 搜就是要跨页找，搜的时候显示全部命中项，
// 保存按钮变成「保存命中项」（提交范围跟着显示范围走，不会偷偷多存）。
const TABS = [
  { k: 'ui', label: '外观与启动', icon: 'settings' },
  { k: 'grab', label: '采集与保存', icon: 'download' },
  { k: 'net', label: '速度与网络', icon: 'gauge' },
  { k: 'ai', label: 'AI 与主题词', icon: 'sparkles' },
  { k: 'web', label: 'Web 访问', icon: 'globe' },
  { k: 'store', label: '存储与维护', icon: 'hard-drive' },
  // V0.0.18：新增「关于」分页（程序更新检查）
  { k: 'about', label: '关于', icon: 'info' },
]
/** section 标题 → 分页。标题改了这里必须同步（漏了会落到「采集与保存」）。 */
const SEC_TAB = {
  '外观与启动': 'ui', '外观': 'ui', '启动与退出': 'ui', '启动后自动恢复': 'ui',
  '采集与下载': 'grab', '图片过滤': 'grab', '保存与命名': 'grab', '保存与整理': 'grab',
  '失败重试与限速': 'net', '速度限制': 'net', '网络与代理': 'net',
  // V0.0.47：HTTPS 是本地网页的加密访问方式，跟 Web 访问同页；放「网络与代理」
  // 页里跟下载代理混在一起，用户找不到。
  'HTTPS 加密访问': 'web',
  'AI 与审核': 'ai', '主题词（全局）': 'ai',
  '站点订阅 / 慢任务': 'store', '存储与清理': 'store',
}
const tab = ref('ui')
// V0.0.18：读取 ?tab= 直达分页（Cookie 管理等外部入口跳转用）
const route = useRoute()
const searching = computed(() => !!search.value.trim())
const tabOf = (title) => SEC_TAB[title] || 'grab'
/** 当前显示哪些分组：搜索时全显示，否则只显示当前页。 */
const pageSections = computed(() => {
  // V0.0.51：HTTPS 的内容并进 Web 访问页的「高级设置」折叠区，这里不再
  // 单独铺一张卡（否则同一页两张长卡，就是用户说的「又多又乱」）。
  const base = searching.value
    ? shownSections.value
    : shownSections.value.filter((s) => tabOf(s.title) === tab.value
      && !(tab.value === 'web' && s.title === 'HTTPS 加密访问'))
  // V0.0.40：条目可以带 `when(form)` —— 与它无关时整行不出现。
  // 典型例子：在线接口的地址/密钥/模型名，选了「本地模型」就不该摊在页面上，
  // 否则用户会以为这三项是必填的（上一版就是这样，自己都说不清要填哪个）。
  return base.map((s) => ({
    ...s,
    // 搜索时不按 when 过滤：用户明确在找这一项，命中就该显示出来
    items: (s.items || []).filter((it) => searching.value || !it.when
      || it.when(form)),
  })).filter((s) => s.items.length)
})

/** 草稿：改过的值先放这里，点保存才发出去。 */
const draft = reactive({})
const draftCount = computed(() => Object.keys(draft).length)
const pageSaving = ref(false)

function pageKeys() {
  const ks = []
  for (const s of pageSections.value) for (const it of s.items) ks.push(it.k)
  return ks
}
/** 当前页（搜索时 = 当前命中项）里还没保存的键。 */
const pageDirtyKeys = computed(() => pageKeys().filter((k) => k in draft))

/** V0.0.23：这几页的字段是「改完立即保存」的（不进草稿），底部不摆假按钮。
 *  ⚠ 只能是 web / about：存储与维护页里的「站点订阅 / 存储与清理」仍然是
 *  草稿制（走「保存本页」），把它算进即时保存会让改动永远存不下去。 */
const INSTANT_TABS = ['web', 'about']
const instantTab = computed(() => !searching.value && INSTANT_TABS.includes(tab.value))
const lastSaved = ref('')

/** 某个标签页有没有未保存修改（给 tab 上的小圆点用）。 */
function tabDirty(k) {
  if (searching.value) return false
  return SECTIONS.some((s) => tabOf(s.title) === k
    && s.items.some((it) => it.k in draft))
}

function onTab(k) {
  return searching.value || tab.value === k
}

function onBeforeUnload(e) {
  if (!draftCount.value) return
  e.preventDefault()
  e.returnValue = ''
}

// ── 通用读写 ────────────────────────────────────────────────────────
function displayValue(it) {
  const v = form[it.k]
  if (it.type === 'kb') return Math.round((Number(v) || 0) / 1024)
  if (it.type === 'list') return Array.isArray(v) ? v.join(', ') : (v || '')
  if (v === undefined || v === null) return ''
  return v
}

function toPatch(it, el) {
  switch (it.type) {
    case 'bool': return { [it.k]: !!el.checked }
    case 'int': return { [it.k]: Math.round(Number(el.value) || 0) }
    case 'float': return { [it.k]: Number(el.value) || 0 }
    case 'kb': return { [it.k]: Math.round(Number(el.value) || 0) * 1024 }
    default: return { [it.k]: el.value }
  }
}

async function onField(it, e) {
  // 密钥类：**留空 = 不改**。不加这道闸会把"没输入"当成"清空"，
  // 一失手就把已存的 API Key 抹掉（而且用户看不出发生了什么）。
  if (it.type === 'password') {
    if (!String(e.target.value || '').trim()) {
      info.value = '留空 = 不修改，已保留原值'
      setTimeout(() => (info.value = ''), 2200)
      return
    }
    // 密钥不进草稿、也不留在输入框里（草稿会把明文一直挂在页面上）。
    const patch = toPatch(it, e.target)
    await save(patch, it)
    e.target.value = ''
    return
  }
  // 2026-10-29（第 8 条）：其余项**只进草稿**，等用户选择「保存本页」。
  const patch = toPatch(it, e.target)
  for (const [k, v] of Object.entries(patch)) {
    if (!can(k)) {
      info.value = '这一项不支持在界面里修改'
      setTimeout(() => (info.value = ''), 2200)
      return
    }
    form[k] = v
    draft[k] = v
  }
}

/** 保存当前页（或搜索结果里）所有草稿项。 */
async function savePage() {
  const body = {}
  for (const k of pageDirtyKeys.value) body[k] = draft[k]
  if (!Object.keys(body).length) return
  pageSaving.value = true
  try {
    const r = await api.saveSettings(body)
    // V0.0.6：后端校验失败是 HTTP 200 + {"ok":false,"error":"中文原因"}。
    // 以前只看有没有抛异常，于是端口填 0 / 目录非法 / 被安全策略拒绝时
    // 前端照样清掉草稿弹「已保存」，用户以为改成了，实际一个字没动。
    if (r && (r.ok === false || r.error)) {
      err.value = r.error || '保存失败'
      return
    }
    const n = Object.keys(body).length
    for (const k of Object.keys(body)) delete draft[k]
    info.value = (r && r.note) ? r.note : `已保存 ${n} 项`
    setTimeout(() => (info.value = ''), 3200)
    // 动态图标开关是**全局运行时状态**：不同步的话要刷新整页才看到变化
    if ('ui_animated_icons' in body) setAnimatedIcons(body.ui_animated_icons)
    await load()
  } catch (e) { err.value = e.message } finally { pageSaving.value = false }
}

/** 放弃本页修改：把草稿丢掉，界面值回到服务端那份。 */
async function revertPage() {
  for (const k of pageDirtyKeys.value) delete draft[k]
  await load()
  info.value = '已放弃本页修改'
  setTimeout(() => (info.value = ''), 2200)
}

async function save(patch, it) {
  try {
    const body = {}
    for (const [k, v] of Object.entries(patch)) if (can(k)) body[k] = v
    if (!Object.keys(body).length) {
      info.value = '这一项不支持在界面里修改'
      setTimeout(() => (info.value = ''), 2200)
      return
    }
    const r = await api.saveSettings(body)
    if (r && (r.ok === false || r.error)) {
      err.value = r.error || '保存失败'
      return
    }
    info.value = (r && r.note) ? r.note : '已保存'
    // V0.0.23：即时保存的页在底部要能看到「最近保存」，否则用户不知道存没存上
    try {
      const _t = new Date().toLocaleTimeString('zh-CN', { hour12: false })
      lastSaved.value = `${_t}（${Object.keys(body).length} 项）`
    } catch (_e) { /* 忽略 */ }
    setTimeout(() => (info.value = ''), 3200)
    // 动态图标开关是**全局运行时状态**：不在这里同步的话，用户要刷新整页才看到变化，
    // 会以为开关没生效（这个坑在别的开关上踩过）。
    if ('ui_animated_icons' in body) setAnimatedIcons(body.ui_animated_icons)
    await load()
  } catch (e) { err.value = e.message }
}

async function load() {
  try {
    const d = await api.settings()
    s.value = d
    for (const k of Object.keys(d)) {
      if (k.startsWith('_')) continue
      // 2026-10-29：有未保存草稿的项**不许被服务端值盖回去**。
      // 密码类仍走即时保存（保存成功后也会调 load()），若这里无条件覆盖，
      // 用户会看到"刚改的那项自己变回旧值"——草稿其实还在，只是显示被盖了。
      form[k] = (k in draft) ? draft[k] : d[k]
    }
    if ('ui_animated_icons' in d) setAnimatedIcons(d.ui_animated_icons)
    // V0.0.40：`ai_backend` 的 local 与 builtin 在后台是同一条路径（都读
    // ai_model_path，也就是「AI 模型」页里选中的那个），界面上不再区分。
    // 老配置里是 local 的，显示成「本地模型」，保存时自然写成 builtin。
    if (form.ai_backend === 'local') form.ai_backend = 'builtin'
    err.value = ''
  } catch (e) { err.value = e.message }
  loadProxies(); loadDisk(); loadBackup(); loadLearn(); loadSys(); loadFull()
}

async function loadProxies() {
  try { proxies.value = (await api.proxies()).items || [] } catch { /* 忽略 */ }
}
async function loadDisk() {
  try { disk.value = await api.diskspace() } catch { /* 忽略 */ }
}
async function loadBackup() {
  try { backup.value = await api.backup() } catch { /* 忽略 */ }
}
async function loadLearn() {
  try { learn.value = await api.learning() } catch { /* 忽略 */ }
}

// ── 限速时段表（编号 18/19）─────────────────────────────────────────
// 后端 console 口径：kbps = KB/s，0 = 不限速；时段支持跨午夜；
// 保存时后端会 normalize（丢废条、按开始时间排序、最多 8 条）。
const planRows = computed(() => Array.isArray(form.rate_plan) ? form.rate_plan : [])
// V0.0.36：结束时间支持 24:00（= 当天结束，后端口径一直如此，桌面端也是
// 这样），但网页用的 <input type="time"> 语法上没有 24:00 —— 用户填的
// 08:00–24:00 显示成空框，看起来就是「时段表又不对了」。改用下拉：
// 30 分钟一档、到 24:00（午夜）为止；历史数据里非整点的值自动补进选项。
const planTimes = computed(() => {
  const out = []
  for (let m = 0; m <= 24 * 60; m += 30) {
    const h = String(Math.floor(m / 60)).padStart(2, '0')
    const mi = String(m % 60).padStart(2, '0')
    out.push(`${h}:${mi}`)
  }
  return out
})
const planDirty = ref(false)
// V0.0.41：后端给的结构化状态（现在几点 / 现在怎么限 / 下次什么时候变）。
// 界面不再自己拼「当前：不限速」这种容易误解的句子。
const planStatus = computed(() => s.value.rate_plan_status || null)

function planEdit(i, key, val) {
  const rows = JSON.parse(JSON.stringify(planRows.value || []))
  if (!rows[i]) return
  if (key === 'kbps') rows[i][key] = Math.max(0, Math.round(Number(val) || 0))
  else rows[i][key] = String(val || '')
  form.rate_plan = rows
  planDirty.value = true
}

function planAdd() {
  const rows = JSON.parse(JSON.stringify(planRows.value || []))
  if (rows.length >= 8) { info.value = '最多 8 条时段'; setTimeout(() => (info.value = ''), 2000); return }
  rows.push({ start: '00:00', end: '08:00', kbps: 0 })
  form.rate_plan = rows
  planDirty.value = true
}

function planDel(i) {
  const rows = JSON.parse(JSON.stringify(planRows.value || []))
  rows.splice(i, 1)
  form.rate_plan = rows
  planDirty.value = true
}

async function planSave() {
  try {
    await api.saveSettings({ rate_plan: planRows.value || [] })
    info.value = '时段表已保存'
    setTimeout(() => (info.value = ''), 2600)
    planDirty.value = false
    await load()
  } catch (e) { err.value = e.message }
}

// V0.0.36：开关就放在时段表旁边。以前开关在另一组设置里，用户配完表
// 以为已经生效，结果「限速计划已关闭」，看起来又是不对。
async function planToggleEnabled(on) {
  try {
    await save({ rate_plan_enabled: !!on })
  } catch (e) { err.value = e.message }
}

// ── 代理池 ──────────────────────────────────────────────────────────
const newProxy = reactive({ name: '', scheme: 'socks5', host: '', port: 1080,
                            user: '', pass: '', priority: 0 })

async function toggleProxy(p) {
  try { await api.proxySave({ id: p.id, enabled: !p.enabled }); await loadProxies() }
  catch (e) { err.value = e.message }
}
async function delProxy(p) {
  if (!confirm(`删除代理「${p.name}」？`)) return
  try { await api.proxyDelete(p.id); await loadProxies() } catch (e) { err.value = e.message }
}
async function addProxy() {
  if (!newProxy.host) { err.value = '请填写主机地址'; return }
  try {
    // 后端只认 url（可选带 user:pass@）+ name/enabled/priority
    const auth = newProxy.user
      ? `${encodeURIComponent(newProxy.user)}:${encodeURIComponent(newProxy.pass || '')}@`
      : ''
    const url = `${newProxy.scheme}://${auth}${newProxy.host}:${Number(newProxy.port) || 1080}`
    await api.proxySave({ name: newProxy.name || newProxy.host, url,
                          priority: Number(newProxy.priority) || 0,
                          enabled: true })
    Object.assign(newProxy, { name: '', scheme: 'socks5', host: '', port: 1080,
                              user: '', pass: '', priority: 0 })
    await loadProxies()
  } catch (e) { err.value = e.message }
}

// ── Web 访问（密码特殊处理：不回显，留空不改）────────────────────────
async function clearPassword() {
  if (!confirm('确认清除登录密码？清除后本机及同网段访问将无需密码。')) return
  try {
    await api.saveSettings({ web_password: '' })
    info.value = '密码已清除（当前为免密访问）'
    setTimeout(() => (info.value = ''), 3500)
    await load()
  } catch (e) { err.value = e.message }
}

async function savePassword(pw) {
  if (!pw) { info.value = '密码未变更'; setTimeout(() => (info.value = ''), 2000); return }
  try {
    await api.saveSettings({ web_password: pw })
    info.value = '密码已更新（立即生效）'
    setTimeout(() => (info.value = ''), 3000)
    await load()
  } catch (e) { err.value = e.message }
}

// ── 在线模型（#5：从 OpenAI 兼容接口拉模型清单）──────────────────
const onlineModels = ref([])
const onlineBusy = ref(false)
const onlineErr = ref('')
async function fetchOnlineModels() {
  onlineBusy.value = true
  onlineErr.value = ''
  try {
    const d = await api.post('/api/ai/online/models',
                             { url: form.ai_api_url || '', key: '' })
    if (!d || d.ok === false) {
      onlineModels.value = []
      onlineErr.value = (d && d.error) || '拉取失败'
      return
    }
    onlineModels.value = d.items || []
    if (!onlineModels.value.length) onlineErr.value = '接口返回了 0 个模型'
  } catch (e) {
    onlineErr.value = e.message
  } finally {
    onlineBusy.value = false
  }
}

// ── 维护 ────────────────────────────────────────────────────────────
async function doBackup() {
  try { info.value = '正在备份…'; await api.backupNow(); info.value = '备份完成'; await loadBackup() }
  catch (e) { err.value = e.message } finally { setTimeout(() => (info.value = ''), 3000) }
}
// plan 必须是**对象**（后端白名单按对象键取值），且中文提示要说清后果：
// 清空隔离标记 = 图回到图库；清空审核记录 = 学习器的输入没了（不可撤销）。
async function doCleanup(plan) {
  const label = plan.quarantine ? '清空隔离标记（这些图会回到图库）'
    : plan.reviews ? '清空审核记录（不可撤销，AI 学习会失去这批输入）'
      : Object.keys(plan).join('、')
  if (!confirm('确认' + label + '？')) return
  try {
    const r = await api.cleanup(plan)
    info.value = '已清理：' + (r.done || []).join('、')
    setTimeout(() => (info.value = ''), 4000)
  } catch (e) { err.value = e.message }
}
async function resetLearn() {
  if (!confirm('确认清空全部学习样本？此操作不可撤销。')) return
  try { await api.learnReset(); await loadLearn(); info.value = '学习样本已重置' }
  catch (e) { err.value = e.message }
}
async function toggleLearn() {
  if (!learn.value) return
  try { await api.learnEnabled(!learn.value.enabled); await loadLearn() }
  catch (e) { err.value = e.message }
}
function toggleDaily(e) {
  api.backupDaily(e.target.checked).then(loadBackup).catch((x) => (err.value = x.message))
}

// ── 整库备份 ────────────────────────────────────────────────────────
async function loadFull() {
  try {
    const r = await api.fullList()
    fullBk.value = r.items || []
    fullTotal.value = r.total_text || ''
  } catch { /* 清单拉不到就不显示 */ }
}

// 页面切走时必须停轮询：定时器不会随组件卸载自动清掉，
// 留着就是「离开设置页后每秒还在打一次请求」。
onUnmounted(() => {
  if (fullTimer) { clearInterval(fullTimer); fullTimer = null }
  // R248：离开设置页必须停掉导入轮询，否则组件销毁后还在打接口
})

function stopFullTimer() {
  if (fullTimer) { clearInterval(fullTimer); fullTimer = null }
}

/** 轮询作业进度。onDone 拿到 result 后由调用方决定刷新什么。 */
function pollJob(jid, onDone) {
  stopFullTimer()
  let bad = 0
  fullTimer = setInterval(async () => {
    try {
      const r = await api.fullJob(jid)
      bad = 0
      fullPct.value = Number(r.pct || 0)
      fullMsg.value = r.message || ''
      if (r.state === 'done') {
        stopFullTimer(); fullBusy.value = ''
        fullPct.value = 100
        onDone(r.result || {})
      } else if (r.state === 'failed') {
        stopFullTimer(); fullBusy.value = ''
        err.value = r.error || '作业失败'
        fullMsg.value = ''
        setTimeout(() => (err.value = ''), 8000)
      }
    } catch (e) {
      // 偶发一次网络抖动不算失败；连错 5 次才认（作业仍在服务端跑）
      bad += 1
      if (bad >= 5) {
        stopFullTimer(); fullBusy.value = ''
        err.value = '进度读取中断：' + e.message + '（作业可能仍在后台执行，稍后刷新清单查看）'
      }
    }
  }, 1000)
}

async function startFullExport() {
  err.value = ''; info.value = ''
  try {
    fullBusy.value = 'export'; fullPct.value = 0; fullMsg.value = '准备中…'
    const r = await api.fullExport(fullIncludeThumbs.value)
    pollJob(r.job, async (res) => {
      info.value = `整库备份已生成：${res.name}（${Math.round((res.bytes || 0) / 1048576)} MB）`
      await loadFull()
      setTimeout(() => (info.value = ''), 6000)
    })
  } catch (e) {
    fullBusy.value = ''; err.value = e.message
  }
}

function pickZip() { zipInput.value?.click() }

async function doUpload(e) {
  const f = e.target.files && e.target.files[0]
  e.target.value = ''
  if (!f) return
  if (!/\.zip$/i.test(f.name)) { err.value = '请选择 .zip 格式的完整备份包'; return }
  err.value = ''
  try {
    fullBusy.value = 'upload'; fullPct.value = 0
    fullMsg.value = `正在上传 ${f.name}…`
    const r = await api.fullUpload(f)
    info.value = `已上传：${r.name}（${Math.round((r.bytes || 0) / 1048576)} MB），可在下方列表中恢复`
    await loadFull()
    setTimeout(() => (info.value = ''), 6000)
  } catch (x) {
    err.value = x.message
  } finally {
    fullBusy.value = ''; fullMsg.value = ''
  }
}

async function doRestore(name) {
  // 两次确认：这个动作会覆盖当前库/规则/配置，一旦选错包，损失是真金白银
  if (!confirm(`从「${name}」恢复？\n\n这会用备份内容覆盖当前的数据库、规则与配置，`
    + `当前缩略图会被同名文件覆盖。\n建议保持「恢复前自动备份」为开，以便事后回退。`)) return
  err.value = ''; info.value = ''
  try {
    fullBusy.value = 'restore'; fullPct.value = 0; fullMsg.value = '准备中…'
    const r = await api.fullRestore(name, fullSafety.value)
    pollJob(r.job, async (res) => {
      const c = res.counts || {}
      info.value = `恢复完成：任务 ${c.tasks || 0} 个、图片 ${c.images || 0} 张、`
        + `规则 ${c.rules || 0} 条`
        + (res.safety_backup ? `（恢复前状态已存为 ${res.safety_backup}）` : '')
      if (res.warning) err.value = res.warning
      await loadFull()
      // 配置/库都换了，整个页面重新拉一遍（否则界面上还是旧值）
      await load()
      setTimeout(() => (info.value = ''), 10000)
    })
  } catch (e) {
    fullBusy.value = ''; err.value = e.message
  }
}

async function delFull(name) {
  if (!confirm(`删除备份「${name}」？\n\n只删除这一个备份包，不影响图库。`)) return
  try {
    await api.fullDelete(name)
    await loadFull()
  } catch (e) { err.value = e.message }
}

function pickPrefs() {
  prefsInput.value?.click()
}
async function doImportPrefs(e) {
  const f = e.target.files && e.target.files[0]
  e.target.value = ''            // 同一个文件再选一次也要触发 change
  if (!f) return
  importing.value = true
  try {
    const text = await f.text()
    let payload
    try { payload = JSON.parse(text) } catch { throw new Error('这个文件不是合法的 JSON') }
    const r = await api.prefsImport(payload)
    const skipped = Number(r.skipped || 0)
    info.value = `已导入 ${r.merged || 0} 条偏好` + (skipped ? `，跳过 ${skipped} 条` : '')
    await loadLearn()
    setTimeout(() => (info.value = ''), 4000)
  } catch (x) {
    err.value = x.message || '导入失败'
  } finally {
    importing.value = false
  }
}

onMounted(() => {
  load()
  // V0.0.18：支持从别处（报表页 Cookie 提示条）带 ?tab= 直达对应分页
  try {
    const want = (route.query && route.query.tab) ? String(route.query.tab) : ''
    if (want && TABS.some((t) => t.k === want)) tab.value = want
  } catch { /* 忽略：读不到就留在默认页 */ }
  // V0.0.23：从别的页面再点进来时组件会被复用（不会重新 mount），
  // 只有 onMounted 读 route.query 会导致「?tab=store 却停在第一页」。
  watch(() => (route.query && route.query.tab) || '', (v) => {
    const w = String(v || '')
    if (w && TABS.some((t) => t.k === w)) tab.value = w
  })
  // 有未保存草稿时关窗口/刷新要拦一下（第 8 条：草稿不落盘）
  window.addEventListener('beforeunload', onBeforeUnload)
  // V0.0.21：Ctrl+S 保存本页已修改的设置，不弹浏览器的「另存为」。
  unbindShell = onShellSave(() => { if (pageDirtyKeys.value.length) savePage() })
})
onUnmounted(() => {
  window.removeEventListener('beforeunload', onBeforeUnload)
  if (unbindShell) unbindShell()
})
</script>

<template>
  <div class="page">
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="info" class="ok">{{ info }}</p>

    <!-- 搜索：60 项平铺太难找 -->
    <div class="card searchbar">
      <input v-model="search" type="text" placeholder="搜索设置项（如 端口 / 间隔 / 阈值 / 备份）" />
      <span class="muted small" v-if="search">命中 {{ hitCount }} 项</span>
      <button v-if="search" class="btn sm ghost" @click="search = ''"><Icon name="eraser" :size="13" />清空</button>
      <span class="muted small" v-else>共 {{ writableCount }} 项设置，改完点右下角保存</span>
    </div>

    <!-- 2026-10-29（第 8 条）：分页。搜索时不显示（搜索跨页） -->
    <div class="tabs card" v-if="!searching">
      <button v-for="t in TABS" :key="t.k" class="tab" :class="{ on: tab === t.k }"
              @click="tab = t.k">
        <Icon :name="t.icon" :size="13" />
        <span>{{ t.label }}</span>
        <i v-if="tabDirty(t.k)" class="tabdot" title="本页有未保存的修改"></i>
      </button>
    </div>
    <p v-else class="muted tiny" style="margin:-4px 0 0">
      搜索结果跨全部页面显示；保存按钮只提交下面这些命中项。
    </p>

    <!-- 通用分组：全部简单项 -->
    
      <!-- 2026-10-27（编号 18/19）：限速时段表。结构化数据，单独一块。 -->
      <!-- V0.0.43：这一块原来少了 sec 这个类 ⇒ 卡片 padding 是 0，标题离左边框
           只有 1px、表格行贴着边框画，跟同页其他卡片（内边距 18px）一比就像
           「从框里顶出来」。用户连着几版说的「超出边框」就是这个。 -->
      <section class="card sec plancard" v-if="onTab('net') && !search.trim()">
        <h3>限速时段表 <span class="tiny muted">（{{ planRows.length }}/8 条）</span></h3>
        <p class="tiny muted">
          每个时段设置一个带宽上限（KB/s，0 表示不限速）；未命中任何时段时表示不限速。
          时段可跨午夜（如 22:00–06:00）；同时配置「全局上限」时取较严格者。
        </p>
        <!-- V0.0.41：状态条。用户上一版的真实困惑：表配好了、开关也开了，
             但那时正是 00:00–08:00 的不限速时段，界面只写「当前：不限速」，
             看起来就像"又没生效"。现在把现在时间与下次切换一起写清楚。 -->
        <div v-if="planStatus" class="planstate"
             :class="{ limited: planStatus.now_kbps > 0, off: !planStatus.enabled }">
          <Icon name="gauge" :size="14" />
          <b>{{ planStatus.now_text }}</b>
          <span v-if="planStatus.next_text" class="muted">· {{ planStatus.next_text }}</span>
        </div>
        <!-- V0.0.36：开关与表格放在一起，且「配了表没开开关」要一眼看得见
             V0.0.38：提醒条上直接给一个「立即启用」按钮 —— 光说「没打开」，
             用户还得自己去找那个勾，等于让用户替我们做排障。 -->
        <div v-if="planRows.length && !s.rate_plan_enabled"
             class="warn-note warn-row">
          <span>时段表已配置，但「启用分时段限速」没有打开 —— 现在仍是
            <b>不限速</b>。</span>
          <button class="btn sm primary" @click="planToggleEnabled(true)">
            <Icon name="check" :size="13" />立即启用</button>
        </div>
        <!-- V0.0.42：真表格，铺满卡片。上一版是「每行一张固定 640px 的小卡片」，
             在 818px 的卡片里右边空出 177px；行内还自带一排小标题，跟表头重复，
             删除键是块大红底，看着像从边框里顶出来 —— 用户连着两次说这块不对。
             现在：一行一条时段，列宽固定、说明列吃掉剩余宽度，表头只出现一次，
             删除键改成低干扰的文字键（红字、无底色），跟右侧列对齐。 -->
        <div class="plantable" v-if="planRows.length">
          <div class="planhead">
            <span>开始</span><span></span><span>结束</span>
            <span>上限（KB/s）</span><span>说明</span><span></span>
          </div>
          <div v-for="(row, i) in planRows" :key="i" class="planrow">
            <select class="pcell" :value="row.start" title="开始时间"
                    :aria-label="'第 ' + (i + 1) + ' 条时段开始时间'"
                    @change="planEdit(i, 'start', $event.target.value)">
              <option v-if="!planTimes.includes(row.start)" :value="row.start">{{ row.start }}</option>
              <option v-for="t in planTimes.filter(t => t !== '24:00')" :key="t" :value="t">{{ t }}</option>
            </select>
            <span class="pto">至</span>
            <select class="pcell" :value="row.end" title="结束时间"
                    :aria-label="'第 ' + (i + 1) + ' 条时段结束时间'"
                    @change="planEdit(i, 'end', $event.target.value)">
              <option v-if="!planTimes.includes(row.end)" :value="row.end">{{ row.end }}</option>
              <option v-for="t in planTimes" :key="t" :value="t">{{ t === '24:00' ? '24:00（午夜）' : t }}</option>
            </select>
            <input class="pcell" type="number" min="0" max="100000" :value="row.kbps"
                   title="带宽上限，0 表示不限速"
                   :aria-label="'第 ' + (i + 1) + ' 条时段上限'"
                   @change="planEdit(i, 'kbps', $event.target.value)" />
            <span class="phint" :class="{ on: Number(row.kbps) > 0 }">
              {{ Number(row.kbps) > 0 ? '限制 ' + row.kbps + ' KB/s' : '本时段不限速' }}</span>
            <button class="btn sm danger-t" :title="'删除第 ' + (i + 1) + ' 条时段'"
                    @click="planDel(i)">
              <Icon name="trash" :size="13" />删除</button>
          </div>
        </div>
        <p v-else class="tiny muted">尚未配置时段，等同不限速。</p>
        <div class="row-actions">
          <!-- 2026-10-29（第 6 条同类）：图标已经是加号，文案里别再写「+」 -->
          <label class="chk small" title="打开后按时段表限速；关着就是不限速">
            <input type="checkbox" :checked="!!s.rate_plan_enabled"
                   @change="planToggleEnabled($event.target.checked)" />启用分时段限速</label>
          <button class="btn sm" @click="planAdd"><Icon name="plus" :size="13" />添加时段</button>
          <button class="btn sm primary" :disabled="!planDirty" @click="planSave"><Icon name="save" :size="13" />保存时段表</button>
          <span class="tiny muted" v-if="planDirty">存在未保存的修改</span>
        </div>
      </section>

    <!-- V0.0.55 批1：定时自动关机。安排/取消都是「动作」，即时调接口，
         不进草稿；每日定点由后端落盘、重启自动续，其余只在本机这一次生效。 -->
    <section class="card sec" v-show="onTab('ui') && !search.trim()">
      <h3>定时自动关机</h3>
      <div class="row">
        <label>关机方式</label>
        <div class="inline">
          <select :value="pdMode" @change="pdSetMode($event.target.value)">
            <option value="">不自动关机</option>
            <option value="on_idle">任务全部跑完后关机</option>
            <option value="daily">每日定点关机</option>
          </select>
          <span class="muted small">关机前均有 {{ pdCountdown }} 秒倒计时，右下角通知里可点「取消」</span>
        </div>
      </div>
      <div class="row" v-if="pdMode === 'on_idle'">
        <label>空闲判定（分钟）</label>
        <div class="inline">
          <input class="num" type="number" min="1" max="120" v-model.number="pdGraceMin" />
          <span class="muted small">队列连续空闲达到该时长才动手，防止任务间隙被误判为跑完</span>
        </div>
      </div>
      <div class="row" v-if="pdMode === 'daily'">
        <label>每日时刻</label>
        <div class="inline">
          <input type="time" v-model="pdAt" @change="pdApply()" />
          <span class="muted small">到点关机并每天重复；软件重启后自动续上</span>
        </div>
      </div>
      <div class="row" v-if="pdMode === 'on_idle'">
        <label></label>
        <div class="inline">
          <button class="btn sm primary" :disabled="pdBusy" @click="pdApply()">
            <Icon name="check" :size="13" />{{ pdBusy ? '安排中…' : '确认安排' }}</button>
          <span class="muted small">只对这次开机期间生效；期间可随时取消</span>
        </div>
      </div>
      <div class="row" v-if="pdPlan.mode">
        <label>当前计划</label>
        <div class="inline">
          <span class="ok small">{{ pdPlanText }}</span>
          <button class="btn sm danger" @click="pdCancel()">
            <Icon name="x" :size="13" />取消计划</button>
        </div>
      </div>
    </section>

<section v-for="sec in pageSections" :key="sec.title" class="card sec">
      <h3>{{ sec.title }}</h3>
      <div v-for="it in sec.items" :key="it.k" class="row" :class="{ col: it.full }">
        <label :title="it.k">{{ it.label }}</label>

        <!-- 布尔 -->
        <div v-if="it.type === 'bool'" class="inline">
          <input type="checkbox" :checked="!!form[it.k]" :disabled="!can(it.k)"
                 @change="onField(it, $event)" />
          <span class="muted small">{{ it.hint }}</span>
        </div>

        <!-- 数字 -->
        <div v-else-if="it.type === 'int' || it.type === 'float' || it.type === 'kb'" class="inline">
          <input class="num" type="number" :value="displayValue(it)"
                 :min="it.min" :max="it.max" :step="it.step || (it.type === 'float' ? 0.01 : 1)"
                 :disabled="!can(it.k)" @change="onField(it, $event)" />
          <span class="muted small">{{ it.unit || (it.type === 'kb' ? 'KB' : '') }}</span>
          <span class="muted small">{{ it.hint }}</span>
        </div>

        <!-- 下拉 -->
        <div v-else-if="it.type === 'select'" class="inline">
          <select :value="form[it.k]" :disabled="!can(it.k)" @change="onField(it, $event)">
            <option v-for="o in it.options" :key="o.v" :value="o.v">{{ o.t }}</option>
          </select>
          <span class="muted small">{{ it.hint }}</span>
        </div>

        <!-- 2026-10-28（编号 32）：源地址下拉 —— 选项来自后端 /api/settings 的
             _local_ips（真实网卡地址），不让用户手打 IP。 -->
        <div v-else-if="it.type === 'select-ip'" class="inline">
          <select :value="form[it.k] || ''" :disabled="!can(it.k)" @change="onField(it, $event)">
            <option value="">自动（系统选网卡）</option>
            <option v-for="ip in (s._local_ips || [])" :key="ip" :value="ip">{{ ip }}</option>
          </select>
          <span class="muted small">{{ it.hint }}</span>
        </div>

        <!-- 皮肤（选项来自后端 _presets，前端不写死） -->
        <div v-else-if="it.type === 'preset'" class="inline">
          <select :value="form.ui_preset" :disabled="!can('ui_preset')"
                  @change="onField(it, $event)">
            <option v-for="p in (s._presets || [])" :key="p.key" :value="p.key">{{ p.label }}</option>
          </select>
          <span class="muted small">{{ it.hint }}</span>
        </div>

        <!-- 密码（不回显） -->
        <div v-else-if="it.type === 'password'" class="inline">
          <input type="password" :placeholder="form[it.k] ? '（已设置，留空则保持不变）' : '（未设置）'"
                 :disabled="!can(it.k)" @change="onField(it, $event)" />
          <span class="muted small">{{ it.hint }}</span>
        </div>

        <!-- 多行文本（IP 过滤规则等）：一行一条，必须用 textarea -->
        <div v-else-if="it.type === 'textarea'" class="inline" style="align-items:flex-start">
          <textarea rows="5" :value="displayValue(it)" :disabled="!can(it.k)"
                    @change="onField(it, $event)"></textarea>
          <span class="muted small">{{ it.hint }}</span>
        </div>

        <!-- 文本 / 列表 -->
        <div v-else class="inline">
          <input type="text" :value="displayValue(it)" :disabled="!can(it.k)"
                 @change="onField(it, $event)" />
          <!-- 2026-10-29（新需求）：目录类项给「浏览」。浏览器拿不到本机路径，
               所以走服务端列目录 —— 本机打开就是本机磁盘，Docker/NAS 打开
               就是容器里挂载的卷，界面上会标出是哪台机器。 -->
          <button v-if="it.dir" class="btn sm" type="button"
                  :disabled="!can(it.k)" title="浏览运行软件那台机器上的目录"
                  @click="pickSettingDir(it)">
            <Icon name="folder" :size="13" />浏览
          </button>
          <span class="muted small">{{ it.hint }}</span>
        </div>
      </div>
    </section>

    <!-- V0.0.17：界面辅助（自定义皮肤 / 快捷键说明 / 新手引导） -->
    <section class="card sec" v-show="onTab('ui') && !search.trim()">
      <h3>界面辅助</h3>
      <div class="inline">
        <!-- V0.0.23：说明与改键彻底分开 —— 说明只读，改键单独一个入口 -->
        <button class="btn sm" @click="openShortcuts"><Icon name="key-round" :size="13" />查看快捷键说明</button>
        <button class="btn sm" @click="openShortcutsEdit"><Icon name="pencil" :size="13" />自定义快捷键</button>
        <button class="btn sm" @click="rerunOnboarding"><Icon name="sparkles" :size="13" />重新运行新手引导</button>
      </div>
      <p class="muted small">
        「查看快捷键说明」仅列出当前生效的键位，不可修改；修改键位请选择「自定义快捷键」。
        按 ? 或 F1 打开的是查看窗口。
      </p>
      <!-- V0.0.27：换皮肤只剩左下角一处，这里只说明 + 管样式文件 -->
      <div class="row">
        <label>界面皮肤</label>
        <div class="inline">
          <span class="pill">当前：{{ themeNow }}</span>
          <span class="muted small">
            换皮肤用<b>左下角</b>那个皮肤下拉（内建皮肤与上传的皮肤都在里面），
            全软件只有这一处；本页只负责上传自定义样式文件。
          </span>
        </div>
      </div>
      <!-- 列出已有自定义皮肤并切换（web_theme 持久化），上传 CSS 由后端校验后入库 -->
      <SkinManager />
    </section>

    <p v-if="search && !hitCount" class="muted" style="padding:8px 2px">
      未找到匹配「{{ search }}」的设置项。请更换检索词，或选择「清空」查看全部。
    </p>

    <!-- V0.0.23：用户问「主题词里的 AI 与上方某些功能，是不是与 AI 模型相冲突」——
         这两块管的事完全不同，先把分工写清楚，再放学习开关（从存储页搬来）。 -->
    <section class="card sec" v-show="onTab('ai') && !search.trim()">
      <h3>这三块是什么关系</h3>
      <ul class="small rel">
        <li><b>主题词</b>（本页）：定义收录与排除条件，属于筛选标准，
          不消耗算力，改完下一个任务生效。</li>
        <li><b>AI 与审核</b>（本页上方）：决定「要不要让 AI 参与判断、门槛多高」，
          用的是下面这条学习到的偏好。</li>
        <li><b>AI 模型</b>（左侧「AI 模型」页）：决定「用哪个模型、跑在 CPU 还是显卡」，
          仅决定计算资源，不参与筛选标准的定义。</li>
      </ul>
      <p class="muted small">
        说明：主题词与审核门槛决定图像的收录范围，AI 模型决定推理所用设备，两者互不冲突，
        也可以完全不用 AI 模型（只用主题词）。
      </p>
      <div class="row" v-if="learn">
        <label>偏好学习</label>
        <div class="inline">
          <input type="checkbox" :checked="!!learn.enabled" @change="toggleLearn" />
          <span class="muted">启用偏好学习（从我的收藏与判退缩小范围）</span>
          <span class="muted small">已记录 {{ learn.samples || 0 }} 条判定</span>
          <button class="btn sm danger" @click="resetLearn">
            <Icon name="eraser" :size="13" />清空学习记录</button>
        </div>
      </div>
      <!-- V0.0.56：影子模式。feedback_auto_harden 默认 false = 影子（只排序/送审核，
           不自动拦截）。这里用「自动拦截」正向开关绑同一字段，避免取反造成语义错位。 -->
      <div class="row">
        <label>回避词自动拦截</label>
        <div class="inline">
          <input type="checkbox" id="f-auto-harden" :checked="!!form.feedback_auto_harden"
                 @change="onField({ k: 'feedback_auto_harden', type: 'bool' }, $event)" />
          <span class="muted">默认关闭＝影子模式：判退学到的词只影响排序与送审核，不自动拦截下载（推荐）。勾选后恢复「累计判退达标自动硬拦」。</span>
        </div>
      </div>
    </section>

    <!-- 在线模型：从接口拉清单直接选（#5）-->
    <section class="card sec" v-show="onTab('ai') && !search.trim()">
      <h3>在线模型（OpenAI 兼容）</h3>
      <p class="muted small">
        先在上面「AI 与审核」里填好<b>在线 API 地址</b>和 <b>Key</b>，这里可以直接
        从接口拉模型清单来选，不用手打模型名。
      </p>
      <div class="inline">
        <button class="btn sm" :disabled="onlineBusy" @click="fetchOnlineModels">
          {{ onlineBusy ? '正在拉取…' : '从接口拉取模型列表' }}
        </button>
        <select v-if="onlineModels.length" :value="form.ai_api_model || ''"
                @change="onField({ k: 'ai_api_model', type: 'text' }, $event)">
          <option value="">（用接口默认模型）</option>
          <option v-for="m in onlineModels" :key="m" :value="m">{{ m }}</option>
        </select>
        <span class="muted small" v-if="onlineModels.length">
          共 {{ onlineModels.length }} 个；当前：{{ form.ai_api_model || '接口默认' }}（选择后按「保存本页」生效）
        </span>
        <span class="err small" v-if="onlineErr">{{ onlineErr }}</span>
      </div>
    </section>

    <!-- Web 访问（V0.0.51：常用四项摊开，高级项按需展开）-->
    <section class="card sec" v-show="onTab('web') && !search.trim()">
      <h3>Web 访问 <span class="muted small">（界面本身即为本地网页，无需单独开启；此处配置访问范围）</span></h3>
      <p class="muted tiny" style="margin-top:-6px">本页改动即时保存；端口与监听地址需重启软件生效。</p>
      <div class="f2">
        <div class="row col"><label>允许局域网访问</label>
          <div class="inline">
            <input type="checkbox" :checked="lanOpen"
                   :disabled="!can('web_bind')"
                   @change="save({ web_bind: $event.target.checked ? '0.0.0.0' : '127.0.0.1' })" />
            <span class="muted small">{{ lanOpen ? '同一网络的手机、平板已能打开本软件' : '仅本机可用，其他设备打不开' }}</span>
          </div>
          <span class="tiny muted">改完需重启软件生效</span></div>
        <div class="row col"><label>端口</label>
          <input type="number" :value="form.web_port || 8080"
                 :disabled="!can('web_port')"
                 @change="save({ web_port: Number($event.target.value) })" />
          <span class="tiny muted">改完需重启软件生效</span></div>
      </div>
      <div class="f2">
        <div class="row col"><label>登录密码</label>
          <input type="password" :placeholder="form.web_password_set ? '（已设置，留空则保持不变）' : '（未设置，留空表示免密）'"
                 :disabled="!can('web_password')"
                 @change="savePassword($event.target.value)" />
          <span class="tiny muted">{{ form.web_password_set ? '当前已设置密码' : '当前为免密访问' }}；留空表示不修改</span></div>
        <div class="row col"><label>局域网免密</label>
          <div class="inline">
            <input type="checkbox" :checked="!!form.web_lan_open" :disabled="!can('web_lan_open')"
                   @change="save({ web_lan_open: $event.target.checked })" />
            <span class="muted small">同网段设备无需密码（仅限内网使用）</span>
          </div>
          <span class="tiny muted">家里自己用可开；放公网环境请关掉并设密码</span></div>
      </div>
      <div class="row">
        <label></label>
        <div class="inline">
          <button class="btn sm danger" :disabled="!form.web_password_set || !can('web_password')"
                  @click="clearPassword"><Icon name="eraser" :size="13" />清除密码（改为免密）</button>
        </div>
      </div>
      <button class="btn sm ghost adv-toggle" @click="webAdvanced = !webAdvanced">
        <Icon :name="webAdvanced ? 'chevron-up' : 'chevron-down'" :size="13" />
        {{ webAdvanced ? '收起高级设置' : '高级设置（监听地址、白名单、HTTPS、安全策略…）' }}
      </button>

      <div v-if="webAdvanced" class="web-advanced">
        <div class="f2">
          <div class="row col"><label>监听地址（高级）</label>
            <div class="inline">
              <input type="text" :value="form.web_bind || '127.0.0.1'"
                     :disabled="!can('web_bind')"
                     @change="save({ web_bind: $event.target.value })" />
            </div>
            <span class="tiny muted">多网卡机器可指定具体 IP；一般不用改，用上面的开关即可</span></div>
          <div class="row col"><label>仅本机免密</label>
            <div class="inline">
              <input type="checkbox" :checked="!!form.web_localhost_free"
                     :disabled="!can('web_localhost_free')"
                     @change="save({ web_localhost_free: $event.target.checked })" />
              <span class="tiny muted">127.0.0.1 免密；外部访问仍需密码</span>
            </div></div>
        </div>
        <div class="f2">
          <div class="row col"><label>白名单内免密</label>
            <div class="inline">
              <input type="checkbox" :checked="form.web_whitelist_nopass !== false"
                     :disabled="!can('web_whitelist_nopass')"
                     @change="save({ web_whitelist_nopass: $event.target.checked })" />
              <span class="tiny muted">关闭后白名单仅表示「允许访问」，仍需密码</span>
            </div></div>
          <div class="row col"><label>IP 白名单</label>
            <input type="text"
                   :value="Array.isArray(form.web_ip_whitelist) ? form.web_ip_whitelist.join(', ') : (form.web_ip_whitelist || '')"
                   placeholder="逗号分隔，例如 192.168.1.0/24"
                   :disabled="!can('web_ip_whitelist')"
                   @change="save({ web_ip_whitelist: $event.target.value })" /></div>
        </div>
        <div class="f2">
          <div class="row col"><label>登录失败锁定阈值</label>
            <input type="number" min="0" max="50" :value="form.web_login_max_fail ?? 5"
                   :disabled="!can('web_login_max_fail')"
                   @change="save({ web_login_max_fail: Number($event.target.value) })" />
            <span class="tiny muted">连续失败达到该次数后锁定该 IP（0 表示不锁定）</span></div>
          <div class="row col"><label>锁定时长（分钟）</label>
            <input type="number" min="1" max="1440" :value="form.web_login_ban_min ?? 10"
                   :disabled="!can('web_login_ban_min')"
                   @change="save({ web_login_ban_min: Number($event.target.value) })" />
            <span class="tiny muted">重复失败时锁定时长倍增，上限 15 分钟</span></div>
        </div>
        <div class="row">
          <label>会话有效期（小时）</label>
          <div class="inline">
            <input class="num" type="number" min="1" max="720" :value="form.web_session_hours ?? 12"
                   :disabled="!can('web_session_hours')"
                   @change="save({ web_session_hours: Number($event.target.value) })" />
            <span class="tiny muted">超过后需重新登录</span>
          </div>
        </div>
        <div class="row">
          <label>安全加固</label>
          <div class="inline" style="flex-wrap:wrap;gap:12px">
            <label class="tiny"><input type="checkbox" :checked="form.web_frame_deny !== false"
                     :disabled="!can('web_frame_deny')"
                     @change="save({ web_frame_deny: $event.target.checked })" /> 禁止被网页嵌入</label>
            <label class="tiny"><input type="checkbox" :checked="form.web_host_check !== false"
                     :disabled="!can('web_host_check')"
                     @change="save({ web_host_check: $event.target.checked })" /> 校验 Host 头</label>
            <label class="tiny"><input type="checkbox" :checked="form.web_csrf !== false"
                     :disabled="!can('web_csrf')"
                     @change="save({ web_csrf: $event.target.checked })" /> 校验请求来源（CSRF）</label>
          </div>
        </div>
        <div class="row">
          <label>信任反向代理</label>
          <div class="inline">
            <input type="checkbox" :checked="!!form.trust_proxy" :disabled="!can('trust_proxy')"
                   @change="save({ trust_proxy: $event.target.checked })" />
            <span class="muted small">部署在反向代理之后时启用，从 X-Forwarded-For 读取真实 IP</span>
          </div>
        </div>
        <div class="row col">
          <label>自定义响应头</label>
          <textarea rows="3" :value="form.web_extra_headers || ''" :disabled="!can('web_extra_headers')"
                    placeholder="一行一个，例如：&#10;X-Robots-Tag: noindex"
                    @change="save({ web_extra_headers: $event.target.value })"></textarea>
          <span class="tiny muted">内容协商类响应头（Content-Type 等）将被忽略，避免页面渲染异常</span>
        </div>
        <h4 class="adv-h4">HTTPS 加密访问</h4>
        <div class="f2">
          <div class="row col"><label>启用 HTTPS</label>
            <div class="inline">
              <input type="checkbox" :checked="!!form.web_https" :disabled="!can('web_https')"
                     @change="save({ web_https: $event.target.checked })" />
              <span class="tiny muted">自动生成自签名证书（有效期 10 年）；改完需重启软件</span>
            </div></div>
          <div class="row col"><label>HTTPS 端口</label>
            <input type="number" :value="form.web_https_port || 8443"
                   :disabled="!can('web_https_port')"
                   @change="save({ web_https_port: Number($event.target.value) })" />
            <span class="tiny muted">与 HTTP 端口分开，HTTP 照常可用</span></div>
        </div>
        <div class="f2">
          <div class="row col"><label>强制走 HTTPS</label>
            <div class="inline">
              <input type="checkbox" :checked="!!form.web_https_redirect"
                     :disabled="!can('web_https_redirect')"
                     @change="save({ web_https_redirect: $event.target.checked })" />
              <span class="tiny muted">HTTP 请求自动跳转；手机浏览器会提示证书不受信任</span>
            </div></div>
          <div class="row col"><label>会话 Cookie 加 Secure</label>
            <div class="inline">
              <input type="checkbox" :checked="!!form.web_secure_cookie"
                     :disabled="!can('web_secure_cookie')"
                     @change="save({ web_secure_cookie: $event.target.checked })" />
              <span class="tiny muted">只在 HTTPS 下生效；HTTP 场景别开，否则登录会掉线</span>
            </div></div>
        </div>
        <div class="row">
          <label>数据库加密</label>
          <div class="inline">
            <input type="checkbox" :checked="!!form.encryption_enabled" :disabled="!can('encryption_enabled')"
                   @change="save({ encryption_enabled: $event.target.checked })" />
            <span class="muted">启用后请在下方设置主密码</span>
          </div>
        </div>
        <DbMaster />
      </div>
      <p class="muted small" v-if="s._note">{{ s._note }}</p>
    </section>

    <section class="card sec" v-show="onTab('net')">
      <h3>代理池 <span class="muted small">（任务启用「使用代理」时从健康节点中选择）</span></h3>
      <table class="tbl">
        <thead><tr><th>名称</th><th>地址</th><th>优先级</th><th>状态</th><th>失败</th><th></th></tr></thead>
        <tbody>
          <tr v-for="p in proxies" :key="p.id">
            <td>{{ p.name }}</td>
            <td class="mono">{{ p.scheme }}://{{ p.host }}:{{ p.port }}</td>
            <td class="mono">{{ p.priority || 0 }}</td>
            <td><i class="pill" :class="p.enabled ? 'ok' : 'muted'">{{ p.enabled ? '启用' : '停用' }}</i></td>
            <td class="mono">{{ p.failures || 0 }}</td>
            <td class="acts">
              <button class="btn sm" @click="toggleProxy(p)">{{ p.enabled ? '停用' : '启用' }}</button>
              <button class="btn sm danger" @click="delProxy(p)"><Icon name="trash" :size="13" />删除</button>
            </td>
          </tr>
          <tr v-if="!proxies.length"><td colspan="6" class="muted">尚未配置代理</td></tr>
        </tbody>
      </table>
      <div class="addrow">
        <input v-model="newProxy.name" type="text" placeholder="名称（可空）" />
        <select v-model="newProxy.scheme">
          <option value="socks5">socks5</option>
          <option value="http">http</option>
          <option value="https">https</option>
        </select>
        <input v-model="newProxy.host" type="text" placeholder="主机，如 127.0.0.1" />
        <input v-model.number="newProxy.port" type="number" placeholder="端口" />
        <input v-model="newProxy.user" type="text" placeholder="用户名（可空）" />
        <input v-model="newProxy.pass" type="password" placeholder="密码（可空）" />
        <input v-model.number="newProxy.priority" type="number" placeholder="优先级" />
        <button class="btn primary" @click="addProxy"><Icon name="plus" :size="13" />添加</button>
      </div>
    </section>

    <!-- V0.0.26：原来这页 9 项全挤在一张卡片里（用户：「维护页也太多，都挤在一块儿」），按用途拆成四块。 -->
    <section class="card sec" v-show="onTab('store')">
      <h3>磁盘与空间</h3>
      <p class="tiny muted">运行软件那台机器的磁盘余量与整理工具。</p>
      <div class="row">
        <label>磁盘</label>
        <div class="inline" v-if="disk">
          <span class="mono">{{ disk.headline || (disk.free_text + ' 可用 / ' + disk.total_text) }}</span>
          <!-- 2026-10-29（路径归属）：远程/NAS 部署时，"剩余空间"说的是**服务器**
               那块盘，不是浏览器这台电脑。不写清楚最容易让人以为看错了盘。 -->
          <span class="tiny muted">运行软件的机器</span>
          <span v-if="disk.alert" class="pill warn">空间紧张</span>
        </div>
        <span v-else class="muted">读取中…</span>
      </div>
      <!-- V0.0.17：近 7 日增长趋势（/api/diskspace/trend，纯 SVG 迷你条形图） -->
      <DiskTrend />
    </section>

    <section class="card sec" v-show="onTab('store')">
      <h3>备份与恢复</h3>
      <p class="tiny muted">出错或换机时能把数据整份拿回来。</p>
      <div class="row">
        <label>备份</label>
        <div class="inline">
          <button class="btn sm" @click="doBackup">立即备份</button>
          <input type="checkbox" :checked="!!(backup && backup.daily)" @change="toggleDaily" />
          <span class="muted">每日自动备份</span>
          <span v-if="backup" class="muted small">已有备份 {{ backup.count || 0 }} 份</span>
        </div>
      </div>
      <div class="row">
        <label>整库备份</label>
        <div class="full-bk">
          <div class="inline">
            <button class="btn sm primary" :disabled="!!fullBusy" @click="startFullExport">
              <Icon name="save" :size="13" />{{ fullBusy === 'export' ? '正在打包…' : '导出整库备份' }}</button>
            <label class="chk" title="缩略图可达几百 MB；取消勾选可只备份配置/库/规则（恢复后图库会重新生成缩略图）">
              <input type="checkbox" v-model="fullIncludeThumbs" :disabled="!!fullBusy" />含缩略图</label>
            <button class="btn sm" :disabled="!!fullBusy" @click="pickZip">
              <Icon name="upload" :size="13" />{{ fullBusy === 'upload' ? '上传中…' : '上传备份包' }}</button>
            <input ref="zipInput" type="file" accept=".zip,application/zip"
                   class="hidden-file" @change="doUpload" />
          </div>
          <div class="inline tiny muted">
            含配置、数据库、规则、缩略图（图片本体在各任务输出目录，不在包内）。
            备份保存在数据目录的 backups 下，共 {{ fullBk.length }} 份<span v-if="fullTotal">，占用 {{ fullTotal }}</span>。
          </div>
          <!-- R250：自动轮换。三条规则取并集，且始终保留最新一份。
               只在「新备份生成完」或点下面那个按钮时才真的删 —— 改数值不会当场删历史。 -->
          <div class="inline tiny rot">
            <span>自动清理：保留最近</span>
            <input class="num" type="number" min="0" max="99" v-model.number="form.full_backup_keep"
                   :disabled="!can('full_backup_keep')" title="0 = 不限份数" />
            <span>份，超过</span>
            <input class="num" type="number" min="0" max="3650" v-model.number="form.full_backup_days"
                   :disabled="!can('full_backup_days')" title="0 = 不限天数" />
            <span>天，总量上限</span>
            <input class="num" type="number" min="0" max="9999" step="0.5" v-model.number="form.full_backup_max_gb"
                   :disabled="!can('full_backup_max_gb')" title="0 = 不限总量" />
            <span>GB</span>
            <button class="btn sm" :disabled="!!fullBusy" @click="pruneFull">
              <Icon name="eraser" :size="13" />按此立即清理</button>
          </div>
          <div v-if="fullBusy === 'export' || fullBusy === 'restore'" class="jobbox">
            <div class="jobbar"><i :style="{ width: Math.max(2, fullPct) + '%' }" /></div>
            <span class="tiny">{{ fullBusy === 'restore' ? '恢复中' : '打包中' }} {{ fullPct }}% · {{ fullMsg }}</span>
          </div>
          <div v-if="fullBk.length" class="bklist">
            <div v-for="b in fullBk" :key="b.name" class="bkrow">
              <span class="mono bkn" :title="b.name">{{ b.name }}</span>
              <span class="tiny muted">{{ b.mtime }}</span>
              <span class="tiny muted">{{ b.bytes_text }}</span>
              <span class="tiny muted" v-if="b.readable">
                任务 {{ (b.counts || {}).tasks || 0 }} · 图 {{ (b.counts || {}).images || 0 }}
              </span>
              <span class="tiny err" v-else :title="b.error">包已损坏</span>
              <span class="bkacts">
                <a class="btn sm" :href="api.fullDownloadUrl(b.name)" download>下载</a>
                <button class="btn sm" :disabled="!!fullBusy || !b.readable"
                        title="用这份备份覆盖当前数据库、规则与配置"
                        @click="doRestore(b.name)">恢复…</button>
                <button class="btn sm danger" :disabled="!!fullBusy"
                        @click="delFull(b.name)">删除</button>
              </span>
            </div>
            <label class="chk tiny muted" title="恢复前先把当前状态整包备份一份；关掉可省一次几百 MB 的压缩">
              <input type="checkbox" v-model="fullSafety" />恢复前自动备份当前状态</label>
          </div>
          <div v-else class="tiny muted">暂无整库备份。</div>
        </div>
      </div>
      <div class="row">
        <label>偏好迁移</label>
        <div class="inline">
          <a class="btn sm" :href="api.prefsExportUrl()" download>导出偏好 JSON</a>
          <button class="btn sm" :disabled="importing" @click="pickPrefs">
            {{ importing ? '导入中…' : '导入偏好 JSON' }}
          </button>
          <input ref="prefsInput" type="file" accept=".json,application/json"
                 class="hidden-file" @change="doImportPrefs" />
          <span class="muted small">更换设备或重装时迁移学习成果（与桌面端格式一致）</span>
        </div>
      </div>
    </section>

    <section class="card sec" v-show="onTab('store')">
      <h3>清理与诊断</h3>
      <p class="tiny muted">清理数据库记录、导出排障用的诊断包。</p>
      <div class="row">
        <label>清理</label>
        <div class="inline">
          <!-- 2026-10-29 修正：这三个按钮原来传的是 ['temp']/['trash']/['orphan']，
               而后端 /api/cleanup 只认 {tasks,reviews,quarantine,images,thumbs,logs}
               这些**对象键**，且要求 plan 是对象 —— 于是三个按钮以前必报
               「plan 必须是对象」，一次都没成功过。现在按后端白名单改准：
               网页端只清**数据库记录**，磁盘文件（缩略图/日志）由桌面端处理。 -->
          <button class="btn sm" @click="doCleanup({ quarantine: true })">
            <Icon name="eraser" :size="13" />清空隔离标记（图回到图库）</button>
          <button class="btn sm danger" @click="doCleanup({ reviews: true })">
            <Icon name="eraser" :size="13" />清空审核记录</button>
        </div>
      </div>
      <!-- R249：磁盘级维护四件套（完整性 / 0KB / 转 JPG / 修复图库） -->
      <div class="row">
        <label>磁盘维护</label>
        <DiskMaint />
      </div>
      <div class="row">
        <label>诊断包</label>
        <div class="inline">
          <a class="btn sm" :href="api.diagnosticUrl(false)" download>
            <Icon name="scroll-text" :size="13" />导出加密日志诊断包</a>
          <a class="btn sm" :href="api.diagnosticUrl(true)" download>
            <Icon name="hard-drive" :size="13" />导出加密含库诊断包</a>
          <span class="muted small">排障用：版本 / 环境 / 脱敏配置 / 库统计 / 日志尾部。
            口令已打码，不含图片；含库版本另含图片路径与来源网址。
            两个包均已加密为 .mrmx，仅本软件可读取</span>
        </div>
      </div>
    </section>

    <section class="card sec" v-show="onTab('store')">
      <h3>程序与站点</h3>
      <p class="tiny muted">版本更新、开机自启，以及各站点的登录态存档。</p>
      <!-- V0.0.26：「导入本地文件夹」已迁到「导入与导出」页 -->
      <!-- R254：系统集成（版本 / 开机自启 / 检查更新）。原来只有桌面端菜单 -->
      <div class="row">
        <label>程序版本</label>
        <div class="inline">
          <span class="mono tiny">{{ sys.version || '读取中…' }}</span>
          <span v-if="sys.built" class="tiny muted">构建于 {{ sys.built }}</span>
          <button class="btn sm" :disabled="updBusy" @click="checkUpdate">
            <Icon :anim="updBusy ? 'spin' : ''" :name="updBusy ? 'loader-circle' : 'refresh-cw'" :size="13" />
            {{ updBusy ? '检查中…' : '检查更新' }}</button>
          <a v-if="updUrl" class="btn sm" :href="updUrl" target="_blank" rel="noopener">
            <Icon name="download" :size="13" />下载新版</a>
        </div>
      </div>
      <div v-if="updText" class="row">
        <label></label>
        <div class="tiny" :class="updHas ? 'ok' : 'muted'">{{ updText }}</div>
      </div>
      <div class="row">
        <label>开机自启</label>
        <div class="inline">
          <label class="chk tiny">
            <input type="checkbox" :checked="!!sys.autostart" :disabled="!sys.autostart_supported"
                   @change="toggleAutostart($event.target.checked)" />
            登录 Windows 后自动在后台运行（托盘常驻）
          </label>
          <span v-if="!sys.autostart_supported" class="tiny muted">
            当前系统不支持（仅 Windows；Linux/Docker 用系统服务或 restart 策略）
          </span>
        </div>
      </div>
      <!-- R251：站点 Cookie（按域名存档） -->
      <div class="row">
        <label>站点 Cookie</label>
        <CookieMgr />
      </div>
    </section>


    <!-- V0.0.53：关于（软件介绍 + 版本信息 + 检查程序更新） -->
    <section class="card sec" v-show="onTab('about')">
      <h3>关于</h3>
      <div class="about-hero">
        <div class="about-mark"><Icon name="images" :size="26" /></div>
        <div>
          <div class="about-title">Sweet 采集 AI 版</div>
          <div class="muted small">本地优先、合规优先的网页图片采集与图库管理工具</div>
          <div class="muted tiny" style="margin-top:4px">
            版本 {{ sys.version || '—' }}<span v-if="sys.built"> · 构建于 {{ sys.built }}</span>
          </div>
        </div>
      </div>
      <p class="muted small about-desc">
        从合规检查、采集、去重，到本地 AI 筛选、人工审核、入库检索，整条流水线都在本机完成。
        图片与判定结果不会上传。软件本体开源（MIT），配套的个人适配器规则不开源。
      </p>
      <div class="row">
        <label>相关链接</label>
        <div class="inline" style="flex-wrap:wrap">
          <a class="btn sm ghost" href="https://github.com/514475844/SweetCollectorAI" target="_blank" rel="noopener">
            <Icon name="external-link" :size="13" />项目主页</a>
          <a class="btn sm ghost" href="https://github.com/514475844/SweetCollectorAI/blob/main/CHANGELOG.md" target="_blank" rel="noopener">
            <Icon name="link" :size="13" />更新公告</a>
          <a class="btn sm ghost" href="https://github.com/514475844/SweetCollectorAI/issues" target="_blank" rel="noopener">
            <Icon name="external-link" :size="13" />问题反馈</a>
        </div>
      </div>
      <div class="row">
        <label>程序更新</label>
        <div class="inline">
          <button class="btn sm" :disabled="aboutBusy" @click="checkAppUpdate">
            <Icon :anim="aboutBusy ? 'spin' : ''" :name="aboutBusy ? 'loader-circle' : 'refresh-cw'" :size="13" />
            {{ aboutBusy ? '检查中…' : '检查程序更新' }}</button>
          <a v-if="aboutUrl" class="btn sm primary" :href="aboutUrl" target="_blank" rel="noopener">
            <Icon name="download" :size="13" />前往下载页</a>
        </div>
      </div>
      <div v-if="aboutText" class="row">
        <label>检查结果</label>
        <div class="tiny" :class="aboutHas ? 'ok' : 'muted'">{{ aboutText }}</div>
      </div>
      <div class="row">
        <label>界面模式</label>
        <div class="inline">
          <label class="tiny"><input type="radio" :checked="uiMode === 'newbie'" @change="setUiMode('newbie')" /> 新手模式</label>
          <label class="tiny"><input type="radio" :checked="uiMode === 'pro'" @change="setUiMode('pro')" /> 老鸟模式</label>
          <span class="muted tiny">桌面端与网页端共用同一设置；新手模式仅简化显示，功能不减少。</span>
        </div>
      </div>
    </section>

    <!-- 2026-10-29（第 8 条）：每页底部的保存条。sticky 贴在视口底部，
         滚多远都点得到；改了几项、存没存，一眼就有答案。
         V0.0.23：用户反馈「保存本页一直是灰的」—— 根因是这几个页的字段
         本来就是**改完即时生效**（走 save() 直存，不进草稿），所以草稿永远
         为空、按钮永远是灰的。这种页不再摆一个点不动的按钮，改成说明文字，
         别让人以为功能坏了。 -->
    <div class="savebar card">
      <template v-if="instantTab">
        <span class="muted small">本页每一项都是改完立即保存，不需要再点保存。</span>
        <span class="sp"></span>
        <span class="muted tiny" v-if="lastSaved">最近保存：{{ lastSaved }}</span>
      </template>
      <template v-else>
        <span v-if="pageDirtyKeys.length" class="pill warn">
          {{ searching ? '命中项' : '本页' }}未保存 {{ pageDirtyKeys.length }} 项
        </span>
        <span v-else class="muted small">
          {{ searching ? '命中项已全部保存' : '本页没有未保存的修改' }}
        </span>
        <span class="sp"></span>
        <button class="btn" :disabled="!pageDirtyKeys.length || pageSaving" @click="revertPage">
          放弃修改
        </button>
        <button class="btn primary" :disabled="!pageDirtyKeys.length || pageSaving" @click="savePage">
          <Icon name="save" :size="13" />{{ pageSaving ? '保存中…' : '保存本页' }}
        </button>
      </template>
    </div>

    <FolderPicker :open="pickerOpen"
                  :start="dirTarget ? String(form[dirTarget.k] || '') : ''"
                  :title="dirTarget ? '选择' + dirTarget.label : '选择要导入的文件夹'"
                  @close="pickerOpen = false; dirTarget = null" @picked="onPicked" />
  </div>
</template>

<style scoped>
/* V0.0.53：关于页头部与简介 */
.about-hero { display: flex; align-items: center; gap: 14px; padding: 6px 0 12px; }
.about-mark {
  width: 52px; height: 52px; flex-shrink: 0; border-radius: 14px;
  display: grid; place-items: center; color: #fff;
  background: linear-gradient(135deg, #7c5cff, #4f8cff);
}
.about-title { font-size: 17px; font-weight: 700; }
.about-desc { margin: 0 0 8px; line-height: 1.8; }
/* V0.0.51：Web 访问页高级区 —— 默认收起，展开后与普通项同款留白。 */
.adv-toggle { margin: 2px 0 0 96px; }
.web-advanced {
  margin-top: 10px; padding: 12px 14px 4px;
  border: 1px solid var(--line); border-radius: 10px;
  background: rgba(127, 127, 127, .04);
}
.adv-h4 { margin: 12px 0 6px; font-size: 13px; font-weight: 600; color: var(--text); }
.searchbar { display: flex; gap: 10px; align-items: center; padding: 10px 14px; }
.searchbar input { flex: 1; min-width: 0; }
/* 2026-10-29（第 8 条）：分页标签 + 底部保存条 */
/* V0.0.35：标签之间 4px 太挤（和导入导出页同款，一起放宽） */
.tabs { display: flex; gap: 8px; padding: 6px; flex-wrap: wrap; }
.tab {
  display: inline-flex; align-items: center; gap: 5px;
  border: 1px solid transparent; background: transparent; color: var(--muted);
  padding: 6px 11px; border-radius: 8px; font-size: 13px; white-space: nowrap;
  position: relative;
}
.tab:hover { background: #f7f5ff; color: var(--text); }
.tab.on {
  background: #f1edff; border-color: #ded4ff; color: var(--brand); font-weight: 600;
}
.tabdot {
  width: 6px; height: 6px; border-radius: 50%; background: var(--warn);
  display: inline-block; margin-left: 2px;
}
.savebar {
  position: sticky; bottom: 8px; z-index: 6;
  display: flex; align-items: center; gap: 10px;
  padding: 9px 14px; margin-top: 2px;
  box-shadow: 0 -2px 12px rgba(16, 24, 40, .06);
}
.savebar .sp { flex: 1; }
/* 2026-10-29（第 7 条）：设置页「左右超出框」的总根因 ——
   grid 子项的 min-width 默认是 auto，内容（按钮组、表格、长路径）不肯收缩，
   于是整张卡片被撑破、横向溢出到页面外，右侧的输入框/按钮就看不见了。
   统一给容器和网格子项 min-width:0，再让长内容自己滚动/换行。 */
/* V0.0.27：块与块之间必须有缝 —— 这页以前没有 gap，搜索框/页签/每张设置卡/
   底部保存条全部贴在一起（用户原话「好多块与块之间没有进行空行处理」）。
   与任务/图库等页同一口径：page 网格 + 14px 缝。 */
.page { min-width: 0; max-width: 100%; display: grid; gap: 14px; align-items: start; }
.sec { padding: 16px 18px; display: grid; gap: 12px; min-width: 0; }
.sec h3 { margin: 0 0 2px; font-size: 15px; }
.row { display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 10px; align-items: center; min-width: 0; }
.row > * { min-width: 0; }
/* V0.0.27：上下排的行（label 在输入框上方）以前只隔 4px，挤成一团 */
.row.col { grid-template-columns: 1fr; gap: 6px; }
.inline { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; min-width: 0; }
.inline input[type="text"] { flex: 1 1 140px; min-width: 0; }
.grid4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; min-width: 0; }
.num { width: 96px; max-width: 100%; }
.small { font-size: 12px; }
.tiny { font-size: 11px; }
/* V0.0.42：时段表＝真表格（V0.0.26 那版「行式卡片」已撤，见下方说明）。 */
.plantable { display: flex; flex-direction: column; gap: 6px; margin: 10px 0;
  width: 100%; box-sizing: border-box; }
.planhead, .planrow {
  display: grid; align-items: center; gap: 10px;
  grid-template-columns: 124px 20px 140px 116px minmax(110px, 1fr) 84px;
}
.planhead { font-size: 11.5px; color: var(--muted); padding: 0 12px; }
.planrow { padding: 8px 12px; border: 1px solid var(--line);
  border-radius: 10px; background: var(--panel); }
.planrow .pcell { width: 100%; box-sizing: border-box; }
.planrow select.pcell { padding-right: 6px; }
.pto { font-size: 12px; color: var(--muted); text-align: center; }
.phint { font-size: 11.5px; color: var(--muted); }
.phint.on { color: var(--brand); }
/* 删除键：低干扰（红字、无底色），鼠标移上去才泛红底 —— 原来它是一块
   实心大红，在一排浅色控件里像从边框里顶出来的异物。 */
.btn.danger-t { color: #dc2626; background: transparent;
  border-color: transparent; justify-self: end; }
.btn.danger-t:hover { background: #fdecec; border-color: #f7cccc; }
/* 窄一点时先牺牲「说明」列（它只是把上限再说一遍），保证不溢出 */
@media (max-width: 900px) {
  .planhead, .planrow {
    grid-template-columns: 116px 18px 132px 110px 84px;
  }
  .planhead span:nth-child(5), .phint { display: none; }
}
/* V0.0.36：时段表开关挪进这一块后，未启用提示给个显眼但不刺眼的底色 */
.warn-note { background: #fff7e6; border: 1px solid #f5deb0; border-radius: 8px;
  padding: 7px 10px; color: #8a6200; margin: 8px 0; }
/* ⚠ 颜色一律走主题变量：这里原来写死了浅色底，深色皮肤下就变成
   「浅底 + 浅字」，整条状态提示看不见（用户上一版截图里那种）。 */
.planstate { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 8px 12px; margin: 2px 0 8px; border-radius: 9px;
  border: 1px solid var(--line); background: var(--panel);
  color: var(--text); font-size: 12.5px; }
.planstate b { font-weight: 600; }
.planstate.limited { border-color: var(--ok); }
.planstate.off { border-color: var(--warn); }
.warn-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  font-size: 12px; }
.warn-row > span { flex: 1 1 auto; min-width: 200px; }
/* V0.0.42：旧的 flex 行 / 固定 126px 宽 / 行内小标题（.pf）全部撤掉，
   改由上面的 .plantable 网格负责；留着会盖掉网格列宽。 */
.row-actions { display: flex; align-items: center; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
/* 限速这块进了 .sec（grid + 12px 缝）以后，子元素自带的上下外边距会叠加，
   统一清零，间距只由 grid 缝决定。 */
.plancard { gap: 10px; }
.plancard > h3, .plancard > p, .plancard > .planstate,
.plancard > .warn-note, .plancard > .plantable,
.plancard > .row-actions { margin: 0; }
.btn.danger { color: #fff; background: #dc2626; border-color: #dc2626; }
.btn.primary { background: var(--accent, #6d5efc); border-color: var(--accent, #6d5efc); color: #fff; }
.btn:disabled { opacity: .5; cursor: not-allowed; }

/* R248：从文件夹导入 */
.imp { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.imp-path {
  font-size: 12px; padding: 3px 8px; border-radius: 6px;
  background: var(--panel2, #f6f3ff); color: var(--text);
  max-width: 320px; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap;
}
.imp-result { margin: 2px 0 0; line-height: 1.6; }
/* R250：轮换设置那行的小数字输入框（默认 input 会撑满，把整行挤断） */
.rot { flex-wrap: wrap; gap: 6px; align-items: center; }
.rot .num { width: 58px; padding: 2px 6px; font-size: 12px; text-align: right; }
/* R241 整库备份：清单是一行一条，名字长（mrm_full_20261028_235900.zip），
   用等宽字体 + 省略号，操作按钮靠右不换行溢出 */
.full-bk { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.chk { display: inline-flex; align-items: center; gap: 5px; cursor: pointer; }
.bklist { display: flex; flex-direction: column; gap: 6px; border-top: 1px dashed var(--line); padding-top: 8px; }
.bkrow { display: grid; grid-template-columns: 1.6fr 108px 76px 1fr auto; gap: 8px; align-items: center; }
.bkn { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.bkacts { display: flex; gap: 6px; }
.jobbox { display: flex; flex-direction: column; gap: 4px; }
.jobbar { height: 6px; border-radius: 4px; background: var(--line); overflow: hidden; }
.jobbar > i { display: block; height: 100%; background: var(--accent, #6d5efc); transition: width .3s ease; }
.hidden-file { display: none; }
/* V0.0.23：Web 访问里「登录密码」那格多了一个「清除密码」按钮，两格高度
   不一致时标签与说明就错位。顶部对齐 + 说明不参与挤压。 */
.f2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; align-items: start; }
.f2 .row.col > label { min-height: 20px; }
.rel { margin: 6px 0 0 18px; padding: 0; display: grid; gap: 6px; line-height: 1.65; }
.err { color: var(--err); margin: 0; }
.ok { color: var(--ok); margin: 0; }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th { text-align: left; font-weight: 500; color: var(--muted); font-size: 12px; padding: 6px 8px; border-bottom: 1px solid var(--line); }
.tbl td { padding: 7px 8px; border-bottom: 1px solid #f2f3f8; }
.acts { display: flex; gap: 6px; }
/* V0.0.59：按钮尺寸已统一到全局 style.css，此处局部覆盖删除 */
.addrow { display: grid; grid-template-columns: 1fr 100px 1.3fr 80px 1fr 1fr 70px 70px; gap: 8px; margin-top: 6px; }
/* 2026-10-29（第 7 条续）：备份清单 5 列在中等宽度（1000–1200）也会顶出去，
   先把按钮组换到第二行，别等 820 才处理。 */
@media (max-width: 1080px) {
  .bkrow { grid-template-columns: minmax(0, 1fr) 96px 72px; }
  .bkrow .bkacts { grid-column: 1 / -1; flex-wrap: wrap; }
  .grid4 { grid-template-columns: repeat(2, 1fr); }
  .addrow { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 820px) {
  .grid4 { grid-template-columns: repeat(2, 1fr); }
  .addrow { grid-template-columns: 1fr 1fr; }
  .row { grid-template-columns: 1fr; }
  .f2 { grid-template-columns: 1fr; }
  /* 手机上备份清单挤成三行：名字 / 信息 / 按钮，别把按钮挤出屏幕 */
  .bkrow { grid-template-columns: 1fr; gap: 2px; }
  .bkacts { flex-wrap: wrap; }
}
</style>
