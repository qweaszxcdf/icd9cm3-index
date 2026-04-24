from __future__ import annotations

import base64
import csv
import io
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

APP_TITLE = "ICD 提取人工校对工具"
DATA_COLUMNS = ["page", "level", "chinese", "english", "code"]
INTERNAL_COLUMNS = ["_uid", "_source_file", "_source_line", "_order", "_deleted", "_is_new"]
CSV_NAME_PATTERN = re.compile(r"icd-index-extraction-(\d+)-(\d+)\.csv$")

ROOT_DIR = Path(__file__).resolve().parent
TARGET_PDF_PATH = ROOT_DIR / "target.pdf"


def get_csv_file_signatures(root_dir: str) -> Tuple[Tuple[str, float], ...]:
    root = Path(root_dir)
    signatures = []
    for path in sorted(root.glob("icd-index-extraction-*.csv")):
        match = CSV_NAME_PATTERN.match(path.name)
        if not match:
            continue
        try:
            signatures.append((path.name, path.stat().st_mtime))
        except OSError:
            continue
    return tuple(signatures)


@st.cache_data(show_spinner=False)
def discover_batch_files(root_dir: str, file_signatures: Tuple[Tuple[str, float], ...]) -> List[Dict[str, object]]:
    root = Path(root_dir)
    batch_files: List[Dict[str, object]] = []

    for name, _ in file_signatures:
        path = root / name
        if not path.exists():
            continue

        match = CSV_NAME_PATTERN.match(name)
        if not match:
            continue

        start_page, end_page = int(match.group(1)), int(match.group(2))

        # Skip wide aggregate files such as 419-956 to avoid duplicated pages.
        if end_page - start_page > 4:
            continue

        batch_files.append(
            {
                "name": name,
                "path": str(path),
                "start": start_page,
                "end": end_page,
            }
        )

    batch_files.sort(key=lambda item: (item["start"], item["end"]))
    return batch_files


def normalize_row(raw_row: List[str]) -> List[str]:
    row = [str(value).strip() for value in raw_row]
    if len(row) < 5:
        row.extend([""] * (5 - len(row)))
    return row[:5]


def parse_int(value: object, default: int) -> int:
    try:
        if value is None:
            return default
        text = str(value).strip()
        if text == "":
            return default
        return int(float(text))
    except (TypeError, ValueError):
        return default


def parse_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.lower() == "nan":
        return ""
    return text.strip()


def strip_hierarchy_indent(value: str) -> str:
    return parse_text(value).lstrip("\u3000").lstrip(" ")


@st.cache_data(show_spinner=False)
def load_dataset(root_dir: str, file_signatures: Tuple[Tuple[str, float], ...]) -> Tuple[pd.DataFrame, List[Dict[str, object]]]:
    batch_files = discover_batch_files(root_dir, file_signatures)
    rows: List[Dict[str, object]] = []

    for batch_info in batch_files:
        path = Path(str(batch_info["path"]))
        with path.open("r", encoding="utf-8", newline="") as fp:
            reader = csv.reader(fp)
            for line_no, raw_row in enumerate(reader, start=1):
                if not raw_row:
                    continue

                row = normalize_row(raw_row)

                # Optional header row support.
                if line_no == 1 and row[0].lower() == "page":
                    continue

                page = parse_int(row[0], -1)
                if page < 0:
                    continue

                level = parse_int(row[1], 0)
                chinese, english, code = row[2], row[3], row[4]

                rows.append(
                    {
                        "_uid": uuid.uuid4().hex,
                        "page": page,
                        "level": level,
                        "chinese": chinese,
                        "english": english,
                        "code": code,
                        "_source_file": str(batch_info["name"]),
                        "_source_line": line_no,
                        "_order": line_no,
                        "_deleted": False,
                        "_is_new": False,
                    }
                )

    if not rows:
        columns = DATA_COLUMNS + INTERNAL_COLUMNS
        return pd.DataFrame(columns=columns), batch_files

    df = pd.DataFrame(rows)
    df["page"] = df["page"].astype(int)
    df["level"] = df["level"].astype(int)
    df["_order"] = df["_order"].astype(float)
    df["_deleted"] = df["_deleted"].astype(bool)
    df["_is_new"] = df["_is_new"].astype(bool)

    return df, batch_files


def build_page_to_file_map(batch_files: List[Dict[str, object]]) -> Dict[int, str]:
    mapping: Dict[int, str] = {}
    for info in batch_files:
        name = str(info["name"])
        start = int(info["start"])
        end = int(info["end"])
        for page in range(start, end + 1):
            mapping[page] = name
    return mapping


