"""sw.stream.testbench — PS-side end-to-end self-test harness (DS-P3.44).

Contract: sw/stream/testbench/requirements.yml (PS-LOOP-REQ-1..9).
Entry point: run_self_test.py — boots the wave_events_v1 tail thread,
runs every scenario in `scenarios/`, emits a verdict JSON.

This package is meant to ship inside the deepskopion rootfs at
/usr/local/share/deepskopion/testbench/ — see sw/linux/rootfs_overlay/
for the deploy hook.
"""
