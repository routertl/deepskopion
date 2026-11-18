/* SPDX-License-Identifier: MPL-2.0
 *
 * Native EDGE_BATCH drain for the DDR ring (DS-P1.74, decision DS-D.49).
 *
 * The Python drain (sw/stream/data_source.py DdrRingDataSource.next_batch)
 * decodes each EDGE_BATCH entry into one EDGE_EVENT stream word plus its
 * absolute time in ns, then hands both to Batch.add_edge. This file does the
 * same walk and the same per-edge arithmetic, and nothing else: frames other
 * than a non-error EDGE_BATCH stop the walk and go back to Python, so the wire
 * contract is unchanged.
 *
 * Mirrored from the Python sources (each line cites its origin):
 *   header  zs_msg.HEADER_STRUCT "<HBBBBHQHH", 20 bytes, magic 0x5A53
 *   entry   zs_msg.EDGE_EVENT_STRUCT "<HBB" (ts_delta, channel, flags)
 *   resync  data_source.py: bad magic -> advance 4 bytes
 *   partial data_source.py: a frame past the span end is left for the next call
 *   malformed EDGE_BATCH -> no edges, counted as resync bytes, cursor advances
 *   word    edge_capture.edge_event_word, ext rounded half-to-even (Python round)
 */
#include "edge_drain.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#define ZS_MAGIC          0x5A53u
#define ZS_HEADER_SIZE    20u
#define EDGE_ENTRY_SIZE   4u
#define DOMAIN_EDGE       0x6u
#define EVT_EDGE_BATCH    0x01u
#define SEV_ERROR         0x2u
#define EDGE_FLAG_E       0x01u
#define EDGE_FLAG_F       0x02u
#define EDGE_FLAG_S       0x04u
#define TS48_MASK         ((UINT64_C(1) << 48) - 1)
#define EDGE_ANCHOR_REACH_US 32768u   /* protocol.EDGE_ANCHOR_REACH_US */
#define TAG_EDGE_EVENTS   7u          /* protocol.TAG_EDGE_EVENTS */

static inline uint16_t rd16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }

static inline uint64_t rd64(const uint8_t *p)
{
    uint64_t v = 0;
    for (int i = 7; i >= 0; --i)
        v = (v << 8) | p[i];
    return v;
}

/* ext for every sub-microsecond remainder 0..999 ns at one clock: the
 * double expression of edge_capture.edge_event_word (round(rem / ns_per_cycle),
 * ties to even through nearbyint), evaluated once per clock instead of once per
 * edge (DS-P1.89). The table holds the same values the per-edge division gave,
 * so the words are unchanged; an exact integer rounding is NOT used because it
 * differs from the double expression at 198 of the 10^6 (clk_mhz, rem) pairs.
 * One table, refilled when the clock changes: the daemon walks from one thread. */
static uint8_t ext_lut[1000];
static uint32_t ext_lut_clk_mhz;

static const uint8_t *ext_table(uint32_t clk_mhz)
{
    if (ext_lut_clk_mhz != clk_mhz) {
        const double ns_per_cycle = 1000.0 / (double)clk_mhz;
        for (unsigned rem = 0; rem < 1000u; ++rem)
            ext_lut[rem] = (uint8_t)((uint32_t)(long long)nearbyint((double)rem / ns_per_cycle) & 0xFFu);
        ext_lut_clk_mhz = clk_mhz;
    }
    return ext_lut;
}

/* edge_capture.edge_event_word(instance, timestamp_ns, s, f, e, clk_mhz). */
static inline uint32_t edge_word(uint32_t instance, uint64_t ts_ns, uint64_t ts_us_full,
                                 uint32_t s, uint32_t f, uint32_t e, const uint8_t *ext_of)
{
    uint32_t ext = ext_of[(uint32_t)(ts_ns - ts_us_full * 1000u)];
    uint32_t ts_us = (uint32_t)(ts_us_full & 0xFFFFu);
    return ((instance & 0x1Fu) << 27) | ((s & 1u) << 26) | ((f & 1u) << 25)
         | ((e & 1u) << 24) | (ts_us << 8) | ext;
}

