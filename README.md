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

- `proofreading_app.py` — main Streamlit app.
- `requirements-proofreading.txt` — Python dependencies for the app.
- `icd-index-extraction-*.csv` — original extracted CSVs.
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

## 运行应用

```bash
.venv/bin/python -m streamlit run proofreading_app.py --server.headless true --server.port 8765
```

然后在浏览器中打开 `http://localhost:8765`。

## Notes

- The app uses page images from `target_pages/` rather than embedding the PDF directly.
- The images in `target_pages/` were extracted from the PDF through a separate preprocessing step.
- `.gitignore` excludes `target.pdf`, the `target_pages/` folder, `.vscode/`, `__pycache__/`, and `.DS_Store`.
- Edits are persisted directly into the original `icd-index-extraction-*.csv` files.

## 说明

- 应用使用 `target_pages/` 中的页面图像，而不是直接嵌入 PDF。
- `target_pages/` 中的图像来自对 PDF 的独立提取处理。
- `.gitignore` 忽略了 `target.pdf`、`target_pages/` 文件夹、`.vscode/`、`__pycache__/` 和 `.DS_Store`。
- 编辑结果会直接保存到原始 `icd-index-extraction-*.csv` 文件中。
