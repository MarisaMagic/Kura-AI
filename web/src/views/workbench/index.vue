<template>
  <AppPage :show-footer="false">
    <div flex-1>
      <n-card rounded-10>
        <div flex items-center>
          <img rounded-full width="60" :src="userStore.avatar" />
          <div ml-10>
            <p text-20 font-semibold>
              {{ $t('views.workbench.text_hello', { username: userStore.name }) }}
            </p>
            <p mt-5 text-14 op-60>{{ $t('views.workbench.text_welcome') }}</p>
          </div>
        </div>
      </n-card>
    </div>
  </AppPage>
</template>

<script setup>
import { useUserStore } from '@/store'

const userStore = useUserStore()
</script>
