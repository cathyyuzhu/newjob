// 「我的简历」页：上传/替换简历、跑 AI 体检、按勾选的建议生成优化版、回看各职位的定制简历。
//
// 跟题库页同一个理由做成独立页面而不是主页上的弹窗：体检结果有评分、问题清单、逐段改写
// 三大块，要一边看一边勾，塞进 max-height:92vh 的弹窗里没法用。

// ---------- state ----------
let resumeMeta = { exists: false };
let reviewContent = null;   // 体检结果（已解析的 JSON）
let reviewMeta = null;      // 体检那一行的元信息：created_at / error / stale
let selectedEdits = new Set(); // 勾选了要应用的改写建议（存 paragraph_edits 的数组下标）
// 体检在后台线程里跑（见 startReview()），这两个只用来驱动按钮/卡片的"进行中"状态，
// 不是体检结果本身——结果本身还是走 reviewContent/reviewMeta。
let reviewGenerating = false;
let reviewBackgroundError = null;
let reviewPollTimer = null;

const DIMENSION_LABELS = {
  structure: '结构与排版',
  impact: '成果说服力',
  keyword: '关键词覆盖',
  clarity: '表达质量',
};

const SEVERITY_LABELS = { high: '严重', medium: '中等', low: '建议' };

function scoreClass(score) {
  // 跟职位卡片上的匹配度用同一套阈值和配色（.match-high/mid/low），
  // 免得同一个应用里"70% 是好还是坏"要看两套标准
  if (score >= 0.7) return 'match-high';
  if (score >= 0.4) return 'match-mid';
  return 'match-low';
}

function pct(score) {
  return `${Math.round((score || 0) * 100)}%`;
}

// ---------- 简历文件 ----------
async function loadResume() {
  try {
    resumeMeta = await (await fetch('/api/resume')).json();
  } catch (e) {
    resumeMeta = { exists: false };
  }
  renderFileCard();
}

function renderFileCard() {
  const root = document.getElementById('resumeFileCard');
  // 隐藏的 file input 一直挂着（不随分支变化），这样 pickResumeFile() 不用关心当前渲染的是哪一版
  const input = `<input type="file" id="resumeFileInput" accept=".docx" style="display:none;" onchange="onResumeFileChange(this)">`;

  if (!resumeMeta.exists) {
    // 没上传时这一块就是整页的主视觉：从主页「去上传」跳过来的人正好落在这
    root.innerHTML = `
      ${input}
      <div class="card">
        <div class="upload-drop" id="uploadDrop" onclick="pickResumeFile()">
          <div class="upload-icon">${RESUME_ICON}</div>
          <div class="upload-title">上传你的简历，先把地基打上</div>
          <div class="upload-desc">
            拖到这里，或点击选择文件。只收 <b>.docx</b>——定制简历和优化版都要按段落改写你的原文件、
            保留你的排版，PDF 做不到这件事（可以先用 Word 另存为 .docx）。
          </div>
          <button class="btn btn-primary btn-sm" onclick="event.stopPropagation(); pickResumeFile()">选择 .docx 文件</button>
        </div>
        <p class="upload-note">
          上传之后，职位列表的「AI分析」才能算出你跟每条 JD 的匹配度，面试准备和题库起草也才有素材。
          文件只存在你自己这台机器上（项目目录下的 <code>resumes/</code>），不会上传到任何第三方。
        </p>
      </div>`;
    bindDropZone();
    return;
  }

  root.innerHTML = `
    ${input}
    <div class="card">
      <div class="card-head">
        <div>
          <h2>当前简历</h2>
          <p>匹配分析、面试准备、题库起草读的都是这一份。换一份会连带影响这三处的结果。</p>
        </div>
        <div class="resume-file-actions">
          <button class="btn btn-secondary btn-sm" onclick="downloadBaseResume()">下载原件</button>
          <button class="btn btn-secondary btn-sm" onclick="pickResumeFile()">替换</button>
          <button class="btn btn-secondary btn-sm" onclick="deleteResume(this)">删除</button>
        </div>
      </div>
      <div class="resume-file-meta" id="uploadDrop" onclick="pickResumeFile()">
        <div class="resume-file-name">${RESUME_ICON}${escapeHtml(resumeMeta.filename || '')}</div>
        <div class="resume-file-sub">
          ${resumeMeta.uploaded_at ? `上传于 ${escapeHtml(resumeMeta.uploaded_at)} · ` : ''}
          解析出 ${resumeMeta.paragraph_count || 0} 个段落 ·
          ${((resumeMeta.size || 0) / 1024).toFixed(0)} KB
        </div>
        <div class="resume-file-hint">点击或拖入新文件即可替换</div>
      </div>
    </div>`;
  bindDropZone();
}

