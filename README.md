# Valleybound market history

The private repository behind Valleybound's optional **history server**:
an hourly GitHub Action fetches the [Pax Dei community market
feed](https://paxdei.gaming.tools/market/api) and archives one snapshot
per observation, in Valleybound's snapshot format, so the app can sync the
hours the player's PC was off — up to 24 real points a day for its price
charts.

Nothing personal lives here: the repository holds only public market
prices (the feed's own data), and the commits are made by the
`github-actions[bot]` identity. The app reads it with a fine-grained
read-only token (Contents: Read-only, this repository only) that each
player creates and keeps on their own machine.

## Layout

- `collector/collect_market.py` — the collector: fetches the feed's
  index, item metadata and the eight compared home-valley zone files,
  aggregates them with rules identical to the app's own refresh, and
  writes `data/<collected>.json` (one file per observation, named for the
  epoch the feed itself carries — unchanged data produces no new file,
  corrected data replaces its own file). Anything older than 10 days is
  pruned. `data/manifest.json` lists every kept observation with the
  checksum of its exact bytes. A fetch failure exits non-zero and touches
  nothing.
- `collector/crafting.json` — the recipe book used for the
  craft-to-sell closure (a copy of the app's; update it with the app).
- `.github/workflows/collect-market.yml` — the hourly schedule. Commits
  only when something changed; opens an issue on failure so a breakage is
  never silent.

## Reading from the app

In Valleybound 1.9.4, Market Watch's **History server** card: the
repository as `owner/name`, a fine-grained GitHub token with only
*Contents: Read-only* access to this repository, and the *keep prices
current* toggle. The app reads through GitHub's contents API pinned to one
revision per sync, verifies every file against the manifest's checksum
and validates it as a real snapshot before it is written; the live prices
are never rolled backward, and an unreachable server falls back to the
direct community feed.

## Token (per player)

github.com → Settings → Developer settings → Personal access tokens →
Fine-grained tokens → Generate new token: Repository access = only this
repository; Permissions → Repository permissions → Contents = Read-only.
Paste it into the app's History server card. It never leaves the player's
machine (Windows Credential Manager on Windows).
