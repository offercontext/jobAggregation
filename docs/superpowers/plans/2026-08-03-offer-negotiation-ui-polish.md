# Offer Negotiation UI Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Offer 卡片、比较维度、横向对比、谈薪准备和 Pilot 入口收口为已确认的 A 方案“结构化准备工作台”，同时保持现有 API、状态机、幂等和 HITL 语义不变。

**Architecture:** 以 `main@14ec28b` 上已经完成的 Offer 谈薪功能和实现基线 `10fc4ba` 为基础，只调整 React 展示层。`OfferNegotiationDrawer` 继续负责请求和状态协调，新增聚焦的展示组件并复用 `SourceStateTag`、`ConfirmationPanel` 与 Ant Design；UI 与 Pilot 继续由 AppShell 按入口隔离草稿。

**Tech Stack:** React 18、TypeScript、Ant Design 5、CSS Modules、Vitest、React DOM test utilities、Vite。

---

## 0. 开发边界与文件结构

实施开始前必须确认：

```powershell
git status --short --branch
git rev-parse --short HEAD
```

预期：分支为 `feat/20260801-offer-negotiation`，HEAD 为 `10fc4ba`，工作区干净。如果 HEAD 已前移，先核对新增提交，不覆盖用户改动。

本计划预计修改或创建以下文件：

| 文件 | 职责 |
| --- | --- |
| `web/src/components/OfferCard.tsx` | Offer 卡片内容和操作层级 |
| `web/src/components/OfferCard.module.css` | Offer 卡片视觉样式 |
| `web/src/components/OfferCard.test.tsx` | Offer 卡片交互回归 |
| `web/src/components/OfferComparisonDimensionPanel.tsx` | 比较维度管理交互 |
| `web/src/components/OfferComparisonDimensionPanel.module.css` | 维度管理布局和状态 |
| `web/src/components/OfferComparisonDimensionPanel.test.tsx` | 创建、选择、保存、清除、归档测试 |
| `web/src/components/OfferCompareDrawer.tsx` | 横向比较展示 |
| `web/src/components/OfferCompareDrawer.module.css` | 比较页表头和操作布局 |
| `web/src/components/OfferCompareDrawer.test.tsx` | 比较事实和入口测试 |
| `web/src/components/offer-negotiation/OfferSnapshotSummary.tsx` | 当前或冻结 Offer 摘要 |
| `web/src/components/offer-negotiation/NegotiationBriefForm.tsx` | 目标、顾虑和场景表单 |
| `web/src/components/offer-negotiation/NegotiationProposalCard.tsx` | 单条 Proposal 审阅 |
| `web/src/components/offer-negotiation/NegotiationHistoryList.tsx` | 历史摘要和只读选择 |
| `web/src/components/offer-negotiation/OfferNegotiationPresentation.module.css` | 谈薪展示组件共享样式 |
| `web/src/components/offer-negotiation/OfferNegotiationPresentation.test.tsx` | 展示组件测试 |
| `web/src/components/OfferNegotiationDrawer.tsx` | 状态协调和产品内确认 |
| `web/src/components/OfferNegotiationDrawer.module.css` | 工作台页面布局 |
| `web/src/components/OfferNegotiationDrawer.test.tsx` | 状态机、确认和错误矩阵 |
| `web/src/components/ChatPanel/PilotOfferSelectionCard.tsx` | Pilot Offer 询问与回答确认 |
| `web/src/components/ChatPanel/PilotOfferSelectionCard.test.tsx` | Pilot 主动选择测试 |
| `web/src/components/ChatPanel/ContextPanel.tsx` | 接入 Pilot 选择卡 |
| `web/src/components/ChatPanel/ChatPanel.module.css` | Pilot 卡片现有主题样式 |
| `web/src/components/OfferPilotNegotiation.test.tsx` | Pilot 挂载与入口回归 |
| `web/src/components/OfferCenterView.tsx` | Offer 中心样式容器和入口 |
| `web/src/components/OfferCenterView.test.tsx` | UI 入口与零额外写入 |
| `web/src/layout/AppShell.offerNegotiation.test.tsx` | UI/Pilot 草稿隔离挂载测试 |
| `artifacts/2026-08-03-offer-negotiation/*.png` | 重新生成的亮色宽屏截图 |
| `docs/reports/2026-08-01-offer-negotiation-release-verification.md` | 历史验收报告（路径已收敛） |

禁止修改 `src/offerpilot/`、数据库迁移、后端 schema、API 路由、Provider prompt、AI 校验器和共享 HTTP 契约。

### Task 1: 收口 Offer 卡片层级

**Files:**
- Create: `web/src/components/OfferCard.test.tsx`
- Create: `web/src/components/OfferCard.module.css`
- Modify: `web/src/components/OfferCard.tsx`

- [ ] **Step 1: 写 Offer 卡片失败测试**