// 拖拽用 addEventListener 而不是内联属性：dragover 必须 preventDefault，
// 内联写法要在 HTML 字符串里塞一串 JS，读起来很糟
function bindDropZone() {
  const zone = document.getElementById('uploadDrop');
  if (!zone) return;
  ['dragenter', 'dragover'].forEach((evt) => {
    zone.addEventListener(evt, (e) => {
      e.preventDefault();
      zone.classList.add('dragging');
    });
  });
  ['dragleave', 'drop'].forEach((evt) => {
    zone.addEventListener(evt, (e) => {
      e.preventDefault();
      zone.classList.remove('dragging');
    });
  });
  zone.addEventListener('drop', (e) => {
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) uploadResumeFile(file);
  });
}

function pickResumeFile() {
  document.getElementById('resumeFileInput').click();
}

function onResumeFileChange(input) {
  const file = input.files && input.files[0];
  // 清空 value：不清的话选同一个文件第二次不会触发 change（比如上传失败后想重试）
  input.value = '';
  if (file) uploadResumeFile(file);
}

async function uploadResumeFile(file) {
  const form = new FormData();
  form.append('file', file);
  showToast(`正在上传 ${file.name}…`, 'info', 3000);
  try {
    const res = await fetch('/api/resume/upload', { method: 'POST', body: form });
    // 超过 MAX_CONTENT_LENGTH 时 Flask 直接回 413 HTML 页面，不是 JSON，
    // 硬 .json() 会抛一个跟真实原因无关的解析错
    if (res.status === 413) throw new Error('文件太大（上限 10MB）');
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '上传失败');
    resumeMeta = data;
    showToast('简历已更新', 'success');
    renderFileCard();
    // 换了简历，之前那份体检结论里的段落索引就对不上新文件了，重新拉一次让它显示"已过期"
    await loadReview();
  } catch (e) {
    showToast(`上传失败：${e.message}`, 'error', 8000);
  }
}

function downloadBaseResume() {
  window.location.href = '/api/resume/download';
}

async function deleteResume(btn) {
  // 删掉会让匹配分析/面试准备/题库全部停摆，这个后果比"忽略一条职位"重得多，
  // 值得拦一道——而且没有撤销可给（原文件已经不在了）
  if (!window.confirm('删除后，AI 匹配分析、面试准备、题库起草都会停用，直到你重新上传一份。确定删除吗？')) return;
  setBtnLoading(btn, '删除中…');
  try {
    await fetch('/api/resume', { method: 'DELETE' });
    resumeMeta = { exists: false };
    showToast('简历已删除', 'success');
    renderFileCard();
  } catch (e) {
    showToast(`删除失败：${e.message}`, 'error');
  }
}

