'use strict';
const $ = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statuses = {open:'Открыт', in_progress:'В работе', closed:'Решён'};
const icons = {elevator:'↕', heating:'♨', water:'≈', electricity:'ϟ', garbage:'▤', noise:'♪', building:'⌂', yard:'♧', parking:'P', other:'•'};
let incidents = [], categories = [], staged = null, requestId = crypto.randomUUID(), toastTimer, submitting = false, activeRole = 'resident';
const date = value => new Intl.DateTimeFormat('ru-RU', {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}).format(new Date(value));
const reportCount = n => `${n} ${n % 10 === 1 && n % 100 !== 11 ? 'обращение' : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14) ? 'обращения' : 'обращений'}`;
const title = category => categories.find(c => c.id === category)?.title || 'Другое';
function reveal(element) {
  if (!element || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  element.getAnimations().forEach(animation => animation.cancel());
  element.animate([{opacity:0,transform:'translateY(10px)'},{opacity:1,transform:'translateY(0)'}],{duration:280,easing:'cubic-bezier(.2,.7,.2,1)'});
}
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 4000); }
async function api(path, data) {
  const response = await fetch('/api/' + path, data === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || (typeof result.detail === 'string' ? result.detail : 'Проверьте введённые данные.'));
  return result;
}
function showError(message, global = false) { const el = $(global ? 'global-error' : 'form-error'); el.textContent = message; el.hidden = false; }
function showView(view) {
  const demo = view === 'demo';
  const resident = view === 'resident';
  if (!demo) activeRole = view;
  $('resident-view').hidden = !resident || demo;
  $('dispatch-view').hidden = resident || demo;
  $('demo-view').hidden = !demo;
  reveal($(demo ? 'demo-view' : resident ? 'resident-view' : 'dispatch-view'));
  $('nav-resident').classList.toggle('active',resident && !demo); $('nav-dispatch').classList.toggle('active',!resident && !demo);
  $('nav-demo').classList.toggle('active',demo);
  $('nav-resident').setAttribute('aria-current',resident && !demo ? 'page' : 'false'); $('nav-dispatch').setAttribute('aria-current',!resident && !demo ? 'page' : 'false');
  $('nav-demo').setAttribute('aria-pressed',String(demo));
  const labels = demo ? ['Демонстрация','Демонстрационные блоки'] : resident ? ['Житель','Сообщить о проблеме'] : ['Диспетчер','Панель диспетчера'];
  document.title = labels[1] + ' — ДомРадар';
  $('breadcrumb').textContent = 'ДомРадар / ' + labels[0];
  if (!resident && !demo) refresh().catch(e => showError(e.message,true));
}
function normalizeAddress(value) { return value.toLowerCase().replaceAll('ё','е').trim().replace(/\s+/g,' '); }
function renderNearby() {
  const address = normalizeAddress($('building').value);
  const resolved = incidents.filter(i => i.building_id === address && i.status === 'closed').sort((a,b) => new Date(b.created_at) - new Date(a.created_at)).slice(0,3);
  $('resolved-list').innerHTML = !address ? '<p class="house-note">Укажите адрес, чтобы увидеть решённые обращения.</p>' : !resolved.length ? '<p class="house-note">По этому адресу пока нет решённых обращений. Они появятся здесь после закрытия диспетчером.</p>' : resolved.map(i => `<div class="resolved-item"><span class="resolved-mark" aria-hidden="true">✓</span><div><strong>${escapeHtml(i.description)}</strong><span>Решено · ${escapeHtml(title(i.category))}</span></div></div>`).join('');
  const rows = incidents.filter(i => i.building_id === address && i.status !== 'closed');
  $('nearby-count').textContent = rows.length;
  $('nearby-list').innerHTML = !address ? '<p class="muted">Введите адрес, чтобы увидеть открытые инциденты.</p>' : !rows.length ? '<p class="muted">По этому адресу пока нет открытых инцидентов. Ваше обращение может стать первым.</p>' : rows.slice(0,5).map(i => `<div class="nearby-item"><strong>${escapeHtml(i.description)}</strong><p>${escapeHtml(title(i.category))} · ${i.reports_count} обращ. · ${statuses[i.status]}</p></div>`).join('');
}
function renderList() {
  const query = $('search').value.toLowerCase().trim(), filter = $('status-filter').value;
  const rows = incidents.filter(i => (filter === 'all' || i.status === filter) && `${i.description} ${i.building_id} ${i.id}`.toLowerCase().includes(query));
  $('queue-count').textContent = rows.length;
  if (!rows.length) {
    $('incident-list').innerHTML = `<div class="empty"><div class="empty-symbol" aria-hidden="true">⌂</div><h3>${incidents.length ? 'Ничего не найдено' : 'Здесь появятся обращения'}</h3><p>${incidents.length ? 'Измените запрос или выберите другой статус.' : 'Создайте первое обращение от жителя — оно сразу появится в этой панели.'}</p>${!incidents.length ? '<button class="primary" id="first-report">Создать обращение</button>' : ''}</div>`;
    $('first-report')?.addEventListener('click', () => showView('resident')); return;
  }
  $('incident-list').innerHTML = rows.map(i => `<button class="incident-row" data-id="${escapeHtml(i.id)}" aria-label="Открыть: ${escapeHtml(i.description)}"><span class="incident-body"><span class="incident-category">${escapeHtml(title(i.category))}</span><span class="incident-title">${escapeHtml(i.description)}</span><span class="incident-address">${escapeHtml(i.building_id)}</span><span class="incident-meta">${date(i.created_at)} · ${reportCount(i.reports_count)}</span></span><span class="row-end"><span class="pill ${i.status}">${statuses[i.status]}</span><span class="open-label">Открыть <span aria-hidden="true">↗</span></span></span></button>`).join('');
  $('incident-list').querySelectorAll('[data-id]').forEach(el => el.addEventListener('click', () => openDetail(el.dataset.id).catch(e => showError(e.message,true))));
}
async function refresh() {
  incidents = await api('incidents');
  $('global-error').hidden = true;
  $('stat-open').textContent = incidents.filter(i => i.status === 'open').length;
  $('stat-progress').textContent = incidents.filter(i => i.status === 'in_progress').length;
  $('stat-closed').textContent = incidents.filter(i => i.status === 'closed').length;
  $('stat-reports').textContent = incidents.reduce((n,i) => n+i.reports_count,0);
  $('nav-count').textContent = incidents.filter(i => i.status !== 'closed').length;
  renderList(); renderNearby();
}
function payload() { return {building_id:$('building').value, location_hint:$('location').value, text:$('description').value, category:$('category').value || null}; }
function invalidate() { $('report-form').hidden=false; fitDescription(); staged = null; $('result').hidden = true; $('form-error').hidden = true; requestId = crypto.randomUUID(); $('char-count').textContent = `${$('description').value.length} / 4000`; renderNearby(); }
function renderDecision(result) {
  const panel = $('result'); panel.hidden = false;
  reveal(panel);
  if (result.action === 'clarify') {
    $('report-form').hidden = false;
    $('manual-category').hidden = false;
    panel.innerHTML = '<span class="small-tag">Нужно уточнение</span><h2>Какого типа проблема?</h2><p>Не удалось однозначно определить категорию. Выберите её в форме и повторите проверку.</p>';
    $('category').focus(); return;
  }
  $('report-form').hidden = true;
  const candidate = result.candidate;
  const urgentNotice = result.urgent ? '<p class="error" role="alert">Если есть угроза жизни или здоровью, звоните 112. Не ждите обработки обращения.</p>' : '';
  const headings = {create:'Проверьте перед отправкой',attach:'Найдено такое же обращение',confirm:'Возможно, об этом уже сообщили'};
  const descriptions = {create:'Подходящих открытых инцидентов не найдено. Создадим новый и передадим его диспетчеру.',attach:'Совпали ключевые признаки проблемы. Можно добавить ваше сообщение к существующему обращению.',confirm:'Совпадение не точное. Сравните обращения и подтвердите, одна ли это проблема.'};
  panel.innerHTML = `${urgentNotice}<span class="small-tag">${escapeHtml(result.category_title)}</span><h2>${headings[result.action]}</h2><p>${result.changed ? 'Список инцидентов изменился. Проверьте результат ещё раз.' : descriptions[result.action]}</p>${candidate ? `<div class="candidate"><span class="review-label">Найденное обращение</span><strong>${escapeHtml(candidate.description)}</strong><p>${escapeHtml(candidate.building_id)} · №${escapeHtml(candidate.id)}</p><p>${date(candidate.created_at)}</p></div>` : ''}<div class="review-summary"><span class="review-label">Ваше обращение</span><p class="review-address">${escapeHtml(staged.building_id)}${staged.location_hint ? ` · ${escapeHtml(staged.location_hint)}` : ''}</p><p class="review-description">${escapeHtml(staged.text)}</p></div><div class="result-actions"><button class="primary" id="send-report">${candidate ? 'Да, это та же проблема' : 'Отправить обращение'}</button>${candidate ? '<button class="secondary" id="separate-report">Нет, это другая проблема</button>' : ''}<button class="secondary" id="edit-report">Изменить обращение</button></div><div id="submit-error" class="error" role="alert" hidden></div>`;
  $('send-report').addEventListener('click', () => submit(candidate ? 'confirm' : undefined, result.incident_id));
  $('separate-report')?.addEventListener('click', () => submit('new'));
  $('edit-report').addEventListener('click',()=>{invalidate();reveal($('report-form'));$('description').focus();});
  panel.focus({preventScroll:false});
}
function validateReport() {
  let first = null;
  [['building',2,200,'Введите улицу и номер дома.'],['description',5,4000,'Добавьте описание — от 5 до 4000 символов.']].forEach(([id,min,max,message]) => {
    const field = $(id), invalid = field.value.trim().length < min || field.value.length > max;
    let error = $(id+'-error');
    if (!error) { error=document.createElement('p'); error.id=id+'-error'; error.className='field-error'; field.after(error); }
    error.textContent=invalid ? message : ''; error.hidden=!invalid;
    field.setAttribute('aria-invalid',String(invalid)); field.setAttribute('aria-describedby',error.id);
    if(invalid && !first) first=field;
  });
  if(first){first.focus();return false;} return true;
}
function fitDescription(){ const el=$('description'); el.style.height='auto'; el.style.height=Math.max(108,el.scrollHeight)+'px'; }
async function analyze() {
  if (!validateReport()) return;
  $('form-error').hidden = true; $('analyze-button').disabled = true; $('analyze-button').textContent = 'Проверяем…';
  staged = payload();
  const snapshot = staged;
  try { const result = await api('analyze',snapshot); if (staged === snapshot) renderDecision(result); }
  catch(e) { showError(e.message); }
  finally { $('analyze-button').disabled = false; $('analyze-button').innerHTML = 'Продолжить'; }
}
async function submit(choice, incident_id) {
  if (!staged || submitting) return;
  submitting = true;
  const controls = [...$('report-form').elements];
  controls.forEach(control => control.disabled = true);
  $('result').querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    const result = await api('submit',{...staged, choice, incident_id, request_id:requestId});
    if (!result.saved) { renderDecision(result); return; }
    $('result').innerHTML = `<span class="success-mark">✓ Обращение принято</span><h2>Обращение сохранено</h2><p>Диспетчер увидит его в общей очереди. Здесь можно открыть карточку и посмотреть статус.</p><p class="receipt-id">Номер: ${escapeHtml(result.incident_id)}</p><div class="result-actions"><button class="primary" id="view-saved">Открыть обращение</button><button class="secondary" id="another-report">Новое обращение</button></div>`;
    reveal($('result')); $('result').focus({preventScroll:true});
    staged = null; requestId = crypto.randomUUID();
    $('view-saved').addEventListener('click', async () => {showView('dispatch'); try { await refresh(); await openDetail(result.incident_id); } catch(e){showError(e.message,true);} });
    $('another-report').addEventListener('click', () => { $('description').value = ''; $('category').value = ''; $('manual-category').hidden = true; invalidate(); $('description').focus(); });
    await refresh();
  } catch(e) { const box = $('submit-error'); if(box) {box.textContent=e.message;box.hidden=false;} else showError(e.message); $('result').querySelectorAll('button').forEach(b => b.disabled=false); }
  finally { submitting = false; controls.forEach(control => control.disabled = false); }
}
async function openDetail(id) {
  const i = incidents.find(row => row.id === id); if(!i) return;
  const reports = await api('reports?id=' + encodeURIComponent(id));
  $('detail-content').innerHTML = `<span class="small-tag">${escapeHtml(title(i.category))}</span><h2 class="detail-title" id="incident-title">${escapeHtml(i.description)}</h2><p class="detail-meta">${escapeHtml(i.building_id)}<br>${escapeHtml(i.id)} · ${date(i.created_at)}</p><div class="detail-status"><label for="detail-status-select">Статус</label><select id="detail-status-select">${Object.entries(statuses).map(([key,label])=>`<option value="${key}" ${i.status===key?'selected':''}>${label}</option>`).join('')}</select><button id="save-status" class="primary">Сохранить</button></div><div id="detail-error" class="error" role="alert" hidden></div><h3 class="history-title">Сообщения жителей <span class="count">${reports.length}</span></h3>${reports.map(r => `<div class="report-entry"><small>${date(r.created_at)}</small><p>${escapeHtml(r.description)}</p></div>`).join('')}`;
  $('save-status').addEventListener('click', async () => {
    $('save-status').disabled=true; $('save-status').textContent='Сохраняем…';
    try { await api('status',{id,status:$('detail-status-select').value}); await refresh(); $('detail-dialog').close(); toast('Статус инцидента обновлён'); }
    catch(e) {$('detail-error').textContent=e.message;$('detail-error').hidden=false;$('save-status').disabled=false;$('save-status').textContent='Сохранить';}
  });
  $('detail-dialog').showModal();
  reveal($('detail-dialog'));
}
$('nav-resident').addEventListener('click',()=>showView('resident'));
$('nav-dispatch').addEventListener('click',()=>showView('dispatch'));
$('nav-demo').addEventListener('click',()=>showView('demo'));
$('close-demo').addEventListener('click',()=>showView(activeRole));
$('report-form').addEventListener('submit', e=>{e.preventDefault();analyze();});
['building','location','description','category'].forEach(id=>$(id).addEventListener('input',invalidate));
document.querySelectorAll('[data-example]').forEach(button=>button.addEventListener('click',()=>{$('description').value=button.dataset.example;invalidate();$('description').focus();}));
$('search').addEventListener('input',()=>{ $('clear-search').hidden=!$('search').value; renderList(); });
$('clear-search').addEventListener('click',()=>{ $('search').value=''; $('clear-search').hidden=true; renderList(); $('search').focus(); });
$('description').addEventListener('input',fitDescription);$('status-filter').addEventListener('change',renderList);
$('refresh').addEventListener('click',()=>refresh().then(()=>toast('Список обновлён')).catch(e=>showError(e.message,true)));
$('close-detail').addEventListener('click',()=>$('detail-dialog').close());
async function start(){try{const meta=await api('meta');categories=meta.categories;$('category').innerHTML += categories.map(c=>`<option value="${escapeHtml(c.id)}">${escapeHtml(c.title)}</option>`).join('');await refresh();}catch(e){showError('Не удалось подключиться к локальному серверу. '+e.message,true);}}

