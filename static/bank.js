// 通用面试题库页（/interview）：跨职位复用的个人标准答案，以及跟 AI 对话打磨答案。
// 跟 common.js 一起加载，不依赖 app.js（它绑死了职位列表的 DOM）。
//
// 这一页原来是主页上的弹窗。搬成独立页面是因为它是"坐下来改很久"的场景：弹窗宽 860px、
// 高 92vh 且自己内滚，长答案在里面根本铺不开，也没法单独开一个标签页挂着背题。
// 理由详见 spec/tech-solution.md。

let bankItems = [];
let bankGenerating = false;
// 上一次起草的失败原因（来自 GET /api/interview/bank 的 error 字段）。起草是后台跑的，
// 没有这个字段的话前端只能看到 generating 翻成 false，会把失败当成成功。
let bankError = null;
let bankPollTimer = null;
// 上一次渲染时题库数据长什么样。轮询每 5 秒就要 loadBank() 一次，数据没变还照样重画的话，
// 用户正在打字的输入框会被整块换掉。见 renderBank()。
let lastBankSignature = null;

// 每道题的对话状态：{ [itemId]: { open, lang, draft, sending, messages: [...] } }
// **只活在内存里**：刷新页面就没了。答案本身已经在库里，对话只是产生答案的过程，
// 为它建一张表还要配套清理逻辑，不值当（见 spec/tech-solution.md）。
let bankChats = {};
let assistantChat = { open: false, draft: '', sending: false, messages: [] };

// 哪些题是展开的：{ [itemId]: true }。默认全部收起——题库有几十条，全展开的时候
// 一屏只放得下一题半，找题只能靠滚轮。跟 bankChats 一样只活在内存里。
let bankExpanded = {};
// 哪个区块正开着"新增一题"的输入框（同时只开一个）
let bankAddingSection = null;

// 顺序要跟后端的 interview.BANK_SECTIONS / models.BANK_CATEGORIES 对上：
// 先讲自己 → 讲故事 → 逐段过往工作 → 最后才是通用套题。
// addable=false 的区块不给"+"号：自我介绍固定只有一条。
const BANK_SECTIONS = [
  { key: 'self_intro', title: '自我介绍', hint: '60-90 秒口播稿，中英文各一版。', addable: false },
  { key: 'star_story', title: 'STAR 故事库', hint: '能反复复用的几个核心故事，按 情境-任务-行动-结果 分四段写，中英文各一版。', addable: true },
  { key: 'work_history', title: '讲述过往工作', hint: '简历上每一段工作经历逐个展开：负责什么、最有代表性的成果、最大的挑战、为什么离开。', addable: true },
  { key: 'common', title: '通用问题', hint: '离职原因、职业规划、优缺点这类每场面试都会遇到的题，每题中英文各一版。', addable: true },
];

const LANG_LABELS = { zh: '中文', en: 'English' };

// ---------- 加载与渲染 ----------

async function loadBank() {
  const body = document.getElementById('bankRoot');
  if (!bankItems.length) body.innerHTML = '<div class="plain-text" style="color:var(--text-faint);">加载中…</div>';
  try {
    const data = await (await fetch('/api/interview/bank')).json();
    bankItems = data.items || [];
    bankGenerating = !!data.generating;
    bankError = data.error || null;
  } catch (e) {
    body.innerHTML = `<div class="plain-text" style="color:var(--danger);">加载失败：${escapeHtml(e.message)}</div>`;
    return;
  }
  renderBank();
  if (bankGenerating) startBankPoll();
}

// 题库数据的指纹。只有它变了才值得重画——起草期间三段各自入库时会变三次，
// 其余每 5 秒一次的轮询都会在这里被挡掉，用户正在编辑的输入框不会被抹掉。
function bankSignature() {
  return JSON.stringify([
    bankGenerating,
    bankError,
    bankAddingSection,
    bankItems.map((i) => [i.id, i.category, i.question, i.answer, i.answer_en, i.user_edited, i.updated_at]),
  ]);
}

