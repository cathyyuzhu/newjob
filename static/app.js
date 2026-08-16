// ---------- state ----------
let allJobs = [];
let currentStatus = 'new';
let currentOrigin = 'foreign';
let currentAppStatus = '';
// "重点关注"筛选：独立开关（不跟外企/国内公司互斥），开启后只显示标了星的职位
let starredOnly = false;
let trackerEntries = [];
let trackerIndex = {};

// ---------- helpers ----------
// 主题切换 / toast / escapeHtml / setBtnLoading / restoreBtn / bulletListHtml 都在
// common.js 里（三个页面共用），本文件只留职位列表专属的部分。
function safeUrl(url) {
  if (typeof url === 'string' && /^https?:\/\//i.test(url)) return url;
  return '#';
}

function normalizeStr(s) {
  return String(s ?? '').trim().toLowerCase();
}

function dedupeKey(company, title) {
  return `${normalizeStr(company)}::${normalizeStr(title)}`;
}

// ---------- more modal (settings / runs) ----------
function openMoreModal(subtab = 'settings') {
  document.getElementById('moreModalOverlay').classList.add('active');
  switchMoreTab(subtab);
}

function closeMoreModal() {
  document.getElementById('moreModalOverlay').classList.remove('active');
}

function switchMoreTab(subtab) {
  document.querySelectorAll('.modal-tabs .tab-btn').forEach((b) => b.classList.toggle('active', b.dataset.moretab === subtab));
  document.querySelectorAll('#moreModalOverlay .tab-panel').forEach((p) => p.classList.toggle('active', p.id === `moreSubpanel-${subtab}`));
  if (subtab === 'runs') loadRuns();
}

// ---------- config ----------
async function loadConfig() {
  const cfg = await (await fetch('/api/config')).json();
  document.getElementById('keywords').value = (cfg.keywords || []).join('\n');
  document.getElementById('locations').value = (cfg.locations || []).join('\n');
  document.getElementById('country_indeed').value = cfg.country_indeed || '';
  document.getElementById('results_wanted').value = cfg.results_wanted;
  document.getElementById('days_old').value = cfg.days_old;
  document.getElementById('schedule_enabled').checked = cfg.schedule_enabled !== false;
  document.getElementById('schedule_hour').value = cfg.schedule_hour;
  document.getElementById('schedule_minute').value = cfg.schedule_minute;
  document.getElementById('tracker_xlsx_path').value = cfg.tracker_xlsx_path || '';
  document.getElementById('base_resume_path').value = cfg.base_resume_path || '';
  document.getElementById('resume_output_dir').value = cfg.resume_output_dir || '';
  const eaProfile = cfg.easy_apply_profile || {};
  document.getElementById('ea_work_authorization').value = eaProfile.work_authorization || '';
  document.getElementById('ea_expected_salary').value = eaProfile.expected_salary || '';
  document.getElementById('ea_notice_period').value = eaProfile.notice_period || '';
  document.getElementById('ea_extra_answers').value = (eaProfile.extra_answers || [])
    .map((qa) => `${qa.keyword}=${qa.answer}`).join('\n');
}

function parseExtraAnswers(text) {
  // 每行"关键词=答案"，允许答案本身包含等号（只在第一个等号处切分）。之前遇到过
  // 用户习惯性用冒号（问题文字本身常带冒号，比如"...Bachelor's Degree?："）而不是
  // 等号，导致这一行没法解析、被静默丢弃、以为是"设置没保存成功"——现在只认"="，
  // 但不再默默丢：调用方（saveConfig）会检查哪些非空行没解析出来，弹提示告诉用户
  // 具体哪几行需要改成"="格式，而不是让人以为是保存功能本身坏了。
  const lines = text.split('\n').map((line) => line.trim()).filter(Boolean);
  const parsed = [];
  const invalidLines = [];
  for (const line of lines) {
    const idx = line.indexOf('=');
    if (idx < 0) {
      invalidLines.push(line);
      continue;
    }
    const keyword = line.slice(0, idx).trim();
    const answer = line.slice(idx + 1).trim();
    if (!keyword || !answer) {
      invalidLines.push(line);
      continue;
    }
    parsed.push({ keyword, answer });
  }
  return { parsed, invalidLines };
}

async function saveConfig() {
  const btn = document.getElementById('saveConfigBtn');
  setBtnLoading(btn, '保存中…');
  const { parsed: extraAnswers, invalidLines } = parseExtraAnswers(document.getElementById('ea_extra_answers').value);
  const body = {
    keywords: document.getElementById('keywords').value.split('\n'),
    locations: document.getElementById('locations').value.split('\n'),
    country_indeed: document.getElementById('country_indeed').value,
    results_wanted: document.getElementById('results_wanted').value,
    days_old: document.getElementById('days_old').value,
    schedule_enabled: document.getElementById('schedule_enabled').checked,
    schedule_hour: document.getElementById('schedule_hour').value,
    schedule_minute: document.getElementById('schedule_minute').value,
    tracker_xlsx_path: document.getElementById('tracker_xlsx_path').value,
    base_resume_path: document.getElementById('base_resume_path').value,
    resume_output_dir: document.getElementById('resume_output_dir').value,
    easy_apply_profile: {
      work_authorization: document.getElementById('ea_work_authorization').value,
      expected_salary: document.getElementById('ea_expected_salary').value,
      notice_period: document.getElementById('ea_notice_period').value,
      extra_answers: extraAnswers,
    },
  };
  try {
    const res = await fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!res.ok) throw new Error('保存失败');
    if (invalidLines.length) {
      // 静默丢弃过一次真实数据（用户以为设置没保存），现在必须显式告诉用户哪几行
      // 没解析成功，而不是只保存"看起来对"的那部分就算完事。
      showToast(
        `设置已保存，但"其它常见问题"里有 ${invalidLines.length} 行没识别出来（缺少"="分隔符，已跳过）：${invalidLines.join(' / ')}`,
        'error', 10000,
      );
    } else {
      showToast('设置已保存', 'success');
    }
    const hint = document.getElementById('configSavedHint');
    hint.style.display = 'inline';
    setTimeout(() => (hint.style.display = 'none'), 2500);
  } catch (e) {
    showToast(`保存失败：${e.message}`, 'error');
  } finally {
    restoreBtn(btn);
  }
}

