# rr_ddr_stream Intel/Altera XPM-body stubs

Vendored verbatim from `routertl/rr_ddr_stream`'s upstream `rtl/support/intel/`
directory at commit `2319261cb86f39b3a91d37cff0e716b12ea65112` (v0.3.7) — the SAME
commit `ip.lock` and the target locks (`targets/<t>.routertl.lock`) pin for this project's
`routertl/rr_ddr_stream` dependency (see the `routertl/rr_ddr_stream:` entry in either lockfile). Pure
IEEE entity-declaration stubs: no `library xpm;`, no `library unisim;`, no
vendor primitive of any kind.

They exist so `tools/sim/pre_sim_seed_libs.py` can seed a non-Xilinx target's
nvc `rr` work library with an XPM-free `rr_fifo_sync_xpm_body` /
`rr_fifo_async_mixed_xpm_body` entity — satisfying the SAME static
entity-binding requirement (nvc, and Quartus on real hardware) that makes the
Xilinx build seed the real XPM-backed bodies, without ever naming a vendor
library. See that script's module docstring for the full mechanism, and
`libs/routertl/rr_ddr_stream/ip.yml`'s `synthesis.sources_per_vendor.altera`
key, which already declares this exact substitution upstream — a substitution
this SDK build does not act on for either `--print-sources` or the sim
compile, which is why deepskopion carries its own copy here rather than
relying on the resolver to pick it.

Deliberately live UNDER `tools/`, not `libs/`:

* `libs/routertl/rr_ddr_stream/rtl/support/intel/` is the registry package's
  own `local_path`. The three files are required by the DE25 build: Quartus
  statically binds the untaken XPM generate branch even when
  `fifo_impl_g="olo"`. They were not dropped upstream: the pinned `6d1089a`
  package declares them through
  `ip.yml`'s `synthesis.sources_per_vendor.{intel,altera}`, and upstream
  `6d1089a` still ships them.
* RouteRTL 4.9.2 derived that auxiliary-file slice from the active target's
  `hardware.vendor`. 4.9.3 vendors every `sources_per_vendor`
  slice and records `synth_only_files_by_vendor`, so `rr pkg install` and
  `rr target set` must leave these three files in `libs/` on every target.
  A regression that deletes them is guarded by
  `tools/tests/test_pkg_install_clean_clone_p3_150.py`.
* `libs/local/<anything-not-a-declared-package>/` trips
  `package_resolver.assert_lock_matches_libs` ("ip.lock out of sync with
  libs/ ... drifted (untracked extras)") — measured: it refused to run the
  simulation and offered to `rm -rf` this directory.
* Every target's `paths.sources` includes `libs` for `auto_sources`
  auto-discovery (zybo_datalogger.yml and the DE25 core baseline target both do), so
  anything placed under `libs/` gets swept into the compile set a second
  time on top of this hook's explicit nvc analyse call — measured: nvc
  reported "Name collision: 'rr_fifo_sync_xpm_body' defined in both ...
  intel_stubs/... and ... xilinx/... — last one wins", i.e. the auto-swept
  real XPM body silently won over the stub this hook seeded.

`tools/` is outside every target's `paths.sources`, so none of the above
applies; only this hook's explicit `nvc -a` call ever touches these files.

Do not hand-edit these files. If `routertl/rr_ddr_stream` is ever re-pinned
to a newer commit, re-vendor from that commit's `rtl/support/intel/` instead.
