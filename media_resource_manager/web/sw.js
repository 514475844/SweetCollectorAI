/* 2026-10-29：旧版网页（PWA）已下线，这个文件只剩一个作用 ——「自我注销」。
 *
 * 为什么还要留一个 sw.js：旧版给浏览器装过 service worker，它会**长期驻留**
 * 在用户浏览器里，把 `/` 的导航请求从缓存里应答。旧文件删了、页面换成新版，
 * 用户刷新后看到的还是旧界面（「更新了但界面没变」的经典故障）。
 * 新版页面（index.v2.html）不注册 SW，所以这里主动注销 + 清空所有缓存，
 * 让浏览器回到正常的网络请求。
 *
 * 浏览器每次打开都会去取一次 sw.js（哪怕这次不注册），所以这段代码
 * 一定会被执行；执行完这个站点就没有 service worker 了。
 */
self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    try {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => caches.delete(k)));
    } catch (e) { /* 清缓存失败不影响注销 */ }
    try {
      await self.registration.unregister();
    } catch (e) { /* 忽略 */ }
    // 让已受控的页面立刻脱离 SW（下次加载直接走网络）
    try {
      const clients = await self.clients.matchAll({ type: 'window' });
      clients.forEach((c) => c.navigate(c.url).catch(() => {}));
    } catch (e) { /* 忽略 */ }
  })());
});

// 不拦截任何请求：这个 SW 只负责「把自己干掉」。
