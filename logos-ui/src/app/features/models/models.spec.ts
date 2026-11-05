import { computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import { AuthService } from '../../core/auth/services/auth.service';
import { ModelManagementService } from '../../core/services/model-management.service';
import { Models } from './models';

describe('Models', () => {
  it('loads the model list on start', async () => {
    const getModels = vi.fn().mockResolvedValue([]);
    await TestBed.configureTestingModule({
      imports: [Models],
      providers: [
        { provide: HttpClient, useValue: {} },
        { provide: Router, useValue: {} },
        { provide: AuthService, useValue: { role: computed(() => 'app_developer') } },
        { provide: ModelManagementService, useValue: { getModels, getModelCapabilities: vi.fn().mockResolvedValue({}) } },
      ],
    })
      .overrideComponent(Models, { set: { template: '' } })
      .compileComponents();

    TestBed.createComponent(Models).componentInstance.ngOnInit();
    await vi.waitFor(() => expect(getModels).toHaveBeenCalled());
  });
});