// ---------- AI 体检 ----------
// 体检改成后台线程跑（见 review_resume_route）之后，"点开始体检"和"看到结果"是两件
// 分开的事：POST 只负责启动，真正的结果靠这里轮询 GET 拿到。这样跳去别的页面再回来
// 也能看到"还在跑"或者已经跑完的结果，不会因为离开过页面就跟体检失去联系。
async function loadReview() {
  try {
    const row = await (await fetch('/api/resume/review')).json();
    reviewGenerating = !!(row && row.generating);
    reviewBackgroundError = (row && row.background_error) || null;
    if (!row || !row.id) {
      reviewContent = null;
      reviewMeta = null;
    } else {
      reviewMeta = row;
      reviewContent = row.content_json ? JSON.parse(row.content_json) : null;
    }
  } catch (e) {
    reviewContent = null;
    reviewMeta = null;
  }
  selectedEdits = new Set((reviewContent && reviewContent.paragraph_edits || []).map((_, i) => i));
  renderReviewCard();
  renderEditsCard();
  if (reviewGenerating) startReviewPoll();
}

async function startReview(btn) {
  setBtnLoading(btn, '启动中…');
  try {
    const res = await fetch('/api/resume/review', { method: 'POST' });
    const data = await res.json();
    if (handleNeedResume(data)) return;
    if (!res.ok) throw new Error(data.error || '未知错误');
    showToast('已开始体检（约 30 秒），可以先去做别的事，跑完首页待办里会提醒你', 'info', 6000);
    reviewGenerating = true;
    renderReviewCard();
    startReviewPoll();
  } catch (e) {
    showToast(`体检启动失败：${e.message}`, 'error', 8000);
    restoreBtn(btn);
  }
}

function stopReviewPoll() {
  if (reviewPollTimer) {
    clearTimeout(reviewPollTimer);
    reviewPollTimer = null;
  }
}

function startReviewPoll() {
  stopReviewPoll();
  reviewPollTimer = setTimeout(async () => {
    reviewPollTimer = null;
    // 页面切到后台标签页就先歇着，回来再继续（跟题库起草轮询同一个理由）
    if (document.hidden) {
      startReviewPoll();
      return;
    }
    await loadReview();
    if (reviewGenerating) return;
    if (reviewBackgroundError) {
      showToast(`体检失败：${reviewBackgroundError}`, 'error', 10000);
    } else if (reviewMeta && reviewMeta.error) {
      showToast(`体检失败：${reviewMeta.error}`, 'error', 10000);
    } else {
      showToast('体检完成，已给出建议', 'success', 5000);
    }
  }, 4000);
}

