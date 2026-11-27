"""
Radon & Earthquake Hazard Dashboard — Station 1 (ST001)
Real-time — Google Sheets (Radon_001: RADON_A เรดอนในดิน / RADON_B เรดอนในอากาศ) + Google Sheets (Earthquake)

โครงสร้างเหมือน dashboard สถานีเชียงใหม่ทุกส่วน (Section 1–8) โดยเปลี่ยน
- แหล่งข้อมูลเป็นชีต Radon_001 (บันทึกทุก 5 นาที, หน่วย Bq/m³)
- เลือกช่องวัดใน sidebar: RADON_A (ดิน) หรือ RADON_B (อากาศ) → ทุก Section วิเคราะห์ตามช่องที่เลือก
- รวมข้อมูลเป็นค่ามัธยฐานรายชั่วโมงก่อนวิเคราะห์ (เซนเซอร์ค้างค่าเดิมหลายรอบ 5 นาที)
- เพิ่มกราฟเปรียบเทียบเรดอนในดิน vs ในอากาศ

UI (27/11/2026): ธีมมืดแบบห้องควบคุม แบ่ง 5 แท็บ มีแถบสถานะ "ความเสี่ยงวันนี้" ด้านบน
เลิกใช้กราฟสองแกน (dual axis) — แยกเป็นกราฟซ้อนที่ใช้แกนเวลาเดียวกันแทน
- พิกัดสถานีอ่านจากคอลัมน์ location ในชีตอัตโนมัติ
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
    page_title="ST001 — Radon & Earthquake Hazard Dashboard",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Config ──────────────────────────────────────────────────────────────────
TZ_TH          = ZoneInfo("Asia/Bangkok")
SHEET_URL      = "https://docs.google.com/spreadsheets/d/1KMv3CdTMxTaEeyIEFpbtE_WZ1zcN2szGaj9NEv0ICxs/edit?gid=0#gid=0"
SHEET_GID      = "0"
EQ_SHEET_ID    = "1ZXCX5H5YJzFdYyuIGZnpYmCBuKRbfz8rx9qT_ZXc8bc"
EQ_GID         = "2058199297"
UNIT           = "Bq/m³"
CHANNELS       = {
    "RADON_A": {"label": "RADON_A — เรดอนในดิน (Soil)", "short": "Soil (RADON_A)", "key": "radon_a", "color": "#a0522d"},
    "RADON_B": {"label": "RADON_B — เรดอนในอากาศ (Air)", "short": "Air (RADON_B)", "key": "radon_b", "color": "#388bfd"},
}
CACHE_TTL      = 300    # cache 5 นาที
FRESH_OK_H     = 2      # ≤ 2 ชม. = ข้อมูลสด
FRESH_WARN_H   = 24     # ≤ 24 ชม. = ล่าช้า, เกินนี้ = หยุดอัปเดต
STATION_DEFAULT = (20.119528, 99.817413)   # จากคอลัมน์ location ในชีต
STATION_NAME   = "Station 1 (ST001)"
DATA_START     = datetime(2025, 1, 31).date()


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
def _col_letter(i):
    """index (0-based) → ตัวอักษรคอลัมน์ A, B, ..., AA"""
    out, n = "", i + 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _norm(h):
    return str(h).strip().lower().replace(" ", "").replace("-", "_")


def _to_num(vals):
    return pd.to_numeric(pd.Series(vals, dtype=object).astype(str).str.replace(",", "").str.strip(),
                         errors="coerce")


def parse_station_sheet(header, cols):
    """
    header: แถวแรกของชีต, cols: dict ชื่อ → list ค่า (ไม่รวม header)
    คืน (raw_5min_df[ts, RADON_A, RADON_B], (lat, lon) หรือ None)
    """
    n = max((len(v) for v in cols.values()), default=0)
    pad = {k: list(v) + [""] * (n - len(v)) for k, v in cols.items()}
    df = pd.DataFrame({
        "ts": parse_th_datetime(pd.Series(pad["ts"], dtype=object)),
        "RADON_A": _to_num(pad["RADON_A"]),
        "RADON_B": _to_num(pad["RADON_B"]),
    })
    df = df.dropna(subset=["ts"]).sort_values("ts").drop_duplicates("ts", keep="last")

    station = None
    if "lat" in pad and "lon" in pad:
        lat, lon = _to_num(pad["lat"]), _to_num(pad["lon"])
        ok = lat.between(5, 21) & lon.between(97, 106)
        if ok.sum() > 0:
            station = (round(float(lat[ok].median()), 6), round(float(lon[ok].median()), 6))
    return df.reset_index(drop=True), station


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_radon_raw():
    """
    คืน (hourly_df, info)
    hourly_df: index=ชั่วโมง, คอลัมน์ RADON_A, RADON_B (ค่ามัธยฐานรายชั่วโมง)
    info:      is_mock, latest (เวลาจริงล่าสุด 5 นาที), จำนวนแถว, ข้อผิดพลาด, พิกัดสถานี
    """
    info = {"is_mock": False, "tabs": {}, "errors": [], "fetched_at": now_th(),
            "station": None, "latest_raw": None}
    raw = pd.DataFrame()
    try:
        sh = get_gspread_client().open_by_url(SHEET_URL)
        ws = next((w for w in sh.worksheets() if str(w.id) == SHEET_GID), None) or sh.get_worksheet(0)
        header = ws.row_values(1)
        hn = [_norm(h) for h in header]

        def find(pred, what):
            i = next((k for k, h in enumerate(hn) if pred(h)), None)
            if i is None:
                raise ValueError(f"ไม่พบคอลัมน์ {what} columns={header}")
            return i

        ti = find(lambda h: "date" in h or "time" in h or "timestamp" in h, "Date Time")
        ai = find(lambda h: h.startswith("radon_a"), "Radon_A")
        bi = find(lambda h: h.startswith("radon_b"), "Radon_B")
        li = next((k for k, h in enumerate(hn) if h.startswith("location")), None)

        # ดึงเฉพาะคอลัมน์ที่ใช้ (ชีตมีหลายหมื่นแถว ดึงทั้งชีตจะช้า)
        keys = ["ts", "RADON_A", "RADON_B"]
        idxs = [ti, ai, bi]
        if li is not None:   # location เก็บเป็น lat, lon สองช่องติดกัน
            keys += ["lat", "lon"]
            idxs += [li, li + 1]
        ranges = [f"{_col_letter(i)}2:{_col_letter(i)}" for i in idxs]
        res = ws.batch_get(ranges)
        cols = {k: [r[0] if r else "" for r in vr] for k, vr in zip(keys, res)}

        raw, station = parse_station_sheet(header, cols)
        info["station"] = station
        info["latest_raw"] = raw["ts"].max() if len(raw) else None
        info["tabs"][ws.title] = {"rows": int(len(raw)), "latest": info["latest_raw"]}
    except Exception as e:
        info["errors"].append(f"อ่านชีต Radon_001 ไม่ได้: {e}")

    if raw.empty or raw[["RADON_A", "RADON_B"]].notna().sum().sum() == 0:
        info["is_mock"] = True
        raw = _mock_radon_raw()
        info["latest_raw"] = raw["ts"].max()

    for c in ["RADON_A", "RADON_B"]:
        raw.loc[~(raw[c] > 0), c] = np.nan          # 0 / ติดลบ = เซนเซอร์ยังไม่พร้อม
    hourly = raw.set_index("ts")[["RADON_A", "RADON_B"]].resample("1h").median()
    hourly = hourly.dropna(how="all")
    return hourly, info


def prepare_radon(hourly: pd.DataFrame, channel: str):
    """เลือกช่องวัด → pd.Series index=ชั่วโมง"""
    s = hourly[channel].dropna()
    s.name = channel
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
def _mock_radon_raw(start="2025-01-31", end=None):
    end = end or now_th().strftime("%Y-%m-%d")
    rng = np.random.default_rng(42)
    idx = pd.date_range(start, end, freq="5min")
    wave = 1 + np.sin(np.linspace(0, 12 * np.pi, len(idx))) * 0.25
    soil = 5000 * wave * rng.lognormal(0, 0.25, len(idx))
    air = 150 * wave * rng.lognormal(0, 0.35, len(idx))
    anom = rng.choice(len(idx), 400, replace=False)
    soil[anom] *= rng.uniform(1.8, 3.0, 400)
    air[anom] *= rng.uniform(2.0, 4.0, 400)
    return pd.DataFrame({"ts": idx, "RADON_A": soil, "RADON_B": air})


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


# ─── Theme: Dark Control Room ─────────────────────────────────────────────────
# ทุกสีอยู่ที่นี่ที่เดียว (แก้ธีมได้จากตรงนี้)
C = {
    "page":    "#0b0b0b",   # พื้นหลังหน้า
    "panel":   "#141414",   # sidebar
    "surface": "#191919",   # การ์ด/พื้นกราฟ
    "raised":  "#222222",
    "border":  "rgba(255,255,255,0.10)",
    "ink":     "#efefef",   # ตัวอักษรหลัก
    "ink2":    "#9c9c9c",   # ตัวอักษรรอง / แกน / legend
    "grid":    "#303030",
    "axis":    "#474747",
    "accent":  "#338ced",
    # status (ใช้เฉพาะระดับความเสี่ยง/สถานะ ไม่ใช้เป็นสีเส้นข้อมูล)
    "good":    "#0aac23",
    "warning": "#fbb315",
    "serious": "#f18a59",
    "critical": "#dc3c3d",
    # สีของ "สิ่งที่วัด" (คงที่ตลอดทั้งหน้า)
    "soil":    "#e76624",   # RADON_A
    "air":     "#338ced",   # RADON_B
    "eq":      "#9887ef",   # แผ่นดินไหว
    "upper":   "#d28e05",   # เส้นขอบบน IQR
}
CHANNELS["RADON_A"]["color"] = C["soil"]
CHANNELS["RADON_B"]["color"] = C["air"]

RISK_COLOR = {"LOW": C["good"], "MODERATE": C["warning"], "HIGH": C["serious"], "VERY HIGH": C["critical"]}
RISK_ICON = {"LOW": "●", "MODERATE": "▲", "HIGH": "▲▲", "VERY HIGH": "■"}
RISK_TH = {"LOW": "ต่ำ", "MODERATE": "ปานกลาง", "HIGH": "สูง", "VERY HIGH": "สูงมาก"}
MAG_COLOR = {3.0: "#9887ef", 4.0: "#e24d8b", 5.0: "#eb6263"}   # กลุ่มขนาดแผ่นดินไหว (categorical)

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Thai:wght@400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
html, body, .stApp, [class*="css"] {{ font-family: 'IBM Plex Sans Thai', system-ui, sans-serif; }}
.stApp {{ background: {C['page']}; color: {C['ink']}; }}
.block-container {{ padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1500px; }}
header[data-testid="stHeader"] {{ background: transparent; }}

/* Sidebar */
[data-testid="stSidebar"] {{ background: {C['panel']}; border-right: 1px solid {C['border']}; }}
[data-testid="stSidebar"] * {{ color: {C['ink']}; }}
[data-testid="stSidebar"] .side-h {{ font-size: .68rem; letter-spacing: .12em; text-transform: uppercase;
    color: {C['ink2']}; margin: 18px 0 6px; font-weight: 600; }}
[data-testid="stSidebar"] input {{ background: {C['surface']} !important; color: {C['ink']} !important; }}

/* Tabs (Streamlit รุ่นใหม่ใช้ data-testid="stTab", รุ่นเก่าใช้ data-baseweb="tab") */
.stTabs [role="tablist"] {{ gap: 6px; border-bottom: 1px solid {C['border']}; }}
.stTabs [role="tab"] {{ background: transparent; padding: 10px 16px; border-radius: 10px 10px 0 0; }}
.stTabs [role="tab"], .stTabs [role="tab"] p {{ color: #d0d0d0 !important; font-weight: 500; font-size: .92rem; }}
.stTabs [role="tab"]:hover {{ background: {C['raised']}; }}
.stTabs [role="tab"][aria-selected="true"] {{ background: {C['surface']}; }}
.stTabs [role="tab"][aria-selected="true"], .stTabs [role="tab"][aria-selected="true"] p {{ color: #ffffff !important; font-weight: 600; }}
.stTabs [role="tab"] span[role="img"] {{ color: {C['accent']} !important; font-size: 1.3rem; margin-right: 4px;
    vertical-align: -4px !important; }}
.stTabs .react-aria-SelectionIndicator, .stTabs [data-baseweb="tab-highlight"] {{ background-color: {C['accent']} !important; }}

/* Buttons */
.stButton button, [data-testid="stDownloadButton"] button {{
    background: {C['raised']}; color: {C['ink']}; border: 1px solid {C['border']}; border-radius: 8px; }}
.stButton button:hover, [data-testid="stDownloadButton"] button:hover {{ border-color: {C['accent']}; color: {C['ink']}; }}
.stButton button[kind="primary"] {{ background: {C['accent']}; border-color: {C['accent']}; color: #fff; }}

/* Dataframe & expander */
[data-testid="stExpander"] {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 12px; }}

/* Components */
.topbar {{ display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px;
    padding: 4px 2px 16px; border-bottom: 1px solid {C['border']}; margin-bottom: 16px; }}
.topbar .t1 {{ font-size: 1.35rem; font-weight: 700; letter-spacing: -.01em; }}
.topbar .t2 {{ font-size: .8rem; color: {C['ink2']}; margin-top: 2px; }}
.pill {{ display:inline-flex; align-items:center; gap:6px; padding: 4px 10px; border-radius: 999px;
    border: 1px solid {C['border']}; background: {C['surface']}; font-size: .75rem; color: {C['ink']}; margin-left: 6px; }}
.dot {{ width:8px; height:8px; border-radius:50%; display:inline-block; }}
.dot.live {{ animation: pulse 1.6s infinite; }}
@keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:.25; }} }}
.mono {{ font-family: 'JetBrains Mono', ui-monospace, monospace; }}

.tile {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 14px; padding: 14px 16px; height: 100%; }}
.tile .lab {{ font-size: .72rem; color: {C['ink2']}; letter-spacing: .04em; }}
.tile .val {{ font-family: 'JetBrains Mono', monospace; font-size: 1.55rem; font-weight: 600; color: {C['ink']}; margin-top: 4px; line-height: 1.15; }}
.tile .unit {{ font-size: .8rem; color: {C['ink2']}; font-weight: 400; margin-left: 4px; }}
.tile .sub {{ font-size: .72rem; color: {C['ink2']}; margin-top: 6px; }}

.hero {{ border-radius: 14px; padding: 16px 18px; height: 100%; border: 1px solid {C['border']}; }}
.hero .lab {{ font-size: .72rem; letter-spacing: .08em; text-transform: uppercase; opacity: .85; }}
.hero .val {{ font-size: 1.9rem; font-weight: 700; margin-top: 2px; line-height: 1.1; }}
.hero .sub {{ font-size: .75rem; margin-top: 6px; opacity: .85; }}

.panel-h {{ font-size: .95rem; font-weight: 600; color: {C['ink']}; margin: 6px 0 2px; }}
.panel-s {{ font-size: .78rem; color: {C['ink2']}; margin-bottom: 8px; }}
.note {{ background: {C['surface']}; border: 1px solid {C['border']}; border-left: 3px solid {C['accent']};
    border-radius: 10px; padding: 10px 14px; font-size: .8rem; color: {C['ink2']}; margin: 8px 0 12px; }}
.badge {{ display:inline-block; padding: 2px 8px; border-radius: 6px; font-size: .72rem; font-weight: 600; }}
.rtable {{ width:100%; border-collapse: collapse; font-size: .8rem; }}
.rtable th {{ text-align:left; color: {C['ink2']}; font-weight: 500; padding: 6px 8px; border-bottom: 1px solid {C['border']}; }}
.rtable td {{ padding: 7px 8px; border-bottom: 1px solid {C['border']}; }}
.scroll {{ max-height: 330px; overflow-y: auto; }}
.footer {{ border-top: 1px solid {C['border']}; margin-top: 28px; padding-top: 14px; display:flex;
    justify-content: space-between; flex-wrap: wrap; gap: 10px; font-size: .74rem; color: {C['ink2']}; }}
/* ปรับสี widget ของ Streamlit (ธีมพื้นฐานเป็นสีแดง) ให้เป็นโทนน้ำเงินของ dashboard */
[data-testid="stSlider"], [data-testid="stCheckbox"], [data-testid="stRadio"] [role="radiogroup"],
[data-testid="stProgress"], [data-testid="stMultiSelect"] [data-baseweb="tag"] {{ filter: hue-rotate(217deg) saturate(1.1); }}
[data-testid="stSlider"] [data-testid="stSliderThumbValue"] p {{ color: {C['ink']} !important; }}
[data-testid="stButtonGroup"] button {{ background: {C['surface']} !important; color: {C['ink2']} !important;
    border-color: {C['border']} !important; }}
[data-testid="stButtonGroup"] button[aria-checked="true"], [data-testid="stButtonGroup"] button[data-selected="true"],
[data-testid="stButtonGroup"] button[aria-pressed="true"] {{ background: {C['accent']} !important; color: #fff !important;
    border-color: {C['accent']} !important; }}
[data-testid="stButtonGroup"] button p {{ color: inherit !important; }}
[data-baseweb="select"] > div, [data-baseweb="input"] > div, [data-baseweb="input"] input {{
    background: {C['surface']} !important; color: {C['ink']} !important; border-color: {C['border']} !important; }}
[data-testid="stExpander"] summary, [data-testid="stExpander"] p, [data-testid="stWidgetLabel"] p {{ color: {C['ink']}; }}
.banner {{ border-radius: 12px; padding: 12px 16px; margin: 0 0 12px; font-size: .85rem; border: 1px solid; }}
.banner.err {{ background: rgba(220,60,61,.12); border-color: rgba(220,60,61,.45); color: #f3b4b4; }}
.banner.warn {{ background: rgba(251,179,21,.10); border-color: rgba(251,179,21,.40); color: #f6d58a; }}
.banner b {{ color: {C['ink']}; }}
/* ตาราง HTML: ลบเส้นขอบเริ่มต้นของ Streamlit */
.rtable, .rtable tr, .rtable td, .rtable th {{ border-left: none !important; border-right: none !important;
    border-top: none !important; background: transparent !important; }}
.rtable thead th, .rtable th {{ position: sticky; top: 0; background: {C['surface']} !important; }}
</style>
""", unsafe_allow_html=True)