// ---------- search run ----------
async function runNow() {
  const keywords = document.getElementById('keywords').value.split('\n').map((s) => s.trim()).filter(Boolean);
  if (keywords.length === 0) {
    showToast('请先填写搜索设置里的关键词', 'error');
    openMoreModal('settings');
    return;
  }
  const btn = document.getElementById('runNowBtn');
  setBtnLoading(btn, '搜索中…');
  try {
    const res = await (await fetch('/api/search/run', { method: 'POST' })).json();
    const parts = [`找到 ${res.found}`, `新增 ${res.added}`, `去重跳过 ${res.skipped_duplicate}`, `不相关跳过 ${res.skipped_irrelevant}`];
    showToast(`搜索完成：${parts.join(' · ')}`, res.errors && res.errors.length ? 'error' : 'success');
    if (res.errors && res.errors.length) showToast(res.errors.join('; '), 'error', 6000);
  } catch (e) {
    showToast(`搜索失败：${e.message}`, 'error');
  } finally {
    restoreBtn(btn);
    loadJobs();
    loadRuns();
  }
}

// ---------- jobs ----------
// 状态筛选（新/已收藏/已忽略/全部）复用顶部统计卡片当筛选按钮，不再单独放一排chip
// （见 filterByStatus()），只有"外企/国内公司/全部"这组还是独立的chip。
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('originChips').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    document.querySelectorAll('#originChips .chip').forEach((c) => c.classList.remove('active'));
    chip.classList.add('active');
    currentOrigin = chip.dataset.origin;
    renderJobs();
  });
  document.getElementById('appStatusChips').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    document.querySelectorAll('#appStatusChips .chip').forEach((c) => c.classList.remove('active'));
    chip.classList.add('active');
    currentAppStatus = chip.dataset.appstatus;
    renderJobs();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeJobDetailModal();
      closeMoreModal();
    }
  });
});

