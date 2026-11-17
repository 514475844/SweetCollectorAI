// 列宽可拖（R243）：把「固定 CSS grid 列宽」变成「用户自己拖、下次还记得」。
//
// 为什么值得做（r235 当时否掉过，理由是「拖宽了反而错位」）：
//   当时列宽写死在 CSS 里，拖动只会让单元格内容溢出。改成**真按宽度排**之后
//   反而是刚需 —— 任务名和入口 URL 长度差十几倍，用户屏幕上「任务名被截成 3 个字、
//   入口空着一大片」是常态。qBittorrent 的传输列表能拖列宽，用户就是拿它对比的。
//
// 三条设计约束（都是踩过坑才这么写）：
//   1. **窄屏交给 CSS**：手机上列数是另一套（少一列），行内 style 会盖掉媒体查询
//      ⇒ 窄屏时本组合式返回空对象，完全不插手；
//   2. **宽度要落盘**：每次刷新都回到默认值 = 用户白拖一次，localStorage 存住；
//   3. **只改宽度不改顺序**：列的含义是固定的（任务/状态/进度/入口/操作），
//      允许换序会让「双击行编辑」「右键菜单」这些按位置写死的交互错位。
import { ref, computed, onUnmounted } from 'vue'

const NARROW_PX = 820          // 与各页面 @media (max-width: 820px) 对齐
const STORE_PREFIX = 'mrm.cols.'

function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v))
}

/**
 * @param {string} key         存储键（每个页面一套，互不干扰）
 * @param {Array}  defaults    [{k, w, min, max}]，最后一项按 minmax(w, 1fr) 处理
 * @param {object} opts        compactUnder：小于这个宽度就不插手、交还 CSS
 *                             （2026-10-29 第 3 条新增）
 */
export function useColResize(key, defaults, opts = {}) {
  // 交还 CSS 的宽度阈值。默认 820：窄屏是另一套列模板。
  // 2026-10-29（第 3 条）：任务页给 1180 —— 行内 gridTemplateColumns 是
  // **inline style**，优先级高于任何 @media 规则；默认列宽加起来 ~1092px，
  // 窗口 900–1150px 时右端操作列会被 .table 的 overflow:hidden 直接裁掉，
  // 改 CSS 断点根本救不回来（inline 赢）。所以阈值必须在这里抬高，
  // 让"放不下"的区间也交还 CSS，由媒体查询给一套紧凑列宽。
  const compactPx = Number(opts.compactUnder || NARROW_PX)
  const defs = defaults.map((d) => ({
    k: d.k, w: Number(d.w), min: Number(d.min ?? 40), max: Number(d.max ?? 900),
  }))
  const storeKey = STORE_PREFIX + key

  function load() {
    const base = defs.map((d) => d.w)
    try {
      const raw = JSON.parse(localStorage.getItem(storeKey) || 'null')
      // 存的是 {v:1, w:[...]}：带版本号，将来列变了就整份丢弃，
      // 而不是把旧数组硬塞进新列数（那会拖出乱七八糟的宽度）。
      if (!raw || raw.v !== 1 || !Array.isArray(raw.w) || raw.w.length !== base.length) {
        return base
      }
      return base.map((b, i) => {
        const n = Number(raw.w[i])
        return Number.isFinite(n) ? clamp(n, defs[i].min, defs[i].max) : b
      })
    } catch {
      return base
    }
  }

  const widths = ref(load())

  function save() {
    try {
      localStorage.setItem(storeKey, JSON.stringify({ v: 1, w: widths.value }))
    } catch { /* 隐私模式下 localStorage 会抛，宽度记不住不影响用 */ }
  }

  // 窄屏：让 CSS 媒体查询接管（它有自己的列模板）
  const narrow = ref(false)
  let mq = null
  if (typeof window !== 'undefined' && window.matchMedia) {
    mq = window.matchMedia(`(max-width: ${compactPx}px)`)
    narrow.value = !!mq.matches
    const onMq = (e) => { narrow.value = !!e.matches }
    try { mq.addEventListener('change', onMq) } catch { mq.addListener?.(onMq) }
    onUnmounted(() => {
      try { mq.removeEventListener('change', onMq) } catch { mq.removeListener?.(onMq) }
    })
  }

  const gridStyle = computed(() => {
    if (narrow.value) return {}
    const last = widths.value.length - 1
    const cols = widths.value.map((w, i) => (i === last
      ? `minmax(${Math.round(w)}px, 1fr)`
      : `${Math.round(w)}px`))
    return { gridTemplateColumns: cols.join(' ') }
  })

  let dragging = null
  function onMove(e) {
    if (!dragging) return
    const w = clamp(dragging.startW + (e.clientX - dragging.startX),
      defs[dragging.i].min, defs[dragging.i].max)
    const next = widths.value.slice()
    next[dragging.i] = Math.round(w)
    widths.value = next
  }
  function onUp() {
    if (!dragging) return
    dragging = null
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
    window.removeEventListener('pointercancel', onUp)
    document.body.style.cursor = ''
    save()
  }
  onUnmounted(onUp)

  /** 表头右边缘的手柄按下：拖到哪宽到哪，松手落盘。 */
  function startResize(i, ev) {
    if (narrow.value) return
    ev.preventDefault()
    ev.stopPropagation()
    dragging = { i, startX: ev.clientX, startW: widths.value[i] }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
    document.body.style.cursor = 'col-resize'
  }

  /** 双击手柄：这一列复位（不需要「一键全复位」—— 那是另一个按钮的事）。 */
  function resetCol(i) {
    const next = widths.value.slice()
    next[i] = defs[i].w
    widths.value = next
    save()
  }

  function resetAll() {
    widths.value = defs.map((d) => d.w)
    save()
  }

  return { widths, gridStyle, startResize, resetCol, resetAll, narrow }
}