创建测试，使用真实 Ant Design 挂载并验证主次操作、状态、拖拽绑定和 Offer 上下文。核心测试必须包含：

```tsx
it('renders preparation as the primary action without replacing the coach', () => {
  const onNegotiation = vi.fn();
  const onCoach = vi.fn();
  act(() => root.render(
    <OfferCard
      offer={offer}
      selected={false}
      onToggleSelect={vi.fn()}
      onCoach={onCoach}
      onNegotiation={onNegotiation}
      onView={vi.fn()}
    />,
  ));

  const prepare = host.querySelector<HTMLButtonElement>('[data-action="start-negotiation"]');
  const coach = host.querySelector<HTMLButtonElement>('[data-action="open-negotiation-coach"]');
  expect(prepare?.textContent).toContain('开始谈薪准备');
  expect(prepare?.className).toContain('ant-btn-primary');
  expect(coach?.textContent).toContain('谈薪教练');

  act(() => prepare?.click());
  expect(onNegotiation).toHaveBeenCalledWith(offer);
  expect(onCoach).not.toHaveBeenCalled();
});
```

测试 fixture 使用中文公司“星云数据”和岗位“后端工程师”，并断言状态、固定月薪、年度现金事实、截止日期和详情按钮仍存在。

- [ ] **Step 2: 运行测试并确认先失败**

Run:

```powershell
cd web
npm.cmd test -- OfferCard.test.tsx
```

Expected: FAIL，因为现有卡片没有新 `data-action`、主操作层级和 CSS Module。

- [ ] **Step 3: 实现卡片结构和样式**

在 `OfferCard.tsx` 中保留所有回调，改用语义结构和 CSS Module。操作区固定为：

```tsx
<div className={styles.actions}>
  <Button
    type="primary"
    data-action="start-negotiation"
    onClick={() => onNegotiation?.(offer)}
  >
    开始谈薪准备
  </Button>
  <Button
    data-action="open-negotiation-coach"
    icon={<MessageOutlined />}
    onClick={() => onCoach(offer)}
  >
    谈薪教练
  </Button>
  <Button data-action="view-offer" icon={<EyeOutlined />} onClick={() => onView(offer)}>
    详情
  </Button>
</div>
```

`OfferCard.module.css` 至少定义：

```css
.card { height: 100%; border-radius: 14px; box-shadow: 0 4px 16px color-mix(in srgb, var(--op-ink) 6%, transparent); }
.heading { display: grid; gap: 4px; min-width: 0; }
.salary { margin-top: 12px; color: var(--op-ink); font-size: 24px; font-weight: 720; font-variant-numeric: tabular-nums; }
.facts { display: grid; gap: 5px; margin-top: 8px; color: var(--op-muted-strong); font-size: 12px; line-height: 1.55; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
.actions :global(.ant-btn) { min-height: 40px; }
```

禁止改变 Offer 状态颜色、选择 checkbox、Pilot 拖拽绑定和详情逻辑。

- [ ] **Step 4: 运行卡片测试**

Run: `npm.cmd test -- OfferCard.test.tsx OfferCenterView.test.tsx`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add web/src/components/OfferCard.tsx web/src/components/OfferCard.module.css web/src/components/OfferCard.test.tsx
```

```powershell
git commit -m "style: AI polish offer card actions"
```

### Task 2: 重做比较维度管理控件

**Files:**
- Modify: `web/src/components/OfferComparisonDimensionPanel.tsx`
- Create: `web/src/components/OfferComparisonDimensionPanel.module.css`
- Modify: `web/src/components/OfferComparisonDimensionPanel.test.tsx`

- [ ] **Step 1: 增加失败测试**

在现有测试中增加真实视觉结构和语义测试：

```tsx
it('renders each active dimension as a structured settings group', async () => {
  const rendered = render();
  await act(async () => {});

  const card = rendered.querySelector('[data-testid="comparison-dimension-card"]');
  expect(card).not.toBeNull();
  expect(card?.textContent).toContain('通勤');
  expect(card?.textContent).toContain('星云数据');
  expect(card?.textContent).toContain('远山科技');
  expect(rendered.querySelector('[data-action="create-dimension"]')?.textContent).toContain('新增维度');
  expect(rendered.querySelectorAll('[data-action="save-value"]')).toHaveLength(2);
  expect(rendered.querySelectorAll('[data-action="clear-value"]')).toHaveLength(2);
});
```

再增加：选中 8 项后出现“已选择 8/8”，第 9 项不可选；归档维度显示只读值；空值仍显示“尚未填写”。

- [ ] **Step 2: 运行测试并确认失败**

Run: `npm.cmd test -- OfferComparisonDimensionPanel.test.tsx`

Expected: FAIL，因为当前是原生输入和连续行布局。

- [ ] **Step 3: 使用 Ant Design 和 CSS Module 实现**

将原生控件替换为 `Input`、`Button`、`Checkbox`、`Tag`、`Card` 和 `Tooltip`。每个维度使用以下结构：

```tsx
<Card className={styles.dimensionCard} data-testid="comparison-dimension-card" size="small">
  <div className={styles.dimensionHeader}>
    <Checkbox checked={selected} disabled={selectionBlocked} onChange={handleSelect}>
      {dimension.label}
    </Checkbox>
    {active ? <Tag>可用于比较</Tag> : <Tag>已归档，仅历史可读</Tag>}
  </div>
  <div className={styles.offerValues}>
    {offers.map((offer) => (
      <div className={styles.offerValue} key={offer.id}>
        <label htmlFor={`dimension-${dimension.id}-offer-${offer.id}`}>{offer.company_name}</label>
        <Input id={`dimension-${dimension.id}-offer-${offer.id}`} value={draftValues[key] ?? ''} />
        <div className={styles.valueActions}>{/* 保存与清除 */}</div>
      </div>
    ))}
  </div>
