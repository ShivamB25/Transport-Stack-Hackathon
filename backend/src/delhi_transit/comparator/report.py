"""Comparator CSV, manifest and Markdown report writers."""
from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path
from typing import Any

CSV_COLUMNS: tuple[str, ...] = (
    "od_id",
    "group",
    "label",
    "slot_id",
    "slot_label",
    "departure_time_ist",
    "departure_time_utc",
    "row_status",
    "our_status",
    "our_mode",
    "our_selected_rung",
    "our_total_duration_s",
    "our_window_p10_s",
    "our_window_p90_s",
    "our_board_wait_s",
    "our_duration_excluding_boarding_wait_s",
    "our_ride_walk_duration_s",
    "our_routes",
    "our_transfers",
    "our_timing_sources",
    "our_option_index",
    "our_option_supported",
    "google_status",
    "google_http_status",
    "google_duration_s",
    "google_distance_m",
    "google_routes",
    "google_transfers",
    "google_raw_route_count",
    "google_bus_only_count",
    "google_excluded_non_bus_count",
    "google_excluded_vehicle_types",
    "diff_s",
    "diff_pct",
    "abs_diff_s",
    "diff_excl_board_s",
    "diff_excluding_boarding_wait_s",
    "google_in_our_p10_p90",
    "google_from_cache",
    "google_cache_key",
    "note",
)


