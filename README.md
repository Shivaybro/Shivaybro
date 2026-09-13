# Shivaybro

Tools for reading **university timetable PDFs** (Russian 3rd-year schedule style) and
turning them into a clean, dated per-group table.

> **Status:** tooling is complete and tested. The source PDFs have not reached this
> environment yet, so no schedule data is committed here. See
> [Getting the PDFs in](#getting-the-pdfs-in).

---

## What's here

| File | Purpose |
|---|---|
| `tools/prepare_schedule_pdf.py` | Read a PDF: pull its text layer, and render + de-blur every page to PNG |
| `tools/parse_schedule_text.py` | Turn pasted/extracted schedule text into CSV + Markdown for one group |
| `tools/test_parse_schedule_text.py` | 15 unit tests for the parser |

### Setup

`apt` is not reachable from this sandbox, so dependencies come from PyPI:

```bash
pip install --break-system-packages pymupdf opencv-python-headless numpy
```

---

## `prepare_schedule_pdf.py` — read a blurry / scanned PDF

```bash
python3 tools/prepare_schedule_pdf.py "Расписание занятий 3 курса.pdf" --dpi 400
```

For every page it:

1. **Extracts the text layer** if the PDF has one → written to `pNN.txt`. Real text,
   so no OCR error at all. Pages without a text layer are reported as `SCAN/JPG`.
2. **Renders and enhances** the page as an image:

| Step | Why |
|---|---|
| 400 DPI render | keeps small print (room numbers, initials) legible |
| Shading flatten | removes uneven scanner lighting / grey corners |
| Deskew | straightens a tilted scan so rows stay horizontal |
| Lanczos 2x upscale | makes thin glyph strokes resolvable |
| CLAHE | recovers faded laser print |
| Unsharp mask | the actual de-blur pass |
| Bilateral filter | kills scan/JPEG speckle without eating thin strokes |
| `--binarize` (optional) | very low-contrast scans |
| Left/right split | wide pages get cut in halves so columns stay readable zoomed |

Outputs into `<pdf_stem>_pages/`: `pNN.txt`, `pNN_raw.png`, `pNN_enhanced.png`,
`pNN_L.png`, `pNN_R.png`. Flags: `--out`, `--dpi`, `--scale`, `--binarize`, `--no-split`.

---

## `parse_schedule_text.py` — one group → dated table

```bash
python3 tools/parse_schedule_text.py --pdf "Расписание занятий 3 курса.pdf" \
    --group 2422 --start 2026-09-14 --weeks 2 --out data/ \
    --parity числитель знаменатель
```

Input: `--pdf FILE`, `--in FILE`, or stdin (paste + `Ctrl-D`).

It handles the three shapes a paste usually arrives in — tab/pipe separated tables
(**positional** parsing via the header row), a `Группа 2422` block followed by its
rows, and unseparated mush (**heuristic** parsing: day, parity, time, room, teacher
are peeled off one by one, and the subject is whatever text is left).

### Output

`group_2422_schedule.csv` — one row per class:

```
date,weekday,week,pair,time,subject,teacher,room
2026-09-14,Monday,W1,1,08:30–10:00,Анатомия человека,Иванов И.И.,312
2026-09-21,Monday,W2,1,08:30–10:00,Биохимия,Петрова А.В.,208
```

`group_2422_schedule.md` — the same thing grouped by real date, ready to read:

```markdown
## Week 1: 14.09.2026 – 20.09.2026  (числитель)

### 14.09.2026 — Monday

- `08:30–10:00` (пара 1) | Анатомия человека | Иванов И.И. | 312
```

### Rules it follows, so nothing is invented

* **Parity routing** — an entry marked `числитель` lands in week 1, `знаменатель` in
  week 2. Entries with no parity marker appear in both weeks, and the Markdown says
  so in a footnote rather than pretending to know.
* **No fan-out** — an entry with no weekday is never repeated across the week; it is
  listed once under "weekday not stated".
* **Group isolation** — `2422` is matched as `2422` / `24-22` / `24–22`, with
  lookarounds that stop the pattern from eating times (`8.30-10.00`) or room numbers
  (`312`), and block parsing stops at the next `Группа NNNN` header.

### Tests

```bash
python3 tools/test_parse_schedule_text.py -v
```

15 tests: group-number false positives, row-per-group vs block-per-group selection,
heuristic and positional parsing, time normalisation, parity detection, pair numbering,
week routing, and the no-fan-out rule.

---

## Getting the PDFs in

File attachments have not been landing in this environment. Two routes that do work:

1. **Paste the text** — open the PDF, `Ctrl+A`, `Ctrl+C`, paste into chat (or
   `--in file.txt` here). Timetable PDFs are usually text-based, so this works.
2. **GitHub upload** — upload the PDFs to any branch of this repo; `git`/`gh` access
   is working (Google Drive / Dropbox are not reachable from here).

Once the schedule is readable, the parsed `group_2422_schedule.md` / `.csv` for the
14–27 September window lands in `data/`.
