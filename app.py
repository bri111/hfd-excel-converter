import copy
import io
import json
import re
from pathlib import Path

import pandas as pd
import streamlit as st

REPORT_TYPES_PATH = Path(__file__).parent / "report_types.json"

st.set_page_config(page_title="HFD Excel Converter", layout="centered")


def find_column(df, target):
    target_norm = target.lower().replace(" ", "")
    for col in df.columns:
        if str(col).strip().lower().replace(" ", "") == target_norm:
            return col
    return None


def parse_year(label):
    text = str(label).strip()
    # Whole-string range labels like "FY20-24" or "20-24" are pre-computed
    # combined rows; skip them since the app recomputes the combined column.
    if re.fullmatch(r"(?:FY)?\s*\d{1,2}\s*-\s*\d{1,2}", text, flags=re.IGNORECASE):
        return None
    # Full timestamps (e.g. Excel's time-of-day export "1899-12-30 00:09:59")
    # contain a leading 4-digit number that looks like a year but isn't one —
    # these are data values (see time_to_seconds), not year labels.
    if re.match(r"^\d{4}-\d{1,2}-\d{1,2}([ T]\d{1,2}:\d{2})?", text):
        return None
    match = re.search(r"(\d{4})", text)
    if match:
        return int(match.group(1))
    match = re.fullmatch(r"(?:FY)?\s*(\d{2})", text, flags=re.IGNORECASE)
    if match:
        return 2000 + int(match.group(1))
    return None


def time_to_seconds(value):
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.hour * 3600 + ts.minute * 60 + ts.second


