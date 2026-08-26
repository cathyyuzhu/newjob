// 面试语音练习页（/interview/practice）的全部逻辑。跟 common.js 一起加载，不依赖 app.js。
//
// 独立于具体职位（不像 interview.js 那样挂在某个 job_id 下）：上传的准备文档往往是用户
// 自己整理的复合材料，不对应库里任何一条职位。三个状态共享同一个页面：文档库 → 练习态
// → （练习态里回看已作答的题就是回顾态，不单独做一个页面）。

// ---------- state ----------
let practiceDocs = [];               // 文档库列表（不含正文，见 list_interview_docs_route）
let setsByDoc = {};                  // { docId: [practice_set, ...] }，懒加载，见 loadDocSets()
let selectedVersionByDoc = {};       // { docId: setId }，文档卡片上选中的题目集版本
let docPollTimer = null;

let view = 'library';                // 'library' | 'runner'
let activeDocId = null;
let currentSetId = null;
let currentSet = null;               // 当前练习题集详情（含 content_json 解析结果 + answers）
let currentContent = null;           // JSON.parse(currentSet.content_json)
let answersByQid = {};               // { questionId: {transcript, score_json, error, ...} }
let currentQuestionIndex = 0;

let recognition = null;
let recording = false;
let recFinalText = '';
let recInterimText = '';
let recLang = 'zh-CN';
let recTimerHandle = null;
let recStartedAt = null;
const draftByQid = {};               // 未提交的作答草稿，切题不丢（{ questionId: text }）

// ---------- 小工具（同 resume.js 的 scoreClass/pct，各页面各存一份，不值得为两行代码抽共享） ----------
function scoreClass(score) {
  if (score >= 0.7) return 'match-high';
  if (score >= 0.4) return 'match-mid';
  return 'match-low';
}
function pct(score) {
  return `${Math.round((score || 0) * 100)}%`;
}

// ==================================================================== 文档库

