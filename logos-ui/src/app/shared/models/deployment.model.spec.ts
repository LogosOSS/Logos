import { describe, expect, it } from 'vitest';

import {
  DeploymentStep,
  clockTime,
  eventTitle,
  firstLine,
  fullDate,
  nodeKindLabel,
  phaseStates,
  planTone,
  stepStatusText,
  stepTone,
} from './deployment.model';

const step = (overrides: Partial<DeploymentStep> = {}): DeploymentStep => ({
  id: 's',
  position: 0,
  kind: 'worker',
  deployer_name: 'w',
  status: 'running',
  phase: 'pulling',
  failure: null,
  message: null,
  started_at: null,
  finished_at: null,
  last_activity_at: '2026-11-07T10:00:00.000Z',
  events: [],
  failure_logs: null,
  details: {},
  ...overrides,
});

describe('tones', () => {
  it('gives each step status one colour, and grey for what did not run', () => {
    expect(stepTone('running')).toBe('running');
    expect(stepTone('succeeded')).toBe('succeeded');
    expect(stepTone('unchanged')).toBe('succeeded');
    expect(stepTone('failed')).toBe('failed');
    expect(stepTone('rolled_back')).toBe('rolled-back');
    expect(stepTone('waiting')).toBe('idle');
    expect(stepTone('cancelled')).toBe('idle');
  });

  it('gives each plan status its colour', () => {
    expect(planTone('rolled_back')).toBe('rolled-back');
    expect(planTone('failed')).toBe('failed');
  });
});

describe('phaseStates', () => {
  it('shows a rollback on the last segment', () => {
    expect(phaseStates(step({ phase: 'rolling_back' }))).toEqual(['done', 'done', 'done', 'rolled_back']);
  });

  it('marks the segment of the phase for a failure without a kind', () => {
    expect(phaseStates(step({ status: 'failed', phase: 'draining', failure: null }))).toEqual([
      'done',
      'failed',
      'pending',
      'pending',
    ]);
  });

  it('marks the verify segment of a rolled-back node without a kind', () => {
    expect(phaseStates(step({ status: 'rolled_back' }))).toEqual(['done', 'done', 'done', 'failed']);
  });

  it('leaves every segment pending for a node that did not start', () => {
    expect(phaseStates(step({ status: 'waiting', phase: null }))).toEqual(['pending', 'pending', 'pending', 'pending']);
  });
});

describe('texts', () => {
  it('names the kinds of nodes', () => {
    expect(nodeKindLabel('core')).toBe('Core node');
    expect(nodeKindLabel('worker')).toBe('GPU worker');
  });

  it('takes the first line of a text', () => {
    expect(firstLine('  one\ntwo ')).toBe('one');
    expect(firstLine(null)).toBe('');
  });

  it('says what each node status means for the node row', () => {
    expect(stepStatusText(step({ status: 'succeeded' }), 'pr-1')).toBe('Done, runs pr-1');
    expect(stepStatusText(step({ status: 'unchanged' }), 'pr-1')).toBe('Unchanged, already runs pr-1');
    expect(stepStatusText(step({ status: 'running' }), 'pr-1')).toBe('Running');
    expect(stepStatusText(step({ status: 'failed', message: 'swap failed\nmore' }), 'pr-1')).toBe('swap failed');
    expect(stepStatusText(step({ status: 'rolled_back' }), 'pr-1')).toBe('Rolled back');
    expect(stepStatusText(step({ status: 'waiting' }), 'pr-1')).toBe('Waiting for the core');
  });
});

describe('times', () => {
  it('writes the full date, or nothing for a missing or broken one', () => {
    expect(fullDate('2026-10-31T13:58:00Z')).toContain('2026');
    expect(fullDate(null)).toBe('');
    expect(fullDate('not a date')).toBe('');
  });

  it('writes the clock time, or nothing for a broken one', () => {
    expect(clockTime('2026-11-07T10:00:00Z')).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(clockTime('not a date')).toBe('');
  });
});

describe('eventTitle', () => {
  it('names each phase, and uses the status for the end', () => {
    expect(eventTitle('queued', 'ordered')).toBe('Queued');
    expect(eventTitle('pulling', 'running')).toBe('Pull');
    expect(eventTitle('draining', 'running')).toBe('Drain');
    expect(eventTitle('swapping', 'running')).toBe('Swap');
    expect(eventTitle('verifying', 'running')).toBe('Verify');
    expect(eventTitle('rolling_back', 'running')).toBe('Rolling back');
    expect(eventTitle('done', 'succeeded')).toBe('Succeeded');
    expect(eventTitle('something else', 'running')).toBe('something else');
  });
});
