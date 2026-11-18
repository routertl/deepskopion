/* SPDX-License-Identifier: MPL-2.0
 *
 * edge_drain_bench <dump> <sample_clk_hz> [--out records.bin] [--repeat N]
 *
 * Decodes a captured or generated DDR ring dump with ds_edge_drain, walking it
 * the way DdrRingDataSource.next_batch does: span by span, a 1 MiB span cap
 * and 4096 frames per call. --out writes one little-endian <IQ> record
 * (word, ts_ns) per edge, the format the Python oracle writes. --repeat times
 * N full decodes of the dump with CLOCK_MONOTONIC and prints ns per edge.
 * A frame that is not a non-error EDGE_BATCH ends the walk, and its offset is
 * printed as other_frame_at: in the daemon such frames go back to the Python
 * path. Exit 3 means the output array filled.
 */
#include "edge_drain.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define SPAN_CAP (1u << 20)
#define MAX_FRAMES 4096u

static double now_ns(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec * 1e9 + (double)t.tv_nsec;
}

/* One full pass over the dump. Returns the edge count, or -1 on a stop that
 * is not END / PARTIAL / MAX_FRAMES. */
static long pass(const uint8_t *buf, size_t len, const struct ds_drain_cfg *cfg,
                 struct ds_edge *out, size_t cap, FILE *rec, size_t *resync, long *other_at)
{
    size_t ofs = 0;
    long total = 0;
    *resync = 0;
    *other_at = -1;
    while (ofs < len) {
        size_t span = len - ofs < SPAN_CAP ? len - ofs : SPAN_CAP;
        struct ds_drain_result r;
        ds_edge_drain(buf + ofs, span, cfg, out, cap, &r);
        if (rec) {
            for (size_t i = 0; i < r.edges; ++i) {
                uint8_t b[12];
                for (int k = 0; k < 4; ++k) b[k] = (uint8_t)(out[i].word >> (8 * k));
                for (int k = 0; k < 8; ++k) b[4 + k] = (uint8_t)(out[i].ts_ns >> (8 * k));
                fwrite(b, 1, sizeof b, rec);
            }
        }
        total += (long)r.edges;
        *resync += r.resync_bytes;
        if (r.stop == DS_DRAIN_STOP_OTHER_FRAME) {
            *other_at = (long)(ofs + r.consumed);
            return total;
        }
        if (r.stop == DS_DRAIN_STOP_FULL) {
            fprintf(stderr, "output full at byte %zu\n", ofs + r.consumed);
            return -1;
        }
        if (r.consumed == 0)
            break;  /* a trailing partial frame or fewer than 20 bytes */
        ofs += r.consumed;
    }
    return total;
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s <dump> <sample_clk_hz> [--out f] [--repeat N]\n", argv[0]);
        return 2;
    }
    const char *out_path = NULL;
    long repeat = 0;
    for (int i = 3; i < argc; ++i) {
        if (!strcmp(argv[i], "--out") && i + 1 < argc) out_path = argv[++i];
        else if (!strcmp(argv[i], "--repeat") && i + 1 < argc) repeat = atol(argv[++i]);
        else { fprintf(stderr, "unknown argument %s\n", argv[i]); return 2; }
    }
    FILE *f = fopen(argv[1], "rb");
    if (!f) { perror(argv[1]); return 2; }
    fseek(f, 0, SEEK_END);
    long len = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *buf = malloc((size_t)len);
    if (!buf || fread(buf, 1, (size_t)len, f) != (size_t)len) { perror("read"); return 2; }
    fclose(f);

    unsigned long long clk_hz = strtoull(argv[2], NULL, 10);
    struct ds_drain_cfg cfg = {
        .ns_per_tick = 1000000000ull / clk_hz,
        .clk_mhz = (uint32_t)(clk_hz / 1000000ull),
        .max_frames = MAX_FRAMES,
    };
    size_t cap = SPAN_CAP / 4;  /* every entry is 4 bytes, so a span holds fewer */
    struct ds_edge *out = malloc(cap * sizeof *out);
    if (!out) { perror("malloc"); return 2; }

    size_t resync = 0;
    long other_at = -1;
    FILE *rec = out_path ? fopen(out_path, "wb") : NULL;
    if (out_path && !rec) { perror(out_path); return 2; }
    long edges = pass(buf, (size_t)len, &cfg, out, cap, rec, &resync, &other_at);
    if (rec) fclose(rec);
    if (edges < 0) return 3;
    printf("bytes=%ld edges=%ld resync_bytes=%zu other_frame_at=%ld\n", len, edges, resync,
           other_at);

    if (repeat > 0) {
        double best = 1e300, sum = 0;
        for (long r = 0; r < repeat; ++r) {
            double t0 = now_ns();
            long e = pass(buf, (size_t)len, &cfg, out, cap, NULL, &resync, &other_at);
            double dt = now_ns() - t0;
            if (e != edges) { fprintf(stderr, "edge count drift\n"); return 4; }
            sum += dt;
            if (dt < best) best = dt;
        }
        printf("repeat=%ld ns_per_edge_best=%.2f ns_per_edge_mean=%.2f "
               "MB_per_s_best=%.1f\n", repeat, best / (double)edges,
               sum / (double)repeat / (double)edges, (double)len / best * 1e3);
    }
    free(out);
    free(buf);
    return 0;
}
