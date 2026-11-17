const $=s=>document.querySelector(s),loginView=$('#login-view'),appView=$('#app-view');let activeRunId=null,selectedRunId=null,timer=null;
async function api(url,options={}){const response=await fetch(url,{credentials:'same-origin',...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});if(response.status===401&&url!=='/api/login'){showLogin();throw new Error('Session expired')}if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.detail||`Request failed (${response.status})`)}return response}
function showLogin(){clearInterval(timer);timer=null;activeRunId=null;selectedRunId=null;setRunning(false);loginView.classList.remove('hidden');appView.classList.add('hidden')}function showApp(){loginView.classList.add('hidden');appView.classList.remove('hidden');loadHistory();window.dispatchEvent(new Event("dashboard-authenticated"))}
async function checkSession(){try{await api('/api/me');showApp()}catch(_){showLogin()}}
$('#login-form').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget;$('#login-error').textContent='';const fields=Object.fromEntries(new FormData(form));try{await api('/api/login',{method:'POST',body:JSON.stringify(fields)});form.reset();showApp()}catch(error){$('#login-error').textContent=error.message}});
const _lo=$('#logout');if(_lo)_lo.addEventListener('click',async()=>{await api('/api/logout',{method:'POST'}).catch(()=>{});showLogin()});function setRunning(running){$('#start-run').disabled=running;$('#stop-run').disabled=!running}
$('#run-form').addEventListener('submit',async event=>{event.preventDefault();const runForm=event.currentTarget;const data=Object.fromEntries(new FormData(runForm));for(const key of ['min_amount','max_amount','max_clicks','clicks_per_minute','proxy'])if(!data[key]||!data[key].trim())data[key]=null;data.delay_ms=500;data.clicks_per_minute=null;if(data.max_clicks!==null)data.max_clicks=Number(data.max_clicks);const sT=document.querySelector('#stealth-mode-toggle');data.stealth_mode=sT?sT.checked:(data.stealth_mode!=='false');$('#run-message').textContent='Starting browser worker…';try{const response=await api('/api/runs',{method:'POST',body:JSON.stringify(data)}),run=await response.json();delete data.email;delete data.password;try{localStorage.setItem('pct_saved',JSON.stringify({site_url:runForm.elements.site_url.value,email:runForm.elements.email.value,min_amount:runForm.elements.min_amount.value,max_amount:runForm.elements.max_amount.value,proxy:runForm.elements.proxy?runForm.elements.proxy.value:''}))}catch(_){}resetStats();watchRun(run.id);loadHistory()}catch(error){$('#run-message').textContent=error.message;await loadHistory()}});
$('#stop-run').addEventListener('click',async()=>{if(!activeRunId)return;try{await api('/api/runs/'+activeRunId+'/stop',{method:'POST'});$('#run-message').textContent='Stop requested…'}catch(error){$('#run-message').textContent=error.message}});
function selectRun(id){selectedRunId=id;$('#export-link').href='/api/runs/'+id+'/export.csv';$('#export-link').classList.remove('disabled');refreshRun(id)}
function watchRun(id){if(activeRunId===id)return;clearInterval(timer);activeRunId=id;setRunning(true);selectRun(id);timer=setInterval(()=>refreshRun(id),1200)}
function resetStats(){['total','accepted','skipped','rate','failures'].forEach(key=>$(`#stat-${key}`).textContent='0');$('#failure-breakdown').textContent='No failure reasons';$('#stat-latency').textContent='0 / 0';$('#clicks-body').innerHTML='<tr><td class="empty" colspan="7">Connecting to browser worker…</td></tr>'}
function escapeHtml(value){return String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))}
function runStatusText(run) {
  return `${run.status.toUpperCase()} · LOGIN ${(run.login_status || 'pending').toUpperCase()}`;
}

function updateHistoryStatus(run) {
  const status = document.querySelector(`#history-list [data-history-run="${CSS.escape(run.id)}"] .run-status`);
  if (status) status.textContent = runStatusText(run);
}