async function loadDocs() {
  const root = document.getElementById('practiceRoot');
  try {
    practiceDocs = await (await fetch('/api/interview/practice/docs')).json();
  } catch (e) {
    root.innerHTML = `<div class="plain-text" style="color:var(--danger);">加载失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  await Promise.all(practiceDocs.map((d) => loadDocSets(d.id)));
  renderLibrary();
  scheduleDocPoll();
}

async function loadDocSets(docId) {
  try {
    const res = await fetch(`/api/interview/practice/sets?doc_id=${docId}`);
    setsByDoc[docId] = res.ok ? await res.json() : [];
  } catch (e) {
    setsByDoc[docId] = [];
  }
  if (!selectedVersionByDoc[docId] && setsByDoc[docId].length) {
    const latestOk = setsByDoc[docId].find((s) => !s.error && s.content_json);
    selectedVersionByDoc[docId] = (latestOk || setsByDoc[docId][0]).id;
  }
}

function renderLibrary() {
  document.title = '面试语音练习';
  document.getElementById('practiceTitle').textContent = '面试语音练习';
  const root = document.getElementById('practiceRoot');
  root.innerHTML = `
    <input type="file" id="docFileInput" accept=".pdf" style="display:none;" onchange="onDocFileChange(this)">
    <div class="card">
      <div class="upload-drop" id="docUploadDrop" onclick="pickDocFile()">
        <div class="upload-icon">${RESUME_ICON}</div>
        <div class="upload-title">上传一份准备文档，AI 帮你出练习题</div>
        <div class="upload-desc">
          拖到这里，或点击选择文件。只收 <b>.pdf</b>——自我介绍、STAR 故事、公司背景研究、
          常见追问这类你自己整理的准备材料都可以。上传后不用先在这里逐字看完，直接点「生成题目」，
          AI 会优先从文档里已经列好的问题改编出一套练习题。
        </div>
        <button class="btn btn-primary btn-sm" onclick="event.stopPropagation(); pickDocFile()">选择 .pdf 文件</button>
      </div>
    </div>
    ${practiceDocs.length ? `<div class="card"><div class="card-head"><div><h2>已上传的文档</h2></div></div>${practiceDocs.map(docCardHtml).join('')}</div>` : ''}
  `;
  bindDocDropZone();
}

function docCardHtml(doc) {
  const sets = setsByDoc[doc.id] || [];
  const okSets = sets.filter((s) => !s.error && s.content_json);
  const selectedId = selectedVersionByDoc[doc.id];
  const selected = sets.find((s) => s.id === selectedId);
  const generating = !!doc.generating;

  const versionSelect = okSets.length > 1 ? `
    <select class="app-status-select" onchange="selectDocVersion(${doc.id}, this.value)" title="题目版本">
      ${okSets.map((s) => `<option value="${s.id}" ${s.id === selectedId ? 'selected' : ''}>
        ${escapeHtml((s.created_at || '').replace('T', ' '))}${s.round_label ? ` · ${escapeHtml(s.round_label)}` : ''}
        （${(JSON.parse(s.content_json).questions || []).length} 题）
      </option>`).join('')}
    </select>` : '';

  return `
    <div class="practice-doc-card" style="padding:0.9rem 0; border-top:1px solid var(--border);">
      <div class="practice-doc-info">
        <div class="practice-doc-name">${RESUME_ICON}${escapeHtml(doc.filename || '未命名文档')}</div>
        <div class="practice-doc-sub">
          上传于 ${escapeHtml((doc.uploaded_at || '').replace('T', ' '))} · ${doc.char_count || 0} 字
          ${okSets.length ? ` · 已生成 ${okSets.length} 套题目` : ''}
          ${sets.some((s) => s.error) && !okSets.length ? ` · <span style="color:var(--danger);">上次生成失败：${escapeHtml(sets.find((s) => s.error).error)}</span>` : ''}
        </div>
        ${versionSelect}
      </div>
      <div class="practice-doc-actions">
        ${!generating ? `<input type="text" class="practice-round-input" id="roundInput-${doc.id}" placeholder="轮次提示，选填（如：二面）" maxlength="20" style="width:11rem;">` : ''}
        <button class="btn btn-secondary btn-sm" ${generating ? 'disabled' : ''} onclick="generatePracticeSet(${doc.id}, this)">
          ${generating ? '<span class="spinner"></span>生成中…' : (okSets.length ? '🔄 重新生成' : '✨ 生成题目')}
        </button>
        ${selected && !generating ? `<button class="btn btn-primary btn-sm" onclick="openPracticeSet(${doc.id}, ${selected.id})">开始练习 →</button>` : ''}
        <button class="btn btn-danger-ghost btn-sm" onclick="deleteDoc(${doc.id}, this)">删除</button>
      </div>
    </div>
  `;
}

function bindDocDropZone() {
  const zone = document.getElementById('docUploadDrop');
  if (!zone) return;
  ['dragenter', 'dragover'].forEach((evt) => {
    zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.add('dragging'); });
  });
  ['dragleave', 'drop'].forEach((evt) => {
    zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.remove('dragging'); });
  });
  zone.addEventListener('drop', (e) => {
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) uploadDocFile(file);
  });
}

function pickDocFile() {
  document.getElementById('docFileInput').click();
}

function onDocFileChange(input) {
  const file = input.files && input.files[0];
  input.value = ''; // 清空 value，选同一个文件第二次也能触发 change（比如上传失败后重试）
  if (file) uploadDocFile(file);
}

async function uploadDocFile(file) {
  if (!file.name.toLowerCase().endsWith('.pdf')) {
    showToast('只支持 .pdf 格式的准备文档', 'error');
    return;
  }
  const form = new FormData();
  form.append('file', file);
  showToast(`正在上传 ${file.name}…`, 'info', 3000);
  try {
    const res = await fetch('/api/interview/practice/docs/upload', { method: 'POST', body: form });
    if (res.status === 413) throw new Error('文件太大（上限 20MB）');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '上传失败');
    showToast('文档已上传，点「生成题目」开始出题', 'success');
    await loadDocs();
  } catch (e) {
    showToast(`上传失败：${e.message}`, 'error', 8000);
  }
}

async function deleteDoc(docId, btn) {
  if (!window.confirm('删除这份文档会连带删除它生成的所有练习题和作答记录，确定吗？')) return;
  setBtnLoading(btn, '删除中…');
  try {
    const res = await fetch(`/api/interview/practice/docs/${docId}`, { method: 'DELETE' });
    if (!res.ok) throw new Error((await res.json()).error || '未知错误');
    delete setsByDoc[docId];
    delete selectedVersionByDoc[docId];
    showToast('已删除', 'success', 2000);
    await loadDocs();
  } catch (e) {
    showToast(`删除失败：${e.message}`, 'error');
    restoreBtn(btn);
  }
}

function selectDocVersion(docId, setId) {
  selectedVersionByDoc[docId] = Number(setId);
  renderLibrary();
}

async function generatePracticeSet(docId, btn) {
  const input = document.getElementById(`roundInput-${docId}`);
  const roundLabel = input ? input.value.trim() : '';
  setBtnLoading(btn, '启动中…');
  try {
    const res = await fetch(`/api/interview/practice/docs/${docId}/generate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ round_label: roundLabel }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '未知错误');
    showToast('已开始出题，可能需要 1-2 分钟，完成后这里会自动刷新', 'info', 6000);
    const doc = practiceDocs.find((d) => d.id === docId);
    if (doc) doc.generating = true;
    renderLibrary();
    scheduleDocPoll();
  } catch (e) {
    showToast(`启动失败：${e.message}`, 'error', 6000);
    restoreBtn(btn);
  }
}

