<script setup>
// 本地 AI 模型清单：选用已就绪的那一档（下载在后端跑，网页只轮询进度）。
// V0.0.40：整页按「用户看不懂」重写——去掉 onnxruntime 后端名、去掉重复的
// 推理设备下拉（那是设置页的一项，这里只显示状态并给入口），模型卡片只留
// 真正影响选择的三个数：多大、够不够快、要不要显卡。
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'
import { useShellRefresh } from '../shellVue'
import Icon from '../components/Icon.vue'

const router = useRouter()
const d = ref(null)
const err = ref('')
const info = ref('')
// R245（用户第 5 条）：本机配置 —— CPU / 内存 / GPU / 各盘容量。
const sys = ref(null)
const sysErr = ref('')
const dev = ref('auto')      // 推理设备（只读展示，改在「设置 → AI 与主题词」）
const dl = ref(null)         // 模型下载状态 { id, running, pct, text, ok, msg }
let dlTimer = null

const DEV_LABEL = {
  auto: '自动',
  gpu: '显卡',
  npu: 'NPU',
  cpu: 'CPU',
}
const devLabel = (v) => DEV_LABEL[String(v || 'auto')] || '自动'

async function loadDevice() {
  try {
    const s = await api.settings()
    dev.value = s.ai_device || 'auto'
  } catch { /* 忽略 */ }
}
function goSettings() {
  router.push({ path: '/settings', query: { tab: 'ai' } })
}

// 网页遥控下载（后端 DownloadWorker 跑，前端只轮询进度）
async function download(m) {
  err.value = ''
  try {
    const r = await api.post('/api/model/download', { id: m.id })
    if (!r || r.ok === false) { err.value = (r && r.error) || '启动下载失败'; return }
    info.value = `开始下载 ${m.name}…`
    pollDownload()
  } catch (e) { err.value = e.message }
}
function pollDownload() {
  if (dlTimer) clearInterval(dlTimer)
  dlTimer = setInterval(async () => {
    try {
      const s = await api.get('/api/model/download/status')
      dl.value = s
      if (!s || !s.running) {
        clearInterval(dlTimer)
        dlTimer = null
        if (s && s.ok) {
          info.value = s.msg || '下载完成'
          setTimeout(() => (info.value = ''), 4000)
          await load()
        } else if (s && s.ok === false) {
          err.value = s.msg || '下载失败'
        }
      }
    } catch { /* 单次失败不打断轮询 */ }
  }, 1500)
}
async function cancelDownload() {
  try { await api.post('/api/model/download/cancel', {}) } catch { /* 忽略 */ }
}

async function load() {
  try { d.value = await api.models(); err.value = '' }
  catch (e) { err.value = e.message }
}
async function loadSys() {
  try {
    const r = await api.sysinfo()
    if (r && r.ok === false) { sysErr.value = r.error || '读取失败'; return }
    sys.value = r
    sysErr.value = ''
  } catch (e) { sysErr.value = e.message }
}
/** 内存/显卡占用条：0..100 的百分比，>85% 标红（一眼能看出吃满了没）。 */
function fmtGB(n) {
  const v = Number(n) || 0
  if (!v) return '—'
  return (v / 1073741824).toFixed(1) + ' GB'
}
// V0.0.50：按本机最大显存给出档位建议（≥12GB 提示可用最高精度档）。
// 面向开源后的各类硬件，不假设某一种显卡；没有检测到独显时不显示提示。
const bigVram = computed(() => {
  const gs = (sys.value && sys.value.gpus) || []
  let max = 0
  for (const g of gs) {
    const b = Number(g && g.vram_total) || 0
    if (b > max) max = b
  }
  return max >= 12 * 1073741824 ? fmtGB(max) : ''
})
function pct(used, total) {
  const u = Number(used) || 0
  const t = Number(total) || 0
  if (!t) return 0
  return Math.max(0, Math.min(100, Math.round((u / t) * 100)))
}
async function use(m) {
  try {
    await api.modelUse(m.id)
    info.value = `已切换到 ${m.name}`
    setTimeout(() => (info.value = ''), 2500)
    await load()
  } catch (e) { err.value = e.message }
}
onMounted(() => { load(); loadDevice(); loadSys() })
useShellRefresh(() => { load(); loadDevice(); loadSys() })
onUnmounted(() => { if (dlTimer) clearInterval(dlTimer) })
</script>

