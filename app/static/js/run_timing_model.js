// Candidate-limit projection only. Persisted results are never changed.
export const STEP = 500;
const order = ['CE', 'TL', 'RE', 'WA', 'FL'];

function upperBound(values, limit) {
  let low = 0, high = values.length;
  while (low < high) {
    const mid = (low + high) >>> 1;
    if (values[mid] <= limit) low = mid + 1;
    else high = mid;
  }
  return low;
}

export function prepareTiming(data, metric, multiplier = 1) {
  if (!Number.isFinite(multiplier) || multiplier <= 0) throw new RangeError('Multiplier must be positive and finite');
  let max = Math.max(STEP, data.current_ms);
  const transitions = new Set([0]);
  const transition = time => transitions.add(time);
  const programs = data.programs.map(program => {
    const correct = [], wrong = [], censored = [], cpuCensored = [], fixed = {};
    let cpuMissing = 0;
    let missing = 0, warningMax = 0, allCorrect = program.cases.length > 0;
    for (const item of program.cases) {
      const time = item[metric] === null ? null : item[metric] * multiplier;
      if (time !== null && !Number.isFinite(time * 2)) throw new RangeError('Scaled runtime is too large');
      if (time !== null) max = Math.max(max, time);
      allCorrect &&= item.complete && item.correct && !item.censored && time !== null;
      if (time !== null) warningMax = Math.max(warningMax, time);
      if (['CE', 'RE', 'FL'].includes(item.verdict)) {
        fixed[item.verdict] = (fixed[item.verdict] || 0) + 1;
      } else if (!item.complete) missing++;
      else if (item.censored && metric === 'cpu') {
        fixed.TL = (fixed.TL || 0) + 1;
        if (time === null) cpuMissing++;
        else { cpuCensored.push(time); transition(time); }
      }
      else if (time === null) missing++;
      else {
        (item.censored ? censored : item.correct ? correct : wrong).push(time);
        transition(time);
      }
    }
    for (const values of [correct, wrong, censored, cpuCensored]) values.sort((a, b) => a - b);
    if (allCorrect && warningMax > 0) {
      transition(warningMax * 2 / 3);
      transition(warningMax * 2);
    }
    const row = { program, correct, wrong, censored, fixed, missing, warningMax: allCorrect ? warningMax : null };
    // A fixed CPU TL preserves the verdict projection, but cannot prove an
    // unlimited upper margin: its complete runtime was never observed.
    row.slackEvidence = metric === 'cpu' ? { ...row, censored: cpuCensored,
      missing: missing + cpuMissing, fixed: { ...fixed, TL: 0 } } : { ...row };
    return row;
  });
  max = Math.ceil(max / STEP) * STEP;
  const model = { programs, max, metric };
  transitions.add(max);
  const limits = [...transitions].sort((a, b) => a - b);
  const intervals = [], slackIntervals = [];
  for (let i = 0; i < limits.length; i++) {
    const from = limits[i], to = limits[i + 1] ?? Infinity;
    const probe = Number.isFinite(to) ? (from + to) / 2 : from + Math.max(1, from / 2);
    const state = evaluateTiming(model, probe).state;
    const previous = intervals.at(-1);
    if (previous?.state === state) previous.to = to;
    else intervals.push({ from, to, state });
    const slackState = evidenceState(programs.map(row => evaluateProgram(row.slackEvidence, probe)));
    const previousSlack = slackIntervals.at(-1);
    if (previousSlack?.state === slackState) previousSlack.to = to;
    else slackIntervals.push({ from, to, state: slackState });
  }
  const bands = intervals.filter(band => band.from < max).map(band => ({ ...band, to: Math.min(band.to, max) }));
  return { ...model, bands, slackIntervals };
}

export function evaluateProgram(row, limit) {
  const counts = { ...row.fixed };
  const accepted = upperBound(row.correct, limit);
  const wrong = upperBound(row.wrong, limit);
  const unknown = row.missing + upperBound(row.censored, limit) + (row.program.cases.length ? 0 : 1);
  counts.AC = accepted;
  counts.WA = wrong;
  counts.TL = (counts.TL || 0) + row.correct.length - accepted + row.wrong.length - wrong
    + row.censored.length - upperBound(row.censored, limit);
  const codes = Object.keys(counts).filter(code => counts[code]);
  const unexpected = codes.some(code => !row.program.allowed.includes(code))
    || (!unknown && row.program.required.length > 0 && !row.program.required.some(code => counts[code]));
  const warning = row.warningMax !== null && row.warningMax > 0
    && row.warningMax * 2 >= limit && row.warningMax * 2 <= limit * 3;
  const label = order.filter(code => counts[code]).map(code => ['TL', 'RE', 'WA'].includes(code) ? code + counts[code] : code);
  if (unknown) label.push('?' + unknown);
  return { counts, unknown, warning, unexpected, label: label.join(' / ') || 'AC',
    state: unexpected ? 'red' : unknown ? 'gray' : warning ? 'yellow' : 'green' };
}

export function evaluateTiming(model, limit) {
  const rows = model.programs.map(program => evaluateProgram(program, limit));
  const state = ['red', 'gray', 'yellow'].find(value => rows.some(row => row.state === value))
    || (rows.length ? 'green' : 'gray');
  let slack = null;
  if (model.slackIntervals) {
    const evidence = evidenceState(model.programs.map(row => evaluateProgram(row.slackEvidence, limit)));
    if (evidence === 'gray') {
      const index = model.slackIntervals.findIndex(band => band.from <= limit && limit < band.to);
      const lower = model.slackIntervals[index - 1];
      // Raising TL past a censored runtime loses the upper bound, not the
      // known failure boundary below the preceding matching interval.
      slack = {
        lower: lower?.state === 'safe' && model.slackIntervals[index - 2]?.state !== 'gray'
          ? limit - lower.from : null,
        upper: null,
      };
    }
    else if (evidence === 'safe') {
      const index = model.slackIntervals.findIndex(band => band.state === 'safe' && band.from <= limit && limit <= band.to);
      const interval = model.slackIntervals[index];
      if (interval) slack = {
        lower: model.slackIntervals[index - 1]?.state === 'gray' ? null : limit - interval.from,
        upper: model.slackIntervals[index + 1]?.state === 'gray' ? null : interval.to - limit,
      };
    }
  }
  return { rows, state, slack };
}

function evidenceState(rows) {
  return rows.some(row => row.unexpected) ? 'red'
    : !rows.length || rows.some(row => row.unknown) ? 'gray' : 'safe';
}

export function snapLimit(value, max) {
  return Math.max(STEP, Math.min(max, Math.round(value / STEP) * STEP));
}
