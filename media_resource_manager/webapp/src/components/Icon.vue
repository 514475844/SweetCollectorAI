<script setup>
// 统一图标组件（Lucide 线条图标 + 可选的动画图标，全部内联 SVG，不联网）。
// - name 找不到时不报错、也不留空白：退化成一个小圆点，避免布局跳。
// - 颜色跟文字走（currentColor），所以主题、深色皮肤、禁用态全都自动正确。
// - anim：传语义键（spin / dl / pulse…，见 icons-anim.js）就在「动态图标」开启时
//   换成带动画的版本；关掉开关或传入空 → 自动退回静态图标，**语义不变**。
//   所以调用点只管说"这里在加载"，不必关心用户选的是动还是静。
import { computed } from 'vue'
import { ICONS, ALIASES } from '../icons'
import { ANIM, ANIM_FALLBACK } from '../icons-anim'
import { animatedIcons, prefersReduced } from '../uiPrefs'

const props = defineProps({
  name: { type: String, default: '' },
  size: { type: [Number, String], default: 16 },
  stroke: { type: [Number, String], default: 1.8 },   // 细一点更接近 qBittorrent 的观感
  // 语义键。留空 = 这个位置不需要动画。
  anim: { type: String, default: '' },
})

// 动画是否真的可用：用户开着 + 系统没要求减少动效 + 这个键确实有素材。
// ⚠ 三个条件缺一不可。少了 prefersReduced 会无视无障碍设置；
//   少了"素材存在"会因为在模板里写错一个键就整块空白（静默失败最难查）。
const animBody = computed(() => {
  if (!props.anim) return ''
  if (!animatedIcons.value || prefersReduced) return ''
  return ANIM[props.anim] || ''
})

const staticBody = computed(() => {
  // 关掉动画时，优先用该动画语义对应的静态图标（比如 spin → refresh-cw）；
  // 调用方显式给了 name 就用 name。
  const fb = ANIM_FALLBACK[props.anim] || ''
  return ICONS[props.name] || ICONS[ALIASES[props.name]]
    || (fb ? (ICONS[fb] || '') : '')
    || ''
})

const body = computed(() => animBody.value || staticBody.value)
const px = computed(() => `${props.size}px`)
</script>

<template>
  <svg
    v-if="body" class="icon" :class="{ anim: !!animBody }"
    :width="px" :height="px" viewBox="0 0 24 24"
    :stroke-width="stroke" aria-hidden="true" focusable="false" v-html="body" />
  <svg v-else class="icon" :width="px" :height="px" viewBox="0 0 24 24" aria-hidden="true">
    <circle cx="12" cy="12" r="3" fill="currentColor" />
  </svg>
</template>

<style scoped>
.icon {
  display: inline-block;
  vertical-align: -0.15em;
  flex: 0 0 auto;      /* 放在 flex 行里不被压缩变形 */
  pointer-events: none;
}
/* 动画图标自带 SMIL，不需要我们驱动；这里只保留一个"我还活着"的类名，
   方便将来给某些位置加动画速度倍率（比如加载慢时放慢）。 */
.icon.anim { will-change: transform; }
</style>
