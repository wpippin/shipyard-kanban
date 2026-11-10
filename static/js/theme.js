/**
 * 船舶完整性管理看板 - 主题切换器 (V1.5.1 UI)
 * 默认跟随系统；localStorage 保存用户选择。
 */
(function () {
  'use strict';

  const KEY = 'shipyard_theme';

  function getSystemTheme() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function getStoredTheme() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }

  function setStoredTheme(name) {
    try { localStorage.setItem(KEY, name); } catch (e) {}
  }

  function applyTheme(name, save) {
    const html = document.documentElement;
    if (!html) return;
    html.setAttribute('data-theme', name);
    html.setAttribute('data-bs-theme', name);   // 让 Bootstrap 5.3 组件也同步
    const btn = document.getElementById('themeToggle');
    if (btn) {
      btn.setAttribute('aria-label', name === 'dark' ? '切换到亮色' : '切换到暗色');
      btn.innerHTML = name === 'dark'
        ? '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>'
        : '<svg viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79Z"/></svg>';
    }
    if (save !== false) setStoredTheme(name);
  }

  // 尽早应用，避免闪烁
  const initial = getStoredTheme() || getSystemTheme();
  applyTheme(initial, false);

  // DOM 就绪后注入切换按钮（如果页面里还没放）
  function ensureButton() {
    if (document.getElementById('themeToggle')) return;
    const btn = document.createElement('button');
    btn.id = 'themeToggle';
    btn.type = 'button';
    btn.title = '切换亮色 / 暗色';
    document.body.appendChild(btn);
    // V1.20 修复：创建后立即补图标（此前 applyTheme 先于按钮创建执行，
    // 图标赋值被跳过 => 按钮是空白蓝球，像页面 Bug）
    applyTheme(document.documentElement.getAttribute('data-theme') || getSystemTheme(), false);
    btn.addEventListener('click', function () {
      const current = document.documentElement.getAttribute('data-theme') || 'light';
      applyTheme(current === 'dark' ? 'light' : 'dark', true);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', ensureButton);
  } else {
    ensureButton();
  }

  // 暴露全局方法供手动调用
  window.ShipyardTheme = {
    set: applyTheme,
    get: function () { return document.documentElement.getAttribute('data-theme') || 'light'; }
  };
})();

/* ===== 全局 Toast（替代 alert；微信/船海智云 WebView 中 alert 可能被静默拦截） ===== */
(function () {
  'use strict';
  const ICONS = {
    ok: '<svg class="wb-toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    err: '<svg class="wb-toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    warn: '<svg class="wb-toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    info: '<svg class="wb-toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>'
  };
  function ensureBox() {
    let box = document.getElementById('wbToastBox');
    if (!box) {
      box = document.createElement('div');
      box.id = 'wbToastBox';
      document.body.appendChild(box);
    }
    return box;
  }
  window.showToast = function (msg, type, duration) {
    type = (type || 'info');
    duration = (duration === undefined) ? (type === 'ok' ? 2200 : 3600) : duration;
    const box = ensureBox();
    const el = document.createElement('div');
    el.className = 'wb-toast ' + type;
    el.innerHTML = (ICONS[type] || ICONS.info) +
      '<span style="flex:1 1 auto;">' + (msg || '').replace(/\n/g, '<br>') + '</span>' +
      '<button class="wb-toast-close" aria-label="关闭">×</button>';
    el.querySelector('.wb-toast-close').addEventListener('click', function () { remove(el); });
    box.appendChild(el);
    if (duration > 0) {
      setTimeout(function () { remove(el); }, duration);
    }
    return el;
  };
  function remove(el) {
    if (!el || el.classList.contains('hiding')) return;
    el.classList.add('hiding');
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 250);
  }
})();
