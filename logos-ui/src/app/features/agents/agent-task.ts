/** A single line for compact session links and cards. */
export function taskPreview(task: string): string {
  const firstLine = task.split('\n')[0].trim();
  return firstLine.length > 110 ? `${firstLine.slice(0, 110)}…` : firstLine;
}
