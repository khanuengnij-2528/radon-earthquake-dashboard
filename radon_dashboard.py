"""
Radon & Earthquake Hazard Dashboard
Real-time — Google Sheets (Radon) + Excel URL (Earthquake)
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from scipy.stats import mannwhitneyu
from lifelines import KaplanMeierFitter, NelsonAalenFitter
import time
from datetime import datetime, timedelta
import warnings
import json
import gspread
from google.oauth2.service_account import Credentials
warnings.filterwarnings("ignore")

# ─── Page Config ────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Radon & Earthquake Hazard Dashboard",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ─── CSS Theme ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
  /* Cream Theme */
  .stApp { background-color: #fdf6ec; color: #2c2c2c; }
  [data-testid="stSidebar"] { background-color: #f5ead8; border-right: 1px solid #d4b896; color: #2c2c2c !important; }
  [data-testid="stSidebar"] * { color: #2c2c2c !important; }
  [data-testid="stSidebar"] label { color: #2c2c2c !important; }
  [data-testid="stSidebar"] p { color: #2c2c2c !important; }
  [data-testid="stSidebar"] span { color: #2c2c2c !important; }
  [data-testid="stSidebar"] input { color: #2c2c2c !important; background-color: #fff8f0 !important; border: 1px solid #d4b896 !important; }
  [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] { color: #2c2c2c !important; }
  /* Download buttons */
  [data-testid="stDownloadButton"] button { color: #fff8f0 !important; background-color: #a0522d !important; border: 1px solid #a0522d !important; }
  [data-testid="stDownloadButton"] button:hover { background-color: #8b4513 !important; }
  /* Metric cards */
  div[data-testid="stMetric"] { background:#fff8f0; border:1px solid #d4b896; border-radius:10px; padding:12px; }
  div[data-testid="stMetric"] label { color:#7a5c3a !important; font-size:0.72rem !important; }
  div[data-testid="stMetric"] [data-testid="stMetricValue"] { font-size:1.6rem !important; color:#2c2c2c !important; }
  .block-container { padding-top: 1rem; padding-bottom: 1rem; }

  /* Metric cards */
  .metric-card {
    background: linear-gradient(135deg, #1c2230 0%, #1a2035 100%);
    border: 1px solid #30363d;
    border-radius: 12px;
    padding: 16px 20px;
    text-align: center;
  }
  .metric-value { font-size: 2rem; font-weight: 700; }
  .metric-label { font-size: 0.75rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.08em; margin-top: 4px; }

  /* Risk badges */
  .badge-low      { background:#1a3a2a; color:#3fb950; border:1px solid #3fb950; border-radius:6px; padding:2px 10px; font-size:0.75rem; font-weight:600; }
  .badge-moderate { background:#2d2a1a; color:#e3b341; border:1px solid #e3b341; border-radius:6px; padding:2px 10px; font-size:0.75rem; font-weight:600; }
  .badge-high     { background:#3a2a1a; color:#f0883e; border:1px solid #f0883e; border-radius:6px; padding:2px 10px; font-size:0.75rem; font-weight:600; }
  .badge-veryhigh { background:#3a1a1a; color:#ff7b72; border:1px solid #ff7b72; border-radius:6px; padding:2px 10px; font-size:0.75rem; font-weight:600; }

  /* Section headers */
  .section-header {
    font-size: 1rem; font-weight: 600; color: #58a6ff;
    border-left: 3px solid #58a6ff; padding-left: 10px;
    margin: 8px 0 12px 0;
  }

  /* Live indicator */
  .live-dot {
    display:inline-block; width:8px; height:8px;
    background:#3fb950; border-radius:50%;
    animation: pulse 1.5s infinite;
    margin-right:6px;
  }
  @keyframes pulse {
    0%,100%{opacity:1;} 50%{opacity:0.3;}
  }

  /* Scrollable table */
  .risk-table { max-height:260px; overflow-y:auto; }
  .risk-table table { width:100%; border-collapse:collapse; font-size:0.8rem; }
  .risk-table th { background:#1c2230; color:#8b949e; padding:6px 10px; text-align:left; position:sticky;top:0; }
  .risk-table td { padding:6px 10px; border-bottom:1px solid #21262d; }

  div[data-testid="stMetric"] { background:#161b22; border:1px solid #30363d; border-radius:10px; padding:12px; }
  div[data-testid="stMetric"] label { color:#8b949e !important; font-size:0.72rem !important; }
  div[data-testid="stMetric"] [data-testid="stMetricValue"] { font-size:1.6rem !important; color:#e6edf3 !important; }
</style>
""", unsafe_allow_html=True)

# ─── Config ──────────────────────────────────────────────────────────────────
SHEET_URL   = "https://docs.google.com/spreadsheets/d/1NtKV_9fanjFD-mm-qVkUWQ-LGIv_0hD4d5A8dSuwdOk/edit?gid=0#gid=0"
XLSX_URL    = "https://docs.google.com/spreadsheets/d/1ZXCX5H5YJzFdYyuIGZnpYmCBuKRbfz8rx9qT_ZXc8bc/export?format=xlsx&gid=2058199297"  # fallback only
EQ_SHEET_ID = "1ZXCX5H5YJzFdYyuIGZnpYmCBuKRbfz8rx9qT_ZXc8bc"
EQ_GID      = "2058199297"
RADON_COL   = "Short Term (pCi/L)"
TIME_COL    = "Timestamp"
CACHE_TTL   = 300   # cache 5 นาที

# ─── Google Sheets Auth (Streamlit Secrets) ───────────────────────────────────
@st.cache_resource
def get_gspread_client():
    """
    อ่าน Service Account จาก st.secrets
    ใส่ทั้ง dict ของ credentials.json ไว้ใน [gcp_service_account]
    ใน Streamlit Cloud → Settings → Secrets
    """
    info = dict(st.secrets["gcp_service_account"])
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)