async function loadJobs(showSkeleton) {
  const skeleton = document.getElementById('jobsSkeleton');
  const list = document.getElementById('jobList');
  if (showSkeleton) {
    skeleton.style.display = 'flex';
    list.style.display = 'none';
  }
  try {
    allJobs = await (await fetch('/api/jobs')).json();
    try {
      const res = await fetch('/api/tracker');
      trackerEntries = res.ok ? await res.json() : [];
    } catch (e) {
      trackerEntries = [];
    }
    trackerIndex = {};
    trackerEntries.forEach((entry) => { trackerIndex[dedupeKey(entry.company, entry.job_title)] = entry; });
    updateStats();
    renderJobs();
    updateAiAnalyzeAllBtn();
    scheduleAnalyzingPoll();
  } catch (e) {
    showToast(`加载职位失败：${e.message}`, 'error');
  } finally {
    skeleton.style.display = 'none';
    list.style.display = 'flex';
  }
}

// 新职位入库后后台会自动开始AI分析（见 app.py 的 /api/search/run），这个过程不是
// 请求-响应式的，前端没法"等它做完"，只能轮询 /api/jobs 让"AI分析中"按钮状态跟后台
// 实际进度对上；没有职位在分析时不轮询，避免空转。
let analyzingPollTimer = null;
function scheduleAnalyzingPoll() {
  if (analyzingPollTimer) return;
  if (!allJobs.some((j) => j.analysis_state)) return;
  analyzingPollTimer = setTimeout(async () => {
    analyzingPollTimer = null;
    await loadJobs();
  }, 4000);
}

// ---------- top "AI分析" 按钮：批量分析所有待审核职位（含历史积压），可再点一次停止 ----------
function updateAiAnalyzeAllBtn() {
  const btn = document.getElementById('aiAnalyzeAllBtn');
  // 按钮自己触发的请求还没结束时（setBtnLoading 状态，disabled=true），不要覆盖它的
  // loading 文案；等请求结束 restoreBtn 之后，下一次 loadJobs 会再调用本函数校准状态。
  if (!btn || btn.disabled) return;
  const label = document.getElementById('aiAnalyzeAllBtnLabel');
  const analyzing = allJobs.some((j) => j.analysis_state);
  btn.classList.toggle('btn-primary', !analyzing);
  btn.classList.toggle('btn-danger', analyzing);
  label.textContent = analyzing ? '停止分析' : 'AI分析';
}

async function toggleAnalyzeAll() {
  const analyzing = allJobs.some((j) => j.analysis_state);
  if (analyzing) await stopAnalyzeAll();
  else await startAnalyzeAll();
}

async function startAnalyzeAll() {
  const btn = document.getElementById('aiAnalyzeAllBtn');
  setBtnLoading(btn, '启动中…');
  try {
    const res = await (await fetch('/api/jobs/analyze_all', { method: 'POST' })).json();
    showToast(res.count > 0 ? `已开始批量AI分析，共 ${res.count} 条待处理` : '没有需要分析的职位', 'info', 5000);
  } catch (e) {
    showToast(`启动失败：${e.message}`, 'error');
  } finally {
    restoreBtn(btn);
    await loadJobs();
  }
}

async function stopAnalyzeAll() {
  const btn = document.getElementById('aiAnalyzeAllBtn');
  setBtnLoading(btn, '停止中…');
  try {
    await fetch('/api/jobs/analyze_stop', { method: 'POST' });
    showToast('已停止分析，当前这条职位的分析结果不会被保存', 'info', 5000);
  } catch (e) {
    showToast(`停止失败：${e.message}`, 'error');
  } finally {
    restoreBtn(btn);
    await loadJobs();
  }
}

function updateStats() {
  const counts = { new: 0, reviewed: 0, dismissed: 0 };
  for (const j of allJobs) {
    if (counts[j.status] !== undefined) counts[j.status] += 1;
  }
  document.getElementById('statNew').textContent = counts.new;
  document.getElementById('statReviewed').textContent = counts.reviewed;
  document.getElementById('statDismissed').textContent = counts.dismissed;
  updateStatCardActive();
}

// 顶部统计卡片兼任状态筛选按钮（取代原来独立的一排"新/已收藏/已忽略/全部"chip）：
// 点中的那张卡片高亮，currentStatus 为空字符串（"全部"）时三张都不高亮。
function updateStatCardActive() {
  document.querySelectorAll('.stat-card.clickable').forEach((card) => {
    card.classList.toggle('active', card.dataset.status === currentStatus && currentStatus !== '');
  });
}

