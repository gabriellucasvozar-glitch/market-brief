import json, re, html, calendar, threading, time, io
from datetime import datetime, timedelta, timezone
from pathlib import Path
import feedparser, yfinance as yf, requests, certifi
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify, request, send_file
from apscheduler.schedulers.background import BackgroundScheduler

BASE = Path(__file__).parent
DATA = BASE / "data"; DATA.mkdir(exist_ok=True)
ART, MKT = DATA / "articles.json", DATA / "markets.json"
CFG = json.loads((BASE / "config.json").read_text())
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) MarketBrief/1.0"}
STATUS = {}
app = Flask(__name__, static_folder="static", static_url_path="")

def load(p, default):
    try: return json.loads(p.read_text())
    except Exception: return default

def clean(t):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t or ""))).strip()

def score(title, summ, w, age_h):
    s, tags, tl, sl = w, [], title.lower(), summ.lower()
    for k, v in CFG["keywords"].items():
        if re.search(rf"\b{re.escape(k)}\b", tl): s += v * 2; tags.append(k)
        elif re.search(rf"\b{re.escape(k)}\b", sl): s += v; tags.append(k)
    s += 3 if age_h < 24 else 1 if age_h < 72 else 0
    return round(s, 1), tags

def tickers_for(text):
    out = []
    for k, v in CFG["ticker_map"].items():
        if re.search(rf"\b{re.escape(k)}\b", text.lower()):
            out += [x for x in v if x not in out]
    return out[:3]

def refresh_articles():
    store, now = load(ART, {}), datetime.now(timezone.utc)
    for f in CFG["feeds"]:
        try:
            r = requests.get(f["url"], headers=UA, timeout=20, verify=certifi.where())
            r.raise_for_status(); feed = feedparser.parse(r.content)
            STATUS[f["name"]] = f"OK ({len(feed.entries)} items)"
        except Exception as ex:
            STATUS[f["name"]] = "FAILED: " + str(ex)[:140]; continue
        for e in feed.entries:
            link = e.get("link")
            if not link: continue
            t = e.get("published_parsed") or e.get("updated_parsed")
            pub = datetime.fromtimestamp(calendar.timegm(t), timezone.utc) if t else now
            title, summ = clean(e.get("title")), clean(e.get("summary"))[:500]
            sc, tags = score(title, summ, f["w"] + (6 if f.get("sector") else 0), (now - pub).total_seconds() / 3600)
            store[link] = dict(title=title, summary=summ, link=link, source=f["name"],
                               published=pub.isoformat(), score=sc, tags=tags,
                               tickers=tickers_for(title + " " + summ))
    cutoff = now - timedelta(days=21)
    store = {k: v for k, v in store.items() if datetime.fromisoformat(v["published"]) > cutoff}
    ART.write_text(json.dumps(store)); (DATA / "status.json").write_text(json.dumps(STATUS))

def one(w):
    try:
        h = yf.Ticker(w["s"]).history(period="3mo")["Close"].dropna()
        c = [round(float(x), 4) for x in h]
        d = [i.strftime("%Y-%m-%d") for i in h.index]
        pct = lambda n: round((c[-1] / c[-1 - n] - 1) * 100, 2) if len(c) > n else None
        return w["s"], dict(name=w["n"], group=w["g"], last=c[-1], day=pct(1), week=pct(5), dates=d, closes=c)
    except Exception:
        return None

def refresh_markets():
    with ThreadPoolExecutor(8) as ex:
        out = dict(x for x in ex.map(one, [w for w in CFG["watchlist"] if w["on"]]) if x)
    if out: MKT.write_text(json.dumps(out))

def refresh_all():
    refresh_articles(); refresh_markets()

