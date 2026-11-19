import { TestBed } from '@angular/core/testing';

import { TeamManagementService } from '../../../../core/services/team-management.service';
import { TeamDetail } from '../../../../shared/models/team.model';
import { parseProviderCap, SettingsTabComponent } from './settings-tab';

function teamDetail(overrides: Partial<TeamDetail> = {}): TeamDetail {
  return {
    id: 7,
    name: 'iOS',
    is_caller_owner: true,
    team_monthly_budget_micro_cents: null,
    budget_used_micro_cents: 0,
    default_monthly_budget_micro_cents: null,
    default_cloud_rpm_limit: 5,
    default_cloud_tpm_limit: 10000,
    default_local_rpm_limit: 5,
    default_local_tpm_limit: 10000,
    priority: null,
    managed: false,
    keycloak_group: null,
    show_on_public_stats: false,
    public_category: null,
    ...overrides,
  };
}

describe('parseProviderCap', () => {
  it('reads blank as no cap', () => {
    expect(parseProviderCap('')).toBeNull();
    expect(parseProviderCap('   ')).toBeNull();
  });

  it('reads a dollar amount as micro-cents', () => {
    expect(parseProviderCap('0')).toBe(0);
    expect(parseProviderCap('12.50')).toBe(1_250_000_000);
    expect(parseProviderCap('12,5')).toBe(1_250_000_000);
  });

  it('rejects anything that is not a complete non-negative amount', () => {
    for (const input of ['abc', '12abc', '-5', '1.2.3', '$5', '9'.repeat(310)]) {
      expect(parseProviderCap(input)).toBeUndefined();
    }
  });
});

