/**
 * 导入作业的共享状态（模块级单例）。
 *
 * 为什么不在组件里用 ref：路由是普通 router-view，没有 keep-alive。
 * 用户点「开始导入」后切到图库再切回来，导入组件会被销毁重建，
 * 组件里的进度 ref 和 setInterval 一起没了 —— 后端其实还在跑，
 * 界面却回到「尚未开始」，看起来就像「不导入了」。
 *
 * 现在把进度、表单勾选、作业编号都放在这个模块里：
 *   · 组件卸载**不停轮询**，作业在后台照常更新；
 *   · 组件重建直接读同一份状态，进度条和按钮状态原样接回；
 *   · 作业编号存 localStorage，页面刷新 / 软件重开后也能试着接回
 *     （后端重启会让旧作业失效，此时静默清掉，不弹吓人的报错）。
 *
 * V0.0.45（用户三条反馈）：
 *   1. 「点了开始导入也没有任何提示」—— 真因是这里调了一个**不存在**的
 *      函数 `_writeForm()`（正名是 `saveForm`）。ReferenceError 在 async 函数里
 *      直接变成被吞掉的 rejection，于是按钮点下去既没请求、也没报错、也没进度，
 *      界面一动不动。现在起：任何一步失败都必须落到 imp.err。
 *   2. 新增 `starting`：请求发出到后端受理之间有个空档（尤其 WebDAV 要先列目录），
 *      这段时间界面必须有「正在准备…」的转圈，不能一声不响。
 *   3. 进度拆成 phase / done / total / eta 四个字段，界面据此说人话
 *      （「正在入库 1,200 / 12,500 张 · 约剩 3 分钟」），不再直接喷后端那串。
 */
import { reactive } from 'vue'
import { api } from './api'

// V0.0.51：表单与作业**必须分开存**。
// 老版本把「路径 + 三个勾选 + 作业号」塞在同一个 localStorage 键里，
// 作业一结束（或后端重启让旧作业失效）_clearJob() 就把整把钥匙删掉 ——
// 用户勾选过的「同时复制一份进软件图库」跟着一起没了，重开软件回来就是
// 未勾选。用户原话「每次重新启动，同时复制一份进软件图库就不会被勾选」。
// 现在：表单单独一份，只有作业号相关的进度归 STORE_KEY，清作业不碰表单。
const FORM_KEY = 'mrm.import.form.v1'
const STORE_KEY = 'mrm.import.job.v1'

export const imp = reactive({
  // 表单（切页回来仍保留用户填的路径和三个开关）
  mode: 'local',          // local（本地/网盘挂载路径）| webdav | remote（FTP/SFTP/SMB）
  folder: '',
  url: '',                // WebDAV 目录地址
  user: '',
  pass: '',
  // V0.0.49：网络协议导入（FTP/SFTP/SMB）
  protocol: 'sftp',
  host: '',
  port: '',
  remotePath: '',
  recursive: true,        // 是否连子文件夹一起导入，默认开 = 原行为
  indexOnly: false,
  copyIn: false,
  // 作业进度
  starting: false,        // 请求已发出、后端还没受理：界面要立刻有反馈
  running: false,
  pct: 0,
  msg: '',
  phase: '',              // index = 正在入库 / tag = 正在打标签
  done: 0,
  total: 0,
  eta: '',
  curName: '',            // 正在处理的那张图（索引阶段是文件名）
  result: null,
  err: '',
  info: '',
  doneSeq: 0,             // 完成次数，组件 watch 它再往外发 done
})

let jid = ''
let timer = null
let infoTimer = null

/** 表单单独落盘：清作业、软件重启都不该把用户勾的选项一起清掉。 */
function _writeForm() {
  try {
    localStorage.setItem(FORM_KEY, JSON.stringify({
      mode: imp.mode, folder: imp.folder, url: imp.url, user: imp.user,
      protocol: imp.protocol, host: imp.host, port: imp.port, remotePath: imp.remotePath,
      recursive: imp.recursive,
      indexOnly: imp.indexOnly, copyIn: imp.copyIn,
    }))
  } catch (e) { /* 隐私模式下 localStorage 不可用，不影响导入 */ }
}

