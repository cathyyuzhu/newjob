async function loadConfig() {
  const cfg = await (await fetch('/api/config')).json();
  document.getElementById('keywords').value = cfg.keywords.join('\n');
  document.getElementById('locations').value = cfg.locations.join('\n');
  document.getElementById('country_indeed').value = cfg.country_indeed;
  document.getElementById('results_wanted').value = cfg.results_wanted;
  document.getElementById('schedule_hour').value = cfg.schedule_hour;
  document.getElementById('schedule_minute').value = cfg.schedule_minute;
  document.getElementById('tracker_xlsx_path').value = cfg.tracker_xlsx_path;
  document.getElementById('base_resume_path').value = cfg.base_resume_path;
  document.getElementById('resume_output_dir').value = cfg.resume_output_dir;
}

async function saveConfig() {
  const body = {
    keywords: document.getElementById('keywords').value.split('\n'),
    locations: document.getElementById('locations').value.split('\n'),
    country_indeed: document.getElementById('country_indeed').value,
    results_wanted: document.getElementById('results_wanted').value,
    schedule_hour: document.getElementById('schedule_hour').value,
    schedule_minute: document.getElementById('schedule_minute').value,
    tracker_xlsx_path: document.getElementById('tracker_xlsx_path').value,
    base_resume_path: document.getElementById('base_resume_path').value,
    resume_output_dir: document.getElementById('resume_output_dir').value,
  };
  await fetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
  setStatus('设置已保存');
}

async function runNow() {
  setStatus('搜索中...');
  const res = await (await fetch('/api/search/run', { method: 'POST' })).json();
  setStatus(`完成：找到 ${res.found}，新增 ${res.added}，去重跳过 ${res.skipped_duplicate}${res.errors.length ? '，错误：' + res.errors.join('; ') : ''}`);
  loadJobs();
  loadRuns();
}

function setStatus(msg) {
  document.getElementById('status').textContent = msg;
}

async function loadJobs() {
  const status = document.getElementById('statusFilter').value;
  const url = status ? `/api/jobs?status=${status}` : '/api/jobs';
  const jobs = await (await fetch(url)).json();
  const tbody = document.querySelector('#jobsTable tbody');
  tbody.innerHTML = '';
  for (const j of jobs) {
    const tr = document.createElement('tr');
    const matchCell = j.overall_match != null
      ? `${Math.round(j.overall_match * 100)}%`
      : (j.analysis_error ? `<span title="${j.analysis_error}">失败</span>` : '—');
    tr.innerHTML = `
      <td><a href="${j.job_url}" target="_blank">${j.title}</a></td>
      <td>${j.company}</td>
      <td>${j.location || ''}</td>
      <td>${j.site || ''}</td>
      <td>${j.first_seen}</td>
      <td>${matchCell}</td>
      <td>
        <button onclick="analyzeJob(${j.id})">自动分析</button>
        <button onclick="setJobStatus(${j.id}, 'reviewed')">已看过</button>
        <button onclick="setJobStatus(${j.id}, 'dismissed')">忽略</button>
      </td>`;
    tbody.appendChild(tr);
  }
}

async function analyzeJob(id) {
  setStatus('分析中，可能需要几十秒...');
  const res = await fetch(`/api/jobs/${id}/analyze`, { method: 'POST' });
  const data = await res.json();
  if (!res.ok) {
    setStatus(`分析失败：${data.error}`);
  } else {
    setStatus(`分析完成：匹配度 ${Math.round(data.overall_match * 100)}%${data.resume_path ? '，已生成定制简历: ' + data.resume_path : ''}`);
  }
  loadJobs();
}

async function setJobStatus(id, status) {
  await fetch(`/api/jobs/${id}/status`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ status }) });
  loadJobs();
}

async function loadRuns() {
  const runs = await (await fetch('/api/runs')).json();
  const tbody = document.querySelector('#runsTable tbody');
  tbody.innerHTML = '';
  for (const r of runs) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${r.ran_at}</td><td>${r.keywords}</td><td>${r.found}</td><td>${r.added}</td><td>${r.skipped_duplicate}</td><td>${r.error || ''}</td>`;
    tbody.appendChild(tr);
  }
}

loadConfig();
loadJobs();
loadRuns();