struct ds_frames;
static inline void frames_push(struct ds_frames *f, uint32_t word, uint64_t ts_ns,
                               uint64_t ts_us);

/* The walk. With f set, edges go to f (capacity ensured by the caller) and
 * out/cap are unused; otherwise they go to out. */
static inline void walk(const uint8_t *span, size_t len, const struct ds_drain_cfg *cfg,
                        struct ds_edge *out, size_t cap, struct ds_frames *f,
                        struct ds_drain_result *res)
{
    size_t cursor = 0;
    size_t n_out = 0;
    const uint8_t *ext_of = ext_table(cfg->clk_mhz);

    memset(res, 0, sizeof(*res));
    res->stop = DS_DRAIN_STOP_END;

    int after_frame = 0;   /* data_source.py after_frame: the cursor sits right after a walked frame */

    while (cursor + ZS_HEADER_SIZE <= len && res->frames < cfg->max_frames) {
        const uint8_t *h = span + cursor;
        if (rd16(h) != ZS_MAGIC) {
            /* DS-P2.281: a zero word right after a frame may be PL zero pad (DS-P2.280). The pad rule needs
             * the ring word width, which this walk does not have: stop here and let Python's _pad_run take
             * it or resync it, as its own walk does. Any other bad word is resynced here, 4 bytes. */
            if (after_frame && (h[0] | h[1] | h[2] | h[3]) == 0) {
                res->stop = DS_DRAIN_STOP_OTHER_FRAME;
                break;
            }
            cursor += 4;
            res->resync_bytes += 4;
            after_frame = 0;
            continue;
        }
        uint8_t datalogger_id = h[4];
        uint16_t msg_type = rd16(h + 6);
        uint64_t anchor = rd64(h + 8);
        uint16_t payload_len = rd16(h + 16);
        size_t frame_bytes = ZS_HEADER_SIZE + payload_len;
        if (cursor + frame_bytes > len) {
            res->stop = DS_DRAIN_STOP_PARTIAL;
            break;
        }
        uint32_t severity = (msg_type >> 12) & 0xFu;
        uint32_t domain = (msg_type >> 8) & 0xFu;
        uint32_t event_id = msg_type & 0xFFu;
        if (!(domain == DOMAIN_EDGE && severity != SEV_ERROR && event_id == EVT_EDGE_BATCH)) {
            res->stop = DS_DRAIN_STOP_OTHER_FRAME;
            break;
        }
        size_t n = payload_len / EDGE_ENTRY_SIZE;
        int ok = payload_len != 0 && (payload_len % EDGE_ENTRY_SIZE) == 0;
        const uint8_t *p = h + ZS_HEADER_SIZE;
        for (size_t i = 0; ok && i < n; ++i)
            if (p[i * EDGE_ENTRY_SIZE + 2] != datalogger_id)
                ok = 0;
        if (!ok) {
            res->resync_bytes += frame_bytes;
        } else {
            if (!f && n_out + n > cap) {
                res->stop = DS_DRAIN_STOP_FULL;
                break;
            }
            /* The frame's anchor in ns, whole us and ns remainder, once per
             * frame: per edge, (a48 + d) * npt = base_ns + d * npt, and
             * d * npt (d < 2^16, npt <= 2^16) plus the remainder fits in 32
             * bits, so the us split is a 32-bit divide by the constant 1000
             * instead of a 64-bit library divide (DS-P1.89). Anchors within
             * 0xFFFF ticks of the 48-bit wrap, and npt > 2^16, keep the
             * per-edge 64-bit path. */
            const uint64_t a48 = anchor & TS48_MASK;
            const uint64_t npt = cfg->ns_per_tick;
            const int fast = npt <= 65536u && a48 <= TS48_MASK - 0xFFFFu;
            const uint64_t base_ns = a48 * npt;
            const uint64_t base_us = base_ns / 1000u;
            const uint32_t base_rem = (uint32_t)(base_ns - base_us * 1000u);
            for (size_t i = 0; i < n; ++i, p += EDGE_ENTRY_SIZE) {
                uint64_t ts_ns, ts_us;
                if (fast) {
                    uint32_t step = (uint32_t)rd16(p) * (uint32_t)npt;
                    ts_ns = base_ns + step;
                    ts_us = base_us + (base_rem + step) / 1000u;
                } else {
                    ts_ns = ((anchor + rd16(p)) & TS48_MASK) * npt;
                    ts_us = ts_ns / 1000u;
                }
                uint32_t flags = p[3];
                uint32_t word = edge_word(p[2], ts_ns, ts_us, (flags & EDGE_FLAG_S) != 0,
                                          (flags & EDGE_FLAG_F) != 0,
                                          (flags & EDGE_FLAG_E) != 0, ext_of);
                if (f) {
                    frames_push(f, word, ts_ns, ts_us);
                } else {
                    out[n_out].word = word;
                    out[n_out].ts_ns = ts_ns;
                }
                ++n_out;
            }
        }
        if (!res->has_first_ts) {
            res->has_first_ts = 1;
            res->first_ts_ticks = anchor & TS48_MASK;
        }
        cursor += frame_bytes;
        ++res->frames;
        after_frame = 1;
    }
    if (res->stop == DS_DRAIN_STOP_END && res->frames >= cfg->max_frames
        && cursor + ZS_HEADER_SIZE <= len)
        res->stop = DS_DRAIN_STOP_MAX_FRAMES;
    res->consumed = cursor;
    res->edges = n_out;
}

