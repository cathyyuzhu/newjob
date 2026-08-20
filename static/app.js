// ---------- state ----------
let allJobs = [];
let currentStatus = 'new';
// 默认「全部」而不是「外企」：默认藏掉一半结果，用户看到的空列表分不清是"没搜到"
// 还是"被默认筛选挡住了"。挑不挑外企是用户自己的判断。
let currentOrigin = '';
let currentAppStatus = '';
// "重点关注"筛选：独立开关（不跟外企/国内公司互斥），开启后只显示标了星的职位
let starredOnly = false;
// 标签筛选：单选（再点一次取消），空字符串表示不筛
let currentTag = '';

// ---------- 筛选状态 ↔ URL ----------
// 五套筛选（审核状态 / 公司国籍 / 重点关注 / 投递状态 / 标签）加搜索词原来只活在内存里，
// 刷新页面就全重置回"待审核的外企"。而好几处后台任务的提示恰恰让用户去刷新看结果，
// 一刷新筛选就没了。把状态放进 query string：刷新不丢，也能把某个组合存成书签。
const FILTER_DEFAULTS = { status: 'new', origin: '', app: '', starred: false, tag: '', q: '' };

function readFiltersFromUrl() {
  const p = new URLSearchParams(location.search);
  // 只认 URL 里真正出现的参数，没出现的用默认值——这样 /（不带参数）行为跟以前一致
  if (p.has('status')) currentStatus = p.get('status');
  if (p.has('origin')) currentOrigin = p.get('origin');
  if (p.has('app')) currentAppStatus = p.get('app');
  if (p.has('starred')) starredOnly = p.get('starred') === '1';
  if (p.has('tag')) currentTag = p.get('tag');
  if (p.has('q')) document.getElementById('jobSearch').value = p.get('q');
}

function writeFiltersToUrl() {
  const p = new URLSearchParams();
  const q = document.getElementById('jobSearch').value.trim();
  // 跟默认值一样的就不写进 URL，免得地址栏挂一串没有信息量的参数
  if (currentStatus !== FILTER_DEFAULTS.status) p.set('status', currentStatus);
  if (currentOrigin !== FILTER_DEFAULTS.origin) p.set('origin', currentOrigin);
  if (currentAppStatus !== FILTER_DEFAULTS.app) p.set('app', currentAppStatus);
  if (starredOnly) p.set('starred', '1');
  if (currentTag) p.set('tag', currentTag);
  if (q) p.set('q', q);
  const qs = p.toString();
  const next = qs ? `${location.pathname}?${qs}` : location.pathname;
  // renderJobs() 每次轮询都会调到这里，没变化就别写——省掉每 4 秒一次无意义的 replaceState
  if (next === location.pathname + location.search) return;
  // replaceState 而不是 pushState：筛选不该往浏览器历史里塞一堆条目、让"后退"变成逐个撤销筛选
  history.replaceState(null, '', next);
}

// 把内存里的筛选状态同步到控件高亮上（从 URL 恢复后调用一次）
function syncFilterControls() {
  document.querySelectorAll('#originChips .chip').forEach((c) => {
    c.classList.toggle('active', (c.dataset.origin || '') === currentOrigin);
  });
  document.querySelectorAll('#appStatusChips .chip').forEach((c) => {
    c.classList.toggle('active', (c.dataset.appstatus || '') === currentAppStatus);
  });
  syncTagChipsActive();
  // 「重点关注」的开关状态现在体现在统计卡片上（原来是筛选栏里一个 chip），
  // 高亮统一交给 updateStatCardActive() 处理
  updateStatCardActive();
}

function hasActiveFilters() {
  return currentStatus !== '' || currentOrigin !== '' || currentAppStatus !== ''
    || starredOnly || currentTag !== '' || document.getElementById('jobSearch').value.trim() !== '';
}

function clearAllFilters() {
  currentStatus = '';
  currentOrigin = '';
  currentAppStatus = '';
  starredOnly = false;
  currentTag = '';
  document.getElementById('jobSearch').value = '';
  syncFilterControls();
  renderJobs();
}

// ---------- 标签筛选 chips ----------
// 集合是动态的：预设标签（跟 common.js 的 PRESET_TAGS 保持一致）+ 库里所有职位实际
// 在用的自定义标签，去重后渲染。渲染时机跟着 renderJobs()——标签是打在职位上的，
// 新增/删除标签会改变这个集合，而那些操作本来就会触发一次 loadJobs()。
function collectAllTags() {
  const set = new Set(PRESET_TAGS);
  allJobs.forEach((j) => {
    (j.tags || '').split(',').map((t) => t.trim()).filter(Boolean).forEach((t) => set.add(t));
  });
  return Array.from(set);
}

function renderTagChips() {
  const el = document.getElementById('tagChips');
  const tags = collectAllTags();
  el.innerHTML = tags.map((t) => `<button class="chip ${t === currentTag ? 'active' : ''}" data-tag="${escapeHtml(t)}">${escapeHtml(t)}</button>`).join('');
}

function syncTagChipsActive() {
  document.querySelectorAll('#tagChips .chip').forEach((c) => {
    c.classList.toggle('active', c.dataset.tag === currentTag);
  });
}

function toggleTagFilter(tag) {
  currentTag = currentTag === tag ? '' : tag;
  syncTagChipsActive();
  renderJobs();
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
  if (subtab === 'settings') loadPreferenceProfile();
}

