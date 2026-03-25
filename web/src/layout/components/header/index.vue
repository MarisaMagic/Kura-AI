<template>
  <div flex items-center>
    <MenuCollapse />
  </div>
  <div ml-auto flex items-center>
    <ThemeMode />
    <FullScreen />
    <UserAvatar />
  </div>
</template>

<script setup>
import MenuCollapse from './components/MenuCollapse.vue'
import FullScreen from './components/FullScreen.vue'
import UserAvatar from './components/UserAvatar.vue'
import ThemeMode from './components/ThemeMode.vue'
</script>