<template>
  <div class="page">
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="info" class="ok">{{ info }}</p>

    <!-- V0.0.40：一句话说清这页是干什么的、不知道该选哪个怎么办。
         原来这页直接把六个型号和一堆英文后端名摊在用户面前，等于让人自己猜。 -->
    <p class="intro muted">
      此页用于选择图像识别模型。软件可运行在各类设备上：从无独立显卡的普通电脑，
      到配备高性能显卡的工作站均可使用。<b>硬件性能决定可选的精度档位与处理速度</b>——
      显卡越强、显存越大，越能发挥高精度档位的效果；默认档位无需独立显卡即可运行。
      不同设备的处理速度与推荐档位会有差异，可按本机配置在下方选择。
      更换模型仅影响后续的识别处理，已处理的图像不会重新计算。
    </p>

    <!-- R245：本机配置。放最上面 —— 选模型之前先看自己有什么硬件。 -->
    <section class="card sys">
      <header class="sys-head">
        <h3>本机配置</h3>
        <span class="muted small">{{ sys ? sys.headline : (sysErr || '读取中…') }}</span>
        <button class="btn sm ghost" title="重新读取" @click="loadSys">
          <Icon name="refresh-cw" :size="13" />刷新</button>
      </header>
      <div v-if="sys" class="sysgrid">
        <div class="sbox">
          <div class="sk">处理器</div>
          <div class="sv">{{ sys.cpu.name }}</div>
          <div class="sm2">{{ sys.cpu.cores_logical }} 线程<span v-if="sys.cpu.mhz"> · {{ sys.cpu.mhz }} MHz</span></div>
        </div>
        <div class="sbox">
          <div class="sk">内存</div>
          <div class="sv">{{ fmtGB(sys.memory.total) }}<span class="sm2"> 可用 {{ fmtGB(sys.memory.available) }}</span></div>
          <div class="bar"><i :class="{ hot: (sys.memory.percent || 0) > 85 }"
                              :style="{ width: (sys.memory.percent || 0) + '%' }"></i></div>
          <div class="sm2">已用 {{ sys.memory.percent || 0 }}%</div>
        </div>
        <div class="sbox" v-for="(g, i) in (sys.gpus || [])" :key="'g' + i">
          <div class="sk">显卡{{ (sys.gpus || []).length > 1 ? ' ' + (i + 1) : '' }}</div>
          <div class="sv">{{ g.name }}</div>
          <div class="bar"><i :class="{ hot: pct(g.vram_used, g.vram_total) > 85 }"
                              :style="{ width: pct(g.vram_used, g.vram_total) + '%' }"></i></div>
          <div class="sm2">显存 {{ g.vram_used_text }} / {{ g.vram_total_text }}
            <span v-if="g.util !== null && g.util !== undefined"> · 占用 {{ g.util }}%</span></div>
        </div>
        <div class="sbox" v-if="!(sys.gpus || []).length">
          <div class="sk">显卡</div>
          <div class="sv">未检测到独立显卡</div>
          <div class="sm2">将采用 CPU 推理（速度较慢，功能不受影响）</div>
        </div>
        <!-- V0.0.40：这里原来还有一格「推理后端」，显示的是两串英文后端名，
             对用户没有任何决策价值（下面那行「实际用在什么」才是答案）。 -->
      </div>
      <div v-if="sys" class="disks">
        <div class="disk" v-for="dk in (sys.drives || [])" :key="dk.path">
          <span class="dl">{{ dk.label }}</span>
          <div class="bar wide"><i :class="{ hot: dk.total && dk.free / dk.total < 0.1 }"
                                   :style="{ width: (dk.total ? Math.round((1 - dk.free / dk.total) * 100) : 0) + '%' }"></i></div>
          <span class="dt">剩余 {{ dk.free_text }} / 共 {{ dk.total_text }}</span>
        </div>
      </div>
      <!-- V0.0.23：用户要求删掉「当前下载目录」这块（他说这个功能没用）。
           磁盘剩余空间在下面「各盘容量」里已经能看到，不必再单列一遍。 -->
    </section>

    <!-- 2026-10-27 修：以前判 `gpu_state === 'ok'`，但 gpu_state 是一整句
         中文说明、永远不等于 'ok' ⇒ 这个灯永远是灰的（假信息）。
         真正表示"在用显卡"的字段是 gpu_have。 -->
    <div v-if="d" class="card head">
      <span class="pill" :class="d.gpu_have === 'gpu' && !d.gpu_degraded ? 'ok' : 'muted'">
        当前使用：{{ d.gpu_have === 'gpu' ? '显卡' : 'CPU' }}
      </span>
      <!-- V0.0.23：原来这里只有一颗「已降级」，用户看到就以为软件坏了。
           现在写清三件事：在用 CPU、本机其实有显卡、为什么还是 CPU。 -->
      <span v-if="d.gpu_degraded" class="pill warn"
            :title="d.gpu_state || ''">在用 CPU（显卡没启用）</span>
      <!-- V0.0.40：设备选择只留一处入口（设置页那一项），这里显示当前档位，
           要改直接跳过去 —— 同一项两个下拉，改完另一处不刷新就会自相矛盾。 -->
      <span class="dev">
        档位：<b>{{ devLabel(dev) }}</b>
        <button class="btn sm ghost" type="button" @click="goSettings">
          <Icon name="settings" :size="12" />调整
        </button>
      </span>
      <span class="muted small">已占用 {{ d.usage || '—' }}</span>
      <p class="gpu-note muted small" style="white-space:pre-line">{{ d.gpu_state }}</p>
      <div v-if="d.gpu_degraded" class="gpuhelp">
        <p class="muted small" style="margin:0">
          显卡加速组件已随软件内置，但本机未能启用，当前采用 CPU 推理 —— 此情况不属于故障。
          所选模型档位在 CPU 上通常具有相近或更优的推理效率。
        </p>
        <p class="muted small" style="margin:8px 0 6px">
          如需启用显卡加速，请将显卡驱动更新至支持 DirectX 12 的版本，重启软件后将自动识别。
        </p>
        <div class="inline">
          <a class="btn sm" target="_blank" rel="noopener"
             href="https://www.nvidia.cn/Download/index.aspx">前往更新显卡驱动</a>
        </div>
      </div>
    </div>
    <!-- V0.0.50：按本机已检测的显存给出档位建议 —— 面向开源后各类硬件，
         不假设某一种显卡；大显存用户能明确知道自己可以上最高精度档。 -->
    <p v-if="bigVram" class="card hint-vram">
      <Icon name="sparkles" :size="13" />
      本机检测到 {{ bigVram }} 显存的显卡，可选中文/英文最高精度档与 DINOv2-L 大显存档，充分发挥硬件性能。
    </p>

    <div class="grid">
      <article v-for="m in (d && d.items) || []" :key="m.id" class="card m">
        <header>
          <b>{{ m.name }}</b>
          <!-- V0.0.40：原来「内置」和「未下载」会同时出现（随包内置的模型
               在 data/models 里没有副本，就算未下载），而且这种模型明明能用
               却只给「下载」按钮。现在分三种状态，内置的直接可选用。 -->
          <span v-if="m.current" class="pill ok">使用中</span>
          <span v-else-if="m.ready" class="pill muted">已下载</span>
          <span v-else-if="m.builtin" class="pill muted">内置可用</span>
          <span v-else class="pill warn">需下载</span>
        </header>
        <p class="muted small">{{ m.note }}</p>
        <ul class="small">
          <li>体积：{{ m.size_hint || '—' }}</li>
          <li>适配：{{ m.hardware || '—' }}</li>
        </ul>
        <button v-if="m.ready || m.builtin" class="btn sm" :disabled="m.current" @click="use(m)">
          {{ m.current ? '当前使用' : '设为当前模型' }}
        </button>
        <button v-else class="btn sm primary" :disabled="dl && dl.running"
                @click="download(m)"><Icon name="download" :size="13" />下载</button>

        <!-- 正在下载这个模型：进度条 -->
        <div v-if="dl && dl.id === m.id" class="dlbox">
          <div class="pbar"><i :style="{ width: (dl.pct || 0) + '%' }"></i></div>
          <div class="muted small dl-line">
            <!-- 下载中给个"数据在流"的动画：几百 MB 的模型，进度条一分钟才动一格，
                 只有这个能让用户确认它还在跑而不是卡死 -->
            <Icon v-if="dl.running" anim="dl" :size="14" />
            {{ dl.running ? (dl.pct || 0) + '% · ' + (dl.text || '') : (dl.ok ? '✓ ' + (dl.msg || '完成') : '✗ ' + (dl.msg || '失败')) }}
          </div>
          <button v-if="dl.running" class="btn sm ghost" @click="cancelDownload">取消</button>
        </div>
      </article>
    </div>
  </div>
