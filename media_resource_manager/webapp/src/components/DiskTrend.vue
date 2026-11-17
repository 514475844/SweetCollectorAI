<script setup>
// V0.0.17 · 磁盘近 7 日增长趋势（/api/diskspace/trend）。
// 纯 SVG 绘制迷你条形图，不引入任何图表库；无历史快照时显示明确空态。
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import Icon from './Icon.vue'

const data = ref(null)
const err = ref('')
const busy = ref(false)
const W = 280
const H = 92
const SLOT = 38
const BASE = 70

const bars = computed(() => {
  const items = (data.value && data.value.items) || []
  const max = Math.max(1, ...items.map((x) => Number(x.total_bytes || 0)))
  return items.map((x, i) => {
    const n = Number(x.total_bytes || 0)
    const h = x.recorded ? Math.max(3, Math.round((n / max) * 56)) : 2
    return {
      x: 8 + i * SLOT, y: BASE - h, w: 26, h,
      label: String(x.date || '').slice(5),
      value: x.text || '', delta: x.delta_text || '',
      recorded: !!x.recorded,
    }
  })
})
const empty = computed(() => !!(data.value && data.value.empty))
const note = computed(() => (data.value && data.value.note) || '')

async function load() {
  busy.value = true
  err.value = ''
  try {
    const r = await api.diskspaceTrend()
    if (!r.ok) throw new Error(r.error || '读取增长趋势失败，请稍后重试。')
    data.value = r
  } catch (e) {
    err.value = e.message || '读取增长趋势失败，请稍后重试。'
  } finally { busy.value = false }
}
onMounted(load)
</script>

<template>
  <div class="trend">
    <div class="bar">
      <b><Icon name="pie-chart" :size="14" />近 7 日增长趋势</b>
      <button class="btn sm" :disabled="busy" @click="load">
        <Icon :anim="busy ? 'spin' : ''" name="refresh-cw" :size="12" />刷新</button>
    </div>
    <p v-if="err" class="err small">{{ err }}</p>
    <template v-if="empty">
      <p class="muted small">{{ note || '暂无历史快照数据，趋势将在每日记录后呈现。' }}</p>
    </template>
    <template v-else>
      <svg class="chart" :viewBox="`0 0 ${W} ${H}`" role="img" aria-label="近 7 日磁盘增长趋势">
        <line :x1="4" :y1="BASE" :x2="W - 4" :y2="BASE" class="axis" />
        <g v-for="(b, i) in bars" :key="i">
          <rect :x="b.x" :y="b.y" :width="b.w" :height="b.h" rx="2"
                :class="b.recorded ? 'col' : 'none'" />
          <text :x="b.x + b.w / 2" :y="H - 6" class="lab">{{ b.label }}</text>
        </g>
      </svg>
      <div class="legend">
        <span v-for="(b, i) in bars" :key="i" class="cell" :title="`${b.label}：${b.value}，增长 ${b.delta}`">
          <b>{{ b.value }}</b><span class="muted tiny">{{ b.delta }}</span>
        </span>
      </div>
    </template>
    <p class="muted tiny">数据来自运行软件那台机器的每日目录快照，空日为「无记录」。</p>
  </div>
</template>

<style scoped>
.trend { display: grid; gap: 6px; margin-top: 10px; padding: 10px 12px;
         border: 1px solid var(--line, rgba(0,0,0,.08)); border-radius: 10px; }
.bar { display: flex; align-items: center; justify-content: space-between; }
.bar b { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.chart { width: 100%; max-width: 340px; height: 92px; }
.axis { stroke: var(--line, rgba(0,0,0,.18)); stroke-width: 1; }
.col { fill: var(--brand, #6d4bd6); opacity: .82; }
.none { fill: var(--line, rgba(0,0,0,.18)); }
.lab { font-size: 9px; fill: var(--muted, #888); text-anchor: middle; }
.legend { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; }
.cell { display: grid; text-align: center; gap: 1px; font-size: 10px; }
.tiny { font-size: 10px; }
.err { color: var(--err, #b91c1c); }
</style>
