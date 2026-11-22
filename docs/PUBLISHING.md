# Publishing Svago Motion Mastery

This guide covers release and catalog submission. A GitHub release and acceptance
into the Marketplace catalog are separate steps.

The Marketplace built into Home Assistant 2026.11 uses the same catalog and
`hacs.json` as HACS. This repository uses the standard
`custom_components/svago/` layout and a minimum Home Assistant version of
2026.10.0, so older supported installations can continue using HACS.

## Repository setup

The repository is
[`roweboat123/svago-home-assistant`](https://github.com/roweboat123/svago-home-assistant),
maintained by `@roweboat123`, with an MIT code license. The manifest's documentation
and issue URLs must resolve there before submission.

Keep the repository public with issues enabled, a short description, and relevant
topics such as `home-assistant`, `hacs`, `bluetooth`, and `svago`. Include the code
license, this documentation, the tests, and the workflows. Keep personal captures,
private development notes, addresses, and saved Home Assistant profiles out of it.
The normal installation requires only a powered chair and Home Assistant
Bluetooth. Examples must use the installing user's own entities.

The integration ships its images in `custom_components/svago/brand/`.
Brand assets must be suitable for the supported themes and follow Home Assistant's
image requirements. The source code license does not grant rights to third-party
trademarks or artwork.

## Checks

Run the local checks in [Contributing](../CONTRIBUTING.md). After pushing the source,
make both GitHub workflows pass:

- **Tests and package:** minimum/current Home Assistant regression suites and Ruff.
- **Marketplace validation:** hassfest and `hacs/action`, without ignored checks.

The second workflow depends on real repository metadata and network services;
local tests cannot substitute for its successful run. Its HACS action does not
post pull-request comments.

Test a clean setup with no saved memories, an upgrade retaining existing memories,
ordinary use on an always-powered chair, and recovery after an interruption.
Check Continuous status and recall of memories with Continuous selected. Long
timer-renewal behavior and physical movement still need testing on a chair.

## Version and release

1. Update the integration version in `manifest.json` and `const.py`, and record the
   changes in the release notes.
2. Run the checks for that exact source revision.
3. Create a tag matching the manifest version exactly, for example `1.0.0`.
4. Publish a full GitHub release from that tag with clear release notes. A tag
   alone does not meet the catalog submission requirement.

The tests workflow builds a manual-install ZIP on a tag push or manual run,
after the regression tests pass. The ZIP contains `custom_components/svago/`,
the public guides, examples, release notes, validation notes, LICENSE and NOTICE.
Relative documentation links are checked against the archive contents.
Download it from the workflow's artifact and attach the
contained ZIP to the release if a manual-install download is wanted. The workflow
does not create or publish releases automatically.

`hacs.json` deliberately does not enable `zip_release`: Marketplace and HACS
install the integration from GitHub's archive of the selected release tag. That
avoids making normal updates depend on a separately uploaded asset. User memories
and timer preferences live in Home Assistant storage outside the integration
folder and do not require `persistent_directory`.

Users can add a released version as a custom integration repository before catalog
acceptance:
[`roweboat123/svago-home-assistant` in HACS or Marketplace](https://my.home-assistant.io/redirect/hacs_repository/?owner=roweboat123&repository=svago-home-assistant&category=integration).
The older `hacs_repository` link supports both HACS and the new Marketplace;
`marketplace_repository` is available from Home Assistant 2026.11.

## Catalog submission

After a full release and passing validation runs are available, the owner or a
major contributor can open a pull request to `hacs/default`, adding the repository
to the alphabetical `integration` list. Complete its current submission template
and link the full release, passing HACS run and passing hassfest run. Do not claim
catalog availability before that pull request is accepted.

## Official references

- [Custom integration publishing](https://contributors.home-assistant.io/marketplace/guides/integrations/)
- [hacs.json](https://contributors.home-assistant.io/marketplace/reference/hacs-json/)
- [Validation checks](https://contributors.home-assistant.io/marketplace/reference/validation/)
- [Catalog submission](https://contributors.home-assistant.io/marketplace/guides/catalog/)
- [Release handling](https://contributors.home-assistant.io/marketplace/guides/releases/)
- [Brand images](https://contributors.home-assistant.io/core/integrations/brands/)

Reviewed November 22, 2026.
