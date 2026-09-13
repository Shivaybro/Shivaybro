#!/usr/bin/env python3
"""
Turn a pasted / text-extracted university timetable into a clean per-group table.

Russian university schedules are usually a grid of "пара" rows against weekday
columns, which falls apart into column-major mush when you copy-paste it out of a
PDF.  This tool takes that mush (or a neatly pasted table) and produces:

    date | weekday | pair | time | subject | teacher | room

It tries two strategies, in order:
  1. POSITIONAL -- if the paste still has a header row (Время / Дисциплина /
     Преподаватель / Аудитория) and tab or pipe separators, fields are mapped by
     column.  Most accurate.
  2. HEURISTIC -- otherwise it pulls time / room / teacher / subject out of each
     line by pattern, in that order of confidence.

Input can be:
  * a text file   (--in file.txt)
  * stdin         (paste into the terminal, then Ctrl-D)
  * a PDF         (--pdf file.pdf -> reads the embedded text layer via pymupdf)

Output:
  * <group>_schedule.csv
  * <group>_schedule.md   (grouped by date, ready to read in chat)

Usage:
    python3 tools/parse_schedule_text.py --pdf "Расписание занятий 3 курса.pdf" \
        --group 2422 --start 2026-09-14 --weeks 2 --out data/ \
        --parity числитель знаменатель
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import pathlib
import re
import sys

# --------------------------------------------------------------------- regexes
# "2422" / "24-22" / "24–22" -- lookarounds stop it from eating times like
# 8.30-10.00 or room numbers like 312.
GROUP_RE = re.compile(
    r"(?<![\d.,:\-–—])\b(\d{2})\s*[-–—]?\s*(\d{2})\b(?![\d.,:])"
)
# a line that is nothing but a group header: "Группа 2422" / "гр. 2422" / "2422"
HEADER_RE = re.compile(
    r"^\s*(?:группа|гр\.?|group)?\s*(\d{2})\s*[-–—]?\s*(\d{2})\s*$", re.IGNORECASE
)
# 8.30-10.00 / 08:30–10:00 / 830-1000 / 10 10 - 11 40
TIME_RE = re.compile(
    r"\b(\d{1,2})\s*[.:\s]\s*(\d{2})\s*[-–—]\s*(\d{1,2})\s*[.:\s]\s*(\d{2})\b"
)
PAIR_RE = re.compile(r"\b([1-9])\s*[-–]?\s*(?:я|ая)?\s*пара\b", re.IGNORECASE)
# "ауд. 312" / "ауд.312" / "каб. 12а" / "аудитория 5" -- the room token must
# start with an optional letter then a digit, so initials like "Д.К." can't match.
ROOM_RE = re.compile(
    r"(?<![\w.])(?:ауд(?:итори[яйи])?|каб(?:инет)?|комн(?:ата)?|класс)\.?\s*"
    r"№?\s*([А-Яа-яA-Za-z]?-?\d[\w\-/]*)",
    re.IGNORECASE,
)
# "312 ауд." / "501 аудитория"
ROOM_SUFFIX_RE = re.compile(
    r"([А-Яа-яA-Za-z]?-?\d[\w\-/]*)\s*"
    r"(?:ауд|аудитори\w*|каб\w*|комн\w*)\.?",
    re.IGNORECASE,
)
# числитель / знаменатель, "1 неделя"/"2 неделя", нечёт/чёт
PARITY_RE = re.compile(
    r"\b(числитель\w*|знаменатель\w*|неч[её]т\w*|ч[её]т\w*|"
    r"[12]\s*(?:-?я|ая)?\s*недел\w*)",
    re.IGNORECASE,
)
TEACHER_RE = re.compile(r"([А-ЯЁ][а-яё\-]+)\s+([А-ЯЁ])\.\s?([А-ЯЁ])?\.")
DEGREE_RE = re.compile(
    r"\b(доц|проф|ст\.?\s*преп|преподаватель|преп|асс|ассист|к\.?м\.?н|д\.?м\.?н)\.?",
    re.IGNORECASE,
)

DAY_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY_RU_TO_EN = {
    "пн": "Monday", "вт": "Tuesday", "ср": "Wednesday", "чт": "Thursday",
    "пт": "Friday", "сб": "Saturday", "вс": "Sunday",
    "понедельник": "Monday", "вторник": "Tuesday", "среда": "Wednesday",
    "четверг": "Thursday", "пятница": "Friday", "суббота": "Saturday",
    "воскресенье": "Sunday",
}

HEADER_KEYS = (
    ("time",    ("врем", "time", "час")),
    ("subject", ("дисциплин", "предмет", "subject", "наимен", "занят")),
    ("teacher", ("препод", "фио", "teacher", "педагог")),
    ("room",    ("ауд", "room", "каб", "мест", "класс")),
    ("group",   ("групп", "group")),
    ("day",     ("день", "day")),
    ("parity",  ("недел", "числит", "знамен", "чётн", "четн")),
    ("pair",    ("пара", "pair")),
)


def norm_time(m: re.Match) -> str:
    h1, m1, h2, m2 = m.groups()
    return f"{int(h1):02d}:{m1}–{int(h2):02d}:{m2}"


def norm_day(token: str) -> str:
    t = re.sub(r"[^а-яa-z]", "", token.lower())[:14]
    if t in DAY_RU_TO_EN:
        return DAY_RU_TO_EN[t]
    for key in ("пн", "вт", "ср", "чт", "пт", "сб", "вс"):
        if t.startswith(key):
            return DAY_RU_TO_EN[key]
    for full, en in DAY_RU_TO_EN.items():
        if len(full) > 4 and t.startswith(full[:4]):
            return en
    for en in DAY_EN:
        if t.startswith(en.lower()[:3]):
            return en
    return ""


def group_variants(group: str) -> set[str]:
    g = re.sub(r"\D", "", group)
    out = {g}
    if len(g) == 4:
        out |= {f"{g[:2]}-{g[2:]}", f"{g[:2]}–{g[2:]}", f"{g[:2]} {g[2:]}"}
    return out


def line_has_group(line: str, variants: set[str]) -> bool:
    return any(v in line for v in variants)


def split_fields(line: str) -> list[str]:
    """Split on tabs, pipes, or 3+ spaces -- whatever the paste preserved."""
    if "\t" in line:
        parts = re.split(r"\t+", line)
    elif "|" in line:
        parts = re.split(r"\s*\|\s*", line)
    else:
        parts = re.split(r"\s{3,}", line)
    return [p.strip(" ,;") for p in parts if p and p.strip(" ,;")]


# ------------------------------------------------------------------- selection
def collect_group_lines(text: str, group: str) -> list[str]:
    """
    Return the lines belonging to `group`, handling both shapes:
      * row-per-group   -> group id and its data on the same line
      * block-per-group -> a "Группа 2422" header followed by its rows
    """
    variants = group_variants(group)
    lines = [ln.rstrip() for ln in text.splitlines()]
    hits = [i for i, ln in enumerate(lines) if line_has_group(ln, variants)]
    if not hits:
        return []

    picked: set[int] = set(hits)

    # keep a *real* header row that precedes the first hit -- validated by the
    # same layout parser used later, so a data line containing "ауд." is not
    # mistaken for a header.
    layout = find_header_layout(lines)
    if layout and layout[0] < min(hits):
        picked.add(layout[0])

    # block shape: header line -> take following rows up to the next group header
    for i in hits:
        if not HEADER_RE.match(lines[i]):
            continue
        for j in range(i + 1, min(len(lines), i + 60)):
            hm = HEADER_RE.match(lines[j])
            if hm and not line_has_group(lines[j], variants):
                break
            picked.add(j)

    return [lines[i] for i in sorted(picked) if lines[i].strip()]


# ------------------------------------------------------------------ extraction
def find_header_layout(lines: list[str]) -> tuple[int, dict[str, int]] | None:
    """
    Locate a header row and map canonical field -> column index.

    Each column is claimed by at most one field, tested in priority order, so
    "День недели" becomes `day` rather than also matching the parity keywords.
    """
    order = ("day", "parity", "time", "subject", "teacher", "room", "group", "pair")
    keys_by_field = dict(HEADER_KEYS)
    for i, ln in enumerate(lines):
        fields = split_fields(ln)
        if len(fields) < 3:
            continue
        layout: dict[str, int] = {}
        claimed: set[int] = set()
        for canon in order:
            for k, f in enumerate(fields):
                if k in claimed:
                    continue
                if any(key in f.lower() for key in keys_by_field[canon]):
                    layout[canon] = k
                    claimed.add(k)
                    break
        if ("subject" in layout or "time" in layout) and len(claimed) >= 3:
            return i, layout
    return None


def clean_subject(s: str) -> str:
    s = DEGREE_RE.sub("", s)
    s = GROUP_RE.sub("", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" ,;|-–—\t.")


def parse_positional(rows: list[str], layout: dict[str, int]) -> list[dict]:
    out: list[dict] = []
    for ln in rows:
        if not ln.strip() or HEADER_RE.match(ln):
            continue
        fields = split_fields(ln)
        if len(fields) < 3:
            continue

        def get(key: str) -> str:
            idx = layout.get(key)
            return fields[idx] if idx is not None and idx < len(fields) else ""

        tm = TIME_RE.search(get("time") or ln)
        ptok = get("parity").lower()
        out.append({
            "day": norm_day(get("day")),
            "parity": ("числитель" if ptok.startswith(("чис", "неч", "1"))
                       else "знаменатель" if ptok else ""),
            "pair": re.sub(r"\D", "", get("pair"))[:1],
            "time": norm_time(tm) if tm else "",
            "subject": clean_subject(get("subject")),
            "teacher": get("teacher"),
            "room": re.sub(r"^[а-яА-Я.\s]+", "", get("room")),
        })
    return out


def parse_heuristic(rows: list[str]) -> list[dict]:
    """
    Pull day / parity / time / room / teacher / subject out of each line.

    Every field is removed from the working line as soon as it is identified, so
    the subject is simply whatever text is left over -- no positional guessing.
    Spans are always recomputed on the *current* line, never carried over.
    """
    out: list[dict] = []
    for raw in rows:
        if not raw.strip() or HEADER_RE.match(raw):
            continue

        line = GROUP_RE.sub(" ", raw)          # drop the group number first

        parity = ""
        parm = PARITY_RE.search(line)
        if parm:
            tok = parm.group(1).lower()
            if tok.startswith(("чис", "неч")) or tok.startswith("1"):
                parity = "числитель"
            else:
                parity = "знаменатель"
            line = line[: parm.start()] + " " + line[parm.end():]

        day, day_span = "", None
        for dm in re.finditer(r"[А-Яа-яЁё]{2,12}", line[:40]):
            if norm_day(dm.group(0)):
                day = norm_day(dm.group(0))
                day_span = dm.span()
                break
        if day_span:
            line = line[: day_span[0]] + " " + line[day_span[1]:]

        tmatch = TIME_RE.search(line)
        time_str = norm_time(tmatch) if tmatch else ""

        pmatch = PAIR_RE.search(line)
        pair = pmatch.group(1) if pmatch else ""

        rest = line
        for m in (tmatch, pmatch):
            if m:
                rest = rest[: m.start()] + " " + rest[m.end():]

        room = ""
        rmatch = ROOM_RE.search(rest) or ROOM_SUFFIX_RE.search(rest)
        if rmatch:
            room = rmatch.group(1).strip()
            rest = rest[: rmatch.start()] + " " + rest[rmatch.end():]

        teacher = ""
        tms = TEACHER_RE.findall(rest)
        if tms:
            teacher = ", ".join(
                f"{s} {i}.{o}." if o else f"{s} {i}." for s, i, o in tms
            )
            rest = TEACHER_RE.sub(" ", rest)

        out.append({
            "day": day,
            "pair": pair,
            "time": time_str,
            "subject": clean_subject(rest),
            "teacher": teacher,
            "room": room,
            "parity": parity,
        })
    return out


def assign_pairs(records: list[dict]) -> None:
    """Fill missing pair numbers from the distinct times, earliest first."""
    times = sorted({r["time"] for r in records if r["time"]})
    order = {t: str(i + 1) for i, t in enumerate(times)}
    for r in records:
        if not r["pair"] and r["time"]:
            r["pair"] = order[r["time"]]


# ----------------------------------------------------------------------- output
def write_outputs(records, start, weeks, out_dir, group, parity_labels):
    """
    Emit CSV + Markdown.

    A record with a known weekday lands on that weekday in the week(s) it belongs
    to: parity "числитель" -> week 1, "знаменатель" -> week 2, no parity info ->
    both weeks (and the Markdown says so, so nothing is silently invented).
    A record with no weekday is listed as-is, never fanned out across the week.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"group_{group}_schedule.csv"
    md_path = out_dir / f"group_{group}_schedule.md"

    week_grids = []
    for i in range(weeks):
        wk = start + dt.timedelta(days=i * 7)
        week_grids.append({
            DAY_EN[(wk + dt.timedelta(days=k)).weekday()]: wk + dt.timedelta(days=k)
            for k in range(7)
        })

    def weeks_for(r):
        p = r.get("parity") or ""
        if p == "числитель":
            return [0]
        if p == "знаменатель":
            return [min(1, weeks - 1)]
        return list(range(weeks))

    rows: list[dict] = []
    for r in records:
        day = r.get("day") or ""
        payload = {k: r.get(k, "") for k in ("pair", "time", "subject",
                                             "teacher", "room")}
        if not day:
            # no weekday -> no date, and repeating it for every week adds nothing
            rows.append({"date": "", "weekday": "", "week": "", **payload})
            continue
        for wi in weeks_for(r):
            rows.append({
                "date": week_grids[wi][day].isoformat(),
                "weekday": day,
                "week": f"W{wi + 1}",
                **payload,
            })

    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["date", "weekday", "week", "pair",
                                          "time", "subject", "teacher", "room"])
        w.writeheader()
        w.writerows(rows)

    def fmt(r):
        return (f"- `{r.get('time') or '?'}` (пара {r.get('pair') or '?'}) | "
                f"{r.get('subject') or '?'} | {r.get('teacher') or '?'} | "
                f"{r.get('room') or '?'}")

    by_date: dict[str, list[dict]] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)

    lines = [f"# Group {group} — schedule", "",
             f"**Window:** {start.isoformat()} → "
             f"{(start + dt.timedelta(days=weeks * 7 - 1)).isoformat()}", ""]
    for wi in range(weeks):
        wk = start + dt.timedelta(days=wi * 7)
        label = parity_labels[wi] if wi < len(parity_labels) else ""
        lines.append(f"## Week {wi + 1}: {wk.strftime('%d.%m.%Y')} – "
                     f"{(wk + dt.timedelta(days=6)).strftime('%d.%m.%Y')}"
                     f"{f'  ({label})' if label else ''}")
        lines.append("")
        for day, d in sorted(week_grids[wi].items(), key=lambda kv: kv[1]):
            entries = by_date.get(d.isoformat(), [])
            lines.append(f"### {d.strftime('%d.%m.%Y')} — {day}")
            lines.append("")
            lines.extend(fmt(r) for r in entries)
            if not entries:
                lines.append("_нет занятий / no entries_")
            lines.append("")

    undated = [r for r in rows if not r["date"]]
    if undated:
        lines.append("## Без дня недели / weekday not stated")
        lines.append("")
        lines.extend(fmt(r) for r in undated)
        lines.append("")

    if any(not r.get("parity") for r in records):
        lines.append("> Записи без пометки числитель/знаменатель показаны в обеих "
                     "неделях — уточните по исходному расписанию.")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, md_path


