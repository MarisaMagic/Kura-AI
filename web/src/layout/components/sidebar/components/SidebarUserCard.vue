<template>
  <n-dropdown trigger="click" placement="top-start" :options="options" @select="handleSelect">
    <div class="layout-sider-user" :class="{ 'layout-sider-user--collapsed': appStore.collapsed }">
      <img :src="userStore.avatar" class="layout-sider-user-avatar" />
      <span v-show="!appStore.collapsed" class="layout-sider-user-name">{{ userStore.name }}</span>
    </div>
  </n-dropdown>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useDark, useFullscreen, useToggle } from '@vueuse/core'
import { useAppStore, useUserStore } from '@/store'
import { renderIcon } from '@/utils'

const { t } = useI18n()
const router = useRouter()

const appStore = useAppStore()
const userStore = useUserStore()

const isDark = useDark()
const { isFullscreen, toggle } = useFullscreen()

const toggleDark = () => {
  appStore.toggleDark()
  useToggle(isDark)()
}

const options = computed(() => {
  const opts = [
    {
      label: t('header.label_profile'),
      key: 'profile',
      icon: renderIcon('mdi:account-circle-outline', { size: '16px' }),
    },
    {
      label: t('header.label_theme'),
      key: 'theme',
      icon: renderIcon(isDark.value ? 'mdi:moon-waning-crescent' : 'mdi:white-balance-sunny', {
        size: '16px',
      }),
    },
  ]
  if (appStore.fullScreen) {
    opts.push({
      label: t('header.label_fullscreen'),
      key: 'fullscreen',
      icon: renderIcon(
        isFullscreen.value
          ? 'ant-design:fullscreen-exit-outlined'
          : 'ant-design:fullscreen-outlined',
        { size: '16px' }
      ),
    })
  }
  opts.push({
    label: t('header.label_logout'),
    key: 'logout',
    icon: renderIcon('mdi:exit-to-app', { size: '16px' }),
  })
  return opts
})

function handleSelect(key) {
  if (key === 'profile') {
    router.push('/profile')
  } else if (key === 'theme') {
    toggleDark()
  } else if (key === 'fullscreen') {
    toggle()
  } else if (key === 'logout') {
    $dialog.confirm({
      title: t('header.label_logout_dialog_title'),
      type: 'warning',
      content: t('header.text_logout_confirm'),
      confirm() {
        userStore.logout()
        $message.success(t('header.text_logout_success'))
      },
    })
  }
}
</script>

<style scoped>
.layout-sider-user {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  height: 60px;
  padding: 0 14px;
  border-top: 1px solid #eee;
  box-sizing: border-box;
  min-width: 0;
  cursor: pointer;
}

.layout-sider-user--collapsed {
  justify-content: center;
  padding: 0;
  border-top: 0;
}

.layout-sider-user-avatar {
  width: 35px;
  height: 35px;
  border-radius: 50%;
  flex-shrink: 0;
  object-fit: cover;
}

.layout-sider-user-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
  color: #0f172a;
}

html.dark .layout-sider-user {
  border-top-color: rgba(255, 255, 255, 0.08);
}

html.dark .layout-sider-user-name {
  color: rgba(255, 255, 255, 0.9);
}
</style>