let savedHome = '';
function renderHome() {
  $('home-address').textContent = savedHome || 'Сохраните адрес, чтобы не вводить его каждый раз.';
  $('edit-home').textContent = savedHome ? 'Изменить дом' : 'Указать дом';
  $('forget-home').hidden = !savedHome;
  $('building-field').hidden = Boolean(savedHome);
}
renderHome();
$('edit-home').addEventListener('click',()=>{
  $('home-form').hidden=false; $('home-input').value=savedHome;
  $('home-error').hidden=true; $('home-input').removeAttribute('aria-invalid');
  reveal($('home-form')); $('home-input').focus();
});
$('cancel-home').addEventListener('click',()=>{ $('home-form').hidden=true; $('edit-home').focus(); });
$('home-form').addEventListener('submit',async event=>{
  event.preventDefault(); const address=$('home-input').value.trim();
  if(address.length<2 || address.length>200){$('home-error').textContent='Введите адрес дома: от 2 до 200 символов.';$('home-error').hidden=false;$('home-input').setAttribute('aria-invalid','true');$('home-input').focus();return;}
  try {await api('profile',{address});} catch { $('home-error').textContent='Не удалось сохранить дом на сервере. Попробуйте ещё раз.';$('home-error').hidden=false;return; }
  savedHome=address; $('building').value=address; $('location').value=''; invalidate(); renderHome();
  $('home-form').hidden=true; $('edit-home').focus(); toast('Дом сохранён в локальной базе');
});
$('forget-home').addEventListener('click',async ()=>{
  try {await api('profile',{address:''});} catch {$('home-error').textContent='Не удалось удалить сохранённый адрес.';$('home-error').hidden=false;return;}
  savedHome=''; $('building').value=''; $('location').value=''; invalidate(); renderHome();
  $('home-form').hidden=true; $('edit-home').focus(); toast('Сохранённый дом удалён. Обращения остались в базе.');
});
async function boot(){try {const profile=await api('profile');savedHome=profile.address;$('building').value=savedHome;renderHome();}catch(e){showError(e.message,true);}await start();}
boot();
if(document.modelContext?.registerTool){
  const lifecycle=new AbortController();
  window.addEventListener('pagehide',()=>lifecycle.abort(),{once:true});
  try{Promise.resolve(document.modelContext.registerTool({name:'stage_resident_report',description:'Заполнить форму обращения и показать результат проверки, без отправки.',inputSchema:{type:'object',properties:{building_id:{type:'string'},text:{type:'string'}},required:['building_id','text'],additionalProperties:false},annotations:{readOnlyHint:false,untrustedContentHint:true},async execute(input){if(typeof input?.building_id!=='string'||typeof input?.text!=='string'||input.text.length<5||input.text.length>4000||input.building_id.trim().length<2||input.building_id.length>200)throw new Error('Укажите адрес и описание от 5 до 4000 символов.');showView('resident');$('building').value=input.building_id;$('description').value=input.text;$('location').value='';$('category').value='';invalidate();await analyze();return{staged:true,sent:false};}},{signal:lifecycle.signal})).catch(()=>{});}catch{}
}
