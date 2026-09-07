import { describe, expect, it } from 'vitest';
import actionDetail from '@/features/pipeline/ActionDetailDrawer.tsx?raw';
import aiSettings from '@/components/AISettingsDrawer.tsx?raw';
import applicationDetail from '@/components/ApplicationDetail.tsx?raw';
import calendarView from '@/components/CalendarView.tsx?raw';
import dashboardView from '@/features/dashboard/DashboardView.tsx?raw';
import materialKit from '@/components/MaterialKitDrawer.tsx?raw';
import offerCenter from '@/components/OfferCenterView.tsx?raw';
import offerCompare from '@/components/OfferCompareDrawer.tsx?raw';
import questionBank from '@/components/QuestionBankView.tsx?raw';
import reviewForm from '@/components/ReviewFormDrawer.tsx?raw';
import reviewManagement from '@/components/ReviewManagementView.tsx?raw';
import resumeEditor from '@/components/ResumeEditorDrawer.tsx?raw';
import resumeLibrary from '@/components/ResumeLibraryView.tsx?raw';
import settingsView from '@/components/SettingsView.tsx?raw';
import scheduleEventForm from '@/components/ScheduleEventForm.tsx?raw';
import appShell from './AppShell.tsx?raw';

const migratedWorkspaceFlows = [
  ['action detail', actionDetail],
  ['AI settings', aiSettings],
  ['application detail', applicationDetail],
  ['material kit', materialKit],
  ['offer compare', offerCompare],
  ['question generator', questionBank],
  ['review form', reviewForm],
  ['resume editor', resumeEditor],
  ['schedule event form', scheduleEventForm],
] as const;

describe('workspace drill-down layout contract', () => {
  it('keeps long-running business flows out of global right drawers', () => {
    for (const [name, source] of migratedWorkspaceFlows) {
      expect(source, `${name} should not render a global AntD Drawer`).not.toContain('<Drawer');
      expect(source, `${name} should not import Drawer from AntD`).not.toMatch(
        /import\s*\{[^}]*\bDrawer\b[^}]*\}\s*from\s*['"]antd['"]/,
      );
    }
  });

  it('opens resume editing as a replacement workspace layer with a return path', () => {
    expect(resumeLibrary).toContain('if (editing) {');
    expect(resumeLibrary).toContain('<ResumeEditorDrawer');
    expect(resumeLibrary.indexOf('if (editing) {')).toBeLessThan(resumeLibrary.indexOf('return ('));
    expect(resumeEditor).toContain('返回简历库');
  });

  it('opens question generation as a replacement workspace layer with a return path', () => {
    expect(questionBank).toContain('if (generateOpen) {');
    expect(questionBank).toContain('<GenerateDrawer');
    expect(questionBank).toContain('返回题库');
  });

  it('opens action detail as a replacement workspace layer with a return path', () => {
    expect(dashboardView).toContain('if (selectedInsight) {');
    expect(dashboardView).toContain('<ActionDetailDrawer');
    expect(actionDetail).toContain('返回工作台');
  });

  it('keeps the calendar visible with the approved local date and edit drawers', () => {
    expect(calendarView).toContain('data-calendar-workspace');
    expect(calendarView).toContain('open={narrow && drawerOpen && !formOpen}');
    expect(calendarView).toContain('open={formOpen}');
    expect(calendarView).toContain('initialDate={selectedDate}');
    expect(scheduleEventForm).toContain('返回上一层');
    expect(calendarView).not.toContain('if (selectedDate) {');
  });

  it('opens application subflows through the shared task owner and keeps record forms local', () => {
    expect(applicationDetail).toContain('<CoreTaskSurfaceHost');
    expect(applicationDetail).toContain('resolveApplicationTasks');
    expect(applicationDetail).toContain('<ScheduleEventForm');
    expect(applicationDetail).not.toContain('if (materialKitOpen && materialKitApplicationId === application.id) {');
    expect(materialKit).toContain('返回投递详情');
    expect(reviewForm).toContain('返回上一层');
  });

  it('opens review and offer compare flows as replacement workspace layers', () => {
    expect(reviewManagement).toContain('if (drawerOpen) {');
    expect(offerCenter).toContain('if (compareOpen) {');
    expect(offerCompare).toContain('返回 Offer 中心');
  });

  it('opens AI settings inside workspace content instead of the shell edge', () => {
    expect(settingsView).toContain('const [aiSettingsOpen, setAISettingsOpen] = useState(false);');
    expect(settingsView).toContain('<AISettingsDrawer open');
    expect(aiSettings).toContain('返回设置');
    expect(appShell).not.toContain('setAISettingsOpen');
  });

  it('renders application detail inside the workspace content instead of the shell edge', () => {
    expect(appShell).toContain('const workspaceContent = selectedApp ? (');
    expect(appShell).toContain('{workspaceContent}');
    expect(appShell).not.toContain('<ApplicationDetail\n        application={selectedApp}');
  });

  it('resets the viewport when entering a replacement workspace layer', () => {
    expect(appShell).toContain('window.scrollTo({ top: 0, left: 0 });');
    expect(resumeLibrary).toContain('window.scrollTo({ top: 0, left: 0 });');
  });
});