</template>

<style scoped>
/* V0.0.27：块与块之间统一 14px 缝（这页以前没有 page 间距，卡片全贴在一起） */
.page { display: grid; gap: 14px; align-items: start; }
.dl-line { display: inline-flex; align-items: center; gap: 5px; }
.head { display: flex; gap: 10px; align-items: center; padding: 10px 14px; flex-wrap: wrap; }
.gpu-note { margin: 0; width: 100%; line-height: 1.7; white-space: pre-wrap; }
.dev { display: inline-flex; gap: 6px; align-items: center; font-size: 12px; color: var(--muted); }
.dev select { width: 210px; }
.dlbox { display: grid; gap: 4px; }
.pbar { height: 6px; background: #eef0f7; border-radius: 999px; overflow: hidden; }
.pbar i { display: block; height: 100%; background: var(--brand); transition: width .3s; }
.grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); }
.m { padding: 14px 16px; display: grid; gap: 8px; }
/* 模型名很长（CLIP ViT-B/32（内置默认）），卡片又只有 260px —— 状态标签
   原来会被挤成两行「已下 / 载」。标签不许断行，放不下就整体换到下一行。 */
.m header { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.m header .pill { white-space: nowrap; }
.m ul { margin: 0; padding-left: 16px; color: var(--muted); }
.small { font-size: 12px; }
.err { color: var(--err); margin: 0; }
.hint-vram { display: flex; align-items: center; gap: 8px; margin: 10px 0;
             padding: 9px 12px; color: var(--text); background: var(--panel);
             border: 1px solid var(--line); border-left: 3px solid var(--accent, #6d5efc);
             border-radius: 8px; font-size: 12px; }

.intro { margin: 0; font-size: 13px; line-height: 1.75; }
.ok { color: var(--ok); margin: 0; }
/* V0.0.59：按钮尺寸已统一到全局 style.css，此处局部覆盖删除 */

/* R245 本机配置 */
.sys { padding: 14px 16px; display: grid; gap: 12px; }
.sys-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.sys-head h3 { margin: 0; font-size: 15px; }
.sys-head .btn { margin-left: auto; }
.sysgrid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); }
.sbox { border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; display: grid; gap: 4px; }
.sk { font-size: 11px; color: var(--muted); letter-spacing: .3px; }
.sv { font-size: 13.5px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sm2 { font-size: 11.5px; color: var(--muted); }
.bar { height: 6px; background: #eef0f7; border-radius: 999px; overflow: hidden; }
.bar.wide { flex: 1; min-width: 80px; }
.bar i { display: block; height: 100%; background: var(--brand); border-radius: 999px; transition: width .3s; }
.bar i.hot { background: #e11d48; }
.disks { display: grid; gap: 6px; }
.disk { display: flex; align-items: center; gap: 10px; font-size: 12px; }
.disk .dl { width: 30px; color: var(--muted); font-variant-numeric: tabular-nums; }
.disk .dt { color: var(--muted); white-space: nowrap; }
.dldir {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 8px 10px; border: 1px solid var(--line); border-radius: 10px;
  background: #fafbff; font-size: 12.5px;
}
.dldir .mono { font-size: 11.5px; color: var(--muted); }
@media (max-width: 760px) {
  .disk { flex-wrap: wrap; }
  .disk .dt { width: 100%; }
}
</style>