function toggleStarredFilter() {
  starredOnly = !starredOnly;
  document.getElementById('starredChip').classList.toggle('active', starredOnly);
  renderJobs();
}

function filterByStatus(status) {
  // 再点一次已经选中的卡片，等于取消筛选、回到"全部"——省掉专门放一个"全部"按钮的空间。
  currentStatus = currentStatus === status ? '' : status;
  updateStatCardActive();
  renderJobs();
}

function matchBadge(job) {
  if (job.overall_match != null) {
    const pct = Math.round(job.overall_match * 100);
    let cls = 'match-low';
    if (pct >= 70) cls = 'match-high';
    else if (pct >= 40) cls = 'match-mid';
    return `<span class="match-pill ${cls}">${pct}%</span>`;
  }
  if (!job.jd_text) {
    return `<span class="match-pill match-none" title="未获取到JD正文，已跳过AI分析，可点「重新获取」重试">JD未获取</span>`;
  }
  if (job.analysis_error) {
    return `<span class="match-pill match-fail" title="${escapeHtml(job.analysis_error)}">分析失败</span>`;
  }
  return `<span class="match-pill match-none">未分析</span>`;
}

function originBadge(companyOrigin) {
  if (companyOrigin === 'foreign') return '<span class="badge" title="AI分析判断为外企/海外总部公司">🌍 外企</span>';
  if (companyOrigin === 'domestic') return '<span class="badge" title="AI分析判断为中国大陆本土公司">🇨🇳 中国公司</span>';
  return '';
}

function siteBadge(site) {
  const s = (site || '').toLowerCase();
  if (s.includes('indeed')) return '<span class="badge badge-site-indeed">Indeed</span>';
  if (s.includes('linkedin')) return '<span class="badge badge-site-linkedin">LinkedIn</span>';
  return site ? `<span class="badge">${escapeHtml(site)}</span>` : '';
}

const STATUS_LABELS = { new: ['新', 'badge-status-new'], reviewed: ['已收藏', 'badge-status-reviewed'], dismissed: ['已忽略', 'badge-status-dismissed'] };
function statusBadge(status) {
  const [label, cls] = STATUS_LABELS[status] || [status, ''];
  return `<span class="badge ${cls}">${escapeHtml(label)}</span>`;
}

const APPLICATION_STATUS_LABELS = {
  not_applied: '待投',
  applied: '已投递',
  interviewing: '面试中',
  rejected: '已拒绝',
  offer: 'Offer',
  declined: '已婉拒',
};
function applicationStatusSelectHtml(job) {
  const current = job.application_status || 'not_applied';
  const opts = Object.entries(APPLICATION_STATUS_LABELS)
    .map(([k, label]) => `<option value="${k}" ${current === k ? 'selected' : ''}>${label}</option>`).join('');
  return `<select class="app-status-select" data-appstatus="${current}" title="投递状态"
    onclick="event.stopPropagation()"
    onchange="event.stopPropagation(); setApplicationStatus(${job.id}, this.value); this.dataset.appstatus = this.value;">${opts}</select>`;
}

const CHECK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';
const X_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';
const SPARK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 1.9 4.9L19 9l-5.1 1.9L12 16l-1.9-5.1L5 9l5.1-1.9L12 3z"/></svg>';
const STAR_PATH = '<path d="m12 3.2 2.7 5.5 6.1.9-4.4 4.3 1 6-5.4-2.9-5.4 2.9 1-6-4.4-4.3 6.1-.9L12 3.2z"/>';
const STAR_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${STAR_PATH}</svg>`;
const STAR_ICON_FILLED = `<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${STAR_PATH}</svg>`;
const REFETCH_ICON ='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M8 16H3v5"/></svg>';

// 星标按钮对所有状态的卡片都渲染（"已忽略"卡片虽然会隐藏其它操作按钮，这个也保留）——
// 否则已经标过重点关注的职位被忽略之后就再也没有入口取消星标了。
function starButtonHtml(job) {
  const starred = !!job.starred;
  return `<button class="icon-btn${starred ? ' starred' : ''}" title="${starred ? '取消重点关注' : '标记为重点关注'}"
    onclick="event.stopPropagation(); setJobStarred(${job.id}, ${!starred})">${starred ? STAR_ICON_FILLED : STAR_ICON}</button>`;
}

function analysisStateButtonHtml(job) {
  if (job.analysis_state === 'analyzing') {
    return `<button class="btn btn-secondary btn-sm" disabled><span class="spinner"></span>AI分析中…</button>`;
  }
  if (job.analysis_state === 'queued') {
    return `<button class="btn btn-secondary btn-sm" disabled>排队中…</button>`;
  }
  if (job.jd_text) {
    return `<button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); analyzeJob(${job.id}, this)">${SPARK_ICON}AI 分析</button>`;
  }
  return `<button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); refetchJd(${job.id}, this)">${REFETCH_ICON}重新获取</button>`;
}

