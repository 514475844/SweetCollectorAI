<script setup>
/**
 * 「导入图片」表单组件。
 *
 * 产品位置：图库的入口 —— 把用户已经有的图片收进软件，原图不移动、不改名。
 * 两阶段（与后端 import_folder 一致）：先入库（几分钟就能翻），再后台打标（可停可续）。
 *
 * V0.0.35：作业状态与轮询移到 importJob.js（模块级单例）。路由切换会销毁
 * 本组件，之前进度条、按钮状态、定时器一起消失，看起来像「切回来就不导入了」；
 * 现在组件只负责显示，状态永远跟着后端作业走。
 * V0.0.44：断点续导入口、路径可手打、新增 WebDAV 来源。
 *
 * V0.0.45（用户：「导入我用着更不明白了，还不如原来的；点了开始导入也没有
 * 任何的提示」）—— 两个问题分别处理：
 *   · 真 Bug：`importJob.js` 里调了不存在的 `_saveForm()`，点按钮那一刻就抛
 *     异常，请求根本没发出去，界面一动不动。已修（详见该文件头注释）。
 *   · 界面：原来是「说明文字 → 来源 → 路径 → 三段长提示 → 三个开关 → 进度条」
 *     全部平铺，看不出先后。现在重排成**三步**（① 图片在哪 ② 选项 ③ 开始），
 *     每步一行标题；提示压成一句话，长解释收进悬停说明；进度与结果用独立
 *     状态卡片，写得像人话（「正在入库 1,200 / 12,500 张 · 约剩 3 分钟」）。
 */
