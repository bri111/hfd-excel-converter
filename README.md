# HFD Excel Converter

A Streamlit app that turns raw HFD CSV exports into transposed, formatted FY summary tables you can download as Excel.

You give it a CSV with one row per fiscal year. It flips it so metrics become rows and years become columns (most recent year first), adds a combined "all years" column, and hands back a formatted `.xlsx`.

There are two kinds of modes:

- **General** — a fluid, no-configuration fallback. Works on basically any row-per-FY CSV: after you upload, you get a checklist (one row per column) where you pick which columns to include and how each should combine across years — Sum, Average, or Ratio (%). Sensible defaults are pre-filled based on each column's content, and the table updates live as you change them. This is the default for any file that doesn't match a specific mode below.
- **Specific modes** (like **SOC Reporting**) — precise, named report formats defined in `report_types.json`, for recurring reports where you want the same math applied automatically every time without touching a checklist. A CSV is auto-detected into one of these if its columns match; otherwise it falls back to General.

## Setup

```
pip install -r requirements.txt
```

## Running the app

From the project folder:

```
streamlit run app.py
```

This opens the app in your browser (usually `http://localhost:8501`).

## Using it

1. **Upload one or more CSVs** using the file uploader. Each file gets its own tab.
2. The app picks a starting mode automatically — a file defaults to **General** unless its columns match a specific mode (currently just **SOC Reporting**, which needs `FYLabel`, `Incident Count`, `Turnout Time`, `Travel Time`, `Total Response Time`).
3. Each tab has a **Report mode** dropdown so you can override the guess — switch a file to General, to SOC Reporting, or to any other mode you've added to `report_types.json`, and the table recalculates.
4. Rows in the source file that don't map to a single year (e.g. a pre-existing "FY20-24" combined row) are ignored — the app always recomputes the combined column itself.
5. **Preview the result** as a table in each file's tab.
6. **Download**:
   - **Download as Excel** — that one file's summary as its own `.xlsx`, sheet named after the source file.
   - **Download all as one Excel file** — only shown when you've uploaded more than one CSV; bundles every summary into a single workbook, one sheet per source file, using each file's currently selected mode.

### General mode details

No column names are required. When a file lands in General mode:

1. **FY / Year column** — a dropdown lets you confirm or change which column holds the fiscal year label. The app guesses based on which column has the most parseable year values.
2. **Column checklist** — every other column gets a row with:
   - **Include** — checkbox; uncheck to leave a column out of the summary entirely.
   - **Operation** — `Sum`, `Average`, `Ratio (%)`, or `Skip`. Defaults to `Average` for anything that looks like a time (`1:20`, `0:04:30`) or a percentage, `Sum` for everything else.
   - **Divide by** — only used by `Ratio (%)`: combined value becomes `sum(this column) / sum(that column)`, formatted as a percent — this is how you get an accurate combined percentage instead of a plain average of yearly percentages.
   - **Label** — editable text for the row header in the output table; defaults to the column name.

Edit the checklist and the preview table below it updates immediately. Nothing is written back to the CSV — this only affects what's in the output table/Excel file.

If you find yourself rebuilding the same checklist for the same kind of file over and over, that's a sign it's worth turning into a specific mode in `report_types.json` instead (see below) so it's auto-applied without touching the checklist each time.

## Adding a new specific mode

Specific report modes live in **`report_types.json`**, next to `app.py` — you don't need to touch any Python to add one. It's a JSON array; each entry is one mode:

```json
{
  "name": "SOC Reporting",
  "fy_column": "FYLabel",
  "index_name": "90% Fractile",
  "rows": [
    { "kind": "sum", "source": "Incident Count", "label": "Incidents", "format": "int" },
    { "kind": "weighted_time", "source": "Turnout Time", "weight": "Incident Count", "label": "Turnout Time" }
  ]
}
```

- `name` — shown in the UI as the mode name (dropdown option, and next to the file name, e.g. "Summary — file.csv (SOC Reporting)").
- `fy_column` — the name of the column holding the FY label, or `null` if it's the first (unnamed) column.
- `index_name` — the row-header label shown in the output table.
- `rows` — one entry per output row. `kind` determines which other keys are required:
  - `sum` — needs `source`, `label`, `format` (`"int"`). Combined column = sum across years; per-year = raw value.
  - `weighted_time` — needs `source`, `weight`, `label`. For `H:MM:SS`-style time columns; combined column = average weighted by another column (e.g. Incident Count), not a flat average.
  - `ratio` — needs `numerator`, `denominator`, `label`. Combined column = sum(numerator) / sum(denominator), formatted as a percentage; any `%` column already in the source CSV is ignored and recomputed from the raw numerator/denominator columns instead.

To support a new precise file layout: add a new object to the JSON array with the right column names and row specs, save the file, and reload the app (Streamlit only reads this file at startup) — no code change needed. It becomes a new option in every file's Report mode dropdown, and files whose columns match it will be auto-detected into it going forward. The app validates the file on load; if the JSON is malformed or a row is missing a required key, it shows exactly which entry and key is wrong instead of crashing.

If a file's real shape is one-off or you're still figuring out what its columns should be, you don't need a `report_types.json` entry at all — just leave it on General mode.
