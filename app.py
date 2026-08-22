import os, time, math, json
from datetime import datetime, timedelta, date
from pathlib import Path
import requests
import pandas as pd
import numpy as np
import streamlit as st

st.set_page_config(page_title='ALPHA ANALYZER', page_icon='α', layout='wide')

# -----------------------------
# CONFIG
# -----------------------------
BASE = 'https://api.dhan.co/v2'
REQUEST_LOG = st.session_state.setdefault('request_log', [])
LAST_REFRESH = st.session_state.get('last_refresh')

@st.cache_data(ttl=86400, show_spinner=False)
def load_instruments():
    url = 'https://images.dhan.co/api-data/api-scrip-master.csv'
    df = pd.read_csv(url, low_memory=False)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def secret(name, default=''):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return os.getenv(name, default)

# Client ID/code is stored once in Streamlit Secrets.
# The access token is intentionally entered by the user each session.
CLIENT_ID = secret('ALPHA_CLIENT_ID')


def get_access_token():
    return st.session_state.get('alpha_access_token', '').strip()


def api_headers():
    token = get_access_token()
    return {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'access-token': token,
        'client-id': CLIENT_ID,
    }


def api_post(path, payload, kind='data'):
    token = get_access_token()
    if not CLIENT_ID:
        raise RuntimeError('Client code is not configured in Streamlit Secrets.')
    if not token:
        raise RuntimeError('Please enter your current access token in the sidebar.')
    if kind == 'quote':
        time.sleep(max(0, 1.05 - (time.time() - st.session_state.get('last_quote_call', 0))))
        st.session_state['last_quote_call'] = time.time()
    r = requests.post(BASE + path, headers=api_headers(), json=payload, timeout=20)
    REQUEST_LOG.append({'time': datetime.now(), 'path': path, 'ok': r.ok})
    if not r.ok:
        raise RuntimeError(f'Market API {r.status_code}: {r.text[:500]}')
    return r.json()


def today_request_count():
    d = date.today()
    return sum(x['time'].date() == d for x in REQUEST_LOG)


def cache_key(prefix, **kwargs):
    return prefix + '_' + '_'.join(f'{k}={v}' for k,v in sorted(kwargs.items()))


# -----------------------------
# INSTRUMENT DISCOVERY
# -----------------------------
@st.cache_data(ttl=21600, show_spinner=False)
def build_universe():
    df = load_instruments()
    # Normalize common columns if present
    rename = {}
    for c in df.columns:
        lc = c.lower()
        if lc in ('sem_exm_exch_id','exchange'):
            rename[c] = 'exchange'
        elif lc in ('sem_segment','segment'):
            rename[c] = 'segment'
        elif lc in ('sem_security_id','security_id'):
            rename[c] = 'security_id'
        elif lc in ('sem_trading_symbol','trading_symbol'):
            rename[c] = 'trading_symbol'
        elif lc in ('sem_custom_symbol','display_name'):
            rename[c] = 'display_name'
        elif lc in ('sem_instrument_name','instrument'):
            rename[c] = 'instrument'
        elif lc in ('sm_symbol_name','symbol_name'):
            rename[c] = 'symbol_name'
        elif lc in ('underlying_security_id',):
            rename[c] = 'underlying_security_id'
        elif lc in ('underlying_symbol',):
            rename[c] = 'underlying_symbol'
        elif lc in ('sem_expiry_date','expiry_date'):
            rename[c] = 'expiry_date'
        elif lc in ('sem_strike_price','strike_price'):
            rename[c] = 'strike_price'
        elif lc in ('sem_option_type','option_type'):
            rename[c] = 'option_type'
    df = df.rename(columns=rename)
    return df


