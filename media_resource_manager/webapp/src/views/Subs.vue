<!--
  站点订阅 / 新图监控（2026-10-28，编号 21、22）

  这是 qBittorrent「RSS 自动下载」在采集软件里的等价物：定时看一个页面，
  出现新条目就自动建任务、只下新的。

  交互上刻意分两步（先预览再开），因为"猜错了就建一堆垃圾任务"比
  "少抓几张"难受得多：
    1. 填写地址 → 执行「测试抓取」→ 确认识别到的条目；
    2. 合意了再勾「启用」并保存。
-->
<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { api } from '../api'
import { useShellRefresh } from '../shellVue'
import Icon from '../components/Icon.vue'

const subs = ref([])
const enabled = ref(true)
const defaultInterval = ref(60)
const err = ref('')
const info = ref('')
const busy = ref('')

// 编辑中的表单（id=0 表示新增）
const form = reactive({
  id: 0, name: '', url: '', pattern: '', interval_min: 60, max_items: 30,
  enabled: true, auto_run: true, output_dir: '',
})
const showForm = ref(false)

// 预览结果
const preview = ref(null)         // { links:[], count, truncated }
const items = ref(null)           // 某条订阅的历史快照
const itemsFor = ref(0)

const patternHint = '留空则自动识别（同域、跳过导航页）；填写正则表达式则仅保留匹配的链接，例如 /\\/post\\/\\d+/'

const enabledCount = computed(() => subs.value.filter((s) => s.enabled).length)

async function load() {
  err.value = ''
  try {
    const d = await api.subs()
    subs.value = d.subs || []
    enabled.value = d.enabled !== false
    defaultInterval.value = d.default_interval || 60
  } catch (e) {
    err.value = e.message || '读取订阅失败'
  }
}

function openNew() {
  Object.assign(form, {
    id: 0, name: '', url: '', pattern: '',
    interval_min: defaultInterval.value || 60, max_items: 30,
    enabled: true, auto_run: true, output_dir: '',
  })
  preview.value = null
  showForm.value = true
  info.value = ''
  err.value = ''
}

function openEdit(s) {
  Object.assign(form, {
    id: s.id, name: s.name, url: s.url, pattern: s.pattern || '',
    interval_min: s.interval_min, max_items: s.max_items,
    enabled: s.enabled, auto_run: s.auto_run, output_dir: s.output_dir || '',
  })
  preview.value = null
  showForm.value = true
  info.value = ''
  err.value = ''
}

async function doPreview() {
  if (!form.url) { err.value = '请先填写订阅地址'; return }
  busy.value = 'preview'
  err.value = ''
  preview.value = null
  try {
    const d = await api.subPreview(form.url, form.pattern)
    if (!d.ok) throw new Error(d.error || '试抓失败')
    preview.value = d
    info.value = d.count
      ? `检测成功：识别到 ${d.count} 条链接${d.truncated ? '（只显示前 40 条）' : ''}`
      : '检测成功，但未识别到任何条目。请尝试放宽链接规则'
  } catch (e) {
    err.value = e.message || '试抓失败'
  } finally {
    busy.value = ''
  }
}

async function doSave() {
  if (!form.url) { err.value = '请先填写订阅地址'; return }
  busy.value = 'save'
  err.value = ''
  try {
    const d = await api.subSave({ ...form })
    if (!d.ok) throw new Error(d.error || '保存失败')
    info.value = d.note || '已保存'
    showForm.value = false
    await load()
  } catch (e) {
    err.value = e.message || '保存失败'
  } finally {
    busy.value = ''
  }
}

async function doDelete(s) {
  if (!confirm(`确认删除订阅「${s.name}」？\n历史快照将一并删除；重新订阅后现有条目会被视为新条目。`)) return
  busy.value = 'del' + s.id
  err.value = ''
  try {
    const d = await api.subDelete(s.id)
    if (!d.ok) throw new Error(d.error || '删除失败')
    info.value = d.note || '已删除'
    await load()
  } catch (e) {
    err.value = e.message || '删除失败'
  } finally {
    busy.value = ''
  }
}

async function doCheck(s, run) {
  busy.value = 'chk' + s.id + (run ? 'r' : '')
  err.value = ''
  try {
    const d = await api.subCheck(s.id, run)
    if (!d.ok) throw new Error(d.error || '检查失败')
    preview.value = { links: d.items || [], count: d.found || 0 }
    info.value = `「${s.name}」：页面上 ${d.found} 条，新条目 ${d.new} 条。${d.note || ''}`
    await load()
  } catch (e) {
    err.value = e.message || '检查失败'
  } finally {
    busy.value = ''
  }
}

async function toggle(s) {
  err.value = ''
  try {
    const d = await api.subSave({ ...s, enabled: !s.enabled })
    if (!d.ok) throw new Error(d.error || '保存失败')
    await load()
  } catch (e) {
    err.value = e.message || '保存失败'
  }
}

async function showItems(s) {
  err.value = ''
  itemsFor.value = s.id
  try {
    const d = await api.subItems(s.id)
    if (!d.ok) throw new Error(d.error || '读取失败')
    items.value = d
  } catch (e) {
    err.value = e.message || '读取失败'
  }
}