function _save() {
  _writeForm()
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({
      jid,
      running: imp.running, pct: imp.pct, msg: imp.msg,
      result: imp.result,
    }))
  } catch (e) { /* 隐私模式下 localStorage 不可用，不影响导入 */ }
}

/** 表单改动随时落盘（手打路径、勾选项都不该丢）。 */
export function saveForm() { _writeForm() }

function _clearJob() {
  jid = ''
  imp.running = false
  try { localStorage.removeItem(STORE_KEY) } catch (e) { /* 忽略 */ }
}

function _stopTimer() {
  if (timer) { clearInterval(timer); timer = null }
}

function _flashInfo(text) {
  imp.info = text
  if (infoTimer) clearTimeout(infoTimer)
  infoTimer = setTimeout(() => { imp.info = '' }, 8000)
}

/** 统一把异常翻成人话：宁可说「没能开始，原因未知」，也不许一声不响。 */
function _humanErr(e, fallback) {
  const m = (e && (e.message || e.error)) || ''
  if (!m) return fallback
  if (/Failed to fetch|NetworkError|Load failed/i.test(m)) {
    return `${fallback}：连不上软件后台（可能刚重启），稍等几秒再点一次。`
  }
  return m
}

async function _tick() {
  if (!jid) return
  try {
    const j = await api.importJob(jid)
    if (!j.ok) {
      // 后端重启 / 作业过期：不再显示「正在导入」，也不打扰用户
      _stopTimer()
      _clearJob()
      imp.starting = false
      return
    }
    imp.pct = Number(j.pct || 0)
    imp.msg = j.message || ''
    imp.phase = j.phase || ''
    imp.done = Number(j.done || 0)
    imp.total = Number(j.total || 0)
    imp.eta = j.eta || ''
    imp.curName = j.name || ''
    if (j.state === 'done' || j.state === 'failed') {
      _stopTimer()
      _clearJob()
      imp.starting = false
      imp.phase = ''
      if (j.state === 'failed') {
        imp.err = j.error || '导入失败'
      } else {
        imp.result = j.result || null
        // 后端在「停止」时也走 done，message 是「已停止（可再次导入接着打）」
        _flashInfo(j.message || '导入完成')
        imp.doneSeq += 1
      }
      return
    }
    imp.running = true
    imp.starting = false
    _save()
  } catch (e) { /* 轮询偶发失败不打断，下一轮再试 */ }
}

function _poll() {
  _stopTimer()
  timer = setInterval(_tick, 1500)
  _tick()
}

/** 清掉上一次的结果卡片（点「开始新的导入」时用）。 */
export function clearResult() {
  imp.result = null
  imp.err = ''
  imp.info = ''
}

/** 开始一次导入（按钮只走这里）。返回 true = 已受理。 */
export async function startImport() {
  if (imp.starting || imp.running) return false
  imp.err = ''
  imp.info = ''
  imp.result = null
  imp.pct = 0
  imp.phase = ''
  imp.done = 0
  imp.total = 0
  imp.eta = ''
  imp.curName = ''
  imp.starting = true          // ← 先亮「正在准备」，请求期间不许静默
  imp.msg = imp.mode === 'webdav' ? '正在连接 WebDAV…'
    : imp.mode === 'remote' ? `正在连接 ${String(imp.protocol).toUpperCase()} 服务器…`
    : '正在读取文件夹…'
  try {
    saveForm()
    const r = imp.mode === 'webdav'
      ? await api.importWebdav({
          url: imp.url,
          user: imp.user,
          password: imp.pass,
          tag: !imp.indexOnly,
          copy: imp.copyIn,
          recursive: imp.recursive,
        })
      : imp.mode === 'remote'
      ? await api.importRemote({
          protocol: imp.protocol,
          host: imp.host,
          port: imp.port ? Number(imp.port) : 0,
          user: imp.user,
          password: imp.pass,
          path: imp.remotePath,
          tag: !imp.indexOnly,
          copy: imp.copyIn,
          recursive: imp.recursive,
        })
      : await api.importFolder({
          folder: imp.folder,
          tag: !imp.indexOnly,
          copy: imp.copyIn,
          recursive: imp.recursive,
        })
    if (!r || !r.ok) {
      imp.err = (r && r.error) || '导入没能开始'
      return false
    }
    jid = r.job
    imp.running = true
    imp.msg = '已开始，正在统计文件数量…'
    _save()
    _poll()
    return true
  } catch (e) {
    imp.err = _humanErr(e, '导入没能开始')
    return false
  } finally {
    // 走到这里要么已在轮询（running=true），要么已经报错；两种情况都不该再转圈
    imp.starting = false
  }
}