</Card>
```

增加 loading、请求错误和空维度状态。保存、清除、归档和选择逻辑必须继续调用现有 service，禁止自动写入。

- [ ] **Step 4: 运行维度相关测试**

Run: `npm.cmd test -- OfferComparisonDimensionPanel.test.tsx OfferCenterView.test.tsx`

Expected: PASS，原有创建、保存、清除和归档测试继续通过。

- [ ] **Step 5: 提交**

```powershell
git add web/src/components/OfferComparisonDimensionPanel.tsx web/src/components/OfferComparisonDimensionPanel.module.css web/src/components/OfferComparisonDimensionPanel.test.tsx
```

```powershell
git commit -m "style: AI polish offer comparison dimensions"
```

### Task 3: 收口 Offer 横向对比

**Files:**
- Modify: `web/src/components/OfferCompareDrawer.tsx`
- Create: `web/src/components/OfferCompareDrawer.module.css`
- Modify: `web/src/components/OfferCompareDrawer.test.tsx`

- [ ] **Step 1: 增加失败测试**

在现有比较测试中增加：

```tsx
expect(host.querySelector('[data-testid="offer-comparison-header-2"]')?.textContent)
  .toContain('Company 2');
expect(host.querySelector('[data-testid="offer-comparison-header-2"]')?.textContent)
  .toContain('Engineer');
expect(host.querySelector('[data-section="fixed-facts"]')).not.toBeNull();
expect(host.querySelector('[data-section="custom-dimensions"]')).not.toBeNull();
expect(host.querySelector('[data-missing="true"]')?.textContent).toContain('尚未填写');
```

继续断言页面没有“评分”“排名”“最佳 Offer”“建议接受”和“建议拒绝”。

- [ ] **Step 2: 运行测试并确认失败**

Run: `npm.cmd test -- OfferCompareDrawer.test.tsx`

Expected: FAIL，因为现有表头只显示公司名，也未区分固定事实与自定义维度。

- [ ] **Step 3: 实现结构化表头和分组行**

为每个 Offer 构造表头：

```tsx
title: (
  <div className={styles.offerHeader} data-testid={`offer-comparison-header-${offer.id}`}>
    <strong>{offer.company_name}</strong>
    <span>{offer.position_name}</span>
    <b>{offer.base_monthly / 1000}K × {offer.months_per_year}</b>
  </div>
)
```

固定事实和自定义维度分别增加不可排序的分组行。缺失值渲染：

```tsx
<span className={styles.missing} data-missing="true">尚未填写</span>
```

底部操作按钮保留明确 `offer.id`，每份 Offer 只显示一个“开始谈薪准备”。保留第一列 fixed、横向滚动、用户选择顺序和现有读取接口。

- [ ] **Step 4: 运行比较测试**

Run: `npm.cmd test -- OfferCompareDrawer.test.tsx OfferCenterView.test.tsx`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add web/src/components/OfferCompareDrawer.tsx web/src/components/OfferCompareDrawer.module.css web/src/components/OfferCompareDrawer.test.tsx
```

```powershell
git commit -m "style: AI polish offer comparison table"
```

### Task 4: 创建谈薪来源摘要与沟通表单

**Files:**
- Create: `web/src/components/offer-negotiation/OfferSnapshotSummary.tsx`
- Create: `web/src/components/offer-negotiation/NegotiationBriefForm.tsx`
- Create: `web/src/components/offer-negotiation/OfferNegotiationPresentation.module.css`
- Create: `web/src/components/offer-negotiation/OfferNegotiationPresentation.test.tsx`

- [ ] **Step 1: 写展示组件失败测试**

测试使用 `OfferNegotiationSnapshot['offer_snapshot']`，覆盖三项摘要、完整来源折叠、中文来源标签和受控表单：