SECTORS = [
 ("IPOs & Listings", {"ipo","initial public offering"}),
 ("M&A & Deals", {"merger","acquisition","takeover","buyout","deal"}),
 ("Private Equity, VC & Angels", {"private equity","venture capital","venture","business angel","angel investor","funding round","series a","series b"}),
 ("Asset Management", {"asset manager","asset management","blackrock","vanguard"}),
 ("Banking", {"investment bank","goldman","jpmorgan","morgan stanley","ubs","deutsche bank"}),
 ("Central Banks & Macro", {"rate decision","interest rate","rate cut","rate rise","central bank","inflation","recession"}),
 ("Geopolitics & Trade", {"war","sanctions","tariff"}),
 ("Commodities & Energy", {"opec","oil","gold"}),
]
FEED_SECTOR = {f["name"]: f["sector"] for f in CFG["feeds"] if f.get("sector")}

def sector_of(a):
    if a["source"] in FEED_SECTOR: return FEED_SECTOR[a["source"]]
    best, bw = None, 0
    for name, tags in SECTORS:
        w = sum(CFG["keywords"].get(t, 0) for t in a["tags"] if t in tags)
        if w > bw: best, bw = name, w
    return best

def major_news(day, everything=False):
    cut = datetime.now(timezone.utc) - timedelta(hours=36 if day else 168)
    arts = [a for a in load(ART, {}).values() if datetime.fromisoformat(a["published"]) > cut]
    if everything:
        return sorted(arts, key=lambda a: (a["score"], a["published"]), reverse=True)[:150]
    cap = CFG.get("per_sector_day", 4) if day else CFG.get("per_sector_week", 6)
    def pick(ms):
        by = {}
        for a in arts:
            sec = sector_of(a)
            if not sec and a["score"] >= ms + 4: sec = "Other major stories"
            if sec and a["score"] >= ms: by.setdefault(sec, []).append(dict(a, sector=sec))
        return by
    ms = CFG.get("min_score", 11)
    by = pick(ms)
    if sum(len(v) for v in by.values()) < 8: by = pick(ms - 4)
    out = []
    for s in [n for n, _ in SECTORS] + ["Other major stories"]:
        out += sorted(by.get(s, []), key=lambda a: (a["score"], a["published"]), reverse=True)[:cap]
    return out

@app.route("/api/news")
def news():
    return jsonify(major_news(request.args.get("range") == "day", bool(request.args.get("all"))))

@app.route("/api/markets")
def markets():
    key = "week" if request.args.get("range") == "week" else "day"
    rows = [dict(symbol=s, name=m["name"], group=m["group"], last=m["last"], day=m["day"], week=m["week"])
            for s, m in load(MKT, {}).items()]
    order = {}
    for w in CFG["watchlist"]: order.setdefault(w["g"], len(order))
    rows.sort(key=lambda r: (order.get(r["group"], 99), -abs(r[key] or 0)))
    return jsonify(rows)


THEMES = {
 "Deals and capital markets": {"ipo","initial public offering","merger","acquisition","takeover","buyout","private equity","venture capital","venture","business angel","angel investor","funding round","series a","series b","asset manager","asset management","investment bank"},
 "Central banks and macro": {"rate decision","interest rate","rate cut","rate rise","central bank","inflation","recession"},
 "Geopolitics and trade": {"war","sanctions","tariff","opec"},
}

def first_sentence(t, n=40):
    s = re.split(r"(?<=[.!?])\s", t or "")[0]
    w = s.split()
    return " ".join(w[:n]) + ("..." if len(w) > n else "")

def wc(s): return len(s.split())

def sgn(v): return f"{v:+.2f}%"

