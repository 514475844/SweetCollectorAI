// 界面偏好（跨页面共享的运行时状态）。
//
// 为什么要有这个文件：图标是**组件级别**渲染的，而"用不用动态图标"是
// **全局**设置。如果每个页面各自读配置，改完开关得刷新整页才生效，
// 用户会以为没生效。这里用一个模块级 ref：Settings 改了立刻全体重渲染。
import { ref } from 'vue'
import { api } from './api'

/** 是否使用带动画特效的图标（真源头是后端配置 ui_animated_icons）。 */
export const animatedIcons = ref(true)

/**
 * 系统级「减少动效」偏好。
 * 用户如果在 Windows/浏览器里开了"减少动画"，我们即使用户配置是开的也不动 ——
 * 这是无障碍要求，不是我们的偏好。只读一次（运行中改系统设置需要刷新，可接受）。
 */
export const prefersReduced =
  typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
    : false

export function setAnimatedIcons(v) {
  animatedIcons.value = !!v
}

/** 真正决定"这个图标要不要动"的唯一判据，Icon.vue 只用它。 */
export function animOn() {
  return animatedIcons.value && !prefersReduced
}

// ── V0.0.17 · web_theme 持久化（网页皮肤，独立于桌面 ui_preset）──
// 内建主题同步写回后端配置；自定义皮肤的选择只保存在本机浏览器，
// 服务端保存的是样式文件，前端运行时再注入。
export const WEB_THEME_KEY = 'mrm_theme'
export const BUILTIN_THEMES = ['classic', 'violet_look', 'modern', 'qbittorrent']
export const webTheme = ref('classic')

export function readWebTheme() {
  try { return localStorage.getItem(WEB_THEME_KEY) || 'classic' } catch { return 'classic' }
}

export function setWebTheme(key) {
  const k = String(key || 'classic')
  webTheme.value = k
  try { localStorage.setItem(WEB_THEME_KEY, k) } catch { /* 隐私模式 */ }
  if (BUILTIN_THEMES.includes(k)) {
    try { api.saveSettings({ web_theme: k }) } catch { /* 仅本地生效 */ }
  }
  return k
}

// ── V0.0.17 · 网页端键位（与桌面 hotkeys.py 同一序列串口径）──
// 动作清单以 /api/web/hotkeys 返回的为准，这里只存生效键位与匹配逻辑，
// 避免同一份默认键位在前后端各写一遍。
export const WEB_HOTKEY_DEFAULTS = {
  'web.shortcuts': '?',
  'web.side': 'Ctrl+B',
  'web.tasks': 'Alt+1',
  'web.gallery': 'Alt+2',
  'web.top': 'Alt+T',
}
export const webHotkeys = ref({ ...WEB_HOTKEY_DEFAULTS })
/** 快捷键弹窗正在录键时置真，全局 keydown 应暂停分发动作。 */
export const recordingHotkey = ref(false)

export function setWebHotkeys(map) {
  const next = { ...WEB_HOTKEY_DEFAULTS }
  for (const [k, v] of Object.entries(map || {})) {
    if (k in next && typeof v === 'string' && v.trim()) next[k] = v.trim()
  }
  webHotkeys.value = next
}

/** 把键盘事件转成规范序列串；单键不看修饰键，与后端口径一致。 */
export function eventSeq(e) {
  if (!e) return ''
  if (e.key === '?') return '?'
  const mods = []
  if (e.ctrlKey) mods.push('Ctrl')
  if (e.altKey) mods.push('Alt')
  if (e.shiftKey) mods.push('Shift')
  if (e.metaKey) mods.push('Meta')
  const MAP = { ' ': 'Space', Escape: 'Esc', ArrowUp: '↑', ArrowDown: '↓',
                ArrowLeft: '←', ArrowRight: '→' }
  let key = MAP[e.key] || e.key
  if (key.length === 1 && /[a-z]/i.test(key)) key = key.toUpperCase()
  return [...mods, key].join('+')
}

/** 返回本次按键对应的网页动作 id；输入框内不拦截，避免影响正常录入。 */
export function matchWebAction(e) {
  if (!e || e.repeat) return null
  const t = e.target || {}
  const tag = String(t.tagName || '').toLowerCase()
  if (tag === 'input' || tag === 'textarea' || tag === 'select' || t.isContentEditable) return null
  const seq = eventSeq(e)
  for (const [id, s] of Object.entries(webHotkeys.value || {})) {
    if (s === seq) return id
  }
  return null
}


// ── V0.0.19 · 界面模式（新手 / 老鸟）────────────────────────────
// 真源头是后端配置 cfg.ui_mode，桌面端读同一个字段；浏览器本地只做
// 首屏缓存，避免刷新时闪一下老鸟布局。切换时同时写两端，不另立口径。
export const WEB_MODE_KEY = 'mrm_ui_mode'
export const uiMode = ref('pro')

/** 仅更新本地状态与缓存（供初始化使用，不回写后端）。 */
export function applyUiMode(m) {
  uiMode.value = m === 'newbie' ? 'newbie' : 'pro'
  try { localStorage.setItem(WEB_MODE_KEY, uiMode.value) } catch { /* 隐私模式 */ }
  return uiMode.value
}

/** 用户主动切换：本地立即生效，同时持久化到后端配置。 */
export function setUiMode(m) {
  const v = applyUiMode(m)
  try {
    const pr = api.saveUiMode(v)
    if (pr && typeof pr.catch === 'function') pr.catch(() => {})
  } catch { /* 仅本机生效 */ }
  return v
}

/** 首屏：先用本地缓存，再以服务端配置为准覆盖。 */
export async function loadUiMode() {
  try {
    const cached = localStorage.getItem(WEB_MODE_KEY)
    if (cached) applyUiMode(cached)
  } catch { /* 隐私模式 */ }
  try {
    const r = await api.uiMode()
    if (r && r.ok && r.mode) return applyUiMode(r.mode)
  } catch { /* 服务未就绪时保留本地值 */ }
  return uiMode.value
}