```tsx
it('shows a compact frozen summary and keeps full facts available', () => {
  act(() => root.render(
    <OfferSnapshotSummary
      offer={snapshot.offer_snapshot}
      brief={snapshot.user_brief}
      sourceState="frozen"
    />,
  ));
  expect(host.textContent).toContain('后端工程师');
  expect(host.textContent).toContain('28K × 12');
  expect(host.textContent).toContain('已冻结来源');
  expect(host.textContent).toContain('查看完整来源');
  expect(host.textContent).not.toContain('/offer_snapshot/base_monthly');
});

it('reports field validation next to the controlled field', () => {
  act(() => root.render(
    <NegotiationBriefForm
      value={{ goal: '', concerns: '远程办公安排', scenario: '电话沟通' }}
      disabled={false}
      errors={{ goal: '请填写本次沟通目标' }}
      onChange={vi.fn()}
    />,
  ));
  expect(host.querySelector('[role="alert"]')?.textContent).toBe('请填写本次沟通目标');
  expect(host.querySelector('label[for="negotiation-goal"]')).not.toBeNull();
});
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `npm.cmd test -- offer-negotiation/OfferNegotiationPresentation.test.tsx`

Expected: FAIL，因为组件尚不存在。

- [ ] **Step 3: 实现 `OfferSnapshotSummary`**

组件接口固定为：

```tsx
interface OfferSnapshotSummaryProps {
  offer: OfferNegotiationSnapshot['offer_snapshot'];
  brief?: OfferNegotiationSnapshot['user_brief'];
  sourceState: 'current' | 'frozen' | 'changed';
}
```

使用 `SourceStateTag`、三个摘要项和 Ant Design `Collapse`。`sourceState=changed` 时只显示来源变化，不重新拼入当前 Offer。所有数值使用原始事实格式化，不计算排名或评分。

- [ ] **Step 4: 实现 `NegotiationBriefForm`**

受控接口固定为：

```tsx
interface NegotiationBriefValue { goal: string; concerns: string; scenario: string }
interface NegotiationBriefFormProps {
  value: NegotiationBriefValue;
  disabled: boolean;
  errors: Partial<Record<keyof NegotiationBriefValue, string>>;
  onChange: (next: NegotiationBriefValue) => void;
}
```

使用 Ant Design `Input` 和 `Input.TextArea`。标签始终显示；错误在字段下方；disabled 状态不能删除用户原文。

- [ ] **Step 5: 运行测试**

Run: `npm.cmd test -- offer-negotiation/OfferNegotiationPresentation.test.tsx`

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add web/src/components/offer-negotiation
```

```powershell
git commit -m "style: AI add negotiation source and brief views"
```

### Task 5: 创建 Proposal 审阅卡与历史列表

**Files:**
- Create: `web/src/components/offer-negotiation/NegotiationProposalCard.tsx`
- Create: `web/src/components/offer-negotiation/NegotiationHistoryList.tsx`
- Modify: `web/src/components/offer-negotiation/OfferNegotiationPresentation.module.css`
- Modify: `web/src/components/offer-negotiation/OfferNegotiationPresentation.test.tsx`

- [ ] **Step 1: 增加失败测试**

```tsx
it('reveals raw evidence only after the user expands it', () => {
  act(() => root.render(
    <NegotiationProposalCard
      block={block}
      selected={false}
      editedText={block.text}
      disabled={false}
      onToggle={vi.fn()}
      onEdit={vi.fn()}
    />,
  ));
  expect(host.textContent).toContain('Offer 固定月薪');
  expect(host.textContent).not.toContain('/offer_snapshot/base_monthly');
  act(() => host.querySelector<HTMLButtonElement>('[data-action="toggle-evidence"]')?.click());
  expect(host.textContent).toContain('/offer_snapshot/base_monthly');
  expect(host.textContent).toContain('28000');
});

it('shows real confirmation time but never invents a generation time', () => {
  act(() => root.render(
    <NegotiationHistoryList
      items={[proposalWithBrief]}
      selectedId={null}
      onSelect={vi.fn()}
    />,
  ));
  expect(host.textContent).toContain('记录 #12');
  expect(host.textContent).toContain('已确认');
  expect(host.textContent).toContain('2026');
});
```

增加证据标签映射测试：`/offer_snapshot/base_monthly` 显示“Offer 固定月薪”，`/user_brief/goal` 显示“用户沟通目标”；未知但已通过后端校验的路径显示安全中文“已验证来源”，原路径只在展开区域显示。

- [ ] **Step 2: 运行测试并确认失败**

Run: `npm.cmd test -- offer-negotiation/OfferNegotiationPresentation.test.tsx`

Expected: FAIL，因为两个组件尚不存在。

- [ ] **Step 3: 实现 Proposal 审阅卡**

接口固定为：

```tsx
interface NegotiationProposalCardProps {
  block: OfferNegotiationBlock;
  selected: boolean;
  editedText: string;
  disabled: boolean;
  onToggle: () => void;
  onEdit: (text: string) => void;
}
```

