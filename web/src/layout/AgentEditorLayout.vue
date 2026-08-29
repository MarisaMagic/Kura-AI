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
  background: #ffffff;
}
.agent-editor-shell__bar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid var(--n-divider-color);
  background: #ffffff;
}
html.dark .agent-editor-shell,
html.dark .agent-editor-shell__bar {
  background: #18181c;
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
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.agent-editor-shell__scroll :deep(> *) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

@media (max-width: 1023px) {
  .agent-editor-shell__scroll {
    overflow: auto;
    -webkit-overflow-scrolling: touch;
  }
}

.agent-editor-shell__scroll {
  scrollbar-width: thin;
  scrollbar-color: rgba(15, 23, 42, 0.14) transparent;
}
html.dark .agent-editor-shell__scroll {
  scrollbar-color: rgba(255, 255, 255, 0.12) transparent;
}
.agent-editor-shell__scroll::-webkit-scrollbar {
  width: 8px;
}
.agent-editor-shell__scroll::-webkit-scrollbar-button {
  display: none;
  width: 0;
  height: 0;
}
.agent-editor-shell__scroll::-webkit-scrollbar-track {
  background: transparent;
}
.agent-editor-shell__scroll::-webkit-scrollbar-thumb {
  background-color: rgba(15, 23, 42, 0.14);
  border-radius: 100px;
  border: 2px solid transparent;
  background-clip: padding-box;
}
.agent-editor-shell__scroll::-webkit-scrollbar-thumb:hover {
  background-color: rgba(15, 23, 42, 0.22);
}
html.dark .agent-editor-shell__scroll::-webkit-scrollbar-thumb {
  background-color: rgba(255, 255, 255, 0.12);
}
html.dark .agent-editor-shell__scroll::-webkit-scrollbar-thumb:hover {
  background-color: rgba(255, 255, 255, 0.2);
}
</style>
