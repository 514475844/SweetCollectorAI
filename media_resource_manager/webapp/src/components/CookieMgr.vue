<template>
  <!-- R251：站点 Cookie。Cloudflare 这类站点的登录态只能从真实浏览器拿，
       拿到后按**域名**存一份，这个站的所有任务自动带上。 -->
  <div class="ck">
    <div class="tiny muted">
      存的是「按域名」的 Cookie：同一个站建多少个任务都会自动带上，
      不用每个任务手填一次。列表里只显示名字，<b>值不会显示</b>（与密码同级）。
    </div>

    <!-- 插件 + 配对码 -->
    <div class="pair">
      <div class="inline">
        <span class="tiny">浏览器插件配对码</span>
        <code class="code" :title="'在浏览器插件中填入该配对码，之后点击推送按钮即可发送 Cookie'">
          {{ pair || '获取中…' }}
        </code>
        <button class="btn sm" :disabled="busy" @click="rotate">
          <Icon name="refresh-cw" :size="13" />换一个</button>
        <button class="btn sm" :disabled="busy" @click="copyCode">
          <Icon name="copy" :size="13" />复制</button>
      </div>
      <div class="tiny muted">
        插件位于安装目录的 <code>extension/Sweet采集助手</code>：在浏览器「扩展管理 → 开发者模式」
        中选择「加载已解压的扩展」。首次填写软件地址与配对码，此后在目标网站点击推送按钮即可。
      </div>
    </div>

    <!-- 已存列表 -->
    <div v-if="items.length" class="tbl">
      <div class="thead"><span>域名</span><span>Cookie</span><span></span></div>
      <div v-for="it in items" :key="it.domain" class="trow">
        <span class="mono dom">{{ it.domain }}</span>
        <span class="tiny names" :title="it.names.join('、')">
          {{ it.count }} 条：{{ it.names.join('、') }}<span v-if="it.more">…</span>
        </span>
        <button class="btn sm danger" :disabled="busy" @click="del(it.domain)">
          <Icon name="trash" :size="13" />删除</button>
      </div>
    </div>
    <div v-else class="tiny muted">暂无已保存的 Cookie。</div>

    <!-- 手动录入 / 导入 -->
    <div class="add">
      <div class="inline">
        <input v-model="domain" class="in-d" placeholder="域名，例如 example.com" />
        <button class="btn sm primary" :disabled="busy" @click="saveHeader">
          <Icon name="save" :size="13" />保存这条 Cookie</button>
      </div>
      <textarea v-model="text" class="ta" rows="3"
                placeholder="粘贴 Cookie（k=v; k2=v2），或直接粘贴 Cookie-Editor 导出的 JSON（整段）"></textarea>
      <div class="inline">
        <button class="btn sm" :disabled="busy || !text.trim()" @click="saveAuto">
          <Icon name="upload" :size="13" />解析并导入</button>
        <label class="chk tiny">
          <input type="checkbox" v-model="merge" />同名合并（不勾选 = 整域替换）
        </label>
      </div>
    </div>

    <p v-if="msg" class="tiny ok">{{ msg }}</p>
    <p v-if="err" class="tiny err">{{ err }}</p>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
import Icon from './Icon.vue'

const items = ref([])
const pair = ref('')
const domain = ref('')
const text = ref('')
const merge = ref(true)
const busy = ref(false)
const msg = ref('')
const err = ref('')

function clear() { msg.value = ''; err.value = '' }

async function load() {
  try {
    const r = await api.cookiesList()
    if (!r.ok) { err.value = r.error || '读取失败'; return }
    items.value = r.items || []
    pair.value = r.pair_code || ''
  } catch (e) { err.value = e.message }
}

async function post(body) {
  busy.value = true
  clear()
  try {
    const r = await api.cookiesPost(body)
    if (!r.ok) { err.value = r.error || '操作失败'; return null }
    return r
  } catch (e) { err.value = e.message; return null }
  finally { busy.value = false }
}

async function rotate() {
  if (!confirm('换一个新的配对码？插件里要重新填一次。')) return
  const r = await post({ action: 'rotate' })
  if (r) { pair.value = r.pair_code; msg.value = r.message || '已更换' }
}

async function copyCode() {
  try {
    await navigator.clipboard.writeText(pair.value)
    msg.value = '配对码已复制'
  } catch (e) { err.value = '复制失败（浏览器不允许）：' + e.message }
}

async function del(dom) {
  if (!confirm(`删除 ${dom} 的 Cookie？\n之后这个站的任务要重新拿一次。`)) return
  const r = await post({ action: 'delete', domain: dom })
  if (r) { msg.value = `已删除 ${dom}`; await load() }
}

async function saveHeader() {
  if (!domain.value.trim()) { err.value = '先填域名'; return }
  if (!text.value.trim()) { err.value = '先粘贴 Cookie 内容'; return }
  const r = await post({
    action: 'save', domain: domain.value, text: text.value, merge: merge.value,
  })
  if (r) {
    msg.value = `已保存：${Object.keys(r.domains || {}).join('、')}`
    text.value = ''
    await load()
  }
}

async function saveAuto() {
  const r = await post({
    action: 'import', domain: domain.value, text: text.value, merge: merge.value,
  })
  if (r) {
    const doms = Object.keys(r.domains || {})
    msg.value = `已导入 ${r.imported || 0} 条到 ${doms.join('、')}`
    text.value = ''
    await load()
  }
}

onMounted(load)
</script>

<style scoped>
.ck { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.pair {
  display: flex; flex-direction: column; gap: 4px;
  padding: 8px 10px; border-radius: 8px; background: var(--panel2, #f6f3ff);
}
.code {
  font-size: 15px; font-weight: 700; letter-spacing: 3px;
  padding: 2px 10px; border-radius: 6px; background: var(--panel, #fff);
  color: var(--brand);
}
.tbl { border: 1px solid var(--line, #e6e1f5); border-radius: 8px; overflow: hidden; }
.thead, .trow {
  display: grid; grid-template-columns: 180px 1fr 76px; gap: 8px;
  align-items: center; padding: 6px 10px; font-size: 12px;
}
.thead { background: var(--panel2, #f6f3ff); font-weight: 600; }
.trow { border-top: 1px solid var(--line, #eee9fa); }
.dom { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.names { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; opacity: .85; }
.add { display: flex; flex-direction: column; gap: 6px; }
.in-d { width: 220px; }
.ta { width: 100%; box-sizing: border-box; resize: vertical; font-size: 12px;
  font-family: Consolas, monospace; padding: 6px 8px; border-radius: 8px;
  border: 1px solid var(--line, #ddd6f3); background: var(--panel, #fff);
  color: var(--text); }
.chk { display: inline-flex; align-items: center; gap: 5px; }
.btn:disabled { opacity: .5; cursor: not-allowed; }
</style>
