import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { BehaviorSubject } from 'rxjs';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ProviderManagementService } from '../../../core/services/provider-management.service';
import { NodeSettings } from '../../../shared/models/node-config.model';
import { NodeSettingsPage } from './node-settings';

const stored = (settings: NodeSettings['settings'] = {}, revision = 2): NodeSettings => ({
  provider_id: 4,
  revision,
  yaml_imported: true,
  settings,
});

describe('NodeSettingsPage', () => {
  let fixture: ComponentFixture<NodeSettingsPage>;
  let service: Record<string, ReturnType<typeof vi.fn>>;

  const settle = async (): Promise<void> => {
    await fixture.whenStable();
    await new Promise((resolve) => setTimeout(resolve, 0));
    fixture.detectChanges();
  };

  const create = async (id = '4'): Promise<HTMLElement> => {
    TestBed.configureTestingModule({
      imports: [NodeSettingsPage],
      providers: [
        provideRouter([]),
        { provide: ProviderManagementService, useValue: service },
        { provide: ActivatedRoute, useValue: { paramMap: new BehaviorSubject(convertToParamMap({ providerId: id })) } },
      ],
    });
    fixture = TestBed.createComponent(NodeSettingsPage);
    fixture.detectChanges();
    await settle();
    return fixture.nativeElement as HTMLElement;
  };

  const page = (): NodeSettingsPage => fixture.componentInstance;
  const saveButton = (root: HTMLElement): HTMLButtonElement => root.querySelector('.savebar .btn-primary') as HTMLButtonElement;

  beforeEach(() => {
    TestBed.resetTestingModule();
    service = {
      getNodeSettings: vi.fn().mockResolvedValue(stored({ gpu_devices: 'all' })),
      getNodeInsights: vi.fn().mockResolvedValue({
        provider_id: 4,
        worker: { online: true, supports_actions: {}, config_revision: 1 },
        gpus: [
          { index: 0, name: 'A100', total_mb: 40000, used_mb: 1 },
          { index: 1, name: 'A100', total_mb: 40000, used_mb: 1 },
        ],
        models: [],
      }),
      getProviders: vi.fn().mockResolvedValue([{ id: 4, name: 'hochbruegge' }]),
      replaceNodeSettings: vi.fn().mockResolvedValue({ provider_id: 4, settings_revision: 3 }),
    };
  });

  it('shows the stored settings of the node', async () => {
    const root = await create();

    expect(root.querySelector('h1')?.textContent).toContain('Node settings');
    expect(root.textContent).toContain('hochbruegge');
    expect((root.querySelector('#gpu-mode') as HTMLSelectElement).value).toBe('all');
    expect(root.textContent).toContain('No changes.');
    expect(saveButton(root).disabled).toBe(true);
  });

  it('says so when the settings cannot be loaded and tries again', async () => {
    service['getNodeSettings'].mockRejectedValueOnce({ error: { error: 'Provider 4 not found' } });
    const root = await create();

    expect(root.textContent).toContain('Provider 4 not found');
    (root.querySelector('.retry') as HTMLButtonElement).click();
    await settle();

    expect(root.querySelector('#gpu-mode')).not.toBeNull();
  });

  it('needs a node', async () => {
    const root = await create('x');

    expect(root.textContent).toContain('needs a node');
  });

  it('warns that the worker is offline but still lets the admin save', async () => {
    service['getNodeInsights'].mockResolvedValue({
      provider_id: 4,
      worker: { online: false, supports_actions: {}, config_revision: 1 },
      gpus: [],
      models: [],
    });
    const root = await create();
    page().setPoll('9');
    fixture.detectChanges();

    expect(root.textContent).toContain('Worker offline');
    expect(saveButton(root).disabled).toBe(false);
  });

  it('lists the GPUs of the node when a selection is wanted', async () => {
    const root = await create();

    page().setMode('pick');
    page().toggleGpu(1);
    fixture.detectChanges();

    expect(root.querySelectorAll('.gpu').length).toBe(2);
    expect(root.textContent).toContain('GPU pool: all → 1');
    page().toggleGpu(1);
    fixture.detectChanges();
    expect(root.textContent).toContain('Select at least one GPU');
    expect(saveButton(root).disabled).toBe(true);
  });

  it('blocks Save with a message at the field while a value cannot be right', async () => {
    const root = await create();

    page().setPoll('0');
    page().setArgs('stray');
    fixture.detectChanges();

    expect(root.textContent).toContain('whole seconds');
    expect(root.textContent).toContain('starts with -');
    expect(saveButton(root).disabled).toBe(true);
  });

  it('saves at the revision it read, reloads and says the worker applies it', async () => {
    service['getNodeSettings'].mockResolvedValueOnce(stored({ gpu_devices: 'all' })).mockResolvedValueOnce(
      stored({ gpu_devices: 'all', gpu_poll_interval: 9 }, 3),
    );
    const root = await create();
    page().setPoll('9');
    page().setReboot('false');
    fixture.detectChanges();

    saveButton(root).click();
    await settle();

    expect(service['replaceNodeSettings']).toHaveBeenCalledWith(4, 2, {
      gpu_devices: 'all',
      auto_reboot_on_stuck_gpu: false,
      gpu_poll_interval: 9,
    });
    expect(root.textContent).toContain('Saved');
    expect(root.textContent).toContain('applies the settings now');
    expect(root.textContent).not.toContain('measured again');
    expect(page().stored()?.revision).toBe(3);
    expect(page().dirty()).toBe(false);
  });

  it('announces the new calibration when the global arguments change', async () => {
    const root = await create();
    page().setArgs('--safetensors-load-strategy=prefetch');
    fixture.detectChanges();
    expect(root.textContent).toContain('every calibration of this node is measured again');

    saveButton(root).click();
    await settle();

    expect(root.textContent).toContain('measured again in the next calibration window');
  });

  it('shows the worker refusal at the field', async () => {
    const root = await create();
    page().setPoll('9');
    fixture.detectChanges();
    service['replaceNodeSettings'].mockRejectedValue({
      status: 400,
      error: { error: 'gpu_poll_interval: nope', errors: [{ key: 'gpu_poll_interval', message: 'nope' }] },
    });

    saveButton(root).click();
    await settle();

    expect(root.textContent).toContain('Settings rejected');
    expect(root.textContent).toContain('nope');
    expect(page().fieldError('gpu_poll_interval')).toBe('nope');
  });

  it('offers a reload after a conflict', async () => {
    const root = await create();
    page().setPoll('9');
    fixture.detectChanges();
    service['replaceNodeSettings'].mockRejectedValue({ status: 409, error: { error: 'Settings changed meanwhile' } });

    saveButton(root).click();
    await settle();
    expect(root.textContent).toContain('Another admin saved');

    service['getNodeSettings'].mockResolvedValue(stored({ gpu_devices: '0' }, 5));
    (Array.from(root.querySelectorAll('button')).find((b) => b.textContent?.includes('Reload')) as HTMLButtonElement).click();
    await settle();

    expect(page().stored()?.revision).toBe(5);
    expect(page().failure()).toBeNull();
  });

  it('reports an unreachable Logos and another failure', async () => {
    const root = await create();
    page().setPoll('9');
    fixture.detectChanges();
    service['replaceNodeSettings'].mockRejectedValueOnce({ status: 503, error: { error: 'down' } });
    saveButton(root).click();
    await settle();
    expect(root.textContent).toContain('Logos not reachable');

    service['replaceNodeSettings'].mockRejectedValueOnce({ status: 500, error: { error: 'broken' } });
    saveButton(root).click();
    await settle();
    expect(root.textContent).toContain('Could not save');
  });

  it('does not save twice while it runs and resets the draft', async () => {
    const root = await create();
    page().setPoll('9');
    fixture.detectChanges();
    let finish: (value: unknown) => void = () => undefined;
    service['replaceNodeSettings'].mockReturnValue(new Promise((resolve) => (finish = resolve)));

    saveButton(root).click();
    void page().save();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(service['replaceNodeSettings']).toHaveBeenCalledTimes(1);
    finish({ provider_id: 4, settings_revision: 3 });
    await settle();

    page().setPoll('11');
    page().reset();
    expect(page().dirty()).toBe(false);
  });
});
