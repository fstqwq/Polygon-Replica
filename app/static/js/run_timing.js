const stateText = {
  red: 'Unexpected result', gray: 'Incomplete evidence; rerun with higher execution limits',
  yellow: 'Expected results match; runtime warning', green: 'Expected results match; no runtime warning',
};

window.PolygonUI.onReady(async () => {
  const payload = document.getElementById('run-timing-data');
  const overlay = document.getElementById('run-timing-popup');
  if (!payload || !overlay) return;
  const { STEP, prepareTiming, evaluateTiming, snapLimit } = await import(payload.dataset.modelUrl);
  const data = JSON.parse(payload.textContent);
  const group = program => program.allowed.length === 1 && program.allowed[0] === 'AC' ? 'ac'
    : program.required.includes('TL') ? 'tl' : program.required.includes('WA') ? 'wa' : 'other';
  const groups = ['ac', 'tl', 'wa', 'other'];
  const compareText = (a, b) => a < b ? -1 : a > b ? 1 : 0;
  data.programs.sort((a, b) => groups.indexOf(group(a)) - groups.indexOf(group(b))
    || compareText(a.name, b.name) || compareText(a.source, b.source));
  const chart = overlay.querySelector('.run-timing-chart');
  const rows = overlay.querySelector('.run-timing-rows');
  const axis = overlay.querySelector('.run-timing-axis');
  const track = overlay.querySelector('.run-timing-track');
  const guide = overlay.querySelector('.run-timing-guide');
  const handle = overlay.querySelector('.run-timing-handle');
  const slackLabel = overlay.querySelector('.run-timing-slack');
  const metricSelect = document.getElementById('run-timing-metric');
  const multiplierInput = document.getElementById('run-timing-multiplier');
  const hoverInfo = overlay.querySelector('.run-timing-hover');
  let model = null, metric = 'cpu', limit = data.current_ms || STEP, multiplier = 1;
  let displayed = [];
  let dragging = null;
  const pointsByTest = new Map();
  let highlighted = [], hoveredTest = null, focusedTest = null, activeTest = null;
  let highlightFrame = null;

  function highlight() {
    highlightFrame = null;
    const point = hoveredTest ?? focusedTest;
    const text = point?.getAttribute('aria-label') || '';
    if (hoverInfo.textContent !== text) hoverInfo.textContent = text;
    const next = point?.dataset.test ?? null;
    if (next === activeTest) return;
    activeTest = next;
    for (const point of highlighted) point.classList.remove('is-highlighted');
    highlighted = pointsByTest.get(next) || [];
    for (const point of highlighted) point.classList.add('is-highlighted');
  }
  function scheduleHighlight() {
    if (highlightFrame === null) highlightFrame = requestAnimationFrame(highlight);
  }
  function clearHighlight() {
    if (highlightFrame !== null) cancelAnimationFrame(highlightFrame);
    hoveredTest = focusedTest = null;
    highlight();
  }
  const testAt = target => target instanceof Element && rows.contains(target)
    ? target.closest('.run-timing-point') : null;
  rows.addEventListener('pointerover', event => { hoveredTest = testAt(event.target); scheduleHighlight(); });
  rows.addEventListener('pointerout', event => { hoveredTest = testAt(event.relatedTarget); scheduleHighlight(); });
  rows.addEventListener('focusin', event => { focusedTest = testAt(event.target); scheduleHighlight(); });
  rows.addEventListener('focusout', event => { focusedTest = testAt(event.relatedTarget); scheduleHighlight(); });

  function element(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  const seconds = ms => `${Number((ms / 1000).toFixed(3))}s`;
  const position = value => `${100 * value / model.max}%`;

  function update() {
    const result = evaluateTiming(model, limit);
    guide.dataset.state = result.state;
    slackLabel.textContent = result.slack ? [
      Number.isFinite(result.slack.lower) ? `+${seconds(result.slack.lower)}` : '',
      Number.isFinite(result.slack.upper) ? `\u2212${seconds(result.slack.upper)}` : '',
    ].filter(Boolean).join(' / ') : '';
    slackLabel.title = slackLabel.textContent
      ? '+: room to lower TL; \u2212: room to raise TL.' : '';
    chart.style.setProperty('--timing-limit', position(limit));
    guide.setAttribute('aria-valuemax', model.max);
    guide.setAttribute('aria-valuenow', limit);
    guide.setAttribute('aria-valuetext', `${seconds(limit)}: ${stateText[result.state]}`);
    handle.textContent = seconds(limit);
    handle.title = stateText[result.state];
    for (const { index, output } of displayed) {
      const row = result.rows[index];
      output.textContent = row.label + (row.state === 'yellow' ? ' \u26a0' : '');
      output.dataset.state = row.state;
      output.title = `${row.label}: ${stateText[row.state]}`;
    }
    const width = track.getBoundingClientRect().width;
    for (const tick of axis.children) {
      tick.style.visibility = Math.abs(Number(tick.dataset.time) - limit) / model.max * width < 26 ? 'hidden' : '';
    }
  }

  function layout() {
    clearHighlight();
    pointsByTest.clear();
    chart.style.setProperty('--timing-count', model.programs.length || 1);
    rows.replaceChildren();
    displayed = model.programs.map(({ program }, index) => {
      const row = element('div', 'run-timing-row');
      row.dataset.expected = group(program);
      const name = element('span', 'run-timing-source', program.name);
      name.title = `${program.source}\nExpected: ${program.required.join('/') || program.allowed.join('/')}`;
      const plot = element('div', 'run-timing-plot');
      for (const [index, item] of program.cases.entries()) {
        const time = item[metric] === null ? null : item[metric] * multiplier;
        if (time === null) continue;
        const point = element('button', 'run-timing-point' + (item.censored ? ' run-timing-censored' : ''), item.censored ? '\u25b7' : '');
        point.type = 'button';
        point.style.left = position(time);
        point.style.top = `${30 + (index % 3) * 20}%`;
        point.dataset.correct = String(item.correct);
        point.dataset.test = item.test;
        point.setAttribute('aria-label', `${program.name} \u00b7 ${item.test} \u00b7 ${seconds(time)}${multiplier !== 1 ? ` (${seconds(item[metric])} \u00d7 ${multiplier})` : ''}${item.censored ? ' \u00b7 incomplete runtime (lower bound)' : ''}`);
        if (!pointsByTest.has(item.test)) pointsByTest.set(item.test, []);
        pointsByTest.get(item.test).push(point);
        plot.append(point);
      }
      const output = element('span', 'run-timing-result');
      row.append(name, plot, output);
      rows.append(row);
      return { index, output, plot };
    });
    const tickStep = Math.max(STEP, Math.ceil(model.max / Math.max(1, Math.floor(track.getBoundingClientRect().width / 90)) / STEP) * STEP);
    const ticks = [];
    for (let time = 0; time < model.max; time += tickStep) ticks.push(time);
    ticks.push(model.max);
    axis.replaceChildren(...ticks.map(time => {
      const tick = element('span', '', seconds(time));
      tick.dataset.time = time;
      tick.style.left = position(time);
      return tick;
    }));
    for (const { plot } of displayed) plot.style.backgroundSize = `${tickStep / model.max * 100}% 100%`;
    update();
  }

  function prepare(nextMultiplier = multiplier) {
    model = prepareTiming({ ...data, current_ms: Math.max(data.current_ms, limit) }, metric, nextMultiplier);
    multiplier = nextMultiplier;
    limit = snapLimit(limit, model.max);
    track.replaceChildren(...model.bands.map(band => {
      const node = element('span', 'run-timing-band');
      node.dataset.state = band.state;
      node.style.left = position(band.from);
      node.style.width = position(band.to - band.from);
      node.title = `${seconds(band.from)}\u2013${seconds(band.to)}: ${stateText[band.state]}`;
      return node;
    }));
    layout();
  }

  function setLimit(value) {
    const next = snapLimit(value, model.max);
    if (next === limit) return;
    limit = next;
    update();
  }
  function move(event) {
    const rect = track.getBoundingClientRect();
    if (rect.width) setLimit((event.clientX - rect.left) / rect.width * model.max);
  }
  for (const control of [guide, track]) {
    control.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault();
      control.setPointerCapture(event.pointerId);
      dragging = { control, id: event.pointerId };
      guide.focus({ preventScroll: true });
      move(event);
    });
    control.addEventListener('pointermove', event => { if (dragging?.control === control && dragging.id === event.pointerId) move(event); });
    for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) {
      control.addEventListener(name, () => { dragging = null; });
    }
  }
  guide.addEventListener('keydown', event => {
    const keys = { ArrowLeft: limit - STEP, ArrowRight: limit + STEP, Home: STEP, End: model.max };
    if (!(event.key in keys)) return;
    event.preventDefault();
    setLimit(keys[event.key]);
  });
  metricSelect.addEventListener('change', () => { metric = metricSelect.value; prepare(); });
  multiplierInput.addEventListener('change', () => {
    const next = multiplierInput.valueAsNumber;
    multiplierInput.setCustomValidity('');
    if (!multiplierInput.reportValidity()) return;
    if (next === multiplier) return;
    try {
      prepare(next);
    } catch (error) {
      if (!(error instanceof RangeError)) throw error;
      multiplierInput.setCustomValidity(error.message);
      multiplierInput.reportValidity();
      return;
    }
  });
  document.addEventListener('polygonlike:popup-opened', event => {
    if (event.detail.overlay !== overlay) return;
    if (model === null) prepare();
    else layout();
  });
  document.addEventListener('polygonlike:popup-closed', event => {
    if (event.detail.overlay !== overlay) return;
    if (dragging?.control.hasPointerCapture(dragging.id)) dragging.control.releasePointerCapture(dragging.id);
    dragging = null;
    clearHighlight();
  });
  window.addEventListener('resize', () => { if (model && !overlay.hidden) layout(); });
  if (!overlay.hidden) prepare();
});
