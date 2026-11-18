<script setup>
// 学习看板：数据全走后端 /api/learning（与桌面端 learning_board 同一份口径：
// 统计来自 classify.learning_stats.collect，权重来自 repo.get_preferences）。
// 老界面（/classic）有「学习看板」页，新版只把它塞进设置里的三个按钮 —— 这里补成独立页。
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import { useShellRefresh } from '../shellVue'
import Icon from '../components/Icon.vue'

const d = ref(null)
const loading = ref(false)
const err = ref('')
const info = ref('')
const kw = ref('')
const only = ref('all')          // all / pos / neg
const busy = ref('')
// V0.0.7：被判掉过的图教出来的「以后别找」词
const fb = ref([])
const fbHard = ref(2)
const fbShadow = ref(true)
const fbBusy = ref(false)

// 统计卡的字段与中文名（后端 collect 的键，顺序即展示顺序）
const STAT_LABELS = [
  ['labels_total', '权重条数'],
  ['pos_count', '喜欢方向'],
  ['neg_count', '排除方向'],
  ['neu_count', '零权重'],
  ['strong_count', '强权重'],
  ['hit_pos_total', '主题词命中'],
  ['hit_neg_total', '排除词命中'],
  ['seen_total', '表态次数'],
  ['reviews_total', '审核次数'],
]

const cards = computed(() => {
  const st = d.value?.stats || {}
  return STAT_LABELS
    .filter(([k]) => st[k] !== undefined)
    .map(([k, label]) => ({ label, value: st[k] }))
})

const samples = computed(() => {
  let arr = (d.value?.samples || []).slice()
  const k = kw.value.trim().toLowerCase()
  if (k) {
    arr = arr.filter((s) => (s.text || '').toLowerCase().includes(k)
      || (s.label || '').toLowerCase().includes(k))
  }
  if (only.value === 'pos') arr = arr.filter((s) => s.sign === 'pos')
  if (only.value === 'neg') arr = arr.filter((s) => s.sign === 'neg')
  return arr
})

function toast(msg, bad) {
  if (bad) { err.value = msg; info.value = '' } else { info.value = msg; err.value = '' }
  setTimeout(() => { err.value = ''; info.value = '' }, 3200)
}

async function load() {
  loading.value = true
  try {
    d.value = await api.learning()
    err.value = ''
  } catch (e) {
    err.value = e.message
  } finally {
    loading.value = false
  }
}

async function toggleEnabled(on) {
  busy.value = 'enabled'
  try {
    await api.learnEnabled(on)
    if (d.value) d.value.enabled = on
    toast(on ? '已开启偏好学习' : '偏好学习已关闭（权重保留，仅停止后续学习）')
  } catch (e) {
    toast(e.message, true)
    await load()
  } finally {
    busy.value = ''
  }
}

async function delOne(s) {
  if (!window.confirm(`确认删除该学习记录？\n「${s.text || s.label}」权重 ${s.weight}`)) return
  busy.value = s.label
  try {
    await api.learnDelete(s.label)
    await load()
    toast('已删除')
  } catch (e) {
    toast(e.message, true)
  } finally {
    busy.value = ''
  }
}

async function resetAll() {
  // 后端还有一道 confirm 校验；这里只是给用户一个反悔机会
  if (!window.confirm('重置将清空全部全局学习权重（不可撤销）。\n确认继续？')) return
  busy.value = 'reset'
  try {
    await api.learnReset()
    await load()
    toast('学习样本已重置')
  } catch (e) {
    toast(e.message, true)
  } finally {
    busy.value = ''
  }
}

async function clearAuto() {
  if (!window.confirm('仅清除模型自动学习的条目，保留手工添加的条目？')) return
  busy.value = 'auto'
  try {
    await api.learnClearModelAuto()
    await load()
    toast('模型自动学习的条目已清除')
  } catch (e) {
    toast(e.message, true)
  } finally {
    busy.value = ''
  }
}

async function loadFb() {
  try {
    const r = await api.feedback()
    fb.value = r.items || []
    fbHard.value = r.hard_hits || 2
    fbShadow.value = r.shadow !== false
  } catch (e) {
    // 后端没有这个接口时不该让整页打不开，安静略过即可
    fb.value = []
  }
}

