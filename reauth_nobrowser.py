"""
One-off re-auth that does NOT auto-open a browser (open_browser=False).
This prevents the race where the auto-opened window completes the flow with
the wrong brand account. We print the URL, complete it in a controlled tab,
and save the token in the EXACT production format via _save_token.

Usage: python -u reauth_nobrowser.py channel_9
"""
import sys
import os
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402
from src.youtube_uploader import SCOPES, _save_token  # noqa: E402

channel_id = sys.argv[1]
cred = Path(f"credentials/{channel_id}_client_secret.json")
token = Path(f"tokens/{channel_id}_token.json")

if not cred.exists():
    print(f"ERROR: missing {cred}")
    sys.exit(1)

if token.exists():
    token.unlink()
    print(f"Removed token: {token}")

flow = InstalledAppFlow.from_client_secrets_file(str(cred), SCOPES)
creds = flow.run_local_server(port=0, open_browser=False)
_save_token(creds, token)
print(f"SUCCESS - fresh token saved to {token}")
