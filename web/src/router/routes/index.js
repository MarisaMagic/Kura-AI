import i18n from '~/i18n'
const { t } = i18n.global

const Layout = () => import('@/layout/index.vue')
const AgentEditorLayout = () => import('@/layout/AgentEditorLayout.vue')

export const basicRoutes = [
  {
    path: '/',
    redirect: '/agent-hub',
    meta: { order: 0 },
  },
  {
    name: t('views.profile.label_profile'),
    path: '/profile',
    component: Layout,
    isHidden: true,
    children: [
      {
        path: '',
        component: () => import('@/views/profile/index.vue'),
        name: `${t('views.profile.label_profile')}Default`,
        meta: {
          title: t('views.profile.label_profile'),
          icon: 'user',
          affix: true,
        },
      },
    ],
    meta: { order: 99 },
  },
  {
    name: 'ErrorPage',
    path: '/error-page',
    component: Layout,
    isHidden: true,
    redirect: '/error-page/404',
    meta: {
      title: t('views.errors.label_error'),
      icon: 'mdi:alert-circle-outline',
      order: 99,
    },
    children: [
      {
        name: 'ERROR-401',
        path: '401',
        component: () => import('@/views/error-page/401.vue'),
        meta: {
          title: '401',
          icon: 'material-symbols:authenticator',
        },
      },
      {
        name: 'ERROR-403',
        path: '403',
        component: () => import('@/views/error-page/403.vue'),
        meta: {
          title: '403',
          icon: 'solar:forbidden-circle-line-duotone',
        },
      },
      {
        name: 'ERROR-404',
        path: '404',
        component: () => import('@/views/error-page/404.vue'),
        meta: {
          title: '404',
          icon: 'tabler:error-404',
        },
      },
      {
        name: 'ERROR-500',
        path: '500',
        component: () => import('@/views/error-page/500.vue'),
        meta: {
          title: '500',
          icon: 'clarity:rack-server-outline-alerted',
        },
      },
    ],
  },
  {
    name: '403',
    path: '/403',
    component: () => import('@/views/error-page/403.vue'),
    isHidden: true,
  },
  {
    name: '404',
    path: '/404',
    component: () => import('@/views/error-page/404.vue'),
    isHidden: true,
  },
  {
    name: 'Login',
    path: '/login',
    component: () => import('@/views/login/index.vue'),
    isHidden: true,
    meta: {
      title: '登录页',
    },
  },
  /** 智能体创建/编辑：无侧栏全屏；勿与后端动态菜单中的 /agents 目录冲突 */
  {
    name: 'AgentEditor',
    path: '/agents',
    component: AgentEditorLayout,
    isHidden: true,
    meta: { order: 98 },
    children: [
      {
        name: 'AgentCreate',
        path: 'create',
        component: () => import('@/views/agents/create/index.vue'),
        meta: {
          title: t('views.agents.title_create_agent'),
          hidden: true,
        },
      },
      {
        name: 'AgentEdit',
        path: 'edit/:id',
        component: () => import('@/views/agents/edit/index.vue'),
        meta: {
          title: t('views.agents.title_edit_agent'),
          hidden: true,
        },
      },
    ],
  },
  {
    path: '/agents/mine',
    redirect: '/agent-hub',
    isHidden: true,
  },
  /** 智能体对话（从智能体中心进入；勿与 /agents 下编辑器路由冲突） */
  {
    path: '/agent-hub/chat/:agentId',
    component: Layout,
    isHidden: true,
    children: [
      {
        path: '',
        name: 'AgentChat',
        component: () => import('@/views/agent-chat/index.vue'),
        meta: {
          title: t('views.agents.title_agent_chat'),
          hidden: true,
          /** 与后端菜单「智能体中心」的 name 一致，侧栏高亮智能体中心 */
          activeMenu: '智能体中心',
        },
      },
    ],
  },
]

export const NOT_FOUND_ROUTE = {
  name: 'NotFound',
  path: '/:pathMatch(.*)*',
  redirect: '/404',
  isHidden: true,
}

export const EMPTY_ROUTE = {
  name: 'Empty',
  path: '/:pathMatch(.*)*',
  component: null,
}

const modules = import.meta.glob('@/views/**/route.js', { eager: true })
const asyncRoutes = []
Object.keys(modules).forEach((key) => {
  asyncRoutes.push(modules[key].default)
})

// 加载 views 下每个模块的 index.vue 文件
const vueModules = import.meta.glob('@/views/**/index.vue')

export { asyncRoutes, vueModules }
