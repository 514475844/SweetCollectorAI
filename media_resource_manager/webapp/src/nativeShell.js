// V0.0.21 · 桌面窗口模式的键位与指针行为。
//
// 这一层只做一件事：把「浏览器默认行为」换成「软件行为」。内置渲染窗口
// 里，网页只是界面，不是网页文档 —— Ctrl+A 应该全选当前列表，Ctrl+F 应该
// 定位到软件自己的搜索框，F5 应该刷新数据而不是重载整页；打印、查看源代码、
// 开发者工具、新标签、后退这类浏览器功能不应出现。
//
// 分层：内核（Qt 事件过滤器）拦住浏览器进程级的键并转发到这里；网页内
// keydown 也走这里。两条路径共用同一张决策表，避免两端各写一套迟早分叉。
// 普通浏览器访问（手机 / 局域网 / 调试）不注入 __MRM_NATIVE_SHELL__，
// 因此完全不受影响，浏览器该有的编辑与查找照常可用。
//
// 决策函数是纯函数，node 可以直接导入做断言（tests/test_v021_native_shell.py）。

const ARROWS = { ArrowUp: '↑', ArrowDown: '↓', ArrowLeft: '←', ArrowRight: '→' }

/** 把按键信息规范化成与 uiPrefs.eventSeq 同一口径的序列串。 */
export function seqOf(e) {
  const o = e || {}
  if (o.key === '?') return '?'
  const mods = []
  if (o.ctrlKey || o.ctrl) mods.push('Ctrl')
  if (o.altKey || o.alt) mods.push('Alt')
  if (o.shiftKey || o.shift) mods.push('Shift')
  if (o.metaKey || o.meta) mods.push('Meta')
  let key = ARROWS[o.key] || o.key || ''
  if (o.key === ' ') key = 'Space'
  if (o.key === 'Escape') key = 'Esc'
  if (key.length === 1 && /[a-z]/i.test(key)) key = key.toUpperCase()
  return [...mods, key].join('+')
}

/** 目标是否处于可编辑控件内（此处必须保留原生编辑键）。 */
export function isEditable(e) {
  const t = e && e.target ? e.target : null
  if (!t) return false
  const tag = String(t.tagName || '').toLowerCase()
  return tag === 'input' || tag === 'textarea' || tag === 'select' || !!t.isContentEditable
}

// ── 语义映射：浏览器键 → 软件动作 ───────────────────────────────────
// 只列「软件里确实有对应功能」的键；其余浏览器键统一归入 BLOCK。
export const SHELL_ACTIONS = Object.freeze({
  'Ctrl+A': 'select-all',       // 全选当前列表（输入框内保留原生全选文字）
  'Ctrl+F': 'focus-search',     // 定位全局搜索框，不弹浏览器查找栏
  'Ctrl+S': 'save-current',     // 保存当前表单 / 设置，不弹另存为
  'F5': 'refresh-view',         // 刷新数据，不重载页面
  'Ctrl+R': 'refresh-view',
  'Ctrl+Shift+R': 'refresh-view',
  'F3': 'focus-search',
})