def build_overview(rng):
    hours = 36 if rng == "day" else 168
    span = "over the last day" if rng == "day" else "over the past seven days"
    key = "day" if rng == "day" else "week"
    cut = datetime.now(timezone.utc) - timedelta(hours=hours)
    arts = [x for x in load(ART, {}).values() if datetime.fromisoformat(x["published"]) > cut]
    arts.sort(key=lambda x: (x["score"], x["published"]), reverse=True)
    mk = load(MKT, {})
    rows = [(s, m) for s, m in mk.items() if m.get(key) is not None]
    units = []
    eq = [(m["name"], m[key]) for s, m in rows if m["group"] == "Equities"]
    if eq:
        eq.sort(key=lambda x: x[1])
        avg = sum(v for _, v in eq) / len(eq)
        units.append(("m", f"Markets {span}: the eight tracked equity indices moved {sgn(avg)} on average, with {eq[-1][0]} strongest ({sgn(eq[-1][1])}) and {eq[0][0]} weakest ({sgn(eq[0][1])})."))
    for sym, txt in (("BZ=F", "Brent crude"), ("GC=F", "gold"), ("BTC-USD", "Bitcoin")):
        m = mk.get(sym)
        if m and m.get(key) is not None:
            units.append(("m", f"{txt.capitalize()} changed {sgn(m[key])} to {m['last']:,}."))
    y = mk.get("^TNX")
    if y and y.get(key) is not None:
        units.append(("m", f"The US 10-year yield stands at {y['last']}%, a relative move of {sgn(y[key])}."))
    firms = [(m["name"], m[key]) for s, m in rows if m["group"] in ("Investment Banks", "Asset Managers & Private Equity", "Oil & Gas Majors", "Mining & Metals")]
    firms.sort(key=lambda x: abs(x[1]), reverse=True)
    if len(firms) >= 2:
        units.append(("m", f"Among the firms tracked, {firms[0][0]} ({sgn(firms[0][1])}) and {firms[1][0]} ({sgn(firms[1][1])}) moved the most."))
    used = set()
    for theme, tags in THEMES.items():
        hit = [x for x in arts if tags & set(x["tags"]) and x["link"] not in used][:2]
        for x in hit:
            used.add(x["link"])
            units.append(("n", f"{theme}: {x['title']} ({x['source']}). {first_sentence(x['summary'])}".strip()))
    for x in [x for x in arts if x["link"] not in used][:8]:
        units.append(("x", f"Also ranking highly: {x['title']} ({x['source']}). {first_sentence(x['summary'])}".strip()))
    out, total = [], 0
    for kind, t in units:
        if total >= 210: break
        if total + wc(t) <= 250:
            out.append((kind, t)); total += wc(t)
    paras = [" ".join(t for k, t in out if k == "m")] + [" ".join(t for k, t in out if k in "nx")]
    paras = [p for p in paras if p]
    if not paras: paras = ["No data yet. Click Refresh now and wait a minute."]
    return dict(paragraphs=paras, words=sum(wc(p) for p in paras), articles=len(arts))

@app.route("/api/overview")
def overview():
    return jsonify(build_overview(request.args.get("range", "day")))


def esc(t):
    return html.escape((t or "").encode("cp1252", "replace").decode("cp1252"), quote=False)

