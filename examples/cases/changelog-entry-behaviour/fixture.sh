# Seeds a changelog with an empty Unreleased section above one release.
cat > CHANGELOG.md <<'MD'
# Changelog

## Unreleased

## 1.3.0

- Added the `--junit` flag.
MD
