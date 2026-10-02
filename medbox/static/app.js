/* 分药盒单页应用：药盒台 / 床旁端 / 医嘱 / 异常 / 流水 */
'use strict';

let D = null;                 // board 数据
let timer = null;

const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const SLOT = {breakfast:'早餐', lunch:'午餐', dinner:'晚餐'};
const STATUS = {packed:'待签发', signed:'已签发', sealed:'已封盒',
  handed_over:'已交接·床旁', completed:'已完成', void:'已作废'};

function toast(msg, ok){
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast ' + (ok ? 'ok' : 'err');
  t.style.display = 'block';
  clearTimeout(t._h);
  t._h = setTimeout(()=> t.style.display='none', ok ? 2200 : 4200);
}

async function api(path, body){
  const res = await fetch(path, {
    method:'POST', headers:{'Content-Type':'application/json','X-Actor': actor()},
    body: JSON.stringify(body || {})
  });
  const j = await res.json();
  if(!j.ok){ toast(j.error.message, false); throw j.error; }
  return j.data;
}
const call = (op, args) => api('/api/call', {op, args});

function actor(){ return $('#actor').value; }

function currentBoxes(){ return D.boxes.filter(b => b.status !== 'void'); }

/* ---------------- 渲染 ---------------- */
function render(){
  if(!D) return;
  $('#verBadge').textContent = `医嘱版本 v${D.version.version_no}（${D.version.status === 'current' ? '现行' : '已失效'}）`;
  renderActors();
  renderStation();
  renderBedsideSelect();
  renderBedside();
  renderOrders();
  renderResidents();
  renderExceptions();
  renderLogs();
}

function renderActors(){
  const sel = $('#actor');
  const prev = sel.value;
  sel.innerHTML = D.users.map(u =>
    `<option value="${u.id}">${esc(u.name)}（${{packer:'配药员',pharmacist:'药师',nurse:'护理员',physician:'医生'}[u.role]}）</option>`).join('');
  if([...sel.options].some(o=>o.value===prev)) sel.value=prev;
  else sel.value = 'u_wang';
  const role = D.users.find(u=>u.id===actor())?.role;
  $('#actorRole').textContent = role ?
    {packer:'配药：装盒', pharmacist:'复核签发 / 异常闭环', nurse:'交接 / 床旁执行', physician:'开立·停用医嘱'}[role] : '';
}

function itemStateBadge(it){
  if(it.state==='broken') return `<span class="state-broken">● 药片碎裂</span>`;
  if(it.state==='misplaced') return `<span class="state-misplaced">● 错放格位</span>`;
  if(it.state==='replaced') return `<span class="muted">已补换</span>`;
  return '';
}
function adminTag(it){
  if(!it.admin_result) return '';
  const m = {taken:'已签收', refused:'拒服', deferred:'暂缓'};
  let s = `<span class="tag t-${it.admin_result}">${m[it.admin_result]}</span>`;
  if(it.admin_reason) s += ` <span class="muted">${esc(it.admin_reason)}</span>`;
  return s;
}

function boxCard(b, mode){
  const stale = b.stale;
  const items = b.items.map(it => `
    <div class="cell">
      <div class="cell-num">${it.cell_no}</div>
      <div class="cell-body">
        <div class="med">${esc(it.med_name)}</div>
        <div class="spec">${esc(it.spec)} · ${esc(it.dose_text)} · 数量 ${it.quantity}</div>
        <div style="margin-top:3px">${itemStateBadge(it)} ${adminTag(it)}</div>
        ${mode==='station' ? stationCellActions(b,it) : ''}
      </div>
    </div>`).join('');
  const photo = b.sealed_photo ?
    `<img class="photo" src="/photos/${encodeURIComponent(b.sealed_photo.split('/').pop())}" title="封盒照片">` : '';
  return `
  <div class="pillbox ${stale && b.status!=='void' ? 'stale':''}">
    <div class="pb-head">
      <span class="bed">🛏 ${esc(b.bed_no)} ${esc(b.resident_name)}</span>
      <span class="slot">${b.meal_label}</span>
      <span class="tag t-${b.status}">${STATUS[b.status]}</span>
      <span class="ver">${b.serve_date} · v${b.version_no}${stale?' · ⛔旧版失效':''} · 盒#${b.id}</span>
    </div>
    ${stale && b.status!=='void' ? `<div style="padding:8px 12px"><div class="flag-high">医嘱版本失效：本盒为 v${b.version_no}，现行 v${D.version.version_no}，禁止继续使用，请作废后按新版本重装</div></div>`:''}
    ${items}
    <div class="pb-foot">
      ${mode==='station' ? stationFootActions(b) : ''}
      ${photo}
    </div>
  </div>`;
}

