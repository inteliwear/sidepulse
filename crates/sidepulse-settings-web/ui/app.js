// Presentation only: typed actions go to the Rust host. All monitoring,
// validation, persistence, device output and platform operations stay in Rust.
const pages = [['agents','Agents'],['animations','Animations'],['advanced','Advanced'],['history','History'],['diagnostics','Diagnostics']];
const auxiliaryPages = [['activity','Activity'],['devices','Devices'],['battery','Battery'],['sleep','Sleep'],['relay','Link computers'],['phones','Link phones']];
const animationRows = [['idle_ready','Idle / Ready'],['working','Working / Tool / Long Task'],['waiting_for_input','Waiting for Input'],['blocked_error','Blocked / Error'],['completed','Completed'],['unknown','Unknown'],['lid_open','Lid Open'],['lid_closed','Lid Closed']];
const modes = [['idle_ready','Idle / Ready'],['working','Working'],['tool_running','Tool running'],
  ['waiting_for_input','Waiting for input'],['long_task_progress','Long task progress'],
  ['blocked_error','Blocked / Error'],['completed','Completed'],['unknown','Unknown']];
const displays = [['agent','Agent status'],['battery','Battery level'],['custom','Manual output']];
const model = {page:new URLSearchParams(window.location.search).get('page')==='setup'?'setup':'agents', settingsPage:'agents', editor:null, profileId:'', submittedRequest:null, previews:{}, revision:0, state:null, setup:null, connected:false, busy:false, pendingAction:false,
  platform:'', drafts:{}, message:'', error:false, lastExport:null, animationMode:'working',
  profileName:'', profileJSON:'', assetId:null, assetState:null, assetName:'', assetProgram:'#00E5FF',
  phoneName:'iPhone', phoneToken:'', openDetails:new Set(), composing:false};
const invoke = (command, args) => window.__TAURI__.core.invoke(command, args);
const pageRoot = document.querySelector('#page');
let fieldNumber = 0;