def format_seconds(total_seconds):
    if total_seconds is None:
        return ""
    total_seconds = round(total_seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def format_value(value, fmt):
    if value is None or pd.isna(value):
        return ""
    if fmt == "int":
        return f"{int(round(value)):,}"
    if fmt == "time":
        return format_seconds(value)
    if fmt == "percent":
        return f"{value * 100:.2f}%"
    if fmt == "auto":
        if float(value).is_integer():
            return f"{int(value):,}"
        return f"{value:,.2f}"
    return str(value)


def to_numeric(series):
    return pd.to_numeric(
        series.astype(str).str.replace(",", "", regex=False).str.replace("%", "", regex=False),
        errors="coerce",
    )


# Each report type describes one raw CSV shape: a column holding the FY
# label (or None for an unnamed/first column) and a list of row specs
# describing how to build one output row from the raw columns. Defined in
# report_types.json (next to this file) so new report types can be added
# without touching this code.
#
# Row spec kinds:
#   sum            combined = sum(source); per-year = raw value
#   weighted_time  source holds time-of-day strings; combined = incident-
#                  weighted average seconds; per-year = raw seconds
#   ratio          combined = sum(numerator) / sum(denominator); per-year =
#                  numerator / denominator for that year
ROW_KIND_REQUIRED_KEYS = {
    "sum": {"source", "label", "format"},
    "weighted_time": {"source", "weight", "label"},
    "ratio": {"numerator", "denominator", "label"},
}


def load_report_types(path):
    with open(path, "r", encoding="utf-8") as f:
        report_types = json.load(f)

    for i, report_type in enumerate(report_types):
        for key in ("name", "fy_column", "index_name", "rows"):
            if key not in report_type:
                raise ValueError(f"report_types.json entry {i}: missing required key '{key}'.")
        for row in report_type["rows"]:
            kind = row.get("kind")
            if kind not in ROW_KIND_REQUIRED_KEYS:
                raise ValueError(
                    f"report_types.json entry {i} ('{report_type['name']}'): "
                    f"row has unknown kind '{kind}'. Must be one of {sorted(ROW_KIND_REQUIRED_KEYS)}."
                )
            missing = ROW_KIND_REQUIRED_KEYS[kind] - row.keys()
            if missing:
                raise ValueError(
                    f"report_types.json entry {i} ('{report_type['name']}'): "
                    f"'{kind}' row missing key(s) {sorted(missing)}."
                )

    return report_types


try:
    REPORT_TYPES = load_report_types(REPORT_TYPES_PATH)
except (json.JSONDecodeError, ValueError, OSError) as e:
    st.error(f"Could not load report_types.json: {e}")
    st.stop()


def required_columns(report_type):
    cols = set()
    for row in report_type["rows"]:
        if row["kind"] == "ratio":
            cols.add(row["numerator"])
            cols.add(row["denominator"])
        else:
            cols.add(row["source"])
    return cols


def detect_report_type(df):
    for report_type in REPORT_TYPES:
        if all(find_column(df, col) is not None for col in required_columns(report_type)):
            return report_type
    return None


def _build_row_table(work, year_labels, combined_label, columns, rows, df):
    row_labels = [row["label"] for row in rows]
    summary = pd.DataFrame(index=row_labels, columns=columns)

    for row in rows:
        label = row["label"]

        if row["kind"] == "sum":
            values = to_numeric(work[find_column(df, row["source"])])
            summary.loc[label, combined_label] = format_value(values.sum(), row["format"])
            for col, value in zip(year_labels, values):
                summary.loc[label, col] = format_value(value, row["format"])

        elif row["kind"] == "weighted_time":
            seconds = work[find_column(df, row["source"])].apply(time_to_seconds)
            weights = to_numeric(work[find_column(df, row["weight"])])
            combined = (seconds * weights).sum() / weights.sum()
            summary.loc[label, combined_label] = format_value(combined, "time")
            for col, value in zip(year_labels, seconds):
                summary.loc[label, col] = format_value(value, "time")

        elif row["kind"] == "ratio":
            numerator = to_numeric(work[find_column(df, row["numerator"])])
            denominator = to_numeric(work[find_column(df, row["denominator"])])
            combined = numerator.sum() / denominator.sum()
            summary.loc[label, combined_label] = format_value(combined, "percent")
            for col, num, den in zip(year_labels, numerator, denominator):
                summary.loc[label, col] = format_value(num / den if den else None, "percent")

    return summary


def build_summary(df, report_type):
    fy_col = find_column(df, report_type["fy_column"]) if report_type["fy_column"] else df.columns[0]
    groups, group_col = resolve_year_groups(df, fy_col)

    tables = {}
    for group_label, work, year_labels, combined_label, columns in groups:
        tables[group_label] = _build_row_table(work, year_labels, combined_label, columns, report_type["rows"], df)

    if group_col is None:
        summary = tables[None]
        summary.index.name = report_type["index_name"]
        return summary

    result = pd.concat(tables, names=[group_col, report_type["index_name"]])
    return result


def detect_year_column(df):
    """Pick the column that looks like the FY/year column. Columns named like
    "FY"/"FYLabel"/"Year" are tried first — this matters because a plain
    numeric column (e.g. a Count of 1001) can coincidentally contain
    4-digit-looking "years" and would otherwise tie with the real FY column.
    Within whichever pool is used, columns whose parsed years are unique are
    preferred over ones with a lot of duplicate matches."""
    def _score(cols):
        candidates = []
        for col in cols:
            parsed = df[col].apply(parse_year).dropna()
            if parsed.empty:
                continue
            candidates.append((col, parsed))
        if not candidates:
            return None
        unique_candidates = [(col, parsed) for col, parsed in candidates if parsed.is_unique]
        pool = unique_candidates if unique_candidates else candidates
        best_col, _ = max(pool, key=lambda item: len(item[1]))
        return best_col

    name_hint_cols = [c for c in df.columns if re.search(r"fy|year", str(c), re.IGNORECASE)]
    if name_hint_cols:
        result = _score(name_hint_cols)
        if result is not None:
            return result

    return _score([c for c in df.columns if c not in name_hint_cols])


def detect_group_column(valid, fy_col):
    """Among the rows that have a parsed year (valid["_year"]), find a column
    that disambiguates duplicate years — e.g. a "service area" column where
    each (service area, year) pair is unique even though each year repeats
    once per service area. Prefers the most "categorical" column (fewest
    distinct values) among those that actually resolve the duplication."""
    candidates = []
    n = len(valid)
    for col in valid.columns:
        if col in (fy_col, "_year"):
            continue
        n_unique = valid[col].nunique(dropna=True)
        # Skip columns that are effectively constant (no grouping info) or
        # effectively unique per row (a measurement, not a category).
        if n_unique <= 1 or n_unique >= n:
            continue
        if not valid.duplicated(subset=[col, "_year"]).any():
            candidates.append((col, n_unique))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[1])
    return candidates[0][0]


