# Vendored third-party code (I1a)

Both copies are verbatim, never edited, and excluded from ruff (`pyproject.toml`).

| Directory | Upstream | Commit | License | Why vendored |
|---|---|---|---|---|
| `workspace-bench/` | https://github.com/camilablank/workspace-bench | `92d763e722377f5bfae045e829a308ab5919a820` (GitHub tarball, sha256 `034d855a0d3df8b7ccd1bdabe7f9ad14609f00066472d5260794a7d01edb64d1`, fetched 2026-09-29) | MIT | The benchmark must run from source: its installed wheel omits the `evals/` item banks and reference readouts (`REPO_ROOT/evals`). Installed editable through `[tool.uv.sources]`. `src/wsbench` is byte-identical to the git-installed wheel at the same commit. |
| `olens_draft/` | https://github.com/agu18dec/olens_draft | `a32590085a6070d75f950155d3c2b3db3eec97ef` (fetched 2026-09-29) | MIT | Reference for the reconstructor loader, whitening and joint bullet-FVE reward (the public equivalent of the private `global_workspace` package the Oracle Lens model card imports). Only the files the I1a port reads are copied: `oracle_lens/{model,readout_text}.py`, `core/{whitening,nnomp,reconstructor}.py`, `pipeline/{ar_loader,rl_reward,multilayer,multilayer_reconstructor,scorer,verbalizer}.py`. Not installed; `src/emx/i1a/fve.py` ports the functions it needs and cites these paths. |
