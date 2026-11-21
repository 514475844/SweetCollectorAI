import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import Tasks from './views/Tasks.vue'
import Gallery from './views/Gallery.vue'
import Quarantine from './views/Quarantine.vue'
import Settings from './views/Settings.vue'
import Review from './views/Review.vue'
import Adapters from './views/Adapters.vue'
import RuleMarket from './views/RuleMarket.vue'
import Models from './views/Models.vue'
import Report from './views/Report.vue'
import Export from './views/Export.vue'
import Maintenance from './views/Maintenance.vue'
import Urls from './views/Urls.vue'
import Subs from './views/Subs.vue'
import Proxies from './views/Proxies.vue'
import Learning from './views/Learning.vue'
import Logs from './views/Logs.vue'
import './style.css'
import './theme.css'

// hash 路由：静态资源由 Python 的 http.server 提供，没有 SPA fallback，
// 用 hash 最稳（刷新/收藏都不会 404）。
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    // V0.0.9：学习看板升级成首页——打开网页第一眼就是
    // 「正按哪些词找、躲哪些词、从图库学到了什么」。
    { path: '/', redirect: '/learning' },
    { path: '/tasks', component: Tasks, meta: { title: '任务' } },
    { path: '/gallery', component: Gallery, meta: { title: '图库' } },
    { path: '/quarantine', component: Quarantine, meta: { title: '隔离区' } },
    { path: '/review', component: Review, meta: { title: '审核' } },
    { path: '/adapters', component: Adapters, meta: { title: '适配器' } },
    { path: '/market', component: RuleMarket, meta: { title: '适配器市场' } },
    { path: '/models', component: Models, meta: { title: 'AI 模型' } },
    { path: '/urls', component: Urls, meta: { title: '常用网址' } },
    { path: '/subs', component: Subs, meta: { title: '站点订阅' } },
    { path: '/proxies', component: Proxies, meta: { title: '代理管理' } },
    { path: '/learning', component: Learning, meta: { title: '首页 · 学习看板' } },
    { path: '/logs', component: Logs, meta: { title: '日志' } },
    { path: '/report', component: Report, meta: { title: '采集报表' } },
    { path: '/export', component: Export, meta: { title: '导入与导出' } },
    { path: '/maintenance', component: Maintenance, meta: { title: '数据维护' } },
    { path: '/settings', component: Settings, meta: { title: '设置' } },
  ],
})

createApp(App).use(router).mount('#app')

