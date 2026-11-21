<script setup>
// 通用右键菜单（2026-10-27）：用户要求网页也能右键操作。
// 用法：父组件维护 { x, y, items }，items = [{label, danger?, disabled?, onClick}]
import { onMounted, onUnmounted, watch, ref, nextTick } from 'vue'
import Icon from './Icon.vue'
import { ICONS, ALIASES } from '../icons'

// 2026-10-28：菜单图标统一成 Lucide。`it.icon` 既可以是图标名（新写法），
// 也可以是一个字符（老写法，比如 '▶'）—— 认得出来就用图标，认不出来照旧显示字符，
// 这样所有调用点可以分批改，不会因为漏改一个菜单就变成空白。

const props = defineProps({
  menu: { type: Object, default: null },   // { x, y, items }
})
const emit = defineEmits(['close'])
const box = ref(null)
let openedAt = 0   // V0.0.67：菜单打开瞬间忽略关闭，防右键同一次事件把它秒关

// 菜单可能超出视口 → 贴边翻转
watch(() => props.menu, async (m) => {
  if (!m) return
  openedAt = Date.now()   // V0.0.67：每次打开都刷新，忽略同一次右键事件的关闭
  await nextTick()
  const el = box.value
  if (!el) return
  const r = el.getBoundingClientRect()
  const maxX = window.innerWidth - r.width - 8
  const maxY = window.innerHeight - r.height - 8
  if (m.x > maxX) m.x = Math.max(8, maxX)
  if (m.y > maxY) m.y = Math.max(8, maxY)
})

function iconOf(name) {
  if (!name) return ''
  return ICONS[name] ? name : (ALIASES[name] || '')
}

function onDocClick(e) {
  // 右键弹菜单时，mousedown/contextmenu 的事件序列可能有一次冒泡到 document，
  // 若立即 close 会把刚弹的菜单关掉（实测「选中了但菜单不弹」）。
  // 打开后 120ms 内的 mousedown 一律忽略；之后正常点外部关闭。
  if (e && e.button === 2) return              // 右键本身从不关闭菜单
  if (Date.now() - openedAt < 120) return
  emit('close')
}
function onEsc(e) { if (e.key === 'Escape') emit('close') }
onMounted(() => {
  openedAt = Date.now()
  document.addEventListener('mousedown', onDocClick)
  window.addEventListener('keydown', onEsc)
})
onUnmounted(() => {
  document.removeEventListener('mousedown', onDocClick)
  window.removeEventListener('keydown', onEsc)
})
</script>

<template>
  <teleport to="body">
    <div v-if="menu" ref="box" class="ctx" :style="{ left: menu.x + 'px', top: menu.y + 'px' }"
         @mousedown.stop>
      <button
        v-for="(it, i) in menu.items" :key="i" class="item"
        :class="{ danger: it.danger, disabled: it.disabled }"
        :disabled="it.disabled"
        @click="emit('close'); it.onClick && it.onClick()">
        <span class="ic">
          <Icon v-if="iconOf(it.icon)" :name="iconOf(it.icon)" :size="14" />
          <template v-else>{{ it.icon || '' }}</template>
        </span>{{ it.label }}
      </button>
    </div>
  </teleport>
</template>

<style scoped>
.ctx {
  position: fixed; z-index: 200; min-width: 168px; padding: 6px;
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; box-shadow: 0 12px 32px rgba(16,24,40,.18);
}
.item {
  display: flex; align-items: center; gap: 8px; width: 100%;
  border: 0; background: transparent; color: var(--text);
  padding: 8px 10px; border-radius: 7px; font-size: 13px; text-align: left;
}
.item:hover { background: #f2f1ff; }
.item.danger { color: var(--err); }
.item.danger:hover { background: #fff1f1; }
.item.disabled { opacity: .45; cursor: not-allowed; }
.item .ic {
  width: 16px; display: inline-flex; align-items: center; justify-content: center;
  opacity: .7; font-size: 13px;
}
html[data-theme="night"] .item:hover, html[data-theme="qbittorrent"] .item:hover { background: #3a3a3a; }
html[data-theme="qbittorrent"] .item.danger:hover { background: #4a2626; }
</style>
