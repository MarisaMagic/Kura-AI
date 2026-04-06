/** 智能体创建/编辑表单共用常量与工具 */

export const DEFAULT_AVATAR = `${import.meta.env.BASE_URL}logo.svg`.replace(/\/{2,}/, '/')

export const CREATE_DRAFT_KEY = 'mg-agent:user-agent-create-draft'
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
    enable_web: false,
    enable_code: false,
    opening_message: '',
    temperature: 0.1,
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

export function buildAgentFormRules(t, { isEdit = false } = {}) {
  const rules = {
    name: { required: true, message: () => t('views.agents.rule_name'), trigger: ['input', 'blur'] },
    model_name: { required: true, message: () => t('views.agents.rule_model_name'), trigger: ['input', 'blur'] },
  }
  if (!isEdit) {
    rules.api_key = {
      required: true,
      message: () => t('views.agents.rule_api_key'),
      trigger: ['input', 'blur'],
    }
  }
  return rules
}
