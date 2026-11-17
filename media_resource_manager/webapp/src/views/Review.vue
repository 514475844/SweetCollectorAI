<script setup>
// 审核页 = 待审核视图 + 审核语义（通过 / 不喜欢并给原因）。
// V0.0.12：补桌面已有的三个能力——撤销上一步 / 随机抽检 / 智能抽样。
//   撤销栈在软件内存里常驻（server 挂一份 ReviewQueue），回滚逻辑与
//   桌面 Ctrl+Z 同一个函数；抽检/智抽复用桌面同款取样方法。
import { ref, onMounted } from 'vue'
import ImageGrid from '../components/ImageGrid.vue'
import { useShellRefresh } from '../shellVue'
import Icon from '../components/Icon.vue'
import { api } from '../api'

const gridKey = ref(0)
const busy = ref('')
const msg = ref('')
const msgKind = ref('info')          // info | err
const undoLabel = ref('')
const canUndo = ref(false)

function flash(text, kind = 'info') {
  msg.value = text
  msgKind.value = kind
  setTimeout(() => { if (msg.value === text) msg.value = '' }, 4000)
}
function refreshGrid() { gridKey.value += 1 }
async function refreshUndo() {
  try {
    const r = await api.reviewUndoInfo()
    canUndo.value = !!r.can_undo
    undoLabel.value = r.label || ''
  } catch (e) { /* 老后端没这接口：按钮保持灰 */ }
}
async function doUndo() {
  busy.value = 'undo'
  try {
    const r = await api.reviewUndo()
    if (r.ok) { flash(r.message || '已撤销'); refreshGrid(); await refreshUndo() }
    else flash(r.error || '撤销失败', 'err')
  } catch (e) { flash('撤销失败：' + e.message, 'err') }
  finally { busy.value = '' }
}
async function doSample(kind) {
  busy.value = kind
  try {
    const r = kind === 'sample' ? await api.reviewSample(12)
                               : await api.reviewSmart(20)
    if (r.ok) {
      flash(r.picked ? `抽中 ${r.picked} 张，已放进待审核` : '没有可抽的图（隔离区里都是已审过的）')
      refreshGrid()
    } else flash(r.error || '抽检失败', 'err')
  } catch (e) { flash('抽检失败：' + e.message, 'err') }
  finally { busy.value = '' }
}
onMounted(refreshUndo)
useShellRefresh(() => { refreshGrid(); refreshUndo() })
</script>

<template>
  <div class="page">
    <div class="bar">
      <button class="btn sm" :disabled="!canUndo || busy === 'undo'"
              :title="undoLabel || '没有可撤销的操作'" @click="doUndo">
        <Icon name="rotate-cw" :size="13" />撤销上一步
      </button>
      <button class="btn sm ghost" :disabled="!!busy"
              title="随机抽取 12 张「AI 自动隔离且尚未人工复核」的图片送入待审核，避免阈值或排除词误判"
              @click="doSample('sample')">
        <Icon name="filter" :size="13" />随机抽检
      </button>
      <button class="btn sm ghost" :disabled="!!busy"
              title="优先抽取判定分数最接近阈值的 20 张送入待审核，标注若干张即可校准判定边界"
              @click="doSample('smart')">
        <Icon name="scan-search" :size="13" />智能抽样
      </button>
      <span v-if="msg" class="msg" :class="msgKind">{{ msg }}</span>
    </div>
    <p class="hint muted">
      每张卡片下方标注<b>送审原因</b>（识别置信度不足 / 命中排除词 / 文件体积超限 / 手工移入…）。
      勾选后选择「通过 / 不喜欢」；打开大图可逐张处理。
      选择「不喜欢」时需说明原因——<b>未匹配主题词</b>仅作为负向权重，<b>加入排除词</b>才会写入任务黑名单；
      判退后软件学习到的排除词将即时提示，可在「学习」页查看与撤销。
    </p>
    <ImageGrid :key="gridKey" view="pending" review-mode />
  </div>
</template>

<style scoped>
.page { display: grid; gap: 12px; }
.bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.msg { font-size: 13px; color: var(--ok, #2f9e44); }
.msg.err { color: var(--err, #e03131); }
.hint { margin: 0; font-size: 13px; }
</style>
