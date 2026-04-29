import { request, getToken } from '@/utils'

export default {
  login: (data) => request.post('/base/access_token', data, { noNeedToken: true }),
  getUserInfo: () => request.get('/base/userinfo'),
  getUserMenu: () => request.get('/base/usermenu'),
  getUserApi: () => request.get('/base/userapi'),
  // profile
  updatePassword: (data = {}) => request.post('/base/update_password', data),
  uploadAvatar: (data) => request.post('/base/upload_avatar', data),
  // users
  getUserList: (params = {}) => request.get('/user/list', { params }),
  getUserById: (params = {}) => request.get('/user/get', { params }),
  createUser: (data = {}) => request.post('/user/create', data),
  updateUser: (data = {}) => request.post('/user/update', data),
  deleteUser: (params = {}) => request.delete(`/user/delete`, { params }),
  resetPassword: (data = {}) => request.post(`/user/reset_password`, data),
  // role
  getRoleList: (params = {}) => request.get('/role/list', { params }),
  createRole: (data = {}) => request.post('/role/create', data),
  updateRole: (data = {}) => request.post('/role/update', data),
  deleteRole: (params = {}) => request.delete('/role/delete', { params }),
  updateRoleAuthorized: (data = {}) => request.post('/role/authorized', data),
  getRoleAuthorized: (params = {}) => request.get('/role/authorized', { params }),
  // menus
  getMenus: (params = {}) => request.get('/menu/list', { params }),
  createMenu: (data = {}) => request.post('/menu/create', data),
  updateMenu: (data = {}) => request.post('/menu/update', data),
  deleteMenu: (params = {}) => request.delete('/menu/delete', { params }),
  // apis
  getApis: (params = {}) => request.get('/api/list', { params }),
  createApi: (data = {}) => request.post('/api/create', data),
  updateApi: (data = {}) => request.post('/api/update', data),
  deleteApi: (params = {}) => request.delete('/api/delete', { params }),
  refreshApi: (data = {}) => request.post('/api/refresh', data),
  // depts
  getDepts: (params = {}) => request.get('/dept/list', { params }),
  createDept: (data = {}) => request.post('/dept/create', data),
  updateDept: (data = {}) => request.post('/dept/update', data),
  deleteDept: (params = {}) => request.delete('/dept/delete', { params }),
  // auditlog
  getAuditLogList: (params = {}) => request.get('/auditlog/list', { params }),
  // user agents
  getUserAgentList: (params = {}) => request.get('/user-agent/list', { params }),
  getUserAgent: (params = {}) => request.get('/user-agent/get', { params }),
  createUserAgent: (data = {}) => request.post('/user-agent/create', data),
  updateUserAgent: (data = {}) => request.post('/user-agent/update', data),
  deleteUserAgent: (params = {}) => request.delete('/user-agent/delete', { params }),
  uploadUserAgentAvatar: (agentId, data) =>
    request.post(`/user-agent/upload_avatar?agent_id=${agentId}`, data),
  /** 智能体对话（DependAuth，不走菜单权限） */
  getAgentChatSessions: (params = {}) => request.get('/user-agent/chat/sessions', { params }),
  /** 当前用户全部智能体下的会话（按最近时间） */
  getAgentChatSessionsAll: (params = {}) =>
    request.get('/user-agent/chat/sessions/all', { params }),
  getAgentChatSessionMessages: (agentId, sessionId) =>
    request.get(`/user-agent/chat/sessions/${encodeURIComponent(sessionId)}`, {
      params: { agent_id: agentId },
    }),
  /** 会话附件上传（先上传再发消息，返回 data.id 作为 attachment_ids） */
  uploadChatAttachment: (agentId, sessionId, file) => {
    const fd = new FormData()
    fd.append('file', file)
    return request.post(
      `/user-agent/chat/attachments/upload?agent_id=${agentId}&session_id=${encodeURIComponent(
        sessionId
      )}`,
      fd,
      { headers: { 'Content-Type': 'multipart/form-data' } }
    )
  },
  /**
   * 拉取会话附件二进制（带 token 请求头），用于 img 等无法用 axios 默认 JSON 解析的场景。
   */
  fetchChatAttachmentPreviewBlob: (agentId, sessionId, attachmentId) => {
    const base = import.meta.env.VITE_BASE_API || '/api/v1'
    const q = new URLSearchParams({
      agent_id: String(agentId),
      session_id: String(sessionId || 'default_session'),
      attachment_id: String(attachmentId),
    })
    const headers = {}
    const token = getToken()
    if (token) headers.token = token
    return fetch(`${base}/user-agent/chat/attachments/preview?${q}`, { headers }).then(
      async (res) => {
        if (!res.ok) throw new Error(String(res.status))
        return res.blob()
      }
    )
  },
  deleteAgentChatSession: ({ agent_id, session_id }) =>
    request.delete(`/user-agent/chat/sessions/${encodeURIComponent(session_id)}`, {
      params: { agent_id },
    }),
  getRecentAgents: () => request.get('/user-agent/recent_agents'),
  touchRecentAgent: (params = {}) =>
    request.post('/user-agent/recent_agents/touch', null, { params }),
  /** 知识库（DependAuth） */
  getKbDocuments: (params = {}) => request.get('/user-agent/kb/documents', { params }),
  uploadKbDocument: (agentId, data) =>
    request.post(`/user-agent/kb/upload?agent_id=${agentId}`, data, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }),
  deleteKbDocument: ({ agent_id, filename }) =>
    request.delete('/user-agent/kb/document', { params: { agent_id, filename } }),
}
