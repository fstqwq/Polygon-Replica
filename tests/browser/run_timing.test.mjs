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
    assert.equal(model.suggested, null);
  }
  const empty = prepareTiming(preview([accepted([])]), 'cpu');
  assert.equal(evaluateTiming(empty, 1000).rows[0].label, '?1');
  const failed = prepareTiming(preview([accepted([point(null), point(300, 300, { verdict: 'WA', correct: false })])]), 'cpu');
  assert.equal(evaluateTiming(failed, 1000).state, 'red');
});

test('bands match exact 500ms projections, including inclusive warning boundaries, and suggest first green', () => {
  const data = preview([accepted([point(600, 1100)]), timedOut([point(3500, 4000)])]);
  const cpu = prepareTiming(data, 'cpu');
  assert.equal(cpu.suggested, 1500);
  assert.equal(evaluateTiming(cpu, 1200).state, 'yellow');
  assert.equal(evaluateTiming(cpu, 1201).state, 'green');
  for (const metric of ['cpu', 'wall']) {
    const model = prepareTiming(data, metric);
    for (let value = 500; value <= model.max; value += 500) {
      const band = model.bands.find(b => value >= b.from && (value < b.to || (b.to === model.max && value === model.max)));
      assert.equal(band.state, evaluateTiming(model, value).state);
    }
  }
  assert.equal(snapLimit(2249, cpu.max), 2000);
  assert.equal(snapLimit(2250, cpu.max), 2500);
  assert.equal(snapLimit(-1, cpu.max), 500);
});

test('all programs and testcases influence safety independently of visible page; each testcase counts once', () => {
  const data = preview(Array.from({ length: 21 }, () => accepted(Array.from({ length: 100 }, () => point(100)))));
  data.programs[20].cases[99] = point(3000);
  const model = prepareTiming(data, 'cpu');
  assert.equal(evaluateTiming(model, 2000).state, 'red');
  assert.equal(evaluateTiming(model, 2000).rows[20].label, 'TL1');
  assert.equal(model.suggested, null);
});

test('sparse safety intervals agree with direct projection at every selectable limit', () => {
  for (const metric of ['cpu', 'wall']) {
    for (const time of [0, 499.9, 500, 500.1, 749.9, 750, 1000, 1500.1, 4500]) {
      const data = preview([
        accepted([point(time, time + 250)]),
        timedOut([point(6000, 6500, { censored: true, correct: false, verdict: 'TL' })]),
      ]);
      const model = prepareTiming(data, metric);
      for (let limit = 500; limit <= model.max; limit += 500) {
        const band = model.bands.find(item => item.from <= limit && (item.to > limit || (item.to === model.max && limit === model.max)));
        assert.equal(band.state, evaluateTiming(model, limit).state, `${metric} ${time} ${limit}`);
      }
    }
  }
});
