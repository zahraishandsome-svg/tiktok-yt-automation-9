"""One TikTok video, one upload per channel — whatever the format.

Runs against a repo's real src/db.py. Pass the repo path as argv[1].
With argv[2] == "control" it expects the OLD behaviour (used to prove the test
can tell the two versions apart).
"""
import importlib.util, os, sqlite3, sys, tempfile, shutil, pathlib

repo = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
control = len(sys.argv) > 2 and sys.argv[2] == "control"

# Point the module at a throwaway database inside the repo's own data dir.
os.environ["DB_PAGE_ID"] = "test_dedup_tmp"
db_file = repo / "data" / "test_dedup_tmp.db"
if db_file.exists():
    db_file.unlink()

spec = importlib.util.spec_from_file_location("dbmod", repo / "src" / "db.py")
db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db)
db.init_db()

CH = "channel_test"
VID = "7777777777777777777"

con = sqlite3.connect(str(db_file))
with con:
    con.execute("""INSERT INTO posted_videos
        (channel_id, tiktok_video_id, format_type, status, youtube_video_id, posted_at)
        VALUES (?, ?, 'longform', 'uploaded', 'yt_longform_1', '2026-09-01')""", (CH, VID))
con.close()

results = []
def check(name, passed, detail=""):
    results.append((name, passed, detail))

# The real-world modes in channels.yaml on 12 Sep 2026.
MODES = ["short_only", "popular_split", "tiered_split", "popular_only",
         "sequence", "dual", "longform_only", "split", "trim_dual"]

for mode in MODES:
    excluded = VID in db.get_posted_video_ids(CH, upload_mode=mode)
    check(f"a video already uploaded as longform is not picked again ({mode})",
          excluded, f"mode={mode} -> excluded={excluded}")

# The escape hatch: a channel may opt back into per-format reposting.
try:
    legacy = db.get_posted_video_ids(CH, upload_mode="short_only",
                                     allow_repost_other_format=True)
    check("allow_repost_other_format brings the old behaviour back",
          VID not in legacy, f"excluded={VID in legacy}")
except TypeError as exc:
    check("allow_repost_other_format brings the old behaviour back", False, str(exc))

db_file.unlink(missing_ok=True)

failed = [r for r in results if not r[1]]
for name, passed, detail in results:
    print(("PASS  " if passed else "FAIL  ") + name + ("" if passed else "\n        -> " + detail))
print(f"\n{len(results) - len(failed)}/{len(results)} passed  ({repo.name}{' CONTROL' if control else ''})")

if control:
    # The point of the control run: the old code must fail these.
    sys.exit(0 if failed else 1)
sys.exit(1 if failed else 0)