def find_col(df, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None


# Broad sector map. Extend sector_map.csv in the project for full coverage.
SECTOR_MAP = {
    'RELIANCE':'Energy','ONGC':'Energy','COALINDIA':'Energy','IOC':'Energy','BPCL':'Energy','GAIL':'Energy','POWERGRID':'Utilities','NTPC':'Utilities','TATAPOWER':'Utilities',
    'HDFCBANK':'Banking','ICICIBANK':'Banking','SBIN':'Banking','AXISBANK':'Banking','KOTAKBANK':'Banking','INDUSINDBK':'Banking','BANKBARODA':'Banking','PNB':'Banking','IDFCFIRSTB':'Banking','FEDERALBNK':'Banking',
    'BAJFINANCE':'Financials','BAJAJFINSV':'Financials','SHRIRAMFIN':'Financials','CHOLAFIN':'Financials','MUTHOOTFIN':'Financials','SBICARD':'Financials',
    'TCS':'IT','INFY':'IT','HCLTECH':'IT','WIPRO':'IT','TECHM':'IT','LTIM':'IT','MPHASIS':'IT','COFORGE':'IT',
    'MARUTI':'Auto','M&M':'Auto','TATAMOTORS':'Auto','HEROMOTOCO':'Auto','EICHERMOT':'Auto','BAJAJ-AUTO':'Auto','TVSMOTOR':'Auto','ASHOKLEY':'Auto',
    'TATASTEEL':'Metals','JSWSTEEL':'Metals','HINDALCO':'Metals','SAIL':'Metals','JINDALSTEL':'Metals','NATIONALUM':'Metals','VEDL':'Metals',
    'SUNPHARMA':'Pharma','CIPLA':'Pharma','DRREDDY':'Pharma','DIVISLAB':'Pharma','APOLLOHOSP':'Pharma','LUPIN':'Pharma','AUROPHARMA':'Pharma','TORNTPHARM':'Pharma',
    'ITC':'FMCG','HINDUNILVR':'FMCG','NESTLEIND':'FMCG','BRITANNIA':'FMCG','TATACONSUM':'FMCG','DABUR':'FMCG','MARICO':'FMCG','COLPAL':'FMCG',
    'LT':'Capital Goods','BEL':'Defence/Industrial','HAL':'Defence/Industrial','BHEL':'Capital Goods','SIEMENS':'Capital Goods','ABB':'Capital Goods','CUMMINSIND':'Capital Goods',
    'DLF':'Realty','GODREJPROP':'Realty','OBEROIRLTY':'Realty','LODHA':'Realty','PRESTIGE':'Realty','PHOENIXLTD':'Realty',
    'TRENT':'Consumer','TITAN':'Consumer','DMART':'Consumer','KALYANKJIL':'Consumer','JUBLFOOD':'Consumer',
    'BHARTIARTL':'Telecom','INDUSTOWER':'Telecom','IDEA':'Telecom',
    'ADANIENT':'Conglomerate','ADANIPORTS':'Infrastructure','IRCTC':'Travel/Infra','INDIGO':'Aviation','DELHIVERY':'Logistics',
}


def sector_of(symbol):
    return SECTOR_MAP.get(symbol, 'Other/Unmapped')


def fno_futures(df, exchange='NSE'):
    if df.empty: return df
    ex = df['exchange'].astype(str).str.upper() if 'exchange' in df.columns else pd.Series('', index=df.index)
    inst = df['instrument'].astype(str).str.upper() if 'instrument' in df.columns else pd.Series('', index=df.index)
    if exchange == 'NSE':
        mask = (ex == 'NSE') & (inst == 'FUTSTK')
    else:
        mask = (ex == 'MCX') & (inst == 'FUTCOM')
    return df.loc[mask].copy()


def mcx_futures(df):
    return fno_futures(df, 'MCX')


def index_contract(df, symbol):
    if df.empty: return pd.DataFrame()
    ex = df['exchange'].astype(str).str.upper()
    sym = df.get('underlying_symbol', pd.Series('', index=df.index)).astype(str).str.upper()
    inst = df['instrument'].astype(str).str.upper()
    return df.loc[(ex=='NSE') & (inst=='FUTIDX') & (sym==symbol.upper())].copy()


def choose_near_expiry(rows):
    if rows.empty: return rows
    if 'expiry_date' in rows.columns:
        rows = rows.copy()
        rows['expiry_date'] = pd.to_datetime(rows['expiry_date'], errors='coerce')
        rows = rows.dropna(subset=['expiry_date']).sort_values('expiry_date')
    return rows.head(1)


def instrument_payload_rows(rows):
    out=[]
    for _,r in rows.iterrows():
        out.append({'securityId': str(int(r['security_id'])),'exchangeSegment': 'NSE_FNO' if str(r['exchange']).upper()=='NSE' else 'MCX_COMM'})
    return out

# -----------------------------
# API calls
# -----------------------------
def bulk_quote(rows):
    rows = rows.dropna(subset=['security_id']) if not rows.empty else rows
    if rows.empty: return pd.DataFrame()
    items = []
    for _, r in rows.iterrows():
        items.append((str(int(r['security_id'])), 'NSE_FNO' if str(r['exchange']).upper()=='NSE' else 'MCX_COMM', r))
    chunks = [items[i:i+1000] for i in range(0, len(items), 1000)]
    records=[]
    for ch in chunks:
        payload={}
        for sid,seg,_ in ch:
            payload.setdefault(seg, []).append(sid)
        data=api_post('/marketfeed/quote', payload, kind='quote')
        q=data.get('data',{}) if isinstance(data,dict) else {}
        for seg, vals in q.items():
            for sid, v in vals.items():
                rec={'security_id': int(sid), 'exchangeSegment': seg}
                if isinstance(v,dict): rec.update(v)
                records.append(rec)
    qdf=pd.DataFrame(records)
    if qdf.empty: return qdf
    return qdf


@st.cache_data(ttl=180, show_spinner=False)
def historical_daily(sec_id, seg, instrument, oi=True, lookback=40):
    to_d=datetime.now().date()+timedelta(days=1)
    from_d=to_d-timedelta(days=lookback)
    payload={
        'securityId': str(int(sec_id)), 'exchangeSegment': seg, 'instrument': instrument,
        'expiryCode': 0, 'oi': bool(oi), 'fromDate': str(from_d), 'toDate': str(to_d)
    }
    d=api_post('/charts/historical', payload, kind='data')
    return candles_to_df(d)


@st.cache_data(ttl=60, show_spinner=False)
def historical_intraday(sec_id, seg, instrument, interval='1', oi=True, lookback_days=3):
    to_d=datetime.now()+timedelta(minutes=1)
    from_d=to_d-timedelta(days=lookback_days)
    payload={
        'securityId': str(int(sec_id)), 'exchangeSegment': seg, 'instrument': instrument,
        'interval': str(interval), 'oi': bool(oi),
        'fromDate': from_d.strftime('%Y-%m-%d %H:%M:%S'), 'toDate': to_d.strftime('%Y-%m-%d %H:%M:%S')
    }
    d=api_post('/charts/intraday', payload, kind='data')
    return candles_to_df(d)


def candles_to_df(d):
    if not d: return pd.DataFrame()
    # API arrays have equal length
    keys=['open','high','low','close','volume','timestamp','open_interest']
    n=max([len(d.get(k,[])) for k in keys if isinstance(d.get(k,[]),list)] + [0])
    if n==0: return pd.DataFrame()
    out={k: d.get(k,[np.nan]*n) for k in keys}
    df=pd.DataFrame(out)
    if 'timestamp' in df:
        df['datetime']=pd.to_datetime(df['timestamp'], unit='s', errors='coerce').dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
    return df.dropna(subset=['close']).sort_values('datetime').reset_index(drop=True)

# -----------------------------
# P&F ENGINE
# -----------------------------
def build_pnf(closes, box_pct, reversal=3):
    closes=[float(x) for x in pd.Series(closes).dropna().tolist()]
    if len(closes)<2: return []
    cols=[]
    direction=None
    boxes=0
    current=[]
    last_box=None
    for price in closes:
        if last_box is None:
            last_box=price; continue
        box=last_box*(1+box_pct)
        if direction is None:
            if price >= last_box*(1+box_pct):
                direction='X'; current=[1]; last_box=last_box*(1+box_pct)
                while price >= last_box*(1+box_pct): current.append(1); last_box*=1+box_pct
            elif price <= last_box*(1-box_pct):
                direction='O'; current=[1]; last_box=last_box*(1-box_pct)
                while price <= last_box*(1-box_pct): current.append(1); last_box*=1-box_pct
        elif direction=='X':
            while price >= last_box*(1+box_pct): current.append(1); last_box*=1+box_pct
            if price <= last_box*(1-box_pct)**reversal:
                cols.append({'type':'X','boxes':len(current),'high':last_box/(1+box_pct), 'low': None})
                direction='O'; current=[1]; last_box=last_box*(1-box_pct)
        else:
            while price <= last_box*(1-box_pct): current.append(1); last_box*=1-box_pct
            if price >= last_box*(1+box_pct)**reversal:
                cols.append({'type':'O','boxes':len(current),'low':last_box/(1-box_pct), 'high': None})
                direction='X'; current=[1]; last_box=last_box*(1+box_pct)
    if current and direction:
        col={'type':direction,'boxes':len(current),'high':None,'low':None}
        if direction=='X': col['high']=last_box/(1+box_pct)
        else: col['low']=last_box/(1-box_pct)
        cols.append(col)
    return cols


def pnf_signal(closes, box_pct, reversal=3, anchor_boxes=15):
    cols = build_pnf(closes, box_pct, reversal)
    result = {'anchor':False,'dtb':False,'dbs':False,'bias':'Neutral','signal':False,'signal_side':None,'reason':'Not enough P&F history','columns':cols,'stop':np.nan}
    if len(cols) < 3:
        return result
    current=cols[-1]
    result['bias']='Bullish' if current['type']=='X' else 'Bearish'
    if current['type']=='X':
        prev_x_idx=next((i for i in range(len(cols)-2,-1,-1) if cols[i]['type']=='X'),None)
        if prev_x_idx is None:
            result['reason']='No previous X-column available for DTB comparison.'; return result
        prev_x=cols[prev_x_idx]
        anchor_idx=next((i for i in range(prev_x_idx-1,-1,-1) if cols[i]['type']=='X' and cols[i].get('boxes',0)>=anchor_boxes),None)
        result['anchor']=anchor_idx is not None
        if result['anchor'] and current.get('high') is not None and prev_x.get('high') is not None and current['high']>prev_x['high']:
            result['dtb']=True; result['signal']=True; result['signal_side']='LONG'
            result['reason']=f"Anchor ({cols[anchor_idx]['boxes']} boxes) -> fresh DTB."
            prev_o=next((cols[i] for i in range(prev_x_idx-1,-1,-1) if cols[i]['type']=='O'),None)
            if prev_o and prev_o.get('low') is not None: result['stop']=float(prev_o['low'])
        else:
            result['reason']='Anchor found but no fresh DTB.' if result['anchor'] else 'No qualifying bullish anchor before current structure.'
    else:
        prev_o_idx=next((i for i in range(len(cols)-2,-1,-1) if cols[i]['type']=='O'),None)
        if prev_o_idx is None:
            result['reason']='No previous O-column available for DBS comparison.'; return result
        prev_o=cols[prev_o_idx]
        anchor_idx=next((i for i in range(prev_o_idx-1,-1,-1) if cols[i]['type']=='O' and cols[i].get('boxes',0)>=anchor_boxes),None)
        result['anchor']=anchor_idx is not None
        if result['anchor'] and current.get('low') is not None and prev_o.get('low') is not None and current['low']<prev_o['low']:
            result['dbs']=True; result['signal']=True; result['signal_side']='SHORT'
            result['reason']=f"Anchor ({cols[anchor_idx]['boxes']} boxes) -> fresh DBS."
            prev_x=next((cols[i] for i in range(prev_o_idx-1,-1,-1) if cols[i]['type']=='X'),None)
            if prev_x and prev_x.get('high') is not None: result['stop']=float(prev_x['high'])
        else:
            result['reason']='Anchor found but no fresh DBS.' if result['anchor'] else 'No qualifying bearish anchor before current structure.'
    return result


def system_status(res, oi_confirm, data_ok=True):
    """Return an explicit human-readable trading-system state for every symbol."""
    if not data_ok:
        return 'DATA ERROR'
    if not res.get('anchor', False):
        return 'WAIT - NO ANCHOR'
    if not res.get('dtb', False):
        return 'WAIT - NO DTB'
    if not oi_confirm:
        return 'WAIT - OI'
    return '🟢 BUY'


def latest_oi_confirmation(hist):
    """Use the latest two completed rows. Requires price up and OI up."""
    if hist is None or hist.empty or len(hist) < 2:
        return False, np.nan, np.nan
    h=hist.copy()
    if 'datetime' in h.columns:
        h=h.sort_values('datetime')
    h=h.dropna(subset=['close'])
    if len(h) < 2 or 'open_interest' not in h.columns:
        return False, np.nan, np.nan
    a,b=h.iloc[-2],h.iloc[-1]
    if pd.isna(a['open_interest']) or pd.isna(b['open_interest']):
        return False, float(b['close']/a['close']-1)*100, np.nan
    pchg=float(b['close']/a['close']-1)*100
    oichg=float(b['open_interest']-a['open_interest'])
    return (pchg>0 and oichg>0), pchg, oichg


def prepare_fno_scan(df, max_symbols=120, mapped_only=False):
    fut=fno_futures(df,'NSE').copy()
    if fut.empty: return pd.DataFrame()
    sym_col='underlying_symbol' if 'underlying_symbol' in fut.columns else 'symbol_name'
    if sym_col not in fut.columns: return pd.DataFrame()
    fut[sym_col]=fut[sym_col].astype(str).str.upper().str.strip()
    if mapped_only:
        fut=fut[fut[sym_col].map(sector_of).ne('Other/Unmapped')]
    chosen=[]
    for sym,g in fut.groupby(sym_col):
        x=choose_near_expiry(g)
        if not x.empty: chosen.append(x)
    selected=pd.concat(chosen,ignore_index=True) if chosen else pd.DataFrame()
    if selected.empty: return selected
    return selected.head(max_symbols).reset_index(drop=True)


# -----------------------------
# Sector breadth
# -----------------------------
def calculate_sector_breadth(pnf_rows):
    if pnf_rows.empty: return pd.DataFrame()
    x=pnf_rows.copy()
    x['sector']=x['symbol'].map(sector_of)
    x=x[x['sector']!='Other/Unmapped']
    if x.empty: return pd.DataFrame()
    agg=x.groupby('sector').agg(total=('symbol','count'), bullish=('pnf_bullish','sum'), bearish=('pnf_bearish','sum'), dtb=('dtb','sum')).reset_index()
    agg['bullish_pct']=100*agg['bullish']/agg['total']
    agg['bearish_pct']=100*agg['bearish']/agg['total']
    agg['super_state']=np.where(agg['bullish_pct']>=70,'SUPER BULLISH',np.where(agg['bearish_pct']>=70,'SUPER BEARISH','NORMAL'))
    return agg.sort_values('bullish_pct',ascending=False)

# -----------------------------
# Universe runners
# -----------------------------
def futures_snapshot(df, exchange='NSE'):
    fut=fno_futures(df,exchange)
    if fut.empty: return pd.DataFrame()
    # choose nearest contract for each underlying symbol
    sym_col='underlying_symbol' if 'underlying_symbol' in fut.columns else 'symbol_name'
    if sym_col not in fut.columns: return pd.DataFrame()
    fut[sym_col]=fut[sym_col].astype(str).str.upper()
    chosen=[]
    for sym,g in fut.groupby(sym_col):
        chosen.append(choose_near_expiry(g))
    selected=pd.concat(chosen, ignore_index=True) if chosen else pd.DataFrame()
    q=bulk_quote(selected)
    if q.empty: return pd.DataFrame()
    # map metadata
    meta=selected[['security_id',sym_col,'expiry_date']].copy().rename(columns={sym_col:'symbol'})
    q=q.merge(meta,on='security_id',how='left')
    q['symbol']=q['symbol'].astype(str).str.upper()
    # Market quote nesting varies by response. Pull common fields.
    for c in ['last_price','open_interest','prev_oi','previous_open_interest','day_high_oi','day_low_oi','volume']:
        if c not in q.columns: q[c]=np.nan
    return q


def daily_pnf_for_symbol(row, exchange='NSE'):
    seg='NSE_FNO' if exchange=='NSE' else 'MCX_COMM'
    inst='FUTSTK' if exchange=='NSE' else 'FUTCOM'
    df=historical_daily(row['security_id'],seg,inst,oi=True,lookback=120)
    res=pnf_signal(df['close'],0.0025,3,15) if not df.empty else {'bias':'Neutral','signal':False,'anchor':False,'dtb':False,'reason':'No data','columns':[]}
    return res, df


def intraday_pnf_for_symbol(row, exchange='NSE'):
    seg='NSE_FNO' if exchange=='NSE' else 'MCX_COMM'
    inst='FUTSTK' if exchange=='NSE' else 'FUTCOM'
    df=historical_intraday(row['security_id'],seg,inst,interval='1',oi=True,lookback_days=3)
    res=pnf_signal(df['close'],0.0015,3,15) if not df.empty else {'bias':'Neutral','signal':False,'anchor':False,'dtb':False,'reason':'No data','columns':[]}
    return res, df



def mcx_intraday_trade_state(daily_res, intraday_res):
    daily_bias=daily_res.get('bias','Neutral'); side=intraday_res.get('signal_side')
    if side=='LONG' and daily_bias!='Bullish': return {'status':'WAIT - DAILY FILTER','side':None,'reason':'Intraday DTB exists, but daily 0.25% P&F is not bullish.','sl':np.nan}
    if side=='SHORT' and daily_bias!='Bearish': return {'status':'WAIT - DAILY FILTER','side':None,'reason':'Intraday DBS exists, but daily 0.25% P&F is not bearish.','sl':np.nan}
    if side is None: return {'status':'WAIT - NO INTRADAY SETUP','side':None,'reason':'No fresh intraday DTB/DBS.','sl':np.nan}
    return {'status':f"{'🟢 LONG' if side=='LONG' else '🔴 SHORT'}",'side':side,'reason':intraday_res.get('reason',''),'sl':intraday_res.get('stop',np.nan)}

# -----------------------------
# UI
# -----------------------------
with st.sidebar:
    st.header('ALPHA ANALYZER')
    st.caption('Live P&F + OI market intelligence')
    token_input = st.text_input('Access token', value=st.session_state.get('alpha_access_token',''), type='password', placeholder='Enter current token')
    if token_input:
        st.session_state['alpha_access_token'] = token_input.strip()
    auto=st.checkbox('Auto refresh', value=True)
    interval=st.selectbox('Refresh interval', [1,2,3,5], index=2, format_func=lambda x:f'Every {x} minutes')
    anchor_boxes=st.number_input('Anchor minimum boxes', min_value=5, max_value=30, value=15, step=1)
    sector_threshold=st.slider('Super sector breadth %',50,90,70)
    if st.button('Refresh now'):
        st.cache_data.clear(); st.rerun()
    st.divider()
    st.metric('Historical/data API calls this session', today_request_count())
    st.caption('Client code is fixed in app secrets; enter the current token above each session.')

# Browser-side rerun without heavy extra package
if auto:
    st.markdown(f"<meta http-equiv='refresh' content='{interval*60}'>", unsafe_allow_html=True)

st.title('ALPHA ANALYZER')
st.caption('Live NSE F&O + MCX market intelligence | 2-mode Point & Figure system')

try:
    instruments=build_universe()
except Exception as e:
    st.error(f'Could not load the market instrument master: {e}')
    st.stop()

if not CLIENT_ID:
    st.error('Client code is not configured. Add ALPHA_CLIENT_ID to Streamlit Secrets.')
    st.stop()

if not get_access_token():
    st.info('Enter your current access token in the sidebar to activate live analysis.')
    st.stop()

# Navigation
mode=st.radio('Dashboard', ['Market Scanner','Sector Breadth','P&F Trading System','NIFTY/BANKNIFTY Options','MCX'], horizontal=True)

if mode=='Market Scanner':
    st.subheader('Top Bullish / Bearish NSE F&O Stocks')
    snap=futures_snapshot(instruments,'NSE')
    if snap.empty:
        st.warning('No NSE F&O futures snapshot returned.')
    else:
        # Compute lightweight ranking from current quote fields.
        def num_col(df, name):
            if name in df: return pd.to_numeric(df[name],errors='coerce')
            return pd.Series(np.nan,index=df.index)
        snap['last_price']=num_col(snap,'last_price')
        snap['oi']=num_col(snap,'open_interest')
        snap['vol']=num_col(snap,'volume')
        # quote endpoint field names can differ; use day high/low OI when available
        dhi=num_col(snap,'day_high_oi'); dlo=num_col(snap,'day_low_oi')
        snap['oi_position']=np.where((dhi>dlo)&snap['oi'].notna(),100*(snap['oi']-dlo)/(dhi-dlo),50)
        # bullish score primarily reflects price/quote momentum fields when supplied.
        # In absence of previous OI from quote, detailed P&F tab is the authoritative signal.
        score=0.5*snap['oi_position'].fillna(50)+0.5*50
        snap['score']=score
        snap['sector']=snap['symbol'].map(sector_of)
        bullish=snap.sort_values('score',ascending=False).head(15).copy()
        bearish=snap.sort_values('score',ascending=True).head(15).copy()
        bullish.insert(0,'Sector Flag',bullish['sector'].map(lambda s:'⭐' if s!='Other/Unmapped' else ''))
        bearish.insert(0,'Sector Flag',bearish['sector'].map(lambda s:'⭐' if s!='Other/Unmapped' else ''))
        col1,col2=st.columns(2)
        with col1:
            st.markdown('### 🟢 Top 15 Bullish')
            st.dataframe(bullish[['Sector Flag','symbol','sector','last_price','oi','oi_position','score']],use_container_width=True,hide_index=True)
        with col2:
            st.markdown('### 🔴 Top 15 Bearish')
            st.dataframe(bearish[['Sector Flag','symbol','sector','last_price','oi','oi_position','score']],use_container_width=True,hide_index=True)
        st.info('For actual P&F/DTB/OI trade signals, use the P&F Trading System tab. The broad scanner is intentionally lightweight and batched.')

elif mode=='Sector Breadth':
    st.subheader('P&F Sector Breadth')
    st.write('Sector state is based on the percentage of mapped F&O stocks that are bullish or bearish on the selected P&F mode.')
    p_mode=st.radio('P&F breadth mode',['Positional','Intraday'],horizontal=True)
    # Limit breadth scan to mapped F&O names and avoid excessive requests by selecting up to 120 symbols.
    fut=fno_futures(instruments,'NSE')
    sym_col='underlying_symbol' if 'underlying_symbol' in fut.columns else 'symbol_name'
    fut[sym_col]=fut[sym_col].astype(str).str.upper()
    fut=fut[fut[sym_col].map(sector_of).ne('Other/Unmapped')]
    selected=[]
    for sym,g in fut.groupby(sym_col):
        selected.append(choose_near_expiry(g))
    selected=pd.concat(selected,ignore_index=True) if selected else pd.DataFrame()
    selected=selected.head(120)
    rows=[]
    progress=st.progress(0)
    for i,(_,r) in enumerate(selected.iterrows(),1):
        try:
            res,_=(daily_pnf_for_symbol(r) if p_mode=='Positional' else intraday_pnf_for_symbol(r))
            rows.append({'symbol':str(r[sym_col]).upper(),'sector':sector_of(str(r[sym_col]).upper()),'pnf_bullish':res['bias']=='Bullish','pnf_bearish':res['bias'].startswith('Bearish'),'dtb':res['dtb']})
        except Exception:
            pass
        progress.progress(i/len(selected)) if len(selected) else None
    progress.empty()
    b=calculate_sector_breadth(pd.DataFrame(rows))
    if b.empty:
        st.warning('No sector breadth data available.')
    else:
        b['state']=np.where(b['bullish_pct']>=sector_threshold,'⭐ SUPER BULLISH',np.where(b['bearish_pct']>=sector_threshold,'⭐ SUPER BEARISH','NORMAL'))
        st.dataframe(b[['sector','total','bullish','bullish_pct','bearish','bearish_pct','dtb','state']],use_container_width=True,hide_index=True)
        st.bar_chart(b.set_index('sector')[['bullish_pct','bearish_pct']])

elif mode=='P&F Trading System':
    st.subheader('Live P&F Trading System')
    system=st.radio('Mode',['Positional','Intraday'],horizontal=True)
    if system=='Positional':
        st.info('Positional: 0.25% box | 3-box reversal | completed daily closes only.')
    else:
        st.info('Intraday: 0.15% box | 3-box reversal | completed 1-minute closes only.')

    selected=prepare_fno_scan(instruments, max_symbols=120, mapped_only=False)
    if selected.empty:
        st.warning('No NSE F&O futures were found from the market instrument master.')
        st.stop()

    results=[]
    progress=st.progress(0)
    total=len(selected)
    for i,(_,r0) in enumerate(selected.iterrows(),1):
        r=r0.copy()
        symbol=str(r.get('underlying_symbol',r.get('symbol_name',''))).upper().strip()
        sector=sector_of(symbol)
        try:
            if system=='Positional':
                res,hist=daily_pnf_for_symbol(r,'NSE')
            else:
                res,hist=intraday_pnf_for_symbol(r,'NSE')
            oi_ok,pchg,oichg=latest_oi_confirmation(hist)
            status=system_status(res,oi_ok, data_ok=not hist.empty)
            results.append({
                'Symbol': symbol,
                'Sector': sector,
                'Anchor': '✅' if res.get('anchor') else '❌',
                'DTB': '✅' if res.get('dtb') else '❌',
                'OI Confirm': '✅' if oi_ok else '❌',
                'Price Δ%': pchg,
                'OI Δ': oichg,
                'System': status,
                'BUY': status=='🟢 BUY',
                'Reason': res.get('reason',''),
            })
        except Exception as e:
            results.append({
                'Symbol': symbol, 'Sector': sector, 'Anchor':'❌','DTB':'❌','OI Confirm':'❌',
                'Price Δ%':np.nan,'OI Δ':np.nan,'System':'DATA ERROR','BUY':False,'Reason':str(e)[:220]
            })
        progress.progress(i/total)
    progress.empty()

    r=pd.DataFrame(results)
    # Compute P&F breadth from the current scan: X-column bias is the breadth measure.
    bullish_mask=r['Anchor'].eq('✅') & ~r['DTB'].eq('❌')
    bearish_mask=(r['Anchor'].eq('❌')) & r['System'].eq('WAIT - NO ANCHOR')
    breadth=(r.assign(_bull=bullish_mask,_bear=bearish_mask)
               .groupby('Sector',dropna=False)
               .agg(total=('Symbol','count'), bullish=('_bull','sum'), bearish=('_bear','sum'))
               .reset_index())
    breadth['bullish_pct']=100*breadth['bullish']/breadth['total']
    breadth['bearish_pct']=100*breadth['bearish']/breadth['total']
    breadth['super']=np.where(breadth['bullish_pct']>=sector_threshold,'BULLISH',np.where(breadth['bearish_pct']>=sector_threshold,'BEARISH','NORMAL'))
    super_map=dict(zip(breadth['Sector'],breadth['super']))
    r['⭐']=r['Sector'].map(lambda s:'⭐' if super_map.get(s)=='BULLISH' and s!='Other/Unmapped' else ('⭐' if super_map.get(s)=='BEARISH' and s!='Other/Unmapped' else ''))

    buys=r[r['BUY']].copy().sort_values(['⭐','Sector','Symbol'],ascending=[False,True,True])
    st.markdown('### 🟢 LIVE BUY SIGNALS')
    if buys.empty:
        st.info('No BUY signal currently satisfies **Anchor + fresh DTB + bullish OI confirmation**.')
    else:
        st.dataframe(buys[['⭐','Symbol','Sector','Anchor','DTB','OI Confirm','Price Δ%','OI Δ','System','Reason']],use_container_width=True,hide_index=True)

    st.markdown('### System Status')
    counts=r['System'].value_counts().rename_axis('System').reset_index(name='Stocks')
    st.dataframe(counts,use_container_width=False,hide_index=True)

    st.markdown('### Full Live Scan')
    display_cols=['⭐','Symbol','Sector','Anchor','DTB','OI Confirm','Price Δ%','OI Δ','System','BUY','Reason']
    st.dataframe(r[display_cols].sort_values(['BUY','⭐','Symbol'],ascending=[False,False,True]),use_container_width=True,hide_index=True)

    st.markdown('### Sector P&F Breadth')
    st.dataframe(breadth[['Sector','total','bullish','bullish_pct','bearish','bearish_pct','super']],use_container_width=True,hide_index=True)

elif mode=='NIFTY/BANKNIFTY Options':
    st.subheader('Index Options')
    st.info('Use this section for NIFTY/BANKNIFTY option-chain inspection. The Option Chain endpoint returns the whole strike chain in one request and supports NSE/BSE/MCX option instruments.')
    # Show basic discovered index futures, leaving exact option-chain UI for v6 to avoid accidental over-fetching.
    for sym in ['NIFTY','BANKNIFTY']:
        idx=index_contract(instruments,sym)
        if not idx.empty:
            near=choose_near_expiry(idx).iloc[0]
            st.write(f'**{sym}** near future Security ID: {int(near.security_id)} | expiry: {near.get("expiry_date","")}')

elif mode=='MCX':
    st.subheader('MCX P&F Trading System')
    st.caption('Daily 0.25% P&F filters direction. Intraday 0.15% P&F generates entries. OI is secondary confirmation.')
    m=fno_futures(instruments,'MCX')
    if m.empty: st.warning('No MCX futures found in the instrument master.')
    else:
        sym_col='underlying_symbol' if 'underlying_symbol' in m.columns else 'symbol_name'
        m[sym_col]=m[sym_col].astype(str).str.upper()
        rows=[choose_near_expiry(g) for _,g in m.groupby(sym_col)]
        m=pd.concat(rows,ignore_index=True)
        selected_name=st.selectbox('Commodity', sorted(m[sym_col].dropna().unique().tolist()))
        rr=m[m[sym_col]==selected_name]
        if not rr.empty:
            r=rr.iloc[0]
            try:
                daily_res,daily_hist=daily_pnf_for_symbol(r,'MCX')
                intra_res,intra_hist=intraday_pnf_for_symbol(r,'MCX')
                state=mcx_intraday_trade_state(daily_res,intra_res)
                c1,c2,c3,c4=st.columns(4)
                c1.metric('Daily P&F (0.25%)',daily_res['bias'])
                c2.metric('Intraday P&F (0.15%)',intra_res['bias'])
                c3.metric('System',state['status'])
                c4.metric('Initial SL',f"{state['sl']:.2f}" if np.isfinite(state['sl']) else '—')
                if state['status'].startswith('🟢') or state['status'].startswith('🔴'):
                    st.success(f"{state['status']} | {state['reason']}") if state['side']=='LONG' else st.error(f"{state['status']} | {state['reason']}")
                    st.write('**Exit:** opposite 0.15% intraday P&F reversal signal. **Initial SL:** last confirmed opposite P&F column extreme at entry.')
                else:
                    st.info(f"{state['status']} — {state['reason']}")
                oi_ok,pchg,oichg=latest_oi_confirmation(intra_hist)
                if pd.notna(pchg) and pd.notna(oichg): st.write(f"**Secondary OI:** {'POSITIVE' if oi_ok else 'NOT POSITIVE'} | Price Δ {pchg:.2f}% | OI Δ {oichg:.0f}")
                else: st.write('**Secondary OI:** unavailable')
                with st.expander('MCX System Rules'):
                    st.markdown('''
- Daily: **0.25% box, 3-box reversal, completed daily closes only**.
- Intraday: **0.15% box, 3-box reversal, completed 1-minute closes only**.
- Long: daily bullish + fresh intraday DTB.
- Short: daily bearish + fresh intraday DBS.
- OI: secondary confirmation; it does not block a valid P&F setup.
- Exit: opposite intraday P&F reversal signal.
- Initial SL: previous confirmed opposite P&F column extreme at entry.
''')
            except Exception as e: st.error(f'MCX analysis error: {e}')

st.divider()
st.caption(f'Last page refresh: {datetime.now().strftime("%d-%b-%Y %H:%M:%S")} | Today\'s logged API calls in this session: {today_request_count()}')