// ---------- 偏好档案 ----------
async function loadPreferenceProfile() {
  const el = document.getElementById('preferenceProfileCard');
  if (!el) return;
  try {
    const profile = await (await fetch('/api/preferences')).json();
    if (profile.error) {
      el.innerHTML = `<div class="plain-text" style="color:var(--danger,#c0392b);">上一次生成失败：${escapeHtml(profile.error)}</div>`;
    } else if (profile.content_text) {
      el.innerHTML = `<div class="plain-text">${escapeHtml(profile.content_text)}</div>
        <div class="plain-text" style="color:var(--text-faint); margin-top:4px;">生成于 ${escapeHtml(profile.created_at)}（基于 ${profile.source_reason_count} 条忽略原因）</div>`;
    } else {
      el.innerHTML = '<div class="plain-text" style="color:var(--text-faint);">还没有偏好档案——忽略职位时留几次原因，攒够几条会自动生成。</div>';
    }
  } catch (e) {
    el.innerHTML = `<div class="plain-text" style="color:var(--text-faint);">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

async function regeneratePreferenceProfile(btn) {
  setBtnLoading(btn, '生成中…');
  try {
    const res = await fetch('/api/preferences/regenerate', { method: 'POST' });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || '未知错误');
    showToast('已开始重新生成，几秒后刷新本面板可看到结果', 'info', 5000);
  } catch (e) {
    showToast(`触发失败：${e.message}`, 'error');
  } finally {
    restoreBtn(btn);
    setTimeout(loadPreferenceProfile, 4000);
  }
}

// ---------- config ----------
async function loadConfig() {
  const cfg = await (await fetch('/api/config')).json();
  document.getElementById('keywords').value = (cfg.keywords || []).join('\n');
  document.getElementById('locations').value = (cfg.locations || []).join('\n');
  document.getElementById('linkedinTargetCompanies').value = (cfg.linkedin_target_companies || []).map((c) => c.name).join('\n');
  renderTargetCompanyStatus(cfg.linkedin_target_companies || []);
  document.getElementById('country_indeed').value = cfg.country_indeed || '';
  document.getElementById('results_wanted').value = cfg.results_wanted;
  document.getElementById('days_old').value = cfg.days_old;
  document.getElementById('schedule_enabled').checked = cfg.schedule_enabled !== false;
  document.getElementById('schedule_hour').value = cfg.schedule_hour;
  document.getElementById('schedule_minute').value = cfg.schedule_minute;
  document.getElementById('tracker_xlsx_path').value = cfg.tracker_xlsx_path || '';
  document.getElementById('resume_output_dir').value = cfg.resume_output_dir || '';
  // 简历改成在 /resume 页上传了，这里只读展示当前用的是哪份（saveConfig 不提交它）
  const resumeMeta = cfg.base_resume_meta || {};
  document.getElementById('settingsResumeName').textContent = cfg.base_resume_path
    ? `${resumeMeta.original_filename || cfg.base_resume_path}${resumeMeta.uploaded_at ? `（上传于 ${resumeMeta.uploaded_at}）` : ''}`
    : '还没有上传简历';
  // 四个模型下拉自己拉 /api/models 填充。它们是"选了就存"的，不跟着下面的「保存设置」走
  // ——所以 saveConfig() 里也不能再提交一遍 llm_tasks，否则会把另外几页刚改的覆盖掉。
  ['analysis', 'materials', 'interview_prep', 'interview_bank', 'resume_review', 'job_chat', 'preference_profile'].forEach((task) => {
    initModelSelect(`modelTask-${task}`, task);
  });
  const eaProfile = cfg.easy_apply_profile || {};
  document.getElementById('ea_work_authorization').value = eaProfile.work_authorization || '';
  document.getElementById('ea_expected_salary').value = eaProfile.expected_salary || '';
  document.getElementById('ea_notice_period').value = eaProfile.notice_period || '';
  document.getElementById('ea_extra_answers').value = (eaProfile.extra_answers || [])
    .map((qa) => `${qa.keyword}=${qa.answer}`).join('\n');
}

function renderTargetCompanyStatus(list) {
  const el = document.getElementById('targetCompanyStatus');
  if (!list.length) {
    el.textContent = '';
    return;
  }
  const resolved = list.filter((c) => c.status === 'resolved').map((c) => c.name);
  const failed = list.filter((c) => c.status !== 'resolved').map((c) => c.name);
  const parts = [];
  if (resolved.length) parts.push(`已解析：${resolved.join('、')}`);
  if (failed.length) parts.push(`解析失败（检查拼写，或换成完整 LinkedIn 公司主页链接）：${failed.join('、')}`);
  el.textContent = parts.join(' · ');
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
    linkedin_target_companies: document.getElementById('linkedinTargetCompanies').value.split('\n'),
    country_indeed: document.getElementById('country_indeed').value,
    results_wanted: document.getElementById('results_wanted').value,
    days_old: document.getElementById('days_old').value,
    schedule_enabled: document.getElementById('schedule_enabled').checked,
    schedule_hour: document.getElementById('schedule_hour').value,
    schedule_minute: document.getElementById('schedule_minute').value,
    tracker_xlsx_path: document.getElementById('tracker_xlsx_path').value,
    // 刻意不提交 base_resume_path：它现在只由 /resume 页的上传/删除流程写，
    // 后端白名单里也去掉了。从这里提交一个只读框的值只会把刚传的简历覆盖没。
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
    const savedCfg = await res.json();
    renderTargetCompanyStatus(savedCfg.linkedin_target_companies || []);
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
    // 搜索本身不需要简历，所以这里是 200 而不是 409——职位已经抓到了，只是没法自动
    // 算匹配度。额外提示一条，不掩盖上面那条"搜索完成"。
    if (res.need_resume) handleNeedResume({ need_resume: true, error: res.need_resume_message });
  } catch (e) {
    showToast(`搜索失败：${e.message}`, 'error');
  } finally {
    restoreBtn(btn);
    loadJobs();
    loadRuns();
  }
}

// ---------- 智能抓取下拉（2026-08-18：「添加链接」从独立按钮收进这个下拉菜单） ----------
function toggleRunNowMenu(e) {
  e.stopPropagation(); // 不冒泡到 document 的点外部关闭监听，不然刚点开就被自己关掉
  const menu = document.getElementById('runNowMenu');
  const open = menu.classList.toggle('open');
  document.getElementById('runNowCaretBtn').setAttribute('aria-expanded', open ? 'true' : 'false');
}

function closeRunNowMenu() {
  document.getElementById('runNowMenu').classList.remove('open');
  document.getElementById('runNowCaretBtn').setAttribute('aria-expanded', 'false');
}

// ---------- 添加职位链接 ----------
function openAddLinkModal() {
  document.getElementById('addLinkModalOverlay').classList.add('active');
  document.getElementById('addLinkUrls').focus();
}

function closeAddLinkModal() {
  document.getElementById('addLinkModalOverlay').classList.remove('active');
}

// 逐条渲染结果，而不是只弹一句"成功N条"：一次贴好几条时，用户真正要知道的是"哪一条
// 没进去、为什么"——重复和抓不到的处置完全不同（前者不用管，后者可能要换个链接重贴）。
function renderLinkResults(results) {
  const box = document.getElementById('addLinkResults');
  if (!results || !results.length) {
    box.innerHTML = '';
    return;
  }
  const label = { added: '已入库', duplicate: '已存在', failed: '失败' };
  box.innerHTML = results.map((r) => {
    const name = r.title ? `${escapeHtml(r.company || '')} · ${escapeHtml(r.title)}` : escapeHtml(r.url || '');
    // 入库成功的给一个直达职位详情页的链接，省得回列表里翻
    const link = r.status === 'added' && r.job_id
      ? ` <a href="/jobs/${r.job_id}" target="_blank" rel="noopener">查看</a>`
      : '';
    const note = r.status === 'added' && r.jd_missing
      ? '没抓到 JD 正文，可在列表里点「全部重新获取JD」'
      : (r.message || '');
    return `<div class="link-result ${r.status}">
      <span class="link-result-tag">${label[r.status] || r.status}</span>
      <span class="link-result-body"><strong>${name}</strong>${link}${note ? `<span class="link-result-note">${escapeHtml(note)}</span>` : ''}</span>
    </div>`;
  }).join('');
}

async function submitJobLinks() {
  const urls = document.getElementById('addLinkUrls').value.trim();
  if (!urls) {
    showToast('请先贴至少一条 LinkedIn 职位链接', 'error');
    return;
  }
  const btn = document.getElementById('addLinkSubmitBtn');
  // 抓取是同步的（还可能要开一次浏览器兜底），这里可能要等十几秒到一两分钟
  setBtnLoading(btn, '抓取中…');
  try {
    const res = await fetch('/api/jobs/add_by_url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ urls }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || '未知错误');
    const results = data.results || [];
    renderLinkResults(results);
    const added = results.filter((r) => r.status === 'added').length;
    const dup = results.filter((r) => r.status === 'duplicate').length;
    const failed = results.filter((r) => r.status === 'failed').length;
    showToast(`入库 ${added} 条 · 已存在 ${dup} 条 · 失败 ${failed} 条`, failed ? 'error' : 'success');
    // 成功入库的那些已经在后台排队分析了，把贴过的链接清掉，免得再点一次重复提交
    if (added) document.getElementById('addLinkUrls').value = '';
    if (data.need_resume) handleNeedResume({ need_resume: true, error: data.need_resume_message });
  } catch (e) {
    showToast(`添加失败：${e.message}`, 'error');
  } finally {
    restoreBtn(btn);
    loadJobs();
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
    currentAppStatus = chip.dataset.appstatus;
    // 选中具体投递状态时（不是"全部"）把审核状态那组卡片的筛选清掉，跟顶部卡片同一个单选组规则
    if (currentAppStatus) currentStatus = '';
    updateStatCardActive(); // 顺带同步顶部「已投递」「面试中」卡片的高亮，两处是同一个状态
    renderJobs();
  });
  // 标签 chips 是动态拼出来的（renderTagChips），用事件委托而不是逐个绑定
  document.getElementById('tagChips').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    toggleTagFilter(chip.dataset.tag);
  });
  // 搜索框防抖：renderJobs() 会把整个列表重新拼成 innerHTML，原来每敲一个字符重建一次整棵 DOM
  let searchDebounce = null;
  document.getElementById('jobSearch').addEventListener('input', () => {
    if (searchDebounce) clearTimeout(searchDebounce);
    searchDebounce = setTimeout(renderJobs, 200);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeMoreModal();
  });
  // 「智能抓取」下拉菜单：点它自己以外的任何地方都收起，跟其它 chip/modal 不一样，
  // 这个下拉没有遮罩层，得靠事件委托判断点击有没有落在 .split-btn 里面
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.split-btn')) closeRunNowMenu();
  });
});

// 工具栏"刷新"按钮。批量重新获取JD、识别公司国籍这些后台任务跑完没有任何通知，
// 提示语里一直让用户"点刷新"，但界面上以前根本没有这个按钮，只能按 F5——而 F5 会
// 把筛选全部重置（现在筛选进了 URL，F5 也不丢了，但重新拉数据仍然比整页重载轻）。
async function refreshJobs(btn) {
  setBtnLoading(btn, '刷新中…');
  try {
    await loadJobs();
    await loadRuns();
  } finally {
    restoreBtn(btn);
  }
}

async function loadJobs(showSkeleton) {
  const skeleton = document.getElementById('jobsSkeleton');
  const list = document.getElementById('jobList');
  if (showSkeleton) {
    skeleton.style.display = 'flex';
    list.style.display = 'none';
  }
  try {
    allJobs = await (await fetch('/api/jobs')).json();
    updateStats();
    renderTagChips();
    renderJobs();
    renderChecklist();
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
// 实际进度对上；没有职位在分析/生成材料时不轮询，避免空转。
// materials_state 跟 analysis_state 用同一套轮询——批量生成材料同样是后台串行跑的，
// 没必要为它单独再开一个定时器。
function anyJobBusy() {
  return allJobs.some((j) => j.analysis_state || j.materials_state);
}

let analyzingPollTimer = null;
function scheduleAnalyzingPoll() {
  if (analyzingPollTimer) return;
  if (!anyJobBusy()) return;
  analyzingPollTimer = setTimeout(async () => {
    analyzingPollTimer = null;
    // 页面在后台就别拉了，切回来时下面的 visibilitychange 会立刻补一次。
    // （题库页 bank.js 和面试准备页 interview.js 早就这么做了，只有这里漏了）
    if (document.hidden) {
      scheduleAnalyzingPoll();
      return;
    }
    await loadJobs();
  }, 4000);
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && anyJobBusy()) loadJobs();
});

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
    const resp = await fetch('/api/jobs/analyze_all', { method: 'POST' });
    const res = await resp.json();
    if (handleNeedResume(res)) return;
    if (!resp.ok) throw new Error(res.error || '未知错误');
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
  let starred = 0;
  let applied = 0;
  let interviewing = 0;
  let reviewedPending = 0;
  for (const j of allJobs) {
    if (counts[j.status] !== undefined) counts[j.status] += 1;
    // 重点关注是跨状态统计：一条职位可以既"已收藏"又"重点关注"，两个数字本来就该重叠
    if (j.starred) starred += 1;
    // 已投递/面试中同理是跨 status 的另一个维度（application_status），不跟 new/reviewed/dismissed 互斥
    const appStatus = j.application_status || 'not_applied';
    if (appStatus === 'applied') applied += 1;
    if (appStatus === 'interviewing') interviewing += 1;
    // 「已收藏」卡片只表示"收藏了但还没投"，已经投出去的改由「已投递」「面试中」两张卡各自计数，不重复算进这里
    if (j.status === 'reviewed' && appStatus === 'not_applied') reviewedPending += 1;
  }
  document.getElementById('statNew').textContent = counts.new;
  document.getElementById('statReviewed').textContent = reviewedPending;
  document.getElementById('statApplied').textContent = applied;
  document.getElementById('statInterviewing').textContent = interviewing;
  document.getElementById('statStarred').textContent = starred;
  document.getElementById('statDismissed').textContent = counts.dismissed;
  updateStatCardActive();
  updateLede();
}

// 首页导语行（"在库 N 条职位，已完成 AI 匹配 M 条，K 条越过 70% 投递线"）：
// 用已经拉到手的 allJobs 算三个数字，反映全局、不跟着当前筛选变，不产生额外请求。
function updateLede() {
  const total = allJobs.length;
  const scored = allJobs.filter((j) => j.overall_match != null).length;
  const strong = allJobs.filter((j) => j.overall_match != null && j.overall_match >= 0.7).length;
  const totalEl = document.getElementById('ledeTotal');
  const scoredEl = document.getElementById('ledeScored');
  const strongEl = document.getElementById('ledeStrong');
  if (!totalEl) return; // 页面没有导语区（不太可能，但防一下）
  totalEl.textContent = total;
  scoredEl.textContent = scored;
  strongEl.textContent = strong;
}

// 顶部统计卡片兼任筛选按钮（取代原来独立的一排"新/已收藏/已忽略/全部"chip）：
// 三张审核状态卡按 data-status 跟 currentStatus 比对高亮，currentStatus 为空（"全部"）时都不亮；
// 「重点关注」是独立开关（data-filter="starred"，可跟任何状态叠加），按 starredOnly 高亮。
function updateStatCardActive() {
  document.querySelectorAll('.stat-card.clickable').forEach((card) => {
    if (card.dataset.filter === 'starred') {
      card.classList.toggle('active', starredOnly);
      return;
    }
    // 「已投递」「面试中」按 application_status 筛，跟审核状态（新/收藏/忽略）是两个维度，
    // 分开判断；同时把底部 appStatusChips 那排也同步一下，两处高亮不会各说各话
    if (card.dataset.appstatus) {
      card.classList.toggle('active', card.dataset.appstatus === currentAppStatus && currentAppStatus !== '');
      return;
    }
    card.classList.toggle('active', card.dataset.status === currentStatus && currentStatus !== '');
  });
  document.querySelectorAll('#appStatusChips .chip').forEach((c) => {
    c.classList.toggle('active', (c.dataset.appstatus || '') === currentAppStatus);
  });
}

function toggleStarredFilter() {
  starredOnly = !starredOnly;
  updateStatCardActive();
  renderJobs();
}

function filterByStatus(status) {
  // 再点一次已经选中的卡片，等于取消筛选、回到"全部"——省掉专门放一个"全部"按钮的空间。
  const turningOn = currentStatus !== status;
  currentStatus = currentStatus === status ? '' : status;
  // 待审核/已收藏/已忽略/已投递/面试中这几张卡是同一个单选组，点其中一张要把另一维度
  // 清掉，不再同时选中两张（比如"已收藏"没取消、又叠加"已投递"）。
  // 「已收藏」卡片数字口径是"收藏且还没投递"（见 updateStats() 的 reviewedPending），
  // 选中它时顺带把 currentAppStatus 设成 not_applied，让列表跟卡片数字对齐。
  currentAppStatus = (status === 'reviewed' && turningOn) ? 'not_applied' : '';
  updateStatCardActive();
  renderJobs();
}

// 顶部「已投递」「面试中」卡片跟底部 appStatusChips 是同一个 currentAppStatus，
// 点卡片、点 chip 效果等价，updateStatCardActive() 里两边高亮一起同步
function filterByAppStatus(status) {
  currentAppStatus = currentAppStatus === status ? '' : status;
  // 同上，跟审核状态那几张卡是同一个单选组，选中投递状态时把它们的筛选清掉
  currentStatus = '';
  updateStatCardActive();
  renderJobs();
}

// first_seen 存的是完整 ISO 时间戳（models.insert_job 用 datetime.now().isoformat()），
// 列表里只需要"几号"，原样显示一长串反而糊了真正该看的分数/标题。解析失败（脏数据）
// 原样返回，不隐藏问题。
function shortDate(iso) {
  if (!iso) return '';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[2]}-${m[3]}` : iso;
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
  if (companyOrigin === 'foreign') return `<span class="badge" title="AI分析判断为外企/海外总部公司">${GLOBE_ICON}外企</span>`;
  if (companyOrigin === 'domestic') return `<span class="badge" title="AI分析判断为中国大陆本土公司">${BUILDING_ICON}中国公司</span>`;
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
const SEARCH_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>';
// SPARK_ICON 挪到 common.js 了（题库/简历/职位详情页也要用，不止这一页）
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

// 「我投了」：只在"已收藏但还没记录投递状态"的卡片上出现，一键把 application_status
// 置为 applied（复用 setApplicationStatus，applied_at 时间戳已经在那条链路里记了，
// 这里不需要新接口）。Easy Apply 走完只是浏览器打开、材料填好、停在提交按钮前——
// 不代表已经提交，所以不在那条链路里自动置状态，而是让用户自己提交完之后点这个按钮确认。
function appliedButtonHtml(job) {
  if ((job.application_status || 'not_applied') !== 'not_applied') return '';
  return `<button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); setApplicationStatus(${job.id}, 'applied')">${CHECK_ICON}我投了</button>`;
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
        showToast('浏览器窗口已打开，材料已尽量自动填好，请切换过去手动检查并自己点击提交——提交完成后回来点卡片上的「我投了」记录投递状态', 'success', 10000);
      }
      return;
    }
  }
  showToast('仍在启动中，可能是浏览器/登录检查比较慢，可稍后查看是否已弹出窗口', 'info', 6000);
}

