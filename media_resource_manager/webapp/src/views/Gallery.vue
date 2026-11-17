<script setup>
// 图库（2026-10-28：原来的「隔离区」页签已拆成左边独立一项，见 Quarantine.vue）。
// V0.0.26：「导入本地文件夹」迁到「导入与导出」页（用户要求进口出口放一起），
// 这里只留一句指引，免得同一功能两个入口、两套说法。
import ImageGrid from '../components/ImageGrid.vue'
import Icon from '../components/Icon.vue'
</script>

<template>
  <div class="page">
    <p class="impnote muted small">
      <Icon name="folder-open" :size="13" />
      <span>要把本机文件夹里的图片收进图库，请到
        <router-link :to="{ path: '/export', query: { tab: 'imp' } }">导入与导出</router-link>
        页的「导入」标签操作（原图不移动、不改名）。</span>
    </p>
    <ImageGrid view="gallery" renamable />
  </div>
</template>

<style scoped>
.page { display: grid; gap: 14px; }
.impnote { display: flex; align-items: center; gap: 6px; margin: 0; }
.impnote a { color: var(--brand, #6d5efc); text-decoration: none; }
.impnote a:hover { text-decoration: underline; }
.hd b { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.spacer { flex: 1; }
</style>
