"""Summarize recorded stream observations per channel.

    python3 -m streaming.report [path-to-jsonl]
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from collections import Counter, defaultdict

import config


def load(path: str | None = None) -> list[dict]:
    path = path or config.STREAM_OBSERVE_LOG
    records = []
    for p in (path + ".1", path):
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    return records


def summarize(records: list[dict]) -> list[dict]:
    by_channel: dict[tuple, list[dict]] = defaultdict(list)
    for r in records:
        by_channel[(r["source"], r["item_id"], r["stream_id"])].append(r)

    out = []
    for (source, item_id, stream_id), rs in by_channel.items():
        hours = sum(r["wall_s"] for r in rs) / 3600
        rewinds = [-r["boundary_s"] for r in rs if r["boundary"] == "rewind"]
        first_idr = [r["first_idr_s"] for r in rs if r["first_idr_s"] is not None]
        gops = [r["gop_avg_s"] for r in rs if r["gop_avg_s"]]
        ratios = [r["realtime_ratio"] for r in rs if r["realtime_ratio"] is not None]
        reconnects = sum(1 for r in rs if r["reason"] == "reconnect")
        out.append({
            "source": source,
            "channel": rs[-1]["channel"],
            "item_id": item_id,
            "stream_id": stream_id,
            "sessions": len(rs),
            "hours": round(hours, 2),
            "reconnects_per_hour": round(reconnects / hours, 1) if hours else None,
            "boundaries": dict(Counter(r["boundary"] for r in rs)),
            "rewind_median_s": round(statistics.median(rewinds), 1) if rewinds else None,
            "first_idr_avg_s": round(statistics.mean(first_idr), 2) if first_idr else None,
            "gop_avg_s": round(statistics.mean(gops), 2) if gops else None,
            "realtime_ratio_min": min(ratios) if ratios else None,
            "jumps_fwd": sum(r["jumps_fwd"] for r in rs),
            "jumps_back": sum(r["jumps_back"] for r in rs),
            "cc_errors_per_hour": round(sum(r["cc_errors"] for r in rs) / hours, 1) if hours else None,
            "tei": sum(r["tei"] for r in rs),
            "pmt_changes": sum(r["pmt_changes"] for r in rs),
            "codecs": sorted({f"{r['video']}/{'+'.join(r['audio'])}" for r in rs}),
        })
    out.sort(key=lambda c: c["hours"], reverse=True)
    return out


def main() -> None:
    rows = summarize(load(sys.argv[1] if len(sys.argv) > 1 else None))
    if not rows:
        print("No observations recorded yet.")
        return
    for c in rows:
        rewind = f"{c['rewind_median_s']}s" if c["rewind_median_s"] is not None else "n/a"
        print(f"[{c['source']}] {c['channel']} (item {c['item_id']}, stream {c['stream_id']})")
        print(f"    {c['sessions']} sessions over {c['hours']}h, {c['reconnects_per_hour']} reconnects/h, "
              f"boundaries {c['boundaries']}, rewind median {rewind}")
        print(f"    first IDR avg {c['first_idr_avg_s']}s, GOP avg {c['gop_avg_s']}s, "
              f"min realtime ratio {c['realtime_ratio_min']}, codecs {', '.join(c['codecs'])}")
        print(f"    jumps fwd {c['jumps_fwd']}/back {c['jumps_back']}, cc_err {c['cc_errors_per_hour']}/h, "
              f"tei {c['tei']}, pmt changes {c['pmt_changes']}")


if __name__ == "__main__":
    main()