def dark_layout(fig, title="", height=360, hover="x unified"):
    fig.update_layout(
        title=dict(text=title, font=dict(color=C["ink"], size=13), x=0.0, xanchor="left") if title else None,
        paper_bgcolor=C["surface"], plot_bgcolor=C["surface"],
        font=dict(color=C["ink2"], family="IBM Plex Sans Thai, sans-serif", size=11),
        height=height, margin=dict(l=56, r=18, t=44 if title else 16, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                    bgcolor="rgba(0,0,0,0)", font=dict(color=C["ink2"], size=11)),
        hovermode=hover,
        hoverlabel=dict(bgcolor=C["raised"], bordercolor=C["axis"], font=dict(color=C["ink"], size=12)),
    )
    fig.update_xaxes(gridcolor=C["grid"], zeroline=False, showline=True, linecolor=C["axis"],
                     tickfont=dict(color=C["ink2"]), title_font=dict(color=C["ink2"]))
    fig.update_yaxes(gridcolor=C["grid"], zeroline=False, showline=False,
                     tickfont=dict(color=C["ink2"]), title_font=dict(color=C["ink2"]))
    return fig


def tile(label, value, unit="", sub="", accent=None):
    bar = f"border-top:3px solid {accent};" if accent else ""
    u = f'<span class="unit">{unit}</span>' if unit else ""
    s = f'<div class="sub">{sub}</div>' if sub else ""
    return f'<div class="tile" style="{bar}"><div class="lab">{label}</div><div class="val">{value}{u}</div>{s}</div>'