选中后才展示编辑框。`block.rationale` 标题为“为什么建议”。证据摘要使用中文标签和摘录；原始 path 放入折叠区。禁止修改 `block`、证据引用或选择 ID。

- [ ] **Step 4: 实现历史列表**

历史项只读取 Proposal 冻结快照：

```tsx
const title = `记录 #${item.id}`;
const confirmedAt = item.brief?.confirmed_at
  ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(item.brief.confirmed_at))
  : null;
```

展示公司、岗位、Proposal 状态、是否已确认和来源是否变化。未确认 Proposal 没有 `confirmed_at` 时不显示任何推测时间。

- [ ] **Step 5: 运行展示组件测试**

Run: `npm.cmd test -- offer-negotiation/OfferNegotiationPresentation.test.tsx`

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add web/src/components/offer-negotiation
```

```powershell
git commit -m "style: AI add negotiation proposal review cards"
```

### Task 6: 将谈薪 Drawer 改造成结构化工作台

**Files:**
- Modify: `web/src/components/OfferNegotiationDrawer.tsx`
- Create: `web/src/components/OfferNegotiationDrawer.module.css`
- Modify: `web/src/components/OfferNegotiationDrawer.test.tsx`

- [ ] **Step 1: 写产品内确认失败测试**

增加测试，禁止使用浏览器原生确认，并验证 preview 和生成仍是两个明确步骤：

```tsx
it('uses the product confirmation panel without changing request order', async () => {
  const nativeConfirm = vi.spyOn(window, 'confirm');
  service.preview.mockResolvedValue(preview());
  service.create.mockResolvedValue(proposal());
  renderDrawer();
  fillBrief(host, { goal: '确认薪资结构', concerns: '远程安排', scenario: '电话沟通' });

  await act(async () => host.querySelector<HTMLButtonElement>('[data-testid="offer-negotiation-generate"]')?.click());
  expect(service.preview).toHaveBeenCalledTimes(1);
  expect(service.create).not.toHaveBeenCalled();
  expect(host.querySelector('[aria-label="确认本次 AI 输入"]')).not.toBeNull();

  await act(async () => host.querySelector<HTMLButtonElement>('[data-action="confirm-generate"]')?.click());
  expect(service.create).toHaveBeenCalledTimes(1);
  expect(nativeConfirm).not.toHaveBeenCalled();
});
```

再增加保存确认测试：选中建议后先显示 `ConfirmationPanel`，只有点击 `data-action="confirm-save"` 才调用 `confirmOfferNegotiationProposal`。

- [ ] **Step 2: 增加状态矩阵测试**

覆盖以下现有语义：

- `202 generating`：保留原 key、冻结输入、显示“使用原尝试重试”。
- `offer_negotiation_provider_error`：保留 key，同 key 重试。
- `offer_negotiation_unverifiable`：清理 key，必须新 Attempt。
- `safe_empty`：显示正常空状态，不显示确认保存按钮。
- `source_changed`：历史只读，禁止保存。
- 确认结果未知：保留 `confirmationKey`、选择和编辑内容。

- [ ] **Step 3: 运行测试并确认失败**

Run: `npm.cmd test -- OfferNegotiationDrawer.test.tsx`

Expected: FAIL，因为当前仍使用原生 HTML 与 `window.confirm`。

- [ ] **Step 4: 拆分 preview 与生成动作**

将当前 `generate()` 拆为两个职责明确的函数：

```tsx
const previewGeneration = async () => {
  if (frozen || !briefValid) return;
  const preview = await previewOfferNegotiation(offer.id, requestWithoutKey);
  setFrozenPreview(preview);
  setPreviewInputKey(currentInputKey);
};

const submitGeneration = async (fromRetry = false) => {
  if (!frozenPreview || (!fromRetry && frozen)) return;
  const result = await createOfferNegotiationProposal(
    offer.id,
    { ...requestWithoutKey, idempotency_key: attemptKey, source_fingerprint: frozenPreview.source_fingerprint },
    entrypoint,
  );
  applyGenerationResult(result);
};
```

`retry()` 继续根据 `pendingOperation` 调用原生成或确认 endpoint，不生成新 key。

- [ ] **Step 5: 接入展示组件和产品内确认**

页面结构固定为：

```tsx
<section className={styles.workspace} data-testid="offer-negotiation-drawer">
  <header className={styles.header}>{/* 标题、边界说明、关闭 */}</header>
  <OfferSnapshotSummary offer={snapshotOffer} brief={visibleBrief} sourceState={sourceState} />
  {!proposal && <NegotiationBriefForm ... />}
  {activePreview && !proposal && (
    <ConfirmationPanel title="确认本次 AI 输入" description="核对后才会发送给 AI" sources={[{ state: 'frozen', detail: '本次 Offer 与用户输入快照' }]}>
      <Button onClick={clearPreview}>返回修改</Button>
      <Button type="primary" data-action="confirm-generate" onClick={() => void submitGeneration()}>确认生成</Button>
    </ConfirmationPanel>
  )}
  {/* Proposal 分区、保存确认和历史 */}
</section>
```