function fmtNext(s) {
  if (!s.last_check) return '尚未检查'
  return '上次 ' + s.last_check
}

onMounted(load)
useShellRefresh(() => load())
</script>

<template>
  <div class="page">
    <div class="head">
      <!-- V0.0.27：页顶横幅已经显示「站点订阅」，这里不再重复一个标题 -->
      <div class="sub">
        定时看这些页面，出现新条目就自动建任务、只下新的。
        当前共 {{ subs.length }} 个订阅（启用 {{ enabledCount }} 个）<span v-if="!enabled">，总开关已关闭</span>。
      </div>
      <div class="actions">
        <!-- 2026-10-29（第 6 条）：原来图标一个加号 + 文本里又写了一个「＋」，
             渲染出来是「＋ ＋ 新增订阅」。去掉文本里那个。 -->
        <button class="btn" @click="openNew"><Icon name="plus" :size="13" />新增订阅</button>
        <button class="btn ghost" @click="load"><Icon name="refresh-cw" :size="13" />刷新</button>
      </div>
    </div>

    <div v-if="err" class="msg err">{{ err }}</div>
    <div v-if="info" class="msg ok">{{ info }}</div>

    <table class="tbl" v-if="subs.length">
      <thead>
        <tr>
          <th style="width:56px">启用</th>
          <th>名称 / 地址</th>
          <th style="width:120px">间隔</th>
          <th style="width:90px">单次上限</th>
          <th style="width:150px">状态</th>
          <th style="width:230px">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="s in subs" :key="s.id">
          <td>
            <input type="checkbox" :checked="s.enabled" @change="toggle(s)" />
          </td>
          <td>
            <div class="nm">{{ s.name }}</div>
            <div class="url">{{ s.url }}</div>
            <div class="pat" v-if="s.pattern">规则：{{ s.pattern }}</div>
          </td>
          <td>{{ s.interval_min }} 分钟</td>
          <td>{{ s.max_items }} 条</td>
          <td>
            <div>{{ fmtNext(s) }}</div>
            <div class="dim" v-if="s.last_found">页 {{ s.last_found }} / 新 {{ s.last_new }}</div>
            <div class="dim">已认识 {{ s.seen }} 条 / 累计新增 {{ s.total_new }}</div>
            <div class="bad" v-if="s.last_error">{{ s.last_error }}</div>
          </td>
          <td class="ops">
            <button class="btn sm" :disabled="busy === 'chk' + s.id" @click="doCheck(s, false)">
              <Icon v-if="busy === 'chk' + s.id" anim="spin" :size="13" />
              <Icon v-else name="scan-search" :size="13" />
              {{ busy === 'chk' + s.id ? '检查中…' : '仅检查' }}
            </button>
            <button class="btn sm" :disabled="busy === 'chk' + s.id + 'r'" @click="doCheck(s, true)">
              <Icon v-if="busy === 'chk' + s.id + 'r'" anim="dl" :size="13" />
              <Icon v-else name="download" :size="13" />
              {{ busy === 'chk' + s.id + 'r' ? '抓取中…' : '立即抓取' }}
            </button>
            <button class="btn sm ghost" @click="showItems(s)"><Icon name="clock" :size="13" />历史</button>
            <button class="btn sm ghost" @click="openEdit(s)"><Icon name="pencil" :size="13" />编辑</button>
            <button class="btn sm danger" @click="doDelete(s)"><Icon name="trash" :size="13" />删除</button>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-else class="empty">
      暂无订阅源。选择「新增订阅」，填写图集列表页 / 作者页 / 标签页的网址。
    </div>

    <!-- 编辑表单 -->
    <div class="card" v-if="showForm">
      <h3>{{ form.id ? '编辑订阅' : '新增订阅' }}</h3>
      <div class="grid">
        <label>
          <span>名称</span>
          <input v-model="form.name" placeholder="留空则使用域名作为名称" />
        </label>
        <label class="wide">
          <span>订阅地址</span>
          <input v-model="form.url" placeholder="https://example.com/gallery 或作者页/标签页" />
        </label>
        <label class="wide">
          <span>链接规则（可选）</span>
          <input v-model="form.pattern" :placeholder="patternHint" />
        </label>
        <label>
          <span>检查间隔（分钟）</span>
          <input type="number" min="5" max="10080" v-model.number="form.interval_min" />
        </label>
        <label>
          <span>单次最多建几条</span>
          <input type="number" min="1" max="500" v-model.number="form.max_items" />
        </label>
        <label>
          <span>保存目录（可选）</span>
          <input v-model="form.output_dir" placeholder="留空则使用全局图库目录" />
        </label>
        <label class="chk">
          <input type="checkbox" v-model="form.enabled" />
          <span>启用（后台定时检查）</span>
        </label>
        <label class="chk">
          <input type="checkbox" v-model="form.auto_run" />
          <span>发现新条目后自动开始下载</span>
        </label>
      </div>
      <div class="hintbox">
        建议流程：先执行「测试抓取」，确认识别到的条目；如不符合预期，填写链接规则
        （只保留匹配的链接）再试。规则填太严会漏，太松会把导航页当条目。
      </div>
      <div class="foot">
        <button class="btn" :disabled="busy === 'save'" @click="doSave">
          {{ busy === 'save' ? '保存中…' : '保存' }}
        </button>
        <button class="btn ghost" :disabled="busy === 'preview'" @click="doPreview">
          {{ busy === 'preview' ? '检测中…' : '测试抓取' }}
        </button>
        <button class="btn ghost" @click="showForm = false">取消</button>
      </div>

      <div v-if="preview" class="preview">
        <div class="dim">认出 {{ preview.count }} 条，下面最多显示 40 条：</div>
        <ul>
          <li v-for="(u, i) in preview.links" :key="i">
            <a :href="u" target="_blank" rel="noreferrer">{{ u }}</a>
          </li>
          <li v-if="!preview.links.length" class="dim">（暂无）</li>
        </ul>
      </div>
    </div>

    <!-- 历史快照 -->
    <div class="card" v-if="items">
      <h3>历史条目（订阅 #{{ itemsFor }}，共认识 {{ items.count }} 条）</h3>
      <div class="dim">以下为已识别条目，后续不再判定为新条目；仅显示最近 100 条。</div>
      <ul class="snap">
        <li v-for="(it, i) in items.items" :key="i">
          <a :href="it.url" target="_blank" rel="noreferrer">{{ it.url }}</a>
          <span class="dim"> · {{ it.first_seen }}<span v-if="it.task_id"> · 任务 #{{ it.task_id }}</span></span>
        </li>
        <li v-if="!items.items.length" class="dim">（暂无历史条目）</li>
      </ul>
      <div class="foot"><button class="btn ghost" @click="items = null"><Icon name="x" :size="13" />关闭</button></div>
    </div>
  </div>