function resumeLinkHtml(job) {
  if (!job.resume_path) return '';
  return `<a class="dot-sep job-link" href="/api/jobs/${job.id}/resume" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">${RESUME_ICON}定制简历</a>`;
}

function coverLetterLinkHtml(job) {
  if (!job.cover_letter) return '';
  return `<a class="dot-sep job-link" href="/jobs/${job.id}">${MAIL_ICON}Cover Letter</a>`;
}

// 只在"已忽略"、且还没补过原因的卡片上出现——这是历史已忽略职位（这次功能上线之前
// 忽略的，包括产品评审提到的那 11 条"高分被忽略"冷启动样本）补录原因的入口，
// 新忽略走 setJobStatus 里已经接好的 openDismissReasonPrompt，不需要这个按钮。
function dismissReasonButtonHtml(job) {
  if (job.status !== 'dismissed' || job.has_dismiss_reason) return '';
  return `<button class="icon-btn" title="记录忽略原因" onclick="event.stopPropagation(); openDismissReasonPrompt(${job.id}, loadJobs)">${NOTE_ICON}</button>`;
}

// 空列表分两种情况，原来共用一句"暂时没有职位"，非常容易误导：默认筛选是
// "待审核 + 外企"，刚跑完搜索抓到的若全是国内公司，页面看起来就像搜索失败了。
function renderEmptyState(el) {
  if (allJobs.length === 0) {
    el.innerHTML = `
      <div class="icon">${INBOX_ICON}</div>
      <div class="title">还没有任何职位</div>
      <div class="desc">点击右上角"智能抓取"跑一次，或在"更多 → 设置"里先填好搜索关键词。</div>`;
    return;
  }
  // 库里有数据，只是被当前筛选组合挡住了——直接说清楚挡了多少条，并给一键清除
  el.innerHTML = `
    <div class="icon">${SEARCH_ICON}</div>
    <div class="title">当前筛选下没有职位</div>
    <div class="desc">库里一共有 ${allJobs.length} 条职位，都被现在的筛选条件挡住了。</div>
    <button class="btn btn-secondary btn-sm" onclick="clearAllFilters()">清除全部筛选</button>`;
}

