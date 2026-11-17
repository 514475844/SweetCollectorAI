<!--
  代理管理（2026-10-28）

  桌面端一直有「维护 → 代理管理…」和「代理健康检查」，但网页端只有任务编辑里的
  一个下拉：能看到已有代理，**没法新增、改、停用、删除、测速**。只用网页的人
  一旦代理换了密码，就再也改不动了。

  三个必须守住的细节：
    1. 列表里的地址是 `user:***@host:port`（后端 hide_secret），**不能直接回填到
       编辑框** —— 那样用户一保存，密码就真的变成三个星号了。
    2. 密码留空 = 不改（后端 urlsplit 拿不到 password 时保留原值）。
    3. 测速是**同步阻塞**请求，所以按钮上必须有明确的"测速中"，否则用户会连点。
-->
<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { api } from '../api'
import { useShellRefresh } from '../shellVue'
import Icon from '../components/Icon.vue'

const items = ref([])
const err = ref('')
const info = ref('')
const busy = ref('')            // 'check:<id>' / 'check' / 'save' / 'del:<id>'
const showForm = ref(false)

// 2026-10-29 用户第 7 条：代理类型必须是**单选框**，不能让人手打
// `socks5://`。所以表单拆成「类型 / 主机 / 端口 / 账号 / 密码」五个字段，
// 保存时再拼成后端要的 url（后端解析 scheme/host/port 的逻辑一行都不用改）。
const PROXY_TYPES = [
  { v: 'http', t: 'HTTP', port: 8080 },
  { v: 'https', t: 'HTTPS', port: 8443 },
  { v: 'socks5', t: 'SOCKS5', port: 1080 },
]
const form = reactive({
  id: 0, name: '', scheme: 'http', host: '', port: 8080,
  username: '', password: '', priority: 0, enabled: true,
})

/** 换类型时顺手把端口换成该协议的常用口（用户改过端口就不动）。 */
function defaultPort(scheme) {
  const t = PROXY_TYPES.find((x) => x.v === scheme)
  return t ? t.port : 8080
}

const total = computed(() => items.value.length)
const enabledCount = computed(() => items.value.filter((p) => p.enabled).length)
// 与后端 ProxyEntry.healthy() 同一口径：启用 + 失败次数 < 3
const healthyCount = computed(
  () => items.value.filter((p) => p.enabled && (p.failures || 0) < 3).length)

async function load() {
  err.value = ''
  try {
    const d = await api.proxies()
    items.value = d.items || []
  } catch (e) {
    err.value = e.message || '读取代理失败'
  }
}

function openNew() {
  Object.assign(form, { id: 0, name: '', scheme: 'http', host: '', port: 8080,
                        username: '', password: '', priority: 0, enabled: true })
  showForm.value = true
  info.value = ''
}

function openEdit(p) {
  // ⚠ 回填**不含凭据**：p.url 是 `socks5://user:***@host:1080` 这种打码形式，
  // 直接塞进输入框再保存，密码就真成 *** 了。
  Object.assign(form, {
    id: p.id,
    name: p.name,
    scheme: p.scheme || 'http',
    host: p.host || '',
    port: p.port || defaultPort(p.scheme),
    username: p.username || '',
    priority: p.priority || 0,
    enabled: !!p.enabled,
    password: '',
  })
  showForm.value = true
  info.value = p.username
    ? `该代理使用账号 ${p.username}：密码留空表示不修改`
    : ''
}