import { ref, computed, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import {
  imp, startImport, cancelImport, resumeImport, loadLast, saveForm, clearResult,
} from '../importJob'
import Icon from './Icon.vue'
import FolderPicker from './FolderPicker.vue'

const emit = defineEmits(['done'])
const router = useRouter()

const pickerOpen = ref(false)
const resume = ref(null)      // { name, folder, total, pending }
const resumeHidden = ref(false)

// 完成时把结果转发给父页（状态在模块里，组件重建也不会漏）
watch(() => imp.doneSeq, () => {
  if (imp.doneSeq > 0) emit('done', imp.result)
  refreshLast()
})

// 表单随时落盘：手打的路径 / 地址不该因为切页或重开软件丢掉
watch(() => [imp.mode, imp.folder, imp.url, imp.user, imp.recursive,
             imp.indexOnly, imp.copyIn], saveForm)

const isDav = computed(() => imp.mode === 'webdav')
const isRemote = computed(() => imp.mode === 'remote')
const isNet = computed(() => isDav.value || isRemote.value)
const busy = computed(() => imp.starting || imp.running)
const canStart = computed(() => !busy.value && (
  isRemote.value ? (!!imp.host && !!imp.remotePath)
    : isDav.value ? !!imp.url : !!imp.folder))

// ── 进度说人话 ───────────────────────────────────────────────
// phase 由后端给：文件夹导入是 index/tag，WebDAV 导入还多 list/download。
const phaseLabel = computed(() => {
  if (imp.starting) return isRemote.value ? '正在连接服务器' : (isDav.value ? '正在连接 WebDAV' : '正在准备')
  // V0.0.60：导入只报入库/下载阶段，AI 识别已经是独立作业（另一条进度条）。
  const m = {
    list: '正在读取目录',
    download: '正在下载图片',
    index: '正在入库',
    tag: '正在 AI 识别',
  }
  return m[imp.phase] || (imp.running ? '正在准备' : '')
})
// V0.0.59：百分比必须与旁边的「已完成 / 总数」是同一套算法。
// 后端 pct 是「整体流程」百分比（入库只占前 20%，打标占后 80%），
// 于是 35750/100365（≈35%）被显示成 20×35%≈7%，用户以为算错了。
// 导入卡这一屏只讲当前阶段，百分比一律按 done/total 现算，进度条同口径。
const stagePct = computed(() => {
  if (!imp.total || imp.done <= 0) return 0
  return Math.min(100, Math.max(0, Math.round(imp.done * 100 / imp.total)))
})
const countText = computed(() => {
  if (!imp.total) return '正在统计文件数量…'
  const pct = stagePct.value > 0 ? ` · ${stagePct.value}%` : ''
  return `${fmt(imp.done)} / ${fmt(imp.total)} 张${pct}`
})
// 只有真的知道「正在处理哪个文件」时才显示这一行，不要用「正在处理…」凑数
const curFile = computed(() => {
  if (imp.curName) return imp.curName
  return imp.starting ? '正在读取文件夹，请稍候…' : ''
})

function fmt(n) {
  const v = Number(n) || 0
  return v.toLocaleString('zh-CN')
}

// ── 结果卡片 ─────────────────────────────────────────────────
const r = computed(() => imp.result || {})
const resRows = computed(() => {
  const x = r.value
  const rows = []
  if (x.indexed) rows.push(['新收进图库', `${fmt(x.indexed)} 张`])
  if (x.skipped_dup) rows.push(['已存在、跳过', `${fmt(x.skipped_dup)} 张`])
  if (x.downloaded) rows.push(['从 WebDAV 下载', `${fmt(x.downloaded)} 张`])
  if (x.already_here) rows.push(['复用已下载', `${fmt(x.already_here)} 张`])
  if (x.copied) rows.push(['复制进软件图库', `${fmt(x.copied)} 张`])
  if (x.tagged) rows.push(['已识别', `${fmt(x.tagged)} 张`])
  if (x.pending) rows.push(['待识别', `${fmt(x.pending)} 张`])
  if (x.errors) rows.push(['读不出、已跳过', `${fmt(x.errors)} 个文件`])
  return rows
})
const resTitle = computed(() =>
  r.value.stopped ? '已停止' : (imp.result ? '导入完成' : ''))

function pick(p) {
  imp.mode = 'local'
  imp.folder = p
  pickerOpen.value = false
}

async function start() {
  resumeHidden.value = true
  await startImport()
}

function poll() {
  // 轮询由共享模块持有，切页不停；这里只保证组件挂载时已接上。
  resumeImport()
}

async function cancel() {
  await cancelImport()
}

async function refreshLast() {
  const items = await loadLast()
  // 只提示「还有没打完的」那一条，且目录得还在（外接盘拔了就点了也没用）
  resume.value = items.find((x) => x.pending > 0 && x.exists) || null
  if (resume.value) resumeHidden.value = false
}

async function continueLast() {
  if (!resume.value) return
  // 续导一律走本地路径：WebDAV 的图片其实已经落在图库 webdav/ 下了
  imp.mode = 'local'
  imp.folder = resume.value.folder
  imp.indexOnly = false
  resumeHidden.value = true
  await startImport()
  resume.value = null
}

/** V0.0.62：结果卡原来的重新导入式补识别入口已撤 —— 识别统一在图库工具栏控制。 */

function goGallery() {
  router.push('/gallery')
}

function again() {
  clearResult()
}

function setMode(m) {
  imp.mode = m
}

onMounted(poll)
onMounted(refreshLast)
</script>

<template>
  <div class="impbox">
    <!-- 断点续导：上次没打完的，这里一键接着来（放在最上面，一进页就看得到） -->
    <div v-if="resume && !resumeHidden && !busy" class="resumebar">
      <Icon name="rotate-cw" :size="14" />
      <span>
        「{{ resume.name || resume.folder }}」还有
        <b>{{ fmt(resume.pending) }}</b> 张没处理完（共 {{ fmt(resume.total) }} 张）
      </span>
      <button class="btn sm primary" @click="continueLast">继续导入</button>
      <button class="btn sm ghost" @click="resumeHidden = true">忽略</button>
    </div>

    <!-- ① 图片在哪里 -->
    <section class="grp">
      <div class="grp-hd">
        <span class="step">1</span>
        <b>图片在哪里</b>
        <span class="muted small">选一种来源</span>
      </div>

      <div class="srctabs">
        <button class="srctab" :class="{ on: !isNet }"
                :disabled="busy" @click="setMode('local')">
          <Icon name="folder" :size="13" />
          <span class="sname">本地文件夹</span>
          <span class="sdesc">本机磁盘、NAS 共享路径</span>
        </button>
        <button class="srctab" :class="{ on: isDav }"
                :disabled="busy" @click="setMode('webdav')">
          <Icon name="globe" :size="13" />
          <span class="sname">WebDAV 地址</span>
          <span class="sdesc">网盘 / NAS 的 WebDAV 入口</span>
        </button>
        <button class="srctab" :class="{ on: isRemote }"
                :disabled="busy" @click="setMode('remote')">
          <Icon name="hard-drive" :size="13" />
          <span class="sname">FTP / SFTP / SMB</span>
          <span class="sdesc">服务器、NAS 共享目录</span>
        </button>
      </div>

      <div v-if="!isNet" class="pathrow">
        <input v-model.trim="imp.folder" class="pathin" type="text"
               :disabled="busy" spellcheck="false"
               placeholder="粘贴文件夹完整路径，例如 D:\图片 或 \\192.168.1.50\photo" />
        <button class="btn sm" :disabled="busy" @click="pickerOpen = true">
          <Icon name="folder-open" :size="13" />选择文件夹…</button>
      </div>

      <div v-else-if="isDav" class="pathrow">
        <input v-model.trim="imp.url" class="pathin" type="text"
               :disabled="busy" spellcheck="false"
               placeholder="WebDAV 目录地址，例如 http://192.168.1.50:5005/photo/" />
        <input v-model.trim="imp.user" class="wd-sm" type="text" :disabled="busy"
               placeholder="账号（可不填）" autocomplete="off" />
        <input v-model="imp.pass" class="wd-sm" type="password" :disabled="busy"
               placeholder="密码（可不填）" autocomplete="new-password" />
      </div>

      <div v-else class="pathrow netpath">
        <select v-model="imp.protocol" class="nets" :disabled="busy" title="传输协议">
          <option value="sftp">SFTP（SSH，推荐）</option>
          <option value="smb">SMB（Windows 共享 / NAS）</option>
          <option value="ftp">FTP</option>
          <option value="ftps">FTPS（加密）</option>
        </select>
        <input v-model.trim="imp.host" class="wd-sm" type="text" :disabled="busy"
               placeholder="服务器地址" autocomplete="off" />
        <input v-model.trim="imp.port" class="wd-xs" type="number" :disabled="busy"
               placeholder="端口" />
        <input v-model.trim="imp.user" class="wd-sm" type="text" :disabled="busy"
               placeholder="账号" autocomplete="off" />
        <input v-model="imp.pass" class="wd-sm" type="password" :disabled="busy"
               placeholder="密码" autocomplete="new-password" />
        <input v-model.trim="imp.remotePath" class="pathin" type="text" :disabled="busy"
               placeholder="远端目录，例如 /photo/2026 或 \\nas\share\照片" />
      </div>

      <p class="tip muted">
        <template v-if="!isNet">
          路径可以直接手打或粘贴。NAS / 共享盘填 UNC 地址（
          <code>\\192.168.1.50\photo</code>），先在资源管理器里连一次并记住密码。
        </template>
        <template v-else-if="isDav">
          只读取，不改动源站文件。图片会先下载到软件图库的 <code>webdav</code>
          目录，再入库、AI 识别；中断后再点一次只补没下完的。
        </template>
        <template v-else>
          端口留空用默认值（SFTP 22 / SMB 445 / FTP 21 / FTPS 990）。只读取，不改动服务器文件；
          图片先下载到软件图库对应协议的目录再入库。中断后再点一次只补没下完的。
        </template>
      </p>
    </section>

    <!-- ② 导入选项 -->
    <section class="grp">
      <div class="grp-hd">
        <span class="step">2</span>
        <b>导入选项</b>
        <span class="muted small">保持默认即可，一般不需要改</span>
      </div>
      <div class="opts">
        <!-- 三个开关的文字与桌面端逐字一致（护栏 test_v007 钉住），别改成同义词。 -->
        <label class="chk" title="勾选：连同所有层级的子文件夹一起导入。取消：只导入所选这一层。">
          <input v-model="imp.recursive" type="checkbox" :disabled="busy" />
          包含子文件夹
        </label>
        <label class="chk" title="勾选后本次只入库、不自动开始 AI 识别；图片仍会留在待识别队列，随时可在下方单独启动。">
          <input v-model="imp.indexOnly" type="checkbox" :disabled="busy" />
          只入库、先不识别
        </label>
        <label class="chk" title="原图保持不动，软件里用副本。之后删除、移动只碰副本，但会占双份磁盘。">
          <input v-model="imp.copyIn" type="checkbox" :disabled="busy" />
          同时复制一份进软件图库
        </label>
      </div>
    </section>

    <!-- ③ 开始 -->
    <div class="grp">
      <div class="grp-hd">
        <span class="step">3</span>
        <b>开始导入</b>
        <span class="muted small">原图不会被移动、改名或删除</span>
      </div>
      <div class="actrow">
        <button class="btn primary go" :disabled="!canStart" @click="start"
                :title="canStart ? '开始导入' : (isRemote ? '先填写服务器地址和远端目录' : (isDav ? '先填写 WebDAV 地址' : '先选择或填写文件夹路径'))">
          <Icon :anim="busy ? 'spin' : ''"
                :name="busy ? 'loader-circle' : 'download'" :size="14" />
          <span v-if="imp.starting">正在准备…</span>
          <span v-else-if="imp.running">正在导入…</span>
          <span v-else>开始导入</span>
        </button>
        <button v-if="imp.running" class="btn danger" @click="cancel">
          <Icon name="pause" :size="13" />停止
        </button>
        <span class="muted small">中途可以停；下次再导入同一来源会接着来。</span>
      </div>
    </div>

    <!-- 进度卡（点了就出现，不给「点了没反应」留空档） -->
    <div v-if="busy" class="state run">
      <div class="state-hd">
        <Icon anim="spin" name="loader-circle" :size="15" />
        <b>{{ phaseLabel }}</b>
        <span class="muted small">{{ countText }}</span>
        <span v-if="imp.eta" class="muted small">约剩 {{ imp.eta }}</span>
      </div>
      <div class="bar"><i :style="{ width: Math.max(2, stagePct) + '%' }" /></div>
      <div v-if="curFile" class="state-ft muted small">{{ curFile }}</div>
    </div>

    <!-- 失败卡 -->
    <div v-else-if="imp.err" class="state bad">
      <div class="state-hd">
        <Icon name="triangle-alert" :size="15" />
        <b>导入没有进行</b>
      </div>
      <p class="msg">{{ imp.err }}</p>
      <div class="state-ft">
        <button class="btn sm" :disabled="!canStart" @click="start">重试</button>
      </div>
    </div>

    <!-- 结果卡 -->
    <div v-else-if="imp.result" class="state good">
      <div class="state-hd">
        <Icon name="check" :size="15" />
        <b>{{ resTitle }}</b>
        <!-- 完成时后端 message 也常常就是「导入完成」，别在标题旁边再重复一遍 -->
        <span v-if="imp.info && imp.info !== resTitle" class="muted small">{{ imp.info }}</span>
      </div>
      <div v-if="resRows.length" class="res imp-result">
        <span v-for="row in resRows" :key="row[0]" class="rk">
          {{ row[0] }} <b>{{ row[1] }}</b>
        </span>
      </div>
      <p v-if="r.ai_note" class="muted small note">{{ r.ai_note }}</p>
      <div class="state-ft">
        <button class="btn sm primary" @click="goGallery">
          <Icon name="images" :size="13" />去图库查看</button>
        <!-- V0.0.62（用户）：识别是图库页的事，结果卡不再放「继续识别」按钮，
             免得同一功能两处控制；待识别数在图库工具栏一眼能看到。 -->
        <button v-if="r.pending" class="btn sm" @click="goGallery">
          <Icon name="sparkles" :size="13" />还有 {{ fmt(r.pending) }} 张待识别（去图库）</button>
        <button class="btn sm ghost" @click="again" title="清空这次结果，回到导入表单（已入库的图片不会重复导）">
          <Icon name="rotate-cw" :size="13" />开始新的导入</button>
      </div>
    </div>

    <FolderPicker :open="pickerOpen" title="选择要导入的图片文件夹"
                  :start="isNet ? '' : imp.folder"
                  hint="只列出文件夹；选到图片所在的那一层即可"
                  @close="pickerOpen = false" @picked="pick" />
  </div>
</template>

<style scoped>
/* 颜色一律走主题变量：写死浅色底在深色皮肤下会变成「浅底浅字」。 */
.impbox { display: flex; flex-direction: column; gap: 12px; min-width: 0; }

/* ── 三步：每步一条细标题，收敛成一张「表单」，不再是一堆散装控件 ── */
.grp { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.grp-hd { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.grp-hd b { font-size: 13px; font-weight: 600; color: var(--text); }
.step {
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: 50%; flex: none;
  background: var(--brand); color: #fff; font-size: 11px; font-weight: 600;
  line-height: 1;
}

/* ── 来源：两张带说明的卡片，比两个小胶囊更容易看懂是两种取图方式 ── */
.srctabs { display: flex; gap: 8px; flex-wrap: wrap; }
.srctab {
  display: grid; grid-template-columns: auto 1fr; grid-template-rows: auto auto;
  column-gap: 7px; row-gap: 1px; align-items: center; text-align: left;
  padding: 7px 12px; min-width: 190px;
  border: 1px solid var(--line); border-radius: 10px;
  background: var(--panel); color: var(--text); font: inherit; cursor: pointer;
  transition: border-color .12s, box-shadow .12s, background .12s;
}
.srctab :deep(svg) { grid-row: 1 / span 2; color: var(--muted); }
.srctab .sname { font-size: 12.5px; }
.srctab .sdesc { font-size: 11px; color: var(--muted); }
.srctab:hover:not(:disabled) { border-color: var(--brand); }
.srctab.on { border-color: var(--brand); box-shadow: inset 0 0 0 1px var(--brand); }
.srctab.on .sname { color: var(--brand); font-weight: 600; }
.srctab.on :deep(svg) { color: var(--brand); }
.srctab:disabled { opacity: .55; cursor: not-allowed; }

/* ── 路径行 ── */
.pathrow { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pathin { flex: 1 1 420px; min-width: 0; }
.wd-sm { flex: 0 1 170px; min-width: 120px; }
.netpath { padding: 8px; background: var(--panel); border-radius: 8px; }
.nets { flex: 0 1 190px; min-width: 150px; padding: 7px 8px; color: var(--text);
        background: var(--bg); border: 1px solid var(--line); border-radius: 6px; }
.wd-xs { flex: 0 1 80px; min-width: 64px; }
.tip { margin: 0; font-size: 11.5px; line-height: 1.7; }
.tip code {
  font-size: 11px; padding: 1px 5px; border-radius: 5px;
  color: var(--text); background: var(--panel); border: 1px solid var(--line);
}

/* ── 选项：一行一个，说明收进悬停 ── */
.opts { display: flex; flex-direction: column; gap: 6px; }
.chk { display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px;
  color: var(--text); cursor: pointer; }
.chk input { width: 14px; height: 14px; flex: none; }

/* ── 开始 ── */
.actrow { display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding-top: 2px; }
.go { min-width: 132px; height: 34px; font-size: 13px; border-radius: 9px; }
.go:disabled { opacity: .55; cursor: not-allowed; }

/* ── 状态卡（进度 / 失败 / 结果共用一套骨架）── */
.state { border: 1px solid var(--line); border-radius: 11px;
  padding: 10px 12px; display: flex; flex-direction: column; gap: 8px;
  background: var(--panel); }
.state-hd { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.state-hd b { font-size: 13px; }
.state-ft { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.msg { margin: 0; font-size: 12.5px; color: var(--err); line-height: 1.6; }
.note { margin: 0; }

.state.run { border-color: var(--brand); }
.state.run .state-hd b { color: var(--brand); }
.state.bad { border-color: #f0c9c9; }
.state.bad .state-hd { color: var(--err); }
.state.good { border-color: #cbe8d3; }
.state.good .state-hd { color: var(--ok); }

.bar { height: 8px; border-radius: 5px; background: var(--line); overflow: hidden; }
.bar i { display: block; height: 100%; background: var(--brand);
  border-radius: 5px; transition: width .4s ease; }

/* 结果明细：一行一格的「键 + 值」，比一整句拼接好读 */
.res { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12.5px; }
.res .rk { color: var(--muted); }
.res .rk b { color: var(--text); font-variant-numeric: tabular-nums; }

/* 续导提示条 */
.resumebar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 8px 12px; border-radius: 10px; font-size: 12.5px;
  border: 1px solid var(--brand); background: var(--panel); color: var(--text); }
.resumebar > span { flex: 1 1 260px; }
.resumebar b { color: var(--brand); }

.small { font-size: 11.5px; }
.muted { color: var(--muted); }

@media (max-width: 720px) {
  .pathin { flex: 1 1 100%; }
  .srctab { min-width: 0; flex: 1 1 100%; }
}
</style>
