"""
Radon & Earthquake Hazard Dashboard
Real-time — Google Sheets (Radon: ชีต1 + Radon_Validated) + Google Sheets (Earthquake)

ปรับปรุง (27/11/2026)
- อ่านข้อมูลเรดอนจากทั้งแท็บแรก (legacy) และแท็บ Radon_Validated แล้วรวมกัน
- แปลงปี พ.ศ. → ค.ศ. อัตโนมัติ, ใช้ Captured UTC → เวลาไทย สำหรับแท็บ Validated
- แสดง "ข้อมูลล่าสุดเมื่อไร / ผ่านมากี่ชั่วโมง" และเตือนเมื่อข้อมูลหยุดอัปเดต
- ใช้เวลาไทย (Asia/Bangkok) ทุกจุด แทนเวลาเซิร์ฟเวอร์ (UTC)
- แยกสถานะ Live / Mock อย่างถูกต้อง (เดิม mock ก็ขึ้น Live)
- เลือกช่วง Short Term (Days = 1 หรือ 7) ไม่ให้ค่าสองแบบปนกัน
- แก้ช่วงวันที่ให้รวมข้อมูลของวันสุดท้ายด้วย
"""

import os
import json
import time
import warnings
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from scipy.stats import mannwhitneyu
from lifelines import KaplanMeierFitter

warnings.filterwarnings("ignore")

