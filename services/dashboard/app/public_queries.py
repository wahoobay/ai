"""The SQL behind the detection and sighting answers, shared by the dashboard's own routes
(main.py) and the public API (routes_public.py), so both read the same rows the same way.

Each function takes an open connection and returns plain rows; shaping them into a response
is the caller's job. A reviewer's verdict is read for what it says, never for who gave it: no
query here selects a reviewer, note, user or session column.

Volunteers' verdicts in the public answers: a verdict counts once it is visible (none while a
spot-check of its box is pending: exports.hide_pending_spot_checks), and the latest visible
one wins. A box called not a fish is no detection, and a track whose latest verdict says not a
fish is no sighting (`hide_not_a_fish`). A rename does not move a sighting between species:
species_sightings_mat counts a track under the model's vote over its frames; only a single
detection (recent_detections) shows the species a volunteer named."""

from __future__ import annotations

from datetime import datetime

from psycopg.rows import dict_row

from .exports import hide_pending_spot_checks

# A detection's saved frame, for a query over detection_events aliased `de`.
# detection_events.image_path is legacy / always NULL — the worker writes
# interesting frames to saved_frames (keyed by frame_id + source_name). A
# correlated subquery, not a JOIN: a LIMIT caps how many lookups fire, the
# saved_frames index on (frame_id, source_name) makes each one O(log N), and
# there is no duplicate-event-row problem when saved_frames has multiple rows
# for the same key (which happens — same frame can be saved under several
# reasons). The timestamp is part of the match: frame_id restarts at 1 with
# every worker restart, so frame_id + camera alone can name a different frame.
# When one frame was saved under several reasons, the lowest id wins.
SAVED_FRAME_PATH = """COALESCE(
                     de.image_path,
                     (SELECT image_path FROM saved_frames sf
                       WHERE sf.frame_id = de.frame_id
                         AND sf.source_name = de.source_name
                         AND sf.ts = de.ts
                       ORDER BY sf.id LIMIT 1)
                   )"""


# The latest visible verdict on the detection `de`.
LATEST_VERDICT = f"""
              SELECT dc.event_id, dc.corrected_name, dc.corrected_species_id, dc.not_a_fish
                FROM detection_corrections dc
               WHERE dc.event_id = de.id
                 AND {hide_pending_spot_checks("dc")}
               ORDER BY dc.created_at DESC, dc.id DESC
               LIMIT 1"""

# Tracks whose latest visible verdict, on any of their boxes, says not a fish. Verdicts are
# few, so the set is built once per query and anti-joined (detection_events.track_id is the
# key species_sightings_mat groups by).
NOT_A_FISH_TRACKS = f"""
    SELECT latest.track_id
      FROM (SELECT DISTINCT ON (e.track_id) e.track_id, dc.not_a_fish
              FROM detection_corrections dc
              JOIN detection_events e ON e.id = dc.event_id
             WHERE e.track_id IS NOT NULL
               AND {hide_pending_spot_checks("dc")}
             ORDER BY e.track_id, dc.created_at DESC, dc.id DESC) latest
     WHERE latest.not_a_fish"""


def track_is_a_fish(track_id: str) -> str:
    """SQL condition: the track `track_id` (an SQL expression; NULL passes) is not one whose
    latest visible verdict says not a fish."""
    return f"NOT EXISTS (SELECT 1 FROM ({NOT_A_FISH_TRACKS}) naf WHERE naf.track_id = {track_id})"


async def _rows(conn, sql: str, params) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


async def detection_with_frame(conn, event_id: int, *, verdict: bool = False) -> dict | None:
    """The detection's box, the model's guess and the path of its own saved frame (None when
    that frame was never saved); None when there is no such detection. With `verdict`, also
    `not_a_fish`: whether its latest visible verdict says so."""
    not_a_fish = (
        f", COALESCE((SELECT v.not_a_fish FROM ({LATEST_VERDICT}) v), false) AS not_a_fish"
        if verdict
        else ""
    )
    rows = await _rows(
        conn,
        f"""
        SELECT de.bbox, de.best_name, de.best_accuracy,
               {SAVED_FRAME_PATH} AS image_path{not_a_fish}
          FROM detection_events de
         WHERE de.id = %s
        """,
        (event_id,),
    )
    return rows[0] if rows else None


