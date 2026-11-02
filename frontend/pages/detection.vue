<template>
  <WechatDetectionPanel @close="onClose" />
</template>

<script setup>
const onClose = () => {
  if (!process.client || typeof window === 'undefined') return
  // 独立窗口打开时直接关闭窗口；被直接导航到本页时回退到上下文中心
  if (window.opener) {
    window.close()
    return
  }
  navigateTo('/')
}
</script>