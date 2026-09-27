# Daily briefing

A page that rebuilds itself every morning with the day's headlines from the Financial Times, Wall Street Journal, Les Echos, Challenges, the New York Times, Le Monde, L'Équipe, The Athletic, Sports Illustrated and The Hockey News, plus a short summary of the essentials and a timeline of upcoming events.

## How it works

Each morning a GitHub Action runs `build.py`. It reads every publication's public RSS feeds (and falls back to a Google News search of that site when a feed is empty or blocked), keeps the last 36 hours, then sends the headlines to Claude, which writes six one-line essentials and extracts the scheduled events they mention. The result is written to `docs/index.html` and served by GitHub Pages. Each day is also kept in `docs/archive/`.

## Set up (about 10 minutes, free)

1. Create a GitHub account if you don't have one, then create a new repository (e.g. `daily-briefing`) and upload all the files from this folder, keeping the `.github/workflows` folder.
2. Get an API key at console.anthropic.com. In the repository go to Settings, then Secrets and variables, then Actions, and add a secret named `ANTHROPIC_API_KEY`. Without it the page still works but has no summary or upcoming events. Cost is a few cents per day.
3. Go to Settings, then Actions, then General, and under Workflow permissions choose "Read and write permissions".
4. Go to the Actions tab, open "Daily briefing" and click "Run workflow" to build the first page.
5. Go to Settings, then Pages, set Source to "Deploy from a branch", branch `main`, folder `/docs`. Your page appears at `https://<your-username>.github.io/daily-briefing/` after a minute.

It then runs every day at 06:00 Zurich time (05:00 in winter). Change the `cron` line in `.github/workflows/daily.yml` to move it.

Note: a GitHub Pages site on a free account is public. It only shows headlines and links, but if you'd rather keep it private, run it on your own computer instead (below).

## Run on your own computer

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python build.py
open docs/index.html
```

`python build.py --demo` renders placeholder data without any network access, useful for tweaking the design in `template.html`.

## Customising

- Add, remove or re-order publications in `sources.json`. Each source takes one or more feed URLs and a `site` used for the fallback search.
- `max_per_source` and `max_age_hours` control how many headlines appear.
- Set `BRIEFING_TZ` (e.g. `America/New_York`) or `CLAUDE_MODEL` as environment variables in the workflow to change the time zone or model.
- If a source shows "No headlines came through", its feed address has probably changed; find the new one on the publication's RSS page and update `sources.json`.