function renderJobs() {
  const list = document.getElementById('jobList');
  const empty = document.getElementById('jobsEmpty');
  const q = document.getElementById('jobSearch').value.trim().toLowerCase();

  // 所有改筛选的入口最后都会走到这里，所以 URL 同步放这一处就够，不用每个 handler 各写一遍
  writeFiltersToUrl();

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
    renderEmptyState(empty);
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';

  // 分组排序（2026-08-17 视觉改版）：重点关注置顶 + 全部按匹配度从高到低，取代原来
  // "最新抓取排最前"——服务"一眼看到该看什么"这个目标，跟用户确认过采用这个方案
  // （而不是"只置顶重点关注、其余仍按最新排序"的折中方案），见 spec/roadmap.md。
  const hasScore = (j) => j.overall_match != null;
  const byScoreDesc = (a, b) => {
    if (hasScore(a) && hasScore(b)) return b.overall_match - a.overall_match;
    if (hasScore(a)) return -1;
    if (hasScore(b)) return 1;
    return 0; // 都没有分数，保持原来的相对顺序（数组 sort 是稳定排序）
  };

  const scoredJobs = jobs.filter(hasScore);
  const heroJob = scoredJobs.length
    ? scoredJobs.reduce((a, b) => (b.overall_match > a.overall_match ? b : a))
    : null;
  const rest = heroJob ? jobs.filter((j) => j.id !== heroJob.id) : jobs;
  const starredJobs = rest.filter((j) => j.starred).sort(byScoreDesc);
  const otherJobs = rest.filter((j) => !j.starred).sort(byScoreDesc);

  let html = '';
  if (heroJob) html += jobCardHtml(heroJob, { hero: true });
  if (starredJobs.length) {
    html += `<div class="job-group-head">${STAR_ICON_FILLED}重点关注 <span class="n">${starredJobs.length}</span></div>`;
    html += starredJobs.map((j) => jobCardHtml(j)).join('');
  }
  if (otherJobs.length) {
    if (heroJob || starredJobs.length) html += `<div class="job-group-head plain">其余职位</div>`;
    // 只在"其余职位"里做相似分组折叠（2026-08-18）：重点关注是已经逐条决定过要盯的，
    // 折叠起来会违背标星的本意；hero 是全场最高分单条拎出来，同理不折叠。真正需要
    // 折叠的场景是"同公司一次开了一堆相近岗位"，恰好都还没决定，落在这个分组里。
    html += groupJobsForRender(otherJobs)
      .map((entry) => (entry.type === 'group' ? similarGroupHtml(entry.jobs) : jobCardHtml(entry.job)))
      .join('');
  }
  list.innerHTML = html;
}

