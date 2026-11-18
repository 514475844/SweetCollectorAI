<script setup>
/**
 * AI 识别独立卡片（V0.0.61：从导入页挪到图库页）。
 *
 * 识别是对**已入库、未识别的图片**做的，跟导入（收新图）是两回事，
 * 所以卡片放在图库页（图片实际所在处），不再挂在导入页。
 * 逻辑全部复用 importJob.js 的模块级单例：识别作业在后台跑，
 * 切页不中断、回来原样接回。
 */
import { computed, onMounted, watch } from 'vue'
import Icon from './Icon.vue'
import { tagImp, startTagging, cancelTagging, refreshTagPending } from '../importJob'

function fmt(n) {
  const v = Number(n) || 0
  return v.toLocaleString('zh-CN')
}

const tagText = computed(() => {
  if (tagImp.pending >= 0 && !tagImp.running && !tagImp.result) {
    return tagImp.pending > 0 ? `还有 ${fmt(tagImp.pending)} 张待识别` : '全部已识别'
  }
  if (!tagImp.total) return '正在统计待识别图片…'
  return `${fmt(tagImp.done)} / ${fmt(tagImp.total)} 张 · ${tagImp.pct}%`
})

const tagPct = computed(() => {
  if (tagImp.total > 0) return Math.min(100, Math.max(0, tagImp.pct))
  return 2
})

watch(() => tagImp.doneSeq, () => { refreshTagPending() })
watch(() => tagImp.running, (v) => { if (!v) refreshTagPending() })

onMounted(refreshTagPending)
</script>

<template>
  <div v-if="tagImp.running || tagImp.result || (tagImp.pending > 0)" class="state tagcard">
    <div class="state-hd">
      <Icon :anim="tagImp.running ? 'spin' : ''"
            :name="tagImp.running ? 'loader-circle' : 'sparkles'" :size="15" />
      <b>AI 识别</b>
      <span class="muted small">{{ tagText }}</span>
      <span v-if="tagImp.eta && tagImp.running" class="muted small">约剩 {{ tagImp.eta }}</span>
    </div>
    <div v-if="tagImp.running" class="bar"><i :style="{ width: tagPct + '%' }" /></div>
    <p v-if="tagImp.err" class="msg err">{{ tagImp.err }}</p>
    <div class="state-ft">
      <button v-if="tagImp.running" class="btn sm danger" @click="cancelTagging">
        <Icon name="pause" :size="13" />停止识别</button>
      <button v-else class="btn sm primary" :disabled="tagImp.starting" @click="startTagging">
        <Icon name="play" :size="13" />{{ tagImp.starting ? '正在启动…' : (tagImp.pending > 0 ? `识别这 ${fmt(tagImp.pending)} 张` : '启动识别') }}</button>
      <span class="muted small">识别可以随时启停，不影响导入。</span>
    </div>
  </div>
</template>
