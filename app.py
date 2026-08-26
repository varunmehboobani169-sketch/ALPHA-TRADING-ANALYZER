# FRIDAY launcher
# Current design: Options + NIFTY Spot + India VIX only.
# Futures are intentionally removed from the application design for now.
import re
import requests
import streamlit as st

SOURCE_URL = "https://raw.githubusercontent.com/varunmehboobani169-sketch/ALPHA-TRADING-ANALYZER/main/friday_app_v2.py"
source = requests.get(SOURCE_URL, timeout=30)
source.raise_for_status()

# Load FRIDAY's functions without executing its final UI block.
marker = "init_state(); inject_css()"
if marker in source.text:
    source_text = source.text.split(marker, 1)[0]
else:
    source_text = source.text

ns = {"__name__": "__main__"}
exec(compile(source_text, "friday_app_v2.py", "exec"), ns, ns)

# Pull the functions/constants created by FRIDAY v2 into the launcher scope.
globals().update(ns)

init_state()
inject_css()


def render_progress_ai():
    st.markdown("## FRIDAY — OPTION PATTERN RESEARCH")
    st.write(
        "FRIDAY uses Options + NIFTY Spot + India VIX to discover repeatable relationships. "
        "Futures are not part of the design."
    )

    opt = st.file_uploader("Option Data (CSV)", type=["csv"], accept_multiple_files=True, key="progress_opt")
    spot = st.file_uploader("NIFTY Spot Data (CSV)", type=["csv"], accept_multiple_files=True, key="progress_spot")
    vix = st.file_uploader("India VIX Data (CSV)", type=["csv"], accept_multiple_files=True, key="progress_vix")

    if not (opt and spot):
        st.info("Upload the Q1 Option + Spot data. Upload Q1 India VIX as well for the full context analysis.")
        return

    if st.button("ANALYZE PATTERNS", use_container_width=True, key="progress_analyze"):
        progress = st.progress(0, text="FRIDAY processing: 0%")
        status = st.empty()
        try:
            status.info("🔄 10% — Reading Option, Spot and VIX files…")
            progress.progress(10, text="FRIDAY processing: 10%")
            options_raw = read_csvs(opt)

            status.info("🔄 25% — Normalizing Options data…")
            progress.progress(25, text="FRIDAY processing: 25%")
            options = normalize_options(options_raw)

            status.info("🔄 40% — Normalizing NIFTY Spot data…")
            progress.progress(40, text="FRIDAY processing: 40%")
            spot_df = normalize_spot(read_csvs(spot))

            status.info("🔄 50% — Normalizing India VIX data…")
            progress.progress(50, text="FRIDAY processing: 50%")
            vix_df = normalize_vix(read_csvs(vix)) if vix else pd.DataFrame()

            status.info("🔄 65% — Synchronizing Options + Spot + VIX timestamps…")
            progress.progress(65, text="FRIDAY processing: 65%")
            features = build_features(options, spot_df, vix_df)

            if features.empty:
                progress.progress(65, text="FRIDAY processing: 65%")
                raise ValueError(
                    "No synchronized ATM option/spot observations were created. "
                    "Check that the option and spot timestamps overlap and that the option file contains both CE and PE rows."
                )

            status.info("🔄 80% — Building market features and forward outcomes…")
            progress.progress(80, text="FRIDAY processing: 80%")
            patterns = discover_patterns(features)

            status.info("🔄 92% — Ranking observed patterns…")
            progress.progress(92, text="FRIDAY processing: 92%")
            st.session_state.analysis = features
            st.session_state.analysis_summary = {
                "rows": len(features),
                "start": str(features.timestamp.min()),
                "end": str(features.timestamp.max()),
            }

            progress.progress(100, text="FRIDAY processing: 100% ✅")
            status.success(
                f"✅ Analysis complete — {len(features):,} synchronized timestamps "
                f"({features.timestamp.min()} → {features.timestamp.max()})."
            )

            st.metric("PROCESSING COMPLETE", "100%")

            if not patterns.empty:
                st.subheader("Pattern Summary")
                st.dataframe(patterns, use_container_width=True, hide_index=True)
            else:
                st.warning("No pattern had at least 10 usable observations in this dataset.")

            st.subheader("Feature Data")
            st.dataframe(features.tail(500), use_container_width=True, hide_index=True)

            b = io.BytesIO()
            with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("FRIDAY_features.csv", features.to_csv(index=False))
                z.writestr("FRIDAY_pattern_summary.csv", patterns.to_csv(index=False))
            st.download_button(
                "DOWNLOAD ANALYSIS ZIP",
                b.getvalue(),
                "FRIDAY_Q1_PATTERN_ANALYSIS.zip",
                "application/zip",
                use_container_width=True,
            )
        except Exception as e:
            progress.progress(min(99, int(progress._repr_svg_() is not None) * 65) if False else 65, text="FRIDAY processing: halted")
            status.error(f"❌ Processing stopped: {e}")

with st.sidebar:
    st.title("FRIDAY")
    st.caption("Option Pattern Research Engine")
    st.session_state.client_id = st.text_input(
        "Dhan Client ID",
        value=st.session_state.client_id or DEFAULT_CLIENT_ID,
    ).strip()
    st.session_state.access_token = st.text_input(
        "Dhan Access Token",
        value=st.session_state.access_token,
        type="password",
    ).strip()
    st.caption(f"Client ID: {st.session_state.client_id or DEFAULT_CLIENT_ID}")

view = st.radio("MODULE", ["AI Strategist", "Data Vault"], horizontal=True)
if view == "Data Vault":
    render_vault()
else:
    render_progress_ai()
