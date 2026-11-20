# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-20

### Added
- **Multi-draw support**: Run multiple gift exchanges (family, office, cousins) on a single instance, with one-click roster cloning into a new draw with fresh private tokens.
- **Role-based quotas & household grouping**: Configurable adult/child draw quotas (`0..10`), soft household grouping (`group_key`) with minimum-crossing relaxation when zero crossings is impossible.
- **Participant experience**: Private `/?t=<uuid>` links, gift-box reveal animation (respecting `prefers-reduced-motion`), autosaving wishlists, and anonymous co-giver coordination notes when `co_givers > 1`.
- **Email delivery (Resend)**: Optional household-bundled emails (one message per mailbox with labelled links), empty-wishlist reminder emails, and live template previews.
- **Live non-pairing edits**: Update `event_title`, `budget_note`, `email_language`, and participant email addresses after a draw is locked without resetting pairings.
- **Bilingual UI (`fr-CA` / `en-CA`)**: Full French and English UI parity (246 keys each) plus per-draw `email_language`.
- **Hardened Docker deployment**: Standard and hardened Compose setups (`127.0.0.1:8085`, read-only root filesystem, uid `1000`), rate limiting, token redaction in logs, and automated CI builds to GHCR.
- **Versioning & repo metadata**: Semver `VERSION` file (`1.0.0`), `CHANGELOG.md`, automated GitHub Release workflow, and README status badges.

[1.0.0]: https://github.com/alecc08/gift-exchange/releases/tag/v1.0.0
