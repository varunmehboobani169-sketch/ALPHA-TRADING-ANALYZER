# FRIDAY Cloud-safe launcher
# Loads the intact FRIDAY source and applies the Streamlit Cloud ZIP fix in memory.
import requests

SOURCE_URL = "https://raw.githubusercontent.com/varunmehboobani169-sketch/ALPHA-TRADING-ANALYZER/361848fd675ecda841c8a6564c9f2caa4d57967c/app.py"

response = requests.get(SOURCE_URL, timeout=30)
response.raise_for_status()
source = response.text

# Ensure zipfile is imported by the loaded source.
if "import zipfile" not in source.splitlines()[:50]:
    source = source.replace("import time\n", "import time\nimport zipfile\n", 1)

# Streamlit Cloud does not guarantee that a generated /mnt/data ZIP path
# exists when download buttons are rendered. Build accumulated ZIPs in memory.
source = source.replace(
'''            buf = Path("/mnt/data") / f"FRIDAY_{ds.replace(' ','_')}_quarters.zip"
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for yr, q, tf, qdf in sorted(items):
                    z.writestr(
                        f"{ds.replace(' ','_')}_{yr}_Q{q}_{tf}_OHLC.csv",
                        qdf.to_csv(index=False).encode("utf-8")
                    )
            st.download_button(
                f"⬇️ Download {ds} accumulated quarters",
                data=buf.read_bytes(),
                file_name=buf.name,
                mime="application/zip",
                key=f"vault_{ds}",
            )
''',
'''            from io import BytesIO
            zip_buffer = BytesIO()
            zip_name = f"FRIDAY_{ds.replace(' ','_')}_quarters.zip"
            with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:
                for yr, q, tf, qdf in sorted(items):
                    z.writestr(
                        f"{ds.replace(' ','_')}_{yr}_Q{q}_{tf}_OHLC.csv",
                        qdf.to_csv(index=False).encode("utf-8")
                    )
            zip_buffer.seek(0)
            st.download_button(
                f"⬇️ Download {ds} accumulated quarters",
                data=zip_buffer.getvalue(),
                file_name=zip_name,
                mime="application/zip",
                key=f"vault_{ds}",
            )
''',
1,
)

# Also make quarterly-report ZIPs memory based.
source = source.replace(
'''    path = Path("/mnt/data/friday_quarterly_reports.zip")

    master_rows = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
''',
'''    from io import BytesIO
    zip_buffer = BytesIO()

    master_rows = []
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:
''',
1,
)
source = source.replace(
'''    return path


def _read_quarterly_zip_files(files):
''',
'''    zip_buffer.seek(0)
    return zip_buffer


def _read_quarterly_zip_files(files):
''',
1,
)
source = source.replace(
'''        selected_zip = _quarterly_report_zip(reports, selected)
        if selected_zip:
            st.download_button(
                "⬇️ Download Selected Quarterly Reports",
                data=selected_zip.read_bytes(),
''',
'''        selected_zip = _quarterly_report_zip(reports, selected)
        if selected_zip:
            st.download_button(
                "⬇️ Download Selected Quarterly Reports",
                data=selected_zip.getvalue(),
''',
1,
)
source = source.replace(
'''        all_zip = _quarterly_report_zip(reports, quarters)
        if all_zip:
            st.download_button(
                "⬇️ Download ALL Quarterly Reports",
                data=all_zip.read_bytes(),
''',
'''        all_zip = _quarterly_report_zip(reports, quarters)
        if all_zip:
            st.download_button(
                "⬇️ Download ALL Quarterly Reports",
                data=all_zip.getvalue(),
''',
1,
)

exec(compile(source, "friday_app.py", "exec"), globals(), globals())