function renderReviewCard() {
  const root = document.getElementById('resumeReviewCard');
  if (!resumeMeta.exists) {
    root.innerHTML = '';
    return;
  }

  const runBtn = reviewGenerating
    ? `<button class="btn btn-primary btn-sm" id="reviewBtn" disabled>${SPARK_ICON}体检中（约 30 秒）…</button>`
    : `<button class="btn btn-primary btn-sm" id="reviewBtn" onclick="startReview(this)">
    ${SPARK_ICON}${reviewContent ? '重新体检' : '开始体检'}
  </button>`;

  if (!reviewContent) {
    let placeholder = '<div class="plain-text" style="color:var(--text-faint);">还没有体检记录。</div>';
    if (reviewGenerating) {
      placeholder = '<div class="plain-text" style="color:var(--text-faint);">体检中，可以先去做别的事，跑完这里会自动出现结果。</div>';
    } else if (reviewMeta && reviewMeta.error) {
      placeholder = `<div class="review-error">上次体检失败：${escapeHtml(reviewMeta.error)}</div>`;
    }
    root.innerHTML = `
      <div class="card">
        <div class="card-head">
          <div>
            <h2>AI 简历体检</h2>
            <p>不针对具体职位，只看简历本身：结构、成果说服力、关键词覆盖、表达质量四个维度打分，
              列出问题清单，并给出可以直接应用的逐段改写建议。目标岗位方向取自「设置」里的搜索关键词。</p>
          </div>
          ${runBtn}
        </div>
        ${placeholder}
      </div>`;
    return;
  }

  const scores = reviewContent.dimension_scores || {};
  root.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div>
          <h2>AI 简历体检</h2>
          <p>${reviewMeta && reviewMeta.created_at ? `体检于 ${escapeHtml(reviewMeta.created_at)}` : ''}</p>
        </div>
        ${runBtn}
      </div>

      ${reviewMeta && reviewMeta.stale
        ? `<div class="review-stale">简历在这次体检之后换过了。下面的建议是针对旧版简历写的，段落位置可能已经对不上，建议重新体检。</div>`
        : ''}

      <div class="score-row">
        <div class="score-overall">
          <div class="score-overall-value ${scoreClass(reviewContent.overall_score)}">${pct(reviewContent.overall_score)}</div>
          <div class="score-overall-label">总体评分</div>
        </div>
        <div class="score-dims">
          ${Object.keys(DIMENSION_LABELS).map((key) => `
            <div class="score-dim">
              <div class="score-dim-head">
                <span>${escapeHtml(DIMENSION_LABELS[key])}</span>
                <span class="score-dim-value">${pct(scores[key])}</span>
              </div>
              <div class="score-bar"><div class="score-bar-fill ${scoreClass(scores[key])}" style="width:${pct(scores[key])}"></div></div>
            </div>`).join('')}
        </div>
      </div>

      ${reviewContent.summary ? `<div class="review-summary">${escapeHtml(reviewContent.summary)}</div>` : ''}

      ${reviewContent.strengths && reviewContent.strengths.length ? `
        <div class="detail-section">
          <h4>写得好的地方</h4>
          ${bulletListHtml(reviewContent.strengths)}
        </div>` : ''}

      ${reviewContent.issues && reviewContent.issues.length ? `
        <div class="detail-section">
          <h4>问题清单</h4>
          <ul class="issue-list">
            ${reviewContent.issues.map((it) => `
              <li class="issue-item sev-${escapeHtml(it.severity)}">
                <span class="issue-sev">${escapeHtml(SEVERITY_LABELS[it.severity] || it.severity)}</span>
                <div>
                  <div class="issue-title">${escapeHtml(it.title)}</div>
                  <div class="issue-detail">${escapeHtml(it.detail)}</div>
                </div>
              </li>`).join('')}
          </ul>
        </div>` : ''}

      ${renderCoverageHtml(reviewContent.keyword_coverage)}
    </div>`;
}

function renderCoverageHtml(coverage) {
  if (!coverage) return '';
  const { covered = [], missing = [] } = coverage;
  if (!covered.length && !missing.length) return '';
  const tags = (list, cls) =>
    `<div class="tag-row">${list.map((t) => `<span class="tag ${cls}">${escapeHtml(t)}</span>`).join('')}</div>`;
  return `
    <div class="detail-section">
      <h4>关键词覆盖（对照目标岗位方向）</h4>
      ${covered.length ? `<div class="coverage-label">已体现</div>${tags(covered, 'tag-ok')}` : ''}
      ${missing.length ? `<div class="coverage-label">可以补上（你可能具备、只是没写出来）</div>${tags(missing, 'tag-gap')}` : ''}
    </div>`;
}

// ---------- 逐段改写建议 → 优化版 docx ----------
function renderEditsCard() {
  const root = document.getElementById('resumeEditsCard');
  const edits = (reviewContent && reviewContent.paragraph_edits) || [];
  if (!edits.length) {
    root.innerHTML = '';
    return;
  }

  root.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div>
          <h2>逐段改写建议</h2>
          <p>勾选想采纳的条目，生成一份优化版 docx。改写按段落替换你的原文件、保留原有字体和排版，
            原件不会被动。带"（此处建议补充…）"字样的地方是 AI 刻意留给你自己填的——它不会替你编数字。</p>
        </div>
        <button class="btn btn-primary btn-sm" id="optimizeBtn" onclick="generateOptimized(this)">生成优化版 docx</button>
      </div>

      <label class="edits-selectall">
        <input type="checkbox" id="editsSelectAll" checked onchange="toggleAllEdits(this)">
        <span>全选（共 ${edits.length} 条）</span>
      </label>

      <div class="edit-list">
        ${edits.map((e, i) => `
          <div class="edit-item">
            <label class="edit-check">
              <input type="checkbox" data-editindex="${i}" checked onchange="toggleEdit(${i}, this)">
            </label>
            <div class="edit-body">
              ${e.reason ? `<div class="edit-reason">${escapeHtml(e.reason)}</div>` : ''}
              <div class="edit-before">
                <span class="edit-tag">原文</span>
                <span>${escapeHtml(e.original || '（原文为空）')}</span>
              </div>
              <div class="edit-after">
                <span class="edit-tag">改写</span>
                <span>${escapeHtml(e.text)}</span>
              </div>
            </div>
          </div>`).join('')}
      </div>
    </div>`;
}

