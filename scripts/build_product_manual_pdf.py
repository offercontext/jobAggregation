"""Build the screenshot handbook as a shareable, bookmarked PDF."""
from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import quote

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, Image, Table, TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'docs/product-manual/产品说明书.md'
OUTPUT = ROOT / 'output/pdf/OfferPilot-产品介绍与用户指南.pdf'
WIDTH, HEIGHT = 1080, 810
MARGIN = 54
BODY = WIDTH - 2 * MARGIN
INK = colors.HexColor('#17243c')
PURPLE = colors.HexColor('#6755dd')
MUTED = colors.HexColor('#66758a')

pdfmetrics.registerFont(TTFont('Yahei', 'C:/Windows/Fonts/msyh.ttc'))
pdfmetrics.registerFont(TTFont('YaheiBold', 'C:/Windows/Fonts/msyhbd.ttc'))
pdfmetrics.registerFontFamily('Yahei', normal='Yahei', bold='YaheiBold')
STYLES = {
    'body': ParagraphStyle('body', fontName='Yahei', fontSize=12, leading=20,
                           textColor=INK, wordWrap='CJK', spaceAfter=9),
    'h2': ParagraphStyle('h2', fontName='YaheiBold', fontSize=25, leading=34,
                         textColor=INK, wordWrap='CJK', spaceAfter=15, keepWithNext=True),
    'h3': ParagraphStyle('h3', fontName='YaheiBold', fontSize=16, leading=23,
                         textColor=PURPLE, wordWrap='CJK', spaceAfter=10, keepWithNext=True),
    'note': ParagraphStyle('note', fontName='Yahei', fontSize=11, leading=18,
                           textColor=MUTED, wordWrap='CJK', spaceAfter=12,
                           spaceBefore=12,
                           borderPadding=10, backColor=colors.HexColor('#f2f3fa')),
    'caption': ParagraphStyle('caption', fontName='Yahei', fontSize=10, leading=16,
                              textColor=MUTED, wordWrap='CJK', spaceBefore=8),
}


def markup(text: str) -> str:
    text = text.replace('–', '-').replace('—', '-').replace('\u2011', '-')
    text = html.escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    def link(match):
        label, target = match.groups()
        if not target.startswith(('#', 'https://', 'http://')):
            path, _, anchor = target.partition('#')
            relative = (SOURCE.parent / html.unescape(path)).resolve().relative_to(ROOT)
            target = 'https://github.com/offercontext/offerpilot/blob/main/' + quote(relative.as_posix())
            if anchor:
                target += '#' + quote(html.unescape(anchor))
        return f'<a href="{target}" color="#6755dd">{label}</a>'
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', link, text)
    return text


def para(text: str, style: str = 'body') -> Paragraph:
    return Paragraph(markup(text), STYLES[style])


def table(lines: list[str]) -> Table:
    data = []
    for line in lines:
        cells = [x.strip() for x in line.strip('|').split('|')]
        if all(re.fullmatch(r':?-+:?', c) for c in cells):
            continue
        data.append([para(c) for c in cells])
    count = len(data[0])
    widths = [BODY / count] * count
    if count == 2:
        widths = [BODY * .32, BODY * .68]
    result = Table(data, colWidths=widths, repeatRows=1, hAlign='LEFT')
    result.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#eceafa')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 12),
        ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ('LINEBELOW', (0, 0), (-1, -1), .5, colors.HexColor('#dde1eb')),
    ]))
    result.spaceAfter = 14
    return result


class ManualDoc(SimpleDocTemplate):
    def afterFlowable(self, flowable):
        key = getattr(flowable, 'chapter_key', None)
        if key:
            title = flowable.getPlainText()
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(title, key, level=0)
            self.notify('TOCEntry', (0, title, self.page, key))


