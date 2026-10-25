# RF hidden specs: curated preservation rules

Method: all 25 `instruction.md` files (the persona's hidden spec) were read in full; every quote below is verbatim and checked by script against the stated line number.
What counts: a task-specific constraint that something must stay as it is or keep working while code around it changes (keep-as-is, behaviour-preserved, compatibility). Generic test-file boilerplate, plain change requests and descriptions of current code are excluded.
Interface entries only count when they say a name or signature must stay the same. Cross-checked against `preservation_rules.md` (regex screen), which has false positives.

## Summary

| Task (last 8) | Full id | Repo | Language | Rules | Strongest rule |
|---|---|---|---|---|---|
| 7be1e531 | rf_task-69391d8d1ce51c407be1e531 | trufflesecurity_trufflehog | Go | 0 | none |
| 7be1e533 | rf_task-69391d8d1ce51c407be1e533 | simple-login_app | Python | 1 | Keep the existing behavior unchanged |
| 4fd11885 | rf_task-694b4b99829f00e24fd11885 | trufflesecurity_trufflehog | Go | 0 | none |
| 4fd11889 | rf_task-694b4b99829f00e24fd11889 | trufflesecurity_trufflehog | Go | 1 | Latency tracking should continue to work |
| 4fd11891 | rf_task-694b4b99829f00e24fd11891 | Automattic_wp-calypso | JavaScript | 0 | none |
| 4fd118a1 | rf_task-694b4b99829f00e24fd118a1 | secdev_scapy | Python | 0 | none |
| 10d4b40c | rf_task-696719205599a51110d4b40c | trufflesecurity_trufflehog | Go | 1 | Maintain the same external behavior |
| 10d4b428 | rf_task-696719205599a51110d4b428 | Automattic_wp-calypso | TypeScript | 0 | none |
| 10d4b434 | rf_task-696719205599a51110d4b434 | Automattic_wp-calypso | TypeScript | 1 | OverviewCard keeps its props and default export |
| 10d4b436 | rf_task-696719205599a51110d4b436 | grafana_k6 | Go | 0 | none |
| 10d4b440 | rf_task-696719205599a51110d4b440 | grafana_k6 | Go | 0 | none |
| 10d4b441 | rf_task-696719205599a51110d4b441 | grafana_k6 | Go | 0 | none |
| 10d4b446 | rf_task-696719205599a51110d4b446 | grafana_k6 | Go | 2 | Keep the Sum field exported (vendored code reads it) |
| 10d4b447 | rf_task-696719205599a51110d4b447 | grafana_k6 | Go | 2 | Keep public API stable; v1 subpackage must be package cloud |
| 10d4b451 | rf_task-696719205599a51110d4b451 | grafana_k6 | Go | 0 | none |
| 10d4b455 | rf_task-696719205599a51110d4b455 | grafana_grafana | Go | 0 | none |
| 10d4b458 | rf_task-696719205599a51110d4b458 | grafana_grafana | Go | 0 | none |
| 10d4b476 | rf_task-696719205599a51110d4b476 | secdev_scapy | Python | 0 | none |
| ed98812f | rf_task-69b7c2a04b6f8ff9ed98812f | OISF_suricata | C | 2 | FileContainer method signatures unchanged |
| ed988137 | rf_task-69b7c2a04b6f8ff9ed988137 | OISF_suricata | C | 1 | Keep compatibility for callers ignoring the return value |
| ed98813b | rf_task-69b7c2a04b6f8ff9ed98813b | netdata_netdata | C | 3 | rrd_memory_mode_name/id keep their names, only types change |
| ed98813d | rf_task-69b7c2a04b6f8ff9ed98813d | netdata_netdata | C | 0 | none |
| 8265afb7 | rf_task-69d196f015a150488265afb7 | secdev_scapy | Python | 1 | Re-parented PCO classes: no other signature changes |
| 8265afb9 | rf_task-69d196f015a150488265afb9 | grafana_grafana | TypeScript | 2 | Don't break unrelated call sites |
| 8265afc1 | rf_task-69d196f015a150488265afc1 | MariaDB_server | C++ | 2 | RENAME TABLE still triggers stats reload; all stats paths work |

## 7be1e531: rf_task-69391d8d1ce51c407be1e531 (trufflesecurity_trufflehog, Go)

none

## 7be1e533: rf_task-69391d8d1ce51c407be1e533 (simple-login_app, Python)

- instruction.md:1: "Keep the existing behavior unchanged."
  - type: behaviour-preserved
  - breaks if: the extracted `delete_custom_domain` stops doing what the inline controller code did (e.g. no longer sets `pending_deletion` or creates the deletion Job).

## 4fd11885: rf_task-694b4b99829f00e24fd11885 (trufflesecurity_trufflehog, Go)

none

## 4fd11889: rf_task-694b4b99829f00e24fd11889 (trufflesecurity_trufflehog, Go)

- instruction.md:9: "Latency tracking should continue to work for files that are processed."
  - type: behaviour-preserved
  - breaks if: dropping or mis-scoping the latency metric while restructuring the defers in `HandleFile`.

## 4fd11891: rf_task-694b4b99829f00e24fd11891 (Automattic_wp-calypso, JavaScript)

none

## 4fd118a1: rf_task-694b4b99829f00e24fd118a1 (secdev_scapy, Python)

none

## 10d4b40c: rf_task-696719205599a51110d4b40c (trufflesecurity_trufflehog, Go)

- instruction.md:7: "Maintain the same external behavior."
  - type: behaviour-preserved
  - breaks if: a verify setting that no longer reaches the scanner, or keyword matching that no longer finds the same detectors.

## 10d4b428: rf_task-696719205599a51110d4b428 (Automattic_wp-calypso, TypeScript)

none

## 10d4b434: rf_task-696719205599a51110d4b434 (Automattic_wp-calypso, TypeScript)

- instruction.md:21: "The OverviewCard component retains its existing public API (props: title, heading, description, icon, link, disabled, intent, isLoading) and default export."
  - type: keep-as-is
  - breaks if: renaming or dropping an `OverviewCard` prop, or changing it from the default export.

## 10d4b436: rf_task-696719205599a51110d4b436 (grafana_k6, Go)

none

## 10d4b440: rf_task-696719205599a51110d4b440 (grafana_k6, Go)

none

## 10d4b441: rf_task-696719205599a51110d4b441 (grafana_k6, Go)

none

## 10d4b446: rf_task-696719205599a51110d4b446 (grafana_k6, Go)

- instruction.md:1: "Keep the Sum field exported because external vendored code reads it directly"
  - type: keep-as-is
  - breaks if: renaming `Sum` to `sum` along with the other `TrendSink` fields that become unexported.
- instruction.md:82: "the underlying Sum field must remain exported because external vendored code reads it directly"
  - type: keep-as-is
  - breaks if: making `TrendSink.Sum` unexported once `Total()` exists.

## 10d4b447: rf_task-696719205599a51110d4b447 (grafana_k6, Go)

- instruction.md:3: "Keep the public API stable."
  - type: keep-as-is
  - breaks if: changing an exported name or signature of the `output/cloud` package while moving code into versions.
- instruction.md:3: "The v1 subpackage must use `package cloud`."
  - type: keep-as-is
  - breaks if: declaring `package v1` (or any other name) in `output/cloud/v1/`.

## 10d4b451: rf_task-696719205599a51110d4b451 (grafana_k6, Go)

none

## 10d4b455: rf_task-696719205599a51110d4b455 (grafana_grafana, Go)

none

## 10d4b458: rf_task-696719205599a51110d4b458 (grafana_grafana, Go)

none

## 10d4b476: rf_task-696719205599a51110d4b476 (secdev_scapy, Python)

none

## ed98812f: rf_task-69b7c2a04b6f8ff9ed98812f (OISF_suricata, C)

- instruction.md:211: "## Internal Implementation Changes (Method Signatures Unchanged)"
  - type: keep-as-is
  - breaks if: changing the parameters or return type of `FileContainer.free`, `file_open`, `file_append` or `file_close` (lines 213-243 list their signatures).
- instruction.md:251: "Must remove SCGetContext and init_ffi calls, keeping only the log level initialization."
  - type: behaviour-preserved
  - breaks if: also deleting the log-level setup in `plugin.rs` `init`.

## ed988137: rf_task-69b7c2a04b6f8ff9ed988137 (OISF_suricata, C)

- instruction.md:16: "Maintain backward compatibility for callers that currently ignore the return value"
  - type: compatibility
  - breaks if: a caller that discards the decoder's int result stops compiling or behaving the same, e.g. by adding `warn_unused_result` beyond `DecodeTunnel`.

## ed98813b: rf_task-69b7c2a04b6f8ff9ed98813b (netdata_netdata, C)

- instruction.md:7: "The function names `rrd_memory_mode_name()` and `rrd_memory_mode_id()` should keep their current names, only their parameter and return types change to `RRD_DB_MODE`."
  - type: keep-as-is
  - breaks if: renaming `rrd_memory_mode_name` to `rrd_db_mode_name`.
- instruction.md:33: "Name stays the same."
  - type: keep-as-is
  - breaks if: renaming the global `default_rrd_memory_mode` (line 29) along with the type rename.
- instruction.md:15: "None of these changes should alter any runtime behavior."
  - type: behaviour-preserved
  - breaks if: a moved or renamed helper that returns a different string or enum value than before.

## ed98813d: rf_task-69b7c2a04b6f8ff9ed98813d (netdata_netdata, C)

none

## 8265afb7: rf_task-69d196f015a150488265afb7 (secdev_scapy, Python)

- instruction.md:86: "Re-parented to `PCO_Protocol`. No other signature changes."
  - type: keep-as-is
  - breaks if: changing fields or defaults of a re-parented class such as `PCO_PPP` while moving it (same sentence recurs for ~25 PCO classes, lines 86-262).

## 8265afb9: rf_task-69d196f015a150488265afb9 (grafana_grafana, TypeScript)

- instruction.md:9: "using the same idea as today—filter jobs to the current repository—and keep the usual loading, error, and empty states you'd expect from RTK."
  - type: behaviour-preserved
  - breaks if: the jobs tab showing jobs of all repositories, or losing its loading/error/empty state.
- instruction.md:11: "Don't break unrelated call sites."
  - type: compatibility
  - breaks if: changing the signature of `ScopedResourceClient` or an RTK endpoint that other provisioning code calls.

## 8265afc1: rf_task-69d196f015a150488265afc1 (MariaDB_server, C++)

- instruction.md:9: "That remains the supported way to force a stats reload"
  - type: behaviour-preserved
  - breaks if: removing stats deinitialisation from the `RENAME TABLE` path along with the one in `dict_table_close()`.
- instruction.md:15: "Ensure `RENAME TABLE` still triggers a stats reload and all existing stats paths (persistent, transient, auto-recalc) continue to work correctly."
  - type: behaviour-preserved
  - breaks if: a refactored path where auto-recalc never fires or a rename no longer reloads stats.

## Counts

- Tasks with at least one keep-as-is rule: 6 of 25
- Tasks with any rule: 12 of 25
