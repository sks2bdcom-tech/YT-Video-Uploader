"""Run once locally to get a YouTube refresh token for the fixed channel.
pip install google-auth-oauthlib ; python get_token.py client_secret.json
Log in with the Google account that owns the target channel."""
import sys
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], ["https://www.googleapis.com/auth/youtube.upload"])
c = flow.run_local_server(port=0, access_type="offline", prompt="consent")
print("YT_CLIENT_ID=", c.client_id); print("YT_CLIENT_SECRET=", c.client_secret); print("YT_REFRESH_TOKEN=", c.refresh_token)
