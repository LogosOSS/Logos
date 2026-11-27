import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { AgentSession, AgentWorkspace, AgentWorkspaceStorage } from '../../shared/models/agent.model';
import { taskPreview } from './agent-task';

@Component({
  selector: 'app-agent-workspaces',
  standalone: true,
  imports: [DecimalPipe],
  templateUrl: './agent-workspaces.html',
  styleUrl: './agent-workspaces.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AgentWorkspaces {
  readonly workspaces = input.required<AgentWorkspace[]>();
  readonly storage = input.required<AgentWorkspaceStorage | null>();
  readonly occupants = input.required<ReadonlyMap<number, AgentSession>>();
  readonly sessionSelected = output<AgentSession>();
  readonly workspaceDeleted = output<AgentWorkspace>();

  private readonly numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

  formatDisk(bytes: number): string {
    if (bytes >= 1024 ** 4) return this.numberFormat.format(bytes / 1024 ** 4) + ' TiB';
    if (bytes >= 1024 ** 3) return this.numberFormat.format(bytes / 1024 ** 3) + ' GiB';
    if (bytes >= 1024 ** 2) return this.numberFormat.format(bytes / 1024 ** 2) + ' MiB';
    if (bytes >= 1024) return this.numberFormat.format(bytes / 1024) + ' KiB';
    return this.numberFormat.format(bytes) + ' B';
  }

  filesystemAvailability = computed(() => {
    const fs = this.storage()?.artifact_filesystem;
    if (!fs || fs.total_bytes <= 0) return null;
    const freePercent = Math.min(100, Math.max(0, (100 * fs.available_bytes) / fs.total_bytes));
    return { ...fs, freePercent, level: freePercent < 5 ? 'critical' : freePercent < 15 ? 'low' : 'ok' };
  });

  readonly taskPreview = taskPreview;
}
