/* SPDX-License-Identifier: MPL-2.0
 * Native EDGE_BATCH drain for the DDR ring (DS-P1.74, decision DS-D.49). */
#ifndef DS_EDGE_DRAIN_H
#define DS_EDGE_DRAIN_H

#include <stddef.h>
#include <stdint.h>

/* One decoded edge: the arguments the Python drain passes to Batch.add_edge. */
struct ds_edge {
    uint32_t word;   /* EDGE_EVENT stream word (edge_capture.edge_event_word) */
    uint64_t ts_ns;  /* absolute time: (anchor + ts_delta) & 48 bits, times ns_per_tick */
};

struct ds_drain_cfg {
    uint64_t ns_per_tick;  /* data_source.py: 1_000_000_000 // sample_clk_hz */
    uint32_t clk_mhz;      /* data_source.py: sample_clk_hz // 1_000_000 */
    size_t max_frames;     /* data_source.py: _MAX_FRAMES_PER_TICK */
    /* DS-P1.117: the PL zero-pad rule of data_source.py _pad_run (DS-P2.280), so a pad directly after a
     * frame is consumed here instead of returning to Python once per frame. ring_word_bytes 0 = undeclared:
     * the walk stops at such a zero word as before (STOP_OTHER_FRAME). Appended fields: ABI version 2. */
    size_t ring_word_bytes;  /* data_source.py: _ring_word_bytes (G_DDR_DATA_WIDTH / 8), or 0 */
    size_t ring_bytes;       /* data_source.py: _ring_bytes; 0 = no ring wrap applied */
    size_t span_ofs;         /* ring byte offset of span[0]: (_read_byte_ofs + cursor) % _ring_bytes */
};

enum ds_drain_stop {
    DS_DRAIN_STOP_END = 0,        /* fewer than a header's bytes left */
    DS_DRAIN_STOP_PARTIAL,        /* a frame, or a zero run that may be pad (its word boundary lies
                                   * past the span end, DS-P1.117), runs past the span: next call */
    DS_DRAIN_STOP_OTHER_FRAME,    /* a frame that is not a non-error EDGE_BATCH, or a zero word
                                   * right after a frame that is not pad by the ring_word_bytes rule
                                   * (or with ring_word_bytes 0): Python's */
    DS_DRAIN_STOP_FULL,           /* the output array is full */
    DS_DRAIN_STOP_MAX_FRAMES,     /* cfg->max_frames reached */
};

struct ds_drain_result {
    size_t consumed;       /* bytes of span walked; the next call starts here */
    size_t frames;         /* frames walked, EDGE_BATCH and malformed */
    size_t edges;          /* entries written to out */
    size_t resync_bytes;   /* bad-magic words plus malformed frames */
    enum ds_drain_stop stop;
    int has_first_ts;         /* 1 when at least one frame was walked */
    uint64_t first_ts_ticks;  /* header timestamp & 48 bits of the first frame walked:
                               * data_source.py seeds _pl_ts_anchor_ticks from it */
    size_t pad_bytes;         /* DS-P1.117: PL zero pad consumed (counted in consumed, not in resync_bytes) */
    int after_frame;          /* DS-P1.117: 1 when the walk ended right after a frame (data_source.py after_frame) */
};

/* The layout version of ds_drain_cfg / ds_drain_result: 2 since DS-P1.117 appended fields to both.
 * edge_frames.py refuses a library that reports another version, or none. */
#define DS_EDGE_DRAIN_ABI 2
int ds_edge_drain_abi(void);

int ds_edge_drain(const uint8_t *span, size_t len, const struct ds_drain_cfg *cfg,
                  struct ds_edge *out, size_t cap, struct ds_drain_result *res);

/* One batch's edges cut into wire EDGE_EVENTS frames (Batch.add_edge +
 * edge_within_reach, DS-P3.138), and each frame packed as the msgpack bytes
 * proto.pack_frame(TAG_EDGE_EVENTS, proto.make_edge_events(...)) produces. */
struct ds_frames;

struct ds_frames *ds_frames_new(void);
void ds_frames_free(struct ds_frames *f);
/* Append one edge; it joins the open frame while within reach of its anchor,
 * otherwise it opens a new frame anchored on itself. 0, or -1 on no memory. */
int ds_frames_add(struct ds_frames *f, uint32_t word, uint64_t ts_ns);
/* ds_edge_drain over span, every edge appended with ds_frames_add.
 * 0, or -1 on no memory (res then describes nothing). */
int ds_frames_drain(struct ds_frames *f, const uint8_t *span, size_t len,
                    const struct ds_drain_cfg *cfg, struct ds_drain_result *res);
size_t ds_frames_count(const struct ds_frames *f);
size_t ds_frames_edges(const struct ds_frames *f);
/* Frame i's anchor (absolute ns of its first edge) and word count; -1 if no frame i. */
int ds_frames_info(const struct ds_frames *f, size_t i, uint64_t *anchor_ns, size_t *n_words);
/* Copies frame i's words to out; returns the count copied (0 if cap is short). */
size_t ds_frames_words(const struct ds_frames *f, size_t i, uint32_t *out, size_t cap);
/* Bytes ds_frames_pack needs at most for frame i. */
size_t ds_frames_pack_bound(const struct ds_frames *f, size_t i);
/* Writes [7, {"seq", "clk_mhz", "anchor_ns", "words"}] for frame i as msgpack.
 * Returns the bytes written, or 0 if there is no frame i or cap is short. */
size_t ds_frames_pack(const struct ds_frames *f, size_t i, uint64_t seq, uint64_t clk_mhz,
                      uint8_t *out, size_t cap);

#endif
