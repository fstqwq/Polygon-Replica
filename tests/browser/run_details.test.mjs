import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

// An event/DOM fixture, deliberately without layout or paint emulation.
class Events {
  listeners = new Map();
  addEventListener(type, fn, options) {
    const entries = this.listeners.get(type) || [];
    entries.push({ fn, once: options?.once });
    this.listeners.set(type, entries);
  }
  removeEventListener(type, fn) {
    this.listeners.set(type, (this.listeners.get(type) || []).filter(entry => entry.fn !== fn));
  }
  dispatchEvent(event) {
    event.target ||= this;
    event.preventDefault ||= () => { event.defaultPrevented = true; };
    for (const entry of [...(this.listeners.get(event.type) || [])]) {
      entry.fn(event);
      if (entry.once) this.removeEventListener(event.type, entry.fn);
    }
    if (event.bubbles && this.parentElement) this.parentElement.dispatchEvent(event);
    return !event.defaultPrevented;
  }
}

class Element extends Events {
  constructor(tag, document) {
    super();
    Object.assign(this, { tag, document, children: [], dataset: {}, attributes: {}, hidden: false, inert: false, className: '', id: '', html: '', text: '' });
    this.classList = {
      contains: name => this.className.split(' ').includes(name),
      add: (...names) => { this.className = [...new Set([...this.className.split(' '), ...names])].join(' '); },
      remove: (...names) => { this.className = this.className.split(' ').filter(name => !names.includes(name)).join(' '); },
    };
  }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
  replaceChildren(...children) { this.children = []; this.html = ''; this.text = ''; this.append(...children); }
  get innerHTML() { return this.html; }
  set innerHTML(value) { this.replaceChildren(); this.html = value; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this.text = value; }
  getAttribute(name) {
    if (name === 'id') return this.id || null;
    if (name.startsWith('data-')) return this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] ?? null;
    return this.attributes[name] ?? null;
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  hasAttribute(name) { return this.getAttribute(name) !== null; }
  matches(selector) {
    return selector.split(',').some(raw => {
      let part = raw.trim();
      let excluded = false;
      part = part.replace(/:not\(([^)]+)\)/g, (_, inner) => { excluded ||= this.matches(inner); return ''; });
      if (excluded) return false;
      const tag = part.match(/^[a-z]+/i)?.[0];
      if (tag && this.tag !== tag) return false;
      for (const [, name] of part.matchAll(/\.([\w-]+)/g)) if (!this.classList.contains(name)) return false;
      for (const [, id] of part.matchAll(/#([\w-]+)/g)) if (this.id !== id) return false;
      for (const [, name, value] of part.matchAll(/\[([\w-]+)(?:=['"]?([^'"\]]+)['"]?)?\]/g)) {
        if (!this.hasAttribute(name) || (value !== undefined && this.getAttribute(name) !== value)) return false;
      }
      return Boolean(part);
    });
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest?.(selector) || null; }
  contains(other) { return this === other || this.children.some(child => child.contains(other)); }
  focus() { this.document.activeElement = this; }
  click() { this.dispatchEvent({ type: 'click', bubbles: true }); }
}

async function fixture() {
  const document = new Events();
  const body = new Element('body', document);
  Object.assign(document, {
    body, readyState: 'loading', activeElement: body,
    createElement: tag => new Element(tag, document),
    createTextNode: text => { const node = new Element('text', document); node.textContent = text; return node; },
    querySelectorAll: selector => body.querySelectorAll(selector),
    querySelector: selector => body.querySelector(selector),
    getElementById: id => body.querySelector(`#${id}`),
  });
  body.parentElement = document;
  const add = (parent, tag, id = '', className = '', dataset = {}) => {
    const element = new Element(tag, document);
    Object.assign(element, { id, className, dataset });
    parent.appendChild(element);
    return element;
  };
  const background = add(body, 'main');
  const alreadyInert = add(body, 'aside'); alreadyInert.inert = true;
  const table = add(background, 'table', '', 'verification-detail-table', { runDetailsFragment: '/details', verificationId: 'ver-123' });
  const nested = add(body, 'section');
  const sibling = add(nested, 'p');
  const overlay = add(nested, 'div', 'run-test-detail-popup', 'ui-popup-overlay', { popupOverlay: '1' }); overlay.hidden = true;
  const close = add(overlay, 'button', '', '', { popupClose: '1' });
  const title = add(overlay, 'h2', 'run-test-detail-popup-title');
  const content = add(overlay, 'div', 'run-test-detail-popup-content');
  const last = add(overlay, 'button');
  const second = add(body, 'div', 'second-popup', 'ui-popup-overlay', { popupOverlay: '1' }); second.hidden = true;
  add(second, 'button', '', '', { popupClose: '1' });
  const confirm = add(body, 'div', 'ui-confirm-overlay'); confirm.hidden = true;
  add(confirm, 'p', 'ui-confirm-message');
  const cancel = add(confirm, 'button', '', 'ui-confirm-cancel');
  add(confirm, 'button', '', 'ui-confirm-ok');
  const timers = [];
  const window = new Events();
  Object.assign(window, { location: { search: '', origin: 'https://test' }, setTimeout: fn => timers.push(fn), clearTimeout() {} });
  const requests = [];
  const fetch = (url, options) => new Promise((resolve, reject) => { requests.push({ url, ...options, resolve, reject }); });
  const context = vm.createContext({ window, document, Element, URLSearchParams, AbortController, fetch, navigator: {}, performance: { getEntriesByType: () => [] }, CustomEvent: class { constructor(type, values) { this.type = type; Object.assign(this, values); } } });
  for (const filename of ['core.js', 'run.js']) {
    const source = await readFile(new URL(`../../app/static/js/${filename}`, import.meta.url), 'utf8');
    const module = new vm.SourceTextModule(source, { context });
    await module.link(() => { throw new Error('unexpected import'); });
    await module.evaluate();
  }
  document.dispatchEvent({ type: 'DOMContentLoaded' });
  const testRows = new Map();
  const opener = (name, program = 'solution-0', target = overlay.id) => {
    if (!testRows.has(name)) {
      testRows.set(name, add(table, 'tr', '', '', { testName: name, testSourceKind: 'manual', testCommand: '' }));
    }
    return add(testRows.get(name), 'button', '', '', { popupOpen: target, programId: program });
  };
  const flush = async () => { await new Promise(resolve => setImmediate(resolve)); };
  const finish = async (index, html, ok = true) => { requests[index].resolve({ ok, text: async () => html }); await flush(); };
  const focus = () => { while (timers.length) timers.shift()(); };
  const key = (value, shiftKey = false) => document.dispatchEvent({ type: 'keydown', key: value, shiftKey });
  return { document, window, body, table, background, alreadyInert, sibling, overlay, second, close, last, content, title, cancel, confirm, opener, requests, finish, flush, focus, key };
}

test('dynamic openers coalesce only pending fetches; reopening obtains late diagnostics', async () => {
  const f = await fixture();
  const a = f.opener('001.in');
  a.click(); f.focus(); f.last.focus(); a.click(); f.focus();
  assert.equal(f.requests.length, 1);
  assert.equal(f.document.activeElement, f.last);
  assert.equal(f.background.inert, true);
  await f.finish(0, 'first');
  f.close.click(); a.click();
  assert.equal(f.requests.length, 2);
  await f.finish(1, 'late diagnostics');
  assert.equal(f.content.innerHTML, 'late diagnostics');
});

test('closing aborts and clears; a late response cannot repopulate a closed dialog', async () => {
  const f = await fixture(); f.opener('001.in').click();
  f.close.click();
  assert.equal(f.requests[0].signal.aborted, true);
  assert.equal(f.content.textContent, '');
  assert.equal(f.title.textContent, '');
  await f.finish(0, 'stale');
  assert.equal(f.content.innerHTML, '');
});

test('A to B to A uses request identity even when abort is ignored', async () => {
  const f = await fixture(); const a = f.opener('001.in'); const b = f.opener('002.in');
  a.click(); b.click();
  assert.equal(f.requests[0].signal.aborted, true);
  await f.finish(0, 'old A');
  assert.equal(f.content.textContent, 'Loading details...');
  a.click();
  assert.equal(f.requests[1].signal.aborted, true);
  await f.finish(2, 'new A'); await f.finish(1, 'old B');
  assert.equal(f.content.innerHTML, 'new A');
  // A separate race leaves the first A unresolved until the second A finishes.
  a.click(); b.click(); a.click();
  await f.finish(5, 'newest A'); await f.finish(3, 'older A');
  assert.equal(f.content.innerHTML, 'newest A');
});

test('HTTP errors can retry and whole-test/program keys remain distinct', async () => {
  const f = await fixture(); const all = f.opener('001.in', ''); const one = f.opener('001.in');
  all.click(); one.click();
  assert.equal(f.requests.length, 2);
  assert.equal(new URL(f.requests[0].url, 'https://test').searchParams.has('program_id'), false);
  assert.equal(new URL(f.requests[1].url, 'https://test').searchParams.get('program_id'), 'solution-0');
  await f.finish(1, '', false);
  one.click(); assert.equal(f.requests.length, 3);
  await f.finish(2, 'recovered'); assert.equal(f.content.innerHTML, 'recovered');
  f.requests[0].reject(new Error('late failure')); await f.flush();
  assert.equal(f.content.innerHTML, 'recovered');
});

test('nested DOM focus trap, Escape, inert restoration and single-modal switching', async () => {
  const f = await fixture(); const a = f.opener('001.in'); a.click(); f.focus();
  assert.equal(f.document.activeElement, f.close);
  assert.equal(f.sibling.inert, true); assert.equal(f.background.inert, true);
  f.key('Tab', true); assert.equal(f.document.activeElement, f.last);
  f.key('Tab'); assert.equal(f.document.activeElement, f.close);
  const confirmation = f.window.PolygonUI.showConfirmDialog('continue?', a);
  f.focus();
  assert.equal(f.overlay.hidden, true); assert.equal(f.requests[0].signal.aborted, true);
  assert.equal(f.document.activeElement, f.cancel);
  assert.equal(f.body.classList.contains('confirm-open'), true);
  f.opener('001.in', '', 'second-popup').click(); f.focus();
  assert.equal(await confirmation, false);
  assert.equal(f.confirm.hidden, true); assert.equal(f.second.hidden, false);
  assert.equal(f.body.classList.contains('popup-open'), true);
  f.key('Escape');
  assert.equal(f.second.hidden, true); assert.equal(f.background.inert, false);
  assert.equal(f.sibling.inert, false); assert.equal(f.alreadyInert.inert, true);
  a.click(); f.key('Escape'); f.focus();
  assert.equal(f.document.activeElement, a);
  assert.equal(f.overlay.hidden, true);
});
