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

// ---------- shared icons ----------
// 24×24 线性描边图标，跟职位列表页 CHECK_ICON/X_ICON 等同一套规格（viewBox 0 0 24 24、
// stroke=currentColor、2px 描边）。放共用文件是因为标签/简历/Cover Letter/面试准备这几个
// 图标不止职位列表页要用，题库/简历/职位详情页也要用——原来这几处用的是 emoji，
// 换成同一套线性图标，不再是两套风格混排（详见 spec/roadmap.md 首页视觉清理条目）。
const SPARK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 1.9 4.9L19 9l-5.1 1.9L12 16l-1.9-5.1L5 9l5.1-1.9L12 3z"/></svg>';
const TAG_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.6 2.6 21 11l-9 9-8.4-8.4A2 2 0 0 1 3 10.2V4a1 1 0 0 1 1-1h6.2a2 2 0 0 1 1.4.6z"/><circle cx="7.4" cy="7.4" r="1.2" fill="currentColor" stroke="none"/></svg>';
const RESUME_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 13h6M9 17h4"/></svg>';
const MAIL_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 6.5 8.5 6 8.5-6"/></svg>';
const MIC_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v1a7 7 0 0 1-14 0v-1M12 18v4M8 22h8"/></svg>';
const NOTE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
const GLOBE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 4 6 4 9s-1.5 6.4-4 9c-2.5-2.6-4-6-4-9s1.5-6.4 4-9z"/></svg>';
const BUILDING_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="3" width="16" height="18" rx="1"/><path d="M9 21v-4h6v4M9 8h.01M9 12h.01M15 8h.01M15 12h.01"/></svg>';
const INBOX_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11Z"/></svg>';
const CHAT_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';

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

// toast 是全站唯一的反馈通道（约 30 处调用），所以它得能被主动关掉，也得能挂一个
// 行动按钮——"撤销"这类操作没地方放，只能长在提示本身上。
//
// action: { label, onClick } —— 点了就执行 onClick 并立刻收起这条 toast。
function showToast(message, type = 'info', timeout = 4000, action = null) {
  const stack = document.getElementById('toastStack');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `
    <span class="toast-icon">${TOAST_ICONS[type] || TOAST_ICONS.info}</span>
    <span class="toast-text">${escapeHtml(message)}</span>
    ${action ? `<button type="button" class="toast-action">${escapeHtml(action.label)}</button>` : ''}
    <button type="button" class="toast-close" title="关闭">&times;</button>`;

  let dismissTimer = null;
  const dismiss = () => {
    if (dismissTimer) clearTimeout(dismissTimer);
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 200);
  };
  el.querySelector('.toast-close').addEventListener('click', dismiss);
  if (action) {
    el.querySelector('.toast-action').addEventListener('click', () => {
      dismiss();
      action.onClick();
    });
  }

  stack.appendChild(el);
  dismissTimer = setTimeout(dismiss, timeout);
  return el;
}

