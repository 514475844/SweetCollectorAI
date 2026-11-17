<script setup>
// V0.0.54：适配器市场。
// 订阅源 = 一个 index.json 网址（官方市场是 GitHub Pages 上的规则仓库）。
// 本页只做三件事：管订阅源、浏览各源的规则目录、一键安装。
// 安装走后端同一道规则校验（与市场无关的手动新建同一条链），
// 已安装的行会标出来；升级 = 勾选覆盖重新安装。
import { ref, onMounted } from 'vue'
import { api } from '../api'
import Icon from '../components/Icon.vue'

const sources = ref([])
const active = ref('')           // 当前浏览的源地址
const newUrl = ref('')
const catalog = ref([])
const installed = ref(new Set()) // 本地已有规则名
const loading = ref(false)
const busy = ref('')             // 正在安装的规则 url
const err = ref('')
const info = ref('')

function say(msg, isErr = false) {
  if (isErr) { err.value = msg; info.value = ''; }
  else { info.value = msg; err.value = ''; }
  setTimeout(() => { info.value = ''; err.value = '' }, 4000)
}

async function loadInstalled() {
  try {
    const d = await api.view('adapters', { limit: 500 })
    installed.value = new Set((d.items || []).map((a) => a.name))
  } catch (e) { /* 读不到已安装状态不挡浏览 */ }
}

async function loadSources() {
  try {
    const r = await api.marketSources()
    if (!r.ok) { say(r.error || '读取订阅源失败', true); return }
    sources.value = r.sources || []
    if (!active.value || !sources.value.includes(active.value)) {
      active.value = sources.value[0] || ''
    }
    if (active.value) await loadCatalog(active.value)
  } catch (e) { say(e.message || '读取订阅源失败', true) }
}

async function loadCatalog(url) {
  if (!url) return
  loading.value = true
  try {
    const r = await api.marketIndex(url)
    if (!r.ok) { catalog.value = []; say(r.error || '订阅源不可用', true); return }
    catalog.value = r.rules || []
    err.value = ''
  } catch (e) {
    catalog.value = []
    say(e.message || '订阅源不可用', true)
  } finally { loading.value = false }
}

async function addSource() {
  const u = newUrl.value.trim()
  if (!u) { say('请先填写订阅源地址', true); return }
  try {
    const r = await api.marketManage('add', u)
    if (!r.ok) { say(r.error || '添加失败', true); return }
    newUrl.value = ''
    sources.value = r.sources || sources.value
    active.value = u
    await loadCatalog(u)
    say(`已添加订阅源${r.name ? '：' + r.name : ''}`)
  } catch (e) { say(e.message || '添加失败', true) }
}

async function removeSource(u) {
  if (!confirm('移除这个订阅源？已安装的规则不受影响。')) return
  try {
    const r = await api.marketManage('remove', u)
    if (!r.ok) { say(r.error || '移除失败', true); return }
    sources.value = r.sources || []
    if (active.value === u) {
      active.value = sources.value[0] || ''
      catalog.value = []
      if (active.value) await loadCatalog(active.value)
    }
  } catch (e) { say(e.message || '移除失败', true) }
}

async function install(rule, overwrite = false) {
  busy.value = rule.url
  try {
    const r = await api.marketInstall(rule.url, rule.name, overwrite)
    if (!r.ok) {
      if (String(r.error || '').includes('已经有一条')) {
        // 重名 → 让用户确认是否覆盖升级
        if (confirm(`本地已有同名规则「${rule.name}」，用市场版本覆盖？`)) {
          const r2 = await api.marketInstall(rule.url, rule.name, true)
          if (r2.ok) { say(`已更新规则 ${rule.name}`); await loadInstalled() }
          else say(r2.error || '覆盖失败', true)
        }
        return
      }
      say(r.error || '安装失败', true)
      return
    }
    say(`已安装规则 ${r.name || rule.name}`)
    await loadInstalled()
  } catch (e) { say(e.message || '安装失败', true) }
  finally { busy.value = '' }
}

onMounted(() => { loadInstalled(); loadSources() })
</script>

