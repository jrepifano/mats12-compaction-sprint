# Task image validation

Generated 2026-10-25T00:40:39Z by `scripts/validate_task_images.py summary` (scanner c06cd3d118db-s8i8t5). Brief: `logs/s2/briefs/task-image-validation.md`.

`verdict` = what an agent in the container can reach: LEAK if the probe or a code-confirmed red-team find says so, CLEAN only if the probe is CLEAN and the red-team ran and found nothing, else ERROR. `layers` = solution text in the image's layer tarballs, git objects inside layers decoded. `hidden tests` = the verifier's added test lines found in the container.

## Counts

- **deepswe**: verdict CLEAN 40; probe CLEAN 40; layers CLEAN 40; redteam CLEAN 40; hidden_tests CLEAN 40
- **pro**: verdict LEAK 25; probe LEAK 25; layers None 25; redteam None 25; hidden_tests CLEAN 4, ERROR 1, LEAK 20
- **pro_rebuilt**: verdict CLEAN 24; probe CLEAN 24; layers CLEAN 24; redteam CLEAN 24; hidden_tests CLEAN 24
- **rf**: verdict ERROR 22, LEAK 3; probe CLEAN 23, LEAK 2; layers ERROR 9, LEAK 16; redteam None 25; hidden_tests CLEAN 25
- **rf_rebuilt**: verdict ERROR 16; probe None 16; layers None 16; redteam None 16; hidden_tests None 16

## Per image

