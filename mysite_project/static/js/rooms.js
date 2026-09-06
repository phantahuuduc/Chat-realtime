/* =====================================================================
   rooms.js — sidebar, tạo/join/rời phòng, panel phải (thành viên,
   tìm kiếm, hình nền), thông báo liên phòng.
   ===================================================================== */

'use strict';

App.rooms = (function () {
  const { dom, util, ui, state: appState } = App;

  const state = {
    rooms: [],
    filter: '',
    activeId: null,
    active: null,
    members: [],
    myRole: null,
    panel: null,
  };

  const PANEL_TITLES = {
    members: 'Thành viên',
    search: 'Tìm trong phòng',
    theme: 'Hình nền',
  };

  // -------------------------------------------------------------------
  // Danh sách phòng
  // -------------------------------------------------------------------

  async function load() {
    const resp = await App.api.fetch('/api/conversations/');
    if (!resp.ok) return;
    const data = await resp.json();
    state.rooms = data.results || [];
    render();
  }

  function previewOf(room) {
    const last = room.last_message;
    if (!last) return 'Chưa có tin nhắn';
    const who = last.sender ? last.sender.username : (last.sender_username || '');
    return `${who}: ${last.content || ''}`;
  }

  function render() {
    const rows = state.rooms.filter((r) =>
      !state.filter || (r.name || '').toLowerCase().includes(state.filter));

    if (!rows.length) {
      dom.roomList.innerHTML = `<div class="sidebar__empty">${
        state.filter ? 'Không có phòng nào khớp.' : 'Chưa có phòng nào. Tạo phòng mới để bắt đầu.'
      }</div>`;
      return;
    }

    dom.roomList.innerHTML = rows.map((room) => `
      <button type="button" class="room-row${room.id === state.activeId ? ' room-row--active' : ''}"
              data-room="${room.id}">
        <span class="avatar">${util.esc(util.initial(room.name || 'R'))}</span>
        <span class="room-row__body">
          <span class="room-row__head">
            <span class="room-row__name">${util.esc(room.name || `Room #${room.id}`)}</span>
            <span class="room-row__time">${
              util.esc(room.last_message ? util.relativeTime(room.last_message.created_at) : '')
            }</span>
          </span>
          <span class="room-row__preview">${util.esc(previewOf(room))}</span>
        </span>
        ${room.unread_count > 0
          ? `<span class="badge-unread${room._bump ? ' badge-unread--bump' : ''}">${room.unread_count}</span>`
          : ''}
      </button>`).join('');

    state.rooms.forEach((r) => { r._bump = false; });
  }

  function clearUnread(roomId) {
    const room = state.rooms.find((r) => r.id === roomId);
    if (room && room.unread_count) {
      room.unread_count = 0;
      render();
    }
  }

  // -------------------------------------------------------------------
  // Mở / đóng phòng
  // -------------------------------------------------------------------

  async function open(roomId) {
    if (state.activeId === roomId) return;

    state.activeId = roomId;
    state.active = state.rooms.find((r) => r.id === roomId) || null;
    render();

    dom.emptyState.hidden = true;
    dom.roomHeader.hidden = false;
    dom.messageList.hidden = false;
    dom.composer.hidden = false;
    renderHeader();

    await App.chat.openRoom(roomId);
    loadMembers(roomId);
  }

  function closeRoom() {
    state.activeId = null;
    state.active = null;
    App.chat.closeRoom();
    dom.emptyState.hidden = false;
    dom.roomHeader.hidden = true;
    dom.messageList.hidden = true;
    dom.composer.hidden = true;
    dom.sidePanel.hidden = true;
    state.panel = null;
    render();
  }

  function renderHeader() {
    const room = state.active;
    if (!room) return;
    const name = room.name || `Room #${room.id}`;
    dom.roomName.textContent = name;
    dom.roomName.dataset.text = name;
    dom.roomAvatar.textContent = util.initial(name);
    dom.roomVisibility.textContent = room.visibility === 'public' ? 'Công khai' : 'Riêng tư';
    dom.roomCode.textContent = room.join_code ? `Mã ${room.join_code}` : '';
    dom.roomCode.hidden = !room.join_code;
    dom.roomMemberCount.textContent = room.members ? `${room.members.length} thành viên` : '';
  }

  // -------------------------------------------------------------------
  // Tạo / tham gia / rời phòng
  // -------------------------------------------------------------------

  function showCreateModal() {
    ui.openModal('Tạo phòng mới', `
      <label class="field">
        <span class="field__label">Tên phòng</span>
        <input type="text" class="field__input" id="newRoomName" maxlength="255"
               placeholder="VD: Moscow 2008">
      </label>
      <label class="field">
        <span class="field__label">Mô tả ngắn</span>
        <input type="text" class="field__input" id="newRoomDesc" maxlength="255" placeholder="Tuỳ chọn">
      </label>
      <div class="field">
        <span class="field__label">Loại phòng</span>
        <div class="radio-row">
          <label class="radio-row__item"><input type="radio" name="visibility" value="private" checked>Riêng tư</label>
          <label class="radio-row__item"><input type="radio" name="visibility" value="public">Công khai</label>
        </div>
      </div>
      <label class="field">
        <span class="field__label">Mời thành viên (username, cách nhau bằng dấu phẩy)</span>
        <input type="text" class="field__input" id="newRoomMembers" placeholder="Tuỳ chọn">
      </label>`, {
      confirmLabel: 'Tạo phòng',
      onConfirm: async (root) => {
        const name = root.querySelector('#newRoomName').value.trim();
        if (!name) { ui.toast({ title: 'Nhập tên phòng' }); return; }
        const resp = await App.api.fetch('/api/conversations/', {
          method: 'POST',
          body: JSON.stringify({
            name,
            type: 'group',
            description: root.querySelector('#newRoomDesc').value.trim(),
            visibility: root.querySelector('input[name="visibility"]:checked').value,
            member_usernames: root.querySelector('#newRoomMembers').value
              .split(',').map((s) => s.trim()).filter(Boolean),
          }),
        });
        if (!resp.ok) {
          ui.toast({
            title: 'Không tạo được phòng',
            body: await App.api.errorText(resp, 'Thử lại sau ít phút.'),
          });
          return;
        }
        const data = await resp.json();
        ui.closeModal();
        await load();
        open(data.id);
        ui.toast({ title: 'Đã tạo phòng', body: `Mã tham gia: ${data.join_code}`, tone: 'ok' });
      },
    });
  }

  function showJoinModal() {
    ui.openModal('Tham gia bằng mã', `
      <label class="field">
        <span class="field__label">Mã phòng (6 ký tự)</span>
        <input type="text" class="field__input field__code" id="joinCode" maxlength="6"
               autocapitalize="characters">
      </label>
      <p class="field__hint">Chủ phòng thấy mã này ở thanh tiêu đề phòng.</p>`, {
      confirmLabel: 'Tham gia',
      onConfirm: async (root) => {
        const code = root.querySelector('#joinCode').value.trim().toUpperCase();
        if (code.length !== 6) { ui.toast({ title: 'Mã phải đủ 6 ký tự' }); return; }
        const resp = await App.api.fetch('/api/rooms/join/', {
          method: 'POST',
          body: JSON.stringify({ join_code: code }),
        });
        if (!resp.ok) {
          ui.toast({
            title: 'Không tham gia được',
            body: await App.api.errorText(resp, 'Kiểm tra lại mã phòng.'),
          });
          return;
        }
        const data = await resp.json();
        ui.closeModal();
        await load();
        open(data.conversation.id);
        ui.toast({
          title: data.joined ? 'Đã tham gia phòng' : 'Bạn đã ở trong phòng này',
          tone: 'ok',
        });
      },
    });
  }

  async function showPublicRooms() {
    const resp = await App.api.fetch('/api/rooms/public/');
    const data = await resp.json();
    const rows = data.results || [];

    const body = rows.length ? rows.map((room) => `
      <div class="public-room-row">
        <span class="avatar">${util.esc(util.initial(room.name))}</span>
        <span class="public-room-row__body">
          <span class="public-room-row__name">${util.esc(room.name)}</span>
          <span class="public-room-row__desc">${
            util.esc(room.description || `${room.member_count} thành viên`)
          }</span>
        </span>
        ${room.joined
          ? '<span class="tag">Đã tham gia</span>'
          : `<button class="btn btn--primary" data-join-public="${room.id}">Tham gia</button>`}
      </div>`).join('')
      : '<div class="side-panel__empty">Chưa có phòng công khai nào.</div>';

    const root = ui.openModal('Phòng công khai', body, { wide: true });
    root.querySelectorAll('[data-join-public]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const joinResp = await App.api.fetch('/api/rooms/join/', {
          method: 'POST',
          body: JSON.stringify({ conversation_id: Number(btn.dataset.joinPublic) }),
        });
        if (!joinResp.ok) { ui.toast({ title: 'Không tham gia được phòng này' }); return; }
        const joinData = await joinResp.json();
        ui.closeModal();
        await load();
        open(joinData.conversation.id);
      });
    });
  }

  function confirmLeave() {
    if (!state.activeId) return;
    const roomId = state.activeId;
    ui.openModal('Rời phòng?',
      '<p class="modal__body-text">Bạn sẽ không nhận tin nhắn của phòng này nữa.</p>', {
        confirmLabel: 'Rời phòng',
        danger: true,
        onConfirm: async () => {
          const resp = await App.api.fetch(`/api/rooms/${roomId}/leave/`, { method: 'POST' });
          ui.closeModal();
          if (!resp.ok) { ui.toast({ title: 'Không rời được phòng' }); return; }
          closeRoom();
          await load();
          ui.toast({ title: 'Đã rời phòng', tone: 'ok' });
        },
      });
  }

  // -------------------------------------------------------------------
  // Panel phải
  // -------------------------------------------------------------------

  function togglePanel(kind) {
    if (state.panel === kind) {
      dom.sidePanel.hidden = true;
      state.panel = null;
      return;
    }
    state.panel = kind;
    dom.sidePanel.hidden = false;
    dom.sidePanelTitle.textContent = PANEL_TITLES[kind];
    dom.searchForm.hidden = kind !== 'search';
    dom.sidePanelBody.innerHTML = '';

    if (kind === 'members') { loadMembers(state.activeId); renderMembers(); }
    else if (kind === 'search') dom.roomSearchInput.focus();
    else if (kind === 'theme') App.atmosphere.renderThemePanel(dom.sidePanelBody);
  }

  async function loadMembers(roomId) {
    if (!roomId) return;
    const resp = await App.api.fetch(`/api/rooms/${roomId}/members/`);
    if (!resp.ok) return;
    const data = await resp.json();
    state.members = data.members || [];
    state.myRole = data.my_role;
    dom.roomMemberCount.textContent = `${state.members.length} thành viên`;
    if (state.panel === 'members') renderMembers();
  }

  function renderMembers() {
    const canKick = state.myRole === 'owner' || state.myRole === 'admin';
    dom.sidePanelBody.innerHTML = state.members.map((m) => `
      <div class="member-row">
        <span class="avatar avatar--sm">${util.esc(util.initial(m.username))}</span>
        <span class="member-row__body">
          <span class="member-row__name">
            <span class="presence-dot" data-state="${util.esc(m.status)}" data-user="${m.user_id}"></span>
            ${util.esc(m.username)}
          </span>
          <span class="member-row__role">${util.esc(m.role)}</span>
        </span>
        ${canKick && m.user_id !== appState.user.id && m.role !== 'owner'
          ? `<button class="btn-icon" data-kick="${m.user_id}" title="Đá khỏi phòng">${util.icon('close')}</button>`
          : ''}
      </div>`).join('') || '<div class="side-panel__empty">Chưa có thành viên.</div>';
  }

  async function runSearch() {
    const q = dom.roomSearchInput.value.trim();
    if (q.length < 2 || !state.activeId) { dom.sidePanelBody.innerHTML = ''; return; }
    const resp = await App.api.fetch(
      `/api/rooms/${state.activeId}/messages/search/?q=${encodeURIComponent(q)}`,
    );
    if (!resp.ok) return;
    const data = await resp.json();
    dom.sidePanelBody.innerHTML = (data.results || []).map((m) => `
      <button type="button" class="search-result" data-jump="${m.id}">
        <span class="search-result__meta">
          <span>${util.esc(m.sender_username)}</span>
          <span>${util.esc(util.timeLabel(m.created_at))}</span>
        </span>
        <span class="search-result__text">${util.esc(m.content)}</span>
      </button>`).join('') || '<div class="side-panel__empty">Không tìm thấy tin nhắn nào.</div>';
  }

  // -------------------------------------------------------------------
  // Sự kiện từ server
  // -------------------------------------------------------------------

  function notifyCrossRoom(payload) {
    const room = state.rooms.find((r) => r.id === payload.conversation_id);
    if (room) {
      room.unread_count = (room.unread_count || 0) + 1;
      room._bump = true;
      room.last_message = {
        content: payload.content,
        sender_username: payload.sender_username,
        created_at: payload.created_at,
        sequence_number: payload.sequence_number,
      };
      render();
    }

    const mentioned = appState.user &&
      new RegExp(`@${appState.user.username}\\b`, 'i').test(payload.content || '');

    ui.toast({
      title: mentioned
        ? `${payload.sender_username} nhắc tới bạn · ${room ? room.name : 'Phòng khác'}`
        : (room ? room.name : `Phòng #${payload.conversation_id}`),
      body: `${payload.sender_username}: ${(payload.content || '').slice(0, 60)}`,
      onClick: () => open(payload.conversation_id),
    });
  }

  function registerWsHandlers() {
    App.ws.on('presence.update', (p) => {
      const member = state.members.find((m) => m.user_id === p.user_id);
      if (member) member.status = p.status;
      dom.sidePanelBody.querySelectorAll(`[data-user="${p.user_id}"]`)
        .forEach((el) => { el.dataset.state = p.status; });
    });

    App.ws.on('member.joined', (p) => {
      if (p.members) {
        state.members = p.members.map((m) => ({ ...m, status: m.status || 'offline' }));
        const me = state.members.find((m) => m.user_id === appState.user.id);
        state.myRole = me ? me.role : null;
        dom.roomMemberCount.textContent = `${state.members.length} thành viên`;
        if (state.panel === 'members') renderMembers();
        return;
      }
      if (p.conversation_id !== state.activeId) return;
      loadMembers(p.conversation_id);
      ui.toast({ title: `${p.username} đã vào phòng` });
    });

    App.ws.on('member.left', (p) => {
      if (p.conversation_id !== state.activeId) return;
      loadMembers(p.conversation_id);
      ui.toast({ title: `${p.username} đã rời phòng` });
    });

    App.ws.on('room.created', (p) => {
      if (!p.conversation) return;
      if (!state.rooms.some((r) => r.id === p.conversation.id)) state.rooms.unshift(p.conversation);
      render();
    });

    App.ws.on('room.deleted', (p) => {
      state.rooms = state.rooms.filter((r) => r.id !== p.conversation_id);
      render();
      if (state.activeId === p.conversation_id) {
        closeRoom();
        ui.toast({
          title: p.reason === 'removed' ? 'Bạn đã bị đưa khỏi phòng' : 'Phòng đã đóng',
        });
      }
    });
  }

  // -------------------------------------------------------------------
  // Khởi tạo
  // -------------------------------------------------------------------

  function init() {
    dom.roomList.addEventListener('click', (e) => {
      const row = e.target.closest('[data-room]');
      if (row) open(Number(row.dataset.room));
    });

    dom.roomFilter.addEventListener('input', () => {
      state.filter = dom.roomFilter.value.trim().toLowerCase();
      render();
    });

    dom.btnCreateRoom.addEventListener('click', showCreateModal);
    dom.emptyCreate.addEventListener('click', showCreateModal);
    dom.btnJoinCode.addEventListener('click', showJoinModal);
    dom.emptyJoin.addEventListener('click', showJoinModal);
    dom.btnPublicRooms.addEventListener('click', showPublicRooms);
    dom.btnLeaveRoom.addEventListener('click', confirmLeave);

    dom.btnMemberPanel.addEventListener('click', () => togglePanel('members'));
    dom.btnSearchPanel.addEventListener('click', () => togglePanel('search'));
    dom.btnClosePanel.addEventListener('click', () => {
      dom.sidePanel.hidden = true;
      state.panel = null;
    });

    dom.roomSearchInput.addEventListener('input', util.debounce(runSearch, 250));

    dom.sidePanelBody.addEventListener('click', async (e) => {
      const jump = e.target.closest('[data-jump]');
      if (jump) { App.chat.jumpTo(Number(jump.dataset.jump)); return; }

      const kick = e.target.closest('[data-kick]');
      if (kick) {
        const resp = await App.api.fetch(
          `/api/rooms/${state.activeId}/members/${Number(kick.dataset.kick)}/remove/`,
          { method: 'POST' },
        );
        if (resp.ok) {
          ui.toast({ title: 'Đã đá thành viên', tone: 'ok' });
          loadMembers(state.activeId);
        } else {
          ui.toast({ title: 'Không đá được thành viên' });
        }
      }
    });

    registerWsHandlers();
  }

  return {
    init,
    load,
    closeRoom,
    togglePanel,
    notifyCrossRoom,
    clearUnread,
    members: () => state.members,
    myRole: () => state.myRole,
  };
}());