const EASY_APPLY_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';

function easyApplyButtonHtml(job) {
  // 只对 LinkedIn 职位、且已经生成过定制简历的开放——半自动投递要上传的就是这份简历。
  if ((job.site || '').toLowerCase() !== 'linkedin' || !job.resume_path) return '';
  if (job.easy_apply_state === 'opening') {
    return `<button class="btn btn-secondary btn-sm" disabled><span class="spinner"></span>启动中…</button>`;
  }
  const errorTitle = job.easy_apply_state === 'error' ? ` title="${escapeHtml(job.easy_apply_error || '')}"` : '';
  return `<button class="btn btn-secondary btn-sm"${errorTitle} onclick="event.stopPropagation(); startEasyApply(${job.id})">${EASY_APPLY_ICON}Easy Apply${job.easy_apply_state === 'error' ? '（失败，点击重试）' : ''}</button>`;
}

async function startEasyApply(id) {
  try {
    const res = await fetch(`/api/jobs/${id}/easy_apply`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '未知错误');
    showToast('正在打开浏览器窗口并尝试自动填写 Easy Apply 表单…', 'info', 4000);
    await pollEasyApplyUntilSettled(id);
  } catch (e) {
    showToast(`启动失败：${e.message}`, 'error', 6000);
  } finally {
    await loadJobs();
  }
}

async function pollEasyApplyUntilSettled(id, { intervalMs = 3000, timeoutMs = 60000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, intervalMs));
    await loadJobs();
    const job = allJobs.find((j) => j.id === id);
    if (!job || job.easy_apply_state !== 'opening') {
      if (job && job.easy_apply_state === 'error') {
        showToast(`Easy Apply 未能自动打开：${job.easy_apply_error || '未知错误'}`, 'error', 8000);
      } else {
        showToast('浏览器窗口已打开，材料已尽量自动填好，请切换过去手动检查并自己点击提交', 'success', 8000);
      }
      return;
    }
  }
  showToast('仍在启动中，可能是浏览器/登录检查比较慢，可稍后查看是否已弹出窗口', 'info', 6000);
}

function resumeLinkHtml(job) {
  if (!job.resume_path) return '';
  return `<a class="dot-sep job-link" href="/api/jobs/${job.id}/resume" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">📄 定制简历</a>`;
}

function coverLetterLinkHtml(job) {
  const e = trackerIndex[dedupeKey(job.company, job.title)];
  if (!e || !e.cover_letter) return '';
  return `<a class="dot-sep job-link" href="#" onclick="event.stopPropagation(); openJobDetailModal(${job.id}); return false;">✉️ Cover Letter</a>`;
}

