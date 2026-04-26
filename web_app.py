from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file

ROOT_DIR = Path(__file__).resolve().parent
CSV_NAME_PATTERN = re.compile(r"icd-index-extraction-(\d+)-(\d+)\.csv$")
DATA_COLUMNS = ["page", "level", "chinese", "english", "code"]
TABULAR_PAGE_MIN = 21
TABULAR_PAGE_MAX = 415
TABULAR_ROOT_DIR = (ROOT_DIR / ".." / "icd9tabular").resolve()
TABULAR_INDEX_PATH = ROOT_DIR / "data" / "tabular_code_page_map.csv"
TABULAR_PDF_PATH = ROOT_DIR / "target.pdf"

app = Flask(__name__, template_folder="templates", static_folder="static")


def normalize_row(raw_row: List[str]) -> List[str]:
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


def get_csv_files(root_dir: Path) -> List[Path]:
    return sorted(
        [path for path in root_dir.glob("icd-index-extraction-*.csv") if CSV_NAME_PATTERN.match(path.name)]
    )


@lru_cache(maxsize=1)
def load_dataset() -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []

    for path in get_csv_files(ROOT_DIR):
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

                rows.append(
                    {
                        "page": page,
                        "level": parse_int(row[1], 0),
                        "chinese": row[2].strip(),
                        "english": row[3].strip(),
                        "code": row[4].strip(),
                        "_source_file": path.name,
                        "_source_line": line_no,
                    }
                )

    if not rows:
        return pd.DataFrame(columns=DATA_COLUMNS + ["_source_file", "_source_line"])

    df = pd.DataFrame(rows)
    df["page"] = df["page"].astype(int)
    df["level"] = df["level"].astype(int)
    df["chinese"] = df["chinese"].astype(str)
    df["english"] = df["english"].astype(str)
    df["code"] = df["code"].astype(str)
    df["_chinese_lower"] = df["chinese"].str.lower()
    df["_english_lower"] = df["english"].str.lower()
    df["_code_lower"] = df["code"].str.lower()
    df["_search_blob"] = (df["_chinese_lower"] + " " + df["_english_lower"] + " " + df["_code_lower"]).str.strip()
    df = df.reset_index(drop=True)
    return df


@lru_cache(maxsize=1)
def get_row_id_index_map() -> Dict[str, int]:
    df = load_dataset()
    id_map: Dict[str, int] = {}
    for idx, row in df.iterrows():
        id_map[get_row_id(row)] = int(idx)
    return id_map


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.lower() == "nan":
        return ""
    return text.strip()


def normalize_code(value: object) -> str:
    if value is None:
        return ""
    code = str(value).strip()
    return re.sub(r"\s+", "", code).lower()


@lru_cache(maxsize=20000)
def _extract_references_cached(text: str, language: str) -> Tuple[Tuple[str, str], ...]:
    refs: List[Tuple[str, str]] = []
    if language == "zh":
        pattern = re.compile(r"(?:[-（(]?\s*)(另见|见)\s*([^；;\n]+)", flags=re.IGNORECASE)
        for match in pattern.finditer(text):
            target = match.group(2).strip()
            if target:
                refs.append((match.group(1), target))
    else:
        pattern = re.compile(r"\b(see also|see)\s+([^;\n]+)", flags=re.IGNORECASE)
        for match in pattern.finditer(text):
            target = match.group(2).strip()
            if target:
                refs.append((match.group(1).lower(), target))
    return tuple(refs)


def parse_code_key(code: str) -> Tuple[int, int] | None:
    """Normalize ICD code to a comparable key: (major, minor_or_-1)."""
    if not code:
        return None

    text = normalize_code(code)
    if not text:
        return None

    if not re.fullmatch(r"\d{1,2}(?:\.\d{1,2})?", text):
        return None

    if "." not in text:
        return (int(text), -1)

    major_text, minor_text = text.split(".", 1)
    # Treat one-decimal and two-decimal forms consistently (e.g., 00.4 == 00.40).
    minor_scaled = int(minor_text.ljust(2, "0")[:2])
    return (int(major_text), minor_scaled)


