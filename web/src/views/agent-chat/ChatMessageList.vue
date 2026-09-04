<template>
  <n-virtual-list
    ref="listRef"
    class="agent-chat-feed-virtual"
    :style="{ height: '100%' }"
    :items="messages"
    :item-size="180"
    item-resizable
    key-field="id"
    :padding-top="24"
    :padding-bottom="76"
    :items-style="{ paddingLeft: '20px', paddingRight: '20px', boxSizing: 'border-box' }"
    @scroll="onNativeScroll"
  >
    <template #default="{ item: m }">
      <ChatMessageItem
        :message="m"
        :chat-agent-id="chatAgentId"
        :session-id="sessionId"
        :agent-avatar-src="agentAvatarSrc"
        :agent-name="agentName"
        :sending="sending"
        :switching-branch="switchingBranch"
        :confirming-mcp-ids="confirmingMcpIds"
        @toggle-thinking="$emit('toggle-thinking', $event)"
        @mcp-approve="(msg, item, approve) => $emit('mcp-approve', msg, item, approve)"
        @md-click="$emit('md-click', $event)"
        @switch-version="(msg, dir) => $emit('switch-version', msg, dir)"
        @regenerate="$emit('regenerate', $event)"
        @copy-plain="$emit('copy-plain', $event)"
        @copy-md="$emit('copy-md', $event)"
      />
    </template>
  </n-virtual-list>
</template>

<script setup>
import { ref } from 'vue'
import { NVirtualList } from 'naive-ui'
import ChatMessageItem from './ChatMessageItem.vue'

defineProps({
  messages: { type: Array, default: () => [] },
  chatAgentId: { type: Number, default: 0 },
  sessionId: { type: String, default: '' },
  agentAvatarSrc: { type: String, default: '' },
  agentName: { type: String, default: '—' },
  sending: { type: Boolean, default: false },
  switchingBranch: { type: Boolean, default: false },
  confirmingMcpIds: { type: Object, default: () => new Set() },
})

const emit = defineEmits([
  'toggle-thinking',
  'mcp-approve',
  'md-click',
  'switch-version',
  'regenerate',
  'copy-plain',
  'copy-md',
  'scroll',
])

const listRef = ref(null)

function getScrollEl() {
  const inst = listRef.value
  if (!inst) return null
  if (typeof inst.getScrollContainer === 'function') {
    return inst.getScrollContainer() || null
  }
  return inst.listElRef || inst.$el || null
}

function onNativeScroll(e) {
  emit('scroll', e)
}

defineExpose({ getScrollEl })
</script>

<style src="./agent-chat-ui.css"></style>
<style>
.agent-chat-feed-virtual,
.agent-chat-feed-virtual.n-scrollbar,
.agent-chat-feed-virtual .n-scrollbar-container,
.agent-chat-feed-virtual .v-vl {
  width: 100% !important;
  max-width: none !important;
  box-sizing: border-box;
  height: 100%;
}

.agent-chat-feed-virtual .n-virtual-list__content,
.agent-chat-feed-virtual .v-vl-items {
  width: 100%;
  max-width: none;
  box-sizing: border-box;
}

/* Naive 浮层轨道会内缩；改回原生滚动条，贴主栏（窗口）最右侧 */
.agent-chat-feed-virtual .n-scrollbar-rail {
  display: none !important;
}

.agent-chat-feed-virtual > *:not(.n-scrollbar-rail),
.agent-chat-feed-virtual .v-vl {
  scrollbar-width: thin !important;
  scrollbar-color: rgba(15, 23, 42, 0.14) transparent;
  /* 流式内容每帧都在整体替换 innerHTML，浏览器滚动锚定只会添乱，
     由粘底逻辑与虚拟列表补偿接管滚动位置 */
  overflow-anchor: none;
}

.agent-chat-feed-virtual > *:not(.n-scrollbar-rail)::-webkit-scrollbar,
.agent-chat-feed-virtual .v-vl::-webkit-scrollbar {
  width: 8px !important;
  height: 8px !important;
  display: block !important;
}

.agent-chat-feed-virtual > *:not(.n-scrollbar-rail)::-webkit-scrollbar-button,
.agent-chat-feed-virtual .v-vl::-webkit-scrollbar-button {
  display: none;
  width: 0;
  height: 0;
}

.agent-chat-feed-virtual > *:not(.n-scrollbar-rail)::-webkit-scrollbar-track,
.agent-chat-feed-virtual .v-vl::-webkit-scrollbar-track {
  background: transparent;
}

.agent-chat-feed-virtual > *:not(.n-scrollbar-rail)::-webkit-scrollbar-thumb,
.agent-chat-feed-virtual .v-vl::-webkit-scrollbar-thumb {
  background-color: rgba(15, 23, 42, 0.14);
  border-radius: 100px;
  border: 2px solid transparent;
  background-clip: padding-box;
}

.agent-chat-feed-virtual > *:not(.n-scrollbar-rail)::-webkit-scrollbar-thumb:hover,
.agent-chat-feed-virtual .v-vl::-webkit-scrollbar-thumb:hover {
  background-color: rgba(15, 23, 42, 0.22);
}

html.dark .agent-chat-feed-virtual > *:not(.n-scrollbar-rail),
html.dark .agent-chat-feed-virtual .v-vl {
  scrollbar-color: rgba(255, 255, 255, 0.12) transparent;
}

html.dark .agent-chat-feed-virtual > *:not(.n-scrollbar-rail)::-webkit-scrollbar-thumb,
html.dark .agent-chat-feed-virtual .v-vl::-webkit-scrollbar-thumb {
  background-color: rgba(255, 255, 255, 0.12);
}

html.dark .agent-chat-feed-virtual > *:not(.n-scrollbar-rail)::-webkit-scrollbar-thumb:hover,
html.dark .agent-chat-feed-virtual .v-vl::-webkit-scrollbar-thumb:hover {
  background-color: rgba(255, 255, 255, 0.2);
}
</style>
