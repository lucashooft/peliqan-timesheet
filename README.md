# Peliqan Script Sync

A starter repo for syncing Peliqan Data App scripts (what Peliqan's API calls
"interfaces") with git: version control, code review, and an automatic push
to Peliqan on every merge to `main`. Use this repo as the template for any
new project that needs the same setup.

## Creating your own repo from this template

This repo is meant to be copied, not extended in place. Each Peliqan account
(or project) gets its own repo made from it, so the `scripts/` it fills up
with and the API token it syncs against stay separate.

1. **Create the repo.** On this repo's GitHub page, click **Use this
   template** → *Create a new repository*, then pick an owner and name. You
   get a fresh repo with all the files below and no shared history.

   (No template button? The setting lives on the source repo: *Settings →
   General → Template repository*. Without it, `git clone` this repo, delete
   `.git`, and `git init` fresh instead — same result, more steps.)

2. **Let the init workflow run.** `.github/workflows/init.yml` fires on the
   first push to `main` in your new repo and does two things: adds the shared
   Claude skills repo as a submodule at `.claude`, and deletes itself. Check
   the *Actions* tab; if nothing ran, Actions is disabled for the repo and you
   need to enable it (*Settings → Actions → General*).

   The workflow commits as `github-actions[bot]` and pushes, so `main` will be
   one or two commits ahead of whatever you cloned. Pull before your next
   push.

3. **Clone your new repo with the submodule:**

   ```powershell
   git clone --recurse-submodules https://github.com/<you>/<your-repo>.git
   ```

   Already cloned without it (empty `.claude/`)? Fill it in:

   ```powershell
   git submodule update --init --recursive
   ```