保存前产品内确认仍是第二次人工确认。状态文字继续使用安全中文错误映射，不透传 Error message。

- [ ] **Step 6: 运行 Drawer 与入口测试**

Run:

```powershell
npm.cmd test -- OfferNegotiationDrawer.test.tsx OfferCenterView.test.tsx OfferPilotNegotiation.test.tsx offer-negotiation/OfferNegotiationPresentation.test.tsx
```

Expected: PASS。

- [ ] **Step 7: 提交**

```powershell
git add web/src/components/OfferNegotiationDrawer.tsx web/src/components/OfferNegotiationDrawer.module.css web/src/components/OfferNegotiationDrawer.test.tsx
```

```powershell
git commit -m "style: AI build structured negotiation workspace"
```

### Task 7: 收口 Pilot Offer 询问与回答卡

**Files:**
- Create: `web/src/components/ChatPanel/PilotOfferSelectionCard.tsx`
- Create: `web/src/components/ChatPanel/PilotOfferSelectionCard.test.tsx`
- Modify: `web/src/components/ChatPanel/ContextPanel.tsx`
- Modify: `web/src/components/ChatPanel/ChatPanel.module.css`
- Modify: `web/src/components/OfferPilotNegotiation.test.tsx`

- [ ] **Step 1: 写 Pilot 主动选择失败测试**

```tsx
it('asks for an explicit Offer and confirms the user answer before continuing', () => {
  const onContinue = vi.fn();
  act(() => root.render(
    <PilotOfferSelectionCard offers={[offer1, offer2]} onContinue={onContinue} onCancel={vi.fn()} />,
  ));
  expect(host.textContent).toContain('选择要准备谈薪的 Offer');
  expect(onContinue).not.toHaveBeenCalled();

  act(() => host.querySelector<HTMLInputElement>('[value="2"]')?.click());
  expect(host.textContent).toContain('已选择');
  expect(host.textContent).toContain(offer2.company_name);
  expect(onContinue).not.toHaveBeenCalled();

  act(() => host.querySelector<HTMLButtonElement>('[data-action="continue-offer-negotiation"]')?.click());
  expect(onContinue).toHaveBeenCalledWith(offer2);
});
```

增加取消、切换 Offer、没有 Offer 时空状态和 disabled 状态测试。

- [ ] **Step 2: 运行测试并确认失败**

Run: `npm.cmd test -- ChatPanel/PilotOfferSelectionCard.test.tsx OfferPilotNegotiation.test.tsx`

Expected: FAIL，因为当前使用原生 select，没有独立询问和回答确认卡。

- [ ] **Step 3: 实现 Pilot 选择卡**

组件接口固定为：

```tsx
interface PilotOfferSelectionCardProps {
  offers: Offer[];
  disabled?: boolean;
  onContinue: (offer: Offer) => void;
  onCancel: () => void;
}
```

使用 Ant Design `Radio.Group`、`Button` 和 Offer 摘要。用户选择只更新本地组件状态；只有点击“继续准备”才调用 `onContinue`。不得调用 API、创建 Chat message 或自动打开 AI。

- [ ] **Step 4: 在 `ContextPanel` 接入**

保留现有主动触发按钮 `pilot-choose-offer-negotiation`。点击后渲染 `PilotOfferSelectionCard`。选择完成后将 Offer 保存为当前本地选择，显示回答确认卡和“准备谈薪”按钮；最终仍调用现有 `onPrepareOfferNegotiation(activeOffer)`。

不得改变 `isNego`、绑定 Offer、capabilities、evidence 和 auto approve 的现有行为。

- [ ] **Step 5: 运行 Pilot 相关测试**

Run:

```powershell
npm.cmd test -- ChatPanel/PilotOfferSelectionCard.test.tsx OfferPilotNegotiation.test.tsx ChatPanel
```

Expected: PASS。

- [ ] **Step 6: 提交**

```powershell
git add web/src/components/ChatPanel/PilotOfferSelectionCard.tsx web/src/components/ChatPanel/PilotOfferSelectionCard.test.tsx web/src/components/ChatPanel/ContextPanel.tsx web/src/components/ChatPanel/ChatPanel.module.css web/src/components/OfferPilotNegotiation.test.tsx
```

```powershell
git commit -m "style: AI polish Pilot offer negotiation selection"
```

### Task 8: 补齐 AppShell、可访问性和视觉状态回归

**Files:**
- Modify: `web/src/components/OfferCenterView.tsx`
- Modify: `web/src/components/OfferCenterView.test.tsx`
- Create: `web/src/layout/AppShell.offerNegotiation.test.tsx`
- Modify: 前述本轮 CSS Modules 和组件测试

- [ ] **Step 1: 写 AppShell 草稿隔离失败测试**