// ---------- 文档库轮询：只要还有文档在生成题目，就每隔几秒刷新一次 ----------
function scheduleDocPoll() {
  if (docPollTimer) return;
  if (!practiceDocs.some((d) => d.generating)) return;
  docPollTimer = setTimeout(async () => {
    docPollTimer = null;
    if (document.hidden) { scheduleDocPoll(); return; }
    try {
      practiceDocs = await (await fetch('/api/interview/practice/docs')).json();
      // 不再 generating 的文档，重新拉一次它的题目集——这一轮题目已经落库了（成功或
      // 失败都算），版本下拉和"开始练习"按钮要跟着更新。
      await Promise.all(practiceDocs.filter((d) => !d.generating).map((d) => loadDocSets(d.id)));
    } catch (e) { /* 静默重试 */ }
    if (view === 'library') renderLibrary();
    scheduleDocPoll();
  }, 4000);
}

// ==================================================================== 练习态

async function openPracticeSet(docId, setId) {
  activeDocId = docId;
  currentSetId = setId;
  view = 'runner';
  currentQuestionIndex = 0;
  answersByQid = {};
  const root = document.getElementById('practiceRoot');
  root.innerHTML = '<div class="plain-text" style="color:var(--text-faint);">加载中…</div>';
  await loadPracticeSet();
}

