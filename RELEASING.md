# Releasing ha-dominion-energy

## Overview

Creating a GitHub release triggers the `Release` workflow, which attaches a `dominion_energy.zip` asset.

**HACS does not use that asset.** Using it requires `zip_release` and `filename` keys in `hacs.json`, and this repo sets neither. HACS instead downloads the repository contents at the release tag and copies `custom_components/dominion_energy/`. The ZIP is currently decorative — for manual installation, or in case `hacs.json` gains those keys later.

The practical consequence: **the version in `manifest.json` must be correct in the commit you tag.** The workflow rewrites the manifest inside its own checkout when building the ZIP, but never commits that back, so it has no effect on what HACS installs.

### Why releases matter here

Without any releases, HACS tracks the default branch and records the **commit SHA** as the installed version. It then builds its download URL as `archive/refs/heads/<version>.zip` — which resolves for a branch name but 404s for a SHA:

```
Got status code 404 when trying to download
https://github.com/<owner>/ha-dominion-energy/archive/refs/heads/16f130d.zip
```

Tagged releases avoid this: HACS fetches `archive/refs/tags/vX.Y.Z.zip`, which resolves. Cut releases rather than relying on branch tracking.

## Steps

1. **Ensure CI is green on `main`** (Hassfest, Type Check, Ruff).

2. **Update `manifest.json`:**
   - Bump `version` to the new version
   - Update `requirements` if the `dompower` dependency changed (ensure the release asset it names already exists — see [Release Order](#release-order))

   Bump `version` in `pyproject.toml` to match. Nothing installs that package — it exists to carry the test and lint configuration — so a stale value breaks nothing, which is exactly why it silently fell two releases behind. Keep the two in step so neither has to be treated as the untrustworthy one.

3. **Commit and push:**
   ```bash
   git add custom_components/dominion_energy/manifest.json pyproject.toml
   git commit -m "Bump version to X.Y.Z"
   git push
   ```

   Add `uv.lock` and `.pre-commit-config.yaml` to that `git add` if the
   `dompower` URL moved.

4. **Wait for CI to pass** on the version bump commit.

5. **Create a GitHub release:**
   ```bash
   gh release create vX.Y.Z --title "vX.Y.Z" --notes "Release notes here"
   ```
   Or create via the GitHub UI at `https://github.com/<owner>/ha-dominion-energy/releases/new`.

   `<owner>` is whichever repository you are releasing — a fork cuts its own releases, and HACS installs from the repository the user added. The `gh` command above needs no owner: it reads the remote.

6. **The `Release` workflow runs automatically:**
   - Checks out the repo
   - Updates `manifest.json` version from the git tag (ensures tag and manifest match)
   - Creates `dominion_energy.zip` from `custom_components/dominion_energy/`
   - Attaches the ZIP to the GitHub release

7. **Verify** the release asset is attached at `https://github.com/<owner>/ha-dominion-energy/releases`.

## Versioning

Follow [semver](https://semver.org/), read at this repo's scale:
- **Patch** (1.3.x): Bug fixes, hardening, dependency bumps, internal services
  and repair flows. **This is the default** — reach for it unless one of the
  cases below clearly applies.
- **Minor** (1.x.0): A user-facing feature someone would choose to upgrade
  *for*. Not a measure of how much changed.
- **Major** (x.0.0): Breaking changes — config flow changes, removed sensors,
  a renamed statistic ID, or a `dompower` release whose public surface moved
  under us. 2.0.0 was cut for the last of those: `dompower` 0.4.0 renamed
  `BillForecast.current_period_end` and made `last_bill` optional, which
  changed a diagnostics key here and required matching fixes on both sides.

## Release Order

`dompower` is installed from a wheel attached to a GitHub release on the fork
at `negative-video/dompower`, **not from PyPI** — that name belongs to
upstream. See `RELEASING.md` in that repository, and the "Installing the forked
`dompower`" section of `CLAUDE.md` here for why the URL is written the way it
is.

If both `dompower` and `ha-dominion-energy` need releases:

1. Release `dompower` first and confirm the wheel is attached to the release
2. Point all three files at the new release, all naming the same version:
   - `custom_components/dominion_energy/manifest.json` — the release asset URL
     plus the `#dompower==X.Y.Z` fragment
   - `pyproject.toml`, `[tool.uv.sources]` — `tag = "vX.Y.Z"`, then `uv lock`
   - `.pre-commit-config.yaml` — `git+https://...@vX.Y.Z` in mypy's
     `additional_dependencies`
3. Release `ha-dominion-energy`

Home Assistant installs the wheel while local work and CI clone the tag; the
release workflow builds that wheel from the tagged commit, so the two agree as
long as both name the same tag. `CLAUDE.md` explains why they differ.

Nothing resolves until the tag and its release asset exist: `uv lock` fails on
a missing tag and Home Assistant 404s on a missing asset, which is the intended
order-of-operations check.
