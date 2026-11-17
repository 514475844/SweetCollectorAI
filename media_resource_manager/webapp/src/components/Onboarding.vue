<script setup>
// V0.0.17 · 首次访问自动弹出的 3 步新手引导（步骤文案对齐 gui/onboarding.py）。
// 是否首次访问存 localStorage；设置页可通过 mrm-onboarding 事件重新运行。
// 网页端引导为说明型遮罩，不创建任务；采集请在任务页进行。
import { ref, computed, onMounted, onUnmounted } from 'vue'
import Icon from './Icon.vue'

const DONE_KEY = 'mrm.onboarding.done'
const STEPS = [
  {
    title: '第 1 步 / 共 3 步 · 准备网址',
    lines: [
      '粘贴一个图册或相册网址，软件将自动匹配适配器。',
      '未匹配到时使用 gallery-dl 自动模式，支持大批站点。',
    ],
  },
  {
    title: '第 2 步 / 共 3 步 · 设置 AI 与密码',
    lines: [
      '设置 AI（可跳过）与 Web 访问密码。',
      'AI 未配置时采集照常进行，仅不提供自动打标与过滤。',
    ],
  },
  {
    title: '第 3 步 / 共 3 步 · 创建第一个任务',
    lines: [
      '创建第一个采集任务（可跳过，之后在任务页随时新建）。',
      '引导结束后，可在设置页重新运行本说明。',
    ],
  },
]
const visible = ref(false)
const step = ref(0)
const cur = computed(() => STEPS[step.value])

function markDone() {
  try { localStorage.setItem(DONE_KEY, '1') } catch { /* 隐私模式 */ }
}
function open() {
  step.value = 0
  visible.value = true
  markDone()
}
function close() {
  visible.value = false
  markDone()
}
function next() {
  if (step.value < STEPS.length - 1) step.value += 1
  else close()
}
function prev() {
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
  <div v-if="visible" class="mask">
    <div class="wiz card">
      <div class="head">
        <h3><Icon name="sparkles" :size="17" />新手引导</h3>
        <span class="muted small">首次访问自动显示</span>
      </div>
      <div class="dots">
        <span v-for="(s, i) in STEPS" :key="i" class="dot" :class="{ on: i === step }"></span>
      </div>
      <div class="body">
        <h4>{{ cur.title }}</h4>
        <p v-for="(ln, i) in cur.lines" :key="i" class="line">{{ ln }}</p>
      </div>
      <p class="muted tiny">单击「下一步」继续；也可跳过引导，之后在任务页开始采集。</p>
      <div class="acts">
        <button class="btn sm ghost" @click="close">跳过引导</button>
        <span class="spacer"></span>
        <button class="btn sm" :disabled="step === 0" @click="prev">
          <Icon name="chevron-left" :size="13" />上一步</button>
        <button class="btn primary sm" @click="next">
          <Icon v-if="step < STEPS.length - 1" name="chevron-right" :size="13" />
          <Icon v-else name="check" :size="13" />
          {{ step < STEPS.length - 1 ? '下一步' : '完成' }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.mask { position: fixed; inset: 0; z-index: 90; background: rgba(16, 24, 40, .58);
        display: grid; place-items: center; padding: 18px; }
.wiz { width: min(520px, 94vw); padding: 22px 24px; display: grid; gap: 12px; }
.head { display: flex; align-items: center; justify-content: space-between; }
.head h3 { margin: 0; font-size: 18px; display: inline-flex; align-items: center; gap: 8px; }
.dots { display: flex; gap: 6px; }
.dot { width: 26px; height: 4px; border-radius: 999px; background: var(--line, rgba(0,0,0,.12)); }
.dot.on { background: var(--brand, #6d4bd6); }
.body { min-height: 116px; padding: 6px 2px; }
.body h4 { margin: 0 0 12px; font-size: 15px; }
.line { margin: 0 0 8px; font-size: 14px; line-height: 1.7; }
.tiny { font-size: 12px; }
.acts { display: flex; align-items: center; gap: 8px; padding-top: 8px;
        border-top: 1px solid var(--line, rgba(0,0,0,.08)); }
.spacer { flex: 1; }
</style>
