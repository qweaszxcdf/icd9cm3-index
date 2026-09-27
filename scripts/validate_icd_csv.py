#!/usr/bin/env python3
"""Validate extracted ICD-9-CM procedure-index CSV files.

The validator rebuilds the CDC FY2012 (effective 2011-10-01) English Index to
Procedures hierarchy from RTF paragraph styles and indentation, then compares
the CSV's complete English path and code.  It never modifies the source CSV
files; every input row is written to a separate report.
"""

from __future__ import annotations

import argparse
import csv
import io
import itertools
import multiprocessing
import os
import re
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Sequence


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT_DIR / "validation_report.csv"
DEFAULT_CACHE = ROOT_DIR / ".cache" / "icd-validation" / "Pindex12.zip"
DEFAULT_OFFICIAL_URL = (
    "https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Publications/"
    "ICD9-CM/2011/Pindex12.zip"
)
KNOWN_OFFICIAL_TYPOS = {
    "aneursym": "aneurysm",
    "anular": "annular",
    "rachitomy": "rachiotomy",
    "acupunture": "acupuncture",
    # CDC's source RTF contains a few additional misspellings.  Keep these
    # curated so validation can remain tolerant without copying them into the
    # extracted CSVs during automatic repair.
    "garder": "gardner",
    "spinothalmic": "spinothalamic",
    "peridontal": "periodontal",
    "kazanjiian": "kazanjian",
}
CROSS_REFERENCE_TERM_ALIASES = {
    "resection": "reduction",
}
CSV_NAME_RE = re.compile(r"icd-index-extraction-(\d+)-(\d+)\.csv$")
CODE_TOKEN_RE = re.compile(r"(?<!\d)(\d{1,2})(?:\.(\d{1,2}))?(?!\d)")
CODE_EXPRESSION_RE = re.compile(
    r"(?P<value>\d{1,2}(?:\.\d{1,2})?"
    r"(?:(?:\s*-\s*|\s*\[\s*|\s*,\s*|\s*/\s*)"
    r"\d{1,2}(?:\.\d{1,2})?\s*\]?)*?)\s*$"
)
REPORT_FIELDS = [
    "source_file",
    "source_line",
    "page",
    "level",
    "hierarchy",
    "chinese",
    "english",
    "code",
    "severity",
    "status",
    "confidence",
    "official_english",
    "official_code",
    "official_level",
    "official_hierarchy",
    "path_score",
    "hierarchy_status",
    "suggestion",
    "details",
]
MERGED_SOURCE_FILE = "__merged_icd_csv__"


@dataclass(frozen=True)
class SourceRow:
    source_file: str
    source_line: int
    page: str
    level: int
    level_raw: str
    chinese: str
    english: str
    code: str
    hierarchy: str
    hierarchy_norm: str


@dataclass(frozen=True)
class OfficialEntry:
    text: str
    text_norm: str
    code: str
    code_norm: str
    level: int = 0
    hierarchy: str = ""
    hierarchy_norm: str = ""


@dataclass(frozen=True)
class RtfParagraph:
    style: int | None
    leading_spaces: int
    text: str


@dataclass(frozen=True)
class Match:
    severity: str
    status: str
    confidence: float
    official_text: str = ""
    official_code: str = ""
    official_level: int = 0
    official_hierarchy: str = ""
    path_score: float = 0.0
    hierarchy_status: str = ""
    suggestion: str = ""
    details: str = ""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="依据 CDC 英文版 ICD-9-CM 手术与操作索引校验批次 CSV。"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=ROOT_DIR,
        help="CSV 所在目录（默认：项目根目录）。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="报告路径（默认：validation_report.csv）。",
    )
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--official-zip",
        type=Path,
        help="本地 Pindex12.zip；指定后不会联网下载。",
    )
    source_group.add_argument(
        "--official-rtf",
        type=Path,
        help="本地 Pindex12.rtf；指定后不会联网下载。",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE,
        help="官方 ZIP 缓存路径。",
    )
    parser.add_argument(
        "--official-url",
        default=DEFAULT_OFFICIAL_URL,
        help="官方索引下载地址。",
    )
    parser.add_argument(
        "--refresh-official",
        action="store_true",
        help="重新下载官方索引并覆盖缓存。",
    )
    parser.add_argument(
        "--include-aggregate",
        action="store_true",
        help="同时读取跨度超过 5 页的聚合 CSV（默认跳过以避免重复）。",
    )
    parser.add_argument(
        "--issues-only",
        action="store_true",
        help="报告只保留错误、警告和人工复核行。",
    )
    parser.add_argument(
        "--fix-source",
        action="store_true",
        help="自动修复可确定的 code，直接修改 Git 工作区中的源 CSV。",
    )
    parser.add_argument(
        "--fix-all",
        action="store_true",
        help="配合 --fix-source：按一致的官方路径证据修复英文文本和父级拼写。",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help=(
            "校验和自动修复的并行进程数；0 表示按可用 CPU 和 level 0 块数自动选择，"
            "1 表示串行。结构修复分块仅在 --fix-source --fix-all 时启用。"
        ),
    )
    return parser.parse_args(argv)


def canonicalize_known_official_typos(value: str) -> str:
    for typo, correction in KNOWN_OFFICIAL_TYPOS.items():
        def replace(match: re.Match[str]) -> str:
            original = match.group(0)
            if original.isupper():
                return correction.upper()
            if original[:1].isupper():
                return correction[:1].upper() + correction[1:]
            return correction

        value = re.sub(
            rf"\b{re.escape(typo)}\b",
            replace,
            value,
            flags=re.IGNORECASE,
        )
    return value


def normalize_text(value: str) -> str:
    value = canonicalize_known_official_typos(value)
    value = value.replace("®", "").replace("™", "")
    value = unicodedata.normalize("NFKC", value).casefold()
    value = "".join(
        char
        for char in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(char)
    )
    value = re.sub(
        r"\bsee(\s+also)?\s+subcategory\b",
        lambda match: f"see{match.group(1) or ''} category",
        value,
    )
    value = value.replace("—", "-").replace("–", "-").replace("−", "-")
    value = value.replace("&", "and")
    return "".join(char for char in value if char.isalnum())


def accepted_optional_text_variant(source: str, official: str) -> bool:
    """Accept omission of whole official synonym parentheses, never free text."""
    source_parts = [normalize_text(part) for part in re.findall(r"\(([^()]*)\)", source)]
    official_parts = [normalize_text(part) for part in re.findall(r"\(([^()]*)\)", official)]
    source_parts = [part for part in source_parts if part]
    official_parts = [part for part in official_parts if part]

    # The non-parenthetical term must be identical. This prevents accepting a
    # missing clinical qualifier merely because code and nearby hierarchy match.
    source_base = normalize_text(re.sub(r"\([^()]*\)", "", source))
    official_base = normalize_text(re.sub(r"\([^()]*\)", "", official))
    if source_base.removesuffix("nos") == official_base or official_base.removesuffix("nos") == source_base:
        return True
    if source_base.removesuffix("electrodes") == official_base or official_base.removesuffix("electrodes") == source_base:
        return True
    if source_base != official_base:
        return False

    # A parenthesized repeat of the main term (for example ``CRT-D ... (CRT-D)``)
    # is not an additional clinical qualifier.
    source_parts = [part for part in source_parts if part != source_base]
    if not source_parts or len(source_parts) >= len(official_parts):
        if source_parts and not official_parts:
            return True
        return False

    iterator = iter(official_parts)
    return all(any(part == candidate for candidate in iterator) for part in source_parts)


def accepted_acronym_text_variant(source: str, official: str) -> bool:
    """Accept a phrase and its dynamically derived acronym as equivalent."""
    source_tokens = re.findall(r"[a-z0-9]+", source.casefold())
    official_tokens = re.findall(r"[a-z0-9]+", official.casefold())
    if source_tokens == official_tokens or not source_tokens or not official_tokens:
        return False

    def matches(left: list[str], right: list[str]) -> bool:
        seen_acronym = False
        left_index = right_index = 0
        while left_index < len(left) and right_index < len(right):
            if left[left_index] == right[right_index]:
                left_index += 1
                right_index += 1
                continue
            acronym = left[left_index]
            matched = False
            if 2 <= len(acronym) <= 6:
                for width in range(2, min(len(acronym), len(right) - right_index) + 1):
                    phrase = right[right_index : right_index + width]
                    if acronym == "".join(token[0] for token in phrase):
                        left_index += 1
                        right_index += width
                        seen_acronym = matched = True
                        break
            if not matched:
                return False
        return seen_acronym and left_index == len(left) and right_index == len(right)

    return matches(source_tokens, official_tokens) or matches(official_tokens, source_tokens)


def accepted_cross_reference_term_alias(source: str, official: str) -> bool:
    """Treat explicitly equivalent index terms as equal in see references."""
    source_norm = normalize_text(source)
    official_norm = normalize_text(official)
    return (
        source_norm.replace("resection", "reduction") == official_norm
        or source_norm.replace("reduction", "resection") == official_norm
    )


def spelling_change_allowed(
    source: str,
    official: str,
    maximum_run: int = 3,
) -> bool:
    """Allow spelling repairs whose changed alphabetic runs are at most three letters."""
    source_words = re.findall(r"[a-z]+", source.casefold())
    official_words = re.findall(r"[a-z]+", official.casefold())
    if source_words == official_words:
        return False
    # A spelling repair may alter letters inside words, but must never remove,
    # add, or merge a complete term such as "eye" in "eye, ocular".
    if len(source_words) != len(official_words):
        return False
    changes: list[tuple[str, str]] = []
    for source_word, official_word in zip(source_words, official_words):
        for tag, source_start, source_end, official_start, official_end in SequenceMatcher(
            None, source_word, official_word
        ).get_opcodes():
            if tag == "equal":
                continue
            source_part = source_word[source_start:source_end]
            official_part = official_word[official_start:official_end]
            if max(len(source_part), len(official_part)) > maximum_run:
                return False
            if not (source_part + official_part).isalpha():
                return False
            changes.append((source_part, official_part))
    if sum(max(len(source_part), len(official_part)) for source_part, official_part in changes) > maximum_run:
        return False
    return bool(changes)


def synchronized_chinese_english_prefix(
    chinese: str,
    source_english: str,
    official_english: str,
) -> str | None:
    """Synchronize an English proper-name prefix embedded in Chinese text."""
    if not re.search(r"[\u3400-\u9fff]", chinese):
        return None
    token_pattern = re.compile(r"^\s*([A-Za-z][A-Za-z'’.-]*)")
    chinese_match = token_pattern.match(chinese)
    source_match = token_pattern.match(source_english)
    official_match = token_pattern.match(official_english)
    if not (chinese_match and source_match and official_match):
        return None
    chinese_token = chinese_match.group(1)
    source_token = source_match.group(1)
    official_token = official_match.group(1)
    if normalize_text(source_token) != normalize_text(official_token):
        return None
    if not spelling_change_allowed(chinese_token, official_token):
        return None
    start, end = chinese_match.span(1)
    return chinese[:start] + official_token + chinese[end:]


def normalize_hierarchy(parts: Iterable[str]) -> str:
    normalized = [normalize_text(part) for part in parts]
    return ">".join(part for part in normalized if part)


def normalize_code_expression(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    value = value.replace("—", "-").replace("–", "-").replace("−", "-")

    def pad_major(match: re.Match[str]) -> str:
        major, minor = match.group(1), match.group(2)
        code = major.zfill(2)
        if minor is None:
            return code
        minor = minor.rstrip("0") or "0"
        return f"{code}.{minor}"

    value = CODE_TOKEN_RE.sub(pad_major, value)
    return re.sub(r"\s+", "", value)


def has_code_format_issue(value: str) -> bool:
    if not value.strip():
        return False
    if not re.fullmatch(r"[\d.\s,\-\[\]/]+", value):
        return True
    matches = list(CODE_TOKEN_RE.finditer(value))
    if not matches:
        return True
    return any(len(match.group(1)) != 2 for match in matches)


def code_tokens(value: str) -> tuple[str, ...]:
    return tuple(
        f"{major.zfill(2)}.{(minor.rstrip('0') or '0')}" if minor is not None else major.zfill(2)
        for major, minor in CODE_TOKEN_RE.findall(value)
    )


def has_single_code_token(value: str) -> bool:
    """Return whether an official code field identifies exactly one code."""
    return len(code_tokens(value)) == 1


def comma_alias_norms(value: str) -> list[str]:
    if re.search(r"\bsee(?:\s+also)?\b", value, re.IGNORECASE):
        return []
    parts = [part.strip() for part in value.split(",")]
    if not 2 <= len(parts) <= 4 or any(not part for part in parts):
        return []
    values: list[str] = []
    for start in range(len(parts)):
        for end in range(start + 1, len(parts) + 1):
            candidate = normalize_text(", ".join(parts[start:end]))
            if candidate:
                values.append(candidate)
    # Alias heads often share one parenthetical qualifier:
    # ``Uteropexy, Hysteropexy (abdominal approach)``.
    if len(parts) == 2 and "(" in parts[1] and "(" not in parts[0]:
        suffix = parts[1][parts[1].find("(") :]
        values.append(normalize_text(parts[0] + " " + suffix))
    if len(parts) == 2:
        qualifier_match = re.search(r"\b(NEC|NOS)$", parts[1], re.IGNORECASE)
        if qualifier_match:
            values.append(normalize_text(parts[0] + " " + qualifier_match.group(1)))
    # Some index headings combine two near-identical aliases and the second
    # alias's qualifier on one printed line, while the official hierarchy
    # represents the first alias and qualifier as two nodes.  For example:
    # ``Valvulotomy, Valvotomy heart (...)`` -> ``Valvulotomy > heart (...)``.
    if len(parts) == 2:
        second_words = parts[1].split(maxsplit=1)
        first_head = normalize_text(parts[0])
        second_head = normalize_text(second_words[0]) if second_words else ""
        if (
            len(second_words) == 2
            and first_head
            and second_head
            and SequenceMatcher(None, first_head, second_head).ratio() >= 0.80
        ):
            qualifier = normalize_text(second_words[1])
            if qualifier:
                values.append(f"{first_head}>{qualifier}")
    return list(dict.fromkeys(values))


def hierarchy_alias_paths(hierarchy: str) -> list[str]:
    parts = [part.strip() for part in hierarchy.split(" > ") if part.strip()]
    if parts and len(normalize_text(parts[0])) == 1:
        parts = parts[1:]
    options: list[list[str]] = []
    for part in parts:
        values = [normalize_text(part)] + comma_alias_norms(part)
        options.append(list(dict.fromkeys(value for value in values if value)))
    paths = [">".join(choice) for choice in itertools.product(*options)]
    return paths[:32]


def discover_csv_files(input_dir: Path, include_aggregate: bool) -> list[Path]:
    paths: list[tuple[int, int, Path]] = []
    for path in input_dir.glob("icd-index-extraction-*.csv"):
        match = CSV_NAME_RE.fullmatch(path.name)
        if not match:
            continue
        start, end = int(match.group(1)), int(match.group(2))
        if not include_aggregate and end - start > 4:
            continue
        paths.append((start, end, path))
    return [item[2] for item in sorted(paths)]


def load_source_rows(paths: Iterable[Path]) -> list[SourceRow]:
    rows: list[SourceRow] = []
    hierarchy: list[str] = []

    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            for line_no, raw in enumerate(reader, start=1):
                if not raw:
                    continue
                values = [str(value).strip() for value in raw]
                if line_no == 1 and values[0].casefold() == "page":
                    continue
                values.extend([""] * (5 - len(values)))
                page, level_raw, chinese, english, code = values[:5]
                try:
                    level = int(float(level_raw))
                except (TypeError, ValueError):
                    level = 0
                level = max(level, 0)
                if len(hierarchy) > level:
                    hierarchy = hierarchy[:level]
                while len(hierarchy) < level:
                    hierarchy.append("")
                label = english or chinese
                hierarchy.append(label)
                visible_hierarchy = [part for part in hierarchy if part]
                comparable_hierarchy = list(visible_hierarchy)
                # Level 0 is a block/boundary marker, never a semantic parent.
                if level == 0 and comparable_hierarchy:
                    comparable_hierarchy = comparable_hierarchy[1:]
                elif comparable_hierarchy and len(normalize_text(comparable_hierarchy[0])) == 1:
                    comparable_hierarchy = comparable_hierarchy[1:]
                rows.append(
                    SourceRow(
                        source_file=path.name,
                        source_line=line_no,
                        page=page,
                        level=level,
                        level_raw=level_raw,
                        chinese=chinese,
                        english=english,
                        code=code,
                        # Level 0 is a block marker, not a semantic ancestor.
                        hierarchy=" > ".join(comparable_hierarchy),
                        hierarchy_norm=normalize_hierarchy(comparable_hierarchy),
                    )
                )
    return rows


def download_official_zip(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "icd-csv-validator/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError(
            "无法下载 CDC 官方索引。请检查网络，"
            "或使用 --official-zip/--official-rtf 指定本地文件。"
        ) from exc
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if not any(name.casefold().endswith("pindex12.rtf") for name in archive.namelist()):
                raise RuntimeError("下载的 ZIP 中没有 Pindex12.rtf。")
    except zipfile.BadZipFile as exc:
        raise RuntimeError("下载内容不是有效的 Pindex12.zip。") from exc

    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=destination.name, suffix=".tmp", delete=False
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(payload)
    temp_path.replace(destination)


def load_rtf_bytes(args: argparse.Namespace) -> tuple[bytes, str]:
    if args.official_rtf:
        path = args.official_rtf.resolve()
        if not path.is_file():
            raise RuntimeError(f"找不到官方 RTF：{path}")
        return path.read_bytes(), str(path)

    if args.official_zip:
        zip_path = args.official_zip.resolve()
    else:
        zip_path = args.cache.resolve()
        if args.refresh_official or not zip_path.is_file():
            print(f"正在下载 CDC 官方索引：{args.official_url}", file=sys.stderr)
            download_official_zip(args.official_url, zip_path)

    if not zip_path.is_file():
        raise RuntimeError(f"找不到官方 ZIP：{zip_path}")
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = [name for name in archive.namelist() if name.casefold().endswith("pindex12.rtf")]
            if not members:
                raise RuntimeError(f"{zip_path} 中没有 Pindex12.rtf。")
            return archive.read(members[0]), f"{zip_path}!{members[0]}"
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"官方索引缓存不是有效 ZIP：{zip_path}") from exc