function renderBank(force) {
  const signature = bankSignature();
  if (!force && signature === lastBankSignature) return;
  lastBankSignature = signature;

  const body = document.getElementById('bankRoot');
  // 兜底：万一真要重画时用户有改了没保存的内容，先记下来，重画完填回去。
  // （正常情况下上面的指纹检查已经把绝大多数重画挡掉了，这里防的是起草完成那一下。）
  const unsaved = {};
  body.querySelectorAll('.bank-answer').forEach((el) => {
    const item = bankItems.find((i) => i.id === Number(el.dataset.itemId));
    if (!item) return;
    const stored = (el.dataset.lang === 'en' ? item.answer_en : item.answer) || '';
    if (el.value !== stored) unsaved[el.id] = el.value;
  });

  const empty = !bankItems.length && !bankGenerating;
  body.innerHTML = `
    ${bankGenerating ? '<div class="plain-text" style="color:var(--text-faint); margin-bottom:0.75rem;">正在根据你的简历起草，分四段生成（自我介绍 → STAR 故事库 → 讲述过往工作 → 通用问题），每段完成会立刻出现在下面，全部跑完可能需要 5-10 分钟…</div>' : ''}
    ${bankError && !bankGenerating ? `
      <div class="detail-section">
        <h4 style="color:var(--danger);">上次起草失败</h4>
        <div class="plain-text" style="color:var(--danger);">${escapeHtml(bankError)}</div>
        <div class="plain-text" style="color:var(--text-faint); font-size:0.8rem; margin-top:0.35rem;">已经改过的条目不受影响，可以直接再点一次「AI 起草 / 补充」重试。</div>
      </div>` : ''}
    ${empty
      ? '<div class="plain-text" style="color:var(--text-faint);">题库还是空的。点右上角「AI 起草 / 补充」按你的简历生成一份初稿——它只填你没改过的条目，你改过并保存的一律跳过，所以之后再点一次是「补充新题」而不是重来一遍。生成完逐条改成你自己的说法；不想手打的话，每题里都能点「跟 AI 聊聊这题」，说清哪里不满意让它改。</div>'
      : `<div class="bank-toolbar">
           <button class="btn btn-secondary btn-sm" onclick="setAllExpanded(true)">全部展开</button>
           <button class="btn btn-secondary btn-sm" onclick="setAllExpanded(false)">全部收起</button>
           <span class="bank-chat-hint">点标题展开某一题；左边目录可以直接跳到具体某题。</span>
         </div>
         ${BANK_SECTIONS.map((s) => bankSectionHtml(s)).join('')}`}
  `;

  Object.entries(unsaved).forEach(([id, value]) => {
    const el = document.getElementById(id);
    if (el) el.value = value;
  });
  body.querySelectorAll('.bank-item:not(.collapsed) .bank-answer').forEach(autoGrow);
  renderBankNav();

  if (bankAddingSection) {
    const input = document.getElementById(`bankAddInput-${bankAddingSection}`);
    if (input) input.focus();
  }
}

// ---------- 目录 ----------

function renderBankNav() {
  const nav = document.getElementById('bankNav');
  if (!nav) return;
  if (!bankItems.length) {
    nav.innerHTML = '';
    return;
  }
  nav.innerHTML = `
    <div class="bank-nav-title">目录</div>
    ${BANK_SECTIONS.map((section) => {
      const items = bankItems.filter((i) => i.category === section.key);
      if (!items.length) return '';
      return `
        <div class="bank-nav-group">
          <a class="bank-nav-section" href="#bankSection-${section.key}"
            onclick="jumpToSection('${section.key}'); return false;">
            ${escapeHtml(section.title)}<span class="bank-count">${items.length}</span>
          </a>
          ${items.map((i) => `
            <a class="bank-nav-item" href="#bankItem-${i.id}" title="${escapeHtml(i.question)}"
              onclick="jumpToItem(${i.id}); return false;">${escapeHtml(i.question)}</a>
          `).join('')}
        </div>`;
    }).join('')}
  `;
}

