/*
 * One-input libxmp property runner. The outer driver invokes this in a fresh
 * process for every malformed input so ASan/LSan failures are attributable to
 * exactly one file and its recorded mutation offset.
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "xmp.h"


static int invariant_fail(const char *name, long value, long limit)
{
	printf("INVARIANT field=%s value=%ld limit=%ld\n", name, value, limit);
	return -1;
}

static int check_module(const struct xmp_module *mod)
{
	int i, j;

	if (mod->len < 0 || mod->len > XMP_MAX_MOD_LENGTH)
		return invariant_fail("mod.len", mod->len, XMP_MAX_MOD_LENGTH);
	if (mod->chn < 0 || mod->chn > XMP_MAX_CHANNELS)
		return invariant_fail("mod.chn", mod->chn, XMP_MAX_CHANNELS);
	if (mod->pat < 0 || mod->pat > 65536)
		return invariant_fail("mod.pat", mod->pat, 65536);
	if (mod->trk < 0 || mod->trk > 1048576)
		return invariant_fail("mod.trk", mod->trk, 1048576);
	if (mod->ins < 0 || mod->ins > 65536)
		return invariant_fail("mod.ins", mod->ins, 65536);
	if (mod->smp < 0 || mod->smp > 65536)
		return invariant_fail("mod.smp", mod->smp, 65536);

	for (i = 0; i < mod->pat; i++) {
		if (mod->xxp[i] == NULL)
			return invariant_fail("pattern.null", i, mod->pat);
		if (mod->xxp[i]->rows < 0 || mod->xxp[i]->rows > 65536)
			return invariant_fail("pattern.rows", mod->xxp[i]->rows, 65536);
		for (j = 0; j < mod->chn; j++) {
			int trk = mod->xxp[i]->index[j];

			if (trk < 0 || trk >= mod->trk)
				return invariant_fail("pattern.track", trk, mod->trk);
		}
	}
	for (i = 0; i < mod->trk; i++) {
		if (mod->xxt[i] == NULL)
			return invariant_fail("track.null", i, mod->trk);
		if (mod->xxt[i]->rows < 0 || mod->xxt[i]->rows > 65536)
			return invariant_fail("track.rows", mod->xxt[i]->rows, 65536);
	}
	for (i = 0; i < mod->ins; i++) {
		const struct xmp_instrument *xxi = &mod->xxi[i];

		if (xxi->nsm < 0 || xxi->nsm > 65536)
			return invariant_fail("instrument.samples", xxi->nsm, 65536);
	}
	for (i = 0; i < mod->smp; i++) {
		const struct xmp_sample *xxs = &mod->xxs[i];

		if (xxs->len < 0)
			return invariant_fail("sample.len", xxs->len, 0x7fffffff);
	}
	return 0;
}

static int check_frame(const struct xmp_module *mod,
		       const struct xmp_frame_info *fi)
{
	int i;

	if (fi->pos < -1 || fi->pos >= mod->len)
		return invariant_fail("frame.pos", fi->pos, mod->len);
	if (fi->pattern < -1 || fi->pattern >= mod->pat)
		return invariant_fail("frame.pattern", fi->pattern, mod->pat);
	if (fi->num_rows < 0 || fi->num_rows > 65536)
		return invariant_fail("frame.num_rows", fi->num_rows, 65536);
	if (fi->row < 0 || (fi->num_rows > 0 && fi->row >= fi->num_rows))
		return invariant_fail("frame.row", fi->row, fi->num_rows);
	if (fi->speed <= 0 || fi->speed > 65536)
		return invariant_fail("frame.speed", fi->speed, 65536);
	if (fi->bpm < XMP_MIN_BPM || fi->bpm > 1000000)
		return invariant_fail("frame.bpm", fi->bpm, 1000000);

	(void)i;
	return 0;
}

static unsigned char *read_file(const char *path, size_t *size)
{
	FILE *f;
	unsigned char *buf;
	long len;

	f = fopen(path, "rb");
	if (f == NULL)
		return NULL;
	if (fseek(f, 0, SEEK_END) != 0) {
		fclose(f);
		return NULL;
	}
	len = ftell(f);
	if (len < 0 || fseek(f, 0, SEEK_SET) != 0) {
		fclose(f);
		return NULL;
	}
	buf = (unsigned char *)malloc(len > 0 ? (size_t)len : 1);
	if (buf == NULL) {
		fclose(f);
		return NULL;
	}
	if (len > 0 && fread(buf, 1, (size_t)len, f) != (size_t)len) {
		free(buf);
		fclose(f);
		return NULL;
	}
	fclose(f);
	*size = (size_t)len;
	return buf;
}

static void touch_public_state(xmp_context ctx)
{
	static const int seeks[] = {
		-1000000, -1, 0, 1, 16, 64, 255, 256, 32767, 1000000
	};
	struct xmp_frame_info fi;
	struct xmp_module_info mi;
	int i;

	xmp_get_module_info(ctx, &mi);
	xmp_get_frame_info(ctx, &fi);

	for (i = 0; i < (int)(sizeof(seeks) / sizeof(seeks[0])); i++) {
		xmp_set_position(ctx, seeks[i]);
		xmp_play_frame(ctx);
		xmp_set_row(ctx, seeks[i]);
		xmp_play_frame(ctx);
		xmp_seek_time(ctx, seeks[i]);
		xmp_play_frame(ctx);
	}

	xmp_set_player(ctx, XMP_PLAYER_INTERP, XMP_INTERP_SPLINE);
	xmp_set_player(ctx, XMP_PLAYER_MODE, XMP_MODE_AUTO);
	for (i = 0; i < 8; i++) {
		xmp_play_frame(ctx);
	}
	xmp_restart_module(ctx);
	xmp_play_frame(ctx);
	xmp_prev_position(ctx);
	xmp_play_frame(ctx);
	xmp_next_position(ctx);
	xmp_play_frame(ctx);
	xmp_stop_module(ctx);
	xmp_play_frame(ctx);
}

int main(int argc, char **argv)
{
	unsigned char *data;
	size_t size;
	struct xmp_test_info ti;
	xmp_context ctx;
	int test_ret;
	int load_ret = -XMP_ERROR_INVALID;
	int play_ret = -XMP_ERROR_STATE;
	int frames = 0;
	int invariant = 0;
	int i;

	if (argc != 2) {
		fprintf(stderr, "usage: %s <malformed-module>\n", argv[0]);
		return 2;
	}

	data = read_file(argv[1], &size);
	if (data == NULL) {
		fprintf(stderr, "cannot read %s\n", argv[1]);
		return 2;
	}

	test_ret = xmp_test_module_from_memory(data, (long)size, &ti);

	ctx = xmp_create_context();
	if (ctx == NULL) {
		free(data);
		return 2;
	}

	load_ret = xmp_load_module_from_memory(ctx, data, (long)size);
	if (load_ret == 0) {
		struct xmp_module_info mi;

		xmp_get_module_info(ctx, &mi);
		if (check_module(mi.mod) != 0)
			invariant = 1;
		if (xmp_start_player(ctx, 44100, 0) == 0) {
			xmp_set_player(ctx, XMP_PLAYER_MIX, 100);
			for (i = 0; i < 2048; i++) {
				struct xmp_frame_info fi;

				play_ret = xmp_play_frame(ctx);
				xmp_get_frame_info(ctx, &fi);
				if (!invariant && play_ret == 0 &&
				    check_frame(mi.mod, &fi) != 0)
					invariant = 1;
				frames++;
				if (play_ret != 0 || fi.loop_count > 2)
					break;
			}
			touch_public_state(ctx);
			xmp_end_player(ctx);
		}
		xmp_release_module(ctx);
	}
	xmp_free_context(ctx);
	free(data);

	printf("RESULT size=%zu test=%d load=%d play=%d frames=%d\n",
	       size, test_ret, load_ret, play_ret, frames);
	return invariant ? 3 : 0;
}
