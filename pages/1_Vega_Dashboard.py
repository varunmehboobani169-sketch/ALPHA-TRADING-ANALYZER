# Streamlit multipage entrypoint for the Vega dashboard.
# Keeping the implementation in the root file avoids duplicating strategy logic.
exec(open("vega_dashboard.py", encoding="utf-8").read(), globals())
