<script setup>
// 图片网格 + 灯箱（图库 / 待审核 / 隔离区 / 去重通用）。
// 接口沿用后端同一分页通道：/api/view/<view>（time 排序走 id 游标，不重不漏）。
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { api } from '../api'
import ContextMenu from './ContextMenu.vue'
import Icon from './Icon.vue'
// V0.0.62（用户）：AI 识别控件并入图库工具栏白框 —— 只留「AI 识别 /
// 还有 N 张待识别 / 启动按钮」三样，识别卡那种整块卡片和说明文字全删。
import { tagImp, startTagging, cancelTagging, refreshTagPending } from '../importJob'
// V0.0.21：内置窗口里 Ctrl+A = 全选本页已加载项，F5 = 重拉当前页数据。
import { onShellSelectAll, onShellRefresh } from '../nativeShell'
let unbindShell = []

const props = defineProps({
  view: { type: String, default: 'gallery' },
  reviewMode: { type: Boolean, default: false },   // 审核模式：动作语义变成 通过/不喜欢
  // V0.0.46：每页张数不再用 prop（图库/审核/隔离区共用同一个可持久化的
  // 本地选择器，见下方 pageSize），原来固定 60 的 prop 取消。
  // V0.0.12：图库视图开启批量重命名（网页补桌面图库菜单同款能力）。
  // 原有图库文件由后端红线闸跳过并计数回报，前端只管发请求。
  renamable: { type: Boolean, default: false },
})

const items = ref([])
const cursor = ref(0)
const hasMore = ref(true)
const loading = ref(false)
const err = ref('')
// V0.0.8：轻提示（判退后「学到了什么」等好消息不该和红字错误混用一个条）
const toastMsg = ref('')
let toastTimer = 0
function toast(msg) {
  toastMsg.value = msg
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toastMsg.value = '' }, 6000)
}
function notifyLearned(res) {
  const L = res && res.learned
  if (!L) return
  if (L.hardened && L.hardened.length) {
    toast(`已学习 ${L.total} 个排除词，其中 ${L.hardened.slice(0, 6).join('、')} 已在下载前拦截`)
  } else if (L.total) {
    toast(`已学习 ${L.total} 个排除词（同类再次判退将升级为下载前拦截）`)
  }
}
const sort = ref('time')
// V0.0.14：图库快捷筛选（后端白名单收窄，未知值自动退化为「不筛选」）。
const flag = ref('')
const q = ref('')
const taskFilter = ref('')        // 2026-10-27 用户要求：按任务筛选
const taskList = ref([])
// 2026-10-27 对齐老界面：标签筛选下拉 / 加删标签 / 缩略图档位
const tagList = ref([])
const tagFilter = ref('')
// R244 · 多级分类：标签名里写 "/" 就自动分层（「场景/室内」「作者/某某」）。
// 为什么用标签名分层而不是另建一张「分类目录」表：
//   · 我们的输出目录规则按「任务」走，再叠一层真目录会两套结构打架
//     （docs/r235 里已经写明这条取舍）；
//   · 标签本来就是自由文本，`场景/室内` 这种写法在桌面端、采集端都原样存，
//     不动库结构、不动采集管线，就能有「树上翻图」的效果；
//   · 点父节点筛的是整棵子树（后端 tags LIKE %词% 天然命中子标签）。
const tagCollapsed = ref(new Set())
const thumbSize = ref('m')        // s / m / l —— 小图省流量，大图看得清
// 2026-10-27：方向 / 分辨率（桌面端图库早就有，Web 一直没有）
const orient = ref('')            // '' | land 横 | port 竖 | square 方
const res = ref('')               // '' | s | m | l | xl（按长边）
const tagModal = ref(null)        // { ids, union, input }
const THUMB_W = { s: 220, m: 360, l: 640 }
// V0.0.46：每次加载多少张（用户要求「别每次固定死 60 张」）。
// 存 localStorage：这是「本机看图的口味」，不是任务配置，不该进后端库，
// 也不该跟着任务走 —— 换设备/换账号回到默认 60 即可。
const PAGE_SIZE_KEY = 'mrm_page_size'
// 上限 200 = 后端 _clamp_limit 的硬顶（webui.py PAGE_LIMIT_MAX），
// 前端档位必须 ≤ 它，否则请求 480 实际回 200，游标会错位。
const PAGE_SIZES = [30, 60, 120, 200]
const pageSize = ref((() => {
  try {
    const v = parseInt(localStorage.getItem(PAGE_SIZE_KEY) || '', 10)
    return PAGE_SIZES.includes(v) ? v : 60
  } catch { return 60 }
})())
function setPageSize(v) {
  const n = parseInt(v, 10)
  if (!PAGE_SIZES.includes(n)) return
  pageSize.value = n
  try { localStorage.setItem(PAGE_SIZE_KEY, String(n)) } catch { /* 隐私模式 */ }
  reload()          // 改了每页张数：从第一页重新按新张数拉
}
const selected = ref(new Set())
const sentinel = ref(null)
// V0.0.52：内存保护。图库动辄十万张，以前无限滚动把每一张都塞进 DOM 并
// 解码，内核渲染进程十来分钟就吃到几个 GB。现在只让视口附近（上下各
// 900px）的图片真正加载，离屏的换成 1×1 透明图 —— DOM 卡片、勾选、
// 滚动位置全保留，但解码出来的大位图会被内核回收。
const BLANK = 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs='
const AUTO_LIMIT = 600        // 自动滚动一次最多堆 600 张，再往下要手动点
const autoLimit = ref(AUTO_LIMIT)
let imgIO = null
const lb = ref(null)          // 灯箱：当前图对象
const lbIndex = ref(-1)
// 2026-10-27：老界面灯箱有缩放/旋转/全屏/幻灯片，v2 只有翻页 —— 这里补齐。
// 100% = 适应窗口（默认，绝不上采样放大）；> 100% 是用户主动放大。
const lbZoom = ref(1)
const lbRot = ref(0)
const lbFs = ref(false)          // 全屏 = 藏掉所有工具栏，图片铺满
const lbPlay = ref(false)        // 幻灯片播放
const lbSpeed = ref(5000)        // 每张停留毫秒
const lbLoop = ref(true)         // 到末尾是否回到第一张
let playTimer = null
const ZOOM_MIN = 0.2
const ZOOM_MAX = 8
// 放大后拖动查看（transform 不占布局，没有它放大完就看不到了）
const lbPan = ref({ x: 0, y: 0 })
let drag = null
const ctx = ref(null)          // 右键菜单：{x,y,items}
const busy = ref(false)
const dislikeModal = ref(null)  // { ids, options }
const simInfo = ref(null)  // { seed, n, candidates }：正在看某张图的相似结果
const simBusy = ref(false)
let io = null

const cols = computed(() => items.value)
const selCount = computed(() => selected.value.size)

async function load(more = false) {
  if (loading.value) return
  if (semMode.value) { await loadSemantic(); return }
  if (more && !hasMore.value) return
  // V0.0.52：没点「加载更多」就别一路自动拉完整库（十万张 = 几 GB 位图）。
  if (more && items.value.length >= autoLimit.value) return
  loading.value = true
  try {
    // 标签筛选走 #63 语法 `tag:xxx`（后端 search.parse_query 解析），
    // 与桌面端搜索框、老界面下拉是**同一套语义**，不另发明参数。
    const qs = [q.value.trim(), tagFilter.value ? `tag:${tagFilter.value}` : '']
      .filter(Boolean).join(' ')
    const d = await api.view(props.view, {
      limit: pageSize.value,
      cursor: more ? cursor.value : 0,
      sort: sort.value,
      q: qs,
      task: taskFilter.value || '',
      orient: orient.value,
      res: res.value,
      flag: flag.value,
    })
    const rows = d.items || []
    items.value = more ? [...items.value, ...rows] : rows
    cursor.value = d.next_cursor || 0
    hasMore.value = !!d.has_more
    err.value = ''
    watchImages()
  } catch (e) {
    err.value = e.message
  } finally {
    loading.value = false
  }
}

function reload() {
  cursor.value = 0
  hasMore.value = true
  autoLimit.value = AUTO_LIMIT
  selected.value = new Set()
  load(false)
}

/** 手动「加载更多」：每次放行一批，自动滚动到此为止。 */
function loadMoreManually() {
  autoLimit.value = items.value.length + AUTO_LIMIT
  load(true)
}

/**
 * V0.0.52：只让视口附近的卡片持有真实图片地址。
 * 用原生 DOM 改 src（不走 Vue 响应式）：十万张卡片若响应式重渲染一遍
 * 本身就会卡死；这样勾选、框选、滚动位置一概不受影响。
 */
function watchImages() {
  nextTick(() => {
    const grid = gridEl.value
    if (!grid || typeof IntersectionObserver === 'undefined') return
    if (imgIO) imgIO.disconnect()
    const root = grid.closest('.main') || null
    imgIO = new IntersectionObserver((entries) => {
      for (const e of entries) {
        const img = e.target.querySelector('img[data-src]')
        if (!img) continue
        const real = img.dataset.src || ''
        if (!real) continue
        if (e.isIntersecting) {
          if (img.getAttribute('src') !== real) img.src = real
        } else if (img.getAttribute('src') !== BLANK) {
          img.src = BLANK
        }
      }
    }, { root, rootMargin: '900px 0px', threshold: 0 })
    grid.querySelectorAll('figure.cell').forEach((el) => imgIO.observe(el))
  })
}

function selectAllInPage() {
  if (selected.value.size === items.value.length) selected.value = new Set()
  else selected.value = new Set(items.value.map((x) => x.id))
}
// Ctrl+A 的语义是「全选」，不是「切换」：重复按不应把已选中的清空。
function selectAllForce() {
  selected.value = new Set(items.value.map((x) => x.id))
}

