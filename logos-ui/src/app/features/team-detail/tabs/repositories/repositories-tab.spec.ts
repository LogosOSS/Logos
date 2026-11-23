import { TestBed } from '@angular/core/testing';

import { TeamManagementService } from '../../../../core/services/team-management.service';
import { TeamRepository } from '../../../../shared/models/team.model';
import { RepositoriesTabComponent } from './repositories-tab';

describe('RepositoriesTabComponent', () => {
  const getTeamRepositories = vi.fn();
  const createTeamRepository = vi.fn();
  const updateTeamRepository = vi.fn();
  const deleteTeamRepository = vi.fn();
  const analyzeRepositoryAgent = vi.fn();
  const storeRepositoryCredentials = vi.fn();
  const revokeRepositoryCredentials = vi.fn();

  const sample: TeamRepository = {
    id: 11,
    team_id: 7,
    repo_url: 'https://github.com/LogosOSS/Logos.git',
    repo_slug: 'LogosOSS/Logos',
    branch: 'main',
    paths: ['logos'],
    has_credentials: false,
    latest_analysis: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    getTeamRepositories.mockResolvedValue([sample]);
    createTeamRepository.mockResolvedValue(sample);
    updateTeamRepository.mockResolvedValue(sample);
    deleteTeamRepository.mockResolvedValue(undefined);
    analyzeRepositoryAgent.mockResolvedValue({ id: 2, status: 'queued' });
    storeRepositoryCredentials.mockResolvedValue({ has_credentials: true });
    revokeRepositoryCredentials.mockResolvedValue(undefined);
    TestBed.configureTestingModule({
      providers: [
        {
          provide: TeamManagementService,
          useValue: {
            getTeamRepositories,
            createTeamRepository,
            updateTeamRepository,
            deleteTeamRepository,
            analyzeRepositoryAgent,
            storeRepositoryCredentials,
            revokeRepositoryCredentials,
          },
        },
      ],
    });
  });

  function setup(): RepositoriesTabComponent {
    const component = TestBed.runInInjectionContext(() => new RepositoriesTabComponent());
    component.teamId = 7;
    component.canEdit = true;
    return component;
  }

  it('loads repositories for the team', async () => {
    const component = setup();
    await component.load();
    expect(getTeamRepositories).toHaveBeenCalledWith(7);
    expect(component.repositories()).toEqual([sample]);
  });

  it('creates a link with parsed path filters', async () => {
    const component = setup();
    component.openCreate();
    component.formUrl.set('https://github.com/ls1intum/Artemis');
    component.formBranch.set('develop');
    component.formPaths.set('src/ai, services/llm');
    await component.submitForm();
    expect(createTeamRepository).toHaveBeenCalledWith(7, {
      agent_enabled: false,
      agent_instructions: '',
      verification_command: '',
      repo_url: 'https://github.com/ls1intum/Artemis',
      branch: 'develop',
      paths: ['src/ai', 'services/llm'],
    });
    expect(component.formOpen()).toBe(false);
  });

  it('updates an existing link', async () => {
    const component = setup();
    component.openEdit(sample);
    component.formBranch.set('release');
    component.formPaths.set('');
    await component.submitForm();
    expect(updateTeamRepository).toHaveBeenCalledWith(7, 11, {
      agent_enabled: false,
      agent_instructions: '',
      verification_command: '',
      repo_url: sample.repo_url,
      branch: 'release',
      paths: [],
    });
  });

  it('clears stored path filters when the paths field is emptied', async () => {
    const component = setup();
    component.openEdit(sample);
    component.formPaths.set('   ');
    await component.submitForm();
    expect(updateTeamRepository).toHaveBeenCalledWith(
      7,
      11,
      expect.objectContaining({ paths: [] }),
    );
  });

  it('requires verification before opting in', async () => {
    const component = setup();
    component.openEdit(sample);
    component.formAgentEnabled.set(true);
    await component.submitForm();
    expect(updateTeamRepository).not.toHaveBeenCalled();
    expect(component.formError()).toContain('build and test command');
  });

  it('saves repository-specific instructions and verification', async () => {
    const component = setup();
    component.openEdit(sample);
    component.formAgentEnabled.set(true);
    component.formVerificationCommand.set('npm test && npm run build');
    component.formAgentInstructions.set('Use the project lint config.');
    await component.submitForm();
    expect(updateTeamRepository).toHaveBeenCalledWith(7, 11, expect.objectContaining({
      agent_enabled: true,
      verification_command: 'npm test && npm run build',
      agent_instructions: 'Use the project lint config.',
    }));
  });

  it('loads opt-in settings for editing and resets them for a new link', () => {
    const component = setup();
    component.openEdit({ ...sample, agent_enabled: true, verification_command: 'make check' });
    expect(component.formAgentEnabled()).toBe(true);
    expect(component.formVerificationCommand()).toBe('make check');
    component.openCreate();
    expect(component.formAgentEnabled()).toBe(false);
    expect(component.formVerificationCommand()).toBe('');
  });

  it('sends the sandbox permissions only for a Logos admin', async () => {
    const component = setup();
    component.isLogosAdmin = true;
    component.openEdit({
      ...sample,
      agent_enabled: true,
      verification_command: 'make check',
      agent_egress_hosts: ['github.com'],
      agent_may_comment: false,
    });
    expect(component.formEgressHosts()).toBe('github.com');
    component.formEgressHosts.set(' GitHub.com\n\n*.githubusercontent.com\ngithub.com ');
    component.formMayComment.set(true);
    await component.submitForm();
    expect(updateTeamRepository).toHaveBeenCalledWith(7, 11, expect.objectContaining({
      agent_egress_hosts: ['github.com', '*.githubusercontent.com'],
      agent_may_comment: true,
    }));
  });

  it('leaves the sandbox permissions out of a team owner request', async () => {
    const component = setup();
    component.openEdit({
      ...sample,
      agent_enabled: true,
      verification_command: 'make check',
      agent_egress_hosts: ['github.com'],
      agent_may_comment: true,
    });
    await component.submitForm();
    const payload = updateTeamRepository.mock.calls[0][2];
    expect(payload).not.toHaveProperty('agent_egress_hosts');
    expect(payload).not.toHaveProperty('agent_may_comment');
    component.openCreate();
    component.formUrl.set('https://github.com/ls1intum/Artemis');
    await component.submitForm();
    expect(createTeamRepository.mock.calls[0][1]).not.toHaveProperty('agent_egress_hosts');
  });

  it('resets the sandbox permissions for a new link and counts hosts', () => {
    const component = setup();
    component.isLogosAdmin = true;
    component.openEdit({ ...sample, agent_egress_hosts: ['a.example.com', 'b.example.com'], agent_may_comment: true });
    expect(component.formEgressHosts()).toBe('a.example.com\nb.example.com');
    expect(component.formMayComment()).toBe(true);
    component.openCreate();
    expect(component.formEgressHosts()).toBe('');
    expect(component.formMayComment()).toBe(false);
    expect(component.hostCount(sample)).toBe(0);
    expect(component.hostCount({ ...sample, agent_egress_hosts: ['github.com'] })).toBe(1);
  });

  it('unlinks a repository after confirm', async () => {
    const component = setup();
    component.askDelete(sample);
    await component.confirmDelete();
    expect(deleteTeamRepository).toHaveBeenCalledWith(7, 11);
    expect(component.deleteOpen()).toBe(false);
  });

  it('surfaces API detail on create failure', async () => {
    createTeamRepository.mockRejectedValue({
      error: { detail: 'repo_url must be a GitHub repository URL' },
    });
    const component = setup();
    component.openCreate();
    component.formUrl.set('https://gitlab.com/x/y');
    await component.submitForm();
    expect(component.formError()).toContain('GitHub');
    expect(component.formOpen()).toBe(true);
  });

  it('queues agent analysis', async () => {
    const component = setup();
    await component.analyzeAgent(sample);
    expect(analyzeRepositoryAgent).toHaveBeenCalledWith(7, 11);
  });

  it('stores a deploy key PEM', async () => {
    const component = setup();
    component.openCredentials(sample);
    component.credPem.set('-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----');
    await component.submitCredentials();
    expect(storeRepositoryCredentials).toHaveBeenCalledWith(7, 11, {
      private_key_pem: expect.stringContaining('BEGIN OPENSSH PRIVATE KEY'),
    });
    expect(component.credOpen()).toBe(false);
  });

  it('revokes credentials', async () => {
    const component = setup();
    await component.revokeCredentials({ ...sample, has_credentials: true });
    expect(revokeRepositoryCredentials).toHaveBeenCalledWith(7, 11);
  });

  it('formats analysis labels', () => {
    const component = setup();
    expect(component.analysisLabel(sample)).toBe('None');
    expect(
      component.analysisLabel({
        ...sample,
        latest_analysis: {
          id: 1,
          status: 'succeeded',
          source: 'heuristic',
          commit_sha: 'abcdef0123',
          finished_at: null,
        },
      }),
    ).toBe('succeeded · heuristic · abcdef0');
  });
});
