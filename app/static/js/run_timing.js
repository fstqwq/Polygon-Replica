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
  const chart = overlay.querySelector('.run-timing-chart');
  const rows = overlay.querySelector('.run-timing-rows');
  const axis = overlay.querySelector('.run-timing-axis');
  const track = overlay.querySelector('.run-timing-track');
  const guide = overlay.querySelector('.run-timing-guide');
  const handle = overlay.querySelector('.run-timing-handle');
  const picked = overlay.querySelector('.run-timing-picked');
  const pages = overlay.querySelector('.run-timing-pages');
  const metricSelect = document.getElementById('run-timing-metric');
  let model = null, metric = 'cpu', limit = data.current_ms || STEP;
  let page = 0, pageSize = 20, displayed = [];
  let dragging = null;

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
    guide.style.left = position(limit);
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
    pageSize = Math.max(1, Math.min(20, Math.floor((window.innerHeight - 200) / 22)));
    page = Math.min(page, Math.max(0, Math.ceil(model.programs.length / pageSize) - 1));
    const start = page * pageSize;
    const visible = model.programs.slice(start, start + pageSize);
    chart.style.setProperty('--timing-count', visible.length || 1);
    rows.replaceChildren();
    displayed = visible.map(({ program }, offset) => {
      const row = element('div', 'run-timing-row');
      row.dataset.expected = program.allowed.length === 1 && program.allowed[0] === 'AC' ? 'ac'
        : program.required.includes('TL') ? 'tl' : program.required.includes('WA') ? 'wa' : 'other';
      const name = element('span', 'run-timing-source', program.name);
      name.title = `${program.source}\nExpected: ${program.required.join('/') || program.allowed.join('/')}`;
      const plot = element('div', 'run-timing-plot');
      for (const [index, item] of program.cases.entries()) {
        const time = item[metric];
        if (time === null) continue;
        const point = element('button', 'run-timing-point' + (item.censored ? ' run-timing-censored' : ''), item.censored ? '\u25b7' : '');
        point.type = 'button';
        point.style.left = position(time);
        point.style.top = `${30 + (index % 3) * 20}%`;
        point.dataset.correct = String(item.correct);
        point.title = `${program.name} \u00b7 ${item.test} \u00b7 ${seconds(time)}${item.censored ? ' \u00b7 incomplete runtime (lower bound)' : ''}`;
        point.setAttribute('aria-label', point.title);
        point.addEventListener('click', () => { picked.textContent = point.title; });
        plot.append(point);
      }
      const output = element('span', 'run-timing-result');
      row.append(name, plot, output);
      rows.append(row);
      return { index: start + offset, output, plot };
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
    pages.hidden = model.programs.length <= pageSize;
    overlay.querySelector('.run-timing-page-label').textContent = `${start + 1}\u2013${start + visible.length} / ${model.programs.length}`;
    for (const button of pages.querySelectorAll('button')) {
      button.disabled = Number(button.dataset.pageStep) < 0 ? page === 0 : start + pageSize >= model.programs.length;
    }
    update();
  }

  function prepare() {
    model = prepareTiming(data, metric);
    limit = snapLimit(limit, model.max);
    track.replaceChildren(...model.bands.map(band => {
      const node = element('span', 'run-timing-band');
      node.dataset.state = band.state;
      node.style.left = position(band.from);
      node.style.width = position(band.to - band.from);
      node.title = `${seconds(band.from)}\u2013${seconds(band.to)}: ${stateText[band.state]}`;
      return node;
    }));
    if (model.suggested !== null) {
      const marker = element('span', 'run-timing-suggested', '\u25c6');
      marker.style.left = position(model.suggested);
      marker.title = `Suggested TL: ${seconds(model.suggested)}`;
      track.append(marker);
    }
    picked.textContent = '';
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
  for (const button of pages.querySelectorAll('button')) {
    button.addEventListener('click', () => { page += Number(button.dataset.pageStep); layout(); });
  }
  document.addEventListener('polygonlike:popup-opened', event => {
    if (event.detail.overlay !== overlay) return;
    if (model === null) prepare();
    else layout();
  });
  document.addEventListener('polygonlike:popup-closed', event => {
    if (event.detail.overlay !== overlay) return;
    if (dragging?.control.hasPointerCapture(dragging.id)) dragging.control.releasePointerCapture(dragging.id);
    dragging = null;
  });
  window.addEventListener('resize', () => { if (model && !overlay.hidden) layout(); });
  if (!overlay.hidden) prepare();
});