function toggleSel(id) {
  const s = new Set(selected.value)
  s.has(id) ? s.delete(id) : s.add(id)
  selected.value = s
}

function onCardContext(e, it) {
  e.preventDefault()
  selected.value = new Set([it.id])
  ctx.value = {
    x: e.clientX, y: e.clientY,
    items: [
      { label: reviewMode ? '通过' : '喜欢', icon: 'thumbs-up', onClick: () => act('like', [it.id]) },
      { label: '收藏 / 取消收藏', icon: 'star', onClick: () => act('favorite', [it.id]) },
      { label: '不喜欢…', icon: 'thumbs-down', onClick: () => act('dislike', [it.id]) },
      { label: '复制图片地址', icon: 'copy', onClick: () => copyText(it.orig_url || it.url) },
      { label: '在文件夹中显示', icon: 'folder-open', onClick: () => revealInFolder(it.id) },
      { label: it.source_url ? '打开来源页' : '（无来源页）', icon: 'external-link',
        disabled: !it.source_url,
        onClick: () => it.source_url && window.open(it.source_url, '_blank', 'noopener') },
      { label: '下载原图', icon: 'download', onClick: () => download(it) },
      { label: '删除', icon: 'trash', danger: true, onClick: () => act('delete', [it.id]) },
    ],
  }
}

async function copyText(s) {
  try { await navigator.clipboard.writeText(String(s || '')) }
  catch {
    const ta = document.createElement('textarea')
    ta.value = String(s || '')
    document.body.appendChild(ta); ta.select()
    try { document.execCommand('copy') } catch { /* 忽略 */ }
    ta.remove()
  }
}

// V0.0.17：在文件夹中显示（/api/image/reveal 只认数据库已登记的图片路径）。
async function revealInFolder(id) {
  try {
    const r = await api.imageReveal(id)
    if (!r.ok) { toast(r.error || '未找到可打开的文件夹。'); return }
    toast(r.note || '已打开图片所在文件夹。')
  } catch (e) {
    toast(e.message || '在文件夹中显示失败，请稍后重试。')
  }
}

function download(it) {
  const a = document.createElement('a')
  a.href = it.orig_url || it.url
  a.download = ''
  a.click()
}

// 2026-10-27：找相似（后端 /api/similar 按标签+哈希+主色排序，与桌面端同一个
// simsearch.rank_similar）。候选集有上限，手机上不会转到天荒地老。
async function findSimilar(it) {
  if (!it || simBusy.value) return
  simBusy.value = true
  err.value = ''
  try {
    const d = await api.similar(it.id, pageSize.value)
    const rows = (d.items || []).map((x) => ({
      id: x.id,
      url: x.url,
      thumb_url: x.url,
      score: x.score,
      tags: x.tags || [],
      meta: `相似度 ${Math.round((x.score || 0) * 100)}%`,
    }))
    if (!rows.length) {
      err.value = '未找到相似图片（候选集中无足够接近的结果）'
      return
    }
    items.value = rows
    hasMore.value = false
    simInfo.value = { seed: it.id, n: rows.length, candidates: d.candidates || 0 }
    closeLightbox()
    watchImages()
  } catch (e) {
    err.value = e.message || '找相似失败'
  } finally {
    simBusy.value = false
  }
}

function exitSimilar() {
  simInfo.value = null
  load(false)
}

// 灯箱打开时锁住 body 滚动：手机上滑一下整页跟着动、关掉后位置全乱
function lockScroll(on) {
  try { document.body.style.overflow = on ? 'hidden' : '' } catch { /* 忽略 */ }
}
// 2026-10-27（编号 2）：桌面端「单击选中、双击开灯箱」；触屏仍然单击就开。
// 为什么分设备：手机上双击要先等 300ms 判断，且没有"选中"这个使用习惯，
// 强行照搬桌面会让点图变成"卡一下才响应"。
const _finePointer = (() => {
  try { return window.matchMedia('(hover: hover) and (pointer: fine)').matches }
  catch { return true }
})()

function onCardClick(it, i) {
  // 框选刚结束时浏览器还会补一个 click，那一下会把刚框好的一片重置成单张
  if (suppressClick) return
  if (_finePointer) { toggleSel(it.id); return }   // 桌面：单击=选中
  openLightbox(it, i)                              // 触屏：单击=开灯箱
}

// ---------------- 框选（拖出一个矩形，框到哪张选哪张） ----------------
const gridEl = ref(null)
const marquee = ref(null)        // {x1,y1,x2,y2} 视口坐标
let mq = null                    // 拖拽中的状态
let suppressClick = false        // 吞掉框选后那一次 click

function onGridDown(e) {
  if (e.button !== 0 || lb.value) return
  // 触屏要滚页面，不能抢手势；点在勾/按钮/链接上也不开始框选
  if (e.pointerType === 'touch') return
  if (e.target.closest('button, a, .tick, input')) return
  mq = { sx: e.clientX, sy: e.clientY, add: e.ctrlKey || e.metaKey, active: false }
  window.addEventListener('pointermove', onGridMove)
  window.addEventListener('pointerup', onGridUp, { once: true })
}

function onGridMove(e) {
  if (!mq) return
  const dx = e.clientX - mq.sx, dy = e.clientY - mq.sy
  // 5px 以内当"手抖"，仍然是普通单击 —— 否则轻点一下图片会变成空框
  if (!mq.active && Math.abs(dx) < 5 && Math.abs(dy) < 5) return
  mq.active = true
  marquee.value = {
    x1: Math.min(mq.sx, e.clientX), y1: Math.min(mq.sy, e.clientY),
    x2: Math.max(mq.sx, e.clientX), y2: Math.max(mq.sy, e.clientY),
  }
}

function onGridUp() {
  window.removeEventListener('pointermove', onGridMove)
  const box = marquee.value
  const active = !!(mq && mq.active)
  const add = !!(mq && mq.add)
  marquee.value = null
  mq = null
  if (!active || !box || !gridEl.value) return
  const next = add ? new Set(selected.value) : new Set()
  gridEl.value.querySelectorAll('.cell').forEach((el) => {
    const r = el.getBoundingClientRect()
    // 任意相交即算（qB 也是"碰到就选"，不要求整张进框）
    if (r.right < box.x1 || r.left > box.x2
        || r.bottom < box.y1 || r.top > box.y2) return
    const id = Number(el.dataset.id)
    if (id) next.add(id)
  })
  selected.value = next
  // click 在 pointerup 之后触发，用 setTimeout 让它在下一轮事件循环之后失效
  suppressClick = true
  setTimeout(() => { suppressClick = false }, 0)
}

function onCardDblClick(it, i) {
  if (!_finePointer) return
  selected.value = new Set([it.id])
  openLightbox(it, i)
}

function openLightbox(it, idx) {
  lb.value = it
  lbIndex.value = idx
  lockScroll(true)
}
function closeLightbox() {
  stopPlay()
  lb.value = null
  lbIndex.value = -1
  lbZoom.value = 1
  lbRot.value = 0
  lbPan.value = { x: 0, y: 0 }
  lbFs.value = false
  lockScroll(false)
}

// —— 视图控制（只改显示，不动原文件）——
function zoomSet(z) {
  // ⚠ 必须夹范围：不夹的话连点放大能推到几千倍，浏览器反复重排大图直接卡死
  lbZoom.value = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Number(z) || 1))
  if (lbZoom.value <= 1) lbPan.value = { x: 0, y: 0 }   // 回到适应窗口就归位
}
function onDragStart(e) {
  if (lbZoom.value <= 1) return
  drag = { x: e.clientX, y: e.clientY, px: lbPan.value.x, py: lbPan.value.y }
  try { e.currentTarget.setPointerCapture?.(e.pointerId) } catch { /* 忽略 */ }
}
function onDragMove(e) {
  if (!drag) return
  lbPan.value = { x: drag.px + (e.clientX - drag.x), y: drag.py + (e.clientY - drag.y) }
}
function onDragEnd() { drag = null }
function zoomBy(f) { zoomSet(lbZoom.value * f) }
function rotBy() { lbRot.value = (lbRot.value + 90) % 360 }
function onWheel(e) {
  // 滚轮缩放：上滚放大、下滚缩小（触摸板上很顺手）
  zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15)
}

// —— 幻灯片 ——
function stopPlay() {
  lbPlay.value = false
  if (playTimer) { clearInterval(playTimer); playTimer = null }
}
function tickPlay() {
  const last = lbIndex.value >= items.value.length - 1
  if (last) {
    if (!lbLoop.value || items.value.length < 2) { stopPlay(); return }
    lbIndex.value = 0
    lb.value = items.value[0]
    return
  }
  step(1)
}
function togglePlay() {
  if (lbPlay.value) { stopPlay(); return }
  if (items.value.length < 2) { err.value = '仅有一张图片，无法播放幻灯片'; return }
  lbPlay.value = true
  playTimer = setInterval(tickPlay, lbSpeed.value)
}
function cycleSpeed() {
  const seq = [3000, 5000, 8000, 15000]
  const i = seq.indexOf(lbSpeed.value)
  lbSpeed.value = seq[(i + 1) % seq.length]
  if (lbPlay.value) {           // 播放中改速度要重建定时器，否则新速度不生效
    clearInterval(playTimer)
    playTimer = setInterval(tickPlay, lbSpeed.value)
  }
}
function step(d) {
  const i = lbIndex.value + d
  if (i < 0 || i >= items.value.length) return
  const next = items.value[i]
  if (!next) { closeLightbox(); return }   // 列表被批量操作缩短后防越界
  lbIndex.value = i
  lb.value = next
  // 换图复位：不复位的话下一张一打开就是上一张的放大/旋转角度（看着像坏了）
  lbZoom.value = 1
  lbRot.value = 0
  lbPan.value = { x: 0, y: 0 }
  if (i >= items.value.length - 6 && hasMore.value) load(true)
}