def badge(level):
    col = RISK_COLOR.get(level, C["ink2"])
    ink = "#0b0b0b" if level == "MODERATE" else "#ffffff"
    return f'<span class="badge" style="background:{col};color:{ink};">{RISK_ICON.get(level, "")} {level}</span>'


def panel(title, sub=""):
    st.markdown(f'<div class="panel-h">{title}</div>' + (f'<div class="panel-s">{sub}</div>' if sub else ""),
                unsafe_allow_html=True)


def html_table(df, max_rows=300, height=360, fmt=None):
    """ตาราง HTML ธีมมืด (st.dataframe ใช้สีธีมสว่างของ Streamlit)"""
    fmt = fmt or {}
    d = df.head(max_rows)
    head = "".join(f"<th>{c}</th>" for c in d.columns)
    body = ""
    for _, r in d.iterrows():
        cells = ""
        for c in d.columns:
            v = r[c]
            if c in fmt:
                v = fmt[c](v)
            cells += f"<td>{v}</td>"
        body += f"<tr>{cells}</tr>"
    more = (f'<div style="font-size:.72rem;color:{C["ink2"]};margin-top:6px;">แสดง {len(d):,} จาก {len(df):,} แถว '
            f'— ดาวน์โหลด CSV เพื่อดูทั้งหมด</div>') if len(df) > max_rows else ""
    st.markdown(f'<div class="tile scroll" style="max-height:{height}px"><table class="rtable">'
                f'<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{more}', unsafe_allow_html=True)


PLOT_CFG = {"displaylogo": False, "modeBarButtonsToRemove": ["lasso2d", "select2d"]}


def show(fig):
    st.plotly_chart(fig, width="stretch", config=PLOT_CFG)


# ─── Sidebar ─────────────────────────────────────────────────────────────────
NOW = now_th()
TODAY = NOW.date()

with st.sidebar:
    st.markdown(f'<div style="font-size:1.05rem;font-weight:700;">📡 ST001 Control</div>'
                f'<div style="font-size:.75rem;color:{C["ink2"]};">ตั้งค่าการแสดงผลและการวิเคราะห์</div>',
                unsafe_allow_html=True)

    st.markdown('<div class="side-h">ช่องวัดเรดอน</div>', unsafe_allow_html=True)
    channel = st.radio(
        "ช่องวัดเรดอน", list(CHANNELS.keys()),
        format_func=lambda k: {"RADON_A": "ดิน · RADON_A", "RADON_B": "อากาศ · RADON_B"}[k],
        index=0, label_visibility="collapsed",
        help="ทุกแท็บวิเคราะห์ตามช่องที่เลือก ส่วนกราฟเทียบดินกับอากาศแสดงทั้งสองช่อง",
    )
    ch_label = CHANNELS[channel]["label"]
    ch_short = CHANNELS[channel]["short"]
    ch_color = CHANNELS[channel]["color"]

    st.markdown('<div class="side-h">ช่วงเวลา</div>', unsafe_allow_html=True)
    quick = st.segmented_control("ช่วงด่วน", ["7 วัน", "30 วัน", "90 วัน", "ทั้งหมด", "กำหนดเอง"],
                                 default="ทั้งหมด", label_visibility="collapsed") or "ทั้งหมด"
    if quick == "กำหนดเอง":
        date_range = st.date_input("ช่วงวันที่", value=(DATA_START, TODAY),
                                   min_value=datetime(2025, 1, 1).date(), max_value=TODAY)
        if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
            start_date, end_date = pd.Timestamp(date_range[0]), pd.Timestamp(date_range[1])
        else:
            start_date, end_date = pd.Timestamp(DATA_START), pd.Timestamp(TODAY)
    else:
        back = {"7 วัน": 7, "30 วัน": 30, "90 วัน": 90}.get(quick)
        end_date = pd.Timestamp(TODAY)
        start_date = end_date - pd.Timedelta(days=back - 1) if back else pd.Timestamp(DATA_START)
    end_ts = end_date + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    st.caption(f"{start_date:%d/%m/%Y} – {end_date:%d/%m/%Y}  ·  IQR ใช้หน้าต่าง 30 วัน")

    st.markdown('<div class="side-h">เงื่อนไขแผ่นดินไหว</div>', unsafe_allow_html=True)
    mag_threshold = st.slider("ขนาดขั้นต่ำ (M)", 1.0, 10.0, 4.0, step=0.5)
    radius_km = st.slider("รัศมีจากสถานี (km)", 100, 1000, 1000, step=100)
    days_ahead = st.slider("หน้าต่างพยากรณ์ (วัน)", 7, 60, 30)

    st.markdown('<div class="side-h">การอัปเดต</div>', unsafe_allow_html=True)
    realtime = st.toggle("รีเฟรชอัตโนมัติ", value=True)
    refresh_sec = st.select_slider("ทุก ๆ", options=[60, 120, 300, 600], value=300,
                                   format_func=lambda s: f"{s // 60} นาที", disabled=not realtime)
    if st.button("🔄 ดึงข้อมูลใหม่ทันที", width="stretch"):
        st.cache_data.clear()
        st.rerun()
    side_status = st.container()

