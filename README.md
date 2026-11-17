# EdgeEngine: hosted version

A public website with self-learning picks for NFL, NBA, soccer and college football, updated every 15 minutes.

## How it works

- **Netlify** hosts the page in `site/`. It is deployed once and only again when you change the design.
- **GitHub Actions** runs `engine.py` every 15 minutes. Each run downloads recent results and odds, retrains the model, grades finished picks, prices upcoming games, and publishes `data.json` to a branch named `data` in this repository.
- The page loads `data.json` from that branch and re-checks it every 3 minutes, so visitors see new picks without a redeploy.

Why not run the engine on Netlify itself: Netlify scheduled functions stop after 30 seconds and do not run Python, and on the Free plan each production deploy costs 15 of the 300 monthly credits, which allows about 20 deploys a month. Publishing data separately avoids both limits.

## Files

| File | Purpose |
|---|---|
| `site/index.html` | The website |
| `site/config.js` | One setting: where the page loads its data from |
| `engine.py` | The model and pick generator (Python, no extra packages) |
| `.github/workflows/update.yml` | The 15-minute updater |
| `netlify.toml` | Tells Netlify to publish `site/` |
| `seed_state.b64` | Starting pick ledger, used on the first run only |

## Setup (about 15 minutes)

Use personal accounts, not work ones.

### 1. Put the files on GitHub

1. Create a free account at github.com and create a new repository. Set it to **Public** (the page reads the data file directly from it, and scheduled runs are free on public repositories).
2. On the repository page choose **Add file > Upload files**, drag in everything from this folder, and commit.
3. The updater lives in a hidden folder that drag-and-drop can miss. Check that `.github/workflows/update.yml` exists in the repository. If it does not: **Add file > Create new file**, type `.github/workflows/update.yml` as the name, paste the contents of that file, and commit.

### 2. Point the page at your repository

Open `site/config.js` on GitHub, click the pencil, and replace `YOUR-GITHUB-USERNAME` and `YOUR-REPO-NAME`. Commit.

### 3. Start the updater

1. Open the **Actions** tab. If asked, enable workflows.
2. Select **update-picks**, then **Run workflow**. The first run takes about a minute because it downloads 15 months of results; later runs take a few seconds.
3. When it shows a green check, a branch named `data` exists and contains `data.json`.

If the run fails on the last step with a permission error: **Settings > Actions > General > Workflow permissions**, choose **Read and write permissions**, save, and run it again.

### 4. Put the site on Netlify

1. Create a free account at netlify.com and choose **Add new project > Import an existing project > GitHub**.
2. Pick the repository. Leave the build command empty; the publish directory is `site` (already set in `netlify.toml`). Deploy.
3. Rename the site under **Project configuration** to get an address like `your-name.netlify.app`. That address is what you share.

## Changing things later

- **Design or wording:** edit `site/index.html`. Netlify redeploys on commit (15 credits each).
- **Model:** edit `engine.py`. The next scheduled run uses it. Netlify also redeploys on any commit to the main branch (15 credits), so batch your edits.
- **Update frequency:** change the `cron` line in `update.yml`. GitHub's minimum is every 5 minutes, and it can start runs late when busy.
- **Soccer look-ahead:** `SOCCER_DAYS` near the top of `engine.py`.

## Things to know

- GitHub turns off scheduled workflows in a public repository after 60 days without repository activity and emails you first. Re-enable it in the Actions tab if that happens.
- If Netlify credits run out on the Free plan, the site is paused until the next month. Normal traffic to this page uses very little because the data is served by GitHub.
- Deleting the `data` branch erases the track record and pending picks. The model itself is rebuilt from results on every run.
- The repository is public, so the engine code and data are visible to anyone.

## Before sharing widely

- The scores and odds come from ESPN's public but undocumented scoreboard feed, which also carries DraftKings prices. It can change or be blocked without notice, and republishing it on a public site may not be permitted by their terms. For a site with real traffic, switch to a licensed data and odds provider.
- Publishing betting picks can be regulated depending on where you and your visitors are. Check the rules that apply to you. This is not legal advice.
- Keep the disclaimer in the page footer: probabilities are estimates, no pick is guaranteed.
