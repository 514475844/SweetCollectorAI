<script setup>
/**
 * 统一空态：图标 + 一句解释 + 可选的一个行动按钮。
 * 文案一律由调用方传入（各处原有措辞保持不变，不在组件里另造词）。
 * 外观定义在全局 style.css 的 .empty-state 里，四套皮肤共用同一套变量。
 */
import Icon from './Icon.vue'

defineProps({
  icon: { type: String, default: 'inbox' },
  title: { type: String, default: '' },
  desc: { type: String, default: '' },
  actionText: { type: String, default: '' },
})
defineEmits(['action'])
</script>

<template>
  <div class="empty-state">
    <span class="es-ic"><Icon :name="icon" :size="20" /></span>
    <span v-if="title" class="es-title">{{ title }}</span>
    <span v-if="desc" class="es-desc">{{ desc }}</span>
    <button v-if="actionText" class="btn sm primary" @click="$emit('action')">
      <Icon name="plus" :size="13" />{{ actionText }}
    </button>
  </div>
</template>
