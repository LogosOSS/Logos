import { TestBed } from '@angular/core/testing';
import { RequestRouting, StatisticsService } from '../../services/statistics.service';
import { RequestRoutingComponent } from './request-routing';

const decision: RequestRouting = {
  selected_model_id: 1, selected_provider_id: 20,
  candidates: [
    { model_id: 1, provider_id: 10, provider_name: 'Node A', model_name: 'Model', provider_type: 'logosnode', state: 'cold', queue_depth: 0, expected_wait_s: 45 },
    { model_id: 1, provider_id: 20, provider_name: 'Node B', model_name: 'Model', provider_type: 'logosnode', state: 'warm', queue_depth: 0, expected_wait_s: 0.2 },
  ],
};

describe('Request routing details', () => {
  const getRequestRouting = vi.fn();
  beforeEach(() => {
    getRequestRouting.mockReset().mockResolvedValue(decision);
    TestBed.configureTestingModule({ providers: [{ provide: StatisticsService, useValue: { getRequestRouting } }] });
  });

  it('shows only the four requested columns and highlights the actual dispatch', async () => {
    const fixture = TestBed.createComponent(RequestRoutingComponent);
    fixture.componentRef.setInput('requestId', 'request-a');
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    const element: HTMLElement = fixture.nativeElement;
    expect(Array.from(element.querySelectorAll('th'), th => th.textContent?.trim()))
      .toEqual(['Candidate', 'State', 'Requests in Queue', 'Est. Wait']);
    expect(element.querySelector('tr.selected')?.textContent).toContain('Node B');
    expect(element.textContent).toContain('0.2 s');
    expect(element.textContent).toContain('Nodes serving the same model share its queue');
    expect(element.textContent).not.toContain('Prefix');
    expect(element.textContent).not.toContain('Score');
  });

  it('keeps old requests and unavailable estimates honest', async () => {
    const fixture = TestBed.createComponent(RequestRoutingComponent);
    fixture.componentRef.setInput('requestId', 'legacy');
    getRequestRouting.mockResolvedValueOnce({ ...decision, candidates: [] });
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('No routing decision recorded');
    getRequestRouting.mockResolvedValueOnce({ ...decision, candidates: [
      { ...decision.candidates[0], state: 'unavailable', queue_depth: null, expected_wait_s: null },
    ] });
    await fixture.componentInstance.load();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('unavailable');
    expect(fixture.nativeElement.querySelector('.wait').textContent.trim()).toBe('—');
  });

  it('ignores an earlier response after switching requests or destroying the panel', async () => {
    const component = TestBed.runInInjectionContext(() => new RequestRoutingComponent());
    let resolve!: (value: RequestRouting) => void;
    getRequestRouting.mockReturnValueOnce(new Promise<RequestRouting>(r => resolve = r));
    const first = component.load();
    const newer = { ...decision, selected_provider_id: 10 };
    getRequestRouting.mockResolvedValueOnce(newer);
    await component.load();
    resolve(decision);
    await first;
    expect(component.routing()).toEqual(newer);
    getRequestRouting.mockReturnValueOnce(new Promise<RequestRouting>(r => resolve = r));
    const last = component.load();
    component.ngOnDestroy();
    resolve(decision);
    await last;
    expect(component.routing()).toBeNull();
  });

  it('can retry an error and preserves cloud queue values as unavailable', async () => {
    const component = TestBed.runInInjectionContext(() => new RequestRoutingComponent());
    getRequestRouting.mockRejectedValueOnce(new Error('offline'));
    await component.load();
    expect(component.error()).toBe(true);
    await component.load();
    expect(component.error()).toBe(false);
    expect(component.stateLabel({ ...decision.candidates[0], provider_type: 'cloud', state: 'warm' })).toBe('cloud');
    expect(component.stateLabel({ ...decision.candidates[0], provider_type: 'cloud', state: 'unavailable' })).toBe('unavailable');
  });
});