def decoration(canvas, doc):
    if doc.page == 1:
        return
    canvas.saveState()
    canvas.setFillColor(MUTED)
    canvas.setFont('Yahei', 9)
    canvas.drawString(MARGIN, HEIGHT - 30, 'OfferPilot / 产品介绍与用户指南')
    canvas.drawRightString(WIDTH - MARGIN, HEIGHT - 30, '本地部署 · 离线阅读版')
    canvas.setStrokeColor(colors.HexColor('#e1e4ee'))
    canvas.line(MARGIN, 44, WIDTH - MARGIN, 44)
    canvas.drawString(MARGIN, 27, '虚构示例数据 · AI 结果需人工核对')
    canvas.drawRightString(WIDTH - MARGIN, 27, f'{doc.page:02d}')
    canvas.restoreState()


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = ManualDoc(str(OUTPUT), pagesize=(WIDTH, HEIGHT),
                    leftMargin=MARGIN, rightMargin=MARGIN,
                    topMargin=58, bottomMargin=58,
                    title='OfferPilot 产品介绍与用户指南',
                    author='OfferPilot', subject='项目介绍、快速开始与完整求职场景操作指南',
                    pageCompression=1)
    cover = ParagraphStyle('cover', parent=STYLES['h2'], fontSize=46, leading=62)
    story = [Spacer(1, 100), para('OFFERPILOT', 'h3'),
             Paragraph('产品介绍与用户指南', cover), Spacer(1, 20),
             para('整理求职进展，连接准备与复盘', 'h2'),
             para('从第一个岗位到 Offer 决策'), Spacer(1, 60),
             para('项目介绍 / 快速开始 / 场景指南 / 2026年9月8日', 'h3'),
             para('页面操作与 Pilot 双入口 · 面试复盘与下一轮准备 · Offer 比较与决策'),
             Spacer(1, 44),
             para('适用本地部署 0.1.0 开发快照。人物、公司、经历及薪酬均为虚构示例；版本基线与当前限制见正文。仓库维护 Markdown 与截图，本 PDF 仅用于离线分享审阅，不纳入版本控制。', 'note'),
             PageBreak(), para('阅读导航', 'h2')]
    toc = TableOfContents()
    toc.levelStyles = [ParagraphStyle('toc', parent=STYLES['body'], fontSize=15,
                                      leading=24, spaceBefore=4, leftIndent=0,
                                      firstLineIndent=0, rightIndent=24)]
    story += [para('点击章节名称或页码跳转；也可以使用 PDF 阅读器的书签面板。部分截图仅展示关键区域，原始素材保留；所有图片保持比例，可放大查看。', 'note'), toc, PageBreak(), para('开始阅读之前', 'h2')]
    lines = SOURCE.read_text(encoding='utf-8').splitlines()
    # Intro is retained, excluding the duplicated cover and Markdown navigation.
    intro = lines[1:]
    skip_navigation = False
    group = []
    pending_anchor = None
    image_count = 0
    i = 0
    while i < len(intro):
        line = intro[i].strip()
        i += 1
        if not line:
            continue
        if line.startswith('<!--'):
            continue
        if line == '### 阅读导航':
            skip_navigation = True
            continue
        if skip_navigation:
            if line.startswith('<a id='):
                skip_navigation = False
            else:
                continue
        if line.startswith('<a id='):
            pending_anchor = re.search(r'id="([^"]+)"', line).group(1)
            if group:
                # Keep a chapter's closing note with its last screenshot.
                if story and isinstance(story[-1], PageBreak):
                    story.pop()
                # Reserve room for closing prose instead of stranding it on a page.
                last_break = max((n for n, f in enumerate(story) if isinstance(f, PageBreak)), default=-1)
                tail = story[last_break + 1:] + group
                used = sum(f.wrap(BODY - 12, HEIGHT)[1] + f.getSpaceBefore() + f.getSpaceAfter() for f in tail)
                for f in tail:
                    if isinstance(f, Image) and used > HEIGHT - 140:
                        height = max(240, f.drawHeight - (used - (HEIGHT - 140)))
                        f.drawWidth = height * f.drawWidth / f.drawHeight
                        f.drawHeight = height
                        break
                story.extend(group)
                story.append(PageBreak())
                group = []
            continue
        if line.startswith('|'):
            rows = [line]
            while i < len(intro) and intro[i].strip().startswith('|'):
                rows.append(intro[i].strip())
                i += 1
            group.append(table(rows))
            continue
        match = re.fullmatch(r'!\[(.*?)\]\((.*?)\)', line)
        if match:
            caption, relative = match.groups()
            path = SOURCE.parent / relative
            image_count += 1
            screenshot = Image(str(path))
            ratio = screenshot.imageWidth / screenshot.imageHeight
            used = sum(f.wrap(BODY - 12, HEIGHT)[1] + f.getSpaceBefore() + f.getSpaceAfter() for f in group)
            remaining = HEIGHT - 116 - 24 - used - 30
            if remaining < min(300, (BODY - 12) / ratio):
                story.extend(group)
                story.append(PageBreak())
                group = []
                remaining = 560
            width = min(BODY - 12, remaining * ratio)
            screenshot.drawWidth = width
            screenshot.drawHeight = width / ratio
            screenshot.hAlign = 'CENTER'
            group += [screenshot, para(f'图 {image_count:02d} · {caption}', 'caption')]
            story.extend(group)
            story.append(PageBreak())
            group = []
            continue
        if line.startswith('## '):
            heading = para(line[3:], 'h2')
            if pending_anchor:
                heading.chapter_key = pending_anchor
                pending_anchor = None
            group.append(heading)
        elif line.startswith('### '):
            if line in ('### 指南范围与获取帮助', '### 下一步：让 AI 帮你准备这次投递'):
                story.extend(group)
                story.append(PageBreak())
                group = []
            group.append(para(line[4:], 'h3'))
        elif line.startswith('> '):
            group.append(para(line[2:], 'note'))
        else:
            if line.startswith('- '):
                line = '• ' + line[2:]
            group.append(para(line))
    story.extend(group)
    assert image_count == len(re.findall(r'^!\[', SOURCE.read_text(encoding='utf-8'), re.M)), image_count
    doc.multiBuild(story, onFirstPage=decoration, onLaterPages=decoration)
    print(f'Created {OUTPUT}; screenshots={image_count}')


if __name__ == '__main__':
    main()
