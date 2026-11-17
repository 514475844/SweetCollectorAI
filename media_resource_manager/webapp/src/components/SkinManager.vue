<script setup>
// V0.0.27 · 自定义皮肤「文件管理」。
//
// 用户反馈原话：「设置外观与启动里面居然有两个外观选择，再加上左下角，
// 这就是 3 个外观选择。这对吗？」
// 起因：设置页这里有一排内建皮肤按钮（第一个选择器）、SECTIONS 里还有一个
// 「网页皮肤」下拉（第二个）、侧栏底部又有一个下拉（第三个）—— 同一个功能
// 三套入口，还各自用不同的保存时机。
//
// 现在只有一个**换皮肤的地方**：左下角那个下拉（内建 + 自定义皮肤都在里面）。
// 这个组件只负责「皮肤文件」本身：上传 CSS、看已有哪些、恢复原始。
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import Icon from './Icon.vue'
import { setWebTheme, readWebTheme } from '../uiPrefs'

const THEME_CN = { classic: '原始', violet_look: '紫罗兰', modern: '现代', qbittorrent: '深色' }
/** 界面上不显示裸键名（classic / custom:xxx），一律翻成中文。 */
const currentCn = computed(() => {
  const k = String(readWebTheme() || 'classic')
  if (k.startsWith('custom:')) return `自定义（${k.slice(7)}）`
  return THEME_CN[k] || k
})

const items = ref([])
const err = ref('')
const info = ref('')
const busy = ref(false)
const fileInput = ref(null)

async function load() {
  err.value = ''
  try {
    const r = await api.skins()
    if (!r.ok) throw new Error(r.error || '读取自定义皮肤失败，请稍后重试。')
    items.value = (r.items || []).filter((s) => s && s.has_web_css)
  } catch (e) {
    err.value = e.message || '读取自定义皮肤失败，请稍后重试。'
  }
}

/** 恢复原始皮肤：只动本机选择（与左下角下拉同一个存储）。 */
function restoreBuiltin() {
  setWebTheme('classic')
  window.dispatchEvent(new CustomEvent('mrm-theme', { detail: 'classic' }))
  info.value = '已恢复原始皮肤，自定义样式已移除。'
  setTimeout(() => (info.value = ''), 4000)
}

async function onFile(e) {
  const f = e.target.files && e.target.files[0]
  if (!f) return
  err.value = ''
  info.value = ''
  if (!String(f.name || '').toLowerCase().endsWith('.css')) {
    err.value = '请选择以 .css 结尾的文件。'
    e.target.value = ''
    return
  }
  busy.value = true
  try {
    const content = await f.text()
    const r = await api.skinUpload(f.name, content)
    if (!r.ok) throw new Error(r.error || '上传失败，请稍后重试。')
    info.value = r.note || '已保存自定义皮肤，可在左下角皮肤下拉里选中它。'
    // 左下角那个下拉要立刻能选到新皮肤（App.vue 监听这个事件重拉列表）
    window.dispatchEvent(new Event('mrm-skins-changed'))
    await load()
  } catch (ex) {
    err.value = ex.message || '文件读取失败，请手工确认文件内容后重试。'
  } finally {
    busy.value = false
    e.target.value = ''
  }
}

onMounted(load)
</script>

<template>
  <div class="skins">
    <div class="bar">
      <b><Icon name="wand-sparkles" :size="14" />自定义皮肤文件</b>
      <span class="muted small">当前使用：{{ currentCn }}</span>
    </div>
    <p v-if="err" class="err small">{{ err }}</p>
    <p v-if="info" class="ok small">{{ info }}</p>

    <div class="group">
      <span class="muted tiny">已上传的网页皮肤（{{ items.length }} 个）</span>
      <div v-if="items.length" class="chips">
        <span v-for="s in items" :key="s.key" class="chip">{{ s.label }}</span>
      </div>
      <p v-else class="muted small">尚未上传自定义皮肤。</p>
    </div>

    <div class="group">
      <span class="muted tiny">上传自定义 CSS 文件</span>
      <div class="inline">
        <!-- 原生文件框带英文「Choose File / No file chosen」，不留它露在界面上 -->
        <input ref="fileInput" type="file" accept=".css" style="display:none"
               @change="onFile" />
        <button class="btn sm" :disabled="busy" @click="fileInput && fileInput.click()">
          <Icon name="upload" :size="13" />{{ busy ? '正在上传…' : '选择 CSS 文件' }}</button>
        <button class="btn sm ghost" @click="restoreBuiltin">
          <Icon name="refresh-cw" :size="13" />换回原始皮肤</button>
      </div>
    </div>
    <p class="muted tiny">
      换皮肤不在这里：用<b>左下角</b>那个皮肤下拉（内建皮肤与上传的皮肤都在里面），
      全软件只有这一处。这里只管上传/删除样式文件；样式文件保存在服务端，选择记在本机。
    </p>
  </div>
</template>

<style scoped>
.skins { display: grid; gap: 8px; margin-top: 10px; padding: 10px 12px;
         border: 1px solid var(--line, rgba(0,0,0,.08)); border-radius: 10px; }
.bar { display: flex; align-items: center; justify-content: space-between; }
.bar b { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.group { display: grid; gap: 5px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px;
        border-radius: 999px; font-size: 12px;
        border: 1px solid var(--line, rgba(0,0,0,.14)); color: var(--text); }
.inline { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.tiny { font-size: 11px; }
.err { color: var(--err, #b91c1c); }
.ok { color: var(--brand, #6d4bd6); }
</style>