| key | verdict | probe | layers | red-team (calls) | hidden tests | why |
|---|---|---|---|---|---|---|
| deepswe/arcane-drift-detection-baselines | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/awilix-async-container-initialization | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/bandit-structured-nosec-directives | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/boa-hierarchical-evaluation-cancellation | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/cattrs-partial-structuring-recovery | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/clack-async-autocomplete-options | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 6 single-needle hit(s) for review |
| deepswe/drizzle-orm-window-function-builders | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 2 single-needle hit(s) for review |
| deepswe/dynamodb-toolbox-conditional-attribute-requirements | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/dynamodb-toolbox-lazy-recursive-schemas | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 1 single-needle hit(s) for review |
| deepswe/effect-sse-httpapi-streaming | CLEAN | CLEAN | CLEAN | CLEAN (12) | CLEAN | 2 single-needle hit(s) for review |
| deepswe/etree-xml-diff-patch | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/fastapi-deprecation-response-headers | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 7 single-needle hit(s) for review |
| deepswe/fastapi-implicit-head-options | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 1 single-needle hit(s) for review |
| deepswe/go-git-worktree-merge-conflicts | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/happy-dom-abort-pending-body-reads | CLEAN | CLEAN | CLEAN | CLEAN (11) | CLEAN | 25 single-needle hit(s) for review |
| deepswe/helm-array-merge-strategies | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 38 single-needle hit(s) for review |
| deepswe/httpx-deterministic-cookie-store | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 81 single-needle hit(s) for review |
| deepswe/httpx-streaming-json-iteration | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/kcp-go-multiplexed-kcp-streams | CLEAN | CLEAN | CLEAN | CLEAN (11) | CLEAN |  |
| deepswe/kombu-single-active-consumer-priority | CLEAN | CLEAN | CLEAN | CLEAN (12) | CLEAN |  |
| deepswe/koota-composite-trait-aspects | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/koota-deferred-mutation-buffer | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/koota-pair-relation-tracking | CLEAN | CLEAN | CLEAN | CLEAN (11) | CLEAN |  |
| deepswe/koota-query-predicates | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/mobly-grouped-test-barriers | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/ofetch-per-origin-circuit-breaker | CLEAN | CLEAN | CLEAN | CLEAN (11) | CLEAN |  |
| deepswe/oxvg-structural-selector-preservation | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/participle-grammar-conflict-analysis | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/pebble-durability-wait-apis | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/pwntools-tube-multiplexing | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/python-statemachine-state-data-scoping | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/returns-validated-error-accumulation | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/scriggo-method-declarations | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/sqlite-utils-safe-import-checkpoints | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/testem-bail-on-test-failure | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 2 single-needle hit(s) for review |
| deepswe/tomlkit-toml-table-converters | CLEAN | CLEAN | CLEAN | CLEAN (12) | CLEAN |  |
| deepswe/true-myth-iterable-collection-combinators | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 365 single-needle hit(s) for review |
| deepswe/vitest-duration-sharding | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| deepswe/wasmi-trap-coredumps | CLEAN | CLEAN | CLEAN | CLEAN (11) | CLEAN |  |
| deepswe/wazero-multi-module-snapshots | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| pro/instance_ansible__ansible-4c5ce5a1a9e79a845aff4978cfeb72a0d4ecf7d6-v1055803c3a812189a1133297f7f5468579283f86 | LEAK | LEAK | None | None (None) | CLEAN | /app/.git: solution commit 4c5ce5a1a9e79a845aff4978cfeb72a0d4ecf7d6 present (commit); /app/.git: 48 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 0000b76a0a5f40f626600064362a1d655f389a68 2016-08-05T06:51:18-07:00 YAML treats some unquoted strings as booleans. For instance, (#16961); 729 single-needle hit(s) for review |
| pro/instance_ansible__ansible-c616e54a6e23fa5616a1d56d243f69576164ef9b-v1055803c3a812189a1133297f7f5468579283f86 | LEAK | LEAK | None | None (None) | CLEAN | /app/.git: solution commit c616e54a6e23fa5616a1d56d243f69576164ef9b present (commit); /app/.git: 44 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 00d5a1610f37395dd9e6010b9f0dbb26342f3a59 2021-05-17T10:28:42-05:00 Fix pb subs collapse (#74301) (#74376); 82 single-needle hit(s) for review |
| pro/instance_ansible__ansible-e40889e7112ae00a21a2c74312b330e67a766cc0-v1055803c3a812189a1133297f7f5468579283f86 | LEAK | LEAK | None | None (None) | CLEAN | /app/.git: solution commit e40889e7112ae00a21a2c74312b330e67a766cc0 present (commit); /app/.git: 42 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 01ff57bdff35e0e97b16caa2c420fe01039d13e1 2022-11-29T16:27:08+01:00 add ansible-galaxy cli documentation for parallel execution (#79433); 284 single-needle hit(s) for review |
| pro/instance_element-hq__element-web-d06cf09bf0b3d4a0fbe6bd32e4115caea2083168-vnan | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit d06cf09bf0b3d4a0fbe6bd32e4115caea2083168 present (commit); /app/.git: 20 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 00aadf158083041897096f3bfd240c6fe86f790c 2024-12-02T11:23:18+00:00 Merge pull request #28538 from element-hq/t3chguy/fix/20721; 69 single-needle hit(s) for review |
| pro/instance_element-hq__element-web-fe14847bb9bb07cab1b9c6c54335ff22ca5e516a-vnan | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit fe14847bb9bb07cab1b9c6c54335ff22ca5e516a present (commit); /app/.git: 20 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 00aadf158083041897096f3bfd240c6fe86f790c 2024-12-02T11:23:18+00:00 Merge pull request #28538 from element-hq/t3chguy/fix/20721; 682 single-needle hit(s) for review |
| pro/instance_flipt-io__flipt-96820c3ad10b0b2305e8877b6b303f7fafdf815f | LEAK | LEAK | None | None (None) | ERROR | /app/.git: solution commit 96820c3ad10b0b2305e8877b6b303f7fafdf815f present (commit); /app/.git: 27 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 004955d8dc0228e88f29179e633f4b85e23114bb 2025-05-28T13:15:09+00:00 chore(deps): bump some vulnerable deps (#4258); solution patch applies in neither direction (fwd 1, rev 1); base state unverified; 14 single-needle hit(s) fo |
| pro/instance_flipt-io__flipt-c1728053367c753688f114ec26e703c8fdeda125 | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit c1728053367c753688f114ec26e703c8fdeda125 present (commit); /app/.git: 26 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 00221352bd3fd440d1d7adf3b5cf0762b9ce8dfd 2023-10-09T03:08:45+00:00 chore(deps): bump github.com/spf13/viper from 1.16.0 to 1.17.0 (#2211); 201 single-needle hit(s) for review |
| pro/instance_flipt-io__flipt-e5fe37c379e1eec2dd3492c5737c0be761050b26 | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit e5fe37c379e1eec2dd3492c5737c0be761050b26 present (commit); /app/.git: 27 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 045fa756012d80a1997ec36e5d33857ba0ffd7c0 2023-11-01T17:08:20-04:00 Merge branch 'main' of https://github.com/flipt-io/flipt; 293 single-needle hit(s) for review |
| pro/instance_future-architect__vuls-7eb77f5b5127c847481bcf600b4dca2b7a85cf3e | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit 7eb77f5b5127c847481bcf600b4dca2b7a85cf3e present (commit); /app/.git: 25 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 0095c40e69023abab6ed5d03aff7777cf459d3a7 2021-06-09T08:00:52+09:00 fix(vet): go vet err of `make build-scanner` (#1248); 183 single-needle hit(s) for review |
| pro/instance_gravitational__teleport-d6ffe82aaf2af1057b69c61bf9df777f5ab5635a-vee9b09fb20c43af7e520f57e9239bbcf46b7113d | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit d6ffe82aaf2af1057b69c61bf9df777f5ab5635a present (commit); /app/.git: 28 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 00092003642e82f6c2a9750a987a5ae34be92ca6 2022-12-21T10:27:24-05:00 Merge branch 'master' into mike.wilson/make-functions-public-for-enterprise; 43 single-needle hit(s) for review |
| pro/instance_gravitational__teleport-f432a71a13e698b6e1c4672a2e9e9c1f32d35c12 | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit f432a71a13e698b6e1c4672a2e9e9c1f32d35c12 present (commit); /app/.git: 34 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 03bd596e25c79a600e68c25df61817cc9cf1247d 2022-10-30T22:36:27-05:00 Merge branch 'master' into fred/multiarch-teleport-actual-container-images; 3885 single-needle hit(s) for review |
| pro/instance_internetarchive__openlibrary-0a90f9f0256e4f933523e9842799e39f95ae29ce-v76304ecdb3a5954fcf13feb710e8c40fcf24b73c | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit 0a90f9f0256e4f933523e9842799e39f95ae29ce present (commit); /app/.git: 34 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 000a0b4470cc3c77f28724bc784ddc37a4dd4b3a 2024-03-22T12:38:45-04:00 Merge pull request #8919 from scottbarnes/fix/stop-api-books-from-defaulting-to-high-priority-is-true; 358 single-needle hit(s) for review |
| pro/instance_internetarchive__openlibrary-4b7ea2977be2747496ba792a678940baa985f7ea-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4 | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit 4b7ea2977be2747496ba792a678940baa985f7ea present (commit); /app/.git: 28 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 0180e2ca33a152e69bdbc97047cf6961787c947d 2025-05-16T12:15:01+02:00 Merge pull request #10795 from internetarchive/fix-cover-aspect-ratio-author; 439 single-needle hit(s) for review |
| pro/instance_internetarchive__openlibrary-5de7de19211e71b29b2f2ba3b1dff2fe065d660f-v08d8e8889ec945ab821fb156c04c7d2e2810debb | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit 5de7de19211e71b29b2f2ba3b1dff2fe065d660f present (commit); /app/.git: 40 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 00f61af41d4bb6f65750c51492953dd14e6321ba 2024-11-26T04:13:36+07:00 Merge pull request #10037 from internetarchive/improve-offline-pages; 82 single-needle hit(s) for review |
| pro/instance_internetarchive__openlibrary-b67138b316b1e9c11df8a4a8391fe5cc8e75ff9f-ve8c8d62a2b60610a3c4631f5f23ed866bada9818 | LEAK | LEAK | None | None (None) | CLEAN | /app/.git: solution commit b67138b316b1e9c11df8a4a8391fe5cc8e75ff9f present (commit); /app/.git: 31 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 000a0b4470cc3c77f28724bc784ddc37a4dd4b3a 2024-03-22T12:38:45-04:00 Merge pull request #8919 from scottbarnes/fix/stop-api-books-from-defaulting-to-high-priority-is-true; 385 single-needle hit(s) for review |
| pro/instance_internetarchive__openlibrary-d40ec88713dc95ea791b252f92d2f7b75e107440-v13642507b4fc1f8d234172bf8129942da2c2ca26 | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit d40ec88713dc95ea791b252f92d2f7b75e107440 present (commit); /app/.git: 31 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 0dc28e1bdfd003bfcd914c6a24fef6bf9015e9e2 2019-09-12T17:29:45+12:00 tidy imports and further addbook docstrings; 38 single-needle hit(s) for review |
| pro/instance_navidrome__navidrome-3972616585e82305eaf26aa25697b3f5f3082288 | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit 3972616585e82305eaf26aa25697b3f5f3082288 present (commit); /app/.git: 24 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 10cd3152bab50f1d298eaab261fbcdeabe0390ec 2022-11-27T22:01:07-05:00 Remove misplaced import; 1974 single-needle hit(s) for review |
| pro/instance_nodebb__nodebb-8168c6c40707478f71b8af60300830fe554c778c-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit 8168c6c40707478f71b8af60300830fe554c778c present (commit); /app/.git: 41 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 00532b6cde249f2254098887f741ef2117b3ff70 2016-08-29T16:00:19-04:00 Merge commit 'b854dd5150837db2be21fd46326fbdbe6a71b1b6' into weekly; 322 single-needle hit(s) for review |
| pro/instance_nodebb__nodebb-f083cd559d69c16481376868c8da65172729c0ca-vnan | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit f083cd559d69c16481376868c8da65172729c0ca present (commit); /app/.git: 33 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 0776f3de5ec41c3aa47554b669a9cdde29cd7229 2023-08-16T14:42:39+00:00 Merge commit 'eafa03ab567fd49f480291f7809c26ba796038e9' into v3.x; 190 single-needle hit(s) for review |
| pro/instance_protonmail__webclients-e65cc5f33719e02e1c378146fb981d27bc24bdf4 | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit e65cc5f33719e02e1c378146fb981d27bc24bdf4 present (commit); /app/.git: 14 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 0191db7f8f28b5f45ae6935050a104f59263944a 2022-08-04T09:40:51+00:00 Merge branch 'es-metadata-vs-content' into 'main'; 391 single-needle hit(s) for review |
| pro/instance_qutebrowser__qutebrowser-70248f256f93ed9b1984494d0a1a919ddd774892-v2ef375ac784985212b1805e1d0431dc8f1b3c171 | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit 70248f256f93ed9b1984494d0a1a919ddd774892 present (commit); /app/.git: 41 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 01642605d5ff3fccf89ac5d1e3db025e95541ed2 2021-01-17T12:53:19+01:00 Cleanup file context and tests; 2 single-needle hit(s) for review |
| pro/instance_qutebrowser__qutebrowser-96b997802e942937e81d2b8a32d08f00d3f4bc4e-v5fc38aaf22415ab0b70567368332beee7955b367 | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit 96b997802e942937e81d2b8a32d08f00d3f4bc4e present (commit); /app/.git: 41 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 01642605d5ff3fccf89ac5d1e3db025e95541ed2 2021-01-17T12:53:19+01:00 Cleanup file context and tests; 159 single-needle hit(s) for review |
| pro/instance_qutebrowser__qutebrowser-bedc9f7fadf93f83d8dee95feeecb9922b6f063f-v2ef375ac784985212b1805e1d0431dc8f1b3c171 | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit bedc9f7fadf93f83d8dee95feeecb9922b6f063f present (commit); /app/.git: 31 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 00d5aa6b22e2fdc4a7a1491a29b2a536e46a5564 2017-07-13T11:03:28+02:00 Refactor tab_close_prompt_if_pinned; 917 single-needle hit(s) for review |
| pro/instance_qutebrowser__qutebrowser-deeb15d6f009b3ca0c3bd503a7cef07462bd16b4-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit deeb15d6f009b3ca0c3bd503a7cef07462bd16b4 present (commit); /app/.git: 50 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 00a394506f149b600c9869a63dd37e847fe17cfe 2021-06-01T20:36:56+03:00 Merge remote-tracking branch 'origin/master' into 4nd3r/hostblock_subdomains; 8 single-needle hit(s) for review |
| pro/instance_qutebrowser__qutebrowser-fea33d607fde83cf505b228238cf365936437a63-v9f8e9d96c85c85a605e382f1510bd08563afc566 | LEAK | LEAK | None | None (None) | LEAK | /app/.git: solution commit fea33d607fde83cf505b228238cf365936437a63 present (commit); /app/.git: 36 commit(s) outside HEAD (refs/reflog/unreachable) touch solution files, e.g. 01f4807eaf8d20dcc3802cd2febf9432568f3935 2024-03-25T12:15:03+01:00 mypy: Set local_partial_types = True |
| pro_rebuilt/instance_ansible__ansible-4c5ce5a1a9e79a845aff4978cfeb72a0d4ecf7d6-v1055803c3a812189a1133297f7f5468579283f86 | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 4 single-needle hit(s) for review |
| pro_rebuilt/instance_ansible__ansible-c616e54a6e23fa5616a1d56d243f69576164ef9b-v1055803c3a812189a1133297f7f5468579283f86 | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| pro_rebuilt/instance_ansible__ansible-e40889e7112ae00a21a2c74312b330e67a766cc0-v1055803c3a812189a1133297f7f5468579283f86 | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| pro_rebuilt/instance_element-hq__element-web-d06cf09bf0b3d4a0fbe6bd32e4115caea2083168-vnan | CLEAN | CLEAN | CLEAN | CLEAN (11) | CLEAN | 8 single-needle hit(s) for review |
| pro_rebuilt/instance_element-hq__element-web-fe14847bb9bb07cab1b9c6c54335ff22ca5e516a-vnan | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| pro_rebuilt/instance_flipt-io__flipt-96820c3ad10b0b2305e8877b6b303f7fafdf815f | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| pro_rebuilt/instance_flipt-io__flipt-c1728053367c753688f114ec26e703c8fdeda125 | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 132 single-needle hit(s) for review |
| pro_rebuilt/instance_flipt-io__flipt-e5fe37c379e1eec2dd3492c5737c0be761050b26 | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 269 single-needle hit(s) for review |
| pro_rebuilt/instance_future-architect__vuls-7eb77f5b5127c847481bcf600b4dca2b7a85cf3e | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 138 single-needle hit(s) for review |
| pro_rebuilt/instance_gravitational__teleport-d6ffe82aaf2af1057b69c61bf9df777f5ab5635a-vee9b09fb20c43af7e520f57e9239bbcf46b7113d | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 21 single-needle hit(s) for review |
| pro_rebuilt/instance_gravitational__teleport-f432a71a13e698b6e1c4672a2e9e9c1f32d35c12 | CLEAN | CLEAN | CLEAN | CLEAN (11) | CLEAN | 132 single-needle hit(s) for review |
| pro_rebuilt/instance_internetarchive__openlibrary-0a90f9f0256e4f933523e9842799e39f95ae29ce-v76304ecdb3a5954fcf13feb710e8c40fcf24b73c | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 347 single-needle hit(s) for review |
| pro_rebuilt/instance_internetarchive__openlibrary-4b7ea2977be2747496ba792a678940baa985f7ea-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4 | CLEAN | CLEAN | CLEAN | CLEAN (11) | CLEAN |  |
| pro_rebuilt/instance_internetarchive__openlibrary-5de7de19211e71b29b2f2ba3b1dff2fe065d660f-v08d8e8889ec945ab821fb156c04c7d2e2810debb | CLEAN | CLEAN | CLEAN | CLEAN (12) | CLEAN | 10 single-needle hit(s) for review |
| pro_rebuilt/instance_internetarchive__openlibrary-d40ec88713dc95ea791b252f92d2f7b75e107440-v13642507b4fc1f8d234172bf8129942da2c2ca26 | CLEAN | CLEAN | CLEAN | CLEAN (11) | CLEAN |  |
| pro_rebuilt/instance_navidrome__navidrome-3972616585e82305eaf26aa25697b3f5f3082288 | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 1932 single-needle hit(s) for review |
| pro_rebuilt/instance_nodebb__nodebb-8168c6c40707478f71b8af60300830fe554c778c-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e | CLEAN | CLEAN | CLEAN | CLEAN (11) | CLEAN | 2 single-needle hit(s) for review |
| pro_rebuilt/instance_nodebb__nodebb-f083cd559d69c16481376868c8da65172729c0ca-vnan | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 2 single-needle hit(s) for review |
| pro_rebuilt/instance_protonmail__webclients-e65cc5f33719e02e1c378146fb981d27bc24bdf4 | CLEAN | CLEAN | CLEAN | CLEAN (12) | CLEAN | 3 single-needle hit(s) for review |
| pro_rebuilt/instance_qutebrowser__qutebrowser-70248f256f93ed9b1984494d0a1a919ddd774892-v2ef375ac784985212b1805e1d0431dc8f1b3c171 | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| pro_rebuilt/instance_qutebrowser__qutebrowser-96b997802e942937e81d2b8a32d08f00d3f4bc4e-v5fc38aaf22415ab0b70567368332beee7955b367 | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 27 single-needle hit(s) for review |
| pro_rebuilt/instance_qutebrowser__qutebrowser-bedc9f7fadf93f83d8dee95feeecb9922b6f063f-v2ef375ac784985212b1805e1d0431dc8f1b3c171 | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| pro_rebuilt/instance_qutebrowser__qutebrowser-deeb15d6f009b3ca0c3bd503a7cef07462bd16b4-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN | 5 single-needle hit(s) for review |
| pro_rebuilt/instance_qutebrowser__qutebrowser-fea33d607fde83cf505b228238cf365936437a63-v9f8e9d96c85c85a605e382f1510bd08563afc566 | CLEAN | CLEAN | CLEAN | CLEAN (10) | CLEAN |  |
| rf/task-69391d8d1ce51c407be1e531 | ERROR | CLEAN | LEAK | None (None) | CLEAN | 2 layer blobs/sha256/4cf1b353d11 file(s) hold 2+ distinct solution needles, e.g. workspace/pkg/writers/buffer_writer/bufferwriter.go (['I6', 'I8', 'S4']); red-team not run; 6 single-needle hit(s) for review |
| rf/task-69391d8d1ce51c407be1e533 | ERROR | CLEAN | LEAK | None (None) | CLEAN | 14 layer blobs/sha256/dac19ab144e git object(s) hold 2+ distinct solution needles, e.g. code/.git/objects:0911a748d1185047d077d6a75d196085517b7710 (['S2', 'S5']); red-team not run |
| rf/task-694b4b99829f00e24fd11885 | ERROR | CLEAN | LEAK | None (None) | CLEAN | 2 layer blobs/sha256/4cf1b353d11 file(s) hold 2+ distinct solution needles, e.g. workspace/pkg/detectors/github/v1/github_old.go (['I1', 'I4', 'S1', 'S2', 'S4', 'S5', 'S7']); red-team not run; 16 single-needle hit(s) for review |
| rf/task-694b4b99829f00e24fd11889 | ERROR | CLEAN | LEAK | None (None) | CLEAN | 9 layer blobs/sha256/10d94a2c19c git object(s) hold 2+ distinct solution needles, e.g. app/.git/objects:204fd080833a6fbfe556aba5ec7924931ce93d5e (['S2', 'S5']); red-team not run; 6 single-needle hit(s) for review |
| rf/task-694b4b99829f00e24fd11891 | ERROR | CLEAN | LEAK | None (None) | CLEAN | 4 layer blobs/sha256/4561c09f07d file(s) hold 2+ distinct solution needles, e.g. calypso/client/my-sites/plugins/categories/index.tsx (['S3', 'S8']); red-team not run; 68 single-needle hit(s) for review |
| rf/task-694b4b99829f00e24fd118a1 | ERROR | CLEAN | LEAK | None (None) | CLEAN | 3 layer blobs/sha256/450b20c8112 file(s) hold 2+ distinct solution needles, e.g. app/scapy/base_classes.py (['S1', 'S6']); red-team not run |
| rf/task-696719205599a51110d4b40c | ERROR | CLEAN | LEAK | None (None) | CLEAN | 2 layer blobs/sha256/79dae46873f file(s) hold 2+ distinct solution needles, e.g. app/main.go (['S1', 'S4', 'S7']); red-team not run; 6 single-needle hit(s) for review |
| rf/task-696719205599a51110d4b428 | ERROR | CLEAN | LEAK | None (None) | CLEAN | 13 layer blobs/sha256/4561c09f07d git object(s) hold 2+ distinct solution needles, e.g. calypso/.git/objects:08ceeef314ef743572753ed78fdf489fff52f4f2 (['S1', 'S2']); red-team not run; 41 single-needle hit(s) for review |
| rf/task-696719205599a51110d4b434 | ERROR | CLEAN | LEAK | None (None) | CLEAN | 24 layer blobs/sha256/ae2d4856aab git object(s) hold 2+ distinct solution needles, e.g. workspace/.git/objects:093b14d8ac627cdffe98926ba5e1b39e34824584 (['S2', 'S4']); red-team not run; 40 single-needle hit(s) for review |
| rf/task-696719205599a51110d4b436 | ERROR | CLEAN | LEAK | None (None) | CLEAN | 21 layer blobs/sha256/7545af7e719 git object(s) hold 2+ distinct solution needles, e.g. src/.git/objects:0c378a8bbb40c6280a50766664c84d0551ff375e (['I1', 'S1', 'S6']); red-team not run |
| rf/task-696719205599a51110d4b440 | ERROR | CLEAN | LEAK | None (None) | CLEAN | 13 layer blobs/sha256/c61835cfa2d git object(s) hold 2+ distinct solution needles, e.g. go/src/go.k6.io/k6/.git/objects:022d8c0d79ef552fa40abf3cb927ec8c04ad1eae (['I1', 'S1', 'S8']); red-team not run; 2 single-needle hit(s) for review |
| rf/task-696719205599a51110d4b441 | LEAK | LEAK | LEAK | None (None) | CLEAN | 3 file(s) hold 2+ distinct solution needles, e.g. /root/.cache/go-build/60/604d17fdb4c709a8aef08730ec54adb31fc575de1a01a3e423c8d0648d67eef9-d (['I4', 'I5', 'I6', 'I7']); 3 archive(s) hold 2+ distinct solution needles, e.g. /root/.cache/go-build/60/604d17fdb4c709a8aef08730ec54adb31fc575de1a01a3e423c8d0648d67eef9-d!_go_.o (['I4', 'I6', 'I7']); 3 layer blobs/sha256/c61835cfa2d file(s) hold 2+ distinc |
| rf/task-696719205599a51110d4b446 | ERROR | CLEAN | LEAK | None (None) | CLEAN | 48 layer blobs/sha256/b4286bcf0ae git object(s) hold 2+ distinct solution needles, e.g. workspace/.git/objects:046a6bfee6ae0a2eb4a523bea33f2c6b8b54bd41 (['I5', 'I7', 'I8', 'S6']); red-team not run |
| rf/task-696719205599a51110d4b447 | ERROR | CLEAN | LEAK | None (None) | CLEAN | 34 layer blobs/sha256/b4286bcf0ae git object(s) hold 2+ distinct solution needles, e.g. workspace/.git/objects:08eb52017eaa9ae4fb03939e4549c60c785204ff (['I1', 'S1', 'S5', 'S6', 'S8']); red-team not run |
| rf/task-696719205599a51110d4b451 | ERROR | CLEAN | LEAK | None (None) | CLEAN | 129 layer blobs/sha256/5091fffe4e3 git object(s) hold 2+ distinct solution needles, e.g. workspace/.git/objects:05476c21443b1a17c623e2a74aa8bde26d3afbfe (['I2', 'I5', 'I8']); red-team not run |
| rf/task-696719205599a51110d4b455 | ERROR | CLEAN | LEAK | None (None) | CLEAN | 17 layer blobs/sha256/8afa4eee2cc git object(s) hold 2+ distinct solution needles, e.g. grafana/.git/objects:0ccabfa9b28c7e3a993a6e19f69e246e1d865070 (['I7', 'S1', 'S7']); red-team not run; 132 single-needle hit(s) for review |
| rf/task-696719205599a51110d4b458 | ERROR | CLEAN | ERROR | None (None) | CLEAN | layer blobs/sha256/8afa4eee2ccd3e1ff (git objects grafana/.git/objects): needles ['I1', 'I2', 'I3', 'I4', 'I5', 'I6', 'I7', 'I8', 'S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8']; red-team not run; 132 single-needle hit(s) for review |
| rf/task-696719205599a51110d4b476 | ERROR | CLEAN | ERROR | None (None) | CLEAN | layer blobs/sha256/55186128ddbfb8c6b (git objects app/.git/objects): needles ['I1', 'I2', 'I3', 'I4', 'I5', 'I6', 'I7', 'I8', 'S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8']; red-team not run |
| rf/task-69b7c2a04b6f8ff9ed98812f | ERROR | CLEAN | ERROR | None (None) | CLEAN | layer blobs/sha256/106444c5a19ca2f62 (git objects app/.git/objects): needles ['S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8']; red-team not run; 8 single-needle hit(s) for review |
| rf/task-69b7c2a04b6f8ff9ed988137 | ERROR | CLEAN | ERROR | None (None) | CLEAN | layer blobs/sha256/8a72aa9b7b438dd8f (raw): needles ['I2', 'I3', 'S4']; red-team not run; 72 single-needle hit(s) for review |
| rf/task-69b7c2a04b6f8ff9ed98813b | LEAK | CLEAN | ERROR | None (None) | CLEAN | manual: prebuilt /opt/netdata-build (2026-02-19) was compiled from a tree that already has this task's refactor: its build.ninja/.ninja_deps/.ninja_log reference all 10 files the gold patch creates (src/database/rrd-algorithm.c/.h, rrdhost-collection.c/.h, rrdhost-labels.c/.h, rrdhost-slots.c/.h, rrdset-type.c/.h; 5 refs per .c), none of which exist in the source tree; object files and /usr/sbin/n |
| rf/task-69b7c2a04b6f8ff9ed98813d | LEAK | LEAK | ERROR | None (None) | CLEAN | 5 file(s) hold 2+ distinct solution needles, e.g. /opt/netdata-build/CMakeFiles/netdata.dir/src/database/rrdhost-system-info.c.o (['I1', 'I2', 'I3', 'I4', 'I5', 'I6', 'I7', 'I8', 'S1']); layer blobs/sha256/27bca2a21a380d3f7 (raw): needles ['I1', 'I2', 'I3', 'I4', 'I5', 'I6', 'I7', 'I8', 'S1', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8']; 9 single-needle hit(s) for review |
| rf/task-69d196f015a150488265afb7 | ERROR | CLEAN | ERROR | None (None) | CLEAN | layer blobs/sha256/55186128ddbfb8c6b (git objects app/.git/objects): needles ['I1', 'I2', 'I3', 'I4', 'I5', 'I6', 'I7', 'I8', 'S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8']; red-team not run |
| rf/task-69d196f015a150488265afb9 | ERROR | CLEAN | ERROR | None (None) | CLEAN | layer blobs/sha256/8afa4eee2ccd3e1ff (raw): needles ['S1', 'S5']; red-team not run; 134 single-needle hit(s) for review |
| rf/task-69d196f015a150488265afc1 | ERROR | CLEAN | ERROR | None (None) | CLEAN | layer blobs/sha256/d6b90f9be3acdf7e6 (git objects app/source/.git/objects): needles ['I1', 'I2', 'I3', 'I4', 'I5', 'I6', 'S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7', 'S8']; red-team not run; 3 single-needle hit(s) for review |
| rf_rebuilt/task-69391d8d1ce51c407be1e531 | ERROR | None | None | None (None) | None | not run |
| rf_rebuilt/task-69391d8d1ce51c407be1e533 | ERROR | None | None | None (None) | None | not run |
| rf_rebuilt/task-694b4b99829f00e24fd11885 | ERROR | None | None | None (None) | None | not run |
| rf_rebuilt/task-694b4b99829f00e24fd11889 | ERROR | None | None | None (None) | None | not run |
| rf_rebuilt/task-694b4b99829f00e24fd11891 | ERROR | None | None | None (None) | None | not run |
| rf_rebuilt/task-694b4b99829f00e24fd118a1 | ERROR | None | None | None (None) | None | not run |
| rf_rebuilt/task-696719205599a51110d4b40c | ERROR | None | None | None (None) | None | not run |
| rf_rebuilt/task-696719205599a51110d4b428 | ERROR | None | None | None (None) | None | not run |
| rf_rebuilt/task-696719205599a51110d4b436 | ERROR | None | None | None (None) | None | not run |
| rf_rebuilt/task-696719205599a51110d4b440 | ERROR | None | None | None (None) | None | not run |
| rf_rebuilt/task-696719205599a51110d4b441 | ERROR | None | None | None (None) | None | not run |
| rf_rebuilt/task-696719205599a51110d4b446 | ERROR | None | None | None (None) | None | not run |
| rf_rebuilt/task-696719205599a51110d4b447 | ERROR | None | None | None (None) | None | not run |
| rf_rebuilt/task-696719205599a51110d4b451 | ERROR | None | None | None (None) | None | not run |
| rf_rebuilt/task-696719205599a51110d4b476 | ERROR | None | None | None (None) | None | not run |
| rf_rebuilt/task-69d196f015a150488265afb7 | ERROR | None | None | None (None) | None | not run |

## What CLEAN means, and its limits

CLEAN means all of these passed on the exact image id:

- **Probe.** Run inside a fresh container (no network), it checks:
  - every git object store on the filesystem (refs, reflogs, stash, notes, packed refs, special
    heads, remotes, alternates; commits outside HEAD and unreachable objects; every object's
    content, and archives committed into history, decoded);
  - the whole filesystem except /proc, /sys and /dev, with archives found by signature and
    decoded recursively;
  - `git apply --check` of the gold patch in both directions;
  - stray patch files and the image config.
- **Detectors.** Up to 8 solution lines, 8 new names and 5 hidden-test lines, each absent from the
  HEAD tree. A location holding 2 or more distinct needles, or a solution line in an object HEAD
  can't reach, is a LEAK. A location with one needle, an undecodable archive outside
  toolchain/dependency fixtures, and a patch with too few distinctive lines are review items.
  Each is cleared by hand in `data/raw/sprint/task_validation/weak_review.json`, bound to the
  image id.
- **Red-team.** A Claude Code agent (Sonnet 5.5 on Max) gets only a container shell. It must make
  at least 10 successful calls over a checklist; up to 3 nudges re-send it when it stops early. A
  claimed find is re-run and checked by code.
- **New-file paths.** No file in the image mentions a path that only the gold patch creates, or
  every mention is reviewed. This catches build directories and caches made from the solved tree.

Limitations, accepted after astra review round 3:

| Limit | Status |
|---|---|
| CLEAN is bounded search evidence, not proof of semantic absence | A reformatted solution with none of the selected names would pass the deterministic layers; the red-team is the backstop |
| Git topology | Gitfile/commondir layouts are covered through the object stores they point to; the main repo must be a `.git` dir |
| Layer scan | Covers raw layer text and the git objects inside each layer. Pack/index split across layers is not reconstructed, and nested archives in lower layers are not decoded. Not reachable from inside the container |
| zstd/7z/rpm and archives nested deeper than 4 levels | Not decoded; review items unless toolchain/dependency fixtures |
| Symlinks | Not followed; they alias files already scanned |


## Single-needle hits (for manual review)

A location holding one needle is not a verdict (a generic line or a stock name can match an unrelated package). Solution-line (S) hits are listed one by one; identifier (I) and test (T) hits are counted.

- **deepswe/clack-async-autocomplete-options**: 6 (S 0, I 6, T 0)
- **deepswe/drizzle-orm-window-function-builders**: 2 (S 0, I 0, T 0)
- **deepswe/dynamodb-toolbox-lazy-recursive-schemas**: 1 (S 0, I 1, T 0)
- **deepswe/effect-sse-httpapi-streaming**: 2 (S 0, I 0, T 0)
- **deepswe/fastapi-deprecation-response-headers**: 7 (S 0, I 7, T 0)
- **deepswe/fastapi-implicit-head-options**: 1 (S 1, I 0, T 0)
  - S8 in 1 file(s), e.g. `/usr/local/lib/python3.12/site-packages/starlette/middleware/httpsredirect.py`
- **deepswe/happy-dom-abort-pending-body-reads**: 25 (S 0, I 25, T 0)
- **deepswe/helm-array-merge-strategies**: 38 (S 0, I 0, T 0)
- **deepswe/httpx-deterministic-cookie-store**: 81 (S 68, I 13, T 0)
  - S7 in 68 /app/.git: git object(s), e.g. `02fc0ddb6a279f72dd8ef1ff0c28853a53adda88`
- **deepswe/testem-bail-on-test-failure**: 2 (S 0, I 0, T 0)
- **deepswe/true-myth-iterable-collection-combinators**: 365 (S 0, I 300, T 0)
- **pro/instance_ansible__ansible-4c5ce5a1a9e79a845aff4978cfeb72a0d4ecf7d6-v1055803c3a812189a1133297f7f5468579283f86**: 729 (S 93, I 189, T 18)
  - S8 in 15 /app/.git: git object(s), e.g. `00b40b4adc80c29f3554c56fac4bc71a071b8acb`
  - S3 in 5 /app/.git: git object(s), e.g. `00bd927d3f57e09fd28ee0b91d12c5981d9a70bb`
  - S5 in 50 /app/.git: git object(s), e.g. `0300d866c31cb765f87bded2201542ebbabcdcc9`
  - S7 in 7 /app/.git: git object(s), e.g. `11a8f61fdb0bf2c804e089d0ad795ed66c7f0b14`
  - S2 in 5 /app/.git: git object(s), e.g. `12b7767593efc3e749df5f20891b2187a50c2fce`
  - S1 in 9 /app/.git: git object(s), e.g. `16549e67667b8973fade1bef5ea832abb1a2cad8`
  - S4 in 2 /app/.git: git object(s), e.g. `1e5ea81ac78e918b001b6e6319cf757ee79fbbb8`
- **pro/instance_ansible__ansible-c616e54a6e23fa5616a1d56d243f69576164ef9b-v1055803c3a812189a1133297f7f5468579283f86**: 82 (S 73, I 0, T 7)
  - S1 in 73 /app/.git: git object(s), e.g. `0525568b483dea109296af33c1d6c0017ed9c2e7`
- **pro/instance_ansible__ansible-e40889e7112ae00a21a2c74312b330e67a766cc0-v1055803c3a812189a1133297f7f5468579283f86**: 284 (S 113, I 146, T 23)
  - S1 in 44 /app/.git: git object(s), e.g. `0525568b483dea109296af33c1d6c0017ed9c2e7`
  - S7 in 13 /app/.git: git object(s), e.g. `0680fab2410d0e2a5861736036939775ad81adc4`
  - S6 in 39 /app/.git: git object(s), e.g. `0a040c7391ac0a893fdd6926be962ee4debf42c6`
  - S2 in 15 /app/.git: git object(s), e.g. `0a2f20a497802a0f4fab9f4e35ceaf13e2d7adaa`
  - S5 in 2 /app/.git: git object(s), e.g. `a1bf0265f6ae0ac440a76ba2f7b7a3a09ec526bf`
- **pro/instance_element-hq__element-web-d06cf09bf0b3d4a0fbe6bd32e4115caea2083168-vnan**: 69 (S 16, I 40, T 13)
  - S5 in 5 /app/.git: git object(s), e.g. `1859e90fd6bb1335764270104f1862ab7d8b9b23`
  - S6 in 4 /app/.git: git object(s), e.g. `26d43cf033009e6881539e489a9afc2993803441`
  - S4 in 7 /app/.git: git object(s), e.g. `57c2b6f0e7dece14ab92683739ae0a9bf8b08095`
- **pro/instance_element-hq__element-web-fe14847bb9bb07cab1b9c6c54335ff22ca5e516a-vnan**: 682 (S 34, I 200, T 66)
  - S5 in 3 /app/.git: git object(s), e.g. `03378d9492a22391ace0bff4cfceac8ee6f84a70`
  - S7 in 7 /app/.git: git object(s), e.g. `0e80d480751fd319c8dd3f0434dd737fffaab702`
  - S1 in 19 /app/.git: git object(s), e.g. `1515c408d7f2bb5978e4ec6cda1ac50abaff5dc2`
  - S2 in 2 /app/.git: git object(s), e.g. `22384cb40c2b55fd816eb8782857f64cbbcf2fa3`
  - S4 in 2 /app/.git: git object(s), e.g. `28cdd7230199b17b5860129dfdabbe6f5f2b366b`
  - S3 in 1 /app/.git: git object(s), e.g. `2ceca2d3ab9eee1fc329ab3a0d2de1fd1bf549e3`
- **pro/instance_flipt-io__flipt-96820c3ad10b0b2305e8877b6b303f7fafdf815f**: 14 (S 7, I 0, T 7)
  - S7 in 1 /app/.git: git object(s), e.g. `0229e1cd169c47e50759e485c5a4912aaae2cfbc`
  - S6 in 1 /app/.git: git object(s), e.g. `33be42f7a33ddea4c106b0ff731053383135be0a`
  - S5 in 2 /app/.git: git object(s), e.g. `4145de41430014ae25e40caa1dc2fac527867917`
  - S4 in 2 /app/.git: git object(s), e.g. `67c597d2d894ef54eb1077b6ca5d8a85ab084dd8`
  - S3 in 1 /app/.git: git object(s), e.g. `84f34efcc795134aeb8dfc52b3e0e4c3ee1b05bb`
- **pro/instance_flipt-io__flipt-c1728053367c753688f114ec26e703c8fdeda125**: 201 (S 52, I 6, T 11)
  - S1 in 42 /app/.git: git object(s), e.g. `0508461636448115c1e0c361ec6b328baeba4902`
  - S2 in 7 /app/.git: git object(s), e.g. `93b4cfb8f1a649b96a6beb406ac80423f5863789`
  - S5 in 2 /app/.git: git object(s), e.g. `c80879c992976c68aea661d9bd329acdb3cecb6c`
  - S6 in 1 /app/.git: git object(s), e.g. `fe050a449e9ffb05011173aba12cfae1bd20a918`
- **pro/instance_flipt-io__flipt-e5fe37c379e1eec2dd3492c5737c0be761050b26**: 293 (S 19, I 272, T 2)
  - S3 in 5 /app/.git: git object(s), e.g. `0a8d6ee99db03bbab6d51adc525790e4fcae2671`
  - S1 in 10 /app/.git: git object(s), e.g. `0d7f4c0b14b94cdfe457aac2c99f4d39dd717a4e`
  - S8 in 2 /app/.git: git object(s), e.g. `296f503fa327c9252958662861551e348a3faf4f`
  - S2 in 2 /app/.git: git object(s), e.g. `6596daf045cf3649e20796455cc16a6e566cf425`
- **pro/instance_future-architect__vuls-7eb77f5b5127c847481bcf600b4dca2b7a85cf3e**: 183 (S 30, I 15, T 6)
  - S7 in 20 /app/.git: git object(s), e.g. `06cc6d4c37ed1370b76488108b20cb515d196740`
  - S4 in 9 /app/.git: git object(s), e.g. `0c90218af9f87b8f5cb490b175ec97851106fa08`
  - S1 in 1 /app/.git: git object(s), e.g. `944f88472ed18c40bb3f1db483aacb5f77fd1220`
- **pro/instance_gravitational__teleport-d6ffe82aaf2af1057b69c61bf9df777f5ab5635a-vee9b09fb20c43af7e520f57e9239bbcf46b7113d**: 43 (S 21, I 22, T 0)
  - S6 in 21 /app/.git: git object(s), e.g. `141b02e55eebb1f95b7ee52715471b425efaa19f`
- **pro/instance_gravitational__teleport-f432a71a13e698b6e1c4672a2e9e9c1f32d35c12**: 3885 (S 0, I 300, T 0)
- **pro/instance_internetarchive__openlibrary-0a90f9f0256e4f933523e9842799e39f95ae29ce-v76304ecdb3a5954fcf13feb710e8c40fcf24b73c**: 358 (S 3, I 297, T 0)
  - S6 in 3 /app/.git: git object(s), e.g. `56bb0fb7048bb57f0c0cbbf8c8835d23b7ec179c`
- **pro/instance_internetarchive__openlibrary-4b7ea2977be2747496ba792a678940baa985f7ea-v0f5aece3601a5b4419f7ccec1dbda2071be28ee4**: 439 (S 5, I 295, T 0)
  - S1 in 4 /app/.git: git object(s), e.g. `35ac34c045811d455a10a06221f77b4a7e9fee76`
  - S7 in 1 /app/.git: git object(s), e.g. `703985233bfeb6f242204738904c019ac101f24f`
- **pro/instance_internetarchive__openlibrary-5de7de19211e71b29b2f2ba3b1dff2fe065d660f-v08d8e8889ec945ab821fb156c04c7d2e2810debb**: 82 (S 72, I 8, T 0)
  - S3 in 68 /app/.git: git object(s), e.g. `0009ac180187586963d3c5724ed2ae5fd1427878`
  - S2 in 4 /app/.git: git object(s), e.g. `2efb1e3a33219fce0a1ca871abf4bb39ebbc065e`
- **pro/instance_internetarchive__openlibrary-b67138b316b1e9c11df8a4a8391fe5cc8e75ff9f-ve8c8d62a2b60610a3c4631f5f23ed866bada9818**: 385 (S 18, I 265, T 17)
  - S4 in 3 /app/.git: git object(s), e.g. `0a8af475ec912fe313e65f6a69e47701c62cc9a8`
  - S1 in 7 /app/.git: git object(s), e.g. `20a09979bcf59a7da37c1b372fb3efab75ce4e75`
  - S7 in 8 /app/.git: git object(s), e.g. `99a1d2089efbbe3df04e804b816bd87593e33883`
- **pro/instance_internetarchive__openlibrary-d40ec88713dc95ea791b252f92d2f7b75e107440-v13642507b4fc1f8d234172bf8129942da2c2ca26**: 38 (S 0, I 38, T 0)
- **pro/instance_navidrome__navidrome-3972616585e82305eaf26aa25697b3f5f3082288**: 1974 (S 9, I 291, T 0)
  - S7 in 3 /app/.git: git object(s), e.g. `205861f902d42fb561269846ebc337c7d92649f0`
  - S1 in 2 /app/.git: git object(s), e.g. `88aa711ed22083d9ebe88b2871f918c0190a8cf6`
  - S6 in 4 file(s), e.g. `/go/pkg/mod/github.com/hashicorp/consul/api@v1.10.1/acl.go`
- **pro/instance_nodebb__nodebb-8168c6c40707478f71b8af60300830fe554c778c-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e**: 322 (S 4, I 296, T 0)
  - S3 in 4 /app/.git: git object(s), e.g. `277b75ccc459ade78e9760203d1cc65ac73765cc`
- **pro/instance_nodebb__nodebb-f083cd559d69c16481376868c8da65172729c0ca-vnan**: 190 (S 5, I 183, T 0)
  - S2 in 5 /app/.git: git object(s), e.g. `6e034b7d0cb28dc4e9e723c972c090b67c6db31c`
- **pro/instance_protonmail__webclients-e65cc5f33719e02e1c378146fb981d27bc24bdf4**: 391 (S 259, I 41, T 0)
  - S2 in 189 /app/.git: git object(s), e.g. `007d5416e3a0bbd9ece92bf91974cb6daa8d90ba`
  - S3 in 14 /app/.git: git object(s), e.g. `0546329b5cb7c1c9c39259a32ce699fb637e9bdc`
  - S7 in 21 /app/.git: git object(s), e.g. `0876293c34f2cae35c43e0ef41141b50ebd1cd0c`
  - S5 in 16 /app/.git: git object(s), e.g. `09961d5bd568f6c9a5af755ce5fe2088b15cad36`
  - S4 in 9 /app/.git: git object(s), e.g. `135ec430116e0a8636cd18328160f321521a57e7`
  - S8 in 10 /app/.git: git object(s), e.g. `1a70bb0a907bdbcb197d52b8ede25ff97fb175a5`
- **pro/instance_qutebrowser__qutebrowser-70248f256f93ed9b1984494d0a1a919ddd774892-v2ef375ac784985212b1805e1d0431dc8f1b3c171**: 2 (S 0, I 2, T 0)
- **pro/instance_qutebrowser__qutebrowser-96b997802e942937e81d2b8a32d08f00d3f4bc4e-v5fc38aaf22415ab0b70567368332beee7955b367**: 159 (S 4, I 155, T 0)
  - S7 in 4 /app/.git: git object(s), e.g. `258cc437d6d01ef047a93952eb0041979b369977`
- **pro/instance_qutebrowser__qutebrowser-bedc9f7fadf93f83d8dee95feeecb9922b6f063f-v2ef375ac784985212b1805e1d0431dc8f1b3c171**: 917 (S 55, I 0, T 245)
  - S4 in 11 /app/.git: git object(s), e.g. `000953d8b2377a6c06d25f8ec29fb99dba9cdc8e`
  - S1 in 41 /app/.git: git object(s), e.g. `0073f1c932a853c7784f451a51a26cb146a82c97`
  - S3 in 3 /app/.git: git object(s), e.g. `01234a42bd1a68daaf81c55de3e8612ff4f13da0`
- **pro/instance_qutebrowser__qutebrowser-deeb15d6f009b3ca0c3bd503a7cef07462bd16b4-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d**: 8 (S 1, I 6, T 1)
  - S1 in 1 /app/.git: git object(s), e.g. `55914efff7a2d0b8638db0a37a43ff37c1948dd9`
- **pro_rebuilt/instance_ansible__ansible-4c5ce5a1a9e79a845aff4978cfeb72a0d4ecf7d6-v1055803c3a812189a1133297f7f5468579283f86**: 4 (S 0, I 4, T 0)
- **pro_rebuilt/instance_element-hq__element-web-d06cf09bf0b3d4a0fbe6bd32e4115caea2083168-vnan**: 8 (S 0, I 8, T 0)
- **pro_rebuilt/instance_flipt-io__flipt-c1728053367c753688f114ec26e703c8fdeda125**: 132 (S 0, I 0, T 0)
- **pro_rebuilt/instance_flipt-io__flipt-e5fe37c379e1eec2dd3492c5737c0be761050b26**: 269 (S 0, I 269, T 0)
- **pro_rebuilt/instance_future-architect__vuls-7eb77f5b5127c847481bcf600b4dca2b7a85cf3e**: 138 (S 0, I 6, T 0)
- **pro_rebuilt/instance_gravitational__teleport-d6ffe82aaf2af1057b69c61bf9df777f5ab5635a-vee9b09fb20c43af7e520f57e9239bbcf46b7113d**: 21 (S 0, I 21, T 0)
- **pro_rebuilt/instance_gravitational__teleport-f432a71a13e698b6e1c4672a2e9e9c1f32d35c12**: 132 (S 0, I 0, T 0)
- **pro_rebuilt/instance_internetarchive__openlibrary-0a90f9f0256e4f933523e9842799e39f95ae29ce-v76304ecdb3a5954fcf13feb710e8c40fcf24b73c**: 347 (S 0, I 300, T 0)
- **pro_rebuilt/instance_internetarchive__openlibrary-5de7de19211e71b29b2f2ba3b1dff2fe065d660f-v08d8e8889ec945ab821fb156c04c7d2e2810debb**: 10 (S 0, I 8, T 0)
- **pro_rebuilt/instance_navidrome__navidrome-3972616585e82305eaf26aa25697b3f5f3082288**: 1932 (S 4, I 296, T 0)
  - S6 in 4 file(s), e.g. `/go/pkg/mod/github.com/hashicorp/consul/api@v1.10.1/acl.go`
- **pro_rebuilt/instance_nodebb__nodebb-8168c6c40707478f71b8af60300830fe554c778c-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e**: 2 (S 0, I 0, T 0)
- **pro_rebuilt/instance_nodebb__nodebb-f083cd559d69c16481376868c8da65172729c0ca-vnan**: 2 (S 0, I 0, T 0)
- **pro_rebuilt/instance_protonmail__webclients-e65cc5f33719e02e1c378146fb981d27bc24bdf4**: 3 (S 0, I 0, T 0)
- **pro_rebuilt/instance_qutebrowser__qutebrowser-96b997802e942937e81d2b8a32d08f00d3f4bc4e-v5fc38aaf22415ab0b70567368332beee7955b367**: 27 (S 0, I 27, T 0)
- **pro_rebuilt/instance_qutebrowser__qutebrowser-deeb15d6f009b3ca0c3bd503a7cef07462bd16b4-v363c8a7e5ccdf6968fc7ab84a2053ac78036691d**: 5 (S 0, I 5, T 0)
- **rf/task-69391d8d1ce51c407be1e531**: 6 (S 0, I 0, T 0)
- **rf/task-694b4b99829f00e24fd11885**: 16 (S 0, I 10, T 0)
- **rf/task-694b4b99829f00e24fd11889**: 6 (S 0, I 0, T 0)
- **rf/task-694b4b99829f00e24fd11891**: 68 (S 0, I 28, T 0)
- **rf/task-696719205599a51110d4b40c**: 6 (S 0, I 0, T 0)
- **rf/task-696719205599a51110d4b428**: 41 (S 0, I 0, T 0)
- **rf/task-696719205599a51110d4b434**: 40 (S 0, I 0, T 0)
- **rf/task-696719205599a51110d4b440**: 2 (S 0, I 2, T 0)
- **rf/task-696719205599a51110d4b441**: 100 (S 0, I 100, T 0)
- **rf/task-696719205599a51110d4b455**: 132 (S 0, I 0, T 0)
- **rf/task-696719205599a51110d4b458**: 132 (S 0, I 0, T 0)
- **rf/task-69b7c2a04b6f8ff9ed98812f**: 8 (S 0, I 0, T 0)
- **rf/task-69b7c2a04b6f8ff9ed988137**: 72 (S 0, I 62, T 0)
- **rf/task-69b7c2a04b6f8ff9ed98813b**: 1 (S 1, I 0, T 0)
  - S1 in 1 file(s), e.g. `/opt/netdata-build/.ninja_deps`
- **rf/task-69b7c2a04b6f8ff9ed98813d**: 9 (S 4, I 5, T 0)
  - S1 in 4 file(s), e.g. `/opt/netdata-build/.ninja_deps`
- **rf/task-69d196f015a150488265afb9**: 134 (S 0, I 2, T 0)
- **rf/task-69d196f015a150488265afc1**: 3 (S 0, I 0, T 0)