def read_input(args) -> str:
    if args.pdf:
        import pymupdf

        doc = pymupdf.open(args.pdf)
        text = "\n".join(p.get_text("text") for p in doc)
        doc.close()
        return text
    if args.infile:
        return pathlib.Path(args.infile).read_text(encoding="utf-8")
    return sys.stdin.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--pdf")
    src.add_argument("--in", dest="infile")
    ap.add_argument("--group", required=True)
    ap.add_argument("--start", default="2026-09-14")
    ap.add_argument("--weeks", type=int, default=2)
    ap.add_argument("--parity", nargs=2, default=["", ""], metavar=("W1", "W2"),
                    help="labels for week 1 / week 2, e.g. числитель знаменатель")
    ap.add_argument("--out", default="data")
    ap.add_argument("--dump-lines", action="store_true")
    args = ap.parse_args()

    text = read_input(args)
    start = dt.date.fromisoformat(args.start)

    rows = collect_group_lines(text, args.group)
    if args.dump_lines:
        print("\n".join(rows))
    if not rows:
        print(f"!! group {args.group} not found in the input", file=sys.stderr)
        return 2

    layout = find_header_layout(rows)
    if layout:
        idx, mapping = layout
        records = parse_positional(rows[idx + 1:], mapping)
        mode = f"positional (header at line {idx}: {mapping})"
        if len(records) < 2:           # header was a red herring -> fall back
            records = parse_heuristic(rows)
            mode = "heuristic (positional found too few rows)"
    else:
        records = parse_heuristic(rows)
        mode = "heuristic"

    records = [r for r in records if r["subject"] or r["time"]]
    assign_pairs(records)

    csv_path, md_path = write_outputs(
        records, start, args.weeks, pathlib.Path(args.out), args.group, tuple(args.parity)
    )
    print(f"group {args.group}: {len(records)} entries via {mode}")
    print(f"  -> {csv_path}")
    print(f"  -> {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