async function act(actName, ids) {
  const list = ids || (selCount.value ? [...selected.value] : (lb.value ? [lb.value.id] : []))
  if (!list.length || busy.value) return
  busy.value = true
  try {
    if (actName === 'dislike') {
      // 「不喜欢」不分页面都先问原因（V0.0.10）：桌面端图库/审核两边都会弹
      // ask_dislike_reason，网页以前只有审核页问、图库直接落库——同一个动作
      // 两端学到的东西不一样。语义照抄桌面：左栏=主题词未命中（只调权重），
      // 右栏=排除词（才写 ai_blacklist）。
      const opt = await api.dislikeOptions(list)
      // V0.0.12：桌面 dislike_dialog 同款（两栏多选+手填桥接+误判纠正）
      dislikeModal.value = {
        ids: list, options: opt,
        selU: new Set(), selE: new Set(),
        manual: '', preview: '', mis: false, correct: '',
        cats: new Set(),
        submitting: false,
      }
      return
    }
    const res = await api.imageAction(list, actName)
    notifyLearned(res)
    await afterAction(list, actName)
  } catch (e) {
    err.value = e.message
  } finally {
    busy.value = false
  }
}

async function afterAction(ids, actName) {
  // like/dislike 会让图在两个视图间移动；就地移除最省事，也让用户看到"处理掉了"
  if (props.reviewMode || actName === 'delete' || actName === 'dislike'
      || actName === 'move' || actName === 'blacklist') {
    const gone = new Set(ids)
    items.value = items.value.filter((x) => !gone.has(x.id))
    selected.value = new Set()
    if (lb.value && gone.has(lb.value.id)) closeLightbox()
  } else {
    await load(false)
  }
}

// —— V0.0.12：桌面 dislike_dialog 同款多选提交（取代原「点一个词即交」）——
function dmToggle(which, w) {
  const m = dislikeModal.value
  if (!m) return
  const s = m[which]
  s.has(w) ? s.delete(w) : s.add(w)
}
function dmSelectAll(which) {
  const m = dislikeModal.value
  if (!m) return
  const src = which === 'selU' ? (m.options.labels || [])
                               : (m.options.blacklist || [])
  const s = m[which]
  src.forEach((w) => s.add(w))
}
let _previewTimer = 0
function dmManualInput() {
  // 手填框实时预演「会记到哪个词表标签上」（桌面 _refresh_preview 同款；
  // 防抖 400ms——桥接矩阵冷建要几百毫秒，别每个按键都请求）
  const m = dislikeModal.value
  if (!m) return
  clearTimeout(_previewTimer)
  const word = String(m.manual || '').trim()
  if (!word) { m.preview = ''; return }
  _previewTimer = setTimeout(async () => {
    try {
      const r = await api.labelBridgePreview([word])
      if (dislikeModal.value === m) {
        m.preview = (r.hints && r.hints[0] && r.hints[0].hint) || ''
      }
    } catch (e) { /* 预览失败不影响提交，静默 */ }
  }, 400)
}
async function dmSubmit() {
  const m = dislikeModal.value
  if (!m || m.submitting) return
  const manual = String(m.manual || '').trim()
  const correct = m.mis
    ? String(m.correct || '').split(/\s+/).filter(Boolean).slice(0, 10)
    : []
  const extra = {
    unmatched: [...(m.options.labels || []).filter((w) => m.selU.has(w))],
    exclusions: [...(m.options.blacklist || []).filter((w) => m.selE.has(w))],
  }
  if (manual) {
    extra.manual = [manual]
    // 桌面 do_exclude 口径：手填词**也**并入排除词（两头都不缺）
    if (!extra.exclusions.includes(manual)) extra.exclusions.push(manual)
  }
  if (correct.length) extra.correct = correct
  // V0.0.56：类别原因（"主题不符/低质/…"）随动作落库到 reviews，供统计。
  if (m.cats && m.cats.size) extra.reason_cat = [...m.cats]
  m.submitting = true
  busy.value = true
  try {
    const res = await api.imageAction(m.ids, 'dislike', extra)
    notifyLearned(res)
    await afterAction(m.ids, 'dislike')
    dislikeModal.value = null
  } catch (e) {
    err.value = e.message
    m.submitting = false
  } finally {
    busy.value = false
  }
}
async function dmPure() {
  // 「单纯不喜欢」：不带词级理由，但保留已勾的类别原因与「正确标签」
  const m = dislikeModal.value
  if (!m) return
  const correct = m.mis
    ? String(m.correct || '').split(/\s+/).filter(Boolean).slice(0, 10) : []
  const extra = { unmatched: [], correct }
  if (m.cats && m.cats.size) extra.reason_cat = [...m.cats]
  busy.value = true
  try {
    const res = await api.imageAction(m.ids, 'dislike', extra)
    notifyLearned(res)
    await afterAction(m.ids, 'dislike')
    dislikeModal.value = null
  } catch (e) {
    err.value = e.message
  } finally {
    busy.value = false
  }
}

// 缩略图按档位要不同宽度（后端 ?w= 生成并缓存，换档只是换一个文件）
function thumbSrc(it) {
  const u = it.thumb_url || it.url || ''
  if (!u.startsWith('/thumb/')) return u
  const w = THUMB_W[thumbSize.value] || 360
  return u + (u.includes('?') ? '&' : '?') + 'w=' + w
}

// —— 标签编辑（对齐老界面「加标签 / 删标签」）——
async function openTagModal() {
  const ids = [...selected.value]
  if (!ids.length) return
  try {
    const d = await api.imageTags(ids)
    tagModal.value = { ids, union: d.tags || [], input: '' }
  } catch (e) { err.value = e.message }
}
async function tagAdd() {
  const m = tagModal.value
  const word = String(m?.input || '').trim()
  if (!m || !word) { err.value = '请先填写标签名'; return }
  try {
    await api.imageTagsSave(m.ids, [word], 'add')
    await openTagModal()          // 刷新勾选清单
    m.input = ''
  } catch (e) { err.value = e.message }
}
async function tagRemove(word) {
  const m = tagModal.value
  if (!m) return
  try {
    await api.imageTagsSave(m.ids, [word], 'remove')
    await openTagModal()
  } catch (e) { err.value = e.message }
}

function fmtSize(b) {
  if (!b) return ''
  if (b > 1024 * 1024) return (b / 1024 / 1024).toFixed(1) + ' MB'
  return Math.round(b / 1024) + ' KB'
}

// —— V0.0.62：工具栏里的 AI 识别控件（图库视图专用，识别对象是已入库图）——
const fmtCnt = (n) => (Number(n) || 0).toLocaleString('zh-CN')
const tagShow = computed(() => props.view === 'gallery' &&
  (tagImp.running || tagImp.starting || tagImp.pending > 0))
const tagLabel = computed(() => {
  if (tagImp.running) return `识别中 ${fmtCnt(tagImp.done)}${tagImp.total ? ' / ' + fmtCnt(tagImp.total) : ''}`
  if (tagImp.starting) return '正在启动…'
  return tagImp.pending > 0 ? `还有 ${fmtCnt(tagImp.pending)} 张待识别` : '全部已识别'
})
const tagBtn = computed(() => {
  if (tagImp.running) return { cls: 'btn xs danger', icon: 'pause', text: '停止' }
  return { cls: 'btn xs primary', icon: 'play',
           text: tagImp.pending > 0 ? `识别这 ${fmtCnt(tagImp.pending)} 张` : '启动识别' }
})
watch(() => tagImp.doneSeq, () => { refreshTagPending() })
watch(() => tagImp.running, (v) => { if (!v) refreshTagPending() })

// —— V0.0.12：批量重命名（桌面图库菜单同款，走 rename_ops 同一份实现）——
const renameModal = ref(null)     // { ids, mode, value }
function openRenameModal() {
  const ids = [...selected.value]
  if (!ids.length) return
  renameModal.value = { ids, mode: 'prefix', value: '' }
}
async function doRename() {
  const m = renameModal.value
  if (!m) return
  if (!String(m.value || '').trim()) { err.value = '请先填写前缀或模板'; return }
  try {
    const r = await api.imageRename(m.ids, m.mode, m.value.trim())
    if (!r.ok) { err.value = r.error || '重命名失败'; return }
    let msg = `已重命名 ${r.renamed} 张`
    if (r.skipped_external) msg += `，跳过原有图库 ${r.skipped_external} 张（文件不改动）`
    toast(msg)
    renameModal.value = null
    await load(false)
  } catch (e) { err.value = e.message }
}

