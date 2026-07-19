# ICD Proofreading Tool

A local Streamlit-based proofreading app for ICD-9-CM-3 index extraction results.

This repository is intended to share and collaborate on the electronic ICD-9-CM-3 index.

# ICD 校对工具

这是一个本地 Streamlit 校对应用，用于 ICD-9-CM-3 索引提取结果的人工校验。

本仓库旨在分享并协作维护电子 ICD-9-CM-3 索引。

## What it does

- Displays OCR/extracted page images from `target_pages/` on the left.
- Shows extracted ICD rows on the right in editable form.
- Supports direct edits to `page`, `level`, `chinese`, `english`, and `code`.
- Saves changes directly back to the original batch CSV source files.

## 功能说明

- 左侧显示 `target_pages/` 中的页面图像。
- 右侧显示可编辑的 ICD 提取结果表格。
- 支持直接编辑 `page`、`level`、`chinese`、`english` 和 `code` 字段。
- 变更会直接保存回原始批次 CSV 源文件。

## Files

- `proofreading_app.py` — main Streamlit proofreading app.
- `web_app.py` — Flask ICD index search/browse web app.
- `workers/` — Cloudflare Worker version of the read-only web app.
- `requirements-proofreading.txt` — Python dependencies for the proofreading app.
- `requirements-web.txt` — Python dependencies for the Flask web app.
- `icd-index-extraction-*.csv` — original extracted CSVs.
- `data/tabular_code_page_map.csv` — tabular code-to-PDF page map.
- `target.pdf` — optional PDF source file (ignored from Git).
- `target_pages/` — page image assets used for preview.
- `.gitignore` — ignores generated assets and editor/cache files.

## Setup

1. Create and activate a Python virtual environment in the repository root:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements-proofreading.txt
   ```

## 安装

1. 在仓库根目录创建并激活 Python 虚拟环境：

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

2. 安装依赖：

   ```bash
   pip install -r requirements-proofreading.txt
   ```

## Run the app

```bash
.venv/bin/python -m streamlit run proofreading_app.py --server.headless true --server.port 8765
```

Then open `http://localhost:8765` in your browser.

## Validate extracted CSV files

Use the CDC FY2012 English ICD-9-CM Index to Procedures to validate every
batch CSV without modifying the source files:

```bash
python scripts/validate_icd_csv.py
```

The first run downloads and caches the official `Pindex12.zip`. The generated
`validation_report.csv` contains every source row, its hierarchy, validation
status, confidence, official candidate, official hierarchy, path comparison,
and suggested action. The validator rebuilds the official hierarchy from the
RTF paragraph styles and indentation, then compares complete English paths
rather than matching leaf terms alone. Use
`--issues-only` to emit only errors, warnings, and rows requiring manual review.

## 校验提取 CSV

使用 CDC FY2012 英文版 ICD-9-CM 手术与操作索引校验全部批次 CSV；脚本不会修改源文件：

```bash
python scripts/validate_icd_csv.py
```

首次运行会下载并缓存官方 `Pindex12.zip`。生成的 `validation_report.csv`
包含每一源行的层级、校验状态、置信度、官方候选、`official_hierarchy`、
完整路径比较结果和处理建议。脚本根据 RTF 段落样式与
缩进重建官方层级树，不再只比较叶节点。传入 `--issues-only` 可只输出错误、
警告及需要人工复核的记录。
中文索引中形如“中文别名—see English term”的交叉引用会标记为
`ignored_cross_reference` 并按要求忽略，不参与英文官方路径匹配。
如需自动修复可确定的代码格式、代码不一致或缺失代码，可追加
`--fix-source`；脚本直接修改 Git 工作区中的源 CSV，不额外生成备份文件。
当官方完整路径对应多个代码时不会自动拼接或写入复合值，而是保留为人工复核。
同一字段若被多个修复规则提出不同值，也会标记为自动修复冲突并跳过写回，
不会按调用顺序采用最后一个规则的结果。
如需同时自动修复确定无疑的英文文本和父级拼写，使用
`--fix-source --fix-all`。父级修复要求至少两个子项指向同一官方父级，
且父级名称本身高度相似。脚本还会识别至少 3 个相邻可靠条目呈现的相同
level 偏移，并分轮修正到稳定状态；不会自动插入、删除或移动条目。修复后
会重新加载 CSV、重建源层级并二次校验，因此最终报告反映修复后的状态。
校验和 `--fix-source --fix-all` 默认先将所有输入 CSV 按文件顺序合并为一个逻辑序列，
再按 level 0 根分块并行处理；自动修复阶段并行计算结构修复建议。
文件有显式 level 0 时直接以其为边界；没有显式 level 0 的文件，则以 level 1 顶级条目
作为隐含边界。主进程会按固定顺序
合并建议、检测冲突并串行写回文件。可用 `--workers 1` 禁用并行，或用
`--workers N` 指定进程数。
对于孤立的 level 错误，如果紧邻上一行已精确匹配且正好是官方父级，脚本
也会把当前行调整为该父级的直接子级。
当多个官方候选仅因交叉引用根节点相同而产生歧义时，如果 CSV 的直接父级
唯一等于其中一个官方根节点，则按该父级消歧；仅在唯一匹配时生效。
父级自身仍有歧义时，若其后至少两个子项一致指向同一候选官方根节点，
也可据此消歧；该规则只更新报告判断，不自动修改源层级。
同一官方父级下连续两行出现相同 level 偏移时，也会作为有界成对偏移修复。
英文拼写自动替换仅允许每个连续差异片段不超过 3 个字母；仅相差一个
`o` 时忽略，并要求修复前后单词数量一致，禁止删除或增加完整单词。
英文比较忽略重音符号差异，例如 `Barany` 与 `Bárány` 视为相同。
`see category` 与 `see subcategory` 按等价合法引用比较；
`see also category` 与 `see also subcategory` 同样等价。
带 code 的合法 `see`/`see also` 交叉引用也必须通过官方完整父级路径校验；
只有 code 和交叉引用语法正确但父级路径不一致时，才标记为层级问题。
当代码和父级路径精确一致时，符合上述短拼写规则的候选从文本相似度
`0.90` 起允许自动修复。
对于逗号分隔的英文同义词，只要其中一项与官方叶节点、代码及完整父级路径
一致，即标记为 `pass_alias_exact`，并保留源 CSV 中的完整同义词列表。
如果这类同义词只造成父级路径比较差异，且删除一个多余父级后可与官方路径精确对应，
`--fix-all` 会整体下移该分支的 level，但保留源英文中的同义词列表。
逗号别名也可出现在父级路径中；例如 `eye, ocular` 会分别展开为官方的
`eye` 或 `ocular`，再匹配各自的子级路径。
同义词项带括号限定词时也可匹配其官方核心词，例如
`lung volume (pulmonary)` 匹配官方 `lung volume`。
当中文字段以英文专名开头时，`--fix-source --fix-all` 还会在英文列已与
官方词条确认一致后，同步修正中文字段中的该英文专名。
已确认的官方英文拼写错误通过显式映射规范化；例如 CDC 索引中的
`aneursym` 会写为 `aneurysm`，同时仍按同一官方路径校验。
已知的 CDC 原文拼写错误不会反向覆盖正确的源文本。