function renderJobs() {
  const list = document.getElementById('jobList');
  const empty = document.getElementById('jobsEmpty');
  const q = document.getElementById('jobSearch').value.trim().toLowerCase();

  let jobs = allJobs;
  if (currentStatus) jobs = jobs.filter((j) => j.status === currentStatus);
  // "外企"：排除已判定为国内公司的职位，未分析/判断不出的仍保留，避免误藏还没看过的职位
  if (currentOrigin === 'foreign') jobs = jobs.filter((j) => j.company_origin !== 'domestic');
  else if (currentOrigin === 'domestic') jobs = jobs.filter((j) => j.company_origin === 'domestic');
  if (currentAppStatus) jobs = jobs.filter((j) => (j.application_status || 'not_applied') === currentAppStatus);
  if (starredOnly) jobs = jobs.filter((j) => !!j.starred);
  if (q) jobs = jobs.filter((j) => (j.title || '').toLowerCase().includes(q) || (j.company || '').toLowerCase().includes(q));

  if (jobs.length === 0) {
    list.innerHTML = '';
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';

  list.innerHTML = jobs.map((j) => {
    const clickable = j.overall_match != null;
    return `
    <div class="job-card${clickable ? ' clickable' : ''}" data-id="${j.id}" ${clickable ? `onclick="openJobDetailModal(${j.id})"` : ''}>
      <div class="job-main">
        <div class="job-title-row">
          <a class="job-title" href="${safeUrl(j.job_url)}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">${escapeHtml(j.title)}</a>
          ${siteBadge(j.site)}
          ${originBadge(j.company_origin)}
          ${currentStatus === '' ? statusBadge(j.status) : ''}
        </div>
        <div class="job-meta">
          <span>${escapeHtml(j.company)}</span>
          ${j.location ? `<span class="dot-sep">${escapeHtml(j.location)}</span>` : ''}
          <span class="dot-sep">${escapeHtml(j.first_seen)}</span>
          ${j.status === 'dismissed' ? '' : `${resumeLinkHtml(j)}${coverLetterLinkHtml(j)}`}
        </div>
      </div>
      ${matchBadge(j)}
      ${interviewPrepBadgeHtml(j)}
      <div class="job-actions">
        ${j.status === 'dismissed' ? '' : `
        ${j.status === 'new' ? '' : applicationStatusSelectHtml(j)}
        ${j.status === 'reviewed' ? '' : analysisStateButtonHtml(j)}
        ${j.status === 'reviewed' ? easyApplyButtonHtml(j) : ''}
        `}
        ${starButtonHtml(j)}
        ${j.status === 'reviewed' ? '' : `<button class="icon-btn" title="标记已收藏" onclick="event.stopPropagation(); setJobStatus(${j.id}, 'reviewed')">${CHECK_ICON}</button>`}
        ${j.status === 'dismissed' ? '' : `<button class="icon-btn" title="忽略" onclick="event.stopPropagation(); setJobStatus(${j.id}, 'dismissed')">${X_ICON}</button>`}
      </div>
    </div>
  `;
  }).join('');
}

async function analyzeJob(id, btn) {
  setBtnLoading(btn, '分析中…');
  try {
    const res = await fetch(`/api/jobs/${id}/analyze`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '未知错误');
    const pct = Math.round(data.overall_match * 100);
    showToast(`分析完成：匹配度 ${pct}%${data.resume_path ? '，已生成定制简历' : ''}`, 'success', 6000);
  } catch (e) {
    showToast(`分析失败：${e.message}`, 'error', 6000);
  } finally {
    await loadJobs();
  }
}

async function refetchJd(id, btn) {
  setBtnLoading(btn, '获取中…');
  const before = allJobs.find((j) => j.id === id) || {};
  const beforeJd = before.jd_text || '';
  const beforeError = before.analysis_error || '';
  try {
    const res = await fetch(`/api/jobs/${id}/refetch_jd`, { method: 'POST' });
    if (!res.ok) throw new Error((await res.json()).error || '未知错误');
    // 实际抓取（重新搜索+抓LinkedIn详情页，可能要一两分钟）放后台线程跑，不用一直挂着
    // 这次请求——挂太久容易被浏览器/网络中间层判定连接失活而中断（"Failed to fetch"）。
    // 改成轮询 /api/jobs 直到这条职位的 jd_text/analysis_error 出现变化（抓到JD后会自动
    // 接着跑AI分析，见 pipeline.refetch_jd），成功后自动刷新列表，不需要用户手动点"刷新"。
    await pollJobUntilSettled(id, beforeJd, beforeError);
  } catch (e) {
    showToast(`重新获取失败：${e.message}`, 'error', 6000);
  } finally {
    restoreBtn(btn);
  }
}

async function pollJobUntilSettled(id, beforeJd, beforeError, { intervalMs = 4000, timeoutMs = 240000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, intervalMs));
    await loadJobs();
    const job = allJobs.find((j) => j.id === id);
    if (!job) return;
    if ((job.jd_text || '') !== beforeJd || (job.analysis_error || '') !== beforeError) {
      if (job.jd_text) {
        const pct = job.overall_match != null ? Math.round(job.overall_match * 100) : null;
        showToast(
          pct != null
            ? `已重新获取JD正文，AI分析完成：匹配度 ${pct}%${job.resume_path ? '，已生成定制简历' : ''}`
            : `已重新获取JD正文，但AI分析失败：${job.analysis_error || '未知错误'}`,
          pct != null ? 'success' : 'error', 6000,
        );
      } else {
        showToast(`仍未获取到JD正文：${job.analysis_error || '可稍后再试'}`, 'error', 6000);
      }
      return;
    }
  }
  showToast('仍在后台处理中，可稍后点"刷新"查看结果', 'info', 6000);
}

