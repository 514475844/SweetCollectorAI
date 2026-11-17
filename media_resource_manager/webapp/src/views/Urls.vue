<script setup>
// 常用网址：与桌面端共用 cfg.saved_urls（后端 /api/urls 直接读同一个字段）。
// 老界面（/classic）有这个页，新版一直没有 —— 这里补齐。
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import { useShellRefresh } from '../shellVue'
import Icon from '../components/Icon.vue'

const router = useRouter()
const items = ref([])
const kw = ref('')
const newUrl = ref('')
const loading = ref(false)
const err = ref('')
const info = ref('')

const shown = computed(() => {
  const k = kw.value.trim().toLowerCase()
  if (!k) return items.value
  return items.value.filter(
    (x) => (x.url || '').toLowerCase().includes(k) || (x.host || '').toLowerCase().includes(k))
})

function toast(msg, bad) {
  if (bad) { err.value = msg; info.value = '' } else { info.value = msg; err.value = '' }
  setTimeout(() => { err.value = ''; info.value = '' }, 3000)
}

async function load() {
  loading.value = true
  try {
    const d = await api.urls()
    items.value = d.items || []
  } catch (e) {
    err.value = e.message
  } finally {
    loading.value = false
  }
}

async function add() {
  const u = newUrl.value.trim()
  if (!u) { toast('请先填写网址', true); return }
  try {
    await api.urlSave(u)
    newUrl.value = ''
    await load()
    toast('已加入常用网址')
  } catch (e) {
    toast(e.message, true)
  }
}

async function del(it) {
  if (!window.confirm(`确认从常用网址中移除？\n${it.url}`)) return
  try {
    await api.urlDelete({ url: it.url })
    await load()
    toast('已移除')
  } catch (e) {
    toast(e.message, true)
  }
}

async function copy(u) {
  try { await navigator.clipboard.writeText(u); toast('已复制') }
  catch { toast('浏览器已禁止自动复制，请手工选择后复制', true) }
}

// 带着这个网址跳到任务页新建（Tasks 会读 ?entry=）
function makeTask(u) {
  router.push({ path: '/tasks', query: { entry: u } })
}

onMounted(load)
useShellRefresh(() => load())
</script>

<template>
  <div class="page">
    <div class="card bar">
      <div class="addbox">
        <input v-model="newUrl" type="text" placeholder="粘贴网址，例如 https://example.com/gallery/"
               @keyup.enter="add" />
        <button class="btn" :disabled="!newUrl.trim()" @click="add"><Icon name="plus" :size="13" />加入常用</button>
      </div>
      <div class="ops">
        <input v-model="kw" type="text" placeholder="筛选…" class="filter" />
        <span class="muted small">共 {{ items.length }} 条</span>
        <button class="btn sm" :disabled="loading" @click="load"><Icon :anim="loading ? 'spin' : ''" name="refresh-cw" :size="13" />刷新</button>
      </div>
    </div>

    <p v-if="err" class="err">{{ err }}</p>
    <p v-else-if="info" class="ok">{{ info }}</p>

    <div class="card list">
      <div v-if="!shown.length" class="empty">
        {{ items.length ? '未找到匹配的网址' : '暂无常用网址。在上方粘贴网址添加后，新建任务时可直接选用。' }}
      </div>
      <div v-for="it in shown" :key="it.url" class="ur">
        <div class="uinfo">
          <b>{{ it.host || it.url }}</b>
          <span class="muted mono small">{{ it.url }}</span>
        </div>
        <div class="uact">
          <button class="btn sm" @click="makeTask(it.url)">用它建任务</button>
          <button class="btn sm ghost" @click="copy(it.url)"><Icon name="copy" :size="13" />复制</button>
          <a class="btn sm ghost" :href="it.url" target="_blank" rel="noopener">打开</a>
          <button class="btn sm danger" @click="del(it)"><Icon name="trash" :size="13" />移除</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { display: grid; gap: 14px; }
.bar { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px; gap: 10px; flex-wrap: wrap; }
.addbox { display: flex; gap: 8px; flex: 1; min-width: 320px; }
.addbox input { flex: 1; }
.ops { display: flex; align-items: center; gap: 8px; }
.filter { width: 140px; }
.list { padding: 6px 0; }
.empty { padding: 26px 16px; text-align: center; color: var(--muted, #888); }
.ur { display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 10px 14px; border-top: 1px solid var(--line, rgba(0,0,0,.08)); }
.ur:first-of-type { border-top: 0; }
.uinfo { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.uinfo span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.uact { display: flex; gap: 6px; flex-shrink: 0; flex-wrap: wrap; }
.btn.danger { color: #b91c1c; border-color: #b91c1c; }
.err { color: #b91c1c; margin: 0; }
.ok { color: #15803d; margin: 0; }
@media (max-width: 720px) {
  .ur { flex-direction: column; align-items: flex-start; }
}
</style>
