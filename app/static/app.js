const $=s=>document.querySelector(s),loginView=$('#login-view'),appView=$('#app-view');let activeRunId=null,selectedRunId=null,timer=null;
async function api(url,options={}){const response=await fetch(url,{credentials:'same-origin',...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});if(response.status===401&&url!=='/api/login'){showLogin();throw new Error('Session expired')}if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.detail||`Request failed (${response.status})`)}return response}
function showLogin(){clearInterval(timer);timer=null;activeRunId=null;selectedRunId=null;setRunning(false);loginView.classList.remove('hidden');appView.classList.add('hidden')}function showApp(){loginView.classList.add('hidden');appView.classList.remove('hidden');loadHistory();window.dispatchEvent(new Event("dashboard-authenticated"))}
async function checkSession(){try{await api('/api/me');showApp()}catch(_){showLogin()}}
$('#login-form').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget;$('#login-error').textContent='';const fields=Object.fromEntries(new FormData(form));try{await api('/api/login',{method:'POST',body:JSON.stringify(fields)});form.reset();showApp()}catch(error){$('#login-error').textContent=error.message}});
const _lo=$('#logout');if(_lo)_lo.addEventListener('click',async()=>{await api('/api/logout',{method:'POST'}).catch(()=>{});showLogin()});function setRunning(running){$('#start-run').disabled=running;$('#stop-run').disabled=!running}
$('#run-form').addEventListener('submit',async event=>{
  event.preventDefault();
  const runForm=event.currentTarget;
  const data=Object.fromEntries(new FormData(runForm));
  for(const key of ['min_amount','max_amount','max_clicks','clicks_per_minute','proxy'])if(!data[key]||!data[key].trim())data[key]=null;
  data.delay_ms=50;
  data.clicks_per_minute=null;
  if(data.max_clicks!==null)data.max_clicks=Number(data.max_clicks);
  const sT=document.querySelector('#stealth-mode-toggle');
  data.stealth_mode=sT?sT.checked:(data.stealth_mode!=='false');

  if(!data.email || !data.email.trim() || !data.password) {
    $('#run-message').textContent = '⚠ Target account email and password are required!';
    $('#run-message').style.color = '#ff8590';
    return;
  }
  $('#run-message').style.color = '';
  $('#run-message').textContent='Starting browser worker…';
  try{
    const response=await api('/api/runs',{method:'POST',body:JSON.stringify(data)}),run=await response.json();
    delete data.email;
    delete data.password;
    try{localStorage.setItem('pct_saved',JSON.stringify({site_url:runForm.elements.site_url.value,email:runForm.elements.email.value,min_amount:runForm.elements.min_amount.value,max_amount:runForm.elements.max_amount.value,proxy:runForm.elements.proxy?runForm.elements.proxy.value:''}))}catch(_){}
    resetStats();
    watchRun(run.id);
    loadHistory();
  }catch(error){
    if(error.message && error.message.includes('already active')) {
      $('#run-message').innerHTML = '<span style="color:#ff8590;">A run is already active. </span><button type="button" id="btn-force-stop" style="padding:3px 8px;font-size:11px;background:#55242d;color:#ff8590;border:1px solid #ff8590;border-radius:4px;cursor:pointer;font-weight:600;">Stop &amp; Restart</button>';
      const b = document.querySelector('#btn-force-stop');
      if (b) {
        b.onclick = async () => {
          b.disabled = true;
          b.textContent = 'Stopping…';
          await api('/api/runs/active/stop', {method:'POST'}).catch(()=>{});
          data.force = true;
          try {
            const resp = await api('/api/runs', {method:'POST', body:JSON.stringify(data)});
            const newRun = await resp.json();
            resetStats();
            watchRun(newRun.id);
            loadHistory();
          } catch(e) {
            $('#run-message').textContent = e.message;
            await loadHistory();
          }
        };
      }
    } else {
      $('#run-message').textContent=error.message;
    }
    await loadHistory();
  }
});
$('#stop-run').addEventListener('click',async()=>{
  try{
    if(activeRunId) {
      await api('/api/runs/'+activeRunId+'/stop',{method:'POST'}).catch(()=>{});
    }
    await api('/api/runs/active/stop',{method:'POST'}).catch(()=>{});
    $('#run-message').textContent='Stop requested…';
    activeRunId=null;
    setRunning(false);
    await loadHistory();
  }catch(error){
    $('#run-message').textContent=error.message;
  }
});
function selectRun(id){selectedRunId=id;$('#export-link').href='/api/runs/'+id+'/export.csv';$('#export-link').classList.remove('disabled');refreshRun(id)}
function watchRun(id){if(activeRunId===id)return;clearInterval(timer);activeRunId=id;setRunning(true);selectRun(id);timer=setInterval(()=>refreshRun(id),1200)}
function resetStats(){['total','accepted','skipped','rate','failures'].forEach(key=>$(`#stat-${key}`).textContent='0');$('#failure-breakdown').textContent='No failure reasons';$('#stat-latency').textContent='0 / 0';$('#clicks-body').innerHTML='<tr><td class="empty" colspan="7">Connecting to browser worker…</td></tr>'}
function escapeHtml(value){return String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))}
function runStatusText(run) {
  const loginTxt = (run.login_status || 'pending').toUpperCase().replace('_', ' ');
  return `${run.status.toUpperCase()} · LOGIN: ${loginTxt}`;
}

