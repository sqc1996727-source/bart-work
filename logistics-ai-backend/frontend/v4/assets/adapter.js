(function () {
  'use strict';

  // 同源接口边界：只负责取数，不理解业务，也不保存任何凭据。
  const BASE = String(window.JL_AGENT_API_BASE || '').replace(/\/+$/, '');

  function requestId() {
    try {
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return window.crypto.randomUUID();
      }
    } catch (_) { /* 忽略，走下面的兜底 */ }
    return 'k-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 14);
  }

  async function request(path, options) {
    const opts = options || {};
    const headers = Object.assign({ Accept: 'application/json' }, opts.headers || {});
    const init = {
      method: opts.method || 'GET',
      credentials: 'same-origin',
      headers
    };
    if (opts.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }

    let response;
    try {
      response = await fetch(BASE + path, init);
    } catch (_) {
      const err = new Error('网络没有响应');
      err.status = 0;
      err.code = 'network';
      err.payload = {};
      throw err;
    }

    let text = '';
    try { text = await response.text(); } catch (_) { text = ''; }
    let data = {};
    if (text) {
      try { data = JSON.parse(text); } catch (_) { data = { message: text }; }
    }

    if (!response.ok) {
      const payload = data && typeof data === 'object' ? data : {};
      const raw = payload.error;
      const code = typeof raw === 'string' ? raw : (raw && typeof raw === 'object' ? String(raw.code || '') : '');
      const err = new Error(code || ('接口返回 ' + response.status));
      err.status = response.status;
      err.code = code;
      err.payload = payload;
      throw err;
    }
    return data && typeof data === 'object' ? data : {};
  }

  window.JLApi = {
    requestId: requestId,

    login(username, password) {
      return request('/api/login', { method: 'POST', body: { username, password } });
    },

    logout() {
      return request('/api/logout', { method: 'POST', body: {} });
    },

    bootstrap() {
      return request('/api/agent/ui/bootstrap');
    },

    today() {
      return request('/api/agent/today');
    },

    state() {
      return request('/api/state');
    },

    notifications() {
      return request('/api/agent/notifications');
    },

    seen(id) {
      return request('/api/agent/notifications/seen', { method: 'POST', body: { id } });
    },

    chat(text, conversationId) {
      const body = { text, memory_scope: 'enterprise' };
      if (conversationId) body.conversation_id = conversationId;
      return request('/api/agent/chat', { method: 'POST', body });
    },

    // 费用变更三步中的第一步：新建候选。后端强制要求幂等键。
    createFeeCandidate(input) {
      const body = {
        password: input.password,
        kind: 'fee',
        fee_id: input.fee_id,
        status: input.status
      };
      if (input.status === '作废') body.reason = input.reason || '';
      return request('/api/finance/candidates', {
        method: 'POST',
        body,
        headers: { 'Idempotency-Key': requestId() }
      });
    },

    reviewCandidate(candidateId) {
      return request('/api/finance/candidates/' + encodeURIComponent(candidateId));
    },

    confirmCandidate(candidateId, password) {
      return request('/api/finance/candidates/' + encodeURIComponent(candidateId) + '/confirm', {
        method: 'POST',
        body: { password }
      });
    }
  };
})();