function stationCellActions(b,it){
  if(b.status!=='packed' || it.admin_result) return '';
  return `<div class="cell-actions">
    <button class="b-bad" onclick="askFlag(${it.id},'broken')">登记药片碎裂</button>
    <button class="b-bad" onclick="askFlag(${it.id},'misplaced')">登记错放格位</button>
    <button onclick="askResolve(${it.id})">补换 / 归位</button>
  </div>`;
}
function stationFootActions(b){
  const btns = [];
  if(b.status==='packed'){
    btns.push(`<button class="btn" style="padding:6px 12px" onclick="doSign(${b.id})">药师核对签发</button>`);
    btns.push(`<button class="btn sec" style="padding:6px 12px" onclick="askVoid(${b.id})">作废</button>`);
  }
  if(b.status==='signed')
    btns.push(`<button class="btn" style="padding:6px 12px" onclick="doSeal(${b.id})">封盒（拍照存本地）</button>`);
  if(b.status==='sealed' || b.status==='handed_over')
    btns.push(`<button class="btn sec" style="padding:6px 12px" onclick="askHandoff(${b.id})">床位交接给护理员</button>`);
  return btns.join(' ');
}

function renderStation(){
  const boxes = D.boxes.filter(b=>['packed','signed','sealed','handed_over'].includes(b.status));
  $('#stationGrid').innerHTML = boxes.length
    ? boxes.map(b=>boxCard(b,'station')).join('')
    : '<div class="muted">暂无在途药盒。</div>';
  // 装盒下拉
  const opts = D.residents.map(r=>
    `<option value="${r.id}">${esc(r.bed_no)} ${esc(r.name)}${r.away?'（外出中）':''}</option>`).join('');
  $('#packRes').innerHTML = opts;
  $('#newRes').innerHTML = opts;
  if(!$('#packDate').value) $('#packDate').value = D.date;
}

/* ---------------- 床旁端 ---------------- */
function renderBedsideSelect(){
  const boxes = D.boxes.filter(b=>b.status==='handed_over');
  const sel = $('#bedBoxSel');
  const prev = sel.value;
  sel.innerHTML = boxes.map(b=>
    `<option value="${b.id}">${esc(b.bed_no)} ${esc(b.resident_name)} · ${b.meal_label}（盒#${b.id}，剩 ${b.remaining}）</option>`).join('')
    || '<option value="">（暂无已交接药盒）</option>';
  if(prev && [...sel.options].some(o=>o.value===prev)) sel.value=prev;
}