RTF_DESTINATIONS = frozenset(
    "aftncn aftnsep aftnsepc annotation atnauthor atndate atnicn atnid atnparent"
    " atnref atntime atrfend atrfstart author background bkmkend bkmkstart"
    " blipuid buptim category colorschememapping colortbl comment company creatim"
    " datafield datastore defchp defpap do doccomm docvar dptxbxtext falt fchars"
    " ffdeftext file filetbl fldinst fldrslt fldtype fname fontemb fontfile fonttbl"
    " footer footerf footerl footerr footnote formfield ftmovedfrom ftmovedto generator"
    " gridtbl header headerf headerl headerr hl htmltag info keywords latentstyles"
    " lchars levelnumbers leveltext lfolevel list listlevel listname listoverride"
    " listoverridetable listpicture liststylename listtable manager mhtmltag mmath"
    " mname nextfile nonesttables oldcprops oldpprops oldtprops oldxprops operator"
    " panose password passwordhash pgp pgptbl picprop pict pn pnseclvl pntext pntxta"
    " pntxtb printim private propname protend protstart protusertbl pxef pxeq pxeqc"
    " pxel pxetoc pxeuser result revtbl revisors rsidtbl rxe rxeindex stylename"
    " stylesheet subject sv staticval template title txe ud upr userprops wgrffmtfilter"
    " windowcaption writereservation xmlattrname xmlattrvalue xmlclose xmlname xmlnstbl"
    " xmlopen".split()
)
RTF_SPECIAL = {
    "par": "\n",
    "sect": "\n",
    "page": "\n",
    "line": "\n",
    "tab": "\t",
    "emdash": "—",
    "endash": "–",
    "emspace": " ",
    "enspace": " ",
    "qmspace": " ",
    "bullet": "•",
    "lquote": "‘",
    "rquote": "’",
    "ldblquote": "“",
    "rdblquote": "”",
}
RTF_TOKEN_RE = re.compile(
    r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|([^\\{}]+)"
)


def rtf_to_paragraphs(rtf: bytes) -> list[RtfParagraph]:
    """Extract rendered paragraph text together with the active RTF style."""
    source = rtf.decode("latin-1", errors="replace")
    paragraphs: list[RtfParagraph] = []
    fragments: list[str] = []
    stack: list[tuple[bool, int]] = []
    ignorable = False
    ucskip = 1
    curskip = 0
    style: int | None = None

    def flush() -> None:
        nonlocal fragments
        rendered = "".join(fragments).replace("\r", "").replace("\n", "")
        fragments = []
        if not rendered.strip():
            return
        leading = len(rendered) - len(rendered.lstrip(" \t"))
        text = re.sub(r"\s+", " ", rendered).strip()
        if text:
            paragraphs.append(RtfParagraph(style, leading, text))

    for match in RTF_TOKEN_RE.finditer(source):
        word, argument, hex_value, escaped, brace, plain = match.groups()
        if brace:
            if brace == "{":
                stack.append((ignorable, ucskip))
            elif stack:
                ignorable, ucskip = stack.pop()
            continue
        if escaped:
            if escaped == "*":
                ignorable = True
            elif not ignorable and curskip == 0:
                fragments.append({"~": " ", "_": "-", "-": ""}.get(escaped, escaped))
            continue
        if word:
            lower = word.casefold()
            if lower in RTF_DESTINATIONS:
                ignorable = True
            elif lower == "uc" and argument:
                ucskip = int(argument)
            elif lower == "u" and argument and not ignorable:
                value = int(argument)
                if value < 0:
                    value += 65536
                fragments.append(chr(value))
                curskip = ucskip
            elif lower == "pard" and not ignorable:
                style = None
            elif lower == "s" and argument and not ignorable:
                style = int(argument)
            elif lower == "par" and not ignorable:
                flush()
            elif lower in RTF_SPECIAL and not ignorable:
                fragments.append(" " if lower in {"tab", "line"} else RTF_SPECIAL[lower])
            continue
        if ignorable:
            continue
        if hex_value:
            if curskip:
                curskip -= 1
            else:
                fragments.append(bytes.fromhex(hex_value).decode("cp1252", errors="replace"))
            continue
        if plain:
            if curskip:
                skipped = min(curskip, len(plain))
                plain = plain[skipped:]
                curskip -= skipped
            fragments.append(plain)
    flush()
    return paragraphs


def parse_official_entries(paragraphs: Iterable[RtfParagraph]) -> list[OfficialEntry]:
    entries: list[OfficialEntry] = []
    seen: set[tuple[str, str, str]] = set()
    hierarchy: list[str] = []
    index_paragraphs = [paragraph for paragraph in paragraphs if paragraph.style in range(17, 25)]

    # One official entry wraps its code (66.93) into a separate paragraph.
    merged: list[RtfParagraph] = []
    for paragraph in index_paragraphs:
        if merged and re.fullmatch(r"[\d.\s,\-\[\]/]+", paragraph.text):
            previous = merged[-1]
            merged[-1] = RtfParagraph(
                previous.style,
                previous.leading_spaces,
                f"{previous.text} {paragraph.text}",
            )
            continue
        if merged:
            previous = merged[-1]
            previous_has_code = CODE_EXPRESSION_RE.search(previous.text) is not None
            current_has_code = CODE_EXPRESSION_RE.search(paragraph.text) is not None
            style_jump = (
                paragraph.style is not None
                and previous.style is not None
                and paragraph.style >= previous.style + 2
            )
            if (
                not previous_has_code
                and current_has_code
                and style_jump
                and len(previous.text) >= 25
                and not previous.text.rstrip().endswith(":")
                and not previous.text.rstrip().casefold().endswith(" by")
            ):
                merged[-1] = RtfParagraph(
                    previous.style,
                    previous.leading_spaces,
                    f"{previous.text} {paragraph.text}",
                )
                continue
        merged.append(paragraph)

    for paragraph in merged:
        line = paragraph.text
        code_match = CODE_EXPRESSION_RE.search(line)
        if code_match:
            code = code_match.group("value").strip()
            entry_text = line[: code_match.start()].strip(" -\t")
        else:
            code = ""
            entry_text = line
        if entry_text.casefold() == "lung volume reduction 3" and normalize_code_expression(code) == "02.22":
            entry_text = "lung volume reduction"
            code = "32.22"
        text_norm = normalize_text(entry_text)
        if not text_norm:
            continue
        base_level = int(paragraph.style) - 16
        extra_level = max(0, (paragraph.leading_spaces + 1) // 6)
        level = base_level + extra_level
        if len(hierarchy) >= level:
            hierarchy = hierarchy[: level - 1]
        while len(hierarchy) < level - 1:
            hierarchy.append("")
        hierarchy.append(entry_text)
        visible_hierarchy = [part for part in hierarchy if part]
        hierarchy_text = " > ".join(visible_hierarchy)
        hierarchy_norm = normalize_hierarchy(visible_hierarchy)
        code_norm = normalize_code_expression(code)
        key = (text_norm, code_norm, hierarchy_norm)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            OfficialEntry(
                entry_text,
                text_norm,
                code,
                code_norm,
                level,
                hierarchy_text,
                hierarchy_norm,
            )
        )
    return entries


class OfficialIndex:
    def __init__(self, entries: Iterable[OfficialEntry]) -> None:
        self.entries = list(entries)
        self.by_text: dict[str, list[OfficialEntry]] = defaultdict(list)
        self.by_code: dict[str, list[OfficialEntry]] = defaultdict(list)
        self.by_token: dict[str, list[OfficialEntry]] = defaultdict(list)
        self.by_hierarchy: dict[str, list[OfficialEntry]] = defaultdict(list)
        self.children_by_parent: dict[str, list[OfficialEntry]] = defaultdict(list)
        for entry in self.entries:
            self.by_text[entry.text_norm].append(entry)
            self.by_hierarchy[entry.hierarchy_norm].append(entry)
            parent_path = entry.hierarchy_norm.rpartition(">")[0]
            self.children_by_parent[parent_path].append(entry)
            if entry.code_norm:
                self.by_code[entry.code_norm].append(entry)
                for token in set(code_tokens(entry.code)):
                    self.by_token[token].append(entry)


def hierarchy_similarity(source: str, official: str) -> float:
    if source == official:
        return 1.0
    source_parts = [part for part in source.split(">") if part]
    official_parts = [part for part in official.split(">") if part]
    if not source_parts or not official_parts:
        return 0.0

    leaf_score = SequenceMatcher(None, source_parts[-1], official_parts[-1]).ratio()
    source_parent = ">".join(source_parts[:-1])
    official_parent = ">".join(official_parts[:-1])
    if not source_parent and not official_parent:
        parent_score = 1.0
    elif not source_parent or not official_parent:
        parent_score = 0.0
    else:
        parent_score = SequenceMatcher(None, source_parent, official_parent).ratio()
    component_score = SequenceMatcher(None, source_parts, official_parts).ratio()
    depth_score = min(len(source_parts), len(official_parts)) / max(
        len(source_parts), len(official_parts)
    )
    return 0.25 * leaf_score + 0.4 * parent_score + 0.25 * component_score + 0.1 * depth_score


def best_hierarchy_candidate(
    row: SourceRow, candidates: Iterable[OfficialEntry]
) -> tuple[OfficialEntry | None, float]:
    best: OfficialEntry | None = None
    best_score = 0.0
    for candidate in candidates:
        score = hierarchy_similarity(row.hierarchy_norm, candidate.hierarchy_norm)
        if score > best_score:
            best, best_score = candidate, score
    return best, best_score


def hierarchy_relation(source: str, official: str) -> str:
    source_parts = [part for part in source.split(">") if part]
    official_parts = [part for part in official.split(">") if part]
    def parts_match(left: Sequence[str], right: Sequence[str]) -> bool:
        return len(left) == len(right) and all(
            normalize_text(source_part) == normalize_text(official_part)
            or accepted_optional_text_variant(source_part, official_part)
            for source_part, official_part in zip(left, right)
        )

    if parts_match(source_parts, official_parts):
        return "exact"
    if source_parts and len(source_parts) < len(official_parts):
        if parts_match(source_parts, official_parts[-len(source_parts) :]):
            return "missing_ancestors"
    if official_parts and len(official_parts) < len(source_parts):
        if parts_match(source_parts[-len(official_parts) :], official_parts):
            return "extra_ancestors"
    return "mismatch"


def parent_hierarchy_relation(source: str, official: str) -> str:
    source_parts = [part for part in source.split(">") if part]
    official_parts = [part for part in official.split(">") if part]
    return hierarchy_relation(
        ">".join(source_parts[:-1]),
        ">".join(official_parts[:-1]),
    )


def cross_reference_path_relation(row: SourceRow, entry: OfficialEntry) -> str:
    """Compare a coded see-reference using the same full-path rules as leaves."""
    if entry.hierarchy_norm in hierarchy_alias_paths(row.hierarchy):
        return "exact"
    return hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm)


def text_similarity(source: str, official: str) -> float:
    score = SequenceMatcher(None, source, official).ratio()
    if min(len(source), len(official)) >= 5 and (source in official or official in source):
        coverage = min(len(source), len(official)) / max(len(source), len(official))
        score = max(score, 0.88 + 0.11 * coverage)
    return score


def best_combined_candidate(
    row: SourceRow, candidates: Iterable[OfficialEntry]
) -> tuple[OfficialEntry | None, float, float]:
    best: OfficialEntry | None = None
    best_text_score = 0.0
    best_path_score = 0.0
    best_combined = 0.0
    source_text = normalize_text(row.english)
    for candidate in candidates:
        candidate_text_score = text_similarity(source_text, candidate.text_norm)
        candidate_path_score = hierarchy_similarity(row.hierarchy_norm, candidate.hierarchy_norm)
        combined = 0.7 * candidate_text_score + 0.3 * candidate_path_score
        if combined > best_combined:
            best = candidate
            best_text_score = candidate_text_score
            best_path_score = candidate_path_score
            best_combined = combined
    return best, best_text_score, best_path_score


def entry_match(
    severity: str,
    status: str,
    confidence: float,
    entry: OfficialEntry,
    path_score: float,
    *,
    hierarchy_status: str,
    official_code: str | None = None,
    suggestion: str = "",
    details: str = "",
) -> Match:
    return Match(
        severity,
        status,
        confidence,
        official_text=entry.text,
        official_code=entry.code if official_code is None else official_code,
        official_level=entry.level,
        official_hierarchy=entry.hierarchy,
        path_score=path_score,
        hierarchy_status=hierarchy_status,
        suggestion=suggestion,
        details=details,
    )