# ─── Real Data Loaders ────────────────────────────────────────────────────────
@st.cache_data(ttl=CACHE_TTL)
def load_radon_data():
    """ดึง Radon จาก Google Sheets → คืน pd.Series index=Timestamp
    โครงสร้าง Sheet ใหม่: Timestamp (dd/mm/yyyy HH:MM:SS ค.ศ.), Short Term (pCi/L)
    """
    try:
        gc = get_gspread_client()
        ws = gc.open_by_url(SHEET_URL).sheet1
        df = pd.DataFrame(ws.get_all_records())
        df.columns = df.columns.str.strip()

        # Timestamp อยู่ในรูปแบบ dd/mm/yyyy HH:MM:SS (ค.ศ. แล้ว ไม่ต้องแปลง)
        if TIME_COL not in df.columns:
            # ลองหาคอลัมน์ที่ชื่อใกล้เคียง
            time_candidates = [c for c in df.columns if "time" in c.lower() or "stamp" in c.lower() or "date" in c.lower()]
            if time_candidates:
                df = df.rename(columns={time_candidates[0]: TIME_COL})
            else:
                raise ValueError(f"ไม่พบคอลัมน์ Timestamp columns={list(df.columns)}")

        # แปลง Timestamp — รองรับทั้ง dd/mm/yyyy และ yyyy-mm-dd
        def parse_timestamp(x):
            s = str(x).strip()
            # ลอง format dd/mm/yyyy HH:MM:SS ก่อน
            for fmt in ["%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                        "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M"]:
                try:
                    return pd.to_datetime(s, format=fmt)
                except:
                    pass
            # fallback
            return pd.to_datetime(s, errors="coerce")

        df[TIME_COL] = df[TIME_COL].apply(parse_timestamp)
        df = df.dropna(subset=[TIME_COL]).set_index(TIME_COL).sort_index()

        # ใช้ Short Term (pCi/L)
        radon_col = RADON_COL  # "Short Term (pCi/L)"
        if radon_col not in df.columns:
            # หาคอลัมน์ที่ชื่อใกล้เคียง
            short_candidates = [c for c in df.columns if "short" in c.lower()]
            if short_candidates:
                radon_col = short_candidates[0]
            else:
                raise ValueError(f"ไม่พบคอลัมน์ Short Term columns={list(df.columns)}")

        radon = pd.to_numeric(df[radon_col], errors="coerce").replace(0, np.nan)
        radon = radon[radon <= 100]  # กรองค่าผิดปกติเกิน 100 pCi/L

        # ลบแถวซ้ำ (ข้อมูลซ้ำ 3 แถวต่อครั้ง) — เก็บเฉพาะ unique timestamp
        radon = radon[~radon.index.duplicated(keep="first")]

        st.sidebar.markdown(f"📡 Radon col: `{radon_col}` | rows after dedup: `{len(radon):,}`")
        return radon

    except Exception as e:
        st.warning(f"⚠️ ไม่สามารถดึงข้อมูล Radon ได้: {e}\n→ ใช้ Mock Data แทน")
        return _mock_radon()

@st.cache_data(ttl=CACHE_TTL)
def load_eq_data():
    """ดึง Earthquake จาก Google Sheets ผ่าน gspread (ใช้ Service Account เดิม)"""
    try:
        gc  = get_gspread_client()
        sh  = gc.open_by_key(EQ_SHEET_ID)
        # หา worksheet จาก gid
        ws  = None
        for w in sh.worksheets():
            if str(w.id) == EQ_GID:
                ws = w
                break
        if ws is None:
            ws = sh.get_worksheet(0)

        records = ws.get_all_records()
        eq = pd.DataFrame(records)

        if eq.empty:
            raise ValueError("ไม่มีข้อมูลใน sheet")

        # หาคอลัมน์เวลา
        time_col = next((c for c in eq.columns if "time" in c.lower() or "วัน" in c.lower()), None)
        if time_col is None:
            raise ValueError(f"ไม่พบคอลัมน์เวลา columns={list(eq.columns)}")
        eq = eq.rename(columns={time_col: "Time (Thailand)"})

        eq["Time (Thailand)"] = pd.to_datetime(eq["Time (Thailand)"], errors="coerce")
        eq = eq.dropna(subset=["Time (Thailand)"]).sort_values("Time (Thailand)").reset_index(drop=True)
        eq["Magnitude"] = pd.to_numeric(eq["Magnitude"], errors="coerce")
        eq = eq.dropna(subset=["Magnitude"])

        def parse_lat(x):
            s = str(x).strip()
            if s.endswith("°N"): return float(s.replace("°N",""))
            if s.endswith("°S"): return -float(s.replace("°S",""))
            try: return float(s)
            except: return np.nan

        def parse_lon(x):
            s = str(x).strip()
            if s.endswith("°E"): return float(s.replace("°E",""))
            if s.endswith("°W"): return -float(s.replace("°W",""))
            try: return float(s)
            except: return np.nan

        eq["Latitude"]  = eq["Latitude"].apply(parse_lat)
        eq["Longitude"] = eq["Longitude"].apply(parse_lon)
        eq = eq.dropna(subset=["Latitude","Longitude"])
        return eq

    except Exception as e:
        st.warning(f"⚠️ ไม่สามารถดึงข้อมูล Earthquake ได้: {e}\n→ ใช้ Mock Data แทน")
        return _mock_eq()

# ─── Fallback Mock Data (ใช้เมื่อเชื่อมไม่ได้) ────────────────────────────────
def _mock_radon(start="2025-01-23", end="2026-05-17"):
    np.random.seed(42)
    idx   = pd.date_range(start, end, freq="1h")
    base  = 2.5 + np.sin(np.linspace(0, 6*np.pi, len(idx))) * 0.8
    radon = base * np.random.lognormal(0, 0.35, len(idx))
    anom  = np.random.choice(len(idx), 120, replace=False)
    radon[anom] *= np.random.uniform(2.5, 5.0, 120)
    return pd.Series(radon, index=idx, name=RADON_COL).replace(0, np.nan).dropna()

def _mock_eq(start="2025-01-23", end="2026-05-17"):
    np.random.seed(7)
    n     = 1800
    times = pd.date_range(start, end, periods=n) + pd.to_timedelta(np.random.randint(0,86400,n), unit="s")
    mags  = np.clip(np.random.exponential(1.0, n) + 2.5, 2.5, 7.5)
    return pd.DataFrame({"Time (Thailand)": times, "Magnitude": mags,
                         "Latitude": np.random.uniform(5,30,n),
                         "Longitude": np.random.uniform(88,110,n)})

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0088
    lat1,lon1,lat2,lon2 = map(np.radians,[lat1,lon1,lat2,lon2])
    dlat, dlon = lat2-lat1, lon2-lon1
    a = np.sin(dlat/2)**2 + np.cos(lat1)*np.cos(lat2)*np.sin(dlon/2)**2
    return R * 2 * np.arcsin(np.sqrt(a))

def compute_iqr_anomaly(radon):
    """Global IQR — ใช้สำหรับ display stats เท่านั้น"""
    Q1, Q3 = radon.quantile(0.25), radon.quantile(0.75)
    IQR = Q3 - Q1
    upper = Q3 + 1.5 * IQR
    anomalies = radon[radon > upper].dropna()
    return Q1, Q3, IQR, upper, anomalies

def compute_rolling_iqr_anomaly(radon, window_days=30):
    """
    Rolling IQR Anomaly Detection
    คำนวณ Q1, Q3, IQR ใหม่ทุก window_days วัน
    สะท้อนพฤติกรรมปัจจุบันได้แม่นยำกว่า Global IQR
    อ้างอิง: Sliding Window Analysis (Non-stationary Time Series)
    """
    radon_daily = radon.resample("D").median()  # รวมเป็นรายวัน
    anomaly_flags = pd.Series(False, index=radon.index)
    upper_series  = pd.Series(np.nan, index=radon.index)

    # คำนวณ rolling IQR ทีละ window
    dates = radon_daily.index
    for i in range(window_days, len(dates)+1):
        window = radon_daily.iloc[i-window_days:i]
        Q1_w = window.quantile(0.25)
        Q3_w = window.quantile(0.75)
        IQR_w = Q3_w - Q1_w
        upper_w = Q3_w + 1.5 * IQR_w

        # ใช้กับข้อมูลในวันสุดท้ายของ window
        day = dates[i-1]
        mask = radon.index.normalize() == day
        upper_series[mask] = upper_w
        anomaly_flags[mask] = radon[mask] > upper_w

    anomalies = radon[anomaly_flags].dropna()
    # Global stats สำหรับแสดงผล
    Q1, Q3 = radon.quantile(0.25), radon.quantile(0.75)
    IQR = Q3 - Q1
    upper_global = Q3 + 1.5 * IQR
    return Q1, Q3, IQR, upper_global, anomalies, upper_series

def time_to_first_eq(anomaly_time, eq_near, days_ahead=30, min_mag=4.0):
    t0 = pd.to_datetime(anomaly_time)
    t1 = t0 + pd.Timedelta(days=days_ahead)
    sub = eq_near[(eq_near["Time (Thailand)"] > t0) &
                  (eq_near["Time (Thailand)"] <= t1) &
                  (eq_near["Magnitude"] >= min_mag)]
    if sub.empty:
        return float(days_ahead), 0
    return float((sub["Time (Thailand)"].min() - t0).total_seconds() / 86400), 1

def piecewise_hazard(T, E, max_day=30):
    T, E = np.asarray(T, float), np.asarray(E, int)
    rows = []
    for d in range(1, max_day+1):
        at_risk = int(np.sum(T >= (d-1)))
        events  = int(np.sum((E==1) & (T>(d-1)) & (T<=d)))
        haz = events/at_risk if at_risk > 0 else np.nan
        rows.append({"day":d,"at_risk":at_risk,"events":events,"hazard":haz})
    return pd.DataFrame(rows)

def risk_level(h):
    if pd.isna(h): return "LOW"
    if h >= 0.20:  return "VERY HIGH"
    if h >= 0.10:  return "HIGH"
    if h >= 0.05:  return "MODERATE"
    return "LOW"

RISK_COLOR = {"LOW":"#3fb950","MODERATE":"#e3b341","HIGH":"#f0883e","VERY HIGH":"#ff7b72"}
PLOT_BG    = "#fdf6ec"
GRID_COLOR = "#d4b896"
FONT_COLOR = "#2c2c2c"

def dark_layout(fig, title="", height=380):
    fig.update_layout(
        title=dict(text=title, font=dict(color=FONT_COLOR, size=13), x=0.01),
        paper_bgcolor=PLOT_BG, plot_bgcolor="#fdf6ec",
        font=dict(color=FONT_COLOR, family="Inter, sans-serif"),
        height=height, margin=dict(l=50,r=20,t=40,b=40),
        legend=dict(bgcolor="rgba(0,0,0,0)", bordercolor="#30363d",
                    borderwidth=1, font=dict(size=11)),
        xaxis=dict(gridcolor=GRID_COLOR, zerolinecolor=GRID_COLOR,
                   showline=True, linecolor="#30363d"),
        yaxis=dict(gridcolor=GRID_COLOR, zerolinecolor=GRID_COLOR,
                   showline=True, linecolor="#30363d"),
    )
    return fig

# ─── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 🔬 Dashboard Controls")
    st.markdown("---")

    _today = datetime.today().date()
    _start_default = datetime(2026,1,23).date()
    date_range = st.date_input(
        "📅 Date Range",
        value=(_start_default, _today),
        min_value=datetime(2025,1,1).date(),
        max_value=_today
    )
    start_date = pd.to_datetime(date_range[0]) if len(date_range)==2 else pd.to_datetime("2026-01-23")
    end_date   = pd.to_datetime(date_range[1]) if len(date_range)==2 else pd.to_datetime(_today)

    mag_threshold = st.slider(
        "⚡ Magnitude Threshold (Forecast)",
        min_value=1.0, max_value=10.0, value=4.0, step=0.5
    )
    radius_km = st.slider("📡 Radius (km)", 100, 1000, 1000, step=100)
    days_ahead = st.slider("📆 Forecast Window (days)", 7, 60, 30)

    st.markdown("---")
    realtime = st.toggle("🔴 Real-time Simulation", value=True)
    refresh_sec = st.slider("Refresh every (sec)", 5, 60, 10, disabled=not realtime)

    st.markdown("---")
    st.markdown("**📍 Station Info**")
    st.markdown("<span style='color:#e6edf3'>Lat: <b>18.795°N</b> &nbsp; Lon: <b>98.953°E</b></span>", unsafe_allow_html=True)
    st.markdown("Location: Chiang Mai, TH")

# ─── Load data ───────────────────────────────────────────────────────────────
station = (18.795, 98.953)

with st.spinner("🔄 กำลังดึงข้อมูลจาก Google Sheets และ Earthquake database..."):
    radon_full = load_radon_data()
    eq_full    = load_eq_data()

# สถานะข้อมูลใน sidebar
data_ok   = len(radon_full) > 100 and len(eq_full) > 10
src_label = "🟢 Live Data" if data_ok else "🟡 Mock Data (fallback)"
st.sidebar.markdown(f"**Data source:** {src_label}")
st.sidebar.markdown(f"<span style='color:#e6edf3'>Radon: <b>{len(radon_full):,}</b> rows | EQ: <b>{len(eq_full):,}</b> rows</span>", unsafe_allow_html=True)

# Filter by date range
radon = radon_full.loc[start_date:end_date].copy()
eq_sel = eq_full[(eq_full["Time (Thailand)"] >= start_date) &
                 (eq_full["Time (Thailand)"] <= end_date)].copy()

# Distance filter
eq_sel["distance_km"] = haversine_km(
    station[0], station[1], eq_sel["Latitude"].values, eq_sel["Longitude"].values)
eq_near = eq_sel[eq_sel["distance_km"] <= radius_km].copy()

# Rolling IQR (30-day window) — สะท้อนพฤติกรรมปัจจุบัน
Q1, Q3, IQR, upper, radon_anom, upper_rolling = compute_rolling_iqr_anomaly(radon, window_days=30)

# Time-to-EQ survival data — ใช้ 1,000 anomaly ล่าสุด
# อ้างอิง: Recency Bias in Precursor Studies + Sliding Window Analysis
sample_anom = radon_anom.iloc[-1000:] if len(radon_anom) > 1000 else radon_anom
min_mag_list = [3.0, 4.0, 5.0]
rows = []
for t in sample_anom.index:
    row = {"Anomaly_Time": t, "pCi/L": float(sample_anom.loc[t] if not isinstance(sample_anom.loc[t], pd.Series) else sample_anom.loc[t].iloc[0])}
    for m in min_mag_list:
        dt, ev = time_to_first_eq(t, eq_near, days_ahead=days_ahead, min_mag=m)
        row[f"dt_M{str(m).replace('.','')}"]=dt
        row[f"ev_M{str(m).replace('.','')}"]=ev
    rows.append(row)
if not rows:
    dt_df = pd.DataFrame(columns=["pCi/L","dt_M30","ev_M30","dt_M40","ev_M40","dt_M50","ev_M50"])
    dt_df.index.name = "Anomaly_Time"
else:
    dt_df = pd.DataFrame(rows).set_index("Anomaly_Time")

T3,E3 = dt_df["dt_M30"], dt_df["ev_M30"]
T4,E4 = dt_df["dt_M40"], dt_df["ev_M40"]
T5,E5 = dt_df["dt_M50"], dt_df["ev_M50"]

dt_3_4 = dt_df.loc[(E3==1)&(E4==0),"dt_M30"].dropna()
dt_4_5 = dt_df.loc[(E4==1)&(E5==0),"dt_M40"].dropna()
dt_5up = dt_df.loc[E5==1,"dt_M50"].dropna()

# Hazard (for selected mag threshold)
# snap mag_threshold ให้ตรงกับ min_mag_list ที่มีอยู่
_snap = min(min_mag_list, key=lambda x: abs(x - mag_threshold))
key = str(_snap).replace(".","")
T_sel = dt_df[f"dt_M{key}"]; E_sel = dt_df[f"ev_M{key}"]
haz_df = piecewise_hazard(T_sel.values, E_sel.values, max_day=days_ahead)
tail_start = max(1, int(days_ahead*0.7))
tail = haz_df.loc[haz_df["day"]>=tail_start,"hazard"].dropna()
baseline = float(tail.mean()) if len(tail) else 0.01
baseline_safe = max(baseline, 1e-6)
haz_df["risk_level"] = haz_df["hazard"].apply(risk_level)
anchor = radon_anom.index[-1] if len(radon_anom) else end_date
haz_df["forecast_date"] = [anchor + timedelta(days=int(d)-1) for d in haz_df["day"]]

# ─── Header ──────────────────────────────────────────────────────────────────
now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
st.markdown(f"""
<div style="background:linear-gradient(90deg,#1c2230,#0d1117);border:1px solid #30363d;
            border-radius:12px;padding:14px 24px;margin-bottom:16px;
            display:flex;align-items:center;justify-content:space-between;">
  <div>
    <span style="font-size:1.4rem;font-weight:700;color:#58a6ff;">
      🔬 Radon & Earthquake Hazard Dashboard
    </span>
    <span style="margin-left:14px;font-size:0.78rem;color:#8b949e;">
      Station: Chiang Mai (18.795°N, 98.953°E) &nbsp;|&nbsp; Radius: {radius_km} km
    </span>
  </div>
  <div style="text-align:right;font-size:0.78rem;color:#8b949e;">
    <span class="live-dot"></span>
    <b style="color:#3fb950;">LIVE</b> &nbsp;{now_str}
  </div>
</div>
""", unsafe_allow_html=True)

# ─── KPI Row ─────────────────────────────────────────────────────────────────
c1,c2,c3,c4,c5 = st.columns(5)
peak_day = int(haz_df.loc[haz_df["hazard"].idxmax(),"day"]) if haz_df["hazard"].notna().any() else 0
n_high = int((haz_df["risk_level"].isin(["HIGH","VERY HIGH"])).sum())
latest_radon = float(radon_anom.iloc[-1]) if len(radon_anom) else 0.0
eq_count = len(eq_near[eq_near["Magnitude"]>=mag_threshold])

c1.metric("🌫️ Latest Anomaly", f"{latest_radon:.2f} pCi/L", f"+{latest_radon-upper:.1f} above IQR")
c2.metric("⚠️ Total Anomalies", f"{len(radon_anom):,}", f"IQR upper: {upper:.2f}")
c3.metric("🌍 EQ Events Nearby", f"{eq_count}", f"M≥{mag_threshold}, R≤{radius_km}km")
c4.metric("📈 Peak Hazard Day", f"Day {peak_day}", f"Max: {haz_df['hazard'].max():.3f}")
c5.metric("🚨 High Risk Days", f"{n_high} days", f"in next {days_ahead} days")

st.markdown("<br>", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — Radon Time Series
# ═══════════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-header">📊 Section 1 — Radon Time Series & IQR Anomaly Detection</div>', unsafe_allow_html=True)

# downsample for display (max 2000 pts)
step = max(1, len(radon)//2000)
radon_plot = radon.iloc[::step]
anom_plot  = radon_anom

fig1 = go.Figure()
fig1.add_trace(go.Scatter(
    x=radon_plot.index, y=radon_plot.values,
    mode="lines", name="Radon (pCi/L)",
    line=dict(color="#388bfd", width=1.2), opacity=0.85
))
fig1.add_trace(go.Scatter(
    x=anom_plot.index, y=anom_plot.values,
    mode="markers", name="Anomaly",
    marker=dict(color="#ff7b72", size=6, symbol="circle",
                line=dict(color="#ff7b72", width=1))
))
fig1.add_hline(y=float(upper), line_dash="dash", line_color="#f0883e",
               annotation_text=f"Global IQR Upper = {upper:.2f}", annotation_font_color="#f0883e")
# Rolling IQR upper bound
upper_r = upper_rolling.dropna()
if len(upper_r) > 0:
    step_r = max(1, len(upper_r)//500)
    fig1.add_trace(go.Scatter(
        x=upper_r.iloc[::step_r].index,
        y=upper_r.iloc[::step_r].values,
        mode="lines", name="Rolling IQR Upper (30d)",
        line=dict(color="#e3b341", width=1.2, dash="dot"), opacity=0.8
    ))
fig1.add_hline(y=float(Q3), line_dash="dot", line_color="#3fb950",
               annotation_text=f"Q3={Q3:.2f}", annotation_font_color="#3fb950")
dark_layout(fig1, f"Radon Short Term — {len(radon_anom)} anomalies detected", height=300)
st.plotly_chart(fig1, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — Earthquake Map
# ═══════════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-header">🗺️ Section 2 — Earthquake Map</div>', unsafe_allow_html=True)

col_map, col_eq_stats = st.columns([3,1])
with col_map:
    eq_map = eq_near.copy()
    eq_map["mag_size"] = (eq_map["Magnitude"] - 2) * 5
    eq_map["label"] = eq_map.apply(
        lambda r: f"M{r['Magnitude']:.1f} | {r['Time (Thailand)'].strftime('%Y-%m-%d')}<br>Dist: {r['distance_km']:.0f} km",
        axis=1)
    fig_map = go.Figure()
    # EQ scatter
    fig_map.add_trace(go.Scattergeo(
        lat=eq_map["Latitude"].tolist(),
        lon=eq_map["Longitude"].tolist(),
        mode="markers",
        marker=dict(
            size=eq_map["mag_size"].clip(3,25).tolist(),
            color=eq_map["Magnitude"].tolist(),
            colorscale=[[0,"#3fb950"],[0.4,"#e3b341"],[0.7,"#f0883e"],[1,"#ff7b72"]],
            colorbar=dict(title="Mag", thickness=10, len=0.6),
            opacity=0.75, line=dict(width=0)
        ),
        text=eq_map["label"].tolist(), hoverinfo="text", name="Earthquakes"
    ))
    # Station
    fig_map.add_trace(go.Scattergeo(
        lat=[station[0]], lon=[station[1]],
        mode="markers+text",
        marker=dict(size=14, color="#f1c40f", symbol="star"),
        text=["📡 Station"], textposition="top center",
        textfont=dict(color="#f1c40f", size=11),
        name="Radon Station", hoverinfo="name"
    ))
    fig_map.update_geos(
        center=dict(lat=station[0], lon=station[1]),
        projection_scale=4,
        showland=True, landcolor="#1c2230",
        showocean=True, oceancolor="#0d1117",
        showcoastlines=True, coastlinecolor="#30363d",
        showframe=False, showcountries=True, countrycolor="#30363d",
        bgcolor=PLOT_BG
    )
    fig_map.update_layout(
        paper_bgcolor=PLOT_BG, font=dict(color=FONT_COLOR),
        height=380, margin=dict(l=0,r=0,t=30,b=0),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
        title=dict(text=f"EQ locations within {radius_km} km (N={len(eq_near)})",
                   font=dict(color=FONT_COLOR, size=12), x=0.01)
    )
    st.plotly_chart(fig_map, use_container_width=True)

with col_eq_stats:
    st.markdown("**📊 EQ Stats**")
    for m, color in [(3.0,"#2e8b57"),(4.0,"#c8860a"),(5.0,"#c05000"),(6.0,"#c0392b")]:
        cnt = int((eq_near["Magnitude"]>=m).sum())
        st.markdown(f"""
        <div style="background:#fff8f0;border:1px solid #d4b896;border-radius:8px;
                    padding:10px;margin-bottom:8px;text-align:center;">
          <div style="font-size:1.4rem;font-weight:700;color:{color};">{cnt}</div>
          <div style="font-size:0.72rem;color:#8b949e;">M ≥ {m}</div>
        </div>""", unsafe_allow_html=True)
    max_mag = eq_near["Magnitude"].max() if len(eq_near) else 0
    st.markdown(f"""
    <div style="background:#fff8f0;border:1px solid #d4b896;border-radius:8px;
                padding:10px;margin-bottom:8px;text-align:center;">
      <div style="font-size:1.4rem;font-weight:700;color:#c0392b;">{max_mag:.1f}</div>
      <div style="font-size:0.72rem;color:#7a5c3a;">Max Magnitude</div>
    </div>""", unsafe_allow_html=True)

    # Detection capability
    total_eq = len(eq_near)
    eq_before_anom = 0
    for t in radon_anom.index:
        window_start = t - pd.Timedelta(days=30)
        matched = eq_near[
            (eq_near["Time (Thailand)"] >= t) &
            (eq_near["Time (Thailand)"] <= t + pd.Timedelta(days=30)) &
            (eq_near["Magnitude"] >= mag_threshold)
        ]
        eq_before_anom += len(matched)
    detect_pct = min(100.0, (eq_before_anom / total_eq * 100)) if total_eq > 0 else 0
    detect_color = "#2e8b57" if detect_pct >= 60 else "#c8860a" if detect_pct >= 30 else "#c0392b"
    st.markdown(f"""
    <div style="background:#fff8f0;border:1px solid #d4b896;border-radius:8px;
                padding:10px;margin-bottom:8px;text-align:center;">
      <div style="font-size:1.4rem;font-weight:700;color:{detect_color};">{detect_pct:.1f}%</div>
      <div style="font-size:0.72rem;color:#7a5c3a;">🎯 จับสัญญาณ EQ ได้</div>
      <div style="font-size:0.65rem;color:#a08060;">M≥{mag_threshold} ใน 30 วันหลัง Anomaly</div>
    </div>""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 & 4 — Precursor + Survival (side by side)
# ═══════════════════════════════════════════════════════════════════════════
col3, col4 = st.columns(2)

with col3:
    st.markdown('<div class="section-header">📦 Section 3 — Precursor Time (Δt) Analysis</div>', unsafe_allow_html=True)

    groups_data = {"3≤M<4": dt_3_4.values, "4≤M<5": dt_4_5.values, "M≥5": dt_5up.values}
    groups_data = {k: v for k, v in groups_data.items() if len(v) > 0}

    if groups_data:
        colors = {"3≤M<4":"#3fb950","4≤M<5":"#e3b341","M≥5":"#ff7b72"}
        fig3 = go.Figure()
        rng = np.random.default_rng(0)
        for i,(gname, gdata) in enumerate(groups_data.items()):
            fig3.add_trace(go.Box(
                y=gdata, name=gname,
                marker_color=colors.get(gname,"#58a6ff"),
                boxmean=True, jitter=0.4, pointpos=-1.8,
                marker=dict(size=4, opacity=0.5)
            ))

        # Mann-Whitney annotation
        if len(dt_4_5)>1 and len(dt_5up)>1:
            stat, p = mannwhitneyu(dt_4_5, dt_5up, alternative="two-sided")
            annot = f"M-W U test (M4 vs M5): p={p:.4f} {'✓ sig' if p<0.05 else '✗ n.s.'}"
        else:
            annot = "Not enough data for M-W U test"

        dark_layout(fig3, "Precursor Δt by Magnitude Group", height=360)
        fig3.update_layout(
            yaxis_title="Days before EQ (Δt)",
            annotations=[dict(text=annot, xref="paper", yref="paper",
                              x=0.01, y=1.05, showarrow=False,
                              font=dict(size=10, color="#8b949e"))]
        )
        st.plotly_chart(fig3, use_container_width=True)
    else:
        st.info("Insufficient event data for precursor analysis.")

with col4:
    st.markdown('<div class="section-header">📈 Section 4 — Kaplan-Meier Survival Curves</div>', unsafe_allow_html=True)

    fig4 = go.Figure()
    km_configs = [(T3,E3,"M≥3","#3fb950"),(T4,E4,"M≥4","#e3b341"),(T5,E5,"M≥5","#ff7b72")]
    for T,E,label,color in km_configs:
        if E.sum() == 0: continue
        try:
            kmf = KaplanMeierFitter()
            kmf.fit(T, event_observed=E, label=label)
            t_ = kmf.survival_function_.index
            s_ = kmf.survival_function_[label].values
            ci = kmf.confidence_interval_
            lo = ci.iloc[:,0].values
            hi = ci.iloc[:,1].values
            r,g,b = tuple(int(color.lstrip('#')[i:i+2],16) for i in (0,2,4))
            fig4.add_trace(go.Scatter(
                x=list(t_)+list(t_[::-1]), y=list(hi)+list(lo[::-1]),
                fill="toself",
                fillcolor=f"rgba({r},{g},{b},0.15)",
                line=dict(width=0), showlegend=False, hoverinfo="skip"
            ))
            fig4.add_trace(go.Scatter(
                x=t_, y=s_, mode="lines", name=label,
                line=dict(color=color, width=2)
            ))
        except Exception:
            pass

    dark_layout(fig4, f"Kaplan-Meier S(t) — window={days_ahead}d, R≤{radius_km}km", height=360)
    fig4.update_layout(
        xaxis_title="Days since radon anomaly",
        yaxis_title="Survival probability S(t)",
        yaxis=dict(range=[0,1.05])
    )
    st.plotly_chart(fig4, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 — Hazard Forecast
# ═══════════════════════════════════════════════════════════════════════════
st.markdown(f'<div class="section-header">🚨 Section 5 — Earthquake Hazard Forecast (M≥{mag_threshold}, R≤{radius_km}km)</div>', unsafe_allow_html=True)

col5a, col5b = st.columns([2,1])

with col5a:
    fig5 = go.Figure()

    # Color-coded hazard bars
    for rl, color in RISK_COLOR.items():
        sub = haz_df[haz_df["risk_level"]==rl]
        if len(sub):
            fig5.add_trace(go.Bar(
                x=sub["forecast_date"], y=sub["hazard"],
                name=rl, marker_color=color, opacity=0.85,
                width=86400000*0.8  # bar width in ms
            ))

    fig5.add_hline(y=baseline_safe, line_dash="dot", line_color="#8b949e",
                   annotation_text=f"Baseline={baseline_safe:.4f}", annotation_font_color="#8b949e")
    fig5.add_hline(y=0.10, line_dash="dash", line_color="#f0883e",
                   annotation_text="HIGH (0.10)", annotation_font_color="#f0883e")
    fig5.add_hline(y=0.20, line_dash="dash", line_color="#ff7b72",
                   annotation_text="VERY HIGH (0.20)", annotation_font_color="#ff7b72")

    dark_layout(fig5, f"Hazard Forecast — Anchor: {anchor.strftime('%Y-%m-%d')}", height=320)
    fig5.update_layout(
        barmode="stack",
        xaxis_title="Forecast Date",
        yaxis_title="Daily Hazard Rate",
        bargap=0.05
    )
    st.plotly_chart(fig5, use_container_width=True)

with col5b:
    st.markdown("**📋 Forecast Report**")
    peak_haz = haz_df["hazard"].max()
    peak_d   = int(haz_df.loc[haz_df["hazard"].idxmax(),"day"]) if haz_df["hazard"].notna().any() else 0

    report_html = f"""
    <div style="background:#fff8f0;border:1px solid #d4b896;border-radius:10px;padding:14px;font-size:0.82rem;color:#2c2c2c;">
      <div style="margin-bottom:8px;"><span style="color:#7a5c3a;">Anchor date</span><br>
        <b style="color:#2c2c2c;">{anchor.strftime('%Y-%m-%d')}</b></div>
      <div style="margin-bottom:8px;"><span style="color:#7a5c3a;">Latest radon anomaly</span><br>
        <b style="color:#1a6fbf;">{latest_radon:.2f} pCi/L</b></div>
      <div style="margin-bottom:8px;"><span style="color:#7a5c3a;">Baseline hazard</span><br>
        <b style="color:#2c2c2c;">{baseline_safe:.5f}</b></div>
      <div style="margin-bottom:8px;"><span style="color:#7a5c3a;">Peak hazard</span><br>
        <b style="color:#c05000;">{peak_haz:.4f}</b> <span style="color:#2c2c2c;">on Day {peak_d}</span></div>
      <div style="margin-bottom:8px;"><span style="color:#7a5c3a;">High-risk days</span><br>
        <b style="color:#c0392b;">{n_high}</b> <span style="color:#2c2c2c;">/ {days_ahead} days</span></div>
    </div>
    """
    st.markdown(report_html, unsafe_allow_html=True)

    st.markdown("<br>**🗓️ Risk Calendar**", unsafe_allow_html=True)
    risk_rows = haz_df[haz_df["risk_level"].isin(["HIGH","VERY HIGH"])].copy()
    if len(risk_rows):
        badge_map = {
            "HIGH":      '<span class="badge-high">HIGH</span>',
            "VERY HIGH": '<span class="badge-veryhigh">VERY HIGH</span>',
        }
        table_html = '<div class="risk-table"><table><tr><th>Date</th><th>Day</th><th>Hazard</th><th>Level</th></tr>'
        for _, r in risk_rows.iterrows():
            table_html += (
                f"<tr><td>{r['forecast_date'].strftime('%m/%d')}</td>"
                f"<td>{int(r['day'])}</td>"
                f"<td>{r['hazard']:.4f}</td>"
                f"<td>{badge_map[r['risk_level']]}</td></tr>"
            )
        table_html += "</table></div>"
        st.markdown(table_html, unsafe_allow_html=True)
    else:
        st.markdown('<span class="badge-low">LOW — No high-risk days in window</span>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6 — Correlation Timeline
# ═══════════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-header">🔗 Section 6 — Radon–Earthquake Correlation Timeline</div>', unsafe_allow_html=True)

fig6 = make_subplots(specs=[[{"secondary_y": True}]])

# Radon (downsample)
step2 = max(1, len(radon)//1500)
radon_ds = radon.iloc[::step2]
fig6.add_trace(
    go.Scatter(x=radon_ds.index, y=radon_ds.values,
               mode="lines", name="Radon (pCi/L)",
               line=dict(color="#388bfd", width=1.2), opacity=0.8),
    secondary_y=False
)

# Anomaly markers
fig6.add_trace(
    go.Scatter(x=radon_anom.index, y=radon_anom.values,
               mode="markers", name="Radon Anomaly",
               marker=dict(color="#ff7b72", size=5, symbol="circle")),
    secondary_y=False
)

# EQ scatter
eq_plot = eq_near[eq_near["Magnitude"]>=mag_threshold].copy()
fig6.add_trace(
    go.Scatter(x=eq_plot["Time (Thailand)"], y=eq_plot["Magnitude"],
               mode="markers", name=f"EQ M≥{mag_threshold}",
               marker=dict(
                   color=eq_plot["Magnitude"],
                   colorscale=[[0,"#e3b341"],[1,"#ff7b72"]],
                   size=(eq_plot["Magnitude"]-2)*3+4,
                   opacity=0.75, symbol="triangle-up"
               )),
    secondary_y=True
)

dark_layout(fig6, "Radon Anomalies vs Earthquake Events (Dual Axis)", height=320)
fig6.update_yaxes(title_text="Radon (pCi/L)", secondary_y=False,
                  gridcolor=GRID_COLOR, color=FONT_COLOR)
fig6.update_yaxes(title_text="Earthquake Magnitude", secondary_y=True,
                  gridcolor=GRID_COLOR, color=FONT_COLOR)
fig6.update_xaxes(gridcolor=GRID_COLOR)
st.plotly_chart(fig6, use_container_width=True)

# Lag Correlation
st.markdown("**📉 Lag Correlation: Radon Anomaly → EQ events (lag 0–30 days)**")
radon_daily = radon.resample("D").max().fillna(0)
anom_flag   = (radon_daily > upper).astype(float)
eq_daily    = eq_near[eq_near["Magnitude"]>=mag_threshold].set_index("Time (Thailand)")["Magnitude"].resample("D").count().reindex(radon_daily.index, fill_value=0)

lags = list(range(0,31))
corrs = []
for lag in lags:
    if lag == 0:
        c = anom_flag.corr(eq_daily)
    else:
        c = anom_flag.shift(lag).dropna().corr(eq_daily.iloc[lag:])
    corrs.append(c if not np.isnan(c) else 0.0)

fig_lag = go.Figure(go.Bar(
    x=lags, y=corrs,
    marker=dict(
        color=corrs,
        colorscale=[[0,"#30363d"],[0.5,"#388bfd"],[1,"#ff7b72"]],
        cmin=-1, cmax=1
    )
))
fig_lag.add_hline(y=0, line_color="#8b949e", line_width=1)
dark_layout(fig_lag, f"Lag Correlation (Radon Anomaly → EQ M≥{mag_threshold})", height=220)
fig_lag.update_layout(xaxis_title="Lag (days)", yaxis_title="Pearson r",
                      yaxis=dict(range=[-0.3,0.3]))
st.plotly_chart(fig_lag, use_container_width=True)



# ═══════════════════════════════════════════════════════════════════════════
# SECTION 7 — Risk Prediction vs Actual Earthquake Verification
# ═══════════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-header">✅ Section 7 — เปรียบเทียบ Risk ที่ทำนาย vs แผ่นดินไหวจริง</div>', unsafe_allow_html=True)

# สร้างตารางเปรียบเทียบจากทุก anomaly
verify_rows = []
for anom_time in radon_anom.index:
    radon_val = float(radon_anom.loc[anom_time]) if not isinstance(radon_anom.loc[anom_time], pd.Series) else float(radon_anom.loc[anom_time].iloc[0])
    # คำนวณ hazard สำหรับ anomaly นี้
    T_v = dt_df[f"dt_M{key}"]
    E_v = dt_df[f"ev_M{key}"]
    haz_v = piecewise_hazard(T_v.values, E_v.values, max_day=days_ahead)
    peak_h = float(haz_v["hazard"].max()) if haz_v["hazard"].notna().any() else 0.0
    rl = risk_level(peak_h)

    # หา EQ จริงที่เกิดใน 30 วันหลัง anomaly
    t_end = anom_time + pd.Timedelta(days=days_ahead)
    # ใช้ mag_threshold จริงๆ ที่ผู้ใช้เลือก (ไม่ใช่ _snap)
    # และใช้ eq_near ที่ผ่าน radius filter แล้ว
    eq_after = eq_near[
        (eq_near["Time (Thailand)"] > anom_time) &
        (eq_near["Time (Thailand)"] <= t_end) &
        (eq_near["Magnitude"] >= mag_threshold)
    ]
    eq_count_v = len(eq_after)
    eq_max_mag = float(eq_after["Magnitude"].max()) if eq_count_v > 0 else 0.0
    eq_first = eq_after["Time (Thailand)"].min().strftime("%Y-%m-%d") if eq_count_v > 0 else "-"
    dt_days = int((eq_after["Time (Thailand)"].min() - anom_time).total_seconds() / 86400) if eq_count_v > 0 else None
    matched = "✅ ใช่" if eq_count_v > 0 else "❌ ไม่"
    # Region ของ EQ แรกที่เกิดขึ้น
    if eq_count_v > 0:
        first_eq_row = eq_after.loc[eq_after["Time (Thailand)"].idxmin()]
        region_col = next((c for c in eq_after.columns if "region" in c.lower() or "พื้นที่" in c.lower()), None)
        eq_region = str(first_eq_row[region_col]) if region_col else "-"
        # ตัดให้สั้นลงถ้ายาวเกิน
        eq_region = eq_region[:50] + "..." if len(eq_region) > 50 else eq_region
    else:
        eq_region = "-"

    verify_rows.append({
        "Anomaly Date": anom_time.strftime("%Y-%m-%d"),
        "Radon (pCi/L)": round(radon_val, 2),
        "Peak Hazard": round(peak_h, 4),
        "Risk Level": rl,
        "EQ จริงไหม": matched,
        "EQ แรก": eq_first,
        "Δt (วัน)": dt_days if dt_days is not None else "-",
        "Max Mag": round(eq_max_mag, 1) if eq_count_v > 0 else "-",
        "Region": eq_region,
        "จำนวน EQ": eq_count_v,
    })

verify_df = pd.DataFrame(verify_rows)
verify_df = verify_df.sort_values("Anomaly Date", ascending=False).reset_index(drop=True)

# ── กราฟ ──────────────────────────────────────────────────────────────────
st.markdown("**📊 กราฟ: Peak Hazard ที่ทำนาย vs แผ่นดินไหวจริง**")

fig7 = go.Figure()

# แยก matched / not matched
matched_df  = verify_df[verify_df["EQ จริงไหม"] == "✅ ใช่"]
no_match_df = verify_df[verify_df["EQ จริงไหม"] == "❌ ไม่"]

fig7.add_trace(go.Bar(
    x=matched_df["Anomaly Date"], y=matched_df["Peak Hazard"],
    name="✅ มี EQ จริง", marker_color="#2e8b57", opacity=0.85
))
fig7.add_trace(go.Bar(
    x=no_match_df["Anomaly Date"], y=no_match_df["Peak Hazard"],
    name="❌ ไม่มี EQ", marker_color="#c0392b", opacity=0.6
))

# scatter EQ จริง
eq_plot7 = eq_near[eq_near["Magnitude"] >= _snap].copy()
fig7.add_trace(go.Scatter(
    x=eq_plot7["Time (Thailand)"].dt.strftime("%Y-%m-%d"),
    y=[0.01] * len(eq_plot7),
    mode="markers", name=f"EQ จริง M≥{_snap}",
    marker=dict(color="#f1c40f", size=eq_plot7["Magnitude"]*2,
                symbol="triangle-up", opacity=0.8)
))

fig7.add_hline(y=0.10, line_dash="dash", line_color="#f0883e",
               annotation_text="HIGH (0.10)", annotation_font_color="#f0883e")
fig7.add_hline(y=0.20, line_dash="dash", line_color="#c0392b",
               annotation_text="VERY HIGH (0.20)", annotation_font_color="#c0392b")

dark_layout(fig7, f"Peak Hazard ที่ทำนาย vs EQ จริง M≥{_snap} (R≤{radius_km}km)", height=360)
fig7.update_layout(barmode="overlay", xaxis_title="Anomaly Date", yaxis_title="Peak Hazard Rate")
st.plotly_chart(fig7, use_container_width=True)

# ── Summary stats ──────────────────────────────────────────────────────────
n_total   = len(verify_df)
n_hit     = len(matched_df)
n_miss    = len(no_match_df)
hit_rate  = n_hit / n_total * 100 if n_total > 0 else 0

sc1, sc2, sc3, sc4 = st.columns(4)
sc1.metric("📊 Anomaly ทั้งหมด", f"{n_total:,}")
sc2.metric("✅ ทำนายถูก (มี EQ)", f"{n_hit:,}", f"{hit_rate:.1f}%")
sc3.metric("❌ ทำนายผิด (ไม่มี EQ)", f"{n_miss:,}", f"{100-hit_rate:.1f}%")
sc4.metric("🎯 Accuracy", f"{hit_rate:.1f}%")

# ── ตาราง ──────────────────────────────────────────────────────────────────
st.markdown("**📋 ตาราง: รายละเอียด Anomaly vs EQ จริง**")

# Color code Risk Level
def color_risk(val):
    colors = {"LOW":"#2e8b57","MODERATE":"#c8860a","HIGH":"#c05000","VERY HIGH":"#c0392b"}
    c = colors.get(val, "#2c2c2c")
    return f"color: {c}; font-weight: bold"

def color_match(val):
    if "✅" in str(val): return "color: #2e8b57; font-weight: bold"
    return "color: #c0392b; font-weight: bold"

try:
    styled = verify_df.style        .map(color_risk, subset=["Risk Level"])        .map(color_match, subset=["EQ จริงไหม"])        .format({"Peak Hazard": "{:.4f}", "Radon (pCi/L)": "{:.2f}"})        .set_properties(**{"background-color": "#fff8f0", "color": "#2c2c2c",
                           "border": "1px solid #d4b896"})
except AttributeError:
    styled = verify_df.style        .applymap(color_risk, subset=["Risk Level"])        .applymap(color_match, subset=["EQ จริงไหม"])        .format({"Peak Hazard": "{:.4f}", "Radon (pCi/L)": "{:.2f}"})        .set_properties(**{"background-color": "#fff8f0", "color": "#2c2c2c",
                           "border": "1px solid #d4b896"})

st.dataframe(styled, use_container_width=True, height=400)

# Download
st.download_button(
    "⬇️ Download Verification Table CSV",
    verify_df.to_csv(index=False),
    "risk_verification.csv", "text/csv"
)

# ─── Visit Counter (session + cumulative via file) ───────────────────────────
import os, json

COUNTER_FILE = "/tmp/visitor_count.json"

def load_total_visits():
    try:
        if os.path.exists(COUNTER_FILE):
            with open(COUNTER_FILE, "r") as f:
                return json.load(f).get("total", 0)
    except:
        pass
    return 0

def save_total_visits(n):
    try:
        with open(COUNTER_FILE, "w") as f:
            json.dump({"total": n}, f)
    except:
        pass

# นับ session ปัจจุบัน
if "visit_count" not in st.session_state:
    st.session_state.visit_count = 0
if "counted" not in st.session_state:
    st.session_state.visit_count += 1
    st.session_state.counted = True
    # บวกยอดสะสม
    _total = load_total_visits() + 1
    save_total_visits(_total)
    st.session_state.total_visits = _total
elif "total_visits" not in st.session_state:
    st.session_state.total_visits = load_total_visits()

# ─── Footer ──────────────────────────────────────────────────────────────────
st.markdown("---")
fcol1, fcol2, fcol3 = st.columns(3)
with fcol1:
    csv_radon = radon_anom.reset_index()
    csv_radon.columns = ["Timestamp","pCi/L"]
    st.download_button("⬇️ Download Anomalies CSV",
                       csv_radon.to_csv(index=False),
                       "radon_anomalies.csv", "text/csv")
with fcol2:
    st.download_button("⬇️ Download Forecast CSV",
                       haz_df.to_csv(index=False),
                       "hazard_forecast.csv", "text/csv")
with fcol3:
    st.markdown(f"<div style='color:#7a5c3a;font-size:0.75rem;padding-top:8px;'>"
                f"Data: {start_date.date()} → {end_date.date()}<br>"
                f"Anomalies: {len(radon_anom)} | EQ nearby: {len(eq_near)}</div>",
                unsafe_allow_html=True)

# ─── Copyright Bar (ล่างสุด) ─────────────────────────────────────────────────
st.markdown(f"""
<div style="background:#f5ead8;border:1px solid #d4b896;border-radius:10px;
            padding:16px 24px;margin-top:20px;
            display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
  <div style="font-size:0.85rem;color:#2c2c2c;min-width:200px;">
    👁️ <b>ผู้เข้าชม session นี้:</b>
    <span style="color:#a0522d;font-size:1.2rem;font-weight:700;margin-left:4px;">
      {st.session_state.visit_count:,}
    </span> ครั้ง<br>
    <span style="font-size:0.75rem;color:#7a5c3a;">
      🌐 ผู้เข้าชมสะสม:
      <b style="color:#a0522d;">{st.session_state.total_visits:,}</b> ครั้ง
    </span>
  </div>
  <div style="font-size:0.82rem;color:#7a5c3a;text-align:center;flex:1;">
    🔬 <b style="color:#2c2c2c;font-size:0.9rem;">iRES-MCM Radon & Earthquake Hazard Dashboard</b><br>
    สถานีตรวจวัดเรดอน มหาวิทยาลัยเทคโนโลยีราชมงคลอีสาน (RMUTI)<br>
    <span style="font-size:0.72rem;">Chiang Mai Station | Lat: 18.795°N, Lon: 98.953°E</span>
  </div>
  <div style="font-size:0.75rem;color:#7a5c3a;text-align:right;min-width:200px;">
    📅 เริ่มพัฒนา: <b>มกราคม 2568</b><br>
    🔄 อัปเดตล่าสุด: <b>{datetime.now().strftime('%d/%m/%Y %H:%M')}</b><br>
    © 2568 สงวนลิขสิทธิ์ ห้ามคัดลอกโดยไม่ได้รับอนุญาต
  </div>
</div>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 8 — Parameter Optimization (Grid Search)
# ═══════════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-header">🎯 Section 8 — Parameter Optimization (Grid Search)</div>', unsafe_allow_html=True)

st.markdown("""
<div style="background:#fff8f0;border:1px solid #d4b896;border-radius:10px;padding:12px 16px;margin-bottom:12px;font-size:0.82rem;color:#2c2c2c;">
  <b>🔬 หลักการ:</b> Grid Search ลองทุก combination ของ Magnitude / Radius / Window
  แล้วหาว่า combination ไหนให้ <b>Accuracy สูงสุด</b> สำหรับข้อมูลของสถานีนี้<br>
  อ้างอิง: Sensitivity Analysis in Radon Precursor Studies (Cicerone et al., 2009)
</div>
""", unsafe_allow_html=True)

# ── Parameter Grid ────────────────────────────────────────────────────────
g1, g2, g3 = st.columns(3)
with g1:
    grid_mags = st.multiselect(
        "⚡ Magnitude options",
        options=[1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
        default=[1.5, 2.0, 2.5, 3.0, 4.0]
    )
with g2:
    grid_radii = st.multiselect(
        "📡 Radius options (km)",
        options=[100, 200, 300, 500, 700, 1000],
        default=[200, 300, 500, 700, 1000]
    )
with g3:
    grid_windows = st.multiselect(
        "📆 Window options (days)",
        options=[7, 14, 21, 30, 45, 60],
        default=[7, 14, 21, 30]
    )

total_combinations = len(grid_mags) * len(grid_radii) * len(grid_windows)
st.markdown(f"**จำนวน combination ทั้งหมด: {total_combinations} แบบ**")

run_grid = st.button("🚀 รัน Grid Search", type="primary", disabled=total_combinations == 0)

if run_grid and total_combinations > 0:
    progress = st.progress(0, text="กำลังคำนวณ...")
    grid_results = []
    total = total_combinations
    done = 0

    # sample anomaly สำหรับ grid search (ใช้ 200 ล่าสุดเพื่อความเร็ว)
    grid_anom = radon_anom.iloc[-200:] if len(radon_anom) > 200 else radon_anom

    for mag in grid_mags:
        for rad in grid_radii:
            for win in grid_windows:
                # กรอง EQ ตาม radius นี้
                eq_grid = eq_sel[eq_sel["distance_km"] <= rad].copy()

                n_hit = 0
                n_total_g = 0
                for at in grid_anom.index:
                    t_end = at + pd.Timedelta(days=win)
                    eq_match = eq_grid[
                        (eq_grid["Time (Thailand)"] > at) &
                        (eq_grid["Time (Thailand)"] <= t_end) &
                        (eq_grid["Magnitude"] >= mag)
                    ]
                    n_total_g += 1
                    if len(eq_match) > 0:
                        n_hit += 1

                acc = n_hit / n_total_g * 100 if n_total_g > 0 else 0
                sens = n_hit / max(1, len(eq_grid[eq_grid["Magnitude"] >= mag])) * 100

                grid_results.append({
                    "Magnitude": mag,
                    "Radius (km)": rad,
                    "Window (days)": win,
                    "Hit": n_hit,
                    "Total": n_total_g,
                    "Accuracy (%)": round(acc, 1),
                    "Sensitivity (%)": round(min(sens, 100), 1),
                })

                done += 1
                progress.progress(done / total, text=f"คำนวณ {done}/{total} combinations...")

    progress.empty()
    grid_df = pd.DataFrame(grid_results).sort_values("Accuracy (%)", ascending=False).reset_index(drop=True)

    # ── Best Parameters ────────────────────────────────────────────────────
    best = grid_df.iloc[0]
    st.markdown(f"""
    <div style="background:#fff8f0;border:2px solid #a0522d;border-radius:12px;
                padding:16px 24px;margin:12px 0;text-align:center;">
      <div style="font-size:1rem;font-weight:700;color:#a0522d;margin-bottom:8px;">
        🏆 Best Parameters
      </div>
      <div style="font-size:0.9rem;color:#2c2c2c;">
        Magnitude ≥ <b style="color:#c05000;">{best['Magnitude']}</b> &nbsp;|&nbsp;
        Radius <b style="color:#c05000;">{best['Radius (km)']:.0f} km</b> &nbsp;|&nbsp;
        Window <b style="color:#c05000;">{best['Window (days)']:.0f} วัน</b>
      </div>
      <div style="font-size:1.3rem;font-weight:700;color:#2e8b57;margin-top:8px;">
        Accuracy: {best['Accuracy (%)']:.1f}%
      </div>
      <div style="font-size:0.75rem;color:#7a5c3a;margin-top:4px;">
        จาก {int(best['Hit'])} anomaly ที่ตรงกับ EQ จริง / {int(best['Total'])} anomaly ทั้งหมด
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Heatmap: Accuracy vs Mag x Radius (window ที่ดีที่สุด) ──────────────
    best_win = int(best["Window (days)"])
    heatmap_df = grid_df[grid_df["Window (days)"] == best_win].pivot_table(
        index="Magnitude", columns="Radius (km)", values="Accuracy (%)"
    )

    fig8a = go.Figure(go.Heatmap(
        z=heatmap_df.values,
        x=[f"{int(c)} km" for c in heatmap_df.columns],
        y=[f"M≥{r}" for r in heatmap_df.index],
        colorscale=[[0,"#fff8f0"],[0.5,"#f0883e"],[1,"#2e8b57"]],
        text=heatmap_df.values.round(1),
        texttemplate="%{text}%",
        showscale=True,
        colorbar=dict(title="Accuracy %")
    ))
    dark_layout(fig8a, f"Heatmap: Accuracy (%) — Window={best_win} วัน", height=350)
    fig8a.update_layout(
        paper_bgcolor="#fdf6ec",
        plot_bgcolor="#fdf6ec",
        xaxis_title="Radius (km)",
        yaxis_title="Magnitude Threshold"
    )
    st.plotly_chart(fig8a, use_container_width=True)

    # ── Line chart: Accuracy vs Window (best mag + radius) ───────────────────
    best_mag = best["Magnitude"]
    best_rad = best["Radius (km)"]
    line_df = grid_df[
        (grid_df["Magnitude"] == best_mag) &
        (grid_df["Radius (km)"] == best_rad)
    ].sort_values("Window (days)")

    fig8b = go.Figure()
    fig8b.add_trace(go.Scatter(
        x=line_df["Window (days)"], y=line_df["Accuracy (%)"],
        mode="lines+markers+text",
        text=line_df["Accuracy (%)"].apply(lambda x: f"{x:.1f}%"),
        textposition="top center",
        line=dict(color="#a0522d", width=2),
        marker=dict(size=10, color="#a0522d")
    ))
    dark_layout(fig8b, f"Accuracy vs Window Time (M≥{best_mag}, R={best_rad:.0f}km)", height=300)
    fig8b.update_layout(
        paper_bgcolor="#fdf6ec", plot_bgcolor="#fdf6ec",
        xaxis_title="Window (days)", yaxis_title="Accuracy (%)"
    )
    st.plotly_chart(fig8b, use_container_width=True)

    # ── Top 10 Table ──────────────────────────────────────────────────────────
    st.markdown("**📋 Top 10 Best Combinations**")
    st.dataframe(
        grid_df.head(10).style.background_gradient(
            subset=["Accuracy (%)"], cmap="RdYlGn"
        ).format({"Accuracy (%)": "{:.1f}%", "Sensitivity (%)": "{:.1f}%"}),
        use_container_width=True, height=380
    )

    # Download
    st.download_button(
        "⬇️ Download Grid Search Results CSV",
        grid_df.to_csv(index=False),
        "grid_search_results.csv", "text/csv"
    )

# ─── Real-time auto-refresh ───────────────────────────────────────────────────
if realtime:
    time.sleep(refresh_sec)
    st.rerun()