function renderBedside(){
  const id = Number($('#bedBoxSel').value);
  const b = D.boxes.find(x=>x.id===id);
  const area = $('#bedsideArea');
  if(!b){ area.innerHTML = '<p class="muted">请选择已完成床位交接的药盒。</p>'; return; }
  const stale = b.stale;
  const resident = D.residents.find(r=>r.id===b.resident_id);
  const warnings = [];
  if(stale) warnings.push(['high',`该药盒医嘱已变更（盒 v${b.version_no}，现行 v${D.version.version_no}），本盒全部格位禁止执行，请作废后重装`]);
  if(resident?.away) warnings.push(['high',`住户外出未归：${esc(resident.away_reason||'')}（预计 ${esc(resident.expected_return||'未定')}），已阻断给药`]);

  const cells = b.items.map(it=>{
    const done = it.admin_result==='taken';
    const refused = it.admin_result==='refused';
    const deferred = it.admin_result==='deferred';
    const blocked = stale || resident?.away || it.state==='broken' || it.state==='misplaced' || done || refused;
    let note = '';
    if(done) note = `<div class="flag-warn" style="background:var(--green-bg);color:var(--green);border-color:#a8d8bf">已签收，重复确认将被阻断</div>`;
    if(refused) note = `<div class="flag-high">拒服已记录（终态），重复给药阻断；如需重发须药师按异常流程处置</div>`;
    if(deferred) note = `<div class="flag-warn">暂缓至 ${esc(it.until_time||'')}：${esc(it.admin_reason||'')}　<button class="btn sec" style="padding:3px 10px" onclick="askFollowup(${it.admin_id},${it.id})">暂缓跟进</button></div>`;
    return `
    <div style="border:1.5px solid var(--line);border-radius:14px;padding:14px;margin-top:12px;${done?'opacity:.85':''}">
      <div class="row">
        <div class="cell-num" style="width:42px;height:42px;font-size:18px">${it.cell_no}</div>
        <div>
          <div style="font-size:20px;font-weight:800">${esc(it.med_name)}</div>
          <div class="muted" style="font-size:14px">${esc(it.spec)} · ${esc(it.dose_text)}</div>
        </div>
        <span style="flex:1"></span>${adminTag(it)}
      </div>
      <div class="scanbox">
        <span class="muted">扫码核对（模拟）：</span>
        <input id="scan-${it.id}" style="flex:1;min-width:220px"
          value="MED:${esc(it.med_name)};${esc(it.spec)};B2026"
          placeholder="扫描药盒条码，例如 MED:硝苯地平控释片;30mg;B2026">
      </div>
      ${note}
      <div class="big-keys">
        <button class="big taken" ${blocked?'disabled':''} onclick="doAdmin(${it.id},'taken')">✔ 已签收</button>
        <button class="big refuse" ${blocked?'disabled':''} onclick="askAdmin(${it.id},'refused')">✘ 拒服</button>
        <button class="big defer" ${blocked?'disabled':''} onclick="askAdmin(${it.id},'deferred')">⏸ 暂缓</button>
      </div>
    </div>`;
  }).join('');

  area.innerHTML = `
    ${warnings.map(w=>`<div class="flag-${w[0]==='high'?'high':'warn'}" style="margin:10px 0">${w[1]}</div>`).join('')}
    <div class="bedside">
      <div class="row">
        <span class="remaining">本盒剩余未执行：<b>${b.remaining}</b> / ${b.items.length} 格</span>
        <span style="flex:1"></span>
        <span class="muted">${esc(b.bed_no)} ${esc(b.resident_name)} · ${b.serve_date} ${b.meal_label} · v${b.version_no}</span>
      </div>
    </div>
    ${cells}`;
}

/* ---------------- 医嘱 / 住户 ---------------- */
function renderOrders(){
  const active = D.orders.filter(o=>o.status==='active');
  $('#orderList').innerHTML = `<table><thead><tr>
    <th>选择</th><th>床位</th><th>住户</th><th>药名 / 规格</th><th>剂量</th><th>餐次</th><th>版本</th>
    </tr></thead><tbody>${active.map(o=>`
      <tr><td><input type="checkbox" class="stopck" value="${o.id}"></td>
      <td>${esc(o.bed_no)}</td><td>${esc(o.resident_name)}</td>
      <td><strong>${esc(o.med_name)}</strong> ${esc(o.spec)}</td>
      <td>${esc(o.dose_text)}</td><td>${SLOT[o.meal_slot]}</td>
      <td>v${o.version_no}</td></tr>`).join('')}</tbody></table>
      <p class="muted">勾选后点"停用"，系统自动产生下一医嘱版本；旧版本药盒即刻不能签发或执行。</p>`;
}
function renderResidents(){
  $('#residentList').innerHTML = D.residents.map(r=>`
    <div class="row" style="padding:8px 0;border-bottom:1px dashed var(--line)">
      <strong style="width:150px">🛏 ${esc(r.bed_no)} ${esc(r.name)}</strong>
      <span class="muted">过敏：${esc(r.allergies)||'无'}</span>
      <span style="flex:1"></span>
      ${r.away ? `<span class="away-pill">外出中：${esc(r.away_reason||'')}（返 ${esc(r.expected_return||'未定')}）</span>
        <button class="btn sec" onclick="doReturn('${r.id}')">登记返回</button>`
        : `<button class="btn ghost" onclick="askAway('${r.id}')">登记外出</button>`}
    </div>`).join('');
}