def resolve_year_groups(df, fy_col):
    """Shared logic for build_summary and build_generic_summary: filter to rows
    with a parseable year, sort descending, and produce the FY column labels.
    If the same year appears more than once (e.g. one row per service area per
    year), auto-detects a grouping column and returns one entry per group
    instead of erroring out. Returns (groups, group_col) where groups is a list
    of (group_label, work, year_labels, combined_label, columns) tuples;
    group_label is None and there is exactly one entry when no grouping is
    needed."""
    work = df.copy()
    work["_year"] = work[fy_col].apply(parse_year)
    valid = work[work["_year"].notna()].copy()
    if valid.empty:
        raise ValueError(f"Could not find any rows with a parseable FY year in column '{fy_col}'.")

    def _axis_for(sub):
        sub = sub.sort_values("_year", ascending=False)
        years = sub["_year"].astype(int).tolist()
        year_labels = [f"FY{y % 100:02d}" for y in years]
        combined_label = f"FY{min(years) % 100:02d}-{max(years) % 100:02d}"
        columns = [combined_label] + year_labels
        return sub, year_labels, combined_label, columns

    if not valid.duplicated(subset=["_year"]).any():
        return [(None, *_axis_for(valid))], None

    group_col = detect_group_column(valid, fy_col)
    if group_col is None:
        years = valid["_year"].astype(int).tolist()
        seen, dupes = set(), []
        for y in years:
            if y in seen and y not in dupes:
                dupes.append(y)
            seen.add(y)
        raise ValueError(
            f"Column '{fy_col}' was detected as the year column, but it produces duplicate "
            f"year(s) {sorted(dupes)} — it's likely not the real FY/year column, or the rows "
            "need to be split by another column (like a service area) that this app couldn't "
            "confidently detect. Rename the year column so it's unambiguous (e.g. 'FY2024'), "
            "make sure any grouping column (e.g. service area) has a small set of repeated "
            "values, and re-upload."
        )

    order = list(dict.fromkeys(df[group_col].astype(str)))
    groups = []
    for group_value, sub in valid.groupby(group_col, sort=False):
        groups.append((str(group_value), *_axis_for(sub)))
    groups.sort(key=lambda g: order.index(g[0]) if g[0] in order else len(order))
    return groups, group_col


