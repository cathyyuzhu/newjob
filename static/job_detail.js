// 职位详情页（/jobs/<id>）：以前是主页上的「分析详情」弹窗，只放匹配分析结论。
// 现在加了 AI 对话和备注，两样都需要长时间挂着交互，弹窗的老毛病（轮询跟弹窗生命周期
// 绑死、内容塞进内滚容器、没有独立URL）又冒出来一遍——跟当年面试准备搬出弹窗是
// 同一个理由，见 spec/tech-solution.md。
//
// 跟 common.js 一起加载，不依赖 app.js（那边绑死了职位列表的 DOM）。

const DETAIL_JOB_ID = window.DETAIL_JOB_ID;

let currentJob = null;
let currentAnalysis = null; // 追踪表里对应的那一行（可能没有）
let notes = [];
let chat = { draft: '', sending: false, messages: [] };
let materialsPollTimer = null;

// ---------- 加载 ----------

async function loadJobHeader() {
  const res = await fetch(`/api/jobs/${DETAIL_JOB_ID}`);
  if (!res.ok) return;
  currentJob = await res.json();
  const title = `${currentJob.title || ''} · ${currentJob.company || ''}`;
  document.getElementById('detailJobTitle').textContent = title;
  document.title = `职位详情 · ${title}`;
  const link = document.getElementById('detailJobUrl');
  if (currentJob.job_url) link.href = safeUrl(currentJob.job_url);
  else link.style.display = 'none';
  document.getElementById('detailPrepLink').href = `/jobs/${DETAIL_JOB_ID}/interview`;
  updateDismissBtnVisibility();
}

function updateDismissBtnVisibility() {
  const btn = document.getElementById('detailDismissBtn');
  // 已经忽略过的就别再显示"忽略"了——按了也没有任何变化，只会让人以为没生效
  if (btn) btn.style.display = currentJob && currentJob.status === 'dismissed' ? 'none' : '';
}

async function dismissFromDetailPage() {
  const previousStatus = currentJob ? currentJob.status : 'new';
  try {
    const res = await fetch(`/api/jobs/${DETAIL_JOB_ID}/status`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: 'dismissed' }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || '未知错误');
    currentJob.status = 'dismissed';
    updateDismissBtnVisibility();
    showToast('已标记为「已忽略」', 'success', 6000, {
      label: '撤销',
      onClick: () => undoDismissFromDetailPage(previousStatus),
    });
  } catch (e) {
    showToast(`操作失败：${e.message}`, 'error');
  }
}

async function undoDismissFromDetailPage(previousStatus) {
  try {
    const res = await fetch(`/api/jobs/${DETAIL_JOB_ID}/status`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: previousStatus }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || '未知错误');
    currentJob.status = previousStatus;
    updateDismissBtnVisibility();
  } catch (e) {
    showToast(`撤销失败：${e.message}`, 'error');
  }
}

async function loadAnalysis() {
  try {
    const res = await fetch(`/api/jobs/${DETAIL_JOB_ID}/analysis`);
    currentAnalysis = res.ok ? await res.json() : {};
  } catch (e) {
    currentAnalysis = {};
  }
  renderMain();
  if (currentJob && currentJob.materials_state) startMaterialsPoll();
}

async function loadNotes() {
  try {
    const res = await fetch(`/api/jobs/${DETAIL_JOB_ID}/notes`);
    notes = res.ok ? await res.json() : [];
  } catch (e) {
    notes = [];
  }
  renderNotes();
}

// ---------- 左栏：匹配分析 + 标签 + 材料 ----------

