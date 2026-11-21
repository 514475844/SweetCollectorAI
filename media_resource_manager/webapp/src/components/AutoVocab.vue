<script setup>
// V0.0.71：AI 看图自己说词 → 用户勾选 → 写进口味库。
// 首页与学习页共用这一个组件（同一套逻辑，放两处入口）。
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
import Icon from './Icon.vue'

const emit = defineEmits(['applied'])

const methods = [
  { key: 'vocab', name: '扩内置词库扫描', tag: '最快',
    desc: '用内置通用词表逐图打分，统计高频词。离线、快、不花钱；词只来自词表。' },
  { key: 'local', name: '本地描述模型', tag: '离线造词',
    desc: 'AI 自己看图说词，不受词表限制。首次需在「AI 模型」页下载「BLIP 看图说话」（约 950 MB），慢但真能自由造词。' },
  { key: 'api', name: '外部视觉大模型', tag: '要配接口',
    desc: '接能看图的在线大模型描述画面。不用下模型，但要在设置里配 API 地址，按量计费。' },
]
const method = ref('vocab')
const amount = ref('1000')
const amounts = [
  { key: '1000', name: '抽样 1000 张' },
  { key: '5000', name: '抽样 5000 张' },
  { key: 'all', name: '全部' },
]

const running = ref(false)
const pct = ref(0)
const msg = ref('')
const words = ref([])          // [{ word, hits, samples }]
const votes = ref({})          // word -> 1(要) / -1(不要) / 0
const warning = ref('')
const applying = ref(false)
const jobId = ref('')          // V0.0.76：当前作业 id（供停止用）
const lastNote = ref('')       // V0.0.76：上次扫描成果的来源说明
const appliedNote = ref('')    // V0.0.78：自动入库数量，持久展示（不随 running/pct 消失）
let pollTimer = 0
let failStreak = 0             // V0.0.78：轮询连续失败计数，成功一次即清零
const MAX_POLL_FAILS = 5       // V0.0.78：连续失败 N 次才提示，但不放弃作业

const chosenYes = computed(() => words.value.filter((w) => votes.value[w.word] === 1).map((w) => w.word))
const chosenNo = computed(() => words.value.filter((w) => votes.value[w.word] === -1).map((w) => w.word))

function vote(word, v) {
  votes.value = { ...votes.value, [word]: votes.value[word] === v ? 0 : v }
}
function voteAll(v) {
  const o = {}
  for (const w of words.value) o[w.word] = v
  votes.value = o
}

async function start() {
  if (running.value) return
  running.value = true
  pct.value = 0
  msg.value = '准备中…'
  words.value = []
  votes.value = {}
  warning.value = ''
  lastNote.value = ''
  appliedNote.value = ''
  failStreak = 0
  try {
    const r = await api.autoVocabStart(method.value, amount.value)
    if (!r.ok) throw new Error(r.error || '启动失败')
    jobId.value = r.id
    poll(r.id)
  } catch (e) {
    msg.value = '出错：' + e.message
    running.value = false
  }
}

// V0.0.76：随时停止（挂机场景的后悔药）。已扫出的词自动保留，可直接勾选应用。
async function stop() {
  if (!jobId.value) return
  try { await api.autoVocabCancel(jobId.value) } catch { /* 忽略：作业可能刚好自己结束 */ }
}

function showResult(res, stopped) {
  words.value = (res && res.words) || []
  warning.value = (res && res.warning) || ''
  const o = {}
  for (const w of words.value) o[w.word] = 0
  votes.value = o
  // V0.0.78：自动入库数量写进持久展示区，不再只靠 poll 的 message 一闪而过
  const ap = res && res.applied
  if (ap && (Number(ap.added) || Number(ap.updated))) {
    appliedNote.value = `已自动加入口味库 ${Number(ap.added) || 0} 个词（更新 ${Number(ap.updated) || 0}）`
  }
  if (stopped) {
    warning.value = `已在 ${((res && res.scanned) || 0).toLocaleString('zh-CN')} 张处停止，以下列出已扫出的 ${words.value.length} 个词（成果已保留，随时可继续或应用）。`
  }
}

