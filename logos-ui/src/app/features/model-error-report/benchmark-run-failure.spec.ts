import { TestBed } from '@angular/core/testing';
import { BenchmarkRunFailure } from './benchmark-run-failure';

describe('Benchmark failure details', () => {
  it('renders a scheduled cutoff as a neutral stop and keeps its technical reason in the disclosure', async () => {
    const fixture = TestBed.createComponent(BenchmarkRunFailure);
    fixture.componentRef.setInput('run', { status: 'failed', error_message: 'window_closed_below_half', result: {} });
    await fixture.whenStable();
    const section = fixture.nativeElement.querySelector('section');
    expect(section.getAttribute('role')).toBe('status');
    expect(section.getAttribute('aria-label')).toBe('Benchmark stopped');
    expect(section.classList.contains('window-stop')).toBe(true);
    expect(fixture.componentInstance.summary()).toContain('nightly window ended');
    expect(fixture.componentInstance.summary()).toContain('not used as a completed measurement');
    expect(fixture.nativeElement.querySelector('summary').textContent).toContain('View stop details');
    expect(fixture.nativeElement.querySelector('pre').textContent).toBe('window_closed_below_half');
    expect(fixture.nativeElement.querySelector('details').open).toBe(false);
  });

  it.each(['stalled', 'preparation_timeout', 'lease_lost', 'GPU failed near window_closed_below_half'])(
    'keeps %s as an actual error', async message => {
      const fixture = TestBed.createComponent(BenchmarkRunFailure);
      fixture.componentRef.setInput('run', { status: 'failed', error_message: message, result: {} });
      await fixture.whenStable();
      expect(fixture.nativeElement.querySelector('section').getAttribute('role')).toBe('alert');
      expect(fixture.nativeElement.querySelector('summary').textContent).toContain('View error details');
    });

  it('does not hide a recorded model failure behind a generic cutoff message', async () => {
    const fixture = TestBed.createComponent(BenchmarkRunFailure);
    fixture.componentRef.setInput('run', { status: 'failed', error_message: 'window_closed_below_half',
      result: { failure: { details: 'CUDA out of memory', settings: {} } } });
    await fixture.whenStable();
    expect(fixture.nativeElement.querySelector('section').getAttribute('role')).toBe('alert');
    expect(fixture.componentInstance.summary()).toContain('CUDA out of memory');
  });

  it('shows the failed backend, repetition and root cause without interpreting the return code', async () => {
    const fixture = TestBed.createComponent(BenchmarkRunFailure);
    fixture.componentRef.setInput('run', {
      error_message: 'return_code=1',
      result: { configuration_index: 2, repetition: 1, completed_runs: 3, total_runs: 9,
        failure: { model: 'org/model', attention_backend: 'FLASHINFER', settings: { samples: 5 },
          details: 'return_code=1. Cause: ValueError: unsupported head size. Recent logs: shutdown' } },
    });
    await fixture.whenStable();
    expect(fixture.nativeElement.textContent).toContain('FLASHINFER');
    expect(fixture.nativeElement.textContent).toContain('Configuration 2 · repetition 1');
    expect(fixture.componentInstance.summary()).toBe('ValueError: unsupported head size.');
  });
  it('explains that historical truncated errors have no recorded root cause', async () => {
    const fixture = TestBed.createComponent(BenchmarkRunFailure);
    fixture.componentRef.setInput('run', { error_message: 'vLLM exited during startup (return_code=1). Engine core initialization failed', result: {} });
    await fixture.whenStable();
    expect(fixture.componentInstance.summary()).toContain('does not identify the cause');
    expect(fixture.nativeElement.querySelector('pre').textContent).toContain('return_code=1');
  });
});
