---
name: sns-incremental-download
description: Implement, debug, or modify incremental collection, X Likes, pixiv bookmarks, anchors, archives, duplicate detection, existing Hitomi Downloader files, destination paths, or deciding which media should be downloaded in SNS Media Collector.
---

# Incremental download and duplicate safety

Treat avoiding duplicate downloads and avoiding skipped media as correctness requirements.

## General rules

When changing collection logic:

- Preserve existing downloaded media whenever possible.
- Do not advance an incremental boundary until the new state is safely confirmed.
- Prefer a recoverable retry state over silently marking unverified media as successfully processed.
- Use stable service/media identifiers where available.
- Respect gallery-dl archives and existing files.
- Existing Hitomi Downloader collections must remain usable where supported.

## X Likes

The "new Likes only" flow conceptually works as:

1. Inspect Likes up to the configured search limit without downloading full media.
2. Search for the saved Likes anchor.
3. Items before that anchor are candidates for new content.
4. Compare candidates against the archive and existing files.
5. Download only missing media.
6. Advance the anchor only after confirming that expected output exists and is valid.

If the previous anchor cannot be found within the search range:

- Do not silently advance it.
- Preserve a recoverable state.
- Surface enough information for the user to understand why incremental progress stopped.

If output verification fails:

- Do not advance the anchor.

## pixiv bookmarks

"New bookmarks only" means newly added bookmarks, not newly published artwork.

A recently bookmarked old work must still be considered new.

Use bookmark-list ordering or equivalent bookmark state rather than publication date as the incremental boundary.

Previously saved works should be skipped through the relevant archive and existing-file checks.

## Existing media

When checking whether something is already saved, consider:

- gallery-dl archive state
- existing Hitomi Downloader files
- current SNS Media Collector output
- service/media IDs where available

Do not rely only on filenames if a stable ID is available.

## Storage behavior

When changing destination or storage code:

- Keep the selected save location visible and predictable.
- Avoid unexpectedly mixing accounts or services.
- Preserve existing user data across app updates.

## Validation

For changes affecting incremental download logic, test at minimum:

- zero new items
- one new item
- multiple new items
- previously downloaded item
- old content newly liked/bookmarked
- missing anchor
- search-limit exhaustion
- failed download
- output verification failure
- retry after failure

Do not declare incremental logic complete based only on successful network enumeration; confirm the expected local result.
