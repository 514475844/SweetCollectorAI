<script setup>
// V0.0.75 · 新手引导重写成 4 步流程（用户拍板的顺序）：
//   第 1 步：有没有本地图库？有 → 让 AI 先学偏好（直达学习页并自动展开
//           「让 AI 认识我的图库」）；没有 → 跳过，采集时现学。
//   第 2 步：填一个要采集的网站。
//   第 3 步：白名单（主题词）/ 黑名单（排除词），可跳过。
//   第 4 步：确认并「保存并运行任务」—— 真建任务、起跑、跳任务页。
// 是否首次访问存 localStorage；设置页通过 mrm-onboarding 事件重新运行。
// 旧版（V0.0.17 三步说明 + V0.0.55 收件口建任务）的行为合并进第 4 步。
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import Icon from './Icon.vue'
import { api } from '../api'

const router = useRouter()
const DONE_KEY = 'mrm.onboarding.done'
const STEPS = [
  {
    title: '第 1 步 / 共 4 步 · 让程序先学习已有图库',
    lines: [
      '是否已经收藏有本地图片库？',
      '若有，可让程序先分析已有图库，学习其中的偏好特征。此后采集的图片将按该偏好筛选，结果更贴合需要。',
      '若没有，可跳过本步骤，在采集过程中逐步学习。',
    ],
  },
  {
    title: '第 2 步 / 共 4 步 · 填写采集网站',
    lines: [
      '填写一个图册或相册网址，程序将自动匹配适配器。',
      '未匹配的站点走 gallery-dl 通用模式，支持大批网站。',
    ],
  },
  {
    title: '第 3 步 / 共 4 步 · 设置白名单与黑名单',
    lines: [
      '白名单（主题词）：仅保留画面命中这些词的图片，多个词以空格分隔。',
      '黑名单（排除词）：命中这些词的图片不予收录。',
      '两项均可留空。留空时全部图片收录至图库，后续可再行筛选。',
    ],
  },
  {
    title: '第 4 步 / 共 4 步 · 保存并运行任务',
    lines: [
      '确认以下信息，单击「保存并运行任务」立即开始采集。',
      '也可跳过，之后随时在任务页新建任务。',
    ],
  },
]
const visible = ref(false)
const step = ref(0)
const cur = computed(() => STEPS[step.value])
const url = ref('')
const want = ref('')
const avoid = ref('')
const saving = ref(false)
const msg = ref('')

// 第 1 步：直达学习页并自动展开「让 AI 认识我的图库」
function goLearn() {
  close()
  router.push('/learning?vocab=1')
}
// 第 2 步的网址先粗校验，别把空串带进第 4 步
const urlOk = computed(() => /^https?:\/\/\S+\.\S+/i.test(url.value.trim()))

