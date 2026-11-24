import { TestBed } from '@angular/core/testing';
import { DeveloperKeyLoggingService } from '../../../services/developer-key-logging.service';
import { LoggingOnboarding } from './logging-onboarding';

describe('LoggingOnboarding', () => {
  const team = { id: 10, name: 'Research team' };
  const saveChoice = vi.fn();

  function setup() {
    TestBed.configureTestingModule({
      providers: [{ provide: DeveloperKeyLoggingService, useValue: { saveChoice } }],
    });
    const fixture = TestBed.createComponent(LoggingOnboarding);
    fixture.componentRef.setInput('team', team);
    const component = fixture.componentInstance;
    const completed = vi.fn();
    const dismissed = vi.fn();
    component.completed.subscribe(completed);
    component.dismissed.subscribe(dismissed);
    return { component, completed, dismissed };
  }

  beforeEach(() => saveChoice.mockReset());

  it('lets the user retry a failed save for the selected team', async () => {
    saveChoice.mockRejectedValueOnce(new Error('Save failed')).mockResolvedValueOnce('FULL');
    const { component, completed } = setup();

    await component.complete('FULL');
    expect(component.error()).toContain('Please try again.');
    expect(component.savingChoice()).toBeNull();
    expect(completed).not.toHaveBeenCalled();

    await component.complete('FULL');
    expect(saveChoice).toHaveBeenNthCalledWith(2, team.id, 'FULL');
    expect(component.error()).toBe('');
    expect(completed).toHaveBeenCalledWith('FULL');
  });

  it.each(['FULL', 'BILLING'] as const)('shows the saved choice (%s) before returning to the key', async (saved) => {
    saveChoice.mockResolvedValue(saved);
    const { component, completed, dismissed } = setup();

    await component.complete(saved === 'FULL' ? 'BILLING' : 'FULL');
    expect(component.earlierChoice()).toBe(saved);
    expect(completed).not.toHaveBeenCalled();

    component.close();
    expect(completed).toHaveBeenCalledWith(saved);
    expect(dismissed).not.toHaveBeenCalled();
  });

  it('does not save a choice when the user decides later', () => {
    const { component, completed, dismissed } = setup();

    component.close();
    expect(dismissed).toHaveBeenCalledOnce();
    expect(completed).not.toHaveBeenCalled();
    expect(saveChoice).not.toHaveBeenCalled();
  });
});
