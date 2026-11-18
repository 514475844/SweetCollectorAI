<script setup>
// 采集周报 / 月报：数据与桌面端同一个 reporting.collect_period，导出直接用后端给的 markdown/csv。
// V0.0.18：新增「站点统计」分区，口径复用 site_stats.task_stats（与桌面端同一份）。
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import { useShellRefresh } from '../shellVue'
import Icon from '../components/Icon.vue'

const router = useRouter()
const kind = ref('week')
const offset = ref(0)
const d = ref(null)
const err = ref('')

async function load() {
  try { d.value = await api.get(`/api/report?kind=${kind.value}&offset=${offset.value}`); err.value = '' }
  catch (e) { err.value = e.message }
}
function download(name, text, type) {
  const blob = new Blob([text], { type: type || 'text/plain;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = name
  a.click()
  URL.revokeObjectURL(a.href)
}
function setKind(k) { kind.value = k; offset.value = 0; load() }

// ── V0.0.18 · 站点统计 ──
const days = ref(0)
const stats = ref(null)
const statsErr = ref('')
const statsBusy = ref(false)
const hints = ref([])

async function loadStats() {
  statsBusy.value = true
  statsErr.value = ''
  try {
    stats.value = await api.siteStats(days.value)
  } catch (e) { statsErr.value = e.message || '读取站点统计失败' }
  finally { statsBusy.value = false }
}
async function loadHints() {
  try { const r = await api.cookieHints(); hints.value = (r && r.items) || [] }
  catch { hints.value = [] }
}
function setDays(n) { days.value = n; loadStats() }
function gotoCookie() { router.push({ path: '/settings', query: { tab: 'store' } }) }

function fmtBytes(b) {
  const n = Number(b) || 0
  if (n <= 0) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0; let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1 }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`
}
function fmtRate(r) {
  if (r === null || r === undefined) return '暂无'
  const p = Math.round(Number(r) * 100)
  return `${(p / 100).toFixed(p % 100 === 0 ? 0 : (p % 10 === 0 ? 1 : 2))}%`
}
function fmtSpeed(v) {
  if (v === null || v === undefined) return '暂无'
  return `${Number(v).toFixed(1)} 张/时`
}

onMounted(() => { load(); loadStats(); loadHints() })
useShellRefresh(() => { load(); loadStats(); loadHints() })
</script>

<template>
  <div class="page">
    <div class="card bar">
      <div class="tabs">
        <button class="tab" :class="{ on: kind === 'week' }" @click="setKind('week')">周报</button>
        <button class="tab" :class="{ on: kind === 'month' }" @click="setKind('month')">月报</button>
      </div>
      <div class="ops">
        <button class="btn sm" :disabled="offset <= 0" @click="offset--; load()"><Icon name="chevron-left" :size="13" />← 上一期</button>
        <span class="muted small">往前 {{ offset }} 期</span>
        <button class="btn sm" :disabled="offset === 0" @click="offset = 0; load()">回到当期</button>
        <button class="btn sm" :disabled="!d" @click="download(`采集${kind === 'week' ? '周报' : '月报'}.md`, d.markdown)"><Icon name="download" :size="13" />导出 Markdown</button>
        <button class="btn sm" :disabled="!d" @click="download(`采集${kind === 'week' ? '周报' : '月报'}.csv`, d.csv, 'text/csv')"><Icon name="download" :size="13" />导出 CSV</button>
      </div>
    </div>
    <p v-if="err" class="err">{{ err }}</p>
    <div v-if="d" class="card body">
      <h3>{{ d.headline }}</h3>
      <pre class="md">{{ d.markdown }}</pre>
    </div>

    <!-- V0.0.18 · 站点统计（口径复用 site_stats.task_stats，与桌面端同一份） -->
    <div class="card sec">
      <div class="sec-hd">
        <Icon name="gauge" :size="15" />
        <h3>站点统计</h3>
        <div class="days">
          <button class="tab" :class="{ on: days === 0 }" @click="setDays(0)">全部</button>
          <button class="tab" :class="{ on: days === 7 }" @click="setDays(7)">近 7 天</button>
          <button class="tab" :class="{ on: days === 30 }" @click="setDays(30)">近 30 天</button>
        </div>
        <button class="btn sm" :disabled="statsBusy" @click="loadStats(); loadHints()">
          <Icon :anim="statsBusy ? 'spin' : ''" :name="statsBusy ? 'loader-circle' : 'refresh-cw'" :size="13" />重新读取</button>
      </div>
      <p v-if="statsErr" class="err">{{ statsErr }}</p>

      <!-- Cookie 疑似登录失效提示条 -->
      <div v-if="hints.length" class="cookiebar">
        <Icon name="shield-alert" :size="15" />
        <div class="cb-body">
          <b>疑似登录失效</b>
          <span class="muted small">以下任务的失败原因命中鉴权特征，建议核对站点 Cookie 是否过期。</span>
          <ul class="cb-list">
            <li v-for="h in hints" :key="h.task_id">
              {{ h.task }}
              <span class="mono tiny">{{ (h.hosts_with_cookie.length ? '已有 Cookie：' : '待补充域名：') + [...h.hosts_with_cookie, ...h.hosts_without_cookie].join('、') }}</span>
            </li>
          </ul>
        </div>
        <button class="btn sm primary" @click="gotoCookie">前往 Cookie 管理</button>
      </div>

      <div v-if="stats && stats.items && stats.items.length" class="tbl">
        <div class="tr th">
          <span>任务</span><span>站点</span><span class="num">图数</span>
          <span class="num">成功率</span><span class="num">速率</span>
          <span class="num">占用</span><span>失败原因 Top</span>
        </div>
        <div v-for="it in stats.items" :key="it.task_id" class="tr">
          <span class="task">{{ it.task }}
            <span v-if="it.auth_suspect" class="pill warn" title="疑似登录失效">鉴权</span></span>
          <span class="hosts mono tiny">{{ (it.hosts || []).join('、') || '暂无' }}</span>
          <span class="num mono">{{ it.images }}</span>
          <span class="num mono">{{ fmtRate(it.success_rate) }}</span>
          <span class="num mono">{{ fmtSpeed(it.images_per_hour) }}</span>
          <span class="num mono">{{ fmtBytes(it.disk_bytes) }}</span>
          <span class="errs">
            <span v-for="(e, k) in it.top_errors" :key="k" class="err-item" :title="e.error">
              {{ e.error }}<b class="mono">{{ e.count }}</b></span>
            <span v-if="!it.top_errors || !it.top_errors.length" class="muted tiny">暂无失败记录</span>
          </span>
        </div>
      </div>
      <p v-else-if="stats && !statsBusy" class="muted small">暂无站点统计数据。</p>
    </div>
  </div>
</template>

<style scoped>
/* V0.0.27：块与块之间统一 14px 缝（这页以前没有 page 间距，卡片全贴在一起） */
.page { display: grid; gap: 14px; align-items: start; }
.bar { display: flex; justify-content: space-between; align-items: center; padding: 10px 14px; gap: 10px; flex-wrap: wrap; }
.tabs { display: flex; gap: 6px; }
.tab { border: 1px solid var(--line); background: #fff; padding: 5px 14px; border-radius: 999px; cursor: pointer; }
.tab.on { background: #efeaff; border-color: #ded4ff; color: var(--brand); font-weight: 600; }
.ops { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.body { padding: 16px 18px; }
.body h3 { margin: 0 0 10px; font-size: 16px; }
.md { white-space: pre-wrap; margin: 0; font-size: 13px; line-height: 1.75; color: var(--text); }
.err { color: var(--err); margin: 0; }
.small { font-size: 12px; }
.tiny { font-size: 11px; }
.mono { font-variant-numeric: tabular-nums; }
/* V0.0.59：按钮尺寸已统一到全局 style.css，此处局部覆盖删除 */
.muted { color: var(--muted); }
/* V0.0.18 · 站点统计 */
.sec { padding: 16px 18px; margin-top: 14px; }
.sec-hd { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.sec-hd h3 { margin: 0; font-size: 15px; }
.days { display: flex; gap: 6px; margin-left: auto; }
.cookiebar { display: flex; align-items: flex-start; gap: 10px; padding: 10px 12px; margin-bottom: 12px;
  border: 1px solid #f0d9a8; border-radius: 10px; background: #fdf6e7; color: #8a6200; }
.cb-body { flex: 1; display: flex; flex-direction: column; gap: 4px; }
.cb-list { margin: 2px 0 0; padding-left: 18px; font-size: 12.5px; }
.cb-list li { margin: 2px 0; }
.tbl { border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.tr { display: grid; grid-template-columns: 1.3fr 1.2fr .6fr .7fr .8fr .7fr 1.6fr; gap: 8px;
  padding: 7px 12px; font-size: 12.5px; border-bottom: 1px solid var(--line); align-items: center; }
.tr:last-child { border-bottom: 0; }
.tr.th { background: #faf9ff; color: var(--muted); font-size: 11.5px; font-weight: 600; }
.tr .num { text-align: right; }
.tr.th .num { text-align: right; }
.task { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hosts { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.errs { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.err-item { font-size: 11.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.err-item b { color: var(--err); margin-left: 6px; }
.pill.warn { background: #fdf0d5; color: #8a6200; border-radius: 999px; padding: 0 7px; font-size: 11px; margin-left: 5px; }
</style>