</template>

<style scoped>
/* 2026-10-29（第 6 条）：这套样式是**旧版深色界面**留下来的（满篇 --mrm-* 变量，
   新版一个都没定义）。后果之一就是用户看到的那颗「纯黑按钮」：
   .btn 用了 var(--mrm-btn, #23262d) 当底色（深色兜底），而 color: inherit
   从浅色页面继承到近黑文字 —— 黑底黑字，不聚焦时什么都看不见；
   聚焦时浏览器给个默认焦点色才显得"变白底黑字"。
   这里把旧皮肤变量全部换成新版主题变量，去掉对 .card / .btn 的重复定义，
   让全局 style.css 的规则生效。 */
.page { padding: 18px 22px 40px; display: grid; gap: 14px; align-items: start; }
.head { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; margin-bottom: 12px; }
.head h2 { margin: 0; font-size: 20px; }
.head .sub { color: var(--muted); font-size: 13px; flex: 1 1 320px; }
.actions { display: flex; gap: 8px; }
.msg { margin: 6px 0 12px; padding: 8px 12px; border-radius: 6px; font-size: 13px; }
.msg.err { background: rgba(220, 60, 60, .1); color: var(--err); }
.msg.ok { background: rgba(22, 163, 74, .1); color: var(--ok); }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; }
.tbl th, .tbl td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
.tbl th { color: var(--muted); font-weight: 500; }
.nm { font-weight: 600; }
.url { color: var(--muted); word-break: break-all; }
.pat { color: var(--muted); font-family: monospace; font-size: 12px; }
.dim { color: var(--muted); font-size: 12px; }
.bad { color: var(--err); font-size: 12px; margin-top: 2px; }
.ops { display: flex; flex-wrap: wrap; gap: 6px; }
/* 只补全局没有的尺寸档位；底色/边框/文字色一律用全局 .btn */
.btn.sm { padding: 3px 8px; font-size: 12px; }
.btn.danger { color: var(--err); border-color: #f3c9c9; }
.empty { padding: 30px 10px; color: var(--muted); text-align: center; }
/* .card 只留本页需要的间距，不再覆盖边框/圆角（原来写的是深色边框） */
.card { margin-top: 18px; padding: 16px; }
.card h3 { margin: 0 0 12px; font-size: 16px; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.grid label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
.grid label.wide { grid-column: 1 / -1; }
.grid label.chk { flex-direction: row; align-items: center; gap: 8px; }
.grid label span:first-child { color: var(--muted); }
.hintbox { margin-top: 12px; padding: 8px 12px; font-size: 12px; color: var(--muted);
  border-left: 3px solid var(--brand); background: #f7f5ff; }
.foot { display: flex; gap: 8px; margin-top: 14px; }
.preview { margin-top: 14px; max-height: 300px; overflow: auto; font-size: 12px; }
.preview ul, .snap { margin: 6px 0 0; padding-left: 18px; }
.preview li, .snap li { word-break: break-all; margin: 3px 0; }
.preview a, .snap a { color: var(--brand); text-decoration: none; }
@media (max-width: 760px) {
  .grid { grid-template-columns: 1fr; }
  .tbl { font-size: 12px; }
  .ops { width: 100%; }
}
</style>