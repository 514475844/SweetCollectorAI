<script setup>
// 日志：/api/view/logstats 给「总条数 + 各动作计数 + 最近 50 条明细」。
// 老界面（/classic）有这个页，新版没有 —— 补齐，并按动作/关键词过滤。
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import { useShellRefresh } from '../shellVue'
import Icon from '../components/Icon.vue'

const d = ref(null)
const loading = ref(false)
const err = ref('')
const kw = ref('')
const busy = ref(false)
const info = ref('')

const counts = computed(() => (d.value?.items || []).filter((x) => x.name !== '日志条数'))
const total = computed(() => (d.value?.items || []).find((x) => x.name === '日志条数')?.value ?? 0)

const recent = computed(() => {
  const k = kw.value.trim().toLowerCase()
  const arr = d.value?.recent || []
  if (!k) return arr
  return arr.filter((r) =>
    [r.action_label, r.target_label, r.detail, r.actor_label, r.action, r.target]
      .some((v) => String(v || '').toLowerCase().includes(k)))
})

async function load() {
  loading.value = true
  try {
    d.value = await api.logstats()
    err.value = ''
  } catch (e) {
    err.value = e.message
  } finally {
    loading.value = false
  }
}

// V0.0.16：网页补齐桌面「维护 → 日志统计」的清空与导出动作。
// 导出按需求 19 加密为 .mrmx；清空只影响日志列表，不动任务与图片。
async function clearLogs() {
  const ok = confirm(
    `即将清空全部操作日志（共 ${total.value} 条），此操作不可撤销。\n\n` +
    '任务、图片与学习记录不会受到影响。请确认继续。')
  if (!ok) return
  busy.value = true
  try {
    const r = await api.siteLogsClear()
    if (!r.ok) throw new Error(r.error || '清空未完成')
    info.value = r.note || '操作日志已清空'
    err.value = ''
    await load()
  } catch (e) {
    err.value = e.message || '清空日志失败'
  } finally {
    busy.value = false
  }
}

onMounted(load)
useShellRefresh(() => load())
</script>

<template>
  <div class="page">
    <div class="card bar">
      <div class="left">
        <b>日志</b>
        <span class="muted small">共 {{ total }} 条；下面显示最近 50 条</span>
      </div>
      <div class="ops">
        <input v-model="kw" type="text" placeholder="搜动作 / 对象 / 详情…" />
        <button class="btn sm" :disabled="loading" @click="load"><Icon :anim="loading ? 'spin' : ''" name="refresh-cw" :size="13" />刷新</button>
        <a class="btn sm" :href="api.siteLogsExportUrl()" download title="导出为加密 .mrmx，仅本软件可读取">
          <Icon name="download" :size="13" />导出加密日志</a>
        <!-- V0.0.17：日志统计报告导出，一律加密 .mrmx，绝不出明文 CSV 或压缩包 -->
        <a class="btn sm" :href="api.logStatsExportUrl()" download title="统计报告加密为 .mrmx，仅本软件可读取">
          <Icon name="download" :size="13" />导出统计报告（加密）</a>
        <button class="btn sm danger" :disabled="busy || !total" @click="clearLogs">
          <Icon name="trash" :size="13" />清空日志</button>
      </div>
    </div>

    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="info" class="goodbar">{{ info }}</p>

    <div v-if="counts.length" class="card body">
      <h3>按动作统计</h3>
      <p class="muted small">下面每个标签是一种操作，数字是发生次数。</p>
      <div class="chips">
        <span v-for="c in counts" :key="c.name" class="chip">
          {{ String(c.name).replace('动作：', '') }}<b>{{ c.value }}</b>
        </span>
      </div>
    </div>

    <div class="card body">
      <h3>最近明细 <span class="muted small">（最近 50 条，新的在上）</span></h3>
      <p class="muted small">
        「谁操作的」一列：<b>客户端</b>＝在本软件窗口里点的；
        <b>浏览器</b>＝用手机或别的电脑打开网页点的；
        <b>旧记录</b>＝升级前的历史数据，当时未区分来源。
      </p>
      <div class="tw">
        <div class="tr th row">
          <span>时间</span><span>做了什么</span><span>对谁做的</span><span>说明</span><span>谁操作的</span>
        </div>
        <div v-for="r in recent" :key="r.id" class="tr row">
          <span class="mono small muted">{{ r.ts }}</span>
          <span>{{ r.action_label || r.action || '—' }}</span>
          <span class="mono small ell" :title="r.target_label || r.target">
            {{ r.target_label || r.target || '—' }}</span>
          <span class="ell" :title="r.detail">{{ r.detail || '（无额外说明）' }}</span>
          <span class="small muted">{{ r.actor_label || r.actor || '—' }}</span>
        </div>
        <div v-if="!recent.length" class="empty">
          {{ (d?.recent || []).length ? '未找到匹配的日志' : '暂无日志。运行任务、修改设置或审核图片后生成。' }}
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { display: grid; gap: 14px; }
.bar { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px; gap: 10px; flex-wrap: wrap; }
.left { display: flex; align-items: baseline; gap: 10px; }
.ops { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.ops input { width: 200px; }
.body { padding: 14px 16px; }
h3 { margin: 0 0 10px; font-size: 15px; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px; border-radius: 999px;
        background: var(--panel-2, rgba(0,0,0,.04)); font-size: 12px; }
.chip b { font-variant-numeric: tabular-nums; }
.tw { border-top: 1px solid var(--line, rgba(0,0,0,.08)); }
.row { display: grid; grid-template-columns: 132px 120px 150px 1fr 110px; gap: 8px; align-items: center;
       padding: 7px 4px; border-bottom: 1px solid var(--line, rgba(0,0,0,.06)); }
.th { font-size: 12px; color: var(--muted, #888); font-weight: 600; }
.ell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty { padding: 22px 10px; text-align: center; color: var(--muted, #888); }
.err { color: #b91c1c; margin: 0; }
.goodbar { color: var(--brand, #6d4bd6); margin: 0; font-size: 13px; }
@media (max-width: 820px) {
  .row { grid-template-columns: 100px 1fr; }
  .row span:nth-child(n+3) { display: none; }
}
</style>