// V0.0.7：词表面板 —— 桌面看板有「现在正按这些词去找图 + 手动加词」，
// 网页端以前只有权重明细，手机上看不见软件到底按什么词采集、也没法加词。
const terms = ref(null)
const newTerm = ref('')
const termBusy = ref(false)
async function loadTerms() {
  try { terms.value = await api.findTerms() } catch (e) { terms.value = null }
}
async function addTerm() {
  const w = newTerm.value.trim()
  if (!w || termBusy.value) return
  termBusy.value = true
  try {
    const r = await api.learnAddTerm(w)
    if (!r.ok) throw new Error(r.error || '加词失败')
    newTerm.value = ''
    if (r.want) terms.value = { ...(terms.value || {}), want: r.want, learned: r.learned, avoid: r.avoid, soft_avoid: r.soft_avoid }
    await loadTerms()
    await load()
    toast(r.added ? '已加入目标词' : '该词已在词表中')
  } catch (e) {
    toast(e.message, true)
  } finally {
    termBusy.value = false
  }
}
async function delFb(x) {
  fbBusy.value = true
  try {
    await api.feedbackRemove(x.word)
    await loadFb()
    await loadTerms()
    toast('已删除该词')
  } catch (e) {
    toast(e.message, true)
  } finally {
    fbBusy.value = false
  }
}

async function clearFb() {
  if (!window.confirm('清空后全部回避词立即失效，无法恢复。确认继续？')) return
  fbBusy.value = true
  try {
    await api.feedbackClear()
    await loadFb()
    await loadTerms()
    toast('已清空')
  } catch (e) {
    toast(e.message, true)
  } finally {
    fbBusy.value = false
  }
}

onMounted(() => { load(); loadFb(); loadTerms() })
useShellRefresh(() => { load(); loadFb(); loadTerms() })
</script>