// 按后端算好的 similar_group_id 把职位聚成 [{type:'single',job} | {type:'group',jobs}]，
// 不改变、不丢失任何一条——组内每条职位展开后仍然是完整的 jobCardHtml，逐条可操作。
function groupJobsForRender(jobs) {
  const seen = new Set();
  const out = [];
  for (const j of jobs) {
    if (!j.similar_group_id) {
      out.push({ type: 'single', job: j });
      continue;
    }
    if (seen.has(j.similar_group_id)) continue;
    seen.add(j.similar_group_id);
    out.push({ type: 'group', jobs: jobs.filter((x) => x.similar_group_id === j.similar_group_id) });
  }
  return out;
}

function similarGroupHtml(jobs) {
  const scored = jobs.filter((j) => j.overall_match != null);
  const best = scored.length ? scored.reduce((a, b) => (b.overall_match > a.overall_match ? b : a)) : jobs[0];
  return `
    <details class="job-similar-group">
      <summary>
        <span class="job-similar-group-label">${escapeHtml(jobs[0].company)} · ${jobs.length} 个相似职位</span>
        ${matchBadge(best)}
      </summary>
      <div class="job-similar-group-body">
        ${jobs.map((j) => jobCardHtml(j)).join('')}
      </div>
    </details>
  `;
}

// 单条职位行的完整 HTML。原来直接写在 renderJobs() 的 .map() 里，抽出来是因为现在
// 同一个模板要给三处复用：hero 大卡、重点关注置顶组、其余职位——避免三份重复模板互相
// 漂移。matchBadge()/siteBadge()/...等生成局部 HTML 的函数一个没改，只是这里把
// matchBadge() 挪到了最前面（左侧分数列），interviewPrepBadgeHtml()/noteBadgeHtml()
// 挪进了标题行（小徽标形式），视觉调整，不影响它们各自的判断逻辑。
function jobCardHtml(j, opts) {
  opts = opts || {};
  const clickable = j.overall_match != null;
  const starred = !!j.starred;
  const cls = ['job-card'];
  if (clickable) cls.push('clickable');
  if (starred) cls.push('star');
  if (opts.hero) cls.push('hero');
  return `
    <div class="${cls.join(' ')}" data-id="${j.id}" ${clickable ? `onclick="location.href='/jobs/${j.id}'"` : ''}>
      ${matchBadge(j)}
      <div class="job-main">
        <div class="job-title-row">
          ${starred ? `<span class="job-title-star" title="重点关注">${STAR_ICON_FILLED}</span>` : ''}
          <a class="job-title" href="${safeUrl(j.job_url)}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">${escapeHtml(j.title)}</a>
          ${siteBadge(j.site)}
          ${originBadge(j.company_origin)}
          ${currentStatus === '' ? statusBadge(j.status) : ''}
          ${interviewPrepBadgeHtml(j)}
          ${noteBadgeHtml(j)}
          ${duplicateOfAppliedBadgeHtml(j)}
        </div>
        <div class="job-meta">
          <span>${escapeHtml(j.company)}</span>
          ${j.location ? `<span class="dot-sep">${escapeHtml(j.location)}</span>` : ''}
          <span class="dot-sep" title="${escapeHtml(j.first_seen)}">${shortDate(j.first_seen)}</span>
          ${j.status === 'dismissed' ? '' : `${resumeLinkHtml(j)}${coverLetterLinkHtml(j)}`}
        </div>
        ${jobTagsRowHtml(j)}
      </div>
      <div class="job-actions">
        ${j.status === 'dismissed' ? '' : `
        ${j.status === 'new' ? '' : applicationStatusSelectHtml(j)}
        ${j.status === 'reviewed' ? '' : analysisStateButtonHtml(j)}
        ${j.status === 'reviewed' ? easyApplyButtonHtml(j) : ''}
        ${j.status === 'reviewed' ? materialsButtonHtml(j) : ''}
        ${j.status === 'reviewed' ? appliedButtonHtml(j) : ''}
        <button class="icon-btn" title="编辑标签" onclick="event.stopPropagation(); editCardTags(${j.id})">${TAG_ICON}</button>
        `}
        ${starButtonHtml(j)}
        ${j.status === 'reviewed' ? '' : `<button class="icon-btn" title="标记已收藏" onclick="event.stopPropagation(); setJobStatus(${j.id}, 'reviewed', '${j.status}')">${CHECK_ICON}</button>`}
        ${j.status === 'dismissed' ? '' : `<button class="icon-btn" title="忽略" onclick="event.stopPropagation(); setJobStatus(${j.id}, 'dismissed', '${j.status}')">${X_ICON}</button>`}
        ${dismissReasonButtonHtml(j)}
      </div>
    </div>
  `;
}