@st.cache_data(show_spinner=False)
def load_pdf_base64(pdf_path: str) -> str:
    path = Path(pdf_path)
    if not path.exists():
        return ""
    return base64.b64encode(path.read_bytes()).decode("utf-8")


@st.cache_data(show_spinner=False)
def render_pdf_page_image(pdf_path: str, page: int, scale: float = 5.0) -> Optional[bytes]:
    path = Path(pdf_path)
    if not path.exists() or page < 1:
        return None

    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None

    try:
        document = pdfium.PdfDocument(str(path))
        page_count = len(document)
        if page > page_count:
            return None

        pdf_page = document[page - 1]
        bitmap = pdf_page.render(scale=scale)
        image = bitmap.to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        return None


def init_state() -> None:
    if st.session_state.get("proofread_initialized"):
        return

    file_signatures = get_csv_file_signatures(str(ROOT_DIR))
    data_df, batch_files = load_dataset(str(ROOT_DIR), file_signatures)

    st.session_state.data_df = data_df
    st.session_state.batch_files = batch_files
    st.session_state.page_to_file = build_page_to_file_map(batch_files)
    st.session_state.file_lookup = {str(item["name"]): item for item in batch_files}
    st.session_state.modified_files = set()
    st.session_state.editor_nonce = 0
    st.session_state.editor_row_uids = {}

    page_candidates = sorted({int(p) for p in data_df["page"].dropna().tolist()})
    if page_candidates:
        st.session_state.current_page = page_candidates[0]
    else:
        st.session_state.current_page = 1

    st.session_state.last_action_message = ""
    st.session_state.proofread_initialized = True


def reset_from_disk() -> None:
    st.cache_data.clear()
    for key in [
        "data_df",
        "batch_files",
        "page_to_file",
        "file_lookup",
        "modified_files",
        "editor_nonce",
        "current_page",
        "last_action_message",
        "proofread_initialized",
    ]:
        if key in st.session_state:
            del st.session_state[key]
    init_state()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def resolve_source_file(page: int) -> str:
    mapping: Dict[int, str] = st.session_state.page_to_file
    return mapping.get(page, "")


def csv_quote_text(value: str) -> str:
    escaped = str(value).replace('"', '""')
    return f'"{escaped}"'


def csv_escape_if_needed(value: str) -> str:
    text = str(value)
    if any(ch in text for ch in [',', '"', '\n', '\r']):
        escaped = text.replace('"', '""')
        return f'"{escaped}"'
    return text


def save_source_file(file_name: str) -> None:
    file_lookup: Dict[str, Dict[str, object]] = st.session_state.file_lookup
    if file_name not in file_lookup:
        return

    info = file_lookup[file_name]
    source_path = Path(str(info["path"]))
    start_page = int(info["start"])
    end_page = int(info["end"])
    data_df: pd.DataFrame = st.session_state.data_df

    subset = data_df[
        (~data_df["_deleted"])
        & (data_df["page"] >= start_page)
        & (data_df["page"] <= end_page)
    ].copy()
    subset = subset.sort_values(by=["page", "_order", "_uid"], ascending=[True, True, True])

    with source_path.open("w", encoding="utf-8", newline="") as fp:
        fp.write(",".join(DATA_COLUMNS) + "\n")
        for _, row in subset.iterrows():
            page_value = str(parse_int(row["page"], 0))
            level_value = str(parse_int(row["level"], 0))
            chinese_value = csv_quote_text(parse_text(row["chinese"]))
            english_value = csv_quote_text(parse_text(row["english"]))
            code_value = csv_escape_if_needed(parse_text(row["code"]))
            fp.write(",".join([page_value, level_value, chinese_value, english_value, code_value]) + "\n")


def get_active_page_df(page: int) -> pd.DataFrame:
    data_df: pd.DataFrame = st.session_state.data_df
    page_df = data_df[(~data_df["_deleted"]) & (data_df["page"] == page)].copy()
    if page_df.empty:
        return page_df
    return page_df.sort_values(by=["_order", "_uid"], ascending=[True, True])


def get_next_order(page: int) -> float:
    data_df: pd.DataFrame = st.session_state.data_df
    subset = data_df[(~data_df["_deleted"]) & (data_df["page"] == page)]
    if subset.empty:
        return 1.0
    return float(subset["_order"].max()) + 1.0


