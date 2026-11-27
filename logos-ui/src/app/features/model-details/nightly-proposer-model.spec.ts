import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { NightlyProposerModel } from './nightly-proposer-model';

const settingsUrl = '/api/logosdb/model_benchmarks/nightly/proposer';
const settings = {
  selection: { provider: 'local', model: 'local/model', revision: null },
  local_models: ['local/model', 'local/alternative'],
  logos_prod: { available: true, models: ['prod/model'] },
};

describe('NightlyProposerModel', () => {
  let fixture: ComponentFixture<NightlyProposerModel>;
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [NightlyProposerModel],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    fixture = TestBed.createComponent(NightlyProposerModel);
    http = TestBed.inject(HttpTestingController);
    fixture.detectChanges();
  });

  afterEach(() => http.verify());

  async function load(response: object = settings) {
    http.expectOne(settingsUrl).flush(response);
    await fixture.whenStable();
    fixture.detectChanges();
  }

  it('changes provider and saves only the selected model and revision', async () => {
    await load();
    const element = fixture.nativeElement as HTMLElement;
    const provider = element.querySelector<HTMLSelectElement>('select[name=provider]')!;
    const model = element.querySelector<HTMLSelectElement>('select[name=model]')!;
    const save = element.querySelector<HTMLButtonElement>('button[type=submit]')!;
    expect(model.value).toBe('local/model');
    expect(save.disabled).toBe(true);

    provider.value = 'logos_prod';
    provider.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    await fixture.whenStable();
    expect(model.value).toBe('');
    expect(Array.from(model.options, option => option.value)).toEqual(['', 'prod/model']);

    model.value = 'prod/model';
    model.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    expect(save.disabled).toBe(false);
    save.click();
    const request = http.expectOne(settingsUrl + '/configure');
    expect(request.request.body).toEqual({ provider: 'logos_prod', model: 'prod/model', revision: null });
    request.flush({ provider: 'logos_prod', model: 'prod/model', revision: 1 });
    await fixture.whenStable();
    fixture.detectChanges();
    expect(save.disabled).toBe(true);
    expect(element.textContent).toContain('Saved for future parameter suggestions.');
  });

  it('reloads the latest settings after a concurrent edit', async () => {
    await load();
    const component = fixture.componentInstance;
    component.selectModel('local/alternative');
    const save = component.save();
    http.expectOne(settingsUrl + '/configure').flush({}, { status: 409, statusText: 'Conflict' });
    await Promise.resolve();
    http.expectOne(settingsUrl).flush({
      ...settings,
      selection: { provider: 'logos_prod', model: 'prod/model', revision: 1 },
    });
    await save;
    expect(component.provider()).toBe('logos_prod');
    expect(component.model()).toBe('prod/model');
    expect(component.error()).toContain('Another admin changed the selection');
  });

  it('allows retrying a failed catalog load', async () => {
    http.expectOne(settingsUrl).flush({}, { status: 503, statusText: 'Unavailable' });
    await fixture.whenStable();
    fixture.detectChanges();
    const element = fixture.nativeElement as HTMLElement;
    expect(element.querySelector('[role=alert]')?.textContent).toContain('Could not load');
    element.querySelector<HTMLButtonElement>('[role=alert] button')!.click();
    await load();
    expect(element.querySelector('[role=alert]')).toBeNull();
  });

  it('keeps the draft available when the server rejects a save', async () => {
    await load();
    const component = fixture.componentInstance;
    component.selectProvider('local');
    component.selectModel('local/alternative');
    const save = component.save();
    http.expectOne(settingsUrl + '/configure').flush(
      { detail: 'This model is unavailable from the selected provider' },
      { status: 400, statusText: 'Bad Request' },
    );
    await save;
    expect(component.model()).toBe('local/alternative');
    expect(component.canSave()).toBe(true);
    expect(component.error()).toContain('This model is unavailable');
  });

  it('shows a retry option when the selected PROD catalog is unavailable', async () => {
    await load({
      ...settings,
      selection: { provider: 'logos_prod', model: 'prod/model', revision: 1 },
      logos_prod: { available: false, models: [] },
    });
    const element = fixture.nativeElement as HTMLElement;
    expect(element.textContent).toContain('Could not load models from Logos PROD');
    expect(element.querySelector<HTMLSelectElement>('select[name=model]')?.disabled).toBe(true);
    element.querySelector<HTMLButtonElement>('[role=status] button')!.click();
    await load({
      ...settings,
      selection: { provider: 'logos_prod', model: 'prod/model', revision: 1 },
    });
    expect(element.querySelector<HTMLSelectElement>('select[name=model]')?.value).toBe('prod/model');
  });
});
