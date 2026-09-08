# README 截图刷新浏览器验收

> 历史验收快照，已归档。表内文件来自 `docs/assets/readme/2026-08-13/`，不代表当前 README 图片；新版记录见 [用户指南维护记录](../../product-manual/维护记录.md)。

- 验收基线：`main@c9929e7`
- 验收日期：2026-08-13
- 模式：亮色、中文、宽屏
- 案例：候选人“筱哲”的隔离合成数据
- 配置：从本机实际配置复制到临时隔离目录；本轮不调用外部 Provider
- 浏览器控制台错误：0

## 截图矩阵

| 文件 | 页面与内容 | 尺寸 | SHA-256 |
| --- | --- | --- | --- |
| `01-workspace-overview.png` | 工作台、本周求职作战台、今日行动与 Haru | 1455×900 | `051e21538974c629af70e9fb575a30f3869a4217e5f593c3faaf0b91c9035470` |
| `02-application-materials.png` | 投递材料工作区、岗位简历版本与当前 JD | 1455×900 | `bc5388487dba92a7d8d3004e3c4b632b50ff50be31696c985faca9fc3e34cdc6` |
| `03-pilot-confirmation.png` | Pilot 确定性 JD 写入确认卡 | 1455×1000 | `78cd2391f79c304d3fa62c2d2a8c3a7d7416378dd731a2abd1924ea3e7293454` |
| `04-interview-practice.png` | 文本模拟面试、简历版本与冻结 JD 来源 | 1455×1000 | `543de86e20803bf1cd7c89bc9516a446ab1e361266796aacf43d4b096945e344` |
| `05-offer-negotiation.png` | Offer 事实、比较维度及谈薪入口 | 1455×1000 | `1e38024a634dacf6423e8bf82d99ad80511017275535337f31e3f6dbccd24357` |

## 边界

- 截图均来自真实挂载页面，不是设计稿或静态拼图。
- Pilot 确认卡使用本地确定性流程，不产生模型费用。
- 演示数据仅存在于临时隔离目录，不写入用户正式数据目录。
- 截图不包含 API 密钥、Authorization Header 或 Provider 原始响应。