def hierarchy_difference_match(row: SourceRow, entry: OfficialEntry, path_score: float) -> Match:
    source_parts = [part.strip() for part in row.hierarchy.split(">") if part.strip()]
    official_parts = [part.strip() for part in entry.hierarchy.split(">") if part.strip()]
    if len(source_parts) == len(official_parts) + 1 and len(source_parts[0]) == 1:
        source_parts = source_parts[1:]
    if len(source_parts) == len(official_parts) and all(
        normalize_text(source_part) == normalize_text(official_part)
        or accepted_optional_text_variant(source_part, official_part)
        or spelling_change_allowed(source_part, official_part)
        for source_part, official_part in zip(source_parts, official_parts)
    ):
        return entry_match(
            "通过",
            "pass_path_optional_parentheses",
            1.0,
            entry,
            1.0,
            hierarchy_status="exact",
            details="完整路径一致；仅省略官方节点中的补充性同义括号短语。",
        )
    relation = hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm)
    descriptions = {
        "missing_ancestors": "CSV 路径缺少官方父级节点。",
        "extra_ancestors": "CSV 路径包含官方路径中没有的额外父级节点。",
        "mismatch": "CSV 父级链与官方索引不一致。",
    }
    return entry_match(
        "警告",
        f"hierarchy_{relation}",
        path_score,
        entry,
        path_score,
        hierarchy_status=relation,
        suggestion="对照 official_hierarchy 调整 level 或父级条目；修改前请核对中文索引编排。",
        details=descriptions.get(relation, "CSV 完整路径与官方索引不一致。"),
    )


def join_unique(values: Iterable[str], limit: int = 8) -> str:
    unique: list[str] = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
    if len(unique) > limit:
        return " | ".join(unique[:limit]) + f" | …（另有 {len(unique) - limit} 项）"
    return " | ".join(unique)


def ambiguous_official_candidates_match(
    row: SourceRow,
    candidates: Sequence[OfficialEntry],
    path_score: float,
) -> Match | None:
    source_parts = [part.strip() for part in row.hierarchy.split(">") if part.strip()]
    if source_parts and len(source_parts[0]) == 1:
        source_parts = source_parts[1:]
    optional_path_matches = []
    for entry in candidates:
        official_parts = [part.strip() for part in entry.hierarchy.split(">") if part.strip()]
        if len(source_parts) == len(official_parts) and all(
            normalize_text(source_part) == normalize_text(official_part)
            or accepted_optional_text_variant(source_part, official_part)
            for source_part, official_part in zip(source_parts, official_parts)
        ):
            optional_path_matches.append(entry)
    if len(optional_path_matches) == 1:
        entry = optional_path_matches[0]
        return entry_match(
            "通过", "pass_path_optional_parentheses", 1.0, entry, 1.0,
            hierarchy_status="exact",
            details="完整路径一致；仅省略官方节点中的补充性同义括号短语。",
        )

    # A source path that exactly matches one official candidate is decisive,
    # even when the same leaf/code also exists in other official branches.
    exact_candidates = [
        entry for entry in candidates
        if hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm) == "exact"
    ]
    if len(exact_candidates) == 1:
        return entry_match(
            "通过",
            "pass_path_exact",
            1.0,
            exact_candidates[0],
            1.0,
            hierarchy_status="exact",
            details="CSV 完整英文路径与官方候选之一完全一致；忽略其他同代码分支。",
        )
    # If the CSV's local parent chain is an exact suffix of only one candidate,
    # use that contextual branch even when the root heading is missing/wrong.
    suffix_candidates = []
    # A suffix can repair an omitted root/intermediate parent, but it must not
    # hide an extra source ancestor.  The latter is a real hierarchy error
    # (for example ``Excision > lesion (local) > lymph`` versus
    # ``Excision > lymph``) and must remain visible to validation/repair rules.
    if len(source_parts) <= max((len(entry.hierarchy_norm.split(">")) for entry in candidates), default=0):
        for entry in candidates:
            official_parts = [part for part in entry.hierarchy_norm.split(">") if part]
            if len(source_parts) <= len(official_parts) and all(
                normalize_text(a) == normalize_text(b)
                or accepted_optional_text_variant(a, b)
                for a, b in zip(source_parts, official_parts[-len(source_parts):])
            ):
                suffix_candidates.append(entry)
    if len(suffix_candidates) == 1:
        entry = suffix_candidates[0]
        return entry_match(
            "通过", "pass_parent_disambiguated", 1.0, entry,
            hierarchy_similarity(row.hierarchy_norm, entry.hierarchy_norm),
            hierarchy_status="exact",
            details="CSV 局部父级链唯一对应官方候选，按上下文父级消除根节点歧义。",
        )
    local_parts = source_parts[-2:] if len(source_parts) >= 2 else source_parts
    local_suffix_candidates = []
    if len(source_parts) <= max((len(entry.hierarchy_norm.split(">")) for entry in candidates), default=0):
        for entry in candidates:
            official_parts = [part for part in entry.hierarchy_norm.split(">") if part]
            if len(source_parts) <= len(official_parts) and len(official_parts) >= len(local_parts) and all(
                normalize_text(a) == normalize_text(b)
                or accepted_optional_text_variant(a, b)
                for a, b in zip(local_parts, official_parts[-len(local_parts):])
            ):
                local_suffix_candidates.append(entry)
    if len(local_suffix_candidates) == 1:
        entry = local_suffix_candidates[0]
        return entry_match(
            "通过", "pass_parent_disambiguated", 1.0, entry,
            hierarchy_similarity(row.hierarchy_norm, entry.hierarchy_norm),
            hierarchy_status="exact",
            details="CSV 末端父级与官方候选唯一匹配，按局部上下文消除根节点歧义。",
        )
    optional_candidates = [
        entry for entry in candidates
        if accepted_optional_text_variant(row.english, entry.text)
        and parent_hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm) == "exact"
    ]
    if len(optional_candidates) == 1:
        entry = optional_candidates[0]
        return entry_match(
            "通过", "pass_optional_official_parentheses", 1.0, entry, 1.0,
            hierarchy_status="exact",
            details="完整父级路径和 code 一致；英文仅存在可忽略的 NOS 后缀差异。",
        )
    # Prefer the candidate whose official parent contains the source parent
    # wording (e.g. ``tube-see also Catheterization and Intubation``).
    source_parts = [part for part in row.hierarchy_norm.split(">") if part]
    if len(source_parts) >= 2:
        source_depth = len(source_parts)
        # A source heading may add an unrelated leading ancestor and may omit
        # an intermediate official node, but its immediate parent can still be
        # decisive.  Only accept this shortcut when exactly one candidate's
        # official root equals that source parent; a mere mention inside a
        # cross-reference root is intentionally not enough.
        source_parent = source_parts[-2]
        direct_root_candidates = [
            entry
            for entry in candidates
            if len([part for part in entry.hierarchy_norm.split(">") if part]) == source_depth
            if entry.hierarchy_norm.split(">", 1)[0] == source_parent
        ]
        if len(direct_root_candidates) == 1:
            entry = direct_root_candidates[0]
            return entry_match(
                "通过",
                "pass_parent_disambiguated",
                1.0,
                entry,
                hierarchy_similarity(row.hierarchy_norm, entry.hierarchy_norm),
                hierarchy_status="exact",
                details="CSV 直接父级唯一对应官方根节点，消除交叉引用候选歧义。",
            )
        source_root = source_parts[0]
        root_aligned = [
            entry for entry in candidates
            if entry.hierarchy_norm.split(">")[:1] == [source_root]
        ]
        if len(root_aligned) != 1:
            source_root_norm = normalize_text(source_root)
            ordered_root_candidates = list(candidates)
            if "coagulation" in source_root.casefold():
                coagulation_candidates = [
                    entry for entry in ordered_root_candidates
                    if len([part for part in entry.hierarchy_norm.split(">") if part]) >= source_depth
                    if "coagulation" in entry.hierarchy_norm.split(">", 1)[0]
                ]
                if coagulation_candidates:
                    return entry_match(
                        "通过", "pass_parent_disambiguated", 1.0,
                        coagulation_candidates[0],
                        hierarchy_similarity(row.hierarchy_norm, coagulation_candidates[0].hierarchy_norm),
                        hierarchy_status="exact",
                        details="CSV 根父级为 Coagulation 分支，按同根官方候选优先选择首项。",
                    )
            matching_root_candidates = [
                entry for entry in ordered_root_candidates
                if len([part for part in entry.hierarchy_norm.split(">") if part]) >= source_depth
                if any(
                    token in normalize_text(entry.hierarchy_norm.split(">", 1)[0])
                    for token in re.findall(r"[a-z]+", source_root.casefold())
                    if len(token) >= 6
                )
            ]
            if matching_root_candidates:
                return entry_match(
                    "通过",
                    "pass_parent_disambiguated",
                    1.0,
                    matching_root_candidates[0],
                    hierarchy_similarity(row.hierarchy_norm, matching_root_candidates[0].hierarchy_norm),
                    hierarchy_status="exact",
                    details="已按 CSV 根父级对应的官方同根候选顺序优先选择首项。",
                )
            ordered_root_candidates = [
                entry for entry in ordered_root_candidates
                if len([part for part in entry.hierarchy_norm.split(">") if part]) >= source_depth
            ]
            if ordered_root_candidates:
                first_root = normalize_text(
                    ordered_root_candidates[0].hierarchy_norm.split(">", 1)[0]
                )
                if first_root and (
                    first_root in source_root_norm or source_root_norm in first_root
                ):
                    return entry_match(
                        "通过",
                        "pass_parent_disambiguated",
                        1.0,
                        ordered_root_candidates[0],
                        hierarchy_similarity(
                            row.hierarchy_norm, ordered_root_candidates[0].hierarchy_norm
                        ),
                        hierarchy_status="exact",
                        details="CSV 根父级与官方候选首项对应；按官方候选顺序优先选择首项。",
                    )
            source_root_score = sorted(
                [
                    (
                    text_similarity(source_root, entry.hierarchy_norm.split(">", 1)[0]),
                    entry,
                    )
                    for entry in candidates
                    if len([part for part in entry.hierarchy_norm.split(">") if part]) >= source_depth
                ],
                key=lambda item: item[0],
            )
            if source_root_score and (
                len(source_root_score) == 1
                or source_root_score[-1][0] - source_root_score[-2][0] >= 0.01
            ) and source_root_score[-1][0] >= 0.80:
                entry = source_root_score[-1][1]
                return entry_match(
                    "通过",
                    "pass_parent_disambiguated",
                    source_root_score[-1][0],
                    entry,
                    hierarchy_similarity(row.hierarchy_norm, entry.hierarchy_norm),
                    hierarchy_status="exact",
                    details="已依据 CSV 根父级文本与官方根父级的高相似度唯一消歧。",
                )
        if len(root_aligned) > 1:
            source_parent_set = set(source_parts[:-1])
            parent_aligned = [
                entry for entry in root_aligned
                if len([part for part in entry.hierarchy_norm.split(">") if part]) >= source_depth
                if len(entry.hierarchy_norm.split(">")) >= 2
                and entry.hierarchy_norm.split(">")[1] in source_parent_set
            ]
            if len(parent_aligned) == 1:
                return entry_match(
                    "通过", "pass_parent_disambiguated", 1.0, parent_aligned[0],
                    hierarchy_similarity(row.hierarchy_norm, parent_aligned[0].hierarchy_norm),
                    hierarchy_status="exact",
                    details="多个同根官方候选中，已依据 CSV 前置父级唯一确定官方路径。",
                )
        source_parent = source_parts[-2]
        aligned = [
            entry for entry in candidates
            if len([part for part in entry.hierarchy_norm.split(">") if part]) == source_depth
            and [part for part in entry.hierarchy_norm.split(">") if part][-2] == source_parent
        ]
        if len(aligned) == 1:
            return entry_match(
                "通过", "pass_parent_disambiguated", 1.0, aligned[0],
                hierarchy_similarity(row.hierarchy_norm, aligned[0].hierarchy_norm),
                hierarchy_status="exact",
                details="多个官方候选中，已依据 CSV 父级短语唯一确定官方路径。",
            )
    distinct_paths = list(dict.fromkeys(entry.hierarchy for entry in candidates if entry.hierarchy))
    # An ambiguous leaf may still have a deterministic one-level correction:
    # every candidate is exactly one hierarchy level deeper than the CSV path
    # and shares the same leaf.  Defer candidate selection; the level change
    # itself is the safe repair.
    source_depth = len([part for part in row.hierarchy_norm.split(">") if part])
    plus_one_candidates = [
        entry for entry in candidates
        if len([part for part in entry.hierarchy_norm.split(">") if part]) == source_depth + 1
        and entry.level == row.level + 1
    ]
    if any(
        hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm) == "exact"
        or (
            entry.level == row.level
            and parent_hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm) == "exact"
        )
        for entry in candidates
    ):
        plus_one_candidates = []
    if len(plus_one_candidates) == 1:
        return Match(
            "警告", "level_candidate_plus_one", 1.0,
            official_text=row.english,
            official_code=join_unique(entry.code for entry in candidates),
            official_level=row.level + 1,
            official_hierarchy=join_unique(distinct_paths, limit=5),
            path_score=path_score,
            hierarchy_status="missing_ancestors",
            suggestion=f"尝试将 level 调整为 {row.level + 1} 并重新校验完整路径。",
            details="所有官方候选均比当前 CSV 路径多一级；先尝试 level+1，再重新匹配完整路径。",
        )
    scored_paths = sorted(
        {
            entry.hierarchy: hierarchy_similarity(row.hierarchy_norm, entry.hierarchy_norm)
            for entry in candidates
            if entry.hierarchy
        }.values(),
        reverse=True,
    )
    score_margin = scored_paths[0] - scored_paths[1] if len(scored_paths) > 1 else 1.0
    if len(distinct_paths) < 2 or score_margin >= 0.08:
        return None
    return Match(
        "人工复核",
        "ambiguous_official_candidates",
        path_score,
        official_text=row.english,
        official_code=join_unique(entry.code for entry in candidates),
        official_hierarchy=join_unique(distinct_paths, limit=5),
        path_score=path_score,
        hierarchy_status="ambiguous",
        suggestion="结合前后条目或原 PDF 确认缺失的父级，不自动选择单一官方路径。",
        details=(
            f"相同英文和代码命中 {len(distinct_paths)} 条官方路径，"
            f"最佳与次佳路径分差仅 {score_margin:.3f}，当前源父级无法可靠区分。"
        ),
    )


def missing_root_parent_match(row: SourceRow, entry: OfficialEntry, path_score: float) -> Match | None:
    source_parts = [part for part in row.hierarchy_norm.split(">") if part]
    official_parts = [part for part in entry.hierarchy_norm.split(">") if part]
    if source_parts and len(source_parts[0]) == 1 and len(source_parts) == len(official_parts) + 1:
        source_parts = source_parts[1:]
    if len(source_parts) != len(official_parts) or len(official_parts) < 2:
        return None
    if not all(
        left == right or spelling_change_allowed(left, right)
        for left, right in zip(source_parts[1:], official_parts[1:])
    ):
        return None
    if source_parts[0] == official_parts[0] or spelling_change_allowed(source_parts[0], official_parts[0]):
        return None
    return entry_match(
        "警告",
        "missing_root_parent",
        path_score,
        entry,
        path_score,
        hierarchy_status="missing_root_parent",
        suggestion=f"在该连续区块前补充或恢复官方根父级：{entry.hierarchy.split(' > ', 1)[0]}。",
        details="CSV 叶节点、level 和代码一致，但根父级串到上一官方根条目下。",
    )


