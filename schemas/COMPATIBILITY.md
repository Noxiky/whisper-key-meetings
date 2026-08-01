# Session Schema Compatibility

1. `schema_version` is a required positive integer in every canonical file and event.
2. Readers reject versions newer than their supported major version instead of guessing.
3. Additive optional fields remain within the same version only when old readers already permit them.
4. Breaking field, meaning, enum, or lifecycle changes increment `schema_version`.
5. Migration creates a backup, writes to a new temporary folder, validates it, and atomically promotes it.
6. Raw audio and original event journals are never rewritten merely to upgrade a projection.
7. Golden fixtures for every supported version remain in the repository.
