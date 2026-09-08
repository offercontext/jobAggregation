// @vitest-environment jsdom
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import JobDescriptionContent from './JobDescriptionContent';

function render(text: string) {
  const element = document.createElement('div');
  element.innerHTML = renderToStaticMarkup(<JobDescriptionContent text={text} />);
  return element;
}

describe('JobDescriptionContent', () => {
  it('renders explicit responsibility and requirement headings with semantic lists', () => {
    const view = render('岗位职责：\n参与 AI 应用开发；\n与产品团队协作。\n\n任职要求\n熟悉 Python/Go；\n具备问题分析能力。');
    expect([...view.querySelectorAll('h5')].map((heading) => heading.textContent)).toEqual(['岗位职责', '任职要求']);
    expect(view.querySelectorAll('ul')).toHaveLength(2);
    expect(view.querySelectorAll('li')).toHaveLength(4);
    expect(view.textContent).toContain('熟悉 Python/Go；');
  });
  it('supports explicit markdown headings and bullets without evaluating HTML', () => {
    const view = render('## 岗位职责\n- 维护 <script>alert(1)</script> 接口\n* 不改写原文\n## 任职要求\n• 熟悉 TypeScript');
    expect(view.querySelectorAll('h5')).toHaveLength(2);
    expect(view.querySelectorAll('li')).toHaveLength(3);
    expect(view.querySelector('script')).toBeNull();
    expect(view.textContent).toContain('<script>alert(1)</script>');
  });
  it('does not guess sections or split prose by punctuation', () => {
    const text = '我们提供岗位职责培训，不要求经验。\n工作地点：上海；薪资面议。\n重复内容\n重复内容';
    const view = render(text);
    expect(view.querySelector('h5')).toBeNull();
    expect(view.querySelector('li')).toBeNull();
    expect(view.textContent).toBe(text);
  });
  it('handles colons inside bold section headings without creating stray markers', () => {
    const view = render('**岗位职责：**\n维护接口\n**任职要求：熟悉 SQL**');
    expect([...view.querySelectorAll('h5')].map((heading) => heading.textContent)).toEqual(['岗位职责', '任职要求']);
    expect([...view.querySelectorAll('li')].map((item) => item.textContent)).toEqual(['维护接口', '熟悉 SQL']);
    expect(view.textContent).not.toContain('**');
  });
  it('preserves inline heading content and numbered requirement text', () => {
    const view = render('岗位职责：开发服务\r\n任职要求：\r\n1. 三年以上经验\r\n2、熟悉 SQL');
    expect(view.querySelectorAll('h5')).toHaveLength(2);
    expect(view.textContent).toContain('开发服务');
    expect(view.textContent).toContain('1. 三年以上经验');
    expect(view.textContent).toContain('2、熟悉 SQL');
  });
});
