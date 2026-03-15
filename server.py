"""
Dubai Watch — Lightweight production server.
Serves the dashboard HTML + API endpoints (suggestions, analytics, data refresh).
"""
import os
import json
import logging
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("dubai_watch")

# ── Database helpers ──────────────────────────────────────────
import sqlite3

DB_DIR = os.getenv("DB_DIR", "/tmp/dubai-watch-data")
os.makedirs(DB_DIR, exist_ok=True)

def get_analytics_db():
    path = os.path.join(DB_DIR, "analytics.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS visits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        ip_hash TEXT NOT NULL,
        referrer TEXT,
        referrer_domain TEXT,
        user_agent TEXT,
        device TEXT,
        country TEXT
    )""")
    conn.commit()
    return conn

def get_suggestions_db():
    path = os.path.join(DB_DIR, "suggestions.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        text TEXT NOT NULL,
        ip_hash TEXT,
        user_agent TEXT
    )""")
    conn.commit()
    return conn

import hashlib
def hash_ip(ip):
    return hashlib.sha256(f"dubaiwatch2026{ip}".encode()).hexdigest()[:16]

def detect_device(ua):
    ua = (ua or "").lower()
    if any(k in ua for k in ["iphone", "android", "mobile"]):
        return "mobile"
    if any(k in ua for k in ["ipad", "tablet"]):
        return "tablet"
    return "desktop"

def extract_domain(ref):
    if not ref:
        return "direct"
    try:
        from urllib.parse import urlparse
        d = urlparse(ref).netloc.lower()
        return d[4:] if d.startswith("www.") else (d or "direct")
    except:
        return "unknown"


# ── FastAPI app ─────────────────────────────────────────────

app = FastAPI(title="Dubai Watch", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files (latest.json, etc.)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Serve data dir for latest.json
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
app.mount("/data", StaticFiles(directory=DATA_DIR), name="data")


# ── Dashboard (serve the HTML) ────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


# ── Tracking pixel ────────────────────────────────────────

@app.get("/t.gif")
async def tracking_pixel(request: Request):
    ip = request.headers.get("x-forwarded-for", request.client.host or "unknown")
    referrer = request.headers.get("referer", "")
    ua = request.headers.get("user-agent", "")
    country = request.headers.get("cf-ipcountry", request.headers.get("x-country", ""))

    try:
        db = get_analytics_db()
        db.execute(
            "INSERT INTO visits (ts, ip_hash, referrer, referrer_domain, user_agent, device, country) VALUES (?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), hash_ip(ip), referrer,
             extract_domain(referrer), ua, detect_device(ua), country)
        )
        db.commit()
        db.close()
    except Exception as e:
        logger.error(f"Analytics error: {e}")

    gif = b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x00\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
    return Response(content=gif, media_type="image/gif",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# ── Analytics API ─────────────────────────────────────────

@app.get("/api/analytics")
async def api_analytics(days: int = 30):
    db = get_analytics_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    total = db.execute("SELECT COUNT(*) FROM visits WHERE ts >= ?", (cutoff,)).fetchone()[0]
    uniques = db.execute("SELECT COUNT(DISTINCT ip_hash) FROM visits WHERE ts >= ?", (cutoff,)).fetchone()[0]

    daily = [dict(r) for r in db.execute("""
        SELECT DATE(ts) as date, COUNT(*) as views, COUNT(DISTINCT ip_hash) as uniques
        FROM visits WHERE ts >= ? GROUP BY DATE(ts) ORDER BY date
    """, (cutoff,)).fetchall()]

    referrers = [dict(r) for r in db.execute("""
        SELECT referrer_domain as domain, COUNT(*) as count
        FROM visits WHERE ts >= ? GROUP BY referrer_domain ORDER BY count DESC LIMIT 20
    """, (cutoff,)).fetchall()]

    devices = {r["device"]: r["count"] for r in db.execute("""
        SELECT device, COUNT(*) as count FROM visits WHERE ts >= ? GROUP BY device
    """, (cutoff,)).fetchall()}

    countries = [dict(r) for r in db.execute("""
        SELECT country, COUNT(*) as count FROM visits WHERE ts >= ? AND country != ''
        GROUP BY country ORDER BY count DESC LIMIT 20
    """, (cutoff,)).fetchall()]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_views = db.execute("SELECT COUNT(*) FROM visits WHERE DATE(ts) = ?", (today,)).fetchone()[0]
    today_uniques = db.execute("SELECT COUNT(DISTINCT ip_hash) FROM visits WHERE DATE(ts) = ?", (today,)).fetchone()[0]

    db.close()
    return {
        "period_days": days, "total_views": total, "unique_visitors": uniques,
        "today_views": today_views, "today_uniques": today_uniques,
        "daily": daily, "referrers": referrers, "devices": devices, "countries": countries,
    }


# ── Suggestions API ───────────────────────────────────────

@app.post("/api/suggestions")
async def submit_suggestion(request: Request):
    try:
        body = await request.json()
        text = body.get("text", "").strip()
        if not text:
            return {"error": "empty"}
        ip = request.client.host if request.client else "unknown"
        ua = request.headers.get("user-agent", "")
        db = get_suggestions_db()
        cursor = db.execute(
            "INSERT INTO suggestions (ts, text, ip_hash, user_agent) VALUES (?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), text, hash_ip(ip), ua)
        )
        db.commit()
        sid = cursor.lastrowid
        db.close()
        return {"ok": True, "id": sid}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/suggestions")
async def read_suggestions(limit: int = 100):
    db = get_suggestions_db()
    rows = db.execute("SELECT id, ts, text FROM suggestions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    db.close()
    return [{"id": r["id"], "date": r["ts"][:16], "text": r["text"]} for r in rows]


# ── Health check ──────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}