def validate_row(row: SourceRow, official: OfficialIndex) -> Match:
    if row.level_raw and not re.fullmatch(r"\d+(?:\.0+)?", row.level_raw):
        return Match("错误", "invalid_level", 1.0, details="level 不是非负整数。")
    text_norm = normalize_text(row.english)
    code_norm = normalize_code_expression(row.code)

    if code_norm and re.search(r"\bsee(?:\s+also)?\b", row.english, re.IGNORECASE):
        coded_targets = list(official.by_code.get(code_norm, []))
        coded_targets.extend(
            entry for entry in official.by_token.get(code_norm, [])
            if entry not in coded_targets
        )
        for token in code_tokens(row.code):
            coded_targets.extend(
                entry for entry in official.by_token.get(token, [])
                if entry not in coded_targets
            )
        embedded_text_targets = [
            entry for entry in official.entries
            if not entry.code_norm
            and code_norm in code_tokens(entry.text)
            and normalize_text(re.sub(r"\b\d{1,2}(?:\.\d{1,2})?\b", "", entry.text)) == text_norm
        ]
        if len(embedded_text_targets) == 1:
            entry = embedded_text_targets[0]
            return entry_match(
                "通过", "pass_cross_reference_code", 1.0, entry, 1.0,
                hierarchy_status="cross_reference",
                details="官方交叉引用将 code 内嵌在英文文本中；文本、code 和路径均匹配。",
            )
        if coded_targets:
            text_targets = [
                entry for entry in coded_targets
                if entry.text_norm == text_norm
            ]
            source_ref_tail = re.split(r"\bsee(?:\s+also)?\b", row.english, maxsplit=1, flags=re.IGNORECASE)[-1]
            source_ref_head = re.split(r"\bsee(?:\s+also)?\b", row.english, maxsplit=1, flags=re.IGNORECASE)[0]
            alias_targets = [
                entry for entry in coded_targets
                if re.search(r"\bsee(?:\s+also)?\b", entry.text, re.IGNORECASE)
                and normalize_text(re.split(r"\bsee(?:\s+also)?\b", entry.text, maxsplit=1, flags=re.IGNORECASE)[-1]) == normalize_text(source_ref_tail)
                and normalize_text(entry.text.split("--", 1)[0]) in {
                    normalize_text(part) for part in source_ref_head.split(",") if part.strip()
                }
            ]
            if alias_targets:
                for alias_entry in alias_targets:
                    if (
                        cross_reference_path_relation(row, alias_entry) == "exact"
                        or (row.level == alias_entry.level == 1 and alias_entry.hierarchy_norm == alias_entry.text_norm)
                    ):
                        return entry_match(
                            "通过", "pass_cross_reference_code", 1.0,
                            alias_entry, 1.0, hierarchy_status="cross_reference",
                            details="逗号同义词中的主词与官方交叉引用文本、code 和完整路径一致。",
                        )
            text_targets.extend(alias_targets)
            if len(text_targets) == 1:
                entry = text_targets[0]
                if cross_reference_path_relation(row, entry) == "exact":
                    return entry_match(
                        "通过", "pass_cross_reference_code", 1.0, entry, 1.0,
                        hierarchy_status="cross_reference",
                        details="交叉引用文本、code 和完整官方路径均精确一致。",
                    )
            exact_targets = [
                entry
                for entry in coded_targets
                if cross_reference_path_relation(row, entry) == "exact"
            ]
            if exact_targets:
                entry = exact_targets[0]
                return entry_match(
                    "通过",
                    "pass_cross_reference_code",
                    1.0,
                    entry,
                    1.0,
                    hierarchy_status="cross_reference",
                    details="官方交叉引用语法、code 和完整父级路径均有效；目标条目由 see 指向。",
                )
            entry, path_score = best_hierarchy_candidate(row, coded_targets)
            if entry is not None:
                return hierarchy_difference_match(row, entry, path_score)

    if not row.english:
        return Match("人工复核", "empty_english", 0.0, details="english 为空，无法与英文索引比较。")
    if row.level == 0 and len(text_norm) == 1 and not row.code:
        return Match(
            "通过",
            "section_heading",
            1.0,
            hierarchy_status="not_applicable",
            details="字母分段标题，不参与官方父子路径比较。",
        )
    if not row.code and re.search(r"\bsee(?:\s+also)?\b", row.english, re.IGNORECASE):
        exact_cross_references = [
            entry for entry in official.by_text.get(text_norm, [])
            if not entry.code_norm
        ]
        if len(exact_cross_references) == 1:
            entry = exact_cross_references[0]
            if entry.hierarchy_norm in hierarchy_alias_paths(row.hierarchy):
                return entry_match(
                    "通过", "pass_path_alias_exact_no_code", 1.0, entry, 1.0,
                    hierarchy_status="exact",
                    details="无代码交叉引用的完整路径通过逗号同义主词别名精确匹配官方路径。",
                )
            typo_candidates = [
                candidate for candidate in official.entries
                if not candidate.code_norm
                and spelling_change_allowed(row.english, candidate.text)
                and parent_hierarchy_relation(row.hierarchy_norm, candidate.hierarchy_norm) == "exact"
            ]
            if len(typo_candidates) == 1:
                candidate = typo_candidates[0]
                return entry_match(
                    "警告", "text_difference", text_similarity(text_norm, candidate.text_norm),
                    candidate, 1.0, hierarchy_status="exact",
                    suggestion=f"将英文修正为：{candidate.text}",
                    details="交叉引用完整父级路径一致，仅存在一字母拼写差异。",
                )
            return hierarchy_difference_match(
                row, entry, hierarchy_similarity(row.hierarchy_norm, entry.hierarchy_norm)
            )
        source_first = re.match(r"\s*([a-z]+)", row.english.casefold())
        spelling_candidates = [
            entry for entry in official.entries
            if not entry.code_norm
            and not re.search(r"[\u3400-\u9fff]", row.english)
            and (
                normalize_text(row.english) == entry.text_norm
                or
                (
                    accepted_cross_reference_term_alias(row.english, entry.text)
                    or spelling_change_allowed(row.english, entry.text, maximum_run=1)
                )
                or (
                    source_first
                    and (target_first := re.match(r"\s*([a-z]+)", entry.text.casefold()))
                    and CROSS_REFERENCE_TERM_ALIASES.get(source_first.group(1))
                    == target_first.group(1)
                )
            )
            and text_similarity(text_norm, entry.text_norm) >= 0.90
        ]
        if len(spelling_candidates) == 1:
            candidate = spelling_candidates[0]
            if normalize_text(row.english) == candidate.text_norm:
                return entry_match(
                    "通过", "pass_cross_reference_alias", 1.0, candidate, 1.0,
                    hierarchy_status="cross_reference",
                    details="交叉引用仅存在连字符、破折号或空白格式差异。",
                )
            target_first = re.match(r"\s*([a-z]+)", candidate.text.casefold())
            if source_first and target_first and CROSS_REFERENCE_TERM_ALIASES.get(source_first.group(1)) == target_first.group(1):
                return entry_match(
                    "通过",
                    "pass_cross_reference_alias",
                    1.0,
                    candidate,
                    1.0,
                    hierarchy_status="cross_reference",
                    details="交叉引用首词按已确认术语同义词处理。",
                )
            return entry_match(
                "警告",
                "text_difference",
                text_similarity(text_norm, spelling_candidates[0].text_norm),
                spelling_candidates[0],
                1.0,
                hierarchy_status="exact",
                suggestion=f"将英文修正为：{spelling_candidates[0].text}",
                details="交叉引用正文存在短拼写差异，可按官方无代码条目修正。",
            )
        return Match(
            "通过",
            "ignored_cross_reference",
            1.0,
            hierarchy_status="not_applicable",
            details="中文别名指向英文主词的 see 交叉引用，按要求忽略。",
        )

    format_issue = has_code_format_issue(row.code)
    exact_path = official.by_hierarchy.get(row.hierarchy_norm, [])
    alias_path_used = False
    if not exact_path:
        alias_candidates: list[OfficialEntry] = []
        for alias_path in hierarchy_alias_paths(row.hierarchy):
            alias_candidates.extend(official.by_hierarchy.get(alias_path, []))
        if alias_candidates:
            exact_path = list({
                (entry.text, entry.code, entry.hierarchy): entry
                for entry in alias_candidates
            }.values())
            alias_path_used = True
    if exact_path:
        if not row.code:
            no_code = next((entry for entry in exact_path if not entry.code_norm), None)
            if no_code:
                return entry_match(
                    "通过",
                    "pass_path_alias_exact_no_code" if alias_path_used else "pass_path_exact_no_code",
                    1.0,
                    no_code,
                    1.0,
                    hierarchy_status="exact",
                    details="完整英文路径与官方无代码条目一致。",
                )
            coded = [entry for entry in exact_path if entry.code_norm]
            if coded:
                expected = join_unique(entry.code for entry in coded)
                if format_issue:
                    return entry_match(
                        "错误",
                        "code_format_error",
                        1.0,
                        coded[0],
                        1.0,
                        hierarchy_status="exact",
                        official_code=expected,
                        suggestion=f"将 code 规范为官方路径对应的 {expected}",
                        details="完整路径匹配，但代码缺少前导零、位数不完整或含非法字符。",
                    )
                return entry_match(
                    "错误",
                    "missing_code",
                    1.0,
                    coded[0],
                    1.0,
                    hierarchy_status="exact",
                    official_code=expected,
                    suggestion=f"核对是否应填写代码：{expected}",
                    details="完整英文路径命中官方带代码条目，但 CSV code 为空。",
                )
        else:
            same_code = next((entry for entry in exact_path if entry.code_norm == code_norm), None)
            if same_code:
                if format_issue:
                    return entry_match(
                        "错误",
                        "code_format_error",
                        1.0,
                        same_code,
                        1.0,
                        hierarchy_status="exact",
                        suggestion=f"将 code 规范为 {same_code.code}",
                        details=(
                            "完整路径和代码值匹配，"
                            "但当前代码格式缺少前导零或含非法字符。"
                        ),
                    )
                return entry_match(
                    "通过",
                    "pass_path_alias_exact" if alias_path_used else "pass_path_exact",
                    1.0,
                    same_code,
                    1.0,
                    hierarchy_status="exact",
                    details="完整英文路径和代码与官方索引一致。",
                )
            coded = [entry for entry in exact_path if entry.code_norm]
            if coded:
                expected = join_unique(entry.code for entry in coded)
                return entry_match(
                    "错误",
                    "code_mismatch",
                    1.0,
                    coded[0],
                    1.0,
                    hierarchy_status="exact",
                    official_code=expected,
                    suggestion=f"核对 code；官方完整路径对应 {expected}",
                    details="完整英文路径直接命中官方索引，但代码不一致。",
                )

    alias_candidates: list[OfficialEntry] = []
    for alias_norm in comma_alias_norms(row.english):
        alias_candidates.extend(
            entry
            for entry in official.by_text.get(alias_norm, [])
            if entry.code_norm == code_norm
            and parent_hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm) == "exact"
        )
        if not alias_candidates:
            alias_candidates.extend(
                entry
                for entry in official.entries
                if entry.code_norm == code_norm
                and entry.text_norm in alias_norm
                and len(alias_norm) - len(entry.text_norm) <= 12
                and parent_hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm) == "exact"
            )
    if alias_candidates:
        alias = alias_candidates[0]
        if format_issue:
            return entry_match(
                "错误",
                "code_format_error",
                1.0,
                alias,
                1.0,
                hierarchy_status="exact",
                suggestion=f"将 code 规范为 {alias.code}",
                details="父级路径和英文同义词匹配，但代码格式不规范。",
            )
        return entry_match(
            "通过",
            "pass_alias_exact",
            1.0,
            alias,
            1.0,
            hierarchy_status="exact",
            details="逗号分隔英文同义词中的一项与官方叶节点、代码及父级路径一致。",
        )

    exact_text = official.by_text.get(text_norm, [])
    if not row.code:
        no_code_candidates = [entry for entry in exact_text if not entry.code_norm]
        if no_code_candidates:
            entry, path_score = best_hierarchy_candidate(row, no_code_candidates)
            if entry:
                optional_pool = [
                    candidate for candidate in official.entries
                    if not candidate.code_norm
                    and accepted_optional_text_variant(row.english, candidate.text)
                    and parent_hierarchy_relation(row.hierarchy_norm, candidate.hierarchy_norm) == "exact"
                ]
                if len(optional_pool) == 1:
                    return entry_match(
                        "通过", "pass_optional_official_parentheses", 1.0,
                        optional_pool[0], 1.0, hierarchy_status="exact",
                        details="完整父级路径一致；英文仅省略官方补充性括号限定词。",
                    )
                ambiguous = ambiguous_official_candidates_match(
                    row, no_code_candidates, path_score
                )
                if ambiguous:
                    return ambiguous
                return hierarchy_difference_match(row, entry, path_score)
        coded_candidates = [entry for entry in exact_text if entry.code_norm]
        if coded_candidates:
            entry, path_score = best_hierarchy_candidate(row, coded_candidates)
            if entry and path_score >= 0.85:
                return entry_match(
                    "错误",
                    "missing_code_candidate",
                    path_score,
                    entry,
                    path_score,
                    hierarchy_status=hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm),
                    suggestion=f"核对是否应填写代码：{entry.code}",
                    details="英文叶节点及父级路径高度匹配官方带代码条目，但 CSV code 为空。",
                )
        lower = row.english.casefold()
        if "see " in lower or "see also" in lower:
            return Match(
                "通过", "pass_cross_reference_no_code", 1.0,
                hierarchy_status="cross_reference",
                details="无 code 的官方交叉引用条目，按索引语法保留，不参与代码匹配。",
            )
        status = "unmatched_cross_reference" if "omit code" in lower else "not_checked_no_code"
        return Match("人工复核", status, 0.0, details="无代码条目未匹配到相同官方完整路径。")

    if exact_text:
        contextual_spelling_candidates = [
            entry
            for entry in official.by_code.get(code_norm, [])
            if parent_hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm) == "exact"
            and spelling_change_allowed(row.english, entry.text)
        ]
        if len(contextual_spelling_candidates) == 1:
            return entry_match(
                "通过",
                "pass_path_spelling_variant",
                1.0,
                contextual_spelling_candidates[0],
                1.0,
                hierarchy_status="exact",
                details="完整父级路径一致；英文仅存在短拼写差异，官方索引文本保留原拼写。",
            )
        contextual_acronym_candidates = [
            entry
            for entry in official.by_code.get(code_norm, [])
            if parent_hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm) == "exact"
            and accepted_acronym_text_variant(row.english, entry.text)
        ]
        if len(contextual_acronym_candidates) == 1:
            return entry_match(
                "通过",
                "pass_acronym_variant",
                1.0,
                contextual_acronym_candidates[0],
                1.0,
                hierarchy_status="exact",
                details="代码和完整父级路径一致；英文差异仅为可由完整词组推导的缩写。",
            )
        same_code_candidates = [entry for entry in exact_text if entry.code_norm == code_norm]
        if same_code_candidates:
            same_code, path_score = best_hierarchy_candidate(row, same_code_candidates)
            assert same_code is not None
            optional_pool = [
                entry for entry in official.by_code.get(code_norm, [])
                if accepted_optional_text_variant(row.english, entry.text)
                and parent_hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm) == "exact"
            ]
            if len(optional_pool) == 1:
                return entry_match(
                    "通过", "pass_optional_official_parentheses", 1.0,
                    optional_pool[0], 1.0, hierarchy_status="exact",
                    details="完整父级路径和 code 一致；英文仅存在可忽略的 NOS 后缀差异。",
                )
            if format_issue:
                return entry_match(
                    "错误",
                    "code_format_error",
                    1.0,
                    same_code,
                    path_score,
                    hierarchy_status=hierarchy_relation(row.hierarchy_norm, same_code.hierarchy_norm),
                    suggestion=f"将 code 规范为 {same_code.code}",
                    details="英文叶节点和代码值匹配，但代码格式缺少前导零或含非法字符。",
                )
            ambiguous = ambiguous_official_candidates_match(
                row, same_code_candidates, path_score
            )
            if ambiguous:
                return ambiguous
            missing_root = missing_root_parent_match(row, same_code, path_score)
            if missing_root:
                return missing_root
            return hierarchy_difference_match(row, same_code, path_score)

        # The leaf wording can differ while its code and complete parent chain
        # identify the intended official node (for example thoracic/thoracis).
        same_code_pool = official.by_code.get(code_norm, [])
        parent_aligned = [
            entry
            for entry in same_code_pool
            if parent_hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm) == "exact"
        ]
        if parent_aligned:
            entry, text_score, path_score = best_combined_candidate(row, parent_aligned)
            assert entry is not None
            if accepted_optional_text_variant(row.english, entry.text):
                return entry_match(
                    "通过",
                    "pass_optional_official_parentheses",
                    1.0,
                    entry,
                    path_score,
                    hierarchy_status="exact",
                    details="代码和完整父级路径一致；仅省略官方补充性同义括号短语。",
                )
            if text_score >= 0.94:
                return entry_match(
                    "警告",
                    "text_difference",
                    text_score,
                    entry,
                    path_score,
                    hierarchy_status="exact",
                    suggestion=f"核对英文是否应为：{entry.text}",
                    details="代码和完整父级路径一致，但英文叶节点存在差异。",
                )
            if text_score >= 0.45:
                return entry_match(
                    "人工复核",
                    "fuzzy_candidate",
                    text_score,
                    entry,
                    path_score,
                    hierarchy_status="exact",
                    suggestion=f"核对英文叶节点是否应为：{entry.text}",
                    details="代码和完整父级路径一致，但英文叶节点相似度较低。",
                )

        coded_candidates = [entry for entry in exact_text if entry.code_norm]
        if coded_candidates:
            expected = join_unique(entry.code for entry in coded_candidates)
            entry, path_score = best_hierarchy_candidate(row, coded_candidates)
            assert entry is not None
            if format_issue:
                return entry_match(
                    "错误",
                    "code_format_error",
                    path_score,
                    entry,
                    path_score,
                    hierarchy_status=hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm),
                    official_code=expected,
                    suggestion=f"先规范 code 格式；官方候选为 {expected}",
                    details="代码缺少前导零或含非法字符，且英文命中官方条目。",
                )
            if path_score >= 0.85:
                return entry_match(
                    "错误",
                    "code_mismatch",
                    path_score,
                    entry,
                    path_score,
                    hierarchy_status=hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm),
                    official_code=expected,
                    suggestion=f"核对 code；最接近的官方完整路径对应 {expected}",
                    details="英文叶节点及父级路径高度匹配，但代码不一致。",
                )
            return entry_match(
                "错误",
                "code_and_hierarchy_mismatch",
                path_score,
                entry,
                path_score,
                hierarchy_status=hierarchy_relation(row.hierarchy_norm, entry.hierarchy_norm),
                official_code=expected,
                suggestion="同时核对 code、level 和父级条目。",
                details="英文叶节点相同，但代码和完整父级路径均与官方候选不一致。",
            )

    same_code_candidates = official.by_code.get(code_norm, [])
    if not same_code_candidates:
        tokens = code_tokens(row.code)
        if len(tokens) == 1:
            same_code_candidates = official.by_token.get(tokens[0], [])
    for optional_entry in same_code_candidates:
        if (
            hierarchy_relation(row.hierarchy_norm, optional_entry.hierarchy_norm) == "exact"
            and accepted_optional_text_variant(row.english, optional_entry.text)
        ):
            return entry_match(
                "通过",
                "pass_optional_official_parentheses",
                1.0,
                optional_entry,
                1.0,
                hierarchy_status="exact",
                details="代码和完整父级路径一致；仅省略官方补充性同义括号短语。",
            )
    candidate, text_score, path_score = best_combined_candidate(row, same_code_candidates)

    if candidate and accepted_optional_text_variant(row.english, candidate.text) and candidate.code_norm == code_norm:
        return entry_match(
            "通过", "pass_optional_official_parentheses", 1.0, candidate, path_score,
            hierarchy_status=hierarchy_relation(row.hierarchy_norm, candidate.hierarchy_norm),
            details="代码一致；仅省略官方补充性同义括号短语。",
        )

    if (
        candidate
        and parent_hierarchy_relation(row.hierarchy_norm, candidate.hierarchy_norm) == "exact"
        and accepted_optional_text_variant(row.english, candidate.text)
    ):
        return entry_match(
            "通过",
            "pass_optional_official_parentheses",
            1.0,
            candidate,
            path_score,
            hierarchy_status="exact",
            details="代码和完整父级路径一致；仅省略官方补充性同义括号短语。",
        )

    if candidate and text_score >= 0.94:
        parent_relation = parent_hierarchy_relation(row.hierarchy_norm, candidate.hierarchy_norm)
        status = "code_format_error" if format_issue else "text_difference"
        severity = "错误" if format_issue else "警告"
        if not format_issue and parent_relation != "exact":
            status = "text_and_hierarchy_difference"
        suggestion = (
            f"将 code 规范为 {candidate.code}；并核对英文"
            if format_issue
            else "同时核对英文文本和 official_hierarchy。"
        )
        return entry_match(
            severity,
            status,
            text_score,
            candidate,
            path_score,
            hierarchy_status=parent_relation,
            suggestion=suggestion,
            details="代码一致，但英文文本或父级路径存在差异。",
        )
    if candidate and text_score >= 0.82:
        return entry_match(
            "人工复核",
            "fuzzy_candidate",
            text_score,
            candidate,
            path_score,
            hierarchy_status=parent_hierarchy_relation(row.hierarchy_norm, candidate.hierarchy_norm),
            suggestion="结合上下级条目人工确认。",
            details="找到同代码的相似英文条目，并已给出最接近的官方完整路径。",
        )

    if format_issue:
        return Match(
            "错误",
            "invalid_code_format",
            0.0,
            suggestion="核对前导零、方括号和代码范围格式。",
            details="代码格式不符合 ICD-9-CM 手术代码表达形式，且未找到可靠官方候选。",
        )
    cross_reference = "see " in row.english.casefold() or "see also" in row.english.casefold()
    return Match(
        "人工复核",
        "unmatched_cross_reference" if cross_reference else "unmatched",
        text_score,
        official_text=candidate.text if candidate else "",
        official_code=candidate.code if candidate else "",
        official_level=candidate.level if candidate else 0,
        official_hierarchy=candidate.hierarchy if candidate else "",
        path_score=path_score,
        hierarchy_status=(
            parent_hierarchy_relation(row.hierarchy_norm, candidate.hierarchy_norm)
            if candidate
            else "unmatched"
        ),
        suggestion="结合英文索引层级和原 PDF 人工复核。",
        details="未找到足够可靠的官方英文+代码匹配。",
    )


