import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { AgentMessage } from '../../shared/models/agent.model';

@Component({
  selector: 'app-session-messages',
  templateUrl: './session-messages.html',
  styleUrl: './session-messages.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SessionMessagesComponent {
  readonly source = input.required<AgentMessage[]>({ alias: 'messages' });
  readonly active = input.required<boolean>();
  readonly messages = computed(() => this.source().map(message => ({ ...message, blocks: messageBlocks(message.text) })));
}

type MessageBlock =
  | { kind: 'paragraph' | 'code'; text: string }
  | { kind: 'ul' | 'ol'; items: string[]; start: number };

/** Interpret block boundaries only. Templates interpolate all content as plain text. */
function messageBlocks(text: string): MessageBlock[] {
  const blocks: MessageBlock[] = [];
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  const fence = /^ {0,3}(`{3,}|~{3,})(.*)$/;
  const bullet = /^(?:([-+*])|(\d{1,9})[.)])\s+(.*)$/;
  let i = 0;
  while (i < lines.length) {
    const line = lines[i++];
    if (!line.trim()) continue;
    const opening = fence.exec(line);
    if (opening) {
      const code: string[] = [];
      while (i < lines.length) {
        const content = lines[i++];
        const end = fence.exec(content);
        if (end && end[1][0] === opening[1][0] && end[1].length >= opening[1].length && !end[2].trim()) break;
        code.push(content);
      }
      blocks.push({ kind: 'code', text: code.join('\n') });
      continue;
    }
    const item = bullet.exec(line);
    if (item) {
      const kind = item[1] ? 'ul' : 'ol';
      const items = [item[3]];
      while (i < lines.length) {
        const next = bullet.exec(lines[i]);
        if (next && (next[1] ? 'ul' : 'ol') === kind) {
          items.push(next[3]);
        } else if (/^\s+\S/.test(lines[i]) && !fence.test(lines[i])) {
          items[items.length - 1] += '\n' + lines[i];
        } else break;
        i++;
      }
      blocks.push({ kind, items, start: Number(item[2] ?? 1) });
      continue;
    }
    const paragraph = [line];
    while (i < lines.length && lines[i].trim() && !fence.test(lines[i]) && !bullet.test(lines[i])) {
      paragraph.push(lines[i++]);
    }
    blocks.push({ kind: 'paragraph', text: paragraph.join('\n') });
  }
  return blocks;
}