// 后端对"还没上传简历"统一回 409 + {need_resume: true}（见 app.py 的 need_resume_response）。
// 匹配分析、面试准备、题库起草全都依赖简历，三个页面都可能撞上，所以处理放在 common.js。
//
// 返回 true 表示"这个错误已经在这里处理完了"，调用方直接 return，不要再弹一遍通用错误
// toast——否则用户会同时看到"请先上传简历"和一句看不懂的原始报错。
function handleNeedResume(data) {
  if (!data || !data.need_resume) return false;
  showToast(data.error || data.need_resume_message || '请先上传简历', 'error', 8000, {
    label: '去上传',
    onClick: () => window.open('/resume', '_blank'),
  });
  return true;
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

// ---------- 模型下拉（面试两页 + 简历页共用） ----------
// 模型清单从 /api/models 拉，不在前端再抄一份——抄了迟早会出现"界面上能选、后端不认"。
// 每个功能位（analysis / interview_prep / interview_bank / resume_review）各存各的，
// 切换即存进 config.json。

async function initModelSelect(selectId, task) {
  const el = document.getElementById(selectId);
  if (!el) return;
  try {
    const data = await (await fetch('/api/models')).json();
    const current = (data.llm_tasks || {})[task] || '';
    const groups = {};
    (data.models || []).forEach((m) => {
      (groups[m.provider] = groups[m.provider] || []).push(m);
    });
    el.innerHTML = `
      <option value="">跟随全局设置（${escapeHtml(data.fallback || '未配置')}）</option>
      ${Object.entries(groups).map(([provider, models]) => `
        <optgroup label="${escapeHtml(provider)}">
          ${models.map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label)} — ${escapeHtml(m.note || '')}</option>`).join('')}
        </optgroup>`).join('')}
    `;
    el.value = current;
    el.dataset.previous = current; // 存失败时回退用
  } catch (e) {
    el.innerHTML = '<option value="">模型列表加载失败</option>';
  }
}

async function setTaskModel(task, modelId, el) {
  const previous = el ? el.dataset.previous || '' : '';
  try {
    const res = await fetch('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ llm_tasks: { [task]: modelId } }),
    });
    if (!res.ok) throw new Error((await res.json()).error || '未知错误');
    if (el) el.dataset.previous = modelId;
    // 说清楚"下次生效"：正在后台跑的那次起草/生成用的还是切换前的模型
    showToast('已切换模型，下一次生成/对话生效', 'success', 4000);
  } catch (e) {
    showToast(`切换模型失败：${e.message}`, 'error');
    if (el) el.value = previous; // 存不进去就把下拉退回原样，别让界面显示一个没生效的选择
  }
}

function bulletListHtml(items) {
  if (!items || !items.length) return '<div class="plain-text" style="color:var(--text-faint);">（无）</div>';
  return `<ul class="bullet-list">${items.map((t) => `<li>${escapeHtml(t)}</li>`).join('')}</ul>`;
}

// 任职要求列表（红色=未达标）。原来只在职位列表页的详情弹窗里用，弹窗拆成独立页面
// （/jobs/<id>）之后职位详情页也要用同一个渲染，搬到这里两边共用。
function reqListHtml(items) {
  if (!items || !items.length) return '<div class="plain-text" style="color:var(--text-faint);">（无）</div>';
  return `<ul class="req-list">${items.map((it) => `<li class="${it.is_gap ? 'gap' : ''}">${escapeHtml(it.text)}</li>`).join('')}</ul>`;
}

function safeUrl(url) {
  if (typeof url === 'string' && /^https?:\/\//i.test(url)) return url;
  return '#';
}

// ---------- 对话通用小工具（题库对话 bank.js + 职位对话 job_detail.js 共用） ----------
function onChatKeydown(event, send) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    send();
  }
}

// ---------- 标签编辑（职位列表卡片 + 职位详情页共用） ----------
// 做成一个自建的浮层小弹窗（不依赖任何模板里预先写好的 DOM），两个页面各自的 HTML
// 都不用为它加标记，用完即删。
const PRESET_TAGS = ['AI', 'ML', 'remote', 'tech'];

function openTagEditor(jobId, currentTags, allKnownTags, onSaved) {
  const existing = document.getElementById('tagEditorOverlay');
  if (existing) existing.remove();

  let tags = [...(currentTags || [])];
  const options = Array.from(new Set([...PRESET_TAGS, ...(allKnownTags || [])]));

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay active';
  overlay.id = 'tagEditorOverlay';
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

  function render() {
    overlay.innerHTML = `
      <div class="modal tag-editor-modal">
        <div class="modal-head">
          <h3>编辑标签</h3>
          <button type="button" class="icon-btn" id="tagEditorClose">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
          </button>
        </div>
        <div class="modal-body">
          <div class="tag-current-row">
            ${tags.length ? tags.map((t) => `
              <span class="badge tag-badge">${escapeHtml(t)}
                <button type="button" class="tag-remove" data-tag="${escapeHtml(t)}" title="移除">&times;</button>
              </span>`).join('')
              : '<span class="plain-text" style="color:var(--text-faint);">还没有标签</span>'}
          </div>
          <div class="chip-group tag-preset-row">
            ${options.map((t) => `<button type="button" class="chip tag-option ${tags.includes(t) ? 'active' : ''}" data-tag="${escapeHtml(t)}">${escapeHtml(t)}</button>`).join('')}
          </div>
          <div class="tag-input-row">
            <input type="text" id="tagEditorInput" placeholder="自定义标签，回车添加" maxlength="20">
            <button type="button" class="btn btn-secondary btn-sm" id="tagEditorAddBtn">添加</button>
          </div>
        </div>
        <div class="card-footer">
          <button type="button" class="btn btn-secondary btn-sm" id="tagEditorCancel">取消</button>
          <button type="button" class="btn btn-primary btn-sm" id="tagEditorSave">保存</button>
        </div>
      </div>`;

    overlay.querySelector('#tagEditorClose').addEventListener('click', () => overlay.remove());
    overlay.querySelector('#tagEditorCancel').addEventListener('click', () => overlay.remove());
    overlay.querySelectorAll('.tag-remove').forEach((btn) => {
      btn.addEventListener('click', () => {
        tags = tags.filter((t) => t !== btn.dataset.tag);
        render();
      });
    });
    overlay.querySelectorAll('.tag-option').forEach((btn) => {
      btn.addEventListener('click', () => {
        const t = btn.dataset.tag;
        tags = tags.includes(t) ? tags.filter((x) => x !== t) : [...tags, t];
        render();
      });
    });
    const addTag = () => {
      const input = overlay.querySelector('#tagEditorInput');
      const v = input.value.trim();
      if (!v) return;
      if (v.includes(',') || v.includes('，')) {
        showToast('标签不能包含逗号', 'error');
        return;
      }
      if (!tags.includes(v)) tags = [...tags, v];
      render();
    };
    overlay.querySelector('#tagEditorAddBtn').addEventListener('click', addTag);
    overlay.querySelector('#tagEditorInput').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); addTag(); }
    });
    overlay.querySelector('#tagEditorSave').addEventListener('click', async () => {
      try {
        const res = await fetch(`/api/jobs/${jobId}/tags`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tags }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || '未知错误');
        overlay.remove();
        showToast('标签已保存', 'success', 2000);
        if (onSaved) onSaved(data.tags);
      } catch (e) {
        showToast(`保存失败：${e.message}`, 'error');
      }
    });
  }

  render();
  document.body.appendChild(overlay);
}

// ---------- 忽略原因（职位列表页 dismiss 流程 + 已忽略卡片补录入口共用） ----------
// 预设原因来自 spec/product-review.md 的 P0-3。跟标签编辑器结构类似（浮层小弹窗、
// 不依赖模板预先写好的 DOM），但不合并成同一个函数——字段语义不同（这里是"原因"，
// 不是"标签"），硬凑复用会两头都不干净。
const DISMISS_REASON_TAGS = ['薪资不符', '职能不对', '公司不感兴趣', '地点', '行业', '层级不匹配'];

// jobId：要记原因的职位。onSaved：保存成功后的回调（没有参数），用于调用方刷新界面上的
// "已记录原因"标记。跳过/直接关闭都不算错误，静默即可——原因收集全程是可选的，
// 不能让用户觉得"不填就报错"。
function openDismissReasonPrompt(jobId, onSaved) {
  const existing = document.getElementById('dismissReasonOverlay');
  if (existing) existing.remove();

  let tags = [];

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay active';
  overlay.id = 'dismissReasonOverlay';
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

  overlay.innerHTML = `
    <div class="modal tag-editor-modal">
      <div class="modal-head">
        <h3>为什么不考虑这个职位？</h3>
        <button type="button" class="icon-btn" id="dismissReasonClose">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
        </button>
      </div>
      <div class="modal-body">
        <div class="plain-text" style="color:var(--text-faint); margin-bottom:8px;">选填——攒够几条之后会帮你总结出求职偏好，让以后的匹配分析更懂你。</div>
        <div class="chip-group tag-preset-row" id="dismissReasonChips">
          ${DISMISS_REASON_TAGS.map((t) => `<button type="button" class="chip" data-tag="${escapeHtml(t)}">${escapeHtml(t)}</button>`).join('')}
        </div>
        <textarea id="dismissReasonNote" rows="2" maxlength="300" placeholder="也可以自己写几句（选填）" style="width:100%; margin-top:10px; resize:vertical;"></textarea>
      </div>
      <div class="card-footer">
        <button type="button" class="btn btn-secondary btn-sm" id="dismissReasonSkip">跳过</button>
        <button type="button" class="btn btn-primary btn-sm" id="dismissReasonSave">记录</button>
      </div>
    </div>`;

  overlay.querySelector('#dismissReasonClose').addEventListener('click', () => overlay.remove());
  overlay.querySelector('#dismissReasonSkip').addEventListener('click', () => overlay.remove());
  overlay.querySelectorAll('#dismissReasonChips .chip').forEach((btn) => {
    btn.addEventListener('click', () => {
      const t = btn.dataset.tag;
      tags = tags.includes(t) ? tags.filter((x) => x !== t) : [...tags, t];
      btn.classList.toggle('active');
    });
  });
  overlay.querySelector('#dismissReasonSave').addEventListener('click', async () => {
    const note = overlay.querySelector('#dismissReasonNote').value.trim();
    if (!tags.length && !note) { overlay.remove(); return; }
    try {
      const res = await fetch(`/api/jobs/${jobId}/dismiss_reason`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tags, note }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || '未知错误');
      overlay.remove();
      showToast('已记录，谢谢反馈', 'success', 2000);
      if (onSaved) onSaved();
    } catch (e) {
      showToast(`记录失败：${e.message}`, 'error');
    }
  });

  document.body.appendChild(overlay);
}