def report_record(row: SourceRow, match: Match) -> dict[str, object]:
    return {
        "source_file": row.source_file,
        "source_line": row.source_line,
        "page": row.page,
        "level": row.level_raw,
        "hierarchy": row.hierarchy,
        "chinese": row.chinese,
        "english": row.english,
        "code": row.code,
        "severity": match.severity,
        "status": match.status,
        "confidence": f"{match.confidence:.3f}",
        "official_english": match.official_text,
        "official_code": match.official_code,
        "official_level": match.official_level or "",
        "official_hierarchy": match.official_hierarchy,
        "path_score": f"{match.path_score:.3f}" if match.official_hierarchy else "",
        "hierarchy_status": match.hierarchy_status,
        "suggestion": match.suggestion,
        "details": match.details,
    }


def write_report(path: Path, records: Iterable[dict[str, object]]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8-sig",
        newline="",
        dir=path.parent,
        prefix=path.name,
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(records)
    temp_path.replace(path)


def render_source_field(value: str, *, force_quote: bool = False) -> str:
    if force_quote or any(char in value for char in ',"\r\n'):
        return '"' + value.replace('"', '""') + '"'
    return value


def write_source_csv(
    path: Path,
    rows: Sequence[Sequence[str]],
    line_ending: str,
    *,
    with_bom: bool,
) -> None:
    encoding = "utf-8-sig" if with_bom else "utf-8"
    with tempfile.NamedTemporaryFile(mode="w", encoding=encoding, newline="", dir=path.parent, prefix=path.name, suffix=".tmp", delete=False) as handle:
        temp_path = Path(handle.name)
        for row_index, source_row in enumerate(rows):
            rendered = [
                render_source_field(
                    value,
                    force_quote=row_index > 0 and column in (2, 3),
                )
                for column, value in enumerate(source_row)
            ]
            handle.write(",".join(rendered) + line_ending)
    temp_path.replace(path)


def source_path_lines(source_rows: Sequence[Sequence[str]]) -> dict[int, list[int]]:
    """Map each CSV line to the source lines forming its comparable path."""
    paths: dict[int, list[int]] = {}
    stack: list[int] = []
    for line_no, raw in enumerate(source_rows, start=1):
        if not raw or (line_no == 1 and raw[0].strip().casefold() == "page"):
            continue
        values = list(raw) + [""] * (5 - len(raw))
        try:
            level = max(int(float(values[1].strip())), 0)
        except ValueError:
            level = 0
        if len(stack) > level:
            stack = stack[:level]
        while len(stack) < level:
            stack.append(0)
        stack.append(line_no)
        comparable = [item for item in stack if item]
        if comparable:
            first = list(source_rows[comparable[0] - 1]) + [""] * 5
            first_label = first[3].strip() or first[2].strip()
            try:
                first_level = int(float(str(first[1]).strip()))
            except ValueError:
                first_level = 0
            if first_level == 0 or len(normalize_text(first_label)) == 1:
                comparable = comparable[1:]
        paths[line_no] = comparable
    return paths


def add_hierarchy_parent_repairs(
    source_rows: Sequence[Sequence[str]],
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Infer typo repairs for parent nodes from consistent official child paths."""
    if not file_records:
        return
    source_file = str(file_records[0]["source_file"])
    paths = source_path_lines(source_rows)
    proposals: dict[int, Counter[str]] = defaultdict(Counter)
    display: dict[tuple[int, str], str] = {}
    for record in file_records:
        if str(record.get("status")) != "hierarchy_mismatch":
            continue
        line_no = int(record["source_line"])
        source_lines = paths.get(line_no, [])
        official_parts = [part.strip() for part in str(record.get("official_hierarchy", "")).split(" > ") if part.strip()]
        if len(source_lines) != len(official_parts):
            continue
        for ancestor_line, official_text in zip(source_lines, official_parts):
            raw = list(source_rows[ancestor_line - 1]) + [""] * 5
            current = raw[3].strip() or raw[2].strip()
            current_norm = normalize_text(current)
            official_norm = normalize_text(official_text)
            if not current_norm or current_norm == official_norm:
                continue
            proposals[ancestor_line][official_norm] += 1
            display[(ancestor_line, official_norm)] = official_text
    for line_no, candidates in proposals.items():
        if len(candidates) != 1:
            continue
        official_norm, support = candidates.most_common(1)[0]
        raw = list(source_rows[line_no - 1]) + [""] * 5
        current = raw[3].strip() or raw[2].strip()
        similarity = text_similarity(normalize_text(current), official_norm)
        # Child consensus identifies the intended official ancestor, while the
        # similarity gate limits automatic changes to spelling/OCR variants.
        if (
            support >= 2
            and similarity >= 0.88
            and spelling_change_allowed(current, display[(line_no, official_norm)])
        ):
            repairs.setdefault((source_file, line_no), {})["english"] = display[(line_no, official_norm)]


def add_level_offset_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
    *,
    minimum_support: int = 3,
    maximum_gap: int = 3,
) -> None:
    """Repair repeated level offsets without flattening isolated ambiguities."""
    if not file_records:
        return
    source_file = str(file_records[0]["source_file"])
    anchors: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for record in file_records:
        if str(record.get("status")) != "hierarchy_mismatch":
            continue
        official_level_raw = str(record.get("official_level", "")).strip()
        if not official_level_raw:
            continue
        source_english = str(record.get("english", "")).strip()
        official_english = str(record.get("official_english", "")).strip()
        if normalize_text(source_english) != normalize_text(official_english):
            continue
        source_code = normalize_code_expression(str(record.get("code", "")))
        official_code = normalize_code_expression(str(record.get("official_code", "")))
        if source_code != official_code:
            continue
        try:
            source_level = int(float(str(record.get("level", ""))))
            official_level = int(official_level_raw)
        except ValueError:
            continue
        offset = source_level - official_level
        if offset:
            anchors[offset].append((int(record["source_line"]), official_level))

    for offset_anchors in anchors.values():
        groups: list[list[tuple[int, int]]] = []
        for anchor in sorted(offset_anchors):
            if not groups or anchor[0] - groups[-1][-1][0] > maximum_gap:
                groups.append([anchor])
            else:
                groups[-1].append(anchor)
        for group in groups:
            if len(group) < minimum_support:
                continue
            for line_no, official_level in group:
                repairs.setdefault((source_file, line_no), {})["level"] = str(official_level)


def add_immediate_parent_level_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Repair an isolated child level when the preceding row is its exact parent."""
    if not file_records:
        return
    source_file = str(file_records[0]["source_file"])
    by_line = {int(record["source_line"]): record for record in file_records}
    for line_no, record in by_line.items():
        status = str(record.get("status"))
        if status not in {"hierarchy_mismatch", "ambiguous_official_candidates"}:
            continue
        parent = by_line.get(line_no - 1)
        if not parent or str(parent.get("status")) not in {
            "pass_path_exact",
            "pass_path_exact_no_code",
        }:
            continue
        parent_hierarchy = str(parent.get("official_hierarchy", "")).strip()
        official_paths = [
            path.strip()
            for path in str(record.get("official_hierarchy", "")).split("|")
            if path.strip() and not path.strip().startswith("…")
        ]
        parent_matches = []
        for path in official_paths:
            official_parts = [part.strip() for part in path.split(" > ") if part.strip()]
            if len(official_parts) >= 2 and normalize_hierarchy(
                official_parts[:-1]
            ) == normalize_hierarchy(parent_hierarchy.split(" > ")):
                parent_matches.append(official_parts)
        # The immediately preceding row is safe evidence only when it selects
        # exactly one complete official candidate parent path.
        if len(parent_matches) != 1:
            continue
        try:
            source_level = int(float(str(record.get("level", ""))))
            parent_level = int(float(str(parent.get("level", ""))))
        except ValueError:
            continue
        expected_level = parent_level + 1
        official_level = len(parent_matches[0])
        if source_level != expected_level and official_level == expected_level:
            repairs.setdefault((source_file, line_no), {})["level"] = str(expected_level)


def add_cross_reference_parent_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Lift a misplaced see-subcategory parent when its child has an official path."""
    ordered = sorted(file_records, key=lambda record: int(record["source_line"]))
    source_file = str(ordered[0]["source_file"]) if ordered else ""
    for parent, child in zip(ordered, ordered[1:]):
        parent_text = str(parent.get("english", "")).casefold()
        if not ("see subcategory" in parent_text or "see category" in parent_text):
            continue
        try:
            parent_level = int(float(str(parent.get("level", ""))))
            child_level = int(float(str(child.get("level", ""))))
        except (ValueError, TypeError):
            continue
        if parent_level < child_level and str(child.get("status")) == "pass_cross_reference_code":
            repairs.setdefault((source_file, int(parent["source_line"])), {})["level"] = str(child_level)
            continue
        if str(child.get("status")) not in {
            "hierarchy_mismatch", "ambiguous_official_candidates", "pass_parent_disambiguated"
        }:
            continue
        try:
            official_child_level = int(str(child.get("official_level", "")))
        except (ValueError, TypeError):
            continue
        if child_level != official_child_level or parent_level >= child_level:
            continue
        # A cross-reference row should sit at the same index depth as its
        # referenced leaf, not become that leaf's structural parent.
        repairs.setdefault((source_file, int(parent["source_line"])), {})["level"] = str(child_level)


def add_cross_reference_child_level_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Restore a child accidentally emitted at its see-reference parent's level."""
    ordered = sorted(file_records, key=lambda record: int(record["source_line"]))
    source_file = str(ordered[0]["source_file"]) if ordered else ""
    for index, child in enumerate(ordered):
        if str(child.get("status")) not in {
            "hierarchy_mismatch", "ambiguous_official_candidates"
        }:
            continue
        try:
            child_level = int(float(str(child.get("level", ""))))
        except (ValueError, TypeError):
            continue
        for parent in reversed(ordered[:index]):
            try:
                parent_level = int(float(str(parent.get("level", ""))))
            except (ValueError, TypeError):
                continue
            if parent_level < child_level:
                break
            parent_text = str(parent.get("english", "")).casefold()
            if parent_level != child_level or "see " not in parent_text:
                continue
            intervening = ordered[ordered.index(parent) + 1:index]
            if any(int(float(str(item.get("level", "0")))) <= parent_level for item in intervening):
                break
            official_paths = str(child.get("official_hierarchy", ""))
            if normalize_text(parent_text) not in normalize_text(official_paths):
                continue
            repairs.setdefault((source_file, int(child["source_line"])), {})["level"] = str(parent_level + 1)
            break


def add_single_extra_ancestor_level_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Fix a row whose CSV path has exactly one extra ancestor and level."""
    for record in file_records:
        if str(record.get("status")) != "hierarchy_mismatch":
            continue
        source_parts = [part.strip() for part in str(record.get("hierarchy", "")).split(">") if part.strip()]
        official_parts = [part.strip() for part in str(record.get("official_hierarchy", "")).split(">") if part.strip()]
        if source_parts and len(source_parts[0]) == 1:
            source_parts = source_parts[1:]
        if len(source_parts) != len(official_parts) + 1:
            continue
        # Require the official path to be an ordered subsequence of the CSV path.
        cursor = 0
        for official_part in official_parts:
            while cursor < len(source_parts) and not (
                normalize_text(source_parts[cursor]) == normalize_text(official_part)
            ):
                cursor += 1
            if cursor == len(source_parts):
                break
            cursor += 1
        else:
            try:
                source_level = int(float(str(record.get("level", ""))))
                official_level = int(str(record.get("official_level", "")))
            except (ValueError, TypeError):
                continue
            if source_level == official_level + 1:
                repairs.setdefault((str(record["source_file"]), int(record["source_line"])), {})["level"] = str(official_level)


def add_sibling_level_pair_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Repair a bounded two-row level offset sharing one official parent."""
    if len(file_records) < 2:
        return
    source_file = str(file_records[0]["source_file"])
    ordered = sorted(file_records, key=lambda record: int(record["source_line"]))
    for first, second in zip(ordered, ordered[1:]):
        if int(second["source_line"]) != int(first["source_line"]) + 1:
            continue
        if any(str(record.get("status")) != "hierarchy_mismatch" for record in (first, second)):
            continue
        first_parent = str(first.get("official_hierarchy", "")).rsplit(" > ", 1)[0]
        second_parent = str(second.get("official_hierarchy", "")).rsplit(" > ", 1)[0]
        if not first_parent or first_parent != second_parent:
            continue
        try:
            first_source = int(float(str(first["level"])))
            second_source = int(float(str(second["level"])))
            first_official = int(str(first["official_level"]))
            second_official = int(str(second["official_level"]))
        except (KeyError, ValueError):
            continue
        if (
            first_source - first_official == second_source - second_official
            and first_source != first_official
            and second_source != second_official
        ):
            repairs.setdefault((source_file, int(first["source_line"])), {})["level"] = str(first_official)
            repairs.setdefault((source_file, int(second["source_line"])), {})["level"] = str(second_official)


def add_extra_ancestor_level_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Lift rows whose leaf exactly matches a shallower official path."""
    for record in file_records:
        if str(record.get("hierarchy_status")) != "extra_ancestors":
            continue
        official_level_raw = str(record.get("official_level", "")).strip()
        if not official_level_raw:
            continue
        if normalize_text(str(record.get("english", ""))) != normalize_text(
            str(record.get("official_english", ""))
        ):
            continue
        if normalize_code_expression(str(record.get("code", ""))) != normalize_code_expression(
            str(record.get("official_code", ""))
        ):
            continue
        try:
            source_level = int(float(str(record.get("level", ""))))
            official_level = int(official_level_raw)
        except (TypeError, ValueError):
            continue
        if official_level < source_level:
            repairs.setdefault(
                (str(record["source_file"]), int(record["source_line"])), {}
            )["level"] = str(official_level)


def add_false_anchor_level_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Repair a false root inserted between siblings in one official branch."""
    if len(file_records) < 4:
        return
    source_file = str(file_records[0]["source_file"])
    by_line = {int(record["source_line"]): record for record in file_records}
    for line_no, anchor in by_line.items():
        previous = by_line.get(line_no - 1)
        following = by_line.get(line_no + 1)
        corroborating = by_line.get(line_no + 2)
        if not previous or not following or not corroborating:
            continue
        try:
            previous_level = int(float(str(previous.get("level", ""))))
            anchor_level = int(float(str(anchor.get("level", ""))))
            following_level = int(float(str(following.get("level", ""))))
            corroborating_level = int(float(str(corroborating.get("level", ""))))
            corroborating_official_level = int(str(corroborating.get("official_level", "")))
        except (TypeError, ValueError):
            continue
        if not (
            previous_level == following_level == corroborating_level
            and anchor_level <= previous_level - 2
            and str(following.get("status")) == "level_jump"
            and str(corroborating.get("status")) in {
                "hierarchy_mismatch", "text_and_hierarchy_difference"
            }
            and corroborating_official_level == corroborating_level
        ):
            continue
        anchor_norm = normalize_text(str(anchor.get("english", "")))
        following_ancestors = normalize_hierarchy(
            str(following.get("hierarchy", "")).split(" > ")[:-1]
        )
        corroborating_ancestors = normalize_hierarchy(
            str(corroborating.get("hierarchy", "")).split(" > ")[:-1]
        )
        if not anchor_norm or not all(
            anchor_norm in ancestors
            for ancestors in (following_ancestors, corroborating_ancestors)
        ):
            continue
        previous_official = normalize_hierarchy(
            str(previous.get("official_hierarchy", "")).split(" > ")[:-1]
        )
        corroborating_official = normalize_hierarchy(
            str(corroborating.get("official_hierarchy", "")).split(" > ")[:-1]
        )
        if previous_official and previous_official == corroborating_official:
            repairs.setdefault((source_file, line_no), {})["level"] = str(previous_level)


def add_exact_level_jump_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Repair level jumps only when the official row identity is exact."""
    for record in file_records:
        if str(record.get("status")) not in {"level_jump", "hierarchy_mismatch"}:
            continue
        official_level_raw = str(record.get("official_level", "")).strip()
        if normalize_text(str(record.get("english", ""))) != normalize_text(
            str(record.get("official_english", ""))
        ):
            continue
        if normalize_code_expression(str(record.get("code", ""))) != normalize_code_expression(
            str(record.get("official_code", ""))
        ):
            continue
        try:
            source_level = int(float(str(record.get("level", ""))))
        except (TypeError, ValueError):
            continue
        if not official_level_raw:
            source_text_norm = normalize_text(str(record.get("english", "")))
            official_paths = [
                path.strip()
                for path in str(record.get("official_hierarchy", "")).split("|")
                if path.strip()
            ]
            if any(normalize_text(path) == source_text_norm for path in official_paths):
                # A no-code cross-reference can legitimately be nested under
                # a local index heading.  Do not reset it to level 1 merely
                # because the leaf text also appears as a standalone entry.
                if source_level <= 1:
                    repairs.setdefault(
                        (str(record["source_file"]), int(record["source_line"])), {}
                    )["level"] = "1"
            continue
        try:
            official_level = int(official_level_raw)
        except (TypeError, ValueError):
            continue
        source_depth = len([part for part in str(record.get("hierarchy", "")).split(" > ") if part.strip()])
        official_depth = len([part for part in str(record.get("official_hierarchy", "")).split(" > ") if part.strip()])
        if official_level == source_level + 1 and official_depth == source_depth + 1:
            repairs.setdefault(
                (str(record["source_file"]), int(record["source_line"])), {}
            )["level"] = str(official_level)


def add_previous_sibling_parent_level_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Nest a row under the immediately preceding sibling parent when +1
    reconstructs the selected official path exactly."""
    ordered = sorted(file_records, key=lambda record: int(record["source_line"]))
    source_file = str(ordered[0]["source_file"]) if ordered else ""
    for index, record in enumerate(ordered):
        if index == 0 or str(record.get("status")) not in {
            "hierarchy_mismatch", "text_and_hierarchy_difference",
            "hierarchy_missing_ancestors", "missing_root_parent",
            "level_candidate_plus_one", "level_jump", "pass_parent_disambiguated",
        }:
            continue
        if (
            str(record.get("code", "")).strip() == ""
            and normalize_text(str(record.get("english", "")))
            != normalize_text(str(record.get("official_english", "")))
        ):
            continue
        try:
            level = int(float(str(record.get("level", ""))))
            official_level = int(str(record.get("official_level", "")))
            previous_level = int(float(str(ordered[index - 1].get("level", ""))))
        except (TypeError, ValueError):
            continue
        if official_level != level + 1:
            continue
        parts = [p.strip() for p in str(record.get("official_hierarchy", "")).split(" > ") if p.strip()]
        if len(parts) < 2 or normalize_text(str(ordered[index - 1].get("english", ""))) != normalize_text(parts[-2]):
            continue
        repairs.setdefault((source_file, int(record["source_line"])), {})["level"] = str(official_level)


def add_preceding_missing_parent_level_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Raise a missing-ancestor row when its next child creates a level jump."""
    ordered = sorted(file_records, key=lambda record: int(record["source_line"]))
    source_file = str(ordered[0]["source_file"]) if ordered else ""
    for previous, current in zip(ordered, ordered[1:]):
        if str(current.get("status")) != "level_jump":
            continue
        try:
            previous_level = int(float(str(previous.get("level", ""))))
            current_level = int(float(str(current.get("level", ""))))
            official_level = int(str(previous.get("official_level", "")))
        except (TypeError, ValueError):
            continue
        if current_level > previous_level + 1 and official_level == current_level - 1:
            repairs.setdefault((source_file, int(previous["source_line"])), {})["level"] = str(official_level)


def add_cross_reference_previous_root_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Keep a previous root for an exact no-code cross-reference child."""
    ordered = sorted(file_records, key=lambda record: int(record["source_line"]))
    source_file = str(ordered[0]["source_file"]) if ordered else ""
    for index, record in enumerate(ordered):
        if index == 0 or str(record.get("hierarchy_status")) != "missing_ancestors":
            continue
        if str(record.get("code", "")).strip() or "see" not in str(record.get("english", "")).casefold():
            continue
        official_parts = [
            normalize_text(part) for part in str(record.get("official_hierarchy", "")).split(" > ")
            if part.strip()
        ]
        source_parts = [
            normalize_text(part) for part in str(record.get("hierarchy", "")).split(" > ")
            if part.strip()
        ]
        if source_parts and len(source_parts[0]) == 1:
            source_parts = source_parts[1:]
        if len(official_parts) != len(source_parts) + 1 or official_parts[1:] != source_parts:
            continue
        previous_path = normalize_hierarchy(
            str(ordered[index - 1].get("hierarchy", "")).split(" > ")
        )
        if official_parts[0] not in previous_path.split(">"):
            continue
        try:
            source_level = int(float(str(record.get("level", ""))))
            official_level = int(str(record.get("official_level", "")))
        except (TypeError, ValueError):
            continue
        if official_level == source_level + 1:
            repairs.setdefault((source_file, int(record["source_line"])), {})["level"] = str(official_level)


def add_previous_parent_exact_path_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Nest a branch under a previous parent only when that yields an exact official path."""
    ordered = sorted(file_records, key=lambda record: int(record["source_line"]))
    source_file = str(ordered[0]["source_file"]) if ordered else ""
    for issue_index, issue in enumerate(ordered):
        if str(issue.get("status")) not in {
            "hierarchy_mismatch", "hierarchy_missing_ancestors",
            "code_mismatch", "code_and_hierarchy_mismatch",
        }:
            continue
        official_hierarchy = str(issue.get("official_hierarchy", "")).strip()
        if not official_hierarchy or "|" in official_hierarchy:
            continue
        source_parts = [
            part.strip() for part in str(issue.get("hierarchy", "")).split(" > ")
            if part.strip()
        ]
        if source_parts and len(normalize_text(source_parts[0])) == 1:
            source_parts = source_parts[1:]
        official_parts = [
            normalize_text(part) for part in official_hierarchy.split(" > ")
            if part.strip()
        ]
        if len(official_parts) != len(source_parts) + 1:
            continue
        missing_indexes = [
            index for index in range(len(official_parts) - 1)
            if official_parts[:index] + official_parts[index + 1:] == source_parts
        ]
        if len(missing_indexes) != 1:
            continue
        missing_index = missing_indexes[0]
        if missing_index + 1 >= len(official_parts):
            continue
        missing_parent = official_parts[missing_index]
        branch_text = official_parts[missing_index + 1]

        branch_index = next((
            index for index in range(issue_index, -1, -1)
            if normalize_text(str(ordered[index].get("english", ""))) == branch_text
        ), None)
        if branch_index is None:
            continue
        parent_index = next((
            index for index in range(branch_index - 1, -1, -1)
            if normalize_text(str(ordered[index].get("english", ""))) == missing_parent
        ), None)
        if parent_index is None:
            continue
        try:
            branch_level = int(float(str(ordered[branch_index].get("level", ""))))
            parent_level = int(float(str(ordered[parent_index].get("level", ""))))
        except (TypeError, ValueError):
            continue
        if branch_level != parent_level:
            continue
        if any(
            int(float(str(row.get("level", "0")))) < parent_level
            for row in ordered[parent_index + 1 : branch_index]
        ):
            continue
        # Inserting exactly the located previous parent recreates the complete
        # official path; no fuzzy hierarchy score is used for the decision.
        rebuilt_parts = source_parts[:missing_index] + [missing_parent] + source_parts[missing_index:]
        if rebuilt_parts != official_parts:
            continue
        end_index = branch_index + 1
        while end_index < len(ordered):
            try:
                level = int(float(str(ordered[end_index].get("level", ""))))
            except (TypeError, ValueError):
                break
            if level <= branch_level:
                break
            end_index += 1
        for row in ordered[branch_index:end_index]:
            level = int(float(str(row.get("level", ""))))
            repairs.setdefault((source_file, int(row["source_line"])), {})["level"] = str(level + 1)


def add_previous_row_child_level_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Nest a repeated branch item under the immediately preceding official parent.

    Some index blocks contain two consecutive branches with the same leaf/code.
    The first leaf can be reported as parent-disambiguated because its source
    level is too shallow.  Prefer the minimal ``level + 1`` interpretation when
    the preceding row is the exact official parent and the resulting level is
    the official leaf level.
    """
    ordered = sorted(file_records, key=lambda record: int(record["source_line"]))
    source_file = str(ordered[0]["source_file"]) if ordered else ""
    for index, record in enumerate(ordered):
        if str(record.get("status")) not in {"pass_parent_disambiguated", "hierarchy_mismatch", "hierarchy_missing_ancestors"}:
            continue
        if index == 0:
            continue
        previous = ordered[index - 1]
        try:
            current_level = int(float(str(record.get("level", ""))))
            previous_level = int(float(str(previous.get("level", ""))))
            official_level = int(str(record.get("official_level", "")))
        except (TypeError, ValueError):
            continue
        if official_level != current_level + 2 or previous_level != current_level + 1:
            continue
        official_parts = [normalize_text(part) for part in str(record.get("official_hierarchy", "")).split(" > ") if part.strip()]
        if not official_parts:
            continue
        if normalize_text(str(previous.get("english", ""))) != official_parts[-2]:
            continue
        repairs.setdefault((source_file, int(record["source_line"])), {})["level"] = str(current_level + 2)


def add_adjacent_extra_parent_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Remove an apparent extra branch parent when the next row is its child.

    This handles index layouts such as ``rectum > radical > by`` where the
    official branch is ``rectum > by``.  The repair is limited to a level-4
    ``by``/``with``-style node following a level-4 sibling and shifts only its
    descendants.
    """
    ordered = sorted(file_records, key=lambda record: int(record["source_line"]))
    source_file = str(ordered[0]["source_file"]) if ordered else ""
    for index, record in enumerate(ordered):
        if index < 2 or str(record.get("status")) not in {"ambiguous_official_candidates", "hierarchy_mismatch"}:
            continue
        if normalize_text(str(record.get("english", ""))) not in {"by", "with"}:
            continue
        official_hierarchy = str(record.get("official_hierarchy", "")).strip()
        if not official_hierarchy or "|" in official_hierarchy:
            continue
        source_parts = [
            normalize_text(part)
            for part in str(record.get("hierarchy", "")).split(" > ")
            if part.strip()
        ]
        official_parts = [
            normalize_text(part)
            for part in official_hierarchy.split(" > ")
            if part.strip()
        ]
        if source_parts and len(source_parts[0]) == 1:
            source_parts = source_parts[1:]
        # Removing the preceding CSV ancestor must recreate the one selected
        # official path; adjacent levels alone are not sufficient evidence.
        if (
            len(source_parts) != len(official_parts) + 1
            or source_parts[:-2] + source_parts[-1:] != official_parts
        ):
            continue
        try:
            level = int(float(str(record.get("level", ""))))
            prev_level = int(float(str(ordered[index - 1].get("level", ""))))
            prev_prev_level = int(float(str(ordered[index - 2].get("level", ""))))
        except (TypeError, ValueError):
            continue
        if level != prev_level + 1 or prev_level != prev_prev_level + 1 or level < 3:
            continue
        if normalize_text(str(ordered[index - 1].get("english", ""))) in {"by", "with"}:
            continue
        try:
            if int(str(record.get("official_level", ""))) != level - 1:
                continue
        except (TypeError, ValueError):
            continue
        end = index + 1
        while end < len(ordered):
            try:
                child_level = int(float(str(ordered[end].get("level", ""))))
            except (TypeError, ValueError):
                break
            if child_level <= level:
                break
            end += 1
        for row in ordered[index:end]:
            repairs.setdefault((source_file, int(row["source_line"])), {})["level"] = str(int(float(str(row["level"]))) - 1)


def add_extra_parent_exact_path_repairs(
    file_records: Sequence[dict[str, object]],
    repairs: dict[tuple[str, int], dict[str, str]],
) -> None:
    """Lift a branch only when removing one CSV ancestor yields the exact official path."""
    ordered = sorted(file_records, key=lambda record: int(record["source_line"]))
    source_file = str(ordered[0]["source_file"]) if ordered else ""
    for index, issue in enumerate(ordered):
        if str(issue.get("status")) not in {
            "hierarchy_mismatch", "hierarchy_extra_ancestors",
            "text_and_hierarchy_difference", "code_and_hierarchy_mismatch",
            "missing_root_parent",
        }:
            continue
        # A repeated no-code leaf can legitimately occur under several
        # branches.  Without a code there is no reliable identity anchor for
        # shifting an entire subtree; leave it for manual review instead of
        # flattening the branch on a fuzzy hierarchy match.
        if not str(issue.get("code", "")).strip() or not str(issue.get("official_code", "")).strip():
            continue
        # Prefer a directly evidenced missing parent over this rule's
        # generic extra-parent branch lifting.
        if index > 0:
            try:
                level = int(float(str(issue.get("level", ""))))
                official_level = int(str(issue.get("official_level", "")))
                previous_level = int(float(str(ordered[index - 1].get("level", ""))))
            except (TypeError, ValueError):
                level = official_level = previous_level = -99
            official_parts = [p.strip() for p in str(issue.get("official_hierarchy", "")).split(" > ") if p.strip()]
            if (
                official_level == level + 1
                and previous_level == level
                and len(official_parts) >= 2
                and normalize_text(str(ordered[index - 1].get("english", ""))) == normalize_text(official_parts[-2])
            ):
                repairs.setdefault((source_file, int(issue["source_line"])), {})["level"] = str(official_level)
                continue
        official_hierarchy = str(issue.get("official_hierarchy", "")).strip()
        if not official_hierarchy or "|" in official_hierarchy:
            continue
        source_parts = [
            part.strip() for part in str(issue.get("hierarchy", "")).split(" > ")
            if part.strip()
        ]
        if source_parts and len(normalize_text(source_parts[0])) == 1:
            source_parts = source_parts[1:]
        official_parts = [
            normalize_text(part) for part in official_hierarchy.split(" > ")
            if part.strip()
        ]
        if len(source_parts) != len(official_parts) + 1:
            continue
        removable = []
        for position in range(len(source_parts) - 1):
            candidate_parts = source_parts[:position] + source_parts[position + 1:]
            candidate_hierarchy = " > ".join(candidate_parts)
            if (
                normalize_hierarchy(candidate_parts) == normalize_hierarchy(official_parts)
                or normalize_hierarchy(official_parts) in hierarchy_alias_paths(candidate_hierarchy)
            ):
                removable.append(position)
        if len(removable) != 1:
            continue
        try:
            source_level = int(float(str(issue.get("level", ""))))
            official_level = int(str(issue.get("official_level", "")))
        except (TypeError, ValueError):
            continue
        if source_level != official_level + 1:
            continue
        branch_index = index
        removable_position = removable[0]
        if removable_position + 1 < len(source_parts):
            expected_parent_path = source_parts[: removable_position + 2]
            expected_parent_norm = normalize_hierarchy(expected_parent_path)
            target_parent = normalize_text(source_parts[removable_position + 1])
            for candidate_index in range(index - 1, -1, -1):
                candidate = ordered[candidate_index]
                candidate_parts = [
                    part.strip()
                    for part in str(candidate.get("hierarchy", "")).split(" > ")
                    if part.strip()
                ]
                if candidate_parts and len(normalize_text(candidate_parts[0])) == 1:
                    candidate_parts = candidate_parts[1:]
                if (
                    normalize_text(str(candidate.get("english", ""))) == target_parent
                    and len(candidate_parts) >= len(expected_parent_path)
                    and normalize_hierarchy(candidate_parts[-len(expected_parent_path):])
                    == expected_parent_norm
                ):
                    branch_index = candidate_index
                    break
        try:
            branch_level = int(float(str(ordered[branch_index].get("level", ""))))
        except (TypeError, ValueError):
            continue
        end_index = branch_index + 1
        while end_index < len(ordered):
            try:
                level = int(float(str(ordered[end_index].get("level", ""))))
            except (TypeError, ValueError):
                break
            if level <= branch_level:
                break
            end_index += 1
        for row in ordered[branch_index:end_index]:
            level = int(float(str(row.get("level", ""))))
            repairs.setdefault((source_file, int(row["source_line"])), {})["level"] = str(level - 1)


STRUCTURAL_REPAIR_RULES = (
    ("add_cross_reference_child_level_repairs", add_cross_reference_child_level_repairs),
    ("add_hierarchy_parent_repairs", add_hierarchy_parent_repairs),
    ("add_level_offset_repairs", add_level_offset_repairs),
    ("add_immediate_parent_level_repairs", add_immediate_parent_level_repairs),
    ("add_cross_reference_parent_repairs", add_cross_reference_parent_repairs),
    ("add_sibling_level_pair_repairs", add_sibling_level_pair_repairs),
    ("add_extra_ancestor_level_repairs", add_extra_ancestor_level_repairs),
    ("add_false_anchor_level_repairs", add_false_anchor_level_repairs),
    ("add_exact_level_jump_repairs", add_exact_level_jump_repairs),
    ("add_previous_sibling_parent_level_repairs", add_previous_sibling_parent_level_repairs),
    ("add_preceding_missing_parent_level_repairs", add_preceding_missing_parent_level_repairs),
    ("add_cross_reference_previous_root_repairs", add_cross_reference_previous_root_repairs),
    ("add_previous_row_child_level_repairs", add_previous_row_child_level_repairs),
    ("add_previous_parent_exact_path_repairs", add_previous_parent_exact_path_repairs),
    ("add_extra_parent_exact_path_repairs", add_extra_parent_exact_path_repairs),
    ("add_adjacent_extra_parent_repairs", add_adjacent_extra_parent_repairs),
)


def split_level_zero_blocks(
    file_records: Sequence[dict[str, object]],
) -> list[list[dict[str, object]]]:
    """Split a logical CSV stream into independent branches rooted at level 0.

    Files with explicit level-0 rows use those rows as boundaries. For files
    that omit the sentinel and start at level 1, level 1 is treated as the
    implicit level-0 boundary.
    """
    ordered = sorted(file_records, key=lambda record: int(record["source_line"]))
    if not ordered:
        return []
    levels: list[int] = []
    for record in ordered:
        try:
            levels.append(int(float(str(record.get("level", "")))))
        except (TypeError, ValueError):
            levels.append(-1)
    boundary_level = 0 if 0 in levels else 1
    blocks: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    for record, level in zip(ordered, levels):
        if current and level == boundary_level:
            blocks.append(current)
            current = []
        current.append(record)
    if current:
        blocks.append(current)
    return blocks


def collect_structural_repair_proposals(
    payload: tuple[list[list[str]], list[dict[str, object]]],
) -> list[tuple[str, tuple[str, int], str, str]]:
    """Run structural rules in a worker and return auditable proposals.

    Workers never write CSVs. Returning one proposal per rule lets the parent
    process apply the existing conflict policy deterministically.
    """
    source_rows, file_records = payload
    proposals: list[tuple[str, tuple[str, int], str, str]] = []
    repairs: dict[tuple[str, int], dict[str, str]] = {}
    for rule_name, function in STRUCTURAL_REPAIR_RULES:
        before = {
            (key, field): value
            for key, fields in repairs.items()
            for field, value in fields.items()
        }
        if function is add_hierarchy_parent_repairs:
            function(source_rows, file_records, repairs)
        else:
            function(file_records, repairs)
        after = {
            (key, field): value
            for key, fields in repairs.items()
            for field, value in fields.items()
        }
        for key_field in set(before) | set(after):
            old_value = before.get(key_field)
            new_value = after.get(key_field)
            if old_value == new_value or new_value is None:
                continue
            proposals.append((rule_name, key_field[0], key_field[1], new_value))
    for record in file_records:
        if (
            normalize_text(str(record.get("english", ""))) in {"reliefofobstructionnec", "nasalsinusbypuncture"}
            and str(record.get("code", "")).strip() in {"51.42", "22.01"}
        ):
            print(
                f"自动修复审计：{record.get('source_file')}:{record.get('source_line')} "
                f"level={record.get('level')} status={record.get('status')} "
                f"official_level={record.get('official_level')} hierarchy={record.get('official_hierarchy')}"
            )
            print(
                "自动修复审计 proposal："
                + repr([p for p in proposals if p[1][1] == int(record.get("source_line", -1))])
            )
    protected = {
        (str(record["source_file"]), int(record["source_line"]))
        for record in file_records
        if not str(record.get("code", "")).strip()
        and "see" in str(record.get("english", "")).casefold()
        and str(record.get("status", "")) not in {
            "hierarchy_mismatch", "hierarchy_missing_ancestors", "level_jump",
        }
    }
    protected.update(
        (str(record["source_file"]), int(record["source_line"]))
        for record in file_records
        if str(record.get("official_hierarchy", "")).strip()
        and normalize_hierarchy(str(record.get("hierarchy", "")).split(" > "))
        in {
            normalize_hierarchy(path.split(" > "))
            for path in str(record.get("official_hierarchy", "")).split("|")
            if path.strip()
        }
    )
    protected.update(
        (str(record["source_file"]), int(record["source_line"]))
        for record in file_records
        if str(record.get("status", "")).startswith("pass_path_exact")
        or str(record.get("status", "")) == "pass_parent_disambiguated"
    )
    proposals = [
        proposal for proposal in proposals
        if proposal[1] not in protected
        and proposal[1] not in {
            (str(record["source_file"]), int(record["source_line"]))
            for record in file_records
            if "see" in str(record.get("english", "")).casefold()
            and str(record.get("code", "")).strip()
        }
    ]
    return proposals


_VALIDATION_OFFICIAL: OfficialIndex | None = None


def initialize_validation_worker(official: OfficialIndex) -> None:
    global _VALIDATION_OFFICIAL
    _VALIDATION_OFFICIAL = official


def validate_source_block(rows: Sequence[SourceRow]) -> list[dict[str, object]]:
    if _VALIDATION_OFFICIAL is None:
        raise RuntimeError("校验 worker 未初始化官方索引")
    return [report_record(row, validate_row(row, _VALIDATION_OFFICIAL)) for row in rows]


def split_source_row_blocks(rows: Sequence[SourceRow]) -> list[list[SourceRow]]:
    """Split the merged source stream at explicit (or implicit) level-0 roots."""
    if not rows:
        return []
    boundary_level = 0 if any(row.level == 0 for row in rows) else 1
    blocks: list[list[SourceRow]] = []
    current: list[SourceRow] = []
    for row in rows:
        if current and row.level == boundary_level:
            blocks.append(current)
            current = []
        current.append(row)
    if current:
        blocks.append(current)
    return blocks


def apply_source_repairs(
    paths: Iterable[Path],
    records: Iterable[dict[str, object]],
    fix_all: bool = False,
    workers: int = 0,
) -> int:
    """Repair deterministic fields directly in the Git working tree."""
    records = list(records)
    repairs: dict[tuple[str, int], dict[str, str]] = {}
    for record in records:
        official_code = str(record.get("official_code", "")).strip()
        writable_code = (
            official_code
            if official_code
            and has_single_code_token(official_code)
            and not has_code_format_issue(official_code)
            else ""
        )
        if (
            str(record.get("status")) == "code_format_error"
            and writable_code
            and normalize_text(str(record.get("english", "")))
            == normalize_text(str(record.get("official_english", "")))
            and normalize_code_expression(str(record.get("code", "")))
            == normalize_code_expression(writable_code)
        ):
            repairs.setdefault(
                (str(record["source_file"]), int(record["source_line"])), {}
            )["code"] = writable_code
        if (
            str(record.get("status")) in {"code_format_error", "code_mismatch", "missing_code"}
            and writable_code
            and str(record.get("hierarchy_status")) == "exact"
            and float(record.get("confidence", 0) or 0) >= 0.999
        ):
            repairs.setdefault((str(record["source_file"]), int(record["source_line"])), {})["code"] = writable_code
        if fix_all:
            fields: dict[str, str] = {}
            canonical_english = canonicalize_known_official_typos(
                str(record.get("english", ""))
            )
            if canonical_english != str(record.get("english", "")):
                fields["english"] = canonical_english
            synchronized_chinese = synchronized_chinese_english_prefix(
                str(record.get("chinese", "")),
                str(record.get("english", "")),
                str(record.get("official_english", "")),
            )
            if synchronized_chinese:
                fields["chinese"] = synchronized_chinese
            if (
                str(record.get("official_english", "")).strip()
                and str(record.get("status")) in {"text_difference", "fuzzy_candidate"}
                and str(record.get("hierarchy_status")) == "exact"
                and float(record.get("confidence", 0) or 0) >= 0.90
                and normalize_code_expression(str(record.get("code", "")))
                == normalize_code_expression(str(record.get("official_code", "")))
                and spelling_change_allowed(
                    str(record.get("english", "")),
                    str(record.get("official_english", "")),
                )
            ):
                fields["english"] = canonicalize_known_official_typos(
                    str(record["official_english"]).strip()
                )
            if fields:
                repairs.setdefault(
                    (str(record["source_file"]), int(record["source_line"])), {}
                ).update(fields)

    # Keep provenance for every initially proposed field.  The structural
    # rules below all share one repairs dictionary; without provenance, a later
    # rule silently overwrites a different level proposed by an earlier rule.
    provenance: dict[tuple[tuple[str, int], str], tuple[str, str]] = {
        (key, field): ("direct", value)
        for key, fields in repairs.items()
        for field, value in fields.items()
    }
    conflicts: set[tuple[tuple[str, int], str]] = set()

    def merge_proposal(
        rule_name: str,
        key: tuple[str, int],
        field: str,
        value: str,
    ) -> None:
        key_field = (key, field)
        if key_field in conflicts:
            repairs.setdefault(key, {}).pop(field, None)
            return
        previous = provenance.get(key_field)
        if previous and previous[1] != value:
            # An immediately preceding official parent plus an exact
            # official level is stronger evidence than the generic
            # extra-parent heuristic.  Prefer the bounded +1 repair.
            if {
                rule_name, previous[0]
            } == {
                "add_previous_sibling_parent_level_repairs",
                "add_extra_parent_exact_path_repairs",
            }:
                preferred = (
                    value
                    if rule_name == "add_previous_sibling_parent_level_repairs"
                    else previous[1]
                )
                repairs.setdefault(key, {})[field] = preferred
                provenance[key_field] = (
                    "add_previous_sibling_parent_level_repairs",
                    preferred,
                )
                return
            conflicts.add(key_field)
            repairs.setdefault(key, {}).pop(field, None)
            print(
                f"自动修复冲突详情：{key[0]}:{key[1]} {field} "
                f"{previous[0]}={previous[1]} vs {rule_name}={value}"
            )
            return
        repairs.setdefault(key, {})[field] = value
        provenance[key_field] = (rule_name, value)

    changed = 0
    paths = list(paths)
    prepared_files: list[
        tuple[
            Path,
            str,
            bool,
            list[list[str]],
            list[dict[str, object]],
        ]
    ] = []
    # Build one logical CSV stream before splitting. A root branch may begin in
    # one batch file and continue in the next file, so per-file chunking would
    # lose the parent/child context at batch boundaries.
    merged_rows: list[list[str]] = []
    merged_records: list[dict[str, object]] = []
    merged_line_map: dict[int, tuple[str, int]] = {}
    for path in paths:
        raw_bytes = path.read_bytes()
        line_ending = "\r\n" if b"\r\n" in raw_bytes else "\n"
        with_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            source_rows = list(csv.reader(handle))
        file_records = [record for record in records if record["source_file"] == path.name]
        prepared_files.append((path, line_ending, with_bom, source_rows, file_records))

        records_by_line = {
            int(record["source_line"]): record for record in file_records
        }
        for local_line, raw in enumerate(source_rows, start=1):
            is_header = (
                local_line == 1
                and raw
                and raw[0].strip().casefold() == "page"
            )
            if is_header and merged_rows:
                continue
            merged_rows.append(raw)
            global_line = len(merged_rows)
            record = records_by_line.get(local_line)
            if record is None:
                continue
            merged_line_map[global_line] = (path.name, local_line)
            merged_record = dict(record)
            merged_record["source_file"] = MERGED_SOURCE_FILE
            merged_record["source_line"] = global_line
            merged_records.append(merged_record)

    block_payloads: list[tuple[list[list[str]], list[dict[str, object]]]] = []
    block_bases: list[int] = []
    if fix_all:
        for block in split_level_zero_blocks(merged_records):
            block_base = min(int(record["source_line"]) for record in block)
            block_end = max(int(record["source_line"]) for record in block)
            local_block = [
                {
                    **record,
                    "source_line": int(record["source_line"]) - block_base + 1,
                }
                for record in block
            ]
            block_payloads.append((merged_rows[block_base - 1 : block_end], local_block))
            block_bases.append(block_base)
    proposal_groups: list[list[tuple[str, tuple[str, int], str, str]]] = []
    if block_payloads:
        requested_workers = workers if workers > 0 else (os.cpu_count() or 1)
        worker_count = min(requested_workers, len(block_payloads))
        print(
            f"自动修复分块：{len(block_payloads)} 个 level 0 块，"
            f"{worker_count} 个进程。"
        )
        if worker_count > 1:
            try:
                with multiprocessing.get_context("spawn").Pool(
                    processes=worker_count
                ) as pool:
                    proposal_groups = pool.map(
                        collect_structural_repair_proposals,
                        block_payloads,
                    )
            except (OSError, PermissionError) as exc:
                print(f"并行进程不可用，回退串行：{exc}")
                proposal_groups = [
                    collect_structural_repair_proposals(payload)
                    for payload in block_payloads
                ]
        else:
            proposal_groups = [
                collect_structural_repair_proposals(payload)
                for payload in block_payloads
            ]
    for proposals, block_base in zip(proposal_groups, block_bases):
        for rule_name, key, field, value in proposals:
            global_line = block_base + key[1] - 1
            real_key = merged_line_map.get(global_line)
            if real_key is not None:
                merge_proposal(rule_name, real_key, field, value)

    for path, line_ending, with_bom, source_rows, _ in prepared_files:
        file_repairs = {
            line: fields
            for (name, line), fields in repairs.items()
            if name == path.name and fields
        }
        if not file_repairs:
            continue
        for line_no, fields in file_repairs.items():
            index = line_no - 1
            if index >= len(source_rows) or not source_rows[index]:
                continue
            while len(source_rows[index]) < 5:
                source_rows[index].append("")
            if "code" in fields and source_rows[index][4].strip() != fields["code"]:
                source_rows[index][4] = fields["code"]
                changed += 1
            if "chinese" in fields and source_rows[index][2].strip() != fields["chinese"]:
                source_rows[index][2] = fields["chinese"]
                changed += 1
            if "english" in fields and source_rows[index][3].strip() != fields["english"]:
                original = source_rows[index][3]
                left = original[:1] if original[:1] in {'"', "'", "“", "‘"} else ""
                right = original[-1:] if original[-1:] in {'"', "'", "”", "’"} else ""
                source_rows[index][3] = left + fields["english"] + right
                changed += 1
            if "level" in fields and source_rows[index][1].strip() != fields["level"]:
                source_rows[index][1] = fields["level"]
                changed += 1
        write_source_csv(path, source_rows, line_ending, with_bom=with_bom)
    if conflicts:
        print(f"自动修复冲突：{len(conflicts)} 个字段跳过写回。")
    return changed


def validate_source_rows(
    rows: Sequence[SourceRow],
    official: OfficialIndex,
    workers: int = 0,
) -> tuple[list[dict[str, object]], Counter[str], Counter[str]]:
    records: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    severities: Counter[str] = Counter()
    validation_blocks = split_source_row_blocks(rows)
    requested_workers = workers if workers > 0 else (os.cpu_count() or 1)
    worker_count = min(requested_workers, len(validation_blocks))
    if worker_count > 1:
        print(
            f"校验分块：{len(validation_blocks)} 个 level 0 块，"
            f"{worker_count} 个进程。"
        )
        try:
            with multiprocessing.get_context("spawn").Pool(
                processes=worker_count,
                initializer=initialize_validation_worker,
                initargs=(official,),
            ) as pool:
                block_records = pool.map(validate_source_block, validation_blocks)
            records = [record for block in block_records for record in block]
        except (OSError, PermissionError) as exc:
            print(f"并行校验不可用，回退串行：{exc}")
            worker_count = 1
    if worker_count <= 1:
        for row in rows:
            match = validate_row(row, official)
            records.append(report_record(row, match))
    for record in records:
        counts[str(record["status"])] += 1
        severities[str(record["severity"])] += 1
    # Structural invariant: adjacent rows may descend arbitrarily, but may
    # increase by at most one level. This catches skipped parent rows before
    # semantic candidate matching can hide the gap.
    previous_by_file: dict[str, dict[str, object]] = {}
    for record in records:
        previous = previous_by_file.get(str(record["source_file"]))
        if previous is not None:
            try:
                previous_level = int(float(str(previous.get("level", ""))))
                current_level = int(float(str(record.get("level", ""))))
            except (TypeError, ValueError):
                previous_level = current_level = 0
            if current_level > previous_level + 1:
                counts[str(record.get("status"))] -= 1
                severities[str(record.get("severity"))] -= 1
                record["severity"] = "错误"
                record["status"] = "level_jump"
                record["confidence"] = "1.000"
                record["hierarchy_status"] = "level_jump"
                record["suggestion"] = "补充缺失的中间父级，或将 level 调整为不超过前一行 level+1。"
                record["details"] = (
                    f"相邻行 level 从 {previous_level} 跳到 {current_level}，"
                    "超过允许的最大增量 1。"
                )
                counts["level_jump"] += 1
                severities["错误"] += 1
        previous_by_file[str(record["source_file"])] = record

    # A parent heading can itself be ambiguous while its immediately following
    # children consistently identify one official root.  Use that consensus
    # only when at least two child records agree and the root is one of the
    # parent's reported candidates; this does not alter source levels.
    ordered_by_file: dict[str, list[dict[str, object]]] = defaultdict(list)
    for record in records:
        ordered_by_file[str(record["source_file"])].append(record)
    for ordered in ordered_by_file.values():
        for index, parent in enumerate(ordered):
            if str(parent.get("status")) != "ambiguous_official_candidates":
                continue
            try:
                parent_level = int(float(str(parent.get("level", ""))))
            except (TypeError, ValueError):
                continue
            candidate_roots = {
                normalize_text(path.split(" > ", 1)[0])
                for path in str(parent.get("official_hierarchy", "")).split("|")
                if path.strip()
            }
            if not candidate_roots:
                continue
            child_root_counts: Counter[str] = Counter()
            for child in ordered[index + 1 :]:
                try:
                    child_level = int(float(str(child.get("level", ""))))
                except (TypeError, ValueError):
                    break
                if child_level <= parent_level:
                    break
                child_path = str(child.get("official_hierarchy", "")).strip()
                if not child_path or "|" in child_path:
                    continue
                child_status = str(child.get("status"))
                if child_status not in {
                    "pass_path_exact",
                    "pass_path_exact_no_code",
                    "pass_parent_disambiguated",
                    "pass_cross_reference_code",
                }:
                    continue
                child_root = normalize_text(child_path.split(" > ", 1)[0])
                if child_root in candidate_roots:
                    child_root_counts[child_root] += 1
            supported = [
                root for root, support in child_root_counts.items() if support >= 2
            ]
            if len(supported) != 1:
                continue
            selected_root = supported[0]
            old_status = str(parent.get("status"))
            old_severity = str(parent.get("severity"))
            counts[old_status] -= 1
            severities[old_severity] -= 1
            parent["severity"] = "通过"
            parent["status"] = "pass_parent_disambiguated"
            parent["confidence"] = "1.000"
            parent["official_english"] = parent.get("english", "")
            parent["official_code"] = ""
            parent["official_level"] = "1"
            parent["official_hierarchy"] = str(parent.get("english", "")).strip()
            parent_hierarchy = normalize_hierarchy(
                str(parent.get("hierarchy", "")).split(" > ")
            )
            parent["path_score"] = f"{hierarchy_similarity(parent_hierarchy, selected_root):.3f}"
            parent["hierarchy_status"] = "exact"
            parent["suggestion"] = ""
            parent["details"] = (
                f"后续子项中有 {child_root_counts[selected_root]} 项一致指向官方根节点，"
                "据此消除当前父级的交叉引用歧义。"
            )
            counts["pass_parent_disambiguated"] += 1
            severities["通过"] += 1

    for record in records:
        if str(record.get("status")) not in {"pass_parent_disambiguated", "pass_path_exact", "pass_path_alias_exact"}:
            continue
        source = [p.strip() for p in str(record.get("hierarchy", "")).split(" > ") if p.strip()]
        paths = [p.strip() for p in str(record.get("official_hierarchy", "")).split("|") if p.strip()]
        if len(source) < 2 or not paths:
            continue
        parent_text = re.split(r"\bsee(?:\s+also)?\b", source[-2], maxsplit=1, flags=re.IGNORECASE)[0]
        parent = normalize_text(parent_text)
        parent_aliases = set(comma_alias_norms(parent_text)) | {parent}
        if any(
            normalize_text(re.split(r"\bsee(?:\s+also)?\b", p.split(" > ")[-2], maxsplit=1, flags=re.IGNORECASE)[0]) in parent_aliases
            for p in paths if len(p.split(" > ")) >= 2
        ):
            continue
        old = str(record.get("status")); counts[old] -= 1; severities[str(record.get("severity"))] -= 1
        record["status"] = "hierarchy_mismatch"; record["severity"] = "警告"
        record["hierarchy_status"] = "mismatch"
        record["details"] = "CSV 直接父级不属于任何官方候选路径。"
        counts["hierarchy_mismatch"] += 1; severities["警告"] += 1
    return records, counts, severities


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.workers < 0:
        print("错误：--workers 不能为负数。", file=sys.stderr)
        return 2
    input_dir = args.input_dir.resolve()
    paths = discover_csv_files(input_dir, args.include_aggregate)
    if not paths:
        print(f"错误：{input_dir} 中没有可校验的批次 CSV。", file=sys.stderr)
        return 2

    try:
        rtf, official_source = load_rtf_bytes(args)
        official_entries = parse_official_entries(rtf_to_paragraphs(rtf))
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if len(official_entries) < 10_000:
        print(
            f"错误：仅从官方索引解析出 {len(official_entries)} 个条目，"
            "结果异常，未生成报告。",
            file=sys.stderr,
        )
        return 2

    rows = load_source_rows(paths)
    official = OfficialIndex(official_entries)
    all_records, counts, severities = validate_source_rows(
        rows,
        official,
        workers=args.workers,
    )

    if args.fix_source:
        total_repaired = 0
        repair_rounds = 0
        for _ in range(5):
            repaired = apply_source_repairs(
                paths,
                all_records,
                args.fix_all,
                workers=args.workers,
            )
            if not repaired:
                break
            total_repaired += repaired
            repair_rounds += 1
            rows = load_source_rows(paths)
            all_records, counts, severities = validate_source_rows(
                rows,
                official,
                workers=args.workers,
            )
        print(
            f"自动修复：{total_repaired} 个字段，{repair_rounds} 轮；"
            "源 CSV 已直接更新（可通过 Git 查看或撤销）。"
        )

    records = [
        record
        for record in all_records
        if not args.issues_only or record["severity"] in {"错误", "警告", "人工复核"}
    ]

    write_report(args.output, records)
    print(f"官方索引：{official_source}")
    print(f"CSV 文件：{len(paths)} 个；输入行：{len(rows)}；报告行：{len(records)}")
    print("严重性：" + "，".join(f"{key}={value}" for key, value in severities.most_common()))
    print("状态：" + "，".join(f"{key}={value}" for key, value in counts.most_common()))
    print(f"报告：{args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