# ─── Load data ───────────────────────────────────────────────────────────────
with st.spinner("กำลังดึงข้อมูลจาก Google Sheets..."):
    radon_raw, radon_info = load_radon_raw()
    eq_full, eq_info = load_eq_data()

STATION = radon_info.get("station") or STATION_DEFAULT
radon_full = prepare_radon(radon_raw, channel)
latest_radon_ts = radon_info.get("latest_raw")
if latest_radon_ts is None or pd.isna(latest_radon_ts):
    latest_radon_ts = radon_full.index.max() if len(radon_full) else None
latest_eq_ts = eq_full["Time (Thailand)"].max() if len(eq_full) else None
age_h, fresh_label, fresh_color = freshness(latest_radon_ts, NOW)
fresh_color = {"🟢": C["good"], "🟡": C["warning"], "🔴": C["critical"]}.get(fresh_label[0], C["ink2"])
eq_age_h, eq_fresh_label, _ = freshness(latest_eq_ts, NOW)

with side_status:
    st.markdown('<div class="side-h">สถานะข้อมูล</div>', unsafe_allow_html=True)
    if radon_info["is_mock"]:
        st.markdown(f'<span class="dot" style="background:{C["warning"]}"></span> Radon: <b>ข้อมูลจำลอง</b>',
                    unsafe_allow_html=True)
    else:
        st.markdown(f'<span class="dot" style="background:{fresh_color}"></span> Radon: {fresh_label[2:]}<br>'
                    f'<small style="color:{C["ink2"]}">ล่าสุด {latest_radon_ts:%d/%m/%Y %H:%M} · '
                    f'ผ่านมา {fmt_age(age_h)}</small>', unsafe_allow_html=True)
    if eq_info["is_mock"]:
        st.markdown(f'<span class="dot" style="background:{C["warning"]}"></span> EQ: <b>ข้อมูลจำลอง</b>',
                    unsafe_allow_html=True)
    else:
        st.markdown(f'<span class="dot" style="background:{C["good"]}"></span> EQ: {len(eq_full):,} เหตุการณ์<br>'
                    f'<small style="color:{C["ink2"]}">ล่าสุด {latest_eq_ts:%d/%m/%Y %H:%M}</small>',
                    unsafe_allow_html=True)
    st.markdown(f'<small style="color:{C["ink2"]}">📍 {STATION[0]}°N, {STATION[1]}°E<br>'
                f'ดึงข้อมูลเมื่อ {radon_info["fetched_at"]:%H:%M:%S}</small>', unsafe_allow_html=True)

# ─── Filter & analysis ───────────────────────────────────────────────────────
radon = radon_full.loc[start_date:end_ts].copy()
eq_sel = eq_full[(eq_full["Time (Thailand)"] >= start_date) & (eq_full["Time (Thailand)"] <= end_ts)].copy()
eq_sel["distance_km"] = haversine_km(STATION[0], STATION[1], eq_sel["Latitude"].values, eq_sel["Longitude"].values)
eq_near = eq_sel[eq_sel["distance_km"] <= radius_km].copy()
eq_big = eq_near[eq_near["Magnitude"] >= mag_threshold].sort_values("Time (Thailand)")

# Rolling IQR ต้องใช้ข้อมูลย้อนหลัง 30 วันก่อนช่วงที่เลือก (ไม่อย่างนั้นช่วงสั้น ๆ จะไม่มีเส้นขอบบน)
radon_ctx = radon_full.loc[start_date - pd.Timedelta(days=30):end_ts]
if radon.empty:
    st.info("ไม่มีข้อมูลเรดอนในช่วงวันที่ที่เลือก — ลองเลือกช่วงเวลาที่กว้างขึ้นใน sidebar")
    st.stop()
Q1, Q3, IQR, upper, _anom_ctx, _upper_ctx = compute_rolling_iqr_anomaly(radon_ctx, window_days=30)
radon_anom = _anom_ctx.loc[start_date:end_ts]
upper_rolling = _upper_ctx.loc[start_date:end_ts]
Q1, Q3 = radon.quantile(0.25), radon.quantile(0.75)
IQR = Q3 - Q1
upper = Q3 + 1.5 * IQR

# Survival data (1,000 anomaly ล่าสุด)
sample_anom = radon_anom.iloc[-1000:]
min_mag_list = [3.0, 4.0, 5.0]
eq_times, eq_mags = eq_near["Time (Thailand)"], eq_near["Magnitude"]
rows = []
for t, v in sample_anom.items():
    row = {"Anomaly_Time": t, "value": float(v)}
    for m in min_mag_list:
        k = str(m).replace(".", "")
        row[f"dt_M{k}"], row[f"ev_M{k}"] = time_to_first_eq(t, eq_times, eq_mags, days_ahead, m)
    rows.append(row)
dt_df = (pd.DataFrame(rows).set_index("Anomaly_Time") if rows else
         pd.DataFrame(columns=["value", "dt_M30", "ev_M30", "dt_M40", "ev_M40", "dt_M50", "ev_M50"]))
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
latest_anom_val = float(radon_anom.iloc[-1]) if len(radon_anom) else 0.0

# ความเสี่ยง "วันนี้" = hazard ของวันที่ตรงกับจำนวนวันนับจาก anomaly ล่าสุด
days_since = (pd.Timestamp(NOW).normalize() - pd.Timestamp(anchor).normalize()).days + 1
today_row = haz_df[haz_df["day"] == days_since]
if len(radon_anom) and len(today_row):
    today_haz = float(today_row["hazard"].iloc[0]) if pd.notna(today_row["hazard"].iloc[0]) else 0.0
    today_level = risk_level(today_haz)
    today_sub = f"วันที่ {days_since} หลัง anomaly ล่าสุด · hazard {today_haz:.3f}"
else:
    today_haz, today_level = 0.0, "LOW"
    today_sub = (f"ไม่มี anomaly ภายใน {days_ahead} วันที่ผ่านมา" if len(radon_anom)
                 else "ยังไม่พบ anomaly ในช่วงที่เลือก")

# ─── Top bar ─────────────────────────────────────────────────────────────────
if radon_info["is_mock"]:
    live_txt, live_col = "MOCK", C["warning"]
else:
    live_txt = {"🟢": "LIVE", "🟡": "DELAYED", "🔴": "STALE"}.get(fresh_label[0], "LIVE")
    live_col = fresh_color
latest_txt = f"{latest_radon_ts:%d/%m/%Y %H:%M}" if latest_radon_ts is not None else "-"

st.markdown(f"""
<div class="topbar">
  <div>
    <div class="t1">Radon Precursor Monitor <span style="color:{C['ink2']};font-weight:400;">· {STATION_NAME}</span></div>
    <div class="t2">iRES-MCM · RMUTI &nbsp;|&nbsp; {STATION[0]}°N, {STATION[1]}°E &nbsp;|&nbsp;
      แผ่นดินไหว M≥{mag_threshold} ภายใน {radius_km} km</div>
  </div>
  <div>
    <span class="pill"><span class="dot" style="background:{ch_color}"></span>{ch_label}</span>
    <span class="pill"><span class="dot live" style="background:{live_col}"></span><b style="color:{live_col}">{live_txt}</b>
      &nbsp;<span class="mono">{latest_txt}</span></span>
    <span class="pill mono">🕒 {NOW:%d/%m/%Y %H:%M}</span>
  </div>
</div>
""", unsafe_allow_html=True)

def banner(kind, html):
    st.markdown(f'<div class="banner {kind}">{html}</div>', unsafe_allow_html=True)


if radon_info["is_mock"]:
    banner("err", "⚠️ <b>ไม่สามารถดึงข้อมูลเรดอนจริงได้</b> — ตัวเลขและกราฟทั้งหมดเป็นข้อมูลจำลอง<br>"
           + "<br>".join(radon_info["errors"]))
elif age_h is not None and age_h > FRESH_WARN_H:
    banner("err", f"⚠️ <b>ข้อมูลเรดอนไม่ได้อัปเดตมา {fmt_age(age_h)}</b> (ค่าล่าสุด {latest_txt} น.) — "
           "โปรดตรวจสอบเครื่องวัดหรือการส่งข้อมูล")
if eq_info["is_mock"]:
    banner("warn", f"⚠️ <b>ดึงข้อมูลแผ่นดินไหวไม่ได้</b> — ใช้ข้อมูลจำลองแทน ({eq_info['error']})")

