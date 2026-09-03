/** 智能体创建/编辑表单共用常量与工具 */

export const DEFAULT_AVATAR = `${import.meta.env.BASE_URL}logo.svg`.replace(/\/{2,}/, '/')

export const CREATE_DRAFT_KEY = 'kura-ai:user-agent-create-draft'
export const CREATE_DRAFT_VERSION = 1
export const MAX_AVATAR_DRAFT_BYTES = 1.5 * 1024 * 1024

export function emptyForm() {
  return {
    name: '',
    model_name: '',
    base_url: '',
    api_key: '',
    description: '',
    system_prompt: '',
    opening_message: '',
    temperature: 0.1,
    supports_vision: false,
    sub_model_name: '',
    sub_base_url: '',
    sub_api_key: '',
  }
}

export function dataURLtoBlob(dataurl) {
  const arr = dataurl.split(',')
  const mime = arr[0].match(/:(.*?);/)?.[1] || 'image/png'
  const bstr = atob(arr[1])
  let n = bstr.length
  const u8arr = new Uint8Array(n)
  while (n--) {
    u8arr[n] = bstr.charCodeAt(n)
  }
  return new Blob([u8arr], { type: mime })
}

export function buildAgentFormRules(t, { isEdit = false, getForm = null } = {}) {
  const rules = {
    name: {
      required: true,
      message: () => t('views.agents.rule_name'),
      trigger: ['input', 'blur'],
    },
    model_name: {
      required: true,
      message: () => t('views.agents.rule_model_name'),
      trigger: ['input', 'blur'],
    },
  }
  if (!isEdit) {
    rules.api_key = {
      required: true,
      message: () => t('views.agents.rule_api_key'),
      trigger: ['input', 'blur'],
    }
  }
  // 子智能体（打杂模型）：任一子字段非空即视为自定义，此时模型名称必填
  rules.sub_model_name = {
    validator: (_rule, value) => {
      const f = getForm?.() || {}
      const customizing =
        !!String(value || '').trim() ||
        !!String(f.sub_base_url || '').trim() ||
        !!String(f.sub_api_key || '').trim()
      if (customizing && !String(value || '').trim()) {
        return new Error(t('views.agents.rule_sub_model_name'))
      }
      return true
    },
    trigger: ['input', 'blur'],
  }
  if (!isEdit) {
    // 创建页无已保存子 Key，自定义子配置时必填；编辑页留空表示保留
    rules.sub_api_key = {
      validator: (_rule, value) => {
        const f = getForm?.() || {}
        const customizing =
          !!String(f.sub_model_name || '').trim() ||
          !!String(f.sub_base_url || '').trim() ||
          !!String(value || '').trim()
        if (customizing && !String(value || '').trim()) {
          return new Error(t('views.agents.rule_sub_api_key'))
        }
        return true
      },
      trigger: ['input', 'blur'],
    }
  }
  return rules
}
