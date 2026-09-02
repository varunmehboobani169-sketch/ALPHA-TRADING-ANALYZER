from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
import pandas as pd
import requests

API_BASE = "https://api.dhan.co/v2"
NIFTY_SECURITY_ID = 13
NIFTY_SEGMENT = "IDX_I"
STRIKE_STEP = 50
WINDOW = 20

def _headers(client_id: str, access_token: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "access-token": access_token.strip(), "client-id": client_id.strip()}

def dhan_post(path: str, client_id: str, access_token: str, payload: dict[str, Any]) -> dict[str, Any]:
    r = requests.post(f"{API_BASE}{path}", headers=_headers(client_id, access_token), json=payload, timeout=15)
    r.raise_for_status()
    body = r.json()
    if body.get("status") == "failure":
        raise RuntimeError(str(body))
    return body

def expiries(client_id: str, access_token: str) -> list[str]:
    body = dhan_post("/optionchain/expirylist", client_id, access_token, {"UnderlyingScrip": NIFTY_SECURITY_ID, "UnderlyingSeg": NIFTY_SEGMENT})
    return [str(x) for x in body.get("data", [])]

def option_chain(client_id: str, access_token: str, expiry: str) -> dict[str, Any]:
    return dhan_post("/optionchain", client_id, access_token, {"UnderlyingScrip": NIFTY_SECURITY_ID, "UnderlyingSeg": NIFTY_SEGMENT, "Expiry": expiry})

def atm_from_spot(spot: float) -> int:
    return int(round(float(spot) / STRIKE_STEP) * STRIKE_STEP)

def capture_snapshot(client_id: str, access_token: str, expiry: str, captured_at: datetime | None = None) -> pd.DataFrame:
    captured_at = captured_at or datetime.now(timezone.utc).astimezone()
    data = option_chain(client_id, access_token, expiry).get("data", {})
    spot = data.get("last_price")
    if spot is None:
        raise RuntimeError("Dhan returned no NIFTY spot price.")
    atm = atm_from_spot(float(spot))
    lo, hi = atm - WINDOW * STRIKE_STEP, atm + WINDOW * STRIKE_STEP
    rows: list[dict[str, Any]] = []
    for strike_key, strike_data in (data.get("oc", {}) or {}).items():
        strike = int(round(float(strike_key)))
        if not lo <= strike <= hi:
            continue
        offset = int((strike - atm) / STRIKE_STEP)
        for side_key, side in (("ce", "CE"), ("pe", "PE")):
            leg = strike_data.get(side_key) or {}
            g = leg.get("greeks") or {}
            ltp, prev = leg.get("last_price"), leg.get("previous_close_price")
            oi, prev_oi = leg.get("oi"), leg.get("previous_oi")
            rows.append({
                "captured_at": captured_at.isoformat(), "date": captured_at.date().isoformat(), "time": captured_at.strftime("%H:%M:%S"),
                "expiry": expiry, "spot": spot, "atm": atm, "strike_offset": offset,
                "moneyness": "ATM" if offset == 0 else f"ATM{offset:+d}", "strike": strike, "option_type": side,
                "security_id": leg.get("security_id"), "last_price": ltp, "previous_close": prev,
                "change": float(ltp) - float(prev) if ltp is not None and prev is not None else None,
                "change_pct": (float(ltp) / float(prev) - 1) * 100 if ltp is not None and prev not in (None, 0) else None,
                "oi": oi, "previous_oi": prev_oi,
                "oi_change": int(oi) - int(prev_oi) if oi is not None and prev_oi is not None else None,
                "volume": leg.get("volume"), "previous_volume": leg.get("previous_volume"),
                "iv": leg.get("implied_volatility"), "delta": g.get("delta"), "theta": g.get("theta"), "gamma": g.get("gamma"), "vega": g.get("vega"),
                "bid": leg.get("top_bid_price"), "bid_qty": leg.get("top_bid_quantity"), "ask": leg.get("top_ask_price"), "ask_qty": leg.get("top_ask_quantity"),
            })
    out = pd.DataFrame(rows)
    if out.empty:
        raise RuntimeError("No option contracts were returned inside ATM ±20.")
    return out.sort_values(["strike", "option_type"]).reset_index(drop=True)
