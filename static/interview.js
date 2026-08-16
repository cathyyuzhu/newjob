// 面试准备页（/jobs/<id>/interview）的全部逻辑。跟 common.js 一起加载，不依赖 app.js。
//
// 这块内容原来是职位详情弹窗里的一个 tab，搬出来是因为弹窗有三个治不好的毛病：
// 生成要 3-5 分钟而轮询跟弹窗生命周期绑死（关掉就断）、内容长却被塞进 max-height:92vh
// 的内滚容器、没有 URL 没法单独开一个标签页挂着。理由详见 spec/tech-solution.md。
// 「匹配分析」相反——扫一眼就关，继续留在主页的弹窗里。

// 当前展示的是哪一份面试准备（历史版本下拉切换时改这个）
let currentPrepId = null;
let interviewPreps = [];
let prepPollTimer = null;
// 这条职位本身（标题、JD、生成状态）。以前是从 app.js 的 allJobs 里 find 出来的，
// 独立页面上没有那个全局，改成自己拉一次 GET /api/jobs/<id>。
let currentJob = null;

const PREP_JOB_ID = window.PREP_JOB_ID;
const PREP_CATEGORY_ORDER = ['行为面', '业务/领域', '技术/方法论', '职业动机'];

// ---------- 加载与渲染 ----------

async function loadJobHeader() {
  try {
    const res = await fetch(`/api/jobs/${PREP_JOB_ID}`);
    if (!res.ok) return;
    currentJob = await res.json();
  } catch (e) {
    return;
  }
  const title = `${currentJob.title || ''} · ${currentJob.company || ''}`;
  document.getElementById('prepJobTitle').textContent = title;
  document.title = `面试准备 · ${title}`;
  const link = document.getElementById('prepJobUrl');
  if (currentJob.job_url) link.href = currentJob.job_url;
  else link.style.display = 'none';
}

