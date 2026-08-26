import io
import time
import zipfile

import pandas as pd
import streamlit as st

from friday_engine import TARGET_QUARTERS, normalize_options, normalize_spot, normalize_vix, run_quarter, package_results, cross_quarter_validation

st.set_page_config(page_title="FRIDAY — Research Engine", layout="wide")


def read_uploaded(files):
    frames = []
    for f in files or []:
        d = pd.read_csv(f, low_memory=False)
        if not d.empty:
            frames.append(d)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def quarter_summary(results):
    rows = []
    for r in results:
        option_rows = 0
        hit = r.audit.loc[r.audit["check"].eq("Option rows"), "value"]
        if len(hit):
            option_rows = int(str(hit.iloc[0]).replace(",", ""))
        rows.append({
            "quarter": r.quarter,
            "option_rows": option_rows,
            "CE/PE pairs": len(r.pairs),
            "ATM observations": len(r.features),
            "fruitful factors": len(r.fruitfulness),
            "fruitful interactions": len(r.interactions),
            "audit failures": int((r.audit.status == "FAIL").sum()),
        })
    return pd.DataFrame(rows)

st.title("FRIDAY — BEST-OF-BEST MARKET RESEARCH ENGINE")
st.caption("Rebuilt around the actual research clock: 1-minute NIFTY Options + native 15-minute NIFTY Spot + native 15-minute India VIX.")

with st.sidebar:
    st.subheader("FRIDAY RESEARCH SCOPE")
    selected = st.multiselect("Quarter(s)", TARGET_QUARTERS, default=["Q1 2025"], help="Select one, several, or all historical quarters you have uploaded.")
    st.markdown("**Native data clocks**")
    st.write("Options — 1 minute")
    st.write("NIFTY Spot — 15 minutes")
    st.write("India VIX — 15 minutes")
    st.markdown("**Forward outcome horizons**")
    st.write("1 / 3 / 5 / 10 / 15 / 30 / 60 / 120 minutes")

st.subheader("RAW DATA INPUT")
opt_files = st.file_uploader("NIFTY Options CSV(s)", type=["csv"], accept_multiple_files=True, key="friday_options")
spot_files = st.file_uploader("NIFTY Spot CSV(s)", type=["csv"], accept_multiple_files=True, key="friday_spot")
vix_files = st.file_uploader("India VIX CSV(s)", type=["csv"], accept_multiple_files=True, key="friday_vix")

st.info("FRIDAY starts from raw data. It preserves option expiry identity, refuses unsafe cross-expiry pairing, respects the 1m/15m clocks, screens single variables before interactions, and applies robustness checks before a candidate can become research knowledge.")