describe('SettingsTabComponent — Keycloak link', () => {
  const updateTeamKeycloakGroup = vi.fn();
  const getKeycloakGroups = vi.fn();
  const getPublicCategories = vi.fn();


  beforeEach(() => {
    vi.clearAllMocks();
    updateTeamKeycloakGroup.mockResolvedValue(undefined);
    getKeycloakGroups.mockResolvedValue({ available: false, groups: [] });
    getPublicCategories.mockResolvedValue([]);
    TestBed.configureTestingModule({
      providers: [
        {
          provide: TeamManagementService,
          useValue: {
            updateTeamKeycloakGroup,
            getKeycloakGroups,
            getPublicCategories,
            updateTeamLimits: vi.fn(),
            deleteTeam: vi.fn(),
            getTeamProviderBudgets: vi.fn().mockResolvedValue([]),
            getAllProviders: vi.fn().mockResolvedValue([]),
          },
        },
      ],
    });
  });

  function setup(team: TeamDetail, canLinkKeycloak = true): SettingsTabComponent {
    const component = TestBed.runInInjectionContext(() => new SettingsTabComponent());
    component.teamId = team.id;
    component.team = team;
    component.canEdit = true;
    component.canLinkKeycloak = canLinkKeycloak;
    // No teamId change, so this exercises the link path without the provider
    // budgets the tab loads when it first lands on a team.
    component.ngOnChanges({});
    return component;
  }

  it('shows the link the team already holds', () => {
    const component = setup(teamDetail({ managed: true, keycloak_group: 'ios-26ws' }));
    expect(component.keycloakGroup()).toBe('ios-26ws');
    expect(component.linkChanged()).toBe(false);
  });

  it('saves a typed group, trimmed', async () => {
    const component = setup(teamDetail());
    component.keycloakGroup.set('  ios-26ws ');

    await component.saveKeycloakLink();

    expect(updateTeamKeycloakGroup).toHaveBeenCalledWith(7, 'ios-26ws');
  });

  it('sends null when the field is cleared, which unlinks the team', async () => {
    const component = setup(teamDetail({ managed: true, keycloak_group: 'ios-26ws' }));
    component.keycloakGroup.set('');

    await component.saveKeycloakLink();

    expect(updateTeamKeycloakGroup).toHaveBeenCalledWith(7, null);
  });

  // An unlinked team has nothing to remove, so the button must not offer it.
  it('labels the button Save Link until a stored link is cleared', () => {
    const unlinked = setup(teamDetail());
    expect(unlinked.linkButtonLabel()).toBe('Save Link');
    unlinked.keycloakGroup.set('ios-26ws');
    expect(unlinked.linkButtonLabel()).toBe('Save Link');

    const linked = setup(teamDetail({ managed: true, keycloak_group: 'ios-26ws' }));
    expect(linked.linkButtonLabel()).toBe('Save Link');
    linked.keycloakGroup.set('');
    expect(linked.linkButtonLabel()).toBe('Remove Link');
  });

  it('does not save while the field still shows the stored link', async () => {
    const component = setup(teamDetail({ managed: true, keycloak_group: 'ios-26ws' }));

    await component.saveKeycloakLink();

    expect(updateTeamKeycloakGroup).not.toHaveBeenCalled();
  });

  // A rejected group (reserved, already linked) is the one case where the
  // server's own wording says what to fix, so it has to reach the tab.
  it('reports the rejection the server sent', async () => {
    const component = setup(teamDetail());
    updateTeamKeycloakGroup.mockRejectedValue({
      error: { detail: "Keycloak group 'ios-26ws' is already linked to team 'iOS'." },
    });
    component.keycloakGroup.set('ios-26ws');

    await component.saveKeycloakLink();

    expect(component.linkError()).toContain("already linked to team 'iOS'");
  });

  it('suggests only the groups no other team holds', async () => {
    getKeycloakGroups.mockResolvedValue({
      available: true,
      groups: [
        { name: 'ios-26ws', source: 'group', linked_team_id: null, linked_team_name: null },
        { name: 'own', source: 'group', linked_team_id: 7, linked_team_name: 'iOS' },
        { name: 'taken', source: 'role', linked_team_id: 9, linked_team_name: 'other' },
      ],
    });
    const component = setup(teamDetail());
    await Promise.resolve();

    expect(component.availableKeycloakGroups().map((g) => g.name)).toEqual(['ios-26ws', 'own']);
  });

  it('offers no suggestions when the deployment has no directory access', async () => {
    const component = setup(teamDetail());
    await Promise.resolve();

    expect(component.availableKeycloakGroups()).toEqual([]);
  });

  // A datalist has no affordance of its own, so the hint is the only thing
  // telling the admin whether the realm can be browsed at all.
  it('says how many groups can be picked when the realm is readable', async () => {
    getKeycloakGroups.mockResolvedValue({
      available: true,
      groups: [
        { name: 'ios-26ws', source: 'group', linked_team_id: null, linked_team_name: null },
        { name: 'taken', source: 'role', linked_team_id: 9, linked_team_name: 'other' },
      ],
    });
    const component = setup(teamDetail());
    await Promise.resolve();

    expect(component.groupPickerHint()).toContain('1 selectable');
  });

  it('tells the admin to type the claim name when the realm is unreadable', async () => {
    const component = setup(teamDetail());
    await Promise.resolve();

    expect(component.groupPickerHint()).toContain('login claim');
  });

  it('asks for no group directory when the caller may not link one', async () => {
    setup(teamDetail(), false);
    await Promise.resolve();

    expect(getKeycloakGroups).not.toHaveBeenCalled();
  });
});

describe('SettingsTabComponent provider budgets', () => {
  const upsertTeamProviderBudget = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    upsertTeamProviderBudget.mockResolvedValue({});
    TestBed.configureTestingModule({
      providers: [
        {
          provide: TeamManagementService,
          useValue: {
            upsertTeamProviderBudget,
            getTeamProviderBudgets: vi.fn().mockResolvedValue([]),
            getAllProviders: vi.fn().mockResolvedValue([]),
          },
        },
      ],
    });
  });

  function create(): SettingsTabComponent {
    const component = TestBed.createComponent(SettingsTabComponent).componentInstance;
    component.teamId = 7;
    component.canEdit = true;
    component.newProviderId.set('3');
    return component;
  }

  it('does not save an invalid cap as unlimited', async () => {
    const component = create();
    component.newProviderBudget.set('abc');

    await component.addProviderBudget();

    expect(upsertTeamProviderBudget).not.toHaveBeenCalled();
    expect(component.providerBudgetError()).toContain('dollar amount');
  });

  it('saves a blank cap as unlimited', async () => {
    const component = create();
    component.newProviderBudget.set('');

    await component.addProviderBudget();

    expect(upsertTeamProviderBudget).toHaveBeenCalledWith(7, 3, null);
  });
});


