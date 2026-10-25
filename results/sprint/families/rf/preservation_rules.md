# RF persona preservation rules: regex screen

Keyword screen of each task's hidden spec (persona task block = instruction.md) by `rf.preservation_rules`, not curated. The curated, verbatim list is preservation_rules_curated.md.

## rf_task-69391d8d1ce51c407be1e531 (trufflesecurity_trufflehog, Go)

- Refactor this duplicated code into two shared packages - one for the buffer pool and buffer primitives, and another for the in-memory content writer to reduce duplication and improve maintainability.
- Move the buffer pool Prometheus metrics out of BufferedFileWriter into the new buffer package, and prefix the remaining BufferedFileWriter metrics to avoid naming conflicts.

## rf_task-69391d8d1ce51c407be1e533 (simple-login_app, Python)

- Keep the existing behavior unchanged.

## rf_task-694b4b99829f00e24fd11885 (trufflesecurity_trufflehog, Go)

- (none found)

## rf_task-694b4b99829f00e24fd11889 (trufflesecurity_trufflehog, Go)

- (none found)

## rf_task-694b4b99829f00e24fd11891 (Automattic_wp-calypso, JavaScript)

- This is fragile and incorrect because the redesign should not activate based on the flag alone.

## rf_task-694b4b99829f00e24fd118a1 (secdev_scapy, Python)

- Additionally, the random string generation has a compatibility issue between Python 2 and Python 3 when handling character types, and the socket close logic needs to handle Windows differently since the file descriptor check behaves differently on that platform.

## rf_task-696719205599a51110d4b40c (trufflesecurity_trufflehog, Go)

- Maintain the same external behavior.

## rf_task-696719205599a51110d4b428 (Automattic_wp-calypso, TypeScript)

- (none found)

## rf_task-696719205599a51110d4b434 (Automattic_wp-calypso, TypeScript)

- The OverviewCard component retains its existing public API (props: title, heading, description, icon, link, disabled, intent, isLoading) and default export.

## rf_task-696719205599a51110d4b436 (grafana_k6, Go)

- The `NewBundleFromArchive` function has similar coupling, and also contains compatibility mode fallback logic that mixes concerns.
- The compiler in `internal/js/compiler/compiler.go` carries a `CompatibilityMode` field in both its `Options` struct and internal `parsingState`, but this field is not actually needed for the parsing logic and adds an unnecessary dependency on the `lib` package.
- The compatibility mode tracking in the compiler should be removed since it is not needed for parsing.

## rf_task-696719205599a51110d4b440 (grafana_k6, Go)

- Parse errors from the underlying parser are returned directly to the caller, preserving their original types.

## rf_task-696719205599a51110d4b441 (grafana_k6, Go)

- The emitted metrics already contain group tags, so maintaining this state separately seems redundant.

## rf_task-696719205599a51110d4b446 (grafana_k6, Go)

- Keep the Sum field exported because external vendored code reads it directly; add a Total() accessor that returns Sum for internal use.
- Note: the underlying Sum field must remain exported because external vendored code reads it directly.

## rf_task-696719205599a51110d4b447 (grafana_k6, Go)

- The cloud output in output/cloud/ is getting hard to maintain.
- Keep the public API stable.

## rf_task-696719205599a51110d4b451 (grafana_k6, Go)

- (none found)

## rf_task-696719205599a51110d4b455 (grafana_grafana, Go)

- (none found)

## rf_task-696719205599a51110d4b458 (grafana_grafana, Go)

- Search the full codebase for all remaining `PluginMeta` references and rename them following the same pattern.

## rf_task-696719205599a51110d4b476 (secdev_scapy, Python)

- (none found)

## rf_task-69b7c2a04b6f8ff9ed98812f (OISF_suricata, C)

- As a strict constraint, please keep this refactor focused entirely on the source code; do not modify or refactor any test files.
- SuricataContext Struct (REMOVED) Path: `rust/src/core.rs` Name: `SuricataContext` Type: struct Input: `NA` Output: `NA` Description: C-compatible struct containing function pointers for file operations (FileOpenFile, FileCloseFile, FileAppendData, FileAppendGAP, FileContainerRecycle).
- ## Internal Implementation Changes (Method Signatures Unchanged)

## rf_task-69b7c2a04b6f8ff9ed988137 (OISF_suricata, C)

- (none found)

## rf_task-69b7c2a04b6f8ff9ed98813b (netdata_netdata, C)

- The function names `rrd_memory_mode_name()` and `rrd_memory_mode_id()` should keep their current names, only their parameter and return types change to `RRD_DB_MODE`.

## rf_task-69b7c2a04b6f8ff9ed98813d (netdata_netdata, C)

- (none found)

## rf_task-69d196f015a150488265afb7 (secdev_scapy, Python)

- (none found)

## rf_task-69d196f015a150488265afb9 (grafana_grafana, TypeScript)

- Third, in `public/app/features/provisioning/RepositoryStatusPage.tsx`, replace the hand-rolled client + async pattern on the jobs tab with the RTK Query hooks from the provisioning API, using the same idea as today—filter jobs to the current repository—and keep the usual loading, error, and empty states you'd expect from RTK.
- Keep changes minimal and limited to production source.
- Don't break unrelated call sites.

## rf_task-69d196f015a150488265afc1 (MariaDB_server, C++)

- Call sites that don't need MDL release should release the table handle directly instead of going through `dict_table_close()`.
- That remains the supported way to force a stats reload (\`RENAME TABLE t TO tmp, tmp TO t;\`).
- Values: SCHEMA_NOT_EXIST (-1) when tables do not exist, SCHEMA_OK (0) when schema is valid, SCHEMA_INVALID when schema exists but is corrupted or has wrong structure.
