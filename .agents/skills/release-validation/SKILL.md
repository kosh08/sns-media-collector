---
name: sns-release-validation
description: Validate SNS Media Collector changes before delivery or release. Use when finishing a feature or bug fix, building the Windows app, producing an installer, updating versions, checking upgrades, or preparing a GitHub release.
---

# Release and delivery validation

A code change is not considered complete merely because it builds.

Use validation proportional to the scope of the change.

## During development

Prefer targeted tests for the changed subsystem so iteration stays fast.

For example:

- authentication change -> authentication/profile tests
- download logic -> incremental/archive tests
- Qt UI change -> relevant Qt regression tests
- packaging change -> build/package smoke tests

Do not run expensive unrelated validation repeatedly without reason.

## Before delivery of a significant change

Check:

1. Python syntax compatibility.
2. Relevant unit tests.
3. Relevant Qt regression tests.
4. Application startup smoke test.
5. No accidental credentials or local user data are included.
6. Existing configuration/data compatibility where relevant.

Use the repository's existing test commands rather than inventing a parallel test system.

The repository provides:

```powershell
full_test.cmd
```

Use the full suite when the change has broad impact or when preparing a release.

## Release behavior

The Windows release pipeline is expected to cover:

- Python compatibility and automated tests
- Qt regression and startup validation
- PyInstaller application packaging
- gallery-dl packaging
- updater packaging
- QtWebEngine packaging
- extractor presence checks
- Inno Setup installer creation
- clean installation
- old-version to new-version update
- reinstall
- startup
- uninstall
- user-data persistence
- SHA-256 verification

Do not weaken an existing release check merely to make CI pass.

## User data

Application updates and uninstall/reinstall testing must account for user data stored separately from the installed application.

Do not accidentally treat user data as disposable application binaries.

## Real-site limitations

Automated tests must not depend on real X cookies or pixiv tokens.

If a behavior can only be fully verified against a real authenticated account:

- automate everything that can be tested safely,
- clearly identify the remaining real-account verification,
- do not fake success.
