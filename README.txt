DXM SportsGameOdds Bot

Fresh rebuild using python-telegram-bot v21 async polling.

What it does
- pulls upcoming events from SportsGameOdds v2 /events
- scans core markets only: moneyline, spread, total
- compares each line's fair odds against best available bookmaker odds
- sends only fresh bets
- keeps one pick per game max
- supports /scan /openbets /stats /win /loss /push
- runs autoscan on an interval

Deploy
1. Put these files in your GitHub repo.
2. Railway:
   - New Project -> Deploy from GitHub
   - Add all env vars from .env.example
   - Start command is handled by Procfile
3. First time in Telegram, send /start to the bot.
4. Then use /scan.

Notes
- This version focuses on stable core markets instead of props.
- Settlement is manual by bet ID so it stays simple and reliable.
- If you want, you can later add auto grading for finalized events.