def build_pdf():
    from zoneinfo import ZoneInfo
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Flowable, KeepTogether

    class Spark(Flowable):
        def __init__(s, m, w=250, h=70):
            super().__init__(); s.m, s.w, s.h = m, w, h
        def wrap(s, *a): return s.w, s.h + 16
        def draw(s):
            v = s.m["closes"][-60:]
            if len(v) < 2: return
            c, lo, hi = s.canv, min(v), max(v)
            r = (hi - lo) or 1
            pts = [(i * s.w / (len(v) - 1), (x - lo) / r * s.h) for i, x in enumerate(v)]
            c.setStrokeColor(colors.HexColor("#0f8f86")); c.setLineWidth(1.4)
            p = c.beginPath(); p.moveTo(*pts[0])
            for q in pts[1:]: p.lineTo(*q)
            c.drawPath(p, stroke=1, fill=0)
            c.setFont("Helvetica", 7); c.setFillColor(colors.grey)
            c.drawString(0, s.h + 5, esc(f"{s.m['name']}: low {lo:g}, high {hi:g}, last {v[-1]:g}"))

    ss = getSampleStyleSheet(); B = ss["BodyText"]
    sm = ParagraphStyle("sm", parent=B, fontSize=8, textColor=colors.grey)
    now = datetime.now(ZoneInfo(CFG["timezone"]))
    S = [Paragraph("Market Brief - offline copy", ss["Title"]),
         Paragraph(f"Snapshot taken {now:%A %d %B %Y, %H:%M} ({CFG['timezone']}). Data is frozen at that time. Links open the original articles when you are online.", sm)]
    for rng, name in (("day", "Daily overview"), ("week", "Weekly overview")):
        S.append(Paragraph(name, ss["Heading2"]))
        for p in build_overview(rng)["paragraphs"]: S += [Paragraph(esc(p), B), Spacer(1, 4)]
    S.append(Paragraph("Major news by sector (past 7 days)", ss["Heading2"]))
    last = None
    for a in major_news(False):
        if a["sector"] != last:
            last = a["sector"]; S.append(Paragraph(esc(last), ss["Heading3"]))
        url = html.escape(a["link"], quote=True)
        when = datetime.fromisoformat(a["published"]).astimezone(ZoneInfo(CFG["timezone"]))
        S.append(KeepTogether([
            Paragraph(f'<link href="{url}" color="#0b5cad"><b>{esc(a["title"])}</b></link>', B),
            Paragraph(esc(f"{a['source']} | {when:%d %b %H:%M}"), sm),
            Paragraph(esc(a["summary"] or "No summary in feed."), B), Spacer(1, 6)]))
    mk = load(MKT, {})
    S.append(Paragraph("Markets", ss["Heading2"]))
    groups = {}
    for s, m in mk.items(): groups.setdefault(m["group"], []).append(m)
    pc = lambda v: "-" if v is None else f"{v:+.2f}%"
    for g, ms in groups.items():
        rows = [[esc(g), "Last", "Day", "Week"]] + [[esc(m["name"]), f"{m['last']:,}", pc(m["day"]), pc(m["week"])] for m in ms]
        st = [("FONTSIZE", (0, 0), (-1, -1), 8), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
              ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey), ("ALIGN", (1, 0), (-1, -1), "RIGHT")]
        for i, m in enumerate(ms, 1):
            for col, key in ((2, "day"), (3, "week")):
                if m[key] is not None:
                    st.append(("TEXTCOLOR", (col, i), (col, i), colors.HexColor("#0a7c3e" if m[key] >= 0 else "#c0392b")))
        t = Table(rows, colWidths=[230, 90, 80, 80]); t.setStyle(TableStyle(st))
        S += [t, Spacer(1, 8)]
    keys = [k for k in ("^GSPC", "^IXIC", "^FTSE", "^GDAXI", "EURUSD=X", "^TNX", "BZ=F", "GC=F", "BTC-USD", "XLF") if k in mk]
    if keys:
        S.append(Paragraph("Key charts (last 60 trading days)", ss["Heading2"]))
        sp = [Spark(mk[k]) for k in keys]
        if len(sp) % 2: sp.append("")
        t = Table([sp[i:i + 2] for i in range(0, len(sp), 2)], colWidths=[265, 265], rowHeights=[95] * ((len(sp) + 1) // 2))
        S.append(t)
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=30, rightMargin=30, topMargin=30, bottomMargin=30).build(S)
    return buf.getvalue()

@app.route("/api/snapshot.pdf")
def snapshot():
    name = datetime.now().strftime("market-brief-%Y-%m-%d.pdf")
    return send_file(io.BytesIO(build_pdf()), mimetype="application/pdf", as_attachment=True, download_name=name)

@app.route("/api/history")
def history():
    m = load(MKT, {})
    return jsonify({s: m[s] for s in request.args.get("symbols", "").split(",") if s in m})

@app.route("/api/status")
def status(): return jsonify(load(DATA / "status.json", {}))

@app.route("/api/refresh", methods=["POST"])
def manual():
    refresh_all(); return jsonify(ok=True)

@app.route("/")
def index(): return app.send_static_file("index.html")

if __name__ == "__main__":
    sch = BackgroundScheduler(timezone=CFG["timezone"])
    sch.add_job(refresh_all, "cron", hour=CFG["refresh_hour"], minute=CFG["refresh_minute"])
    sch.start()
    if not ART.exists() or time.time() - ART.stat().st_mtime > 12 * 3600:
        threading.Thread(target=refresh_all, daemon=True).start()
    print("Open http://127.0.0.1:5050")
    app.run(host="0.0.0.0", port=5050)
