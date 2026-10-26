"""Self-check for the playlist -> cut mapping. Run: python test_app.py (no deps)."""
import app
from app import ClipError, Seg, parse_playlist, plan_cut

PL = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:10
#EXT-X-PROGRAM-DATE-TIME:2026-10-21T07:00:00.000+0000
#EXTINF:2.000000,
seg_00000010.ts
#EXTINF:2.000000,
seg_00000011.ts
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2026-10-21T07:00:10.000+0000
#EXTINF:2.000000,
seg_00000012.ts
#EXTINF:1.500000,
seg_00000013.ts
"""


def raises(fn, *a):
    try:
        fn(*a)
    except (ClipError, ValueError):
        return True
    return False


def main():
    segs = parse_playlist(PL)
    t0 = segs[0].start
    assert [s.name for s in segs] == [f'seg_000000{n}.ts' for n in (10, 11, 12, 13)]
    assert segs[1].start == t0 + 2, 'PDT carried forward when a segment has none'
    assert segs[2].start == t0 + 10, 'PDT after discontinuity is taken as written (gap 4..10)'
    assert segs[3].end == t0 + 13.5
    assert app.parse_pdt('2026-10-21T14:00:00.000+0700') == t0, 'basic offset +0700'
    assert app.parse_pdt('2026-10-21T07:00:00Z') == t0

    # simple range inside one segment
    chosen, off, dur, gap = plan_cut(segs, t0 + 0.5, t0 + 1.5)
    assert [s.name for s in chosen] == ['seg_00000010.ts'] and abs(off - 0.5) < 1e-9 and abs(dur - 1.0) < 1e-9 and gap == 0

    # across the reconnect gap (4..10 has no picture): concat timeline collapses the gap
    chosen, off, dur, gap = plan_cut(segs, t0 + 3, t0 + 11)
    assert [s.name for s in chosen] == ['seg_00000011.ts', 'seg_00000012.ts']
    assert abs(off - 1) < 1e-9 and abs(dur - 2) < 1e-9, (off, dur)   # 1s of seg11 + 1s of seg12
    assert abs(gap - 6) < 1e-9, gap

    # In inside the gap clamps to the next segment, Out inside the gap clamps to the previous end
    chosen, off, dur, gap = plan_cut(segs, t0 + 5, t0 + 12)
    assert [s.name for s in chosen] == ['seg_00000012.ts'] and off == 0 and abs(dur - 2) < 1e-9
    chosen, off, dur, gap = plan_cut(segs, t0 + 1, t0 + 6)
    assert [s.name for s in chosen] == ['seg_00000010.ts', 'seg_00000011.ts'] and abs(off - 1) < 1e-9 and abs(dur - 3) < 1e-9

    # loud failures
    assert raises(plan_cut, segs, t0 + 2, t0 + 1), 'Out before In'
    assert raises(plan_cut, segs, t0 - 5, t0 + 1), 'In already rolled out of the buffer'
    assert raises(plan_cut, segs, t0 + 1, t0 + 20), 'Out not recorded yet'
    assert raises(plan_cut, segs, t0 + 5, t0 + 9), 'range entirely inside the gap'
    assert raises(plan_cut, [], t0, t0 + 1)
    assert raises(parse_playlist, '#EXTM3U\n#EXTINF:2,\nseg_1.ts\n'), 'no PDT at all'
    assert raises(parse_playlist, '#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:2026-10-21T07:00:00Z\n#EXTINF:2,\n../etc/passwd\n'), 'odd segment name'

    # PDT normalisation used when serving the playlist to browsers
    assert app.PDT_OFFSET_RE.sub(r'\1:\2', '#EXT-X-PROGRAM-DATE-TIME:2026-10-21T07:00:00.000+0000') \
        == '#EXT-X-PROGRAM-DATE-TIME:2026-10-21T07:00:00.000+00:00'
    assert app.content_disposition('สัมภาษณ์ "ช่วงแรก"').startswith('attachment; filename="')
    assert Seg(1, 2, 'x').end == 3
    print('test_app: all checks passed')


if __name__ == '__main__':
    main()
