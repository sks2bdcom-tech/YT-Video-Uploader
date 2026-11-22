# YT Video Uploader Bot

Send an .m3u8 link (optionally followed by a title) to the Telegram bot. It downloads the video with ffmpeg and uploads it to a fixed YouTube channel as unlisted.

## Setup
1. Create the bot with @BotFather and get your user ID from @userinfobot.
2. Enable YouTube Data API v3 in Google Cloud and create a Desktop OAuth client. Publish the consent screen.
3. Run `python get_token.py client_secret.json` and log in with the channel owner account.
4. Push this folder to GitHub, then create a Render Web Service (Docker) or use `render.yaml`.
5. Set the env vars listed in `.env.example`.
