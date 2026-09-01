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


def build_summary(df, report_type):
    fy_col = find_column(df, report_type["fy_column"]) if report_type["fy_column"] else df.columns[0]

    work = df.copy()
    work["_year"] = work[fy_col].apply(parse_year)
    work = work[work["_year"].notna()]
    if work.empty:
        raise ValueError("Could not find any rows with a parseable FY year.")

    work = work.sort_values("_year", ascending=False)

    years = work["_year"].astype(int).tolist()
    year_labels = [f"FY{y % 100:02d}" for y in years]
    combined_label = f"FY{min(years) % 100:02d}-{max(years) % 100:02d}"
    columns = [combined_label] + year_labels

    row_labels = [row["label"] for row in report_type["rows"]]
    summary = pd.DataFrame(index=row_labels, columns=columns)

    for row in report_type["rows"]:
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

    summary.index.name = report_type["index_name"]
    return summary


def _bold(cell):
    font = copy.copy(cell.font)
    font.bold = True
    cell.font = font


def _style_summary_sheet(ws, title):
    ws["A1"] = title
    _bold(ws["A1"])

    header_row = 2
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
            summary.to_excel(writer, sheet_name=sheet_name, startrow=1)
            _style_summary_sheet(writer.sheets[sheet_name], "Summary")
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
                raise ValueError(
                    "Unrecognized CSV format — no known report type matches these columns."
                )
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