// ── 浏览器功能键：一律停用 ─────────────────────────────────────────
// 分类列出，便于核对；键名用 seqOf 的口径。
const BROWSER_GROUPS = Object.freeze({
  '打印与另存': ['Ctrl+P', 'Ctrl+Shift+P', 'Ctrl+Shift+S'],
  '查找与地址栏': ['Ctrl+G', 'Ctrl+K', 'Ctrl+L', 'F6', 'Ctrl+E'],
  '历史与下载': ['Ctrl+H', 'Ctrl+J', 'Ctrl+D', 'Alt+Home', 'Alt+←', 'Alt+→'],
  '查看源代码与开发者工具': ['Ctrl+U', 'F12', 'Ctrl+Shift+I', 'Ctrl+Shift+J',
                            'Ctrl+Shift+C', 'Ctrl+Shift+K', 'Ctrl+Shift+Del'],
  '标签与窗口': ['Ctrl+T', 'Ctrl+N', 'Ctrl+W', 'Ctrl+Q', 'Ctrl+Shift+T',
                 'Ctrl+Tab', 'Ctrl+Shift+Tab', 'Ctrl+1', 'Ctrl+2', 'Ctrl+3',
                 'Ctrl+4', 'Ctrl+5', 'Ctrl+6', 'Ctrl+7', 'Ctrl+8', 'Ctrl+9'],
  '页面形态': ['F7', 'F11', 'Ctrl+O'],
  '缩放': ['Ctrl+=', 'Ctrl++', 'Ctrl+-', 'Ctrl+_', 'Ctrl+0'],
  '清除与隐私': ['Ctrl+Shift+Delete'],
})
export const BLOCK_SEQS = Object.freeze(
  new Set(Object.values(BROWSER_GROUPS).flat()))

// 输入框内的编辑键（保留原生行为）；其余带修饰键的组合在输入区也拦。
const EDIT_SEQS = new Set([
  'Ctrl+A', 'Ctrl+C', 'Ctrl+V', 'Ctrl+X', 'Ctrl+Z', 'Ctrl+Y',
  'Ctrl+Shift+Z', 'Ctrl+Shift+V', 'Ctrl+Home', 'Ctrl+End',
  'Ctrl+←', 'Ctrl+→', 'Ctrl+Backspace', 'Ctrl+Delete',
])

// 软件自有固定键位：优先放行，交给全局 onKey / 组件处理。
// （Ctrl+B 侧栏、Ctrl+Z 撤销审核、F1 说明、Esc 灯箱、? 说明、Alt+1/2/T 导航）
const SOFTWARE_SEQS = new Set([
  'Ctrl+B', 'Ctrl+Z', 'F1', 'Esc', '?', 'Alt+1', 'Alt+2', 'Alt+T',
  '↑', '↓', '←', '→', 'Space', 'Enter', 'Tab', 'PageUp', 'PageDown',
  'Home', 'End', 'Backspace', 'Delete',
])

/**
 * 决策单个按键。
 * @returns {{kind: 'allow'|'block'|'action', action?: string}}
 */
export function decideShellKey(seq, ctx) {
  const c = ctx || {}
  // 录键态优先级最高：快捷键说明里要能录到任意组合键。
  if (c.recording) return { kind: 'allow' }
  // 用户自定义的网页动作键位优先，避免被浏览器键表误拦。
  if (c.matchedAction) return { kind: 'allow' }

  const editable = !!c.editable
  const action = SHELL_ACTIONS[seq]

  // Ctrl+A：可编辑控件内保留原生全选文字；列表页变成全选当前列表。
  if (seq === 'Ctrl+A' && editable) return { kind: 'allow' }
  if (action) return { kind: 'action', action }

  if (SOFTWARE_SEQS.has(seq)) return { kind: 'allow' }
  if (BLOCK_SEQS.has(seq)) return { kind: 'block' }

  // 复制选中文本在桌面软件里是合理的；粘贴 / 剪切只在输入框内有效。
  if (seq === 'Ctrl+C' && !editable && c.hasSelection) return { kind: 'allow' }
  if (editable && EDIT_SEQS.has(seq)) return { kind: 'allow' }

  // 其余带 Ctrl / Meta / Alt 的组合一律拦，防止浏览器行为漏出来。
  const hasMod = /^(Ctrl|Meta|Alt)\+/.test(seq) || seq.startsWith('Ctrl+Shift+')
  if (hasMod) return { kind: 'block' }
  return { kind: 'allow' }
}

export function isNativeShell() {
  return typeof window !== 'undefined' && window.__MRM_NATIVE_SHELL__ === true
}

