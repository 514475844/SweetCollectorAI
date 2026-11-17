<script setup>
/**
 * V0.0.16 · 数据维护
 *
 * 把桌面「数据 / 维护」菜单里、网页此前没有的动作收在这一页，全部复用
 * 后端 site_ops 的纯逻辑（与桌面端同一份口径）：
 *   数据存放位置 / 回填本地图片 / 备份立即同步 / 全面清理。
 *
 * 破坏性操作一律「先勾选、再确认、后端再校验一次」，不提供一键真删。
 */
import { ref, reactive, onMounted } from 'vue'
import { api } from '../api'
import { useShellRefresh } from '../shellVue'
import Icon from '../components/Icon.vue'
import FolderPicker from '../components/FolderPicker.vue'

const loc = ref(null)
const err = ref('')
const info = ref('')
const busy = ref('')
// V0.0.51：用户已经在「备份同步」这一页了，还让他去「设置 → 采集与保存」
// 找同步目录，属于踢皮球。这里直接给输入框 + 浏览 + 保存，写的是同一份
// backup_sync_dir 配置，两处永远一致。
const syncDir = ref('')
const syncPicker = ref(false)
const syncSaving = ref(false)

async function loadSyncDir() {
  try {
    const s = await api.settings()
    syncDir.value = Array.isArray(s.backup_sync_dir)
      ? (s.backup_sync_dir[0] || '')
      : (s.backup_sync_dir || '')
  } catch (e) { /* 读不到就留空，不影响本页其他操作 */ }
}

async function saveSyncDir() {
  syncSaving.value = true
  err.value = ''
  try {
    const r = await api.saveSettings({ backup_sync_dir: syncDir.value.trim() })
    if (r && (r.ok === false || r.error)) throw new Error(r.error || '保存失败')
    info.value = syncDir.value.trim()
      ? '同步目录已保存，设置页里也是这一个目录'
      : '已清空同步目录，现在只做本地备份'
    setTimeout(() => (info.value = ''), 3500)
    await loadSyncDir()
  } catch (e) { err.value = e.message || '保存同步目录失败' }
  finally { syncSaving.value = false }
}

const backfill = reactive({ task_id: '', re_filter_ai: false })
const items = ref([])
const plan = ref(new Set())

async function load() {
  try {
    const [a, b] = await Promise.all([
      api.siteDataLocation(),
      api.siteCleanupItems(),
    ])
    loc.value = a
    items.value = b.items || []
    plan.value = new Set(items.value.filter((x) => x.checked).map((x) => x.key))
    err.value = ''
  } catch (e) {
    err.value = e.message || '读取维护信息失败'
  }
}

function togglePlan(k) {
  const s = new Set(plan.value)
  s.has(k) ? s.delete(k) : s.add(k)
  plan.value = s
}

async function doBackfill() {
  busy.value = 'backfill'
  err.value = ''
  const payload = { re_filter_ai: backfill.re_filter_ai }
  if (backfill.task_id !== '') payload.task_id = Number(backfill.task_id)
  try {
    const r = await api.siteBackfill(payload)
    if (!r.ok) throw new Error(r.error || '回填未完成')
    info.value = r.note || '回填完成'
  } catch (e) {
    err.value = e.message || '回填本地图片失败'
  } finally {
    busy.value = ''
  }
}

async function doSync() {
  busy.value = 'sync'
  err.value = ''
  try {
    const r = await api.siteBackupSync({})
    if (!r.ok) throw new Error(r.error || '同步未完成')
    info.value = r.note || '备份同步完成'
  } catch (e) {
    err.value = e.message || '备份同步失败'
  } finally {
    busy.value = ''
  }
}

async function doCleanup() {
  const keys = [...plan.value]
  if (!keys.length) { err.value = '请至少勾选一项要清理的内容。'; return }
  const labels = items.value.filter((x) => keys.includes(x.key)).map((x) => x.label)
  const ok = confirm(
    '即将执行全面清理，以下已勾选内容将被永久删除且不可恢复：\n\n' +
    labels.map((x) => '· ' + x).join('\n') +
    '\n\n任务、图片、学习记录与站点规则一旦清除无法还原。请确认已备份重要数据。')
  if (!ok) return
  busy.value = 'cleanup'
  err.value = ''
  try {
    const r = await api.siteCleanup(keys)
    if (!r.ok) throw new Error(r.error || '全面清理未完成')
    info.value = r.note || '全面清理完成'
    await load()
  } catch (e) {
    err.value = e.message || '全面清理失败'
  } finally {
    busy.value = ''
  }
}

onMounted(() => { load(); loadSyncDir() })
useShellRefresh(() => { load(); loadSyncDir() })
</script>

