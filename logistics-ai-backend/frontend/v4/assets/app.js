(function () {
  'use strict';

  const ORG = '巨龙物流';
  const ROLE_NAMES = { owner_finance: '企业负责人', dispatcher: '调度管理', employee: '业务员工' };

  const ICONS = {
    brand: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 7.5h10.5v9.5H2.5z"/><path d="M13 10.5h4.2L20.5 14v3H13z"/><circle cx="6.5" cy="18.5" r="1.4"/><circle cx="17" cy="18.5" r="1.4"/></svg>',
    plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
    brief: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/></svg>',
    file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M9 13.5l2 2 3.5-3.5"/></svg>',
    money: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="6" width="18" height="12" rx="2"/><circle cx="12" cy="12" r="2.4"/><path d="M6 9.5v5M18 9.5v5" stroke-linecap="round"/></svg>',
    data: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/></svg>',
    task: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3 2"/></svg>',
    clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3 2"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="11" cy="11" r="6"/><path d="M15.5 15.5L20 20"/></svg>',
    bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 16v-5a6 6 0 0 1 12 0v5l1.8 2H4.2z"/><path d="M10 20a2 2 0 0 0 4 0"/></svg>',
    grid: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/></svg>'
  };

  const S = {
    me: null,
    role: '',
    capabilities: {},
    suggestions: [],
    today: null,
    todayError: null,
    pending: [],
    waybills: [],
    fees: [],
    receipts: [],
    rev: 0,
    updated: '',
    lastStateOk: false,
    view: 'brief',
    rightTab: 'work',
    conversationId: '',
    notifications: [],
    timer: 0,
    detail: null,
    detailFrom: 'brief'
  };

  // 左栏品牌旁的提醒徽标是动态建出来的，这里留一个引用，避免每次去文档里找。
  let badgeEl = null;

  // ---------- 基础工具 ----------

  function $(id) { return document.getElementById(id); }

  function make(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function iconNode(name) {
    const holder = document.createElement('span');
    holder.innerHTML = ICONS[name] || ICONS.file;
    return holder.firstElementChild;
  }

  function canFinance() { return S.role === 'owner_finance' || S.role === 'dispatcher'; }

  function roleName() { return ROLE_NAMES[S.role] || '成员'; }

  function displayName() {
    if (!S.me) return '未登录';
    return S.me.display_name || S.me.name || S.me.salutation || '当前用户';
  }

  function pad2(n) { return n < 10 ? '0' + n : String(n); }

  function dayLine() {
    const d = new Date();
    return (d.getMonth() + 1) + '月' + d.getDate() + '日 · ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  function shortStamp(value) {
    if (value === undefined || value === null || value === '') return '';
    let d;
    if (typeof value === 'number') d = new Date(value < 1e12 ? value * 1000 : value);
    else d = new Date(String(value));
    if (isNaN(d.getTime())) return '';
    return (d.getMonth() + 1) + '月' + d.getDate() + '日 ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  function shortTime(value) {
    const text = shortStamp(value);
    return text ? text.split(' ')[1] || text : '刚刚';
  }

  function fmtMoney(value) {
    if (value === undefined || value === null || String(value).trim() === '') return '';
    const n = Number(String(value).replace(/[,，\s¥]/g, ''));
    if (!isFinite(n)) return '';
    return '¥' + n.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  }

  function parseList(value) {
    let list = value;
    if (typeof list === 'string') {
      try { list = JSON.parse(list); } catch (_) { return []; }
    }
    return Array.isArray(list) ? list.filter(function (x) { return x && typeof x === 'object'; }) : [];
  }

  function toast(text, ms) {
    const node = $('toast');
    if (!node) return;
    node.textContent = text;
    node.classList.add('open');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () { node.classList.remove('open'); }, ms || 3200);
  }

  function humanError(err, context) {
    const status = err && err.status;
    const code = (err && err.code) || '';
    if (context === 'chat') {
      if (status === 401) return '登录状态已过期，请重新登录。';
      if (status === 0) return '企业智能体这次没有回应，请稍后再试。';
      return '这次没有拿到回答，请稍后再试。';
    }
    if (status === 0) return '网络没有响应，请检查网络后再试。';
    if (status === 401) return '登录状态已过期，请重新登录。';
    if (context === 'fee') {
      if (code === 'reauthentication_failed') return '密码不对，请重新输入。';
      if (code === 'revision_conflict' || code === 'fee_evidence_changed' || code === 'difference_evidence_changed') return '账本已变更，请重新核对。';
      if (code === 'confirmed_fee_requires_reversal') return '这笔费用已经确认过，只能作废。';
      if (code === 'invalid_fee_status') return '这笔费用的当前状态不允许这样改。';
      if (code === 'invalid_fee_update') return '这次改动的内容不合规，请重新选择要改成的状态。';
      if (code === 'invalid_fee_type') return '这笔费用的类型不在可改范围内，需要先修正类型。';
      if (code === 'invalid_request') return '这次提交的内容不完整，请重新试一次。';
      if (code === 'invalid_or_duplicate_fee_id') return '这笔费用找不到了，请刷新后重试。';
      if (code === 'duplicate_fee') return '这笔费用和另一笔重复，需要先在账本里理清，不能直接改。';
      if (code === 'invalid_fee_amount') return '这笔费用的金额不完整，暂时不能直接改。';
      if (code === 'invalid_fee_waybill') return '这笔费用对应的运单对不上，暂时不能直接改。';
      if (code === 'reversal_reason_required') return '作废要写清楚理由。';
      if (code === 'idempotency_key_required') return '这次提交没有准备好，请再点一次。';
      if (status === 409) return '账本已变更，请重新核对。';
      if (status === 404) return '这条确认记录已失效，请重新发起。';
      if (status === 403) return '密码不对，请重新输入。';
    }
    if (status === 403) return '这个操作不在你的账号范围里。';
    if (status === 404) return '这条记录已经找不到了。';
    if (status === 409) return '数据刚刚变了，请刷新后再试。';
    if (status === 429) return '操作太频繁，请稍后再试。';
    if (status === 503 || status >= 500) return '业务系统暂时没有响应，请稍后再试。';
    return '这次没有成功，请稍后再试。';
  }

  // ---------- 登录 ----------

  function showAuth() {
    const shade = $('auth-shade');
    if (!shade) return;
    shade.classList.add('open');
    shade.setAttribute('aria-hidden', 'false');
    const user = $('login-username');
    if (user) setTimeout(function () { user.focus(); }, 40);
  }

  function hideAuth() {
    const shade = $('auth-shade');
    if (!shade) return;
    shade.classList.remove('open');
    shade.setAttribute('aria-hidden', 'true');
    const pw = $('login-password');
    if (pw) pw.value = '';
    const err = $('login-error');
    if (err) { err.hidden = true; err.textContent = ''; }
  }

  function resetSession() {
    stopNotifications();
    S.me = null;
    S.role = '';
    S.capabilities = {};
    S.suggestions = [];
    S.today = null;
    S.todayError = null;
    S.pending = [];
    S.waybills = [];
    S.fees = [];
    S.receipts = [];
    S.rev = 0;
    S.updated = '';
    S.lastStateOk = false;
    S.conversationId = '';
    S.notifications = [];
    S.view = 'brief';
    S.detail = null;
    S.detailFrom = 'brief';
    badgeEl = null;
    clear($('left'));
    clear($('right'));
    clear($('scene'));
    const title = $('title');
    if (title) title.textContent = '工作空间';
    const org = $('orgname');
    if (org) org.textContent = ORG;
    const chips = $('chips');
    if (chips) clear(chips);
  }

  function bindLogin() {
    const form = $('login-form');
    if (!form) return;
    form.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      const username = ($('login-username').value || '').trim();
      const password = $('login-password').value || '';
      const errBox = $('login-error');
      const submit = $('login-submit');
      errBox.hidden = true;
      errBox.textContent = '';
      if (!username || !password) {
        errBox.textContent = '账号和密码都要填。';
        errBox.hidden = false;
        return;
      }
      submit.disabled = true;
      submit.textContent = '正在登录…';
      try {
        await window.JLApi.login(username, password);
        $('login-password').value = '';
        await boot();
      } catch (err) {
        errBox.textContent = err.status === 401
          ? '账号或密码不对，请重新输入。'
          : err.status === 429
            ? '试错次数太多，请一分钟后再试。'
            : err.status === 0
              ? '网络没有响应，请稍后再试。'
              : '登录没有成功，请稍后再试。';
        errBox.hidden = false;
        $('login-password').value = '';
      } finally {
        submit.disabled = false;
        submit.textContent = '登录';
      }
    });
  }

  // ---------- 启动与取数 ----------

  async function boot() {
    try {
      const data = await window.JLApi.bootstrap();
      S.me = data.user || {};
      S.role = S.me.role || '';
      S.capabilities = data.capabilities || {};
      S.suggestions = Array.isArray(data.suggestions) ? data.suggestions : [];
      hideAuth();
      applyIdentity();
      await refreshAll();
      renderBrief();
      startNotifications();
    } catch (err) {
      if (err.status === 401) {
        resetSession();
        showAuth();
        return;
      }
      hideAuth();
      applyIdentity();
      renderOffline(err);
    }
  }

  function applyIdentity() {
    const org = $('orgname');
    if (org) org.textContent = ORG;
    const agent = $('agent-label');
    if (agent) agent.textContent = '企业智能体';
  }

  async function refreshAll() {
    let authErr = null;
    try {
      const today = await window.JLApi.today();
      S.today = today && typeof today === 'object' ? today : null;
      S.pending = today && Array.isArray(today.pending_state) ? today.pending_state : [];
      S.todayError = null;
    } catch (err) {
      if (err.status === 401) authErr = err;
      S.todayError = err;
    }
    try {
      const result = await window.JLApi.state();
      applyState(result);
    } catch (err) {
      if (err.status === 401) authErr = err;
      S.lastStateOk = false;
    }
    if (authErr) throw authErr;
    return S.lastStateOk;
  }

  function applyState(result) {
    const raw = result && result.state && typeof result.state === 'object' ? result.state : {};
    S.rev = Number(result && result.rev) || 0;
    S.updated = (result && result.updated) || '';
    S.waybills = parseList(raw['julong_demo_waybills_v2']);
    S.receipts = parseList(raw['julong_demo_receipts_v1']);
    S.fees = canFinance() ? parseList(raw['julong_demo_fees_v1']) : [];
    S.lastStateOk = true;
  }

  function candidates() {
    const routine = S.today && S.today.routine;
    const list = routine && Array.isArray(routine.routine_candidates) ? routine.routine_candidates : [];
    return list.filter(function (c) { return c && typeof c === 'object'; });
  }

  function byRoutine() {
    const routine = S.today && S.today.routine;
    const raw = routine && routine.by_routine && typeof routine.by_routine === 'object' ? routine.by_routine : {};
    return {
      fee: Number(raw['对账差异']) || 0,
      receipt: Number(raw['回单催办']) || 0
    };
  }

  function waybillOf(number) {
    const key = String(number === undefined || number === null ? '' : number);
    for (let i = 0; i < S.waybills.length; i++) {
      if (String(S.waybills[i].number) === key) return S.waybills[i];
    }
    return null;
  }

  function feesOf(number) {
    if (!canFinance()) return [];
    const key = String(number === undefined || number === null ? '' : number);
    return S.fees.filter(function (f) { return String(f.number) === key; });
  }

  function feeTotal(number) {
    if (!canFinance()) return null;
    const list = feesOf(number).filter(function (f) { return f.status !== '作废'; });
    if (!list.length) return null;
    let total = 0;
    let has = false;
    list.forEach(function (f) {
      const n = Number(String(f.amount === undefined || f.amount === null ? '' : f.amount).replace(/[,，\s¥]/g, ''));
      if (isFinite(n)) { total += n; has = true; }
    });
    return has ? total : null;
  }

  // ---------- 外壳：左侧 / 右侧 ----------

  function setTitle(text) {
    const title = $('title');
    if (title) title.textContent = text;
  }

  function paint() {
    renderLeft();
    renderRight();
  }

  function navButton(label, iconName, view, active) {
    const button = make('button', active ? 'active' : '');
    button.append(iconNode(iconName), make('span', '', label));
    button.onclick = function () { openView(view); };
    return button;
  }

  function renderLeft() {
    const left = $('left');
    if (!left) return;
    clear(left);

    const brand = make('div', 'brand');
    const logo = make('span', 'logo');
    logo.innerHTML = ICONS.brand;
    brand.append(logo, make('span', '', ORG));

    const badge = make('span', '');
    badge.id = 'notif-badge';
    badge.style.cssText = 'margin-left:auto;min-width:16px;height:16px;padding:0 4px;border-radius:8px;background:#b95861;color:#ffffff;font-size:10px;line-height:16px;text-align:center;display:none;cursor:pointer;flex-shrink:0;';
    badge.title = '工作提醒';
    badge.onclick = openNotifications;
    brand.append(badge);
    left.append(brand);
    badgeEl = badge;

    const newBtn = make('button', 'new');
    newBtn.innerHTML = ICONS.plus;
    newBtn.append(make('span', '', '新建工作'));
    newBtn.onclick = function () { go('new'); };
    left.append(newBtn);

    left.append(make('div', 'navlabel', '工作记录'));
    left.append(navButton('今日业务简报', 'brief', 'brief', S.view === 'brief'));
    left.append(navButton('回单跟进', 'file', 'receipts', S.view === 'receipts'));
    if (canFinance()) left.append(navButton('费用核查', 'money', 'fees', S.view === 'fees' || S.view === 'feecheck'));

    left.append(make('div', 'navlabel', '企业空间'));
    left.append(navButton('业务数据', 'data', 'data', S.view === 'data'));
    left.append(navButton('持续任务', 'task', 'tasks', S.view === 'tasks'));

    const profile = make('div', 'profile');
    const name = displayName();
    const initial = Array.from(name)[0] || '·';
    profile.append(make('div', 'avatar', initial));
    const box = make('div');
    box.style.minWidth = '0';
    const nameLine = make('div', '', name);
    nameLine.style.cssText = 'font-size:11px;color:#d5d6e1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
    const roleLine = make('div', '', roleName());
    roleLine.style.cssText = 'font-size:9.5px;color:#7d7f8e;margin-top:2px;';
    box.append(nameLine, roleLine);
    profile.append(box);
    left.append(profile);

    updateBadge();
  }

  function taskLabel() {
    const map = {
      brief: '今日业务简报',
      receipts: '回单跟进',
      fees: '费用核查',
      feecheck: '费用确认',
      detail: '事项详情',
      data: '业务数据',
      tasks: '持续任务',
      new: '新建工作'
    };
    return map[S.view] || '工作空间';
  }

  function capabilityRow(iconName, label, note, onClick, offline) {
    const row = make('div', 'tool');
    row.append(iconNode(iconName));
    const box = make('div');
    box.style.minWidth = '0';
    box.append(make('span', '', label), make('small', '', note));
    row.append(box, make('span', offline ? 'dot off' : 'dot'));
    if (onClick && !offline) {
      row.style.cursor = 'pointer';
      row.onclick = onClick;
    }
    return row;
  }

  function factBlock(label, body) {
    const box = make('div', 'factblock');
    box.append(make('div', 'factblock-title', label));
    if (typeof body === 'string') {
      box.append(make('div', 'factblock-text', body));
    } else if (body) {
      box.append(body);
    }
    return box;
  }

  // 三组读数各占一行：图标、标签、数字三列对齐，数字等宽。
  function businessFacts() {
    const list = make('div', 'facts-list');
    if (!S.lastStateOk) {
      list.append(make('div', 'facts-empty', '这次没读到业务数据'));
      return list;
    }
    const rows = [
      ['brief', S.role === 'employee' ? '经手运单' : '运单', S.waybills.length, '票'],
      ['file', '回单', S.receipts.length, '份']
    ];
    if (canFinance()) rows.push(['money', '费用', S.fees.length, '笔']);
    rows.forEach(function (row) {
      const item = make('div', 'factrow');
      item.append(iconNode(row[0]));
      item.append(make('span', 'factrow-label', row[1]));
      const value = make('span', 'factrow-value');
      value.append(make('span', 'factrow-num', row[2]), make('span', 'factrow-unit', row[3]));
      item.append(value);
      list.append(item);
    });
    return list;
  }

  function ruleFacts() {
    if (canFinance()) return '涉钱改动要再输一次登录密码；已确认的费用只能作废，作废要写理由。';
    return '费用和金额只对授权岗位开放，当前账号看不到。';
  }

  function renderRight() {
    const right = $('right');
    if (!right) return;
    clear(right);
    right.append(make('h3', '', '工作上下文'));

    const module = make('div', 'module');
    module.append(iconNode('grid'), make('span', '', taskLabel()));
    right.append(module);

    // 工作上下文的三段：当前任务在上面，业务数据与规则在这里，都是真实读数。
    right.append(factBlock('企业业务数据', businessFacts()));
    right.append(factBlock('企业规则', ruleFacts()));

    const tabs = make('div', 'tabs');
    const workTab = make('button', S.rightTab === 'work' ? 'active' : '', '持续工作');
    const skillTab = make('button', S.rightTab === 'skills' ? 'active' : '', '能力');
    workTab.onclick = function () { S.rightTab = 'work'; renderRight(); };
    skillTab.onclick = function () { S.rightTab = 'skills'; renderRight(); };
    tabs.append(workTab, skillTab);
    right.append(tabs);

    if (S.rightTab === 'work') {
      if (!S.pending.length) {
        right.append(make('div', 'group', '暂时没有持续任务'));
      } else {
        right.append(make('div', 'group', '机器记录'));
        S.pending.forEach(function (item) {
          const row = make('div', 'tool');
          row.append(iconNode('clock'));
          const box = make('div');
          box.style.minWidth = '0';
          box.append(make('span', '', item.subject || '持续任务'));
          if (item.progress) box.append(make('small', '', item.progress));
          if (item.next_step) box.append(make('small', '', '下一步：' + item.next_step));
          row.append(box, make('span', item.status === 'done' ? 'dot off' : 'dot'));
          right.append(row);
        });
      }
    } else {
      right.append(capabilityRow('search', '运单查询', '按单号、收货人、到站查找', function () {
        prefill('帮我查一下运单 ');
      }, false));
      right.append(capabilityRow('file', '回单管理', '查看待催回单', function () {
        openView('receipts');
      }, false));
      if (canFinance()) {
        right.append(capabilityRow('money', '财务核查', '核对费用与差异', function () {
          openView('fees');
        }, false));
      }
      right.append(capabilityRow('clock', '定时巡检', '让智能体按时扫账', function () {
        prefill('每天早上八点帮我扫一遍账，有异常就提醒我');
      }, false));
      right.append(capabilityRow('bell', '微信通知', '暂未接入', null, true));
    }

    const foot = make('div', 'foot');
    foot.append(make('div', '', S.lastStateOk ? '已连接业务系统' : '未连接业务系统'));
    if (S.lastStateOk) foot.append(make('div', '', '账本第 ' + (S.rev || 0) + ' 版 · ' + shortTime(S.updated)));
    right.append(foot);
  }

  function prefill(text) {
    const input = $('prompt');
    if (!input) return;
    input.value = text;
    input.focus();
  }

  // ---------- 视图调度 ----------

  function openView(view) {
    if (view === 'fees' && !canFinance()) {
      toast('费用核查不在你的账号范围里。');
      return;
    }
    if (view === 'brief') renderBrief();
    else if (view === 'receipts') renderReceipts();
    else if (view === 'fees') renderFees();
    else if (view === 'data') renderData();
    else if (view === 'tasks') renderTasks();
    else if (view === 'new') go('new');
    else renderBrief();
  }

  function go(view) {
    if (view === 'new') {
      S.view = 'new';
      S.conversationId = '';
      setTitle('新建工作');
      const scene = clear($('scene'));
      const glass = make('div', 'glass');
      glass.append(make('h2', '', '开始一件新工作'));
      glass.append(make('p', '', '把要办的事说清楚：查哪一票、核哪笔费用、催哪份回单。也可以从下面的常用指令开始。'));
      scene.append(glass);
      scene.append(make('p', 'note', '交代之后，回答会留在这块工作区里，之前的记录不会被覆盖。'));
      paint();
      renderChips();
      prefill('');
      return;
    }
    openView(view);
  }

  function numBox(value, label) {
    const box = make('div');
    box.append(make('strong', '', value), make('small', '', label));
    return box;
  }

  function emptyCard(title, text) {
    const glass = make('div', 'glass');
    glass.append(make('h2', '', title), make('p', '', text));
    return glass;
  }

  function conclusion(by) {
    const fee = by.fee;
    const receipt = by.receipt;
    const routine = S.today && S.today.routine;
    const coverage = routine && typeof routine.coverage === 'string' ? routine.coverage : '';
    if (!fee && !receipt) {
      if (coverage.indexOf('incomplete') !== -1) return '巡检跑完了，但有一部分数据没接上，暂时不能当作没有待办。';
      return '业务巡检已完成。今天没有需要盯的事。';
    }
    let text = '业务巡检已完成。';
    if (fee) text += '发现 ' + fee + ' 项费用异常';
    if (fee && receipt) text += '，';
    if (receipt) text += (fee ? '' : '有 ') + receipt + ' 笔回单待收';
    return text + '。';
  }

  function candidateItem(c) {
    const item = make('button', 'item');
    const icon = make('div', 'itemicon');
    icon.innerHTML = c.routine === '对账差异' ? ICONS.money : ICONS.file;
    const box = make('div');
    box.style.cssText = 'flex:1;min-width:0;';
    box.append(make('b', '', c.kind || c.routine || '工作事项'));
    box.append(make('small', '', c.evidence || ''));
    item.append(icon, box);
    const total = feeTotal(c.waybill_number);
    if (total !== null) item.append(make('span', 'amount', fmtMoney(total)));
    item.append(make('span', 'arrow', '›'));
    item.onclick = function () { renderDetail(c); };
    return item;
  }

  // ---------- 今日简报 ----------

  function renderBrief() {
    S.view = 'brief';
    setTitle('今日业务简报');
    const scene = clear($('scene'));
    scene.append(make('p', 'time', dayLine()));

    if (!S.today) {
      const glass = make('div', 'glass');
      glass.append(make('h2', '', '今天的简报暂时取不到'));
      glass.append(make('p', '', S.todayError && S.todayError.status === 0
        ? '网络没有连上业务系统，稍后再试一次。'
        : '业务系统这次没有返回今天的巡检结果，稍后再试一次。'));
      const retry = make('button', 'primary', '重新读取');
      retry.onclick = async function () {
        retry.disabled = true;
        retry.textContent = '正在读取…';
        try {
          await refreshAll();
          renderBrief();
        } catch (err) {
          if (err.status === 401) { resetSession(); showAuth(); return; }
          toast(humanError(err));
          retry.disabled = false;
          retry.textContent = '重新读取';
        }
      };
      glass.append(retry);
      scene.append(glass);
      paint();
      return;
    }

    const by = byRoutine();
    const list = candidates();

    const glass = make('div', 'glass');
    glass.append(make('h2', '', '今日业务简报'));
    glass.append(make('p', '', conclusion(by)));
    const numbers = make('div', 'numbers');
    if (canFinance()) {
      numbers.append(numBox(S.waybills.length, '运单数'));
      numbers.append(numBox(by.fee, '费用异常'));
    }
    numbers.append(numBox(by.receipt, '待收回单'));
    glass.append(numbers);
    scene.append(glass);

    scene.append(make('div', 'itemslabel', '工作事项 · 共 ' + list.length + ' 件'));
    if (!list.length) {
      scene.append(emptyCard('今天没有待盯的事', '巡检已经跑完，暂时不用处理什么。明天会再扫一遍。'));
    } else {
      list.forEach(function (c) { scene.append(candidateItem(c)); });
    }

    const routine = S.today.routine || {};
    if (routine.note) scene.append(make('p', 'note', routine.note));
    const waiting = S.pending.filter(function (x) { return x && x.status !== 'done'; })[0];
    if (waiting && waiting.progress) {
      scene.append(make('p', 'note', (waiting.subject || '机器记录') + '：' + waiting.progress + (waiting.next_step ? '。下一步，' + waiting.next_step + '。' : '')));
    }

    paint();
    renderChips();
  }

  function renderReceipts() {
    S.view = 'receipts';
    setTitle('回单跟进');
    const scene = clear($('scene'));
    scene.append(make('p', 'time', dayLine()));
    const list = candidates().filter(function (c) { return c.routine === '回单催办'; });

    const glass = make('div', 'glass');
    glass.append(make('h2', '', '回单跟进'));
    glass.append(make('p', '', list.length
      ? '现在还有 ' + list.length + ' 笔回单没有落实。点开一条，可以让企业智能体安排跟进。'
      : '今天的回单都落实了，没有需要催的。'));
    scene.append(glass);

    if (list.length) {
      scene.append(make('div', 'itemslabel', '待催回单 · 共 ' + list.length + ' 件'));
      list.forEach(function (c) { scene.append(candidateItem(c)); });
      scene.append(make('p', 'note', '只显示本人可以查看的运单。回单收齐后，这条会自动从名单里消失。'));
    }
    paint();
  }

  function renderFees() {
    if (!canFinance()) {
      toast('费用核查不在你的账号范围里。');
      renderBrief();
      return;
    }
    S.view = 'fees';
    setTitle('费用核查');
    const scene = clear($('scene'));
    scene.append(make('p', 'time', dayLine()));
    const list = candidates().filter(function (c) { return c.routine === '对账差异'; });

    const glass = make('div', 'glass');
    glass.append(make('h2', '', '费用核查'));
    glass.append(make('p', '', list.length
      ? '有 ' + list.length + ' 项费用需要核对。点开一条可以查看这笔运单的费用记录。'
      : '今天没有发现费用异常。'));
    scene.append(glass);

    if (list.length) {
      scene.append(make('div', 'itemslabel', '待核对事项 · 共 ' + list.length + ' 件'));
      list.forEach(function (c) { scene.append(candidateItem(c)); });
      scene.append(make('p', 'note', '涉钱的改动不会自动生效：先出确认记录，再重新输入密码确认，才会写入账本。'));
    }
    paint();
  }

  function renderData() {
    S.view = 'data';
    setTitle('业务数据');
    const scene = clear($('scene'));
    scene.append(make('p', 'time', dayLine()));

    const glass = make('div', 'glass');
    glass.append(make('h2', '', '企业业务数据'));
    glass.append(make('p', '', '下面是业务系统里的当前记录，只读展示。'));
    const numbers = make('div', 'numbers');
    if (canFinance()) {
      numbers.append(numBox(S.waybills.length, '运单'));
      numbers.append(numBox(S.receipts.length, '回单登记'));
      numbers.append(numBox(S.fees.length, '费用记录'));
    } else {
      numbers.append(numBox(S.receipts.length, '回单登记'));
      numbers.append(numBox(byRoutine().receipt, '待收回单'));
    }
    glass.append(numbers);
    scene.append(glass);

    scene.append(make('p', 'note', S.lastStateOk
      ? '账本第 ' + (S.rev || 0) + ' 版，更新于 ' + (shortStamp(S.updated) || '刚刚') + '。费用和金额只对授权岗位可见。'
      : '这次没有读到业务系统，看到的可能不是最新状态。'));
    paint();
  }

  function renderTasks() {
    S.view = 'tasks';
    setTitle('持续任务');
    const scene = clear($('scene'));
    scene.append(make('p', 'time', dayLine()));

    const glass = make('div', 'glass');
    glass.append(make('h2', '', '持续任务'));
    glass.append(make('p', '', S.pending.length
      ? '这些是企业智能体自己记下的长期任务，做到哪一步、下一步做什么，都在这里。'
      : '还没有持续任务。让智能体按时扫账之后，这里会出现机器记录。'));
    scene.append(glass);

    if (S.pending.length) {
      scene.append(make('div', 'itemslabel', '机器记录 · 共 ' + S.pending.length + ' 件'));
      S.pending.forEach(function (item) {
        const row = make('div', 'item');
        const icon = make('div', 'itemicon');
        icon.innerHTML = ICONS.clock;
        const box = make('div');
        box.style.cssText = 'flex:1;min-width:0;';
        box.append(make('b', '', item.subject || '持续任务'));
        if (item.progress) box.append(make('small', '', item.progress));
        if (item.next_step) box.append(make('small', '', '下一步：' + item.next_step));
        const status = make('span', '');
        status.style.cssText = 'font-size:10px;color:#a8a9bb;white-space:nowrap;';
        status.textContent = item.status === 'done' ? '已完成' : item.status === 'cancelled' ? '已停止' : '等待中';
        row.append(icon, box, status);
        scene.append(row);
      });
    }
    paint();
  }

  // ---------- 事项详情 ----------

  function field(label, value) {
    const row = make('div', 'field');
    row.append(make('span', '', label), make('span', '', value === undefined || value === null || value === '' ? '—' : value));
    return row;
  }

  function renderDetail(c) {
    const from = S.view;
    if (from === 'brief' || from === 'receipts' || from === 'fees') S.detailFrom = from;
    S.view = 'detail';
    S.detail = c;
    const isFee = c.routine === '对账差异';
    setTitle(isFee ? '费用事项' : '回单事项');
    const scene = clear($('scene'));

    const backLabel = S.detailFrom === 'receipts' ? '回单跟进' : S.detailFrom === 'fees' ? '费用核查' : '今日简报';
    const back = make('button', 'back', '‹ 返回' + backLabel);
    back.onclick = function () {
      if (S.detailFrom === 'receipts') renderReceipts();
      else if (S.detailFrom === 'fees') renderFees();
      else renderBrief();
    };
    scene.append(back);
    scene.append(make('p', 'time', '运单 ' + (c.waybill_number || '—')));

    const wb = waybillOf(c.waybill_number) || {};
    const glass = make('div', 'glass');
    glass.append(make('h2', '', c.kind || (isFee ? '费用未落账' : '回单未收')));
    glass.append(make('p', '', c.evidence || ''));

    glass.append(field('运单号', c.waybill_number || '—'));
    if (isFee) {
      glass.append(field('相关方', wb.carrier || wb.receiver || '—'));
      glass.append(field('货物', wb.cargo || '—'));
      if (canFinance() && fmtMoney(wb.amount)) glass.append(field('运单登记金额', fmtMoney(wb.amount)));
    } else {
      glass.append(field('收货人', wb.receiver || wb.recipient || '—'));
      glass.append(field('到站', wb.destination || '—'));
      glass.append(field('回单类型', c.field === 'delivered' ? '厂家原件' : '纸质回单'));
      glass.append(field('货物', wb.cargo || '—'));
    }
    scene.append(glass);

    if (isFee) {
      if (canFinance()) {
        const enter = make('button', 'primary', '进入费用确认');
        enter.onclick = function () {
          openFees(c.waybill_number, function () { renderDetail(c); });
        };
        scene.append(enter);
        scene.append(make('p', 'note', '涉及费用的改动不会自动生效：先出确认记录，再重新输入登录密码确认，才会写入账本。'));
      } else {
        scene.append(make('p', 'note', '费用明细只对授权岗位可见，当前账号看不到这笔金额。'));
      }
    } else {
      const follow = make('button', 'primary', '安排跟进');
      follow.onclick = async function () {
        follow.disabled = true;
        follow.textContent = '正在交代…';
        const ok = await followup(c);
        follow.disabled = false;
        follow.textContent = '安排跟进';
        if (ok) toast('已经交给企业智能体跟进。', 3600);
      };
      scene.append(follow);
      scene.append(make('p', 'note', '跟进会记进这次的工作对话里，回单收齐后这条会自动从名单里消失。'));
    }
    paint();
  }

  async function followup(c) {
    const number = c.waybill_number || '';
    const label = c.field === 'delivered' ? '厂家原件' : '纸质回单';
    return submitPrompt('请帮我跟进运单 ' + number + ' 的' + label + '，安排催办，并把结果记下来。');
  }

  // ---------- 费用确认 ----------

  async function openFees(number, back) {
    if (!canFinance()) {
      toast('费用核查不在你的账号范围里。');
      return;
    }
    S.view = 'feecheck';
    setTitle('费用确认');
    const scene = clear($('scene'));
    scene.append(make('p', 'time', '费用核对 · 运单 ' + number));
    const loading = make('p', 'note', '正在从业务系统读取这笔运单的费用…');
    scene.append(loading);
    paint();
    try {
      await refreshAll();
    } catch (err) {
      if (err.status === 401) { resetSession(); showAuth(); return; }
    }
    if (!S.lastStateOk) {
      loading.textContent = '账本这次没有读到，请返回后再试。';
      return;
    }
    renderFeeList(number, back);
  }

  function renderFeeList(number, back) {
    S.view = 'feecheck';
    setTitle('费用确认');
    const scene = clear($('scene'));
    const goBack = make('button', 'back', '‹ 返回');
    goBack.onclick = function () { if (typeof back === 'function') back(); else renderBrief(); };
    scene.append(goBack);
    scene.append(make('p', 'time', '费用核对 · 运单 ' + (number || '—')));

    const list = feesOf(number);
    const glass = make('div', 'glass');
    glass.append(make('h2', '', '运单 ' + (number || '—') + ' 的费用'));
    glass.append(make('p', '', list.length
      ? '每一笔费用都要先核对，再重新输入登录密码。确认之后才会写入账本。'
      : '这笔运单暂时没有登记费用记录。'));
    scene.append(glass);

    if (list.length) {
      scene.append(make('div', 'itemslabel', '费用明细 · 共 ' + list.length + ' 笔'));
      list.forEach(function (fee) { scene.append(feeCard(fee, number, back)); });
      scene.append(make('p', 'note', '每笔改动都会留下记录：谁、什么时候、改前改后。已确认的费用只能作废，不能直接改回其他状态。'));
    }
    paint();
  }

  function feeCard(fee, number, back) {
    const card = make('div', 'glass confirm-box');
    card.append(make('h2', '', fee.type || '费用'));
    const amount = fmtMoney(fee.amount);
    card.append(field('金额', amount || '在确认页核对'));
    card.append(field('当前状态', fee.status || '待确认'));
    if (fee.partner) card.append(field('合作方', fee.partner));
    if (fee.confirmBy) card.append(field('上次确认', fee.confirmBy + (fee.confirmAt ? ' · ' + shortStamp(fee.confirmAt) : '')));

    if (fee.status === '作废') {
      card.append(make('p', 'note', '这笔费用已经作废，不能再修改。'));
      return card;
    }

    const actions = make('div', 'actions');
    if (fee.status === '已确认') {
      // 账本规则：已确认的费用只能作废，不能改回其他状态。
      const voidBtn = make('button', 'danger-btn', '作废');
      voidBtn.onclick = function () { openFeeModal(fee, number, '作废', back); };
      actions.append(voidBtn);
    } else {
      const ok = make('button', 'primary', '确认');
      ok.onclick = function () { openFeeModal(fee, number, '已确认', back); };
      actions.append(ok);
      if (fee.status !== '有差异') {
        const diff = make('button', 'ghost-btn', '标记有差异');
        diff.onclick = function () { openFeeModal(fee, number, '有差异', back); };
        actions.append(diff);
      }
    }
    card.append(actions);
    return card;
  }

  function buildOverlay() {
    const shade = make('div', 'auth-shade open');
    shade.setAttribute('aria-hidden', 'false');
    const panel = make('form', 'auth-panel confirm-box');
    panel.noValidate = true;
    shade.append(panel);
    document.body.append(shade);
    function close() {
      document.removeEventListener('keydown', onKey);
      if (shade.parentNode) shade.parentNode.removeChild(shade);
    }
    function onKey(ev) {
      if (ev.key === 'Escape') close();
    }
    document.addEventListener('keydown', onKey);
    shade.addEventListener('click', function (ev) {
      if (ev.target === shade) close();
    });
    return { shade: shade, panel: panel, close: close };
  }

  function openFeeModal(fee, number, target, back) {
    if (!canFinance()) {
      toast('费用操作不在你的账号范围里。');
      return;
    }
    const overlay = buildOverlay();
    const panel = overlay.panel;
    panel.dataset.step = '1';

    panel.append(make('div', 'auth-label', '费用确认'));
    panel.append(make('h2', '', '请核对这笔费用'));
    const amount = fmtMoney(fee.amount);
    panel.append(make('p', 'auth-sub',
      '运单 ' + (number || '—') + ' · ' + (fee.type || '费用') + (amount ? ' · ' + amount : '') +
      '。将把状态改为“' + target + '”。请重新输入登录密码。'));
    panel.append(field('当前状态', fee.status || '待确认'));
    panel.append(field('改为', target));

    let reasonInput = null;
    if (target === '作废') {
      reasonInput = make('input', 'reason-input');
      reasonInput.type = 'text';
      reasonInput.placeholder = '写一句作废理由（必填，最多 300 字）';
      reasonInput.maxLength = 300;
      panel.append(reasonInput);
    }

    panel.append(make('label', '', '登录密码'));
    const pw = make('input', '');
    pw.type = 'password';
    pw.autocomplete = 'current-password';
    panel.append(pw);

    const errBox = make('div', 'auth-error');
    errBox.hidden = true;
    panel.append(errBox);

    const actions = make('div', 'actions');
    const cancel = make('button', 'ghost-btn', '取消');
    cancel.type = 'button';
    cancel.onclick = overlay.close;
    const next = make('button', 'primary', '下一步');
    next.type = 'submit';
    actions.append(cancel, next);
    panel.append(actions);

    let candidateId = '';

    panel.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      if (panel.dataset.step !== '1') return;
      errBox.hidden = true;

      const password = pw.value || '';
      if (!candidateId && !password) {
        errBox.textContent = '请输入登录密码。';
        errBox.hidden = false;
        return;
      }
      if (target === '作废' && reasonInput && !reasonInput.value.trim()) {
        errBox.textContent = '作废要写清楚理由。';
        errBox.hidden = false;
        return;
      }

      next.disabled = true;
      cancel.disabled = true;
      next.textContent = '正在生成确认记录…';
      try {
        if (!candidateId) {
          const created = await window.JLApi.createFeeCandidate({
            password: password,
            fee_id: fee.id,
            status: target,
            reason: reasonInput ? reasonInput.value.trim() : ''
          });
          candidateId = created && created.candidate_id ? created.candidate_id : '';
          if (!candidateId) throw new Error('missing_candidate');
        }
        pw.value = '';
        const review = await window.JLApi.reviewCandidate(candidateId);
        renderReviewStep(panel, review, overlay.close, back, number);
      } catch (err) {
        pw.value = '';
        errBox.textContent = humanError(err, 'fee');
        errBox.hidden = false;
        next.disabled = false;
        cancel.disabled = false;
        next.textContent = '下一步';
      }
    });
  }

  function renderReviewStep(panel, review, close, back, number) {
    panel.dataset.step = '2';
    clear(panel);

    const fee = review && review.fee && typeof review.fee === 'object' ? review.fee : {};
    const target = (review && review.target_status) || '—';
    const stale = !!(review && review.stale);
    const completed = !!(review && review.completed);

    panel.append(make('div', 'auth-label', '费用确认 · 第二步'));
    panel.append(make('h2', '', '请再确认一次'));
    panel.append(make('p', 'auth-sub', '确认后会立即写入账本，并留下操作记录。'));

    panel.append(field('运单号', fee.number || number || '—'));
    panel.append(field('费用类型', fee.type || '—'));
    panel.append(field('金额', fmtMoney(fee.amount) || '未填写'));
    panel.append(field('当前状态', fee.status || '—'));
    panel.append(field('将改为', target));
    if (review && review.reason) panel.append(field('作废理由', review.reason));

    if (stale) {
      const warn = make('div', 'stale-warn', '这本账在你核对之后已经变了，确认记录已过期。请关闭后重新核对，不能直接写入。');
      panel.append(warn);
    }
    if (completed) {
      panel.append(make('div', 'stale-warn', '这笔费用已经确认处理过了，不需要重复操作。'));
    }

    panel.append(make('label', '', '登录密码'));
    const pw = make('input', '');
    pw.type = 'password';
    pw.autocomplete = 'current-password';
    if (stale || completed) pw.disabled = true;
    panel.append(pw);

    const errBox = make('div', 'auth-error');
    errBox.hidden = true;
    panel.append(errBox);

    const actions = make('div', 'actions');
    const cancel = make('button', 'ghost-btn', '关闭');
    cancel.type = 'button';
    cancel.onclick = close;
    const confirm = make('button', 'primary', '确认写入');
    confirm.type = 'submit';
    if (stale || completed) confirm.disabled = true;
    actions.append(cancel, confirm);
    panel.append(actions);

    panel.addEventListener('submit', async function (ev) {
      ev.preventDefault();
      if (panel.dataset.step !== '2') return;
      if (stale || completed) return;
      errBox.hidden = true;
      const password = pw.value || '';
      if (!password) {
        errBox.textContent = '请输入登录密码。';
        errBox.hidden = false;
        return;
      }
      confirm.disabled = true;
      cancel.disabled = true;
      confirm.textContent = '正在写入账本…';
      try {
        await window.JLApi.confirmCandidate(review.candidate_id, password);
        pw.value = '';
        close();
        toast('已写入账本：运单 ' + (fee.number || number || '—') + ' 的' + (fee.type || '费用') + '已改为“' + target + '”。', 4200);
        await reloadAfterWrite(number, back);
      } catch (err) {
        pw.value = '';
        errBox.textContent = humanError(err, 'fee');
        errBox.hidden = false;
        confirm.disabled = false;
        cancel.disabled = false;
        confirm.textContent = '确认写入';
      }
    });
  }

  async function reloadAfterWrite(number, back) {
    let ok = false;
    try {
      ok = await refreshAll();
    } catch (err) {
      if (err.status === 401) { resetSession(); showAuth(); return; }
    }
    paint();
    if (ok) {
      renderFeeList(number, back);
    } else {
      toast('已经写入账本，但页面暂时没刷新，请返回后重新进入核对。', 4200);
    }
  }

  // ---------- 对话 ----------

  function scrollScene() {
    const scene = $('scene');
    if (scene) scene.scrollTop = scene.scrollHeight;
  }

  function appendReply(text, who) {
    const scene = $('scene');
    if (!scene) return;
    const line = make('div', 'reply', who ? who + '：' + text : text);
    scene.append(line);
    scrollScene();
  }

  async function sendChat(text) {
    const result = await window.JLApi.chat(text, S.conversationId || '');
    if (result && result.conversation_id) S.conversationId = result.conversation_id;
    const message = result && result.message && typeof result.message === 'object' ? result.message : {};
    return message.text || '这次没有新的回复。';
  }

  async function submitPrompt(text) {
    const form = document.querySelector('.composer');
    const button = form ? form.querySelector('.send') : null;
    if (button) { button.disabled = true; button.style.opacity = '.45'; }
    appendReply(text, '你');
    try {
      const reply = await sendChat(text);
      appendReply(reply, '企业智能体');
      return true;
    } catch (err) {
      if (err.status === 401) { resetSession(); showAuth(); return false; }
      toast(humanError(err, 'chat'), 4000);
      return false;
    } finally {
      if (button) { button.disabled = false; button.style.opacity = ''; }
    }
  }

  function ask(ev) {
    if (ev) ev.preventDefault();
    const input = $('prompt');
    if (!input) return;
    const text = (input.value || '').trim();
    const file = $('attach');
    const hasFile = !!(file && file.files && file.files.length);
    if (!text) {
      if (hasFile) toast('附件上传还没接通，先用一句话交代工作吧。');
      else input.focus();
      return;
    }
    if (hasFile) toast('附件上传还没接通，这次先发送文字。');
    input.value = '';
    submitPrompt(text);
  }

  function renderChips() {
    const chips = $('chips');
    if (!chips) return;
    clear(chips);
    if (!S.suggestions.length) return;
    S.suggestions.slice(0, 4).forEach(function (item) {
      const label = item && (item.label || item.prompt);
      if (!label) return;
      const button = make('button', '', label);
      button.type = 'button';
      button.onclick = function () {
        const text = (item.prompt || item.label || '').trim();
        if (text) submitPrompt(text);
      };
      chips.append(button);
    });
  }

  // ---------- 通知 ----------

  function updateBadge() {
    const badge = badgeEl || $('notif-badge');
    if (!badge) return;
    const count = S.notifications.length;
    badge.textContent = count > 99 ? '99+' : String(count);
    badge.style.display = count ? 'block' : 'none';
  }

  async function pollNotifications() {
    try {
      const data = await window.JLApi.notifications();
      S.notifications = data && Array.isArray(data.items) ? data.items : [];
      updateBadge();
    } catch (err) {
      if (err.status === 401) {
        resetSession();
        showAuth();
      }
    }
  }

  function startNotifications() {
    stopNotifications();
    pollNotifications();
    S.timer = setInterval(pollNotifications, 15000);
  }

  function stopNotifications() {
    if (S.timer) {
      clearInterval(S.timer);
      S.timer = 0;
    }
  }

  function openNotifications() {
    if (!S.notifications.length) {
      toast('现在没有新的工作提醒。');
      return;
    }
    const overlay = buildOverlay();
    const panel = overlay.panel;
    panel.append(make('div', 'auth-label', '工作提醒'));
    panel.append(make('h2', '', '有 ' + S.notifications.length + ' 条新提醒'));
    panel.append(make('p', 'auth-sub', '点一条就标记为已读。'));

    const list = make('div', '');
    list.style.cssText = 'max-height:46vh;overflow-y:auto;';
    S.notifications.forEach(function (note) {
      const item = make('button', 'item');
      item.type = 'button';
      const icon = make('div', 'itemicon');
      icon.innerHTML = ICONS.bell;
      const box = make('div');
      box.style.cssText = 'flex:1;min-width:0;';
      box.append(make('b', '', note.text || '工作提醒'));
      box.append(make('small', '', shortStamp(note.created_at)));
      item.append(icon, box);
      item.onclick = async function () {
        try {
          await window.JLApi.seen(note.id);
          S.notifications = S.notifications.filter(function (x) { return x.id !== note.id; });
          updateBadge();
          overlay.close();
          toast('这条提醒已读。');
        } catch (err) {
          toast(humanError(err));
        }
      };
      list.append(item);
    });
    panel.append(list);

    const actions = make('div', 'actions');
    const close = make('button', 'ghost-btn', '关闭');
    close.type = 'button';
    close.onclick = overlay.close;
    actions.append(close);
    panel.append(actions);
  }

  // ---------- 收尾 ----------

  function renderOffline(err) {
    paint();
    S.view = 'brief';
    setTitle('工作空间');
    const scene = clear($('scene'));
    scene.append(make('p', 'time', dayLine()));
    const glass = make('div', 'glass');
    glass.append(make('h2', '', '工作台暂时连不上业务系统'));
    glass.append(make('p', '', err && err.status === 0
      ? '网络没有连上，请检查网络后再试。'
      : '业务系统这次没有响应，稍后再试一次。'));
    const retry = make('button', 'primary', '重新连接');
    retry.onclick = async function () {
      retry.disabled = true;
      retry.textContent = '正在连接…';
      await boot();
    };
    glass.append(retry);
    scene.append(glass);
  }

  function pane(side) {
    const win = $('win');
    if (!win) return;
    const target = side === 'left' ? 'showleft' : 'showright';
    const isOn = win.classList.contains(target);
    win.classList.remove('showleft', 'showright');
    if (!isOn) win.classList.add(target);
  }

  function init() {
    const scene = $('scene');
    if (scene) {
      // 遮罩层以外不动样式表；这里只让中栏内容在固定高度里滚动。
      scene.style.flex = '1 1 auto';
      scene.style.minHeight = '0';
      scene.style.overflowY = 'auto';
    }
    window.pane = pane;
    window.go = go;
    window.ask = ask;
    bindLogin();
    showAuth();
    boot();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