function updateHistoryStatus(run) {
  const status = document.querySelector(`#history-list [data-history-run="${CSS.escape(run.id)}"] .run-status`);
  if (status) status.textContent = runStatusText(run);
}

async function refreshRun(id){try{const response=await api(`/api/runs/${id}`),{stats,clicks,run}=await response.json();
  updateHistoryStatus(run);if(id===activeRunId&&run.status!=='running'){clearInterval(timer);timer=null;activeRunId=null;setRunning(false);loadHistory()}if(id!==selectedRunId)return;$('#stat-total').textContent=stats.total_clicks;$('#stat-accepted').textContent=stats.accepted+stats.otp_reached;$('#stat-skipped').textContent=stats.skipped+stats.limit_reached;$('#stat-rate').textContent=stats.rate_limited||0;$('#stat-failures').textContent=stats.failures;$('#failure-breakdown').textContent=Object.entries(stats.failure_reasons).map(([reason,count])=>`${count}× ${reason}`).join(' · ')||'No failure reasons';$('#stat-latency').textContent=`${stats.avg_latency_ms} / ${stats.p95_latency_ms}`;$('#click-count').textContent=clicks.length;$('#clicks-body').innerHTML=clicks.length?clicks.map(click=>{const isAcc=click.outcome==='ACCEPTED'||click.outcome==='OTP_REACHED';return `<tr class="${isAcc?'row-accepted':''}"><td>${escapeHtml(new Date(click.timestamp).toLocaleTimeString())}</td><td>${escapeHtml(click.row_id)}</td><td>₹${escapeHtml(click.amount)}</td><td>${click.popup_shown?'YES':'NO'}</td><td><span class="outcome ${escapeHtml(click.outcome)}">${escapeHtml(click.outcome)}</span></td><td>${escapeHtml(click.latency_ms)} ms</td><td title="${escapeHtml(click.error_text||'')}">${escapeHtml(click.error_text||'—')}</td></tr>`}).join(''):'<tr><td class="empty" colspan="7">Browser initializing — events will appear here.</td></tr>';const loginTxt=(run.login_status||'pending').toUpperCase().replace('_',' ');$('#run-message').textContent=run.error_text?`${run.status} · ${run.error_text}`:`Run ${id} (${run.status.toUpperCase()} · LOGIN: ${loginTxt})`;}catch(_){}}
async function loadHistory(){try{const response=await api('/api/runs'),runs=await response.json();const running=runs.find(run=>run.status==='running');if(running)watchRun(running.id);const hCount=$('#history-count');if(hCount)hCount.textContent=runs.length;$('#history-list').innerHTML=runs.length?runs.map(run=>`<div class="history-item" data-history-run="${escapeHtml(run.id)}"><span>${escapeHtml(new Date(run.started_at).toLocaleString())}</span><span>${escapeHtml(run.site_url)}</span><span class="run-status">${escapeHtml(runStatusText(run))}</span><span>${escapeHtml(run.id)}</span><button data-run="${escapeHtml(run.id)}">VIEW / CSV ↗</button></div>`).join(''):'<div class="empty">No runs yet.</div>';document.querySelectorAll('[data-run]').forEach(button=>button.addEventListener('click',()=>{selectRun(button.dataset.run);document.querySelector('.log-panel').scrollIntoView({behavior:'smooth'})}))}catch(_){}}
checkSession();

