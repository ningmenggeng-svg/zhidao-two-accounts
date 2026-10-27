'use strict'
const $ = id => document.getElementById(id)
let state = null, selectedId = null, pending = false, settingsDirty = false
let autostartSaving = false
let passwordGeneration = 0, passwordJob = null, passwordTimer = null, passwordPolling = false
let qrGeneration = 0, qrJob = null, qrTimer = null, qrPolling = false
let accountSignature = '', courseSignature = '', editingId = null, epoch = 0, polling = false
let pickerSignature = ''
try { selectedId=sessionStorage.getItem('view-account') } catch {}
const statusNames = {idle:'尚未开始',running:'正在执行',paused:'已暂停',stopping:'正在停止',cancelled:'已停止',completed:'已完成',partial:'部分未完成',failed:'执行失败',pending:'等待中',limited:'已达上限',skipped:'未执行',interrupted:'上次已中断'}
const settingsIds = ['start-mode','daily-time','execution-mode','parallel-accounts','speed','course-minutes','auto-answer','continue-on-error']

function element(tag, className, text) {
  const node = document.createElement(tag)
  if (className) node.className = className
  if (text !== undefined) node.textContent = text
  return node
}
function toast(message, error = false) {
  clearTimeout(toast.timer)
  $('toast').textContent = message
  $('toast').className = `toast${error ? ' error' : ''}`
  $('toast').hidden = false
  toast.timer = setTimeout(() => { $('toast').hidden = true }, error ? 6500 : 2800)
}
async function api(path, method = 'GET', body) {
  const response = await fetch(`/api${path}`, {
    method, headers: {'X-Local-Request':'1', ...(body === undefined ? {} : {'Content-Type':'application/json'})},
    body: body === undefined ? undefined : JSON.stringify(body), cache:'no-store'
  })
  const result = await response.json()
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '填写内容不符合要求，请检查后重试。')
  return result
}
function currentAccount() { return state?.accounts.find(a => a.id === selectedId) }
function parallelLimit() { return state?.limits?.parallel_accounts??2 }
function rememberViewedAccount() {
  try { if(selectedId)sessionStorage.setItem('view-account',selectedId);else sessionStorage.removeItem('view-account') } catch {}
}
function viewAccount(id) {
  if(!state?.accounts.some(account=>account.id===id))return
  selectedId=id
  rememberViewedAccount()
  $('course-search').value=''
  $('course-error').hidden=true
  render()
}
$('view-account').onchange=event=>viewAccount(event.target.value)
function setDisabled() {
  const locked = pending || !state || state.active || state.qr_active || state.password_login_active
  document.querySelectorAll('[data-idle-control]').forEach(node => { node.disabled = locked })
  settingsIds.forEach(id => { $(id).disabled = locked })
  ;['add-account','add-account-bottom','save-settings','edit-account','qr-login','password-login'].forEach(id => { $(id).disabled = locked })
  ;['add-account','add-account-bottom'].forEach(id => { $(id).disabled = locked || (state?.accounts.length??0) >= 2 })
  $('refresh-courses').disabled = locked || !currentAccount()
  $('select-all').disabled = locked
  const selected = state?.accounts.some(a => a.included && a.selected.length)
  $('start-queue').disabled = locked || !selected
  $('start-current').disabled = locked || !currentAccount()?.selected.length
  $('daily-time').disabled = locked || $('start-mode').value!=='daily'
  $('parallel-accounts').disabled = locked || $('execution-mode').value!=='parallel'
  const stopping = state?.queue.status === 'stopping'
  $('pause-queue').disabled = pending || stopping
  $('stop-queue').disabled = pending || stopping
  $('autostart-enabled').disabled = pending || autostartSaving || !state?.autostart?.supported
  $('view-account').disabled = pending || !state?.accounts.length
}
async function refresh() {
  const generation = epoch
  const result = await api('/state')
  if (generation !== epoch) return
  state = result
  $('offline').hidden = true
  if (!state.accounts.some(a => a.id === selectedId)) selectedId = state.accounts[0]?.id || null
  const login = state.password_login
  if (state.password_login_active && login && !passwordJob && !pending && ['opening','loading','waiting'].includes(login.status)) {
    selectedId = login.account_id
    passwordJob = login.id
    const generation = ++passwordGeneration
    const account = state.accounts.find(a => a.id === login.account_id)
    $('password-login-caption').textContent = `正在登录：${account?.label || '当前账号'}`
    $('password-login-message').textContent = login.message
    $('password-login-window').textContent = login.window_opened ? '已连接官方登录窗口' : '正在连接专用登录窗口'
    $('password-login-show').disabled = false
    if (!$('password-login-dialog').open) $('password-login-dialog').showModal()
    clearInterval(passwordTimer)
    passwordTimer = setInterval(() => pollPasswordLogin(generation), 2000)
  }
  render()
}
async function operation(button, callback, success) {
  if (pending) return
  pending = true
  epoch++
  if (button) { button.classList.add('is-busy'); button.setAttribute('aria-busy','true') }
  setDisabled()
  try {
    await callback()
    await refresh()
    if (success) toast(success)
  } catch (error) {
    toast(error.message || '无法连接本地程序。', true)
  } finally {
    pending = false
    if (button) { button.classList.remove('is-busy'); button.removeAttribute('aria-busy') }
    setDisabled()
  }
}
function render() {
  $('account-count').textContent = state.accounts.length
  $('included-count').textContent = state.accounts.filter(a => a.included && a.selected.length).length
  $('selected-count').textContent = state.accounts.filter(a => a.included).reduce((sum,a) => sum + a.selected.length,0)
  const jobs = state.queue.jobs || []
  $('finished-count').textContent = jobs.filter(j => j.status === 'completed').length
  $('finished-total').textContent = `/ ${jobs.length} 门`
  const badge = $('queue-badge')
  badge.textContent = statusNames[state.queue.status] || state.queue.status
  badge.className = `badge ${state.queue.status}`
  renderAccounts()
  renderAccountPicker()
  renderCourses()
  if (!settingsDirty) populateSettings()
  renderAutostart()
  renderQueue()
  setDisabled()
}
function renderAccounts() {
  const signature = JSON.stringify([state.accounts, selectedId])
  if (signature === accountSignature) return
  accountSignature = signature
  const list = $('account-list')
  list.replaceChildren()
  if (!state.accounts.length) list.append(element('p','sidebar-empty','还没有保存的账号。添加后，可以为每个账号单独选择课程。'))
  state.accounts.forEach((account,index) => {
    const row = element('div',`account-item${account.id === selectedId ? ' selected' : ''}`)
    const check = element('input')
    check.type = 'checkbox'; check.checked = account.included
    check.setAttribute('aria-label', `${account.label} 参与执行`)
    check.dataset.idleControl = '1'
    check.addEventListener('change', () => operation(check, () => api(`/accounts/${account.id}/selection`,'PUT',{selected:account.selected,included:check.checked})))
    const button = element('button','account-content')
    button.append(element('strong','',account.label),element('small','',`${index+1} · 已选 ${account.selected.length} / ${account.courses.length} 门`))
    button.setAttribute('aria-label', `查看 ${account.label} 的课程`)
    button.addEventListener('click',()=>viewAccount(account.id))
    const order = element('div','account-order')
    ;[-1,1].forEach(direction => {
      const up = direction === -1
      const move = element('button','icon-button',up ? '↑' : '↓')
      move.dataset.idleControl = '1'; move.title = up ? '上移账号' : '下移账号'
      move.setAttribute('aria-label', `${up ? '上移' : '下移'} ${account.label}`)
      if (index+direction < 0 || index+direction >= state.accounts.length) move.hidden = true
      move.addEventListener('click', () => {
        const ids = state.accounts.map(a => a.id)
        ;[ids[index],ids[index+direction]] = [ids[index+direction],ids[index]]
        operation(move, () => api('/accounts/order','PUT',{ids}))
      })
      order.append(move)
    })
    const remove=element('button','account-delete','删除')
    remove.dataset.idleControl='1'
    remove.setAttribute('aria-label',`删除账号 ${account.label}`)
    remove.addEventListener('click',()=>deleteAccount(account,remove))
    row.append(check,button,order,remove); list.append(row)
  })
}
function renderAccountPicker() {
  const picker=$('view-account')
  const signature=JSON.stringify(state.accounts.map(account=>[account.id,account.label]))
  if(signature!==pickerSignature) {
    pickerSignature=signature
    picker.replaceChildren()
    if(!state.accounts.length){const option=element('option','','先添加账号');option.value='';picker.append(option)}
    state.accounts.forEach(account=>{const option=element('option','',account.label);option.value=account.id;picker.append(option)})
  }
  picker.value=selectedId||''
  rememberViewedAccount()
  const account=currentAccount()
  $('view-account-summary').textContent=account ? `${account.courses.length} 门课程 · 已选 ${account.selected.length} 门` : '先添加备注，再到官方窗口登录'
}
function renderCourses(force = false) {
  const account = currentAccount(), query = $('course-search').value.trim().toLowerCase()
  const signature = JSON.stringify([account,query])
  if (!force && signature === courseSignature) return
  courseSignature = signature
  $('course-title').textContent = account ? `${account.label} 的课程` : '先添加一个账号'
  $('course-subtitle').textContent = account ? (account.updated ? `最近获取 ${account.updated}` : '请先在 Edge 官方窗口完成登录') : '添加备注后，在官方窗口登录获取课程。'
  $('edit-account').hidden = !account
  $('qr-login').hidden = !account
  $('password-login').hidden = !account
  $('course-tools').hidden = !account?.courses.length
  $('course-selection-count').textContent = `已选 ${account?.selected.length || 0} 门`
  $('select-all').checked = !!account?.courses.length && account.selected.length === account.courses.length
  $('select-all').indeterminate = !!account?.selected.length && account.selected.length < account.courses.length
  $('selection-note').textContent = account && !account.included ? '这个账号未勾选，可点「只启动当前账号」单独运行。' : '课程选择自动保存，重新打开程序仍会保留。'
  const list = $('course-list'); list.replaceChildren()
  const courses = account?.courses.filter(c => `${c.name} ${c.school}`.toLowerCase().includes(query)) || []
  if (!courses.length) {
    const empty = element('div','empty')
    empty.append(element('span','empty-icon','▤'))
    empty.append(element('h3','',!account ? '从一个账号开始' : account.courses.length ? '没有匹配的课程' : account.updated ? '该账号暂无课程' : '先获取这个账号的课程'))
    empty.append(element('p','',!account ? '点击左侧「添加账号」保存备注，再到官方窗口登录。' : account.courses.length ? '试试其他关键词。' : '点击「账号密码登录」，在 Edge 官方窗口手动填写账号密码。'))
    list.append(empty)
  }
  courses.forEach(course => {
    const row = element('label','course-row'), check = element('input')
    check.type='checkbox'; check.checked=account.selected.includes(course.id); check.dataset.idleControl='1'
    check.setAttribute('aria-label',`选择课程 ${course.name}`)
    check.addEventListener('change', () => {
      const ids = new Set(account.selected)
      if(check.checked) ids.add(course.id); else ids.delete(course.id)
      saveSelection(account, account.courses.filter(c => ids.has(c.id)).map(c => c.id), check)
    })
    const detail = element('div','course-detail')
    detail.append(element('strong','',course.name),element('small','',[course.school,course.term].filter(Boolean).join(' · ') || '知到共享课程'))
    row.append(check,element('span','course-avatar',course.name.charAt(0)),detail,element('span','course-id',`# ${course.id}`)); list.append(row)
  })
}
function saveSelection(account, selected, button) {
  operation(button, () => api(`/accounts/${account.id}/selection`,'PUT',{selected,included:account.included}))
}
function populateSettings() {
  const s=state.settings
  $('start-mode').value=s.daily_enabled ? 'daily' : 'manual'; $('daily-time').value=s.daily_time
  $('execution-mode').value=s.execution_mode||'sequential'; $('parallel-accounts').value=Math.min(parallelLimit(),s.parallel_accounts||2)
  $('speed').value=String(s.speed); $('course-minutes').value=s.course_minutes
  $('auto-answer').checked=s.auto_answer; $('continue-on-error').checked=s.continue_on_error
  $('save-settings').textContent='保存运行设置'
  $('next-run').textContent = s.daily_enabled && s.next_run ? `下次：${new Date(s.next_run).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})}` : '手动模式，等待你点击开始。'
  updateExecutionControls()
}
function updateExecutionControls() {
  $('daily-time-row').hidden=$('start-mode').value!=='daily'
  $('parallel-accounts-row').hidden=$('execution-mode').value!=='parallel'
  if(!state.active)$('queue-title').textContent=$('execution-mode').value==='parallel' ? '多账号同时执行' : '轮流执行'
}
function settingsValues() {
  const minutes=Number($('course-minutes').value)
  if (!Number.isInteger(minutes) || minutes<0 || minutes>240) throw new Error('运行上限请填写 0 到 240 之间的整数。')
  if (!$('daily-time').value) throw new Error('请选择每天开始的时间。')
  const parallel=Number($('parallel-accounts').value)
  if(!Number.isInteger(parallel)||parallel<1||parallel>parallelLimit())throw new Error(`当前同时运行账号数请填写 1 到 ${parallelLimit()} 之间的整数。`)
  return {daily_enabled:$('start-mode').value==='daily',daily_time:$('daily-time').value,execution_mode:$('execution-mode').value,parallel_accounts:parallel,speed:Number($('speed').value),course_minutes:minutes,auto_answer:$('auto-answer').checked,continue_on_error:$('continue-on-error').checked}
}
function renderAutostart() {
  const startup=state.autostart||{enabled:false,supported:false}
  if(!autostartSaving)$('autostart-enabled').checked=startup.enabled
  $('autostart-status').textContent=startup.error||(startup.supported ? (startup.enabled ? '已开启：登录 Windows 后在后台启动，课程按每日定时设置执行。' : '未开启。开启后，登录 Windows 即可在后台启动程序。') : '开机自启动仅支持 Windows 成品版。')
}
$('autostart-enabled').onchange=async()=>{
  const enabled=$('autostart-enabled').checked
  autostartSaving=true
  setDisabled()
  try {
    state.autostart=await api('/autostart','PUT',{enabled})
    toast(enabled ? '已开启开机自启动，后台运行' : '已关闭开机自启动')
  } catch(error) { toast(error.message,true) }
  finally { autostartSaving=false;renderAutostart();setDisabled() }
}
async function saveSettings() { await api('/settings','PUT',settingsValues()); settingsDirty=false }
function renderQueue() {
  $('start-queue').hidden=state.active
  $('start-current').hidden=state.active||!currentAccount()
  if(state.active)$('queue-title').textContent=state.queue.execution_mode==='parallel' ? '多账号同时执行' : '轮流执行'
  $('pause-queue').hidden=!state.active
  $('stop-queue').hidden=!state.active
  $('pause-queue').textContent=state.queue.status==='paused' ? '继续' : '暂停'
  const list=$('queue-list'), scroll=list.scrollTop
  const jobs=state.queue.jobs || []
  const completed=jobs.filter(job=>job.status==='completed').length
  const totalProgress=jobs.reduce((sum,job)=>sum+(job.status==='completed' ? 100 : Math.min(100,Math.max(0,Number(job.percentage)||0))),0)
  const overall=jobs.length ? totalProgress/jobs.length : 0
  $('queue-progress').value=overall
  $('queue-progress').setAttribute('aria-valuetext',`${overall.toFixed(1)}%，完成 ${completed} / ${jobs.length} 门课程`)
  $('queue-progress-percent').textContent=`${overall.toFixed(1)}%`
  $('queue-progress-detail').textContent=jobs.length ? `完成 ${completed} / ${jobs.length} 门 · ${statusNames[state.queue.status]||state.queue.status}` : '开始执行后，进度会自动更新。'
  if (jobs.length) {
    list.replaceChildren()
    jobs.forEach((job,index) => {
      const row=element('div','queue-row'), detail=element('div')
      detail.append(element('div','queue-title',job.course),element('div','queue-meta',`${job.account} · ${job.message}${job.current_video && job.status==='running' ? ` · ${job.current_video}` : ''}`))
      const percentage=Math.min(100,Math.max(0,Number(job.percentage)||0))
      const hasProgress=job.status==='running'||job.status==='completed'||percentage>0||Boolean(job.current_video)
      const courseProgress=element('div','course-progress')
      const progressHeading=element('div','course-progress-heading')
      progressHeading.append(element('span','','课程总体进度'),element('strong','',hasProgress ? `${percentage.toFixed(2)}%` : '待获取'))
      const bar=element('progress'); bar.max=100; bar.value=percentage; bar.setAttribute('aria-label',`${job.course} 的课程总体进度`)
      bar.setAttribute('aria-valuetext',hasProgress ? `${percentage.toFixed(2)}%` : '等待执行后获取课程进度')
      courseProgress.append(progressHeading,bar)
      detail.append(courseProgress)
      row.append(element('span','queue-index',index+1),detail,element('span',`queue-status ${job.status}`,statusNames[job.status]||job.status))
      list.append(row)
    })
    list.scrollTop=scroll
  }
  const logs=$('logs'), nearBottom=logs.scrollHeight-logs.scrollTop-logs.clientHeight<35
  const lines=state.queue.logs||[]
  if (JSON.stringify(lines)!==renderQueue.logSignature) {
    renderQueue.logSignature=JSON.stringify(lines); logs.replaceChildren()
    if(!lines.length) logs.append(element('p','','尚无运行记录。'))
    lines.forEach(line => {const p=element('p');p.append(element('time','',line.time),document.createTextNode(line.message));logs.append(p)})
    if(nearBottom) logs.scrollTop=logs.scrollHeight
  }
}
function accountDialog(account) {
  if(pending||state?.active) return
  editingId=account?.id||null
  $('dialog-title').textContent=account ? '编辑账号' : '添加账号'
  $('account-label').value=account?.label||''
  $('delete-account').hidden=!account; $('form-error').hidden=true
  $('account-dialog').showModal(); $('account-label').focus()
}
function confirmAction(title,message) {
  $('confirm-title').textContent=title; $('confirm-message').textContent=message
  const dialog=$('confirm-dialog'); dialog.showModal()
  return new Promise(resolve => {
    let settled=false
    const finish = result => {if(settled)return;settled=true;dialog.close();resolve(result)}
    $('confirm-yes').onclick=()=>finish(true); $('confirm-no').onclick=()=>finish(false)
    dialog.oncancel=event=>{event.preventDefault();finish(false)}
  })
}
$('add-account').onclick=$('add-account-bottom').onclick=()=>accountDialog()
$('edit-account').onclick=()=>accountDialog(currentAccount())
$('close-dialog').onclick=()=>$('account-dialog').close()
$('account-form').addEventListener('submit',async event=>{
  event.preventDefault()
  const button=$('submit-account'); button.disabled=true; $('form-error').hidden=true
  try {
    const payload={label:$('account-label').value}
    const result=await api(editingId ? `/accounts/${editingId}` : '/accounts',editingId?'PUT':'POST',payload)
    epoch++; selectedId=result.id; $('account-dialog').close(); await refresh(); toast('账号已保存到本机')
  } catch(error){$('form-error').textContent=error.message;$('form-error').hidden=false}
  finally{button.disabled=false}
})
async function deleteAccount(account,button) {
  if(!account||pending||state.active||state.qr_active||state.password_login_active)return
  if(!await confirmAction(`删除「${account.label}」？`,'将删除本机这个账号条目、登录状态、课程列表和课程选择。'))return
  if(editingId===account.id&&$('account-dialog').open)$('account-dialog').close()
  await operation(button,()=>api(`/accounts/${account.id}`,'DELETE'),'账号已删除')
}
$('delete-account').onclick=()=>deleteAccount(state.accounts.find(a=>a.id===editingId),$('delete-account'))
$('refresh-courses').onclick=()=>{
  const account=currentAccount(); if(!account)return
  $('course-error').hidden=true
  operation($('refresh-courses'),async()=>{
    try{await api(`/accounts/${account.id}/courses`,'POST')}
    catch(error){$('course-error').textContent=error.message;$('course-error').hidden=false;throw error}
  },'课程列表已更新，请勾选需要执行的课程')
}
$('course-search').addEventListener('input',()=>{if(state){renderCourses();setDisabled()}})
$('select-all').onchange=()=>{const a=currentAccount();if(a)saveSelection(a,$('select-all').checked?a.courses.map(c=>c.id):[],$('select-all'))}
settingsIds.forEach(id=>$(id).addEventListener('change',()=>{settingsDirty=true;$('save-settings').textContent='保存运行设置 *';$('next-run').textContent='设置尚未保存';updateExecutionControls();setDisabled()}))
$('save-settings').onclick=()=>operation($('save-settings'),saveSettings,'运行设置已保存')
$('start-queue').onclick=()=>operation($('start-queue'),async()=>{if(settingsDirty)await saveSettings();await api('/queue/start','POST')},'已开始执行选中账号')
$('start-current').onclick=()=>{
  const account=currentAccount();if(!account)return
  operation($('start-current'),async()=>{if(settingsDirty)await saveSettings();await api('/queue/start','POST',{account_ids:[account.id]})},'已启动当前账号')
}
$('pause-queue').onclick=()=>operation($('pause-queue'),()=>api(`/queue/${state.queue.status==='paused'?'resume':'pause'}`,'POST'))
$('stop-queue').onclick=()=>operation($('stop-queue'),()=>api('/queue/stop','POST'))
$('shutdown').onclick=async()=>{
  if(!await confirmAction('退出程序？','退出会停止正在执行的队列和每日定时。只关闭网页则可以继续在后台运行。'))return
  try{await api('/shutdown','POST');document.body.replaceChildren(element('div','empty','程序已退出。可以关闭网页，下次双击 ZhidaoMulti.exe 重新打开。'))}
  catch(error){toast(error.message,true)}
}
async function poll() {
  if(polling||pending||$('account-dialog').open||$('confirm-dialog').open||$('qr-dialog').open||$('password-login-dialog').open)return
  polling=true
  try{await refresh()}catch{ $('offline').hidden=false; $('start-queue').disabled=true }
  finally{polling=false}
}
setDisabled();poll();setInterval(poll,2000)