async function analyzeJob(id, btn) {
  setBtnLoading(btn, '分析中…');
  try {
    const res = await fetch(`/api/jobs/${id}/analyze`, { method: 'POST' });
    const data = await res.json();
    if (handleNeedResume(data)) return;
    if (!res.ok) throw new Error(data.error || '未知错误');
    if (data.discarded) {
      // 分析还没跑完这条职位就被标记「忽略」了（见 job_state.discard_job）——结果没有
      // 保存，不是失败，也不是真的分析完成，用中性的提示说清楚，不要弹绿色的"完成"。
      showToast('这条职位在分析过程中被标记为忽略，结果不会保留', 'info', 5000);
    } else {
      const pct = Math.round(data.overall_match * 100);
      showToast(`分析完成：匹配度 ${pct}%`, 'success', 6000);
    }
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
            ? `已重新获取JD正文，AI分析完成：匹配度 ${pct}%`
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
    const res = await fetch('/api/jobs/refetch_jd', { method: 'POST' });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || '未知错误');
    // 提示里让用户点"刷新"，工具栏上就得真有这个按钮（见 index.html 的 #refreshBtn）
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
    const res = await fetch('/api/jobs/classify_origin', { method: 'POST' });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || '未知错误');
    showToast('已开始批量识别公司国籍（后台进行，完成后点"刷新"查看结果）', 'info', 6000);
  } catch (e) {
    showToast(`触发失败：${e.message}`, 'error');
  } finally {
    restoreBtn(btn);
  }
}

// previousStatus 传了就在成功提示里挂一个"撤销"。
// 「忽略」是一次点击就生效、又没有确认弹窗的破坏性操作，点错了原来只能去"已忽略"里翻回来；
// 加确认弹窗会拖慢日常操作（这是每天要点几十次的动作），所以走"先执行、给撤销"这条路。
async function setJobStatus(id, status, previousStatus = null) {
  try {
    const res = await fetch(`/api/jobs/${id}/status`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }),
    });
    // 原来这里不看 res.ok，后端 500 也照样弹绿色的"已标记为「已收藏」"
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || '未知错误');
    const label = STATUS_LABELS[status] ? STATUS_LABELS[status][0] : status;
    const undoable = previousStatus && previousStatus !== status;
    showToast(
      `已标记为「${label}」`, 'success', undoable ? 6000 : 2000,
      undoable ? { label: '撤销', onClick: () => setJobStatus(id, previousStatus) } : null,
    );
    // 忽略已经生效之后再问原因（可选、可跳过）——不能反过来，问原因不该拖慢"忽略"
    // 这个刻意做成零摩擦的高频动作（见上面的注释）。
    if (status === 'dismissed') openDismissReasonPrompt(id, loadJobs);
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
    if (!res.ok) throw new Error(data.error || '未知错误');
    showToast(`投递状态已更新为「${APPLICATION_STATUS_LABELS[applicationStatus] || applicationStatus}」`, 'success', 2000);
    if (data.interview_prep_started) {
      // 后端已经起了后台线程在生成，这里只负责告知 + 安排轮询，等生成完卡片上会出现「面试准备」标记
      showToast('已开始生成这条职位的面试准备材料，完成后卡片上会出现「面试准备」标记', 'info', 6000);
      pollInterviewPrepUntilDone(id);
    }
  } catch (e) {
    showToast(`操作失败：${e.message}`, 'error');
  } finally {
    loadJobs();
  }
}

// ---------- 每日任务清单 ----------
// 待审核/待生成材料/待投递三项直接用现成的 allJobs 现算（renderChecklist 每次 loadJobs()
// 之后都会重跑一遍，天然保持新鲜）；后端算不出来的部分（超7天没跟进的投递、用户自建的
// 待办、简历有没有体检过）拉一次 /api/checklist 缓存下来，不用每次都发请求。
let checklistExtra = { followups: [], custom_items: [], resume_review_done: true, resume_review_ready: false, resume_review_id: null };

async function loadChecklist() {
  try {
    const res = await fetch('/api/checklist');
    if (res.ok) checklistExtra = await res.json();
  } catch (e) {
    // 拉不到就用上一次的缓存值，不弹错误 toast——这块是锦上添花的提醒，不该给日常操作添堵。
  }
  renderChecklist();
}

// 自动生成的几项勾掉只是"今天已经看过、先别提醒了"，真实来源永远是当前数据库状态，
// 所以不落库，存 localStorage、按日期分 key，换一天自动失效，不需要写清理逻辑。
function checklistDismissedToday() {
  const key = `checklist_dismissed_${new Date().toISOString().slice(0, 10)}`;
  return { key, set: new Set(JSON.parse(localStorage.getItem(key) || '[]')) };
}

function dismissChecklistItemToday(itemKey) {
  const { key, set } = checklistDismissedToday();
  set.add(itemKey);
  localStorage.setItem(key, JSON.stringify([...set]));
  renderChecklist();
}

// "体检已给出建议，去优化简历"这条不跟着日期重置（见 renderChecklist 里的说明），
// 记的是"哪一次体检已经被处理/忽略过"，只要 resume_review_id 没变就一直生效。
function ignoreResumeReviewReady(reviewId) {
  localStorage.setItem('resume_review_ready_ignored_id', String(reviewId));
  renderChecklist();
}

