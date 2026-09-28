// Presentation only: typed actions go to the Rust host. All monitoring,
// validation, persistence, device output and platform operations stay in Rust.
const pages = [
  ['activity','Activity'], ['devices','Devices'], ['battery','Battery'],
  ['monitoring','Monitoring'], ['sleep','Sleep'], ['animations','Animations'],
  ['history','History'], ['sessions','Sessions'], ['relay','Link computers'],
  ['phones','Link phones'], ['setup','Setup'], ['diagnostics','Diagnostics'],
];
const modes = [['idle_ready','Idle / Ready'],['working','Working'],['tool_running','Tool running'],
  ['waiting_for_input','Waiting for input'],['long_task_progress','Long task progress'],
  ['blocked_error','Blocked / Error'],['completed','Completed'],['unknown','Unknown']];
const displays = [['agent','Agent status'],['battery','Battery level'],['custom','Manual output']];
const model = {page:'activity', revision:0, state:null, setup:null, connected:false, busy:false, pendingAction:false,
  platform:'', drafts:{}, message:'', error:false, lastExport:null, animationMode:'working',
  profileName:'', profileJSON:'', assetId:null, assetName:'', assetProgram:'#00E5FF',
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
  if(group){for(const button of group.querySelectorAll('button'))button.disabled=Boolean(draft(name).awaiting);group.querySelector('.dirty').hidden=false;}
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
    case 'animation': { const current=state.animation_states.find(a=>a.mode===model.animationMode);return {style:current?.style??'',program:current?.program??''}; }
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
  group.dataset.draft=name;return group;
}
async function send(action) {
  if(model.busy)return;
  for(const field of pageRoot.querySelectorAll('input,textarea,select'))if(!field.checkValidity()){field.reportValidity();return;}
  model.busy=true;model.pendingAction=true;announce('Working…');
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
  renderMessage();
  const connection=document.querySelector('#connection');
  connection.textContent=model.connected?'Service connected':'Service unavailable · reconnecting…';
  connection.className=`connection ${model.connected?'online':'offline'}`;
  for(const node of document.querySelectorAll('nav button')) {
    if(node.dataset.page===model.page)node.setAttribute('aria-current','page'); else node.removeAttribute('aria-current');
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
  if(!model.state && model.page!=='setup') content=[...header(pages.find(([id])=>id===model.page)[1],'Agent activity and device settings'),p('Waiting for the SidePulse service…','empty')];
  else content=renderers[model.page]();
  const fields=el('fieldset',{disabled:model.busy || (!model.connected && model.page!=='setup')},...content);
  pageRoot.replaceChildren(fields);
  if(focus) {
    const target=document.getElementById(focus);
    if(target && !target.disabled) { target.focus({preventScroll:true}); if(selection) target.setSelectionRange(...selection); }
  }
  window.scrollTo(...scroll);
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
function monitoring() {
  const s=model.state.settings,c=s.controls,d=draft('monitoring').value;
  return [...header('Monitoring','Provider hooks supply activity. Optional transcript monitoring adds events from saved sessions.'),
    card('Activity sources',...['codex','claude'].map(provider=>check(`${provider}-transcripts`,`${provider==='codex'?'Codex':'Claude'} transcripts`,c[`${provider}_transcripts`],enabled=>request('set_transcript_monitoring',{provider,enabled})))),
    card('Status bar',check('tray-visible','Show status bar icon',c.visible,visible=>request('set_tray_visibility',{visible})),note('You can reopen Settings from the SidePulse command line to restore a hidden icon.')),
    card('Agent list',field('Consider inactive after (minutes)',input('idle-minutes',d.idle,value=>edit('monitoring','idle',value),{type:'number',min:0,max:525600})),
      field('Keep completed sessions for (hours)',input('retention-hours',d.retention,value=>edit('monitoring','retention',value),{type:'number',min:0,max:8760})),
      saveButtons('monitoring','Save agent list settings',()=>request('set_agent_list_settings',{patch:{idle_timeout_seconds:d.idle*60,recent_session_retention_seconds:d.retention*3600}})))];
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
function sessions() {
  const s=model.state.settings,d=draft('terminal').value;
  const choices=model.platform==='windows'?[['terminal','Windows Terminal / PowerShell']]:[
    ['terminal',model.platform==='macos'?'Terminal':'System terminal'],...(model.platform==='macos'?[['iterm','iTerm'],['warp','Warp']]:[]),
    ['ghostty','Ghostty'],['kitty','Kitty'],['wezterm','WezTerm'],['alacritty','Alacritty'],['custom','Custom']];
  return [...header('Session opening','Choose where agent sessions open when you select them.'),card('Default openers',
    ...s.session_open_preferences.map(([provider,action])=>field(provider==='codex'?'Codex':provider==='claude'?'Claude':'Grok',
      select(`opener-${provider}`,action,provider==='claude'?[['vscode','VS Code'],['app','Claude App'],['terminal','Terminal']]:provider==='codex'?[['app','Codex App'],['terminal','Terminal']]:[['terminal','Terminal']],
        action=>request('set_session_open_preference',{provider,origin:null,action}))))),
    card('Terminal app',field('Preferred terminal',select('terminal-app',d.terminal,choices,value=>{edit('terminal','terminal',value);render();})),
      d.terminal==='custom'?field(model.platform==='macos'?'Application path':'Terminal executable path',input('terminal-path',d.path,value=>edit('terminal','path',value))):null,
      saveButtons('terminal','Save terminal preference',()=>request('set_session_terminal',{terminal:d.terminal,custom_path:d.path})),note('Choose an installed terminal. Session opening is available from Activity and the tray.'))];
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
  const s=model.state,d=draft('animation'),a=d.value,lib=s.animation_library;
  const choices=s.animation_choices.map(c=>[c.id,c.name]);
  const protectedProfile=id=>['profile:cyan','profile:ember','profile:purple'].includes(id);
  const editor=card('Agent status',field('Status',select('animation-mode',model.animationMode,modes,value=>{
    model.animationMode=value;model.drafts.animation={value:draftSource('animation',model.state),dirty:false,awaiting:0};render();
  },d.dirty || Boolean(d.awaiting))),
    field('Animation',select('animation-style',a.style,choices,value=>{edit('animation','style',value);if(value==='custom'&&!a.program.trim())edit('animation','program','#00E5FF 500ms pulse\nrepeat\n');render();})),
    ['working','tool_running','long_task_progress'].includes(model.animationMode)?note('Working, tool running, and long task progress share their animation.'):null,
    a.style==='custom'?[field('Custom LED program',textArea('animation-program',a.program,value=>edit('animation','program',value),9)),note('The program is checked before it is saved or sent to a device.')]:null,
    saveButtons('animation','Save animation',()=>request('set_agent_animation',{mode:model.animationMode,style:a.style,custom_program:a.style==='custom'?a.program:null})));
  const libField=el('fieldset',{disabled:d.dirty || Boolean(d.awaiting)},
    details('profiles','Animation profiles',p(`Current: ${lib.profiles[lib.matching_profile]?.name??'Custom selection'}`),
      ...Object.entries(lib.profiles).map(([id,profile])=>row(el('strong',{},profile.name),
        button('Apply',()=>library({operation:'apply_profile',id})),button('Export',()=>request('export_animation_profile',{id})),
        !protectedProfile(id)?button('Delete',()=>library({operation:'delete_profile',id}),false,'danger'):null)),
      field('Profile name',input('profile-name',model.profileName,value=>{model.profileName=value;})),
      actions(button('Save current as profile',()=>library({operation:'save_profile',id:null,name:model.profileName}),!model.profileName.trim()),
        button('Export current selection',()=>request('export_animation_profile',{id:null}))),
      field('Import or export profile JSON',textArea('profile-json',model.profileJSON,value=>{model.profileJSON=value;},8)),
      actions(button('Copy JSON',()=>copy(model.profileJSON),!model.profileJSON),button('Import and apply',()=>{
        try{library({operation:'import_profile',document:JSON.parse(model.profileJSON)});}catch(error){announce(`Invalid profile JSON: ${error.message}`,true);}
      },!model.profileJSON.trim()))),
    details('custom-animations','Named custom animations',field('Animation',select('named-animation',model.assetId??'',
      [['','New animation'],...Object.entries(lib.custom_animations).map(([id,asset])=>[id,asset.name])],id=>{
        model.assetId=id||null;model.assetName=lib.custom_animations[id]?.name??'';model.assetProgram=lib.custom_animations[id]?.program??'#00E5FF';render();
      })),field('Animation name',input('asset-name',model.assetName,value=>{model.assetName=value;})),
      field('LED program',textArea('asset-program',model.assetProgram,value=>{model.assetProgram=value;},8)),
      actions(button('Save named animation',()=>library({operation:'save_animation',id:model.assetId,name:model.assetName,program:model.assetProgram}),!model.assetName.trim(),'primary'),
        model.assetId?button('Delete animation',()=>library({operation:'delete_animation',id:model.assetId}),false,'danger'):null)),
    details('lid-transitions','Lid transition animations',...['lid_open','lid_closed'].map((state,index)=>field(index?'Closing the lid':'Opening the lid',
      select(state,lib.current[state],choices.filter(([id])=>id!=='custom'),style=>request('set_animation_state',{state,style,custom_program:null})))),
      note('Custom transition duration; default lid presets use their built-in timing.'),
      el('div',{class:'grid'},field('Open duration (seconds)',input('lid-open-seconds',draft('lid_timing').value.open,value=>edit('lid_timing','open',value),{type:'number',min:.1,max:10,step:.1})),
        field('Close duration (seconds)',input('lid-close-seconds',draft('lid_timing').value.close,value=>edit('lid_timing','close',value),{type:'number',min:.1,max:10,step:.1}))),
      saveButtons('lid_timing','Save timing',()=>request('set_lid_animation_timing',{open_seconds:draft('lid_timing').value.open,close_seconds:draft('lid_timing').value.close}))));
  return [...header('Animations','Choose device animations for agent activity and lid transitions.'),editor,card('Library',libField),d.dirty?note('Save or reset your status animation before changing profiles.'):null];
}
function setup() {
  const s=model.setup;
  const startupButtons=(job)=>actions(...[['Enable at login','install'],['Start now','start'],['Stop now','stop'],['Disable','uninstall'],['Check status','status']].map(([label,operation])=>button(label,()=>send({type:'startup',job,operation,dry_run:false}))),
    button('Preview login setup',()=>send({type:'startup',job,operation:'install',dry_run:true})));
  return [...header('Setup','Connect your agents and choose what starts at login.'),!s?note('Open the staged Settings application to configure startup.'):null,
    s?card('Agent hooks',note('Existing settings and unrelated hooks are kept. Changed files receive a backup.'),
      !s.configured?note('The native hook executable is unavailable.'):null,
      ...s.providers.map(provider=>el('section',{},row(el('strong',{},provider.provider==='claude'?'Claude':provider.provider==='codex'?'Codex':provider.provider==='cursor'?'Cursor':provider.provider==='junie'?'Junie':'Grok'),
        el('span',{class:'badge'},provider.native?'Installed':provider.installed?'Older hooks installed':'Not installed')),
        actions(button('Install',()=>request('configure_hooks',{provider:provider.provider,install:true,dry_run:false}),!model.connected || !s.configured),
          button('Remove',()=>request('configure_hooks',{provider:provider.provider,install:false,dry_run:false}),!model.connected || !s.configured || !provider.installed),
          button('Preview change',()=>request('configure_hooks',{provider:provider.provider,install:true,dry_run:true}),!model.connected || !s.configured)),
        provider.error?p(provider.error,'error'):null))):null,
    s?card('Run at login',note('Enable the monitor and status bar to keep SidePulse available after signing in.'),
      !s.stage_dir?note('Stage the native package to enable login setup.'):el('div',{},el('h3',{},'Monitor'),startupButtons('service'),el('h3',{},'Status bar'),startupButtons('tray'))):null,
    s?.stage_dir && model.platform==='macos'?card('SD eject protection',note('Keep SidePulse Pro and SidePulse Dot available after sleep.'),
      actions(...[['Enable at login','install'],['Remove','uninstall'],['Check status','status']].map(([label,operation])=>button(label,()=>send({type:'startup',job:'sd-guard',operation,dry_run:false}))))):null,
    s?.stage_dir && model.platform==='macos'?card('Closed-lid sleep prevention',note('Open a one-time administrator setup in Terminal.'),
      actions(...[['Open administrator setup','install'],['Remove setup','uninstall'],['Check status','status']].map(([label,operation])=>button(label,()=>send({type:'sleep_helper',operation}))))):null];
}
function diagnostics() {
  const d=model.state.diagnostics;
  return [...header('Diagnostics','Export agent events for troubleshooting.'),card('Debug log',p(`${d.audit_bytes.toLocaleString()} bytes recorded`),
    ...d.audit_paths.map(path=>el('code',{class:'path'},path)),
    actions(button('Export CSV',()=>request('export_diagnostics',{format:'csv'}),!d.export_directory),button('Export HTML',()=>request('export_diagnostics',{format:'html'}),!d.export_directory),
      model.lastExport?button('Open export',()=>send({type:'open_export'})):null),
    model.lastExport?el('code',{class:'path'},model.lastExport):null,d.export_directory?note(`Exports are saved in ${d.export_directory}`):null),
    card('Settings file',el('code',{class:'path'},d.settings_path??'No settings file configured')),
    card('Status history',el('code',{class:'path'},d.history_path??'No history file configured'))];
}
function history() {
  const s=model.state,points=s.history_points;
  const top=[...header('Activity history','Agent status, battery level, charger power, and sleep activity over time.'),
    field('Timeframe',select('history-timeframe',String(s.history_timeframe),[3600,21600,43200,86400,172800].map(seconds=>[String(seconds),`Last ${seconds/3600} ${seconds===3600?'hour':'hours'}`]),seconds=>request('set_history_timeframe',{seconds:Number(seconds)})))];
  if(!points.length)return [...top,p('No history yet. New observations appear while the service is running.','empty')];
  const width=532,height=375,left=0,right=532,first=Date.parse(points[0].recorded_at),last=Date.parse(points[points.length-1].recorded_at);
  const span=Math.max(1,last-first),x=point=>left+(Date.parse(point.recorded_at)-first)/span*(right-left);
  const chart=svg('svg',{class:'history',viewBox:`0 0 ${width} ${height}`,preserveAspectRatio:'none',role:'img','aria-label':'Battery, charger, agent status, awake, sleep and lid history',tabindex:'0'});
  const maxWatts=Math.max(10,...points.map(p=>p.charger_power_watts??0));
  const labels=el('div',{class:'chart-labels'},
    ...[['battery-label','Battery','0–100%'],['charger-label','Charger',`0–${Math.ceil(maxWatts)} W`],
      ['agent-label','Agent status'],['awake-label','SidePulse awake'],['sleep-label','Mac sleep'],['lid-label','Lid']]
      .map(([cls,text,range])=>el('span',{class:cls},text,range?el('small',{},range):null)));
  for(const y of [20,65,110,130,160,190])chart.append(svg('line',{x1:left,x2:right,y1:y,y2:y,stroke:'var(--line)','stroke-width':1}));
  const color=mode=>['working','tool_running','long_task_progress'].includes(mode)?'#32becf':['blocked_error','waiting_for_input'].includes(mode)?'#e19b37':mode==='completed'?'#50be82':'#8290a2';
  const bit=(value,on,off)=>value===null||value===undefined?'#536070':value?on:off;
  for(let i=0;i<points.length;i++) {
    const a=points[i],b=points[i+1],xa=x(a),xb=b?x(b):Math.min(right,xa+2);
    if(b)for(const [key,max,bottom,hue,h] of [['battery_level',100,110,'#50be82',90],['charger_power_watts',maxWatts,190,'#5fa5e6',60]]) {
      if(a[key]!==null && b[key]!==null)chart.append(svg('line',{x1:xa,x2:xb,y1:bottom-Math.max(0,Math.min(max,a[key]))/max*h,y2:bottom-Math.max(0,Math.min(max,b[key]))/max*h,stroke:hue,'stroke-width':2}));
    }
    for(const [y,fill] of [[219,color(a.agent_status)],[253,bit(a.keep_awake_active,'#32becf',a.keep_awake_requested?'#e19b37':'#8290a2')],
      [287,bit(a.mac_sleep_prevented,'#e19b37','#5fa5e6')],[321,bit(a.lid_closed,'#e19b37','#50be82')]])
      chart.append(svg('rect',{x:Math.min(xa,right-1),y,width:Math.max(1,xb-xa),height:12,fill}));
  }
  const cursor=svg('line',{x1:left,x2:left,y1:18,y2:336,stroke:'currentColor','stroke-width':1,'stroke-dasharray':'3 3'});chart.append(cursor);
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
  return [...top,s.history_sampled?note('Showing a summary of the recorded observations.'):null,card('Recorded observations',el('div',{class:'history-grid'},labels,chart),
    el('div',{class:'row spread'},el('small',{},timestamp(points[0].recorded_at)),el('small',{},timestamp(points[points.length-1].recorded_at))),tip,
    note('Status: cyan = working · amber = needs attention · green = completed'),
    note('Awake: cyan = active · amber = requested. Sleep: amber = prevented · blue = allowed. Lid: amber = closed · green = open. Dim bands indicate unknown observations.'))];
}
const renderers={activity,devices,battery,monitoring,sleep,animations,history,sessions,relay,phones,setup,diagnostics};
for(const [id,label] of pages)document.querySelector('#navigation').append(el('button',{'data-page':id,type:'button',onclick:()=>{model.page=id;render();window.scrollTo(0,0);}},label));
pageRoot.addEventListener('compositionstart',()=>{model.composing=true;});
pageRoot.addEventListener('compositionend',()=>{model.composing=false;render();});
function applyPoll(result) {
  const changed=Boolean(result.state) || result.events.length>0 || model.connected!==result.connected || model.busy!==result.busy || JSON.stringify(model.setup)!==JSON.stringify(result.setup);
  model.connected=result.connected;model.platform=result.platform;model.setup=result.setup;
  for(const event of result.events) {
    if(['saved','export','profile_export'].includes(event.type) || event.completed)model.pendingAction=false;
    if(event.type==='saved') {
      const d=draft(event.draft);
      if(!event.error && d){d.dirty=false;d.awaiting=(event.revision??model.revision)+1;}
      if(!event.error && event.draft==='phone')model.phoneToken='';
      announce(event.error?`Could not save: ${event.error}`:'Saved',Boolean(event.error));
    } else if(event.type==='export') {model.lastExport=event.path;announce(`Exported ${event.count} debug events to ${event.path}`);}
    else if(event.type==='profile_export'){model.profileJSON=event.document;announce('Profile ready to copy or save');}
    else if(event.type==='message')announce(event.message,event.error);
  }
  model.busy=result.busy || model.pendingAction;
  if(result.state){model.state=result.state;model.revision=result.revision;syncDrafts();}
  if(changed)render();
}
async function poll() {
  try { applyPoll(await invoke('settings_poll',{afterRevision:model.revision})); }
  catch(error) {model.connected=false;model.busy=false;model.pendingAction=false;announce(`Settings client unavailable: ${error}`,true);render();}
  setTimeout(poll,500);
}
render();poll();