async def sighting_counts(
    conn,
    hours: int,
    min_frames: int,
    *,
    source_name: str | None = None,
    limit: int | None = 50,
    hide_not_a_fish: bool = False,
) -> list[dict]:
    """Sightings per species: tracks of at least `min_frames` frames whose last frame is in
    the last `hours`, most first (species_id, name, n, mean_acc, last_seen, total_frames,
    mean_frames_per_track). One camera's alone with `source_name`; without the tracks a
    verdict calls not a fish with `hide_not_a_fish`."""
    where = ["last_seen >= NOW() - (%s::int || ' hours')::interval", "frame_count >= %s"]
    params: list = [hours, min_frames]
    if source_name is not None:
        where.append("source_name = %s")
        params.append(source_name)
    if hide_not_a_fish:
        where.append(track_is_a_fish("species_sightings_mat.track_id"))
    sql = f"""
        SELECT species_id, name,
               count(*)::int                AS n,
               avg(mean_accuracy)::real     AS mean_acc,
               max(last_seen)               AS last_seen,
               sum(frame_count)::int        AS total_frames,
               avg(frame_count)::real       AS mean_frames_per_track
          FROM species_sightings_mat
         WHERE {" AND ".join(where)}
         GROUP BY 1, 2
         ORDER BY n DESC
    """
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    return await _rows(conn, sql, params)


async def species_sightings(
    conn,
    *,
    min_frames: int,
    hours: int | None = None,
    species_id: str | None = None,
    limit: int | None = None,
    hide_not_a_fish: bool = False,
) -> list[dict]:
    """Per species: sightings (tracks of at least `min_frames` frames), frames, accuracy,
    cameras and first/last seen, most seen first. Every sighting, or those whose last frame
    is in the last `hours`; every species, or `species_id` alone; without the tracks a
    verdict calls not a fish with `hide_not_a_fish`."""
    where: list[str] = []
    params: list = []
    if hours is not None:
        where.append("last_seen >= NOW() - (%s::int || ' hours')::interval")
        params.append(hours)
    where.append("frame_count >= %s")
    params.append(min_frames)
    if species_id is not None:
        where.append("species_id = %s")
        params.append(species_id)
    if hide_not_a_fish:
        where.append(track_is_a_fish("species_sightings_mat.track_id"))
    sql = f"""
        SELECT species_id,
               MIN(name)                          AS name,
               COUNT(*)::int                      AS sightings,
               SUM(frame_count)::bigint           AS total_frames,
               AVG(mean_accuracy)::real           AS mean_accuracy,
               MAX(peak_accuracy)::real           AS peak_accuracy,
               array_agg(DISTINCT source_name)    AS sources,
               MIN(first_seen)                    AS first_seen,
               MAX(last_seen)                     AS last_seen
          FROM species_sightings_mat
         WHERE {" AND ".join(where)}
         GROUP BY species_id
         ORDER BY sightings DESC
    """
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    return await _rows(conn, sql, params)


async def sightings_by_hour_of_day(
    conn,
    species_id: str,
    tz: str,
    *,
    hours: int | None = None,
    min_frames: int | None = None,
    hide_not_a_fish: bool = False,
) -> list[int]:
    """The species' sightings by the local hour of day (in `tz`) of their first frame: 24
    counts, midnight first. Every sighting, or those whose last frame is in the last `hours`;
    every track, or those of at least `min_frames` frames; without the tracks a verdict
    calls not a fish with `hide_not_a_fish`."""
    where = ["species_id = %s"]
    params: list = [tz, species_id]
    if hours is not None:
        where.append("last_seen >= NOW() - (%s::int || ' hours')::interval")
        params.append(hours)
    if min_frames is not None:
        where.append("frame_count >= %s")
        params.append(min_frames)
    if hide_not_a_fish:
        where.append(track_is_a_fish("species_sightings_mat.track_id"))
    rows = await _rows(
        conn,
        f"""
        SELECT extract(hour FROM (first_seen AT TIME ZONE %s))::int AS hod,
               COUNT(*)::int AS sightings
          FROM species_sightings_mat
         WHERE {" AND ".join(where)}
         GROUP BY 1
         ORDER BY 1
        """,
        params,
    )
    by_hour = [0] * 24
    for r in rows:
        if r["hod"] is not None:
            by_hour[int(r["hod"])] = r["sightings"]
    return by_hour