onMounted(async () => {
  // 任务下拉：只拉一次（任务不会频繁变）
  try {
    const d = await api.tasks()
    taskList.value = (d.tasks || d || []).map((x) => ({ id: x.id, name: x.name }))
  } catch { /* 拉不到就不显示下拉 */ }
  try {
    const tg = await api.tags()
    tagList.value = (tg.items || []).map((x) => ({ tag: x.tag, n: x.n }))
  } catch { /* 拉不到就不显示标签下拉 */ }
  load(false)
  watchImages()
  nextTick(() => {
    // V0.0.52：滚动容器是 <main class="main">，以前用默认视口判断哨兵，
    // 主区域滚到底了哨兵还被当成「可见」，会连着拉几十页。
    if (typeof IntersectionObserver === 'undefined' || !sentinel.value) return
    const root = gridEl.value?.closest('.main') || null
    io = new IntersectionObserver((es) => {
      if (es.some((e) => e.isIntersecting) && hasMore.value && !loading.value) load(true)
    }, { root, rootMargin: '600px' })
    io.observe(sentinel.value)
  })
})
onMounted(() => {
  window.addEventListener('keydown', onKey)
  unbindShell = [
    onShellSelectAll(() => selectAllForce()),
    onShellRefresh(() => load(false)),
  ]
})
onUnmounted(() => {
  if (io) io.disconnect()
  if (imgIO) imgIO.disconnect()
  stopSemBfPoll()
  window.removeEventListener('keydown', onKey)
  unbindShell.forEach((fn) => fn())
  unbindShell = []
  stopPlay()
  lockScroll(false)
})

function onKey(e) {
  if (!lb.value) return
  if (e.key === 'Escape') { lbFs.value ? (lbFs.value = false) : closeLightbox() }
  else if (e.key === 'ArrowLeft') step(-1)
  else if (e.key === 'ArrowRight') step(1)
  else if (e.key === '+' || e.key === '=') zoomBy(1.25)
  else if (e.key === '-') zoomBy(1 / 1.25)
  else if (e.key === '0') zoomSet(1)
  else if (e.key === 'r' || e.key === 'R') rotBy()
  else if (e.key === 'f' || e.key === 'F') lbFs.value = !lbFs.value
  else if (e.key === ' ') { e.preventDefault(); togglePlay() }
  else { return }
  e.preventDefault?.()
}
watch(() => [props.view], reload)
watch(sort, reload)
watch(flag, reload)
watch(taskFilter, reload)
watch(tagFilter, reload)

// ── R244 标签树（多级分类）────────────────────────────────────────────
/** 把平铺的标签表拼成树：`a/b/c` → a → b → c，计数向上累加。 */
const tagTree = computed(() => {
  const root = { children: new Map(), n: 0 }
  for (const it of tagList.value) {
    const parts = String(it.tag || '').split('/').map((x) => x.trim()).filter(Boolean)
    if (!parts.length) continue
    let node = root
    let path = ''
    for (let i = 0; i < parts.length; i++) {
      path = path ? `${path}/${parts[i]}` : parts[i]
      let child = node.children.get(parts[i])
      if (!child) {
        child = { label: parts[i], path, children: new Map(), n: 0, self: 0 }
        node.children.set(parts[i], child)
      }
      child.n += Number(it.n || 0)
      if (i === parts.length - 1) child.self += Number(it.n || 0)
      node = child
    }
  }
  return root
})

/** 摊平成可渲染的行（遵守折叠状态）。递归组件不划算：一行一个按钮就够。 */
const tagRows = computed(() => {
  const rows = []
  const walk = (node, depth) => {
    const kids = [...node.children.values()].sort(
      (a, b) => (b.n - a.n) || a.label.localeCompare(b.label, 'zh'))
    for (const k of kids) {
      const open = !tagCollapsed.value.has(k.path)
      rows.push({ path: k.path, label: k.label, n: k.n, self: k.self,
                  depth, hasKids: k.children.size > 0, open })
      if (k.children.size && open) walk(k, depth + 1)
    }
  }
  walk(tagTree.value, 0)
  return rows
})

/** 有没有任何标签用到分层（没有就不显示「可分层」的提示，免得像故障）。 */
const tagHasTree = computed(() => tagRows.value.some((r) => r.hasKids))

/**
 * 下拉里的层级显示（2026-10-29 第 1 条）：标签树搬进 <select>。
 * 缩进必须用**全角空格**（U+3000）—— 半角空格在 option 里会被 HTML 折叠掉，
 * 层级就看不出来了。末尾带计数，跟原来侧栏的 `名称 … 12` 一致。
 */
function tagOptionLabel(tg) {
  const pad = '\u3000'.repeat(Math.max(0, Number(tg.depth) || 0))
  return `${pad}${tg.label}（${tg.n}）`
}

function toggleTagNode(path) {
  const s = new Set(tagCollapsed.value)
  if (s.has(path)) s.delete(path); else s.add(path)
  tagCollapsed.value = s
}
watch([orient, res], reload)
// 换档只改图片地址，不重新取数；重新绑定视口观察，让可见卡片换成新尺寸。
watch(thumbSize, () => { watchImages() })
let qTimer = null
watch(q, () => {
  clearTimeout(qTimer)
  qTimer = setTimeout(reload, 350)
})

// ── V0.0.49 · 语义搜索直接集成在原搜索框 ──────────────────────
// 用户要求：像飞牛 Seek 那样，直接在搜索框输入画面特征就找图，不要再多点一个
// 做法：按输入内容自动判断走哪条路 ——
//   · 含结构化语法（tag:/site:/score> 等）或路径/扩展名 → 走原有精确搜索；
//   · 其它（「比基尼」「白丝」「海边日落」这类自然语言）→ 走向量语义搜索。
// 这样两种找法共用一个框，用户不用知道有「模式」这回事。
const STRUCT_RE = /\b(?:tag|site|score|fav|black|quar|review|w|h|width|height|minw|minh)\s*[:><=]|(?:^|\s)-[^\s]+|fav\b|black\b|quar\b|review\b|[\\/]|\.(?:jpg|jpeg|png|webp|gif|avif|bmp)\b/i
const semAuto = computed(() => {
  const kw = q.value.trim()
  if (!kw) return false
  // 结构化查询 / 路径 / 文件名 → 精确搜索；纯特征词 → 语义
  return !STRUCT_RE.test(kw)
})
// 兼容旧模板/逻辑里的 semMode 引用：语义模式 = 自动判定 && 不在相似图模式
const semMode = computed(() => semAuto.value && !simInfo.value)
const semNote = ref('')
const semBusy = ref(false)
// 补算状态：{ pending, running, job, pct, msg }
const semBf = ref(null)
let semBfTimer = 0

// 输入变化驱动的自动搜索：首次命中语义查询时查一次补算状态。
// 模式由输入内容自动决定。
let _semWasAuto = false
watch(semAuto, (on) => {
  if (on && !_semWasAuto) checkSemStatus()
  _semWasAuto = on
})

async function checkSemStatus() {
  try {
    const d = await api.semanticStatus()
    if (!d.ok) { semBf.value = null; return }
    if (!d.available) { err.value = d.reason || '模型不可用'; return }
    semBf.value = d.pending > 0 ? { pending: d.pending, running: false } : null
  } catch { semBf.value = null }
}

async function startBackfill() {
  const d = await api.semanticBackfill()
  if (!d.ok) { err.value = d.error || '补算起不来'; return }
  semBf.value = { pending: semBf.value?.pending || 0, running: true,
                  job: d.job, pct: 0, msg: '正在准备…' }
  stopSemBfPoll()
  semBfTimer = setInterval(pollBackfill, 2000)
}

async function pollBackfill() {
  const j = semBf.value
  if (!j?.job) return stopSemBfPoll()
  try {
    const d = await api.importJob(j.job)
    const pct = Math.max(0, Math.min(100, Number(d.pct) || 0))
    if (d.state === 'running') {
      semBf.value = { ...j, pct, msg: d.message || '正在补算…' }
      return
    }
    stopSemBfPoll()
    if (d.state === 'failed') err.value = d.error || '补算失败'
    semBf.value = null
    if (q.value.trim()) loadSemantic()   // 补完了重搜一次
  } catch { stopSemBfPoll() }
}

function stopSemBfPoll() {
  if (semBfTimer) { clearInterval(semBfTimer); semBfTimer = 0 }
}

async function cancelBackfill() {
  try { await api.semanticBackfillCancel() } catch { /* 已经停了 */ }
}

// 语义模式：一次拉一整批（无游标分页），结果按相似度排好序直接进 items。
async function loadSemantic() {
  const kw = q.value.trim()
  if (!kw) { items.value = []; err.value = ''; semNote.value = ''; return }
  semBusy.value = true
  try {
    const d = await api.semanticSearch(kw, {
      task: taskFilter.value || '', limit: pageSize.value,
    })
    if (!d.ok) { err.value = d.error || '画面特征检索失败'; items.value = [] }
    else {
      items.value = d.items || []
      semNote.value = d.note || ''
      err.value = ''
      if (d.empty) err.value = d.message || '没找到足够像的图'
    }
    hasMore.value = false          // 语义结果一次给全，不分页
  } catch (e) {
    err.value = e.message
  } finally {
    semBusy.value = false
    loading.value = false
    watchImages()
  }
}

const viewLabel = { gallery: '图库', pending: '待审核', quarantine: '隔离区', dedup: '去重输家' }

// 同一个 like 动作，在不同视图里语义不同：
//   隔离区 → 还原到图库（后端 act=like 且原来是隔离态时会清 blacklisted）
//   审核页 → 通过
//   图库   → 喜欢
// 按钮上必须说实话，否则用户在隔离区点「喜欢」根本猜不到文件被放回去了。
const likeLabel = computed(() =>
  props.view === 'quarantine' ? '还原' : (props.reviewMode ? '通过' : '喜欢'))
</script>