<template>
  <div class="page">
    <div class="card bar">
      <div class="left">
        <label class="switch">
          <input type="checkbox" :checked="!!d?.enabled" :disabled="busy === 'enabled'"
                 @change="toggleEnabled($event.target.checked)" />
          <span>启用偏好学习</span>
        </label>
        <span class="muted small">关闭后仅停止后续学习，已有权重仍然生效</span>
      </div>
      <div class="ops">
        <button class="btn sm" :disabled="loading" @click="load"><Icon :anim="loading ? 'spin' : ''" name="refresh-cw" :size="13" />刷新</button>
        <button class="btn sm ghost" :disabled="busy === 'auto'" @click="clearAuto"><Icon name="eraser" :size="13" />清除模型学习项</button>
        <button class="btn sm danger" :disabled="busy === 'reset'" @click="resetAll"><Icon name="rotate-cw" :size="13" />全部重置</button>
      </div>
    </div>

    <p v-if="err" class="err">{{ err }}</p>
    <p v-else-if="info" class="ok">{{ info }}</p>

    <div v-if="d" class="card body">
      <h3>{{ d.summary }}</h3>
      <p class="muted small" v-if="d.origin_line">{{ d.origin_line }}</p>
      <p class="muted small" v-if="d.sample_line">{{ d.sample_line }}</p>

      <div class="stats">
        <div class="st" v-for="c in cards" :key="c.label">
          <b>{{ c.value }}</b><span class="muted small">{{ c.label }}</span>
        </div>
      </div>
    </div>

    <div v-if="terms" class="card body">
      <div class="thead">
        <h3>当前采集所用目标词与排除词<span class="muted small">（下载前按此筛选）</span></h3>
      </div>
      <p class="small"><b>目标词：</b>{{ (terms.want || []).length ? terms.want.join('、') : '暂无（导入原有图库或手工添加后出现）' }}</p>
      <p v-if="(terms.learned || []).length" class="small muted">其中学到的：{{ terms.learned.join('、') }}</p>
      <p class="small"><b>排除词：</b>{{ (terms.avoid || []).length ? terms.avoid.join('、') : '暂无' }}</p>
      <p v-if="(terms.soft_avoid || []).length" class="small muted">
        观察期（判退 1 次，仅进入待审核）：{{ terms.soft_avoid.join('、') }}
      </p>
      <div class="addrow">
        <input v-model="newTerm" type="text" placeholder="手工添加目标词"
               :disabled="termBusy" @keydown.enter="addTerm" />
        <button class="btn sm" :disabled="termBusy || !newTerm.trim()" @click="addTerm">加词</button>
      </div>
      <p class="muted small">删除某个词：在下方「权重明细」里删该词的权重。</p>
    </div>

    <div class="card body">
      <div class="thead">
        <h3>判退图片学到的回避词<span class="muted small">（{{ fb.length }} 条）</span></h3>
        <div class="ops">
          <button class="btn sm" :disabled="fbBusy" @click="loadFb"><Icon name="refresh-cw" :size="13" />刷新</button>
          <button class="btn sm danger" :disabled="fbBusy || !fb.length" @click="clearFb">全部清空</button>
        </div>
      </div>
      <p class="muted small">
        判退 1 次：同类图片进入待审核复核；累计判退 {{ fbHard }} 次：下载前直接拦截。
        误判可逐条删除；手工填写的主题词不会被自动屏蔽。
      </p>
      <p class="small" :class="fbShadow ? 'ok' : 'muted'">
        {{ fbShadow
          ? '当前为「影子模式」：学到的回避词只影响排序与送审核，不会自动拦截下载。需自动拦截请到「设置 → AI 与主题词」关闭影子模式。'
          : '当前为「自动拦截」：判退累计达标后会在下载前直接拦截。' }}
      </p>
      <div class="tw">
        <div class="tr fbrow fbth"><span>词</span><span>判退次数</span><span>当前处置</span><span></span></div>
        <div v-for="x in fb" :key="x.word" class="tr fbrow">
          <span class="w" :title="x.word">{{ x.word }}</span>
          <span class="num">{{ x.hits }}</span>
          <span class="small" :class="x.hard ? 'neg' : 'muted'">{{ x.hard ? '下载前拦截' : '待审核复核' }}</span>
          <button class="btn sm ghost" :disabled="fbBusy" @click="delFb(x)"><Icon name="trash" :size="13" />删除</button>
        </div>
        <div v-if="!fb.length" class="empty">
          暂无记录。对误采图片执行「不喜欢 / 拉黑」后自动累积。
        </div>
      </div>
    </div>

    <div class="card body">
      <div class="thead">
        <h3>权重明细<span class="muted small">（{{ samples.length }} 条）</span></h3>
        <div class="filters">
          <select v-model="only">
            <option value="all">全部方向</option>
            <option value="pos">仅看喜欢</option>
            <option value="neg">仅看排除</option>
          </select>
          <input v-model="kw" type="text" placeholder="搜词…" />
        </div>
      </div>

      <div class="tw">
        <div class="tr th row">
          <span>词</span><span>权重</span><span>表态</span><span>命中 +/-</span><span>来源</span><span></span>
        </div>
        <div v-for="s in samples" :key="s.label" class="tr row">
          <span class="w" :title="s.label">{{ s.text || s.label }}</span>
          <span class="mono" :class="s.sign">{{ s.weight }}</span>
          <span class="num">{{ s.seen }}</span>
          <span class="num">{{ s.hit_pos }} / {{ s.hit_neg }}</span>
          <span class="small muted">{{ s.source === 'user_typed' ? '手工添加' : '模型自动' }}</span>
          <button class="btn sm ghost" :disabled="busy === s.label" @click="delOne(s)"><Icon name="trash" :size="13" />删除</button>
        </div>
        <div v-if="!samples.length" class="empty">
          {{ (d?.samples || []).length ? '未找到匹配的词' : '暂无学习记录。在「审核」页进行若干次判定后生成。' }}
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.page { display: grid; gap: 14px; }
.bar { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px; gap: 10px; flex-wrap: wrap; }
.left { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.ops { display: flex; gap: 6px; flex-wrap: wrap; }
.switch { display: inline-flex; align-items: center; gap: 6px; }
.body { padding: 14px 16px; }
h3 { margin: 0 0 6px; font-size: 15px; }
.stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(108px, 1fr)); gap: 10px; margin-top: 12px; }
.st { display: flex; flex-direction: column; gap: 2px; padding: 10px; border-radius: 8px; background: var(--panel-2, rgba(0,0,0,.03)); }
.st b { font-size: 18px; }
.thead { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
.fbrow { display: grid; grid-template-columns: 1fr 88px 100px 56px; gap: 8px; align-items: center;
         padding: 8px 4px; border-bottom: 1px solid var(--line, rgba(0,0,0,.06)); }
.fbth { font-size: 12px; color: var(--muted, #888); font-weight: 600; }
@media (max-width: 720px) {
  .fbrow { grid-template-columns: 1fr 64px 84px 48px; }
}
.filters { display: flex; gap: 8px; }
.filters input { width: 140px; }
.addrow { display: flex; gap: 8px; margin: 8px 0 4px; }
.addrow input { flex: 1; }
.small { font-size: 13px; }
p.small { margin: 3px 0; }
.tw { border-top: 1px solid var(--line, rgba(0,0,0,.08)); }
.row { display: grid; grid-template-columns: 1fr 76px 60px 92px 88px 64px; gap: 8px; align-items: center;
       padding: 8px 4px; border-bottom: 1px solid var(--line, rgba(0,0,0,.06)); }
.th { font-size: 12px; color: var(--muted, #888); font-weight: 600; }
.w { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.pos { color: #15803d; }
.neg { color: #b91c1c; }
.empty { padding: 22px 10px; text-align: center; color: var(--muted, #888); }
.btn.danger { color: #b91c1c; border-color: #b91c1c; }
.err { color: #b91c1c; margin: 0; }
.ok { color: #15803d; margin: 0; }
@media (max-width: 720px) {
  /* 手机上只藏「来源」一列 —— **删除按钮必须留着**，
     否则手机上删不掉学错的词，只能回去用电脑。 */
  .row { grid-template-columns: 1fr 64px 46px 84px 56px; }
  .row span:nth-child(5) { display: none; }
}
</style>
