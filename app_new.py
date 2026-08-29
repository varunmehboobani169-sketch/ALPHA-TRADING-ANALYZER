import io
import time
import zipfile
import pandas as pd
import requests
import streamlit as st

DHAN_API='https://api.dhan.co/v2'
MASTER_URL='https://images.dhan.co/api-data/api-scrip-master-detailed.csv'
st.set_page_config(page_title='Dhan Options Downloader',layout='wide')

def master():
    d=pd.read_csv(MASTER_URL,low_memory=False); d.columns=[str(c).strip() for c in d.columns]; return d

def pick(d,*names):
    m={str(c).strip().upper():c for c in d.columns}
    for n in names:
        if n.upper() in m:return m[n.upper()]
    return None

def build_universe(d):
    E,S,I,ID,UID,US=pick(d,'EXCH_ID'),pick(d,'SEGMENT'),pick(d,'INSTRUMENT'),pick(d,'SECURITY_ID'),pick(d,'UNDERLYING_SECURITY_ID'),pick(d,'UNDERLYING_SYMBOL')
    SY,EX,F=pick(d,'SYMBOL_NAME'),pick(d,'EXPIRY_DATE'),pick(d,'EXPIRY_FLAG')
    if not all([E,S,I,ID,UID,US]): raise RuntimeError('Required Dhan detailed-master columns are missing.')
    x=pd.DataFrame({'exchange':d[E].astype(str).str.upper().str.strip(),'segment':d[S].astype(str).str.upper().str.strip(),'instrument':d[I].astype(str).str.upper().str.strip(),'security_id':pd.to_numeric(d[ID],errors='coerce'),'underlying_security_id':pd.to_numeric(d[UID],errors='coerce'),'underlying_symbol':d[US].astype(str).str.strip(),'symbol':d[SY].astype(str).str.strip() if SY else '','expiry_date':pd.to_datetime(d[EX],errors='coerce') if EX else pd.NaT,'expiry_flag':d[F].astype(str).str.upper().str.strip() if F else ''})
    # FIX: the previous code removed EXCH_ID/SEGMENT and then tried to infer the segment from the reduced row, so every row became ''.
    x['exchange_segment']=''
    x.loc[x.exchange.eq('NSE')&x.segment.eq('D'),'exchange_segment']='NSE_FNO'
    x.loc[x.exchange.eq('BSE')&x.segment.eq('D'),'exchange_segment']='BSE_FNO'
    x=x[x.instrument.isin(['OPTIDX','OPTSTK'])&x.exchange_segment.isin(['NSE_FNO','BSE_FNO'])].dropna(subset=['underlying_security_id'])
    x['family']=x.instrument.map({'OPTIDX':'INDEX','OPTSTK':'STOCK'})
    return x

def underlyings(x):
    return x.groupby(['exchange','exchange_segment','underlying_security_id','underlying_symbol','family'],dropna=False).agg(first_expiry=('expiry_date','min'),last_expiry=('expiry_date','max'),contracts=('security_id','count')).reset_index().sort_values(['exchange','family','underlying_symbol'])

def parse_years(s):
    out=[]
    for t in str(s).replace(';',',').split(','):
        if not t.strip():continue
        if '-' in t:
            a,b=t.split('-',1);out.extend(range(int(a),int(b)+1))
        else:out.append(int(t))
    return sorted(set(out))

def windows(y):
    cur=pd.Timestamp(y,1,1);end=pd.Timestamp(y+1,1,1)
    while cur<end:
        nxt=min(cur+pd.Timedelta(days=30),end);yield cur.strftime('%Y-%m-%d'),nxt.strftime('%Y-%m-%d');cur=nxt

def request(token,client,payload):
    r=requests.post(DHAN_API+'/charts/rollingoption',headers={'Accept':'application/json','Content-Type':'application/json','access-token':token,'client-id':client},json=payload,timeout=90)
    if r.status_code>=400:raise RuntimeError(f'Dhan HTTP {r.status_code}: {r.text[:700]}')
    j=r.json()
    if str(j.get('status','')).lower() not in ('','success'):raise RuntimeError(str(j)[:700])
    return j

