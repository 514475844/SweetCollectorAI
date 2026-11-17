<script setup>
/**
 * V0.0.14 · 导出与素材交付
 *
 * 产品的最后一环：采集到的图片最终要交给 AI 学习并生成更多图片。
 * 所以这一页负责三件事，顺序就是实际使用顺序：
 *   1. 从用户已做过的判定里反推一条合格线，先把素材挑干净；
 *   2. 需要留档或换机时，导出加密的报告 / 图库索引；
 *   3. 把合格图复制成一份带同名标注的素材集，可直接用于训练。
 *
 * 训练素材集只复制、不移动、不改名，原图库全程只读。
 */
import { ref, reactive, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'
import { onShellSelectAll, onShellRefresh } from '../nativeShell'
let unbindShell = []
import Icon from '../components/Icon.vue'
import EmptyState from '../components/EmptyState.vue'
import ImportFolder from '../components/ImportFolder.vue'
import FolderPicker from '../components/FolderPicker.vue'

const tune = ref(null)
// V0.0.32：导入/导出拆成两个标签页（用户要求，和设置页一样）。
// 默认停在导出——这一页的主体功能；?tab=imp 直达导入。
const TABS = [
  { k: 'imp', label: '导入', icon: 'folder-open' },
  { k: 'exp', label: '导出', icon: 'layers' },
]
const route = useRoute()
const tab = ref(route.query && route.query.tab === 'imp' ? 'imp' : 'exp')
// 从别处带 ?tab= 跳转时同步（组件复用只 onMounted 读一次会停在旧页）
watch(() => (route.query && route.query.tab) || '', (v) => {
  if (v === 'imp' || v === 'exp') tab.value = v
})
const tuneBusy = ref(false)
const applyTasks = ref(false)
const err = ref('')
const info = ref('')
const busy = ref('')

const pickerOpen = ref(false)
const kit = reactive({
  dest: '', min_score: '', only_favorite: false,
  task_id: '', tag: '', limit: 500,
  group_by: 'task', extra_terms: '', caption_template: '',
})
const kitResult = ref(null)

// ── V0.0.18 · 两步工作流：先预览、勾选，再导出 ──
// 预览不落盘，把候选图的缩略图 / 文件名 / 分数 / 任务 / 预演标注 / 磁盘状态列清楚；
// 勾选后的 ids 与标注模板回传 /api/export/training-kit，做到所见即所得。
const preview = ref(null)          // { items:[], total, exportable, skipped_missing, skipped_empty }
const previewBusy = ref(false)
const checked = ref(new Set())      // 已勾选的图片 id
// 勾选变化时自动剔除「源文件未找到 / 零字节」的不可导出项
const candidates = computed(() => (preview.value && preview.value.items) || [])
const exportableIds = computed(() =>
  candidates.value.filter((x) => x.on_disk && !x.zero_bytes).map((x) => x.id))

function previewFilters() {
  const p = {
    only_favorite: kit.only_favorite,
    limit: Number(kit.limit) || 500,
    group_by: kit.group_by,
    extra_terms: kit.extra_terms,
    caption_template: kit.caption_template,
  }
  if (kit.min_score !== '') p.min_score = Number(kit.min_score)
  if (kit.task_id !== '') p.task_id = Number(kit.task_id)
  if (kit.tag) p.tag = kit.tag
  return p
}

async function doPreview() {
  previewBusy.value = true
  err.value = ''
  preview.value = null
  checked.value = new Set()
  try {
    const r = await api.exportKitPreview(previewFilters())
    if (!r.ok) throw new Error(r.error || '生成导出预览失败')
    preview.value = r
    lastPreviewParams = JSON.stringify(previewFilters())
    // 默认勾选全部可导出项（源文件在且非零字节）
    checked.value = new Set(r.items.filter((x) => x.on_disk && !x.zero_bytes).map((x) => x.id))
  } catch (e) {
    err.value = e.message || '生成导出预览失败'
  } finally {
    previewBusy.value = false
  }
}
let lastPreviewParams = ''

function toggleOne(id, on) {
  const s = new Set(checked.value)
  if (on) s.add(id); else s.delete(id)
  checked.value = s
}
function selectAll() {
  checked.value = new Set(exportableIds.value)
}
function selectNone() {
  checked.value = new Set()
}
function invertSelect() {
  const s = new Set()
  for (const id of exportableIds.value) if (!checked.value.has(id)) s.add(id)
  checked.value = s
}
const checkedCount = computed(() => checked.value.size)

async function doSuggest() {
  tuneBusy.value = true
  err.value = ''
  try {
    tune.value = await api.thresholdSuggest()
  } catch (e) {
    err.value = e.message || '计算合格线失败'
  } finally {
    tuneBusy.value = false
  }
}

async function doApply() {
  if (!tune.value || !tune.value.ok) return
  tuneBusy.value = true
  err.value = ''
  try {
    const r = await api.thresholdApply(tune.value.suggested, applyTasks.value)
    info.value = r.note || '合格线已设置'
    await doSuggest()
  } catch (e) {
    err.value = e.message || '应用合格线失败'
  } finally {
    tuneBusy.value = false
  }
}

async function downloadEncrypted(kind) {
  busy.value = kind
  err.value = ''
  try {
    const r = await fetch(`/api/export/file?kind=${kind}`, {
      credentials: 'same-origin',
    })
    if (!r.ok) throw new Error(`导出失败（HTTP ${r.status}）`)
    const ct = r.headers.get('content-type') || ''
    if (!ct.includes('octet-stream')) {
      throw new Error('会话状态已失效，请重新登录后再执行导出')
    }
    const blob = await r.blob()
    const ts = new Date().toISOString().slice(0, 10).replace(/-/g, '')
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = (kind === 'report' ? '采集报告_' : '图库索引_') + ts + '.mrmx'
    a.click()
    URL.revokeObjectURL(a.href)
    info.value = '加密文件已开始下载，该文件仅供本软件读取。'
  } catch (e) {
    err.value = e.message || '导出失败'
  } finally {
    busy.value = ''
  }
}

function onPicked(p) {
  kit.dest = p
  pickerOpen.value = false
}

async function doKit() {
  if (!kit.dest) { err.value = '请先选择导出目录'; return }
  if (!preview.value) { err.value = '请先生成导出预览并勾选图片'; return }
  if (JSON.stringify(previewFilters()) !== lastPreviewParams) {
    err.value = '筛选条件或标注模板已修改，请重新生成预览后再导出。'
    return
  }
  if (!checkedCount.value) { err.value = '请至少勾选一张要导出的图片'; return }
  busy.value = 'kit'
  err.value = ''
  kitResult.value = null
  const payload = previewFilters()
  payload.dest = kit.dest
  // 只导出勾选中的图片：把 ids 交给后端（export_kit 的白名单口径）
  payload.ids = Array.from(checked.value)
  try {
    const r = await api.exportTrainingKit(payload)
    if (!r.ok) throw new Error(r.error || '导出训练素材集失败')
    kitResult.value = r
    info.value = r.note || '训练素材集已导出'
    await loadHistory()
  } catch (e) {
    err.value = e.message || '导出训练素材集失败'
  } finally {
    busy.value = ''
  }
}

// ── V0.0.18 · 导出历史 ──
const history = ref(null)
const historyBusy = ref(false)
async function loadHistory() {
  historyBusy.value = true
  try {
    const r = await api.exportHistory(50)
    if (r.ok) history.value = r.items || []
  } catch (e) { /* 静默：历史读不到不影响导出 */ }
  finally { historyBusy.value = false }
}

function filterSummary(f) {
  const parts = []
  if (f && f.only_favorite) parts.push('仅收藏')
  if (f && f.task_id) parts.push(`任务 #${f.task_id}`)
  if (f && f.tag) parts.push(`标签 ${f.tag}`)
  if (f && f.limit) parts.push(`上限 ${f.limit} 张`)
  return parts.length ? parts.join(' · ') : '默认条件'
}

onMounted(() => {
  doSuggest(); loadHistory()
  unbindShell = [
    onShellSelectAll(() => selectAll()),
    onShellRefresh(() => { doSuggest(); loadHistory() }),
  ]
})
onUnmounted(() => {
  unbindShell.forEach((fn) => fn())
  unbindShell = []
})
</script>

<template>
  <div class="page">
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="info" class="goodbar">{{ info }}</p>

    <!-- V0.0.32：和设置页同款标签页（用户要求导入/导出分页） -->
    <div class="tabs card">
      <button v-for="t in TABS" :key="t.k" class="tab" :class="{ on: tab === t.k }"
              @click="tab = t.k">
        <Icon :name="t.icon" :size="13" />
        <span>{{ t.label }}</span>
      </button>
    </div>

    <div class="card intro" v-show="tab === 'exp'">
      <h3>这一页做什么</h3>
      <p class="small">
        把判定合格的图片整理成一份可直接用于训练 AI 的素材集。
        本机已有图片想收进图库，请切到「导入」标签页。
      </p>
      <p class="small" style="margin:8px 0 4px">导出按下面三步走：</p>
      <ol class="small steps">
        <li><b>定合格线</b>：按已收藏与已判退图片的评分算出一个分数门槛，低于门槛的图片不算合格。</li>
        <li><b>生成预览并勾选</b>：先列出符合条件的图片供核对（此步骤不复制、不改动任何文件），
          不需要的取消勾选。</li>
        <li><b>导出素材集</b>：把勾选的图片复制到指定文件夹，每张图片旁生成一份同名标注文件，
          这份「图片 + 标注」即训练用数据集。原图库只读，不会被移动或改名。</li>
      </ol>
      <p class="muted small">
        另提供两个留档按钮：将采集报告或图库索引导出为加密文件（.mrmx），
        仅能在本软件中打开，用于换机或备份。
      </p>
    </div>

    <!-- V0.0.32：导入独立成标签页（原「展开导入面板」折叠取消）。
         V0.0.45：原来这里还有一段和组件里重复的流程说明，用户反馈「越看越
         不明白」—— 说明只留一处，卡片里只放标题和一句定位。 -->
    <div class="card sec" v-show="tab === 'imp'">
      <div class="sec-hd">
        <Icon name="folder-open" :size="15" />
        <h3>导入图片</h3>
        <span class="muted small">把已有图片收进图库；原图不移动、不改名</span>
      </div>
      <ImportFolder />
    </div>

    <div class="expwrap" v-show="tab === 'exp'">
    <!-- 1. 合格线：先把素材挑干净 -->
    <div class="card sec">
      <div class="sec-hd">
        <Icon name="sliders-horizontal" :size="15" />
        <h3>第一步 · 定合格线</h3>
        <span class="muted small">门槛以下的不进素材集</span>
      </div>
      <div v-if="tune" class="tune">
        <div class="kv">
          <span>当前合格线</span><b>{{ tune.current }}</b>
          <span>样本数量</span><b>收藏 {{ tune.pos }} 张 · 判退 {{ tune.neg }} 张</b>
        </div>
        <div v-if="tune.ok" class="tune-ok">
          <div class="big">建议合格线 {{ tune.suggested }}</div>
          <p class="muted small">{{ tune.note }}</p>
          <label class="chk">
            <input v-model="applyTasks" type="checkbox" />
            同时覆盖所有任务中已填写的合格线
          </label>
          <button class="btn sm primary" :disabled="tuneBusy" @click="doApply">
            <Icon name="check" :size="13" />应用建议
          </button>
        </div>
        <p v-else class="muted small">{{ tune.note }}</p>
      </div>
      <p v-else class="muted small">正在读取判定样本…</p>
      <div class="sec-ft">
        <button class="btn sm" :disabled="tuneBusy" @click="doSuggest">
          <Icon name="refresh-cw" :size="13" />重新计算
        </button>
        <span class="muted tiny">任务中单独填写过合格线的，以任务设置为准。</span>
      </div>
    </div>

    <!-- 2. 留档 / 换机：含图库信息一律加密 -->
    <div class="card sec">
      <div class="sec-hd">
        <Icon name="lock" :size="15" />
        <h3>另外 · 留档与换机</h3>
        <span class="muted small">导出加密文件，只有本软件能打开</span>
      </div>
      <div class="ops">
        <button class="btn sm" :disabled="!!busy" @click="downloadEncrypted('report')">
          <Icon name="download" :size="13" />导出采集报告
        </button>
        <button class="btn sm" :disabled="!!busy" @click="downloadEncrypted('index')">
          <Icon name="download" :size="13" />导出图库索引
        </button>
      </div>
      <p class="muted tiny">
        采集报告：这段时间采了多少、合格率多少，给别人看用。
        图库索引：每张图在哪、有什么标签，换电脑或重装时用。
        两者都是 .mrmx 加密文件，只能在本软件里打开。
      </p>
    </div>

    <!-- 3. 交付给 AI：两步工作流 —— 先预览勾选，再导出素材集 -->
    <div class="card sec">
      <div class="sec-hd">
        <Icon name="sparkles" :size="15" />
        <h3>第二步、第三步 · 预览并导出训练素材集</h3>
        <span class="muted small">复制合格图 + 生成同名标注，交给 AI 训练用</span>
      </div>

      <div class="form">
        <label class="row">
          <span>导出到哪个文件夹</span>
          <input v-model="kit.dest" type="text" placeholder="选择软件所在机器上的一个文件夹" />
          <button class="btn sm" @click="pickerOpen = true">选择文件夹…</button>
          <span class="muted tiny">图片会复制到这里；原图库不动，只新增副本。</span>
        </label>
        <label class="row">
          <span>分数不低于</span>
          <input v-model="kit.min_score" type="number" min="0" max="1" step="0.01"
                 placeholder="留空则用第一步的合格线" />
          <span class="muted tiny">0 到 1 之间，越大越严格。</span>
        </label>
        <label class="row">
          <span>最多导出几张</span>
          <input v-model="kit.limit" type="number" min="1" max="5000" />
          <span class="muted tiny">一次最多 5000 张，够训练用即可。</span>
        </label>
        <label class="row">
          <span>只导出某个任务的图</span>
          <input v-model="kit.task_id" type="number" min="0" placeholder="留空则不限制" />
          <span class="muted tiny">任务编号在「任务」页每行最左边可以看到。</span>
        </label>
        <label class="row">
          <span>只导出带某个标签的图</span>
          <input v-model="kit.tag" type="text" placeholder="留空则不限制" />
          <span class="muted tiny">按标签再筛一层，例如只导「白丝」。</span>
        </label>
        <label class="row">
          <span>额外写进标注的词</span>
          <input v-model="kit.extra_terms" type="text"
                 placeholder="多个词用逗号分开，例如：白丝, 长腿" />
          <span class="muted tiny">这些词会一起写进每张图的标注文件。</span>
        </label>
        <label class="row">
          <span>标注文字规范</span>
          <input v-model="kit.caption_template" type="text"
                 placeholder="留空则用默认写法" />
          <span class="muted tiny">用 <code>{terms}</code> 代表上面的词，留空就是默认的「词1, 词2」。</span>
        </label>
        <div class="row">
          <span>文件夹组织方式</span>
          <select v-model="kit.group_by">
            <option value="task">按任务分文件夹</option>
            <option value="flat">全部平铺在一个文件夹</option>
          </select>
          <label class="chk inline">
            <input v-model="kit.only_favorite" type="checkbox" />只导出我收藏过的图
          </label>
        </div>
      </div>
      <p class="muted tiny">
        标注文件：每张图旁边生成一个同名 .txt，内容是这张图的描述词，训练时和图片配对使用。
      </p>

      <!-- 第一步：预览 -->
      <div class="sec-ft">
        <button class="btn sm" :disabled="previewBusy" @click="doPreview">
          <Icon :anim="previewBusy ? 'spin' : ''" :name="previewBusy ? 'loader-circle' : 'eye'" :size="13" />生成预览</button>
        <span class="muted tiny">预览不落盘，仅列出候选图片供核对与勾选。</span>
      </div>

      <div v-if="preview" class="pv">
        <div class="pv-sum">
          <span>候选 <b>{{ preview.total }}</b> 张</span>
          <span>可导出 <b>{{ preview.exportable }}</b> 张</span>
          <span v-if="preview.skipped_missing">源文件未找到 <b>{{ preview.skipped_missing }}</b> 张</span>
          <span v-if="preview.skipped_empty">零字节图 <b>{{ preview.skipped_empty }}</b> 张</span>
          <span class="muted tiny">合格线 {{ preview.min_score }}</span>
        </div>

        <div class="pv-bar">
          <button class="btn sm ghost" @click="selectAll">全选</button>
          <button class="btn sm ghost" @click="invertSelect">反选</button>
          <button class="btn sm ghost" @click="selectNone">取消全选</button>
          <span class="muted small">已勾选 {{ checkedCount }} 张</span>
        </div>

        <div class="pv-list">
          <div v-for="x in preview.items" :key="x.id" class="pv-row" :class="{ off: !(x.on_disk && !x.zero_bytes) }">
            <input type="checkbox" :checked="checked.has(x.id)"
                   :disabled="!(x.on_disk && !x.zero_bytes)"
                   @change="toggleOne(x.id, $event.target.checked)" />
            <img class="pv-thumb" :src="'/thumb/' + x.id" :alt="x.name" loading="lazy" />
            <div class="pv-info">
              <div class="pv-name" :title="x.name">{{ x.name }}</div>
              <div class="pv-cap">标注：{{ x.caption }}</div>
              <div class="pv-meta">
                <span>任务：{{ x.task || ('#' + x.task_id) }}</span>
                <span>评分 {{ x.score }}</span>
              </div>
            </div>
            <div class="pv-flag">
              <span v-if="!x.on_disk" class="tag bad">源文件未找到</span>
              <span v-else-if="x.zero_bytes" class="tag warn">零字节</span>
              <span v-else class="tag ok">可导出</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 第二步：导出 -->
      <div class="sec-ft">
        <button class="btn sm primary" :disabled="busy === 'kit' || !preview || !checkedCount" @click="doKit">
          <Icon name="layers" :size="13" />导出训练素材集</button>
        <span class="muted tiny">原图仅被复制，不会删除、移动或改名；零字节图片将跳过。</span>
      </div>
      <div v-if="kitResult" class="kit-res">
        <p>{{ kitResult.note }}</p>
        <p class="muted tiny">导出位置：{{ kitResult.dest }}</p>
      </div>
    </div>

    <!-- 4. V0.0.18：导出历史 -->
    <div class="card sec">
      <div class="sec-hd">
        <Icon name="clock" :size="15" />
        <h3>导出历史</h3>
        <button class="btn sm ghost" :disabled="historyBusy" @click="loadHistory">
          <Icon :anim="historyBusy ? 'spin' : ''" :name="historyBusy ? 'loader-circle' : 'refresh-cw'" :size="13" />刷新</button>
      </div>
      <div v-if="history && history.length" class="hs">
        <div class="tr th">
          <span>时间</span><span>目录</span><span class="num">张数</span>
          <span class="num">合格线</span><span>筛选摘要</span>
        </div>
        <div v-for="h in history" :key="h.id" class="tr">
          <span class="mono tiny">{{ h.ts }}</span>
          <span class="path" :title="h.dest">{{ h.dest }}</span>
          <span class="num mono">{{ h.copied }}</span>
          <span class="num mono">{{ h.min_score }}</span>
          <span class="muted tiny">{{ filterSummary(h.filters) }}</span>
        </div>
      </div>
      <EmptyState v-else icon="folder-open" title="暂无导出记录。"
                  desc="设定合格线并生成预览后，导出的素材集会显示在这里。" />
    </div>
    </div><!-- /expwrap -->

    <FolderPicker :open="pickerOpen" :start="kit.dest"
                  title="选择导出目录" hint="建议选择图库目录以外的空文件夹"
                  @close="pickerOpen = false" @picked="onPicked" />
  </div>
</template>

<style scoped>
/* V0.0.27：块间距统一交给 page 网格（14px），不再用 sec 自己的 margin-bottom
   —— intro 卡与第一张 sec 卡之间原来是 0 缝。 */
.page { display: grid; gap: 14px; align-items: start; }
/* V0.0.32：导出标签页里的卡片同样走 14px 缝（外层 page 网格管不到包装层内部） */
.expwrap { display: grid; gap: 14px; align-items: start; }
/* 标签页样式与设置页同款 */
/* V0.0.35：标签之间 4px 太挤（和设置页同款，一起放宽） */
.tabs { display: flex; gap: 8px; padding: 6px; flex-wrap: wrap; }
.tab {
  display: inline-flex; align-items: center; gap: 5px;
  border: 1px solid transparent; background: transparent; color: var(--muted);
  padding: 6px 11px; border-radius: 8px; font-size: 13px; white-space: nowrap;
}
.tab:hover { background: #f7f5ff; color: var(--text); }
.tab.on {
  background: #f1edff; border-color: #ded4ff; color: var(--brand); font-weight: 600;
}
.sec { padding: 16px 18px; }
.sec-hd { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.sec-hd h3 { margin: 0; font-size: 15px; }
/* V0.0.23：页首「这一页做什么」——用户原话「完全不明白」，先把流程说清 */
.intro { padding: 16px 18px; }
.intro h3 { margin: 0 0 8px; font-size: 15px; }
.intro .steps { margin: 8px 0 8px 18px; padding: 0; display: grid; gap: 6px; }
.intro .steps li { line-height: 1.6; }
.sec-ft { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
.ops { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.btn.sm { padding: 5px 12px; font-size: 12px; border-radius: 8px; }
.tune { margin-bottom: 6px; }
.kv { display: grid; grid-template-columns: auto 1fr auto 1fr; gap: 6px 14px; font-size: 13px; align-items: center; }
.kv span { color: var(--muted); }
.tune-ok { margin-top: 12px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; }
.big { font-size: 16px; font-weight: 600; color: var(--brand); margin-bottom: 4px; }
.chk { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; margin-right: 12px; }
.chk.inline { margin-right: 0; }
.form { display: flex; flex-direction: column; gap: 10px; }
/* V0.0.23：字段名比原来的「导出目录／最低合格线」长（改成白话后），
   88px 的标签列会折行压到输入框上。加宽到 130px 并允许换行、顶部对齐。 */
.row { display: flex; align-items: flex-start; gap: 10px; flex-wrap: wrap; font-size: 13px; }
.row > span:first-child { width: 130px; flex: none; color: var(--muted); line-height: 1.5;
  padding-top: 6px; white-space: normal; }
.row input[type="text"], .row input[type="number"], .row select {
  border: 1px solid var(--line); border-radius: 8px; padding: 5px 9px;
  background: #fff; font-size: 13px; min-width: 180px;
}
.row input[type="number"] { min-width: 110px; }
.kit-res { margin-top: 12px; padding: 10px 12px; border-radius: 10px; background: #f4f1ff; font-size: 13px; }
.small { font-size: 12px; }
.tiny { font-size: 11px; }
.err { color: var(--err); margin: 0 0 10px; }
.goodbar { color: var(--brand); margin: 0 0 10px; }
code { background: #f2f3f8; border-radius: 4px; padding: 0 4px; font-size: 11px; }
/* V0.0.18 · 预览清单 */
.pv { margin-top: 12px; border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.pv-sum { display: flex; gap: 16px; flex-wrap: wrap; padding: 8px 12px; background: #faf9ff; font-size: 12.5px; border-bottom: 1px solid var(--line); }
.pv-bar { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-bottom: 1px solid var(--line); }
.pv-list { max-height: 420px; overflow: auto; }
.pv-row { display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-bottom: 1px solid var(--line); }
.pv-row:last-child { border-bottom: 0; }
.pv-row.off { opacity: .55; }
.pv-row input[type="checkbox"] { width: 16px; height: 16px; flex: none; }
.pv-thumb { width: 44px; height: 44px; border-radius: 6px; object-fit: cover; background: #eee; flex: none; }
.pv-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.pv-name { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pv-cap { font-size: 11.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pv-meta { display: flex; gap: 14px; font-size: 11px; color: var(--muted); }
.pv-flag { flex: none; }
.tag { font-size: 11px; border-radius: 999px; padding: 1px 8px; }
.tag.bad { background: #fde6e6; color: var(--err); }
.tag.warn { background: #fdf0d5; color: #8a6200; }
.tag.ok { background: #e6f4ea; color: #2e9e5b; }
/* V0.0.18 · 导出历史表 */
.hs { border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
.hs .tr { display: grid; grid-template-columns: 1.1fr 1.6fr .5fr .6fr 1.4fr; gap: 10px;
  padding: 7px 12px; font-size: 12.5px; border-bottom: 1px solid var(--line); align-items: center; }
.hs .tr:last-child { border-bottom: 0; }
.hs .tr.th { background: #faf9ff; color: var(--muted); font-size: 11.5px; font-weight: 600; }
.hs .num { text-align: right; }
.hs .tr.th .num { text-align: right; }
.hs .path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hs .mono { font-variant-numeric: tabular-nums; }
</style>