async function poll(id) {
  clearTimeout(pollTimer)
  pollTimer = setTimeout(async () => {
    try {
      const r = await api.autoVocabJob(id)
      if (!r.ok) throw new Error(r.error || '查询失败')
      failStreak = 0   // V0.0.78：成功一次即清零，容忍偶发抖动
      pct.value = r.pct || 0
      msg.value = r.message || ''
      if (r.state === 'done') {
        showResult(r.result, !!(r.result && r.result.stopped))
        running.value = false
        jobId.value = ''
        return
      }
      if (r.state === 'failed') {
        msg.value = '失败：' + (r.error || '未知错误')
        running.value = false
        jobId.value = ''
        return
      }
      poll(id)
    } catch (e) {
      // V0.0.78：单次查询失败（网络抖动/后端重启）不再放弃整个作业。
      // 保留 jobId 继续定时 poll（容错写法参考 importJob.js:177），
      // 连续失败 N 次才提示；running 不置 false，开始按钮不会被误放开。
      failStreak += 1
      msg.value = failStreak >= MAX_POLL_FAILS
        ? `连接中断，正在重试（连续 ${failStreak} 次）：${e.message}`
        : '网络波动，重连中…'
      poll(id)
    }
  }, 900)
}

// 打开面板时自动带出上次扫描成果（含中途停止的半截），避免"出门回来成果没了"
async function loadLast() {
  if (running.value || words.value.length) return
  try {
    const r = await api.autoVocabLast()
    if (r.ok && r.found && (r.words || []).length) {
      showResult(r, r.stopped || r.partial)
      const t = r.ts ? new Date(r.ts * 1000) : null
      lastNote.value = `上次扫描：${r.method === 'local' ? '本地描述模型' : r.method === 'api' ? '外部大模型' : '词库打分'} · ${Number(r.scanned || 0).toLocaleString('zh-CN')}/${Number(r.total || 0).toLocaleString('zh-CN')} 张`
        + (t ? ` · ${t.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}` : '')
    }
  } catch { /* 无上次成果属正常 */ }
}

onMounted(loadLast)

async function apply() {
  if (applying.value) return
  if (!chosenYes.value.length && !chosenNo.value.length) return
  applying.value = true
  try {
    const r = await api.autoVocabApply(chosenYes.value, chosenNo.value)
    if (!r.ok) throw new Error(r.error || '应用失败')
    emit('applied', r)
  } catch (e) {
    warning.value = '应用失败：' + e.message
  } finally {
    applying.value = false
  }
}

onUnmounted(() => clearTimeout(pollTimer))
</script>

