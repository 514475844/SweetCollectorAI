<script setup>
/**
 * 目录选择器：给「从文件夹导入图库」「任务保存目录」「设置里的各种目录」用。
 *
 * 为什么要有它：浏览器出于安全**拿不到真实文件路径**，网页端没法像桌面端
 * 那样弹系统对话框。只能由服务端列目录、用户点选。
 *
 * 2026-10-29（新需求）：原来这个接口只在本机开放，注释里写的是「远程访问时
 * 列出服务器的文件系统帮不上忙」。Docker/NAS 部署之后这个判断就不成立了 ——
 * 用户在另一台电脑上打开网页，要选的恰恰是**服务器（NAS）上**的那个目录。
 * 现在改为：远程一样能浏览，但界面必须**说清这是哪台机器**，否则用户会拿它
 * 当自己的电脑去找 D:\图片（找不到，然后以为功能坏了）。
 *
 * 三个入口文案的区别只靠 `title` + `where` 表达：
 *   · 本机打开 → 盘符（C: / D:）
 *   · 远程/NAS → 容器里挂载的卷（/data、/mnt/...）
 */
import { ref, watch } from 'vue'
import { api } from '../api'
import Icon from './Icon.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  start: { type: String, default: '' },
  title: { type: String, default: '选择文件夹' },
  hint: { type: String, default: '只列出文件夹，不列文件' },
})
const emit = defineEmits(['close', 'picked'])

const cur = ref('')
const parent = ref('')
const items = ref([])
const err = ref('')
const loading = ref(false)
const where = ref('')        // 运行软件的机器名（后端给）
const remote = ref(false)    // 这次请求是不是从别的机器发来的

async function load(path) {
  loading.value = true
  err.value = ''
  try {
    const r = await api.fsList(path)
    if (!r.ok) { err.value = r.error || '列目录失败'; return }
    cur.value = r.path || ''
    parent.value = r.parent || ''
    items.value = r.items || []
    if (r.where) where.value = r.where
    remote.value = !!r.remote
  } catch (e) {
    err.value = e.message
  } finally {
    loading.value = false
  }
}

watch(() => props.open, (v) => {
  if (v) load(props.start || '')
})
</script>

<template>
<!-- V0.0.58：挂到 body，避免 fixed 遮罩被 .page/过渡的 transform 困住盖不住顶栏标题 -->
    <Teleport to="body">
  <div v-if="open" class="fp-mask" @click.self="$emit('close')">
    <div class="fp card">
      <div class="fp-hd">
        <Icon name="folder" :size="15" />
        <b>{{ title }}</b>
        <button class="fp-x" title="关闭" @click="$emit('close')">
          <Icon name="x" :size="14" />
        </button>
      </div>

      <!-- 归属必须写死在这里：不写，用户就会在自己的 D 盘里找 NAS 的目录 -->
      <div class="fp-where">
        <Icon name="server" :size="12" />
        <span v-if="where">位置：<b>{{ where }}</b>（运行软件的这台机器{{ remote ? '，并非当前浏览器所在电脑' : '' }}）</span>
        <span v-else>位置：运行软件的这台机器</span>
      </div>

      <div class="fp-path mono" :title="cur || '磁盘 / 挂载点列表'">
        {{ cur || '（选择一个位置）' }}
      </div>

      <p v-if="err" class="fp-err">{{ err }}</p>

      <div class="fp-list">
        <button v-if="parent" class="fp-item up" @click="load(parent)">
          <Icon name="arrow-up" :size="13" /><span>上一级</span>
        </button>
        <button v-for="d in items" :key="d.path" class="fp-item"
                :title="d.path" @click="load(d.path)">
          <Icon name="folder" :size="13" /><span>{{ d.name }}</span>
        </button>
        <p v-if="!loading && !items.length && !parent" class="fp-empty muted">
          这里没有子文件夹
        </p>
        <p v-if="loading" class="fp-empty muted">正在读取…</p>
      </div>

      <p v-if="!cur" class="fp-tip muted tiny">
        容器部署时只能看到<b>挂载进容器</b>的目录：先在 compose 里加
        <code>- /NAS路径:/mnt/xxx</code>，这里才会出现 /mnt/xxx。
      </p>

      <div class="fp-ft">
        <span class="muted tiny">{{ hint }}</span>
        <span class="sp"></span>
        <button class="btn sm" @click="$emit('close')">取消</button>
        <button class="btn sm primary" :disabled="!cur" @click="$emit('picked', cur)">
          <Icon name="check" :size="13" />就用这个文件夹
        </button>
      </div>
    </div>
  </div>
  </Teleport>
</template>

<style scoped>
.fp-mask {
  position: fixed; inset: 0; z-index: 60;
  background: rgba(15, 12, 30, .38);
  display: flex; align-items: center; justify-content: center; padding: 20px;
}
.fp {
  width: min(560px, 96vw); max-height: 78vh;
  display: flex; flex-direction: column; padding: 0; overflow: hidden;
}
.fp-hd {
  display: flex; align-items: center; gap: 8px;
  padding: 12px 14px; border-bottom: 1px solid var(--line);
}
.fp-hd b { flex: 1; font-size: 14px; }
.fp-x {
  border: 0; background: transparent; color: var(--muted);
  cursor: pointer; padding: 2px; border-radius: 6px;
}
.fp-x:hover { background: var(--hover, #f2eefc); color: var(--text); }
.fp-where {
  display: flex; align-items: center; gap: 5px;
  padding: 6px 14px; font-size: 12px; color: var(--muted);
  border-bottom: 1px dashed var(--line);
  flex-wrap: wrap;
}
.fp-where b { color: var(--text); }
.fp-path {
  padding: 8px 14px; font-size: 12px; color: var(--muted);
  background: var(--panel2, #faf8ff); border-bottom: 1px solid var(--line);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.fp-err { margin: 8px 14px 0; color: var(--err); font-size: 12px; }
.fp-list {
  flex: 1; overflow: auto; padding: 8px;
  display: flex; flex-direction: column; gap: 2px; min-height: 160px;
}
.fp-item {
  display: flex; align-items: center; gap: 8px; text-align: left;
  padding: 7px 10px; border: 0; border-radius: 8px; cursor: pointer;
  background: transparent; color: var(--text); font-size: 13px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.fp-item span { overflow: hidden; text-overflow: ellipsis; }
.fp-item:hover { background: var(--hover, #f2eefc); }
.fp-item.up { color: var(--muted); }
.fp-empty { margin: 10px; font-size: 12px; }
.fp-tip { margin: 0 14px 8px; line-height: 1.6; }
.fp-tip code { background: #f4f2ff; padding: 1px 4px; border-radius: 4px; }
.fp-ft {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 14px; border-top: 1px solid var(--line);
}
.fp-ft .sp { flex: 1; }
</style>
