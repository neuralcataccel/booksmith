# Working on Booksmith

Read README.md before changing behavior. Preserve source ebooks, immutable job settings and completed media. Keep credentials, runtime state, model weights, source books and generated audio outside Git. Never write a Plex database directly. Use isolated stores and fake providers for tests. Run `python -m pytest tests -q` for behavior changes and frontend syntax/asset checks for web changes. Commit validated milestones; do not force-push shared history.
