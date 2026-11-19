<script setup>
// 图库（2026-10-28：原来的「隔离区」页签已拆成左边独立一项，见 Quarantine.vue）。
// V0.0.26：「导入本地文件夹」迁到「导入与导出」页（用户要求进口出口放一起）。
// V0.0.62（用户）：
//   · 那行「请到导入与导出页导入」的指引句删掉 —— 侧栏本来就有入口，不重复说；
//   · AI 识别控件并入图库工具栏白框（见 ImageGrid.vue），原独立识别卡组件已删除。
import ImageGrid from '../components/ImageGrid.vue'
</script>

<template>
  <div class="page">
    <ImageGrid view="gallery" renamable />
  </div>
</template>

<style scoped>
.page { display: grid; gap: 14px; }
</style>