/* ---------------- 异常 / 日志 ---------------- */
function renderExceptions(){
  if(!D.exceptions.length){ $('#excList').innerHTML='<p class="muted">暂无异常。</p>'; return; }
  $('#excList').innerHTML = D.exceptions.map(e=>`
    <div class="exc ${e.status} ${e.level}">
      <strong>${esc(codeLabel(e.code))}</strong>
      ${e.status==='open'
        ? (e.level==='high' ? `<span class="flag-high" style="margin-left:8px">高风险待复核</span>`
                            : `<span class="flag-warn" style="margin-left:8px">待复核</span>`)
        : '<span class="tag t-taken" style="margin-left:8px">已闭环</span>'}
      <div style="margin-top:5px">${esc(e.detail)}</div>
      ${e.resolution?`<div class="meta">处置：${esc(e.resolution)}</div>`:''}
      <div class="meta">${e.resident_name?esc(e.resident_name)+' · ':''}盒#${e.box_id??'—'} · ${fmt(e.created_at)}</div>
      ${e.status==='open' ? `<div class="row" style="margin-top:7px">
        <input id="res-${e.id}" placeholder="复核处置说明（必填）" style="flex:1;min-width:240px">
        <button class="btn" onclick="doCloseExc(${e.id})">复核闭环</button></div>`:''}
    </div>`).join('');
}
function codeLabel(c){
  return {STALE_BOX:'旧版本药盒', BROKEN_PILL:'药片碎裂', MISPLACED_CELL:'错放格位',
    RESIDENT_AWAY:'住户外出', DUPLICATE_ADMIN:'重复给药', REFUSED:'拒服', DEFERRED:'暂缓',
    MISSED_SIGN:'漏签', CONTRAINDICATION:'禁忌', CONTENT_MISMATCH:'药盒内容不符',
    QTY_MISMATCH:'数量不符', BOX_VOID:'药盒作废'}[c] || c;
}
function renderLogs(){
  $('#notifList').innerHTML = D.notifications.length
    ? D.notifications.map(n=>`<div class="notif ${n.read?'':'unread'}"><span class="nkind">●</span>${esc(n.message)}<span class="meta" style="float:right">${fmt(n.created_at)}</span></div>`).join('')
    : '<p class="muted">暂无提醒。</p>';
  $('#eventList').innerHTML = D.events.map(v=>`
    <tr><td>${fmt(v.created_at)}</td><td>${esc(v.nurse_name||v.nurse_id||'系统')}</td>
    <td>${esc(v.action)}</td><td>${esc(v.detail||'')}</td></tr>`).join('');
}
const fmt = s => s ? s.replace('T',' ').replace('+00:00',' UTC') : '';

/* ---------------- 弹窗 ---------------- */
const dlg = $('#dlg');
function ask(title, fieldsHtml, onOk){
  $('#dlgTitle').textContent = title;
  $('#dlgBody').innerHTML = fieldsHtml;
  $('#dlgOk').onclick = async ()=>{
    try{ await onOk(); dlg.close(); await reload(true); }
    catch(e){ /* toast 已显示，保持弹窗 */ }
  };
  dlg.showModal();
}
const reasonInput = '<textarea id="dlgReason" placeholder="原因（必填）"></textarea>';
const untilInput = '<input id="dlgUntil" type="datetime-local" style="width:100%;margin-top:8px"><div class="muted">暂缓必须填写预计执行时间</div>';

function askFlag(itemId,state){
  ask(state==='broken'?'登记药片碎裂':'登记错放格位', reasonInput,
    ()=>call('flag_item',{item_id:itemId,state,note:$('#dlgReason').value}));
}
function askResolve(itemId){
  ask('补换 / 归位（异常处置闭环）',
    `<input id="dlgCell" type="number" min="1" placeholder="归位格位号（碎药补换可留空）" style="width:100%">
     ${reasonInput}<div class="muted">说明补换药品批号或归位核对结果</div>`,
    ()=>call('resolve_item',{item_id:itemId,
      new_cell_no:$('#dlgCell').value?Number($('#dlgCell').value):null,
      note:$('#dlgReason').value || '已补换/归位'}));
}
function askVoid(boxId){
  ask(`作废药盒 #${boxId}`, reasonInput +
    '<div class="flag-high">已签发后的药盒作废需药师操作，且不可恢复</div>',
    ()=>call('void_box',{box_id:boxId, reason:$('#dlgReason').value}));
}
function askHandoff(boxId){
  const nurses = D.users.filter(u=>u.role==='nurse');
  ask(`床位交接（盒 #${boxId}）`,
    `<select id="dlgNurse" style="width:100%">${nurses.map(n=>`<option value="${n.id}">${esc(n.name)}</option>`).join('')}</select>
     <input id="dlgNote" placeholder="交接备注（双人核对封签）" style="width:100%;margin-top:8px">`,
    ()=>call('handoff',{box_id:boxId,to_nurse_id:$('#dlgNurse').value,note:$('#dlgNote').value}));
}
function askAway(rid){
  ask('登记住户外出',
    reasonInput + '<input id="dlgRet" placeholder="预计返回时间" style="width:100%;margin-top:8px">',
    ()=>call('set_away',{resident_id:rid,away:true,
      reason:$('#dlgReason').value,expected_return:$('#dlgRet').value}));
}
function askAdmin(itemId,result){
  ask(result==='refused'?'记录拒服':'记录暂缓',
    reasonInput + (result==='deferred'?untilInput:''),
    ()=>{
      const until = $('#dlgUntil')?.value;
      const args={item_id:itemId,result,reason:$('#dlgReason').value,
        until_time: until ? until.replace('T',' ')+':00' : null,
        scan_code:null};
      return call('administer',args);
    });
}
function askFollowup(adminId,itemId){
  ask('暂缓到期跟进',
    `<select id="dlgFinal" style="width:100%"><option value="taken">已补服签收</option>
     <option value="refused">仍拒服</option></select>${reasonInput}
     <div class="muted">补服需重新扫码核对，默认取该格位扫码框读数。</div>`,
    ()=>call('followup_deferred',{admin_id:adminId,
      final_result:$('#dlgFinal').value, reason:$('#dlgReason').value,
      scan_code: $('#scan-'+itemId)?.value || null}));
}