async function saveAndRun() {
  const u = url.value.trim()
  if (!urlOk.value) { msg.value = '请先填写有效的网址（可返回第 2 步修改）'; return }
  saving.value = true
  msg.value = ''
  try {
    const host = u.replace(/^https?:\/\//i, '').split('/')[0]
    const body = {
      name: ('采 ' + host).slice(0, 60),
      entry_urls: [u],
      ai_enabled: true,
      // 主题词一层（白名单）；排除词空格分多个
      ai_label_levels: want.value.trim() ? [want.value.trim()] : [],
      ai_label_strict_levels: [],
      ai_blacklist: avoid.value.trim(),
    }
    const r = await api.taskSave(body)
    if (!r || r.ok === false) { msg.value = (r && r.error) || '保存失败'; return }
    try { await api.taskCtl('start', r.task_id) } catch { /* 起跑失败任务仍已保存 */ }
    close()
    router.push('/tasks')   // 任务页立即能看到这条新建并在跑的任务
  } catch (e) { msg.value = e.message || '保存失败' }
  finally { saving.value = false }
}

function markDone() {
  try { localStorage.setItem(DONE_KEY, '1') } catch { /* 隐私模式 */ }
}
function open() {
  step.value = 0
  url.value = ''
  want.value = ''
  avoid.value = ''
  msg.value = ''
  saving.value = false
  visible.value = true
  markDone()
}
function close() {
  visible.value = false
  markDone()
}
function next() {
  msg.value = ''
  if (step.value < STEPS.length - 1) step.value += 1
  else close()
}
function prev() {
  msg.value = ''
  if (step.value > 0) step.value -= 1
}
function onKey(e) {
  if (!visible.value) return
  if (e.key === 'Escape') close()
}
onMounted(() => {
  window.addEventListener('mrm-onboarding', open)
  window.addEventListener('keydown', onKey)
  try {
    if (localStorage.getItem(DONE_KEY) !== '1') open()
  } catch { open() }
})
onUnmounted(() => {
  window.removeEventListener('mrm-onboarding', open)
  window.removeEventListener('keydown', onKey)
})
</script>

<template>
<!-- 挂到 body，避免 fixed 遮罩被 .page/过渡的 transform 困住盖不住顶栏标题 -->
    <Teleport to="body">
  <div v-if="visible" class="mask">
    <div class="wiz card">
      <div class="head">
        <h3><Icon name="sparkles" :size="17" />新手引导</h3>
        <span class="muted small">四步跑通第一次采集</span>
      </div>
      <div class="dots">
        <span v-for="(s, i) in STEPS" :key="i" class="dot" :class="{ on: i === step }"></span>
      </div>
      <div class="body">
        <h4>{{ cur.title }}</h4>
        <p v-for="(ln, i) in cur.lines" :key="i" class="line">{{ ln }}</p>

        <!-- 第 1 步：有图库 → 直达 AI 认识图库 -->
        <div v-if="step === 0" class="first">
          <button class="btn sm primary" @click="goLearn">
            <Icon name="sparkles" :size="13" />有，让 AI 学我的图库</button>
          <button class="btn sm" @click="next">没有，继续</button>
        </div>

        <!-- 第 2 步：网址 -->
        <div v-if="step === 1" class="first">
          <input v-model="url" type="text" placeholder="粘贴一个图册 / 相册网址…" />
        </div>

        <!-- 第 3 步：白名单 / 黑名单 -->
        <div v-if="step === 2" class="terms">
          <label>白名单（主题词，空格分开，可留空）</label>
          <input v-model="want" type="text" placeholder="例如：白丝 制服" />
          <label>黑名单（排除词，空格分开，可留空）</label>
          <input v-model="avoid" type="text" placeholder="例如：真人 直播" />
        </div>

        <!-- 第 4 步：确认摘要 + 真建任务 -->
        <div v-if="step === 3" class="summary">
          <p><b>网站</b>{{ url || '（未填）' }}</p>
          <p><b>白名单</b>{{ want || '（不设）' }}</p>
          <p><b>黑名单</b>{{ avoid || '（不设）' }}</p>
          <button class="btn sm primary" :disabled="saving" @click="saveAndRun">
            <Icon :anim="saving ? 'spin' : ''" :name="saving ? 'loader-circle' : 'download'" :size="13" />
            {{ saving ? '创建中…' : '保存并运行任务' }}</button>
        </div>
        <p v-if="msg" class="tiny err-line">{{ msg }}</p>
      </div>
      <p class="muted tiny">跳过也可以：之后随时在任务页新建任务、在学习页教 AI。</p>
      <div class="acts">
        <button class="btn sm ghost" @click="close">跳过引导</button>
        <span class="spacer"></span>
        <button class="btn sm" :disabled="step === 0" @click="prev">
          <Icon name="chevron-left" :size="13" />上一步</button>
        <button class="btn primary sm" :disabled="saving" @click="next">
          <Icon v-if="step < STEPS.length - 1" name="chevron-right" :size="13" />
          <Icon v-else name="check" :size="13" />
          {{ step < STEPS.length - 1 ? '下一步' : '完成' }}</button>
      </div>
    </div>
  </div>
  </Teleport>
</template>

<style scoped>
.mask { position: fixed; inset: 0; z-index: 90; background: rgba(16, 24, 40, .58);
        display: grid; place-items: center; padding: 18px; }
.wiz { width: min(560px, 94vw); padding: 22px 24px; display: grid; gap: 12px; }
.head { display: flex; align-items: center; justify-content: space-between; }
.head h3 { margin: 0; font-size: 18px; display: inline-flex; align-items: center; gap: 8px; }
.dots { display: flex; gap: 6px; }
.dot { width: 26px; height: 4px; border-radius: 999px; background: var(--line, rgba(0,0,0,.12)); }
.dot.on { background: var(--brand, #6d4bd6); }
.body { min-height: 132px; padding: 6px 2px; }
.body h4 { margin: 0 0 12px; font-size: 15px; }
.line { margin: 0 0 8px; font-size: 14px; line-height: 1.7; }
.first { display: flex; gap: 8px; margin-top: 6px; }
.first input { flex: 1; }
.terms { display: grid; gap: 6px; margin-top: 4px; }
.terms label { font-size: 12px; color: var(--muted); }
.summary { display: grid; gap: 6px; margin-top: 4px; }
.summary p { margin: 0; font-size: 13px; word-break: break-all; }
.summary b { display: inline-block; width: 56px; color: var(--muted); font-weight: 600; }
.summary .btn { justify-self: start; margin-top: 6px; }
.err-line { color: var(--danger, #c0392b); margin: 6px 0 0; }
.tiny { font-size: 12px; }
.acts { display: flex; align-items: center; gap: 8px; padding-top: 8px;
        border-top: 1px solid var(--line, rgba(0,0,0,.08)); }
.spacer { flex: 1; }
</style>
