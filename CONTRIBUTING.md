# WIRE — Branch & Release Policy

## Three-Branch Model

```
main ──────────────────────────────────────────────────────▶  active dev
  │
  │  (regression passes + version bump)
  ▼
beta ──────────────────────────────────────────────────────▶  release candidates
  │
  │  (final regression + stable tag)
  ▼
stable ────────────────────────────────────────────────────▶  PyPI production
```

---

## What lives on each branch

### `main` — Active development
**Everything goes here first.**

- New features, bug fixes, refactors
- Experiments and work-in-progress
- Updated tests and docs
- Pre-release bumps (e.g. `1.4.0b1`)

**Users never install from main.** It may break between commits.

```bash
# Work on main as normal
git checkout main
git add ...
git commit -m "feat: ..."
git push origin main          # pre-push regression hook runs automatically
```

### `beta` — Release candidates
**Tested, stabilised code ready for wider testing.**

- Promoted from main when a version is ready for pre-release
- Beta builds published for early adopters: `pip install wire-ai --pre`
- No direct commits — only merges from main via `release.sh`
- Tagged as `v1.4.0b1`, `v1.4.0b2`, etc.

```bash
# Promote main → beta
bash scripts/release.sh promote-to-beta
# → runs regression suite
# → merges main into beta
# → tags v1.4.0-beta
# → pushes origin/beta
```

### `stable` — Production releases
**What `pip install wire-ai` installs.**

- Only receives merges from beta via `release.sh`
- Every push triggers GitHub Actions → builds → publishes to PyPI
- Tagged as clean semver: `v1.4.0`
- Never force-pushed (except rollback)

```bash
# Promote beta → stable + publish PyPI
bash scripts/release.sh promote-to-stable
# → final regression suite
# → merges beta into stable
# → tags v1.4.0
# → pushes origin/stable
# → builds + publishes wire-ai==1.4.0 to PyPI
```

---

## Version Numbering

| Branch | Version format | Example | pip install |
|---|---|---|---|
| `main` | `X.Y.Zb1` during dev | `1.4.0b1` | `pip install wire-ai --pre` |
| `beta` | `X.Y.Zb1`, `X.Y.Zbeta` | `1.4.0b1` | `pip install wire-ai --pre` |
| `stable` | `X.Y.Z` (clean) | `1.4.0` | `pip install wire-ai` |

### Auto version bump

```bash
bash scripts/bump-version.sh patch    # 1.3.0 → 1.3.1  (bug fixes)
bash scripts/bump-version.sh minor    # 1.3.0 → 1.4.0  (new features)
bash scripts/bump-version.sh major    # 1.3.0 → 2.0.0  (breaking changes)
bash scripts/bump-version.sh beta     # 1.3.0 → 1.3.0b1 (beta build)
bash scripts/bump-version.sh 2.0.0   # explicit version
```

The script updates **both** `pyproject.toml` and `src/wire/__init__.py` atomically, commits, and tags.

---

## Release Checklist

### Cut a new patch/minor/major release

```bash
# 1. Make sure main is clean and all tests pass
git checkout main
bash scripts/regression.sh

# 2. Bump version
bash scripts/bump-version.sh minor    # or patch / major

# 3. Promote to beta
bash scripts/release.sh promote-to-beta

# 4. Smoke test beta
pip install wire-ai --pre --upgrade
wire version
wire status

# 5. Promote to stable → auto-publishes to PyPI
bash scripts/release.sh promote-to-stable
```

### Hotfix (bug in stable)

```bash
# 1. Branch off stable
git checkout stable
git checkout -b hotfix/fix-description

# 2. Fix, test, bump patch version
bash scripts/bump-version.sh patch

# 3. Merge to stable directly (skip beta for urgent fixes)
git checkout stable
git merge hotfix/fix-description
bash scripts/release.sh promote-to-stable

# 4. Backport to main
git checkout main
git cherry-pick <commit>
```

---

## CI/CD

| Trigger | Action |
|---|---|
| PR to `main` | Run tests (Python 3.11/3.12/3.13) |
| Push to `main` | Pre-push hook: regression suite |
| Push to `beta` | CI tests (all Python versions) |
| Push to `stable` | CI tests + PyPI publish (if tag matches) |
| Tag `vX.Y.Z` on `stable` | Build + publish to PyPI |

---

## Key rule

> **Never commit directly to `beta` or `stable`.  
> Never push to PyPI manually from main or beta.  
> All changes flow: `main` → `beta` → `stable` → PyPI.**

The `release.sh` script enforces this — it runs the regression suite before every promotion and refuses to proceed on failure.