def resequence_page(page: int) -> None:
    data_df: pd.DataFrame = st.session_state.data_df
    idxs = data_df.index[(~data_df["_deleted"]) & (data_df["page"] == page)].tolist()
    if not idxs:
        return

    idxs.sort(key=lambda idx: (float(data_df.at[idx, "_order"]), str(data_df.at[idx, "_uid"])))
    for order, idx in enumerate(idxs, start=1):
        data_df.at[idx, "_order"] = float(order)


def swap_page_row_order(page: int, position: int, direction: int) -> None:
    data_df: pd.DataFrame = st.session_state.data_df
    page_df = get_active_page_df(page)
    if page_df.empty:
        return

    sorted_idx = list(page_df.sort_values(by=["_order", "_uid"], ascending=[True, True]).index)
    if position < 1 or position > len(sorted_idx):
        return

    target_position = position + direction
    if target_position < 1 or target_position > len(sorted_idx):
        return

    current_idx = sorted_idx[position - 1]
    target_idx = sorted_idx[target_position - 1]
    current_order = float(data_df.at[current_idx, "_order"])
    target_order = float(data_df.at[target_idx, "_order"])

    data_df.at[current_idx, "_order"] = target_order
    data_df.at[target_idx, "_order"] = current_order
    resequence_page(page)
    st.session_state.data_df = data_df


def add_row_below(page: int, position: int) -> None:
    data_df: pd.DataFrame = st.session_state.data_df
    page_df = get_active_page_df(page)
    if page_df.empty:
        return

    sorted_idx = list(page_df.sort_values(by=["_order", "_uid"], ascending=[True, True]).index)
    if position < 1 or position > len(sorted_idx):
        return

    selected_idx = sorted_idx[position - 1]
    selected_order = float(data_df.at[selected_idx, "_order"])

    for idx in sorted_idx[position:]:
        data_df.at[idx, "_order"] = float(data_df.at[idx, "_order"]) + 1.0

    new_row = {
        "_uid": uuid.uuid4().hex,
        "page": page,
        "level": 0,
        "chinese": "",
        "english": "",
        "code": "",
        "_source_file": resolve_source_file(page),
        "_source_line": -1,
        "_order": selected_order + 1.0,
        "_deleted": False,
        "_is_new": True,
    }
    data_df.loc[len(data_df)] = new_row
    resequence_page(page)
    st.session_state.data_df = data_df


def apply_page_edits(current_page: int, edited_df: pd.DataFrame, row_uid_map: Optional[List[str]] = None) -> None:
    data_df: pd.DataFrame = st.session_state.data_df

    if "_uid" not in edited_df.columns and row_uid_map is not None:
        edited_df = edited_df.reset_index(drop=True)
        uid_values = [str(uid) for uid in row_uid_map]
        uid_values = uid_values[: len(edited_df)] + [""] * max(0, len(edited_df) - len(uid_values))
        edited_df["_uid"] = uid_values

    uid_to_idx = {str(uid): idx for idx, uid in data_df["_uid"].items()}

    existing_ids = set(
        data_df.loc[(~data_df["_deleted"]) & (data_df["page"] == current_page), "_uid"].astype(str).tolist()
    )

    touched_pages = {current_page}
    seen_ids = set()

    normalized = edited_df.copy()
    for col in ["_uid", "_order"] + DATA_COLUMNS:
        if col not in normalized.columns:
            normalized[col] = ""

    for position, row in normalized.iterrows():
        uid = parse_text(row.get("_uid", ""))
        page = parse_int(row.get("page", current_page), current_page)
        level = parse_int(row.get("level", 0), 0)
        order_value = parse_int(row.get("_order", position + 1), position + 1)
        chinese = strip_hierarchy_indent(parse_text(row.get("chinese", "")))
        english = parse_text(row.get("english", ""))
        code = parse_text(row.get("code", ""))

        if uid == "" and chinese == "" and english == "" and code == "":
            continue

        target_source_file = resolve_source_file(page)

        if uid and uid in uid_to_idx:
            idx = uid_to_idx[uid]
            seen_ids.add(uid)
            old_page = int(data_df.at[idx, "page"])
            old_source_file = parse_text(data_df.at[idx, "_source_file"])

            field_updates = {
                "page": page,
                "level": level,
                "chinese": chinese,
                "english": english,
                "code": code,
                "_order": order_value,
            }

            for field_name, new_value in field_updates.items():
                old_value = data_df.at[idx, field_name]
                if str(old_value) != str(new_value):
                    data_df.at[idx, field_name] = new_value

            if old_source_file != target_source_file and target_source_file:
                data_df.at[idx, "_source_file"] = target_source_file

            if old_source_file:
                st.session_state.modified_files.add(old_source_file)
            if target_source_file:
                st.session_state.modified_files.add(target_source_file)

            if page != current_page:
                data_df.at[idx, "_order"] = float(order_value) if order_value > 0 else get_next_order(page)

        else:
            new_uid = uuid.uuid4().hex
            source_file = target_source_file
            order_value_float = float(order_value) if order_value > 0 else (float(position + 1) if page == current_page else get_next_order(page))

            new_row = {
                "_uid": new_uid,
                "page": page,
                "level": level,
                "chinese": chinese,
                "english": english,
                "code": code,
                "_source_file": source_file,
                "_source_line": -1,
                "_order": order_value_float,
                "_deleted": False,
                "_is_new": True,
            }

            data_df.loc[len(data_df)] = new_row
            seen_ids.add(new_uid)
            touched_pages.add(page)

            if source_file:
                st.session_state.modified_files.add(source_file)

    deleted_ids = existing_ids - seen_ids
    for uid in deleted_ids:
        idx = uid_to_idx.get(uid)
        if idx is None:
            continue
        if bool(data_df.at[idx, "_deleted"]):
            continue

        old_source_file = parse_text(data_df.at[idx, "_source_file"])
        old_page = int(data_df.at[idx, "page"])

        data_df.at[idx, "_deleted"] = True

        if old_source_file:
            st.session_state.modified_files.add(old_source_file)
        touched_pages.add(old_page)

    for page in touched_pages:
        resequence_page(page)

    st.session_state.data_df = data_df

    saved_files = sorted(st.session_state.modified_files)
    for file_name in saved_files:
        save_source_file(file_name)

    saved_count = len(saved_files)
    st.session_state.modified_files = set()
    st.session_state.last_action_message = (
        f"已应用本页修改并保存至原文件。共保存 {saved_count} 个文件。"
    )


