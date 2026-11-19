// V0.0.67 · 命令式右键菜单
//
// 为什么不用 Vue 的 <Teleport>+v-if：实测 ctx.value 已赋值（title=CTX-SET），
// 但菜单 DOM 始终不出现 —— Vue 的 Teleport 渲染链在这个嵌套/内核环境里不可靠。
// 这里直接用原生 DOM 建菜单、append 到 body，样式全 inline（不依赖 scoped CSS），
// 关闭逻辑自包含。这是主流右键菜单库（floating-vue 等）内部同样采用的做法。

let current = null

function onDocMouseDown(e) {
  // 点在菜单外部才关闭；用捕获阶段确保一定收到
  if (current && !current.contains(e.target)) closeContextMenu()
}
function onKeyDown(e) {
  if (e.key === 'Escape') closeContextMenu()
}

export function closeContextMenu() {
  if (!current) return
  try { current.remove() } catch { /* ignore */ }
  current = null
  document.removeEventListener('mousedown', onDocMouseDown, true)
  document.removeEventListener('keydown', onKeyDown, true)
  window.removeEventListener('blur', closeContextMenu)
  window.removeEventListener('resize', closeContextMenu)
}

/**
 * 显示右键菜单。
 * @param {number} x 屏幕 clientX
 * @param {number} y 屏幕 clientY
 * @param {Array<{label,icon?,danger?,disabled?,onClick?}>} items
 */
export function showContextMenu(x, y, items) {
  closeContextMenu()
  const el = document.createElement('div')
  el.className = 'mrm-ctx-menu'
  Object.assign(el.style, {
    position: 'fixed',
    zIndex: '2147483647',
    minWidth: '170px',
    padding: '6px',
    background: '#ffffff',
    border: '1px solid #e3e6f0',
    borderRadius: '10px',
    boxShadow: '0 12px 32px rgba(16,24,40,.18)',
    fontFamily: 'inherit',
    fontSize: '13px',
    boxSizing: 'border-box',
  })
  for (const it of items || []) {
    const b = document.createElement('button')
    b.type = 'button'
    b.textContent = it.label || ''
    b.disabled = !!it.disabled
    Object.assign(b.style, {
      display: 'flex', alignItems: 'center', width: '100%',
      border: '0', background: 'transparent',
      color: it.danger ? '#d92d20' : '#1f2430',
      padding: '8px 10px', borderRadius: '7px',
      font: 'inherit', textAlign: 'left',
      cursor: it.disabled ? 'not-allowed' : 'pointer',
      opacity: it.disabled ? '0.45' : '1',
      whiteSpace: 'nowrap',
    })
    if (!it.disabled) {
      b.addEventListener('mouseenter', () => {
        b.style.background = it.danger ? '#fff1f1' : '#f2f1ff'
      })
      b.addEventListener('mouseleave', () => { b.style.background = 'transparent' })
      b.addEventListener('click', (ev) => {
        ev.stopPropagation()
        closeContextMenu()
        if (it.onClick) it.onClick()
      })
    }
    el.appendChild(b)
  }
  document.body.appendChild(el)
  // 先量尺寸再钳制到视口内（高 DPI 下 clientX/Y 可能超视口）
  const r = el.getBoundingClientRect()
  const px = Math.max(8, Math.min(Number(x) || 8, window.innerWidth - r.width - 8))
  const py = Math.max(8, Math.min(Number(y) || 8, window.innerHeight - r.height - 8))
  el.style.left = px + 'px'
  el.style.top = py + 'px'

  current = el
  // 延迟一帧挂全局关闭监听：避免「触发菜单的这次 mousedown」立刻把菜单关掉
  setTimeout(() => {
    if (current !== el) return   // 期间已被替换/关闭
    document.addEventListener('mousedown', onDocMouseDown, true)
    document.addEventListener('keydown', onKeyDown, true)
    window.addEventListener('blur', closeContextMenu)
    window.addEventListener('resize', closeContextMenu)
  }, 0)
}
