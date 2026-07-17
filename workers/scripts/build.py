from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
WORKERS_DIR = ROOT_DIR / "workers"
PUBLIC_DIR = WORKERS_DIR / "public"
STATIC_DIR = PUBLIC_DIR / "static"
DATA_DIR = PUBLIC_DIR / "data"
PDF_DIR = PUBLIC_DIR / "pdf"

DATA_COLUMNS = ["page", "level", "chinese", "english", "code"]
TABULAR_PAGE_MIN = 21
TABULAR_PAGE_MAX = 415
TABULAR_PDF_KEY = "target.pdf"

# Compact row layout consumed by workers/src/index.js:
# page, level, chinese, english, code, source_file_index, search_blob,
# normalized_code, parent_index, subtree_end
ROW_SUBTREE_END = 9


def normalize_row(raw_row: list[str]) -> list[str]:
    row = [str(value).strip() for value in raw_row]
    if len(row) < len(DATA_COLUMNS):
        row.extend([""] * (len(DATA_COLUMNS) - len(row)))
    return row[: len(DATA_COLUMNS)]


def parse_int(value: object, default: int = 0) -> int:
    try:
        if value is None:
            return default
        text = str(value).strip()
        if text == "":
            return default
        return int(float(text))
    except (TypeError, ValueError):
        return default


def normalize_code(value: object) -> str:
    if value is None:
        return ""
    return "".join(str(value).split()).lower()


def copy_tree(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def build_dataset() -> tuple[dict[str, object], dict[str, object]]:
    raw_rows: list[dict[str, object]] = []

    for path in sorted(ROOT_DIR.glob("icd-index-extraction-*.csv")):
        with path.open("r", encoding="utf-8", newline="") as fp:
            reader = csv.reader(fp)
            for line_no, raw_row in enumerate(reader, start=1):
                if not raw_row:
                    continue

                row = normalize_row(raw_row)
                if line_no == 1 and [cell.lower() for cell in row[: len(DATA_COLUMNS)]] == DATA_COLUMNS:
                    continue

                page = parse_int(row[0], -1)
                if page < 0:
                    continue

                chinese = row[2].strip()
                english = row[3].strip()
                code = row[4].strip()
                raw_rows.append(
                    {
                        "page": page,
                        "level": parse_int(row[1], 0),
                        "chinese": chinese,
                        "english": english,
                        "code": code,
                        "_source_file": path.name,
                        "_code_lower": normalize_code(code),
                        "_search_blob": " ".join(
                            part for part in [chinese.lower(), english.lower(), normalize_code(code)] if part
                        ).strip(),
                    }
                )

    source_files = sorted({str(row["_source_file"]) for row in raw_rows})
    source_file_indices = {name: index for index, name in enumerate(source_files)}
    rows: list[list[object]] = []
    stack: list[int] = []

    for row in raw_rows:
        level = parse_int(row["level"], 0)
        while stack and parse_int(rows[stack[-1]][1], 0) >= level:
            rows[stack.pop()][ROW_SUBTREE_END] = len(rows)

        parent_index = stack[-1] if stack else -1
        rows.append(
            [
                row["page"],
                level,
                row["chinese"],
                row["english"],
                row["code"],
                source_file_indices[str(row["_source_file"])],
                row["_search_blob"],
                row["_code_lower"],
                parent_index,
                -1,
            ]
        )
        stack.append(len(rows) - 1)

    while stack:
        rows[stack.pop()][ROW_SUBTREE_END] = len(rows)

    code_index: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        code_norm = str(row[7])
        if code_norm:
            code_index.setdefault(code_norm, []).append(index)

    tabular_rows: list[dict[str, object]] = []
    tabular_path = ROOT_DIR / "data" / "tabular_code_page_map.csv"
    if tabular_path.exists():
        with tabular_path.open("r", encoding="utf-8", newline="") as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                page = parse_int(row.get("page", -1), -1)
                code = str(row.get("code", "")).strip()
                code_norm = normalize_code(code)
                if page < TABULAR_PAGE_MIN or page > TABULAR_PAGE_MAX or not code_norm:
                    continue
                tabular_rows.append(
                    {
                        "page": page,
                        "code": code,
                        "code_norm": code_norm,
                        "row_type": str(row.get("row_type", "")).strip(),
                    }
                )

    dataset = {
        "meta": {
            "row_count": len(rows),
            "row_schema": [
                "page",
                "level",
                "chinese",
                "english",
                "code",
                "source_file_index",
                "search_blob",
                "normalized_code",
                "parent_index",
                "subtree_end",
            ],
        },
        "source_files": source_files,
        "rows": rows,
        "code_index": code_index,
    }
    tabular_dataset = {
        "meta": {"row_count": len(tabular_rows)},
        "rows": tabular_rows,
    }
    return dataset, tabular_dataset


def main() -> None:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if PDF_DIR.exists():
        shutil.rmtree(PDF_DIR)

    copy_tree(ROOT_DIR / "templates" / "index.html", PUBLIC_DIR / "index.html")
    copy_tree(ROOT_DIR / "static", STATIC_DIR)

    dataset, tabular_dataset = build_dataset()
    (DATA_DIR / "dataset.json").write_text(json.dumps(dataset, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (DATA_DIR / "tabular.json").write_text(
        json.dumps(tabular_dataset, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )

    pdf_path = ROOT_DIR / "target.pdf"
    pdf_manifest = {
        "available": pdf_path.exists() and pdf_path.stat().st_size > 0,
        "storage": "r2",
        "key": TABULAR_PDF_KEY,
        "total_size": pdf_path.stat().st_size if pdf_path.exists() else 0,
    }

    (DATA_DIR / "pdf-manifest.json").write_text(json.dumps(pdf_manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