def detect_header_hint(page_df: pd.DataFrame) -> Optional[str]:
    if page_df.empty:
        return None

    try:
        has_level_zero = bool((page_df["level"].astype(int) == 0).any())
    except ValueError:
        has_level_zero = False

    if has_level_zero:
        return None

    english_series = page_df["english"].fillna("").astype(str).str.strip()
    english_series = english_series[english_series != ""]
    if english_series.empty:
        return None

    first_chars = english_series.str[0].str.upper()
    alpha = first_chars[first_chars.str.match(r"[A-Z]")]
    if len(alpha) < 3:
        return None

    if alpha.nunique() == 1:
        return str(alpha.iloc[0])

    return None


def build_tree_preview(page_df: pd.DataFrame) -> pd.DataFrame:
    if page_df.empty:
        return pd.DataFrame(columns=["level", "hierarchy", "english", "code"])

    preview = page_df[["level", "chinese", "english", "code"]].copy()

    def to_hierarchy(row: pd.Series) -> str:
        level = parse_int(row["level"], 0)
        chinese = parse_text(row["chinese"])
        indent = "\u3000" * max(level, 0)
        return f"{indent}{chinese}"

    preview["hierarchy"] = preview.apply(to_hierarchy, axis=1)
    preview = preview[["level", "hierarchy", "english", "code"]]
    return preview