async function save() {
  err.value = ''
  busy.value = 'save'
  try {
    const host = String(form.host || '').trim().replace(/^[a-z0-9+.-]+:\/\//i, '')
    if (!host) { err.value = '请填写代理主机（IP 或域名）'; busy.value = ''; return }
    if (!form.port) { err.value = '请填写端口'; busy.value = ''; return }
    // 拼 URL：账号留空 = 不带 userinfo（后端 url. 为 None ⇒ **不覆盖**已有账号密码）
    // 填了账号但没填密码 = 只带账号（编辑态下密码保留原值）
    let userinfo = ''
    const user = String(form.username || '').trim()
    if (user) userinfo = form.password ? `${user}:${form.password}@` : `${user}@`
    const body = {
      id: form.id || null,
      name: form.name.trim(),
      url: `${form.scheme}://${userinfo}${host}:${Number(form.port)}`,
      priority: Number(form.priority) || 0,
      enabled: !!form.enabled,
    }
    const r = await api.proxySave(body)
    if (r && r.ok === false) { err.value = r.error || '保存失败'; return }
    info.value = form.id ? '已保存' : '已新增'
    setTimeout(() => (info.value = ''), 3000)
    showForm.value = false
    await load()
  } catch (e) {
    err.value = e.message || '保存失败'
  } finally {
    busy.value = ''
  }
}

async function toggle(p) {
  err.value = ''
  try {
    const r = await api.proxySave({ id: p.id, enabled: !p.enabled })
    if (r && r.ok === false) err.value = r.error || '切换失败'
    await load()
  } catch (e) {
    err.value = e.message || '切换失败'
  }
}

async function del(p) {
  if (!confirm(`确认删除代理「${p.name}」？\n\n使用该代理的任务将退回「自动选择健康节点」。`)) return
  err.value = ''
  busy.value = 'del:' + p.id
  try {
    const r = await api.proxyDelete(p.id)
    if (r && r.ok === false) err.value = r.error || '删除失败'
    await load()
  } catch (e) {
    err.value = e.message || '删除失败'
  } finally {
    busy.value = ''
  }
}

async function checkOne(p) {
  err.value = ''
  busy.value = 'check:' + p.id
  try {
    const r = await api.proxyCheck(p.id)
    if (r && r.ok === false) { err.value = r.error || '测速失败'; return }
    const it = (r.items || [])[0] || {}
    info.value = it.ok
      ? `「${p.name}」可用，延迟 ${it.latency_ms} ms`
      : `「${p.name}」不可用：${it.error || '未知原因'}`
    setTimeout(() => (info.value = ''), 5000)
    await load()
  } catch (e) {
    err.value = e.message || '测速失败'
  } finally {
    busy.value = ''
  }
}

async function checkAll() {
  if (!items.value.length) return
  err.value = ''
  busy.value = 'check'
  info.value = '正在逐个检测（不可用节点每个需等待 5 秒）…'
  try {
    const r = await api.proxyCheck(null)
    if (r && r.ok === false) { err.value = r.error || '测速失败'; return }
    const arr = r.items || []
    const okN = arr.filter((x) => x.ok).length
    info.value = `检测完成：共 ${arr.length} 个，可用 ${okN} 个，不可用 ${arr.length - okN} 个` +
      (r.note ? `（${r.note}）` : '')
    setTimeout(() => (info.value = ''), 8000)
    await load()
  } catch (e) {
    err.value = e.message || '测速失败'
  } finally {
    busy.value = ''
  }
}

function latClass(p) {
  if (!p.latency_ms || p.latency_ms < 0) return 'dim'
  if (p.latency_ms < 300) return 'fast'
  if (p.latency_ms < 1000) return 'mid'
  return 'slow'
}

onMounted(load)
useShellRefresh(() => load())
</script>

<template>
  <div class="page">
    <div class="head">
      <div>
        <h2><Icon name="globe" :size="18" /> 代理管理</h2>
        <p class="sub">
          共 {{ total }} 个（启用 {{ enabledCount }}，健康 {{ healthyCount }}）。
          「健康」= 启用中且连续失败不到 3 次。任务里选「自动挑健康节点」时按这个顺序挑。
        </p>
      </div>
      <div class="acts">
        <button class="btn" @click="openNew"><Icon name="plus" :size="14" />新增代理</button>
        <button class="btn" :disabled="!total || busy === 'check'" @click="checkAll">
          <Icon :anim="busy === 'check' ? 'spin' : ''" name="gauge" :size="14" />
          {{ busy === 'check' ? '正在测速…' : '全部测速' }}
        </button>
        <button class="btn ghost" @click="load"><Icon name="refresh-cw" :size="14" />刷新</button>
      </div>
    </div>

    <div v-if="err" class="msg err">{{ err }}</div>
    <div v-if="info" class="msg ok">{{ info }}</div>

    <!-- 新增 / 编辑 -->
    <div v-if="showForm" class="card">
      <h3>{{ form.id ? `编辑代理 #${form.id}` : '新增代理' }}</h3>
      <div class="grid">
        <label>
          <span>名称</span>
          <input v-model="form.name" type="text" placeholder="名称（例如：家用 SOCKS 代理）" />
        </label>
        <!-- 第 7 条：类型是单选项，不再手打协议前缀 -->
        <label>
          <span>类型</span>
          <div class="radios">
            <label v-for="t in PROXY_TYPES" :key="t.v" class="radio"
                   :class="{ on: form.scheme === t.v }">
              <input type="radio" :value="t.v" v-model="form.scheme"
                     @change="form.port = defaultPort(t.v)" />
              {{ t.t }}
            </label>
          </div>
        </label>
        <label>
          <span>主机</span>
          <input v-model="form.host" type="text" placeholder="127.0.0.1 或 proxy.example.com" />
        </label>
        <label>
          <span>端口</span>
          <input v-model.number="form.port" type="number" min="1" max="65535" />
        </label>
        <label>
          <span>账号（可选）</span>
          <input v-model="form.username" type="text" autocomplete="off"
                 :placeholder="form.id ? '留空表示不修改' : '没有账号就留空'" />
        </label>
        <label v-if="form.id">
          <span>新密码（留空表示不修改）</span>
          <input v-model="form.password" type="password" autocomplete="new-password" />
        </label>
        <label v-else>
          <span>密码（可选）</span>
          <input v-model="form.password" type="password" autocomplete="new-password" />
        </label>
        <label>
          <span>优先级（数值越小越优先）</span>
          <input v-model.number="form.priority" type="number" />
        </label>
        <label class="chk">
          <input v-model="form.enabled" type="checkbox" />
          <span>启用（停用后不参与自动选择，条目保留）</span>
        </label>
      </div>
      <p class="hint">
        类型 + 主机 + 端口会拼成 <code>{{ form.scheme }}://{{ form.host || 'host' }}:{{ form.port }}</code>
        再保存；密码不会回显，列表里显示成 <code>user:***</code> 是打码，不是真的变成星号。
      </p>
      <div class="foot">
        <button class="btn primary" :disabled="busy === 'save'" @click="save">
          <Icon name="save" :size="14" />保存
        </button>
        <button class="btn ghost" @click="showForm = false"><Icon name="x" :size="14" />取消</button>
      </div>
    </div>

    <table v-if="total" class="tbl">
      <thead>
        <tr>
          <th style="width:52px">启用</th>
          <th>名称 / 地址</th>
          <th style="width:110px">延迟</th>
          <th style="width:90px">连续失败</th>
          <th>最近错误</th>
          <th style="width:210px">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="p in items" :key="p.id" :class="{ off: !p.enabled }">
          <td><input type="checkbox" :checked="p.enabled" :title="p.enabled ? '点击停用' : '点击启用'"
                     @change="toggle(p)" /></td>
          <td>
            <div class="nm">{{ p.name }}</div>
            <div class="url">{{ p.url }}</div>
          </td>
          <td>
            <span :class="latClass(p)">
              {{ (!p.latency_ms || p.latency_ms < 0) ? '未测' : p.latency_ms + ' ms' }}
            </span>
          </td>
          <td :class="{ bad: (p.failures || 0) >= 3 }">{{ p.failures || 0 }}</td>
          <td class="err-cell">{{ p.last_error || '—' }}</td>
          <td class="ops">
            <button class="btn sm" :disabled="busy === 'check:' + p.id || busy === 'check'"
                    @click="checkOne(p)">
              <Icon :anim="busy === 'check:' + p.id ? 'spin' : ''" name="gauge" :size="13" />
              {{ busy === 'check:' + p.id ? '测试中…' : '测速' }}
            </button>
            <button class="btn sm" @click="openEdit(p)"><Icon name="pencil" :size="13" />编辑</button>
            <button class="btn sm danger" :disabled="busy === 'del:' + p.id" @click="del(p)">
              <Icon name="trash" :size="13" />删除
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <p v-else class="empty">
      还没有代理。如果你本机跑着 v2rayN / Clash，通常就是
      <code>http://127.0.0.1:10808</code> 格式的地址，选择「新增代理」添加一条记录。
    </p>
  </div>
</template>

<style scoped>
.page { display: grid; gap: 12px; }
.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; flex-wrap: wrap; }
.head h2 { margin: 0 0 4px; font-size: 20px; display: flex; align-items: center; gap: 7px; }
.sub { margin: 0; color: var(--muted); font-size: 13px; max-width: 720px; }
.acts { display: flex; gap: 8px; flex-wrap: wrap; }