async function loadInterviewPrep() {
  const panel = document.getElementById('prepRoot');
  if (!interviewPreps.length) {
    panel.innerHTML = '<div class="plain-text" style="color:var(--text-faint);">加载中…</div>';
  }
  try {
    const res = await fetch(`/api/jobs/${PREP_JOB_ID}/interview_prep?all=1`);
    interviewPreps = res.ok ? await res.json() : [];
  } catch (e) {
    panel.innerHTML = `<div class="plain-text" style="color:var(--danger);">加载失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  // 已经选中的那一份还在就别乱跳（轮询刷新时尤其重要），否则默认看最新的一份
  if (!interviewPreps.some((p) => p.id === currentPrepId)) {
    currentPrepId = interviewPreps.length ? interviewPreps[0].id : null;
  }
  renderInterviewPrep();
}

function renderInterviewPrep() {
  const panel = document.getElementById('prepRoot');
  const generating = currentJob && currentJob.interview_prep_state === 'generating';
  const prep = interviewPreps.find((p) => p.id === currentPrepId) || null;

  panel.innerHTML = `
    ${prepToolbarHtml(prep, generating)}
    ${generating && !prep ? '<div class="plain-text" style="color:var(--text-faint);">正在生成中，可能需要 3-5 分钟，完成后会自动刷新（这个页面可以一直开着，刷新或切走再回来也不影响）…</div>' : ''}
    ${!generating && !prep ? prepEmptyHtml() : ''}
    ${prep ? prepContentHtml(prep) : ''}
  `;
  if (generating) startInterviewPrepPoll();
}

function prepToolbarHtml(prep, generating) {
  const options = interviewPreps
    .map((p) => {
      const label = `${p.created_at.replace('T', ' ')}${p.round_label ? ` · ${p.round_label}` : ''}${p.error ? ' · 失败' : ''}`;
      return `<option value="${p.id}" ${p.id === currentPrepId ? 'selected' : ''}>${escapeHtml(label)}</option>`;
    })
    .join('');
  return `
    <div class="prep-toolbar">
      ${interviewPreps.length > 1
        ? `<select class="app-status-select" onchange="switchPrepVersion(this.value)" title="历史版本">${options}</select>`
        : prep ? `<span class="plain-text" style="color:var(--text-faint); font-size:0.8rem;">生成于 ${escapeHtml(prep.created_at.replace('T', ' '))}${prep.round_label ? ` · ${escapeHtml(prep.round_label)}` : ''}</span>` : ''}
      <span style="flex:1"></span>
      <input class="prep-round-input" id="prepRoundInput" type="text" placeholder="轮次（选填，如：二面）" maxlength="20">
      <button class="btn btn-secondary btn-sm" ${generating ? 'disabled' : ''} onclick="regenerateInterviewPrep(this)">
        ${generating ? '生成中…' : '🔄 重新生成'}
      </button>
      ${prep ? `<button class="btn btn-danger btn-sm" onclick="deleteInterviewPrep(${prep.id}, this)">删除这份</button>` : ''}
    </div>
  `;
}

function prepEmptyHtml() {
  const noJd = currentJob && !(currentJob.jd_text || '').trim();
  return `<div class="plain-text" style="color:var(--text-faint);">
    还没有生成面试准备材料。${noJd
      ? '这条职位没有JD正文，需要先回职位列表点卡片上的「重新获取」抓到JD才能生成。'
      : '把投递状态改成「面试中」会自动生成，也可以点上面的「重新生成」立刻生成一份。'}
  </div>`;
}

function prepContentHtml(prep) {
  if (prep.error) {
    return `<div class="detail-section">
      <h4>生成失败</h4>
      <div class="plain-text" style="color:var(--danger);">${escapeHtml(prep.error)}</div>
      <div class="plain-text" style="color:var(--text-faint); font-size:0.8rem; margin-top:0.35rem;">可以点上面的「重新生成」再试一次。</div>
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

function switchPrepVersion(prepId) {
  currentPrepId = Number(prepId);
  renderInterviewPrep();
}

async function regenerateInterviewPrep(btn) {
  const input = document.getElementById('prepRoundInput');
  const roundLabel = input ? input.value.trim() : '';
  setBtnLoading(btn, '启动中…');
  try {
    const res = await fetch(`/api/jobs/${PREP_JOB_ID}/interview_prep`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ round_label: roundLabel }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '未知错误');
    showToast('已开始生成，可能需要 3-5 分钟，完成后这里会自动刷新', 'info', 6000);
    // 后端是后台线程跑的，先把本地状态标成生成中，让页面立刻显示"生成中"并开始轮询，
    // 不用等下一次拉接口才反应过来。
    if (currentJob) currentJob.interview_prep_state = 'generating';
    renderInterviewPrep();
  } catch (e) {
    showToast(`生成失败：${e.message}`, 'error', 6000);
    restoreBtn(btn);
  }
}

async function deleteInterviewPrep(prepId, btn) {
  if (!window.confirm('确定删除这一份面试准备材料吗？删除后不可恢复。')) return;
  setBtnLoading(btn, '删除中…');
  try {
    const res = await fetch(`/api/interview_preps/${prepId}`, { method: 'DELETE' });
    if (!res.ok) throw new Error((await res.json()).error || '未知错误');
    showToast('已删除', 'success', 2000);
    currentPrepId = null;
    await loadInterviewPrep();
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

// 每几秒查一次这条职位是否还在生成，生成完自动把内容刷出来。
// 页面切到后台标签页时先歇着——查了也没人看，白烧请求。
function startInterviewPrepPoll() {
  stopInterviewPrepPoll();
  prepPollTimer = setTimeout(async () => {
    prepPollTimer = null;
    if (document.hidden) {
      startInterviewPrepPoll();
      return;
    }
    if (await refreshJobPrepState()) await loadInterviewPrep();
    else startInterviewPrepPoll();
  }, 4000);
}

// 查一次这条职位当前的生成状态，顺带更新本地 currentJob。返回"是否已经跑完了"。
async function refreshJobPrepState() {
  try {
    const res = await fetch(`/api/jobs/${PREP_JOB_ID}`);
    if (!res.ok) return true;
    currentJob = await res.json();
    return currentJob.interview_prep_state !== 'generating';
  } catch (e) {
    return false;
  }
}

// ---------- init ----------
document.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  await loadJobHeader();
  loadInterviewPrep();
});
