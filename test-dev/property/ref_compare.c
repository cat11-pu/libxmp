/*
 * Compare one valid module's libxmp replay state with an OpenMPT-generated
 * reference trace. The trace contains one row per active sample/channel and
 * replay frame, so the comparison is both per-sample and per-frame.
 *
 * Output on mismatch:
 *   MISMATCH module=... trace=... line=N field=... expected=... actual=...
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../src/common.h"
#include "../../src/player.h"
#include "../../src/mixer.h"
#include "../../src/virtual.h"


static int map_channel(struct player_data *p, int chn)
{
	int voc;

	if (chn < 0 || (uint32)chn >= (uint32)p->virt.virt_channels)
		return -1;
	voc = p->virt.virt_channel[chn].map;
	if (voc < 0 || (uint32)voc >= (uint32)p->virt.maxvoc)
		return -1;
	return voc;
}

static void mismatch(const char *mod, const char *trace, int line,
		     const char *field, long expected, long actual)
{
	printf("MISMATCH module=%s trace=%s line=%d field=%s expected=%ld actual=%ld\n",
	       mod, trace, line, field, expected, actual);
}

int main(int argc, char **argv)
{
	const char *mod, *trace;
	FILE *f;
	xmp_context opaque;
	struct context_data *ctx;
	struct player_data *p;
	struct mixer_voice *vi;
	struct xmp_frame_info fi;
	char line_buf[512];
	int line_no = 0;
	int i, voc, ret;
	int max_channels;
	int mode = XMP_MODE_AUTO;
	int ignore_rv = 0;

	if (argc < 3 || argc > 5) {
		fprintf(stderr, "usage: %s <module> <openmpt-trace> [mode] [ignore_rv]\n",
			argv[0]);
		return 2;
	}
	mod = argv[1];
	trace = argv[2];
	if (argc >= 4)
		mode = atoi(argv[3]);
	if (argc >= 5)
		ignore_rv = atoi(argv[4]);

	f = fopen(trace, "r");
	if (f == NULL) {
		fprintf(stderr, "cannot open trace %s\n", trace);
		return 2;
	}
	opaque = xmp_create_context();
	if (opaque == NULL) {
		fclose(f);
		return 2;
	}
	ret = xmp_load_module(opaque, mod);
	if (ret != 0) {
		printf("LOAD_FAIL module=%s ret=%d\n", mod, ret);
		xmp_free_context(opaque);
		fclose(f);
		return 1;
	}

	ctx = (struct context_data *)opaque;
	p = &ctx->p;

	if (xmp_start_player(opaque, 44100, 0) != 0) {
		printf("START_FAIL module=%s\n", mod);
		xmp_release_module(opaque);
		xmp_free_context(opaque);
		fclose(f);
		return 1;
	}
	xmp_set_player(opaque, XMP_PLAYER_MIX, 100);
	xmp_set_player(opaque, XMP_PLAYER_MODE, mode);
	max_channels = p->virt.virt_channels;

	for (;;) {
		xmp_play_frame(opaque);
		xmp_get_frame_info(opaque, &fi);
		if (fi.loop_count >= 1)
			break;

		for (i = 0; i < max_channels; i++) {
			struct channel_data *xc = &p->xc_data[i];
			int time, row, frame, chan, period, note, ins, vol, pan;
			int pos0, cutoff, resonance, num;

			voc = map_channel(p, i);
			if (voc < 0 || TEST_NOTE(NOTE_SAMPLE_END))
				continue;
			vi = &p->virt.voice_array[voc];

			if (fgets(line_buf, sizeof(line_buf), f) == NULL) {
				printf("MISMATCH module=%s trace=%s line=%d field=eof expected=row actual=missing\n",
				       mod, trace, line_no + 1);
				goto fail;
			}
			line_no++;
			num = sscanf(line_buf, "%d %d %d %d %d %d %d %d %d %d %d %d",
				     &time, &row, &frame, &chan, &period, &note,
				     &ins, &vol, &pan, &pos0, &cutoff, &resonance);
			if (num < 10) {
				printf("TRACE_BAD module=%s trace=%s line=%d fields=%d\n",
				       mod, trace, line_no, num);
				goto fail;
			}

			if (abs(fi.time - time) > 1) {
				mismatch(mod, trace, line_no, "time", time, fi.time);
				goto fail;
			}
			if (fi.row != row) {
				mismatch(mod, trace, line_no, "row", row, fi.row);
				goto fail;
			}
			if (fi.frame != frame) {
				mismatch(mod, trace, line_no, "frame", frame, fi.frame);
				goto fail;
			}
			if (i != chan) {
				mismatch(mod, trace, line_no, "channel", chan, i);
				goto fail;
			}
			if (xc->info_period != period) {
				mismatch(mod, trace, line_no, "period", period, xc->info_period);
				goto fail;
			}
			if (vi->note != note) {
				mismatch(mod, trace, line_no, "note", note, vi->note);
				goto fail;
			}
			if (vi->ins != ins) {
				mismatch(mod, trace, line_no, "instrument", ins, vi->ins);
				goto fail;
			}
			if (!ignore_rv && vi->vol != vol) {
				mismatch(mod, trace, line_no, "volume", vol, vi->vol);
				goto fail;
			}
			if (!ignore_rv && vi->pan != pan) {
				mismatch(mod, trace, line_no, "pan", pan, vi->pan);
				goto fail;
			}
			if ((vi->pos0 == vi->start && pos0 == vi->end) ||
			    (vi->pos0 == vi->end && pos0 == vi->start))
				pos0 = vi->pos0;
			if (abs(vi->pos0 - pos0) > 1) {
				mismatch(mod, trace, line_no, "position", pos0, vi->pos0);
				goto fail;
			}
			if (num >= 11 && cutoff < 254 && vi->filter.cutoff != cutoff) {
				mismatch(mod, trace, line_no, "cutoff", cutoff, vi->filter.cutoff);
				goto fail;
			}
			if (num >= 12 && vi->filter.resonance != resonance) {
				mismatch(mod, trace, line_no, "resonance", resonance,
					 vi->filter.resonance);
				goto fail;
			}
		}
	}

	if (fgets(line_buf, sizeof(line_buf), f) != NULL && line_buf[0] != '\0') {
		printf("MISMATCH module=%s trace=%s line=%d field=extra expected=eof actual=data\n",
		       mod, trace, line_no + 1);
		goto fail;
	}

	xmp_end_player(opaque);
	xmp_release_module(opaque);
	xmp_free_context(opaque);
	fclose(f);
	printf("OK module=%s trace=%s lines=%d\n", mod, trace, line_no);
	return 0;

fail:
	xmp_end_player(opaque);
	xmp_release_module(opaque);
	xmp_free_context(opaque);
	fclose(f);
	return 1;
}
