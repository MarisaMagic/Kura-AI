<template>
  <div min-w-0 flex flex-1 items-center>
    <MenuCollapse />
    <AgentChatTitle />
  </div>
  <div ml-auto flex shrink-0 items-center>
    <ThemeMode />
    <FullScreen />
    <UserAvatar />
  </div>
</template>

<script setup>
import AgentChatTitle from './components/AgentChatTitle.vue'
import MenuCollapse from './components/MenuCollapse.vue'
import FullScreen from './components/FullScreen.vue'
import UserAvatar from './components/UserAvatar.vue'
import ThemeMode from './components/ThemeMode.vue'
</script>