function renderMain() {
  const root = document.getElementById('detailMain');
  const job = currentJob;
  const e = currentAnalysis || {};
  const hasAnalysis = job && job.overall_match != null;

  if (!hasAnalysis) {
    root.innerHTML = `
      ${tagsSectionHtml()}
      <div class="detail-section">
        <div class="plain-text" style="color:var(--text-faint);">这条职位还没有完成 AI 匹配分析，回<a class="job-link" href="/">职位列表</a>点「AI 分析」看结果。</div>
      </div>
    `;
    return;
  }

  const hasTrackerEntry = e && Object.keys(e).length > 0;
  root.innerHTML = `
    ${tagsSectionHtml()}
    ${!hasTrackerEntry ? `
      <div class="detail-section"><div class="plain-text" style="color:var(--text-faint);">未在追踪表里找到这条职位的匹配分析记录（可能追踪表路径变了）。定制简历/Cover Letter 仍然可以生成。</div></div>
    ` : `
      <div class="detail-section">
        <h4>公司简介</h4>
        <div class="plain-text">${escapeHtml(e.company_overview || '（未获取到公司简介）')}</div>
      </div>
      <div class="detail-section">
        <h4>职位内容</h4>
        ${bulletListHtml(e.job_content_bullets)}
      </div>
      <div class="detail-section">
        <h4>任职要求（红色 = 未达标）</h4>
        ${reqListHtml(e.requirement_items)}
      </div>
      <div class="detail-grid">
        <div class="detail-section">
          <h4>技能匹配 · 已匹配</h4>
          ${bulletListHtml(e.skill_matched_bullets)}
        </div>
        <div class="detail-section">
          <h4>技能匹配 · 未达标</h4>
          ${bulletListHtml(e.skill_gap_bullets)}
        </div>
      </div>
      <div class="detail-grid">
        <div class="detail-section">
          <h4>相关经验年限</h4>
          <div class="plain-text">${escapeHtml(e.experience_years || '—')}</div>
        </div>
        <div class="detail-section">
          <h4>薪资范围</h4>
          <div class="plain-text">${escapeHtml(e.salary || '—')}</div>
        </div>
      </div>
      <div class="detail-section">
        <h4>行业背景</h4>
        ${bulletListHtml(e.industry_bullets)}
      </div>
      <div class="detail-section">
        <h4>团队规模 / 汇报线</h4>
        ${bulletListHtml(e.team_bullets)}
      </div>
      <div class="detail-section">
        <h4>地理位置 / 远程要求</h4>
        <div class="plain-text">${escapeHtml(e.location || '—')}</div>
      </div>
    `}
    ${materialsSectionHtml()}
  `;
}

function tagsSectionHtml() {
  const tags = (currentJob.tags || '').split(',').map((t) => t.trim()).filter(Boolean);
  return `
    <div class="detail-section">
      <h4>标签</h4>
      <div class="job-tags-row">
        ${tags.map((t) => `<span class="badge tag-badge">${escapeHtml(t)}</span>`).join('')}
        <button type="button" class="btn btn-secondary btn-sm" onclick="editDetailTags()">${TAG_ICON}编辑标签</button>
      </div>
    </div>
  `;
}

function editDetailTags() {
  const tags = (currentJob.tags || '').split(',').map((t) => t.trim()).filter(Boolean);
  openTagEditor(DETAIL_JOB_ID, tags, [], (savedTags) => {
    currentJob.tags = savedTags.join(',');
    renderMain();
  });
}

function materialsSectionHtml() {
  const job = currentJob;
  const busy = job.materials_state === 'queued' || job.materials_state === 'generating';
  if (busy) {
    return `
      <div class="detail-section">
        <h4>定制简历 / Cover Letter</h4>
        <div class="plain-text" style="color:var(--text-faint);"><span class="spinner"></span> ${job.materials_state === 'queued' ? '排队中…' : '生成中…'}</div>
      </div>
    `;
  }
  const hasMaterials = job.resume_path || job.cover_letter;
  return `
    <div class="detail-section">
      <h4>定制简历</h4>
      ${job.resume_path
        ? `<a class="job-title" href="/api/jobs/${job.id}/resume" target="_blank" rel="noopener noreferrer">打开定制简历 ↗</a>
           <div style="margin-top:0.5rem;">${bulletListHtml(parseResumeBullets(job.resume_bullets))}</div>`
        : '<div class="plain-text">（未生成）</div>'}
    </div>
    <div class="detail-section">
      <h4>Cover Letter</h4>
      <div class="plain-text">${job.cover_letter ? escapeHtml(job.cover_letter) : '（未生成）'}</div>
    </div>
    <div class="detail-section">
      <button type="button" class="btn btn-primary btn-sm" onclick="generateMaterials(this)">
        ${SPARK_ICON}${hasMaterials ? '重新生成' : '生成'}定制简历 + Cover Letter
      </button>
    </div>
  `;
}

