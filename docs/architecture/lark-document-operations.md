# 飞书文档 / 画板操作参考

仅在任务涉及飞书文档或画板时读取。授权边界见 [AGENTS.md](../../AGENTS.md) §0；本参考不授予额外的远端写权限。

以下为仓库已有操作经验，本次仅迁移整理，工具行为变化时需重新核实。

飞书文档使用 `lark-cli docs +fetch` 和 `lark-cli docs +update`。编辑飞书内容前，先通过 `lark-cli skills read lark-doc` 读取相关 `lark-doc` skill 指南。

### 跨文档画板引用

- `docs +update --command block_insert_after --content '<whiteboard token="X"></whiteboard>'` 是跨文档复用画板的可靠路径。服务端会把源画板 clone 成新 token 后插入目标位置。
- 不要用 `block_replace` 复用已有画板 token。它可能返回 `Whiteboard clone failed. Retry later` 并生成空块。
- clone 出来的画板是一次性快照，不是 live link。源画板后续更新不会同步到 clone。如需手动同步，先用 `whiteboard +query --output_as svg` 导出源 SVG，再用 `whiteboard +update --whiteboard-token 目标 --input_format svg --overwrite --source @./svg` 更新目标画板。

### Mermaid subgraph 和 node ID

Mermaid 的 subgraph 和 node 使用 ASCII ID，中文标签放在方括号里。style 目标引用 ASCII ID。

```mermaid
%% 错误：中文名不一定能稳定作为 style 目标
subgraph 只读来源
  ...
end
style 只读来源 fill:#f0f4ff

%% 正确：ASCII ID，中文标签
subgraph SOURCE[只读来源]
  ...
end
style SOURCE fill:#f0f4ff,stroke:#d1d5db
```

Node 也一样：使用 `NODE_ID["中文标签"]`，style 写 `NODE_ID`。

### 只改色或装饰时

`whiteboard +query --output_as svg` 返回的是渲染后的 SVG。只改颜色或清理装饰时，可以直接编辑渲染后的 SVG 再推回去，不必重画整张图。只有结构或布局变化时才重画。

### `docs +update str_replace` 雷区

1. 绝不要用 `str_replace` 改 `<pre><code>` 块内的行。匹配到其中一行可能会删除整个代码块，而且命令仍返回 `success`。
2. pattern 里不要包含 `</code>` 或 `</b>` 这类闭合标签。`str_replace` 用纯 rendered text；需要保留样式时用 `block_replace`。
3. 大段 `--content @file` / `--source @file` 可能 silent no-op。大内容优先用 stdin 和 `--content -`。
4. `str_replace` 没匹配到也可能返回 `success`。更新后必须 fetch 回读并验证新旧字符串。

推荐流程：大改前先把 full fetch 备份到系统临时目录，这样误删 `pre` 或 whiteboard 时还能恢复。先 fetch 目标范围和 block id；结构化内容优先用 `block_replace`；`str_replace` 只用短且唯一的纯文本 pattern；更新后再次 fetch 并检查新旧字符串。高风险编辑最终用 `docs +fetch --scope full` 验证，不要只相信 update 命令返回值。