async function refreshRun(id){try{const response=await api(`/api/runs/${id}`),{stats,clicks,run}=await response.json();
  updateHistoryStatus(run);if(id===activeRunId&&run.status!=='running'){clearInterval(timer);timer=null;activeRunId=null;setRunning(false);loadHistory()}if(id!==selectedRunId)return;$('#stat-total').textContent=stats.total_clicks;$('#stat-accepted').textContent=stats.accepted+stats.otp_reached;$('#stat-skipped').textContent=stats.skipped+stats.limit_reached;$('#stat-rate').textContent=stats.rate_limited||0;$('#stat-failures').textContent=stats.failures;$('#failure-breakdown').textContent=Object.entries(stats.failure_reasons).map(([reason,count])=>`${count}× ${reason}`).join(' · ')||'No failure reasons';$('#stat-latency').textContent=`${stats.avg_latency_ms} / ${stats.p95_latency_ms}`;$('#click-count').textContent=clicks.length;$('#clicks-body').innerHTML=clicks.length?clicks.map(click=>`<tr><td>${escapeHtml(new Date(click.timestamp).toLocaleTimeString())}</td><td>${escapeHtml(click.row_id)}</td><td>₹${escapeHtml(click.amount)}</td><td>${click.popup_shown?'YES':'NO'}</td><td><span class="outcome ${escapeHtml(click.outcome)}">${escapeHtml(click.outcome)}</span></td><td>${escapeHtml(click.latency_ms)} ms</td><td title="${escapeHtml(click.error_text||'')}">${escapeHtml(click.error_text||'—')}</td></tr>`).join(''):'<tr><td class="empty" colspan="7">Browser initializing — events will appear here.</td></tr>';$('#run-message').textContent=run.error_text?`${run.status} · ${run.error_text}`:`Run ${id} · ${run.status}`;}catch(_){}}
async function loadHistory(){try{const response=await api('/api/runs'),runs=await response.json();const running=runs.find(run=>run.status==='running');if(running)watchRun(running.id);$('#history-list').innerHTML=runs.length?runs.map(run=>`<div class="history-item" data-history-run="${escapeHtml(run.id)}"><span>${escapeHtml(new Date(run.started_at).toLocaleString())}</span><span>${escapeHtml(run.site_url)}</span><span class="run-status">${escapeHtml(runStatusText(run))}</span><span>${escapeHtml(run.id)}</span><button data-run="${escapeHtml(run.id)}">VIEW / CSV ↗</button></div>`).join(''):'<div class="empty">No runs yet.</div>';document.querySelectorAll('[data-run]').forEach(button=>button.addEventListener('click',()=>{selectRun(button.dataset.run);document.querySelector('.log-panel').scrollIntoView({behavior:'smooth'})}))}catch(_){}}
checkSession();

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
      } catch (_) {}
    });
    f.addEventListener('input',()=>{clearTimeout(t);t=setTimeout(save,400)});
  }
  init();
})();

;(function(){
  // ---- sound + vibration + notification when an order gets ACCEPTED ----
  let ctx=null,lastAcc=0,primed=false;
  const on=()=>{const c=document.querySelector('#sound-on');return !c||c.checked};
  function unlock(){ // browsers need one tap before audio/notifications work
    if(primed)return;primed=true;
    try{ctx=new (window.AudioContext||window.webkitAudioContext)();ctx.resume()}catch(_){}
    try{if('Notification' in window&&Notification.permission==='default')Notification.requestPermission()}catch(_){}
  }
  ['click','touchstart','keydown'].forEach(e=>document.addEventListener(e,unlock,{once:false,passive:true}));
  function beep(){
    try{ if(!ctx)return; const t=ctx.currentTime;
      [880,1175].forEach((f,i)=>{const o=ctx.createOscillator(),g=ctx.createGain();o.type='sine';o.frequency.value=f;
        g.gain.setValueAtTime(0.0001,t+i*0.16);g.gain.exponentialRampToValueAtTime(0.4,t+i*0.16+0.02);g.gain.exponentialRampToValueAtTime(0.0001,t+i*0.16+0.15);
        o.connect(g);g.connect(ctx.destination);o.start(t+i*0.16);o.stop(t+i*0.16+0.16)});
    }catch(_){}
  }
  function alertUser(n,total){
    if(!on())return;
    beep();
    try{navigator.vibrate&&navigator.vibrate([200,80,200])}catch(_){}
    try{if('Notification' in window&&Notification.permission==='granted')new Notification('Order accepted',{body:'Accepted so far: '+total})}catch(_){}
    document.title='✅ '+total+' accepted';
  }
  function poll(){
    const el=document.querySelector('#stat-accepted'); if(!el)return;
    const v=parseInt(el.textContent,10)||0;
    if(v>lastAcc&&lastAcc>=0&&primed!==null){ if(v>lastAcc)alertUser(v-lastAcc,v) }
    lastAcc=v;
  }
  setInterval(poll,700);
  // reset baseline when a new run starts so old counts don't ring
  document.addEventListener('click',e=>{if(e.target&&e.target.closest&&e.target.closest('#start-run'))lastAcc=0});
})();