function el(tag, attrs={}, ...children) {
  const node = document.createElement(tag);
  for (const [key,value] of Object.entries(attrs)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2),value);
    else if (key==='class') node.className=value;
    else if (key==='text') node.textContent=value;
    else if (key==='disabled' || key==='checked' || key==='hidden' || key==='required') node[key]=Boolean(value);
    else if (value!==null && value!==undefined) node.setAttribute(key,String(value));
  }
  for (const child of children.flat(Infinity)) if (child!==null && child!==undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
const p = (text, cls='') => el('p',{class:cls},text);
const note = text => p(text,'muted');
const row = (...children) => el('div',{class:'row'},...children);
const actions = (...children) => el('div',{class:'actions'},...children);
const card = (title,...children) => el('section',{class:'card'},title?el('h2',{},title):null,...children);
function button(label, handler, disabled=false, cls='') { return el('button',{type:'button',onclick:handler,disabled,class:cls},label); }
function field(label, input) {
  const control=input.matches('input,select,textarea')?input:input.querySelector('input,select,textarea');
  if (control && !control.id)control.id=`field-${++fieldNumber}`;
  return el('label',{class:'field',for:control?.id},el('span',{},label),input);
}
function input(id,value,change,{type='text',...attrs}={}) {
  const node=el('input',{id,type,...(type==='number'?{step:'any'}:{}),...attrs,required:type==='number',oninput:event=>{change(type==='number' || type==='range' ? Number(event.target.value) : event.target.value);if(type==='text'||type==='password')render();}});
  node.value=String(value ?? ''); return node;
}
function textArea(id,value,change,rows=6) {
  const node=el('textarea',{id,rows,spellcheck:'false',oninput:event=>{change(event.target.value);render();}});
  node.value=value??''; return node;
}
function select(id,value,choices,change,disabled=false) {
  const node=el('select',{id,disabled,onchange:event=>change(event.target.value)});
  for (const [key,label] of choices) node.append(el('option',{value:key},label));
  if (value && !choices.some(([key])=>key===value)) node.append(el('option',{value},value));
  node.value=value??''; return node;
}
function check(id,label,value,change,disabled=false) {
  return el('label',{class:'check'},el('input',{id,type:'checkbox',checked:value,disabled,onchange:event=>change(event.target.checked)}),el('span',{},label));
}
function details(id,label,...children) {
  const node=el('details',{id},el('summary',{},label),...children);
  node.open=model.openDetails.has(id);
  node.querySelector('summary').addEventListener('click',event=>{event.preventDefault();node.open=!node.open;node.open?model.openDetails.add(id):model.openDetails.delete(id);});
  return node;
}
function header(title,description) { return [el('h1',{},title),p(description,'intro')]; }
function announce(message,error=false) { model.message=String(message);model.error=error;renderMessage(); }
function renderMessage() {
  const node=document.querySelector('#message'); node.hidden=!model.message; node.textContent=model.message;
  node.className=model.error?'error':'';
}
function draft(name) { return model.drafts[name]; }
function edit(name,key,value) { const d=draft(name);d.value[key]=value;d.dirty=true;d.awaiting=0; updateDraftButtons(name); }
function updateDraftButtons(name) {
  const group=document.querySelector(`[data-draft="${name}"]`);
  if(group){group.hidden=false;for(const button of group.querySelectorAll('button'))button.disabled=Boolean(draft(name).awaiting);group.querySelector('.dirty').hidden=false;}
}
function draftSource(name,state) {
  const s=state.settings;
  switch(name) {
    case 'battery': return {auto:s.full_charge_watts===null,watts:s.full_charge_watts??100,preview:s.controls.battery_power_preview,seconds:s.power_change_preview_seconds};
    case 'monitoring': return {idle:s.idle_timeout_seconds/60,retention:s.controls.recent_session_retention_seconds/3600};
    case 'sleep': return {percent:s.sleep_min_battery_percent};
    case 'terminal': return {terminal:s.session_terminal,path:s.custom_terminal_path};
    case 'relay': return {server:state.relay.server,name:state.relay.machine_name,outbound:state.relay.outbound_code};
    case 'lid_timing': return {open:state.lid_durations[0],close:state.lid_durations[1]};
    case 'animation': return animationSelection(state,model.animationMode);
  }
}
function syncDrafts() {
  if(!model.state)return;
  for(const name of ['battery','monitoring','sleep','terminal','relay','lid_timing','animation']) {
    const d=draft(name);
    if(!d || (!d.dirty && (!d.awaiting || model.revision>=d.awaiting))) model.drafts[name]={value:draftSource(name,model.state),dirty:false,awaiting:0};
  }
}
function reset(name) { model.drafts[name]={value:draftSource(name,model.state),dirty:false,awaiting:0};render(); }
function saveButtons(name,label,handler) {
  const d=draft(name);
  const group=actions(button(label,handler,!d.dirty || Boolean(d.awaiting),'primary'),button('Reset changes',()=>reset(name),!d.dirty || Boolean(d.awaiting)),el('span',{class:'dirty',hidden:!d.dirty},'Unsaved changes'));
  group.dataset.draft=name;if(model.page==='agents' && name==='terminal')group.hidden=!d.dirty;return group;
}
async function send(action) {
  if(model.busy)return;
  for(const field of pageRoot.querySelectorAll('input,textarea,select'))if(!field.checkValidity()){field.reportValidity();return;}
  model.busy=true;model.pendingAction=true;model.submittedRequest=action.request??null;announce('Working…');
  pageRoot.querySelector('fieldset').disabled=true;
  try { await invoke('settings_action',{action}); }
  catch(error) { model.busy=false;model.pendingAction=false;announce(error,true);render(); }
}
const request = (command,values={}) => send({type:'request',request:{command,...values}});
const library = edit => request('edit_animation_library',{edit});
async function copy(text) {
  try { await window.__TAURI__.clipboardManager.writeText(text);announce('Copied'); }
  catch { announce('Select and copy the text below.'); }
}
const timestamp = at => at?new Date(at).toLocaleString():'No updates yet';
const statusName = mode => modes.find(([id])=>id===mode)?.[1]??mode;
function render() {
  const primary=pages.some(([id])=>id===model.page);
  document.querySelector('#navigation').hidden=!primary;
  document.querySelector('#back-settings').hidden=primary;
  pageRoot.classList.toggle('primary-page',primary);
  pageRoot.setAttribute('aria-labelledby',primary?`tab-${model.page}`:'');
  renderMessage();
  const connection=document.querySelector('#connection');
  connection.textContent=model.connected?'Service connected':'Service unavailable · reconnecting…';
  connection.className=`connection ${model.connected?'online':'offline'}`;
  for(const node of document.querySelectorAll('#navigation button')) {
    node.setAttribute('aria-selected',String(node.dataset.page===model.page));node.tabIndex=node.dataset.page===model.page?0:-1;
  }
  // Preserve typing, selection, expanded sections and scroll during live updates.
  if(model.composing)return;
  const focused=pageRoot.contains(document.activeElement)?document.activeElement:null;
  const focus=focused?.id;
  const selection=(focused && ['text','password','search','url','tel'].includes(focused.type)) || focused?.tagName==='TEXTAREA'
    ? [focused.selectionStart,focused.selectionEnd]:null;
  const scroll=[window.scrollX,window.scrollY];
  fieldNumber=0;
  let content;
  if(!model.state && !['agents','setup'].includes(model.page)) content=[...header([...pages,...auxiliaryPages,['setup','Setup']].find(([id])=>id===model.page)[1],'Agent activity and device settings'),p('Waiting for the SidePulse service…','empty')];
  else content=renderers[model.page]();
  const fields=el('fieldset',{disabled:model.busy || (!model.connected && !['agents','setup'].includes(model.page))},...content);
  pageRoot.replaceChildren(fields);
  if(model.editor)fields.append(editorDialog());
  paintPreviews();
  if(focus) {
    const target=document.getElementById(focus);
    if(target && !target.disabled) { target.focus({preventScroll:true}); if(selection) target.setSelectionRange(...selection); }
  }
  window.scrollTo(...scroll);
}

function providerName(provider) { return {codex:'Codex',claude:'Claude',grok:'Grok',cursor:'Cursor',junie:'Junie'}[provider]??provider; }
function animationSelection(state,id) {
  const current=state.animation_states.find(a=>a.mode===id),style=state.animation_library.current[id]??current?.style??'default';
  return {style,program:state.animation_library.custom_animations[style]?.program??current?.program??''};
}
function terminalChoices() {
  return model.platform==='windows'?[['terminal','Windows Terminal / PowerShell'],['custom','Custom']]:[
    ['terminal',model.platform==='macos'?'Terminal':'System terminal'],...(model.platform==='macos'?[['iterm','iTerm'],['warp','Warp']]:[]),
    ['ghostty','Ghostty'],['kitty','Kitty'],['wezterm','WezTerm'],['alacritty','Alacritty'],['custom','Custom']];
}
function agents() {
  const setup=model.setup,s=model.state?.settings,d=draft('terminal')?.value;
  const hooks=card('Agent Hooks',!setup?note('Checking installed hooks…'):null,...(setup?.providers??[]).map(provider=>el('div',{class:'hook-row','data-provider':provider.provider},
    el('strong',{},providerName(provider.provider)),el('span',{class:'hook-status'},provider.error??(provider.native?'Installed':provider.installed?'Older hooks installed':'Not installed')),
    button('Install',()=>request('configure_hooks',{provider:provider.provider,install:true,dry_run:false}),!model.connected || !setup.configured),
    button('Uninstall',()=>request('configure_hooks',{provider:provider.provider,install:false,dry_run:false}),!model.connected || !setup.configured || !provider.installed))));
  const opening=s?card('Session Opening',el('div',{class:'session-columns'},
    el('div',{},...s.session_open_preferences.map(([provider,action])=>el('div',{class:'provider-row'},el('span',{},providerName(provider)),
      field(`${providerName(provider)} opener`,select(`opener-${provider}`,action,provider==='claude'?[['vscode','VS Code'],['app','Claude App'],['terminal','Terminal']]:provider==='codex'?[['app','Codex App'],['terminal','Terminal']]:[['terminal','Terminal']],
        action=>request('set_session_open_preference',{provider,origin:null,action}),!model.connected))))),
    el('div',{},el('h3',{},'Terminal App'),field('Terminal App',select('terminal-app',d.terminal,terminalChoices(),value=>{edit('terminal','terminal',value);render();},!model.connected)),
      d.terminal==='custom'?field(model.platform==='macos'?'Application path':'Terminal executable path',input('terminal-path',d.path,value=>edit('terminal','path',value))):note(d.path||'Use the selected installed terminal.'),
      saveButtons('terminal','Save terminal preference',()=>request('set_session_terminal',{terminal:d.terminal,custom_path:d.path}))))):null;
  return [...header('Agents','Agent hooks, session opening and transcript monitoring.'),hooks,opening,
    card('Transcript Monitoring',s?el('div',{class:'transcript-row'},...['codex','claude'].map(provider=>check(`${provider}-transcripts`,`CLI fallback: ${providerName(provider)} transcripts`,s.controls[`${provider}_transcripts`],enabled=>request('set_transcript_monitoring',{provider,enabled}),!model.connected))):note('Connect the service to change transcript monitoring.'))];
}
function advanced() {
  const s=model.state.settings,c=s.controls,d=draft('monitoring').value;
  const numeric=(label,id,value,change,unit,attrs)=>el('label',{class:'timing-row',for:id},el('span',{},label),input(id,value,change,{type:'number',...attrs}),el('span',{},unit));
  return [...header('Advanced','Agent list timing and background behavior.'),
    card('Agent List',numeric('Keep last 10 sessions for','retention-hours',d.retention,value=>edit('monitoring','retention',value),'hours',{min:0,max:8760}),
      numeric('Idle timeout','idle-minutes',d.idle,value=>edit('monitoring','idle',value),'minutes',{min:0,max:525600}),
      saveButtons('monitoring','Save agent list settings',()=>request('set_agent_list_settings',{patch:{idle_timeout_seconds:d.idle*60,recent_session_retention_seconds:d.retention*3600}}))),
    card('Sleep Prevention',model.state.power_control.supported?[
      numeric('Let Mac sleep on battery below','sleep-battery',draft('sleep').value.percent,value=>edit('sleep','percent',value),'%',{min:0,max:100}),
      saveButtons('sleep','Save battery safeguard',()=>request('set_sleep_settings',{patch:{min_battery_percent:draft('sleep').value.percent}}))]:note('Sleep prevention is currently available on macOS.')),
    card('',check('power-preview',`Show battery for ${s.power_change_preview_seconds}s on plug/unplug`,c.battery_power_preview,enabled=>request('set_battery_settings',{patch:{show_on_power_change:enabled}}))),
    card('',check('tray-visible',model.platform==='macos'?'Show menu bar icon':'Show system tray icon',c.visible,visible=>request('set_tray_visibility',{visible})),
      note('Reopen settings anytime: run sidepulse settings in Terminal.'))];
}
function ledPreview(id) { return el('div',{class:'led-preview','data-preview':id,role:'img','aria-label':`${id==='editor'?'Custom animation':animationRows.find(([state])=>state===id)?.[1]??id} live preview`},...Array.from({length:8},()=>el('span',{}))); }
function paintPreviews() {
  for(const node of pageRoot.querySelectorAll('[data-preview]')) {
    const frame=model.previews[node.dataset.preview];
    if(!frame)continue;
    node.title=frame.error??'';
    for(const [index,light] of [...node.children].entries()) {
      const color=frame.colors?.[index]??[0,0,0];
      light.style.backgroundColor=`rgb(${color.join(',')})`;light.style.color=light.style.backgroundColor;
    }
  }
  const error=pageRoot.querySelector('#editor-preview-status');if(error)error.textContent=model.previews.editor?.error??'Updates as you type';
}
function openEditor(type,state=null) {
  model.editor=type;
  if(type==='animation') {
    model.animationMode=state??'working';model.drafts.animation={value:animationSelection(model.state,model.animationMode),dirty:false,awaiting:0};
  }
  render();
  pageRoot.querySelector('[role=dialog] input,[role=dialog] textarea')?.focus();
}
function openCustomEditor(state) {
  const selected=model.state.animation_library.current[state],asset=model.state.animation_library.custom_animations[selected];
  model.assetId=asset?selected:null;model.assetState=state;
  model.assetName=asset?.name??`${model.state.animation_choices.find(choice=>choice.id===selected)?.name??'Animation'} Copy`;
  model.assetProgram=asset?.program??model.previews[state]?.program??model.state.animation_states.find(item=>item.mode===state)?.program??'#00E5FF';
  openEditor('asset');
}
function closeEditor() {if(model.busy)return;model.editor=null;render();}
function editorDialog() {
  const close=button('Cancel',closeEditor);
  let content,title;
  if(model.editor==='profile') {title='Save Animation Profile';content=[field('Profile Name',input('profile-name',model.profileName,value=>{model.profileName=value;})),actions(close,button('Save',()=>library({operation:'save_profile',id:null,name:model.profileName}),!model.profileName.trim(),'primary'))];}
  else if(model.editor==='profile-json') {title='Animation Profile JSON';content=[field('Import or export profile JSON',textArea('profile-json',model.profileJSON,value=>{model.profileJSON=value;},10)),actions(button('Copy JSON',()=>copy(model.profileJSON),!model.profileJSON),close,button('Import and apply',()=>{
    try{library({operation:'import_profile',document:JSON.parse(model.profileJSON)});}catch(error){announce(`Invalid profile JSON: ${error.message}`,true);}
  },!model.profileJSON.trim(),'primary'))];}
  else if(model.editor==='asset') {title=model.assetId?'Edit Custom Animation':'Add Custom Animation';content=[field('Name',input('asset-name',model.assetName,value=>{model.assetName=value;})),row(el('span',{},'Live Preview'),ledPreview('editor'),el('span',{id:'editor-preview-status',class:'muted'},'Updates as you type')),
    field('LED program',textArea('asset-program',model.assetProgram,value=>{model.assetProgram=value;},9)),
    actions(button('Show on Device',()=>request('preview_animation',{state:model.assetState??'working',program:model.assetProgram,seconds:10})),model.assetId?button('Delete animation',()=>library({operation:'delete_animation',id:model.assetId}),false,'danger'):null,close,
      button('Save',()=>library({operation:'save_animation',state:model.assetState,id:model.assetId,name:model.assetName,program:model.assetProgram}),!model.assetName.trim(),'primary'))];}
  else {const d=draft('animation').value;title=`Edit ${animationRows.find(([id])=>id===model.animationMode)?.[1]??model.animationMode}`;
    content=[field('Animation',select('animation-style',d.style,model.state.animation_choices.map(c=>[c.id,c.name]),value=>{edit('animation','style',value);render();})),
      row(el('span',{},'Live Preview'),ledPreview('editor'),el('span',{id:'editor-preview-status',class:'muted'},'Updates as you type')),
      field('Custom LED program',textArea('animation-program',d.program||'#00E5FF',value=>{edit('animation','program',value);edit('animation','style','custom');},9)),
      note('Enter a LEDS.LED program (maximum 512 bytes and 20 lines).'),
      actions(button('Show on Device',()=>request('preview_animation',{state:model.animationMode,program:d.style==='custom'?d.program:null,seconds:10})),close,
        button('Save animation',()=>request('set_animation_state',{state:model.animationMode,style:d.style,custom_program:d.style==='custom'?d.program:null}),!draft('animation').dirty,'primary'))];}
  return el('div',{class:'modal-overlay'},el('section',{class:'editor-dialog',role:'dialog','aria-modal':'true','aria-label':title},el('h2',{},title),...content));
}
function activity() {
  const s=model.state;
  const agentCard=(agent,recent=false)=> {
    const info=s.agents.find(a=>a.agent_id===agent.id);
    const options=s.session_actions[agent.id]??[];
    return card('',row(el('span',{class:`status-dot ${agent.icon}`}),el('span',{class:'agent-title'},agent.title),recent?el('span',{class:'badge'},'Recent'):null),p(agent.subtitle,'muted'),info?.cwd?el('code',{},info.cwd):null,
      agent.can_open?actions(button('Open session',()=>request('session_targets',{agent_id:agent.id,action:null})),
        options.length?field('Open with',select(`open-${agent.id}`,'',[['','Choose…'],...options.map(o=>[o.action,o.label])],action=>action&&request('session_targets',{agent_id:agent.id,action}))):null):null);
  };
  return [...header('Activity',s.activity.tooltip),...s.activity.rows.map(a=>agentCard(a)),
    !s.activity.rows.length?p('No active agents.','empty'):null,
    s.activity.stale_rows.length?details('recent-sessions','Recent sessions',...s.activity.stale_rows.map(a=>agentCard(a,true))):null];
}
function brightness(id,value,handler,disabled=false) {
  const range=input(id,value,()=>{},{type:'range',min:0,max:255,disabled});
  const output=el('output',{for:id},`${Math.round(value*100/255)}%`);
  range.addEventListener('input',()=>output.textContent=`${Math.round(Number(range.value)*100/255)}%`);
  range.addEventListener('change',()=>handler(Number(range.value)));
  return row(range,output);
}
function devices() {
  const s=model.state,c=s.settings.controls;
  return [...header('Devices','Choose which device shows your agent or battery status.'),card('Connected devices',
    !s.devices.length?note('No SidePulse devices connected.'):s.devices.map((d,i)=>row(button(s.device_names[i],()=>request('select_device',{root:d.root}),d.target===s.active_device,d.target===s.active_device?'primary':''),el('span',{class:'path'},d.root))),
    field('Brightness',brightness('brightness',c.brightness??255,value=>request('set_brightness',{brightness:value}),c.brightness===null)),
    field('Display',select('device-display',c.display_mode,displays,mode=>request('set_display_mode',{mode}),c.display_mode===null)),note('Manual output keeps the program already on the device.')),
    card('Virtual display',check('virtual-enabled','Show status on screen',s.settings.virtual_display_enabled,enabled=>request('set_virtual_display',{patch:{enabled}})),
      note(model.platform==='macos'?'Appears beneath the notch, or at the top of a screen without a notch.':'Appears in a movable status window.'),
      field('Virtual brightness',brightness('virtual-brightness',s.settings.virtual_display_brightness,brightness=>request('set_virtual_display',{patch:{brightness}}),!s.settings.virtual_display_enabled)),
      field('Virtual display mode',select('virtual-mode',s.settings.virtual_display_mode,displays,display=>request('set_virtual_display',{patch:{display}}),!s.settings.virtual_display_enabled)),note('Manual output hides the virtual display.'))];
}
function battery() {
  const d=draft('battery').value;
  return [...header('Battery','Set how battery status appears on your devices.'),card('Charger and power changes',
    check('baseline-auto','Detect full-speed charger wattage automatically',d.auto,value=>{edit('battery','auto',value);render();}),
    field('Full-speed charger (watts)',input('charger-watts',d.watts,value=>edit('battery','watts',value),{type:'number',min:1,max:1000,disabled:d.auto})),
    check('power-preview','Show battery briefly when power is connected or disconnected',d.preview,value=>{edit('battery','preview',value);render();}),
    field('Preview duration (seconds)',input('preview-seconds',d.seconds,value=>edit('battery','seconds',value),{type:'number',min:0,max:3600,disabled:!d.preview})),
    saveButtons('battery','Save battery settings',()=>request('set_battery_settings',{patch:{full_charge_watts:d.auto?{kind:'auto'}:{kind:'watts',watts:d.watts},show_on_power_change:d.preview,power_change_preview_seconds:d.seconds}})))];
}
function sleep() {
  const s=model.state,power=s.power_control,d=draft('sleep').value;
  if(!power.supported)return [...header('Sleep prevention','Sleep prevention is currently available on macOS.'),note('Agent monitoring and device output are available on this computer.')];
  return [...header('Sleep prevention','Choose when SidePulse keeps your Mac awake.'),card('Keep awake',
    p(!power.enabled?'Sleep prevention is paused in this session.':power.active?'SidePulse is keeping your Mac awake.':'SidePulse is allowing your Mac to sleep.'),
    power.error?p(power.error,'error'):null,power.error?button('Retry keeping awake',()=>request('retry_power_control')):null,
    field('When to prevent sleep',select('sleep-policy',s.settings.controls.sleep_policy,[['never','Never'],['agents','While agents work'],['always','Always']],policy=>request('set_sleep_policy',{policy})))),
    card('Battery safeguard',field('Allow sleep when battery falls below (%)',input('sleep-battery',d.percent,value=>edit('sleep','percent',value),{type:'number',min:0,max:100})),
      note('The safeguard applies while your Mac is running on battery power.'),saveButtons('sleep','Save battery safeguard',()=>request('set_sleep_settings',{patch:{min_battery_percent:d.percent}})))];
}
function relay() {
  const r=model.state.relay,d=draft('relay').value;
  if(!r.configured)return [...header('Link computers','See agent activity from your other computers.'),note('Relay is unavailable in this session.')];
  return [...header('Link computers','See agent activity from your other computers.'),card('Receive activity here',
    r.receiver_code?[
      row(el('code',{},r.receiver_code),button('Copy code',()=>copy(r.receiver_code))),note('Use this code to link the sending computer.'),
      actions(button('Replace code',()=>request('set_relay_settings',{patch:{rotate_receiver:true}})),button('Stop receiving',()=>request('set_relay_settings',{patch:{receiver_enabled:false}}))),
      note(`Last activity received: ${timestamp(r.last_received_at)}`),r.receive_error?p(r.receive_error,'error'):null,
    ]:button('Create receiving code',()=>request('set_relay_settings',{patch:{receiver_enabled:true}}))),
    card('Send activity to another computer',field('Receiving code from the other computer',input('relay-outbound',d.outbound,value=>edit('relay','outbound',value))),
      field("This computer’s name",input('relay-name',d.name,value=>edit('relay','name',value))),
      details('relay-server','Server',field('Relay server',input('relay-server-url',d.server,value=>edit('relay','server',value)))),
      saveButtons('relay','Save link',()=>request('set_relay_settings',{patch:{server:d.server,machine_name:d.name,outbound_code:d.outbound}})),
      actions(button('Reload saved link',()=>request('reload_relay_settings')),
        r.outbound_code?button('Disconnect sending link',()=>request('set_relay_settings',{patch:{outbound_code:''}})):null),
      r.outbound_code?note(`Last activity sent: ${timestamp(r.last_sent_at)}`):null,r.send_error?p(r.send_error,'error'):null)];
}
function svg(tag,attrs={}) {
  const node=document.createElementNS('http://www.w3.org/2000/svg',tag);
  for(const [key,value] of Object.entries(attrs))node.setAttribute(key,String(value));return node;
}
function pairingQR(matrix) {
  const n=matrix.length;
  if(!n || n>512 || !matrix.every(row=>row.length===n))return p('Pairing code unavailable. Use the pairing link below.','error');
  const node=svg('svg',{viewBox:`0 0 ${n} ${n}`,class:'qr',role:'img','aria-label':'Phone pairing QR code','shape-rendering':'crispEdges'});
  node.append(svg('rect',{width:n,height:n,fill:'white'}));
  matrix.forEach((line,y)=>line.forEach((dark,x)=>{if(dark)node.append(svg('rect',{x,y,width:1,height:1,fill:'black'}));}));return node;
}
function phones() {
  const s=model.state,pair=s.phone_pairing;
  if(!s.phones_configured)return [...header('Link phones','Send LED programs and notifications to a phone running SidePulse.'),note('Phone linking is unavailable in this session.')];
  return [...header('Link phones','Send LED programs and notifications to a phone running SidePulse.'),!s.phone_output_enabled?note('Automatic phone updates are paused in this session.'):null,
    card('Pair a phone',pair?[
      pair.state==='awaiting'?[p('Scan this code with SidePulse on your phone.'),pairingQR(pair.qr),
        field('Pairing link',input('pairing-link',pair.url,()=>{},{readonly:true})),
        actions(button('Copy pairing link',()=>copy(pair.url)),button('Cancel pairing',()=>request('cancel_phone_pairing'))),note(`Expires: ${timestamp(pair.expires_at)}`)]
      :p({linked:'Phone linked.',cancelled:'Pairing cancelled.',expired:'Pairing expired. Start again to get a new code.'}[pair.state]??'Pairing could not finish.'),
      pair.message?note(pair.message):null,
    ]:note('Start pairing to create a code.'),actions(button('Start new pairing',()=>request('begin_phone_pairing',{server:null}),false,'primary'))),
    card('Saved phones',!s.phones.length?note('No phones linked yet.'):null,...s.phones.map(phone=>el('section',{},row(el('strong',{},phone.name),el('span',{class:'badge'},phone.id),
      button('Remove',()=>request('remove_phone',{id:phone.id}),false,'danger')),
      field('Display',select(`phone-display-${phone.id}`,phone.display,displays,display=>request('set_phone_display',{id:phone.id,display}))),
      phone.last_sent_at?note(`Last update sent: ${timestamp(phone.last_sent_at)}`):null,phone.delivery_error?p(`Could not send an update: ${phone.delivery_error}`,'error'):null)),
      actions(button('Reload saved phones',()=>request('reload_phone_links'))),details('phone-manual','Link with a push token',
        field('Phone name',input('phone-name',model.phoneName,value=>{model.phoneName=value;})),
        field('Push token',input('phone-token',model.phoneToken,value=>{model.phoneToken=value;},{type:'password',autocomplete:'off'})),
        actions(button('Save phone',()=>request('register_phone',{name:model.phoneName,token:model.phoneToken,server:null}),!model.phoneToken.trim(),'primary'))))];
}
function animations() {
  const s=model.state,lib=s.animation_library;
  const selected=model.profileId || lib.matching_profile || '';
  const protectedProfile=id=>['profile:cyan','profile:ember','profile:purple'].includes(id);
  const profileChoices=[['','Custom selection'],...Object.entries(lib.profiles).map(([id,profile])=>[id,profile.name])];
  const rows=animationRows.map(([id,label])=>{
    const picker=select(`animation-${id}`,lib.current[id]??'default',s.animation_choices.map(c=>[c.id,c.name]),style=>{
      if(style==='custom')openCustomEditor(id);
      else request('set_animation_state',{state:id,style,custom_program:null});
    });
    return el('tr',{'data-animation-state':id},el('td',{},label),el('td',{},picker),el('td',{},ledPreview(id)),
      el('td',{},actions(button('Show',()=>request('preview_animation',{state:id,program:null,seconds:3})),button('Edit',()=>openCustomEditor(id)))));
  });
  const table=el('table',{class:'animation-table'},el('thead',{},el('tr',{},...['State','Animation','Live Preview','Actions'].map(label=>el('th',{scope:'col'},label)))),el('tbody',{},...rows));
  return [...header('Animations','Profiles and animations for each agent and lid state.'),
    el('div',{class:'animation-toolbar'},el('label',{for:'animation-profile'},'Profile'),select('animation-profile',selected,profileChoices,id=>{model.profileId=id;if(id)library({operation:'apply_profile',id});}),
      button('Save Profile…',()=>openEditor('profile')),button('Delete',()=>library({operation:'delete_profile',id:selected}),!selected || protectedProfile(selected),'danger')),
    el('div',{class:'animation-toolbar'},button('Export JSON',()=>{model.editor='profile-json';request('export_animation_profile',{id:null});}),button('Import JSON',()=>openEditor('profile-json')),
      button('Add Custom…',()=>{model.assetId=null;model.assetState=null;model.assetName='';model.assetProgram='#00E5FF';openEditor('asset');})),table,
    details('custom-animations','Saved custom animations',...Object.entries(lib.custom_animations).map(([id,asset])=>row(el('span',{},asset.name),button('Edit',()=>{model.assetId=id;model.assetState=null;model.assetName=asset.name;model.assetProgram=asset.program;openEditor('asset');})))),
    details('lid-timing','Lid transition timing',note('Custom transitions use this timing; default presets use their built-in timing.'),
      el('div',{class:'grid'},field('Open duration (seconds)',input('lid-open-seconds',draft('lid_timing').value.open,value=>edit('lid_timing','open',value),{type:'number',min:.1,max:10,step:.1})),
        field('Close duration (seconds)',input('lid-close-seconds',draft('lid_timing').value.close,value=>edit('lid_timing','close',value),{type:'number',min:.1,max:10,step:.1}))),
      saveButtons('lid_timing','Save timing',()=>request('set_lid_animation_timing',{open_seconds:draft('lid_timing').value.open,close_seconds:draft('lid_timing').value.close})))];
}
function setup() {
  const s=model.setup;
  const startupButtons=(job)=>actions(...[['Enable at login','install'],['Start now','start'],['Stop now','stop'],['Disable','uninstall'],['Check status','status']].map(([label,operation])=>button(label,()=>send({type:'startup',job,operation,dry_run:false}))),
    button('Preview login setup',()=>send({type:'startup',job,operation:'install',dry_run:true})));
  return [...header('SidePulse Setup',model.platform==='macos'?'Finish setup for this Mac.':'Finish setup for this computer.'),!s?note('Open the staged Settings application to configure startup.'):null,
    s?details('hook-change-preview','Preview agent hook changes',...s.providers.map(provider=>row(el('span',{},providerName(provider.provider)),
      button('Preview change',()=>request('configure_hooks',{provider:provider.provider,install:true,dry_run:true}),!model.connected || !s.configured)))):null,
    s?card('Run at login',note('Enable the monitor and status bar to keep SidePulse available after signing in.'),
      !s.stage_dir?note('Stage the native package to enable login setup.'):el('div',{},el('h3',{},'Monitor'),startupButtons('service'),el('h3',{},'Status bar'),startupButtons('tray'))):null,
    s?.stage_dir && model.platform==='macos'?card('SD eject protection',note('Keep SidePulse Pro and SidePulse Dot available after sleep.'),
      actions(...[['Enable at login','install'],['Remove','uninstall'],['Check status','status']].map(([label,operation])=>button(label,()=>send({type:'startup',job:'sd-guard',operation,dry_run:false}))))):null,
    s?.stage_dir && model.platform==='macos'?card('Closed-lid sleep prevention',note('Open a one-time administrator setup in Terminal.'),
      actions(...[['Open administrator setup','install'],['Remove setup','uninstall'],['Check status','status']].map(([label,operation])=>button(label,()=>send({type:'sleep_helper',operation}))))):null];
}
function diagnostics() {
  const d=model.state.diagnostics;
  return [...header('Diagnostics','Export agent events for troubleshooting.'),card('Debug Log',p(`${d.audit_bytes.toLocaleString()} bytes recorded`),
    ...d.audit_paths.map(path=>el('code',{class:'path'},path)),
    actions(button('Export CSV',()=>request('export_diagnostics',{format:'csv'}),!d.export_directory),button('Export HTML',()=>request('export_diagnostics',{format:'html'}),!d.export_directory),
      model.lastExport?button('Open export',()=>send({type:'open_export'})):null),
    model.lastExport?el('code',{class:'path'},model.lastExport):null,d.export_directory?note(`Exports are saved in ${d.export_directory}`):null),
    card('Settings File',el('code',{class:'path'},d.settings_path??'No settings file configured')),
    details('diagnostic-paths','Status history file',el('code',{class:'path'},d.history_path??'No history file configured'))];
}
function history() {
  const s=model.state,points=s.history_points;
  const top=[...header('History','Status observations over time.'),el('div',{class:'history-toolbar'},el('h2',{},'Status History'),
    field('Timeframe',select('history-timeframe',String(s.history_timeframe),[3600,21600,43200,86400,172800].map(seconds=>[String(seconds),`Last ${seconds/3600}h`]),seconds=>request('set_history_timeframe',{seconds:Number(seconds)})))),
    note(`${points.length} samples${s.history_sampled?' · Showing a summary of the recorded observations.':''}`)];
  if(!points.length)return [...top,p('No history yet. New observations appear while the service is running.','empty')];
  const width=532,height=250,left=0,right=532,first=Date.parse(points[0].recorded_at),last=Date.parse(points[points.length-1].recorded_at);
  const span=Math.max(1,last-first),x=point=>left+(Date.parse(point.recorded_at)-first)/span*(right-left);
  const chart=svg('svg',{class:'history',viewBox:`0 0 ${width} ${height}`,preserveAspectRatio:'none',role:'img','aria-label':'Battery, charger, agent status, awake, sleep and lid history',tabindex:'0'});
  const maxWatts=Math.max(10,...points.map(p=>p.charger_power_watts??0));
  const labels=el('div',{class:'chart-labels'},
    ...[['agent-label','Agent'],['battery-label','Battery','0–100%'],['charger-label','Charger',`0–${Math.ceil(maxWatts)} W`],
      ['awake-label','SidePulse'],['sleep-label','macOS Sleep'],['lid-label','Lid']]
      .map(([cls,text,range])=>el('span',{class:cls},text,range?el('small',{},range):null)));
  for(const y of [24,58,82,94,118,140,176,212])chart.append(svg('line',{x1:left,x2:right,y1:y,y2:y,stroke:'var(--line)','stroke-width':1}));
  const color=mode=>['working','tool_running','long_task_progress'].includes(mode)?'#32becf':['blocked_error','waiting_for_input'].includes(mode)?'#e19b37':mode==='completed'?'#50be82':'#8290a2';
  const bit=(value,on,off)=>value===null||value===undefined?'#536070':value?on:off;
  for(let i=0;i<points.length;i++) {
    const a=points[i],b=points[i+1],xa=x(a),xb=b?x(b):Math.min(right,xa+2);
    if(b)for(const [key,max,bottom,hue,h] of [['battery_level',100,82,'#50be82',24],['charger_power_watts',maxWatts,118,'#5fa5e6',24]]) {
      if(a[key]!==null && b[key]!==null)chart.append(svg('line',{x1:xa,x2:xb,y1:bottom-Math.max(0,Math.min(max,a[key]))/max*h,y2:bottom-Math.max(0,Math.min(max,b[key]))/max*h,stroke:hue,'stroke-width':2}));
    }
    for(const [y,fill] of [[18,color(a.agent_status)],[134,bit(a.keep_awake_active,'#32becf',a.keep_awake_requested?'#e19b37':'#8290a2')],
      [170,bit(a.mac_sleep_prevented,'#e19b37','#5fa5e6')],[206,bit(a.lid_closed,'#e19b37','#50be82')]])
      chart.append(svg('rect',{x:Math.min(xa,right-1),y,width:Math.max(1,xb-xa),height:12,fill}));
  }
  const cursor=svg('line',{x1:left,x2:left,y1:12,y2:226,stroke:'currentColor','stroke-width':1,'stroke-dasharray':'3 3'});chart.append(cursor);
  let hovered=points.length-1;
  const tip=el('div',{class:'chart-tooltip',role:'status','aria-live':'polite'});
  const show=index=>{
    hovered=Math.max(0,Math.min(points.length-1,index));const point=points[hovered],at=x(point);
    cursor.setAttribute('x1',at);cursor.setAttribute('x2',at);
    const yes=value=>value===null||value===undefined?'Unknown':value?'Yes':'No';
    tip.textContent=`${timestamp(point.recorded_at)} · ${statusName(point.agent_status)} · Battery: ${point.battery_level===null?'Unknown':`${point.battery_level.toFixed(0)}%`} · Charger: ${point.charger_power_watts===null?'Unknown':`${point.charger_power_watts.toFixed(1)} W`} · Lid: ${point.lid_closed===null?'Unknown':point.lid_closed?'Closed':'Open'} · Keeping awake: ${yes(point.keep_awake_active)} · Mac sleep prevented: ${yes(point.mac_sleep_prevented)}`;
  };
  chart.addEventListener('pointermove',event=>{const target=(event.clientX-chart.getBoundingClientRect().left)/chart.getBoundingClientRect().width*width;
    let nearest=0;for(let i=1;i<points.length;i++)if(Math.abs(x(points[i])-target)<Math.abs(x(points[nearest])-target))nearest=i;show(nearest);});
  chart.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();show(event.key==='Home'?0:event.key==='End'?points.length-1:hovered+(event.key==='ArrowLeft'?-1:1));}});
  show(hovered);
  return [...top,card('',el('div',{class:'history-grid'},labels,chart),
    el('div',{class:'row spread'},el('small',{},timestamp(points[0].recorded_at)),el('small',{},timestamp(points[points.length-1].recorded_at))),tip,
    row(button('Refresh',async()=>{try{applyPoll(await invoke('settings_poll',{afterRevision:0,previewProgram:null}));}catch(error){announce(error,true);}}),el('span',{class:'history-caption'},'Rows: agent display, battery %, charger W, SidePulse awake, macOS sleep, lid.')),
    el('p',{class:'history-caption'},'Agent: cyan working · amber attention · green completed. Awake: cyan active · amber requested. Sleep: amber prevented · blue allowed. Lid: amber closed · green open. Dim bands indicate unknown observations.'))];
}
const renderers={agents,advanced,animations,history,diagnostics,activity,devices,battery,sleep,relay,phones,setup};
function navigate(id) {
  if(model.editor)return;
  model.page=id;if(pages.some(([page])=>page===id))model.settingsPage=id;
  document.querySelector('#controls-menu').hidden=true;document.querySelector('#controls-toggle').setAttribute('aria-expanded','false');
  render();window.scrollTo(0,0);
}
for(const [id,label] of pages)document.querySelector('#navigation').append(el('button',{id:`tab-${id}`,'data-page':id,role:'tab',type:'button','aria-controls':'page',onclick:()=>navigate(id)},label));
for(const [id,label] of auxiliaryPages)document.querySelector('#controls-menu').append(button(label,()=>navigate(id)));
document.querySelector('#setup-open').addEventListener('click',()=>navigate('setup'));
document.querySelector('#back-settings').addEventListener('click',()=>navigate(model.settingsPage));
document.querySelector('#controls-toggle').addEventListener('click',()=>{const menu=document.querySelector('#controls-menu');menu.hidden=!menu.hidden;document.querySelector('#controls-toggle').setAttribute('aria-expanded',String(!menu.hidden));});
document.querySelector('#navigation').addEventListener('keydown',event=>{
  if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();
  const current=pages.findIndex(([id])=>id===model.page),next=event.key==='Home'?0:event.key==='End'?pages.length-1:(current+(event.key==='ArrowLeft'?-1:1)+pages.length)%pages.length;
  navigate(pages[next][0]);document.querySelector(`#tab-${pages[next][0]}`).focus();
});
document.addEventListener('keydown',event=>{
  if(event.key==='Escape'){if(model.editor){event.preventDefault();closeEditor();}else {document.querySelector('#controls-menu').hidden=true;document.querySelector('#controls-toggle').setAttribute('aria-expanded','false');}}
  if(event.key==='Tab'&&model.editor){const controls=[...pageRoot.querySelectorAll('[role=dialog] button:not(:disabled),[role=dialog] input:not(:disabled),[role=dialog] select:not(:disabled),[role=dialog] textarea:not(:disabled)')];
    if(controls.length && event.shiftKey && document.activeElement===controls[0]){event.preventDefault();controls[controls.length-1].focus();}
    else if(controls.length && !event.shiftKey && document.activeElement===controls[controls.length-1]){event.preventDefault();controls[0].focus();}}
});
pageRoot.addEventListener('compositionstart',()=>{model.composing=true;});
pageRoot.addEventListener('compositionend',()=>{model.composing=false;render();});
function applyPoll(result) {
  const changed=Boolean(result.state) || result.events.length>0 || model.connected!==result.connected || model.busy!==result.busy || JSON.stringify(model.setup)!==JSON.stringify(result.setup);
  model.connected=result.connected;model.platform=result.platform;model.setup=result.setup;
  for(const event of result.events) {
    if(['saved','export','profile_export'].includes(event.type) || event.completed)model.pendingAction=false;
    if(event.type==='saved') {
      const d=draft(event.draft);
      if(!event.error && d && !(event.draft==='battery' && model.submittedRequest?.patch?.full_charge_watts===undefined)){d.dirty=false;d.awaiting=(event.revision??model.revision)+1;}
      if(!event.error && ['animation','library'].includes(event.draft))model.editor=null;
      if(!event.error && event.draft==='phone')model.phoneToken='';
      announce(event.error?`Could not save: ${event.error}`:'Saved',Boolean(event.error));
    } else if(event.type==='export') {model.lastExport=event.path;announce(`Exported ${event.count} debug events to ${event.path}`);}
    else if(event.type==='profile_export'){model.profileJSON=event.document;announce('Profile ready to copy or save');}
    else if(event.type==='message')announce(event.message,event.error);
  }
  model.busy=result.busy || model.pendingAction;
  if(result.state){model.state=result.state;model.revision=result.revision;syncDrafts();}
  if(result.previews)model.previews=result.previews;
  if(changed)render();else paintPreviews();
}
async function poll() {
  try { applyPoll(await invoke('settings_poll',{afterRevision:model.revision,previewProgram:model.editor==='asset'?model.assetProgram:model.editor==='animation' && draft('animation').value.style==='custom'?draft('animation').value.program:null})); }
  catch(error) {model.connected=false;model.busy=false;model.pendingAction=false;announce(`Settings client unavailable: ${error}`,true);render();}
  setTimeout(poll,model.page==='animations'?120:500);
}
render();poll();