function parseResumeBullets(raw) {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch (e) {
    return [];
  }
}

async function generateMaterials(btn) {
  setBtnLoading(btn, '启动中…');
  try {
    const res = await fetch(`/api/jobs/${DETAIL_JOB_ID}/generate_materials`, { method: 'POST' });
    const data = await res.json();
    if (handleNeedResume(data)) return;
    if (!res.ok) throw new Error(data.error || '未知错误');
    showToast('已开始生成，可能需要 30-60 秒，完成后这里会自动刷新', 'info', 6000);
    currentJob.materials_state = 'generating';
    renderMain();
    startMaterialsPoll();
  } catch (e) {
    showToast(`启动失败：${e.message}`, 'error', 6000);
  } finally {
    restoreBtn(btn);
  }
}

function startMaterialsPoll() {
  if (materialsPollTimer) return;
  materialsPollTimer = setTimeout(async () => {
    materialsPollTimer = null;
    if (document.hidden) { startMaterialsPoll(); return; }
    try {
      const res = await fetch(`/api/jobs/${DETAIL_JOB_ID}`);
      if (res.ok) currentJob = await res.json();
    } catch (e) { /* 下一轮再试 */ }
    if (currentJob && (currentJob.materials_state === 'queued' || currentJob.materials_state === 'generating')) {
      startMaterialsPoll();
    } else {
      showToast('材料生成完成', 'success', 3000);
      await loadAnalysis();
    }
  }, 4000);
}

// ---------- 右栏：AI 对话 ----------

function renderChat() {
  const body = document.getElementById('jobChatBody');
  body.innerHTML = `
    <div class="bank-chat-msgs">
      ${chat.messages.length ? chat.messages.map((m, idx) => jobChatMsgHtml(m, idx)).join('')
        : '<div class="bank-chat-empty">问点什么都行，比如「这家公司最近有什么新闻」「这个职级大概对应我现在的什么水平」「JD里这句话是什么意思」。回复觉得有用可以点「记进备注」。</div>'}
      ${chat.sending ? '<div class="bank-chat-msg ai pending"><span class="spinner"></span>AI 正在想…</div>' : ''}
    </div>
    <div class="bank-chat-input">
      <textarea id="jobChatInput" rows="2" placeholder="想问什么？（回车发送，Shift+回车换行）"
        ${chat.sending ? 'disabled' : ''}
        oninput="chat.draft = this.value"
        onkeydown="onChatKeydown(event, sendJobChat)">${escapeHtml(chat.draft)}</textarea>
      <button class="btn btn-primary btn-sm" ${chat.sending ? 'disabled' : ''} onclick="sendJobChat()">发送</button>
    </div>
  `;
  const msgs = body.querySelector('.bank-chat-msgs');
  if (msgs) msgs.scrollTop = msgs.scrollHeight;
}

function jobChatMsgHtml(msg, idx) {
  if (msg.role === 'user') {
    return `<div class="bank-chat-msg user">${escapeHtml(msg.content)}</div>`;
  }
  if (msg.error) {
    return `<div class="bank-chat-msg error">${escapeHtml(msg.content)}</div>`;
  }
  return `
    <div class="bank-chat-msg ai">
      <div>${escapeHtml(msg.content)}</div>
      <button class="btn btn-secondary btn-sm" style="margin-top:0.4rem;" onclick="saveNoteFromChat(${idx})">📌 记进备注</button>
    </div>
  `;
}