describe('SettingsTabComponent public stats', () => {
  const updateTeamLimits = vi.fn();
  const getPublicCategories = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    updateTeamLimits.mockResolvedValue(undefined);
    getPublicCategories.mockResolvedValue(['Research', 'Teaching']);
    TestBed.configureTestingModule({
      providers: [{
        provide: TeamManagementService,
        useValue: { updateTeamLimits, getPublicCategories },
      }],
    });
  });

  async function setup(overrides: Partial<TeamDetail> = {}) {
    const fixture = TestBed.createComponent(SettingsTabComponent);
    fixture.componentRef.setInput('team', teamDetail(overrides));
    fixture.componentRef.setInput('canEdit', true);
    // The public settings do not need the provider budget catalog.
    fixture.componentInstance.teamId = 7;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  it('lists every category without typing, plus Uncategorized', async () => {
    const fixture = await setup({ public_category: 'Teaching' });
    const select: HTMLSelectElement = fixture.nativeElement.querySelector('#s-public-category');
    expect(Array.from(select.options).map((option) => option.textContent?.trim()))
      .toEqual(['Uncategorized', 'Research', 'Teaching']);
    expect(select.value).toBe('Teaching');
  });

  it('saves public settings within their section without submitting or clearing budget drafts', async () => {
    const fixture = await setup();
    const component = fixture.componentInstance;
    component.teamBudget.set('123');
    const refreshed = vi.spyOn(component.refresh, 'emit');
    const saved = vi.spyOn(component.publicStatsSaved, 'emit');
    const checkbox: HTMLInputElement = fixture.nativeElement.querySelector('#s-public-stats');
    checkbox.click();
    const select: HTMLSelectElement = fixture.nativeElement.querySelector('#s-public-category');
    select.value = 'Research';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    const save: HTMLButtonElement = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('button'))
      .find((button) => button.textContent?.includes('Save Public Stats')) as HTMLButtonElement;
    save.click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(updateTeamLimits).toHaveBeenCalledExactlyOnceWith(7, {
      show_on_public_stats: true,
      public_category: 'Research',
    });
    expect(component.teamBudget()).toBe('123');
    expect(refreshed).not.toHaveBeenCalled();
    expect(saved).toHaveBeenCalledWith({ show_on_public_stats: true, public_category: 'Research' });
    // The parent feeds its updated model back into this tab and later tab visits.
    fixture.componentRef.setInput('team', { ...component.team, show_on_public_stats: true, public_category: 'Research' });
    fixture.detectChanges();
    expect(component.teamBudget()).toBe('123');
    expect(component.publicStatsChanged()).toBe(false);
    expect(fixture.nativeElement.textContent).toContain('Public stats settings saved.');
    expect(save.disabled).toBe(true);
  });

  it('only selects a new category after explicit creation, then persists it with the section save', async () => {
    const fixture = await setup();
    const component = fixture.componentInstance;
    const input: HTMLInputElement = fixture.nativeElement.querySelector('#s-new-public-category');
    input.value = '  Student projects  ';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    expect(component.publicCategory()).toBe('');
    expect(updateTeamLimits).not.toHaveBeenCalled();
    const create: HTMLButtonElement = fixture.nativeElement.querySelector('.category-create');
    expect(create.textContent).toContain('Create');
    expect(create.textContent).toContain('Student projects');
    create.click();
    fixture.detectChanges();
    expect(component.publicCategory()).toBe('Student projects');
    expect(component.selectablePublicCategories()).toContain('Student projects');
    await component.savePublicStats();
    expect(updateTeamLimits).toHaveBeenCalledWith(7, {
      show_on_public_stats: false, public_category: 'Student projects',
    });
  });

  it('prevents blank, duplicate and overlong category creation', async () => {
    const { componentInstance: component } = await setup();
    for (const name of [' ', ' research ', 'x'.repeat(65)]) {
      component.newPublicCategory.set(name);
      expect(component.canCreatePublicCategory()).toBe(false);
      component.createPublicCategory();
      expect(component.publicCategory()).toBe('');
    }
  });

  it('clears a category by selecting Uncategorized', async () => {
    const fixture = await setup({ public_category: 'Research' });
    const select: HTMLSelectElement = fixture.nativeElement.querySelector('#s-public-category');
    select.value = '';
    select.dispatchEvent(new Event('change'));
    await fixture.componentInstance.savePublicStats();
    expect(updateTeamLimits).toHaveBeenCalledWith(7, {
      show_on_public_stats: false, public_category: '',
    });
  });

  it('keeps failed changes available for retry and reports the server error', async () => {
    const { componentInstance: component } = await setup();
    component.showOnPublicStats.set(true);
    updateTeamLimits.mockRejectedValueOnce({ error: { detail: 'Please retry later.' } });
    await component.savePublicStats();
    expect(component.publicStatsError()).toBe('Please retry later.');
    expect(component.publicStatsSuccess()).toBe(false);
    expect(component.publicStatsChanged()).toBe(true);
    await component.savePublicStats();
    expect(component.publicStatsError()).toBe('');
    expect(component.publicStatsSuccess()).toBe(true);
  });

  it('keeps public stats drafts unsaved when unrelated team fields change', async () => {
    const fixture = await setup();
    const component = fixture.componentInstance;
    component.showOnPublicStats.set(true);
    component.publicCategory.set('Research');
    fixture.componentRef.setInput('team', { ...component.team, team_monthly_budget_micro_cents: 500_000_000 });
    fixture.detectChanges();
    expect(component.teamBudget()).toBe('5');
    expect(component.publicCategory()).toBe('Research');
    expect(component.showOnPublicStats()).toBe(true);
    expect(component.publicStatsChanged()).toBe(true);
  });

  it('does not submit twice while a public settings save is pending', async () => {
    const { componentInstance: component } = await setup();
    component.showOnPublicStats.set(true);
    let complete!: () => void;
    updateTeamLimits.mockReturnValueOnce(new Promise<void>((resolve) => { complete = resolve; }));
    const first = component.savePublicStats();
    await component.savePublicStats();
    expect(updateTeamLimits).toHaveBeenCalledTimes(1);
    expect(component.publicStatsLoading()).toBe(true);
    complete();
    await first;
    expect(component.publicStatsLoading()).toBe(false);
  });

  it('does not save public settings through the shared budget button', async () => {
    const { componentInstance: component } = await setup();
    component.showOnPublicStats.set(true);
    component.publicCategory.set('Research');
    await component.saveSettings();
    const payload = updateTeamLimits.mock.calls[0][1];
    expect(payload).not.toHaveProperty('show_on_public_stats');
    expect(payload).not.toHaveProperty('public_category');
  });

  it('reports category loading errors and allows retry before creating a category', async () => {
    getPublicCategories.mockRejectedValueOnce(new Error('Unavailable'));
    const fixture = await setup();
    const component = fixture.componentInstance;
    component.newPublicCategory.set('Teaching');
    expect(component.publicCategoriesError()).toContain('Failed to load');
    expect(component.canCreatePublicCategory()).toBe(false);
    expect(fixture.nativeElement.querySelector('#s-public-category').disabled).toBe(true);
    const retry: HTMLButtonElement = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('button'))
      .find((button) => button.textContent?.includes('Retry categories')) as HTMLButtonElement;
    retry.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(component.publicCategoriesError()).toBe('');
    expect(component.publicCategoryOptions()).toEqual(['Research', 'Teaching']);
  });

  it('does not save public settings for a read-only viewer', async () => {
    const fixture = await setup();
    const component = fixture.componentInstance;
    fixture.componentRef.setInput('canEdit', false);
    fixture.detectChanges();
    component.showOnPublicStats.set(true);
    await component.savePublicStats();
    expect(updateTeamLimits).not.toHaveBeenCalled();
    expect(fixture.nativeElement.querySelector('#s-public-stats').disabled).toBe(true);
    expect(fixture.nativeElement.querySelector('#s-new-public-category')).toBeNull();
  });
});
