/* 巨龙工作台 · 服务端共用账适配层
   只在服务端返回页面时临时注入，磁盘上的离线原型 v9 一个字节不改。
   作用：把原来「各存各浏览器」的 4 个本地键，接到服务器同一份账上。 */
(function () {
  'use strict';
  var KEYS = ['julong_demo_waybills_v2', 'julong_demo_recipients_v2',
              'julong_demo_dispatch_v5', 'julong_demo_month_v1',
              'julong_demo_fees_v1', 'julong_demo_receipts_v1',
              'julong_demo_bills_v1', 'julong_demo_diffs_v1', 'julong_demo_delivery_v1',
              'julong_demo_ai_v1'];
  var boot = window.__JL_CLOUD || { me: '', share: '' };
  var SHARE = boot.share || '';
  var ME = boot.me || '未署名';
  var mem = {}, rev = 0, updated = '', myRev = -1, offline = false, conflict = false;
  var pollTimer = null;

  function url(p) {
    var q = [];
    if (SHARE) q.push('sharecode=' + encodeURIComponent(SHARE));
    q.push('_=' + Date.now());
    return p + (p.indexOf('?') >= 0 ? '&' : '?') + q.join('&');
  }
  function xhr(method, path, body, sync) {
    var x = new XMLHttpRequest();
    // XHR.open 第三参数是 async：sync=true 表示要同步等待，必须传 false，
    // 否则拿不到服务器回执里的新版本号，后续写入会全部被判成冲突。
    x.open(method, url(path), !sync);
    if (body) { x.setRequestHeader('Content-Type', 'application/json'); x.setRequestHeader('X-JL-Client', '1'); }
    try { x.send(body || null); } catch (e) { return { status: 0, text: '' }; }
    return { status: x.status, text: x.responseText || '' };
  }
  function parse(t) { try { return JSON.parse(t); } catch (e) { return {}; } }

  /* 首屏账由服务器随页面一起递出，打开即有；只有没带账时才回退去取一次 */
  function pull() {
    if (boot.state) {
      mem = boot.state; rev = boot.rev || 0; updated = boot.updated || ''; myRev = rev;
      for (var i = 0; i < KEYS.length; i++) if (mem[KEYS[i]] == null) mem[KEYS[i]] = '[]';
      return;
    }
    var r = xhr('GET', '/api/state', null, true);
    if (r.status !== 200) { r = xhr('GET', '/api/state', null, true); }
    if (r.status !== 200) { offline = true; return; }
    var d = parse(r.text);
    mem = d.state || {}; rev = d.rev || 0; updated = d.updated || ''; myRev = rev;
    for (var i = 0; i < KEYS.length; i++) if (mem[KEYS[i]] == null) mem[KEYS[i]] = '[]';
  }

  /* 保存动作：把整份账推回服务器，带版本号防两人互相盖掉 */
  function push() {
    var body = JSON.stringify({ base_rev: rev, state: mem });
    var r = xhr('POST', '/api/state', body, true);
    if (r.status === 200) {
      var d = parse(r.text); rev = d.rev || rev; myRev = rev; updated = d.updated || updated;
      offline = false; conflict = false; stamp(); return;
    }
    if (r.status === 409) {
      var c = parse(r.text);
      if(c.error && c.error!=='revision_conflict') {
        var words={duplicate_fee:'这笔费用已经登记过了，本次没有重复保存。',confirmed_fee_requires_reversal:'这笔费用已经确认。更正时请先作废原记录，再登记正确的一笔。',confirmed_decision_is_immutable:'原来的确认记录会保留，请补一条新的说明。',financial_confirmation_forbidden:'这项费用需要财务授权人确认。'};
        conflict=true;banner(words[c.error]||'这次修改没有保存，请核对后再试。',true);throw new Error('业务校验未通过，本次没有保存');
      }
      conflict = true; banner('这一票没保存上：另一位同事刚改过账。草稿已保留，请刷新核对后再保存。', true); throw new Error('revision_conflict');
    }
    if (r.status === 401) { banner('登录已过期，草稿未保存，请重新登录。', true); throw new Error('authentication_required'); }
    offline = true; banner('服务器没连上，本次没有保存；草稿已保留。', true); throw new Error('save_failed');
  }

  /* 劫持 localStorage：原业务脚本照旧同步读写，完全感知不到底下换了地方 */
  var P = window.Storage && Storage.prototype;
  if (P) {
    var rawGet = P.getItem, rawSet = P.setItem, rawRemove = P.removeItem;
    P.getItem = function (k) {
      if (KEYS.indexOf(k) >= 0) return Object.prototype.hasOwnProperty.call(mem, k) ? mem[k] : null;
      return rawGet.apply(this, arguments);
    };
    P.setItem = function (k, v) {
      if (KEYS.indexOf(k) >= 0) { var previous=mem[k];mem[k] = String(v);try{push()}catch(err){mem[k]=previous;throw err}return; }
      return rawSet.apply(this, arguments);
    };
    P.removeItem = function (k) {
      if (KEYS.indexOf(k) >= 0) { var previous=mem[k];mem[k] = '[]';try{push()}catch(err){mem[k]=previous;throw err}return; }
      return rawRemove.apply(this, arguments);
    };
  }
  pull();

  /* ---------- 界面上极轻的一层：谁在改、同步到几点、出事怎么说 ---------- */
  function style() {
    var s = document.createElement('style');
    s.textContent = '.jl-cloud-only{font:500 11px/1.4 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}'
      + '.jl-tag{color:#3d6b52;background:#eaf3ed;border:1px solid #cfe4d6;border-radius:3px;padding:3px 7px;white-space:nowrap}'
      + '.jl-btn{background:none;border:0;border-bottom:1px solid #9fb3c6;color:#1f5c9e;padding:2px 0;cursor:pointer;font-size:11px}'
      + '.jl-banner{position:fixed;left:50%;top:12px;transform:translateX(-50%);z-index:9999;max-width:92vw;'
      + 'padding:10px 16px;border-radius:6px;background:#fff5f2;border:1px solid #e3b7ae;color:#9d3327;'
      + 'box-shadow:0 6px 20px rgba(20,40,60,.14);font-size:12px;cursor:pointer;display:none}'
      + '.jl-banner.info{background:#f0f5fb;border-color:#c6d8ea;color:#1f5c9e}'
      + '.jl-panel{position:fixed;right:0;top:0;bottom:0;width:min(430px,92vw);background:#fff;z-index:9998;'
      + 'border-left:1px solid #dce3ea;box-shadow:-10px 0 30px rgba(20,40,60,.12);display:none;flex-direction:column}'
      + '.jl-panel.open{display:flex}.jl-panel h3{font-size:15px;margin:0;padding:18px 20px 12px;border-bottom:1px solid #e6ebf0}'
      + '.jl-panel .jl-list{flex:1;overflow:auto;padding:8px 20px 20px}'
      + '.jl-log{padding:10px 0;border-bottom:1px solid #eef2f6;font-size:12px;line-height:1.6}'
      + '.jl-log b{font-weight:650}.jl-log span{color:#6b7a89;font-size:11px}'
      + '.jl-panel .jl-foot{padding:14px 20px;border-top:1px solid #e6ebf0;font-size:11px;color:#6b7a89}'
      + '@media print{.jl-cloud-only,.jl-banner,.jl-panel{display:none!important}}';
    document.head.appendChild(s);
  }
  function banner(text, bad) {
    var b = document.getElementById('jl-banner');
    if (!b) return;
    b.textContent = text + (bad ? '（点此刷新）' : '（点此刷新）');
    b.className = 'jl-banner' + (bad ? '' : ' info');
    b.style.display = 'block';
    b.onclick = function () { location.href = '/' + (SHARE ? '?sharecode=' + encodeURIComponent(SHARE) : ''); };
  }
  function stamp() {
    var t = document.getElementById('jl-sync');
    if (t) t.textContent = '共用账 · ' + (offline ? '未连上服务器' : '已同步 ' + (updated ? updated.slice(11, 16) : '刚刚'));
  }
  function panel() {
    var p = document.createElement('div');
    p.className = 'jl-panel jl-cloud-only'; p.id = 'jl-log-panel';
    p.innerHTML = '<h3>操作流水 <span style="font-weight:400;color:#6b7a89;font-size:11px">谁改了这份账</span></h3>'
      + '<div class="jl-list" id="jl-log-list"><div style="padding:16px 0;color:#6b7a89">读取中…</div></div>'
      + '<div class="jl-foot">只追加、不修改。试跑环境数据保存在服务器上，正式版需另接数据库与备份。</div>';
    document.body.appendChild(p);
    p.addEventListener('click', function (e) { if (e.target === p) p.classList.remove('open'); });
  }
  function openLog() {
    var p = document.getElementById('jl-log-panel'), box = document.getElementById('jl-log-list');
    if (!p) return;
    p.classList.add('open');
    var r = xhr('GET', '/api/log', null, true);
    var items = (parse(r.text).items) || [];
    if (!items.length) { box.innerHTML = '<div style="padding:16px 0;color:#6b7a89">还没有人改过这份账。</div>'; return; }
    box.innerHTML = items.map(function (x) {
      return '<div class="jl-log"><b>' + esc(x.who) + '</b> · ' + esc(x.action)
        + '<br><span>' + esc(x.time) + ' · 账上 ' + (x.count || 0) + ' 票 · ' + esc(x.ip || '') + '</span></div>';
    }).join('');
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

  function typing() {
    var ids = ['number', 'receiver', 'phone', 'address', 'destination', 'cargo', 'qty', 'weight', 'freightTotal', 'carrier'];
    for (var i = 0; i < ids.length; i++) {
      var e = document.getElementById(ids[i]);
      if (e && e.value && String(e.value).trim()) return true;
    }
    return false;
  }
  function poll() {
    if (document.hidden || conflict) return;
    var x = new XMLHttpRequest();
    x.open('GET', url('/api/state?brief=1'), true);
    x.timeout = 5000;
    x.onloadend = function () {
      if (x.status === 401) { banner('登录已过期，点这里重新打开链接。', true); return; }
      if (x.status !== 200) { offline = true; stamp(); return; }
      var d = parse(x.responseText);
      if (offline) { offline = false; stamp(); }
      if (d.rev > rev && !d.byMe) {
        if (!typing()) { location.reload(); return; }
        conflict = true;
        banner('同事刚更新了这份账，你正在录入，点这里刷新后再继续。', false);
      }
      stamp();
    };
    try { x.send(); } catch (e) { offline = true; stamp(); }
  }

  document.addEventListener('DOMContentLoaded', function () {
    style();
    var b = document.createElement('div');
    b.id = 'jl-banner'; b.className = 'jl-banner'; document.body.appendChild(b);
    var bar = document.querySelector('.top-status');
    if (bar) {
      bar.innerHTML = '<span class="jl-tag jl-cloud-only" id="jl-sync">共用账</span>'
        + '<span class="jl-tag jl-cloud-only">当前：' + esc(ME) + '</span>'
        + '<button type="button" class="jl-btn jl-cloud-only" id="jl-log-btn">操作流水</button>'
        + '<a class="jl-btn jl-cloud-only" href="/logout' + (SHARE ? '?sharecode=' + encodeURIComponent(SHARE) : '') + '" style="text-decoration:none">退出</a>';
      var btn = document.getElementById('jl-log-btn');
      if (btn) btn.onclick = openLog;
    }
    var t = document.title;
    if (t.indexOf('服务端试跑') < 0) document.title = t + ' · 服务端试跑（共用账）';
    stamp(); panel();
    pollTimer = setInterval(poll, 3000);
    window.addEventListener('beforeunload', function () { if (pollTimer) clearInterval(pollTimer); });
  });
})();
