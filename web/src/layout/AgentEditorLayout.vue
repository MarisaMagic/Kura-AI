<template>
  <div class="agent-editor-shell">
    <header class="agent-editor-shell__bar">
      <n-button
        quaternary
        circle
        size="medium"
        :title="$t('views.agents.title_back')"
        :aria-label="$t('views.agents.title_back')"
        @click="goBack"
      >
        <TheIcon icon="material-symbols:arrow-back-rounded" :size="22" />
      </n-button>
    </header>
    <main class="agent-editor-shell__main">
      <div class="agent-editor-shell__scroll">
        <router-view />
      </div>
    </main>
  </div>
</template>

<script setup>
import { NButton } from 'naive-ui'
import { useRouter } from 'vue-router'
import TheIcon from '@/components/icon/TheIcon.vue'

const router = useRouter()

/** 优先回到进入本页前的路由；无历史（如直接打开/刷新）时回智能体中心 */
function goBack() {
  if (window.history.state?.back != null) {
    router.back()
  } else {
    router.push({ path: '/agent-hub' })
  }
}
</script>

<style scoped>
.agent-editor-shell {
  display: flex;
  flex-direction: column;
  box-sizing: border-box;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  background: var(--n-color);
}
.agent-editor-shell__bar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid var(--n-divider-color);
  background: var(--n-color);
}
.agent-editor-shell__main {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.agent-editor-shell__scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  -webkit-overflow-scrolling: touch;
}
</style>