# ─── Page Config ────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Radon & Earthquake Hazard Dashboard",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── CSS Theme ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
  /* Cream Theme */
  .stApp { background-color: #fdf6ec; color: #2c2c2c; }
  [data-testid="stSidebar"] { background-color: #f5ead8; border-right: 1px solid #d4b896; color: #2c2c2c !important; }
  [data-testid="stSidebar"] * { color: #2c2c2c !important; }
  [data-testid="stSidebar"] input { color: #2c2c2c !important; background-color: #fff8f0 !important; border: 1px solid #d4b896 !important; }
  /* Download buttons */
  [data-testid="stDownloadButton"] button { color: #fff8f0 !important; background-color: #a0522d !important; border: 1px solid #a0522d !important; }
  [data-testid="stDownloadButton"] button:hover { background-color: #8b4513 !important; }
  /* Metric cards */
  div[data-testid="stMetric"] { background:#fff8f0; border:1px solid #d4b896; border-radius:10px; padding:12px; }
  div[data-testid="stMetric"] label { color:#7a5c3a !important; font-size:0.72rem !important; }
  div[data-testid="stMetric"] [data-testid="stMetricValue"] { font-size:1.6rem !important; color:#2c2c2c !important; }
  .block-container { padding-top: 1rem; padding-bottom: 1rem; }

  /* Risk badges */
  .badge-low      { background:#1a3a2a; color:#3fb950; border:1px solid #3fb950; border-radius:6px; padding:2px 10px; font-size:0.75rem; font-weight:600; }
  .badge-moderate { background:#2d2a1a; color:#e3b341; border:1px solid #e3b341; border-radius:6px; padding:2px 10px; font-size:0.75rem; font-weight:600; }
  .badge-high     { background:#3a2a1a; color:#f0883e; border:1px solid #f0883e; border-radius:6px; padding:2px 10px; font-size:0.75rem; font-weight:600; }
  .badge-veryhigh { background:#3a1a1a; color:#ff7b72; border:1px solid #ff7b72; border-radius:6px; padding:2px 10px; font-size:0.75rem; font-weight:600; }

  /* Section headers */
  .section-header {
    font-size: 1rem; font-weight: 600; color: #1a6fbf;
    border-left: 3px solid #1a6fbf; padding-left: 10px;
    margin: 8px 0 12px 0;
  }

  /* Live indicator */
  .live-dot {
    display:inline-block; width:8px; height:8px;
    border-radius:50%; animation: pulse 1.5s infinite; margin-right:6px;
  }
  @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:0.3;} }

  /* Scrollable table */
  .risk-table { max-height:260px; overflow-y:auto; }
  .risk-table table { width:100%; border-collapse:collapse; font-size:0.8rem; }
  .risk-table th { background:#f5ead8; color:#7a5c3a; padding:6px 10px; text-align:left; position:sticky; top:0; }
  .risk-table td { padding:6px 10px; border-bottom:1px solid #e8d8c0; }
</style>
""", unsafe_allow_html=True)

# ─── Config ──────────────────────────────────────────────────────────────────
TZ_TH          = ZoneInfo("Asia/Bangkok")
SHEET_URL      = "https://docs.google.com/spreadsheets/d/1NtKV_9fanjFD-mm-qVkUWQ-LGIv_0hD4d5A8dSuwdOk/edit?gid=0#gid=0"
VALIDATED_TAB  = "Radon_Validated"
EQ_SHEET_ID    = "1ZXCX5H5YJzFdYyuIGZnpYmCBuKRbfz8rx9qT_ZXc8bc"
EQ_GID         = "2058199297"
RADON_COL      = "Short Term (pCi/L)"
CACHE_TTL      = 300    # cache 5 นาที
FRESH_OK_H     = 2      # ≤ 2 ชม. = ข้อมูลสด
FRESH_WARN_H   = 24     # ≤ 24 ชม. = ล่าช้า, เกินนี้ = หยุดอัปเดต
STATION        = (18.795, 98.953)


def now_th():
    """เวลาปัจจุบันตามเวลาไทย (naive) — เซิร์ฟเวอร์ Streamlit Cloud ใช้ UTC"""
    return datetime.now(TZ_TH).replace(tzinfo=None)


# ─── Google Sheets Auth (Streamlit Secrets) ───────────────────────────────────
@st.cache_resource
def get_gspread_client():
    """อ่าน Service Account จาก st.secrets["gcp_service_account"]"""
    import gspread
    from google.oauth2.service_account import Credentials

    info = dict(st.secrets["gcp_service_account"])
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


# ─── Helpers: parsing ─────────────────────────────────────────────────────────
def _values_to_df(values):
    """แปลงผล get_all_values() → DataFrame (แถวแรกเป็น header)"""
    if not values or len(values) < 2:
        return pd.DataFrame()
    header = [str(h).strip() for h in values[0]]
    width = len(header)
    rows = [r + [""] * (width - len(r)) if len(r) < width else r[:width] for r in values[1:]]
    return pd.DataFrame(rows, columns=header)


def _be_to_ce(s: pd.Series) -> pd.Series:
    """แปลงปี พ.ศ. ในสตริงวันที่ (dd/mm/yyyy หรือ yyyy-mm-dd) ให้เป็น ค.ศ."""
    s = s.astype(str).str.strip()

    def fix_dmy(m):
        y = int(m.group(3))
        return f"{m.group(1)}/{m.group(2)}/{y - 543 if y > 2400 else y}"

    def fix_ymd(m):
        y = int(m.group(1))
        return f"{y - 543 if y > 2400 else y}-{m.group(2)}-{m.group(3)}"

    s = s.str.replace(r"^(\d{1,2})/(\d{1,2})/(\d{4})", fix_dmy, regex=True)
    s = s.str.replace(r"^(\d{4})-(\d{1,2})-(\d{1,2})", fix_ymd, regex=True)
    return s


def parse_th_datetime(s: pd.Series) -> pd.Series:
    """แปลงสตริงเวลา (รองรับ พ.ศ./ค.ศ., dd/mm/yyyy และ yyyy-mm-dd) แบบ vectorized"""
    s = _be_to_ce(s)
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    for fmt in ["%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%d/%m/%Y", "%Y-%m-%d"]:
        miss = out.isna()
        if not miss.any():
            break
        out[miss] = pd.to_datetime(s[miss], format=fmt, errors="coerce")
    miss = out.isna()
    if miss.any():
        out[miss] = pd.to_datetime(s[miss], errors="coerce", dayfirst=True)
    return out


def _find_col(df, *keywords, exclude=()):
    for c in df.columns:
        lc = c.lower()
        if all(k in lc for k in keywords) and not any(x in lc for x in exclude):
            return c
    return None


# ─── Radon loaders ────────────────────────────────────────────────────────────
def _load_legacy_tab(sh):
    """แท็บแรก: Timestamp, Long Term, Short Term (pCi/L), Days, ..."""
    df = _values_to_df(sh.sheet1.get_all_values())
    if df.empty:
        return pd.DataFrame(columns=["ts", "value", "days"])

    tcol = "Timestamp" if "Timestamp" in df.columns else _find_col(df, "time") or _find_col(df, "date")
    vcol = RADON_COL if RADON_COL in df.columns else _find_col(df, "short")
    dcol = "Days" if "Days" in df.columns else _find_col(df, "day")
    if tcol is None or vcol is None:
        raise ValueError(f"แท็บแรก: ไม่พบคอลัมน์เวลา/Short Term columns={list(df.columns)}")

    return pd.DataFrame({
        "ts":    parse_th_datetime(df[tcol]),
        "value": pd.to_numeric(df[vcol], errors="coerce"),
        "days":  pd.to_numeric(df[dcol], errors="coerce") if dcol else np.nan,
    })


def _load_validated_tab(sh):
    """แท็บ Radon_Validated: Record ID, Revision, Captured UTC, Captured Thailand, Days, Short (pCi/L), ..."""
    df = _values_to_df(sh.worksheet(VALIDATED_TAB).get_all_values())
    if df.empty:
        return pd.DataFrame(columns=["ts", "value", "days"])

    # เก็บ revision ล่าสุดของแต่ละ Record ID
    if "Record ID" in df.columns and "Revision" in df.columns:
        df["_rev"] = pd.to_numeric(df["Revision"], errors="coerce").fillna(0)
        df = df.sort_values("_rev").drop_duplicates("Record ID", keep="last")

    # เวลา: ใช้ Captured UTC → เวลาไทย, ถ้าไม่มีใช้ Captured Thailand (พ.ศ.)
    ts = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    if "Captured UTC" in df.columns:
        utc = pd.to_datetime(df["Captured UTC"], errors="coerce", utc=True)
        ts = utc.dt.tz_convert(TZ_TH).dt.tz_localize(None)
    if "Captured Thailand" in df.columns:
        miss = ts.isna()
        if miss.any():
            ts[miss] = parse_th_datetime(df.loc[miss, "Captured Thailand"])

    vcol = "Short (pCi/L)" if "Short (pCi/L)" in df.columns else _find_col(df, "short", "pci")
    if vcol is None:
        raise ValueError(f"{VALIDATED_TAB}: ไม่พบคอลัมน์ Short (pCi/L) columns={list(df.columns)}")
    dcol = "Days" if "Days" in df.columns else None

    return pd.DataFrame({
        "ts":    ts.values,
        "value": pd.to_numeric(df[vcol], errors="coerce").values,
        "days":  pd.to_numeric(df[dcol], errors="coerce").values if dcol else np.nan,
    })


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_radon_raw():
    """
    คืน (raw_df, info)
    raw_df: ts, value, days, source  (ยังไม่กรอง Days)
    info:   is_mock, latest ต่อแท็บ, จำนวนแถว, ข้อผิดพลาด
    """
    info = {"is_mock": False, "tabs": {}, "errors": [], "fetched_at": now_th()}
    parts = []
    try:
        sh = get_gspread_client().open_by_url(SHEET_URL)
    except Exception as e:
        info["errors"].append(f"เชื่อม Google Sheets ไม่ได้: {e}")
        sh = None

    if sh is not None:
        for name, loader in [("ชีต1 (legacy)", _load_legacy_tab),
                             (VALIDATED_TAB, _load_validated_tab)]:
            try:
                d = loader(sh).dropna(subset=["ts"])
                d["source"] = name
                parts.append(d)
                info["tabs"][name] = {
                    "rows": int(len(d)),
                    "latest": d["ts"].max() if len(d) else None,
                }
            except Exception as e:
                info["errors"].append(f"{name}: {e}")

    raw = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if raw.empty or raw["value"].notna().sum() == 0:
        info["is_mock"] = True
        raw = _mock_radon_raw()
    return raw, info


def prepare_radon(raw: pd.DataFrame, days_choice):
    """กรอง Days, ค่าผิดปกติ, ลบเวลาซ้ำ → pd.Series index=Timestamp"""
    df = raw.copy()
    if days_choice is not None and "days" in df.columns:
        # แถวเก่าที่ไม่มีค่า Days ให้คงไว้
        df = df[(df["days"] == days_choice) | df["days"].isna()]
    df = df[(df["value"] > 0) & (df["value"] <= 100)]
    # Validated มาทีหลังใน concat → keep="last" ให้ Validated ชนะเมื่อเวลาซ้ำ
    order = {"ชีต1 (legacy)": 0, VALIDATED_TAB: 1, "mock": 0}
    df["_o"] = df["source"].map(order).fillna(0)
    df = df.sort_values(["ts", "_o"]).drop_duplicates("ts", keep="last")
    s = pd.Series(df["value"].values, index=pd.DatetimeIndex(df["ts"]), name=RADON_COL)
    return s.sort_index()


# ─── Earthquake loader ────────────────────────────────────────────────────────
def _parse_coord(x, pos, neg):
    s = str(x).strip()
    try:
        if s.endswith(pos):
            return float(s.replace(pos, ""))
        if s.endswith(neg):
            return -float(s.replace(neg, ""))
        return float(s)
    except Exception:
        return np.nan


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_eq_data():
    """คืน (eq_df, info)"""
    info = {"is_mock": False, "error": None}
    try:
        sh = get_gspread_client().open_by_key(EQ_SHEET_ID)
        ws = next((w for w in sh.worksheets() if str(w.id) == EQ_GID), None) or sh.get_worksheet(0)
        eq = _values_to_df(ws.get_all_values())
        if eq.empty:
            raise ValueError("ไม่มีข้อมูลใน sheet")

        time_col = next((c for c in eq.columns if "time" in c.lower() or "วัน" in c.lower()), None)
        if time_col is None:
            raise ValueError(f"ไม่พบคอลัมน์เวลา columns={list(eq.columns)}")
        eq = eq.rename(columns={time_col: "Time (Thailand)"})
        eq["Time (Thailand)"] = parse_th_datetime(eq["Time (Thailand)"])
        eq["Magnitude"] = pd.to_numeric(eq["Magnitude"], errors="coerce")
        eq["Latitude"] = eq["Latitude"].apply(lambda x: _parse_coord(x, "°N", "°S"))
        eq["Longitude"] = eq["Longitude"].apply(lambda x: _parse_coord(x, "°E", "°W"))
        eq = (eq.dropna(subset=["Time (Thailand)", "Magnitude", "Latitude", "Longitude"])
                .sort_values("Time (Thailand)").reset_index(drop=True))
        if eq.empty:
            raise ValueError("ไม่มีแถวที่ใช้ได้หลังแปลงข้อมูล")
        return eq, info
    except Exception as e:
        info["is_mock"] = True
        info["error"] = str(e)
        return _mock_eq(), info


# ─── Fallback Mock Data ───────────────────────────────────────────────────────
def _mock_radon_raw(start="2025-01-23", end="2026-05-17"):
    rng = np.random.default_rng(42)
    idx = pd.date_range(start, end, freq="1h")
    base = 2.5 + np.sin(np.linspace(0, 6 * np.pi, len(idx))) * 0.8
    radon = base * rng.lognormal(0, 0.35, len(idx))
    anom = rng.choice(len(idx), 120, replace=False)
    radon[anom] *= rng.uniform(2.5, 5.0, 120)
    return pd.DataFrame({"ts": idx, "value": radon, "days": 1.0, "source": "mock"})


def _mock_eq(start="2025-01-23", end="2026-05-17"):
    rng = np.random.default_rng(7)
    n = 1800
    times = pd.date_range(start, end, periods=n) + pd.to_timedelta(rng.integers(0, 86400, n), unit="s")
    mags = np.clip(rng.exponential(1.0, n) + 2.5, 2.5, 7.5)
    return pd.DataFrame({"Time (Thailand)": times, "Magnitude": mags,
                         "Latitude": rng.uniform(5, 30, n),
                         "Longitude": rng.uniform(88, 110, n)})


# ─── Analysis functions ───────────────────────────────────────────────────────
def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0088
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return R * 2 * np.arcsin(np.sqrt(a))


def compute_rolling_iqr_anomaly(radon, window_days=30):
    """
    Rolling IQR Anomaly Detection (Sliding Window, non-stationary time series)
    คำนวณ Q1/Q3/IQR จากค่ามัธยฐานรายวันย้อนหลัง window_days วัน (รวมวันนั้น)
    แล้วใช้ขอบบนของวันนั้นตัดสินข้อมูลรายชั่วโมงในวันเดียวกัน
    """
    empty = pd.Series(dtype=float)
    if radon.empty:
        return np.nan, np.nan, np.nan, np.nan, empty, empty

    daily = radon.resample("D").median()
    q1 = daily.rolling(window_days, min_periods=1).quantile(0.25)
    q3 = daily.rolling(window_days, min_periods=1).quantile(0.75)
    upper_daily = q3 + 1.5 * (q3 - q1)
    upper_daily.iloc[:window_days - 1] = np.nan   # ต้องมีข้อมูลครบหน้าต่างก่อน

    upper_series = pd.Series(
        upper_daily.reindex(radon.index.normalize()).values, index=radon.index)
    anomalies = radon[radon > upper_series].dropna()

    Q1, Q3 = radon.quantile(0.25), radon.quantile(0.75)
    IQR = Q3 - Q1
    return Q1, Q3, IQR, Q3 + 1.5 * IQR, anomalies, upper_series


def time_to_first_eq(t0, eq_times, eq_mags, days_ahead=30, min_mag=4.0):
    t1 = t0 + pd.Timedelta(days=days_ahead)
    m = (eq_times > t0) & (eq_times <= t1) & (eq_mags >= min_mag)
    if not m.any():
        return float(days_ahead), 0
    return float((eq_times[m].min() - t0).total_seconds() / 86400), 1


def piecewise_hazard(T, E, max_day=30):
    T, E = np.asarray(T, float), np.asarray(E, int)
    rows = []
    for d in range(1, max_day + 1):
        at_risk = int(np.sum(T >= (d - 1)))
        events = int(np.sum((E == 1) & (T > (d - 1)) & (T <= d)))
        rows.append({"day": d, "at_risk": at_risk, "events": events,
                     "hazard": events / at_risk if at_risk > 0 else np.nan})
    return pd.DataFrame(rows)


def risk_level(h):
    if pd.isna(h):
        return "LOW"
    if h >= 0.20:
        return "VERY HIGH"
    if h >= 0.10:
        return "HIGH"
    if h >= 0.05:
        return "MODERATE"
    return "LOW"


def freshness(latest, ref):
    """คืน (ชั่วโมงที่ผ่านมา, ป้ายสถานะ, สี)"""
    if latest is None or pd.isna(latest):
        return None, "⚪ ไม่มีข้อมูล", "#7a5c3a"
    age_h = (ref - pd.Timestamp(latest)).total_seconds() / 3600
    if age_h <= FRESH_OK_H:
        return age_h, "🟢 ข้อมูลสด", "#2e8b57"
    if age_h <= FRESH_WARN_H:
        return age_h, "🟡 ข้อมูลล่าช้า", "#c8860a"
    return age_h, "🔴 ข้อมูลหยุดอัปเดต", "#c0392b"


def fmt_age(h):
    if h is None:
        return "-"
    if h < 1:
        return f"{h * 60:.0f} นาที"
    if h < 48:
        return f"{h:.1f} ชั่วโมง"
    return f"{h / 24:.1f} วัน"


RISK_COLOR = {"LOW": "#3fb950", "MODERATE": "#e3b341", "HIGH": "#f0883e", "VERY HIGH": "#ff7b72"}
PLOT_BG = "#fdf6ec"
GRID_COLOR = "#e8d8c0"
LINE_COLOR = "#b89b78"
FONT_COLOR = "#2c2c2c"


def cream_layout(fig, title="", height=380):
    fig.update_layout(
        title=dict(text=title, font=dict(color=FONT_COLOR, size=13), x=0.01),
        paper_bgcolor=PLOT_BG, plot_bgcolor=PLOT_BG,
        font=dict(color=FONT_COLOR, family="Inter, sans-serif"),
        height=height, margin=dict(l=50, r=20, t=40, b=40),
        legend=dict(bgcolor="rgba(0,0,0,0)", bordercolor=LINE_COLOR, borderwidth=1, font=dict(size=11)),
        xaxis=dict(gridcolor=GRID_COLOR, zerolinecolor=GRID_COLOR, showline=True, linecolor=LINE_COLOR),
        yaxis=dict(gridcolor=GRID_COLOR, zerolinecolor=GRID_COLOR, showline=True, linecolor=LINE_COLOR),
    )
    return fig


def card(value, label, color="#2c2c2c", sub=""):
    sub_html = f'<div style="font-size:0.65rem;color:#a08060;">{sub}</div>' if sub else ""
    return f"""
    <div style="background:#fff8f0;border:1px solid #d4b896;border-radius:8px;
                padding:10px;margin-bottom:8px;text-align:center;">
      <div style="font-size:1.4rem;font-weight:700;color:{color};">{value}</div>
      <div style="font-size:0.72rem;color:#7a5c3a;">{label}</div>{sub_html}
    </div>"""


# ─── Sidebar ─────────────────────────────────────────────────────────────────
NOW = now_th()
TODAY = NOW.date()

with st.sidebar:
    st.markdown("### 🔬 Dashboard Controls")
    st.markdown("---")

    _start_default = datetime(2026, 1, 23).date()
    date_range = st.date_input(
        "📅 Date Range",
        value=(_start_default, TODAY),
        min_value=datetime(2025, 1, 1).date(),
        max_value=TODAY,
    )
    if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
        start_date = pd.Timestamp(date_range[0])
        end_date = pd.Timestamp(date_range[1])
    else:
        start_date, end_date = pd.Timestamp(_start_default), pd.Timestamp(TODAY)
    # รวมข้อมูลทั้งวันสุดท้าย (เดิมตัดที่ 00:00 ทำให้ข้อมูลวันนี้หายไป)
    end_ts = end_date + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)

    days_label = st.selectbox(
        "🧪 Short Term period (คอลัมน์ Days)",
        ["1 วัน (Days = 1)", "7 วัน (Days = 7)", "ทั้งหมด (ไม่กรอง)"],
        index=0,
        help="เครื่องบันทึกค่า Short Term 2 แบบต่อเวลาเดียวกัน เลือกแบบเดียวเพื่อไม่ให้ค่าปนกัน",
    )
    days_choice = {"1 วัน (Days = 1)": 1, "7 วัน (Days = 7)": 7}.get(days_label)

    mag_threshold = st.slider("⚡ Magnitude Threshold (Forecast)", 1.0, 10.0, 4.0, step=0.5)
    radius_km = st.slider("📡 Radius (km)", 100, 1000, 1000, step=100)
    days_ahead = st.slider("📆 Forecast Window (days)", 7, 60, 30)

    st.markdown("---")
    realtime = st.toggle("🔴 Auto-refresh", value=True)
    refresh_sec = st.slider("Refresh every (sec)", 30, 600, 60, step=30, disabled=not realtime,
                            help=f"ข้อมูลถูก cache ไว้ {CACHE_TTL // 60} นาที — รีเฟรชถี่กว่านี้จะไม่ได้ข้อมูลใหม่")
    if st.button("🔄 ดึงข้อมูลใหม่ทันที (ล้าง cache)"):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.markdown("**📍 Station Info**")
    st.markdown(f"Lat: **{STATION[0]}°N** &nbsp; Lon: **{STATION[1]}°E**")
    st.markdown("Location: Chiang Mai, TH")

# ─── Load data ───────────────────────────────────────────────────────────────
with st.spinner("🔄 กำลังดึงข้อมูลจาก Google Sheets..."):
    radon_raw, radon_info = load_radon_raw()
    eq_full, eq_info = load_eq_data()

radon_full = prepare_radon(radon_raw, days_choice)
latest_radon_ts = radon_full.index.max() if len(radon_full) else None
latest_eq_ts = eq_full["Time (Thailand)"].max() if len(eq_full) else None
age_h, fresh_label, fresh_color = freshness(latest_radon_ts, NOW)
eq_age_h, eq_fresh_label, _ = freshness(latest_eq_ts, NOW)

# ── สถานะข้อมูลใน sidebar ──
with st.sidebar:
    st.markdown("---")
    st.markdown("**📡 สถานะข้อมูล**")
    if radon_info["is_mock"]:
        st.markdown("Radon: 🟡 **Mock Data** (เชื่อม Sheet ไม่ได้)")
    else:
        st.markdown(f"Radon: {fresh_label}")
        st.markdown(f"ล่าสุด **{latest_radon_ts:%d/%m/%Y %H:%M}** (ผ่านมา {fmt_age(age_h)})")
        for tab, t in radon_info["tabs"].items():
            lt = f"{t['latest']:%d/%m/%Y %H:%M}" if t["latest"] is not None else "-"
            st.markdown(f"<small>• {tab}: {t['rows']:,} แถว | ล่าสุด {lt}</small>", unsafe_allow_html=True)
    if eq_info["is_mock"]:
        st.markdown("EQ: 🟡 **Mock Data**")
    else:
        st.markdown(f"EQ: {eq_fresh_label} — ล่าสุด {latest_eq_ts:%d/%m/%Y %H:%M}")
    st.markdown(f"<small>Radon {len(radon_full):,} แถว | EQ {len(eq_full):,} แถว<br>"
                f"ดึงข้อมูลเมื่อ {radon_info['fetched_at']:%H:%M:%S}</small>", unsafe_allow_html=True)

# ── คำเตือนหลัก ──
if radon_info["is_mock"]:
    st.error("⚠️ ไม่สามารถดึงข้อมูลเรดอนจริงได้ — กราฟทั้งหมดด้านล่างเป็น **ข้อมูลจำลอง (Mock)**  \n"
             + "  \n".join(radon_info["errors"]))
elif age_h is not None and age_h > FRESH_WARN_H:
    st.error(f"⚠️ ข้อมูลเรดอนไม่ได้อัปเดตมา **{fmt_age(age_h)}** "
             f"(ค่าล่าสุด {latest_radon_ts:%d/%m/%Y %H:%M} น.) — โปรดตรวจสอบเครื่องวัด/สคริปต์ส่งข้อมูล")
elif radon_info["errors"]:
    st.warning("⚠️ อ่านบางแท็บไม่สำเร็จ: " + " | ".join(radon_info["errors"]))
if eq_info["is_mock"]:
    st.warning(f"⚠️ ไม่สามารถดึงข้อมูลแผ่นดินไหวได้ ({eq_info['error']}) → ใช้ Mock Data แทน")

# ─── Filter ──────────────────────────────────────────────────────────────────
radon = radon_full.loc[start_date:end_ts].copy()
eq_sel = eq_full[(eq_full["Time (Thailand)"] >= start_date) &
                 (eq_full["Time (Thailand)"] <= end_ts)].copy()
eq_sel["distance_km"] = haversine_km(STATION[0], STATION[1],
                                     eq_sel["Latitude"].values, eq_sel["Longitude"].values)
eq_near = eq_sel[eq_sel["distance_km"] <= radius_km].copy()

if radon.empty:
    st.info("ไม่มีข้อมูลเรดอนในช่วงวันที่ที่เลือก — ลองขยายช่วงวันที่ใน sidebar")
    st.stop()

Q1, Q3, IQR, upper, radon_anom, upper_rolling = compute_rolling_iqr_anomaly(radon, window_days=30)

# ─── Survival data (1,000 anomaly ล่าสุด) ─────────────────────────────────────
sample_anom = radon_anom.iloc[-1000:]
min_mag_list = [3.0, 4.0, 5.0]
eq_times = eq_near["Time (Thailand)"]
eq_mags = eq_near["Magnitude"]
rows = []
for t, v in sample_anom.items():
    row = {"Anomaly_Time": t, "pCi/L": float(v)}
    for m in min_mag_list:
        k = str(m).replace(".", "")
        row[f"dt_M{k}"], row[f"ev_M{k}"] = time_to_first_eq(t, eq_times, eq_mags, days_ahead, m)
    rows.append(row)
if rows:
    dt_df = pd.DataFrame(rows).set_index("Anomaly_Time")
else:
    dt_df = pd.DataFrame(columns=["pCi/L", "dt_M30", "ev_M30", "dt_M40", "ev_M40", "dt_M50", "ev_M50"])

T3, E3 = dt_df["dt_M30"], dt_df["ev_M30"]
T4, E4 = dt_df["dt_M40"], dt_df["ev_M40"]
T5, E5 = dt_df["dt_M50"], dt_df["ev_M50"]
dt_3_4 = dt_df.loc[(E3 == 1) & (E4 == 0), "dt_M30"].dropna()
dt_4_5 = dt_df.loc[(E4 == 1) & (E5 == 0), "dt_M40"].dropna()
dt_5up = dt_df.loc[E5 == 1, "dt_M50"].dropna()

_snap = min(min_mag_list, key=lambda x: abs(x - mag_threshold))
key = str(_snap).replace(".", "")
haz_df = piecewise_hazard(dt_df[f"dt_M{key}"].values, dt_df[f"ev_M{key}"].values, max_day=days_ahead)
tail = haz_df.loc[haz_df["day"] >= max(1, int(days_ahead * 0.7)), "hazard"].dropna()
baseline_safe = max(float(tail.mean()) if len(tail) else 0.01, 1e-6)
haz_df["risk_level"] = haz_df["hazard"].apply(risk_level)
anchor = radon_anom.index[-1] if len(radon_anom) else end_date
haz_df["forecast_date"] = [anchor + timedelta(days=int(d) - 1) for d in haz_df["day"]]
has_haz = haz_df["hazard"].notna().any()
peak_haz = float(haz_df["hazard"].max()) if has_haz else 0.0
peak_day = int(haz_df.loc[haz_df["hazard"].idxmax(), "day"]) if has_haz else 0
n_high = int(haz_df["risk_level"].isin(["HIGH", "VERY HIGH"]).sum())

# ─── Header ──────────────────────────────────────────────────────────────────
if radon_info["is_mock"]:
    live_txt, live_col = "MOCK", "#c8860a"
else:
    live_txt = {"🟢": "LIVE", "🟡": "DELAYED", "🔴": "STALE"}.get(fresh_label[0], "LIVE")
    live_col = fresh_color
latest_txt = f"{latest_radon_ts:%d/%m/%Y %H:%M}" if latest_radon_ts is not None else "-"

st.markdown(f"""
<div style="background:#fff8f0;border:1px solid #d4b896;border-radius:12px;padding:14px 24px;
            margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
  <div>
    <span style="font-size:1.4rem;font-weight:700;color:#1a6fbf;">🔬 Radon & Earthquake Hazard Dashboard</span>
    <span style="margin-left:14px;font-size:0.78rem;color:#7a5c3a;">
      Station: Chiang Mai ({STATION[0]}°N, {STATION[1]}°E) &nbsp;|&nbsp; Radius: {radius_km} km
    </span>
  </div>
  <div style="text-align:right;font-size:0.78rem;color:#7a5c3a;">
    <span class="live-dot" style="background:{live_col};"></span>
    <b style="color:{live_col};">{live_txt}</b> &nbsp;ข้อมูลล่าสุด {latest_txt}
    (ผ่านมา {fmt_age(age_h)})<br>เวลาขณะนี้ {NOW:%d/%m/%Y %H:%M:%S}
  </div>
</div>
""", unsafe_allow_html=True)

# ─── KPI Row ─────────────────────────────────────────────────────────────────
c1, c2, c3, c4, c5 = st.columns(5)
latest_anom_val = float(radon_anom.iloc[-1]) if len(radon_anom) else 0.0
eq_count = int((eq_near["Magnitude"] >= mag_threshold).sum())
c1.metric("🌫️ Latest Radon", f"{radon.iloc[-1]:.2f} pCi/L", f"{radon.index[-1]:%d/%m %H:%M}", delta_color="off")
c2.metric("⚠️ Total Anomalies", f"{len(radon_anom):,}", f"Global IQR upper: {upper:.2f}", delta_color="off")
c3.metric("🌍 EQ Events Nearby", f"{eq_count}", f"M≥{mag_threshold}, R≤{radius_km}km", delta_color="off")
c4.metric("📈 Peak Hazard Day", f"Day {peak_day}", f"Max: {peak_haz:.3f}", delta_color="off")
c5.metric("🚨 High Risk Days", f"{n_high} days", f"in next {days_ahead} days", delta_color="off")
st.markdown("<br>", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — Radon Time Series
# ═══════════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-header">📊 Section 1 — Radon Time Series & IQR Anomaly Detection</div>',
            unsafe_allow_html=True)

step = max(1, len(radon) // 2000)
radon_plot = radon.iloc[::step]
fig1 = go.Figure()
fig1.add_trace(go.Scatter(x=radon_plot.index, y=radon_plot.values, mode="lines", name="Radon (pCi/L)",
                          line=dict(color="#388bfd", width=1.2), opacity=0.85))
fig1.add_trace(go.Scatter(x=radon_anom.index, y=radon_anom.values, mode="markers", name="Anomaly",
                          marker=dict(color="#ff7b72", size=6)))
upper_r = upper_rolling.dropna()
if len(upper_r):
    step_r = max(1, len(upper_r) // 500)
    fig1.add_trace(go.Scatter(x=upper_r.iloc[::step_r].index, y=upper_r.iloc[::step_r].values,
                              mode="lines", name="Rolling IQR Upper (30d)",
                              line=dict(color="#e3b341", width=1.2, dash="dot"), opacity=0.8))
fig1.add_hline(y=float(upper), line_dash="dash", line_color="#f0883e",
               annotation_text=f"Global IQR Upper = {upper:.2f}", annotation_font_color="#f0883e")
fig1.add_hline(y=float(Q3), line_dash="dot", line_color="#3fb950",
               annotation_text=f"Q3={Q3:.2f}", annotation_font_color="#3fb950")
if latest_radon_ts is not None and start_date <= latest_radon_ts <= end_ts:
    fig1.add_vline(x=latest_radon_ts, line_dash="dot", line_color="#7a5c3a")
cream_layout(fig1, f"Radon Short Term ({days_label}) — {len(radon_anom)} anomalies detected", height=300)
st.plotly_chart(fig1, width="stretch")

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — Earthquake Map
# ═══════════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-header">🗺️ Section 2 — Earthquake Map</div>', unsafe_allow_html=True)
col_map, col_eq_stats = st.columns([3, 1])

with col_map:
    eq_map = eq_near.copy()
    eq_map["mag_size"] = ((eq_map["Magnitude"] - 2) * 5).clip(3, 25)
    eq_map["label"] = (
        "M" + eq_map["Magnitude"].round(1).astype(str) + " | "
        + eq_map["Time (Thailand)"].dt.strftime("%Y-%m-%d")
        + "<br>Dist: " + eq_map["distance_km"].round(0).astype(int).astype(str) + " km"
    )
    fig_map = go.Figure()
    fig_map.add_trace(go.Scattergeo(
        lat=eq_map["Latitude"], lon=eq_map["Longitude"], mode="markers",
        marker=dict(size=eq_map["mag_size"], color=eq_map["Magnitude"],
                    colorscale=[[0, "#3fb950"], [0.4, "#e3b341"], [0.7, "#f0883e"], [1, "#ff7b72"]],
                    colorbar=dict(title="Mag", thickness=10, len=0.6), opacity=0.75, line=dict(width=0)),
        text=eq_map["label"], hoverinfo="text", name="Earthquakes"))
    fig_map.add_trace(go.Scattergeo(
        lat=[STATION[0]], lon=[STATION[1]], mode="markers+text",
        marker=dict(size=14, color="#c0392b", symbol="star"),
        text=["📡 Station"], textposition="top center", textfont=dict(color="#c0392b", size=11),
        name="Radon Station", hoverinfo="name"))
    fig_map.update_geos(
        center=dict(lat=STATION[0], lon=STATION[1]), projection_scale=4,
        showland=True, landcolor="#f5ead8", showocean=True, oceancolor="#dbe9f4",
        showcoastlines=True, coastlinecolor=LINE_COLOR, showframe=False,
        showcountries=True, countrycolor=LINE_COLOR, bgcolor=PLOT_BG)
    fig_map.update_layout(
        paper_bgcolor=PLOT_BG, font=dict(color=FONT_COLOR), height=380,
        margin=dict(l=0, r=0, t=30, b=0), legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=10)),
        title=dict(text=f"EQ locations within {radius_km} km (N={len(eq_near)})",
                   font=dict(color=FONT_COLOR, size=12), x=0.01))
    st.plotly_chart(fig_map, width="stretch")

with col_eq_stats:
    st.markdown("**📊 EQ Stats**")
    for m, color in [(3.0, "#2e8b57"), (4.0, "#c8860a"), (5.0, "#c05000"), (6.0, "#c0392b")]:
        st.markdown(card(int((eq_near["Magnitude"] >= m).sum()), f"M ≥ {m}", color), unsafe_allow_html=True)
    max_mag = eq_near["Magnitude"].max() if len(eq_near) else 0
    st.markdown(card(f"{max_mag:.1f}", "Max Magnitude", "#c0392b"), unsafe_allow_html=True)

    # สัดส่วน EQ (M≥threshold) ที่มี anomaly เกิดก่อนภายใน 30 วัน
    eq_big = eq_near[eq_near["Magnitude"] >= mag_threshold]
    if len(eq_big) and len(radon_anom):
        anom_idx = radon_anom.index.values
        pos = np.searchsorted(anom_idx, eq_big["Time (Thailand)"].values, side="left")
        prev_anom = np.where(pos > 0, anom_idx[np.maximum(pos - 1, 0)], np.datetime64("NaT"))
        gap = (eq_big["Time (Thailand)"].values - prev_anom) / np.timedelta64(1, "D")
        detect_pct = float(np.mean((pos > 0) & (gap <= 30)) * 100)
    else:
        detect_pct = 0.0
    detect_color = "#2e8b57" if detect_pct >= 60 else "#c8860a" if detect_pct >= 30 else "#c0392b"
    st.markdown(card(f"{detect_pct:.1f}%", "🎯 จับสัญญาณ EQ ได้", detect_color,
                     f"EQ M≥{mag_threshold} ที่มี Anomaly ก่อนหน้า ≤30 วัน"), unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 & 4 — Precursor + Survival
# ═══════════════════════════════════════════════════════════════════════════
col3, col4 = st.columns(2)

with col3:
    st.markdown('<div class="section-header">📦 Section 3 — Precursor Time (Δt) Analysis</div>',
                unsafe_allow_html=True)
    groups_data = {k: v for k, v in {"3≤M<4": dt_3_4.values, "4≤M<5": dt_4_5.values,
                                     "M≥5": dt_5up.values}.items() if len(v) > 0}
    if groups_data:
        colors = {"3≤M<4": "#3fb950", "4≤M<5": "#e3b341", "M≥5": "#ff7b72"}
        fig3 = go.Figure()
        for gname, gdata in groups_data.items():
            fig3.add_trace(go.Box(y=gdata, name=gname, marker_color=colors[gname], boxmean=True,
                                  jitter=0.4, pointpos=-1.8, marker=dict(size=4, opacity=0.5)))
        if len(dt_4_5) > 1 and len(dt_5up) > 1:
            _, p = mannwhitneyu(dt_4_5, dt_5up, alternative="two-sided")
            annot = f"M-W U test (M4 vs M5): p={p:.4f} {'✓ sig' if p < 0.05 else '✗ n.s.'}"
        else:
            annot = "Not enough data for M-W U test"
        cream_layout(fig3, "Precursor Δt by Magnitude Group", height=360)
        fig3.update_layout(yaxis_title="Days before EQ (Δt)",
                           annotations=[dict(text=annot, xref="paper", yref="paper", x=0.01, y=1.05,
                                             showarrow=False, font=dict(size=10, color="#7a5c3a"))])
        st.plotly_chart(fig3, width="stretch")
    else:
        st.info("Insufficient event data for precursor analysis.")

with col4:
    st.markdown('<div class="section-header">📈 Section 4 — Kaplan-Meier Survival Curves</div>',
                unsafe_allow_html=True)
    fig4 = go.Figure()
    for T, E, label, color in [(T3, E3, "M≥3", "#3fb950"), (T4, E4, "M≥4", "#e3b341"), (T5, E5, "M≥5", "#ff7b72")]:
        if len(E) == 0 or E.sum() == 0:
            continue
        try:
            kmf = KaplanMeierFitter().fit(T.astype(float), event_observed=E.astype(int), label=label)
            t_ = kmf.survival_function_.index
            s_ = kmf.survival_function_[label].values
            lo, hi = kmf.confidence_interval_.iloc[:, 0].values, kmf.confidence_interval_.iloc[:, 1].values
            r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
            fig4.add_trace(go.Scatter(x=list(t_) + list(t_[::-1]), y=list(hi) + list(lo[::-1]),
                                      fill="toself", fillcolor=f"rgba({r},{g},{b},0.15)",
                                      line=dict(width=0), showlegend=False, hoverinfo="skip"))
            fig4.add_trace(go.Scatter(x=t_, y=s_, mode="lines", name=label, line=dict(color=color, width=2)))
        except Exception:
            pass
    cream_layout(fig4, f"Kaplan-Meier S(t) — window={days_ahead}d, R≤{radius_km}km", height=360)
    fig4.update_layout(xaxis_title="Days since radon anomaly", yaxis_title="Survival probability S(t)",
                       yaxis=dict(range=[0, 1.05]))
    st.plotly_chart(fig4, width="stretch")

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 — Hazard Forecast
# ═══════════════════════════════════════════════════════════════════════════
st.markdown(f'<div class="section-header">🚨 Section 5 — Earthquake Hazard Forecast '
            f'(M≥{_snap}, R≤{radius_km}km)</div>', unsafe_allow_html=True)
col5a, col5b = st.columns([2, 1])

with col5a:
    fig5 = go.Figure()
    for rl, color in RISK_COLOR.items():
        sub = haz_df[haz_df["risk_level"] == rl]
        if len(sub):
            fig5.add_trace(go.Bar(x=sub["forecast_date"], y=sub["hazard"], name=rl,
                                  marker_color=color, opacity=0.85, width=86400000 * 0.8))
    fig5.add_hline(y=baseline_safe, line_dash="dot", line_color="#7a5c3a",
                   annotation_text=f"Baseline={baseline_safe:.4f}", annotation_font_color="#7a5c3a")
    fig5.add_hline(y=0.10, line_dash="dash", line_color="#f0883e",
                   annotation_text="HIGH (0.10)", annotation_font_color="#f0883e")
    fig5.add_hline(y=0.20, line_dash="dash", line_color="#ff7b72",
                   annotation_text="VERY HIGH (0.20)", annotation_font_color="#ff7b72")
    cream_layout(fig5, f"Hazard Forecast — Anchor: {anchor:%Y-%m-%d}", height=320)
    fig5.update_layout(barmode="stack", xaxis_title="Forecast Date", yaxis_title="Daily Hazard Rate", bargap=0.05)
    st.plotly_chart(fig5, width="stretch")

with col5b:
    st.markdown("**📋 Forecast Report**")
    st.markdown(f"""
    <div style="background:#fff8f0;border:1px solid #d4b896;border-radius:10px;padding:14px;font-size:0.82rem;color:#2c2c2c;">
      <div style="margin-bottom:8px;"><span style="color:#7a5c3a;">Anchor date (anomaly ล่าสุด)</span><br>
        <b>{anchor:%Y-%m-%d %H:%M}</b></div>
      <div style="margin-bottom:8px;"><span style="color:#7a5c3a;">Latest radon anomaly</span><br>
        <b style="color:#1a6fbf;">{latest_anom_val:.2f} pCi/L</b></div>
      <div style="margin-bottom:8px;"><span style="color:#7a5c3a;">Baseline hazard</span><br>
        <b>{baseline_safe:.5f}</b></div>
      <div style="margin-bottom:8px;"><span style="color:#7a5c3a;">Peak hazard</span><br>
        <b style="color:#c05000;">{peak_haz:.4f}</b> on Day {peak_day}</div>
      <div style="margin-bottom:8px;"><span style="color:#7a5c3a;">High-risk days</span><br>
        <b style="color:#c0392b;">{n_high}</b> / {days_ahead} days</div>
    </div>""", unsafe_allow_html=True)

    st.markdown("<br>**🗓️ Risk Calendar**", unsafe_allow_html=True)
    risk_rows = haz_df[haz_df["risk_level"].isin(["HIGH", "VERY HIGH"])]
    if len(risk_rows):
        badge = {"HIGH": '<span class="badge-high">HIGH</span>',
                 "VERY HIGH": '<span class="badge-veryhigh">VERY HIGH</span>'}
        html = '<div class="risk-table"><table><tr><th>Date</th><th>Day</th><th>Hazard</th><th>Level</th></tr>'
        for _, r in risk_rows.iterrows():
            html += (f"<tr><td>{r['forecast_date']:%m/%d}</td><td>{int(r['day'])}</td>"
                     f"<td>{r['hazard']:.4f}</td><td>{badge[r['risk_level']]}</td></tr>")
        st.markdown(html + "</table></div>", unsafe_allow_html=True)
    else:
        st.markdown('<span class="badge-low">LOW — No high-risk days in window</span>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6 — Correlation Timeline
# ═══════════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-header">🔗 Section 6 — Radon–Earthquake Correlation Timeline</div>',
            unsafe_allow_html=True)
fig6 = make_subplots(specs=[[{"secondary_y": True}]])
step2 = max(1, len(radon) // 1500)
radon_ds = radon.iloc[::step2]
fig6.add_trace(go.Scatter(x=radon_ds.index, y=radon_ds.values, mode="lines", name="Radon (pCi/L)",
                          line=dict(color="#388bfd", width=1.2), opacity=0.8), secondary_y=False)
fig6.add_trace(go.Scatter(x=radon_anom.index, y=radon_anom.values, mode="markers", name="Radon Anomaly",
                          marker=dict(color="#ff7b72", size=5)), secondary_y=False)
eq_plot = eq_near[eq_near["Magnitude"] >= mag_threshold]
fig6.add_trace(go.Scatter(
    x=eq_plot["Time (Thailand)"], y=eq_plot["Magnitude"], mode="markers", name=f"EQ M≥{mag_threshold}",
    marker=dict(color=eq_plot["Magnitude"], colorscale=[[0, "#e3b341"], [1, "#ff7b72"]],
                size=(eq_plot["Magnitude"] - 2) * 3 + 4, opacity=0.75, symbol="triangle-up")),
    secondary_y=True)
cream_layout(fig6, "Radon Anomalies vs Earthquake Events (Dual Axis)", height=320)
fig6.update_yaxes(title_text="Radon (pCi/L)", secondary_y=False, gridcolor=GRID_COLOR, color=FONT_COLOR)
fig6.update_yaxes(title_text="Earthquake Magnitude", secondary_y=True, gridcolor=GRID_COLOR, color=FONT_COLOR)
st.plotly_chart(fig6, width="stretch")

st.markdown("**📉 Lag Correlation: Radon Anomaly → EQ events (lag 0–30 days)**")
radon_daily = radon.resample("D").max()
anom_flag = (radon_daily > upper).astype(float)
eq_daily = (eq_plot.set_index("Time (Thailand)")["Magnitude"].resample("D").count()
            .reindex(radon_daily.index, fill_value=0)) if len(eq_plot) else pd.Series(0, index=radon_daily.index)
lags = list(range(31))
corrs = []
for lag in lags:
    c = anom_flag.shift(lag).corr(eq_daily)  # anomaly วันที่ t เทียบกับ EQ วันที่ t+lag
    corrs.append(0.0 if pd.isna(c) else float(c))
fig_lag = go.Figure(go.Bar(x=lags, y=corrs, marker=dict(
    color=corrs, colorscale=[[0, "#b89b78"], [0.5, "#388bfd"], [1, "#ff7b72"]], cmin=-1, cmax=1)))
fig_lag.add_hline(y=0, line_color="#7a5c3a", line_width=1)
cream_layout(fig_lag, f"Lag Correlation (Radon Anomaly → EQ M≥{mag_threshold})", height=220)
lim = max(0.3, max(abs(c) for c in corrs) * 1.1)
fig_lag.update_layout(xaxis_title="Lag (days)", yaxis_title="Pearson r", yaxis=dict(range=[-lim, lim]))
st.plotly_chart(fig_lag, width="stretch")

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 7 — Risk Prediction vs Actual Earthquake Verification
# ═══════════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-header">✅ Section 7 — เปรียบเทียบ Risk ที่ทำนาย vs แผ่นดินไหวจริง</div>',
            unsafe_allow_html=True)

# Peak hazard มาจากโมเดลรวมของทุก anomaly → ค่าเดียวกันทุกแถว คำนวณครั้งเดียว
peak_rl = risk_level(peak_haz)
region_col = next((c for c in eq_near.columns if "region" in c.lower() or "พื้นที่" in c.lower()), None)
eq_big = eq_near[eq_near["Magnitude"] >= mag_threshold].sort_values("Time (Thailand)")
big_t = eq_big["Time (Thailand)"].values

verify_rows = []
for anom_time, radon_val in radon_anom.items():
    t0 = np.datetime64(anom_time)
    lo_i = np.searchsorted(big_t, t0, side="right")
    hi_i = np.searchsorted(big_t, np.datetime64(anom_time + pd.Timedelta(days=days_ahead)), side="right")
    eq_after = eq_big.iloc[lo_i:hi_i]
    n = len(eq_after)
    if n:
        first = eq_after.iloc[0]
        region = str(first[region_col]) if region_col else "-"
        region = region[:50] + "..." if len(region) > 50 else region
    verify_rows.append({
        "Anomaly Time": anom_time.strftime("%Y-%m-%d %H:%M"),
        "Radon (pCi/L)": round(float(radon_val), 2),
        "Peak Hazard": round(peak_haz, 4),
        "Risk Level": peak_rl,
        "EQ จริงไหม": "✅ ใช่" if n else "❌ ไม่",
        "EQ แรก": first["Time (Thailand)"].strftime("%Y-%m-%d") if n else "-",
        "Δt (วัน)": int((first["Time (Thailand)"] - anom_time).total_seconds() // 86400) if n else "-",
        "Max Mag": round(float(eq_after["Magnitude"].max()), 1) if n else "-",
        "Region": region if n else "-",
        "จำนวน EQ": n,
    })

verify_df = pd.DataFrame(verify_rows, columns=[
    "Anomaly Time", "Radon (pCi/L)", "Peak Hazard", "Risk Level", "EQ จริงไหม",
    "EQ แรก", "Δt (วัน)", "Max Mag", "Region", "จำนวน EQ"])
verify_df = verify_df.sort_values("Anomaly Time", ascending=False).reset_index(drop=True)
for c in ["Δt (วัน)", "Max Mag"]:
    verify_df[c] = verify_df[c].astype(str)

st.markdown("**📊 กราฟ: Radon Anomaly ที่ตามด้วยแผ่นดินไหวจริง vs ไม่มี**")
matched_df = verify_df[verify_df["EQ จริงไหม"] == "✅ ใช่"]
no_match_df = verify_df[verify_df["EQ จริงไหม"] == "❌ ไม่"]
fig7 = go.Figure()
fig7.add_trace(go.Scatter(x=pd.to_datetime(matched_df["Anomaly Time"]), y=matched_df["Radon (pCi/L)"],
                          mode="markers", name="✅ มี EQ ตามมา", marker=dict(color="#2e8b57", size=7)))
fig7.add_trace(go.Scatter(x=pd.to_datetime(no_match_df["Anomaly Time"]), y=no_match_df["Radon (pCi/L)"],
                          mode="markers", name="❌ ไม่มี EQ", marker=dict(color="#c0392b", size=6, opacity=0.6)))
y_eq = float(radon_anom.min()) * 0.9 if len(radon_anom) else 0
fig7.add_trace(go.Scatter(x=eq_big["Time (Thailand)"], y=[y_eq] * len(eq_big), mode="markers",
                          name=f"EQ จริง M≥{mag_threshold}",
                          marker=dict(color="#f1c40f", size=eq_big["Magnitude"] * 2, symbol="triangle-up",
                                      opacity=0.8, line=dict(color="#7a5c3a", width=0.5))))
cream_layout(fig7, f"Radon Anomaly vs EQ จริง M≥{mag_threshold} ภายใน {days_ahead} วัน (R≤{radius_km}km)", height=360)
fig7.update_layout(xaxis_title="Time", yaxis_title="Radon (pCi/L)")
st.plotly_chart(fig7, width="stretch")

n_total, n_hit = len(verify_df), len(matched_df)
hit_rate = n_hit / n_total * 100 if n_total else 0
sc1, sc2, sc3 = st.columns(3)
sc1.metric("📊 Anomaly ทั้งหมด", f"{n_total:,}")
sc2.metric("✅ มี EQ ตามมา (Hit rate)", f"{n_hit:,}", f"{hit_rate:.1f}%", delta_color="off")
sc3.metric("❌ ไม่มี EQ (False alarm)", f"{n_total - n_hit:,}", f"{100 - hit_rate:.1f}%", delta_color="off")

st.markdown("**📋 ตาราง: รายละเอียด Anomaly vs EQ จริง**")


def color_risk(val):
    c = {"LOW": "#2e8b57", "MODERATE": "#c8860a", "HIGH": "#c05000", "VERY HIGH": "#c0392b"}.get(val, "#2c2c2c")
    return f"color: {c}; font-weight: bold"


def color_match(val):
    return "color: #2e8b57; font-weight: bold" if "✅" in str(val) else "color: #c0392b; font-weight: bold"


sty = verify_df.style
_map = sty.map if hasattr(sty, "map") else sty.applymap
styled = (_map(color_risk, subset=["Risk Level"]))
styled = (styled.map if hasattr(styled, "map") else styled.applymap)(color_match, subset=["EQ จริงไหม"])
styled = styled.format({"Peak Hazard": "{:.4f}", "Radon (pCi/L)": "{:.2f}"}).set_properties(
    **{"background-color": "#fff8f0", "color": "#2c2c2c", "border": "1px solid #d4b896"})
st.dataframe(styled, width="stretch", height=400)
st.download_button("⬇️ Download Verification Table CSV", verify_df.to_csv(index=False).encode("utf-8-sig"),
                   "risk_verification.csv", "text/csv")

# ═══════════════════════════════════════════════════════════════════════════
# SECTION 8 — Parameter Optimization (Grid Search)
# ═══════════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-header">🎯 Section 8 — Parameter Optimization (Grid Search)</div>',
            unsafe_allow_html=True)
st.markdown("""
<div style="background:#fff8f0;border:1px solid #d4b896;border-radius:10px;padding:12px 16px;margin-bottom:12px;font-size:0.82rem;color:#2c2c2c;">
  <b>🔬 หลักการ:</b> Grid Search ลองทุก combination ของ Magnitude / Radius / Window
  แล้วหาว่า combination ไหนให้ <b>Hit rate สูงสุด</b> สำหรับข้อมูลของสถานีนี้<br>
  อ้างอิง: Sensitivity Analysis in Radon Precursor Studies (Cicerone et al., 2009)
</div>
""", unsafe_allow_html=True)

g1, g2, g3 = st.columns(3)
with g1:
    grid_mags = st.multiselect("⚡ Magnitude options", [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
                               default=[1.5, 2.0, 2.5, 3.0, 4.0])
with g2:
    grid_radii = st.multiselect("📡 Radius options (km)", [100, 200, 300, 500, 700, 1000],
                                default=[200, 300, 500, 700, 1000])
with g3:
    grid_windows = st.multiselect("📆 Window options (days)", [7, 14, 21, 30, 45, 60], default=[7, 14, 21, 30])

total_combinations = len(grid_mags) * len(grid_radii) * len(grid_windows)
st.markdown(f"**จำนวน combination ทั้งหมด: {total_combinations} แบบ**")

if st.button("🚀 รัน Grid Search", type="primary", disabled=total_combinations == 0):
    progress = st.progress(0, text="กำลังคำนวณ...")
    grid_results, done = [], 0
    grid_anom = radon_anom.iloc[-200:]
    a_t = grid_anom.index.values

    for mag in grid_mags:
        for rad in grid_radii:
            eq_g = eq_sel[(eq_sel["distance_km"] <= rad) & (eq_sel["Magnitude"] >= mag)].sort_values("Time (Thailand)")
            g_t = eq_g["Time (Thailand)"].values
            for win in grid_windows:
                lo_i = np.searchsorted(g_t, a_t, side="right")
                hi_i = np.searchsorted(g_t, a_t + np.timedelta64(win, "D"), side="right")
                n_hit_g = int(np.sum(hi_i > lo_i))
                n_tot_g = len(a_t)
                # EQ ที่มี anomaly ก่อนหน้าภายใน win วัน
                if len(g_t) and n_tot_g:
                    p = np.searchsorted(a_t, g_t, side="left")
                    prev = a_t[np.maximum(p - 1, 0)]
                    covered = int(np.sum((p > 0) & ((g_t - prev) / np.timedelta64(1, "D") <= win)))
                    sens = covered / len(g_t) * 100
                else:
                    sens = 0.0
                grid_results.append({
                    "Magnitude": mag, "Radius (km)": rad, "Window (days)": win,
                    "Hit": n_hit_g, "Total": n_tot_g,
                    "Accuracy (%)": round(n_hit_g / n_tot_g * 100 if n_tot_g else 0, 1),
                    "Sensitivity (%)": round(sens, 1),
                })
                done += 1
                progress.progress(done / total_combinations, text=f"คำนวณ {done}/{total_combinations} combinations...")
    progress.empty()

    grid_df = pd.DataFrame(grid_results).sort_values("Accuracy (%)", ascending=False).reset_index(drop=True)
    best = grid_df.iloc[0]
    st.markdown(f"""
    <div style="background:#fff8f0;border:2px solid #a0522d;border-radius:12px;padding:16px 24px;margin:12px 0;text-align:center;">
      <div style="font-size:1rem;font-weight:700;color:#a0522d;margin-bottom:8px;">🏆 Best Parameters</div>
      <div style="font-size:0.9rem;color:#2c2c2c;">
        Magnitude ≥ <b style="color:#c05000;">{best['Magnitude']}</b> &nbsp;|&nbsp;
        Radius <b style="color:#c05000;">{best['Radius (km)']:.0f} km</b> &nbsp;|&nbsp;
        Window <b style="color:#c05000;">{best['Window (days)']:.0f} วัน</b>
      </div>
      <div style="font-size:1.3rem;font-weight:700;color:#2e8b57;margin-top:8px;">Hit rate: {best['Accuracy (%)']:.1f}%</div>
      <div style="font-size:0.75rem;color:#7a5c3a;margin-top:4px;">
        จาก {int(best['Hit'])} anomaly ที่ตามด้วย EQ จริง / {int(best['Total'])} anomaly ทั้งหมด
        &nbsp;|&nbsp; Sensitivity {best['Sensitivity (%)']:.1f}%
      </div>
    </div>""", unsafe_allow_html=True)

    best_win = int(best["Window (days)"])
    heat = grid_df[grid_df["Window (days)"] == best_win].pivot_table(
        index="Magnitude", columns="Radius (km)", values="Accuracy (%)")
    fig8a = go.Figure(go.Heatmap(
        z=heat.values, x=[f"{int(c)} km" for c in heat.columns], y=[f"M≥{r}" for r in heat.index],
        colorscale=[[0, "#fff8f0"], [0.5, "#f0883e"], [1, "#2e8b57"]],
        text=heat.values.round(1), texttemplate="%{text}%", colorbar=dict(title="Hit %")))
    cream_layout(fig8a, f"Heatmap: Hit rate (%) — Window={best_win} วัน", height=350)
    fig8a.update_layout(xaxis_title="Radius (km)", yaxis_title="Magnitude Threshold")
    st.plotly_chart(fig8a, width="stretch")

    line_df = grid_df[(grid_df["Magnitude"] == best["Magnitude"]) &
                      (grid_df["Radius (km)"] == best["Radius (km)"])].sort_values("Window (days)")
    fig8b = go.Figure(go.Scatter(
        x=line_df["Window (days)"], y=line_df["Accuracy (%)"], mode="lines+markers+text",
        text=line_df["Accuracy (%)"].map(lambda x: f"{x:.1f}%"), textposition="top center",
        line=dict(color="#a0522d", width=2), marker=dict(size=10, color="#a0522d")))
    cream_layout(fig8b, f"Hit rate vs Window (M≥{best['Magnitude']}, R={best['Radius (km)']:.0f}km)", height=300)
    fig8b.update_layout(xaxis_title="Window (days)", yaxis_title="Hit rate (%)")
    st.plotly_chart(fig8b, width="stretch")

    st.markdown("**📋 Top 10 Best Combinations**")
    st.dataframe(grid_df.head(10).style.background_gradient(subset=["Accuracy (%)"], cmap="RdYlGn")
                 .format({"Accuracy (%)": "{:.1f}%", "Sensitivity (%)": "{:.1f}%"}),
                 width="stretch", height=380)
    st.download_button("⬇️ Download Grid Search Results CSV", grid_df.to_csv(index=False).encode("utf-8-sig"),
                       "grid_search_results.csv", "text/csv")

# ─── Visit Counter ───────────────────────────────────────────────────────────
COUNTER_FILE = "/tmp/visitor_count.json"


def load_total_visits():
    try:
        if os.path.exists(COUNTER_FILE):
            with open(COUNTER_FILE) as f:
                return json.load(f).get("total", 0)
    except Exception:
        pass
    return 0


def save_total_visits(n):
    try:
        with open(COUNTER_FILE, "w") as f:
            json.dump({"total": n}, f)
    except Exception:
        pass


if "counted" not in st.session_state:
    st.session_state.counted = True
    st.session_state.visit_count = 1
    st.session_state.total_visits = load_total_visits() + 1
    save_total_visits(st.session_state.total_visits)

# ─── Footer ──────────────────────────────────────────────────────────────────
st.markdown("---")
fcol1, fcol2, fcol3 = st.columns(3)
with fcol1:
    csv_radon = radon_anom.rename("pCi/L").rename_axis("Timestamp").reset_index()
    st.download_button("⬇️ Download Anomalies CSV", csv_radon.to_csv(index=False).encode("utf-8-sig"),
                       "radon_anomalies.csv", "text/csv")
with fcol2:
    st.download_button("⬇️ Download Forecast CSV", haz_df.to_csv(index=False).encode("utf-8-sig"),
                       "hazard_forecast.csv", "text/csv")
with fcol3:
    st.markdown(f"<div style='color:#7a5c3a;font-size:0.75rem;padding-top:8px;'>"
                f"Data: {start_date.date()} → {end_date.date()}<br>"
                f"Anomalies: {len(radon_anom)} | EQ nearby: {len(eq_near)}</div>", unsafe_allow_html=True)

# ─── Copyright Bar (ล่างสุด) ─────────────────────────────────────────────────
st.markdown(f"""
<div style="background:#f5ead8;border:1px solid #d4b896;border-radius:10px;padding:16px 24px;margin-top:20px;
            display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
  <div style="font-size:0.85rem;color:#2c2c2c;min-width:200px;">
    🌐 <b>ผู้เข้าชมสะสม:</b>
    <span style="color:#a0522d;font-size:1.2rem;font-weight:700;margin-left:4px;">{st.session_state.total_visits:,}</span> ครั้ง
  </div>
  <div style="font-size:0.82rem;color:#7a5c3a;text-align:center;flex:1;">
    🔬 <b style="color:#2c2c2c;font-size:0.9rem;">iRES-MCM Radon & Earthquake Hazard Dashboard</b><br>
    สถานีตรวจวัดเรดอน มหาวิทยาลัยเทคโนโลยีราชมงคลอีสาน (RMUTI)<br>
    <span style="font-size:0.72rem;">Chiang Mai Station | Lat: {STATION[0]}°N, Lon: {STATION[1]}°E</span>
  </div>
  <div style="font-size:0.75rem;color:#7a5c3a;text-align:right;min-width:200px;">
    📅 เริ่มพัฒนา: <b>มกราคม 2568</b><br>
    📡 ข้อมูลเรดอนล่าสุด: <b>{latest_txt}</b><br>
    🔄 หน้าเว็บโหลดเมื่อ: <b>{NOW:%d/%m/%Y %H:%M}</b><br>
    © 2568 สงวนลิขสิทธิ์ ห้ามคัดลอกโดยไม่ได้รับอนุญาต
  </div>
</div>
""", unsafe_allow_html=True)

# ─── Auto-refresh ────────────────────────────────────────────────────────────
if realtime:
    time.sleep(refresh_sec)
    st.rerun()
