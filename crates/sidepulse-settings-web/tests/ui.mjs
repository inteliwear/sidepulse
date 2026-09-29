// Browser presentation checks use a local fixture and a mocked typed Rust host.
// They never start the daemon, execute startup actions or contact linked devices.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {createServer} from 'node:http';
import {readFile, mkdir, writeFile} from 'node:fs/promises';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {chromium, webkit} from 'playwright';
const root=dirname(dirname(fileURLToPath(import.meta.url)));
const reference=JSON.parse(execFileSync('python3',[join(root,'tests/python_structure.py')],{encoding:'utf8'}));
const state=JSON.parse(await readFile(join(root,'tests/state.json'),'utf8'));
const assets=new Map(await Promise.all(['index.html','app.js','styles.css'].map(async name=>[name,await readFile(join(root,'ui',name))])));
const server=createServer((request,response)=>{
  const name=request.url==='/'?'index.html':request.url.slice(1),body=assets.get(name);
  response.writeHead(body?200:404,{'Content-Type':name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html'});response.end(body??'Not found');
}).listen(0,'127.0.0.1');
await new Promise(resolve=>server.once('listening',resolve));
const base=`http://127.0.0.1:${server.address().port}`;
const artifactDir=process.env.SIDEPULSE_UI_ARTIFACT_DIR;
if(artifactDir)await mkdir(artifactDir,{recursive:true});
const contracts=[];
async function waitFor(page,condition,message) {
  for(let i=0;i<50;i++){if(await condition())return;await page.waitForTimeout(100);}throw new Error(message);
}
try {
  for(const engine of (process.env.SIDEPULSE_UI_ENGINES??'chromium,webkit').split(',')) {
    const browser=await ({chromium,webkit}[engine]).launch({headless:true});
    const page=await browser.newPage({viewport:{width:680,height:560},colorScheme:'light'});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    try {
      await page.addInitScript(({state})=>{
        window.fixture={state,revision:1,connected:true,busy:false,events:[],calls:[],fail:null};
        const classify=command=>({set_battery_settings:'battery',set_agent_list_settings:'monitoring',set_sleep_settings:'sleep',set_agent_animation:'animation',set_session_terminal:'terminal',set_lid_animation_timing:'lid_timing',set_relay_settings:'relay',edit_animation_library:'library',set_animation_state:'library',preview_animation:'none',register_phone:'phone',remove_phone:'phone',begin_phone_pairing:'phone',cancel_phone_pairing:'phone',reload_phone_links:'phone',set_phone_display:'phone'}[command]??'none');
        window.__TAURI__={clipboardManager:{writeText:async text=>{window.fixture.clipboard=text;}},core:{invoke:async(command,args)=>{
          const f=window.fixture;
          if(command==='settings_poll'){
            const events=f.events.splice(0);f.busy=false;
            const previews=Object.fromEntries(['idle_ready','working','waiting_for_input','blocked_error','completed','unknown','lid_open','lid_closed','editor'].map(id=>[id,{colors:Array.from({length:8},()=>[0,229,255]),program:'#00E5FF',error:null}]));
            return {previews,revision:f.revision,connected:f.connected,busy:f.busy,platform:'macos',setup:f.state.setup,state:args.afterRevision!==f.revision?structuredClone(f.state):null,events};
          }
          if(command!=='settings_action')throw new Error('Unknown host command');
          const action=args.action;f.calls.push(structuredClone(action));f.busy=true;
          const r=action.request,s=f.state;
          if(action.type!=='request'){f.events.push({type:'message',error:false,message:'Fixture management completed',completed:true});return;}
          if(r.command==='export_diagnostics'){f.events.push({type:'export',path:`/fixture/exports/report.${r.format}`,count:42});return;}
          if(r.command==='export_animation_profile'){f.events.push({type:'profile_export',document:JSON.stringify({format:'sidepulse-animation-profile',version:1,name:'Fixture profile',animations:s.animation_library.current,custom_animations:{}})});return;}
          if(r.command==='session_targets'||r.command==='configure_hooks'){f.events.push({type:'message',error:false,message:'Fixture action completed',completed:true});return;}
          const fail=f.fail;f.fail=null;
          f.events.push({type:'saved',draft:classify(r.command),revision:f.revision,error:fail});
          if(!fail){
            if(r.command==='set_virtual_display' && r.patch.enabled!==undefined)s.settings.virtual_display_enabled=r.patch.enabled;
            if(r.command==='set_battery_settings'){
              if(r.patch.full_charge_watts)s.settings.full_charge_watts=r.patch.full_charge_watts.kind==='auto'?null:r.patch.full_charge_watts.watts;
              if(r.patch.show_on_power_change!==undefined)s.settings.controls.battery_power_preview=r.patch.show_on_power_change;
              if(r.patch.power_change_preview_seconds!==undefined)s.settings.power_change_preview_seconds=r.patch.power_change_preview_seconds;
            }
            if(r.command==='set_agent_list_settings'){s.settings.idle_timeout_seconds=r.patch.idle_timeout_seconds;s.settings.controls.recent_session_retention_seconds=r.patch.recent_session_retention_seconds;}
            if(r.command==='set_sleep_settings')s.settings.sleep_min_battery_percent=r.patch.min_battery_percent;
            if(r.command==='set_session_terminal'){s.settings.session_terminal=r.terminal;s.settings.custom_terminal_path=r.custom_path;}
            if(r.command==='set_agent_animation'){for(const a of s.animation_states)if(a.mode===r.mode){a.style=r.style;a.program=r.custom_program??a.program;}}
            if(r.command==='set_lid_animation_timing')s.lid_durations=[r.open_seconds,r.close_seconds];
          }
          f.revision++;
        }}};
      },{state});
      await page.goto(base);
      await waitFor(page,()=>page.locator('#connection').textContent().then(t=>t==='Service connected'),'service fixture did not connect');
      assert.deepEqual(await page.getByRole('tab').allTextContents(),reference.tabs.map(tab=>tab[1]),'Settings tab structure differs from Python');
      assert.deepEqual(await page.locator('.hook-row strong').allTextContents(),reference.hooks);
      assert.deepEqual(await page.locator('main h2').allTextContents(),reference.sections.agents);
      assert.equal(await page.locator('main img').count(),0,'provider titles must be text, not HTML');
      const nav=async label=>{if(await page.locator('#navigation').isHidden())await page.locator('#back-settings').click();
        if(reference.tabs.some(tab=>tab[1]===label))await page.getByRole('tab',{name:label,exact:true}).click();
        else if(label==='Setup')await page.locator('#setup-open').click();
        else {await page.locator('#controls-toggle').click();await page.getByRole('navigation',{name:'Other SidePulse controls'}).getByRole('button',{name:label,exact:true}).click();}};
      const click=async label=>{await page.locator('main').getByRole('button',{name:label,exact:true}).click();await waitFor(page,()=>page.locator('main fieldset').first().evaluate(node=>!node.disabled),`${label} remained busy`);};
      const last=async command=>{
        const action=await page.evaluate(()=>window.fixture.calls.at(-1));
        if(command)assert.equal(action.request.command,command);contracts.push(action);return action.request??action;
      };
      for(const label of ['Agents','Animations','Advanced','History','Diagnostics','Activity','Devices','Battery','Sleep','Link computers','Link phones','Setup']){
        await nav(label);assert.equal(await page.locator('h1').count(),1);
        assert.ok((await page.locator('main').textContent()).length>20);
        assert.ok(!(await page.locator('main').textContent()).includes('[object '),'nested presentation must contain DOM nodes');
        if(artifactDir)await page.screenshot({path:join(artifactDir,`${engine}-${label.replaceAll(' ','-')}.png`),fullPage:true});
      }
      await nav('Activity');await click('Open session');assert.equal((await last('session_targets')).agent_id,'codex:session:fixture');
      await page.locator('#recent-sessions summary').click();await page.locator('#recent-sessions').getByRole('button',{name:'Open session',exact:true}).click();
      await waitFor(page,()=>page.locator('main fieldset').first().evaluate(node=>!node.disabled),'recent session remained busy');
      assert.equal((await last('session_targets')).agent_id,'codex:session:recent');
      await nav('Devices');await page.locator('#brightness').evaluate(node=>{node.value='192';node.dispatchEvent(new Event('change',{bubbles:true}));});await page.waitForTimeout(600);assert.equal((await last('set_brightness')).brightness,192);
      await page.locator('#device-display').selectOption('battery');await page.waitForTimeout(600);assert.equal((await last('set_display_mode')).mode,'battery');
      await page.locator('#virtual-enabled').check();await page.waitForTimeout(600);assert.equal((await last('set_virtual_display')).patch.enabled,true);
      await nav('Battery');await page.locator('#baseline-auto').uncheck();await page.locator('#charger-watts').fill('85');
      await click('Save battery settings');assert.deepEqual((await last('set_battery_settings')).patch.full_charge_watts,{kind:'watts',watts:85});
      await waitFor(page,()=>page.locator('#charger-watts').inputValue().then(v=>v==='85'),'successful save did not refresh the draft');
      await page.locator('#charger-watts').fill('90');
      await page.evaluate(()=>{window.fixture.fail='Settings changed in another window';window.fixture.state.settings.full_charge_watts=100;window.fixture.revision++;});
      await page.waitForTimeout(700);assert.equal(await page.locator('#charger-watts').inputValue(),'90','live update overwrote an unsaved draft');
      await click('Save battery settings');assert.match(await page.locator('#message').textContent(),/another window/);
      assert.equal(await page.locator('#charger-watts').inputValue(),'90','failed save discarded the draft');
      await click('Reset changes');assert.equal(await page.locator('#charger-watts').inputValue(),'100');
      await nav('Advanced');assert.deepEqual(await page.locator('main h2').allTextContents(),reference.sections.advanced);await page.locator('#idle-minutes').fill('30');await page.locator('#retention-hours').fill('24');await click('Save agent list settings');
      assert.deepEqual((await last('set_agent_list_settings')).patch,{idle_timeout_seconds:1800,recent_session_retention_seconds:86400});
      await nav('Agents');await page.locator('#codex-transcripts').check();await page.waitForTimeout(600);assert.equal((await last('set_transcript_monitoring')).provider,'codex');
      await nav('Advanced');await page.locator('#tray-visible').uncheck();await page.waitForTimeout(600);assert.equal((await last('set_tray_visibility')).visible,false);
      await nav('Sleep');await click('Retry keeping awake');await last('retry_power_control');
      await page.locator('#sleep-policy').selectOption('never');await page.waitForTimeout(600);await last('set_sleep_policy');
      await page.locator('#sleep-battery').fill('25');await click('Save battery safeguard');assert.equal((await last('set_sleep_settings')).patch.min_battery_percent,25);
      await nav('Agents');await page.locator('#opener-codex').selectOption('terminal');await page.waitForTimeout(600);await last('set_session_open_preference');
      await page.locator('#terminal-app').selectOption('custom');await page.locator('#terminal-path').fill('/fixture/terminal');await click('Save terminal preference');assert.equal((await last('set_session_terminal')).custom_path,'/fixture/terminal');
      await nav('Agents');await page.getByRole('tab',{name:'Agents',exact:true}).focus();await page.keyboard.press('ArrowRight');assert.equal(await page.getByRole('tab',{name:'Animations',exact:true}).getAttribute('aria-selected'),'true');
      assert.deepEqual(await page.locator('.animation-table th').allTextContents(),reference.columns);
      assert.deepEqual(await page.locator('.animation-table tbody tr').evaluateAll(rows=>rows.map(row=>[row.dataset.animationState,row.cells[0].textContent])),reference.animations);
      assert.equal(await page.locator('.animation-table .led-preview span').count(),64);
      await page.locator('[data-animation-state="working"] select').selectOption('amber-pulse');assert.equal((await last('set_animation_state')).state,'working');await page.waitForTimeout(600);
      await page.locator('[data-animation-state="working"]').getByRole('button',{name:'Show',exact:true}).click();await page.waitForTimeout(600);assert.equal((await last('preview_animation')).seconds,reference.durations.AGENT_ANIMATION_DEVICE_PREVIEW_SECONDS);
      await page.locator('[data-animation-state="working"]').getByRole('button',{name:'Edit',exact:true}).click();
      assert.equal(await page.getByRole('dialog').count(),1);await page.locator('#asset-name').fill('Working clone');await page.locator('#asset-program').fill('#00E5FF 500ms pulse\nrepeat');
      await click('Show on Device');assert.equal((await last('preview_animation')).seconds,reference.durations.AGENT_ANIMATION_EDITOR_DEVICE_PREVIEW_SECONDS);
      await click('Save');assert.equal((await last('edit_animation_library')).edit.state,'working');assert.equal(await page.getByRole('dialog').count(),0);
      await click('Save Profile…');await page.locator('#profile-name').fill('Portable profile');await click('Save');assert.equal((await last('edit_animation_library')).edit.name,'Portable profile');
      await click('Export JSON');await last('export_animation_profile');assert.match(await page.locator('#profile-json').inputValue(),/sidepulse-animation-profile/);
      await click('Import and apply');assert.equal((await last('edit_animation_library')).edit.operation,'import_profile');
      await click('Add Custom…');await page.locator('#asset-name').fill('Portable animation');await click('Save');assert.equal((await last('edit_animation_library')).edit.operation,'save_animation');
      await page.locator('[data-animation-state="lid_open"] select').selectOption('amber-pulse');await page.waitForTimeout(600);assert.equal((await last('set_animation_state')).state,'lid_open');
      await page.locator('#lid-timing summary').click();await page.locator('#lid-open-seconds').fill('2');await click('Save timing');assert.equal((await last('set_lid_animation_timing')).open_seconds,2);
      await nav('History');assert.equal(await page.locator('svg.history').count(),1);assert.deepEqual(await page.locator('.chart-labels>span').evaluateAll(nodes=>nodes.map(node=>node.firstChild.textContent)),reference.history);assert.equal(await page.locator('svg.history rect').count(),32);
      await page.locator('svg.history').focus();await page.keyboard.press('Home');assert.match(await page.locator('.chart-tooltip').textContent(),/Idle/);
      await page.locator('#history-timeframe').selectOption('86400');await page.waitForTimeout(600);assert.equal((await last('set_history_timeframe')).seconds,86400);
      await nav('Link computers');await page.locator('#relay-name').fill('Portable computer');await click('Save link');assert.equal((await last('set_relay_settings')).patch.machine_name,'Portable computer');
      await click('Copy code');assert.equal(await page.evaluate(()=>window.fixture.clipboard),'Fixture_receiver_code');
      await click('Replace code');assert.equal((await last('set_relay_settings')).patch.rotate_receiver,true);await click('Reload saved link');await last('reload_relay_settings');
      await nav('Link phones');assert.equal(await page.locator('svg.qr').count(),1);await click('Copy pairing link');assert.equal(await page.evaluate(()=>window.fixture.clipboard),state.phone_pairing.url);await click('Cancel pairing');await last('cancel_phone_pairing');await click('Start new pairing');await last('begin_phone_pairing');
      await page.locator('#phone-display-fixture-phone').selectOption('battery');await page.waitForTimeout(600);await last('set_phone_display');
      await page.locator('#phone-manual summary').click();await page.locator('#phone-token').fill('Fixture_token_123');await page.waitForTimeout(600);await click('Save phone');await last('register_phone');assert.equal(await page.locator('#phone-token').inputValue(),'');
      await click('Reload saved phones');await last('reload_phone_links');await click('Remove');await last('remove_phone');
      await nav('Agents');await page.locator('.hook-row').first().getByRole('button',{name:'Install',exact:true}).click();await page.waitForTimeout(600);assert.equal((await last('configure_hooks')).provider,'codex');await nav('Setup');await page.locator('#hook-change-preview summary').click();await page.getByRole('button',{name:'Preview change',exact:true}).first().click();await page.waitForTimeout(600);assert.equal((await last('configure_hooks')).dry_run,true);
      await page.evaluate(()=>{window.fixture.connected=false;});await page.waitForTimeout(600);
      await page.locator('main').getByRole('button',{name:'Check status',exact:true}).first().click();await page.waitForTimeout(600);assert.equal((await last()).type,'startup');
      await nav('Agents');assert.equal(await page.locator('main').getByRole('button',{name:'Install',exact:true}).first().isDisabled(),true,'offline hooks must be disabled');
      await page.evaluate(()=>{window.fixture.connected=true;});await page.waitForTimeout(600);
      await nav('Diagnostics');assert.deepEqual(await page.locator('main h2').allTextContents(),reference.sections.diagnostics);await click('Export HTML');await last('export_diagnostics');await click('Open export');assert.equal((await last()).type,'open_export');
      // Narrow window, dark theme, keyboard focus, and unsupported platform state.
      await page.setViewportSize({width:580,height:420});await page.emulateMedia({colorScheme:'dark'});
      await nav('History');assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'narrow layout overflows horizontally');
      assert.equal(await page.locator('.chart-labels>span').count(),6);
      assert.ok(await page.locator('.chart-labels').evaluate(node=>parseFloat(getComputedStyle(node).fontSize)>=12),'history labels must remain readable at minimum width');
      if(artifactDir)await page.screenshot({path:join(artifactDir,`${engine}-narrow-dark.png`),fullPage:true});
      await page.evaluate(()=>{window.fixture.state.power_control.supported=false;window.fixture.revision++;});await page.waitForTimeout(600);await nav('Sleep');assert.match(await page.locator('main').textContent(),/available on macOS/);
      assert.deepEqual(errors,[],'frontend exceptions');
      console.log(`${engine}: Python-matched five tabs and auxiliary controls, typed actions, drafts/conflicts, offline recovery, charts, pairing and narrow/dark layout passed`);
    } catch(error) {if(artifactDir)await page.screenshot({path:join(artifactDir,`${engine}-failure.png`),fullPage:true});throw error;}
    finally {await browser.close();}
  }
  if(process.env.SIDEPULSE_UI_RECORD_CONTRACTS)await writeFile(join(root,'tests/actions.json'),JSON.stringify(contracts,null,2)+'\n');
} finally {server.close();}
