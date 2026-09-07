/* =====================================================================
   chat.js — render message, optimistic send, gap buffer, tương tác,
   typing, read receipt, composer, lệnh gạch chéo, ghim, kéo trả lời.
   ===================================================================== */

'use strict';

App.chat = (function () {
  const { config, dom, util, ui, state: appState } = App;

  const state = {
    roomId: null,
    messages: {},
    lastApplied: {},
    gapBuffer: {},
    gapTimer: {},
    hasMoreOlder: {},
    loadingOlder: false,
    pending: new Map(),
    replyTo: null,
    editing: null,
    typingUsers: new Map(),
    lastTypingSent: 0,
    typingIdleTimer: null,
    maxSeenSequence: 0,
    lastReadSent: 0,
    readTimer: null,
    unseenBelow: 0,
    mention: null,
    slash: null,
    sendBlockedUntil: 0,
    observer: null,
    hoveredMessageId: null,
    seenAt: new Map(),   // key tin nhắn -> lúc thấy lần đầu, để loé sáng đúng một lần
    pinnedIndex: 0,
    pendingDraft: null,
  };

  const SLASH_COMMANDS = [
    { name: 'shrug', hint: 'Chèn ¯\\_(ツ)_/¯' },
    { name: 'me', hint: 'Gửi tin dạng hành động' },
    { name: 'theme', hint: 'Đổi hình nền theo id' },
    { name: 'clear', hint: 'Xoá khung hiển thị (không đụng DB)' },
    { name: 'who', hint: 'Ai đang online trong phòng' },
  ];

  const SHRUG = '¯\\_(ツ)_/¯';

  // Thẻ gợi ý hiện ở màn hình khởi đầu của phòng trợ lý AI.
  const AI_SUGGESTIONS = [
    { label: 'Giải thích khái niệm', text: 'Giải thích WebSocket cho người mới, ngắn gọn.' },
    { label: 'Tóm tắt giúp mình', text: 'Tóm tắt đoạn sau thành 3 gạch đầu dòng:\n' },
    { label: 'Dịch sang tiếng Anh', text: 'Dịch đoạn này sang tiếng Anh:\n' },
    { label: 'Sửa chính tả, ngữ pháp', text: 'Sửa lỗi chính tả và ngữ pháp cho đoạn sau:\n' },
    { label: 'Gợi ý ý tưởng', text: 'Gợi ý 5 ý tưởng tên phòng chat theo chủ đề bóng đá.' },
    { label: 'Viết code đơn giản', text: 'Viết hàm Python đảo ngược một chuỗi, kèm ví dụ.' },
  ];
  const URL_RE = /https?:\/\/[^\s<]+/i;

  /**
   * Từ khoá trong tin nhắn -> hiệu ứng toàn màn hình.
   * Duyệt theo thứ tự, khớp cái đầu tiên là dừng, nên mỗi tin chỉ bắn một
   * hiệu ứng dù có nhiều từ khoá.
   */
  const REACTION_WORDS = [
    { effect: 'hearts', words: ['yêu', 'thương', 'love', 'tim', '\u2764', '\u{1F496}', '\u{1F60D}'] },
    { effect: 'rain', words: ['mưa', 'rain', 'bão', '\u{1F327}', '\u2614'] },
    { effect: 'snow', words: ['tuyết', 'snow', 'lạnh', '\u2744'] },
    { effect: 'fireworks', words: ['vô', 'vào', 'goal', 'bàn thắng', 'siu', 'vô địch', '\u26BD', '\u{1F525}'] },
    { effect: 'confetti', words: ['chúc mừng', 'chuc mung', 'congrat', 'happy', 'tiệc', '\u{1F389}', '\u{1F38A}'] },
    { effect: 'stars', words: ['sao', 'star', 'tuyệt', 'đỉnh', 'xuất sắc', '\u2728', '\u2B50'] },
  ];

  // -------------------------------------------------------------------
  // Kho message
  // -------------------------------------------------------------------

  function listOf(roomId) {
    if (!state.messages[roomId]) state.messages[roomId] = [];
    return state.messages[roomId];
  }

  function sortMessages(list) {
    list.sort((a, b) => {
      const sa = a.sequence_number;
      const sb = b.sequence_number;
      if (sa != null && sb != null) return sa - sb;
      if (sa == null && sb == null) return new Date(a.created_at) - new Date(b.created_at);
      return sa == null ? 1 : -1;   // tin đang gửi luôn ở cuối
    });
  }

  function findByClientId(roomId, cmid) {
    return listOf(roomId).find((m) => m.client_message_id === cmid);
  }

  function findById(id) {
    return listOf(state.roomId).find((m) => m.id === id);
  }

  /** Dò từ khoá theo ranh giới từ để "sao" không khớp trong "sao chép". */
  function effectFor(text) {
    if (!text) return null;
    const lower = text.toLowerCase();
    for (const rule of REACTION_WORDS) {
      for (const word of rule.words) {
        const isWord = /\p{L}/u.test(word);
        if (!isWord) {
          if (lower.includes(word)) return rule.effect;
        } else if (new RegExp(`(^|[^\p{L}])${word}([^\p{L}]|$)`, 'iu').test(lower)) {
          return rule.effect;
        }
      }
    }
    return null;
  }

  function playEffect(text) {
    const effect = effectFor(text);
    if (effect) App.atmosphere.celebrate(effect);
  }

  function messageKey(m) {
    return String(m.client_message_id || m.id);
  }

  /**
   * Tin vừa xuất hiện thì loé sáng. Dùng mốc thời gian chứ không dùng cờ
   * một lần: `render()` vẽ lại cả danh sách nhiều lần trong lúc chờ ack,
   * nếu gỡ class ngay thì animation bị cắt ngang.
   */
  function isFresh(key) {
    const now = Date.now();
    if (!state.seenAt.has(key)) {
      state.seenAt.set(key, now);
      return true;
    }
    return now - state.seenAt.get(key) < 800;
  }

  function mentionsMe(text) {
    if (!text || !appState.user) return false;
    return new RegExp(`@${appState.user.username}\\b`, 'i').test(text);
  }

  // -------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------

  function renderContent(text) {
    return util.esc(text).replace(/@([\w.\-]+)/g, (match, name) => {
      const mine = appState.user && name.toLowerCase() === appState.user.username.toLowerCase();
      return `<span class="msg__mention${mine ? ' msg__mention--me' : ''}">${util.esc(match)}</span>`;
    });
  }

  function stateGlyph(messageState) {
    if (messageState === 'sending') return '<span class="msg__state-dot"></span>';
    if (messageState === 'failed') return util.icon('alert');
    if (messageState === 'sent') return util.icon('check');
    return util.icon('check-double');
  }

  function renderPreview(preview) {
    if (!preview || !preview.title) return '';
    return `<a class="link-preview" href="${util.esc(preview.url)}" target="_blank" rel="noopener noreferrer">
      ${preview.image ? `<img class="link-preview__thumb" src="${util.esc(preview.image)}" alt="">` : ''}
      <span class="link-preview__body">
        <span class="link-preview__title">${util.esc(preview.title)}</span>
        <span class="link-preview__desc">${util.esc(preview.description || preview.url)}</span>
      </span>
    </a>`;
  }

  function renderReactions(m) {
    if (!m.reactions || !m.reactions.length) return '';
    return `<div class="msg__reactions">${m.reactions.map((r) => `
      <button type="button"
              class="reaction-chip${r.user_ids.includes(appState.user.id) ? ' reaction-chip--mine' : ''}"
              data-react="${m.id}" data-emoji="${util.esc(r.emoji)}">
        <span>${util.esc(r.emoji)}</span><span>${r.count}</span>
      </button>`).join('')}</div>`;
  }

  function canManage(m) {
    const role = App.rooms.myRole();
    return m.sender_id === appState.user.id || role === 'owner' || role === 'admin';
  }

  function renderActions(m) {
    if (m.is_deleted || m.id == null || String(m.id).startsWith('local-')) return '';
    const mine = m.sender_id === appState.user.id;
    return `
      <div class="msg__quick">
        ${config.quickReactions.map((e) =>
          `<button type="button" class="msg__quick-btn" data-react="${m.id}"
                   data-emoji="${util.esc(e)}" title="${util.esc(e)}">${util.esc(e)}</button>`,
        ).join('')}
      </div>
      <div class="msg__actions">
        <button class="btn-icon" data-action="reply" data-id="${m.id}" title="Trả lời">${util.icon('reply')}</button>
        ${mine ? `<button class="btn-icon" data-action="edit" data-id="${m.id}" title="Sửa">${util.icon('edit')}</button>` : ''}
        <button class="btn-icon" data-action="copy" data-id="${m.id}" title="Sao chép">${util.icon('copy')}</button>
        ${canManage(m) ? `<button class="btn-icon${m.is_pinned ? ' btn-icon--on' : ''}"
                data-action="pin" data-id="${m.id}"
                title="${m.is_pinned ? 'Bỏ ghim' : 'Ghim'}">${util.icon('pin')}</button>` : ''}
        ${canManage(m) ? `<button class="btn-icon" data-action="delete" data-id="${m.id}" title="Xoá">${util.icon('trash')}</button>` : ''}
      </div>`;
  }

  function renderMessage(m, grouped) {
    const mine = m.sender_id === appState.user.id;
    const isAction = !m.is_deleted && m.content.startsWith('/me ');
    const key = messageKey(m);
    const classes = ['msg'];
    if (isFresh(key)) classes.push('msg--new');
    if (mine) classes.push('msg--mine');
    if (grouped) classes.push('msg--grouped');
    if (m.is_deleted) classes.push('msg--deleted');
    if (isAction) classes.push('msg--action');
    if (!m.is_deleted && !mine && mentionsMe(m.content)) classes.push('msg--mentioned');

    const author = !mine && App.rooms.activeIsAi()
      ? App.rooms.aiName()
      : m.sender_username;

    const head = grouped ? '' : `
      <div class="msg__head">
        <span class="msg__author">${util.esc(author)}</span>
        <span class="msg__time">${util.esc(util.timeLabel(m.created_at))}</span>
        ${m.sequence_number != null ? `<span class="msg__seq">#${m.sequence_number}</span>` : ''}
        ${m.is_pinned ? `<span class="msg__pin-flag">${util.icon('pin')}</span>` : ''}
      </div>`;

    const quote = m.reply_to ? `
      <button type="button" class="msg__quote" data-jump="${m.reply_to.id}">
        <b>${util.esc(m.reply_to.sender_username)}</b>
        ${util.esc(m.reply_to.is_deleted ? 'Tin nhắn đã bị xóa' : m.reply_to.content)}
      </button>` : '';

    const displayText = isAction
      ? `${util.esc(m.sender_username)} ${m.content.slice(4)}`
      : m.content;

    const inner = m.is_deleted
      ? '<div class="msg__text">Tin nhắn đã bị xóa</div>'
      : `<div class="msg__text">${renderContent(displayText)}${
          m.edited_at ? '<span class="msg__edited">đã sửa</span>' : ''
        }${
          mine ? `<span class="msg__state" data-state="${m._state || 'delivered'}">${stateGlyph(m._state)}</span>` : ''
        }${
          mine && m._state === 'failed'
            ? `<button class="msg__retry" data-retry="${util.esc(m.client_message_id)}">Gửi lại</button>` : ''
        }</div>${renderPreview(m.preview)}`;

    const caret = m._streaming ? '<span class="msg__caret" aria-hidden="true"></span>' : '';
    const body = `<div class="msg__bubble">${quote}${inner}${caret}</div>`;

    return `
      <div class="${classes.join(' ')}" data-message-id="${util.esc(m.id)}"
           data-key="${util.esc(key)}"
           data-sequence="${m.sequence_number == null ? '' : m.sequence_number}">
        <div class="msg__gutter">${
          grouped ? '' : `<span class="avatar avatar--sm">${util.esc(util.initial(author))}</span>`
        }</div>
        <div class="msg__body">${head}${body}${renderReactions(m)}</div>
        ${renderActions(m)}
      </div>`;
  }

  function render() {
    const list = listOf(state.roomId);
    const html = [];
    let previous = null;

    list.forEach((m) => {
      if (!previous || util.dayKey(previous.created_at) !== util.dayKey(m.created_at)) {
        html.push(`<div class="day-divider">${util.esc(util.dayLabel(m.created_at))}</div>`);
        previous = null;
      }
      const grouped = Boolean(
        previous &&
        previous.sender_id === m.sender_id &&
        new Date(m.created_at) - new Date(previous.created_at) < config.groupWindowMs,
      );
      html.push(renderMessage(m, grouped));
      previous = m;
    });

    dom.messageList.innerHTML = html.join('') || (App.rooms.activeIsAi()
      ? renderAiWelcome()
      : '<div class="side-panel__empty">Chưa có tin nhắn nào trong phòng này.</div>');

    observeMessages();
    renderPinBar();
  }

  function renderAiWelcome() {
    return `
      <div class="ai-welcome">
        <svg class="icon ai-welcome__mark"><use href="#icon-ball"></use></svg>
        <div class="ai-welcome__title display-text">Chào bạn, mình là CR7 AI</div>
        <div class="ai-welcome__sub">
          Hỏi mình bất cứ điều gì — giải thích, tóm tắt, dịch, sửa văn, hay viết code.
        </div>
        <div class="ai-welcome__cards">
          ${AI_SUGGESTIONS.map((item, i) => `
            <button type="button" class="ai-card" data-suggest="${i}">
              <span>${util.esc(item.label)}</span>
            </button>`).join('')}
        </div>
      </div>`;
  }

  function renderPinBar() {
    const pinned = listOf(state.roomId).filter((m) => m.is_pinned && !m.is_deleted);
    dom.messageList.classList.toggle('message-list--pinned', pinned.length > 0);
    if (!pinned.length) { dom.pinBar.hidden = true; return; }
    if (state.pinnedIndex >= pinned.length) state.pinnedIndex = 0;
    const current = pinned[pinned.length - 1 - state.pinnedIndex];
    dom.pinBar.hidden = false;
    dom.pinBarText.textContent = `${current.sender_username}: ${current.content}`;
    dom.pinBarText.dataset.jump = current.id;
    dom.pinBarCount.textContent = pinned.length > 1
      ? `${state.pinnedIndex + 1}/${pinned.length}` : '';
    dom.pinBarNext.hidden = pinned.length < 2;
  }

  // -------------------------------------------------------------------
  // Gửi tin — optimistic + ack + retry
  // -------------------------------------------------------------------

  function send() {
    const raw = dom.msgInput.value.trim();
    if (!raw || !state.roomId) return;

    if (state.editing) { submitEdit(raw); return; }
    if (raw.startsWith('/') && runSlashCommand(raw)) return;

    if (raw.length > config.messageMaxLength) {
      ui.toast({ title: 'Tin nhắn quá dài', body: `Tối đa ${config.messageMaxLength} ký tự.` });
      return;
    }
    if (!App.ws.isOnline()) return;

    const cmid = util.uuid();
    const roomId = state.roomId;
    const replyToId = state.replyTo ? state.replyTo.id : null;

    listOf(roomId).push({
      id: `local-${cmid}`,
      conversation_id: roomId,
      sender_id: appState.user.id,
      sender_username: appState.user.username,
      sequence_number: null,
      client_message_id: cmid,
      content: raw,
      is_deleted: false,
      is_pinned: false,
      edited_at: null,
      preview: null,
      reply_to: state.replyTo ? {
        id: state.replyTo.id,
        sender_username: state.replyTo.sender_username,
        content: state.replyTo.content,
        sequence_number: state.replyTo.sequence_number,
      } : null,
      reactions: [],
      created_at: new Date().toISOString(),
      _state: 'sending',
    });
    render();
    scrollToBottom();
    playEffect(raw);

    dispatch(cmid, roomId, raw, replyToId);

    dom.msgInput.value = '';
    autoResize();
    updateCounter();
    setReplyTo(null);
    stopTyping();
    localStorage.removeItem('draft');
  }

  function dispatch(cmid, roomId, text, replyToId) {
    const previous = state.pending.get(cmid);
    if (previous) clearTimeout(previous.timer);

    App.ws.send('message.new', {
      conversation_id: roomId,
      content: text,
      reply_to_id: replyToId,
    }, cmid);

    state.pending.set(cmid, {
      roomId,
      text,
      replyToId,
      // Không nhận ack sau 8s -> báo lỗi + cho gửi lại đúng cmid cũ.
      timer: setTimeout(() => {
        const message = findByClientId(roomId, cmid);
        if (message && message._state === 'sending') {
          message._state = 'failed';
          render();
        }
      }, config.ackTimeoutMs),
    });
  }

  function retry(cmid) {
    const entry = state.pending.get(cmid);
    if (!entry) return;
    const message = findByClientId(entry.roomId, cmid);
    if (message) { message._state = 'sending'; render(); }
    dispatch(cmid, entry.roomId, entry.text, entry.replyToId);
  }

  // -------------------------------------------------------------------
  // Nhận tin — gap buffer
  // -------------------------------------------------------------------

  function onMessageAck(payload, cmid) {
    const entry = state.pending.get(cmid);
    const roomId = payload.conversation_id || (entry && entry.roomId);
    if (entry) { clearTimeout(entry.timer); state.pending.delete(cmid); }
    if (!roomId) return;

    const message = findByClientId(roomId, cmid);
    if (!message) return;
    message.id = payload.message_id;
    message.sequence_number = payload.sequence_number;
    message.created_at = payload.server_time || message.created_at;
    message._state = 'sent';
    sortMessages(listOf(roomId));

    if (roomId === state.roomId) {
      render();
      const el = dom.messageList.querySelector(`[data-message-id="${message.id}"]`);
      if (el) el.classList.add('msg--acked');
    }
  }

  function onMessageNew(payload) {
    const roomId = payload.conversation_id;

    if (payload.cross_room) {
      if (roomId === state.roomId) return;
      App.rooms.notifyCrossRoom(payload);
      return;
    }
    if (roomId !== state.roomId) return;

    const mine = findByClientId(roomId, payload.client_message_id);
    if (mine && payload.sender_id === appState.user.id) {
      Object.assign(mine, payload, { _state: mine._state === 'read' ? 'read' : 'delivered' });
      const entry = state.pending.get(payload.client_message_id);
      if (entry) { clearTimeout(entry.timer); state.pending.delete(payload.client_message_id); }
      state.lastApplied[roomId] = Math.max(state.lastApplied[roomId] || 0, payload.sequence_number);
      sortMessages(listOf(roomId));
      render();
      return;
    }

    applyIncoming(roomId, payload);
  }

  /**
   * sequence_number > lastApplied + 1 -> đưa vào buffer, chờ tối đa 500ms,
   * hết timeout thì gọi offline-sync lấy phần thiếu rồi flush theo thứ tự.
   */
  function applyIncoming(roomId, payload) {
    const seq = payload.sequence_number;
    const applied = state.lastApplied[roomId] || 0;

    if (seq <= applied) return;
    if (seq === applied + 1) {
      commit(roomId, payload);
      flushGap(roomId);
      return;
    }

    if (!state.gapBuffer[roomId]) state.gapBuffer[roomId] = [];
    if (!state.gapBuffer[roomId].some((m) => m.sequence_number === seq)) {
      state.gapBuffer[roomId].push(payload);
    }
    if (!state.gapTimer[roomId]) {
      state.gapTimer[roomId] = setTimeout(() => {
        state.gapTimer[roomId] = null;
        requestSync(roomId);
      }, config.gapWaitMs);
    }
  }

  function commit(roomId, payload) {
    const list = listOf(roomId);
    const existing = list.find(
      (m) => (payload.id != null && m.id === payload.id) ||
             m.client_message_id === payload.client_message_id,
    );
    if (existing) Object.assign(existing, payload);
    else list.push({ ...payload, _state: 'delivered' });

    state.lastApplied[roomId] = Math.max(state.lastApplied[roomId] || 0, payload.sequence_number);
    sortMessages(list);

    if (roomId !== state.roomId) return;

    const atBottom = isNearBottom();
    render();
    if (atBottom) scrollToBottom();
    else if (payload.sender_id !== appState.user.id) bumpUnseen();

    if (payload.sender_id !== appState.user.id) playEffect(payload.content);

    if (payload.sender_id !== appState.user.id && mentionsMe(payload.content)) {
      ui.toast({
        title: `${payload.sender_username} nhắc tới bạn`,
        body: payload.content.slice(0, 60),
      });
    }
  }

  function flushGap(roomId) {
    const buffer = state.gapBuffer[roomId];
    if (!buffer || !buffer.length) return;
    buffer.sort((a, b) => a.sequence_number - b.sequence_number);

    let progressed = true;
    while (progressed) {
      progressed = false;
      for (let i = 0; i < buffer.length; i += 1) {
        const applied = state.lastApplied[roomId] || 0;
        if (buffer[i].sequence_number <= applied) {
          buffer.splice(i, 1);
          progressed = true;
          break;
        }
        if (buffer[i].sequence_number === applied + 1) {
          commit(roomId, buffer.splice(i, 1)[0]);
          progressed = true;
          break;
        }
      }
    }
    if (!buffer.length) {
      clearTimeout(state.gapTimer[roomId]);
      state.gapTimer[roomId] = null;
    }
  }

  function requestSync(roomId) {
    App.ws.send('sync.request', {
      conversation_id: roomId,
      after_sequence: state.lastApplied[roomId] || 0,
    });
  }

  function onSyncResponse(payload) {
    const roomId = payload.conversation_id;
    (payload.messages || []).forEach((m) => {
      const applied = state.lastApplied[roomId] || 0;
      if (m.sequence_number <= applied) {
        const existing = listOf(roomId).find((x) => x.id === m.id);
        if (existing) Object.assign(existing, m);
        return;
      }
      commit(roomId, m);
    });
    flushGap(roomId);
    // has_more = còn message MỚI hơn ngoài trang này -> xin tiếp trang sau.
    if (payload.has_more) requestSync(roomId);
    if (roomId === state.roomId) render();
  }

  /**
   * Trợ lý AI trả lời theo dòng. Ba loại sự kiện dùng chung type
   * `message.stream`: mở đầu (kèm nguyên message rỗng), từng mẩu chữ, và
   * kết thúc (kèm nội dung đầy đủ).
   */
  function onMessageStream(payload) {
    const roomId = payload.conversation_id
      || (payload.message && payload.message.conversation_id);
    if (roomId == null) return;

    const list = listOf(roomId);

    // Mở đầu: dựng bong bóng rỗng để người dùng thấy trợ lý đang gõ.
    if (payload.message) {
      state.seenAt.set(messageKey(payload.message), Date.now());
      if (!list.some((m) => m.id === payload.message.id)) {
        list.push({ ...payload.message, content: '', _state: 'delivered', _streaming: true });
        sortMessages(list);
      }
      state.lastApplied[roomId] = Math.max(
        state.lastApplied[roomId] || 0, payload.message.sequence_number,
      );
      if (roomId === state.roomId) { render(); scrollToBottom(); }
      return;
    }

    const message = list.find((m) => m.id === payload.message_id);
    if (!message) return;

    if (payload.done) {
      message.content = payload.content || message.content;
      message._streaming = false;
      if (roomId === state.roomId) render();
      return;
    }

    message.content += payload.chunk || '';
    if (roomId !== state.roomId) return;

    // Vẽ thẳng vào DOM thay vì render() cả danh sách sau mỗi mẩu chữ.
    const node = dom.messageList.querySelector(
      `[data-message-id="${payload.message_id}"] .msg__text`,
    );
    if (node) {
      const atBottom = isNearBottom();
      node.textContent = message.content;
      if (atBottom) scrollToBottom();
    } else {
      render();
    }
  }

  function patchMessage(payload, mutate) {
    const list = listOf(payload.conversation_id);
    const message = list.find((m) => m.id === (payload.id || payload.message_id));
    if (message) mutate(message);
    if (payload.conversation_id === state.roomId) render();
  }

  // -------------------------------------------------------------------
  // Composer
  // -------------------------------------------------------------------

  function autoResize() {
    const el = dom.msgInput;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  }

  function updateCounter() {
    const remaining = config.messageMaxLength - dom.msgInput.value.length;
    const show = remaining < config.counterThreshold;
    dom.charCounter.hidden = !show;
    if (show) {
      dom.charCounter.textContent = remaining;
      dom.charCounter.classList.toggle('composer__counter--over', remaining < 0);
    }
    dom.btnSend.disabled = !App.ws.isOnline() || remaining < 0 ||
      Date.now() < state.sendBlockedUntil;
  }

  function updateComposerAvailability() {
    const offline = !App.ws.isOnline();
    dom.msgInput.disabled = offline;
    dom.msgInput.placeholder = offline ? 'Đang kết nối lại…' : 'Nhắn gì đó…';
    updateCounter();
  }

  function insertAtCursor(text) {
    const el = dom.msgInput;
    const start = el.selectionStart ?? el.value.length;
    const end = el.selectionEnd ?? el.value.length;
    el.value = el.value.slice(0, start) + text + el.value.slice(end);
    el.selectionStart = el.selectionEnd = start + text.length;
    el.focus();
    autoResize();
    updateCounter();
  }

  // -------------------------------------------------------------------
  // Lệnh gạch chéo
  // -------------------------------------------------------------------

  function runSlashCommand(raw) {
    const [name, ...rest] = raw.slice(1).split(' ');
    const argument = rest.join(' ').trim();

    switch (name) {
      case 'shrug':
        dom.msgInput.value = argument ? `${argument} ${SHRUG}` : SHRUG;
        autoResize();
        updateCounter();
        return true;

      case 'me':
        if (!argument) { ui.toast({ title: 'Cú pháp: /me <hành động>' }); return true; }
        return false;   // gửi nguyên chuỗi "/me ..." để client khác render nghiêng

      case 'theme': {
        const preset = App.atmosphere.presetById(argument);
        if (!argument || preset.id !== argument) {
          ui.toast({
            title: 'Không có nền này',
            body: App.atmosphere.presets.map((p) => p.id).join(', '),
          });
        } else {
          App.prefs.set({ theme_id: preset.id });
          ui.toast({ title: `Đã đổi nền: ${preset.name}`, tone: 'ok' });
        }
        clearInput();
        return true;
      }

      case 'clear':
        state.messages[state.roomId] = [];
        state.lastApplied[state.roomId] = 0;
        render();
        clearInput();
        ui.toast({ title: 'Đã xoá khung hiển thị', body: 'Tin nhắn trên máy chủ vẫn còn.' });
        return true;

      case 'who': {
        const online = App.rooms.members().filter((m) => m.status === 'online');
        ui.toast({
          title: `${online.length} người đang online`,
          body: online.map((m) => m.username).join(', ') || 'Không có ai.',
        });
        clearInput();
        return true;
      }

      default:
        ui.toast({ title: `Không có lệnh /${name}` });
        return true;
    }
  }

  function clearInput() {
    dom.msgInput.value = '';
    autoResize();
    updateCounter();
    hideMenu();
  }

  // -------------------------------------------------------------------
  // Menu gợi ý (mention + slash) — dùng chung một popover
  // -------------------------------------------------------------------

  function renderMenu(items, activeIndex) {
    dom.menuPanel.innerHTML = items.map((item, i) => `
      <button type="button" class="menu__item${i === activeIndex ? ' menu__item--active' : ''}"
              data-pick="${util.esc(item.value)}">
        ${item.icon || ''}<span>${util.esc(item.label)}</span>
        ${item.hint ? `<span class="menu__hint">${util.esc(item.hint)}</span>` : ''}
      </button>`).join('');
    dom.menuPanel.hidden = false;
  }

  function hideMenu() {
    state.mention = null;
    state.slash = null;
    dom.menuPanel.hidden = true;
  }

  function refreshMenu() {
    const el = dom.msgInput;
    const value = el.value;
    const upToCursor = value.slice(0, el.selectionStart);

    if (value.startsWith('/') && !value.includes(' ')) {
      const query = value.slice(1).toLowerCase();
      const items = SLASH_COMMANDS
        .filter((c) => c.name.startsWith(query))
        .map((c) => ({ value: `/${c.name}`, label: `/${c.name}`, hint: c.hint }));
      if (!items.length) { hideMenu(); return; }
      state.slash = { items, index: 0 };
      state.mention = null;
      renderMenu(items, 0);
      return;
    }

    const match = /@([\w.\-]*)$/.exec(upToCursor);
    if (!match) { hideMenu(); return; }

    const query = match[1].toLowerCase();
    const items = App.rooms.members()
      .filter((m) => m.user_id !== appState.user.id && m.username.toLowerCase().startsWith(query))
      .slice(0, 6)
      .map((m) => ({
        value: m.username,
        label: m.username,
        icon: `<span class="avatar avatar--sm">${util.esc(util.initial(m.username))}</span>`,
      }));
    if (!items.length) { hideMenu(); return; }

    state.mention = { items, index: 0 };
    state.slash = null;
    renderMenu(items, 0);
  }

  function menuKeydown(e) {
    const menu = state.mention || state.slash;
    if (!menu) return false;

    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const step = e.key === 'ArrowDown' ? 1 : -1;
      menu.index = (menu.index + step + menu.items.length) % menu.items.length;
      renderMenu(menu.items, menu.index);
      return true;
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault();
      pickMenu(menu.items[menu.index].value);
      return true;
    }
    return false;
  }

  function pickMenu(value) {
    if (state.slash) {
      dom.msgInput.value = `${value} `;
      hideMenu();
      dom.msgInput.focus();
      autoResize();
      return;
    }
    const el = dom.msgInput;
    const cursor = el.selectionStart;
    const before = el.value.slice(0, cursor).replace(/@[\w.\-]*$/, `@${value} `);
    el.value = before + el.value.slice(cursor);
    el.selectionStart = el.selectionEnd = before.length;
    hideMenu();
    el.focus();
    autoResize();
  }

  // -------------------------------------------------------------------
  // Typing
  // -------------------------------------------------------------------

  function startTyping() {
    const now = Date.now();
    if (now - state.lastTypingSent > config.typingThrottleMs) {
      state.lastTypingSent = now;
      App.ws.send('typing.start', { conversation_id: state.roomId });
    }
    clearTimeout(state.typingIdleTimer);
    state.typingIdleTimer = setTimeout(stopTyping, config.typingIdleMs);
  }

  function stopTyping() {
    clearTimeout(state.typingIdleTimer);
    if (state.lastTypingSent) {
      state.lastTypingSent = 0;
      App.ws.send('typing.stop', { conversation_id: state.roomId });
    }
  }

  function onTyping(payload, isTyping) {
    if (payload.user_id === appState.user.id) return;
    if (payload.conversation_id !== state.roomId) return;

    clearTimeout(state.typingUsers.get(payload.username));
    if (!isTyping) {
      state.typingUsers.delete(payload.username);
    } else {
      state.typingUsers.set(payload.username, setTimeout(() => {
        state.typingUsers.delete(payload.username);
        renderTypingLine();
      }, config.typingIdleMs + 1000));
    }
    renderTypingLine();
  }

  function renderTypingLine() {
    const names = [...state.typingUsers.keys()];
    if (!names.length) { dom.typingLine.innerHTML = ''; return; }

    let label;
    if (names.length === 1) label = `${names[0]} đang gõ`;
    else if (names.length === 2) label = `${names[0]} và ${names[1]} đang gõ`;
    else label = `${names.length} người đang gõ`;

    dom.typingLine.innerHTML = `<span>${util.esc(label)}</span>
      <span class="typing-dots">
        <span class="typing-dots__dot"></span>
        <span class="typing-dots__dot"></span>
        <span class="typing-dots__dot"></span>
      </span>`;
  }

  // -------------------------------------------------------------------
  // Read receipt
  // -------------------------------------------------------------------

  function observeMessages() {
    if (!('IntersectionObserver' in window)) return;
    if (!state.observer) {
      state.observer = new IntersectionObserver((entries) => {
        let highest = state.maxSeenSequence;
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          const seq = Number(entry.target.dataset.sequence);
          if (Number.isFinite(seq) && seq > highest) highest = seq;
        });
        if (highest > state.maxSeenSequence) {
          state.maxSeenSequence = highest;
          scheduleReadReceipt();
        }
      }, { root: dom.messageList, threshold: 0.6 });
    }
    state.observer.disconnect();
    dom.messageList.querySelectorAll('[data-sequence]:not([data-sequence=""])')
      .forEach((el) => state.observer.observe(el));
  }

  function scheduleReadReceipt() {
    const wait = Math.max(0, config.readThrottleMs - (Date.now() - state.lastReadSent));
    clearTimeout(state.readTimer);
    state.readTimer = setTimeout(() => {
      state.lastReadSent = Date.now();
      App.ws.send('message.read', {
        conversation_id: state.roomId,
        sequence_number: state.maxSeenSequence,
      });
      App.rooms.clearUnread(state.roomId);
    }, wait);
  }

  // -------------------------------------------------------------------
  // Cuộn
  // -------------------------------------------------------------------

  function isNearBottom() {
    const el = dom.messageList;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 200;
  }

  function scrollToBottom() {
    dom.messageList.scrollTop = dom.messageList.scrollHeight;
    state.unseenBelow = 0;
    updateScrollButton();
  }

  function bumpUnseen() {
    state.unseenBelow += 1;
    updateScrollButton();
  }

  function updateScrollButton() {
    dom.scrollDown.classList.toggle('scroll-down--visible', !isNearBottom());
    dom.scrollDownCount.textContent = state.unseenBelow > 0 ? String(state.unseenBelow) : '';
  }

  async function loadOlder(roomId) {
    if (!roomId || state.loadingOlder || state.hasMoreOlder[roomId] === false) return;

    const list = listOf(roomId);
    const oldest = list.find((m) => m.sequence_number != null);
    if (!oldest) return;

    state.loadingOlder = true;
    const host = dom.messageList;
    const previousHeight = host.scrollHeight;
    const previousTop = host.scrollTop;

    try {
      const resp = await App.api.fetch(
        `/api/conversations/${roomId}/messages/?before_sequence=${oldest.sequence_number}&limit=50`,
      );
      const data = await resp.json();
      const rows = data.results || [];
      state.hasMoreOlder[roomId] = Boolean(data.has_more);
      if (rows.length) {
        const known = new Set(list.map((m) => m.id));
        const long_ago = Date.now() - 10000;
        rows.forEach((m) => {
          state.seenAt.set(messageKey(m), long_ago);
          if (!known.has(m.id)) list.push({ ...m, _state: 'delivered' });
        });
        sortMessages(list);
        if (roomId === state.roomId) {
          render();
          host.scrollTop = previousTop + (host.scrollHeight - previousHeight);
        }
      }
    } catch {
      /* im lặng — cuộn lại sẽ thử tiếp */
    } finally {
      state.loadingOlder = false;
    }
  }

  // -------------------------------------------------------------------
  // Reply / edit / jump
  // -------------------------------------------------------------------

  function setReplyTo(message) {
    state.replyTo = message;
    if (!message) { dom.replyPreview.hidden = true; return; }
    dom.replyPreviewText.textContent = `${message.sender_username}: ${message.content}`;
    dom.replyPreview.hidden = false;
    dom.msgInput.focus();
  }

  function startEdit(message) {
    state.editing = message;
    setReplyTo(null);
    dom.msgInput.value = message.content;
    dom.msgInput.focus();
    autoResize();
    dom.msgInput.placeholder = 'Đang sửa — Enter để lưu, Esc để huỷ';
  }

  function cancelEdit() {
    state.editing = null;
    clearInput();
    updateComposerAvailability();
  }

  function submitEdit(text) {
    App.ws.send('message.edited', { message_id: state.editing.id, content: text });
    cancelEdit();
  }

  function editLastOwn() {
    const own = listOf(state.roomId)
      .filter((m) => m.sender_id === appState.user.id && !m.is_deleted && m.id != null);
    if (!own.length) return false;
    startEdit(own[own.length - 1]);
    return true;
  }

  function replyToHovered() {
    if (!state.hoveredMessageId) return;
    const message = findById(state.hoveredMessageId);
    if (message && !message.is_deleted) setReplyTo(message);
  }

  function jumpTo(id) {
    const el = dom.messageList.querySelector(`[data-message-id="${id}"]`);
    if (!el) { ui.toast({ title: 'Tin nhắn không còn trong danh sách đang tải' }); return; }
    el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    el.classList.remove('msg--highlight');
    void el.offsetWidth;
    el.classList.add('msg--highlight');
    setTimeout(() => el.classList.remove('msg--highlight'), 2000);
  }

  function onEscape() {
    if (!dom.emojiPanel.hidden) { toggleEmoji(false); return; }
    if (!dom.menuPanel.hidden) { hideMenu(); return; }
    if (state.editing) { cancelEdit(); return; }
    if (state.replyTo) setReplyTo(null);
  }

  function toggleEmoji(force) {
    const show = force === undefined ? dom.emojiPanel.hidden : force;
    dom.emojiPanel.hidden = !show;
    dom.btnEmoji.classList.toggle('btn-icon--on', show);
  }

  // -------------------------------------------------------------------
  // Kéo sang phải để trả lời
  // -------------------------------------------------------------------

  function bindSwipeReply() {
    let target = null;
    let startX = 0;
    let startY = 0;
    let offset = 0;

    dom.messageList.addEventListener('pointerdown', (e) => {
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      if (e.target.closest('button, a')) return;
      const el = e.target.closest('.msg');
      if (!el) return;
      target = el;
      startX = e.clientX;
      startY = e.clientY;
      offset = 0;
    });

    dom.messageList.addEventListener('pointermove', (e) => {
      if (!target) return;
      const dx = e.clientX - startX;
      const dy = Math.abs(e.clientY - startY);
      if (dx < 0 || dy > 40) return;
      offset = Math.min(dx, 90);
      target.classList.add('msg--swiping');
      target.style.transform = `translateX(${offset}px)`;
    });

    const release = () => {
      if (!target) return;
      const el = target;
      const reached = offset > config.swipeReplyPx;
      el.classList.remove('msg--swiping');
      el.style.transform = '';
      if (reached) {
        const message = findById(Number(el.dataset.messageId));
        if (message && !message.is_deleted) setReplyTo(message);
      }
      target = null;
      offset = 0;
    };

    dom.messageList.addEventListener('pointerup', release);
    dom.messageList.addEventListener('pointercancel', release);
    dom.messageList.addEventListener('pointerleave', release);
  }

  // -------------------------------------------------------------------
  // Bắt sự kiện
  // -------------------------------------------------------------------

  function onListClick(e) {
    const suggest = e.target.closest('[data-suggest]');
    if (suggest) {
      const item = AI_SUGGESTIONS[Number(suggest.dataset.suggest)];
      dom.msgInput.value = item.text;
      autoResize();
      updateCounter();
      // Gợi ý kết thúc bằng xuống dòng nghĩa là chờ người dùng dán nội dung vào.
      if (item.text.endsWith('\n')) dom.msgInput.focus(); else send();
      return;
    }

    const retryBtn = e.target.closest('[data-retry]');
    if (retryBtn) { retry(retryBtn.dataset.retry); return; }

    const reactBtn = e.target.closest('[data-react]');
    if (reactBtn) {
      App.ws.send('message.reaction', {
        message_id: Number(reactBtn.dataset.react),
        emoji: reactBtn.dataset.emoji,
      });
      return;
    }

    const jumpBtn = e.target.closest('[data-jump]');
    if (jumpBtn) { jumpTo(Number(jumpBtn.dataset.jump)); return; }

    const actionBtn = e.target.closest('[data-action]');
    if (!actionBtn) return;

    const id = Number(actionBtn.dataset.id);
    const message = findById(id);
    if (!message) return;

    switch (actionBtn.dataset.action) {
      case 'reply': setReplyTo(message); break;
      case 'edit': startEdit(message); break;
      case 'pin':
        App.ws.send(message.is_pinned ? 'message.unpinned' : 'message.pinned',
          { message_id: id });
        break;
      case 'copy':
        navigator.clipboard.writeText(message.content)
          .then(() => ui.toast({ title: 'Đã sao chép' }))
          .catch(() => ui.toast({ title: 'Không sao chép được' }));
        break;
      case 'delete':
        ui.openModal('Xoá tin nhắn?',
          '<p class="modal__body-text">Tin nhắn sẽ hiển thị là “Tin nhắn đã bị xóa”.</p>',
          {
            confirmLabel: 'Xoá',
            danger: true,
            onConfirm: () => {
              App.ws.send('message.deleted', { message_id: id });
              ui.closeModal();
            },
          });
        break;
      default: break;
    }
  }

  function bindComposer() {
    dom.emojiPanel.innerHTML = config.emojiSet
      .map((e) => `<button type="button" class="emoji-panel__btn" data-emoji="${util.esc(e)}">${util.esc(e)}</button>`)
      .join('');

    dom.btnEmoji.addEventListener('click', () => toggleEmoji());
    dom.emojiPanel.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-emoji]');
      if (!btn) return;
      insertAtCursor(btn.dataset.emoji);
      toggleEmoji(false);
    });

    dom.menuPanel.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-pick]');
      if (btn) pickMenu(btn.dataset.pick);
    });

    dom.msgInput.addEventListener('input', () => {
      autoResize();
      updateCounter();
      refreshMenu();
      if (dom.msgInput.value.trim()) startTyping(); else stopTyping();
    });

    dom.msgInput.addEventListener('keydown', (e) => {
      if (menuKeydown(e)) return;
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });

    dom.btnSend.addEventListener('click', send);
    dom.replyCancel.addEventListener('click', () => setReplyTo(null));
  }

  function bindList() {
    dom.messageList.addEventListener('click', onListClick);
    dom.messageList.addEventListener('scroll', () => {
      if (isNearBottom()) state.unseenBelow = 0;
      updateScrollButton();
      if (dom.messageList.scrollTop < 80) loadOlder(state.roomId);
    });
    dom.messageList.addEventListener('mouseover', (e) => {
      const el = e.target.closest('.msg');
      state.hoveredMessageId = el ? Number(el.dataset.messageId) : null;
    });
    dom.scrollDown.addEventListener('click', scrollToBottom);
    dom.pinBarText.addEventListener('click', () => jumpTo(Number(dom.pinBarText.dataset.jump)));
    dom.pinBarNext.addEventListener('click', () => {
      state.pinnedIndex += 1;
      renderPinBar();
    });
    bindSwipeReply();
  }

  function registerWsHandlers() {
    App.ws.on('message.new', onMessageNew);
    App.ws.on('message.ack', onMessageAck);
    App.ws.on('sync.response', onSyncResponse);
    App.ws.on('message.stream', onMessageStream);

    App.ws.on('message.edited', (p) => patchMessage(p, (m) => Object.assign(m, p)));
    App.ws.on('message.deleted', (p) => patchMessage(p, (m) => {
      m.is_deleted = true;
      m.content = '';
      m.is_pinned = false;
    }));
    App.ws.on('message.reaction', (p) => patchMessage(p, (m) => { m.reactions = p.reactions || []; }));
    App.ws.on('message.preview', (p) => patchMessage(p, (m) => { m.preview = p.preview; }));
    App.ws.on('message.pinned', (p) => patchMessage(p, (m) => { m.is_pinned = true; }));
    App.ws.on('message.unpinned', (p) => patchMessage(p, (m) => { m.is_pinned = false; }));

    App.ws.on('message.read', (p) => {
      if (p.user_id === appState.user.id) return;
      listOf(p.conversation_id).forEach((m) => {
        if (m.sender_id === appState.user.id && m.sequence_number != null &&
            m.sequence_number <= p.sequence_number) {
          m._state = 'read';
        }
      });
      if (p.conversation_id === state.roomId) render();
    });

    App.ws.on('typing.start', (p) => onTyping(p, true));
    App.ws.on('typing.stop', (p) => onTyping(p, false));
    App.ws.on('heartbeat.pong', () => {});
    App.ws.on('error', onServerError);
  }

  function onServerError(payload) {
    if (payload.code === 'RATE_LIMITED') {
      const seconds = payload.retry_after || 1;
      state.sendBlockedUntil = Date.now() + seconds * 1000;
      updateCounter();
      ui.toast({ title: `Gửi quá nhanh, chờ ${seconds}s` });
      setTimeout(updateCounter, seconds * 1000);
      return;
    }
    if (payload.code === 'ROOM_FORBIDDEN') {
      ui.toast({ title: 'Bạn không có quyền vào phòng này' });
      App.rooms.closeRoom();
      App.rooms.load();
      return;
    }
    ui.toast({ title: payload.message || payload.code });
  }

  // -------------------------------------------------------------------
  // Vòng đời phòng
  // -------------------------------------------------------------------

  async function openRoom(roomId) {
    saveDraft();
    stopTyping();

    dom.messageList.classList.add('message-list--swapping');
    state.roomId = roomId;
    state.typingUsers.clear();
    state.maxSeenSequence = 0;
    state.unseenBelow = 0;
    state.pinnedIndex = 0;
    renderTypingLine();

    App.ws.send('room.open', { conversation_id: roomId });

    const resp = await App.api.fetch(`/api/conversations/${roomId}/messages/?limit=50`);
    if (!resp.ok) {
      ui.toast({ title: 'Không tải được lịch sử phòng' });
      dom.messageList.classList.remove('message-list--swapping');
      return;
    }
    const data = await resp.json();
    const rows = data.results || [];
    state.messages[roomId] = rows.map((m) => ({ ...m, _state: 'delivered' }));
    const long_ago = Date.now() - 10000;
    rows.forEach((m) => state.seenAt.set(messageKey(m), long_ago));
    state.hasMoreOlder[roomId] = Boolean(data.has_more);
    state.lastApplied[roomId] = rows.length ? rows[rows.length - 1].sequence_number : 0;

    render();
    scrollToBottom();
    dom.messageList.classList.remove('message-list--swapping');

    if (state.pendingDraft && state.pendingDraft.roomId === roomId) {
      dom.msgInput.value = state.pendingDraft.text;
      state.pendingDraft = null;
      autoResize();
      updateCounter();
    }
  }

  function closeRoom() {
    state.roomId = null;
    setReplyTo(null);
    dom.pinBar.hidden = true;
  }

  function onReconnected() {
    if (!state.roomId) return;
    App.ws.send('room.open', { conversation_id: state.roomId });
    requestSync(state.roomId);
  }

  function resync() {
    if (state.roomId) requestSync(state.roomId);
  }

  function saveDraft() {
    const text = dom.msgInput ? dom.msgInput.value : '';
    if (text && state.roomId) {
      util.writeJson('draft', { roomId: state.roomId, text });
    }
  }

  function init() {
    state.pendingDraft = util.readJson('draft', null);
    bindComposer();
    bindList();
    registerWsHandlers();
    updateComposerAvailability();
  }

  return {
    init,
    openRoom,
    closeRoom,
    onReconnected,
    resync,
    saveDraft,
    onEscape,
    jumpTo,
    editLastOwn,
    replyToHovered,
    updateComposerAvailability,
  };
}());