<template>
  <div class="viewwrap">
   <div class="gb">
    <!-- 2026-10-29（第 1 条）：左侧那根「按标签 / 全部标签」栏撤掉了。
         用户原话「放在这里太突兀了」—— 任务筛选上一轮已经改成工具栏下拉，
         标签还留在侧栏，两套交互并排，视觉上确实突兀。
         现在标签也进工具栏、紧跟在「全部任务」后面；层级靠全角空格缩进保留
         （`场景/室内` 仍然看得出父子关系），计数据跟在名字后面。 -->
    <div class="gmain">
    <div class="toolbar card">
      <div class="left">
        <select v-model="sort" title="排序方式">
          <option value="time">最新采集</option>
          <option value="name">按文件名</option>
          <option value="size">按大小</option>
          <option value="score">按 AI 评分</option>
          <option value="site">按站点</option>
        </select>
        <!-- V0.0.14：图库快捷筛选，补齐桌面端早有的三种视图。 -->
        <select v-model="flag" title="快捷筛选">
          <option value="">全部图片</option>
          <option value="fav">仅显示收藏</option>
          <option value="unseen">尚未查看</option>
          <option value="missing">文件缺失</option>
        </select>
        <!-- 2026-10-29（第 1 条）：按任务筛选紧跟在排序后面，标签紧跟任务 ——
             两个筛选挨着，语义一致（"看哪一批"→"看哪个分类"）。 -->
        <select v-model="taskFilter" class="tasksel" :title="`按任务筛选（共 ${taskList.length} 个）`">
          <option value="">全部任务</option>
          <option v-for="tt in taskList" :key="'t' + tt.id" :value="String(tt.id)">
            {{ tt.name }}（#{{ tt.id }}）
          </option>
        </select>
        <select v-model="tagFilter" class="tagsel" :title="`按标签筛选（共 ${tagList.length} 个标签）`">
          <option value="">全部标签</option>
          <option v-for="tg in tagRows" :key="'g' + tg.path" :value="tg.path">
            {{ tagOptionLabel(tg) }}
          </option>
        </select>
        <input v-model="q" type="text"
               placeholder="搜索画面特征（如 比基尼 / 白丝 / 海边日落）或标签 / 路径；支持 tag:白丝 site:xxx score>0.5 -排除" />
        <select v-model="orient" title="按画面方向筛选">
          <option value="">全部方向</option>
          <option value="land">横图</option>
          <option value="port">竖图</option>
          <option value="square">正方形</option>
        </select>
        <select v-model="res" title="按长边分辨率筛选">
          <option value="">全部分辨率</option>
          <option value="s">小于 1280</option>
          <option value="m">1280 – 1920</option>
          <option value="l">1920 – 2560</option>
          <option value="xl">2560 以上</option>
        </select>
        <select v-model="thumbSize" title="缩略图大小">
          <option value="s">小图</option>
          <option value="m">中图</option>
          <option value="l">大图</option>
        </select>
        <!-- V0.0.62（用户）：AI 识别控件并入这一行白框，只留标识/张数/按钮，
             不要「可随时启停」那种说明文字。全部识别完且不跑时整组隐藏。 -->
        <span v-if="tagShow" class="tagctl" :title="tagLabel">
          <Icon :anim="tagImp.running ? 'spin' : ''"
                :name="tagImp.running ? 'loader-circle' : 'sparkles'" :size="13" />
          <b>AI 识别</b>
          <span class="muted small tagnum">{{ tagLabel }}</span>
          <button :class="tagBtn.cls" :disabled="tagImp.starting"
                  @click="tagImp.running ? cancelTagging() : startTagging()">
            <Icon :name="tagBtn.icon" :size="12" />{{ tagBtn.text }}</button>
        </span>
      </div>
      <div class="right">
        <span class="muted small counttxt" v-if="items.length">{{ items.length }} 张</span>
        <!-- V0.0.46：每次加载多少张（用户要求，审核/图库/隔离区共用）。
             往下滚到底会自动续加载，这个数只管「一次拉多少」。 -->
        <label class="pagesize" title="每次加载多少张：往下滚动会自动续加载，这个数字只决定一次拉多少">
          <span>每页</span>
          <select :value="pageSize" @change="setPageSize($event.target.value)">
            <option v-for="n in PAGE_SIZES" :key="n" :value="n">{{ n }} 张</option>
          </select>
        </label>
        <!-- 2026-10-27：文案改成实话 —— 滚了几页它就是选**全部已加载**，
             原来写「本页」会让用户以为只选了看得见的那一屏，一删删掉几百张。 -->
        <button class="btn sm ghost" :disabled="!items.length" @click="selectAllInPage"
                :title="(items.length && selected.size === items.length) ? '取消全选' : `全选已加载（${items.length}）`">
          <Icon name="square-check-big" :size="14" />
          <span class="bt">{{ (items.length && selected.size === items.length) ? '取消全选' : `全选已加载（${items.length}）` }}</span>
        </button>
        <span v-if="selCount" class="pill warn">已选 {{ selCount }}</span>
        <button class="btn sm" :disabled="!selCount || busy" :title="likeLabel" @click="act('like')">
          <Icon name="thumbs-up" :size="14" /><span class="bt">{{ likeLabel }}</span>
        </button>
        <button class="btn sm" :disabled="!selCount || busy" title="收藏" @click="act('favorite')">
          <Icon name="star" :size="14" /><span class="bt">收藏</span>
        </button>
        <button class="btn sm" :disabled="!selCount || busy" title="不喜欢" @click="act('dislike')">
          <Icon name="thumbs-down" :size="14" /><span class="bt">不喜欢</span>
        </button>
        <button class="btn sm" :disabled="!selCount || busy" title="标签…" @click="openTagModal">
          <Icon name="tags" :size="14" /><span class="bt">标签…</span>
        </button>
        <button v-if="props.renamable" class="btn sm" :disabled="!selCount || busy"
                title="给选中文件名加前缀，或按模板批量重命名（原有图库的文件不会改动）"
                @click="openRenameModal">
          <Icon name="pencil" :size="14" /><span class="bt">重命名…</span>
        </button>
        <button class="btn sm danger" :disabled="!selCount || busy" title="删除" @click="act('delete')">
          <Icon name="trash" :size="14" /><span class="bt">删除</span>
        </button>
        <button v-if="selCount" class="btn sm ghost" title="取消选择" @click="selected = new Set()">
          <Icon name="x" :size="14" />
        </button>
      </div>
    </div>

    <div v-if="simInfo" class="simbanner card">
      <span>
        正在看 <b>#{{ simInfo.seed }}</b> 的相似图 —— {{ simInfo.n }} 张
        <span class="muted small">（从 {{ simInfo.candidates }} 张候选里比出来的）</span>
      </span>
      <button class="btn sm" @click="exitSimilar">← 返回图库</button>
    </div>

    <!-- 语义检索状态条 —— 实际拿什么词在检索（中文会翻成英文特征） -->
    <p v-if="semMode && semNote" class="semnote muted small">
      <Icon name="sparkles" :size="13" />{{ semNote }}
      <span v-if="items.length"> · 按相似度排了 {{ items.length }} 张</span>
    </p>
    <!-- V0.0.46：历史图还没建「画面特征」时的补算入口（一次性，跑完不再出现） -->
    <div v-if="semMode && semBf" class="semnote bf">
      <template v-if="semBf.running">
        <Icon anim="spin" name="loader-circle" :size="13" />
        <span>{{ semBf.msg }}</span>
        <span class="bar"><i :style="{ width: Math.max(2, semBf.pct) + '%' }" /></span>
        <button class="btn sm danger" @click="cancelBackfill">停止</button>
      </template>
      <template v-else>
        <Icon name="sparkles" :size="13" />
        <span>图库里还有 {{ semBf.pending.toLocaleString() }} 张图没建画面特征，
              这些图像不会出现在画面特征检索结果里。</span>
        <button class="btn sm primary" @click="startBackfill">补算画面特征</button>
      </template>
    </div>
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="toastMsg" class="goodbar">{{ toastMsg }}</p>

    <div ref="gridEl" class="grid" :class="'size-' + thumbSize"
         @pointerdown="onGridDown">
      <!-- 2026-10-28（对标 qBittorrent 传输列表的框选）：按住空白处拖一个框，
           框到的卡片全选中。用 fixed 定位 + pointer-events:none，不参与布局。 -->
      <div v-if="marquee" class="marquee" :style="{
             left: marquee.x1 + 'px', top: marquee.y1 + 'px',
             width: (marquee.x2 - marquee.x1) + 'px',
             height: (marquee.y2 - marquee.y1) + 'px' }"></div>
      <figure
        v-for="(it, i) in cols" :key="it.id"
        :data-id="it.id" draggable="false"
        class="cell" :class="{ picked: selected.has(it.id) }"
        @click.exact="onCardClick(it, i)"
        @dblclick.prevent="onCardDblClick(it, i)"
        @contextmenu="onCardContext($event, it)">
        <!-- V0.0.52：真实地址放 data-src，由 watchImages 按视口决定何时装载 -->
        <img src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs="
             :data-src="thumbSrc(it)" :alt="it.meta" />
        <!-- 语义模式角标 = 与查询的画面相似度；普通模式 = AI 主题分 -->
        <span v-if="semMode && it.sem_score != null" class="score">相似 {{ it.sem_score }}%</span>
        <span v-else-if="!semMode && it.score != null" class="score">{{ Math.round(it.score * 100) }}%</span>
        <span class="tick" @click.stop="toggleSel(it.id)" :class="{ on: selected.has(it.id) }">✓</span>
        <!-- V0.0.63（用户拍板 A+D）：A = 右上角识别徽章（只标已识别，
             待识别满屏问号太吵）；D = 文字行胶囊。紧贴勾选圈左侧，不冲突。 -->
        <span v-if="it.ai === 1" class="ai-mark" title="已完成 AI 识别">✓</span>
        <figcaption>
          <span class="meta">{{ it.meta }}</span>
          <span class="dim mono">{{ it.width }}×{{ it.height }} · {{ fmtSize(it.bytes) }}
            <span class="ai-chip" :class="{ none: it.ai !== 1 }"
                  :title="it.ai === 1 ? '已完成 AI 识别' : '尚未 AI 识别'">
              {{ it.ai === 1 ? '✦ 已识别' : '✧ 待识别' }}</span>
          </span>
          <!-- V0.0.8：待审核卡片显示「为什么被送进来」，审核页不再靠猜 -->
          <span v-if="it.reason" class="reason" :title="it.reason">{{ it.reason }}</span>
        </figcaption>
      </figure>
    </div>

    <div ref="sentinel" class="sentinel">
      <span v-if="hasMore && items.length >= autoLimit" class="muted small"
            style="display:block;margin-bottom:6px">已显示 {{ items.length }} 张；自动加载已暂停，继续加载会占用更多内存。</span>
      <button v-if="hasMore" class="btn" :disabled="loading" @click="loadMoreManually">
        <Icon v-if="loading" anim="spin" :size="14" />
        {{ loading ? '加载中…' : (items.length >= autoLimit ? '继续加载（再显示 600 张）' : '加载更多') }}
      </button>
      <span v-else-if="items.length" class="muted">— 已到底（{{ items.length }} 张）—</span>
      <span v-else-if="!loading" class="muted">当前视图无图片</span>
    </div>
    </div>
   </div>

    <!-- 灯箱
         V0.0.46：用 Teleport 挂到 <body>。原来这层 `position:fixed` 藏在
         `.main`（滚动容器）+ 路由过渡（transform）里 —— 祖先一旦形成层叠
         上下文/包含块，fixed 就会被算到祖先坐标系、并被 sticky 顶栏
         （z-index:20）压在下面，于是「灯箱开着、上方『图库』标题还亮着」
         （用户报的 bug）。挂到 body 彻底脱离这些祖先，遮罩必然盖住整屏。 -->
    <Teleport to="body">
    <div v-if="lb" class="lb" :class="{ 'lb-fs': lbFs }" @click.self="closeLightbox">
      <button class="lb-close" title="关闭（Esc）" @click="closeLightbox">
        <Icon name="x" :size="20" :stroke="2.2" />
      </button>
      <button class="lb-nav prev" :disabled="lbIndex <= 0" title="上一张（←）" @click="step(-1)">
        <Icon name="chevron-left" :size="26" />
      </button>
      <button class="lb-sim" :disabled="simBusy" title="按标签 / 哈希 / 主色找同类画面"
              @click="findSimilar(lb)">
        <Icon :anim="simBusy ? 'spin-ring' : ''" name="scan-search" :size="14" />{{ simBusy ? '找相似…' : '找相似' }}
      </button>
      <div class="lb-body" @wheel.prevent="onWheel" @click.self="closeLightbox">
        <!-- V0.0.35：图片单独占一层「舞台」。以前图片和底栏是同一个 grid 的
             两行，大图会把行撑开、底栏被顶出屏幕；舞台 flex:1 + min-height:0
             后，图片永远只有舞台那么大，底栏始终固定在下方。
             舞台空白处点击＝关闭灯箱（遮罩层本身也支持）。 -->
        <div class="lb-stage" @click.self="closeLightbox">
          <img :class="{ zoomed: lbZoom > 1 }"
               :style="{ transform: `translate(${lbPan.x}px, ${lbPan.y}px) rotate(${lbRot}deg) scale(${lbZoom})` }"
               :src="lb.orig_url || lb.full_url || lb.url"
               :alt="lb.meta"
               @pointerdown="onDragStart" @pointermove="onDragMove"
               @pointerup="onDragEnd" @pointercancel="onDragEnd"
               @click.stop="lbZoom <= 1 && (lbFs = !lbFs)" />
        </div>
        <div class="lb-bar">
          <span class="pill muted mono">{{ lbIndex + 1 }} / {{ items.length }}</span>
          <span class="mono">#{{ lb.id }} · {{ lb.meta }}</span>
          <span class="muted small">
            {{ lb.width || '?' }}×{{ lb.height || '?' }} · {{ fmtSize(lb.bytes) }}
            <template v-if="lb.collected_at"> · {{ String(lb.collected_at).slice(0, 16) }}</template>
          </span>
          <!-- V0.0.8：点开大图逐张审时同样看得见「为什么被送进来」 -->
          <span v-if="lb.reason" class="reason" :title="lb.reason">{{ lb.reason }}</span>
          <span class="spacer"></span>
          <!-- 视图控制（对齐老界面：缩放/适应/旋转/全屏/幻灯片） -->
          <span class="zoomg">
            <button class="btn sm" title="缩小（-）" @click="zoomBy(1 / 1.25)">
              <Icon name="zoom-out" :size="14" /></button>
            <button class="btn sm mono" title="适应窗口（0）" @click="zoomSet(1)">{{ Math.round(lbZoom * 100) }}%</button>
            <button class="btn sm" title="放大（+）" @click="zoomBy(1.25)">
              <Icon name="zoom-in" :size="14" /></button>
            <button class="btn sm" title="旋转 90°（R）" @click="rotBy">
              <Icon name="rotate-cw" :size="14" /></button>
            <button class="btn sm" :class="{ on: lbFs }" title="全屏查看（F）" @click="lbFs = !lbFs">
              <Icon :name="lbFs ? 'minimize-2' : 'maximize-2'" :size="14" /></button>
            <button class="btn sm" :class="{ on: lbPlay }" title="幻灯片（空格）" @click="togglePlay">
              <Icon :name="lbPlay ? 'pause' : 'play'" :size="14" /></button>
            <button class="btn sm" title="切换幻灯片速度" @click="cycleSpeed">{{ lbSpeed / 1000 }}s</button>
            <button class="btn sm" :class="{ on: lbLoop }" title="播完是否回到第一张" @click="lbLoop = !lbLoop">∞</button>
          </span>
          <button class="btn sm" :disabled="busy" title="收藏" @click="act('favorite', [lb.id])">
            <Icon name="star" :size="14" /></button>
          <button class="btn sm" :disabled="busy" @click="act('like', [lb.id])">
            <Icon name="thumbs-up" :size="14" />{{ likeLabel }}</button>
          <button class="btn sm" :disabled="busy" @click="act('dislike', [lb.id])">
            <Icon name="thumbs-down" :size="14" />不喜欢</button>
          <a class="btn sm" :href="lb.orig_url || lb.url" :download="''">
            <Icon name="download" :size="14" />下载</a>
          <a v-if="lb.source_url" class="btn sm ghost" :href="lb.source_url"
             target="_blank" rel="noopener"><Icon name="external-link" :size="14" />原页</a>
          <button class="btn sm danger" :disabled="busy" @click="act('delete', [lb.id])">
            <Icon name="trash" :size="14" />删除</button>
          <button class="btn sm ghost" @click="closeLightbox"><Icon name="x" :size="14" />关闭</button>
        </div>
      </div>
      <button class="lb-nav next" :disabled="lbIndex >= items.length - 1" title="下一张（→）" @click="step(1)">
        <Icon name="chevron-right" :size="26" />
      </button>
    </div>
    </Teleport>

    <ContextMenu :menu="ctx" @close="ctx = null" />

    <!-- 标签编辑（对齐老界面「加标签 / 删标签」） -->
    <Teleport to="body">
    <div v-if="tagModal" class="lb" @click.self="tagModal = null">
      <div class="modal card">
        <h3>编辑 {{ tagModal.ids.length }} 张图的标签</h3>
        <p class="muted small">加标签：给选中的每张都追加（已存在的不会重复）。
          删标签：从下面这份<b>并集</b>里点掉——只删你点的那个词。</p>
        <div class="inline">
          <input v-model="tagModal.input" type="text" placeholder="新标签名（如 白丝）"
                 @keyup.enter="tagAdd" />
          <button class="btn primary sm" @click="tagAdd">加上去</button>
        </div>
        <div class="chipwrap" v-if="tagModal.union.length">
          <div class="chip-h">单击即从当前选择中移除该标签</div>
          <button v-for="w in tagModal.union" :key="w" class="chip warn" @click="tagRemove(w)">
            {{ w }} ✕
          </button>
        </div>
        <p v-else class="muted small">这些图还没有标签</p>
        <div class="modal-foot">
          <button class="btn" @click="tagModal = null">关闭</button>
        </div>
      </div>
    </div>
    </Teleport>

    <!-- 批量重命名（V0.0.12，桌面图库菜单同款；原有图库文件后端红线跳过） -->
    <Teleport to="body">
    <div v-if="renameModal" class="lb" @click.self="renameModal = null">
      <div class="modal card">
        <h3>重命名 {{ renameModal.ids.length }} 张</h3>
        <div class="inline" style="margin-bottom:8px">
          <label><input type="radio" value="prefix" v-model="renameModal.mode" /> 加前缀</label>
          <label><input type="radio" value="template" v-model="renameModal.mode" /> 按模板</label>
        </div>
        <input v-if="renameModal.mode === 'prefix'" v-model="renameModal.value"
               type="text" placeholder="前缀（如 精选_）" @keyup.enter="doRename" />
        <template v-else>
          <input v-model="renameModal.value" type="text"
                 placeholder="{date}_{site}_{n}" @keyup.enter="doRename" />
          <p class="muted small">占位符：{n}=序号 {orig}=原名 {date}=今天
            {site}=站点 {task}=任务名</p>
        </template>
        <p class="muted tiny">属于你原有图库的文件不会被改动（自动跳过并计数）。</p>
        <div class="modal-foot">
          <button class="btn" @click="renameModal = null">取消</button>
          <button class="btn primary" @click="doRename">执行</button>
        </div>
      </div>
    </div>
    </Teleport>

    <!-- 不喜欢原因（全页面统一：审核+图库都先问，与桌面两端对齐）。
         V0.0.12：升级为桌面 dislike_dialog 同款——两栏多选（各带全选）、
         手填框+词表桥接预演、「模型误判→正确标签」纠正通道。 -->
    <Teleport to="body">
    <div v-if="dislikeModal" class="lb" @click.self="dislikeModal = null">
      <div class="modal card dm">
        <h3>为什么不喜欢？</h3>
        <p class="muted small">
          <b>主题词</b>：勾选表示该图未匹配该词，仅下调该词权重，不会拉黑整个主题；<br />
          <b>排除词</b>：勾选或手填表示排除此类内容，写进任务排除词后下次运行自动跳过。
        </p>
        <div class="dm-cols">
          <div class="dm-col">
            <div class="dm-h"><span>主题词（未命中）</span>
              <button class="btn xs" @click="dmSelectAll('selU')">全选</button></div>
            <div class="dm-list">
              <label v-for="w in (dislikeModal.options.labels || [])" :key="'t' + w" class="dm-ck">
                <input type="checkbox" :checked="dislikeModal.selU.has(w)"
                       @change="dmToggle('selU', w)" /> {{ w }}
              </label>
              <p v-if="!(dislikeModal.options.labels || []).length" class="muted small">（无）</p>
            </div>
          </div>
          <div class="dm-col">
            <div class="dm-h"><span>排除词</span>
              <button class="btn xs" @click="dmSelectAll('selE')">全选</button></div>
            <div class="dm-list">
              <label v-for="w in (dislikeModal.options.blacklist || [])" :key="'e' + w" class="dm-ck">
                <input type="checkbox" :checked="dislikeModal.selE.has(w)"
                       @change="dmToggle('selE', w)" /> {{ w }}
              </label>
              <p v-if="!(dislikeModal.options.blacklist || []).length" class="muted small">（无）</p>
            </div>
            <input v-model="dislikeModal.manual" type="text"
                   placeholder="也可以手填排除原因（如 金发）" @input="dmManualInput" />
            <p v-if="dislikeModal.preview" class="dm-pv">{{ dislikeModal.preview }}</p>
          </div>
        </div>
        <!-- V0.0.56：类别原因标签（与词级分开，落 reviews.reason_cat）。
             可全不选；选了仅作统计/审计，不影响下面的词级学习逻辑。 -->
        <div class="dm-cats">
          <span class="muted small">原因类别（可多选，仅用于统计）：</span>
          <label v-for="c in ['主题不符','低质','重复','水印','来源不想要','稍后再看']"
                 :key="c" class="dm-tag">
            <input type="checkbox" :checked="dislikeModal.cats && dislikeModal.cats.has(c)"
                   @change="dmToggle('cats', c)" /> {{ c }}
          </label>
        </div>
        <div class="dm-mis">
          <label class="dm-ck">
            <input type="checkbox" v-model="dislikeModal.mis" />
            模型误判：这张属于我想要的目标内容，被错误标记
          </label>
          <input v-model="dislikeModal.correct" type="text" :disabled="!dislikeModal.mis"
                 placeholder="正确标签（多个用空格分隔，如 红丝 长袜）" />
          <p class="muted tiny">填了会作为<b>正向</b>信号学习，绝不会写进排除词。</p>
        </div>
        <div class="modal-foot">
          <button class="btn primary" :disabled="dislikeModal.submitting" @click="dmSubmit">
            {{ dislikeModal.submitting ? '提交中…' : '确认（加入勾选的排除词并移除）' }}
          </button>
          <button class="btn" @click="dmPure">不写原因，直接不喜欢</button>
          <button class="btn ghost" @click="dislikeModal = null">取消</button>
        </div>
      </div>
    </div>
    </Teleport>
  </div>
