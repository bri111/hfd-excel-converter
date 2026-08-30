import io
import re

import pandas as pd
import streamlit as st

st.set_page_config(page_title="HFD Excel Converter", layout="centered")

ROW_LABELS = [
    ("Incident Count", "Incidents"),
    ("Turnout Time", "Turnout Time"),
    ("Travel Time", "Travel Time"),
    ("Total Response Time", "TRT"),
]


def find_column(df, target):
    target_norm = target.lower().replace(" ", "")
    for col in df.columns:
        if str(col).strip().lower().replace(" ", "") == target_norm:
            return col
    return None


def extract_year(label):
    match = re.search(r"(\d{4})", str(label))
    return int(match.group(1)) if match else None


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


def build_summary(df):
    fy_col = find_column(df, "FYLabel")
    incident_col = find_column(df, "Incident Count")
    if fy_col is None or incident_col is None:
        raise ValueError("CSV must contain 'FYLabel' and 'Incident Count' columns.")

    time_cols = {}
    for source_name, _ in ROW_LABELS[1:]:
        col = find_column(df, source_name)
        if col is None:
            raise ValueError(f"CSV is missing expected column: '{source_name}'.")
        time_cols[source_name] = col

    work = df.copy()
    work["_year"] = work[fy_col].apply(extract_year)
    if work["_year"].isna().any():
        raise ValueError("Could not parse a 4-digit year out of every FYLabel value.")

    for source_name, col in time_cols.items():
        work[f"_seconds_{source_name}"] = work[col].apply(time_to_seconds)

    work = work.sort_values("_year", ascending=False)

    years = work["_year"].tolist()
    year_labels = [f"FY{y % 100:02d}" for y in years]
    combined_label = f"FY{min(years) % 100:02d}-{max(years) % 100:02d}"

    columns = [combined_label] + year_labels
    summary = pd.DataFrame(index=[disp for _, disp in ROW_LABELS], columns=columns)

    incidents = work[incident_col].astype(float)
    total_incidents = incidents.sum()

    summary.loc["Incidents", combined_label] = f"{int(total_incidents):,}"
    for label, count in zip(year_labels, incidents):
        summary.loc["Incidents", label] = f"{int(count):,}"

    for source_name, disp in ROW_LABELS[1:]:
        seconds = work[f"_seconds_{source_name}"]
        weighted_avg = (seconds * incidents).sum() / total_incidents
        summary.loc[disp, combined_label] = format_seconds(weighted_avg)
        for label, s in zip(year_labels, seconds):
            summary.loc[disp, label] = format_seconds(s)

    summary.index.name = "90% Fractile"
    return summary


def to_excel_bytes(summary):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", startrow=1)
        ws = writer.sheets["Summary"]
        ws["A1"] = "Summary"
        ws["A1"].font = ws["A1"].font.copy(bold=True)

        header_row = 2
        for cell in ws[header_row]:
            cell.font = cell.font.copy(bold=True)

        for row in ws.iter_rows(min_row=header_row + 1, max_col=1):
            for cell in row:
                cell.font = cell.font.copy(bold=True)

        for col_cells in ws.columns:
            length = max(len(str(c.value)) if c.value is not None else 0 for c in col_cells)
            ws.column_dimensions[col_cells[0].column_letter].width = max(length + 2, 10)

    buffer.seek(0)
    return buffer


st.title("HFD Excel Converter")
st.caption("Upload one or more raw CSVs and get back each one's transposed FY summary table.")

uploaded_files = st.file_uploader("Upload CSV(s)", type=["csv"], accept_multiple_files=True)

if uploaded_files:
    tabs = st.tabs([f.name for f in uploaded_files]) if len(uploaded_files) > 1 else [st.container()]
    for uploaded, tab in zip(uploaded_files, tabs):
        with tab:
            try:
                raw_df = pd.read_csv(uploaded)
                summary_df = build_summary(raw_df)
            except ValueError as e:
                st.error(f"{uploaded.name}: {e}")
                continue

            st.subheader(f"Summary — {uploaded.name}")
            st.dataframe(summary_df, use_container_width=True)

            excel_bytes = to_excel_bytes(summary_df)
            out_name = re.sub(r"\.csv$", "", uploaded.name, flags=re.IGNORECASE) + "_summary.xlsx"
            st.download_button(
                "Download as Excel",
                data=excel_bytes,
                file_name=out_name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"download_{uploaded.name}",
            )
else:
    st.info("Upload one or more CSVs with FYLabel, Incident Count, Turnout Time, Travel Time, and Total Response Time columns.")