4. Continue with [Setup](#setup) below — token, then first fetch.

## Updating the `.claude` submodule

`.claude` is a submodule, so your repo doesn't store its files: it stores a
single commit pointer into
[`peliqan-agents-skills`](https://github.com/lucashooft/peliqan-agents-skills).
New skills landing in that repo don't reach yours until you move the pointer.

Pull the latest `main` of the skills repo and commit the new pointer:

```powershell
git submodule update --remote .claude
git add .claude
git commit -m "Update .claude skills submodule"
git push
```

`git pull` in your repo updates the *pointer* to whatever a teammate
committed, but doesn't check the submodule out at it. After any pull that
touches `.claude`, run:

```powershell
git submodule update --init --recursive
```

Or set it once and forget it: `git config submodule.recurse true` makes
`git pull` update submodule contents automatically.

Notes:

- `git status` showing `.claude` as *modified: new commits* means the pointer
  and the checked-out commit disagree — either commit the move (you ran
  `--remote` deliberately) or discard it with
  `git submodule update -- .claude`.
- Don't commit inside `.claude` from your repo. Changes to the skills
  themselves belong in the `peliqan-agents-skills` repo; pull them back here
  as a pointer update.

## Setup

1. **Install dependencies** in your clone (see step 3 above).

   ```powershell
   pip install -r requirements.txt
   ```

2. **Get a Peliqan API token.** In Peliqan: `Admin > Security settings > API token`.

3. **Set it locally**, for running the scripts by hand:

   ```powershell
   $env:PELIQAN_API_TOKEN = "your-token-here"
   ```

   (bash: `export PELIQAN_API_TOKEN=your-token-here`)

4. **Add it as a repo secret**, for CI: GitHub repo → *Settings → Secrets and
   variables → Actions → New repository secret* → name it
   `PELIQAN_API_TOKEN`. This is a separate value from step 3: setting one
   does not set the other, even though they share a name.

5. **Pull down everything Peliqan currently has:**

   ```powershell
   python fetch_peliqan_data_apps.py
   ```

   This creates `scripts/`, one `<id>_<name>.py` file per Data App, plus
   `scripts/.manifest.json` tracking what's in sync.

6. Commit and push `scripts/` to `main`. From here on, CI keeps Peliqan and
   `main` in sync automatically for anything pushed through git.

## What's in this repo

| File | Purpose |
|---|---|
| `fetch_peliqan_data_apps.py` | Pulls every Data App script down from Peliqan into `scripts/`. |
| `push_peliqan_data_apps.py` | Pushes local edits in `scripts/` back up to Peliqan. |
| `peliqan_common.py` | Shared helpers (API calls, hashing, manifest I/O). Not run directly. |
| `requirements.txt` | Everything needed to run any script in this repo: `requests` (for fetch/push themselves) plus `peliqan`/`streamlit` (for running a *fetched* Data App script locally via its dev shim). |
| `.github/workflows/sync-peliqan-scripts.yml` | CI job: runs `push_peliqan_data_apps.py` on every push to `main` that touches `scripts/**.py`. |
| `.github/workflows/init.yml` | One-shot CI job in a repo made from this template: adds the `.claude` skills submodule, then deletes itself. |
| `.claude/` | Submodule of shared Claude Code skills, added by `init.yml`. Not present in the template itself. |
| `scripts/` | Generated on first fetch. Contains one `.py` per Data App plus `.manifest.json`. Don't hand-edit the manifest. |

`scripts/` itself isn't part of this template: it's created the first time
you run `fetch_peliqan_data_apps.py` against your own Peliqan account, and
will hold that account's scripts specifically.

## How the sync actually works

Nothing here is a real diff or merge: both scripts just compare **sha256
hashes** and decide overwrite, push, or refuse based on equality.

Four values matter, tracked per script:

- **On-disk file**: what's in `scripts/<id>_<name>.py` right now, *including*
  a local-dev shim (~15 lines) that fetch prepends so the script can also run
  standalone outside Peliqan (needs `PELIQAN_API_KEY`, not the same as
  `PELIQAN_API_TOKEN` above: see the shim itself in `peliqan_common.py`).
- **Peliqan's live script**: the `raw_script` field on the interface, fetched
  fresh over the API, *without* the shim.
- **Manifest's memory of each of the above** (`local_hash`, `remote_hash` in
  `scripts/.manifest.json`): what they *were* as of the last successful
  fetch or push.

`fetch` only ever compares the on-disk file to the manifest's memory of it:
if they still match, it's safe to overwrite with whatever's live (this is the
only path that actually pulls remote edits down). If they don't match, you
have an unpushed local edit, so it leaves your file alone and saves Peliqan's
current version next to it as `<name>.remote.py` instead.

`push` compares Peliqan's live script to the manifest's memory of it. If
Peliqan hasn't moved since the last sync, your edit goes up normally. If it
has moved, push checks one more thing: does Peliqan's current content happen
to already equal what you're about to send? If so, it's harmless: both
sides converged independently and it just refreshes the manifest. Only a
genuine three-way divergence (local changed, remote changed, to different
content) is treated as a conflict and refused.

## Everyday workflow

- **Editing an existing script:** edit the file in `scripts/`, commit, push to
  `main`. CI runs `push_peliqan_data_apps.py` for you and commits the updated
  manifest back.
- **Testing a change on a branch, before merging:** CI only triggers on
  `main`, so run `python push_peliqan_data_apps.py` yourself to get it live
  early.
- **Someone edited a script directly in the Peliqan UI:** run
  `python fetch_peliqan_data_apps.py`: this is the only direction that pulls
  Peliqan → git, and nothing does it automatically.
- **A new Data App was created in Peliqan:** `fetch_peliqan_data_apps.py` is
  also what creates its file in `scripts/` for the first time.
- **Genuine conflict** (a `<name>.remote.py` appears): open both files, decide
  the final content by hand, write it into the real file, delete the
  `.remote.py`, then `python push_peliqan_data_apps.py --force`. Plain push
  will refuse again even after a manual merge: force is what tells it you've
  already reconciled.

## Useful flags

```powershell
python fetch_peliqan_data_apps.py --force        # overwrite local edits with what's live
python fetch_peliqan_data_apps.py --skip-scripts  # just the summary JSON/CSV, no scripts/
python push_peliqan_data_apps.py  --dry-run       # show what would be pushed, change nothing
python push_peliqan_data_apps.py  --force         # push even if Peliqan drifted since last sync
```

Both also accept `--scripts-dir` (default `scripts`) and `--base-url`
(default `https://app.eu.peliqan.io`, override via `PELIQAN_BASE_URL` if
you're on a different Peliqan instance).

## Gotchas

- **CI writes back to `main`** after every successful sync (the manifest
  commit). That advances `main` past whatever you have locally, so your next
  local push may get rejected until you pull first: expect a
  `git pull` before committing your next script edit, or you'll pick up an
  extra merge commit.
- **`requirements.txt` covers this repo's own tooling and the local-dev shim
  only** (`requests`, `peliqan`, `streamlit`): not whatever a specific
  fetched script imports on its own. If a script has its own `import pandas`
  or similar, install that yourself before running it locally; nothing here
  tracks or installs per-script dependencies automatically.
- **Don't hand-edit `scripts/.manifest.json`** outside of what fetch/push
  write. A stale or incorrect hash in there causes false conflicts (or false
  "nothing to sync") on the next run.