// Run history collapse/expand toggle
const btnToggleHistory = $('#btn-toggle-history');
const headToggleHistory = $('#history-toggle-head');
function toggleHistory() {
  const content = $('#history-content');
  const icon = $('#history-toggle-icon');
  const text = $('#history-toggle-text');
  if (!content) return;
  const isHidden = content.classList.toggle('hidden');
  if (icon) icon.textContent = isHidden ? '▼' : '▲';
  if (text) text.textContent = isHidden ? 'Expand History' : 'Collapse History';
}
if (btnToggleHistory) {
  btnToggleHistory.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleHistory();
  });
}
if (headToggleHistory) {
  headToggleHistory.addEventListener('click', toggleHistory);
}

// Tab switching
document.querySelectorAll('.nav-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const target = btn.dataset.tab;
    const consoleView = document.getElementById('console-view');
    const settingsView = document.getElementById('settings-view');
    if (consoleView) consoleView.classList.toggle('hidden', target !== 'console-view');
    if (settingsView) settingsView.classList.toggle('hidden', target !== 'settings-view');
  });
});

;(function(){
  const F=['site_url','email','password','min_amount','max_amount','proxy'];
  let t=null;
  function form(){return document.querySelector('#run-form')}
  const sToggle = document.querySelector('#stealth-mode-toggle');
  const sInput = document.querySelector('#stealth-mode-input');
  const sStatus = document.querySelector('#stealth-save-status');

  function updateStealthLabel(enabled) {
    if (!sStatus) return;
    sStatus.textContent = enabled ? '⌑ Stealth Protection Active' : '⚠ Stealth Protection Disabled';
    sStatus.style.color = enabled ? 'var(--mint)' : '#f5c86c';
  }

  function save(){
    const f=form();
    if(!f)return;
    const d={};
    F.forEach(k=>{if(f.elements[k])d[k]=f.elements[k].value});
    if (sToggle) d['stealth_mode'] = sToggle.checked;
    fetch('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).catch(()=>{});
  }

  function syncProxyUI(val) {
    const sel = document.querySelector('#proxy-select');
    const inp = document.querySelector('#proxy-input');
    if (!sel || !inp) return;
    inp.value = val || '';
    let found = false;
    for (let i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === val) {
        sel.selectedIndex = i;
        found = true;
        break;
      }
    }
    if (!found) {
      sel.value = val ? 'custom' : '';
    }
  }

  function init(){
    const f=form();
    if(!f)return setTimeout(init,300);
    const pSel = document.querySelector('#proxy-select');
    const pInp = document.querySelector('#proxy-input');
    if (pSel && pInp) {
      pSel.addEventListener('change', () => {
        if (pSel.value !== 'custom') {
          pInp.value = pSel.value;
          pInp.dispatchEvent(new Event('input', { bubbles: true }));
        }
      });
      pInp.addEventListener('input', () => {
        let matched = false;
        for (let i = 0; i < pSel.options.length; i++) {
          if (pSel.options[i].value === pInp.value) {
            pSel.selectedIndex = i;
            matched = true;
            break;
          }
        }
        if (!matched) pSel.value = pInp.value ? 'custom' : '';
      });
    }

    if (sToggle) {
      sToggle.addEventListener('change', () => {
        const checked = sToggle.checked;
        if (sInput) sInput.value = checked ? 'true' : 'false';
        updateStealthLabel(checked);
        save();
      });
    }

    // ---- Saved Accounts (Unlimited) ----
    let savedAccounts = [];
    const btnSaveAcc = document.querySelector('#btn-save-acc');
    const btnToggleSaved = document.querySelector('#btn-toggle-saved-acc');
    const btnCloseSaved = document.querySelector('#btn-close-saved-acc');
    const panelSaved = document.querySelector('#saved-acc-panel');
    const listSaved = document.querySelector('#saved-acc-list');
    const countSaved = document.querySelector('#saved-acc-count');
    const msgSaved = document.querySelector('#saved-acc-msg');

    function flashMsg(msg, isError) {
      if (!msgSaved) return;
      msgSaved.textContent = msg;
      msgSaved.style.color = isError ? '#ff8590' : 'var(--mint)';
      clearTimeout(msgSaved._t);
      msgSaved._t = setTimeout(() => {
        msgSaved.textContent = '';
      }, 3500);
    }

    function renderSavedAccounts() {
      if (countSaved) countSaved.textContent = savedAccounts.length;
      if (!listSaved) return;
      if (!savedAccounts.length) {
        listSaved.innerHTML = '<div style="font-size:11px;color:var(--muted);padding:8px 0;">No accounts saved yet. Enter email and password above and click "Save Account".</div>';
        return;
      }
      listSaved.innerHTML = savedAccounts.map((acc, idx) => {
        const safeEmail = (acc.email || '').replace(/"/g, '&quot;');
        return `
          <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:6px;gap:8px;">
            <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;">
              <b style="color:#ffffff;font-size:12px;display:block;overflow:hidden;text-overflow:ellipsis;" title="${safeEmail}">${safeEmail}</b>
              <span style="color:var(--muted);font-size:10px;font-family:var(--mono);">Password: ••••••••</span>
            </div>
            <div style="display:flex;gap:6px;flex-shrink:0;">
              <button type="button" class="btn-autofill-acc" data-idx="${idx}" style="padding:5px 10px;font-size:11px;font-weight:600;border-radius:5px;background:var(--mint);color:#042116;border:none;cursor:pointer;" title="Autofill this account into the form">📋 Autofill</button>
              <button type="button" class="btn-copy-acc" data-idx="${idx}" style="padding:5px 8px;font-size:11px;border-radius:5px;background:rgba(255,255,255,0.08);color:#fff;border:1px solid rgba(255,255,255,0.18);cursor:pointer;" title="Copy email & password">📄 Copy</button>
              <button type="button" class="btn-del-acc" data-idx="${idx}" style="padding:5px 8px;font-size:11px;border-radius:5px;background:#2a161b;color:#ff8590;border:1px solid #55242d;cursor:pointer;" title="Delete this account">🗑</button>
            </div>
          </div>
        `;
      }).join('');
    }

    function persistAccounts() {
      try {
        localStorage.setItem('pct_saved_accounts', JSON.stringify(savedAccounts));
      } catch (_) {}
      fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ saved_accounts: savedAccounts })
      }).catch(() => {});
    }

    // Preload from localStorage if available
    try {
      const local = localStorage.getItem('pct_saved_accounts');
      if (local) {
        const parsed = JSON.parse(local);
        if (Array.isArray(parsed)) {
          savedAccounts = parsed;
          renderSavedAccounts();
        }
      }
    } catch (_) {}

    if (btnSaveAcc) {
      btnSaveAcc.addEventListener('click', () => {
        const emailEl = f.elements['email'];
        const passEl = f.elements['password'];
        const email = emailEl ? emailEl.value.trim() : '';
        const password = passEl ? passEl.value : '';
        if (!email || !password) {
          flashMsg('⚠ Enter email & password above before saving', true);
          return;
        }
        const existingIdx = savedAccounts.findIndex(a => a.email.toLowerCase() === email.toLowerCase());
        if (existingIdx >= 0) {
          savedAccounts[existingIdx].password = password;
          flashMsg(`✓ Password updated for ${email}`);
        } else {
          savedAccounts.unshift({ email, password });
          flashMsg(`✓ Account saved: ${email}`);
        }
        persistAccounts();
        renderSavedAccounts();
        if (panelSaved && panelSaved.classList.contains('hidden')) {
          panelSaved.classList.remove('hidden');
        }
      });
    }

    if (btnToggleSaved) {
      btnToggleSaved.addEventListener('click', () => {
        if (panelSaved) panelSaved.classList.toggle('hidden');
      });
    }

    if (btnCloseSaved) {
      btnCloseSaved.addEventListener('click', () => {
        if (panelSaved) panelSaved.classList.add('hidden');
      });
    }

    if (listSaved) {
      listSaved.addEventListener('click', (e) => {
        const autofillBtn = e.target.closest('.btn-autofill-acc');
        const copyBtn = e.target.closest('.btn-copy-acc');
        const delBtn = e.target.closest('.btn-del-acc');

        if (autofillBtn) {
          const idx = parseInt(autofillBtn.dataset.idx, 10);
          const acc = savedAccounts[idx];
          if (acc) {
            if (f.elements['email']) f.elements['email'].value = acc.email;
            if (f.elements['password']) f.elements['password'].value = acc.password;
            f.dispatchEvent(new Event('input', { bubbles: true }));
            flashMsg(`✓ Autofilled ${acc.email}!`);
          }
        } else if (copyBtn) {
          const idx = parseInt(copyBtn.dataset.idx, 10);
          const acc = savedAccounts[idx];
          if (acc) {
            navigator.clipboard.writeText(`${acc.email}\t${acc.password}`).then(() => {
              flashMsg(`✓ Copied credentials for ${acc.email}`);
            }).catch(() => {
              flashMsg(`Email: ${acc.email}`);
            });
          }
        } else if (delBtn) {
          const idx = parseInt(delBtn.dataset.idx, 10);
          if (idx >= 0 && idx < savedAccounts.length) {
            const removed = savedAccounts.splice(idx, 1);
            persistAccounts();
            renderSavedAccounts();
            flashMsg(`Removed ${removed[0]?.email || 'account'}`);
          }
        }
      });
    }

    window.addEventListener('dashboard-authenticated',async()=>{
      try {
        const response = await fetch('/api/settings');
        if (!response.ok) return;
        const settings = await response.json();
        for (const key of F) {
          const field = f.elements[key];
          if (!field || typeof settings[key] !== 'string') continue;
          if (key === 'site_url' && !settings[key].trim()) continue;
          field.value = settings[key];
        }
        syncProxyUI(settings['proxy'] || '');
        if (typeof settings['stealth_mode'] === 'boolean') {
          if (sToggle) sToggle.checked = settings['stealth_mode'];
          if (sInput) sInput.value = settings['stealth_mode'] ? 'true' : 'false';
          updateStealthLabel(settings['stealth_mode']);
        }
        if (Array.isArray(settings['saved_accounts']) && settings['saved_accounts'].length > 0) {
          savedAccounts = settings['saved_accounts'];
          try { localStorage.setItem('pct_saved_accounts', JSON.stringify(savedAccounts)); } catch (_) {}
          renderSavedAccounts();
        } else if (savedAccounts.length > 0) {
          persistAccounts();
        }
      } catch (_) {}
    });
    f.addEventListener('input',()=>{clearTimeout(t);t=setTimeout(save,400)});
  }
  init();
})();