int ds_edge_drain(const uint8_t *span, size_t len, const struct ds_drain_cfg *cfg,
                  struct ds_edge *out, size_t cap, struct ds_drain_result *res)
{
    walk(span, len, cfg, out, cap, NULL, res);
    return 0;
}

/* ---- frame cut and wire encoding ------------------------------------- */

struct ds_frame {
    uint64_t anchor_ns;
    uint64_t anchor_us;  /* anchor_ns / 1000, for the reach test */
    size_t start;
    size_t count;
};

struct ds_frames {
    uint32_t *words;
    size_t n_words, cap_words;
    struct ds_frame *frames;
    size_t n_frames, cap_frames;
};

struct ds_frames *ds_frames_new(void)
{
    return calloc(1, sizeof(struct ds_frames));
}

void ds_frames_free(struct ds_frames *f)
{
    if (!f)
        return;
    free(f->words);
    free(f->frames);
    free(f);
}

static int grow(void **p, size_t *cap, size_t need, size_t elem)
{
    if (need <= *cap)
        return 0;
    size_t n = *cap ? *cap : 1024;
    while (n < need)
        n *= 2;
    void *q = realloc(*p, n * elem);
    if (!q)
        return -1;
    *p = q;
    *cap = n;
    return 0;
}

/* Room for `more` edges: words, and a frame per edge at worst. */
static int reserve(struct ds_frames *f, size_t more)
{
    if (grow((void **)&f->words, &f->cap_words, f->n_words + more, sizeof *f->words))
        return -1;
    return grow((void **)&f->frames, &f->cap_frames, f->n_frames + more, sizeof *f->frames);
}

/* Batch.add_edge with data_source.edge_within_reach: the edge joins the open
 * frame while |ts_us - anchor_us| < EDGE_ANCHOR_REACH_US, otherwise it opens a
 * frame anchored on itself. Capacity is the caller's (reserve). */
static inline void frames_push(struct ds_frames *f, uint32_t word, uint64_t ts_ns,
                               uint64_t t)
{
    struct ds_frame *open = f->n_frames ? &f->frames[f->n_frames - 1] : NULL;
    if (!open || (t > open->anchor_us ? t - open->anchor_us : open->anchor_us - t)
                     >= EDGE_ANCHOR_REACH_US) {
        open = &f->frames[f->n_frames++];
        open->anchor_ns = ts_ns;
        open->anchor_us = t;
        open->start = f->n_words;
        open->count = 0;
    }
    f->words[f->n_words++] = word;
    ++open->count;
}

int ds_frames_add(struct ds_frames *f, uint32_t word, uint64_t ts_ns)
{
    if (reserve(f, 1))
        return -1;
    frames_push(f, word, ts_ns, ts_ns / 1000u);
    return 0;
}

