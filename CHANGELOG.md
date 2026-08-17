# Changelog

<!-- Generated from Git history by scripts/release.sh. Do not edit manually. -->

Release notes for Kensa. Full notes are available on [GitHub Releases](https://github.com/kensa-sh/kensa/releases).

## 0.20.0
### Features
* feat(judge): update default models by @satyaborg in https://github.com/kensa-sh/kensa/pull/108
### Bug Fixes
* fix(setup): keep setup production-backed by @satyaborg in https://github.com/kensa-sh/kensa/pull/111
* fix(providers): clamp legacy observation page limit by @satyaborg in https://github.com/kensa-sh/kensa/pull/114
### Chores
* chore(tests): remove tests that assert on documentation prose by @satyaborg in https://github.com/kensa-sh/kensa/pull/107
* chore(agents): follow Agent Skills guidance by @satyaborg in https://github.com/kensa-sh/kensa/pull/112


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.19.1...v0.20.0

## 0.19.1
### Features
* feat: structured-tool-evidence by @satyaborg in https://github.com/kensa-sh/kensa/pull/81


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.19.0...v0.19.1

## 0.19.0
### Breaking Changes
* feat!: versioned run results by @satyaborg in https://github.com/kensa-sh/kensa/pull/79


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.18.1...v0.19.0

## 0.18.1
### Chores
* chore: publish releases after manual merge by @satyaborg in https://github.com/kensa-sh/kensa/pull/77


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.18.0...v0.18.1

## 0.18.0
### Breaking Changes
* feat!: trial-failure-provenance by @satyaborg in https://github.com/kensa-sh/kensa/pull/75


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.17.0...v0.18.0

## 0.17.0
### Features
* feat: external-run-evidence by @satyaborg in https://github.com/kensa-sh/kensa/pull/72
### Chores
* chore: update changelog for 0.16 releases by @satyaborg in https://github.com/kensa-sh/kensa/pull/73


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.16.1...v0.17.0

## 0.16.1
### Features
* feat: add reliability scoring by @satyaborg in https://github.com/kensa-sh/kensa/pull/70
### Chores
* chore: auto-merge release pull requests by @satyaborg in https://github.com/kensa-sh/kensa/pull/69


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.16.0...v0.16.1

## 0.16.0
### Breaking Changes
* feat!: add conversational evals by @satyaborg in https://github.com/kensa-sh/kensa/pull/66
### Features
* feat: expose trace on case results by @satyaborg in https://github.com/kensa-sh/kensa/pull/67
### Chores
* chore: update changelog for 0.15.0 by @satyaborg in https://github.com/kensa-sh/kensa/pull/64


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.15.0...v0.16.0

## 0.15.0
### Breaking Changes
* feat!: move project config to pyproject.toml by @satyaborg in https://github.com/kensa-sh/kensa/pull/60
### Features
* feat: add repo-aware run diagnosis by @satyaborg in https://github.com/kensa-sh/kensa/pull/61
### Bug Fixes
* fix: streamline eval console output by @satyaborg in https://github.com/kensa-sh/kensa/pull/62
### Chores
* chore: update changelog for 0.13.0 and 0.14.0 by @satyaborg in https://github.com/kensa-sh/kensa/pull/59


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.14.0...v0.15.0

## 0.14.0
### Breaking Changes
* feat!: parallelize Kensa trials by default by @satyaborg in https://github.com/kensa-sh/kensa/pull/57


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.13.0...v0.14.0

## 0.13.0
### Features
* feat: add hard eval timeouts by @satyaborg in https://github.com/kensa-sh/kensa/pull/55
### Chores
* chore: migrate docs by @satyaborg in https://github.com/kensa-sh/kensa/pull/53
* chore: restore docs changelog by @satyaborg in https://github.com/kensa-sh/kensa/pull/54


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.12.0...v0.13.0

## 0.12.0
### Breaking Changes
* feat!: minimize imported trace evidence by @satyaborg in https://github.com/kensa-sh/kensa/pull/47


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.11.1...v0.12.0

## 0.11.1
### Bug Fixes
* fix: streamline redaction setup by @satyaborg in https://github.com/kensa-sh/kensa/pull/45


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.11.0...v0.11.1

## 0.11.0
### Breaking Changes
* feat: mandatory trace redaction before every evidence boundary by @satyaborg in https://github.com/kensa-sh/kensa/pull/43


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.10.3...v0.11.0

## 0.10.3
### Bug Fixes
* fix: avoid trace reads during langfuse connection by @satyaborg in https://github.com/kensa-sh/kensa/pull/41
### Chores
* chore: defer langfuse import scope to cli by @satyaborg in https://github.com/kensa-sh/kensa/pull/40


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.10.2...v0.10.3

## 0.10.2
### Chores
* chore: migrate langfuse imports to sdk by @satyaborg in https://github.com/kensa-sh/kensa/pull/38


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.10.1...v0.10.2

## 0.10.1
### Bug Fixes
* fix: align Langfuse init with eval setup by @satyaborg in https://github.com/kensa-sh/kensa/pull/35
* fix: align langfuse setup parity by @satyaborg in https://github.com/kensa-sh/kensa/pull/36


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.10.0...v0.10.1

## 0.10.0
### Features
* feat: support langfuse events-only imports by @satyaborg in https://github.com/kensa-sh/kensa/pull/33


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.9.4...v0.10.0

## 0.9.4
### Features
* feat: verify langfuse import readiness by @satyaborg in https://github.com/kensa-sh/kensa/pull/31
### Bug Fixes
* fix: respect langfuse endpoint env by @satyaborg in https://github.com/kensa-sh/kensa/pull/30


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.9.3...v0.9.4

## 0.9.3
### Bug Fixes
* fix: use absolute URLs for banner and license link in README by @satyaborg in https://github.com/kensa-sh/kensa/pull/27
* fix: simplify tty agent picker by @satyaborg in https://github.com/kensa-sh/kensa/pull/28


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.9.2...v0.9.3

## 0.9.2
### Features
* feat: add init agent onboarding choices by @satyaborg in https://github.com/kensa-sh/kensa/pull/25
### Chores
* chore: automate release note labels by @satyaborg in https://github.com/kensa-sh/kensa/pull/22
* chore: lead README install with single agent fetch line by @satyaborg in https://github.com/kensa-sh/kensa/pull/23
* chore: add legacy version callout and modify README by @satyaborg in https://github.com/kensa-sh/kensa/pull/24


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.9.1...v0.9.2

## 0.9.1
### Other Changes
* chore: remove examples by @satyaborg in https://github.com/kensa-sh/kensa/pull/15
* fix: relax package dependency bounds by @satyaborg in https://github.com/kensa-sh/kensa/pull/16
* chore: align release labels with commit prefixes by @satyaborg in https://github.com/kensa-sh/kensa/pull/20
* chore: release 0.9.1 by @satyaborg in https://github.com/kensa-sh/kensa/pull/21


**Full Changelog**: https://github.com/kensa-sh/kensa/compare/v0.9.0...v0.9.1

## 0.9.0

## New Contributors
* @dependabot[bot] made their first contribution in https://github.com/kensa-sh/kensa/pull/6

**Full Changelog**: https://github.com/kensa-sh/kensa/commits/v0.9.0
