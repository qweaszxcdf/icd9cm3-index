from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd
from flask import Flask, jsonify, render_template, request

ROOT_DIR = Path(__file__).resolve().parent
CSV_NAME_PATTERN = re.compile(r"icd-index-extraction-(\d+)-(\d+)\.csv$")
DATA_COLUMNS = ["page", "level", "chinese", "english", "code"]

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
    return df


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.lower() == "nan":
        return ""
    return text.strip()


def extract_references(text: str, language: str) -> List[Dict[str, str]]:
    if not text:
        return []

    refs: List[Dict[str, str]] = []
    if language == "zh":
        # Capture the target after '见' or '另见' including comma-separated phrases
        # (stop at semicolon or newline). Previously this stopped at the first
        # comma which made only the first token clickable; capturing the whole
        # phrase allows titles like '修补术,疝,腹股沟的 / Repair, hernia, inguinal' to
        # be treated as a single reference target.
        pattern = re.compile(r"(?:[-（(]?\s*)(另见|见)\s*([^；;\n]+)", flags=re.IGNORECASE)
        for match in pattern.finditer(text):
            target = match.group(2).strip()
            if target:
                refs.append({"kind": match.group(1), "target": target})
    else:
        # Likewise capture the English 'see' target including comma-separated
        # terms until a semicolon or end-of-line.
        pattern = re.compile(r"\b(see also|see)\s+([^;\n]+)", flags=re.IGNORECASE)
        for match in pattern.finditer(text):
            target = match.group(2).strip()
            if target:
                refs.append({"kind": match.group(1).lower(), "target": target})
    return refs


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

    for row in ordered:
        level = parse_int(row.get("level", 0), 0)
        if level < 0:
            level = 0

        node = {
            "id": row.get("id", f"{row.get('page', 0)}-{level}-{row.get('source_file', '')}-{row.get('source_line', 0)}"),
            "page": row.get("page", 0),
            "level": level,
            "code": row.get("code", ""),
            "chinese": row.get("chinese", ""),
            "english": row.get("english", ""),
            "source_file": row.get("source_file", ""),
            "source_line": row.get("source_line", 0),
            "references": extract_references(normalize_text(row.get("chinese", "")), "zh")
            + extract_references(normalize_text(row.get("english", "")), "en"),
            "has_children": row.get("has_children", False),
            "matched": row.get("matched", False),
            "children": [],
        }

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


def collect_relevant_rows(results: pd.DataFrame, all_rows: pd.DataFrame) -> List[Dict[str, Any]]:
    matched_indices = set(results.index)
    ancestor_indices = set()

    for result_idx, result_row in results.iterrows():
        row_index = int(result_idx)
        current_level = parse_int(result_row.get("level", 0), 0)
        search_idx = row_index
        while search_idx > 0 and current_level > 0:
            search_idx -= 1
            prior_level = parse_int(all_rows.at[search_idx, "level"], 0)
            if prior_level < current_level:
                if prior_level > 0:
                    ancestor_indices.add(search_idx)
                current_level = prior_level

    combined_indices = sorted(matched_indices | ancestor_indices)
    return [row_to_json(all_rows.loc[index], matched=(index in matched_indices), has_children=has_descendants(index, all_rows)) for index in combined_indices]


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

    if not query_text:
        browse_rows = df[df["level"] == 0].copy()
        result_rows = [row_to_json(row, has_children=has_descendants(int(idx), df)) for idx, row in browse_rows.iterrows()]
        return result_rows, result_rows

    is_code_query = bool(re.fullmatch(r"\d+(?:\.\d+)?", query_text))
    query_lower = query_text.lower()
    results: pd.DataFrame
    code_series = df["code"].astype(str).str.lower()

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
        text_values = df[fields].astype(str).fillna("").agg(" ".join, axis=1).str.lower()
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

    rows, tree_rows = search_dataframe(
        query=query,
        mode=mode,
        fields=fields,
        page_min=page_min,
        page_max=page_max,
        level_min=level_min,
        level_max=level_max,
        file_filters=file_filters,
    )

    tree = build_hierarchy(tree_rows)
    return jsonify(
        {
            "query": query,
            "count": len(rows),
            "rows": rows,
            "tree": tree,
        }
    )


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
        results = df[df["code"].astype(str).str.lower() == target_lower].copy()
    else:
        # Exact text match against chinese or english columns first
        mask_cn = df["chinese"].astype(str).fillna("").str.strip().str.lower() == target_lower
        mask_en = df["english"].astype(str).fillna("").str.strip().str.lower() == target_lower
        results = df[mask_cn | mask_en].copy()

        # If not found, try exact-leading-token match (e.g., reference 'Buckling' -> 'Buckling, scleral')
        if results.empty:
            starts_cn = df["chinese"].astype(str).fillna("").str.strip().str.lower().str.startswith(target_lower)
            starts_en = df["english"].astype(str).fillna("").str.strip().str.lower().str.startswith(target_lower)
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
                    results = df[df["code"].astype(str).str.lower() == code].copy()

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

    df = load_dataset()
    try:
        start_index = find_row_index_by_id(node_id, df)
    except ValueError:
        return jsonify({"children": []})

    descendants = collect_descendant_rows(start_index, df, query=query, mode=mode, fields=fields)
    children = build_hierarchy(descendants)
    return jsonify({"children": children})


def find_row_index_by_id(node_id: str, all_rows: pd.DataFrame) -> int:
    for index, row in all_rows.iterrows():
        if get_row_id(row) == node_id:
            return int(index)
    raise ValueError(f"Node not found: {node_id}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True, use_reloader=True)
