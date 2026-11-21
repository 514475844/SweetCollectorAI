// API 客户端：与后端 /api/* 一一对应（后端 59 个端点不动，前端重写）。
// 约定：
//   · 同源 + credentials（会话 cookie）；
//   · 401 → AuthError，由顶层显示登录框；
//   · 错误信息一律中文（后端本来就说人话，这里只做兜底）。
export class AuthError extends Error {}

async function req(path, { method = 'GET', body, headers, timeout = 0 } = {}) {
  const opt = { method, credentials: 'same-origin', headers: { ...(headers || {}) } }
  // V0.0.23：把「这是内置窗口还是外部浏览器」告诉后端 —— 日志的「来源」
  // 原来一律记成 web，用户分不清是自己点的还是手机/浏览器点的。
  try {
    opt.headers['X-MRM-Surface'] =
      (typeof window !== 'undefined' && window.__MRM_NATIVE_SHELL__) ? 'client' : 'browser'
  } catch (_e) { /* 忽略：环境不允许加头时按浏览器算 */ }
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json'
    opt.body = JSON.stringify(body)
  }
  const ctl = timeout ? new AbortController() : null
  if (ctl) {
    opt.signal = ctl.signal
    setTimeout(() => ctl.abort(), timeout)
  }
  const r = await fetch(path, opt)
  if (r.status === 401) {
    // 2026-10-27：会话过期时以前只有当前页面显示一句「需要登录」，
    // 用户不知道为什么、也没地方重新登录（必须自己猜着刷新）。
    // 这里广播一个事件，App 收到后直接把登录框弹出来。
    try { window.dispatchEvent(new Event('mrm-auth-needed')) } catch { /* 忽略 */ }
    throw new AuthError('需要登录')
  }
  const ct = r.headers.get('content-type') || ''
  const data = ct.includes('json') ? await r.json() : await r.text()
  if (!r.ok) {
    const msg = (data && data.error) || `请求失败（HTTP ${r.status}）`
    throw new Error(msg)
  }
  return data
}

const qs = (o) => {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(o || {})) {
    if (v !== undefined && v !== null && v !== '') p.set(k, v)
  }
  const s = p.toString()
  return s ? `?${s}` : ''
}

