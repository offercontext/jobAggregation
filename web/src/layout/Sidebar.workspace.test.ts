import { describe, expect, it } from 'vitest';
import source from './Sidebar.tsx?raw';

describe('Sidebar workspace hierarchy', () => {
  it('renders grouped task, resource, and utility navigation', () => {
    expect(source).toContain("MODULE_NAV.filter((item) => item.key !== 'settings')");
    expect(source).toContain('data-navigation-tier="utility"');
    expect(source).toContain("onChange('settings')");
    expect(source).toContain('NAVIGATION_GROUPS');
    expect(source).toContain('group.label');
  });

  it('keeps keyboard hit targets and visible focus styles in the navigation stylesheet', () => {
    expect(source).toContain("import styles from './Sidebar.module.css'");
    expect(source).toContain('styles.navItem');
    expect(source).toContain('styles.utilityButton');
  });
});