function toggleEdit(index, checkbox) {
  if (checkbox.checked) selectedEdits.add(index);
  else selectedEdits.delete(index);
  const edits = (reviewContent && reviewContent.paragraph_edits) || [];
  document.getElementById('editsSelectAll').checked = selectedEdits.size === edits.length;
}

function toggleAllEdits(checkbox) {
  const edits = (reviewContent && reviewContent.paragraph_edits) || [];
  selectedEdits = checkbox.checked ? new Set(edits.map((_, i) => i)) : new Set();
  document.querySelectorAll('[data-editindex]').forEach((box) => {
    box.checked = checkbox.checked;
  });
}

async function generateOptimized(btn) {
  const edits = (reviewContent && reviewContent.paragraph_edits) || [];
  const chosen = edits.filter((_, i) => selectedEdits.has(i));
  if (!chosen.length) {
    showToast('先勾选至少一条改写建议', 'error');
    return;
  }
  setBtnLoading(btn, '生成中…');
  try {
    const res = await fetch('/api/resume/optimize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edits: chosen }),
    });
    const data = await res.json();
    if (handleNeedResume(data)) return;
    if (!res.ok) throw new Error(data.error || '未知错误');
    showToast(`已生成 ${data.download_name}，开始下载`, 'success');
    window.location.href = '/api/resume/optimized';
  } catch (e) {
    showToast(`生成失败：${e.message}`, 'error', 8000);
  } finally {
    restoreBtn(btn);
  }
}

// ---------- 各职位的定制简历 ----------
async function loadTailored() {
  let rows = [];
  try {
    rows = await (await fetch('/api/resume/tailored')).json();
  } catch (e) {
    rows = [];
  }
  const root = document.getElementById('tailoredCard');
  if (!rows.length) {
    root.innerHTML = '';
    return;
  }

  root.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div>
          <h2>各职位的定制简历</h2>
          <p>AI 匹配分析在匹配度 ≥70% 且判定需要定制时自动生成的版本。以前这些文件只在职位详情弹窗里
            露一面，关掉就找不着了，这里统一列出来。</p>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>公司</th><th>职位</th><th>匹配度</th><th></th></tr></thead>
          <tbody>
            ${rows.map((r) => `
              <tr>
                <td>${escapeHtml(r.company || '')}</td>
                <td>${escapeHtml(r.title || '')}</td>
                <td><span class="match-pill ${scoreClass(r.overall_match)}">${pct(r.overall_match)}</span></td>
                <td>${r.file_exists
                  ? `<a class="btn btn-secondary btn-sm" href="/api/jobs/${r.id}/resume">下载</a>`
                  : '<span class="tailored-missing" title="文件可能已被移动或删除">文件已丢失</span>'}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>
    </div>`;
}

// ---------- init ----------
initTheme();
initModelSelect('reviewModelSelect', 'resume_review');
// 顺序：先知道有没有简历，再决定体检卡片怎么渲染（renderReviewCard 会读 resumeMeta.exists）
loadResume().then(loadReview);
loadTailored();
