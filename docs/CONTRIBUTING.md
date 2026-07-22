# Contributing to docs/

## Adding a new spec

Choose the correct subdirectory:

- `specs/` for implementation specs and build prompts
- `framework/` for domain-model docs (decision frameworks, module catalogs)
- `external_apis/` for external API reference

Use snake_case lowercase filenames. Subfolder context carries the category, so filenames can be short.

Update `docs/README.md` with a new index entry in the same PR.

If implementing a new spec, add its path to the relevant workstream file's "Active specs in use" section.

## Updating a spec

Specs are versioned in git history. Do not keep v1/v2 suffixes in filenames.

The spec document header should note the current version.

If a spec revision is substantial, add a changelog entry at the bottom of the file.

## Archiving a spec

When a spec is fully implemented and the code becomes the source of truth, move it to `docs/specs/archive/` (create this subdirectory if needed). Update `docs/README.md`.
