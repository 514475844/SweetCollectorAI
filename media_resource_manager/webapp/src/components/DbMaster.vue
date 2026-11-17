<script setup>
// V0.0.17 · 数据库主密码（/api/db/master）。密码框一律 type=password，
// 读接口只返回「是否已设置」，界面绝不回显已保存的密码。
import { ref, onMounted } from 'vue'
import { api } from '../api'
import Icon from './Icon.vue'

const isSet = ref(false)
const note = ref('')
const pw = ref('')
const cf = ref('')
const err = ref('')
const info = ref('')
const busy = ref(false)

async function load() {
  try {
    const r = await api.dbMaster()
    if (!r.ok) throw new Error(r.error || '读取主密码状态失败，请稍后重试。')
    isSet.value = !!r.set
    note.value = r.note || ''
  } catch (e) {
    err.value = e.message || '读取主密码状态失败，请稍后重试。'
  }
}

async function submit() {
  err.value = ''
  info.value = ''
  if (!pw.value.trim()) { err.value = '请填写主密码。'; return }
  if (pw.value !== cf.value) {
    err.value = '两次输入的主密码不一致，请重新填写。'
    return
  }
  busy.value = true
  try {
    const r = await api.dbMasterSet(pw.value, cf.value)
    if (!r.ok) throw new Error(r.error || '设置主密码失败，请稍后重试。')
    pw.value = ''
    cf.value = ''
    info.value = r.note || '主密码已设置并在本次运行生效。'
    await load()
  } catch (e) {
    err.value = e.message || '设置主密码失败，请稍后重试。'
  } finally { busy.value = false }
}

onMounted(load)
</script>

<template>
  <div class="dbm">
    <div class="bar">
      <b><Icon name="key-round" :size="14" />主密码</b>
      <span class="muted small">当前状态：{{ isSet ? '已设置' : '尚未设置' }}</span>
    </div>
    <div class="inline">
      <input v-model="pw" type="password" placeholder="请输入主密码" autocomplete="new-password" />
      <input v-model="cf" type="password" placeholder="请再次输入主密码" autocomplete="new-password" />
      <button class="btn sm" :disabled="busy" @click="submit">
        <Icon name="lock" :size="13" />{{ busy ? '正在设置…' : '设置主密码' }}</button>
    </div>
    <p v-if="err" class="err small">{{ err }}</p>
    <p v-if="info" class="ok small">{{ info }}</p>
    <p class="muted tiny">{{ note || '主密码仅保存在运行内存中，界面不会回显已保存的密码。' }}</p>
  </div>
</template>

<style scoped>
.dbm { display: grid; gap: 6px; margin-top: 8px; padding: 10px 12px;
       border: 1px solid var(--line, rgba(0,0,0,.08)); border-radius: 10px; }
.bar { display: flex; align-items: center; justify-content: space-between; }
.bar b { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.inline { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.inline input { width: 160px; }
.tiny { font-size: 11px; }
.err { color: var(--err, #b91c1c); }
.ok { color: var(--brand, #6d4bd6); }
</style>
