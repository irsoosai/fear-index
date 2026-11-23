# Fear & Greed Index

A composite 0-100 fear/greed gauge built from free data, served by GitHub Pages.

- Official score: built each morning from FRED end-of-day data (`scripts/build.py`).
- Live estimate: every ~30 min during market hours from delayed Yahoo Finance quotes (`scripts/live.py`).

## Files

| Path | What it does |
|---|---|
| `index.html` | The web page (no external libraries). Reads `data/fear.json` and `data/live.json`, auto-refreshes every 5 min. |
| `scripts/build.py` | Pulls FRED series, computes 7 components + composite, writes `data/fear.json`. Needs `FRED_API_KEY`. |
| `scripts/live.py` | Pulls intraday quotes, scores them against the same history, writes `data/live.json`. No key needed. |
| `.github/workflows/update.yml` | Runs `build.py` Tue-Sat ~6:17 AM Central, plus a manual Run button. |
| `.github/workflows/live.yml` | Runs `live.py` every 30 min, Mon-Fri, during US market hours, plus a manual Run button. |
| `data/` | Generated JSON lands here (committed by the workflows). |

## Setup

1. New repo on github.com: **+ > New repository**, name `fear-index`, **Public**, no template. Create.
2. Upload: on the empty repo page click **uploading an existing file**, drag in the *contents* of the unzipped `fear-index` folder, commit.
   - macOS Finder hides `.github`. Press **Cmd+Shift+.** in Finder to show it before dragging.
   - Check afterwards that `.github/workflows/update.yml` and `live.yml` exist in the repo. If not, use **Add file > Create new file**, type `.github/workflows/update.yml` as the name, paste the file contents, commit; repeat for `live.yml`.
3. Secret: **Settings > Secrets and variables > Actions > New repository secret**. Name `FRED_API_KEY`, value = your key.
4. Permissions: **Settings > Actions > General > Workflow permissions > Read and write permissions > Save**.
5. First data: **Actions** tab (click "I understand... enable" if prompted) > **Update fear index** > **Run workflow**. Wait for the green check (~1 min). `data/fear.json` now exists.
6. Pages: **Settings > Pages > Build and deployment > Source: Deploy from a branch**, Branch `main`, folder `/ (root)`, Save. After 1-2 min the URL shows at the top of that page: `https://<user>.github.io/fear-index/`.
7. Optional test of live: **Actions > Live intraday estimate > Run workflow** during market hours. Outside market hours it logs "nothing to do", which is expected.

## Notes

- Live estimates appear only after the session opens and disappear when the next morning's official score covers that day.
- GitHub often starts scheduled jobs 5-30 min late; that is normal.
- Scheduled workflows in a public repo are paused after 60 days without repo activity; the bot's data commits normally keep it active. If paused, re-enable on the Actions tab.
- Yahoo's endpoint is unofficial. If `live.yml` starts failing, the official daily score keeps working.
- FRED carries about 10 years of S&P 500 history, so the chart starts around 2018.
