/* R154-F20：扫码登录确认页脚本。从 URL query 取 token，轮询状态，
   approved 后自动 confirm 拿会话 cookie 并跳转首页。 */
(function () {
  var tok = new URLSearchParams(location.search).get('token') || '';
  var msg = document.getElementById('qr-msg');
  var sub = document.getElementById('qr-sub');
  var netErr = false;
  function finish(ok, text) {
    msg.innerHTML = '<span class="dot ' + (ok ? 'ok' : 'bad') + '"></span>' + text;
    if (ok) { location.href = '/'; }
  }
  function poll() {
    fetch('/api/login/qrcode/status?token=' + encodeURIComponent(tok))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok || d.status === 'expired') {
          finish(false, '二维码已过期，请回登录页重新获取'); return;
        }
        if (d.status === 'approved') {
          fetch('/api/login/qrcode/confirm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token: tok })
          }).then(function (r) { return r.json(); })
            .then(function (d2) {
              if (d2.ok) { finish(true, '登录成功，正在进入…'); }
              else { finish(false, d2.error || '登录失败'); }
            })
            .catch(function () { finish(false, '登录请求失败'); });
          return;
        }
        if (d.status === 'rejected') { finish(false, '桌面端已拒绝本次登录'); return; }
        if (d.status === 'used') { finish(false, '该二维码已使用'); return; }
        sub.textContent = '剩余 ' + d.expires_in + ' 秒';
        setTimeout(poll, 2000);
      })
      .catch(function () {
        if (!netErr) {
          netErr = true;
          msg.innerHTML = '<span class="dot"></span>网络异常，3 秒后重试…';
        }
        setTimeout(poll, 3000);
      });
  }
  poll();
})();