/** 请求停止（优雅停，已入库的保留）。 */
export async function cancelImport() {
  try {
    const r = await api.importCancel()
    if (!r.ok) { imp.err = r.error || '停止请求未被受理'; return }
    imp.msg = '正在停止，已入库的图片会保留…'
    _flashInfo(r.message || '已请求停止，已入库的图片保留')
  } catch (e) { imp.err = _humanErr(e, '停止请求失败') }
}

/**
 * 组件挂载（或模块首次加载）时接回上次的作业。
 * 轮询是模块级的：切页不停，回来时状态已经是最新。
 */
export function resumeImport() {
  if (timer) return
  // 表单每次都从**表单存档**读回来（不再要求「有作业」才读）：
  // 用户上次手打的 NAS 路径、WebDAV 地址、账号、三个勾选，重开软件后还在。
  // 注意别再从 STORE_KEY 读表单 —— 那份老存档会被清作业整把删掉。
  // 唯一例外：从老版本升级上来时表单还在旧键里，先原样搬过来一次，
  // 别让正在用的用户刚升级就丢掉勾选（V0.0.51 兼容）。
  try {
    let raw = localStorage.getItem(FORM_KEY)
    if (!raw && localStorage.getItem(STORE_KEY)) {
      const old = JSON.parse(localStorage.getItem(STORE_KEY) || '{}')
      if (old && 'copyIn' in old) {
        localStorage.setItem(FORM_KEY, JSON.stringify({
          mode: old.mode, folder: old.folder, url: old.url, user: old.user,
          protocol: old.protocol, host: old.host, port: old.port,
          remotePath: old.remotePath, recursive: old.recursive,
          indexOnly: old.indexOnly, copyIn: old.copyIn,
        }))
        raw = localStorage.getItem(FORM_KEY)
      }
    }
    const s = JSON.parse(raw || '{}')
    if (s) {
      imp.mode = ['webdav', 'remote'].includes(s.mode) ? s.mode : 'local'
      imp.protocol = s.protocol || imp.protocol
      imp.host = s.host || imp.host
      imp.port = s.port || imp.port
      imp.remotePath = s.remotePath || imp.remotePath
      imp.folder = s.folder || imp.folder
      imp.url = s.url || imp.url
      imp.user = s.user || imp.user
      imp.recursive = s.recursive !== false
      imp.indexOnly = !!s.indexOnly
      imp.copyIn = !!s.copyIn
    }
  } catch (e) { /* 坏数据直接忽略 */ }
  try {
    const s = JSON.parse(localStorage.getItem(STORE_KEY) || '{}')
    if (s && !jid && s.jid) {
      jid = s.jid
      imp.pct = Number(s.pct || 0)
      imp.msg = s.msg || ''
      imp.running = !!s.running
      imp.result = s.result || null
    }
  } catch (e) { /* 坏数据直接忽略 */ }
  if (jid) _poll()
}

/**
 * 断点续导。「上次导到哪儿了」由后端按 images.ai_state 算出来 ——
 * 中途停止 / 崩溃 / 关机的行都还在库里，只是没打标。
 */
export async function loadLast() {
  try {
    const r = await api.importLast()
    if (!r.ok) return []
    return (r.items || []).filter((x) => x.pending > 0)
  } catch (e) { return [] }
}

// 模块一被加载就尝试接回：即使组件还没挂载，进度也在继续更新。
resumeImport()
