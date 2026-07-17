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
