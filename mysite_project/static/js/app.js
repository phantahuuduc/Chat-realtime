/* =====================================================================
   app.js — namespace duy nhất, tiện ích dùng chung, toast/modal, auth,
   preferences, chế độ tập trung, phím tắt, âm thanh, khởi tạo.
   Mọi module khác gắn vào window.App.
   ===================================================================== */

'use strict';

window.App = (function () {
  const config = {
    wsSubprotocol: 'chat.v1',
    heartbeatMs: 20000,
    ackTimeoutMs: 8000,
    gapWaitMs: 500,
    typingThrottleMs: 3000,
    typingIdleMs: 3000,
    readThrottleMs: 1000,
    offlineToastDelayMs: 2000,
    maxToasts: 3,
    messageMaxLength: 5000,
    counterThreshold: 200,
    groupWindowMs: 5 * 60 * 1000,
    swipeReplyPx: 60,
    quickReactions: ['\u{1F44D}', '❤️', '\u{1F602}', '\u{1F62E}', '\u{1F622}', '\u{1F525}'],
    emojiSet: [
      '\u{1F600}', '\u{1F602}', '\u{1F60A}', '\u{1F60D}', '\u{1F914}', '\u{1F44F}', '\u{1F44D}', '\u{1F64C}',
      '\u{1F525}', '⚽', '\u{1F3C6}', '\u{1F947}', '\u{1F389}', '❤️', '\u{1F494}', '\u{1F62D}',
      '\u{1F621}', '\u{1F631}', '\u{1F644}', '\u{1F643}', '\u{1F4AA}', '\u{1F91D}', '\u{1F440}', '✨',
    ],
  };

  const state = {
    accessToken: null,
    refreshToken: null,
    user: null,
    prefs: null,
  };

  const dom = {};

  // -------------------------------------------------------------------
  // Tiện ích
  // -------------------------------------------------------------------

  const util = {
    esc(value) {
      return String(value == null ? '' : value)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },

    initial(name) {
      return (name || '?').trim().charAt(0).toUpperCase() || '?';
    },

    icon(name, modifier) {
      return `<svg class="icon ${modifier || 'icon--sm'}"><use href="#icon-${name}"></use></svg>`;
    },

    timeLabel(iso) {
      return new Date(iso).toLocaleTimeString('vi-VN', { hour: '2-digit', minute: '2-digit' });
    },

    dayKey(iso) {
      const d = new Date(iso);
      return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
    },

    dayLabel(iso) {
      const d = new Date(iso);
      const now = new Date();
      const yesterday = new Date(now.getTime() - 86400000);
      if (util.dayKey(iso) === util.dayKey(now.toISOString())) return 'Hôm nay';
      if (util.dayKey(iso) === util.dayKey(yesterday.toISOString())) return 'Hôm qua';
      return `${d.getDate()} tháng ${d.getMonth() + 1}`;
    },

    relativeTime(iso) {
      if (!iso) return '';
      const diff = Date.now() - new Date(iso).getTime();
      if (diff < 60000) return 'vừa xong';
      if (diff < 3600000) return `${Math.floor(diff / 60000)}p`;
      if (diff < 86400000) return `${Math.floor(diff / 3600000)}h`;
      return `${Math.floor(diff / 86400000)}n`;
    },

    uuid() {
      if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
      return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        return (c === 'x' ? r : ((r & 0x3) | 0x8)).toString(16);
      });
    },

    debounce(fn, wait) {
      let timer = null;
      return (...args) => {
        clearTimeout(timer);
        timer = setTimeout(() => fn(...args), wait);
      };
    },

    readJson(key, fallback) {
      try {
        const raw = localStorage.getItem(key);
        return raw ? JSON.parse(raw) : fallback;
      } catch {
        return fallback;
      }
    },

    writeJson(key, value) {
      try {
        localStorage.setItem(key, JSON.stringify(value));
      } catch { /* quota đầy — bỏ qua */ }
    },

    prefersReducedMotion() {
      return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    },
  };

  // -------------------------------------------------------------------
  // Toast + Modal
  // -------------------------------------------------------------------

  const ui = {
    toast({ title, body, action, tone, duration = 5000, sticky = false, onClick }) {
      const stack = dom.toastStack;
      while (stack.children.length >= config.maxToasts) stack.firstElementChild.remove();

      const el = document.createElement('div');
      el.className = 'toast' + (tone === 'ok' ? ' toast--ok' : '');
      el.innerHTML =
        `<div class="toast__row">
           <span class="toast__title">${util.esc(title)}</span>
           ${action ? `<button type="button" class="btn-text">${util.esc(action.label)}</button>` : ''}
         </div>
         <div class="toast__body"${body ? '' : ' hidden'}>${util.esc(body || '')}</div>`;

      const dismiss = () => {
        el.classList.remove('toast--in');
        setTimeout(() => el.remove(), 200);
      };

      if (action) {
        el.querySelector('.btn-text').addEventListener('click', (e) => {
          e.stopPropagation();
          action.run();
          dismiss();
        });
      }
      if (onClick) el.addEventListener('click', () => { onClick(); dismiss(); });

      el.dismiss = dismiss;
      el.setContent = (nextTitle, nextBody, nextTone, dropAction = false) => {
        el.querySelector('.toast__title').textContent = nextTitle;
        const bodyEl = el.querySelector('.toast__body');
        bodyEl.textContent = nextBody || '';
        bodyEl.hidden = !nextBody;
        el.classList.toggle('toast--ok', nextTone === 'ok');
        if (dropAction) {
          const btn = el.querySelector('.btn-text');
          if (btn) btn.remove();
        }
      };

      stack.appendChild(el);
      void el.offsetWidth;   // ép reflow để transition vào chạy
      el.classList.add('toast--in');
      if (!sticky) setTimeout(dismiss, duration);
      return el;
    },

    openModal(title, bodyHtml, { confirmLabel = 'Xong', onConfirm, wide, danger } = {}) {
      const root = dom.modalRoot;
      root.innerHTML = `
        <div class="modal${wide ? ' modal--wide' : ''}" role="dialog" aria-modal="true">
          <div class="modal__head">
            <span class="modal__title display-text">${util.esc(title)}</span>
            <button class="btn-icon" data-modal-close title="Đóng">${util.icon('close')}</button>
          </div>
          <div class="modal__body">${bodyHtml}</div>
          ${onConfirm ? `<div class="modal__actions">
            <button class="btn" data-modal-close>Huỷ</button>
            <button class="btn ${danger ? 'btn--danger' : 'btn--primary'}"
                    data-modal-confirm>${util.esc(confirmLabel)}</button>
          </div>` : ''}
        </div>`;
      root.hidden = false;

      root.querySelectorAll('[data-modal-close]').forEach((b) =>
        b.addEventListener('click', ui.closeModal));
      const confirmBtn = root.querySelector('[data-modal-confirm]');
      if (confirmBtn) confirmBtn.addEventListener('click', () => onConfirm(root));
      root.onclick = (e) => { if (e.target === root) ui.closeModal(); };

      const firstInput = root.querySelector('input, textarea, select');
      if (firstInput) firstInput.focus();
      return root;
    },

    closeModal() {
      dom.modalRoot.hidden = true;
      dom.modalRoot.innerHTML = '';
    },

    modalOpen() {
      return !dom.modalRoot.hidden;
    },
  };

  // -------------------------------------------------------------------
  // REST
  // -------------------------------------------------------------------

  const api = {
    async fetch(url, options = {}) {
      const headers = { ...(options.headers || {}) };
      if (!(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
      if (state.accessToken) headers.Authorization = `Bearer ${state.accessToken}`;

      let resp = await fetch(url, { ...options, headers });

      // 401 là chuẩn; giữ thêm 403 phòng khi backend cũ trả sai mã.
      const expired = (resp.status === 401 || resp.status === 403) && state.accessToken;
      if (expired) {
        if (await api.refresh()) {
          headers.Authorization = `Bearer ${state.accessToken}`;
          resp = await fetch(url, { ...options, headers });
          if (resp.status === 401 || resp.status === 403) auth.sessionExpired();
        } else {
          auth.sessionExpired();
        }
      }
      return resp;
    },

    /** Rút thông điệp lỗi thật từ response để toast nói đúng chuyện gì xảy ra. */
    async errorText(resp, fallback) {
      try {
        const data = await resp.clone().json();
        if (typeof data === 'string') return data;
        if (data.error) return data.error;
        if (data.detail) return data.detail;
        const first = Object.values(data)[0];
        if (Array.isArray(first)) return first[0];
        if (typeof first === 'string') return first;
      } catch { /* body không phải JSON */ }
      return fallback;
    },

    async refresh() {
      if (!state.refreshToken) return false;
      try {
        const resp = await fetch('/api/auth/refresh/', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: state.refreshToken }),
        });
        if (!resp.ok) return false;
        const data = await resp.json();
        state.accessToken = data.access_token;
        state.refreshToken = data.refresh_token;
        const saved = util.readJson('auth', {});
        saved.access_token = data.access_token;
        saved.refresh_token = data.refresh_token;
        util.writeJson('auth', saved);
        return true;
      } catch {
        return false;
      }
    },
  };

  // -------------------------------------------------------------------
  // Preferences — localStorage áp dụng ngay, server đồng bộ nền
  // -------------------------------------------------------------------

  // Người dùng CHỈ đổi được hình nền. Mọi thông số hiệu ứng do app quyết định.
  const DEFAULT_PREFS = {
    theme_id: 'moscow-rain',
    custom_bg_url: null,
  };

  const prefs = {
    load() {
      state.prefs = { ...DEFAULT_PREFS, ...util.readJson('prefs', {}) };
      return state.prefs;
    },

    set(patch, { sync = true } = {}) {
      state.prefs = { ...state.prefs, ...patch };
      util.writeJson('prefs', state.prefs);
      App.atmosphere.applyPrefs();
      if (sync && state.accessToken) {
        api.fetch('/api/preferences/', { method: 'PATCH', body: JSON.stringify(patch) })
          .catch(() => { /* offline — localStorage đã giữ */ });
      }
    },

    async pull() {
      try {
        const resp = await api.fetch('/api/preferences/');
        if (!resp.ok) return;
        const data = await resp.json();
        state.prefs = { ...state.prefs, ...data };
        util.writeJson('prefs', state.prefs);
        App.atmosphere.applyPrefs();
      } catch { /* giữ nguyên bản local */ }
    },
  };

  // -------------------------------------------------------------------
  // Auth
  // -------------------------------------------------------------------

  const auth = {
    showError(message) {
      dom.authErrorText.textContent = message;
      dom.authError.hidden = false;
    },

    async request(url, body) {
      try {
        const resp = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (!resp.ok) {
          throw new Error(
            (data.username && data.username[0]) ||
            (data.password && data.password[0]) ||
            data.error || data.detail || 'Không đăng nhập được.',
          );
        }
        auth.onAuthenticated(data);
      } catch (err) {
        auth.showError(err.message);
      }
    },

    onAuthenticated(data) {
      state.accessToken = data.access_token;
      state.refreshToken = data.refresh_token;
      state.user = data.user;
      util.writeJson('auth', data);

      dom.authScreen.hidden = true;
      dom.app.hidden = false;
      dom.meName.textContent = state.user.username;
      dom.meAvatar.textContent = util.initial(state.user.username);

      prefs.pull();
      App.rooms.load();
      App.ws.connect();
    },

    showLogin() {
      dom.app.hidden = true;
      dom.authScreen.hidden = false;
    },

    logout() {
      App.chat.saveDraft();
      localStorage.removeItem('auth');
      App.ws.close();
      location.reload();
    },

    sessionExpired() {
      App.chat.saveDraft();
      ui.toast({ title: 'Phiên đã hết hạn', body: 'Đăng nhập lại để tiếp tục.' });
      setTimeout(() => { localStorage.removeItem('auth'); location.reload(); }, 1200);
    },
  };

  // -------------------------------------------------------------------
  // Chế độ tập trung + phím tắt
  // -------------------------------------------------------------------

  const SHORTCUTS = [
    ['Ctrl / Cmd + K', 'Tìm phòng nhanh'],
    ['Esc', 'Đóng modal, huỷ trả lời'],
    ['↑', 'Sửa tin nhắn cuối của bạn'],
    ['R', 'Trả lời tin nhắn đang trỏ chuột'],
    ['/', 'Mở danh sách lệnh'],
    ['?', 'Mở bảng phím tắt này'],
  ];

  function showShortcuts() {
    ui.openModal('Phím tắt', `<div class="shortcut-list">${
      SHORTCUTS.map(([key, label]) =>
        `<div class="shortcut-list__row">
           <span>${util.esc(label)}</span><span class="kbd">${util.esc(key)}</span>
         </div>`,
      ).join('')
    }</div>`);
  }

  function isTypingTarget(target) {
    return target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA');
  }

  function onKeydown(e) {
    const mod = e.ctrlKey || e.metaKey;

    if (mod && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      dom.roomFilter.focus();
      dom.roomFilter.select();
      return;
    }
    if (e.key === 'Escape') {
      if (ui.modalOpen()) { ui.closeModal(); return; }
      App.chat.onEscape();
      return;
    }
    if (isTypingTarget(e.target)) {
      if (e.key === 'ArrowUp' && e.target === dom.msgInput && !dom.msgInput.value) {
        if (App.chat.editLastOwn()) e.preventDefault();
      }
      return;
    }
    if (e.key === '?') { e.preventDefault(); showShortcuts(); return; }
    if (e.key.toLowerCase() === 'r') App.chat.replyToHovered();
  }

  // -------------------------------------------------------------------
  // Khởi tạo
  // -------------------------------------------------------------------

  const DOM_IDS = [
    'authScreen', 'authError', 'authErrorText', 'loginForm', 'registerForm',
    'loginUser', 'loginPass', 'regUser', 'regPass',
    'app', 'roomList', 'roomFilter', 'meAvatar', 'meName',
    'btnCreateRoom', 'btnJoinCode', 'btnPublicRooms', 'btnLogout', 'btnTheme',
    'btnShortcuts',
    'roomHeader', 'roomAvatar', 'roomName', 'connDot',
    'roomVisibility', 'roomCode', 'roomMemberCount',
    'btnSearchPanel', 'btnMemberPanel', 'btnLeaveRoom',
    'pinBar', 'pinBarText', 'pinBarCount', 'pinBarNext',
    'emptyState', 'emptyCreate', 'emptyJoin',
    'messageList', 'scrollDown', 'scrollDownCount',
    'composer', 'typingLine', 'replyPreview', 'replyPreviewText', 'replyCancel',
    'btnEmoji', 'msgInput', 'charCounter', 'btnSend', 'emojiPanel', 'menuPanel',
    'sidePanel', 'sidePanelTitle', 'sidePanelBody', 'btnClosePanel',
    'searchForm', 'roomSearchInput',
    'toastStack', 'modalRoot',
    'atmos', 'atmosImage', 'atmosImageNext', 'atmosVideo', 'atmosCanvas',
  ];

  function bindAuth() {
    document.querySelectorAll('.auth__tab').forEach((btn) => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.auth__tab').forEach((b) =>
          b.setAttribute('aria-selected', String(b === btn)));
        dom.loginForm.hidden = btn.dataset.tab !== 'login';
        dom.registerForm.hidden = btn.dataset.tab !== 'register';
        dom.authError.hidden = true;
      });
    });

    dom.loginForm.addEventListener('submit', (e) => {
      e.preventDefault();
      auth.request('/api/auth/login/', {
        username: dom.loginUser.value.trim(),
        password: dom.loginPass.value,
      });
    });

    dom.registerForm.addEventListener('submit', (e) => {
      e.preventDefault();
      auth.request('/api/auth/register/', {
        username: dom.regUser.value.trim(),
        password: dom.regPass.value,
      });
    });

    dom.btnLogout.addEventListener('click', auth.logout);
  }

  /** Một module hỏng không được kéo sập cả trang. */
  function safely(label, fn) {
    try {
      fn();
    } catch (err) {
      console.error(`[App] ${label} lỗi:`, err);
    }
  }

  function init() {
    DOM_IDS.forEach((id) => { dom[id] = document.getElementById(id); });

    const missing = DOM_IDS.filter((id) => !dom[id]);
    if (missing.length) {
      console.error('[App] thiếu phần tử trong DOM:', missing.join(', '));
    }

    safely('prefs', prefs.load);
    safely('auth', bindAuth);
    safely('atmosphere', App.atmosphere.init);
    safely('chat', App.chat.init);
    safely('rooms', App.rooms.init);

    safely('shell', () => {
      dom.btnShortcuts.addEventListener('click', showShortcuts);
      document.addEventListener('keydown', onKeydown);
      window.addEventListener('beforeunload', () => App.chat.saveDraft());
    });

    const saved = util.readJson('auth', null);
    if (saved && saved.access_token) auth.onAuthenticated(saved);
    else auth.showLogin();
  }

  document.addEventListener('DOMContentLoaded', init);

  return { config, state, dom, util, ui, api, prefs, auth };
}());