async def recent_detections(
    conn,
    *,
    hours: int,
    limit: int,
    min_confidence: float,
    source_name: str | None = None,
    species_id: str | None = None,
    species_names: list[str] | None = None,
    saved_only: bool = False,
) -> list[dict]:
    """The newest classified detections of the last `hours` the model scored at least
    `min_confidence`, each with the latest verdict on it (`reviewed`, `corrected_name`,
    `corrected_species_id`) and the path of its saved frame. A verdict on a box whose
    spot-check is pending does not count yet (exports.hide_pending_spot_checks); a box
    called not a fish is left out, and so is every box of a track whose latest verdict says
    not a fish (as the sightings leave it out). With `species_id`, the boxes the model gave
    that species and no verdict moved to another (a verdict names its species as one of
    `species_names`). With `saved_only`, only boxes whose own frame was saved."""
    where = [
        "de.ts >= NOW() - (%s::int || ' hours')::interval",
        "de.best_name IS NOT NULL",
        "de.best_accuracy >= %s",
        "NOT COALESCE(v.not_a_fish, false)",
        track_is_a_fish("de.track_id"),
    ]
    params: list = [hours, min_confidence]
    if source_name is not None:
        where.append("de.source_name = %s")
        params.append(source_name)
    if species_id is not None:
        where.append("de.best_species_id = %s")
        where.append("(v.event_id IS NULL OR v.corrected_name = ANY(%s))")
        params += [species_id, list(species_names or [species_id])]
    if saved_only:
        where.append(f"{SAVED_FRAME_PATH} IS NOT NULL")
    params.append(limit)
    sql = f"""
        SELECT de.id, de.ts, de.source_name,
               de.best_name, de.best_species_id, de.best_accuracy,
               v.event_id IS NOT NULL AS reviewed,
               v.corrected_name, v.corrected_species_id,
               {SAVED_FRAME_PATH} AS image_path
          FROM detection_events de
          LEFT JOIN LATERAL ({LATEST_VERDICT}
          ) v ON true
         WHERE {" AND ".join(where)}
         ORDER BY de.ts DESC, de.id DESC
         LIMIT %s
    """
    return await _rows(conn, sql, params)


async def sightings_by_bucket(
    conn, since: datetime, tz: str, min_frames: int, *, week: bool
) -> list[dict]:
    """Sightings (tracks of at least `min_frames` frames) per species per local day in `tz`,
    or per ISO week as its Monday, by their first frame, from `since` on: species_id, name,
    bucket (a date), n. Read per track, not from the hourly rollup, which keeps 60 days;
    without the tracks a verdict calls not a fish."""
    local = "first_seen AT TIME ZONE %s"
    bucket = f"date_trunc('week', {local})::date" if week else f"({local})::date"
    return await _rows(
        conn,
        f"""
        SELECT species_id, MIN(name) AS name, {bucket} AS bucket, COUNT(*)::int AS n
          FROM species_sightings_mat
         WHERE first_seen >= %s AND frame_count >= %s
           AND {track_is_a_fish("species_sightings_mat.track_id")}
         GROUP BY 1, 3
        """,
        (tz, since, min_frames),
    )


async def sightings_by_month(
    conn, species_id: str, since: datetime, tz: str, min_frames: int
) -> dict[str, int]:
    """The species' sightings (tracks of at least `min_frames` frames) per local calendar
    month in `tz` ("YYYY-MM") by their first frame, from `since` on; months without one are
    absent. Read per track, not from the hourly rollup, which keeps 60 days; without the
    tracks a verdict calls not a fish."""
    rows = await _rows(
        conn,
        f"""
        SELECT to_char(first_seen AT TIME ZONE %s, 'YYYY-MM') AS month, COUNT(*)::int AS n
          FROM species_sightings_mat
         WHERE species_id = %s AND first_seen >= %s AND frame_count >= %s
           AND {track_is_a_fish("species_sightings_mat.track_id")}
         GROUP BY 1
        """,
        (tz, species_id, since, min_frames),
    )
    return {r["month"]: r["n"] for r in rows}
