// 三个页面（职位列表 / 通用题库 / 某条职位的面试准备）共用的那点东西：主题切换、toast、
// 几个无状态的小工具。
//
// 为什么单独一个文件而不是直接引 app.js：app.js 文件末尾就跑初始化，而它的
// DOMContentLoaded 一上来就 document.getElementById('originChips').addEventListener(...)，
// 在没有职位列表的页面上会直接抛错、把后面的脚本一起带崩。把共用部分摘出来，
// 每个页面各引各的模块，谁也不用迁就谁。
//
// 只放"任何页面都成立"的东西：safeUrl / dedupeKey 这类只有职位列表用得上的留在 app.js。

// ---------- theme ----------
const SUN_PATHS = '<circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>';
const MOON_PATHS = '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>';

function effectiveIsDark() {
  const forced = document.documentElement.getAttribute('data-theme');
  if (forced) return forced === 'dark';
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function updateThemeIcon() {
  const icon = document.getElementById('themeIcon');
  icon.innerHTML = effectiveIsDark() ? SUN_PATHS : MOON_PATHS;
}

function initTheme() {
  const saved = localStorage.getItem('theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);
  updateThemeIcon();
}

function toggleTheme() {
  const next = effectiveIsDark() ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
  updateThemeIcon();
}

// ---------- toasts ----------
const TOAST_ICONS = {
  success: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6 9 17l-5-5"/></svg>',
  error: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 8v5M12 16h.01"/></svg>',
  info: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>',
};

function showToast(message, type = 'info', timeout = 4000) {
  const stack = document.getElementById('toastStack');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<span class="toast-icon">${TOAST_ICONS[type] || TOAST_ICONS.info}</span><span>${escapeHtml(message)}</span>`;
  stack.appendChild(el);
  setTimeout(() => {
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 200);
  }, timeout);
}

// ---------- helpers ----------
function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function setBtnLoading(btn, loadingText) {
  btn.dataset.originalHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>${loadingText}`;
}

function restoreBtn(btn) {
  if (btn.dataset.originalHtml) btn.innerHTML = btn.dataset.originalHtml;
  btn.disabled = false;
}

function bulletListHtml(items) {
  if (!items || !items.length) return '<div class="plain-text" style="color:var(--text-faint);">（无）</div>';
  return `<ul class="bullet-list">${items.map((t) => `<li>${escapeHtml(t)}</li>`).join('')}</ul>`;
}