`proofreading_app.py` 会自动读取项目根目录的 `validation_report.csv`，
按 `source_file` 和 `source_line` 关联源条目，并在页面顶部 banner 中展示
每条问题的源行号、原始中英文、校验状态、官方完整路径和处理建议；编辑表格
仍只显示原始提取字段。报告更新后点击“从磁盘重新加载”即可刷新校验信息。

## ICD Web App (Flask)

A mobile-first browser interface without Streamlit.

```bash
pip install -r requirements-web.txt
.venv/bin/python web_app.py
```

Then open `http://localhost:8000` in your browser.

## Cloudflare Deployment

The Cloudflare-ready version lives under `workers/` and serves the read-only search and browse UI through a Worker with static assets. Its runtime data layout is optimized independently from the Flask app.

The Worker uses:

- Workers Assets for the generated HTML, CSS, JS, and JSON search dataset.
- R2 bucket `icd9cm3-index-pdf` for `target.pdf`.
- Worker cache enabled in `wrangler.jsonc`.
- Workers Caching for successful API responses by full request URL for 1 day.
- PDF response caching for 7 days while preserving byte-range requests.

```bash
cd workers
npm install
npm run build
npx wrangler dev
```

Then open the Wrangler local URL in your browser.

For local PDF testing, seed Wrangler's local R2 bucket:

```bash
cd workers
npm run r2:upload-pdf:local
```

To deploy:

```bash
cd workers
npm run build
npm run r2:create
npm run r2:upload-pdf
npm run deploy
```

If the R2 bucket already exists, `npm run r2:create` can be skipped.

The build step packages the CSV sources into a compact `workers/public/data/dataset.json`, writes the small tabular lookup to `workers/public/data/tabular.json`, writes `workers/public/data/pdf-manifest.json`, and copies the UI assets into `workers/public/` so the Worker can run without Flask or pandas at runtime. Search responses still include every match; hierarchy metadata and code indexes are prepared during the build to reduce Worker CPU time. `workers/public/` is generated output and should not be committed.

Routes and custom domains are intentionally not documented here. Keep route details in the Cloudflare Dashboard or in a private local Wrangler config, not in committed files.

## Notes

- The app uses page images from `target_pages/` rather than embedding the PDF directly.
- The images in `target_pages/` were extracted from the PDF through a separate preprocessing step.
- The Cloudflare Worker serves the tabular PDF from R2 at `/tabular-pdf`.
- `.gitignore` excludes local/generated artifacts such as `.venv/`, `node_modules/`, `.wrangler/`, `workers/public/`, `target.pdf`, and `target_pages/`.
- Edits are persisted directly into the original `icd-index-extraction-*.csv` files.

## 说明

- 应用使用 `target_pages/` 中的页面图像，而不是直接嵌入 PDF。
- `target_pages/` 中的图像来自对 PDF 的独立提取处理。
- Cloudflare Worker 版本通过 R2 在 `/tabular-pdf` 提供类目表 PDF。
- `.gitignore` 忽略 `.venv/`、`node_modules/`、`.wrangler/`、`workers/public/`、`target.pdf`、`target_pages/` 等本地或生成文件。
- 编辑结果会直接保存到原始 `icd-index-extraction-*.csv` 文件中。