def render_hierarchical_editor(page_df: pd.DataFrame, current_page: int) -> None:
    if page_df.empty:
        st.info("本页没有提取到内容。")
        return

    st.write("按层级结构预览并直接编辑当前页条目。")
    st.markdown(
        """
        <style>
        section[data-testid="stForm"] .stTextInput, section[data-testid="stForm"] .stNumberInput {
            margin-top: 0.1rem;
            margin-bottom: 0.1rem;
        }
        section[data-testid="stForm"] .stTextInput > div > div,
        section[data-testid="stForm"] .stNumberInput > div > div {
            padding-top: 0.1rem;
            padding-bottom: 0.1rem;
        }
        section[data-testid="stForm"] .stMarkdown > div {
            margin-top: 0.1rem;
            margin-bottom: 0.1rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    edited_rows: List[Dict[str, object]] = []

    with st.form(key=f"hierarchy_editor_{current_page}"):
        header_cols = st.columns([0.7, 4, 4, 2])
        header_cols[0].markdown("**Level**")
        header_cols[1].markdown("**中文**")
        header_cols[2].markdown("**English**")
        header_cols[3].markdown("**Code**")

        for row_idx, row in enumerate(
            page_df[["_uid", "page", "level", "chinese", "english", "code"]].itertuples(index=False, name=None),
            start=1,
        ):
            uid = parse_text(row[0])
            level = parse_int(row[2], 0)
            chinese = parse_text(row[3])
            english = parse_text(row[4])
            code = parse_text(row[5])

            indent = "\u3000" * max(level, 0)
            cols = st.columns([1, 4, 4, 2])
            level_input = cols[0].number_input(
                label=f"Level {row_idx}",
                value=level,
                min_value=0,
                step=1,
                key=f"hierarchy_level_{uid}",
                label_visibility="collapsed",
            )

            display_chinese = f"{indent}{chinese}"
            chinese_input = cols[1].text_input(
                label=f"中文 {row_idx}",
                value=display_chinese,
                key=f"hierarchy_chinese_{uid}",
                label_visibility="collapsed",
            )
            chinese_input = chinese_input.lstrip("\u3000").lstrip(" ")
            english_input = cols[2].text_input(
                label=f"English {row_idx}",
                value=english,
                key=f"hierarchy_english_{uid}",
                label_visibility="collapsed",
            )
            code_input = cols[3].text_input(
                label=f"Code {row_idx}",
                value=code,
                key=f"hierarchy_code_{uid}",
                label_visibility="collapsed",
            )
            edited_rows.append(
                {
                    "_uid": uid,
                    "page": current_page,
                    "level": int(level_input),
                    "chinese": chinese_input,
                    "english": english_input,
                    "code": code_input,
                }
            )

        submit_pressed = st.form_submit_button("保存层级编辑")
        if submit_pressed:
            edited_df = pd.DataFrame(edited_rows)
            apply_page_edits(current_page=current_page, edited_df=edited_df)
            st.rerun()




def render_sidebar(page_list: List[int]) -> None:
    st.sidebar.header("控制面板")

    if st.sidebar.button("从磁盘重新加载（丢弃未保存）", width="stretch"):
        reset_from_disk()
        st.rerun()

    min_page = min(page_list) if page_list else 1
    max_page = max(page_list) if page_list else 1

    current_page = int(st.session_state.current_page)
    if current_page < min_page:
        current_page = min_page
    if current_page > max_page:
        current_page = max_page

    selected_page = st.sidebar.number_input(
        "当前页码",
        min_value=min_page,
        max_value=max_page,
        value=current_page,
        step=1,
        help="与左侧图像及右侧表格联动。",
    )
    st.session_state.current_page = int(selected_page)

    st.sidebar.caption("快速定位")
    jump_page = st.sidebar.selectbox(
        "跳转到有内容的页",
        options=page_list if page_list else [st.session_state.current_page],
        index=(page_list.index(st.session_state.current_page) if page_list and st.session_state.current_page in page_list else 0),
    )
    if jump_page != st.session_state.current_page:
        st.session_state.current_page = int(jump_page)
        st.rerun()

    st.sidebar.divider()


def render_page_navigation(page_list: List[int]) -> None:
    current_page = int(st.session_state.current_page)
    min_page = min(page_list) if page_list else 1
    max_page = max(page_list) if page_list else 1

    prev_col, cur_col, next_col = st.columns([1, 2, 1])
    with prev_col:
        if st.button("上一页", width="stretch", disabled=current_page <= min_page):
            st.session_state.current_page = current_page - 1
            st.rerun()
    with cur_col:
        st.markdown(f"### 当前页：{current_page}")
    with next_col:
        if st.button("下一页", width="stretch", disabled=current_page >= max_page):
            st.session_state.current_page = current_page + 1
            st.rerun()


def render_main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption("左侧查看页面图像，右侧编辑提取结果；支持新增/删除/修改并直接保存至原文件。")

    init_state()

    data_df: pd.DataFrame = st.session_state.data_df
    page_list = sorted({int(p) for p in data_df["page"].dropna().tolist()})

    render_sidebar(page_list)
    render_page_navigation(page_list)

    if st.session_state.last_action_message:
        st.info(st.session_state.last_action_message)

    current_page = int(st.session_state.current_page)
    page_df = get_active_page_df(current_page)

    hint = detect_header_hint(page_df)
    if hint:
        st.warning(f"该页疑似缺少 0 级字母标题：{hint}。请人工核对后决定是否补入 `page,0,\"{hint}\",\"{hint}\",`。")

    left_col, right_col = st.columns([1.05, 1.4], gap="large")

    with left_col:
        st.subheader("PDF 原文")
        page_image_bytes = render_pdf_page_image(str(TARGET_PDF_PATH), current_page)
        if page_image_bytes is None:
            st.error(f"无法从 PDF 渲染页码 {current_page}：{TARGET_PDF_PATH}")
        else:
            st.image(
                page_image_bytes,
                caption=f"Page {current_page}",
                width="stretch",
            )

    with right_col:
        st.subheader("提取结果（可编辑）")

        if page_df.empty:
            editor_input = pd.DataFrame(
                [
                    {
                        "page": current_page,
                        "level": 0,
                        "_order": 1,
                        "chinese": "",
                        "english": "",
                        "code": "",
                    }
                ]
            )
            row_uid_map: List[str] = []
        else:
            editor_input = page_df[["_order"] + DATA_COLUMNS].copy()
            editor_input["chinese"] = page_df.apply(
                lambda row: f"{chr(0x3000) * max(parse_int(row['level'], 0), 0)}{parse_text(row['chinese'])}",
                axis=1,
            )
            row_uid_map = page_df["_uid"].astype(str).tolist()

        st.session_state.editor_row_uids[current_page] = row_uid_map

        editor_input = editor_input.reset_index(drop=True)
        editor_input.index = range(1, len(editor_input) + 1)

        st.markdown(
            """
            <style>
            [data-testid="stDataFrame"] {
                min-height: calc(100vh - 260px) !important;
                height: calc(100vh - 260px) !important;
            }
            [data-testid="stDataFrameGlideDataEditor"] {
                min-height: 100% !important;
                height: 100% !important;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        editor_key = f"page_editor_{st.session_state.editor_nonce}_{current_page}"
        edited_df = st.data_editor(
            editor_input,
            key=editor_key,
            hide_index=False,
            width="stretch",
            height=900,
            num_rows="dynamic",
            column_order=["page", "level", "_order", "chinese", "english", "code"],
            column_config={
                "page": st.column_config.NumberColumn("page", step=1, format="%d", width="small"),
                "level": st.column_config.NumberColumn("level", step=1, format="%d", width="small"),
                "_order": st.column_config.NumberColumn("order", step=1, format="%d", width="small"),
                "chinese": st.column_config.TextColumn("chinese", width="large"),
                "english": st.column_config.TextColumn("english", width="large"),
                "code": st.column_config.TextColumn("code", width="small"),
            },
        )

        st.info(
            "当前 Streamlit 版本不支持在表格中直接拖拽排序。请编辑“顺序”列或使用下面的行移动按钮来调整条目顺序。"
        )

        if not page_df.empty:
            row_options = [
                f"{idx + 1}. {parse_text(row['chinese'])[:60]}"
                for idx, row in enumerate(page_df.sort_values(by=["_order", "_uid"], ascending=[True, True]).to_dict(orient="records"))
            ]
            selected_row_index = st.selectbox(
                "选择要移动的行",
                options=list(range(1, len(row_options) + 1)),
                format_func=lambda i: row_options[i - 1],
                key=f"row_move_select_{current_page}",
            )

            move_col1, move_col2, move_col3 = st.columns([1, 1, 1])
            with move_col1:
                if st.button("上移一行", key=f"move_up_{current_page}"):
                    swap_page_row_order(current_page, selected_row_index, -1)
                    st.session_state.editor_nonce += 1
                    st.rerun()
            with move_col2:
                if st.button("下移一行", key=f"move_down_{current_page}"):
                    swap_page_row_order(current_page, selected_row_index, 1)
                    st.session_state.editor_nonce += 1
                    st.rerun()
            with move_col3:
                if st.button("在下方插入空行", key=f"insert_below_{current_page}"):
                    add_row_below(current_page, selected_row_index)
                    st.session_state.editor_nonce += 1
                    st.rerun()

        action_col1, action_col2 = st.columns([1, 1])
        with action_col1:
            if st.button("应用本页修改", type="primary", width="stretch"):
                apply_page_edits(
                    current_page=current_page,
                    edited_df=edited_df,
                    row_uid_map=st.session_state.editor_row_uids.get(current_page, []),
                )
                st.session_state.editor_nonce += 1
                st.rerun()
        with action_col2:
            if st.button("放弃本页未应用修改", width="stretch"):
                st.session_state.editor_nonce += 1
                st.session_state.last_action_message = "已放弃当前页编辑器中的临时修改。"


if __name__ == "__main__":
    render_main()