async function loadPracticeSet() {
  try {
    const res = await fetch(`/api/interview/practice/sets/${currentSetId}`);
    if (!res.ok) throw new Error((await res.json()).error || '未知错误');
    currentSet = await res.json();
  } catch (e) {
    document.getElementById('practiceRoot').innerHTML =
      `<div class="plain-text" style="color:var(--danger);">加载失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  currentContent = currentSet.content_json ? JSON.parse(currentSet.content_json) : { questions: [] };
  answersByQid = {};
  (currentSet.answers || []).forEach((a) => { answersByQid[a.question_id] = a; });
  renderRunner();
}

function backToLibrary() {
  stopRecording();
  view = 'library';
  currentSet = null;
  currentContent = null;
  renderLibrary();
  scheduleDocPoll();
}

function renderRunner() {
  const questions = currentContent.questions || [];
  const q = questions[currentQuestionIndex];
  document.title = `面试语音练习 · ${currentContent.round_label || ''}`;
  document.getElementById('practiceTitle').textContent = `面试语音练习 · ${currentContent.round_label || ''}`;

  const root = document.getElementById('practiceRoot');
  if (!questions.length) {
    root.innerHTML = `
      <button class="btn btn-secondary btn-sm" style="margin-bottom:1rem;" onclick="backToLibrary()">← 返回文档列表</button>
      <div class="plain-text" style="color:var(--text-faint);">这套题目是空的。</div>`;
    return;
  }

  root.innerHTML = `
    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:0.8rem; flex-wrap:wrap; gap:0.5rem;">
      <button class="btn btn-secondary btn-sm" onclick="backToLibrary()">← 返回文档列表</button>
      ${(currentContent.interview_tips || []).length ? `
        <details class="prep-item" style="flex-basis:100%; margin-top:0.4rem;">
          <summary>评分标准（来自你的文档，打分时会参考这些）</summary>
          <div class="prep-item-body">${bulletListHtml(currentContent.interview_tips)}</div>
        </details>` : ''}
    </div>

    <div class="practice-progress-row">
      <span class="practice-progress-count">${currentQuestionIndex + 1} / ${questions.length}</span>
      <div class="practice-progress-bar"><div class="practice-progress-fill" style="width:${Math.round(((currentQuestionIndex + 1) / questions.length) * 100)}%"></div></div>
      <span class="badge">${escapeHtml(q.category || '')}</span>
    </div>

    <div class="practice-q-nav">
      ${questions.map((qq, i) => {
        const ans = answersByQid[qq.id];
        const cls = i === currentQuestionIndex ? 'current' : (ans && ans.score_json ? `answered ${scoreClass(JSON.parse(ans.score_json).overall_score)}` : '');
        return `<div class="practice-q-dot ${cls}" title="${escapeHtml(qq.question)}" onclick="jumpToQuestion(${i})">${i + 1}</div>`;
      }).join('')}
    </div>

    <div class="detail-section practice-q-card">
      <div class="practice-q-text">${escapeHtml(q.question)}</div>
      ${q.source_hint ? `<div class="practice-q-source">${escapeHtml(q.source_hint)}</div>` : ''}
      ${(q.why_asked || (q.answer_points || []).length) ? `
        <details class="prep-item">
          <summary>为什么会问 / 提示要点（建议先自己想，答完再看）</summary>
          <div class="prep-item-body">
            ${q.why_asked ? `<div class="plain-text" style="color:var(--text-faint);">${escapeHtml(q.why_asked)}</div>` : ''}
            ${(q.answer_points || []).length ? `<div style="margin-top:0.4rem;">${bulletListHtml(q.answer_points)}</div>` : ''}
          </div>
        </details>` : ''}
    </div>

    ${recorderHtml(q)}
    ${answerScoreHtml(answersByQid[q.id])}

    <div style="display:flex; justify-content:space-between; margin-top:1rem;">
      <button class="btn btn-secondary btn-sm" ${currentQuestionIndex === 0 ? 'disabled' : ''} onclick="jumpToQuestion(${currentQuestionIndex - 1})">← 上一题</button>
      <button class="btn btn-secondary btn-sm" ${currentQuestionIndex === questions.length - 1 ? 'disabled' : ''} onclick="jumpToQuestion(${currentQuestionIndex + 1})">下一题 →</button>
    </div>
  `;

  initRecorderUI(q);
}

function jumpToQuestion(index) {
  const questions = currentContent.questions || [];
  if (index < 0 || index >= questions.length) return;
  stopRecording();
  currentQuestionIndex = index;
  renderRunner();
}

// ---------- 录音 / 语音转文字 ----------

function speechSupported() {
  return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
}

function recorderHtml(q) {
  const draft = draftByQid[q.id] !== undefined ? draftByQid[q.id] : '';
  if (!speechSupported()) {
    return `
      <div class="detail-section">
        <h4>作答（你的浏览器不支持语音识别，请直接打字）</h4>
        <textarea class="practice-transcript" id="practiceTranscript" rows="4"
          placeholder="在这里打字作答…" oninput="draftByQid['${q.id}'] = this.value">${escapeHtml(draft)}</textarea>
        <div class="practice-rec-row">
          <button class="btn btn-primary btn-sm" onclick="submitAnswer()">提交打分</button>
        </div>
      </div>`;
  }
  return `
    <div class="detail-section">
      <h4>作答</h4>
      <div class="practice-rec-row">
        <button class="btn btn-secondary btn-sm" id="recToggleBtn" onclick="toggleRecording()">${MIC_ICON} 开始录音</button>
        <span class="practice-lang-toggle chip-group" id="recLangToggle">
          <button type="button" class="chip ${recLang === 'zh-CN' ? 'active' : ''}" onclick="setRecLang('zh-CN')">中文</button>
          <button type="button" class="chip ${recLang === 'en-US' ? 'active' : ''}" onclick="setRecLang('en-US')">English</button>
        </span>
        <span class="practice-timer" id="recTimer" style="display:none;"></span>
      </div>
      <textarea class="practice-transcript" id="practiceTranscript" rows="4"
        placeholder="点「开始录音」说话，文字会实时出现在这里；停止后可以手动修正识别错误。"
        oninput="draftByQid['${q.id}'] = this.value">${escapeHtml(draft)}</textarea>
      <div class="practice-rec-row">
        <button class="btn btn-primary btn-sm" onclick="submitAnswer()">提交打分</button>
      </div>
    </div>`;
}

function initRecorderUI(q) {
  recording = false;
  recFinalText = '';
  recInterimText = '';
  updateRecToggleBtn();
}

function setRecLang(lang) {
  if (recording) { showToast('先停止当前录音再切换语言', 'error'); return; }
  recLang = lang;
  document.querySelectorAll('#recLangToggle .chip').forEach((el) => el.classList.remove('active'));
  const target = Array.from(document.querySelectorAll('#recLangToggle .chip'))
    .find((el) => el.textContent.trim() === (lang === 'zh-CN' ? '中文' : 'English'));
  if (target) target.classList.add('active');
}

function updateRecToggleBtn() {
  const btn = document.getElementById('recToggleBtn');
  if (!btn) return;
  btn.innerHTML = recording ? `${MIC_ICON} 停止并提交转写` : `${MIC_ICON} 开始录音`;
  const textarea = document.getElementById('practiceTranscript');
  if (textarea) textarea.readOnly = recording;
}

function toggleRecording() {
  if (recording) stopRecording();
  else startRecording();
}

function startRecording() {
  const SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognitionCtor) return;

  const q = (currentContent.questions || [])[currentQuestionIndex];
  recFinalText = draftByQid[q.id] || '';
  recInterimText = '';

  recognition = new SpeechRecognitionCtor();
  recognition.lang = recLang;
  recognition.continuous = true;
  recognition.interimResults = true;

  recognition.onresult = (event) => {
    let interim = '';
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const transcript = event.results[i][0].transcript;
      if (event.results[i].isFinal) recFinalText += transcript;
      else interim += transcript;
    }
    recInterimText = interim;
    const textarea = document.getElementById('practiceTranscript');
    if (textarea) textarea.value = recFinalText + recInterimText;
  };
  recognition.onerror = (event) => {
    if (event.error === 'no-speech') return; // 静音超时很常见，不算真正的错误，不打断
    showToast(`语音识别出错：${event.error}`, 'error');
    stopRecording();
  };
  recognition.onend = () => {
    // 部分浏览器在静音一段时间后会自己结束识别；如果用户没有手动点停止，就地重启，
    // 避免用户还在讲、识别却已经安静地断掉了。
    if (recording) {
      try { recognition.start(); } catch (e) { /* 已经在跑或已销毁，忽略 */ }
    }
  };

  try {
    recognition.start();
  } catch (e) {
    showToast('无法启动麦克风，请检查浏览器权限', 'error');
    return;
  }
  recording = true;
  recStartedAt = Date.now();
  startRecTimer();
  updateRecToggleBtn();
}

function stopRecording() {
  if (!recording) return;
  recording = false;
  if (recognition) {
    try { recognition.stop(); } catch (e) { /* ignore */ }
  }
  stopRecTimer();
  const q = (currentContent.questions || [])[currentQuestionIndex];
  const textarea = document.getElementById('practiceTranscript');
  if (q && textarea) draftByQid[q.id] = textarea.value;
  updateRecToggleBtn();
}

function startRecTimer() {
  const el = document.getElementById('recTimer');
  if (el) el.style.display = '';
  stopRecTimer();
  recTimerHandle = setInterval(() => {
    const el2 = document.getElementById('recTimer');
    if (!el2 || !recStartedAt) return;
    const secs = Math.floor((Date.now() - recStartedAt) / 1000);
    el2.innerHTML = `<span class="rec-dot"></span> ${String(Math.floor(secs / 60)).padStart(2, '0')}:${String(secs % 60).padStart(2, '0')}`;
  }, 500);
}

function stopRecTimer() {
  if (recTimerHandle) { clearInterval(recTimerHandle); recTimerHandle = null; }
  const el = document.getElementById('recTimer');
  if (el) el.style.display = 'none';
}

// ---------- 提交打分 ----------

function answerScoreHtml(answer) {
  if (!answer) return '';
  if (answer.error) {
    return `<div class="detail-section"><h4>打分失败</h4><div class="plain-text" style="color:var(--danger);">${escapeHtml(answer.error)}</div></div>`;
  }
  if (!answer.score_json) return '';
  const score = JSON.parse(answer.score_json);
  return `
    <div class="detail-section">
      <h4>打分反馈</h4>
      <div class="score-row">
        <div class="score-overall">
          <div class="score-overall-value ${scoreClass(score.overall_score)}">${pct(score.overall_score)}</div>
          <div class="score-overall-label">综合评分</div>
        </div>
        <div class="score-dims">
          ${(score.dimensions || []).map((d) => `
            <div class="score-dim">
              <div class="score-dim-head">
                <span>${escapeHtml(d.name)}</span>
                <span class="score-dim-value">${pct(d.score)}</span>
              </div>
              <div class="score-bar"><div class="score-bar-fill ${scoreClass(d.score)}" style="width:${pct(d.score)}"></div></div>
              ${d.comment ? `<div class="plain-text" style="color:var(--text-faint); font-size:0.78rem; margin-top:0.15rem;">${escapeHtml(d.comment)}</div>` : ''}
            </div>`).join('')}
        </div>
      </div>
      ${(score.strengths || []).length ? `<div style="margin-top:0.7rem;"><strong class="prep-sub">做得好的地方</strong>${bulletListHtml(score.strengths)}</div>` : ''}
      ${(score.improvements || []).length ? `<div style="margin-top:0.5rem;"><strong class="prep-sub">可以改进的地方</strong>${bulletListHtml(score.improvements)}</div>` : ''}
    </div>`;
}

async function submitAnswer() {
  const q = (currentContent.questions || [])[currentQuestionIndex];
  if (!q) return;
  if (recording) stopRecording();
  const textarea = document.getElementById('practiceTranscript');
  const transcript = (textarea ? textarea.value : draftByQid[q.id] || '').trim();
  if (!transcript) {
    showToast('还没有作答内容，请先录音或打字', 'error');
    return;
  }

  const btns = document.querySelectorAll('.practice-rec-row .btn-primary');
  btns.forEach((b) => setBtnLoading(b, '打分中…'));
  try {
    const res = await fetch(`/api/interview/practice/sets/${currentSetId}/questions/${q.id}/answer`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ transcript }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '未知错误');
    answersByQid[q.id] = { question_id: q.id, transcript, score_json: JSON.stringify(data.score) };
    delete draftByQid[q.id];
    showToast('打分完成', 'success', 2000);
    renderRunner();
  } catch (e) {
    showToast(`打分失败：${e.message}`, 'error', 6000);
    btns.forEach((b) => restoreBtn(b));
  }
}

// ---------- init ----------
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initModelSelect('practiceModelSelect', 'interview_practice');
  loadDocs();
});