async function beginQr() {
  const account = currentAccount()
  if (!account || pending || state.active) return
  const generation = ++qrGeneration
  clearInterval(qrTimer)
  const old = qrJob
  qrJob = null
  if (old) { try { await api(`/qr/${old}`, 'DELETE') } catch {} }
  $('qr-caption').textContent = `请用「${account.label}」对应的知到账号扫码。`
  $('qr-image').hidden = true
  $('qr-message').textContent = '正在生成二维码…'
  $('qr-regenerate').hidden = true
  if (!$('qr-dialog').open) $('qr-dialog').showModal()
  pending = true
  setDisabled()
  try {
    const result = await api(`/accounts/${account.id}/qr`, 'POST')
    if (generation !== qrGeneration || !$('qr-dialog').open) {
      await api(`/qr/${result.id}`, 'DELETE')
      return
    }
    qrJob = result.id
    $('qr-image').src = result.image
    $('qr-image').hidden = false
    $('qr-message').textContent = '请使用知到 App 扫码，并在手机上确认登录。'
    qrTimer = setInterval(() => pollQr(generation), 2000)
  } catch (error) {
    if (generation === qrGeneration) {
      $('qr-message').textContent = error.message
      $('qr-regenerate').hidden = false
    }
  } finally {
    pending = false
    setDisabled()
  }
}
async function pollQr(generation) {
  if (qrPolling || !qrJob || generation !== qrGeneration) return
  qrPolling = true
  try {
    const result = await api(`/qr/${qrJob}`)
    if (generation !== qrGeneration) return
    $('qr-message').textContent = result.message
    if (result.status === 'success') {
      clearInterval(qrTimer)
      qrJob = null
      $('qr-dialog').close()
      $('course-error').hidden = true
      await refresh()
      toast(result.message + ' 请勾选需要执行的课程。')
    } else if (['failed', 'expired', 'cancelled'].includes(result.status)) {
      clearInterval(qrTimer)
      $('qr-regenerate').hidden = false
      $('qr-image').hidden = true
    }
  } catch (error) {
    if (generation === qrGeneration) {
      clearInterval(qrTimer)
      $('qr-message').textContent = error.message || '连接中断，请重新获取二维码。'
      $('qr-regenerate').hidden = false
    }
  } finally { qrPolling = false }
}
$('qr-login').onclick = beginQr
$('qr-regenerate').onclick = beginQr
$('qr-close').onclick = () => $('qr-dialog').close()
$('qr-dialog').addEventListener('close', async () => {
  qrGeneration++
  clearInterval(qrTimer)
  const old = qrJob
  qrJob = null
  $('qr-image').removeAttribute('src')
  if (old) { try { await api(`/qr/${old}`, 'DELETE') } catch {} }
  try { await refresh() } catch {}
})

