<template>
  <div class="app-header-inner w-full min-w-0">
    <div class="app-header-left">
      <MenuCollapse />
      <AgentChatTitle />
    </div>
    <div v-if="sessionTitleCenter" class="app-header-center" :title="sessionTitleCenter">
      <span class="app-header-session-title">{{ sessionTitleCenter }}</span>
    </div>
    <div class="app-header-right">
      <ThemeMode />
      <FullScreen />
      <UserAvatar />
    </div>
  </div>
</template>

<script setup>
import { storeToRefs } from 'pinia'
import AgentChatTitle from './components/AgentChatTitle.vue'
import MenuCollapse from './components/MenuCollapse.vue'
import FullScreen from './components/FullScreen.vue'
import UserAvatar from './components/UserAvatar.vue'
import ThemeMode from './components/ThemeMode.vue'
import { useAgentChatHeaderStore } from '@/store'

const agentChatHeader = useAgentChatHeaderStore()
const { sessionTitle: sessionTitleCenter } = storeToRefs(agentChatHeader)
</script>

<style scoped>
.app-header-inner {
  display: grid;
  grid-template-columns: 1fr minmax(0, min(520px, 46vw)) 1fr;
  align-items: center;
  width: 100%;
  min-width: 0;
  gap: 8px;
}

.app-header-left {
  display: flex;
  align-items: center;
  min-width: 0;
  overflow: hidden;
}

.app-header-center {
  display: flex;
  justify-content: center;
  min-width: 0;
  max-width: 100%;
  padding: 0 8px;
  pointer-events: none;
  overflow: hidden;
}

.app-header-session-title {
  flex: 1 1 auto;
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 15px;
  font-weight: 600;
  line-height: 1.35;
  color: #0f172a;
  text-align: center;
}

html.dark .app-header-session-title {
  color: rgba(255, 255, 255, 0.9);
}

/* 无中间会话标题时仍只占第 3 列，避免三列网格下第二个子项落到中间列而显得偏左 */
.app-header-right {
  grid-column: 3;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-shrink: 0;
  gap: 0;
}
</style>