// 跳转前先清空其它筛选——不然筛选栏里残留的"外企"之类条件会把清单指向的那批职位
// 挡住，用户点了条目却在列表里什么都看不到。
function focusChecklistStatus(status, appStatus) {
  clearAllFilters();
  currentStatus = status;
  if (appStatus) currentAppStatus = appStatus;
  syncFilterControls();
  renderJobs();
  document.getElementById('panel-jobs').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function checklistRowHtml(key, text, onClickExpr) {
  // event.preventDefault() 是关键：<span> 套在关联着 checkbox 的 <label> 里，点它文字部分
  // 浏览器会顺带把这个 label 的默认动作也执行一遍——也就是连带勾选那个 checkbox，等价于
  // 用户自己顺手点掉了这条待办。不 preventDefault 的话，点文字跳转的同时这条就没了，
  // 用户看起来像是"点一下就被自动勾掉"，而不是真的点了勾选框。
  return `
    <label class="checklist-row">
      <input type="checkbox" onchange="dismissChecklistItemToday('${key}')">
      <span class="checklist-text" onclick="event.preventDefault(); ${onClickExpr}">${escapeHtml(text)}</span>
    </label>`;
}

function renderChecklist() {
  const el = document.getElementById('checklistItems');
  if (!el) return;
  const { set: dismissedToday } = checklistDismissedToday();
  const rows = [];

  const pendingReview = allJobs.filter((j) => j.status === 'new').length;
  if (pendingReview && !dismissedToday.has('review')) {
    rows.push(checklistRowHtml('review', `${pendingReview} 条职位待审核`, `focusChecklistStatus('new')`));
  }
  const pendingApply = allJobs.filter((j) => j.status === 'reviewed' && (j.application_status || 'not_applied') === 'not_applied').length;
  if (pendingApply && !dismissedToday.has('apply')) {
    rows.push(checklistRowHtml('apply', `${pendingApply} 条已收藏、还没投递`, `focusChecklistStatus('reviewed', 'not_applied')`));
  }
  (checklistExtra.followups || []).forEach((f) => {
    const key = `followup-${f.job_id}`;
    if (dismissedToday.has(key)) return;
    rows.push(checklistRowHtml(key, `《${f.title}》@ ${f.company} 投递超过7天了，该跟进一下`, `window.open('/jobs/${f.job_id}', '_blank')`));
  });
  if (!checklistExtra.resume_review_done && !dismissedToday.has('resume_review')) {
    rows.push(checklistRowHtml('resume_review', '还没做过简历体检，AI 能帮你挑出结构/成果/关键词/表达上的问题', `window.open('/resume', '_blank')`));
  }
  // 体检改成后台跑之后（见 static/resume.js），人可能已经离开「我的简历」页了，光靠那一页
  // 的完成 toast 会错过。这条刻意不用上面那套"按天重置"的 dismissedToday——用户明确要求
  // 它要一直留到真的处理完（生成过优化版，见 app.py get_checklist 的 resume_review_ready
  // 判断）或者自己主动点掉，不该因为跨了一天就凭空消失，也不该因为点文字跳转就顺手被
  // 点掉（checklistRowHtml 已经堵住"点文字连带勾选框"这个问题，这里额外用按 review id
  // 持久化的 localStorage 键，标记的是"这一次体检的提醒"而不是"今天的提醒"，跟着体检
  // 这个具体对象走，而不是跟着日期走。跟上面那条"还没体检过"互斥：这条出现时上面那条
  // 一定是 false（resume_review_done 已经是 true 了），不会同时显示两条。
  if (checklistExtra.resume_review_ready
    && String(checklistExtra.resume_review_id) !== localStorage.getItem('resume_review_ready_ignored_id')) {
    rows.push(`
      <label class="checklist-row">
        <input type="checkbox" onchange="ignoreResumeReviewReady(${checklistExtra.resume_review_id})">
        <span class="checklist-text" onclick="event.preventDefault(); window.open('/resume', '_blank')">体检已给出建议，去优化简历</span>
      </label>`);
  }
  (checklistExtra.custom_items || []).forEach((item) => {
    rows.push(`
      <label class="checklist-row">
        <input type="checkbox" onchange="deleteChecklistItem(${item.id})">
        <span class="checklist-text">${escapeHtml(item.content)}</span>
      </label>`);
  });

  el.innerHTML = rows.length ? rows.join('') : '<div class="checklist-empty">今天没有待办，干得不错。</div>';

  const countEl = document.getElementById('checklistCount');
  if (countEl) {
    countEl.textContent = rows.length;
    countEl.style.display = rows.length ? '' : 'none';
  }
}

// 折叠状态不持久化：每次刷新都想让用户先看一眼今天有什么事，看完自己收起就行，
// 不用记住上次开关状态（见 index.html 里 checklist-card 默认带的 open class）。
function toggleChecklist() {
  document.getElementById('checklistCard').classList.toggle('open');
}

async function addChecklistItem() {
  const input = document.getElementById('checklistInput');
  const content = input.value.trim();
  if (!content) return;
  try {
    const res = await fetch('/api/checklist', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '未知错误');
    input.value = '';
    await loadChecklist();
  } catch (e) {
    showToast(`添加失败：${e.message}`, 'error');
  }
}

async function deleteChecklistItem(id) {
  try {
    const res = await fetch(`/api/checklist/${id}`, { method: 'DELETE' });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || '未知错误');
  } catch (e) {
    showToast(`操作失败：${e.message}`, 'error');
  } finally {
    await loadChecklist();
  }
}

// ---------- runs ----------
async function loadRuns() {
  const tbody = document.querySelector('#runsTable tbody');
  let runs;
  try {
    const res = await fetch('/api/runs');
    if (!res.ok) throw new Error('未知错误');
    runs = await res.json();
  } catch (e) {
    // 原来这里没有任何错误处理：接口挂了就静默抛出，表格空着、统计卡片停在占位符"–"，
    // 看起来跟"还没跑过"一模一样
    tbody.innerHTML = `<tr><td colspan="7" class="run-error">运行记录加载失败：${escapeHtml(e.message)}</td></tr>`;
    renderFunnel([]);
    return;
  }

  renderFunnel(runs);

  if (!runs.length) {
    // 空 tbody 只会剩一行光秃秃的表头，不写点什么用户不知道是没跑过还是没加载出来
    tbody.innerHTML = '<tr><td colspan="7" class="run-ok">还没有运行记录，点右上角"智能抓取"跑一次。</td></tr>';
    return;
  }

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
}

// 首页「今日抓取」漏斗：把 ran_at 是今天的几条运行记录（可能一天跑好几次）加总，
// 展示 今日抓取 → 不相关跳过 → 重复跳过 → 新增。list_runs() 默认只返回最近 20 条，
// 正常使用（每天定时跑一次 + 偶尔手动点几次）足够覆盖当天。
function renderFunnel(runs) {
  const el = document.getElementById('funnelRow');
  if (!el) return;
  const todayStr = new Date().toISOString().slice(0, 10);
  const todayRuns = runs.filter((r) => (r.ran_at || '').startsWith(todayStr) && !r.error);
  if (!todayRuns.length) {
    el.style.display = 'none';
    return;
  }
  const sum = (key) => todayRuns.reduce((acc, r) => acc + (r[key] || 0), 0);
  const found = sum('found');
  const irrelevant = sum('skipped_irrelevant');
  const duplicate = sum('skipped_duplicate');
  const added = sum('added');
  el.innerHTML = `<b>今日抓取</b> <span>${found} 条</span><i>→</i>` +
    `<span>不相关 ${irrelevant}</span><i>→</i>` +
    `<span>重复 ${duplicate}</span><i>→</i>` +
    `<b>新增 ${added}</b>`;
  el.style.display = 'flex';
}

// reqListHtml 搬进了 common.js（跟职位详情页共用）。

// 把投递状态改成"面试中"会触发后台生成面试准备材料。这里跟进到跑完为止，好让卡片上的
// 「面试准备」标记自己冒出来，不用用户手动刷新。真正看内容是在 /jobs/<id>/interview 页面上，
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