# ─── Status strip ────────────────────────────────────────────────────────────
rc = RISK_COLOR[today_level]
hero_ink = "#0b0b0b" if today_level == "MODERATE" else "#ffffff"
last_val = float(radon.iloc[-1])
med30 = float(radon_full.loc[radon.index[-1] - pd.Timedelta(days=30):].median())
delta_pct = (last_val / med30 - 1) * 100 if med30 else 0.0
anom_7d = int((radon_anom.index >= radon.index[-1] - pd.Timedelta(days=7)).sum())
last_upper = upper_rolling.dropna().iloc[-1] if upper_rolling.notna().any() else np.nan
eq_30d = eq_big[eq_big["Time (Thailand)"] >= pd.Timestamp(NOW) - pd.Timedelta(days=30)]
fmt_v = (lambda v: f"{v:,.0f}") if channel == "RADON_A" else (lambda v: f"{v:,.1f}")

h, k1, k2, k3, k4 = st.columns([1.35, 1, 1, 1, 1])
h.markdown(f"""
<div class="hero" style="background:{rc};color:{hero_ink};">
  <div class="lab">ความเสี่ยงวันนี้ (M≥{_snap})</div>
  <div class="val">{RISK_ICON[today_level]} {today_level}</div>
  <div class="sub">ระดับ{RISK_TH[today_level]} · {today_sub}</div>
</div>""", unsafe_allow_html=True)
k1.markdown(tile(f"ค่าล่าสุด · {ch_short}", fmt_v(last_val), UNIT,
                 f"{'▲' if delta_pct >= 0 else '▼'} {abs(delta_pct):.0f}% เทียบมัธยฐาน 30 วัน", ch_color),
            unsafe_allow_html=True)
k2.markdown(tile("Anomaly 7 วันล่าสุด", f"{anom_7d:,}", "ชม.",
                 f"ขอบบน IQR ล่าสุด {fmt_v(last_upper) if pd.notna(last_upper) else '-'} {UNIT}"),
            unsafe_allow_html=True)
k3.markdown(tile(f"แผ่นดินไหว M≥{mag_threshold} · 30 วัน", f"{len(eq_30d):,}", "ครั้ง",
                 f"ทั้งช่วงที่เลือก {len(eq_big):,} ครั้ง · R≤{radius_km} km"), unsafe_allow_html=True)
k4.markdown(tile("วันที่ hazard สูงสุด", f"Day {peak_day}", "",
                 f"hazard {peak_haz:.3f} · วันเสี่ยงสูง {n_high}/{days_ahead}"), unsafe_allow_html=True)
st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)

# ─── Tabs ────────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    ":material/sensors: เรดอน",
    ":material/public: แผ่นดินไหว",
    ":material/crisis_alert: พยากรณ์ความเสี่ยง",
    ":material/query_stats: สถิติ Precursor",
    ":material/fact_check: ตรวจสอบย้อนหลัง",
])