export const api = {
  raw: req,
  get: (p) => req(p),
  post: (p, body, opt) => req(p, { method: 'POST', body: body ?? {}, ...(opt || {}) }),

  // 会话
  session: () => req('/api/session'),
  login: (password) => req('/api/login', { method: 'POST', body: { password } }),
  logout: () => req('/api/logout', { method: 'POST', body: {} }),

  // 任务
  tasks: () => req('/api/tasks'),
  task: (id) => req('/api/task?id=' + encodeURIComponent(id)),
  taskSave: (payload) => req('/api/task/save', { method: 'POST', body: payload }),
  // 后端 _task_id_of 同时认 query 与 body；这里两样都带，省得踩「只发一半」的老坑。
  // V0.0.27：extra 用于删除时的「同时删除图片」（{with_images: true}）。
  taskCtl: (act, id, extra) => req(`/api/task/${act}?id=${encodeURIComponent(id)}`,
                                   { method: 'POST',
                                     body: { task_id: Number(id), ...(extra || {}) } }),
  taskBatch: (act, ids, extra) => req('/api/task/batch',
                                      { method: 'POST',
                                        body: ids ? { act, ids, ...(extra || {}) } : { act, ...(extra || {}) } }),
  taskFailedUrls: (id) => req('/api/task/failed-urls?id=' + encodeURIComponent(id)),
  taskRetryUrls: (id, urls) => req('/api/task/retry-urls', { method: 'POST', body: { id: Number(id), urls } }),
  // V0.0.55 批1：收件口（安卓壳用，首启向导的「建第一个任务」也走它）
  inboxLink: (link) => req('/api/inbox/link', { method: 'POST', body: { link } }),

  // 图库 / 审核（同一分页通道：time 排序用 id 游标）
  // ⚠ 2026-10-27 真 bug：这里以前把参数**逐个写死**（limit/cursor/sort/q），
  //   所以 ImageGrid 传进来的 `task`（按任务筛选）被静默丢掉 ——
  //   下拉切换看起来完全没反应，用户以为筛选坏了。
  //   改成「多余参数原样透传」，以后加过滤维度不用再改这一行。
  view: (view, opts = {}) => {
    const { limit = 60, cursor = 0, sort = 'time', q = '', ...rest } = opts
    return req(`/api/view/${view}${qs({ limit, cursor, sort, q, ...rest })}`)
  },
  imageAction: (ids, act, extra) =>
    req('/api/image/action', { method: 'POST', body: { ids, act, ...(extra || {}) } }),
  // V0.0.12：网页审核补桌面三能力——撤销上一步 / 随机抽检 / 智能抽样。
  reviewUndo: () => req('/api/review/undo', { method: 'POST', body: {} }),
  reviewUndoInfo: () => req('/api/review/undo-info'),
  reviewSample: (n = 12) => req('/api/review/sample', { method: 'POST', body: { n } }),
  reviewSmart: (n = 20) => req('/api/review/smart', { method: 'POST', body: { n } }),
  dislikeOptions: (ids) => req('/api/dislike/options?ids=' + encodeURIComponent(ids.join(','))),
  // V0.0.12：批量重命名（桌面图库菜单同款，原有图库文件后端红线跳过）
  imageRename: (ids, mode, value) =>
    req('/api/image/rename', { method: 'POST', body: { ids, mode, value } }),
  // V0.0.12：手填词的词表桥接预演（桌面判退框同款提示）
  labelBridgePreview: (words) =>
    req('/api/label-bridge/preview', { method: 'POST', body: { words } }),
  imageTags: (ids) => req('/api/image/tags?ids=' + encodeURIComponent(ids.join(','))),
  // ⚠ 2026-10-27 修：后端要 `{ids, act:'add'|'remove', tags:[...]}`，
  // 旧封装发的是 `{ids, add, remove}` ⇒ 每次调用都 400（一直没人用所以没暴露）。
  imageTagsSave: (ids, tags, act = 'add') =>
    req('/api/image/tags', { method: 'POST', body: { ids, act, tags } }),
  similar: (id, limit = 60) => req(`/api/similar?id=${encodeURIComponent(id)}&limit=${limit}`),
  tags: () => req('/api/tags'),
  // V0.0.14：合格线自动校准（只读建议 / 应用）与导出交付层。
  // 二进制 .mrmx 下载不走 req()（它只解 JSON/文本），见 Export.vue 的 downloadEncrypted。
  thresholdSuggest: (current) => req('/api/threshold/suggest' + qs({ current })),
  thresholdApply: (value, applyTasks = false) =>
    req('/api/threshold/apply', { method: 'POST', body: { value, apply_tasks: applyTasks } }),
  exportTrainingKit: (payload) => req('/api/export/training-kit', {
    method: 'POST', body: payload, timeout: 180000,
  }),

  // 设置 / 代理 / 词表
  settings: () => req('/api/settings'),
  saveSettings: (patch) => req('/api/settings/save', { method: 'POST', body: patch }),
  proxies: () => req('/api/proxies'),
  proxySave: (body) => req('/api/proxy/save', { method: 'POST', body }),
  proxyDelete: (id) => req('/api/proxy/delete', { method: 'POST', body: { id } }),
  // 2026-10-28：测代理延迟。传 id 测单个，不传测全部（后端最多测 8 个）。
  proxyCheck: (id) => req('/api/proxy/check', {
    method: 'POST', body: id ? { id } : {},
  }),
  words: () => req('/api/words/history'),
  aiGpu: (on) => req('/api/ai/gpu', { method: 'POST', body: { on: !!on } }),
  models: () => req('/api/models'),
  modelUse: (id) => req('/api/model/use', { method: 'POST', body: { id } }),

  // 维护
  summary: () => req('/api/summary'),
  diskspace: () => req('/api/diskspace', { timeout: 30000 }),
  cleanup: (plan) => req('/api/cleanup', { method: 'POST', body: { plan } }),
  // V0.0.16：网页补齐桌面「数据 / 维护」菜单的对称动作。
  // 破坏性三个动作后端强制要求 confirm:true，前端另有一次确认对话框。
  siteDataLocation: () => req('/api/site/data-location'),
  siteCleanupItems: () => req('/api/site/cleanup-items'),
  siteLogsExportUrl: () => '/api/site/logs/export',
  siteBackfill: (payload) => req('/api/site/backfill', {
    method: 'POST', body: payload, timeout: 180000,
  }),
  siteBackupSync: (payload) => req('/api/site/backup-sync', {
    method: 'POST', body: payload, timeout: 180000,
  }),
  siteLogsClear: () => req('/api/site/logs/clear', {
    method: 'POST', body: { confirm: true },
  }),
  // V0.0.56：keep_days>0 只清"隔离超过 N 天"的图；不传 = 全清。
  siteQuarantinePrune: (keepDays) => req('/api/site/quarantine/prune', {
    method: 'POST', body: { confirm: true, keep_days: Number(keepDays) || 0 },
  }),
  siteCleanup: (plan) => req('/api/site/cleanup', {
    method: 'POST', body: { confirm: true, plan }, timeout: 180000,
  }),
  backup: () => req('/api/backup'),
  backupNow: () => req('/api/backup', { method: 'POST', body: {}, timeout: 180000 }),
  backupDaily: (enabled) => req('/api/backup/daily', { method: 'POST', body: { enabled: !!enabled } }),
  learning: () => req('/api/learning'),
  learnClearModelAuto: () => req('/api/learn/clear-model-auto', { method: 'POST', body: {} }),

  // 常用网址（与桌面端共用 cfg.saved_urls）
  urls: () => req('/api/urls'),
  urlSave: (url) => req('/api/url/save', { method: 'POST', body: { url } }),
  urlDelete: (payload) => req('/api/url/delete', { method: 'POST', body: payload }),

  // 日志（展示 recent 明细）
  logstats: () => req('/api/view/logstats'),

  // 2026-10-28：站点订阅 / 新图监控（编号 21、22）
  //   subCheck 的 run=false 才是「只看一眼」；run=true 才会真建任务并开始下载。
  subs: () => req('/api/subs'),
  subSave: (payload) => req('/api/sub/save', { method: 'POST', body: payload }),
  subDelete: (id) => req('/api/sub/delete', { method: 'POST', body: { id } }),
  subCheck: (id, run = false) => req('/api/sub/check', { method: 'POST', body: { id, run }, timeout: 120000 }),
  subPreview: (url, pattern = '') => req('/api/sub/preview', { method: 'POST', body: { url, pattern }, timeout: 120000 }),
  subItems: (id) => req('/api/sub/items?id=' + encodeURIComponent(id)),
  // 编号 32：本机网卡地址（设置页选源地址用）
  netIps: () => req('/api/net/ips'),
  // 2026-10-28：底栏实时速率（最近 1 分钟写库字节数 / 每分钟新增张数）
  stats: () => req('/api/stats'),
  // 在资源管理器里打开任务输出目录（只有服务跑在本机时才有意义）
  taskReveal: (id) => req('/api/task/reveal', { method: 'POST', body: { id } }),

  // 偏好导入导出：导出是**文件下载**，不能走 req()（会被当成 JSON 解析）
  prefsExportUrl: () => '/api/preferences/export',
  prefsImport: (payload) => req('/api/preferences/import', { method: 'POST', body: payload }),
  learnEnabled: (enabled) => req('/api/learn/enabled', { method: 'POST', body: { enabled: !!enabled } }),
  learnReset: () => req('/api/learn/reset', { method: 'POST', body: { confirm: true } }),
  learnDelete: (label) => req('/api/learn/delete', { method: 'POST', body: { label } }),
  // V0.0.7：被判掉过的图教出来的「以后别找」词，能看能删能清空
  feedback: () => req('/api/feedback'),
  feedbackRemove: (word) => req('/api/feedback/remove', { method: 'POST', body: { word } }),
  feedbackClear: () => req('/api/feedback/clear', { method: 'POST', body: {} }),
  findTerms: () => req('/api/find-terms'),
  learnAddTerm: (word) => req('/api/learn/add-term', { method: 'POST', body: { word } }),
  // V0.0.71：AI 看图说词 → 勾选 → 写进口味库
  autoVocabStart: (method, amount) => req('/api/autovocab/start',
    { method: 'POST', body: { method, amount } }),
  autoVocabJob: (id) => req('/api/autovocab/job?id=' + encodeURIComponent(id)),
  // V0.0.76：随时停止扫描（已扫成果自动保留）+ 读取上次成果
  autoVocabCancel: (id) => req('/api/autovocab/cancel',
    { method: 'POST', body: { id } }),
  autoVocabLast: () => req('/api/autovocab/last'),
  autoVocabApply: (yes, no) => req('/api/autovocab/apply',
    { method: 'POST', body: { yes, no } }),

  // R254：桌面系统集成（版本 / 开机自启 / 检查更新）
  sysIntegration: () => req('/api/system/integration', { timeout: 20000 }),
  sysUpdate: () => req('/api/system/update', { timeout: 30000 }),
  sysAutostart: (enabled) => req('/api/system/autostart',
    { method: 'POST', body: { enabled: !!enabled }, timeout: 20000 }),

  // R253：AI 写适配器（后台作业：抓页面 + 模型 + 本地验证）
  adapterAi: (url, useAi) => req('/api/adapter/ai',
    { method: 'POST', body: { url, use_ai: useAi !== false }, timeout: 30000 }),
  adapterAiJob: (id) => req('/api/adapter/ai/job?id=' + encodeURIComponent(id),
    { timeout: 20000 }),
  // V0.0.51：复制提示词给外部 AI / 把外部 AI 的回复带回来验证
  adapterPrompt: (url) => req('/api/adapter/prompt',
    { method: 'POST', body: { url }, timeout: 60000 }),
  adapterFromReply: (url, reply) => req('/api/adapter/from-reply',
    { method: 'POST', body: { url, reply }, timeout: 60000 }),
  // V0.0.23：读一条已有规则的原文 + 覆盖保存（手动编辑 / 本地导入）
  adapterGet: (name) => req('/api/adapter/get?name=' + encodeURIComponent(name),
    { timeout: 20000 }),
  adapterSave: (rule, name, overwrite) => req('/api/adapter/save',
    { method: 'POST', body: { rule, name, overwrite: !!overwrite }, timeout: 30000 }),

  // ── V0.0.54：适配器市场（订阅源 / 目录 / 安装） ──
  marketSources: () => req('/api/market/sources', { timeout: 15000 }),
  marketIndex: (url) => req('/api/market/index?url=' + encodeURIComponent(url),
    { timeout: 30000 }),
  marketManage: (action, url) => req('/api/market/manage',
    { method: 'POST', body: { action, url }, timeout: 30000 }),
  marketInstall: (url, name, overwrite) => req('/api/market/install',
    { method: 'POST', body: { url, name, overwrite: !!overwrite }, timeout: 30000 }),

  // ── 站点 Cookie（R251）：按域名存档，插件推送 / JSON 导入 ──
  cookiesList: () => req('/api/cookies', { timeout: 20000 }),
  cookiesPost: (body) => req('/api/cookies',
    { method: 'POST', body, timeout: 30000 }),

  // R250：按配置清理旧整库备份（最新一份永远保留）
  fullPrune: () => req('/api/backup/full/prune', { method: 'POST', body: {} }),

  // ── 磁盘级维护（R249）──
  //   扫描/转换/修复都是后台作业；删异常记录是同步的（毫秒级）
  maintStart: (kind, opts) => req('/api/maintenance/' + kind,
    { method: 'POST', body: opts || {}, timeout: 30000 }),
  maintJob: (id) => req('/api/maintenance/job?id=' + encodeURIComponent(id),
    { timeout: 20000 }),
  maintCancel: () => req('/api/maintenance/cancel', { method: 'POST', body: {} }),
  maintClean: (ids) => req('/api/maintenance/clean',
    { method: 'POST', body: { ids }, timeout: 30000 }),

  // ── 从文件夹导入图库（R248）──
  //   · fsList：浏览器拿不到真实路径，只能由服务端列目录（仅本机可用）
  //   · 导入本身是**后台作业**：先索引（快）、再打标（慢，可停可续）
  fsList: (path) => req('/api/fs/list?path=' + encodeURIComponent(path || '')),
  importFolder: (opts) => req('/api/import/folder',
    { method: 'POST', body: opts, timeout: 30000 }),
  importJob: (id) => req('/api/import/job?id=' + encodeURIComponent(id),
    { timeout: 20000 }),
  // V0.0.44：断点续导（上次导到哪儿了）+ 从 WebDAV 导入（NAS / 网盘入口）
  importLast: () => req('/api/import/last', { timeout: 20000 }),
  importWebdav: (opts) => req('/api/import/webdav',
    { method: 'POST', body: opts, timeout: 30000 }),
  // V0.0.49：FTP / SFTP / SMB 网络协议导入
  importRemote: (opts) => req('/api/import/remote',
    { method: 'POST', body: opts, timeout: 30000 }),
  importCancel: () => req('/api/import/cancel', { method: 'POST', body: {} }),
  // V0.0.60：AI 识别独立作业（自己的进度条、自己的停止键，可与导入同时跑）
  tagStart: (opts = {}) => req('/api/import/tag-start',
    { method: 'POST', body: opts, timeout: 30000 }),
  tagCancel: () => req('/api/import/tag-cancel', { method: 'POST', body: {} }),
  tagJob: (id) => req('/api/import/tag-job?id=' + encodeURIComponent(id),
    { timeout: 20000 }),
  tagPending: () => req('/api/import/tag-pending', { timeout: 20000 }),

  // V0.0.46：按意思找（语义搜索）+ 画面特征补算（历史图一次性建向量）
  semanticStatus: () => req('/api/semantic/status', { timeout: 30000 }),
  semanticSearch: (q, opt = {}) => req('/api/semantic/search',
    { method: 'POST', body: { q, ...opt }, timeout: 60000 }),
  semanticBackfill: () => req('/api/semantic/backfill',
    { method: 'POST', body: {}, timeout: 20000 }),
  semanticBackfillCancel: () => req('/api/semantic/backfill/cancel',
    { method: 'POST', body: {} }),

  // ── 整库备份（配置 + 库 + 规则 + 缩略图）：导出/恢复都是**后台作业** ──
  //   · 导出要压几百 MB 缩略图、恢复要等运行中的任务收尾，同步请求必超时；
  //   · 统一「起作业 → 轮询 /api/backup/full/job」两段式。
  fullList: () => req('/api/backup/full'),
  fullInspect: (name) => req('/api/backup/full/inspect?name=' + encodeURIComponent(name)),
  fullExport: (includeThumbs = false) => req('/api/backup/full/export', {
    method: 'POST', body: { include_thumbs: !!includeThumbs }, timeout: 30000 }),
  fullRestore: (name, restoreSafety = true) => req('/api/backup/full/restore', {
    method: 'POST', body: { name, restore_safety: !!restoreSafety }, timeout: 30000 }),
  fullJob: (id) => req('/api/backup/full/job?id=' + encodeURIComponent(id), { timeout: 20000 }),
  fullDelete: (name) => req('/api/backup/full/delete', { method: 'POST', body: { name } }),
  // 下载是文件流，走裸 URL
  fullDownloadUrl: (name) => '/api/backup/full/download?name=' + encodeURIComponent(name),
  /** 上传备份包：**裸二进制**（不是 multipart，后端按 Content-Length 落盘）。 */
  fullUpload: async (file) => {
    const q = new URLSearchParams({ name: file.name || '' }).toString()
    const r = await fetch('/api/backup/full/upload?' + q, {
      method: 'POST',
      credentials: 'same-origin',
      // 不带 Content-Type：让浏览器自己发 application/octet-stream，
      // 后端也不看它（只认 Content-Length + query 里的文件名）。
      body: file,
    })
    const data = await r.json().catch(() => ({}))
    if (!r.ok) throw new Error(data.error || `上传失败（HTTP ${r.status}）`)
    return data
  },

  // R245：机器信息（CPU/内存/GPU/各盘容量/当前下载目录剩余空间）—— AI 模型页用
  sysinfo: () => req('/api/sysinfo', { timeout: 30000 }),

  // 诊断包（脱敏后整体加密为 .mrmx，与桌面端同一格式，直接下载）
  diagnosticUrl: (withDb = true) => '/api/diagnostic?db=' + (withDb ? '1' : '0'),

  // ── V0.0.17 · 网页端补齐（新端点一律具名方法，页面不裸写 fetch）──
  diskspaceTrend: () => req('/api/diskspace/trend'),
  logStatsExportUrl: () => '/api/logs/stats/export',
  skins: () => req('/api/skins'),
  skinCss: (key) => req('/api/skins/css?key=' + encodeURIComponent(key || '')),
  skinUpload: (name, content) => req('/api/skins/upload',
    { method: 'POST', body: { name, content } }),
  webHotkeys: () => req('/api/web/hotkeys'),
  webHotkeysSave: (hotkeys) => req('/api/web/hotkeys',
    { method: 'POST', body: { hotkeys } }),
  dbMaster: () => req('/api/db/master'),
  dbMasterSet: (password, confirm) => req('/api/db/master',
    { method: 'POST', body: { password, confirm } }),
  imageReveal: (id) => req('/api/image/reveal',
    { method: 'POST', body: { id: Number(id) } }),

  // ── V0.0.18 · 网页端补齐（新端点一律具名方法，页面不裸写 fetch）──
  // 素材集导出前预览（勾选后把 ids 回传 exportTrainingKit 实现所见即所得）
  exportKitPreview: (payload) => req('/api/export/kit/preview', {
    method: 'POST', body: payload || {}, timeout: 60000,
  }),
  exportHistory: (limit = 50) => req('/api/export/history' + qs({ limit })),
  siteStats: (days = 0) => req('/api/site-stats' + qs({ days })),
  cookieHints: () => req('/api/cookies/hints', { timeout: 30000 }),
  // 通知中心：列表 / 标记已读 / 清空（清空后端要求 confirm）
  notifications: (opts = {}) => req('/api/notifications' + qs({
    limit: opts.limit, only_unread: opts.onlyUnread ? 1 : '',
  })),
  notificationsRead: (ids, all = false) => req('/api/notifications/read', {
    method: 'POST', body: all ? { all: true } : { ids: ids || [] },
  }),
  notificationsClear: () => req('/api/notifications/clear', {
    method: 'POST', body: { confirm: true },
  }),
  // V0.0.39：右下角「完全退出」（只有壳窗口里有意义，浏览器访问会被后端拒绝）
  appQuit: () => req('/api/app/quit', { method: 'POST', body: { confirm: true } }),
  // 全局搜索（任务 / 图库 / 日志，只读 LIKE）
  searchAll: (q) => req('/api/search/all' + qs({ q })),
  // 程序更新检查（版本比对，返回中文文本）
  updateCheck: () => req('/api/update/check', { timeout: 30000 }),
  // V0.0.55 批1：定时自动关机（GET=查当前计划，post('/api/powerdown')=安排/取消）
  powerdown: () => req('/api/powerdown'),
  // V0.0.19：界面模式（与桌面端共用 cfg.ui_mode）
  uiMode: () => req('/api/ui/mode'),
  saveUiMode: (mode) => req('/api/ui/mode', { method: 'POST', body: { mode } }),
}

/** SSE 事件流（/api/events）：只送版本号/摘要，页面按需再拉详情。 */
export function connectEvents(onEvent) {
  const es = new EventSource('/api/events')
  es.onmessage = (e) => {
    try { onEvent(JSON.parse(e.data)) } catch { /* 忽略坏帧 */ }
  }
  es.onerror = () => { /* EventSource 自带重连 */ }
  return () => es.close()
}
