import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const module = new vm.SourceTextModule(await readFile(new URL('../../app/static/js/run_timing_model.js', import.meta.url), 'utf8'));
await module.link(() => { throw new Error('unexpected dependency'); });
await module.evaluate();
const { prepareTiming, evaluateTiming, snapLimit } = module.namespace;
const point = (cpu, wall = cpu, extra = {}) => ({ test: '001.in', cpu, wall, correct: true, verdict: 'AC', censored: false, complete: true, ...extra });
const program = (required, allowed, cases) => ({ id: 'solution', name: 'solution.cpp', source: 'solutions/solution.cpp', required, allowed, cases });
const accepted = cases => program(['AC'], ['AC'], cases);
const timedOut = cases => program(['TL'], ['AC', 'TL'], cases);
const preview = programs => ({ current_ms: 2000, programs });

test('slack crosses warnings and ends at unexpected results, using each program’s last TL case', () => {
  const data = preview([accepted([point(600)]), timedOut([point(2500), point(3500)]), timedOut([point(4500)])]);
  const model = prepareTiming(data, 'cpu');
  assert.equal(evaluateTiming(model, 2000).slack.lower, 1400);
  assert.equal(evaluateTiming(model, 2000).slack.upper, 1500);
  assert.equal(evaluateTiming(model, 1200).state, 'yellow');
  assert.equal(evaluateTiming(model, 1200).slack.lower, 600);
  assert.equal(evaluateTiming(model, 1200).slack.upper, 2300);
  assert.equal(evaluateTiming(model, 600).slack.lower, 0);
  for (const limit of [500, 3500]) assert.equal(evaluateTiming(model, limit).slack, null);
  const scaled = prepareTiming(data, 'cpu', 2);
  assert.equal(evaluateTiming(scaled, 4000).slack.lower, 2800);
  assert.equal(evaluateTiming(scaled, 4000).slack.upper, 3000);
});

test('complete evidence without an upper constraint has infinite upper slack', () => {
  for (const row of [accepted([point(400)]), program([], ['AC', 'TL'], [point(400)]),
    program(['TL', 'RE'], ['AC', 'TL', 'RE'], [point(400), point(null, null, { verdict: 'RE', correct: false })])]) {
    const model = prepareTiming(preview([row]), 'cpu');
    assert.equal(evaluateTiming(model, 2000).slack.upper, Infinity);
  }
  const model = prepareTiming(preview([accepted([point(400)])]), 'cpu');
  assert.equal(evaluateTiming(model, model.max).slack.lower, 1600);
});

test('hard-killed TL evidence leaves the upper margin unknown for either metric', () => {
  const killed = timedOut([point(3000, 3500, { censored: true, correct: false, verdict: 'TL' })]);
  for (const metric of ['cpu', 'wall']) {
    const model = prepareTiming(preview([accepted([point(400)]), killed]), metric);
    assert.equal(evaluateTiming(model, 2000).slack.lower, 1600);
    assert.equal(evaluateTiming(model, 2000).slack.upper, null);
    const bound = killed.cases[0][metric];
    for (const limit of [bound, bound + 500, bound + 5000]) {
      assert.equal(evaluateTiming(model, limit).slack.upper, null);
      assert.equal(evaluateTiming(model, limit).slack.lower, limit - 400);
    }
    const scaled = prepareTiming(preview([accepted([point(400)]), killed]), metric, 2);
    assert.equal(evaluateTiming(scaled, bound * 2 + 500).slack.lower, bound * 2 + 500 - 800);
    // A definite mismatch before uncertain evidence still supplies a bound.
    const early = prepareTiming(preview([accepted([point(400)]), killed, timedOut([point(2500)])]), metric);
    assert.equal(evaluateTiming(early, 2000).slack.upper, 500);
    const late = prepareTiming(preview([accepted([point(400)]), killed, timedOut([point(5000)])]), metric);
    assert.equal(evaluateTiming(late, 2000).slack.upper, null);
    assert.equal(evaluateTiming(late, 5500).slack, null);
  }
});

test('missing and empty evidence cannot imply an unlimited margin', () => {
  for (const programs of [[], [accepted([])], [accepted([point(null)])], [timedOut([point(null, null, { censored: true, correct: false, verdict: 'TL' })])]]) {
    const result = evaluateTiming(prepareTiming(preview(programs), 'cpu'), 2000);
    assert.equal(result.slack.lower, null);
    assert.equal(result.slack.upper, null);
  }
});

test('runtime multiplier scales projections and bounds without changing evidence or fixed verdicts', () => {
  const data = preview([accepted([point(400)]), timedOut([point(3000, 3500, { verdict: 'TL', censored: true, correct: false })])]);
  const original = JSON.stringify(data);
  for (const multiplier of [0.5, 1, 2]) {
    const cpu = prepareTiming(data, 'cpu', multiplier);
    assert.equal(evaluateTiming(cpu, 500).rows[0].label, multiplier === 2 ? 'TL1' : 'AC');
    assert.equal(evaluateTiming(cpu, 10000).rows[1].label, 'TL1');
    assert.equal(cpu.bands[0].to, 400 * multiplier);
    const wall = prepareTiming(data, 'wall', multiplier);
    assert.equal(evaluateTiming(wall, 3500 * multiplier - 1).rows[1].label, 'TL1');
    assert.equal(evaluateTiming(wall, 3500 * multiplier).rows[1].label, '?1');
    const missing = prepareTiming(preview([accepted([point(null)])]), 'cpu', multiplier);
    assert.equal(evaluateTiming(missing, 500).state, 'gray');
  }
  assert.equal(JSON.stringify(data), original);
  for (const multiplier of [0, -1, NaN, Infinity, Number.MAX_VALUE]) {
    assert.throws(() => prepareTiming(data, 'cpu', multiplier), { name: 'RangeError' });
  }
});

