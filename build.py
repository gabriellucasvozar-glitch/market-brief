"""Refresh the data and write the static files GitHub Pages serves from docs/."""
import json, shutil
from datetime import datetime, timezone
from pathlib import Path
import app

DOCS = Path(__file__).parent / "docs"
OUT = DOCS / "data"; OUT.mkdir(parents=True, exist_ok=True)
PAGES = {
    "news-day": "/api/news?range=day", "news-week": "/api/news?range=week", "news-all": "/api/news?range=week&all=1",
    "markets-day": "/api/markets?range=day", "markets-week": "/api/markets?range=week",
    "overview-day": "/api/overview?range=day", "overview-week": "/api/overview?range=week",
    "status": "/api/status",
}

app.refresh_all()
client = app.app.test_client()
for name, url in PAGES.items():
    (OUT / f"{name}.json").write_bytes(client.get(url).data)
if app.MKT.exists():
    shutil.copyfile(app.MKT, OUT / "history.json")
try:
    (DOCS / "snapshot.pdf").write_bytes(client.get("/api/snapshot.pdf").data)
except Exception as ex:
    print("PDF skipped:", ex)
(OUT / "meta.json").write_text(json.dumps({"updated": datetime.now(timezone.utc).isoformat()}))
print("Built", datetime.now(timezone.utc).isoformat(), json.loads((OUT / "status.json").read_text()))