function jumpToSection(key) {
  const el = document.getElementById(`bankSection-${key}`);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// 从目录点进来的题一定要顺手展开——否则滚过去只看到一行标题，还得再点一下才看得到答案。
function jumpToItem(itemId) {
  setItemExpanded(itemId, true);
  const el = document.getElementById(`bankItem-${itemId}`);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ---------- 展开 / 收起 ----------
//
// 收起用的是 CSS 隐藏（.collapsed 把 .bank-item-body display:none），**不是**把 DOM 删掉：
// 删掉的话，展开着改了一半没保存的答案、正开着的对话，一收起就全没了。
// 也没有改回 details 元素——见 style.css 里那条注释，那样保存一次会把所有题一起收回去。

function setItemExpanded(itemId, expanded) {
  if (expanded) bankExpanded[itemId] = true;
  else delete bankExpanded[itemId];
  const card = document.getElementById(`bankItem-${itemId}`);
  if (!card) return;
  card.classList.toggle('collapsed', !expanded);
  // 收起时 textarea 在 display:none 里，scrollHeight 是 0，autoGrow 会把高度算成 0，
  // 所以展开之后必须重新量一次。
  if (expanded) card.querySelectorAll('.bank-answer').forEach(autoGrow);
}

function toggleItemExpand(itemId) {
  setItemExpanded(itemId, !bankExpanded[itemId]);
}

function setAllExpanded(expanded) {
  bankItems.forEach((i) => setItemExpanded(i.id, expanded));
}

function bankSectionHtml(section) {
  const items = bankItems.filter((i) => i.category === section.key);
  return `
    <div class="detail-section bank-section" id="bankSection-${section.key}">
      <h4 class="bank-section-head">
        <span>${escapeHtml(section.title)}<span class="bank-count">${items.length}</span></span>
        ${section.addable ? `
          <button class="icon-btn bank-add-btn" title="手动加一题"
            onclick="startAddItem('${section.key}')">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
          </button>` : ''}
      </h4>
      <div class="plain-text" style="color:var(--text-faint); font-size:0.78rem; margin-bottom:0.5rem;">${escapeHtml(section.hint)}</div>
      ${items.map((i) => bankItemHtml(i)).join('') || '<div class="plain-text" style="color:var(--text-faint); font-size:0.82rem;">（暂无）</div>'}
      ${bankAddingSection === section.key ? addFormHtml(section.key) : ''}
    </div>
  `;
}

function bankItemHtml(item) {
  const chat = bankChats[item.id];
  const collapsed = !bankExpanded[item.id];
  return `
    <div class="bank-item${collapsed ? ' collapsed' : ''}" id="bankItem-${item.id}" data-id="${item.id}">
      <div class="bank-item-head" onclick="toggleItemExpand(${item.id})" title="点一下展开/收起这一题">
        <span class="bank-caret">▸</span>
        <span class="bank-q">${escapeHtml(item.question)}</span>
        ${item.user_edited ? '<span class="bank-badge">已自定义</span>' : '<span class="bank-badge ai">AI 初稿</span>'}
      </div>
      <div class="bank-item-body">
        <label class="bank-label" for="bankAnswer-${item.id}">中文</label>
        <textarea class="bank-answer" id="bankAnswer-${item.id}" data-item-id="${item.id}" data-lang="zh"
          placeholder="用你自己的说法写下答案…">${escapeHtml(item.answer || '')}</textarea>
        <label class="bank-label" for="bankAnswerEn-${item.id}">English</label>
        <textarea class="bank-answer" id="bankAnswerEn-${item.id}" data-item-id="${item.id}" data-lang="en"
          placeholder="English version…">${escapeHtml(item.answer_en || '')}</textarea>
        <div class="bank-actions">
          <span class="bank-updated plain-text" style="color:var(--text-faint); font-size:0.74rem;">更新于 ${escapeHtml((item.updated_at || '').replace('T', ' '))}</span>
          <span style="flex:1"></span>
          <button class="btn btn-secondary btn-sm" onclick="toggleItemChat(${item.id})">
            ${chat && chat.open ? '收起对话' : CHAT_ICON + '跟 AI 聊聊这题'}
          </button>
          <button class="btn btn-secondary btn-sm" onclick="saveBankItem(${item.id}, this)">保存</button>
          <button class="btn btn-danger btn-sm"
            title="把这道题和你写的中英文答案一起从题库里永久删掉，不可恢复"
            onclick="removeBankItem(${item.id}, this)">删除</button>
        </div>
        <div class="bank-chat" id="bankChat-${item.id}" ${chat && chat.open ? '' : 'hidden'}>${chat && chat.open ? itemChatHtml(item.id) : ''}</div>
      </div>
    </div>
  `;
}

// textarea 高度跟着内容走，框里不出现滚动条（页面只滚外层）。
// 长答案挤在固定 6 行的小框里滚动是这个页面之前最难用的地方。
function autoGrow(el) {
  el.style.height = 'auto';
  el.style.height = `${el.scrollHeight}px`;
}

// ---------- 增删改 ----------

async function generateBankDraft(btn) {
  setBtnLoading(btn, '启动中…');
  try {
    const res = await fetch('/api/interview/bank/generate', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '未知错误');
    showToast('已开始根据简历起草题库，分三段生成，每段完成会立刻出现', 'info', 6000);
    bankGenerating = true;
    bankError = null; // 上一轮的报错立刻收掉，别让它跟这一轮的"起草中"同时挂在界面上
    renderBank();
    startBankPoll();
  } catch (e) {
    showToast(`起草失败：${e.message}`, 'error', 6000);
    restoreBtn(btn);
  }
}

async function saveBankItem(itemId, btn) {
  const payload = {
    answer: document.getElementById(`bankAnswer-${itemId}`).value,
    answer_en: document.getElementById(`bankAnswerEn-${itemId}`).value,
  };
  setBtnLoading(btn, '保存中…');
  try {
    const res = await fetch(`/api/interview/bank/${itemId}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error((await res.json()).error || '未知错误');
    showToast('已保存，这条以后不会被 AI 起草覆盖', 'success', 3000);
    // 只改这一条的 DOM，不重画整页——重画会把别的条目里没保存的编辑和展开的对话一起抹掉，
    // 页面滚动位置也会跳。
    const item = bankItems.find((i) => i.id === itemId);
    if (item) {
      item.answer = payload.answer;
      item.answer_en = payload.answer_en;
      item.user_edited = 1;
      // 后端存的是本地时间（datetime.now()），这里也要用本地时间，不能用 toISOString()
      // 拿 UTC——否则刚保存显示的时间跟刷新页面之后显示的时间对不上（差好几个小时）
      const now = new Date();
      item.updated_at = new Date(now - now.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
      lastBankSignature = bankSignature(); // 本地已经跟服务端一致了，别让下一次轮询白重画一遍
      const card = document.querySelector(`.bank-item[data-id="${itemId}"]`);
      if (card) {
        const badge = card.querySelector('.bank-badge');
        badge.textContent = '已自定义';
        badge.classList.remove('ai');
        card.querySelector('.bank-updated').textContent = `更新于 ${item.updated_at.replace('T', ' ')}`;
      }
    }
    restoreBtn(btn);
  } catch (e) {
    showToast(`保存失败：${e.message}`, 'error');
    restoreBtn(btn);
  }
}

// 手动加一题：区块标题右边的 "+" 打开一个行内输入框。
//
// 原来这里用的是浏览器自带的 prompt 弹窗，用户反馈"点了没反应"——弹出的系统对话框会被浏览器
// 拦掉（尤其是勾了"阻止此页面创建更多对话框"之后），而且点取消时代码直接 return、
// 连个提示都没有，两种情况在界面上长得一模一样。行内输入框看得见、拦不掉。

function addFormHtml(category) {
  return `
    <div class="bank-add-form">
      <input type="text" id="bankAddInput-${category}" placeholder="新增一题，先写问题（回车提交，Esc 取消）"
        onkeydown="onAddKeydown(event, '${category}')">
      <button class="btn btn-primary btn-sm" onclick="submitAddItem('${category}', this)">添加</button>
      <button class="btn btn-secondary btn-sm" onclick="cancelAddItem()">取消</button>
    </div>
  `;
}

function startAddItem(category) {
  bankAddingSection = category;
  renderBank(true);
}

function cancelAddItem() {
  bankAddingSection = null;
  renderBank(true);
}

function onAddKeydown(event, category) {
  if (event.key === 'Enter') {
    event.preventDefault();
    submitAddItem(category, event.target.parentElement.querySelector('.btn-primary'));
  } else if (event.key === 'Escape') {
    event.preventDefault();
    cancelAddItem();
  }
}

async function submitAddItem(category, btn) {
  const input = document.getElementById(`bankAddInput-${category}`);
  const question = (input ? input.value : '').trim();
  if (!question) {
    showToast('先写一下问题吧', 'info', 3000);
    if (input) input.focus();
    return;
  }
  setBtnLoading(btn, '添加中…');
  try {
    const res = await fetch('/api/interview/bank', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category, question }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '未知错误');
    bankAddingSection = null;
    // 新加的题直接展开：手动加题的下一步一定是写答案，还要再点一下才能写就太别扭了
    if (data.id) bankExpanded[data.id] = true;
    await loadBank();
    if (data.id) jumpToItem(data.id);
    showToast('已添加，接着写答案吧', 'success', 3000);
  } catch (e) {
    showToast(`添加失败：${e.message}`, 'error');
    restoreBtn(btn);
  }
}

async function removeBankItem(itemId, btn) {
  const item = bankItems.find((i) => i.id === itemId);
  const question = item ? item.question : '这一题';
  // 说清后果：这是真删库、不可恢复；而且判重只看库里现存的题，删掉 AI 出的题之后
  // 下一次起草很可能又把它生成回来。
  const confirmed = window.confirm(
    `确定删除「${question}」吗？\n\n这会把题目和你写的中英文答案一起从题库里永久删掉，无法恢复。\n`
    + '（如果这是 AI 出的题，下次点「AI 起草 / 补充」可能会再生成一遍。）'
  );
  if (!confirmed) return;
  setBtnLoading(btn, '删除中…');
  try {
    const res = await fetch(`/api/interview/bank/${itemId}`, { method: 'DELETE' });
    if (!res.ok) throw new Error((await res.json()).error || '未知错误');
    showToast('已删除', 'success', 2000);
    delete bankChats[itemId];
    delete bankExpanded[itemId];
    await loadBank();
  } catch (e) {
    showToast(`删除失败：${e.message}`, 'error');
    restoreBtn(btn);
  }
}

// ---------- 轮询 ----------

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
    // 页面切到后台标签页就先歇着，回来再继续（查了也没人看）
    if (document.hidden) {
      startBankPoll();
      return;
    }
    const before = bankItems.length;
    await loadBank();
    // 还在跑就等下一轮——loadBank() 里已经替我们重新排好了轮询
    if (bankGenerating) return;
    // 跑完了：必须区分成功和失败。以前这里只要 generating 变 false 就报"起草完成"，
    // 起草炸了也照样弹绿色提示，用户看到的是"成功了但题库还是空的"。
    if (bankError) {
      showToast(`题库起草失败：${bankError}`, 'error', 10000);
      return;
    }
    const added = bankItems.length - before;
    showToast(added > 0 ? `题库起草完成，新增 ${added} 条` : '题库起草完成', 'success', 5000);
  }, 5000);
}

// ============================================================================
// 跟 AI 对话完善答案
//
// 分工：每题的对话负责**改写这道题**，每轮给一整版新答案，点「采用」才写进输入框、
// 再点「保存」才落库——聊崩了也毁不掉已经写好的东西。右侧的全局助手只做跨题诊断
// （故事重不重复、覆盖面缺什么），不给采用按钮，因为它改完也不知道该回填哪一条。
// ============================================================================

function ensureChat(itemId) {
  if (!bankChats[itemId]) {
    bankChats[itemId] = { open: false, lang: 'zh', draft: '', sending: false, messages: [] };
  }
  return bankChats[itemId];
}

function toggleItemChat(itemId) {
  const chat = ensureChat(itemId);
  chat.open = !chat.open;
  const panel = document.getElementById(`bankChat-${itemId}`);
  panel.hidden = !chat.open;
  panel.innerHTML = chat.open ? itemChatHtml(itemId) : '';
  // 按钮文案跟着变（这里不走 renderBank，避免把别处没保存的编辑抹掉）
  const card = document.querySelector(`.bank-item[data-id="${itemId}"]`);
  if (card) card.querySelector('.bank-actions .btn-secondary').innerHTML = chat.open ? '收起对话' : CHAT_ICON + '跟 AI 聊聊这题';
  if (chat.open) {
    const input = document.getElementById(`bankChatInput-${itemId}`);
    if (input) input.focus();
  }
}

function itemChatHtml(itemId) {
  const chat = ensureChat(itemId);
  return `
    <div class="bank-chat-head">
      <span class="bank-chat-hint">这轮改哪一版：</span>
      <div class="chip-group">
        ${Object.entries(LANG_LABELS).map(([key, label]) => `
          <button class="chip ${chat.lang === key ? 'active' : ''}" onclick="setChatLang(${itemId}, '${key}')">${label}</button>
        `).join('')}
      </div>
    </div>
    <div class="bank-chat-msgs">
      ${chat.messages.length ? chat.messages.map((m, idx) => chatMsgHtml(m, itemId, idx)).join('')
        : `<div class="bank-chat-empty">说清楚哪里不满意就行，比如「太长了，压到 60 秒」「把 XX 项目的数据放前面」「这段听起来像套话，换成我自己的经历」。
             AI 每轮会给一整版改好的答案，点「采用」才会填进上面的输入框。</div>`}
      ${chat.sending ? '<div class="bank-chat-msg ai pending"><span class="spinner"></span>AI 正在改…</div>' : ''}
    </div>
    <div class="bank-chat-input">
      <textarea id="bankChatInput-${itemId}" rows="2" placeholder="想怎么改？（回车发送，Shift+回车换行）"
        ${chat.sending ? 'disabled' : ''}
        oninput="bankChats[${itemId}].draft = this.value"
        onkeydown="onChatKeydown(event, () => sendItemChat(${itemId}))">${escapeHtml(chat.draft)}</textarea>
      <button class="btn btn-primary btn-sm" ${chat.sending ? 'disabled' : ''} onclick="sendItemChat(${itemId})">发送</button>
    </div>
  `;
}

function chatMsgHtml(msg, itemId, idx) {
  if (msg.role === 'user') {
    return `<div class="bank-chat-msg user">${escapeHtml(msg.content)}</div>`;
  }
  if (msg.error) {
    return `<div class="bank-chat-msg error">${escapeHtml(msg.content)}</div>`;
  }
  return `
    <div class="bank-chat-msg ai">
      <div>${escapeHtml(msg.content)}</div>
      ${msg.answer ? `
        <div class="bank-chat-draft">${escapeHtml(msg.answer)}</div>
        <button class="btn btn-secondary btn-sm" onclick="applyChatAnswer(${itemId}, ${idx})">
          用这版替换${LANG_LABELS[msg.lang]}答案
        </button>` : ''}
    </div>
  `;
}

// onChatKeydown 搬进了 common.js（跟职位详情页的对话共用）。

function setChatLang(itemId, lang) {
  // 不清空对话：之前几轮聊出来的共识（哪里啰嗦、要突出什么）对另一版同样成立，
  // 只是接下来改的目标换了一版。
  ensureChat(itemId).lang = lang;
  renderItemChat(itemId);
}

function renderItemChat(itemId) {
  const panel = document.getElementById(`bankChat-${itemId}`);
  if (panel && !panel.hidden) {
    panel.innerHTML = itemChatHtml(itemId);
    const msgs = panel.querySelector('.bank-chat-msgs');
    if (msgs) msgs.scrollTop = msgs.scrollHeight;
  }
}

// 发给后端的历史：assistant 那条要还原成模型自己的输出格式（reply + answer 的 JSON），
// 它下一轮才认得出上文里哪些是它给过的版本。
function chatHistoryFor(messages) {
  return messages
    .filter((m) => !m.error)
    .map((m) => (m.role === 'user'
      ? { role: 'user', content: m.content }
      : { role: 'assistant', content: JSON.stringify({ reply: m.content, answer: m.answer || null }) }));
}

async function sendItemChat(itemId) {
  const chat = ensureChat(itemId);
  const message = (chat.draft || '').trim();
  if (!message || chat.sending) return;

  chat.messages.push({ role: 'user', content: message });
  chat.draft = '';
  chat.sending = true;
  renderItemChat(itemId);

  try {
    const res = await fetch(`/api/interview/bank/${itemId}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        lang: chat.lang,
        message,
        history: chatHistoryFor(chat.messages.slice(0, -1)),
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '未知错误');
    chat.messages.push({ role: 'assistant', content: data.reply, answer: data.answer || null, lang: chat.lang });
  } catch (e) {
    // 错误当成一条气泡插进对话，不打断已经聊出来的内容，用户可以直接再发一次
    chat.messages.push({ role: 'assistant', error: true, content: `出错了：${e.message}` });
  } finally {
    chat.sending = false;
    renderItemChat(itemId);
  }
}

function applyChatAnswer(itemId, idx) {
  const msg = bankChats[itemId].messages[idx];
  if (!msg || !msg.answer) return;
  const box = document.getElementById(msg.lang === 'en' ? `bankAnswerEn-${itemId}` : `bankAnswer-${itemId}`);
  box.value = msg.answer;
  autoGrow(box);
  box.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  // 刻意不自动保存：什么时候覆盖库里的答案由用户说了算。
  showToast(`已填入${LANG_LABELS[msg.lang]}答案框，确认没问题记得点「保存」`, 'info', 5000);
}

// ---------- 全局助手 ----------

function toggleAssistant() {
  assistantChat.open = !assistantChat.open;
  renderAssistant();
  if (assistantChat.open) {
    const input = document.getElementById('assistantInput');
    if (input) input.focus();
  }
}

function renderAssistant() {
  const panel = document.getElementById('bankAssistant');
  panel.classList.toggle('open', assistantChat.open);
  if (!assistantChat.open) {
    panel.innerHTML = '';
    return;
  }
  panel.innerHTML = `
    <div class="bank-assistant-head">
      <strong>${CHAT_ICON}题库助手</strong>
      <button class="icon-btn" onclick="toggleAssistant()" title="关闭">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
      </button>
    </div>
    <div class="bank-chat-hint" style="padding:0 0.9rem 0.5rem;">
      它看得到整个题库，负责挑毛病：故事有没有重复、缺哪一类、哪几题答得空洞。
      要改具体某一题，去那道题下面的「跟 AI 聊聊这题」，那边改完能一键采用。
    </div>
    <div class="bank-chat-msgs">
      ${assistantChat.messages.length ? assistantChat.messages.map((m) => chatMsgHtml(m)).join('')
        : '<div class="bank-chat-empty">试试问：「我这几个 STAR 故事有没有在讲同一件事？」「按我找的岗位方向还缺哪些常见题？」「哪几题答得最空？」</div>'}
      ${assistantChat.sending ? '<div class="bank-chat-msg ai pending"><span class="spinner"></span>AI 正在看整个题库…</div>' : ''}
    </div>
    <div class="bank-chat-input">
      <textarea id="assistantInput" rows="2" placeholder="想问什么？（回车发送，Shift+回车换行）"
        ${assistantChat.sending ? 'disabled' : ''}
        oninput="assistantChat.draft = this.value"
        onkeydown="onChatKeydown(event, sendAssistantChat)">${escapeHtml(assistantChat.draft)}</textarea>
      <button class="btn btn-primary btn-sm" ${assistantChat.sending ? 'disabled' : ''} onclick="sendAssistantChat()">发送</button>
    </div>
  `;
  const msgs = panel.querySelector('.bank-chat-msgs');
  if (msgs) msgs.scrollTop = msgs.scrollHeight;
}

async function sendAssistantChat() {
  const message = (assistantChat.draft || '').trim();
  if (!message || assistantChat.sending) return;

  assistantChat.messages.push({ role: 'user', content: message });
  assistantChat.draft = '';
  assistantChat.sending = true;
  renderAssistant();

  try {
    const res = await fetch('/api/interview/bank/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, history: chatHistoryFor(assistantChat.messages.slice(0, -1)) }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '未知错误');
    assistantChat.messages.push({ role: 'assistant', content: data.reply });
  } catch (e) {
    assistantChat.messages.push({ role: 'assistant', error: true, content: `出错了：${e.message}` });
  } finally {
    assistantChat.sending = false;
    renderAssistant();
  }
}

// ---------- init ----------
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initModelSelect('bankModelSelect', 'interview_bank');
  // 事件委托：答案框数量随题库变，逐个挂 oninput 内联属性又多又难改
  document.getElementById('bankRoot').addEventListener('input', (e) => {
    if (e.target.matches('.bank-answer')) autoGrow(e.target);
  });
  loadBank();
});