int ds_frames_drain(struct ds_frames *f, const uint8_t *span, size_t len,
                    const struct ds_drain_cfg *cfg, struct ds_drain_result *res)
{
    /* Every entry is 4 bytes after a 20-byte header, so a span holds fewer than len/4. */
    if (reserve(f, len / EDGE_ENTRY_SIZE + 1))
        return -1;
    walk(span, len, cfg, NULL, 0, f, res);
    return 0;
}

size_t ds_frames_count(const struct ds_frames *f) { return f->n_frames; }
size_t ds_frames_edges(const struct ds_frames *f) { return f->n_words; }

int ds_frames_info(const struct ds_frames *f, size_t i, uint64_t *anchor_ns, size_t *n_words)
{
    if (i >= f->n_frames)
        return -1;
    *anchor_ns = f->frames[i].anchor_ns;
    *n_words = f->frames[i].count;
    return 0;
}

size_t ds_frames_words(const struct ds_frames *f, size_t i, uint32_t *out, size_t cap)
{
    if (i >= f->n_frames || cap < f->frames[i].count)
        return 0;
    memcpy(out, f->words + f->frames[i].start, f->frames[i].count * sizeof *out);
    return f->frames[i].count;
}

/* msgpack, as msgpack-python packs these values: the smallest unsigned form
 * for a non-negative int, fixstr for the short keys, big-endian lengths. */
static uint8_t *put_be(uint8_t *p, uint64_t v, int n)
{
    for (int k = n - 1; k >= 0; --k)
        *p++ = (uint8_t)(v >> (8 * k));
    return p;
}

static uint8_t *put_uint(uint8_t *p, uint64_t v)
{
    if (v < 0x80u) { *p++ = (uint8_t)v; return p; }
    if (v <= 0xFFu) { *p++ = 0xcc; return put_be(p, v, 1); }
    if (v <= 0xFFFFu) { *p++ = 0xcd; return put_be(p, v, 2); }
    if (v <= 0xFFFFFFFFu) { *p++ = 0xce; return put_be(p, v, 4); }
    *p++ = 0xcf;
    return put_be(p, v, 8);
}

static uint8_t *put_key(uint8_t *p, const char *s)
{
    size_t n = strlen(s);  /* every key is under 32 bytes: fixstr */
    *p++ = (uint8_t)(0xa0u | n);
    memcpy(p, s, n);
    return p + n;
}

size_t ds_frames_pack_bound(const struct ds_frames *f, size_t i)
{
    if (i >= f->n_frames)
        return 0;
    /* array, tag, map, 4 keys (4+8+10+6), 3 uint64, array header, words */
    return 3 + 28 + 3 * 9 + 5 + 5 * f->frames[i].count;
}

size_t ds_frames_pack(const struct ds_frames *f, size_t i, uint64_t seq, uint64_t clk_mhz,
                      uint8_t *out, size_t cap)
{
    if (i >= f->n_frames || cap < ds_frames_pack_bound(f, i))
        return 0;
    const struct ds_frame *fr = &f->frames[i];
    uint8_t *p = out;
    *p++ = 0x92;              /* [tag, payload] */
    *p++ = TAG_EDGE_EVENTS;
    *p++ = 0x84;              /* proto.make_edge_events: four keys, in this order */
    p = put_key(p, "seq");
    p = put_uint(p, seq);
    p = put_key(p, "clk_mhz");
    p = put_uint(p, clk_mhz);
    p = put_key(p, "anchor_ns");
    p = put_uint(p, fr->anchor_ns);
    p = put_key(p, "words");
    if (fr->count < 16) {
        *p++ = (uint8_t)(0x90u | fr->count);
    } else if (fr->count <= 0xFFFFu) {
        *p++ = 0xdc;
        p = put_be(p, fr->count, 2);
    } else {
        *p++ = 0xdd;
        p = put_be(p, fr->count, 4);
    }
    const uint32_t *w = f->words + fr->start;
    for (size_t k = 0; k < fr->count; ++k)
        p = put_uint(p, w[k]);
    return (size_t)(p - out);
}