@lru_cache(maxsize=1)
def load_tabular_page_boundaries() -> List[Dict[str, Any]]:
    df = load_tabular_index()
    if df.empty:
        return []

    boundaries: List[Dict[str, Any]] = []
    seen_pages: set[int] = set()
    ordered = df.sort_values(["page", "code_norm"], kind="stable")
    for _, row in ordered.iterrows():
        page = parse_int(row.get("page", -1), -1)
        if page in seen_pages:
            continue

        code = normalize_text(row.get("code", ""))
        code_key = parse_code_key(code)
        if code_key is None:
            continue

        seen_pages.add(page)
        boundaries.append(
            {
                "page": page,
                "code": code,
                "code_norm": normalize_code(code),
                "code_key": code_key,
            }
        )

    return boundaries


@lru_cache(maxsize=1)
def load_tabular_index() -> pd.DataFrame:
    if not TABULAR_INDEX_PATH.exists():
        return pd.DataFrame(columns=["page", "code", "row_type", "code_norm"])

    try:
        df = pd.read_csv(
            TABULAR_INDEX_PATH,
            dtype={"code": str, "row_type": str},
            keep_default_na=False,
        )
    except Exception:
        return pd.DataFrame(columns=["page", "code", "row_type", "code_norm"])

    if "page" not in df.columns or "code" not in df.columns:
        return pd.DataFrame(columns=["page", "code", "row_type", "code_norm"])

    if "row_type" not in df.columns:
        df["row_type"] = ""

    df = df.copy()
    df["page"] = df["page"].apply(lambda value: parse_int(value, -1))
    df["code"] = df["code"].astype(str).str.strip()
    df["code_norm"] = df["code"].apply(normalize_code)
    df = df[(df["page"] >= TABULAR_PAGE_MIN) & (df["page"] <= TABULAR_PAGE_MAX)]
    df = df[df["code_norm"] != ""]
    return df


@lru_cache(maxsize=1)
def get_tabular_pdf_path() -> Path | None:
    if not TABULAR_PDF_PATH.exists():
        return None
    return TABULAR_PDF_PATH


def extract_references(text: str, language: str) -> List[Dict[str, str]]:
    if not text:
        return []
    return [{"kind": kind, "target": target} for kind, target in _extract_references_cached(text, language)]


