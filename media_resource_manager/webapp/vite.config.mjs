import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'node:path'

// 2026-10-26 Web 重写（Vue3）构建配置。
// 目标：**不破坏现有老 SPA**——老入口 web/index.html + web/static/js/app.js
// 原样保留；新前端的产物落 web/static/app/，入口单独叫 index.v2.html
// （由 webui.py 的 /v2 路由送出）。两套并行，按页迁移，迁完再切默认。
export default defineConfig({
  plugins: [vue()],
  // base 用 '/'：产物路径里已经写了 static/app/assets，再叠一次会变成
  // /static/app/static/app/assets（构建后实拦过）。
  base: '/',
  build: {
    outDir: '../web',
    emptyOutDir: false,
    assetsDir: 'static/app/assets',
    rollupOptions: {
      input: { v2: resolve(__dirname, 'index.v2.html') },
      output: {
        entryFileNames: 'static/app/assets/[name]-[hash].js',
        chunkFileNames: 'static/app/assets/[name]-[hash].js',
        assetFileNames: 'static/app/assets/[name]-[hash][extname]'
      }
    }
  }
})
