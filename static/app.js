async function loadConfig() {
  const cfg = await (await fetch('/api/config')).json();
  document.getElementById('keywords').value = cfg.keywords.join('\n');
  document.getElementById('locations').value = cfg.locations.join('\n');
  document.getElementById('country_indeed').value = cfg.country_indeed;
  document.getElementById('results_wanted').value = cfg.results_wanted;
  document.getElementById('schedule_hour').value = cfg.schedule_hour;
  document.getElementById('schedule_minute').value = cfg.schedule_minute;
  document.getElementById('tracker_xlsx_path').value = cfg.tracker_xlsx_path;
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
    tr.innerHTML = `
      <td><a href="${j.job_url}" target="_blank">${j.title}</a></td>
      <td>${j.company}</td>
      <td>${j.location || ''}</td>
      <td>${j.site || ''}</td>
      <td>${j.first_seen}</td>
      <td>
        <button onclick="setJobStatus(${j.id}, 'reviewed')">已看过</button>
        <button onclick="setJobStatus(${j.id}, 'dismissed')">忽略</button>
      </td>`;
    tbody.appendChild(tr);
  }
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
