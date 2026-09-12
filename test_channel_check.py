"""The channel's own listing stops a second upload of the same clip.

Run from inside a channel repo:  python test_channel_check.py
No network: the channel listing cache is written by hand.
"""
import importlib, json, os, pathlib, sqlite3, sys, time

repo = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(repo))
os.environ["DB_PAGE_ID"] = "test_channel_check_tmp"

db = importlib.import_module("src.db")
runner = importlib.import_module("src.channel_runner")
yt_index = importlib.import_module("src.youtube_channel_index")

db_file = repo / "data" / "test_channel_check_tmp.db"
if db_file.exists():
    db_file.unlink()
db.init_db()

CH = {"id": "channel_test", "tiktok_username": "someone", "youtube_channel_name": "Test"}
cache = repo / "data" / "channel_test_yt_titles.json"
cache.write_text(json.dumps({
    "youtube_channel": "UCtest",
    "fetched_at": int(time.time()),
    "titles": [yt_index.normalise("Meeting a Hermit Crab #family")],
}, ensure_ascii=False), encoding="utf-8")

results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))

# 1. a clip whose title is already on the channel must not be uploaded again
tried = set()
video = {"id": "111", "title": "Meeting a Hermit Crab #family", "url": "u", "timestamp": 1}
out = runner._skip_if_already_on_channel(CH, video, tried)
check("a title already on the channel is not uploaded again", out is None, repr(out))
check("and that clip is excluded from the rest of the run", "111" in tried, str(tried))

con = sqlite3.connect(str(db_file))
row = con.execute("SELECT status, error_message FROM posted_videos WHERE tiktok_video_id='111'").fetchone()
con.close()
check("it is recorded so later runs skip it too",
      bool(row) and row[0] == "skipped", str(row))

# 2. a genuinely new clip passes straight through
video2 = {"id": "222", "title": "A brand new morning #dailylife", "url": "u", "timestamp": 1}
out2 = runner._skip_if_already_on_channel(CH, video2, set())
check("a clip that is not on the channel still uploads", out2 is video2, repr(out2))

# 3. nothing to pick stays nothing to pick
check("no candidate stays no candidate",
      runner._skip_if_already_on_channel(CH, None, set()) is None)

# 4. if the listing cannot be read the run is not blocked
cache.unlink()
orig = yt_index.load_index
yt_index.load_index = lambda *a, **k: None
try:
    out4 = runner._skip_if_already_on_channel(CH, video2, set())
    check("an unreadable channel listing does not block the run", out4 is video2, repr(out4))
finally:
    yt_index.load_index = orig

db_file.unlink(missing_ok=True)

failed = [r for r in results if not r[1]]
for name, ok, detail in results:
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "\n        -> " + detail))
print(f"\n{len(results) - len(failed)}/{len(results)} passed  ({repo.name})")
sys.exit(1 if failed else 0)
