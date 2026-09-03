<template>
  <AppPage :show-footer="true" bg-cover :style="{ backgroundImage: `url(${bgImg})` }">
    <div
      style="transform: translateY(25px)"
      class="m-auto max-w-1500 min-w-345 f-c-c rounded-10 bg-white bg-opacity-60 p-15 card-shadow"
      dark:bg-dark
    >
      <div hidden w-380 px-20 py-35 md:block>
        <icon-custom-front-page pt-10 text-300 color-primary></icon-custom-front-page>
      </div>

      <div w-320 flex-col px-20 py-35>
        <h5 f-c-c text-24 font-normal color="#6a6a6a">
          <img
            :src="brandLogoSrc"
            alt=""
            width="50"
            height="50"
            class="mr-10 h-50 w-50 shrink-0 object-contain"
          />{{ $t('app_name') }}
        </h5>
        <div mt-30>
          <n-input
            v-model:value="form.email"
            autofocus
            class="h-50 items-center pl-10 text-16"
            :placeholder="$t('views.register.placeholder_email')"
            :maxlength="255"
          />
        </div>
        <div mt-20>
          <n-input
            v-model:value="form.username"
            class="h-50 items-center pl-10 text-16"
            :placeholder="$t('views.register.placeholder_username_optional')"
            :maxlength="20"
          />
        </div>
        <div mt-20>
          <n-input
            v-model:value="form.password"
            class="h-50 items-center pl-10 text-16"
            type="password"
            show-password-on="mousedown"
            :placeholder="$t('views.register.placeholder_password')"
            :maxlength="128"
            @keypress.enter="handleRegister"
          />
        </div>
        <div mt-20>
          <n-input
            v-model:value="form.passwordConfirm"
            class="h-50 items-center pl-10 text-16"
            type="password"
            show-password-on="mousedown"
            :placeholder="$t('views.register.placeholder_password_confirm')"
            :maxlength="128"
            @keypress.enter="handleRegister"
          />
        </div>

        <div mt-20>
          <n-button
            h-50
            w-full
            rounded-5
            text-16
            type="primary"
            :loading="loading"
            @click="handleRegister"
          >
            {{ $t('views.register.text_register') }}
          </n-button>
        </div>
        <div mt-16 f-c-c text-14>
          <router-link to="/login">{{ $t('views.register.link_login') }}</router-link>
        </div>
      </div>
    </div>
  </AppPage>
</template>

<script setup>
import bgImg from '@/assets/images/login_bg.webp'
import api from '@/api'
import { useI18n } from 'vue-i18n'

const router = useRouter()
const { t } = useI18n({ useScope: 'global' })
const brandLogoSrc = `${import.meta.env.BASE_URL}logo.svg`.replace(/\/{2,}/, '/')

const form = ref({
  email: '',
  username: '',
  password: '',
  passwordConfirm: '',
})

const loading = ref(false)

onMounted(async () => {
  try {
    const res = await api.registrationEnabled()
    if (!res.data?.enabled) {
      $message.warning(t('views.register.message_registration_closed'))
      router.replace('/login')
    }
  } catch {
    router.replace('/login')
  }
})

async function handleRegister() {
  const { email, username, password, passwordConfirm } = form.value
  if (!email || !password) {
    $message.warning(t('views.register.message_input_required'))
    return
  }
  if (password !== passwordConfirm) {
    $message.warning(t('views.register.message_password_mismatch'))
    return
  }
  try {
    loading.value = true
    $message.loading(t('views.register.message_submitting'))
    const payload = { email: email.trim(), password: password.toString() }
    if (username.trim()) payload.username = username.trim()
    await api.register(payload)
    $message.success(t('views.register.message_register_success'))
    router.push('/login')
  } catch (e) {
    console.error('register error', e?.error || e)
  }
  loading.value = false
}
</script>