/**
 * 供各视图订阅软件动作；返回卸载函数，在 onUnmounted 里调用。
 * 普通浏览器访问时返回空卸载函数，页面行为与改前一致。
 */
export function bindShellAction(name, fn) {
  if (!isNativeShell() || typeof window === 'undefined') return () => {}
  const h = (e) => { try { fn(e) } catch (_e) { /* 忽略：页面卸载竞态 */ } }
  window.addEventListener(name, h)
  return () => window.removeEventListener(name, h)
}
export const onShellSelectAll = (fn) => bindShellAction('mrm-shell-select-all', fn)
export const onShellRefresh = (fn) => bindShellAction('mrm-refresh-view', fn)
export const onShellSave = (fn) => bindShellAction('mrm-shell-save-current', fn)
export const onShellFocusSearch = (fn) => bindShellAction('mrm-shell-focus-search', fn)

/** 是否处于「应该允许拖选复制」的区域。 */
function selectableTarget(t) {
  let n = t
  for (let i = 0; n && i < 6; i++, n = n.parentNode) {
    if (n.nodeType !== 1) continue
    if (n.isContentEditable) return true
    const tag = String(n.tagName || '').toLowerCase()
    if (tag === 'input' || tag === 'textarea' || tag === 'pre' || tag === 'code') return true
    const cl = String(n.className || '')
    if (/\b(mono|log-line|log-body|selectable|gs-label|keyin)\b/.test(cl)) return true
  }
  return false
}

function dispatch(name, detail) {
  try {
    // cancelable：当前页若有自己的搜索框 / 表单，先处理并可取消全局兜底。
    window.dispatchEvent(new CustomEvent(name, { detail: detail || {}, cancelable: true }))
  } catch (_e) { /* 忽略：界面尚未挂载时不产生动作 */ }
}

/** 执行语义动作（统一广播，由各视图自行订阅）。 */
export function runShellAction(action, seq) {
  const names = {
    'select-all': 'mrm-shell-select-all',
    'focus-search': 'mrm-shell-focus-search',
    'save-current': 'mrm-shell-save-current',
    'refresh-view': 'mrm-shell-refresh-view',
  }
  const name = names[action]
  if (name) dispatch(name, { seq })
}

/** 当前窗口状态（内核转发时没有原生事件对象，按同一口径补齐）。 */
function shellState(seed) {
  const d = seed || {}
  let editable = !!d.editable
  try {
    const a = document.activeElement
    if (a) {
      const tag = String(a.tagName || '').toLowerCase()
      editable = editable || tag === 'input' || tag === 'textarea' ||
        tag === 'select' || !!a.isContentEditable
    }
  } catch (_e) { /* 忽略 */ }
  let hasSelection = false
  try { hasSelection = !!String(window.getSelection() || '') } catch (_e) { /* 忽略 */ }
  let matchedAction = false
  try {
    matchedAction = !!(window.__mrmMatchWebAction &&
      window.__mrmMatchWebAction({ ...d, target: d.target || null }))
  } catch (_e) { /* 忽略 */ }
  return {
    editable, hasSelection, matchedAction,
    recording: !!window.__mrmRecordingHotkey,
  }
}

/** 内核转发过来的键（浏览器进程级，网页收不到原生 keydown）走同一张表。 */
function onKernelKey(e) {
  const d = (e && e.detail) || {}
  const seq = seqOf(d)
  if (!seq) return
  const r = decideShellKey(seq, shellState(d))
  if (r.kind === 'block') return
  if (r.kind === 'action') { runShellAction(r.action, seq); return }
  // 录键态或用户自定义键位：内核已拦下原生事件，这里补发一个可区分的
  // keydown，让快捷键说明的录键与既有动作分发照常拿到按键。
  try {
    const ev = new KeyboardEvent('keydown', {
      key: d.key, ctrlKey: !!d.ctrlKey, altKey: !!d.altKey,
      shiftKey: !!d.shiftKey, metaKey: !!d.metaKey,
      bubbles: true, cancelable: true,
    })
    Object.defineProperty(ev, '_mrmKernelReplay', { value: true })
    window.dispatchEvent(ev)
  } catch (_e) { /* 忽略 */ }
}