def fetch(client,token,row,year,strike,side,flag,code):
    frames=[]
    for a,b in windows(year):
        p={'exchangeSegment':row.exchange_segment,'interval':'1','securityId':str(int(row.underlying_security_id)),'instrument':'OPTIDX' if row.family=='INDEX' else 'OPTSTK','expiryFlag':flag,'expiryCode':code,'strike':strike,'drvOptionType':side,'requiredData':['open','high','low','close','iv','volume','strike','oi','spot'],'fromDate':a,'toDate':b}
        j=request(token,client,p);leg=(j.get('data') or {}).get('ce' if side=='CALL' else 'pe') or {};ts=leg.get('timestamp') or []
        if not ts:continue
        n=len(ts);arr=lambda k:(list(leg.get(k) or [])+[None]*n)[:n]
        f=pd.DataFrame({'timestamp':pd.to_datetime(ts,unit='s',utc=True).tz_convert('Asia/Kolkata').tz_localize(None),'open':arr('open'),'high':arr('high'),'low':arr('low'),'close':arr('close'),'iv':arr('iv'),'volume':arr('volume'),'strike':arr('strike'),'oi':arr('oi'),'spot':arr('spot')})
        for k,v in {'underlying_symbol':row.underlying_symbol,'underlying_security_id':int(row.underlying_security_id),'exchange_segment':row.exchange_segment,'family':row.family,'option_type':side,'requested_strike':strike,'expiry_flag':flag,'expiry_code':code,'year':year}.items():f[k]=v
        frames.append(f)
    return pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()

def zip_years(df):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
        for y,g in df.groupby('year'):z.writestr(f'options_{int(y)}.csv',g.to_csv(index=False))
    return b.getvalue()

st.title('Dhan NSE / BSE Options Downloader');st.caption('Year-wise minute data • Index ATM±10 • Stock F&O ATM±3')
with st.sidebar:
    client=st.text_input('Dhan Client ID');token=st.text_input('Dhan Access Token',type='password');years_text=st.text_input('Years','2022-2026');stock_mode=st.selectbox('Stock strike range',['ATM-3 to ATM+3','ATM only']);expiry_codes=st.multiselect('Expiry codes',[0,1,2],default=[0,1,2]);delay=st.number_input('Request delay (sec)',0.0,5.0,0.25,0.25)
if not client or not token:st.info('Enter your Dhan Client ID and Access Token.');st.stop()
if st.button('LOAD NSE + BSE F&O UNIVERSE',type='primary'):
    try:
        raw=master();u=build_universe(raw);st.session_state.underlyings=underlyings(u);st.success(f'Loaded {len(st.session_state.underlyings):,} option underlyings.');st.caption(f'Master rows: {len(raw):,} | option contracts: {len(u):,} | segments: {sorted(u.exchange_segment.unique())}')
    except Exception as e:st.error(str(e));st.stop()
u=st.session_state.get('underlyings',pd.DataFrame())
if u.empty:st.stop()
ex=st.multiselect('Exchange',sorted(u.exchange.unique()),default=sorted(u.exchange.unique()));fam=st.multiselect('Type',['INDEX','STOCK'],default=['INDEX','STOCK']);f=u[u.exchange.isin(ex)&u.family.isin(fam)];st.dataframe(f,use_container_width=True,height=420)
symbols=st.multiselect('Select underlyings',sorted(f.underlying_symbol.unique()),default=sorted(f.underlying_symbol.unique())[:5]);sel=f[f.underlying_symbol.isin(symbols)]
if st.button('DOWNLOAD YEAR-WISE DATA',type='primary',use_container_width=True):
    ys=parse_years(years_text)
    if not ys or not expiry_codes or sel.empty:st.error('Select valid years, expiry codes and underlyings.');st.stop()
    idx=[f'ATM{n:+d}' if n else 'ATM' for n in range(-10,11)];stk=[f'ATM{n:+d}' if n else 'ATM' for n in (range(-3,4) if stock_mode.startswith('ATM-3') else [0])];jobs=[]
    for _,r in sel.iterrows():
        for y in ys:
            for flag in (['WEEK','MONTH'] if r.family=='INDEX' else ['MONTH']):
                for code in expiry_codes:
                    for strike in (idx if r.family=='INDEX' else stk):
                        for side in ['CALL','PUT']:jobs.append((r,y,strike,side,flag,code))
    prog=st.progress(0);status=st.empty();frames=[];errors=[]
    for i,(r,y,strike,side,flag,code) in enumerate(jobs,1):
        status.write(f'{r.underlying_symbol} | {y} | {flag} {code} | {strike} | {side}')
        try:
            z=fetch(client,token,r,y,strike,side,flag,code)
            if not z.empty:frames.append(z)
        except Exception as e:errors.append({'underlying':r.underlying_symbol,'year':y,'expiry_flag':flag,'expiry_code':code,'strike':strike,'side':side,'error':str(e)})
        time.sleep(delay);prog.progress(i/len(jobs))
    if not frames:
        st.error('No data returned. Universe discovery is fixed; remaining causes are Dhan API access, date availability, or request parameters.');
        if errors:st.dataframe(pd.DataFrame(errors),use_container_width=True)
    else:
        data=pd.concat(frames,ignore_index=True).drop_duplicates();st.success(f'Downloaded {len(data):,} rows across {data.year.nunique()} years.');st.download_button('DOWNLOAD YEAR-WISE ZIP',zip_years(data),'dhan_options_yearwise.zip','application/zip',use_container_width=True)
        if errors:st.warning(f'{len(errors):,} request groups returned errors.');st.dataframe(pd.DataFrame(errors),use_container_width=True)