def build_hierarchy(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted(
        rows,
        key=lambda item: (
            item.get("page", 0),
            item.get("source_file", ""),
            item.get("source_line", 0),
        ),
    )
    tree: List[Dict[str, Any]] = []
    stack: List[Dict[str, Any]] = []

    for node in ordered:
        level = parse_int(node.get("level", 0), 0)
        if level < 0:
            level = 0

        # Reuse the existing row object and only normalize missing fields to
        # avoid rebuilding an equivalent dict for large result sets.
        node["level"] = level
        node.setdefault(
            "id",
            f"{node.get('page', 0)}-{level}-{node.get('source_file', '')}-{node.get('source_line', 0)}",
        )
        node.setdefault("page", 0)
        node.setdefault("code", "")
        node.setdefault("chinese", "")
        node.setdefault("english", "")
        node.setdefault("source_file", "")
        node.setdefault("source_line", 0)
        if "references" not in node:
            chinese = normalize_text(node.get("chinese", ""))
            english = normalize_text(node.get("english", ""))
            node["references"] = extract_references(chinese, "zh") + extract_references(english, "en")
        node["has_children"] = bool(node.get("has_children", False))
        node["matched"] = bool(node.get("matched", False))
        node["children"] = []

        while stack and stack[-1]["level"] >= level:
            stack.pop()

        if stack:
            stack[-1]["children"].append(node)
        else:
            tree.append(node)

        stack.append(node)

    return tree


def build_term_index(df: pd.DataFrame) -> Dict[str, List[Dict[str, Any]]]:
    index: Dict[str, List[Dict[str, Any]]] = {}
    for _, row in df.iterrows():
        for text in [normalize_text(row.get("chinese", "")), normalize_text(row.get("english", ""))]:
            key = text.lower()
            if key:
                index.setdefault(key, []).append(row)
    return index


def has_descendants(index: int, all_rows: pd.DataFrame) -> bool:
    if index + 1 >= len(all_rows):
        return False
    return parse_int(all_rows.at[index + 1, "level"], 0) > parse_int(all_rows.at[index, "level"], 0)


def has_descendants_from_levels(index: int, levels: List[int]) -> bool:
    if index + 1 >= len(levels):
        return False
    return parse_int(levels[index + 1], 0) > parse_int(levels[index], 0)


def row_index_to_json(all_rows: pd.DataFrame, index: int, matched: bool = False, has_children: bool = False) -> Dict[str, Any]:
    chinese = normalize_text(all_rows.at[index, "chinese"])
    english = normalize_text(all_rows.at[index, "english"])
    source_file = normalize_text(all_rows.at[index, "_source_file"])
    source_line = parse_int(all_rows.at[index, "_source_line"], 0)
    level = parse_int(all_rows.at[index, "level"], 0)
    page = parse_int(all_rows.at[index, "page"], 0)
    code = normalize_text(all_rows.at[index, "code"])
    references = extract_references(chinese, "zh") + extract_references(english, "en")
    return {
        "id": f"{page}-{level}-{source_file}-{source_line}",
        "page": page,
        "level": level,
        "code": code,
        "chinese": chinese,
        "english": english,
        "source_file": source_file,
        "source_line": source_line,
        "references": references,
        "matched": matched,
        "has_children": has_children,
    }


def collect_relevant_rows(results: pd.DataFrame, all_rows: pd.DataFrame) -> List[Dict[str, Any]]:
    matched_indices = {int(idx) for idx in results.index}
    if not matched_indices:
        return []

    ancestor_indices = set()
    levels = list(all_rows["level"])

    for row_index in matched_indices:
        current_level = parse_int(levels[row_index], 0)
        search_idx = row_index
        while search_idx > 0 and current_level > 0:
            search_idx -= 1
            prior_level = parse_int(levels[search_idx], 0)
            if prior_level < current_level:
                if prior_level > 0:
                    ancestor_indices.add(search_idx)
                current_level = prior_level

    combined_indices = sorted(matched_indices | ancestor_indices)
    return [
        row_index_to_json(
            all_rows,
            index,
            matched=(index in matched_indices),
            has_children=has_descendants_from_levels(index, levels),
        )
        for index in combined_indices
    ]


@lru_cache(maxsize=256)
def get_cached_search_payload(
    query: str,
    mode: str,
    fields_key: Tuple[str, ...],
    page_min: int | None,
    page_max: int | None,
    level_min: int | None,
    level_max: int | None,
    file_filters_key: Tuple[str, ...],
) -> Dict[str, Any]:
    rows, tree_rows = search_dataframe(
        query=query,
        mode=mode,
        fields=list(fields_key),
        page_min=page_min,
        page_max=page_max,
        level_min=level_min,
        level_max=level_max,
        file_filters=list(file_filters_key),
    )

    return {
        "query": query,
        "count": len(rows),
        "rows": rows,
        "tree": build_hierarchy(tree_rows),
    }


@lru_cache(maxsize=1024)
def get_cached_children_payload(node_id: str, query: str, mode: str, fields_key: Tuple[str, ...]) -> Dict[str, Any]:
    df = load_dataset()
    try:
        start_index = find_row_index_by_id(node_id, df)
    except ValueError:
        return {"children": []}

    descendants = collect_descendant_rows(start_index, df, query=query, mode=mode, fields=list(fields_key))
    return {"children": build_hierarchy(descendants)}


def collect_descendant_rows(start_index: int, all_rows: pd.DataFrame, query: str = "", mode: str = "auto", fields: List[str] = None) -> List[Dict[str, Any]]:
    if fields is None:
        fields = ["chinese", "english", "code"]
    query_lower = query.strip().lower()
    is_code_query = bool(re.fullmatch(r"\d+(?:\.\d+)?", query_lower))
    rows: List[Dict[str, Any]] = []
    start_level = parse_int(all_rows.at[start_index, "level"], 0)
    cursor = start_index
    while cursor + 1 < len(all_rows):
        cursor += 1
        next_level = parse_int(all_rows.at[cursor, "level"], 0)
        if next_level <= start_level:
            break
        row = all_rows.loc[cursor]
        matched = False
        if query_lower:
            if is_code_query or mode == "code":
                matched = str(row.get("code", "")).lower().startswith(query_lower)
            else:
                text_values = " ".join(str(row.get(field, "")) for field in fields).lower()
                if mode == "phrase":
                    matched = query_lower in text_values
                elif mode == "any":
                    tokens = [token for token in query_lower.split() if token]
                    matched = any(token in text_values for token in tokens) if tokens else False
                else:
                    matched = query_lower in text_values
        rows.append(row_to_json(row, matched=matched, has_children=has_descendants(cursor, all_rows)))
    return rows


def search_dataframe(
    query: str,
    mode: str,
    fields: List[str],
    page_min: int,
    page_max: int,
    level_min: int,
    level_max: int,
    file_filters: List[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    df = load_dataset()
    query_text = query.strip()
    if page_min is not None:
        df = df[df["page"] >= page_min]
    if page_max is not None:
        df = df[df["page"] <= page_max]
    if level_min is not None:
        df = df[df["level"] >= level_min]
    if level_max is not None:
        df = df[df["level"] <= level_max]
    if file_filters:
        df = df[df["_source_file"].isin(file_filters)]

    if not df.empty:
        df = df.reset_index(drop=True)

    if not query_text:
        browse_rows = df[df["level"] == 0].copy()
        result_rows = [row_to_json(row, has_children=has_descendants(int(idx), df)) for idx, row in browse_rows.iterrows()]
        return result_rows, result_rows

    is_code_query = bool(re.fullmatch(r"\d+(?:\.\d+)?", query_text))
    query_lower = query_text.lower()
    results: pd.DataFrame
    code_series = df["_code_lower"]

    if is_code_query or mode == "code":
        exact_results = df[code_series == query_lower].copy()
        if not exact_results.empty:
            # Return all exact code matches so all ancestor chains are preserved
            results = exact_results.sort_values(
                ["page", "level", "_source_file", "_source_line"],
                kind="stable",
            ).copy()
        else:
            results = df[code_series.str.startswith(query_lower)].copy()
    else:
        if fields == ["chinese", "english", "code"]:
            text_values = df["_search_blob"]
        else:
            field_to_lower_col = {
                "chinese": "_chinese_lower",
                "english": "_english_lower",
                "code": "_code_lower",
            }
            selected_cols = [field_to_lower_col[field] for field in fields if field in field_to_lower_col]
            if selected_cols:
                text_values = df[selected_cols].fillna("").agg(" ".join, axis=1).str.strip()
            else:
                text_values = pd.Series("", index=df.index)
        if mode == "phrase":
            results = df[text_values.str.contains(query_lower, regex=False)].copy()
        elif mode == "any":
            tokens = [token for token in query_lower.split() if token]
            if tokens:
                mask = pd.Series(False, index=df.index)
                for token in tokens:
                    mask |= text_values.str.contains(token, regex=False)
                results = df[mask].copy()
            else:
                results = df.copy()
        else:
            results = df[text_values.str.contains(query_lower, regex=False)].copy()

    result_rows = [row_to_json(row, matched=True, has_children=has_descendants(int(idx), df)) for idx, row in results.iterrows()]
    tree_rows = collect_relevant_rows(results, df) if query_text else [row_to_json(row, matched=False, has_children=has_descendants(int(idx), df)) for idx, row in df.iterrows()]
    if query_text:
        tree_rows = [row for row in tree_rows if row.get("level", 0) != 0]
    return result_rows, tree_rows


def get_row_id(row: pd.Series) -> str:
    return f"{int(row.get('page', 0))}-{parse_int(row.get('level', 0), 0)}-{normalize_text(row.get('_source_file', ''))}-{int(row.get('_source_line', 0))}"


def row_to_json(row: pd.Series, matched: bool = False, has_children: bool = False) -> Dict[str, Any]:
    chinese = normalize_text(row.get("chinese", ""))
    english = normalize_text(row.get("english", ""))
    references = extract_references(chinese, "zh") + extract_references(english, "en")
    return {
        "id": get_row_id(row),
        "page": int(row.get("page", 0)) if row.get("page", None) is not None else 0,
        "level": int(row.get("level", 0)) if row.get("level", None) is not None else 0,
        "code": normalize_text(row.get("code", "")),
        "chinese": chinese,
        "english": english,
        "source_file": normalize_text(row.get("_source_file", "")),
        "source_line": int(row.get("_source_line", 0)) if row.get("_source_line", None) is not None else 0,
        "references": references,
        "matched": matched,
        "has_children": has_children,
    }


def row_matches_target(row: pd.Series, target_lower: str, strict_prefix: bool = False) -> bool:
    for key in ["english", "chinese"]:
        text = normalize_text(row.get(key, "")).lower()
        if not text:
            continue
        if strict_prefix:
            if text == target_lower:
                return True
            if text.startswith(target_lower):
                remainder = text[len(target_lower):]
                if remainder and remainder[0] in " ,-/()—":
                    return True
        else:
            if text == target_lower or text.startswith(target_lower) or target_lower in text:
                return True
    return False


def find_ordered_target_rows(df: pd.DataFrame, parts: List[str]) -> pd.DataFrame:
    scored = []
    for idx, row in df.iterrows():
        english = normalize_text(row.get("english", "")).lower()
        chinese = normalize_text(row.get("chinese", "")).lower()
        combined = " / ".join([p for p in [english, chinese] if p])
        cursor = 0
        ok = True
        positions = []
        for part in parts:
            pos = combined.find(part, cursor)
            if pos == -1:
                ok = False
                break
            positions.append(pos)
            cursor = pos + len(part)
        if not ok:
            continue

        score = 0
        see_index = combined.find(" see ")
        if see_index == -1:
            see_index = combined.find(" see also ")
        if see_index == -1:
            see_index = len(combined)

        final_pos = positions[-1]
        if final_pos < see_index:
            score += 100
        if final_pos == 0:
            score += 50
        if combined.startswith(parts[0]):
            score += 30
        if combined.startswith(parts[-1]):
            score += 20
        if final_pos <= combined.find(parts[0]) + len(parts[0]) + 20:
            score += 10

        # Prefer titles that start with the first path part and that don't place the
        # final target exclusively inside a 'see' clause.
        scored.append((score, idx))

    if not scored:
        return df.iloc[0:0]
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_idx = scored[0][1]
    return df.loc[[best_idx]].copy()


def find_hierarchical_target_rows(df: pd.DataFrame, parts: List[str]) -> pd.DataFrame:
    if not parts:
        return df.iloc[0:0]

    for idx, row in df.iterrows():
        if not row_matches_target(row, parts[0], strict_prefix=True):
            continue

        current_index = int(idx)
        success = True
        for part in parts[1:]:
            target_lower = part.lower()
            start_level = parse_int(df.at[current_index, "level"], 0)
            found_index = None
            cursor = current_index
            while cursor + 1 < len(df):
                cursor += 1
                next_level = parse_int(df.at[cursor, "level"], 0)
                if next_level <= start_level:
                    break
                if row_matches_target(df.loc[cursor], target_lower):
                    found_index = cursor
                    break
            if found_index is None:
                if row_matches_target(df.loc[current_index], target_lower):
                    continue
                success = False
                break
            current_index = found_index
        if success:
            return df.loc[[current_index]].copy()

    return df.iloc[0:0]


@app.route("/")
def index() -> Any:
    return render_template("index.html")


@app.route("/api/search")
def api_search() -> Any:
    query = request.args.get("q", "", type=str)
    mode = request.args.get("mode", "text", type=str)
    field_list = request.args.get("fields", "chinese,english,code", type=str)
    fields = [field for field in field_list.split(",") if field in DATA_COLUMNS]
    if not fields:
        fields = ["chinese", "english", "code"]

    page_min = request.args.get("page_min", type=int)
    page_max = request.args.get("page_max", type=int)
    level_min = request.args.get("level_min", type=int)
    level_max = request.args.get("level_max", type=int)
    file_filters = request.args.getlist("file")
    payload = get_cached_search_payload(
        query=query,
        mode=mode,
        fields_key=tuple(fields),
        page_min=page_min,
        page_max=page_max,
        level_min=level_min,
        level_max=level_max,
        file_filters_key=tuple(sorted(file_filters)),
    )
    return jsonify(payload)


@app.route("/api/locate")
def api_locate() -> Any:
    target = request.args.get("target", "", type=str).strip()
    if not target:
        return jsonify({"query": target, "count": 0, "rows": [], "tree": []})

    # Optionally skip highlighting (mark=false) when request asks so
    mark_param = request.args.get("mark", "true", type=str).lower()
    mark = not (mark_param in ("0", "false", "no"))

    # If reference explicitly points to a subcategory (亚目 / subcategory), ignore
    tl = target.lower()
    if "亚目" in target or "subcategory" in tl:
        return jsonify({"query": target, "count": 0, "rows": [], "tree": [], "ignored": True})

    df = load_dataset()
    target_lower = target.lower()

    # Exact code match?
    if re.fullmatch(r"\d+(?:\.\d+)?", target_lower):
        results = df[df["_code_lower"] == target_lower].copy()
    else:
        # Exact text match against chinese or english columns first
        mask_cn = df["_chinese_lower"].str.strip() == target_lower
        mask_en = df["_english_lower"].str.strip() == target_lower
        results = df[mask_cn | mask_en].copy()

        # If not found, try exact-leading-token match (e.g., reference 'Buckling' -> 'Buckling, scleral')
        if results.empty:
            starts_cn = df["_chinese_lower"].str.strip().str.startswith(target_lower)
            starts_en = df["_english_lower"].str.strip().str.startswith(target_lower)
            if starts_cn.any() or starts_en.any():
                results = df[starts_cn | starts_en].copy()
            elif "," in target_lower:
                parts = [part.strip() for part in re.split(r"[，,]", target_lower) if part.strip()]
                if len(parts) > 1:
                    results = find_hierarchical_target_rows(df, parts)
                    if results.empty:
                        results = find_ordered_target_rows(df, parts)
            else:
                # If still not found, try to extract trailing code like '... 78.1'
                m = re.search(r"(\d+(?:\.\d+)?)$", target_lower)
                if m:
                    code = m.group(1)
                    results = df[df["_code_lower"] == code].copy()

    result_rows = [row_to_json(row, matched=mark, has_children=has_descendants(int(idx), df)) for idx, row in results.iterrows()]
    tree_rows = collect_relevant_rows(results, df) if not results.empty else []
    if tree_rows:
        tree_rows = [row for row in tree_rows if row.get("level", 0) != 0]

    # If caller requested no marking, clear matched flags in the tree as well
    if not mark:
        for r in result_rows:
            r["matched"] = False
        for r in tree_rows:
            r["matched"] = False

    tree = build_hierarchy(tree_rows)
    return jsonify({"query": target, "count": len(result_rows), "rows": result_rows, "tree": tree})


@app.route("/api/children")
def api_children() -> Any:
    node_id = request.args.get("id", "", type=str)
    query = request.args.get("q", "", type=str)
    mode = request.args.get("mode", "auto", type=str)
    field_list = request.args.get("fields", "chinese,english,code", type=str)
    fields = [field for field in field_list.split(",") if field in DATA_COLUMNS]
    if not fields:
        fields = ["chinese", "english", "code"]
    return jsonify(get_cached_children_payload(node_id=node_id, query=query, mode=mode, fields_key=tuple(fields)))


@app.route("/api/tabular")
def api_tabular() -> Any:
    query_code = request.args.get("code", "", type=str).strip()
    if not query_code:
        return jsonify({"query": query_code, "count": 0, "rows": [], "page": None})

    code_norm = normalize_code(query_code)
    if not code_norm:
        return jsonify({"query": query_code, "count": 0, "rows": [], "page": None})

    df = load_tabular_index()
    if df.empty:
        return jsonify(
            {
                "query": query_code,
                "count": 0,
                "rows": [],
                "page": None,
                "error": "tabular index not available",
            }
        )

    matches = df[df["code_norm"] == code_norm].copy()
    rows: List[Dict[str, Any]] = []

    if not matches.empty:
        matches = matches.sort_values(["page", "code_norm", "row_type"], kind="stable")
        for _, row in matches.head(10).iterrows():
            rows.append(
                {
                    "page": parse_int(row.get("page", -1), -1),
                    "code": normalize_text(row.get("code", "")),
                    "row_type": normalize_text(row.get("row_type", "")),
                }
            )
    else:
        query_key = parse_code_key(code_norm)
        boundaries = load_tabular_page_boundaries()
        selected_boundary: Dict[str, Any] | None = None

        if query_key is not None:
            # Pick the boundary with the largest code_key <= query_key.
            # This is robust to occasional out-of-order/abnormal rows in the
            # page->first-code map (e.g., a late page containing a small code).
            for boundary in boundaries:
                boundary_key = boundary["code_key"]
                if boundary_key > query_key:
                    continue
                if selected_boundary is None or boundary_key > selected_boundary["code_key"]:
                    selected_boundary = boundary
                elif (
                    selected_boundary is not None
                    and boundary_key == selected_boundary["code_key"]
                    and boundary["page"] < selected_boundary["page"]
                ):
                    # On identical boundary code, prefer earlier page.
                    selected_boundary = boundary

        if selected_boundary is None:
            return jsonify({"query": query_code, "count": 0, "rows": [], "page": None})

        rows.append(
            {
                "page": selected_boundary["page"],
                "code": selected_boundary["code"],
                "row_type": "page_boundary",
            }
        )

    selected = rows[0]
    page = parse_int(selected.get("page", -1), -1)
    pdf_available = get_tabular_pdf_path() is not None

    return jsonify(
        {
            "query": query_code,
            "count": len(rows),
            "rows": rows,
            "page": page,
            "pdf_url": "/tabular-pdf" if pdf_available else "",
        }
    )


@app.route("/tabular-pdf")
def tabular_pdf() -> Any:
    pdf_path = get_tabular_pdf_path()
    if pdf_path is None or not pdf_path.exists():
        return ("Tabular PDF not found", 404)
    # Allow browser PDF viewers to request byte ranges instead of pulling the
    # whole file in one response.
    return send_file(
        pdf_path,
        mimetype="application/pdf",
        conditional=True,
        etag=True,
        max_age=3600,
    )


def find_row_index_by_id(node_id: str, all_rows: pd.DataFrame) -> int:
    if all_rows is load_dataset():
        mapped = get_row_id_index_map().get(node_id)
        if mapped is not None:
            return mapped

    for index, row in all_rows.iterrows():
        if get_row_id(row) == node_id:
            return int(index)
    raise ValueError(f"Node not found: {node_id}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True, use_reloader=True)