function onKeyDown(e) {
  if (!isNativeShell()) return
  if (e._mrmKernelReplay) return   // 内核转发键的重放事件，避免二次决策
  const seq = seqOf(e)
  const editable = isEditable(e)
  let hasSelection = false
  try { hasSelection = !!String(window.getSelection() || '') } catch (_e) { /* 忽略 */ }
  let matchedAction = false
  try { matchedAction = !!(window.__mrmMatchWebAction && window.__mrmMatchWebAction(e)) } catch (_e) { /* 忽略 */ }
  let recording = false
  try { recording = !!window.__mrmRecordingHotkey } catch (_e) { /* 忽略 */ }
  const r = decideShellKey(seq, { editable, hasSelection, matchedAction, recording })
  // 内核已清掉 SelectAll 的默认快捷键，输入框里的 Ctrl+A 由前端补齐，
  // 保证「全选文字」这个正常编辑动作不会因桌面化而失效。
  if (r.kind === 'allow' && seq === 'Ctrl+A' && editable) {
    try { e.target.select && e.target.select() } catch (_e) { /* 忽略 */ }
    e.preventDefault()
    return
  }
  if (r.kind === 'block') { e.preventDefault(); e.stopImmediatePropagation(); return }
  if (r.kind === 'action') {
    // 只走广播一条路径：各视图自行订阅，不再让页面里散落的 keydown
    // 重复处理同一个键（否则「全选」会被切换两次，等于没选中）。
    e.preventDefault()
    e.stopImmediatePropagation()
    runShellAction(r.action, seq)
  }
}

// ── 指针 / 拖放 / 文本选择：桌面软件里不该出现的浏览器行为 ──────────
function onDrag(e) {
  if (!isNativeShell()) return
  // 拖文件进入窗口会导航离开软件；图片 / 链接拖出属于浏览器行为。
  if (!selectableTarget(e.target)) e.preventDefault()
}
function onSelectStart(e) {
  if (!isNativeShell()) return
  if (!selectableTarget(e.target)) e.preventDefault()
}
function onAuxClick(e) {
  if (!isNativeShell()) return
  if (e.button === 1) e.preventDefault()   // 中键自动滚动 / 新标签打开链接
}
function onWheel(e) {
  if (!isNativeShell()) return
  if (e.ctrlKey || e.metaKey) e.preventDefault()
}
function onGesture(e) {
  if (!isNativeShell()) return
  e.preventDefault()
}

const HOOKS = [
  ['keydown', onKeyDown, true],
  ['mrm-shell-key', onKernelKey, false],
  // contextmenu 不在此拦截：内核层会构造只有软件动作的精简菜单，
  // 前端若 preventDefault 反而会把复制 / 粘贴菜单一起吞掉。
  ['dragstart', onDrag, true],
  ['dragover', onDrag, true],
  ['drop', onDrag, true],
  ['selectstart', onSelectStart, true],
  ['auxclick', onAuxClick, true],
  ['wheel', onWheel, { capture: true, passive: false }],
  ['gesturestart', onGesture, true],
]

/** 在 App.vue 的 onMounted 调用；返回卸载函数。 */
export function installNativeShell() {
  if (!isNativeShell()) return () => {}
  try { document.documentElement.classList.add('mrm-native') } catch (_e) { /* 忽略 */ }
  for (const [name, fn, opt] of HOOKS) window.addEventListener(name, fn, opt)
  return () => {
    for (const [name, fn, opt] of HOOKS) window.removeEventListener(name, fn, opt)
    try { document.documentElement.classList.remove('mrm-native') } catch (_e) { /* 忽略 */ }
  }
}
