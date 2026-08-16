// 面试准备模块的前端逻辑。跟 app.js 一样是无构建步骤的普通脚本，直接复用它的全局工具
// 函数（escapeHtml / showToast / setBtnLoading / restoreBtn / bulletListHtml / allJobs /
// currentDetailJobId / loadJobs）——这些都只在函数体里用到，运行时两个脚本都已加载完。
// 单独一个文件是因为 app.js 已经接近 800 行，再往里堆一整个模块不好找东西。
// 引入顺序见 index.html 末尾的说明（本文件在 app.js 之前）。

// 当前详情弹窗里"面试准备"tab 展示的是哪一份（历史版本下拉切换时改这个）
let currentPrepId = null;
let interviewPreps = [];
let prepPollTimer = null;

const PREP_CATEGORY_ORDER = ['行为面', '业务/领域', '技术/方法论', '职业动机'];

function interviewPrepBadgeHtml(job) {
  if (job.interview_prep_state === 'generating') {
    return '<span class="match-pill prep-pill" title="正在生成面试准备材料">🎤 准备中…</span>';
  }
  if (!job.has_interview_prep) return '';
  return `<span class="match-pill prep-pill done" title="已有面试准备材料，点击查看"
    onclick="event.stopPropagation(); openJobDetailModal(${job.id}, 'interview')">🎤 面试准备</span>`;
}

// ---------- 加载与渲染 ----------

