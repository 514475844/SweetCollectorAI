<template>
  <!-- R249：磁盘级维护四件套。原来这四项只有桌面端有，而且其中两项
       在安装包里根本 import 不到（tools/ 没打包）—— 等于一直是坏的。 -->
  <div class="maint">
    <!-- ① 完整性检查 -->
    <div class="blk">
      <div class="bhead">
        <b>文件完整性检查</b>
        <label class="tiny chk">
          <input type="checkbox" v-model="deep" :disabled="busy" />
          深度检查（逐张解码验证，图多时较慢）
        </label>
      </div>
      <div class="tiny muted">
        只列出问题，不自动删除。默认查「文件缺失 / 0 字节 / 过小」；
        深度检查还能抓出「大小正常但内容截断」的图（下载中断最常见）。
      </div>
      <div class="inline">
        <button class="btn sm" :disabled="busy" @click="startScan">
          <Icon name="search" :size="13" />开始检查</button>
        <button v-if="scanRows.length" class="btn sm danger" :disabled="busy"
                @click="cleanSelected">
          <Icon name="trash" :size="13" />删除勾选的记录（{{ picked.length }}）</button>
        <button v-if="busy" class="btn sm" @click="cancel">
          <Icon name="pause" :size="13" />停止</button>
      </div>

      <div v-if="scanKinds" class="tiny kinds">
        共发现 <b>{{ scanTotal }}</b> 条异常<span v-if="scanText">（{{ scanText }}）</span>
        <span v-if="scanTruncated" class="muted">
          · 下面只列前 {{ scanRows.length }} 条，清理请分批勾选</span>
      </div>
      <div v-if="scanRows.length" class="tbl">
        <div class="thead">
          <span><input type="checkbox" :checked="allPicked"
                       @change="toggleAll" /></span>
          <span>文件名</span><span>问题</span><span>大小</span>
        </div>
        <div v-for="r in scanRows" :key="r.id" class="trow">
          <span><input type="checkbox" :value="r.id" v-model="picked" /></span>
          <span class="mono name" :title="r.path">{{ r.name }}</span>
          <span class="why">{{ r.why }}</span>
          <span class="mono sz">{{ r.size ? fmtBytes(r.size) : '—' }}</span>
        </div>
      </div>
      <p v-else-if="scanDone" class="tiny ok">没有发现异常。</p>
    </div>

    <!-- ② 清理 0KB / .part -->
    <div class="blk">
      <div class="bhead"><b>清理 0 字节与下载残片</b></div>
      <div class="tiny muted">
        只扫软件自己的目录和下载任务的输出目录（不会碰手动指定的相册目录）。
        文件先移到暂存区（<code>data/logs/orphan_trash</code>），确认没问题再自行删除。
      </div>
      <button class="btn sm" :disabled="busy" @click="startOrphans">
        <Icon name="eraser" :size="13" />开始清理</button>
    </div>

    <!-- ③ 批量转 JPG -->
    <div class="blk">
      <div class="bhead"><b>批量转成 JPG</b></div>
      <div class="tiny muted">
        avif / heic 这类格式桌面端与网页端都读不出来。转完会同步更新图库记录，
        原文件删除。<b>先试算规模</b>，看清楚再动手。
      </div>
      <div class="inline">
        <button class="btn sm" :disabled="busy" @click="startConvert(true)">
          <Icon name="calculator" :size="13" />试算规模</button>
        <button class="btn sm primary" :disabled="busy || !convertible"
                @click="startConvert(false)">
          <Icon name="refresh-cw" :size="13" />转换 {{ convertible || '' }} 张</button>
        <button v-if="busy" class="btn sm" @click="cancel">
          <Icon name="pause" :size="13" />停止</button>
      </div>
    </div>

    <!-- ④ 修复图库 -->
    <div class="blk">
      <div class="bhead"><b>修复图库</b></div>
      <div class="tiny muted">
        重新给「被隔离的图」打分，达标的放回图库；顺手清掉指向已消失文件的死链记录。
      </div>
      <label class="tiny chk"><input type="checkbox" v-model="fixThr" :disabled="busy" />
        把任务门槛降到安全值（{{ 0.35 }}）</label>
      <label class="tiny chk"><input type="checkbox" v-model="dropGhosts" :disabled="busy" />
        删除指向已消失文件的记录</label>
      <div class="inline">
        <button class="btn sm primary" :disabled="busy" @click="startRepair">
          <Icon name="wrench" :size="13" />开始修复</button>
        <button v-if="busy" class="btn sm" @click="cancel">
          <Icon name="pause" :size="13" />停止</button>
      </div>
    </div>

    <!-- 进度与结果 -->
    <div v-if="jobPct > 0 || jobMsg" class="jobbox">
      <div class="jobbar"><i :style="{ width: Math.max(2, jobPct) + '%' }" /></div>
      <span class="tiny">{{ jobPct }}% · {{ jobMsg }}</span>
    </div>
    <p v-if="result" class="tiny ok">{{ result }}</p>
    <p v-if="err" class="tiny err">{{ err }}</p>
  </div>
</template>

<script setup>
import { ref, computed, onUnmounted } from 'vue'
import { api } from '../api'
import Icon from './Icon.vue'

const busy = ref(false)
const jobPct = ref(0)
const jobMsg = ref('')
const result = ref('')
const err = ref('')
let timer = null

const deep = ref(false)
const scanRows = ref([])
const scanKinds = ref(null)
const scanTotal = ref(0)
const scanText = ref('')
const scanTruncated = ref(false)
const scanDone = ref(false)
const picked = ref([])
const convertible = ref(0)