/* ---------------- 直接动作 ---------------- */
async function doSign(id){ await call('sign_box',{box_id:id}); await reload(true); toast('药师签发成功',true); }
async function doSeal(id){ await api('/api/seal',{box_id:id}); await reload(true); toast('封盒照片已保存到本地',true); }
async function doAdmin(itemId,result){
  const code = $('#scan-'+itemId)?.value || '';
  await call('administer',{item_id:itemId,result,scan_code:code});
  await reload(true); toast('签收成功',true);
}
async function doReturn(rid){ await call('set_away',{resident_id:rid,away:false}); await reload(true); }
async function doCloseExc(id){
  await call('close_exception',{exception_id:id,resolution:$('#res-'+id).value});
  await reload(true); toast('异常已复核闭环',true);
}

$('#btnPack').onclick = async ()=>{
  const data = await call('pack_box',{resident_id:$('#packRes').value,
    serve_date:$('#packDate').value, meal_slot:$('#packSlot').value});
  await reload(true);
  toast(`已按医嘱 v${data.version_no} 装盒 #${data.id}（${data.items.length} 格），待药师签发`,true);
};
$('#btnStop').onclick = async ()=>{
  const ids=[...document.querySelectorAll('.stopck:checked')].map(c=>Number(c.value));
  const reason=$('#stopReason').value.trim();
  if(!ids.length) return toast('请先勾选要停用的医嘱',false);
  if(!reason) return toast('停用必须填写原因',false);
  const v = await call('stop_order_temp',{order_ids:ids,reason});
  await reload(true);
  toast(`医嘱已更新至 v${v}：旧版本即刻失效，旧版药盒被阻断`,true);
};
$('#btnNewOrder').onclick = async ()=>{
  const med=$('#newMed').value.trim(), spec=$('#newSpec').value.trim(),
        dose=$('#newDose').value.trim();
  if(!med||!spec||!dose) return toast('药名/规格/剂量必填',false);
  // 新增前先开新版本（医生动作）
  await call('new_version',{note:`新增医嘱：${med}`});
  await call('add_order',{resident_id:$('#newRes').value,med_name:med,spec:spec,
    dose_text:dose,meal_slot:$('#newSlot').value});
  await reload(true); toast('已在新版本中新增医嘱',true);
};
$('#btnRemind').onclick = async ()=>{
  const r = await call('run_reminders',{});
  await reload(true);
  toast(`提醒任务完成：暂缓到期 ${r.deferred_due} 条，漏签 ${r.missed_sign} 条`,true);
};

$('#actor').onchange = render;
$('#bedBoxSel').onchange = renderBedside;
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  document.querySelectorAll('.wrap section').forEach(s=>s.classList.add('hidden'));
  $('#tab-'+b.dataset.tab).classList.remove('hidden');
});

async function reload(silent){
  const r = await fetch('/api/board?date=' + encodeURIComponent($('#packDate')?.value || ''));
  const j = await r.json();
  if(j.ok){ D=j.data; render(); }
  else if(!silent) toast(j.error.message,false);
}
function tickClock(){
  $('#clockBadge').textContent = new Date().toLocaleString('zh-CN');
}
async function boot(){
  tickClock(); setInterval(tickClock,1000);
  await reload();
  // 进程内任务在服务端 20s 跑一次；页面 20s 同步看板
  timer = setInterval(()=>reload(true), 20000);
}
boot();