// 职位卡片上的面试准备标记：有面试准备材料就点进那条职位的面试准备页。
function interviewPrepBadgeHtml(job) {
  if (job.interview_prep_state === 'generating') {
    return `<span class="match-pill prep-pill" title="正在生成面试准备材料">${MIC_ICON}准备中…</span>`;
  }
  if (!job.has_interview_prep) return '';
  return `<a class="match-pill prep-pill done" href="/jobs/${job.id}/interview"
    title="已有面试准备材料，点击查看" onclick="event.stopPropagation()">${MIC_ICON}面试准备</a>`;
}

// 职位卡片上的备注数角标：有备注就点进详情页（备注在那边看/加/删）。
function noteBadgeHtml(job) {
  if (!job.note_count) return '';
  return `<a class="match-pill prep-pill done" href="/jobs/${job.id}"
    title="有 ${job.note_count} 条备注" onclick="event.stopPropagation()">${NOTE_ICON}${job.note_count}</a>`;
}

// 「疑似重复」提示角标：annotate_similar_groups() 发现同公司下有一条近似标题的职位
// 已经投递/面试中（很可能是同一个岗位被换了标题重新挂出来），哪怕这条自己标了星也提醒
// ——标星故意不参与首页的折叠展示（避免削弱"我特意标星"这个动作），但这个提示不折叠。
function duplicateOfAppliedBadgeHtml(job) {
  const dup = job.duplicate_of_applied;
  if (!dup) return '';
  const label = dup.application_status === 'interviewing' ? '面试中' : '已投递';
  return `<a class="match-pill dup-pill" href="/jobs/${dup.id}"
    title="疑似同一岗位换标题重新挂出：${escapeHtml(dup.title)}（${label}）" onclick="event.stopPropagation()">⚠ 疑似重复 · 近似岗位${label}</a>`;
}

// 职位卡片上的标签行：展示已有标签 + 一个编辑入口。「分析详情」弹窗拆成独立页面
// （/jobs/<id>）之后，这是列表页唯一直接操作标签的地方。
function jobTagsRowHtml(job) {
  const tags = (job.tags || '').split(',').map((t) => t.trim()).filter(Boolean);
  if (!tags.length) return '';
  return `<div class="job-tags-row" onclick="event.stopPropagation()">
    ${tags.map((t) => `<span class="badge tag-badge">${escapeHtml(t)}</span>`).join('')}
  </div>`;
}

function editCardTags(jobId) {
  const job = allJobs.find((j) => j.id === jobId);
  if (!job) return;
  const tags = (job.tags || '').split(',').map((t) => t.trim()).filter(Boolean);
  openTagEditor(jobId, tags, collectAllTags(), (savedTags) => {
    job.tags = savedTags.join(',');
    renderTagChips();
    renderJobs();
  });
}

// ---------- 定制简历 / Cover Letter 材料生成 ----------
function materialsButtonHtml(job) {
  if (job.overall_match == null) return '';
  if (job.materials_state === 'queued') {
    return `<button class="btn btn-secondary btn-sm" disabled>材料排队中…</button>`;
  }
  if (job.materials_state === 'generating') {
    return `<button class="btn btn-secondary btn-sm" disabled><span class="spinner"></span>生成材料中…</button>`;
  }
  if (job.resume_path || job.cover_letter) return '';
  return `<button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); generateMaterialsFromCard(${job.id}, this)">${SPARK_ICON}生成材料</button>`;
}

async function generateMaterialsFromCard(id, btn) {
  setBtnLoading(btn, '启动中…');
  try {
    const res = await fetch(`/api/jobs/${id}/generate_materials`, { method: 'POST' });
    const data = await res.json();
    if (handleNeedResume(data)) return;
    if (!res.ok) throw new Error(data.error || '未知错误');
    showToast('已开始生成定制简历 + Cover Letter，完成后卡片会自动更新', 'info', 5000);
  } catch (e) {
    showToast(`启动失败：${e.message}`, 'error', 6000);
  } finally {
    await loadJobs();
  }
}

// 已分析出匹配度、但还没生成过定制简历/Cover Letter 的职位（排除已经在排队/生成中的，
// 避免重复触发）。抽出来是因为"每日任务清单"的"待生成材料"计数要用同一套判断，
// 不能让两处标准各写一份、慢慢飘出不一致。
function jobsNeedingMaterials(jobs) {
  return jobs.filter((j) => j.overall_match != null && !j.resume_path && !j.cover_letter
    && j.materials_state !== 'queued' && j.materials_state !== 'generating');
}

async function batchGenerateMaterials(btn) {
  const jobs = jobsNeedingMaterials(allJobs);
  // 对"当前筛选出来的职位"生效，跟眼睛看到的一致；已经生成过/正在生成的不重复算进去
  const q = document.getElementById('jobSearch').value.trim().toLowerCase();
  let candidates = jobs;
  if (currentStatus) candidates = candidates.filter((j) => j.status === currentStatus);
  if (currentOrigin === 'foreign') candidates = candidates.filter((j) => j.company_origin !== 'domestic');
  else if (currentOrigin === 'domestic') candidates = candidates.filter((j) => j.company_origin === 'domestic');
  if (currentAppStatus) candidates = candidates.filter((j) => (j.application_status || 'not_applied') === currentAppStatus);
  if (starredOnly) candidates = candidates.filter((j) => !!j.starred);
  if (currentTag) candidates = candidates.filter((j) => (j.tags || '').split(',').map((t) => t.trim()).includes(currentTag));
  if (q) candidates = candidates.filter((j) => (j.title || '').toLowerCase().includes(q) || (j.company || '').toLowerCase().includes(q));

  if (!candidates.length) {
    showToast('当前筛选下没有可以生成材料的职位（都已分析过并生成过，或还没做AI分析）', 'info', 5000);
    return;
  }
  if (!window.confirm(`将为当前筛选下的 ${candidates.length} 条职位批量生成定制简历 + Cover Letter，每条都会产生一次 API 调用费用，确定继续吗？`)) return;

  setBtnLoading(btn, '启动中…');
  try {
    const res = await fetch('/api/jobs/generate_materials', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_ids: candidates.map((j) => j.id) }),
    });
    const data = await res.json();
    if (handleNeedResume(data)) return;
    if (!res.ok) throw new Error(data.error || '未知错误');
    showToast(`已开始批量生成，共 ${data.count} 条待处理`, 'info', 5000);
  } catch (e) {
    showToast(`启动失败：${e.message}`, 'error');
  } finally {
    restoreBtn(btn);
    await loadJobs();
  }
}

// ---------- init ----------
// 顺序有讲究：先把 URL 里的筛选读回内存、同步到控件高亮，再拉数据，
// 否则 loadJobs() 里的首次 renderJobs() 用的还是默认筛选。
initTheme();
readFiltersFromUrl();
syncFilterControls();
loadConfig();
loadJobs(true);
loadRuns();
loadChecklist();
