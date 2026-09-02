import { describe, expect, it } from 'vitest';

import { toolMeta } from './capabilities';


describe('Pilot tool presentation metadata', () => {
  it('renders create_offer as a confirmed write action', () => {
    const metadata = toolMeta('create_offer');

    expect(metadata.label).toBe('新建 Offer');
    expect(metadata.kind).toBe('write');
  });
});
