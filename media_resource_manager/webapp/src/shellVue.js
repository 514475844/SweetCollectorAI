// V0.0.21 · 桌面窗口动作的 Vue 侧封装。
// nativeShell.js 保持纯函数（便于 node 直接断言），生命周期只放在这里。
import { onMounted, onUnmounted } from 'vue'
import {
  bindShellAction, onShellSelectAll, onShellRefresh,
  onShellSave, onShellFocusSearch,
} from './nativeShell'

/** 在组件 setup 里调用；卸载时自动解绑。 */
export function useShellActions(map) {
  let off = []
  onMounted(() => {
    off = Object.entries(map || {}).map(([name, fn]) => bindShellAction(name, fn))
  })
  onUnmounted(() => { off.forEach((f) => f()); off = [] })
}

/** 常见的单动作简写。 */
export const useShellSelectAll = (fn) => useShellActions({ 'mrm-shell-select-all': fn })
export const useShellRefresh = (fn) => useShellActions({ 'mrm-refresh-view': fn })
export const useShellSave = (fn) => useShellActions({ 'mrm-shell-save-current': fn })
export const useShellFocusSearch = (fn) => useShellActions({ 'mrm-shell-focus-search': fn })

export { onShellSelectAll, onShellRefresh, onShellSave, onShellFocusSearch }
