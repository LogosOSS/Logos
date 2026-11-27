import { expect, test } from '@playwright/test';

const PRESETS = ['Day', 'Week', 'Month', 'Last 30 Days', '6 Months', 'Year'];

for (const width of [320, 375, 390]) {
  test(`Statistics time ranges fit and are usable at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/statistics?tab=requests');

    const ranges = page.getByRole('group', { name: 'Time range', exact: true });
    const timeBar = page.locator('app-time-range-bar.stats-time-range');
    const previous = timeBar.getByRole('button', { name: 'Previous period' });
    const next = timeBar.getByRole('button', { name: 'Next period' });
    await expect(ranges.getByRole('button')).toHaveText(PRESETS);
    await page.evaluate(() => document.fonts.ready);

    // A clipped shell can keep the document width unchanged while hiding the
    // last button. Measure the controls against their actual parent as well.
    const metrics = await timeBar.evaluate(bar => {
      const parent = bar.getBoundingClientRect();
      const group = bar.querySelector('[role="group"]')!.getBoundingClientRect();
      const scrollport = bar.closest('main')!;
      return {
        parentRight: parent.right,
        groupRight: group.right,
        documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        pageOverflow: scrollport.scrollWidth - scrollport.clientWidth,
        buttons: [...bar.querySelectorAll('[role="group"] button')].map(button => {
          const rect = button.getBoundingClientRect();
          return { label: button.textContent?.trim(), left: rect.left - group.left, right: group.right - rect.right };
        }),
      };
    });
    expect.soft(metrics.groupRight).toBeLessThanOrEqual(metrics.parentRight + 1);
    expect.soft(metrics.documentOverflow).toBeLessThanOrEqual(1);
    expect.soft(metrics.pageOverflow).toBeLessThanOrEqual(1);
    for (const button of metrics.buttons) {
      expect.soft(button.left, `${button.label} left inset`).toBeGreaterThanOrEqual(-1);
      expect.soft(button.right, `${button.label} right inset`).toBeGreaterThanOrEqual(-1);
    }

    for (const label of PRESETS) {
      const preset = ranges.getByRole('button', { name: label, exact: true });
      await preset.click();
      await expect(preset).toHaveClass(/pill--active/);
    }
    await expect(next).toBeDisabled();
    await previous.click();
    await expect(next).toBeEnabled();
    await next.click();
    await expect(next).toBeDisabled();
  });
}