.msg { padding: 8px 12px; border-radius: 8px; font-size: 13px; }
.msg.err { background: rgba(216, 74, 74, .10); color: var(--err, #d84a4a); }
.msg.ok { background: rgba(46, 158, 91, .10); color: #2e9e5b; }

.card { padding: 14px 16px; border: 1px solid var(--line); border-radius: 11px; background: #fff; }
.card h3 { margin: 0 0 12px; font-size: 15px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 14px; }
.grid label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
.grid label > span { color: var(--muted); font-size: 12px; }
.grid label.chk { flex-direction: row; align-items: center; gap: 7px; }
/* 第 7 条：类型单选 —— 做成胶囊按钮，比原生 radio 好点、也看得清选中的是哪个 */
.radios { display: flex; gap: 6px; flex-wrap: wrap; }
.radios .radio {
  display: inline-flex; align-items: center; gap: 5px; padding: 5px 10px;
  border: 1px solid var(--line); border-radius: 999px; cursor: pointer;
  font-size: 12.5px; color: var(--muted); background: #fff;
}
.radios .radio.on { border-color: var(--brand); color: var(--brand);
  background: #f5f2ff; font-weight: 600; }
.radios .radio input { margin: 0; accent-color: var(--brand); }
.hint { margin: 12px 0 0; font-size: 12px; color: var(--muted); }
.hint code, .empty code { background: #f4f4f8; padding: 1px 4px; border-radius: 4px; }
.foot { display: flex; gap: 8px; margin-top: 12px; }

.tbl { width: 100%; border-collapse: collapse; font-size: 13px; background: #fff; }
.tbl th, .tbl td {
  text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top;
}
.tbl th { color: var(--muted); font-weight: 500; font-size: 12px; }
.tbl tbody tr:nth-child(even) { background: #fcfcff; }
.tbl tbody tr.off { opacity: .55; }
.nm { font-weight: 600; }
.url { color: var(--muted); word-break: break-all; font-family: ui-monospace, monospace; font-size: 12px; }
.err-cell { color: var(--muted); font-size: 12px; word-break: break-word; }
.bad { color: var(--err, #d84a4a); font-weight: 600; }
.dim { color: var(--muted); }
.fast { color: #2e9e5b; }
.mid { color: #b8860b; }
.slow { color: #d8843a; }
.ops { display: flex; flex-wrap: wrap; gap: 6px; }
.empty { padding: 26px 12px; color: var(--muted); text-align: center; font-size: 13px; }

@media (max-width: 820px) {
  .grid { grid-template-columns: 1fr; }
  .tbl { display: block; overflow-x: auto; }
}
</style>
