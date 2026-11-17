# qawrench mermaid-paths：流程图路径附录

`qawrench mermaid-paths`（原为独立工具 `mermaid-paths`）把基础 Mermaid 流程图导出为离线 HTML
附录，提供语义节点列表、分支邻接表、逐节点跟随的原生链接，以及明确的循环和返回路线。
当流程图需要在看不到 SVG 的情况下也能被阅读（例如无障碍文档），并且希望每条连接的
说明文字在发布前都经过作者确认时使用。导出过程不需要浏览器、服务器、网络或模型；生成的
HTML 不包含 JavaScript，也不会加载图中引用的图片。

Mermaid 图内的键盘导航已有现成工具 `@inclunet/mermaid-a11y`。本命令补充的是独立于 SVG
的静态附录，以及“作者逐条确认连接文字”的交付流程。英文说明见
[mermaid-paths.md](mermaid-paths.md)，其中包含完整的语法范围、限制和前人工作对比。

## 安装

需要 Python 3.11 或更新版本。本命令只使用 Python 标准库，无需安装额外依赖（extra）。

```sh
pipx install qawrench                 # 或：pip install qawrench
qawrench mermaid-paths --version      # 输出 qawrench 版本号，例如 0.1.0
```

`python -m qawrench mermaid-paths` 与 `qawrench mermaid-paths` 等价。选项写在子命令之后，
例如 `qawrench mermaid-paths build INPUT --draft`。

## 快速开始

以下示例使用 qawrench 源码仓库中的文件，请在仓库根目录执行。中文示例的确认文件已由
示例作者确认，下面的命令会逐字节重新生成仓库中的 `examples/mermaid-paths/approval-zh.html`：

```sh
qawrench mermaid-paths build examples/mermaid-paths/approval-zh.mmd \
  --review examples/mermaid-paths/approval-zh.review.json \
  --lang zh-CN --label-lang zh-CN -o /tmp/approval-zh.html
```

英语示例为 `examples/mermaid-paths/review-process.html`。

处理自己的流程图时，先生成确认文件：

```sh
qawrench mermaid-paths review workflow.mmd --lang zh-CN -o workflow.review.json
```

打开 JSON 文件，逐条检查：

- `from`、`to` 及对应的 `fromLabel`、`toLabel`：连接起点、终点是否正确。
- `sourceLine`、`lineStyle`、`sourceLabel`：源文件位置、线条样式和原有连接文字。
- `text`：读者将听到的连接说明。请补足无文字连接的用途、条件或返回要求。
- `confirmed`：仅在核对该条说明后改为 `true`。

然后导出：

```sh
qawrench mermaid-paths build workflow.mmd --review workflow.review.json \
  --lang zh-CN --label-lang zh-CN -o workflow.html
```

`--lang` 选择英语（`en`）或简体中文（`zh-CN`）界面；`--label-lang` 用 BCP 47 标签标记源
节点文字和作者说明的语言。工具不会自动翻译作者的文字，也不会从菱形、颜色或布局推断业务
含义。

尚未确认完时可用 `--draft` 生成带有明确草稿标记的附录。提供的确认文件如格式不正确或已经
过期，即使在草稿模式也会报错。原始 `.mmd` 的任何字节变化（包括空白）都会使确认文件过期
（`STALE_REVIEW`），需要重新生成并逐条确认。确认文件是作者的声明，不是身份签名。

```sh
qawrench mermaid-paths build workflow.mmd --draft -o workflow.draft.html
qawrench mermaid-paths inspect workflow.mmd -o workflow.graph.json
```

## 命令与退出码

| 命令 | 作用 |
| --- | --- |
| `review INPUT` | 生成确认文件模板（`mermaid-paths/review-v1` JSON） |
| `inspect INPUT` | 输出分析后的图结构（`mermaid-paths/graph-v1` JSON） |
| `build INPUT` | 生成 HTML 附录（需要 `--review` 或 `--draft`） |
| `styles` | 输出嵌入片段所需的样式表 |

`INPUT` 和 `-o/--output` 可用 `-` 表示标准输入/输出，默认输出到标准输出。输入必须是
UTF-8。导出拒绝覆盖现有文件，确实需要覆盖时可用 `--force`；源文件和输入的确认文件始终
受保护（包括通过符号链接等其他名称指向同一文件的情况）。

退出码：成功为 0；参数、输入或确认文件无效为 2（错误信息格式为
`qawrench mermaid-paths: 错误代码: 说明`，例如 `PARSE`、`UNSUPPORTED_EDGE`、
`STALE_REVIEW`）；文件系统或意外错误为 1。

## 从 Markdown 提取

```sh
qawrench mermaid-paths review guide.md --markdown --diagram 2 \
  --lang zh-CN -o diagram-2.review.json
qawrench mermaid-paths build guide.md --markdown --diagram 2 \
  --review diagram-2.review.json --lang zh-CN -o diagram-2.html
```

仅提取信息字符串第一个词为 `mermaid`（不区分大小写）的代码围栏，包括嵌套在列表和引用块
中的围栏；编号从 1 开始。文档包含多个 Mermaid 图时必须明确选择。解析器会规范化换行和围栏
缩进，确认文件绑定的是提取后的图源码；修改周围正文不会使它过期。此时 `sourceLine` 相对于
提取后的图。

## 阅读和嵌入

用 Tab、Shift+Tab 选择链接，按 Enter 跟随连接。用浏览器“后退”回溯此前跟随的位置，也可用
读屏软件的标题导航切换节点。每个节点都能从节点列表访问，包含孤立节点和没有外部入口的循环。
循环组附有一条具体的闭合返回路线。

用 `--fragment` 可生成适合放入现有文档的 HTML 片段；再执行
`qawrench mermaid-paths styles -o appendix.css`，将样式表放入宿主文档的 head。
同页嵌入多个图时，请提供不同的 `--id-prefix` 和有区分度的 `--title`。

## 范围与限制

支持 `flowchart`/`graph` 图中的基础文字节点（各种基本形状）、单向实线/虚线/粗线箭头及其
文字、链式连接、`&` 扇出、重复边、自循环和较大的循环；`accTitle`、`accDescr` 作为附录标题
和说明。样式、类、方向声明、配置指令和前置配置会被接受，但不会解释，附录中会列出作者注意
事项。子图、新版 `@{...}` 数据块、点击动作、双向/无箭头/圆点/叉号/隐形连接及其他图种会明确
报错。隐藏或依赖 CSS/ARIA 的标签、图形、列表和表格需要先改成明确的纯文字标签。数字实体请
用 Mermaid 的 `#9829;` 形式，避免 HTML 数字实体（`&#9829;`）在不同处理阶段产生歧义。

`%%` 注释必须独占一行；某一行只要出现 `direction` 后接 `TB`、`BT`、`RL`、`LR` 或 `TD`，
整行都会按 Mermaid 规则被当作方向声明（即使位于带引号的标签内）。完整的语法范围见英文说明的
“Supported Mermaid subset”一节。

输入、图规模和输出都有资源上限：源文件 1 MiB、1,000 个节点、展开后 5,000 条连接、每个标签
或标识符 20,000 个字符、每条确认说明 2,000 个字符。

已进行真实 Mermaid 官方文档样例对比、HTML 结构检查、循环一致性检查和 CLI 检查。尚未进行
真实读屏软件和浏览器组合的人工测试，不声称已获得 WCAG 认证。