;(function(){
  // ---- Mobile ServiceWorker + Notification + Audio + Vibration engine ----
  let ctx=null,lastAcc=0,primed=false,swReg=null;
  const on=()=>{const c=document.querySelector('#sound-on');return !c||c.checked};

  // Register service worker (mandatory for Android notifications)
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').then(reg => {
      swReg = reg;
    }).catch(err => console.log('SW reg error', err));
  }

  function getAudioCtx() {
    if (!ctx) {
      try {
        ctx = new (window.AudioContext || window.webkitAudioContext)();
      } catch (_) {}
    }
    if (ctx && ctx.state === 'suspended') {
      ctx.resume().catch(() => {});
    }
    return ctx;
  }

  function unlock() {
    if (primed) return;
    primed = true;
    getAudioCtx();
  }
  ['click', 'touchstart', 'keydown'].forEach(e => document.addEventListener(e, unlock, { once: false, passive: true }));

  function beep() {
    try {
      const audio = getAudioCtx();
      if (!audio) return;
      const t = audio.currentTime;
      [880, 1175, 1400].forEach((f, i) => {
        const o = audio.createOscillator(), g = audio.createGain();
        o.type = 'sine';
        o.frequency.value = f;
        g.gain.setValueAtTime(0.001, t + i * 0.14);
        g.gain.exponentialRampToValueAtTime(0.6, t + i * 0.14 + 0.02);
        g.gain.exponentialRampToValueAtTime(0.001, t + i * 0.14 + 0.13);
        o.connect(g);
        g.connect(audio.destination);
        o.start(t + i * 0.14);
        o.stop(t + i * 0.14 + 0.14);
      });
    } catch (_) {}
  }

  function updatePermUI() {
    const s = document.querySelector('#notif-perm-status');
    const b1 = document.querySelector('#btn-enable-notif');
    const b2 = document.querySelector('#btn-request-perm');
    if (!('Notification' in window)) {
      if (s) { s.textContent = 'Not Supported'; s.style.color = '#f5c86c'; }
      if (b1) b1.style.display = 'none';
      if (b2) { b2.textContent = 'Notifications Not Supported'; b2.disabled = true; }
      return;
    }
    const perm = Notification.permission;
    if (perm === 'granted') {
      if (s) { s.textContent = 'Active (Allowed) ✓'; s.style.color = 'var(--mint)'; }
      if (b1) { b1.textContent = '🔔 Alerts Active ✓'; b1.style.borderColor = 'var(--mint)'; }
      if (b2) { b2.textContent = 'Notifications Active ✓'; b2.disabled = true; b2.style.opacity = '0.7'; }
    } else if (perm === 'denied') {
      if (s) { s.textContent = 'Blocked in Browser Settings'; s.style.color = 'var(--red)'; }
      if (b1) { b1.textContent = '⚠ Alerts Blocked'; b1.style.borderColor = 'var(--red)'; b1.style.color = 'var(--red)'; }
      if (b2) { b2.textContent = 'Blocked by Browser'; b2.disabled = true; }
    } else {
      if (s) { s.textContent = 'Permission Required'; s.style.color = '#f5c86c'; }
      if (b1) { b1.textContent = '🔔 Enable Phone Alerts'; }
      if (b2) { b2.textContent = '🔔 Enable Notifications'; b2.disabled = false; }
    }
  }

  async function requestPermission() {
    getAudioCtx();
    if (!('Notification' in window)) {
      alert('This mobile browser does not support Web Notifications.');
      return;
    }
    try {
      const perm = await Notification.requestPermission();
      updatePermUI();
      if (perm === 'granted') {
        beep();
        showNotification('🔔 Notifications Enabled!', 'You will be alerted immediately when an order is accepted.');
      }
    } catch (e) {
      console.log('Perm error', e);
    }
  }

  function showNotification(title, body) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    const options = {
      body: body,
      tag: 'payout-accept-' + Date.now(),
      renotify: true,
      vibrate: [300, 100, 300, 100, 300]
    };
    if (swReg && swReg.showNotification) {
      swReg.showNotification(title, options).catch(() => {
        try { new Notification(title, options); } catch (_) {}
      });
    } else if (navigator.serviceWorker && navigator.serviceWorker.ready) {
      navigator.serviceWorker.ready.then(reg => reg.showNotification(title, options)).catch(() => {
        try { new Notification(title, options); } catch (_) {}
      });
    } else {
      try { new Notification(title, options); } catch (_) {}
    }
  }

  function alertUser(n, total) {
    if (!on()) return;
    beep();
    try { navigator.vibrate && navigator.vibrate([300, 120, 300, 120, 400]); } catch (_) {}
    showNotification('✅ Order ACCEPTED!', 'Total accepted so far: ' + total);
    document.title = '✅ ' + total + ' accepted';
  }

  function poll() {
    const el = document.querySelector('#stat-accepted');
    if (!el) return;
    const v = parseInt(el.textContent, 10) || 0;
    if (v > lastAcc && lastAcc >= 0 && primed !== null) {
      alertUser(v - lastAcc, v);
    }
    lastAcc = v;
  }
  setInterval(poll, 700);

  // Wire buttons
  function wireNotifButtons() {
    updatePermUI();
    const b1 = document.querySelector('#btn-enable-notif');
    const b2 = document.querySelector('#btn-request-perm');
    const bTest = document.querySelector('#btn-test-notif');
    if (b1) b1.addEventListener('click', requestPermission);
    if (b2) b2.addEventListener('click', requestPermission);
    if (bTest) bTest.addEventListener('click', () => {
      getAudioCtx();
      beep();
      try { navigator.vibrate && navigator.vibrate([300, 100, 300]); } catch (_) {}
      if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
        showNotification('⚡ Test Alert Working!', 'Your phone will ring, vibrate, and notify you when orders are accepted.');
      } else {
        requestPermission();
      }
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', wireNotifButtons);
  } else {
    wireNotifButtons();
  }

  // reset baseline when a new run starts so old counts don't ring
  document.addEventListener('click', e => {
    if (e.target && e.target.closest && e.target.closest('#start-run')) lastAcc = 0;
  });
})();