创建实际挂载测试，使用两个同 ID Offer 入口状态，验证 UI 和 Pilot 草稿不能互相覆盖：

```tsx
it('keeps UI and Pilot negotiation drafts isolated for the same Offer', async () => {
  renderAppShellWithOffer(offer);
  await openUiNegotiation(offer.id);
  await fillNegotiationBrief({ goal: 'UI 目标', concerns: 'UI 顾虑', scenario: 'UI 场景' });
  await closeNegotiation();

  await openPilotNegotiation(offer.id);
  expect(screen.queryByDisplayValue('UI 目标')).toBeNull();
  await fillNegotiationBrief({ goal: 'Pilot 目标', concerns: 'Pilot 顾虑', scenario: 'Pilot 场景' });
  await closeNegotiation();

  await openUiNegotiation(offer.id);
  expect(screen.getByDisplayValue('UI 目标')).not.toBeNull();
  expect(screen.queryByDisplayValue('Pilot 目标')).toBeNull();
});
```

测试 helper 必须通过真实 AppShell 回调打开入口，不直接调用内部 state setter。

- [ ] **Step 2: 增加零额外写入和可访问性测试**

覆盖：

- Offer 中心、摘要、历史和证据展开只触发读取 service。
- 只有保存维度、清除值、生成 Proposal 和确认 Brief 才产生对应写请求。
- 所有 input 有 label。
- 主按钮、次按钮和折叠触发器可通过键盘聚焦。
- `role=status` 与 `role=alert` 分别用于等待和错误。
- 暗色主题下来源标签和选中卡仍使用语义 class，不写死白色背景。

- [ ] **Step 3: 运行前端定向套件并修复回归**

Run:

```powershell
cd web
npm.cmd test -- OfferCard.test.tsx OfferComparisonDimensionPanel.test.tsx OfferCompareDrawer.test.tsx OfferNegotiationDrawer.test.tsx OfferCenterView.test.tsx OfferPilotNegotiation.test.tsx ChatPanel/PilotOfferSelectionCard.test.tsx offer-negotiation/OfferNegotiationPresentation.test.tsx src/layout/AppShell.offerNegotiation.test.tsx
```

Expected: 所有测试 PASS。

- [ ] **Step 4: 检查前端可见文案**

逐项确认界面不存在以下遗留表达：

```text
查看 1
查看 2
pending
/offer_snapshot/
/user_brief/
```

原始证据路径只允许在用户主动展开的详情区出现；`pending` 必须映射为中文 Offer 状态。

- [ ] **Step 5: 提交**

```powershell
git add web/src/components/OfferCenterView.tsx web/src/components/OfferCenterView.test.tsx web/src/layout/AppShell.offerNegotiation.test.tsx web/src/components
```

```powershell
git commit -m "test: AI cover polished negotiation workflows"
```

### Task 9: 完整验证、真实浏览器走查与截图替换

**Files:**
- Replace: `artifacts/2026-08-03-offer-negotiation/01-ui-offer-center-light.png`
- Replace: `artifacts/2026-08-03-offer-negotiation/02-ui-offer-comparison.png`
- Replace: `artifacts/2026-08-03-offer-negotiation/03-ui-source-confirmation-frozen.png`
- Replace: `artifacts/2026-08-03-offer-negotiation/04-ui-generated-edited-proposal.png`
- Replace: `artifacts/2026-08-03-offer-negotiation/05-ui-confirmed-history.png`
- Replace: `artifacts/2026-08-03-offer-negotiation/06-pilot-question-select-offer.png`
- Replace: `artifacts/2026-08-03-offer-negotiation/07-pilot-answer-selected-offer.png`
- Replace: `artifacts/2026-08-03-offer-negotiation/08-pilot-source-confirmation-frozen.png`
- Replace: `artifacts/2026-08-03-offer-negotiation/09-pilot-generated-proposal.png`
- Replace: `artifacts/2026-08-03-offer-negotiation/10-pilot-confirmed-history.png`
- Replace: `artifacts/2026-08-03-offer-negotiation/11-single-offer-coach.png`
- Modify: `docs/reports/2026-08-01-offer-negotiation-release-verification.md`

- [ ] **Step 1: 运行前端全量门禁**

```powershell
cd web
npm.cmd test -- --run
npm.cmd run build
```

Expected: 全部测试退出码 0；TypeScript 与 Vite 生产构建退出码 0。记录测试文件数和测试数，不将 React `act()` 警告误报为失败。

- [ ] **Step 2: 运行仓库静态检查和 Offer 后端回归**

```powershell
cd ..
uv run ruff check .
uv run mypy src
uv run pytest tests/test_offer_negotiation_ai.py tests/test_offer_negotiation_safe_construction.py tests/test_offer_negotiation_repository.py tests/test_offer_negotiation_api.py tests/test_offer_comparison_dimensions.py tests/test_offer_negotiation_browser_harness.py tests/test_browser_network_audit.py -q
git diff --check 10fc4ba..HEAD
```