if st.button("RUN BEST-OF-BEST FRIDAY AUDIT", type="primary", use_container_width=True):
    if not selected:
        st.error("Select at least one quarter.")
    elif not opt_files or not spot_files or not vix_files:
        st.error("Upload Options, NIFTY Spot and India VIX CSVs. All three are required for the full research engine.")
    else:
        started = time.time()
        bar = st.progress(0, text="FRIDAY: 0% — loading raw data")
        status = st.empty()
        try:
            status.info("Normalizing raw inputs...")
            options = normalize_options(read_uploaded(opt_files))
            spot = normalize_spot(read_uploaded(spot_files))
            vix = normalize_vix(read_uploaded(vix_files))
            bar.progress(10, text="FRIDAY: 10% — raw data normalized")

            results = []
            errors = []
            for i, q in enumerate(selected, 1):
                status.info(f"{q}: deep audit + fruitfulness discovery ({i}/{len(selected)})")
                try:
                    results.append(run_quarter(q, options, spot, vix))
                except Exception as exc:
                    errors.append((q, str(exc)))
                bar.progress(10 + int(80 * i / len(selected)), text=f"FRIDAY: {10 + int(80 * i / len(selected))}% — {q}")

            if not results:
                raise RuntimeError("No quarter completed. " + " | ".join(f"{q}: {e}" for q, e in errors))

            bar.progress(90, text="FRIDAY: 90% — cross-quarter validation")
            cross = cross_quarter_validation(results)
            bar.progress(100, text="FRIDAY: 100% ✅")
            status.success(f"FRIDAY completed {len(results)} quarter(s) in {time.time() - started:.1f}s")

            st.subheader("Quarter Summary")
            st.dataframe(quarter_summary(results), use_container_width=True, hide_index=True)

            if not cross.empty:
                st.subheader("Cross-Quarter Validation")
                st.dataframe(cross.head(40), use_container_width=True, hide_index=True)
                st.download_button("DOWNLOAD CROSS-QUARTER VALIDATION (.CSV)", cross.to_csv(index=False).encode(), "FRIDAY_CROSS_QUARTER_VALIDATION.csv", "text/csv", use_container_width=True)

            for r in results:
                with st.expander(f"{r.quarter} — DEEP AUDIT", expanded=(len(results) == 1)):
                    st.markdown(r.report)
                    st.subheader("Fruitfulness — Top Single Factors")
                    st.dataframe(r.fruitfulness.head(25), use_container_width=True, hide_index=True)
                    st.subheader("Fruitfulness — Top Interactions")
                    st.dataframe(r.interactions.head(25), use_container_width=True, hide_index=True)
                    c1, c2 = st.columns(2)
                    safe = r.quarter.replace(" ", "_")
                    with c1:
                        st.download_button("DOWNLOAD AUDIT (.MD)", r.report.encode(), f"FRIDAY_{safe}_DEEP_AUDIT.md", "text/markdown", key=f"md_{safe}", use_container_width=True)
                    with c2:
                        payload = io.BytesIO()
                        with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as z:
                            z.writestr(f"{safe}_DEEP_AUDIT.md", r.report)
                            z.writestr(f"{safe}_audit.csv", r.audit.to_csv(index=False))
                            z.writestr(f"{safe}_features.csv", r.features.to_csv(index=False))
                            z.writestr(f"{safe}_fruitfulness.csv", r.fruitfulness.to_csv(index=False))
                            z.writestr(f"{safe}_interactions.csv", r.interactions.to_csv(index=False))
                            z.writestr(f"{safe}_robustness.csv", r.validation.to_csv(index=False))
                            z.writestr(f"{safe}_pairs.csv", r.pairs.to_csv(index=False))
                        st.download_button("DOWNLOAD FULL QUARTER (.ZIP)", payload.getvalue(), f"FRIDAY_{safe}_DEEP_RESEARCH.zip", "application/zip", key=f"zip_{safe}", use_container_width=True)

            st.download_button("DOWNLOAD ALL SELECTED QUARTERS (.ZIP)", package_results(results), "FRIDAY_SELECTED_QUARTERS_DEEP_RESEARCH.zip", "application/zip", use_container_width=True)
            if errors:
                st.warning("These quarters were blocked by the strict audit: " + " | ".join(f"{q}: {e}" for q, e in errors))
        except Exception as exc:
            status.error(f"FRIDAY stopped after {time.time() - started:.1f}s: {exc}")
            st.exception(exc)

st.divider()
st.subheader("FRIDAY research rules")
st.markdown("""
**Raw-first:** no fixed ten-pattern list is assumed. Features are derived from the uploaded raw dataset and screened for information content.

**Native clocks:** options remain 1-minute; Spot and VIX remain 15-minute. They are aligned backward only.

**No leakage:** forward targets are strictly later observations.

**Expiry identity:** CE/PE pairing requires timestamp + expiry + strike. Missing expiry is a hard stop.

**Fruitfulness first:** single factors are screened before pair interactions. Outlier-sensitive results are flagged and temporal stability is checked.

**Cross-quarter validation:** a feature is more credible when its effect survives across independent quarters.

**No premature AI:** the numerical research engine produces evidence; the later AI layer will interpret only validated research memory.
""")