<template>
  <div class="page">
    <div class="head">
      <h2>适配器市场</h2>
      <span class="muted small">订阅源是一批别人写好的适配器规则；安装前先核对，安装后在「适配器」页可见可编辑。</span>
    </div>

    <!-- 订阅源 -->
    <div class="card src">
      <div class="inline src-in">
        <input v-model="newUrl" placeholder="粘贴订阅源地址（index.json 的网址）"
               @keyup.enter="addSource" />
        <button class="btn sm primary" :disabled="!newUrl.trim()" @click="addSource">
          <Icon name="plus" :size="13" />添加订阅源</button>
      </div>
      <div class="src-list">
        <button v-for="s in sources" :key="s" class="chip"
                :class="{ on: s === active }" @click="active = s; loadCatalog(s)">
          <Icon name="rss" :size="12" />{{ s.replace(/^https?:\/\//, '').slice(0, 46) }}
          <i class="x" @click.stop="removeSource(s)">×</i>
        </button>
        <span v-if="!sources.length" class="muted small">暂无订阅源，先添加一个。</span>
      </div>
    </div>

    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="info" class="ok">{{ info }}</p>

    <!-- 目录 -->
    <div class="card table">
      <div class="th row"><span>规则</span><span>站点 / 域名</span><span>来源</span><span>状态</span><span>操作</span></div>
      <div v-for="r in catalog" :key="r.url" class="row tr">
        <span class="nm">
          <b>{{ r.title || r.name }}</b>
          <em v-if="r.note" class="note">{{ r.note }}</em>
        </span>
        <span class="mono dim">
          {{ r.site || '' }}
          <em v-if="r.domains && r.domains.length" class="dm">{{ r.domains.join('、') }}</em>
        </span>
        <span class="small muted">{{ r.author || '—' }}{{ r.added ? ' · ' + r.added : '' }}</span>
        <span>
          <i v-if="installed.has(r.name)" class="pill ok">已安装</i>
          <i v-else class="pill soft">未安装</i>
        </span>
        <span class="acts">
          <button class="btn sm" :class="{ primary: !installed.has(r.name) }"
                  :disabled="busy === r.url" @click="install(r)">
            <Icon :anim="busy === r.url ? 'spin' : ''"
                  :name="busy === r.url ? 'loader-circle' : 'download'" :size="13" />
            {{ busy === r.url ? '安装中…' : (installed.has(r.name) ? '覆盖重装' : '安装') }}</button>
        </span>
      </div>
      <p v-if="loading" class="empty muted"><Icon anim="spin" name="loader-circle" :size="14" /> 正在读取订阅源…</p>
      <p v-else-if="!catalog.length" class="empty muted">这个订阅源还没有可显示的规则</p>
    </div>

    <p class="muted small">
      想提交自己写的规则：市场仓库的 README 里有完整字段说明与提交流程；
      也可以只在本地用「适配器 → 导入本地规则文件」，不必公开分享。
    </p>
  </div>
</template>

<style scoped>
.page { display: grid; gap: 14px; align-items: start; }
.head { display: flex; flex-direction: column; gap: 2px; }
.head h2 { margin: 0; font-size: 17px; }
.src { padding: 12px 14px; display: flex; flex-direction: column; gap: 10px; }
.src-in { display: flex; gap: 8px; }
.src-in input { flex: 1; min-width: 240px; }
.src-list { display: flex; gap: 8px; flex-wrap: wrap; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px;
  border-radius: 999px; border: 1px solid var(--line); background: #fbfaff;
  font-size: 12px; cursor: pointer; color: var(--text); }
.chip.on { border-color: var(--brand); color: var(--brand); background: #f1edff; }
.chip .x { font-style: normal; opacity: .55; padding: 0 2px; }
.chip .x:hover { opacity: 1; color: var(--err); }
.table { overflow-x: auto; overflow-y: hidden; }
.row { display: grid; grid-template-columns: minmax(140px, 200px) minmax(0, 1fr)
  120px 80px 118px; gap: 10px; align-items: center; padding: 10px 14px; }
.th { background: #fafbff; border-bottom: 1px solid var(--line); font-size: 12px; color: var(--muted); }
.tr { border-bottom: 1px solid #f1f2f8; }
.nm { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.note { font-style: normal; font-size: 11px; color: var(--muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dm { display: block; font-style: normal; font-size: 11px; color: var(--muted);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dim { color: var(--muted); overflow: hidden; }
.acts { display: flex; gap: 8px; justify-content: flex-end; }
.pill { font-style: normal; font-size: 11px; padding: 2px 8px; border-radius: 999px;
  border: 1px solid var(--line); }
.pill.ok { color: var(--ok); border-color: #bfe3cd; background: #eefaf1; }
.pill.soft { color: var(--muted); }
.err { color: var(--err); margin: 0; }
.ok { color: var(--ok); margin: 0; }
.small { font-size: 12px; }
.empty { padding: 24px; text-align: center; margin: 0; }
.btn.sm { padding: 4px 10px; font-size: 12px; border-radius: 8px; }
</style>
