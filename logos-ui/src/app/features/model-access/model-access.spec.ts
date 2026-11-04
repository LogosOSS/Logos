import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import { ModelManagementService } from '../../core/services/model-management.service';
import { TeamManagementService } from '../../core/services/team-management.service';
import { ModelAccess } from './model-access';

describe('ModelAccess', () => {
  it('loads the access of the model named in the route on start', async () => {
    const getModelAccess = vi.fn().mockResolvedValue(null);
    await TestBed.configureTestingModule({
      imports: [ModelAccess],
      providers: [
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ id: '12' }) } } },
        { provide: ModelManagementService, useValue: { getModelAccess } },
        { provide: TeamManagementService, useValue: {} },
      ],
    })
      .overrideComponent(ModelAccess, { set: { template: '' } })
      .compileComponents();

    TestBed.createComponent(ModelAccess).componentInstance.ngOnInit();
    await vi.waitFor(() => expect(getModelAccess).toHaveBeenCalledWith(12));
  });
});