// 发给后端的历史：跟题库对话不同，这里的回复本身就是纯文本，不用再包一层 JSON。
function jobChatHistoryFor(messages) {
  return messages.filter((m) => !m.error).map((m) => ({ role: m.role, content: m.content }));
}

async function sendJobChat() {
  const message = (chat.draft || '').trim();
  if (!message || chat.sending) return;

  chat.messages.push({ role: 'user', content: message });
  chat.draft = '';
  chat.sending = true;
  renderChat();

  try {
    const res = await fetch(`/api/jobs/${DETAIL_JOB_ID}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, history: jobChatHistoryFor(chat.messages.slice(0, -1)) }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '未知错误');
    chat.messages.push({ role: 'assistant', content: data.reply });
  } catch (e) {
    chat.messages.push({ role: 'assistant', error: true, content: `出错了：${e.message}` });
  } finally {
    chat.sending = false;
    renderChat();
  }
}

async function saveNoteFromChat(idx) {
  const msg = chat.messages[idx];
  if (!msg || !msg.content) return;
  try {
    const res = await fetch(`/api/jobs/${DETAIL_JOB_ID}/notes`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: msg.content, source: 'chat' }),
    });
    if (!res.ok) throw new Error((await res.json()).error || '未知错误');
    showToast('已记进备注', 'success', 2000);
    await loadNotes();
  } catch (e) {
    showToast(`记录失败：${e.message}`, 'error');
  }
}

// ---------- 右栏：备注 ----------

function renderNotes() {
  const body = document.getElementById('jobNotesBody');
  body.innerHTML = `
    <div class="note-list">
      ${notes.length ? notes.map((n) => noteItemHtml(n)).join('')
        : '<div class="plain-text" style="color:var(--text-faint); padding:0.3rem 0;">还没有备注</div>'}
    </div>
    <div class="note-input-row">
      <textarea id="noteInput" rows="2" placeholder="手动写点备注…"></textarea>
      <button class="btn btn-secondary btn-sm" onclick="addManualNote()">添加</button>
    </div>
  `;
}

function noteItemHtml(n) {
  const sourceLabel = n.source === 'chat' ? '[AI]' : '[手写]';
  return `
    <div class="note-item">
      <div class="note-meta">
        <span class="note-source ${n.source === 'chat' ? 'ai' : ''}">${sourceLabel}</span>
        <span class="note-time">${escapeHtml((n.created_at || '').replace('T', ' '))}</span>
        <button type="button" class="note-remove" onclick="deleteNote(${n.id})" title="删除">&times;</button>
      </div>
      <div class="note-content">${escapeHtml(n.content)}</div>
    </div>
  `;
}

async function addManualNote() {
  const input = document.getElementById('noteInput');
  const content = input.value.trim();
  if (!content) return;
  try {
    const res = await fetch(`/api/jobs/${DETAIL_JOB_ID}/notes`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, source: 'manual' }),
    });
    if (!res.ok) throw new Error((await res.json()).error || '未知错误');
    await loadNotes();
  } catch (e) {
    showToast(`添加失败：${e.message}`, 'error');
  }
}

async function deleteNote(noteId) {
  if (!window.confirm('确定删除这条备注吗？删除后不可恢复。')) return;
  try {
    const res = await fetch(`/api/notes/${noteId}`, { method: 'DELETE' });
    if (!res.ok) throw new Error((await res.json()).error || '未知错误');
    await loadNotes();
  } catch (e) {
    showToast(`删除失败：${e.message}`, 'error');
  }
}

// ---------- init ----------
document.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  initModelSelect('detailChatModelSelect', 'job_chat');
  renderChat();
  await loadJobHeader();
  await loadAnalysis();
  await loadNotes();
});
