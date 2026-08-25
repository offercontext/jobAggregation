import { describe, expect, it } from 'vitest';
import {
  readWorkspaceViewFromUrl,
  workspaceViewUrl,
} from './viewRoute';

describe('workspace view deep links', () => {
  it.each([
    ['http://localhost/?view=board', 'board'],
    ['http://localhost/?view=list', 'applications-list'],
    ['http://localhost/?view=applications-list', 'applications-list'],
    ['http://localhost/?view=offers', 'offers'],
    ['http://localhost/applications/list', 'applications-list'],
    ['http://localhost/calendar', 'calendar'],
    ['http://localhost/materials/reviews', 'reviews'],
    ['http://localhost/#/knowledge', 'knowledge'],
    ['http://localhost/#/applications/list', 'applications-list'],
    ['http://localhost/#/materials/reviews', 'reviews'],
  ])('keeps canonical and legacy entry %s', (url, expected) => {
    expect(readWorkspaceViewFromUrl(url)).toBe(expected);
  });

  it('falls back safely when an entry is unknown', () => {
    expect(readWorkspaceViewFromUrl('http://localhost/?view=not-a-view')).toBe('dashboard');
    expect(readWorkspaceViewFromUrl('http://localhost/#%E0%A4%A')).toBe('dashboard');
  });

  it('writes the canonical id without discarding unrelated query state', () => {
    expect(workspaceViewUrl('applications-list', 'http://localhost/app?source=desktop#section'))
      .toBe('/app?source=desktop&view=applications-list#section');
  });
});