test('complete soft TL can become AC; censored CPU remains TL while wall becomes unknown at its bound', () => {
  const data = preview([
    accepted([point(1200, 1800, { verdict: 'TL' })]),
    timedOut([point(3000, 3500, { verdict: 'TL', correct: false, censored: true })]),
  ]);
  const cpu = prepareTiming(data, 'cpu');
  assert.equal(evaluateTiming(cpu, 1500).rows[0].label, 'AC');
  assert.equal(evaluateTiming(cpu, 10000).rows[1].label, 'TL1');
  const wall = prepareTiming(data, 'wall');
  assert.equal(evaluateTiming(wall, 3499).rows[1].label, 'TL1');
  assert.equal(evaluateTiming(wall, 3500).rows[1].label, '?1');
  assert.equal(evaluateTiming(wall, 3500).state, 'gray');
  assert.equal(evaluateTiming(wall, 3500).rows[1].warning, false);
});

test('required and allowed rules retain strict WA, TL-or-AC, TL-or-RE and compile/error behavior', () => {
  const cases = [
    [program(['WA'], ['AC', 'WA'], [point(600, 600, { correct: false, verdict: 'WA' })]), 500, 'red'],
    [program(['WA'], ['AC', 'WA'], [point(600, 600, { correct: false, verdict: 'WA' })]), 1000, 'green'],
    [program([], ['AC', 'TL'], [point(600)]), 2000, 'green'],
    [program(['TL', 'RE'], ['AC', 'TL', 'RE'], [point(null, null, { correct: false, verdict: 'RE' })]), 500, 'green'],
    [program(['CE'], ['CE'], [point(null, null, { correct: false, verdict: 'CE' })]), 500, 'green'],
    [accepted([point(null, null, { correct: false, verdict: 'FL' })]), 500, 'red'],
  ];
  for (const [row, limit, state] of cases) assert.equal(evaluateTiming(prepareTiming(preview([row]), 'cpu'), limit).state, state);
});

test('unexpected precedes unknown; missing metrics, unfinished and empty runs cannot produce safe advice', () => {
  const cases = [point(200, null), point(200, 250, { complete: false }), point(null)];
  for (const item of cases) {
    const model = prepareTiming(preview([accepted([item])]), 'wall');
    assert.equal(evaluateTiming(model, 1000).state, 'gray');
  }
  const empty = prepareTiming(preview([accepted([])]), 'cpu');
  assert.equal(evaluateTiming(empty, 1000).rows[0].label, '?1');
  const failed = prepareTiming(preview([accepted([point(null), point(300, 300, { verdict: 'WA', correct: false })])]), 'cpu');
  assert.equal(evaluateTiming(failed, 1000).state, 'red');
});

test('continuous bands retain sub-step failures and inclusive warning boundaries', () => {
  const data = preview([accepted([point(600, 1100)]), timedOut([point(3500, 4000)])]);
  const cpu = prepareTiming(data, 'cpu');
  assert.equal(evaluateTiming(cpu, 1200).state, 'yellow');
  assert.equal(evaluateTiming(cpu, 1201).state, 'green');
  for (const metric of ['cpu', 'wall']) {
    const model = prepareTiming(data, metric);
    for (const band of model.bands) {
      const value = (band.from + band.to) / 2;
      assert.equal(band.state, evaluateTiming(model, value).state);
    }
  }
  assert.equal(snapLimit(2249, cpu.max), 2000);
  assert.equal(snapLimit(2250, cpu.max), 2500);
  assert.equal(snapLimit(-1, cpu.max), 500);
  const fast = prepareTiming(preview([accepted([point(400)])]), 'cpu');
  assert.deepEqual(JSON.parse(JSON.stringify(fast.bands)), [
    { from: 0, to: 400, state: 'red' },
    { from: 400, to: 800, state: 'yellow' },
    { from: 800, to: 2000, state: 'green' },
  ]);
  const exact = prepareTiming(preview([accepted([point(500)])]), 'cpu');
  assert.equal(evaluateTiming(exact, 1000).state, 'yellow');
});

test('all programs and testcases influence safety; each testcase counts once', () => {
  const data = preview(Array.from({ length: 21 }, () => accepted(Array.from({ length: 100 }, () => point(100)))));
  data.programs[20].cases[99] = point(3000);
  const model = prepareTiming(data, 'cpu');
  assert.equal(evaluateTiming(model, 2000).state, 'red');
  assert.equal(evaluateTiming(model, 2000).rows[20].label, 'TL1');
});

test('sparse safety intervals agree with continuous projection between boundaries', () => {
  for (const metric of ['cpu', 'wall']) {
    for (const time of [0, 499.9, 500, 500.1, 749.9, 750, 1000, 1500.1, 4500]) {
      const data = preview([
        accepted([point(time, time + 250)]),
        timedOut([point(6000, 6500, { censored: true, correct: false, verdict: 'TL' })]),
      ]);
      const model = prepareTiming(data, metric);
      for (const band of model.bands) {
        for (const fraction of [0.001, 0.5, 0.999]) {
          const limit = band.from + (band.to - band.from) * fraction;
          assert.equal(band.state, evaluateTiming(model, limit).state, `${metric} ${time} ${limit}`);
        }
      }
    }
  }
});