# ══ TAB 1 — Radon ══════════════════════════════════════════════════════════
with tab1:
    panel(f"{ch_label} — ค่ามัธยฐานรายชั่วโมง",
          f"Anomaly = ค่าที่สูงกว่าขอบบน Rolling IQR (Q3 + 1.5·IQR จากค่ามัธยฐานรายวันย้อนหลัง 30 วัน) · "
          f"พบ {len(radon_anom):,} ชั่วโมงในช่วงที่เลือก")
    step = max(1, len(radon) // 3000)
    rp = radon.iloc[::step]
    fig1 = go.Figure()
    fig1.add_trace(go.Scatter(x=rp.index, y=rp.values, mode="lines", name=ch_short,
                              line=dict(color=ch_color, width=1.4),
                              hovertemplate="%{y:,.1f} " + UNIT + "<extra>" + ch_short + "</extra>"))
    ur = upper_rolling.dropna()
    if len(ur):
        urp = ur.iloc[::max(1, len(ur) // 1000)]
        fig1.add_trace(go.Scatter(x=urp.index, y=urp.values, mode="lines", name="ขอบบน Rolling IQR",
                                  line=dict(color=C["upper"], width=1.4, dash="dot"),
                                  hovertemplate="%{y:,.1f}<extra>ขอบบน IQR</extra>"))
    fig1.add_trace(go.Scatter(x=radon_anom.index, y=radon_anom.values, mode="markers", name="Anomaly",
                              marker=dict(color=C["critical"], size=8, symbol="diamond",
                                          line=dict(color=C["surface"], width=2)),
                              hovertemplate="%{y:,.1f}<extra>Anomaly</extra>"))
    dark_layout(fig1, height=380)
    fig1.update_yaxes(title_text=UNIT)
    fig1.update_xaxes(rangeslider=dict(visible=True, bgcolor=C["page"], thickness=0.06))
    show(fig1)

    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
    panel("เรดอนในดิน vs เรดอนในอากาศ", "สองช่องมีสเกลต่างกันหลายสิบเท่า จึงแยกกราฟแต่ใช้แกนเวลาเดียวกัน")
    both = radon_raw.loc[start_date:end_ts, ["RADON_A", "RADON_B"]]
    cA, cB = st.columns([3, 1])
    with cA:
        bp = both.iloc[::max(1, len(both) // 2500)]
        fig1b = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
                              subplot_titles=("ดิน · RADON_A", "อากาศ · RADON_B"))
        fig1b.add_trace(go.Scatter(x=bp.index, y=bp["RADON_A"], mode="lines", name="ดิน · RADON_A",
                                   line=dict(color=C["soil"], width=1.3)), row=1, col=1)
        fig1b.add_trace(go.Scatter(x=bp.index, y=bp["RADON_B"], mode="lines", name="อากาศ · RADON_B",
                                   line=dict(color=C["air"], width=1.3)), row=2, col=1)
        dark_layout(fig1b, height=420)
        fig1b.update_layout(showlegend=False)
        fig1b.update_annotations(font=dict(color=C["ink2"], size=12), x=0, xanchor="left")
        fig1b.update_yaxes(title_text=UNIT)
        show(fig1b)
    with cB:
        pair = both.dropna()
        if len(pair) > 10:
            r_p = pair["RADON_A"].corr(pair["RADON_B"])
            r_s = pair["RADON_A"].corr(pair["RADON_B"], method="spearman")
            ratio = (pair["RADON_A"] / pair["RADON_B"]).median()
        else:
            r_p = r_s = ratio = np.nan
        f_ = lambda v, f: "-" if pd.isna(v) else format(v, f)
        st.markdown(tile("มัธยฐาน · ดิน", f_(both["RADON_A"].median(), ",.0f"), UNIT, accent=C["soil"]),
                    unsafe_allow_html=True)
        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        st.markdown(tile("มัธยฐาน · อากาศ", f_(both["RADON_B"].median(), ",.1f"), UNIT, accent=C["air"]),
                    unsafe_allow_html=True)
        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        st.markdown(tile("อัตราส่วน ดิน/อากาศ", f_(ratio, ",.1f"), "เท่า", "มัธยฐานรายชั่วโมง"),
                    unsafe_allow_html=True)
        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
        st.markdown(tile("Pearson r", f_(r_p, ".3f"), "", f"Spearman ρ = {f_(r_s, '.3f')}"),
                    unsafe_allow_html=True)

# ══ TAB 2 — Earthquakes ════════════════════════════════════════════════════
with tab2:
    cm, cs = st.columns([3, 1])
    with cm:
        panel(f"ตำแหน่งแผ่นดินไหวภายใน {radius_km} km", f"{len(eq_near):,} เหตุการณ์ · ขนาดจุดตามขนาดแผ่นดินไหว")
        em = eq_near.copy()
        em["sz"] = ((em["Magnitude"] - 1.5) * 4).clip(4, 26)
        region_col = next((c for c in em.columns if "region" in c.lower() or "พื้นที่" in c.lower()), None)
        em["txt"] = ("M" + em["Magnitude"].round(1).astype(str) + " · " + em["Time (Thailand)"].dt.strftime("%d/%m/%Y %H:%M")
                     + "<br>" + em["distance_km"].round(0).astype(int).astype(str) + " km จากสถานี"
                     + (("<br>" + em[region_col].astype(str).str[:40]) if region_col else ""))
        fig_map = go.Figure()
        for lab, lo, hi, col in [("M<3", 0, 3, "#5f5f5f"), ("3≤M<4", 3, 4, MAG_COLOR[3.0]),
                                 ("4≤M<5", 4, 5, MAG_COLOR[4.0]), ("M≥5", 5, 99, MAG_COLOR[5.0])]:
            sub = em[(em["Magnitude"] >= lo) & (em["Magnitude"] < hi)]
            if len(sub):
                fig_map.add_trace(go.Scattermap(
                    lat=sub["Latitude"], lon=sub["Longitude"], mode="markers", name=lab,
                    marker=dict(size=sub["sz"], color=col, opacity=0.8),
                    text=sub["txt"], hovertemplate="%{text}<extra></extra>"))
        fig_map.add_trace(go.Scattermap(lat=[STATION[0]], lon=[STATION[1]], mode="markers+text",
                                        name="สถานี ST001", text=["ST001"], textposition="top right",
                                        textfont=dict(color=C["ink"], size=12),
                                        marker=dict(size=16, color=C["accent"]),
                                        hovertemplate="สถานี ST001<extra></extra>"))
        zoom = 6.2 if radius_km <= 200 else 5.2 if radius_km <= 400 else 4.3 if radius_km <= 700 else 3.8
        fig_map.update_layout(map=dict(style="carto-darkmatter", center=dict(lat=STATION[0], lon=STATION[1]), zoom=zoom),
                              paper_bgcolor=C["surface"], height=470, margin=dict(l=0, r=0, t=0, b=0),
                              legend=dict(bgcolor="rgba(25,25,25,0.85)", font=dict(color=C["ink"]), x=0.01, y=0.99),
                              hoverlabel=dict(bgcolor=C["raised"], font=dict(color=C["ink"])))
        show(fig_map)
    with cs:
        panel("สรุป", f"รัศมี {radius_km} km")
        for m in [3.0, 4.0, 5.0, 6.0]:
            st.markdown(tile(f"M ≥ {m}", f"{int((eq_near['Magnitude'] >= m).sum()):,}", "ครั้ง"), unsafe_allow_html=True)
            st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
        if len(eq_big) and len(radon_anom):
            ai = radon_anom.index.values
            pos = np.searchsorted(ai, eq_big["Time (Thailand)"].values, side="left")
            prev = np.where(pos > 0, ai[np.maximum(pos - 1, 0)], np.datetime64("NaT"))
            gap = (eq_big["Time (Thailand)"].values - prev) / np.timedelta64(1, "D")
            detect_pct = float(np.mean((pos > 0) & (gap <= 30)) * 100)
        else:
            detect_pct = 0.0
        st.markdown(tile("จับสัญญาณล่วงหน้าได้", f"{detect_pct:.1f}", "%",
                         f"EQ M≥{mag_threshold} ที่มี anomaly ก่อนหน้า ≤30 วัน", C["accent"]), unsafe_allow_html=True)

    panel("ไทม์ไลน์: Anomaly เรดอน กับ แผ่นดินไหว", "แกนเวลาเดียวกัน — ดูว่าแผ่นดินไหวเกิดตามหลังช่วงที่มี anomaly หรือไม่")
    fig6 = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06, row_heights=[0.6, 0.4])
    rd = radon.iloc[::max(1, len(radon) // 2000)]
    fig6.add_trace(go.Scatter(x=rd.index, y=rd.values, mode="lines", name=ch_short,
                              line=dict(color=ch_color, width=1.2)), row=1, col=1)
    fig6.add_trace(go.Scatter(x=radon_anom.index, y=radon_anom.values, mode="markers", name="Anomaly",
                              marker=dict(color=C["critical"], size=7, symbol="diamond")), row=1, col=1)
    fig6.add_trace(go.Scatter(x=eq_big["Time (Thailand)"], y=eq_big["Magnitude"], mode="markers",
                              name=f"แผ่นดินไหว M≥{mag_threshold}",
                              marker=dict(color=C["eq"], size=(eq_big["Magnitude"] - 1) * 2.5 + 4,
                                          symbol="triangle-up", line=dict(color=C["surface"], width=1)),
                              hovertemplate="M%{y:.1f}<extra>แผ่นดินไหว</extra>"), row=2, col=1)
    dark_layout(fig6, height=440, hover="closest")
    fig6.update_yaxes(title_text=UNIT, row=1, col=1)
    fig6.update_yaxes(title_text="Magnitude", row=2, col=1)
    show(fig6)

    panel(f"Lag correlation: anomaly วันที่ t → จำนวนแผ่นดินไหว M≥{mag_threshold} วันที่ t+lag",
          "ค่า Pearson r ระหว่างธง anomaly รายวันกับจำนวนเหตุการณ์ (lag 0–30 วัน)")
    rdly = radon.resample("D").max()
    aflag = (rdly > upper).astype(float)
    eqd = (eq_big.set_index("Time (Thailand)")["Magnitude"].resample("D").count()
           .reindex(rdly.index, fill_value=0)) if len(eq_big) else pd.Series(0, index=rdly.index)
    lags = list(range(31))
    corrs = [0.0 if pd.isna(c := aflag.corr(eqd.shift(-lag))) else float(c) for lag in lags]
    fig_lag = go.Figure(go.Bar(x=lags, y=corrs, marker=dict(color=[C["accent"] if c >= 0 else "#5f5f5f" for c in corrs],
                                                           cornerradius=4),
                               hovertemplate="lag %{x} วัน · r = %{y:.3f}<extra></extra>"))
    dark_layout(fig_lag, height=240, hover="closest")
    lim = max(0.3, max(abs(c) for c in corrs) * 1.15)
    fig_lag.update_layout(xaxis_title="lag (วัน)", yaxis_title="Pearson r", yaxis=dict(range=[-lim, lim]))
    show(fig_lag)

# ══ TAB 3 — Hazard Forecast ════════════════════════════════════════════════
with tab3:
    c5a, c5b = st.columns([2.2, 1])
    with c5a:
        panel(f"Daily hazard rate — {days_ahead} วันนับจาก anomaly ล่าสุด",
              f"M≥{_snap} ภายใน {radius_km} km · เส้นประคือเกณฑ์ HIGH (0.10) และ VERY HIGH (0.20)")
        fig5 = go.Figure()
        for rl, col in RISK_COLOR.items():
            sub = haz_df[haz_df["risk_level"] == rl]
            if len(sub):
                fig5.add_trace(go.Bar(x=sub["forecast_date"], y=sub["hazard"], name=f"{RISK_ICON[rl]} {rl}",
                                      marker=dict(color=col, cornerradius=4), width=86400000 * 0.75,
                                      hovertemplate="%{x|%d/%m/%Y}<br>hazard %{y:.4f}<extra>" + rl + "</extra>"))
        for y, lab, col in [(0.10, "HIGH 0.10", C["serious"]), (0.20, "VERY HIGH 0.20", C["critical"])]:
            fig5.add_hline(y=y, line_dash="dash", line_color=col, line_width=1,
                           annotation_text=lab, annotation_font_color=C["ink2"], annotation_position="top right")
        fig5.add_hline(y=baseline_safe, line_dash="dot", line_color=C["ink2"], line_width=1,
                       annotation_text=f"baseline {baseline_safe:.4f}", annotation_font_color=C["ink2"],
                       annotation_position="bottom right")
        if anchor <= pd.Timestamp(NOW) <= anchor + pd.Timedelta(days=days_ahead):
            fig5.add_vline(x=pd.Timestamp(NOW), line_color=C["ink"], line_width=1)
            fig5.add_annotation(x=pd.Timestamp(NOW), y=1, yref="paper", text="วันนี้", showarrow=False,
                                font=dict(color=C["ink"], size=11), yanchor="bottom")
        dark_layout(fig5, height=380, hover="closest")
        fig5.update_layout(barmode="overlay", xaxis_title="วันที่", yaxis_title="hazard")
        show(fig5)
    with c5b:
        panel("รายงานการพยากรณ์")
        st.markdown(f"""
        <div class="tile">
          <table class="rtable">
            <tr><td style="color:{C['ink2']}">Anomaly ล่าสุด (จุดเริ่มนับ)</td><td class="mono" style="text-align:right">{anchor:%d/%m/%Y %H:%M}</td></tr>
            <tr><td style="color:{C['ink2']}">ค่า ณ จุดนั้น</td><td class="mono" style="text-align:right">{fmt_v(latest_anom_val)} {UNIT}</td></tr>
            <tr><td style="color:{C['ink2']}">Baseline hazard</td><td class="mono" style="text-align:right">{baseline_safe:.5f}</td></tr>
            <tr><td style="color:{C['ink2']}">Peak hazard</td><td class="mono" style="text-align:right">{peak_haz:.4f} (Day {peak_day})</td></tr>
            <tr><td style="color:{C['ink2']}">วันเสี่ยงสูง</td><td class="mono" style="text-align:right">{n_high} / {days_ahead} วัน</td></tr>
            <tr><td style="color:{C['ink2']}">ความเสี่ยงวันนี้</td><td style="text-align:right">{badge(today_level)}</td></tr>
          </table>
        </div>""", unsafe_allow_html=True)
        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
        panel("ปฏิทินวันเสี่ยง", "เฉพาะวันที่ระดับ HIGH ขึ้นไป")
        risk_rows = haz_df[haz_df["risk_level"].isin(["HIGH", "VERY HIGH"])]
        if len(risk_rows):
            html = '<div class="tile scroll"><table class="rtable"><tr><th>วันที่</th><th>Day</th><th>hazard</th><th>ระดับ</th></tr>'
            for _, r in risk_rows.iterrows():
                html += (f"<tr><td class='mono'>{r['forecast_date']:%d/%m}</td><td class='mono'>{int(r['day'])}</td>"
                         f"<td class='mono'>{r['hazard']:.4f}</td><td>{badge(r['risk_level'])}</td></tr>")
            st.markdown(html + "</table></div>", unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="tile">{badge("LOW")} &nbsp;ไม่มีวันเสี่ยงสูงในหน้าต่างพยากรณ์</div>',
                        unsafe_allow_html=True)
    st.markdown('<div class="note">Hazard คำนวณจากสัดส่วนของ anomaly ในอดีตที่ตามด้วยแผ่นดินไหวในวันที่ d '
                '(piecewise hazard จาก anomaly ล่าสุดไม่เกิน 1,000 จุด) เป็นค่าเชิงสถิติเพื่อการวิจัย '
                'ไม่ใช่การพยากรณ์แผ่นดินไหวอย่างเป็นทางการ</div>', unsafe_allow_html=True)
    st.download_button("⬇️ ดาวน์โหลดผลพยากรณ์ (CSV)", haz_df.to_csv(index=False).encode("utf-8-sig"),
                       f"st001_{channel.lower()}_hazard_forecast.csv", "text/csv")

# ══ TAB 4 — Precursor statistics ═══════════════════════════════════════════
with tab4:
    c3, c4 = st.columns(2)
    with c3:
        groups = {k: v for k, v in {"3≤M<4": (dt_3_4.values, MAG_COLOR[3.0]),
                                    "4≤M<5": (dt_4_5.values, MAG_COLOR[4.0]),
                                    "M≥5": (dt_5up.values, MAG_COLOR[5.0])}.items() if len(v[0]) > 0}
        if len(dt_4_5) > 1 and len(dt_5up) > 1:
            _, p = mannwhitneyu(dt_4_5, dt_5up, alternative="two-sided")
            mw = f"Mann-Whitney U (4≤M<5 vs M≥5): p = {p:.4f} {'· มีนัยสำคัญ' if p < 0.05 else '· ไม่มีนัยสำคัญ'}"
        else:
            mw = "ข้อมูลไม่พอสำหรับ Mann-Whitney U test"
        panel("ระยะเวลานำ (Δt) จาก anomaly ถึงแผ่นดินไหว", mw)
        if groups:
            fig3 = go.Figure()
            for g, (vals, col) in groups.items():
                fig3.add_trace(go.Box(y=vals, name=g, marker=dict(color=col, size=4, opacity=0.5),
                                      line=dict(width=1.5), boxmean=True, boxpoints="outliers"))
            dark_layout(fig3, height=380, hover="closest")
            fig3.update_layout(showlegend=False, yaxis_title="Δt (วัน)")
            show(fig3)
        else:
            st.info("ยังไม่มีเหตุการณ์พอสำหรับวิเคราะห์ Δt")
    with c4:
        panel("Kaplan-Meier: ความน่าจะเป็นที่ยังไม่เกิดแผ่นดินไหว",
              f"S(t) หลัง anomaly · หน้าต่าง {days_ahead} วัน · R≤{radius_km} km · แถบจางคือ 95% CI")
        fig4 = go.Figure()
        for T, E, lab, col in [(T3, E3, "M≥3", MAG_COLOR[3.0]), (T4, E4, "M≥4", MAG_COLOR[4.0]),
                               (T5, E5, "M≥5", MAG_COLOR[5.0])]:
            if len(E) == 0 or E.sum() == 0:
                continue
            try:
                kmf = KaplanMeierFitter().fit(T.astype(float), event_observed=E.astype(int), label=lab)
                t_ = kmf.survival_function_.index
                s_ = kmf.survival_function_[lab].values
                lo, hi = kmf.confidence_interval_.iloc[:, 0].values, kmf.confidence_interval_.iloc[:, 1].values
                r, g, b = (int(col[i:i + 2], 16) for i in (1, 3, 5))
                fig4.add_trace(go.Scatter(x=list(t_) + list(t_[::-1]), y=list(hi) + list(lo[::-1]), fill="toself",
                                          fillcolor=f"rgba({r},{g},{b},0.15)", line=dict(width=0),
                                          showlegend=False, hoverinfo="skip"))
                fig4.add_trace(go.Scatter(x=t_, y=s_, mode="lines", name=lab, line=dict(color=col, width=2, shape="hv"),
                                          hovertemplate="วันที่ %{x:.1f} · S = %{y:.3f}<extra>" + lab + "</extra>"))
            except Exception:
                pass
        dark_layout(fig4, height=380, hover="closest")
        fig4.update_layout(xaxis_title="วันหลัง anomaly", yaxis_title="S(t)", yaxis=dict(range=[0, 1.05]))
        show(fig4)

# ══ TAB 5 — Verification & tuning ══════════════════════════════════════════
with tab5:
    big_t = eq_big["Time (Thailand)"].values
    peak_rl = risk_level(peak_haz)
    region_col = next((c for c in eq_near.columns if "region" in c.lower() or "พื้นที่" in c.lower()), None)
    vrows = []
    for at, val in radon_anom.items():
        lo_i = np.searchsorted(big_t, np.datetime64(at), side="right")
        hi_i = np.searchsorted(big_t, np.datetime64(at + pd.Timedelta(days=days_ahead)), side="right")
        ea = eq_big.iloc[lo_i:hi_i]
        n = len(ea)
        first = ea.iloc[0] if n else None
        vrows.append({
            "Anomaly Time": at, f"Radon ({UNIT})": round(float(val), 1), "Risk Level": peak_rl,
            "มี EQ ตามมา": "✅ ใช่" if n else "❌ ไม่",
            "EQ แรก": first["Time (Thailand)"].strftime("%Y-%m-%d") if n else "-",
            "Δt (วัน)": f"{(first['Time (Thailand)'] - at).total_seconds() / 86400:.1f}" if n else "-",
            "Max Mag": f"{ea['Magnitude'].max():.1f}" if n else "-",
            "Region": (str(first[region_col])[:50] if region_col else "-") if n else "-",
            "จำนวน EQ": n,
        })
    vdf = pd.DataFrame(vrows, columns=["Anomaly Time", f"Radon ({UNIT})", "Risk Level", "มี EQ ตามมา", "EQ แรก",
                                       "Δt (วัน)", "Max Mag", "Region", "จำนวน EQ"])
    hit = vdf[vdf["มี EQ ตามมา"] == "✅ ใช่"]
    miss = vdf[vdf["มี EQ ตามมา"] == "❌ ไม่"]
    n_total, n_hit = len(vdf), len(hit)
    hit_rate = n_hit / n_total * 100 if n_total else 0

    s1, s2, s3 = st.columns(3)
    s1.markdown(tile("Anomaly ทั้งหมด", f"{n_total:,}", "ชม."), unsafe_allow_html=True)
    s2.markdown(tile("มีแผ่นดินไหวตามมา (hit)", f"{hit_rate:.1f}", "%", f"{n_hit:,} ครั้ง", C["good"]),
                unsafe_allow_html=True)
    s3.markdown(tile("ไม่มีแผ่นดินไหว (false alarm)", f"{100 - hit_rate:.1f}", "%", f"{n_total - n_hit:,} ครั้ง",
                     C["critical"]), unsafe_allow_html=True)
    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    panel(f"Anomaly ที่ตามด้วยแผ่นดินไหว M≥{mag_threshold} ภายใน {days_ahead} วัน",
          f"วงกลมทึบ = มีแผ่นดินไหวตามมา · วงกลมกลวง = ไม่มี · สามเหลี่ยม = แผ่นดินไหวจริง (R≤{radius_km} km)")
    fig7 = go.Figure()
    fig7.add_trace(go.Scatter(x=hit["Anomaly Time"], y=hit[f"Radon ({UNIT})"], mode="markers", name="มี EQ ตามมา",
                              marker=dict(color=C["good"], size=8, line=dict(color=C["surface"], width=2))))
    fig7.add_trace(go.Scatter(x=miss["Anomaly Time"], y=miss[f"Radon ({UNIT})"], mode="markers", name="ไม่มี EQ",
                              marker=dict(color="rgba(0,0,0,0)", size=8, line=dict(color=C["critical"], width=1.5))))
    y_eq = float(radon_anom.min()) * 0.9 if len(radon_anom) else 0
    fig7.add_trace(go.Scatter(x=eq_big["Time (Thailand)"], y=[y_eq] * len(eq_big), mode="markers",
                              name=f"แผ่นดินไหว M≥{mag_threshold}", customdata=eq_big["Magnitude"],
                              marker=dict(color=C["eq"], size=eq_big["Magnitude"] * 2, symbol="triangle-up"),
                              hovertemplate="%{x|%d/%m/%Y} · M%{customdata:.1f}<extra></extra>"))
    dark_layout(fig7, height=360, hover="closest")
    fig7.update_layout(yaxis_title=UNIT)
    show(fig7)

    panel("ตารางรายละเอียด")
    vshow = vdf.sort_values("Anomaly Time", ascending=False).copy()
    vshow["Anomaly Time"] = vshow["Anomaly Time"].dt.strftime("%Y-%m-%d %H:%M")
    vshow_html = vshow.copy()
    vshow_html["Risk Level"] = vshow_html["Risk Level"].map(badge)
    html_table(vshow_html, max_rows=300, height=380)
    st.download_button("⬇️ ดาวน์โหลดตารางตรวจสอบ (CSV)", vshow.to_csv(index=False).encode("utf-8-sig"),
                       f"st001_{channel.lower()}_risk_verification.csv", "text/csv")

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
    with st.expander("🎯 ปรับพารามิเตอร์ (Grid Search)", expanded=False):
        st.markdown('<div class="note">ลองทุกชุดของ Magnitude × Radius × Window แล้วหาชุดที่ให้ hit rate สูงสุดสำหรับสถานีนี้ '
                    '(ใช้ anomaly ล่าสุด 200 จุด) · อ้างอิง Cicerone et al. (2009)</div>', unsafe_allow_html=True)
        g1, g2, g3 = st.columns(3)
        grid_mags = g1.multiselect("Magnitude", [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0], default=[1.5, 2.0, 2.5, 3.0, 4.0])
        grid_radii = g2.multiselect("Radius (km)", [100, 200, 300, 500, 700, 1000], default=[200, 300, 500, 700, 1000])
        grid_windows = g3.multiselect("Window (วัน)", [7, 14, 21, 30, 45, 60], default=[7, 14, 21, 30])
        total_comb = len(grid_mags) * len(grid_radii) * len(grid_windows)
        if st.button(f"🚀 รัน Grid Search ({total_comb} ชุด)", type="primary", disabled=total_comb == 0):
            prog = st.progress(0, text="กำลังคำนวณ...")
            res, done = [], 0
            a_t = radon_anom.iloc[-200:].index.values
            for mag in grid_mags:
                for rad in grid_radii:
                    g_t = eq_sel[(eq_sel["distance_km"] <= rad) & (eq_sel["Magnitude"] >= mag)] \
                        .sort_values("Time (Thailand)")["Time (Thailand)"].values
                    for win in grid_windows:
                        lo_i = np.searchsorted(g_t, a_t, side="right")
                        hi_i = np.searchsorted(g_t, a_t + np.timedelta64(win, "D"), side="right")
                        nh, nt = int(np.sum(hi_i > lo_i)), len(a_t)
                        if len(g_t) and nt:
                            pp = np.searchsorted(a_t, g_t, side="left")
                            pv = a_t[np.maximum(pp - 1, 0)]
                            sens = int(np.sum((pp > 0) & ((g_t - pv) / np.timedelta64(1, "D") <= win))) / len(g_t) * 100
                        else:
                            sens = 0.0
                        res.append({"Magnitude": mag, "Radius (km)": rad, "Window (days)": win, "Hit": nh, "Total": nt,
                                    "Accuracy (%)": round(nh / nt * 100 if nt else 0, 1),
                                    "Sensitivity (%)": round(sens, 1)})
                        done += 1
                        prog.progress(done / total_comb, text=f"{done}/{total_comb}")
            prog.empty()
            gdf = pd.DataFrame(res).sort_values("Accuracy (%)", ascending=False).reset_index(drop=True)
            best = gdf.iloc[0]
            b1, b2, b3, b4 = st.columns(4)
            b1.markdown(tile("Magnitude ที่ดีที่สุด", f"≥ {best['Magnitude']}", accent=C["accent"]), unsafe_allow_html=True)
            b2.markdown(tile("Radius", f"{best['Radius (km)']:.0f}", "km"), unsafe_allow_html=True)
            b3.markdown(tile("Window", f"{best['Window (days)']:.0f}", "วัน"), unsafe_allow_html=True)
            b4.markdown(tile("Hit rate", f"{best['Accuracy (%)']:.1f}", "%",
                             f"{int(best['Hit'])}/{int(best['Total'])} · Sensitivity {best['Sensitivity (%)']:.1f}%",
                             C["good"]), unsafe_allow_html=True)
            bw = int(best["Window (days)"])
            heat = gdf[gdf["Window (days)"] == bw].pivot_table(index="Magnitude", columns="Radius (km)",
                                                               values="Accuracy (%)")
            fig8 = go.Figure(go.Heatmap(z=heat.values, x=[f"{int(c)} km" for c in heat.columns],
                                        y=[f"M≥{r}" for r in heat.index], xgap=2, ygap=2,
                                        colorscale=[[0, "#0d2a4d"], [0.5, "#1f6fc6"], [1, "#9ccbff"]],
                                        text=heat.values.round(1), texttemplate="%{text}%",
                                        textfont=dict(color=C["page"]), colorbar=dict(title="Hit %")))
            dark_layout(fig8, f"Hit rate (%) — Window = {bw} วัน", height=340, hover="closest")
            show(fig8)
            html_table(gdf, max_rows=20, height=340)
            st.download_button("⬇️ ดาวน์โหลดผล Grid Search (CSV)", gdf.to_csv(index=False).encode("utf-8-sig"),
                               f"st001_{channel.lower()}_grid_search.csv", "text/csv")

    csv_anom = radon_anom.rename(UNIT).rename_axis("Timestamp").reset_index()
    st.download_button("⬇️ ดาวน์โหลดรายการ Anomaly (CSV)", csv_anom.to_csv(index=False).encode("utf-8-sig"),
                       f"st001_{channel.lower()}_anomalies.csv", "text/csv")

# ─── Visit Counter ───────────────────────────────────────────────────────────
COUNTER_FILE = "/tmp/visitor_count_st001.json"


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
    st.session_state.total_visits = load_total_visits() + 1
    save_total_visits(st.session_state.total_visits)

# ─── Footer ──────────────────────────────────────────────────────────────────
st.markdown(f"""
<div class="footer">
  <div>🔬 <b style="color:{C['ink']}">iRES-MCM Radon &amp; Earthquake Hazard Dashboard</b> · {STATION_NAME}<br>
    สถานีตรวจวัดเรดอน มหาวิทยาลัยเทคโนโลยีราชมงคลอีสาน (RMUTI) · Lat {STATION[0]}°N, Lon {STATION[1]}°E</div>
  <div style="text-align:right">ผู้เข้าชมสะสม <b class="mono" style="color:{C['ink']}">{st.session_state.total_visits:,}</b> ครั้ง ·
    ข้อมูลเรดอนล่าสุด <span class="mono">{latest_txt}</span><br>
    เริ่มพัฒนา มกราคม 2568 · © 2568 สงวนลิขสิทธิ์ ห้ามคัดลอกโดยไม่ได้รับอนุญาต</div>
</div>
""", unsafe_allow_html=True)

# ─── Auto-refresh ────────────────────────────────────────────────────────────
if realtime:
    time.sleep(refresh_sec)
    st.rerun()