<template>
  <div class="page">
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="info" class="goodbar">{{ info }}</p>

    <div class="card sec">
      <div class="sec-hd">
        <Icon name="folder-open" :size="15" />
        <h3>数据存放位置</h3>
        <span class="muted small">只读展示，不会移动或删除任何文件</span>
      </div>
      <div v-if="loc" class="kv">
        <span>数据根目录</span><b class="mono">{{ loc.root }}</b>
        <span>数据库</span><b class="mono">{{ loc.database }}</b>
        <span>站点规则</span><b class="mono">{{ loc.rules }}</b>
        <span>图片库</span><b class="mono">{{ loc.library }}</b>
        <span>隔离区</span><b class="mono">{{ loc.quarantine }}</b>
        <span>日志</span><b class="mono">{{ loc.logs }}</b>
        <span>缩略图</span><b class="mono">{{ loc.thumbs }}</b>
      </div>
      <p class="muted tiny">{{ loc?.note }}</p>
    </div>

    <div class="card sec">
      <div class="sec-hd">
        <Icon name="refresh-cw" :size="15" />
        <h3>回填本地图片到图库</h3>
        <span class="muted small">扫描各任务保存目录，补写磁盘已有但尚未登记的图片</span>
      </div>
      <div class="form">
        <label class="row">
          <span>任务编号</span>
          <input v-model="backfill.task_id" type="number" min="0" placeholder="留空则扫描全部任务" />
        </label>
        <label class="chk">
          <input v-model="backfill.re_filter_ai" type="checkbox" />
          重新执行 AI 主题词过滤（耗时较长，不符合条件的图片将进入审核）
        </label>
      </div>
      <div class="sec-ft">
        <button class="btn sm primary" :disabled="busy === 'backfill'" @click="doBackfill">
          <Icon name="refresh-cw" :size="13" />开始回填
        </button>
        <span class="muted tiny">已在库中的图片自动跳过，不会重复登记。</span>
      </div>
    </div>

    <div class="card sec">
      <div class="sec-hd">
        <Icon name="hard-drive" :size="15" />
        <h3>备份同步</h3>
        <span class="muted small">按设置中的同步目录，立即复制一份</span>
      </div>
      <p class="muted small">数据库采用在线快照，配置与学习记录均加密为 .mrmx 后同步。</p>
      <div class="syncrow">
        <label>同步目录</label>
        <input v-model.trim="syncDir" type="text" spellcheck="false"
               placeholder="留空表示只做本地备份，例如 D:\备份\Sweet" />
        <button class="btn sm" @click="syncPicker = true">
          <Icon name="folder" :size="13" />浏览
        </button>
        <button class="btn sm primary" :disabled="syncSaving" @click="saveSyncDir">
          <Icon name="save" :size="13" />{{ syncSaving ? '保存中…' : '保存目录' }}</button>
      </div>
      <p class="muted tiny">这里改的就是设置里的「备份同步目录」，保存后两处同步生效；留空则只保留本地备份。</p>
      <div class="sec-ft">
        <button class="btn sm" :disabled="busy === 'sync' || !syncDir" @click="doSync">
          <Icon name="hard-drive" :size="13" />立即同步
        </button>
        <span class="muted tiny">{{ syncDir ? '按上面的目录，立即复制一份' : '先填同步目录再同步' }}</span>
      </div>
      <FolderPicker :open="syncPicker" title="选择备份同步目录"
                    :start="syncDir" hint="选一个网盘、移动盘或本机目录"
                    @close="syncPicker = false"
                    @picked="(p) => { syncDir = p; syncPicker = false }" />
    </div>

    <div class="card sec danger-sec">
      <div class="sec-hd">
        <Icon name="alert-triangle" :size="15" />
        <h3>全面清理</h3>
        <span class="muted small">按勾选清单重置到新装状态，属于不可撤销操作</span>
      </div>
      <div class="items">
        <label v-for="it in items" :key="it.key" class="item" :class="{ risky: it.risky }">
          <input type="checkbox" :checked="plan.has(it.key)" @change="togglePlan(it.key)" />
          <span>
            <b>{{ it.label }}</b>
            <span class="muted tiny">{{ it.desc }}</span>
          </span>
        </label>
      </div>
      <div class="sec-ft">
        <button class="btn sm danger" :disabled="busy === 'cleanup' || !plan.size" @click="doCleanup">
          <Icon name="trash" :size="13" />按勾选执行清理
        </button>
        <span class="muted tiny">未勾选项原样保留；执行前会再次确认。</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* V0.0.27：块间距统一交给 page 网格（14px），不再用 sec 自己的 margin-bottom */
.page { display: grid; gap: 14px; align-items: start; }
.sec { padding: 16px 18px; }
.danger-sec { border-color: #f0c9c9; }
.sec-hd { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.sec-hd h3 { margin: 0; font-size: 15px; }
.sec-ft { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
.btn.sm { padding: 5px 12px; font-size: 12px; border-radius: 8px; }
.kv { display: grid; grid-template-columns: 96px 1fr; gap: 6px 12px; font-size: 12px; align-items: center; }
.kv span { color: var(--muted); }
.kv b { font-weight: 500; word-break: break-all; }
.form { display: flex; flex-direction: column; gap: 10px; }
.row { display: flex; align-items: center; gap: 10px; font-size: 13px; }
.row > span:first-child { width: 88px; flex: none; color: var(--muted); }
.row input { border: 1px solid var(--line); border-radius: 8px; padding: 5px 9px; background: #fff; min-width: 180px; font-size: 13px; }
.chk { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
.syncrow { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.syncrow > label { width: 64px; flex: none; color: var(--muted); font-size: 12px; }
.syncrow > input { flex: 1 1 260px; min-width: 180px; border: 1px solid var(--line); border-radius: 8px; padding: 6px 9px; background: #fff; font-size: 12px; }
.items { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 8px; }
.item { display: flex; gap: 8px; align-items: flex-start; border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; font-size: 12px; }
.item.risky { border-color: #f0c9c9; }
.item span { display: flex; flex-direction: column; gap: 2px; }
.small { font-size: 12px; }
.tiny { font-size: 11px; }
.err { color: var(--err); margin: 0 0 10px; }
.goodbar { color: var(--brand); margin: 0 0 10px; }
</style>