const fixThr = ref(false)
const dropGhosts = ref(true)

const allPicked = computed(
  () => scanRows.value.length > 0 && picked.value.length === scanRows.value.length)

function fmtBytes(n) {
  const v = Number(n) || 0
  if (!v) return '0 B'
  if (v < 1024) return v + ' B'
  if (v < 1048576) return (v / 1024).toFixed(1) + ' KB'
  return (v / 1048576).toFixed(1) + ' MB'
}

function toggleAll(e) {
  picked.value = e.target.checked ? scanRows.value.map((r) => r.id) : []
}

function reset() {
  err.value = ''
  result.value = ''
  jobPct.value = 0
  jobMsg.value = ''
}

// 统一跑一个后台作业，直到 done/failed
function run(kind, opts, onDone) {
  reset()
  busy.value = true
  jobMsg.value = '准备中…'
  api.maintStart(kind, opts).then((r) => {
    if (!r.ok) { busy.value = false; err.value = r.error || '无法开始'; return }
    timer = setInterval(async () => {
      try {
        const j = await api.maintJob(r.job)
        if (!j.ok) {
          busy.value = false; clearInterval(timer); timer = null
          err.value = j.error || '作业已失效'
          return
        }
        jobPct.value = Number(j.pct || 0)
        jobMsg.value = j.message || ''
        if (j.state === 'done') {
          busy.value = false; clearInterval(timer); timer = null
          onDone(j.result || {}, j.message || '完成')
        } else if (j.state === 'failed') {
          busy.value = false; clearInterval(timer); timer = null
          err.value = j.error || '执行失败'
        }
      } catch (e) { /* 轮询偶发失败不打断 */ }
    }, 1000)
  }).catch((e) => { busy.value = false; err.value = e.message })
}

function cancel() {
  api.maintCancel().then((r) => {
    jobMsg.value = r.message || '已请求停止'
    if (!r.ok) err.value = r.error
  }).catch((e) => (err.value = e.message))
}

function startScan() {
  scanRows.value = []; picked.value = []; scanKinds.value = null
  scanDone.value = false; scanTruncated.value = false
  run('scan', { deep: deep.value }, (res, msg) => {
    scanRows.value = res.items || []
    scanKinds.value = res.kinds || {}
    scanTotal.value = Object.values(scanKinds.value).reduce((a, b) => a + b, 0)
    scanText.value = Object.entries(scanKinds.value)
      .map(([k, n]) => `${k} ${n}`).join('、')
    scanTruncated.value = !!res.truncated
    scanDone.value = true
    result.value = `${msg}（检查 ${res.checked || 0} 张，用时 ${res.elapsed || 0} 秒）`
  })
}

function cleanSelected() {
  if (!picked.value.length) { err.value = '先勾选要清理的记录'; return }
  if (!confirm(`确认删除勾选的 ${picked.value.length} 条图库记录？\n`
    + '只删数据库记录，磁盘上的文件不动。')) return
  api.maintClean(picked.value).then((r) => {
    if (!r.ok) { err.value = r.error; return }
    result.value = `已删除 ${r.deleted} 条记录`
    const gone = new Set(picked.value)
    scanRows.value = scanRows.value.filter((x) => !gone.has(x.id))
    picked.value = []
  }).catch((e) => (err.value = e.message))
}

function startOrphans() {
  run('orphans', {}, (res, msg) => {
    result.value = `${msg}（扫了 ${res.scanned || 0} 个文件，${res.dirs || 0} 个目录）`
  })
}

function startConvert(dry) {
  if (!dry && !confirm(`确认转换 ${convertible.value} 张图片为 JPG？\n`
    + '转换后原文件会被删除，图库记录会同步更新。')) return
  run('convert', { dry_run: dry }, (res, msg) => {
    if (res.dry_run) {
      convertible.value = res.to_convert || 0
      result.value = `可以转换 ${convertible.value} 张`
        + (res.skipped ? `（${res.skipped} 张因同名文件已存在而跳过）` : '')
    } else {
      convertible.value = 0
      result.value = `${msg}（失败 ${res.failed || 0}，跳过 ${res.skipped || 0}，`
        + `文件已缺失 ${res.missing || 0}）`
    }
  })
}

function startRepair() {
  run('repair', {
    fix_threshold: fixThr.value,
    restore_quarantine: true,
    drop_ghosts: dropGhosts.value,
  }, (res, msg) => {
    result.value = `${msg}；可见图 ${res.visible_before || 0} → ${res.visible_after || 0} 张`
  })
}

onUnmounted(() => {
  // 离开设置页必须停轮询，否则组件销毁后还在打接口
  if (timer) { clearInterval(timer); timer = null }
})
</script>

<style scoped>
.maint { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.blk { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.bhead { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.bhead b { font-size: 13px; }
.chk { display: inline-flex; align-items: center; gap: 5px; }
.kinds { margin-top: 2px; }
.tbl { border: 1px solid var(--line, #e6e1f5); border-radius: 8px; overflow: auto;
  max-height: 300px; }
.thead, .trow {
  display: grid; grid-template-columns: 28px 1fr 120px 80px; gap: 8px;
  align-items: center; padding: 5px 8px; font-size: 12px;
}
.thead { position: sticky; top: 0; background: var(--panel2, #f6f3ff);
  font-weight: 600; }
.trow { border-top: 1px solid var(--line, #eee9fa); }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.why { color: var(--err, #c0392b); }
.sz { text-align: right; opacity: .8; }
.btn:disabled { opacity: .5; cursor: not-allowed; }
</style>
