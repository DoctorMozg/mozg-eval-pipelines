______________________________________________________________________

## name: release-check description: Checks whether the project is ready to tag a release. Use when the user asks if a version can be tagged or released, or what is missing before a release. Not for writing changelog entries (use changelog-entry).

# Release check

Report ready or not ready, and list what is missing:

- `CHANGELOG.md` has a section for the version, and `## Unreleased` is empty or gone.
- The version in `pyproject.toml` matches the one to be tagged.
- The working tree has no uncommitted changes.
