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
   - Update `requirements` if the `dompower` dependency changed (ensure the new version is already published to PyPI)

3. **Commit and push:**
   ```bash
   git add custom_components/dominion_energy/manifest.json
   git commit -m "Bump version to X.Y.Z"
   git push
   ```

4. **Wait for CI to pass** on the version bump commit.

5. **Create a GitHub release:**
   ```bash
   gh release create vX.Y.Z --title "vX.Y.Z" --notes "Release notes here"
   ```
   Or create via the GitHub UI at https://github.com/YeomansIII/ha-dominion-energy/releases/new.

6. **The `Release` workflow runs automatically:**
   - Checks out the repo
   - Updates `manifest.json` version from the git tag (ensures tag and manifest match)
   - Creates `dominion_energy.zip` from `custom_components/dominion_energy/`
   - Attaches the ZIP to the GitHub release

7. **Verify** the release asset is attached at https://github.com/YeomansIII/ha-dominion-energy/releases.

## Versioning

Follow [semver](https://semver.org/):
- **Patch** (1.3.x): Bug fixes, dependency bumps
- **Minor** (1.x.0): New sensors, new features
- **Major** (x.0.0): Breaking changes (config flow changes, removed sensors)

## Release Order

If both `dompower` and `ha-dominion-energy` need releases:

1. Release `dompower` first and wait for PyPI publish to complete
2. Update `manifest.json` with the new `dompower==X.Y.Z` requirement
3. Release `ha-dominion-energy`

This ensures Home Assistant can resolve the `dompower` dependency when users install the update.
