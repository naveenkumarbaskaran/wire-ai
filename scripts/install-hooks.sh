#!/usr/bin/env bash
# Install WIRE git hooks into .git/hooks/
# Run once after cloning: bash scripts/install-hooks.sh

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS_DIR="$REPO/.git/hooks"

cp "$REPO/scripts/pre-push.hook" "$HOOKS_DIR/pre-push"
chmod +x "$HOOKS_DIR/pre-push"
chmod +x "$REPO/scripts/regression.sh"

echo "✓  pre-push hook installed → .git/hooks/pre-push"
echo "   Regression suite will run before every git push."
echo "   To bypass: git push --no-verify"
