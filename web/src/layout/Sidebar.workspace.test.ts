import { describe, expect, it } from 'vitest';
import source from './Sidebar.tsx?raw';

describe('Sidebar workspace hierarchy', () => {
  it('keeps settings out of the primary business destination list', () => {
    expect(source).toContain("MODULE_NAV.filter((item) => item.key !== 'settings')");
    expect(source).toContain('data-navigation-tier="utility"');
    expect(source).toContain("onChange('settings')");
  });
});