def _fmt_min(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    return f"{seconds / 60.0:.1f} min"


def _fmt_dur(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    return f"{seconds} s ({seconds / 60.0:.1f} m)"


def _fmt_diff(diff_s: int | None, diff_pct: float | None) -> str:
    if diff_s is None:
        return "—"
    sign = "+" if diff_s > 0 else ""
    pct_str = f" ({sign}{diff_pct:.1f}%)" if diff_pct is not None else ""
    return f"{sign}{diff_s} s{pct_str}"


def _group_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    compared = [r for r in rows if r["row_status"] == "compared"]
    if not compared:
        return {
            "planned": len(rows),
            "compared": 0,
            "missing": len(rows),
            "mean_our_s": None,
            "mean_google_s": None,
            "mean_diff_s": None,
            "median_diff_s": None,
            "mae_s": None,
            "within_p10_p90_pct": None,
            "excl_wait_compared": 0,
            "mean_our_excl_wait_s": None,
            "mean_diff_excl_wait_s": None,
            "median_diff_excl_wait_s": None,
            "mae_excl_wait_s": None,
        }
    ours = [int(r["our_total_duration_s"]) for r in compared]
    goog = [int(r["google_duration_s"]) for r in compared]
    diffs = [int(r["diff_s"]) for r in compared]
    abs_diffs = [int(r["abs_diff_s"]) for r in compared]
    in_win = [r for r in compared if r["google_in_our_p10_p90"] is True]
    excl = [r for r in compared if r.get("diff_excluding_boarding_wait_s") is not None]
    ours_excl = [int(r["our_duration_excluding_boarding_wait_s"]) for r in excl]
    diffs_excl = [int(r["diff_excluding_boarding_wait_s"]) for r in excl]
    return {
        "planned": len(rows),
        "compared": len(compared),
        "missing": len(rows) - len(compared),
        "mean_our_s": round(statistics.mean(ours), 1),
        "mean_google_s": round(statistics.mean(goog), 1),
        "mean_diff_s": round(statistics.mean(diffs), 1),
        "median_diff_s": round(statistics.median(diffs), 1),
        "mae_s": round(statistics.mean(abs_diffs), 1),
        "within_p10_p90_pct": round(100.0 * len(in_win) / len(compared), 1),
        "excl_wait_compared": len(excl),
        "mean_our_excl_wait_s": round(statistics.mean(ours_excl), 1) if excl else None,
        "mean_diff_excl_wait_s": round(statistics.mean(diffs_excl), 1) if excl else None,
        "median_diff_excl_wait_s": round(statistics.median(diffs_excl), 1) if excl else None,
        "mae_excl_wait_s": round(statistics.mean(abs(d) for d in diffs_excl), 1) if excl else None,
    }


def _signed_s(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    sign = "+" if seconds > 0 else ""
    return f"{sign}{seconds:.0f} s ({sign}{seconds / 60.0:.1f} m)"


def render_markdown_report(
    rows: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    csv_rel_path: str = "reports/google-comparator.csv",
    manifest_rel_path: str = "reports/google-manifest.json",
) -> str:
    """Render reports/google-comparator.md with full honesty caveats and all rows."""
    call_acc = manifest["call_accounting"]
    corpus = manifest["corpus_summary"]
    overall = _group_stats(rows)
    missing_rows = [r for r in rows if r["row_status"] != "compared"]

    lines: list[str] = [
        "# Google Routes API (Bus-Only) Comparator Report",
        "",
        "## Methodological Notice: Comparator Only, Not Ground Truth",
        "",
        (
            "- **Google Maps Routes API is a live/future comparator, NOT ground truth.** "
            "It reflects Google's own bus schedule and traffic model, not observed GPS vehicle arrivals on Delhi roads. "
            "Disagreement between our model and Google does not imply either system is right or wrong; real ground-truth "
            "accuracy is measured strictly against observed October 2026 GPS stop passages in `reports/eval-full.md`."
        ),
        (
            "- **October 2026 backtesting against Google is impossible.** Google Routes API `TRANSIT` does not support "
            "past departure times (verified during our feasibility probe: querying `2026-10-20T08:30:00+05:30` returns "
            "HTTP 200 with `routes: []`). All comparator queries therefore use **upcoming matching weekday/time slots** "
            f"(`{manifest['target_date_ist']}`, {manifest['target_weekday']}, matching held-out Tuesday `2026-10-27` and "
            "the brief's Tuesday `08:30` example)."
        ),
        (
            "- **Which quantity is compared:** Our door-to-door `total_duration_s` includes walk-to-stop, the "
            "estimated first boarding wait, model-timed bus ride(s), joint-scenario transfer risk/wait, and egress walk. "
            "Google's `routes.duration` runs from leaving the origin, timed to its bus, to arriving at the destination, "
            "so it contains no first boarding wait. The like-for-like basis is therefore "
            "`our_duration_excluding_boarding_wait_s` = `our_total_duration_s` − `our_board_wait_s` (the median "
            "first boarding wait of our top option), compared as `diff_excluding_boarding_wait_s`. The door-to-door "
            "difference `diff_s` is kept as a separate row. Subtracting a median wait from a joint median total is an "
            "approximation, and our `[p10, p90]` window remains door-to-door. OTP and Google may also select "
            "different bus lines or transfer points. The CSV additionally keeps `diff_excl_board_s`, an older "
            "diagnostic built from the sum of marginal walk/ride/transfer leg medians. "
            f"See `{csv_rel_path}`."
        ),
        (
            "- **Only supported journeys are compared.** `GET /journeys` abstains unless some option has a ride "
            "with route-level observations (`observed_coverage > 0`), and the compared option must be one of "
            "those. Rows whose reported option has none are kept as `our_unsupported_option` "
            "(`our_option_supported = False`). Inputs frozen before per-ride coverage was recorded are classified "
            "from their timing sources and routes: only 717A rides (the one corridor with stop passages) and "
            "`model_live` rides carry observed coverage."
        ),
        "",
        "## Collection Contract & Call Accounting",
        "",
        f"- **Target weekday / date (IST):** `{manifest['target_date_ist']}` ({manifest['target_weekday']}; upcoming, zero October/past dates)",
        f"- **Time slots per OD pair:** {', '.join(manifest['time_slots'])}",
        (
            f"- **Corpus size:** {corpus['od_pair_count']} OD pairs × {corpus['time_slots_per_pair']} time slots = **{corpus['planned_slots']} planned slots** "
            f"({corpus['compared_rows']} compared, {corpus['missing_or_error_rows']} missing/error, 0 silently omitted)"
        ),
        (
            f"- **Google request settings:** `travelMode={manifest['travel_mode']}`, "
            f"`transitPreferences.allowedTravelModes={manifest['allowed_travel_modes']}`, "
            f"`languageCode=\"{manifest['language_code']}\"`, `computeAlternativeRoutes=true`, field-masked (`X-Goog-FieldMask`)"
        ),
        (
            f"- **Strict bus-only post-filtering:** Every compared Google itinerary is verified to contain >=1 `TRANSIT` step "
            f"with vehicle type `BUS` only (and `WALK` access/egress/transfers). "
            f"**{corpus['google_non_bus_itineraries_filtered']}** non-bus Google itinerary(ies) "
            "(e.g., Delhi Metro / `SUBWAY` or `RAIL`) were filtered out across the corpus."
        ),
        (
            f"- **Mission-wide Google call budget:** **{call_acc['total_mission_calls']} / {call_acc['hard_cap']}** "
            f"(prior probe calls: {call_acc['prior_mission_calls']}, comparator network calls cached: {call_acc['api_calls_made']}, "
            f"remaining budget: {call_acc['remaining_budget']}; cache hits on this run: {call_acc['cache_hits_this_run']})"
        ),
        (
            f"- **Artifacts:** Report `reports/google-comparator.md`, CSV table `{csv_rel_path}`, "
            f"request manifest `{manifest_rel_path}` (mirrored at `{manifest['scratch_manifest_path']}`), "
            f"raw field-masked cache directory `{manifest['cache_dir']}`."
        ),
        "",
        "## Headline Comparison",
        "",
        "| Basis | Compared | Mean Ours | Mean Google | Mean Diff (Ours − Google) | Median Diff | Mean Abs Diff |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    if overall["compared"]:
        lines.append(
            f"| **Like-for-like: ours excluding first boarding wait** | {overall['excl_wait_compared']} | "
            f"{_fmt_min(overall['mean_our_excl_wait_s'])} | {_fmt_min(overall['mean_google_s'])} | "
            f"{_signed_s(overall['mean_diff_excl_wait_s'])} | {_signed_s(overall['median_diff_excl_wait_s'])} | "
            f"{_fmt_min(overall['mae_excl_wait_s'])} |"
        )
        lines.append(
            f"| Door-to-door: ours including first boarding wait | {overall['compared']} | "
            f"{_fmt_min(overall['mean_our_s'])} | {_fmt_min(overall['mean_google_s'])} | "
            f"{_signed_s(overall['mean_diff_s'])} | {_signed_s(overall['median_diff_s'])} | "
            f"{_fmt_min(overall['mae_s'])} |"
        )
    lines += [
        "",
        "## Aggregate Summary",
        "",
        (
            "**Mean Diff (Ours − Google)** is the door-to-door difference, **Our Total Duration − Google Bus-Only "
            "Duration** (positive = our model estimates longer; negative = shorter). **Mean Diff excl. Wait** is the "
            "like-for-like difference with our first boarding wait removed."
        ),
        "",
        "| Slice | Planned | Compared | Missing/Err | Mean Ours | Mean Google | Mean Diff (Ours − Google) | Mean Diff excl. Wait | Median Diff | Mean Abs Diff | Google in Our [p10, p90] |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    def _add_summary_row(name: str, stats: dict[str, Any]) -> None:
        if stats["compared"] == 0:
            lines.append(
                f"| {name} | {stats['planned']} | 0 | {stats['missing']} | — | — | — | — | — | — | — |"
            )
            return
        sign_m = "+" if stats["mean_diff_s"] > 0 else ""
        sign_med = "+" if stats["median_diff_s"] > 0 else ""
        lines.append(
            f"| {name} | {stats['planned']} | {stats['compared']} | {stats['missing']} | "
            f"{_fmt_min(stats['mean_our_s'])} | {_fmt_min(stats['mean_google_s'])} | "
            f"{sign_m}{stats['mean_diff_s']:.0f} s ({sign_m}{stats['mean_diff_s'] / 60.0:.1f} m) | "
            f"{_signed_s(stats['mean_diff_excl_wait_s'])} | "
            f"{sign_med}{stats['median_diff_s']:.0f} s | "
            f"{stats['mae_s']:.0f} s ({stats['mae_s'] / 60.0:.1f} m) | "
            f"{stats['within_p10_p90_pct']:.1f}% |"
        )

    _add_summary_row("**All OD × Time Slots**", overall)
    for grp_id, grp_label in (
        ("brief_example", "Brief Example (Kashmere Gate -> Nehru Place)"),
        ("corridor_717a_up", "717A Corridor UP OD Group"),
        ("corridor_717a_down", "717A Corridor DOWN OD Group"),
        ("city_wide", "City-Wide Corridors (19 OD pairs)"),
    ):
        grp_rows = [r for r in rows if r["group"] == grp_id]
        if grp_rows:
            _add_summary_row(grp_label, _group_stats(grp_rows))

    lines.extend(
        [
            "",
            "### Breakdown by Time of Day",
            "",
            "| Time Slot (IST) | Planned | Compared | Missing/Err | Mean Ours | Mean Google | Mean Diff (Ours − Google) | Mean Diff excl. Wait | Median Diff | Mean Abs Diff | Google in Our [p10, p90] |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for slot_id, slot_label in (("morning_peak", "08:30 IST (Morning Peak)"), ("midday", "14:00 IST (Midday)"), ("evening_peak", "18:30 IST (Evening Peak)")):
        slot_rows = [r for r in rows if r["slot_id"] == slot_id]
        if slot_rows:
            _add_summary_row(slot_label, _group_stats(slot_rows))

    lines.extend(["", "## Missing / Error / Non-Bus-Only Rows", ""])
    if not missing_rows:
        lines.append(
            f"All **{len(rows)} / {len(rows)}** planned OD × time slots returned both a model-timed itinerary from our API and at least "
            "one strictly bus-only itinerary from Google Routes API (0 missing or error rows)."
        )
    else:
        lines.append(
            f"**{len(missing_rows)} / {len(rows)}** planned slots could not be numerically compared and are explicitly "
            "retained below (never silently omitted):"
        )
        lines.extend(
            [
                "",
                "| OD ID | Corridor | Slot (IST) | Row Status | Our Status | Google Status | Reason / Note |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for r in missing_rows:
            lines.append(
                f"| `{r['od_id']}` | {r['label']} | {r['slot_label']} | `{r['row_status']}` | "
                f"`{r['our_status']}` | `{r['google_status']}` | {r['note'] or '—'} |"
            )

    lines.extend(
        [
            "",
            "## Full Per-OD / Per-Time Comparison Table (All Planned Slots)",
            "",
            "| OD | Group | Origin -> Destination | Slot | Status | Our Duration | Our [p10, p90] | Our Duration excl. Wait | Google Bus Duration | Diff (Ours − Google) | Diff excl. Wait | Our Route(s) | Google Bus Route(s) | Our Timing Source | Note |",
            "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---|---|",
        ]
    )
    for r in rows:
        win_str = (
            f"[{r['our_window_p10_s'] / 60.0:.1f}, {r['our_window_p90_s'] / 60.0:.1f}] m"
            if r["our_window_p10_s"] is not None and r["our_window_p90_s"] is not None
            else "—"
        )
        lines.append(
            f"| `{r['od_id']}` | `{r['group']}` | {r['label']} | {r['slot_label']} | `{r['row_status']}` | "
            f"{_fmt_dur(r['our_total_duration_s'])} | {win_str} | "
            f"{_fmt_dur(r.get('our_duration_excluding_boarding_wait_s'))} | "
            f"{_fmt_dur(r['google_duration_s'])} | {_fmt_diff(r['diff_s'], r['diff_pct'])} | "
            f"{_fmt_diff(r.get('diff_excluding_boarding_wait_s'), None)} | "
            f"{r['our_routes'] or '—'} | {r['google_routes'] or '—'} | "
            f"`{r['our_timing_sources'] or '—'}` | {r['note'] or '—'} |"
        )

    lines.extend(
        [
            "",
            "## Reproducing From Frozen Inputs (0 Planner or Google Calls)",
            "",
            "The checked-in inputs freeze both our planner estimates and the cached Google observations. Rendering does not query either service.",
            "",
            "```bash",
            "cd backend && uv run python -m delhi_transit.comparator --render-only",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def write_comparison_outputs(
    rows: list[dict[str, Any]],
    manifest: dict[str, Any],
    report_dir: Path,
    scratch_manifest_path: Path | None,
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    with (report_dir / "google-comparator.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in CSV_COLUMNS})

    manifest_json = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    (report_dir / "google-manifest.json").write_text(manifest_json, encoding="utf-8")
    if scratch_manifest_path is not None:
        scratch_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        scratch_manifest_path.write_text(manifest_json, encoding="utf-8")
    md_text = render_markdown_report(rows, manifest)
    (report_dir / "google-comparator.md").write_text(md_text, encoding="utf-8")