Expected: 所有命令退出码 0。

- [ ] **Step 3: 验证没有越界修改**

```powershell
$changed = git diff --name-only 10fc4ba..HEAD
$forbidden = $changed | Where-Object { $_ -match '^(src/offerpilot/|tests/.*\.py$|migrations/)' }
if ($forbidden) { throw "UI polish modified forbidden backend files:`n$($forbidden -join "`n")" }
```

Expected: 无输出，退出码 0。

- [ ] **Step 4: 运行 local 与真实 Offer API 验收**

使用隔离临时数据目录；真实配置只复制 `config.json`，不输出或修改密钥：

```powershell
uv run oc smoke --static-dir web/dist
uv run oc verify --profile local --static-dir web/dist
uv run oc verify-offer-negotiation --static-dir web/dist
uv run oc verify --profile real-ai --static-dir web/dist
```

Expected: local smoke、local verify、隔离 Offer negotiation API acceptance 和完整 real-AI verify 均通过。Provider 未知时只允许按既有语义使用原 Attempt/key 重试。

- [ ] **Step 5: 完成 UI 与 Pilot 双入口浏览器闭环**

使用中文候选人“筱哲”、亮色模式和不低于 1440×900 的宽屏视口：

1. Offer 中心显示两份中文 Offer、维度管理和优化后的 Offer 卡片。
2. 对比页显示固定事实、自定义维度和“尚未填写”，不显示排名或推荐。
3. UI 入口完成 preview、产品内来源确认、真实生成、选择、编辑、保存和历史查看。
4. Pilot 入口展示询问卡、用户选择回答卡，再完成相同的来源确认、生成、保存和历史查看。
5. 单 Offer“谈薪教练”仍能打开并绑定正确 Offer。
6. 浏览器控制台无应用错误。
7. CDP 审计中浏览器只访问本地页面和 `/api`；Provider 仅由服务端访问已配置 HTTPS endpoint；无招聘平台访问。
8. 数据库基线对比确认没有 Application、Event、Resume、Material、Knowledge、Opportunity Fit、Interview、Question、Reminder、Memory 或 Chat 跨领域写入。

任一入口未完成时不得将浏览器闭环报告为通过。

- [ ] **Step 6: 替换并逐张检查 11 张截图**

每张图片检查：

- 尺寸不低于 1440×900。
- 亮色模式。
- 关键标题、主操作、来源状态和结果完整可见。
- 不处于无意义滚动位置。
- 没有大片空白、遮挡、裁切、密钥、完整 Provider 原文或敏感配置。
- Pilot 的询问和用户回答分别有独立截图。

使用 `view_image` 或等价图像查看工具逐张查看原图，不能只相信截图命令成功。

- [ ] **Step 7: 更新发布报告**

报告必须记录：

- 验收 HEAD。
- 前端全量、构建、Ruff、Mypy、Offer 后端回归、local 和 real-AI 结果。
- UI 与 Pilot 各自完成的步骤。
- CDP 与 Provider 出站边界。
- 零跨领域写入结果。
- 11 张新截图的相对链接。
- 仍存在的真实剩余风险。

- [ ] **Step 8: 请求独立代码复审**

复审范围为 `10fc4ba..HEAD`，重点检查：

- UI/Pilot 草稿和幂等键是否仍隔离。
- preview、生成、确认和重试调用顺序是否改变。
- 历史是否错误使用当前 Offer。
- 是否新增自动写入或减少 HITL。
- 是否存在不可访问控件、暗色模式回归或窄屏溢出。

P0/P1/P2 必须修复并补回归后，重新执行受影响门禁。

- [ ] **Step 9: 提交验收证据**

```powershell
git add -f artifacts/2026-08-03-offer-negotiation/*.png docs/reports/2026-08-01-offer-negotiation-release-verification.md
```

```powershell
git commit -m "test: AI verify polished offer negotiation UI"
```

- [ ] **Step 10: 最终工作区检查**

```powershell
git diff --check 10fc4ba..HEAD
git status --short --branch
```

Expected: 差异检查通过，工作区干净。未获得用户明确授权前，不推送、不合并。

## 完成定义

只有同时满足以下条件才可声称 UI 收口完成：

- A 方案的结构化工作台在 UI 和 Pilot 两个入口都真实可用。
- 所有错误、空状态、来源变化、结果未知和历史只读状态都有正式视觉呈现。
- API、数据库、AI 契约、证据门控、幂等和 HITL 语义没有变化。
- 前端全量、构建、静态检查、Offer 后端回归、local 和真实 Offer API 验收通过。
- UI/Pilot 双入口浏览器闭环、CDP 网络审计和零跨领域写入断言通过。
- 11 张截图已替换并逐张人工检查。
- 独立代码复审无未处理 P0/P1/P2。
- 工作区干净，未推送、未合并。