def _build_generic_table(work, year_labels, combined_label, columns, df, fy_col, group_col):
    min_valid = max(1, len(work) // 2)
    weight_col = next(
        (c for c in df.columns if c not in (fy_col, group_col) and "count" in str(c).lower()),
        None,
    )
    weights = to_numeric(work[weight_col]) if weight_col else None

    summary_rows = []  # (label, format, per_year_series, combined_value)
    for col in df.columns:
        if col in (fy_col, group_col):
            continue

        numeric_values = to_numeric(work[col])
        if numeric_values.notna().sum() >= min_valid:
            summary_rows.append((col, "auto", numeric_values, numeric_values.sum()))
            continue

        time_values = work[col].apply(time_to_seconds)
        if time_values.notna().sum() >= min_valid:
            if weights is not None and col != weight_col and weights.notna().sum() >= min_valid:
                combined = (time_values * weights).sum() / weights.sum()
            else:
                combined = time_values.mean()
            summary_rows.append((col, "time", time_values, combined))
            continue

    if not summary_rows:
        return None

    summary = pd.DataFrame(index=[label for label, *_ in summary_rows], columns=columns)
    for label, fmt, per_year, combined in summary_rows:
        summary.loc[label, combined_label] = format_value(combined, fmt)
        for col, value in zip(year_labels, per_year):
            summary.loc[label, col] = format_value(value, fmt)

    return summary


def build_generic_summary(df):
    """Fallback for CSVs that don't match any configured report type: auto-detect
    the FY column, then summarize every other column by year. Plain numbers are
    summed/shown as-is; HH:MM:SS-style time columns are parsed and averaged
    (weighted by a "count"-like column when one is present). If the same year
    repeats (e.g. once per service area), auto-detects that grouping column and
    produces one sub-table per group."""
    fy_col = detect_year_column(df)
    if fy_col is None:
        raise ValueError("Could not detect an FY/year column for a generic summary.")

    groups, group_col = resolve_year_groups(df, fy_col)

    tables = {}
    for group_label, work, year_labels, combined_label, columns in groups:
        table = _build_generic_table(work, year_labels, combined_label, columns, df, fy_col, group_col)
        if table is not None:
            tables[group_label] = table

    if not tables:
        raise ValueError("No numeric or time columns found to summarize.")

    if group_col is None:
        summary = tables[None]
        summary.index.name = "Summary"
        return summary

    return pd.concat(tables, names=[group_col, "Summary"])


def _bold(cell):
    font = copy.copy(cell.font)
    font.bold = True
    cell.font = font


def _style_summary_sheet(ws):
    header_row = 1
    for cell in ws[header_row]:
        _bold(cell)

    for row in ws.iter_rows(min_row=header_row + 1, max_col=1):
        for cell in row:
            _bold(cell)

    for col_cells in ws.columns:
        length = max(len(str(c.value)) if c.value is not None else 0 for c in col_cells)
        ws.column_dimensions[col_cells[0].column_letter].width = max(length + 2, 10)


def sanitize_sheet_name(name, used):
    name = re.sub(r"\.csv$", "", name, flags=re.IGNORECASE)
    name = re.sub(r'[:\\/?*\[\]]', "_", name).strip() or "Sheet"
    name = name[:31]
    base, i = name, 2
    while name in used:
        suffix = f"_{i}"
        name = base[: 31 - len(suffix)] + suffix
        i += 1
    used.add(name)
    return name


def to_excel_bytes(summaries):
    """summaries: dict of sheet_name -> summary DataFrame, one sheet per entry."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, summary in summaries.items():
            summary.to_excel(writer, sheet_name=sheet_name)
            _style_summary_sheet(writer.sheets[sheet_name])
    buffer.seek(0)
    return buffer


st.title("HFD Excel Converter")
st.caption("Upload one or more raw CSVs and get back each one's transposed FY summary table.")

uploaded_files = st.file_uploader("Upload CSV(s)", type=["csv"], accept_multiple_files=True)

if uploaded_files:
    results = []
    for uploaded in uploaded_files:
        try:
            raw_df = pd.read_csv(uploaded)
            report_type = detect_report_type(raw_df)
            if report_type is None:
                summary_df = build_generic_summary(raw_df)
                report_type = {"name": "Generic (auto-detected)"}
            else:
                summary_df = build_summary(raw_df, report_type)
        except ValueError as e:
            st.error(f"{uploaded.name}: {e}")
            continue
        results.append((uploaded.name, report_type, summary_df))

    combined_bytes = None
    if len(results) > 1:
        used_names = set()
        combined = {
            sanitize_sheet_name(name, used_names): summary_df for name, _, summary_df in results
        }
        combined_bytes = to_excel_bytes(combined)

    tabs = st.tabs([name for name, _, _ in results]) if len(results) > 1 else [st.container()]
    for (name, report_type, summary_df), tab in zip(results, tabs):
        with tab:
            st.subheader(f"Summary — {name} ({report_type['name']})")
            st.dataframe(summary_df, use_container_width=True)

            sheet_name = sanitize_sheet_name(name, set())
            excel_bytes = to_excel_bytes({sheet_name: summary_df})
            out_name = re.sub(r"\.csv$", "", name, flags=re.IGNORECASE) + "_summary.xlsx"

            if combined_bytes is not None:
                _, col1, col2, _ = st.columns([1, 2, 2, 1])
            else:
                _, col1, _ = st.columns([1, 2, 1])
                col2 = None

            with col1:
                st.download_button(
                    "Download as Excel",
                    data=excel_bytes,
                    file_name=out_name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"download_{name}",
                    use_container_width=True,
                )
            if col2 is not None:
                with col2:
                    st.download_button(
                        "Download all as one Excel file",
                        data=combined_bytes,
                        file_name="hfd_summary_combined.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=f"download_combined_{name}",
                        use_container_width=True,
                    )
else:
    st.info("Upload one or more raw CSVs. The report type (response times, unit workload, etc.) is detected automatically from the columns.")
