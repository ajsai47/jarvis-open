// Jarvis for Dia: a bridge between Jarvis (ws://127.0.0.1:8765/api/browser) and this browser.
// Jarvis works in its own background tabs (grouped as "Jarvis"), using chrome.debugger.
const URL = 'ws://127.0.0.1:8765/api/browser';
const VERSION = chrome.runtime.getManifest().version;
let ws = null;
const attached = new Set();
const refs = new Map();          // tabId -> Map(ref -> backendDOMNodeId)
const lastUse = new Map();       // tabId -> ms, to detach idle tabs (hides the debugging bar)
let groupId = null;

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  try { ws = new WebSocket(URL); } catch { return; }
  ws.onopen = () => ws.send(JSON.stringify({hello: 'jarvis-dia', version: VERSION}));
  ws.onmessage = async e => {
    const msg = JSON.parse(e.data);
    if (msg.ping) return ws.send(JSON.stringify({pong: msg.ping}));
    let reply;
    try { reply = {id: msg.id, ok: true, result: await run(msg.cmd, msg.args || {})}; }
    catch (err) { reply = {id: msg.id, ok: false, error: String(err && err.message || err)}; }
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(reply));
  };
  ws.onclose = () => { ws = null; setTimeout(connect, 3000); };
  ws.onerror = () => {};
}
chrome.alarms.create('jarvis-keepalive', {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(() => { connect(); detachIdle(); });
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();

const cdp = (tabId, method, params = {}) => chrome.debugger.sendCommand({tabId}, method, params);
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function attach(tabId) {
  lastUse.set(tabId, Date.now());
  if (attached.has(tabId)) return;
  await chrome.debugger.attach({tabId}, '1.3');
  attached.add(tabId);
  await cdp(tabId, 'Emulation.setFocusEmulationEnabled', {enabled: true}).catch(() => {});
  await cdp(tabId, 'DOM.enable').catch(() => {});
  await cdp(tabId, 'Accessibility.enable').catch(() => {});
}
chrome.debugger.onDetach.addListener(({tabId}) => { attached.delete(tabId); refs.delete(tabId); });
chrome.tabs.onRemoved.addListener(tabId => { attached.delete(tabId); refs.delete(tabId); lastUse.delete(tabId); });
async function detachIdle() {
  for (const tabId of [...attached]) {
    if (Date.now() - (lastUse.get(tabId) || 0) > 120000) {
      await chrome.debugger.detach({tabId}).catch(() => {});
      attached.delete(tabId);
    }
  }
}

async function waitLoaded(tabId, ms = 15000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const t = await chrome.tabs.get(tabId);
    if (t.status === 'complete') return t;
    await sleep(200);
  }
  return chrome.tabs.get(tabId);
}
async function settle(tabId) { await sleep(400); await waitLoaded(tabId, 8000); }
async function intoGroup(tabId) {
  try {
    if (groupId !== null) { await chrome.tabs.group({groupId, tabIds: [tabId]}); return; }
  } catch { groupId = null; }
  try {
    groupId = await chrome.tabs.group({tabIds: [tabId]});
    await chrome.tabGroups.update(groupId, {title: 'Jarvis', color: 'blue', collapsed: true});
  } catch {}
}

const INTERACTIVE = new Set(['button', 'link', 'textbox', 'searchbox', 'combobox', 'checkbox', 'radio',
  'switch', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option', 'slider', 'spinbutton', 'treeitem', 'listbox']);
const LANDMARK = new Set(['heading', 'alert', 'dialog', 'alertdialog', 'status']);
const val = v => (v && v.value !== undefined ? String(v.value) : '');

async function look(tabId, filter = '', limit = 250) {
  await attach(tabId);
  const {nodes} = await cdp(tabId, 'Accessibility.getFullAXTree');
  const map = new Map(), lines = [], f = filter.toLowerCase();
  let n = 0;
  for (const node of nodes) {
    if (node.ignored || !node.backendDOMNodeId) continue;
    const role = val(node.role), name = val(node.name).replace(/\s+/g, ' ').trim().slice(0, 120);
    if (!(INTERACTIVE.has(role) || (LANDMARK.has(role) && name))) continue;
    const props = Object.fromEntries((node.properties || []).map(p => [p.name, val(p.value)]));
    if (props.disabled === 'true' && !f) continue;
    let line = `${role} "${name}"`;
    const value = val(node.value);
    if (value) line += ` value="${value.slice(0, 80)}"`;
    if (props.checked) line += ` checked=${props.checked}`;
    if (props.expanded) line += ` expanded=${props.expanded}`;
    if (props.focused === 'true') line += ' focused';
    if (f && !line.toLowerCase().includes(f)) continue;
    const ref = `e${++n}`;
    map.set(ref, node.backendDOMNodeId);
    lines.push(`[${ref}] ${line}`);
    if (lines.length >= limit) { lines.push(`… more; pass a filter to narrow it down`); break; }
  }
  refs.set(tabId, map);
  const t = await chrome.tabs.get(tabId);
  return {title: t.title, url: t.url, elements: lines.join('\n') || '(nothing clickable found)'};
}

function node(tabId, ref) {
  const id = refs.get(tabId)?.get(ref);
  if (!id) throw new Error(`Unknown ref ${ref}. Look at the page again first (refs change after every look).`);
  return id;
}
async function objectFor(tabId, backendNodeId) {
  const {object} = await cdp(tabId, 'DOM.resolveNode', {backendNodeId});
  return object.objectId;
}
async function click(tabId, ref) {
  await attach(tabId);
  const backendNodeId = node(tabId, ref);
  await cdp(tabId, 'DOM.scrollIntoViewIfNeeded', {backendNodeId}).catch(() => {});
  let done = false;
  try {
    const {model} = await cdp(tabId, 'DOM.getBoxModel', {backendNodeId});
    const q = model.content, x = (q[0] + q[2] + q[4] + q[6]) / 4, y = (q[1] + q[3] + q[5] + q[7]) / 4;
    for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased'])
      await cdp(tabId, 'Input.dispatchMouseEvent', {type, x, y, button: 'left', clickCount: 1});
    done = true;
  } catch {}
  if (!done) {
    const objectId = await objectFor(tabId, backendNodeId);
    await cdp(tabId, 'Runtime.callFunctionOn', {objectId, functionDeclaration: 'function(){this.click()}'});
  }
  await settle(tabId);
  return 'Clicked.';
}
async function type(tabId, ref, text, clear = true, submit = false) {
  await attach(tabId);
  const backendNodeId = node(tabId, ref);
  await cdp(tabId, 'DOM.scrollIntoViewIfNeeded', {backendNodeId}).catch(() => {});
  await cdp(tabId, 'DOM.focus', {backendNodeId});
  if (clear) {
    const objectId = await objectFor(tabId, backendNodeId);
    await cdp(tabId, 'Runtime.callFunctionOn', {objectId, functionDeclaration:
      `function(){ if ('value' in this && this.select) { this.select(); } else { const r = document.createRange(); r.selectNodeContents(this); const s = getSelection(); s.removeAllRanges(); s.addRange(r); } }`});
  }
  await cdp(tabId, 'Input.insertText', {text});
  if (submit) await key(tabId, 'Enter');
  return submit ? 'Typed and pressed Enter.' : 'Typed.';
}
const KEYS = {Enter: [13, '\r'], Tab: [9, '\t'], Escape: [27, ''], Backspace: [8, ''], ArrowDown: [40, ''], ArrowUp: [38, '']};
async function key(tabId, name) {
  await attach(tabId);
  const [code, text] = KEYS[name] || [0, ''];
  if (!code) throw new Error(`Unsupported key ${name}. Use one of ${Object.keys(KEYS).join(', ')}.`);
  await cdp(tabId, 'Input.dispatchKeyEvent', {type: text ? 'keyDown' : 'rawKeyDown', key: name, code: name,
    windowsVirtualKeyCode: code, nativeVirtualKeyCode: code, text});
  await cdp(tabId, 'Input.dispatchKeyEvent', {type: 'keyUp', key: name, code: name, windowsVirtualKeyCode: code, nativeVirtualKeyCode: code});
  await settle(tabId);
  return `Pressed ${name}.`;
}
async function read(tabId, limit = 12000) {
  await attach(tabId);
  const {result} = await cdp(tabId, 'Runtime.evaluate', {returnByValue: true,
    expression: `({title: document.title, url: location.href, text: (document.body ? document.body.innerText : '').slice(0, ${limit})})`});
  return result.value;
}

async function run(cmd, a) {
  switch (cmd) {
    case 'tabs': return (await chrome.tabs.query({})).filter(t => t.url && !t.url.startsWith('chrome'))
      .map(t => ({tab: t.id, title: t.title, url: t.url, active: t.active, jarvis: t.groupId === groupId && groupId !== null}));
    case 'open': {
      const t = await chrome.tabs.create({url: a.url, active: false});
      await intoGroup(t.id);
      const d = await waitLoaded(t.id);
      return {tab: t.id, title: d.title, url: d.url};
    }
    case 'go': {
      await chrome.tabs.update(a.tab, {url: a.url});
      await sleep(300);
      const d = await waitLoaded(a.tab);
      refs.delete(a.tab);
      return {tab: a.tab, title: d.title, url: d.url};
    }
    case 'look': return look(a.tab, a.filter || '', a.limit || 250);
    case 'click': return click(a.tab, a.ref);
    case 'type': return type(a.tab, a.ref, a.text, a.clear !== false, !!a.submit);
    case 'key': return key(a.tab, a.key);
    case 'read': return read(a.tab, a.limit || 12000);
    case 'scroll': await attach(a.tab);
      await cdp(a.tab, 'Runtime.evaluate', {expression: `scrollBy(0, ${Number(a.dy) || 600})`});
      await sleep(300); return 'Scrolled.';
    case 'close': await chrome.tabs.remove(a.tab); return 'Closed.';
    case 'hush': {  // Jarvis is talking: mute tabs playing sound, and say which, so only those come back
      const loud = (await chrome.tabs.query({audible: true})).filter(t => !(t.mutedInfo && t.mutedInfo.muted));
      await Promise.all(loud.map(t => chrome.tabs.update(t.id, {muted: true})));
      return loud.map(t => t.id);
    }
    case 'unhush':
      await Promise.all((a.tabs || []).map(id => chrome.tabs.update(id, {muted: false}).catch(() => {})));
      return 'Unmuted.';
    case 'reload': setTimeout(() => chrome.runtime.reload(), 200); return 'Reloading.';
    case 'shot': {  // a small picture of Jarvis's tab for the Preview panel; never stored
      await attach(a.tab);
      const r = await Promise.race([
        cdp(a.tab, 'Page.captureScreenshot', {format: 'jpeg', quality: 55, optimizeForSpeed: true}),
        sleep(4000).then(() => { throw new Error('Timed out'); }),
      ]);
      return r.data;
    }
    default: throw new Error(`Unknown command ${cmd}`);
  }
}
