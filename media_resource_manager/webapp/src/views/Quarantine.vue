<script setup>
// 隔离区（2026-10-28 从「图库」页签里拆出来，成为左边独立一项）。
// 语义：AI 判定"不像主题词"或一二级都没命中的图，先搬到这里不删，
// 人看一眼再决定 —— 这是 r29 定下的铁律「宁可漏放不可误杀」的落地处。
import { ref } from 'vue'
import { api } from '../api'
import ImageGrid from '../components/ImageGrid.vue'
import Icon from '../components/Icon.vue'

// V0.0.16：网页补齐桌面「隔离区 → 清空隔离区」。物理删除不可撤销，
// 因此前端确认 + 后端 confirm 双重校验，且明确说明与系统回收站无关。
const busy = ref(false)
const err = ref('')
const info = ref('')
const keepDays = ref(30)   // V0.0.56：按保留期清理的默认天数

async function pruneByDays() {
  const d = Math.max(1, Math.round(Number(keepDays.value) || 30))
  const ok = confirm(
    `即将物理删除隔离超过 ${d} 天、且未收藏未拉黑未待审的图片文件，操作不可撤销。\n\n` +
    '此处清理的是软件内隔离区，与 Windows 系统回收站无关。请确认继续。')
  if (!ok) return
  busy.value = true
  try {
    const r = await api.siteQuarantinePrune(d)
    if (!r.ok) throw new Error(r.error || '清理未完成')
    info.value = r.note || '隔离区已按保留期清理'
    err.value = ''
  } catch (e) {
    err.value = e.message || '按保留期清理失败'
  } finally {
    busy.value = false
  }
}

async function pruneAll() {
  const ok = confirm(
    '即将物理删除隔离区内未收藏、未拉黑、未待审的全部图片文件，操作不可撤销。\n\n' +
    '已收藏、已拉黑与待审核的图片不会受到影响。' +
    '此处清理的是软件内隔离区，与 Windows 系统回收站无关。请确认继续。')
  if (!ok) return
  busy.value = true
  try {
    const r = await api.siteQuarantinePrune()
    if (!r.ok) throw new Error(r.error || '清空未完成')
    info.value = r.note || '隔离区已清空'
    err.value = ''
  } catch (e) {
    err.value = e.message || '清空隔离区失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="page">
    <p class="hint">
      <Icon name="shield-alert" :size="15" />
      <span>
        这里是被 AI 判为「不像主题词」的图，<b>没有被删除</b>，文件还在隔离区目录里。
        判定有误时，选中后选择「喜欢 / 还原」将其移回图库；确认无需保留后再执行删除。
      </span>
    </p>
    <div class="card bar">
      <span class="muted small">隔离区不会自动膨胀。可只清超过保留期的旧图，或一次性清空。</span>
      <div class="inline">
        <label class="muted small">保留</label>
        <input type="number" min="1" max="3650" v-model.number="keepDays"
               style="width:70px" :disabled="busy" />
        <span class="muted small">天</span>
        <button class="btn sm" :disabled="busy" @click="pruneByDays">
          <Icon name="clock" :size="13" />清理超期图
        </button>
        <button class="btn sm danger" :disabled="busy" @click="pruneAll">
          <Icon name="trash" :size="13" />全部清空
        </button>
      </div>
    </div>
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="info" class="goodbar">{{ info }}</p>
    <ImageGrid view="quarantine" />
  </div>
</template>

<style scoped>
/* V0.0.27：块与块之间统一 14px 缝，hint/条子上自己的 margin-bottom 退场 */
.page { display: grid; gap: 14px; align-items: start; }
.hint {
  margin: 0; font-size: 13px; display: flex; gap: 7px;
  align-items: flex-start; color: var(--muted);
}
.hint .icon { margin-top: 2px; color: var(--warn); }
.bar { display: flex; align-items: center; justify-content: space-between;
       gap: 10px; flex-wrap: wrap; padding: 10px 14px; }
.btn.sm { padding: 5px 12px; font-size: 12px; border-radius: 8px; }
.err { color: var(--err, #b91c1c); margin: 0; }
.goodbar { color: var(--brand, #6d4bd6); margin: 0; font-size: 13px; }
</style>
