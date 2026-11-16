/* Sweet采集助手 popup logic.
 * Cookie is fetched via chrome.cookies (includes httpOnly values such as
 * cf_clearance, which document.cookie can never see) for the URL the user
 * is actually on, formatted as a ready-to-paste Cookie header.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const statusEl = $("status"), hitsEl = $("hits");

function say(msg, cls = "") {
  statusEl.textContent = msg;
  statusEl.className = cls;
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab || null;
}

function hostOf(url) {
  try { return new URL(url).host; } catch { return ""; }
}

function fmtCookies(cs) {
  return cs.map((c) => `${c.name}=${c.value}`).join("; ");
}

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    say(`已复制${label}（${text.length} 字符）✅ 粘贴到软件即可`, "ok");
  } catch (e) {
    say(`复制失败：${e.message}`, "err");
  }
}

async function readPageStorage(tabId) {
  const [res] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => ({
      ls: Object.fromEntries(Object.entries(localStorage)),
      ss: Object.fromEntries(Object.entries(sessionStorage)),
    }),
  });
  return res ? res.result : null;
}

let currentTab = null;

async function init() {
  try {
    const cfg = await loadCfg();
    if (cfg.code) $("cfghint").style.display = "none";
  } catch (e) { /* 读配置失败不影响其它按钮 */ }
  try {
    currentTab = await activeTab();
    const host = currentTab ? hostOf(currentTab.url) : "";
    $("site").textContent = host
      ? `当前站点：${host}`
      : "当前标签页不可用（请打开一个网站后再点插件）";
  } catch (e) {
    $("site").textContent = `读取标签页失败：${e.message}`;
  }
}

async function collectCookies(url) {
  // url-based: 恰好是"访问这个页面时会带上的全部 Cookie"
  let cs = await chrome.cookies.getAll({ url });
  if (!cs.length) {
    const host = hostOf(url);
    if (host) cs = await chrome.cookies.getAll({ domain: host });
  }
  return cs;
}

// ── 直推软件（R251）───────────────────────────────────────────────
// 不用「复制 → 切到软件 → 粘贴」：点一下就把当前站点的 Cookie 按域名
// 写进软件，之后这个站的所有任务自动带上。
//   · 地址与配对码存 chrome.storage.local（只在这台机器上，不同步）；
//   · 软件端 /api/cookie/ingest 在鉴权之前，靠配对码放行 —— 所以
//     即使软件设了网页密码也不用填。
const DEF_SRV = "http://127.0.0.1:8080";

async function loadCfg() {
  const d = await chrome.storage.local.get(["srv", "code"]);
  $("srv").value = d.srv || DEF_SRV;
  $("code").value = d.code || "";
  return { srv: d.srv || DEF_SRV, code: d.code || "" };
}

$("b_save").addEventListener("click", async () => {
  let srv = $("srv").value.trim().replace(/\/+$/, "");
  if (!/^https?:\/\//i.test(srv)) srv = "http://" + srv;
  const code = $("code").value.trim();
  if (!code) return say("配对码不能为空（软件：设置 → 站点 Cookie）", "err");
  await chrome.storage.local.set({ srv, code });
  $("srv").value = srv;
  say("已保存。现在打开目标网站，点「发送到 Sweet 采集」", "ok");
});

$("b_push").addEventListener("click", async () => {
  if (!currentTab || !currentTab.url || !/^https?:/i.test(currentTab.url)) {
    return say("当前标签页不是普通网页（浏览器内置页拿不到 Cookie）", "err");
  }
  const cfg = await loadCfg();
  if (!cfg.code) {
    $("code").focus();
    return say("先填配对码（软件：设置 → 站点 Cookie → 6 位数字）", "err");
  }
  say("正在发送…");
  try {
    const cs = await collectCookies(currentTab.url);
    if (!cs.length) return say("该站点没有可读取的 Cookie", "err");
    const res = await fetch(cfg.srv.replace(/\/+$/, "") + "/api/cookie/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        code: cfg.code,
        url: currentTab.url,
        ua: navigator.userAgent,
        cookies: cs.map((c) => ({
          name: c.name, value: c.value, domain: c.domain || hostOf(currentTab.url),
        })),
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (data.ok) {
      const doms = Object.keys(data.domains || {});
      say(`已保存 ${data.imported || 0} 条到 ${doms[0] || "软件"} ✅`, "ok");
    } else {
      say(data.error || `失败（HTTP ${res.status}）`, "err");
    }
  } catch (e) {
    say(`连不上软件：${e.message}。检查地址与软件是否在运行`, "err");
  }
});

$("b_cookie").addEventListener("click", async () => {
  if (!currentTab || !currentTab.url) return say("没有可用的标签页", "err");
  try {
    const cs = await collectCookies(currentTab.url);
    if (!cs.length) return say("该站点没有可读取的 Cookie", "err");
    const header = fmtCookies(cs);
    const nHttp = cs.filter((c) => c.httpOnly).length;
    await copyText(header, `整串 Cookie（${cs.length} 条，含 ${nHttp} 条 httpOnly）`);
  } catch (e) {
    say(`读取 Cookie 失败：${e.message}`, "err");
  }
});

$("b_ua").addEventListener("click", () => copyText(navigator.userAgent, "User-Agent"));
$("b_url").addEventListener("click", () => {
  if (!currentTab || !currentTab.url) return say("没有可用的标签页", "err");
  copyText(currentTab.url, "网址");
});

async function copyStorage(which) {
  if (!currentTab) return say("没有可用的标签页", "err");
  try {
    const st = await readPageStorage(currentTab.id);
    const data = st && st[which];
    const text = JSON.stringify(data || {}, null, 2);
    if (text === "{}") return say(`${which} 是空的`, "err");
    await copyText(text, which);
  } catch (e) {
    say(`读取 ${which} 失败（浏览器内置页不支持）：${e.message}`, "err");
  }
}
$("b_ls").addEventListener("click", () => copyStorage("ls"));
$("b_ss").addEventListener("click", () => copyStorage("ss"));

$("b_find").addEventListener("click", async () => {
  const key = $("key").value.trim().toLowerCase();
  hitsEl.innerHTML = "";
  if (!key) return say("先输入要找的键名", "err");
  if (!currentTab || !currentTab.url) return say("没有可用的标签页", "err");
  const found = [];
  try {
    for (const c of await collectCookies(currentTab.url)) {
      if (c.name.toLowerCase().includes(key)) found.push(["Cookie", c.name, c.value]);
    }
    const st = await readPageStorage(currentTab.id);
    if (st) {
      for (const [bag, obj] of [["localStorage", st.ls], ["sessionStorage", st.ss]]) {
        for (const [k, v] of Object.entries(obj || {})) {
          if (k.toLowerCase().includes(key)) found.push([bag, k, v]);
        }
      }
    }
  } catch (e) {
    return say(`查找失败：${e.message}`, "err");
  }
  if (!found.length) return say(`没有找到包含「${key}」的键`, "err");
  say(`找到 ${found.length} 项，点击复制：`, "ok");
  for (const [bag, k, v] of found.slice(0, 12)) {
    const b = document.createElement("button");
    b.textContent = `[${bag}] ${k}`;
    b.title = v.length > 120 ? v.slice(0, 120) + "…" : v;
    b.addEventListener("click", () => copyText(v, k));
    hitsEl.appendChild(b);
  }
});

init();
