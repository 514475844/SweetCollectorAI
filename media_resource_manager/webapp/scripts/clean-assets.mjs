/**
 * 构建前清空上一次的前端产物（R247）。
 *
 * 为什么需要：vite 配置里 `outDir: '../web'` + `emptyOutDir: false`
 * （不能整体清空 web/，那里还有 sw.js、static/img、index.v2.html），
 * 于是 `static/app/assets` 只增不删 —— 实测堆了 **53 个 js、13 MB**，
 * 每版构建都再多一份。它们全部会被 PyInstaller 打进安装包，等于每发
 * 一版安装包都白胖 13MB，而且旧 chunk 留在包里没有任何入口能引用到。
 *
 * 只清 `static/app/assets` 这一个目录：其余路径下有手工维护的文件。
 */
import { existsSync, rmSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const assets = resolve(here, '..', '..', 'web', 'static', 'app', 'assets')

if (existsSync(assets)) {
  rmSync(assets, { recursive: true, force: true })
  console.log('[build] 已清空旧的前端产物：' + assets)
} else {
  console.log('[build] 没有旧产物需要清理')
}