async function refetchAllJd(btn) {
  setBtnLoading(btn, '已开始…');
  try {
    await fetch('/api/jobs/refetch_jd', { method: 'POST' });
    showToast('已开始批量重新获取JD正文（后台进行，完成后点"刷新"查看结果）', 'info', 6000);
  } catch (e) {
    showToast(`触发失败：${e.message}`, 'error');
  } finally {
    restoreBtn(btn);
  }
}

async function classifyCompanyOrigin(btn) {
  setBtnLoading(btn, '已开始…');
  try {
    await fetch('/api/jobs/classify_origin', { method: 'POST' });
    showToast('已开始批量识别公司国籍（后台进行，完成后点"刷新"查看结果）', 'info', 6000);
  } catch (e) {
    showToast(`触发失败：${e.message}`, 'error');
  } finally {
    restoreBtn(btn);
  }
}

async function setJobStatus(id, status) {
  try {
    await fetch(`/api/jobs/${id}/status`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }) });
    const label = STATUS_LABELS[status] ? STATUS_LABELS[status][0] : status;
    showToast(`已标记为「${label}」`, 'success', 2000);
  } catch (e) {
    showToast(`操作失败：${e.message}`, 'error');
  } finally {
    loadJobs();
  }
}

async function setJobStarred(id, starred) {
  try {
    const res = await fetch(`/api/jobs/${id}/starred`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ starred }),
    });
    if (!res.ok) throw new Error((await res.json()).error || '未知错误');
    showToast(starred ? '已标记为「重点关注」' : '已取消「重点关注」', 'success', 2000);
  } catch (e) {
    showToast(`操作失败：${e.message}`, 'error');
  } finally {
    loadJobs();
  }
}

async function setApplicationStatus(id, applicationStatus) {
  try {
    const res = await fetch(`/api/jobs/${id}/application_status`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ application_status: applicationStatus }),
    });
    const data = await res.json().catch(() => ({}));
    showToast(`投递状态已更新为「${APPLICATION_STATUS_LABELS[applicationStatus] || applicationStatus}」`, 'success', 2000);
    if (data.interview_prep_started) {
      // 后端已经起了后台线程在生成，这里只负责告知 + 安排轮询，等生成完卡片上会出现 🎤 标记
      showToast('已开始生成这条职位的面试准备材料，完成后卡片上会出现 🎤 标记', 'info', 6000);
      pollInterviewPrepUntilDone(id);
    }
  } catch (e) {
    showToast(`操作失败：${e.message}`, 'error');
  } finally {
    loadJobs();
  }
}

// ---------- runs ----------
async function loadRuns() {
  const runs = await (await fetch('/api/runs')).json();
  const tbody = document.querySelector('#runsTable tbody');
  tbody.innerHTML = runs.map((r) => `
    <tr>
      <td>${escapeHtml(r.ran_at)}</td>
      <td>${escapeHtml(r.keywords)}</td>
      <td>${r.found}</td>
      <td>${r.added}</td>
      <td>${r.skipped_duplicate}</td>
      <td>${r.skipped_irrelevant ?? '-'}</td>
      <td>${r.error ? `<span class="run-error">${escapeHtml(r.error)}</span>` : '<span class="run-ok">正常</span>'}</td>
    </tr>
  `).join('');

  if (runs.length) {
    const last = runs[0];
    document.getElementById('statLastRun').textContent = last.ran_at;
  } else {
    document.getElementById('statLastRun').textContent = '暂无';
  }
}

// ---------- job detail modal ----------
function reqListHtml(items) {
  if (!items || !items.length) return '<div class="plain-text" style="color:var(--text-faint);">（无）</div>';
  return `<ul class="req-list">${items.map((it) => `<li class="${it.is_gap ? 'gap' : ''}">${escapeHtml(it.text)}</li>`).join('')}</ul>`;
}

