import { expect, it } from 'vitest';
import source from './AppShell.tsx?raw';
import { readFileSync } from 'node:fs';
const css = readFileSync(new URL('../theme/tokens.css', import.meta.url), 'utf8');
it('confines fixed-height calendar and its Haru slot to the calendar, not application details', () => {
  expect(source).toContain("const calendarWorkspaceActive = view === 'calendar' && !selectedApp;");
  expect(source).toContain('haruHostRef={setCalendarHaruHost}');
  expect(source).toContain('calendarActive={calendarWorkspaceActive}');
  expect(css).toContain('.op-app-content-calendar');
});
