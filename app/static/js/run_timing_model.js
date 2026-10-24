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

export function prepareTiming(data, metric) {
  let max = Math.max(STEP, data.current_ms);
  const transitions = new Set([STEP]);
  const transition = time => {
    // Verdict and warning boundaries have different inclusive ends. Include
    // both neighboring selectable limits instead of rounding measured times.
    const before = Math.max(STEP, Math.floor(time / STEP) * STEP);
    transitions.add(before);
    transitions.add(before + STEP);
  };
  const programs = data.programs.map(program => {
    const correct = [], wrong = [], censored = [], fixed = {};
    let missing = 0, warningMax = 0, allCorrect = program.cases.length > 0;
    for (const item of program.cases) {
      const time = item[metric];
      if (time !== null) max = Math.max(max, time);
      allCorrect &&= item.complete && item.correct && !item.censored && time !== null;
      if (time !== null) warningMax = Math.max(warningMax, time);
      if (['CE', 'RE', 'FL'].includes(item.verdict)) {
        fixed[item.verdict] = (fixed[item.verdict] || 0) + 1;
      } else if (!item.complete) missing++;
      else if (item.censored && metric === 'cpu') fixed.TL = (fixed.TL || 0) + 1;
      else if (time === null) missing++;
      else {
        (item.censored ? censored : item.correct ? correct : wrong).push(time);
        transition(time);
      }
    }
    for (const values of [correct, wrong, censored]) values.sort((a, b) => a - b);
    if (allCorrect && warningMax > 0) {
      transition(warningMax * 2 / 3);
      transition(warningMax * 2);
    }
    return { program, correct, wrong, censored, fixed, missing, warningMax: allCorrect ? warningMax : null };
  });
  max = Math.ceil(max / STEP) * STEP;
  const model = { programs, max, metric };
  const limits = [...transitions].filter(value => value <= max).sort((a, b) => a - b);
  const bands = [];
  for (let i = 0; i < limits.length; i++) {
    const value = limits[i];
    const state = evaluateTiming(model, value).state;
    const from = value === STEP ? 0 : value - STEP / 2;
    const to = i + 1 < limits.length ? limits[i + 1] - STEP / 2 : max;
    const previous = bands.at(-1);
    if (previous?.state === state) previous.to = to;
    else bands.push({ from, to, state, first: value });
  }
  const hasAccepted = data.programs.some(p => p.required.includes('AC') && p.allowed.length === 1);
  return { ...model, bands, suggested: hasAccepted ? bands.find(b => b.state === 'green')?.first ?? null : null };
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
  return { rows, state };
}

export function snapLimit(value, max) {
  return Math.max(STEP, Math.min(max, Math.round(value / STEP) * STEP));
}