</template>

<style scoped>
.viewwrap { display: grid; gap: 12px; }
/* 左栏（任务/标签树）+ 右栏（工具条+图） */
/* 2026-10-29（第 1 条）：侧栏撤掉后只剩一栏，gb 退化成普通纵向容器。
   旧的 .gside/.gitem/.fg-* 规则保留着没有害处（选择器不再命中），
   等确认没人再要回侧栏时一起清。 */
.gb { display: grid; gap: 12px; align-items: start; }
.gmain { display: grid; gap: 12px; min-width: 0; }
.gside {
  padding: 8px 6px; display: grid; gap: 1px; align-content: start;
  max-height: calc(100vh - 190px); overflow: auto; position: sticky; top: 8px;
}
.fg-title { font-size: 11px; color: var(--muted); padding: 8px 8px 4px; letter-spacing: .4px; }
.gitem {
  display: flex; align-items: center; gap: 6px; width: 100%;
  border: 0; background: transparent; color: var(--text);
  padding: 4px 8px; border-radius: 7px; font-size: 13px; text-align: left;
}
.gitem:hover { background: #f5f3ff; }
.gitem.on { background: #efeaff; color: var(--brand); font-weight: 600; }
.gitem .fl { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.gitem .fc { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
.gitem.on .fc { color: var(--brand); }
/* R244 标签树：折叠箭头占位固定宽度，避免有/无子分类的行文字对不齐 */
.gitem .tw { display: inline-flex; width: 12px; height: 12px; align-items: center;
  justify-content: center; margin-left: -4px; color: var(--muted); }
.gitem .tw:hover { color: var(--brand); }
.fg-hint { font-size: 10.5px; padding: 2px 8px 6px; opacity: .8; }
.gitem.reset { margin-top: 8px; color: var(--muted); }
@media (max-width: 900px) {
  .gb { grid-template-columns: 1fr; }
  .gside {
    max-height: none; position: static; grid-auto-flow: column;
    grid-auto-columns: max-content; overflow-x: auto; padding: 6px;
  }
  .fg-title { display: none; }
  .gitem.reset { margin-top: 0; }
}
.toolbar .left select { width: 118px; }
.toolbar { display: flex; gap: 10px; align-items: center; justify-content: space-between; padding: 10px 12px; flex-wrap: wrap; }
.toolbar .left { display: flex; gap: 8px; flex: 1; min-width: 320px; flex-wrap: wrap; }
.toolbar .left select { width: 130px; }
/* 任务下拉给宽一点（任务名长）；标签下拉也放宽，层级缩进后文字更长 */
.toolbar .left select:nth-of-type(2) { width: 200px; }
.toolbar .left select.tagsel { width: 190px; }
.toolbar .right { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
/* V0.0.62：工具栏里的 AI 识别控件（图内角标语义 = 内联一组，不换整行） */
.tagctl { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap;
  padding: 2px 4px 2px 8px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--panel); color: var(--text); font-size: 12px; }
.tagctl b { font-size: 12px; font-weight: 600; }
/* V0.0.62（用户）：最小窗口 1024 下所有控件都要看得见 —— 下拉按选中项文字
   自适应收窄、动作按钮退成纯图标（悬停有说明）、计数与「每页」字样隐藏；
   实在放不下就自动换到第二行（wrap），宁可两行也绝不裁切溢出。 */
@media (max-width: 1120px) {
  .toolbar { gap: 6px 10px; padding: 8px 10px; }
  .toolbar .left { gap: 6px; min-width: 0; flex-wrap: wrap; flex: 1 1 auto; }
  .toolbar .left select { width: auto; flex: 0 1 auto; max-width: 112px;
    padding: 4px 4px 4px 6px; font-size: 12px; text-overflow: ellipsis; }
  .toolbar .left select:nth-of-type(2) { width: auto; }
  .toolbar .left select.tagsel { width: auto; }
  .toolbar .left input { min-width: 90px; flex: 1 1 110px; padding: 4px 8px; font-size: 12px; }
  .toolbar .right { gap: 6px; flex-wrap: wrap; flex: 0 1 auto; }
  .toolbar .right .btn.sm { padding: 4px 6px; font-size: 12px; border-radius: 7px;
    height: 26px; box-sizing: border-box; }
  .toolbar .right .pagesize { font-size: 11px; }
  .toolbar .right .pagesize > span, .toolbar .right .counttxt { display: none; }
  /* 批量动作按钮只留图标（文字进 title），选中计数仍显示 */
  .toolbar .right .btn.sm .bt { display: none; }
  .tagctl { padding: 2px 3px 2px 6px; gap: 4px; }
  .tagctl .tagnum { display: none; }
}
/* V0.0.49：语义检索 —— 状态条、补算条 */
.btn.sem.on { background: var(--brand); border-color: var(--brand); color: #fff; }
.semnote { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  margin: 0; padding: 8px 12px; border: 1px solid var(--line);
  border-radius: 10px; background: var(--panel); }
.semnote .bar { flex: 1 1 120px; max-width: 260px; height: 6px;
  border-radius: 999px; background: var(--line); overflow: hidden; }
.semnote .bar i { display: block; height: 100%; border-radius: 999px;
  background: var(--brand); transition: width .3s; }
/* V0.0.46：每页张数选择器 —— 和旁边的按钮同高、低调不抢视线 */
.pagesize { display: inline-flex; align-items: center; gap: 4px;
  font-size: 12px; color: var(--muted); white-space: nowrap; }
.pagesize select { padding: 3px 6px; font-size: 12px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--panel); color: var(--text); }
/* V0.0.59：按钮尺寸已统一到全局 style.css，此处局部覆盖删除 */
.btn.danger { color: var(--err); border-color: #f3c9c9; }
.btn.danger:hover { background: #fff5f5; }

.grid {
  display: grid; gap: 10px;
  grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
}
/* 缩略图档位：只改格子大小，不重新请求列表 */
.grid.size-s { grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); }
.grid.size-l { grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); }
.cell {
  margin: 0; position: relative; border-radius: 10px; overflow: hidden;
  background: #fff; border: 1px solid var(--line); cursor: zoom-in;
  transition: .15s;
}
.cell:hover { transform: translateY(-1px); box-shadow: 0 6px 18px rgba(16,24,40,.08); }
/* 框选：禁止拖图（否则浏览器开原生拖影，框选框不住）、禁止选中文字 */
.grid { user-select: none; }
.cell img { -webkit-user-drag: none; }
.marquee {
  position: fixed; z-index: 60; pointer-events: none; border-radius: 3px;
  border: 1px solid var(--brand); background: rgba(124, 92, 255, .12);
}
.cell.picked { outline: 2px solid var(--brand); }
.cell img { display: block; width: 100%; aspect-ratio: 1/1; object-fit: cover; background: #f2f3f8; }
.cell figcaption { padding: 6px 8px; display: grid; gap: 1px; }
.cell .meta { font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cell .dim { color: var(--muted); font-size: 11px; }
.tick {
  position: absolute; top: 6px; right: 6px; width: 22px; height: 22px;
  display: grid; place-items: center; border-radius: 50%;
  background: rgba(255,255,255,.85); border: 1px solid var(--line);
  font-size: 12px; color: transparent; cursor: pointer;
}
.tick.on { background: var(--brand); border-color: var(--brand); color: #fff; }
/* V0.0.63 方案A：已识别徽章（紫色圆点，贴在勾选圈左侧） */
.ai-mark {
  position: absolute; top: 8px; right: 33px; z-index: 2;
  width: 18px; height: 18px; border-radius: 50%;
  background: rgba(109, 94, 252, .92); color: #fff;
  font-size: 10px; font-weight: 700; display: grid; place-items: center;
  box-shadow: 0 1px 4px rgba(0, 0, 0, .25); pointer-events: auto;
}
/* V0.0.63 方案D：文字行胶囊（跟在尺寸行后；小图档文字行裁掉时靠 tooltip） */
.ai-chip {
  display: inline-flex; align-items: center; gap: 3px; margin-left: 4px;
  padding: 0 6px; border-radius: 999px; font-size: 10px;
  background: #efeaff; color: var(--brand); border: 1px solid #ddd6fb;
  white-space: nowrap;
}
.ai-chip.none { background: #f2f1f6; color: var(--muted); border-color: #e6e4ee; }
.sentinel { display: grid; place-items: center; padding: 14px; }

.lb {
  position: fixed; inset: 0; background: rgba(14,16,24,.72);
  display: grid; grid-template-columns: 56px 1fr 56px; align-items: center;
  z-index: 60; backdrop-filter: blur(2px);
}
.lb-nav { background: transparent; border: 0; color: #fff; font-size: 40px; opacity: .7; }
.lb-nav:hover { opacity: 1; }
/* V0.0.35：上下两段用 flex 分配，图片舞台吃掉剩余高度、底栏固定占自己那份。
   以前用 grid 两行 auto，超大图会把第一行撑到超过视口，底栏被挤下去看不见。 */
.lb-body {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 12px; min-height: 0;
  height: calc(100vh - 40px);
  padding: 8px 0;
  box-sizing: border-box;
  overflow: hidden;
}
.lb-stage {
  flex: 1 1 auto; min-height: 0; width: 100%;
  display: flex; align-items: center; justify-content: center;
  overflow: hidden;
}
.lb-stage img {
  /* 默认**适应边框**：宽高都不超过舞台，且绝不放大（小图按原尺寸居中）。
     object-fit: contain 保证不裁切。 */
  max-width: min(100%, 1600px);
  max-height: 100%;
  width: auto; height: auto;
  object-fit: contain;
  border-radius: 8px;
  cursor: zoom-in;
  /* 缩放/旋转用 transform 实现：不动布局、不重新解码，手机上才不卡。
     transform 不占布局 → 用拖动代替滚动条；这里不能给 auto，否则出现假滚动条。 */
  transition: transform .12s ease-out;
  will-change: transform;
}
.lb-stage img.zoomed { cursor: grab; }
.lb-stage img.zoomed:active { cursor: grabbing; }
/* 全屏：藏掉上下所有栏，只看图（比 Fullscreen API 稳 —— iOS Safari 不给用） */
.lb-fs .lb-nav, .lb-fs .lb-sim { opacity: 0; pointer-events: none; }
.lb-fs .lb-bar { display: none; }
.lb-fs .lb-body { height: 100vh; padding: 0; gap: 0; }
.lb-fs .lb-stage { width: 100vw; }
.lb-fs .lb-close { opacity: .35; }
.lb-fs .lb-close:hover { opacity: 1; }
/* V0.0.35：灯箱缩放按钮组原来 4px 缝，图标按钮挤成一坨；放宽到 6px */
.zoomg { display: inline-flex; gap: 6px; flex-wrap: wrap; }
.zoomg .btn.on { background: var(--brand); border-color: var(--brand); color: #fff; }
.lb-sim {
  position: fixed; top: 14px; right: 74px; z-index: 70;
  height: 40px; padding: 0 16px; border-radius: 20px;
  border: 1px solid rgba(255,255,255,.35);
  background: rgba(20,22,30,.78); color: #fff; font-size: 13px;
  box-shadow: 0 4px 14px rgba(0,0,0,.35);
}
.lb-sim:hover { background: #2563eb; border-color: #2563eb; }
.lb-sim:disabled { opacity: .6; }
.simbanner { display: flex; align-items: center; justify-content: space-between;
             gap: 10px; padding: 10px 14px; flex-wrap: wrap; }
.score {
  position: absolute; top: 6px; left: 6px; z-index: 2;
  padding: 1px 6px; border-radius: 999px; font-size: 11px;
  background: rgba(20,22,30,.72); color: #fff;
}
.cell { position: relative; }
.lb-close {
  position: fixed; top: 14px; right: 18px; z-index: 70;
  width: 44px; height: 44px; border-radius: 50%;
  border: 1px solid rgba(255,255,255,.35);
  background: rgba(20,22,30,.78); color: #fff;
  font-size: 26px; line-height: 1; display: grid; place-items: center;
  box-shadow: 0 4px 14px rgba(0,0,0,.35);
}
.lb-close:hover { background: #dc2626; border-color: #dc2626; }
.lb-bar {
  display: flex; flex: none; gap: 8px; align-items: center; width: min(900px, 92vw);
  background: #fff; border-radius: 10px; padding: 8px 12px;
  /* 2026-10-27：底栏有 8 个控件，窄屏一行塞不下会被挤出屏幕外
     （关闭键就点不到了）→ 允许换行。 */
  flex-wrap: wrap;
}
@media (max-width: 760px) {
  .lb { grid-template-columns: 32px 1fr 32px; }
  .lb-nav { font-size: 28px; }
  .lb-bar { width: 94vw; gap: 6px; padding: 8px; }
  .lb-sim { top: 10px; right: 66px; height: 34px; font-size: 12px; padding: 0 12px; }
  .lb-close { top: 10px; right: 12px; width: 40px; height: 40px; }
}
.lb-bar .spacer { flex: 1; }
.lb-bar .small { font-size: 12px; }

.modal { width: min(520px, 94vw); padding: 20px; display: grid; gap: 12px; }
.modal h3 { margin: 0; }
.chipwrap { display: flex; flex-wrap: wrap; gap: 6px; }
.chip-h { width: 100%; font-size: 12px; color: var(--muted); }
.chip {
  border: 1px solid var(--line); background: #fff; border-radius: 999px;
  padding: 4px 12px; font-size: 13px;
}
.chip:hover { border-color: var(--brand); color: var(--brand); }
.chip.warn:hover { border-color: var(--err); color: var(--err); }
.modal-foot { display: flex; gap: 8px; justify-content: flex-end; }
.err { color: var(--err); margin: 0; }
/* V0.0.12 判退模态（桌面 dislike_dialog 同款） */
.dm { max-width: 640px; }
.dm-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 8px 0; }
.dm-h { display: flex; align-items: center; justify-content: space-between;
        font-size: 12px; font-weight: 600; margin-bottom: 4px; }
.dm-list { max-height: 170px; overflow: auto; display: grid; gap: 2px;
           border: 1px solid var(--border, #444); border-radius: 8px; padding: 6px; }
.dm-ck { display: flex; gap: 6px; align-items: center; font-size: 13px;
         cursor: pointer; padding: 2px 4px; border-radius: 6px; }
.dm-ck:hover { background: var(--surface_alt, #f5f3ff); }
.dm-pv { font-size: 12px; color: var(--brand, #6d5bd0); margin: 4px 0 0; white-space: pre-wrap; }
.dm-cats { display: flex; flex-wrap: wrap; gap: 6px 8px; align-items: center;
  margin: 8px 0; padding: 8px 0; border-top: 1px dashed var(--border, #444); }
.dm-tag { display: inline-flex; gap: 4px; align-items: center; font-size: 12px;
  padding: 3px 8px; border: 1px solid var(--border, #444); border-radius: 12px; cursor: pointer; }
.dm-mis { border-top: 1px dashed var(--border, #444); margin-top: 6px; padding-top: 8px;
          display: grid; gap: 6px; }
.dm-mis .tiny { margin: 0; }
@media (max-width: 640px) { .dm-cols { grid-template-columns: 1fr; } }
.goodbar { color: var(--ok, #2c7a4b); margin: 0; font-size: 13px; }
.reason { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; font-size: 11px; color: var(--warn, #9a6b1f); line-height: 1.3; }
</style>