async function beginPasswordLogin() {
  const account = currentAccount()
  if (!account || pending || state.active) return
  const generation = ++passwordGeneration
  $('password-login-caption').textContent = `正在登录：${account.label}`
  $('password-login-message').textContent = '正在打开知到官方登录窗口…'
  $('password-login-window').textContent = '正在连接专用登录窗口'
  $('password-login-show').disabled = true
  $('password-login-dialog').showModal()
  pending = true
  setDisabled()
  try {
    const result = await api(`/accounts/${account.id}/password-login`, 'POST')
    if (generation !== passwordGeneration || !$('password-login-dialog').open) {
      await api(`/password-login/${result.id}`, 'DELETE')
      return
    }
    passwordJob = result.id
    $('password-login-show').disabled = false
    passwordTimer = setInterval(() => pollPasswordLogin(generation), 2000)
  } catch (error) {
    if (generation === passwordGeneration) $('password-login-message').textContent = error.message
  } finally { pending = false; setDisabled() }
}
async function pollPasswordLogin(generation) {
  if (passwordPolling || !passwordJob || generation !== passwordGeneration) return
  passwordPolling = true
  try {
    const result = await api(`/password-login/${passwordJob}`)
    if (generation !== passwordGeneration) return
    $('password-login-message').textContent = result.message
    $('password-login-window').textContent = result.status === 'success' ? '官方登录已完成' : result.status === 'failed' ? (result.window_opened ? '窗口曾连接，当前连接中断' : '专用登录窗口连接失败') : ['expired','cancelled'].includes(result.status) ? '本次登录等待已结束' : result.window_opened ? '已连接官方登录窗口' : '正在连接专用登录窗口'
    if (result.status === 'success') {
      clearInterval(passwordTimer)
      passwordJob = null
      $('password-login-dialog').close()
      $('course-error').hidden = true
      await refresh()
      toast(result.message + ' 请勾选课程。')
    } else if (['failed','cancelled','expired'].includes(result.status)) {
      clearInterval(passwordTimer)
      $('password-login-show').disabled = true
    }
  } catch (error) {
    if (generation === passwordGeneration) $('password-login-message').textContent = error.message
  } finally { passwordPolling = false }
}
$('password-login').onclick = beginPasswordLogin
$('password-login-close').onclick = () => $('password-login-dialog').close()
$('password-login-dialog').addEventListener('close', async () => {
  passwordGeneration++
  clearInterval(passwordTimer)
  const old = passwordJob
  passwordJob = null
  if (old) { try { await api(`/password-login/${old}`, 'DELETE') } catch {} }
  try { await refresh() } catch {}
})

$('password-login-check').onclick = async () => {
  if (!passwordJob) return
  const button = $('password-login-check')
  button.disabled = true
  try {
    await api(`/password-login/${passwordJob}/check`, 'POST')
    toast('已请求检查登录状态')
    await pollPasswordLogin(passwordGeneration)
  } catch (error) { toast(error.message, true) }
  finally { button.disabled = false }
}

$('password-login-show').onclick = async () => {
  if (!passwordJob) return
  const button = $('password-login-show')
  const job = passwordJob
  button.disabled = true
  try {
    await api(`/password-login/${job}/show`, 'POST')
    toast('已请求显示官方登录窗口')
  } catch (error) { toast(error.message, true) }
  finally { if (passwordJob === job) button.disabled = false }
}
