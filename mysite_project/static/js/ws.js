/* =====================================================================
   ws.js — WebSocket client: envelope, reconnect, chỉ báo kết nối.
   Token đi qua subprotocol: new WebSocket(url, ["chat.v1", token]).
   ===================================================================== */

'use strict';

App.ws = (function () {
  const { config, dom, ui, state: appState } = App;

  const state = {
    socket: null,
    generation: 0,
    status: 'offline',
    attempt: 0,
    reconnectTimer: null,
    heartbeatTimer: null,
    offlineTimer: null,
    offlineToast: null,
  };

  const handlers = {};

  function on(type, handler) {
    handlers[type] = handler;
  }

  function envelope(type, payload, clientMessageId = null) {
    return { type, payload: payload || {}, client_message_id: clientMessageId };
  }

  function send(type, payload, clientMessageId = null) {
    if (!state.socket || state.socket.readyState !== WebSocket.OPEN) return false;
    state.socket.send(JSON.stringify(envelope(type, payload, clientMessageId)));
    return true;
  }

  function isOnline() {
    return state.status === 'online';
  }

  // -------------------------------------------------------------------
  // Kết nối
  // -------------------------------------------------------------------

  function connect() {
    if (!appState.accessToken) return;
    clearTimeout(state.reconnectTimer);

    const generation = ++state.generation;
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
    setStatus('connecting');

    let socket;
    try {
      socket = new WebSocket(
        `${scheme}//${location.host}/ws/chat/`,
        [config.wsSubprotocol, appState.accessToken],
      );
    } catch {
      scheduleReconnect();
      return;
    }
    state.socket = socket;

    socket.onopen = () => {
      if (generation !== state.generation) { socket.close(); return; }
      state.attempt = 0;
      setStatus('online');
      startHeartbeat();
      App.chat.onReconnected();
    };

    socket.onmessage = (event) => {
      let data;
      try { data = JSON.parse(event.data); } catch { return; }
      const handler = handlers[data.type];
      if (handler) handler(data.payload || {}, data.client_message_id);
    };

    socket.onclose = async (event) => {
      if (generation !== state.generation) return;
      stopHeartbeat();
      setStatus('offline');

      if (event.code === 4001) {
        // Token hết hạn giữa phiên: refresh rồi reconnect, thất bại thì về login.
        if (await App.api.refresh()) { connect(); return; }
        App.auth.sessionExpired();
        return;
      }
      if (event.code === 4003) {
        ui.toast({ title: 'Bạn không có quyền vào phòng này' });
        App.rooms.closeRoom();
        App.rooms.load();
      }
      scheduleReconnect();
    };
  }

  function close() {
    state.generation += 1;
    stopHeartbeat();
    if (state.socket) state.socket.close(1000, 'client');
    state.socket = null;
  }

  function scheduleReconnect() {
    state.attempt += 1;
    const delay = Math.min(1000 * 2 ** (state.attempt - 1), 15000);
    clearTimeout(state.reconnectTimer);
    state.reconnectTimer = setTimeout(connect, delay);
    if (state.offlineToast) {
      state.offlineToast.setContent(`Mất kết nối · thử lại sau ${Math.round(delay / 1000)}s`, '');
    }
  }

  function startHeartbeat() {
    stopHeartbeat();
    state.heartbeatTimer = setInterval(() => send('heartbeat.ping', {}), config.heartbeatMs);
  }

  function stopHeartbeat() {
    clearInterval(state.heartbeatTimer);
    state.heartbeatTimer = null;
  }

  // -------------------------------------------------------------------
  // Chỉ báo: chấm thường trực + toast góc dưới phải sau 2 giây
  // -------------------------------------------------------------------

  function setStatus(status) {
    if (state.status === status) return;
    const previous = state.status;
    state.status = status;

    dom.connDot.dataset.state = status;
    dom.connDot.title = {
      online: 'Kết nối trực tiếp',
      connecting: 'Đang kết nối…',
      offline: 'Mất kết nối',
    }[status];

    App.chat.updateComposerAvailability();

    if (status === 'online') {
      clearTimeout(state.offlineTimer);
      state.offlineTimer = null;
      if (state.offlineToast) {
        const toast = state.offlineToast;
        state.offlineToast = null;
        toast.setContent('Đã kết nối lại', '', 'ok', true);
        setTimeout(() => toast.dismiss(), 1500);
      }
      if (previous === 'offline') App.chat.resync();
      return;
    }

    if (!state.offlineToast && !state.offlineTimer) {
      state.offlineTimer = setTimeout(() => {
        state.offlineTimer = null;
        if (state.status === 'online') return;
        state.offlineToast = ui.toast({
          title: 'Mất kết nối · đang thử lại',
          sticky: true,
          action: {
            label: 'Thử lại ngay',
            run: () => { state.attempt = 0; connect(); },
          },
        });
      }, config.offlineToastDelayMs);
    }
  }

  return { connect, close, send, on, isOnline };
}());
