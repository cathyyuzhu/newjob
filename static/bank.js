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

const BANK_SECTIONS = [
  { key: 'self_intro', title: '自我介绍', hint: '60-90 秒口播稿，中英文各一版。' },
  { key: 'common', title: '通用问题', hint: '离职原因、职业规划、优缺点这类每场面试都会遇到的题，每题中英文各一版。' },
  { key: 'star_story', title: 'STAR 故事库', hint: '能反复复用的几个核心故事，按 情境-任务-行动-结果 分四段写，中英文各一版。' },
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
    bankItems.map((i) => [i.id, i.question, i.answer, i.answer_en, i.user_edited, i.updated_at]),
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

  body.innerHTML = `
    ${bankGenerating ? '<div class="plain-text" style="color:var(--text-faint); margin-bottom:0.75rem;">正在根据你的简历起草，分三段生成（自我介绍 → 通用问题 → STAR 故事库），每段完成会立刻出现在下面，全部跑完可能需要 5-10 分钟…</div>' : ''}
    ${bankError && !bankGenerating ? `
      <div class="detail-section">
        <h4 style="color:var(--danger);">上次起草失败</h4>
        <div class="plain-text" style="color:var(--danger);">${escapeHtml(bankError)}</div>
        <div class="plain-text" style="color:var(--text-faint); font-size:0.8rem; margin-top:0.35rem;">已经改过的条目不受影响，可以直接再点一次「AI 起草 / 补充」重试。</div>
      </div>` : ''}
    ${!bankItems.length && !bankGenerating
      ? '<div class="plain-text" style="color:var(--text-faint);">题库还是空的。点右上角「AI 起草 / 补充」根据你的简历生成一份初稿，然后逐条改成你自己的说法——不想手打的话，每题下面都能点「跟 AI 聊聊这题」，说清哪里不满意让它改。</div>'
      : BANK_SECTIONS.map((s) => bankSectionHtml(s)).join('')}
  `;

  Object.entries(unsaved).forEach(([id, value]) => {
    const el = document.getElementById(id);
    if (el) el.value = value;
  });
  body.querySelectorAll('.bank-answer').forEach(autoGrow);
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
  const chat = bankChats[item.id];
  return `
    <div class="bank-item" data-id="${item.id}">
      <div class="bank-item-head">
        <span class="bank-q">${escapeHtml(item.question)}</span>
        ${item.user_edited ? '<span class="bank-badge">已自定义</span>' : '<span class="bank-badge ai">AI 初稿</span>'}
      </div>
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
          ${chat && chat.open ? '收起对话' : '💬 跟 AI 聊聊这题'}
        </button>
        <button class="btn btn-secondary btn-sm" onclick="saveBankItem(${item.id}, this)">保存</button>
        <button class="btn btn-danger btn-sm"
          title="把这道题和你写的中英文答案一起从题库里永久删掉"
          onclick="removeBankItem(${item.id}, this)">删除</button>
      </div>
      <div class="bank-chat" id="bankChat-${item.id}" ${chat && chat.open ? '' : 'hidden'}>${chat && chat.open ? itemChatHtml(item.id) : ''}</div>
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
  if (card) card.querySelector('.bank-actions .btn-secondary').textContent = chat.open ? '收起对话' : '💬 跟 AI 聊聊这题';
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

function onChatKeydown(event, send) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    send();
  }
}

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
      <strong>💬 题库助手</strong>
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
  // 事件委托：答案框数量随题库变，逐个挂 oninput 内联属性又多又难改
  document.getElementById('bankRoot').addEventListener('input', (e) => {
    if (e.target.matches('.bank-answer')) autoGrow(e.target);
  });
  loadBank();
});