// 把投递状态改成"面试中"会触发后台生成面试准备材料。这里跟进到跑完为止，好让卡片上的
// 🎤 标记自己冒出来，不用用户手动刷新。真正看内容是在 /jobs/<id>/interview 页面上，
// 那边有自己的轮询，跟这条互不相干。
function pollInterviewPrepUntilDone(jobId, elapsed = 0) {
  if (elapsed > 300000) return; // 最多跟5分钟，超时就不管了，用户手动刷新也能看到
  setTimeout(async () => {
    let done = true;
    try {
      const fresh = await (await fetch(`/api/jobs/${jobId}`)).json();
      done = fresh.interview_prep_state !== 'generating';
    } catch (e) {
      done = false;
    }
    if (done) loadJobs();
    else pollInterviewPrepUntilDone(jobId, elapsed + 5000);
  }, 5000);
}

// 职位卡片上的 🎤 标记：有面试准备材料就点进那条职位的面试准备页。
// （以前这里是打开详情弹窗并切到"面试准备"tab，现在面试准备是独立页面了。）
function interviewPrepBadgeHtml(job) {
  if (job.interview_prep_state === 'generating') {
    return '<span class="match-pill prep-pill" title="正在生成面试准备材料">🎤 准备中…</span>';
  }
  if (!job.has_interview_prep) return '';
  return `<a class="match-pill prep-pill done" href="/jobs/${job.id}/interview"
    title="已有面试准备材料，点击查看" onclick="event.stopPropagation()">🎤 面试准备</a>`;
}

function openJobDetailModal(jobId) {
  const job = allJobs.find((j) => j.id === jobId);
  if (!job) return;
  const e = trackerIndex[dedupeKey(job.company, job.title)] || null;
  if (!e && !job.has_interview_prep) {
    showToast('未在追踪表中找到匹配的分析详情，可能追踪表文件路径已更改', 'error');
    return;
  }

  document.getElementById('jobDetailModalTitle').textContent = `${(e && e.job_title) || job.title || ''} · ${(e && e.company) || job.company || ''}`;
  document.getElementById('jobDetailPrepLink').href = `/jobs/${jobId}/interview`;

  const jobUrl = (e && e.job_url) || job.job_url;
  const body = !e ? `
    ${jobUrl ? `<div class="detail-section"><a class="job-title" href="${safeUrl(jobUrl)}" target="_blank" rel="noopener noreferrer">查看原职位页面 ↗</a></div>` : ''}
    <div class="detail-section"><div class="plain-text" style="color:var(--text-faint);">未在追踪表中找到这条职位的匹配分析记录（可能追踪表路径变了，或还没跑过 AI 分析）。面试准备不受影响，点右上角「🎤 面试准备」查看。</div></div>
  ` : `
    ${jobUrl ? `<div class="detail-section"><a class="job-title" href="${safeUrl(jobUrl)}" target="_blank" rel="noopener noreferrer">查看原职位页面 ↗</a></div>` : ''}
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
    <div class="detail-section">
      <h4>状态 / 下一步</h4>
      <div class="plain-text">${escapeHtml(e.status || '—')}</div>
    </div>
    <div class="detail-section">
      <h4>定制简历</h4>
      ${job.resume_path
        ? `<a class="job-title" href="/api/jobs/${job.id}/resume" target="_blank" rel="noopener noreferrer">打开定制简历 ↗</a>
           <div class="plain-text" style="color:var(--text-faint); font-size:0.75rem; margin-top:0.25rem;">${escapeHtml(e.resume_path || '')}</div>
           <div style="margin-top:0.5rem;">${bulletListHtml(e.resume_optimization_bullets)}</div>`
        : '<div class="plain-text">（未生成）</div>'}
    </div>
    <div class="detail-section">
      <h4>Cover Letter</h4>
      <div class="plain-text">${e.cover_letter ? escapeHtml(e.cover_letter) : '（未生成）'}</div>
    </div>
  `;
  document.getElementById('detailSubpanel-analysis').innerHTML = body;
  document.getElementById('jobDetailModalOverlay').classList.add('active');
}

function closeJobDetailModal() {
  document.getElementById('jobDetailModalOverlay').classList.remove('active');
}

// ---------- init ----------
initTheme();
loadConfig();
loadJobs(true);
loadRuns();