<template>
  <div class="autovocab">
    <p class="hint">重新分析已有图库，由 AI 说出画面中的词汇。扫完即按出现频率自动加入口味库（命中越多的词分量越重），正在跑的任务也会立即按新词表找图；已亲手填过的词不会被覆盖。下方勾选只用于补充微调，比如把某个词标成「不要」。</p>

    <div class="block">
      <div class="block-title"><span class="num">1</span>用什么方式看图说词</div>
      <div class="methods">
        <button v-for="m in methods" :key="m.key" class="method" :class="{ on: method === m.key, off: m.disabled }" :disabled="m.disabled" @click="method = m.key">
          <span class="mhead"><b>{{ m.name }}</b><span class="mtag">{{ m.tag }}</span></span>
          <small>{{ m.desc }}</small>
        </button>
      </div>
    </div>

    <div class="block">
      <div class="block-title"><span class="num">2</span>扫描多少张图</div>
      <div class="amounts">
        <button v-for="a in amounts" :key="a.key" class="chip" :class="{ on: amount === a.key }" @click="amount = a.key">{{ a.name }}</button>
      </div>
      <p class="small muted">按当前机器速度，1000 张约 8 分钟，5000 张约 40 分钟；扫描在后台跑，可以关掉页面。</p>
      <div class="runrow">
        <button class="btn primary" :disabled="running" @click="start">{{ running ? '正在看图…' : '开始让 AI 看图' }}</button>
        <button v-if="running" class="btn danger" @click="stop"><Icon name="pause" :size="13" />停止（成果保留）</button>
      </div>

      <div v-if="running || pct" class="prog">
        <div class="progbar"><i :style="{ width: pct + '%' }"></i></div>
        <p class="small muted">{{ pct }}% · {{ msg }}</p>
      </div>
      <p v-if="lastNote && !running" class="small muted">{{ lastNote }}</p>
      <p v-if="appliedNote && !running" class="small applied-note">{{ appliedNote }}</p>
    </div>

    <div v-if="warning" class="warnbox">{{ warning }}</div>

    <div v-if="words.length" class="block result">
      <div class="res-head">
        <div class="block-title" style="margin:0"><span class="num">3</span>AI 说出的词</div>
        <span class="small muted">已选要 {{ chosenYes.length }} · 不要 {{ chosenNo.length }}</span>
      </div>
      <p class="small muted">这些词已按命中频率自动加入口味库。这里只做补充：点 ✓ 加强某个词，点 ✕ 把它标成「不要」。每个词后面是命中张数。</p>
      <div class="toolrow">
        <button class="btn sm ghost" @click="voteAll(1)">全部先选要</button>
        <button class="btn sm ghost" @click="voteAll(0)">全部清空</button>
      </div>
      <div class="words">
        <div v-for="w in words" :key="w.word" class="word" :class="{ yes: votes[w.word] === 1, no: votes[w.word] === -1 }" @click="vote(w.word, votes[w.word] === 1 ? 0 : 1)">
          <div class="winfo">
            <b>{{ w.word }}</b>
            <small class="muted">命中 {{ w.hits }} 张</small>
          </div>
          <div class="vote">
            <button class="btn xs" :class="votes[w.word] === 1 ? 'primary' : 'ghost'" @click.stop="vote(w.word, 1)">✓</button>
            <button class="btn xs" :class="votes[w.word] === -1 ? 'danger' : 'ghost'" @click.stop="vote(w.word, -1)">✕</button>
          </div>
        </div>
      </div>
      <div class="applybar">
        <span class="small muted">{{ chosenYes.length || chosenNo.length ? `将写入 ${chosenYes.length} 个要、${chosenNo.length} 个不要` : '还没勾选任何词' }}</span>
        <button class="btn primary" :disabled="applying || (!chosenYes.length && !chosenNo.length)" @click="apply">{{ applying ? '写入中…' : '应用到我的口味' }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 组件直接作为页面卡片使用：铺满整行、自带内边距，字号与学习页其它卡片统一（正文 13px）。 */
.autovocab { width: 100%; box-sizing: border-box; padding: 16px; }
.autovocab > .hint:first-child { margin-top: 0; }
.hint { color: var(--muted); font-size: 13px; margin: 0 0 16px; line-height: 1.6; }
.block { border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; margin-bottom: 14px; background: var(--card); }
.block:last-child { margin-bottom: 0; }
.block-title { font-weight: 600; font-size: 14px; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
.num { width: 20px; height: 20px; border-radius: 50%; background: var(--brand); color: #fff; font-size: 12px; display: inline-flex; align-items: center; justify-content: center; flex: none; }
.methods { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.method { text-align: left; border: 2px solid var(--line); border-radius: 10px; padding: 12px 14px; background: var(--card); cursor: pointer; color: var(--text); display: flex; flex-direction: column; gap: 6px; }
.method.on { border-color: var(--brand); background: var(--brand-bg, #f5f3ff); }
.method.off { opacity: .55; cursor: not-allowed; }
.mhead { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.mhead b { font-size: 13.5px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.method small { display: block; font-size: 12px; color: var(--muted); line-height: 1.6; }
.mtag { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--brand-bg, #eef0ff); color: var(--brand); flex: none; white-space: nowrap; }
.amounts { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.chip { padding: 7px 14px; border: 1px solid var(--line); border-radius: 8px; background: var(--card); cursor: pointer; color: var(--text); font-size: 13px; }
.chip.on { background: var(--brand); color: #fff; border-color: var(--brand); }
.prog { margin-top: 12px; }
.runrow { display: flex; gap: 8px; align-items: center; }
.progbar { height: 7px; background: var(--line); border-radius: 6px; overflow: hidden; }
.progbar i { display: block; height: 100%; background: var(--brand); transition: width .3s; }
.applied-note { color: var(--ok, #1d9e75); margin-top: 4px; }
.warnbox { background: var(--warn-bg, #fff8ec); color: var(--warn, #9a6b1f); border-radius: 8px; padding: 10px 12px; font-size: 13px; margin-bottom: 14px; line-height: 1.6; }
.res-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.toolrow { display: flex; gap: 8px; margin: 10px 0; }
.words { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 8px; }
.word { display: flex; align-items: center; justify-content: space-between; gap: 10px; border: 1.5px solid var(--line); border-radius: 10px; padding: 8px 10px; cursor: pointer; }
.word.yes { border-color: var(--ok, #1d9e75); }
.word.no { opacity: .5; }
.winfo { min-width: 0; flex: 1; }
.winfo b { display: block; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.winfo small { font-size: 12px; }
.vote { display: flex; gap: 4px; flex: none; }
.applybar { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
@media (max-width: 700px) { .methods { grid-template-columns: 1fr; } }
</style>