async function loadInterviewPrep(jobId) {
  if (!jobId) return;
  const panel = document.getElementById('detailSubpanel-interview');
  if (!panel) return;
  panel.innerHTML = '<div class="plain-text" style="color:var(--text-faint);">加载中…</div>';
  try {
    const res = await fetch(`/api/jobs/${jobId}/interview_prep?all=1`);
    interviewPreps = res.ok ? await res.json() : [];
  } catch (e) {
    panel.innerHTML = `<div class="plain-text" style="color:var(--danger);">加载失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  currentPrepId = interviewPreps.length ? interviewPreps[0].id : null;
  renderInterviewPrep(jobId);
}

function renderInterviewPrep(jobId) {
  const panel = document.getElementById('detailSubpanel-interview');
  if (!panel) return;

  const job = (typeof allJobs !== 'undefined' ? allJobs : []).find((j) => j.id === jobId);
  const generating = job && job.interview_prep_state === 'generating';
  const prep = interviewPreps.find((p) => p.id === currentPrepId) || null;

  panel.innerHTML = `
    ${prepToolbarHtml(jobId, prep, generating)}
    ${generating && !prep ? '<div class="plain-text" style="color:var(--text-faint);">正在生成中，通常需要一两分钟，完成后会自动刷新…</div>' : ''}
    ${!generating && !prep ? prepEmptyHtml(job) : ''}
    ${prep ? prepContentHtml(prep) : ''}
  `;
  if (generating) startInterviewPrepPoll(jobId);
}

function prepToolbarHtml(jobId, prep, generating) {
  const options = interviewPreps
    .map((p) => {
      const label = `${p.created_at.replace('T', ' ')}${p.round_label ? ` · ${p.round_label}` : ''}${p.error ? ' · 失败' : ''}`;
      return `<option value="${p.id}" ${p.id === currentPrepId ? 'selected' : ''}>${escapeHtml(label)}</option>`;
    })
    .join('');
  return `
    <div class="prep-toolbar">
      ${interviewPreps.length > 1
        ? `<select class="app-status-select" onchange="switchPrepVersion(${jobId}, this.value)" title="历史版本">${options}</select>`
        : prep ? `<span class="plain-text" style="color:var(--text-faint); font-size:0.8rem;">生成于 ${escapeHtml(prep.created_at.replace('T', ' '))}${prep.round_label ? ` · ${escapeHtml(prep.round_label)}` : ''}</span>` : ''}
      <span style="flex:1"></span>
      <input class="prep-round-input" id="prepRoundInput" type="text" placeholder="轮次（选填，如：二面）" maxlength="20">
      <button class="btn btn-secondary btn-sm" ${generating ? 'disabled' : ''} onclick="regenerateInterviewPrep(${jobId}, this)">
        ${generating ? '生成中…' : '🔄 重新生成'}
      </button>
      ${prep ? `<button class="btn btn-danger btn-sm" onclick="deleteInterviewPrep(${jobId}, ${prep.id}, this)">删除这份</button>` : ''}
    </div>
  `;
}

function prepEmptyHtml(job) {
  const noJd = job && !(job.jd_text || '').trim();
  return `<div class="plain-text" style="color:var(--text-faint);">
    还没有生成面试准备材料。${noJd
      ? '这条职位没有JD正文，需要先点卡片上的「重新获取」抓到JD才能生成。'
      : '把投递状态改成「面试中」会自动生成，也可以点右上角「重新生成」立刻生成一份。'}
  </div>`;
}

function prepContentHtml(prep) {
  if (prep.error) {
    return `<div class="detail-section">
      <h4>生成失败</h4>
      <div class="plain-text" style="color:var(--danger);">${escapeHtml(prep.error)}</div>
      <div class="plain-text" style="color:var(--text-faint); font-size:0.8rem; margin-top:0.35rem;">可以点右上角「重新生成」再试一次。</div>
    </div>`;
  }

  let c;
  try {
    c = JSON.parse(prep.content_json);
  } catch (e) {
    return `<div class="plain-text" style="color:var(--danger);">内容解析失败：${escapeHtml(e.message)}</div>`;
  }

  const r = c.company_research || {};
  const questions = c.questions || [];
  // 按 category 分组展示，同一类问题放一起，比混在一起更好准备
  const grouped = {};
  questions.forEach((q) => {
    const cat = q.category || '其它';
    (grouped[cat] = grouped[cat] || []).push(q);
  });
  const cats = Object.keys(grouped).sort((a, b) => {
    const ia = PREP_CATEGORY_ORDER.indexOf(a);
    const ib = PREP_CATEGORY_ORDER.indexOf(b);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });

  return `
    <div class="detail-section">
      <h4>公司 / 业务背景</h4>
      <div class="plain-text">${escapeHtml(r.business || '—')}</div>
      ${r.role_context ? `<div class="plain-text" style="margin-top:0.4rem;"><strong>这个岗位的位置：</strong>${escapeHtml(r.role_context)}</div>` : ''}
      ${r.pain_points && r.pain_points.length ? `<div style="margin-top:0.5rem;"><strong class="prep-sub">可以聊的业务痛点</strong>${bulletListHtml(r.pain_points)}</div>` : ''}
      ${r.talking_points && r.talking_points.length ? `<div style="margin-top:0.5rem;"><strong class="prep-sub">显示做过功课的切入点</strong>${bulletListHtml(r.talking_points)}</div>` : ''}
    </div>

    <div class="detail-section">
      <h4>预测面试题（${questions.length} 题，点开看答法）</h4>
      ${cats.map((cat) => `
        <div class="prep-cat">
          <div class="prep-cat-name">${escapeHtml(cat)}</div>
          ${grouped[cat].map((q) => questionItemHtml(q)).join('')}
        </div>
      `).join('')}
    </div>

    ${(c.gap_scripts || []).length ? `
    <div class="detail-section">
      <h4>缺口应对话术（对应匹配分析里标红的未达标项）</h4>
      ${c.gap_scripts.map((g) => `
        <details class="prep-item gap">
          <summary>${escapeHtml(g.gap || '')}</summary>
          <div class="prep-item-body">
            ${g.likely_question ? `<div class="plain-text"><strong>大概率会这么问：</strong>${escapeHtml(g.likely_question)}</div>` : ''}
            ${g.script ? `<div class="plain-text" style="margin-top:0.4rem;"><strong>建议话术：</strong>${escapeHtml(g.script)}</div>` : ''}
            ${g.transferable ? `<div class="plain-text" style="margin-top:0.4rem; color:var(--text-faint);"><strong>可迁移经历：</strong>${escapeHtml(g.transferable)}</div>` : ''}
          </div>
        </details>
      `).join('')}
    </div>` : ''}

    ${(c.questions_to_ask || []).length ? `
    <div class="detail-section">
      <h4>反问面试官</h4>
      <ul class="bullet-list">
        ${c.questions_to_ask.map((q) => `<li>${escapeHtml(q.question || '')}${q.intent ? `<span style="color:var(--text-faint);"> — ${escapeHtml(q.intent)}</span>` : ''}</li>`).join('')}
      </ul>
    </div>` : ''}

    ${(c.prep_checklist || []).length ? `
    <div class="detail-section">
      <h4>面试前准备清单</h4>
      ${bulletListHtml(c.prep_checklist)}
    </div>` : ''}
  `;
}

function questionItemHtml(q) {
  return `
    <details class="prep-item">
      <summary>${escapeHtml(q.question || '')}</summary>
      <div class="prep-item-body">
        ${q.why_asked ? `<div class="plain-text" style="color:var(--text-faint);"><strong>为什么会问：</strong>${escapeHtml(q.why_asked)}</div>` : ''}
        ${(q.answer_points || []).length ? `<div style="margin-top:0.4rem;"><strong class="prep-sub">答题要点</strong>${bulletListHtml(q.answer_points)}</div>` : ''}
        ${q.resume_evidence ? `<div class="plain-text" style="margin-top:0.4rem;"><strong>简历依据：</strong>${escapeHtml(q.resume_evidence)}</div>` : ''}
      </div>
    </details>
  `;
}

// ---------- 操作 ----------

function switchPrepVersion(jobId, prepId) {
  currentPrepId = Number(prepId);
  renderInterviewPrep(jobId);
}

async function regenerateInterviewPrep(jobId, btn) {
  const input = document.getElementById('prepRoundInput');
  const roundLabel = input ? input.value.trim() : '';
  setBtnLoading(btn, '启动中…');
  try {
    const res = await fetch(`/api/jobs/${jobId}/interview_prep`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ round_label: roundLabel }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '未知错误');
    showToast('已开始生成，通常需要一两分钟，完成后这里会自动刷新', 'info', 6000);
    // 后端是后台线程跑的，先把本地状态标成生成中，让面板立刻显示"生成中"并开始轮询，
    // 不用等下一次 loadJobs() 才反应过来（同 analysis_state 那套轮询的思路）。
    const job = allJobs.find((j) => j.id === jobId);
    if (job) job.interview_prep_state = 'generating';
    renderInterviewPrep(jobId);
  } catch (e) {
    showToast(`生成失败：${e.message}`, 'error', 6000);
    restoreBtn(btn);
  }
}

async function deleteInterviewPrep(jobId, prepId, btn) {
  if (!window.confirm('确定删除这一份面试准备材料吗？删除后不可恢复。')) return;
  setBtnLoading(btn, '删除中…');
  try {
    const res = await fetch(`/api/interview_preps/${prepId}`, { method: 'DELETE' });
    if (!res.ok) throw new Error((await res.json()).error || '未知错误');
    showToast('已删除', 'success', 2000);
    await loadInterviewPrep(jobId);
    loadJobs();
  } catch (e) {
    showToast(`删除失败：${e.message}`, 'error');
    restoreBtn(btn);
  }
}

// ---------- 轮询 ----------

function stopInterviewPrepPoll() {
  if (prepPollTimer) {
    clearTimeout(prepPollTimer);
    prepPollTimer = null;
  }
}

// 详情弹窗开着的时候用：每几秒查一次这条职位是否还在生成，生成完自动把内容刷出来。
function startInterviewPrepPoll(jobId) {
  stopInterviewPrepPoll();
  prepPollTimer = setTimeout(async () => {
    prepPollTimer = null;
    if (currentDetailJobId !== jobId) return; // 弹窗已经关掉或切到别的职位了
    const done = await refreshJobPrepState(jobId);
    if (done) {
      await loadInterviewPrep(jobId);
      loadJobs();
    } else {
      startInterviewPrepPoll(jobId);
    }
  }, 4000);
}

// 弹窗没开着也要能跟进（比如刚把状态改成"面试中"就去看别的职位了）：生成结束后
// 刷新一次列表，让卡片上的 🎤 标记出现。
function pollInterviewPrepUntilDone(jobId, elapsed = 0) {
  if (elapsed > 300000) return; // 最多跟5分钟，超时就不管了，用户手动刷新也能看到
  setTimeout(async () => {
    const done = await refreshJobPrepState(jobId);
    if (done) {
      loadJobs();
      if (currentDetailJobId === jobId) loadInterviewPrep(jobId);
    } else {
      pollInterviewPrepUntilDone(jobId, elapsed + 5000);
    }
  }, 5000);
}

// 查一次这条职位当前的生成状态，顺带更新本地 allJobs 里的字段。返回"是否已经跑完了"。
async function refreshJobPrepState(jobId) {
  try {
    const jobs = await (await fetch('/api/jobs')).json();
    const fresh = jobs.find((j) => j.id === jobId);
    if (!fresh) return true;
    const local = allJobs.find((j) => j.id === jobId);
    if (local) {
      local.interview_prep_state = fresh.interview_prep_state;
      local.has_interview_prep = fresh.has_interview_prep;
    }
    return fresh.interview_prep_state !== 'generating';
  } catch (e) {
    return false;
  }
}

// ============================================================================
// 通用面试题库（跨职位复用的个人标准答案）
// ============================================================================

let bankItems = [];
let bankGenerating = false;
let bankPollTimer = null;

const BANK_SECTIONS = [
  { key: 'self_intro', title: '自我介绍', hint: '60-90 秒口播稿，中英文各一版（外企面试常用英文）。' },
  { key: 'common', title: '通用问题', hint: '离职原因、职业规划、优缺点这类每场面试都会遇到的题。' },
  { key: 'star_story', title: 'STAR 故事库', hint: '能反复复用的几个核心故事，按 情境-任务-行动-结果 组织。' },
];

function openInterviewModal() {
  document.getElementById('interviewModalOverlay').classList.add('active');
  loadBank();
}

function closeInterviewModal() {
  document.getElementById('interviewModalOverlay').classList.remove('active');
  stopBankPoll();
}

async function loadBank() {
  const body = document.getElementById('interviewModalBody');
  if (!bankItems.length) body.innerHTML = '<div class="plain-text" style="color:var(--text-faint);">加载中…</div>';
  try {
    const data = await (await fetch('/api/interview/bank')).json();
    bankItems = data.items || [];
    bankGenerating = !!data.generating;
  } catch (e) {
    body.innerHTML = `<div class="plain-text" style="color:var(--danger);">加载失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  renderBank();
  if (bankGenerating) startBankPoll();
}

function renderBank() {
  const body = document.getElementById('interviewModalBody');
  body.innerHTML = `
    <div class="prep-toolbar">
      <span class="plain-text" style="color:var(--text-faint); font-size:0.8rem;">
        AI 起草只填你没改过的条目，改过的答案不会被覆盖。
      </span>
      <span style="flex:1"></span>
      <button class="btn btn-secondary btn-sm" ${bankGenerating ? 'disabled' : ''} onclick="generateBankDraft(this)">
        ${bankGenerating ? '起草中…' : '✨ AI 起草 / 补充'}
      </button>
    </div>
    ${bankGenerating ? '<div class="plain-text" style="color:var(--text-faint); margin-bottom:0.75rem;">正在根据你的简历起草，通常需要一两分钟，完成后会自动刷新…</div>' : ''}
    ${!bankItems.length && !bankGenerating
      ? '<div class="plain-text" style="color:var(--text-faint);">题库还是空的。点右上角「AI 起草 / 补充」根据你的简历生成一份初稿，然后逐条改成你自己的说法。</div>'
      : BANK_SECTIONS.map((s) => bankSectionHtml(s)).join('')}
  `;
}

function bankSectionHtml(section) {
  const items = bankItems.filter((i) => i.category === section.key);
  return `
    <div class="detail-section">
      <h4>${escapeHtml(section.title)}<span class="bank-count">${items.length}</span></h4>
      <div class="plain-text" style="color:var(--text-faint); font-size:0.78rem; margin-bottom:0.5rem;">${escapeHtml(section.hint)}</div>
      ${items.map((i) => bankItemHtml(i)).join('') || '<div class="plain-text" style="color:var(--text-faint); font-size:0.82rem;">（暂无）</div>'}
      <button class="btn btn-secondary btn-sm" style="margin-top:0.5rem;" onclick="addBankItem('${section.key}', this)">+ 手动加一题</button>
    </div>
  `;
}

function bankItemHtml(item) {
  // 自我介绍要中英双栏，其它条目只有中文——英文版只有自我介绍是刚需（外企面试开场），
  // 每道通用题都配英文版意义不大，也会让 AI 起草的输出量翻倍。
  const withEn = item.category === 'self_intro';
  return `
    <details class="prep-item bank-item" data-id="${item.id}">
      <summary>
        ${escapeHtml(item.question)}
        ${item.user_edited ? '<span class="bank-badge">已自定义</span>' : '<span class="bank-badge ai">AI 初稿</span>'}
      </summary>
      <div class="prep-item-body">
        <textarea class="bank-answer" id="bankAnswer-${item.id}" rows="6"
          placeholder="用你自己的说法写下答案…">${escapeHtml(item.answer || '')}</textarea>
        ${withEn ? `
          <label class="bank-label">English version</label>
          <textarea class="bank-answer" id="bankAnswerEn-${item.id}" rows="6"
            placeholder="English version…">${escapeHtml(item.answer_en || '')}</textarea>` : ''}
        <div class="bank-actions">
          <span class="plain-text" style="color:var(--text-faint); font-size:0.74rem;">更新于 ${escapeHtml((item.updated_at || '').replace('T', ' '))}</span>
          <span style="flex:1"></span>
          <button class="btn btn-secondary btn-sm" onclick="saveBankItem(${item.id}, ${withEn}, this)">保存</button>
          <button class="btn btn-danger btn-sm" onclick="removeBankItem(${item.id}, this)">删除</button>
        </div>
      </div>
    </details>
  `;
}

async function generateBankDraft(btn) {
  setBtnLoading(btn, '启动中…');
  try {
    const res = await fetch('/api/interview/bank/generate', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '未知错误');
    showToast('已开始根据简历起草题库，通常需要一两分钟', 'info', 6000);
    bankGenerating = true;
    renderBank();
    startBankPoll();
  } catch (e) {
    showToast(`起草失败：${e.message}`, 'error', 6000);
    restoreBtn(btn);
  }
}

async function saveBankItem(itemId, withEn, btn) {
  const answer = document.getElementById(`bankAnswer-${itemId}`).value;
  const payload = { answer };
  if (withEn) payload.answer_en = document.getElementById(`bankAnswerEn-${itemId}`).value;
  setBtnLoading(btn, '保存中…');
  try {
    const res = await fetch(`/api/interview/bank/${itemId}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error((await res.json()).error || '未知错误');
    showToast('已保存，这条以后不会被 AI 起草覆盖', 'success', 3000);
    // 只更新本地数据 + 重画，不整个重新拉——重画会关掉所有展开的 details，
    // 但保存完这条本来就该收起来，比整页闪一下好。
    const item = bankItems.find((i) => i.id === itemId);
    if (item) {
      item.answer = answer;
      if (withEn) item.answer_en = payload.answer_en;
      item.user_edited = 1;
      item.updated_at = new Date().toISOString().slice(0, 19);
    }
    renderBank();
  } catch (e) {
    showToast(`保存失败：${e.message}`, 'error');
    restoreBtn(btn);
  }
}

async function addBankItem(category, btn) {
  const question = window.prompt('新增一题，先填问题：');
  if (!question || !question.trim()) return;
  setBtnLoading(btn, '添加中…');
  try {
    const res = await fetch('/api/interview/bank', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category, question: question.trim() }),
    });
    if (!res.ok) throw new Error((await res.json()).error || '未知错误');
    await loadBank();
  } catch (e) {
    showToast(`添加失败：${e.message}`, 'error');
    restoreBtn(btn);
  }
}

async function removeBankItem(itemId, btn) {
  if (!window.confirm('确定删除这一题吗？')) return;
  setBtnLoading(btn, '删除中…');
  try {
    const res = await fetch(`/api/interview/bank/${itemId}`, { method: 'DELETE' });
    if (!res.ok) throw new Error((await res.json()).error || '未知错误');
    showToast('已删除', 'success', 2000);
    await loadBank();
  } catch (e) {
    showToast(`删除失败：${e.message}`, 'error');
    restoreBtn(btn);
  }
}

function stopBankPoll() {
  if (bankPollTimer) {
    clearTimeout(bankPollTimer);
    bankPollTimer = null;
  }
}

function startBankPoll() {
  stopBankPoll();
  bankPollTimer = setTimeout(async () => {
    bankPollTimer = null;
    // 弹窗关掉了就不继续轮询（重新打开时 loadBank() 会再看一次状态）
    if (!document.getElementById('interviewModalOverlay').classList.contains('active')) return;
    const before = bankItems.length;
    await loadBank();
    if (!bankGenerating) {
      const added = bankItems.length - before;
      showToast(added > 0 ? `题库起草完成，新增 ${added} 条` : '题库起草完成', 'success', 5000);
    }
  }, 5000);
}
