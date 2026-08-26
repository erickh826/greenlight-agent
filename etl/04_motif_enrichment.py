#!/usr/bin/env python3
"""Turn plot summaries into abstract structural tags with Gemini Flash.

This is the step that makes the whole dataset queryable. Everything after it
aggregates over `motif_tags` / `character_archetypes`, so a failure here is
silent and total: bad tags still produce valid-looking GROUP BY results.

Three things guard against that.

*Closed vocabulary.* The response schema is `app.contracts.FilmMotifs`, whose
enums come from `etl/vocab.py`. The model picks from 30 motifs and 25
archetypes; it cannot invent "redemptive arc" beside "redemption" and split a
bucket in two. This matters more than the model choice does.

*Mode collapse check.* A closed vocabulary stops synonym sprawl but not the
opposite failure -- the model labelling half the corpus "redemption" because it
is the first option it reads. `--histogram` prints the distribution; if the top
three motifs take more than half the assignments, the problem is the vocabulary
or the prompt, not the sample size.

*Licence boundary.* CMU plot text is CC BY-SA 3.0. It is read from the local
extract, held in memory, and never written anywhere -- not to the checkpoint,
not to the parquet, not to ClickHouse. Only the abstract tags leave this step.
See SYSTEM_SPEC §4.6.

Usage:
    ./scripts/run_etl.sh etl/04_motif_enrichment.py --limit 20   # smoke test
    ./scripts/run_etl.sh etl/04_motif_enrichment.py              # full run
    ./scripts/run_etl.sh etl/04_motif_enrichment.py --sample 20  # manual QA

Outputs:
    data/motifs.jsonl          append-only checkpoint, one film per line
    data/films_motifs.parquet  film_id + tags, no plot text
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "etl"))

from app.contracts import FilmMotifs  # noqa: E402
from app.env import load_env  # noqa: E402
from vocab import ARCHETYPES, MOTIFS, as_sql_list  # noqa: E402

# A summary this short is a stub sentence, not a plot. The model would invent
# structure to fill the schema, and invented structure is worse than a gap
# because nothing downstream can tell the two apart.
MIN_PLOT_CHARS = 500

# CMU summaries run to 20k characters. The structural signal is in the whole
# arc, but the tail is usually scene-by-scene detail that costs tokens without
# changing which motifs apply.
MAX_PLOT_CHARS = 8000

CONCURRENCY = 10
MAX_RETRIES = 5

# Thinking is off, and the reason is reproducibility rather than cost.
#
# Measured over 12 films, two runs at this exact config agree at Jaccard 0.84 on
# motif_tags and 12/12 on both act_structure and conflict_scale. Thinking on
# versus off agrees at only 0.55 and 6/12. So thinking is not extra care on the
# same answer -- it is a different labelling policy, and the single-valued
# fields flip half the time between them.
#
# That makes a mid-run switch the real hazard: half the corpus labelled under
# one policy and half under the other would put a systematic ~45% disagreement
# seam through every GROUP BY, invisibly. Whichever policy is chosen, the whole
# corpus must be labelled under it, so changing this constant means discarding
# data/motifs.jsonl and relabelling from scratch.
#
# Off was chosen because it is 4-7x faster and cheaper at equal stability, and
# on the sample it was not worse: it read Hoop Dreams as ensemble_parallel
# (the film does follow two boys in parallel) where thinking said hero_journey.
THINKING_BUDGET = 0

SYSTEM_INSTRUCTION = f"""\
You label films with abstract narrative structure for a research database.

Choose only from the controlled vocabularies given in the schema. Never invent a
term, and never pick a near-synonym of one you already chose -- each tag must
earn its place by describing a different aspect of the story.

Judge the dramatic situation, not the subject matter or the genre. A heist film
and a courtroom drama can both be `man_versus_system`; a war film is not
automatically `survival`.

motif_tags:            3-6, the dramatic situations the plot actually turns on
character_archetypes:  2-4, roles in the story's machinery
act_structure:         exactly one, the shape of the telling
conflict_scale:        personal / communal / existential

tone_axis is a continuous value, and the endpoints are reserved. Place the film
against these anchors rather than reaching for -1 or +1 because a story is sad:

  -1.0  unrelieved despair, no survivor and no consolation
  -0.6  grim, but someone is left standing
  -0.2  serious and often painful, with warmth in it
   0.0  genuinely balanced, or ironic about both
  +0.4  affectionate, with real loss along the way
  +0.8  warm and reassuring throughout
  +1.0  uncomplicated joy

Most films are between -0.7 and +0.7. A value at an endpoint is a claim that
nothing in the film pulls the other way; make sure that is true before using it.

Legal motifs:     {as_sql_list(MOTIFS)}
Legal archetypes: {as_sql_list(ARCHETYPES)}
"""

# --- wiki markup ------------------------------------------------------------
# 29.6% of CMU summaries carry residual markup. Left in, it reaches the model as
# noise and occasionally as instructions ({{cleanup}}, {{spoiler}}).
RE_REF = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", re.S | re.I)
RE_TAG = re.compile(r"<[^>]+>")
RE_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
RE_LINK_PIPED = re.compile(r"\[\[[^\]|]*\|([^\]]*)\]\]")
RE_LINK_PLAIN = re.compile(r"\[\[([^\]]*)\]\]")
RE_QUOTES = re.compile(r"'{2,5}")
RE_SPACE = re.compile(r"[ \t]{2,}")


def clean_markup(text: str) -> str:
    text = RE_REF.sub(" ", text)
    text = RE_TAG.sub(" ", text)
    # Templates nest; peel one layer at a time until nothing changes.
    for _ in range(5):
        stripped = RE_TEMPLATE.sub(" ", text)
        if stripped == text:
            break
        text = stripped
    text = RE_LINK_PIPED.sub(r"\1", text)
    text = RE_LINK_PLAIN.sub(r"\1", text)
    text = RE_QUOTES.sub("", text)
    text = RE_SPACE.sub(" ", text)
    return text.strip()


# --- model call -------------------------------------------------------------

def extract(client, model: str, title: str, year: int, plot: str) -> FilmMotifs:
    """One film, one structured call. Raises after MAX_RETRIES."""
    from google.genai import types

    prompt = (f"Film: {title} ({year})\n\nPlot summary:\n{plot[:MAX_PLOT_CHARS]}")

    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    response_mime_type="application/json",
                    response_schema=FilmMotifs,
                    # Labelling is a classification task; sampling variance here
                    # shows up downstream as noise in every aggregate.
                    temperature=0.2,
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=THINKING_BUDGET),
                ),
            )
            return FilmMotifs.model_validate_json(response.text)
        except Exception as exc:  # quota, transient 5xx, or a schema violation
            last = exc
            if attempt == MAX_RETRIES - 1:
                break
            # Jittered, so ten workers hitting a quota wall do not retry in
            # lockstep and hit it again together.
            time.sleep((2 ** attempt) + random.random())
    raise RuntimeError(f"{title!r}: {last}") from last


# --- checkpoint -------------------------------------------------------------

def load_done(path: Path) -> dict[str, dict]:
    """Resume from a previous run. 1,238 paid calls are worth not repeating."""
    if not path.exists():
        return {}
    done: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            done[row["film_id"]] = row
    return done


# --- reporting --------------------------------------------------------------

def histogram(rows: list[dict], field: str, vocabulary: tuple[str, ...],
              label: str) -> None:
    counts = Counter(tag for r in rows for tag in r[field])
    total = sum(counts.values())
    if not total:
        return

    print(f"\n{label}（{len(counts)}/{len(vocabulary)} 個被用到，"
          f"共 {total:,} 次標註）")
    width = max(len(v) for v in vocabulary)
    for tag, n in counts.most_common():
        share = n / total
        print(f"  {tag:<{width}}  {n:>4}  {share:>5.1%}  "
              + "█" * round(share * 200))

    unused = [v for v in vocabulary if v not in counts]
    if unused:
        print(f"  未被使用（{len(unused)}）：{', '.join(unused)}")

    top3 = sum(n for _, n in counts.most_common(3)) / total
    if len(rows) < 50:
        verdict = f"樣本僅 {len(rows)} 部，不足以判斷塌縮"
    elif top3 > 0.5:
        verdict = "眾數塌縮 — 問題在詞彙表或 prompt，不在資料量"
    else:
        verdict = "分布可接受"
    print(f"  前三名合計 {top3:.1%} → {verdict}")


def tone_distribution(rows: list[dict]) -> None:
    """Watch for the model snapping to the ends of a continuous scale.

    sql/003 aggregates this with avgState(), so boundary saturation would not
    show up as an error -- it would show up as every cohort averaging the same
    number and the column quietly carrying no signal.
    """
    values = [r["tone_axis"] for r in rows]
    at_edge = sum(1 for v in values if abs(v) >= 0.99) / len(values)
    distinct = len(set(values))

    print(f"\ntone_axis 分布（{len(values)} 部，{distinct} 個相異值）")
    edges = [-1.0, -0.6, -0.2, 0.2, 0.6, 1.01]
    labels = ["≤-0.6", "-0.6~-0.2", "-0.2~0.2", "0.2~0.6", "≥0.6"]
    for label, lo, hi in zip(labels, edges, edges[1:]):
        n = sum(1 for v in values if lo <= v < hi)
        print(f"  {label:>10}  {n:>4}  {n / len(values):>5.1%}  "
              + "█" * round(n / len(values) * 100))
    verdict = ("端點飽和 — 模型在把連續值當成三選一，avgState(tone_axis) 會失去解析度"
               if at_edge > 0.25 else "端點比例正常")
    print(f"  落在 ±1.0 端點: {at_edge:.1%} → {verdict}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--films", type=Path,
                    default=ROOT / "data" / "films_with_plots.parquet")
    ap.add_argument("--checkpoint", type=Path,
                    default=ROOT / "data" / "motifs.jsonl")
    ap.add_argument("--out", type=Path,
                    default=ROOT / "data" / "films_motifs.parquet")
    ap.add_argument("--limit", type=int, help="only label the first N films")
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY)
    ap.add_argument("--model", help="overrides MODEL_FAST from .env")
    ap.add_argument("--sample", type=int, metavar="N",
                    help="skip labelling; print N labelled films for manual "
                         "review (the DoD's 20-film check)")
    ap.add_argument("--histogram", action="store_true",
                    help="skip labelling; print the distribution only")
    args = ap.parse_args()

    if not args.films.exists():
        sys.exit(f"ERROR: {args.films} not found. Run 02_cmu_join.py first.")

    load_env()
    done = load_done(args.checkpoint)

    if args.sample or args.histogram:
        return report_only(args, done)

    from google import genai

    # The SDK warns about automatic function calling on every structured call.
    # Nothing here uses tools, and 1,238 copies of it would bury the progress.
    logging.getLogger("google_genai.models").setLevel(logging.ERROR)

    model = args.model or os.environ.get("MODEL_FAST") or "gemini-2.5-flash"
    client = genai.Client()

    films = pd.read_parquet(args.films)
    if args.limit:
        films = films.head(args.limit).copy()

    films["plot_clean"] = films["plot"].map(clean_markup)
    too_short = films["plot_clean"].str.len() < MIN_PLOT_CHARS
    skipped = films[too_short]
    films = films[~too_short]

    todo = films[~films["film_id"].isin(done)]

    print(f"motif enrichment: model={model} concurrency={args.concurrency}")
    print(f"  劇情過短跳過（< {MIN_PLOT_CHARS} 字元）: {len(skipped)}")
    print(f"  已有標註（checkpoint）:              {len(done)}")
    print(f"  本次要跑:                            {len(todo)}\n")

    if todo.empty:
        print("沒有新的影片要標註。")
    else:
        run(client, model, todo, args)

    return finish(args, load_done(args.checkpoint), skipped)


def run(client, model: str, todo: pd.DataFrame, args) -> None:
    """Label every film in `todo`, appending to the checkpoint as results land."""
    lock = threading.Lock()
    handle = args.checkpoint.open("a", encoding="utf-8")
    failures: list[str] = []
    completed = 0

    def label(film) -> dict:
        motifs = extract(client, model, film.title, int(film.release_year),
                         film.plot_clean)
        # Only the abstract tags. The plot text stops here.
        return {"film_id": film.film_id,
                "motif_tags": [m.value for m in motifs.motif_tags],
                "act_structure": motifs.act_structure.value,
                "character_archetypes": [a.value
                                         for a in motifs.character_archetypes],
                "tone_axis": motifs.tone_axis,
                "conflict_scale": motifs.conflict_scale.value}

    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(label, f): f.title
                       for f in todo.itertuples(index=False)}
            for future in as_completed(futures):
                completed += 1
                try:
                    row = future.result()
                except Exception as exc:
                    failures.append(str(exc))
                else:
                    with lock:
                        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                        handle.flush()
                if completed % 50 == 0 or completed == len(futures):
                    print(f"  {completed}/{len(futures)}  "
                          f"failed={len(failures)}")
    finally:
        handle.close()

    if failures:
        print(f"\n失敗 {len(failures)} 部（checkpoint 已保留成功的，重跑即續）:")
        for f in failures[:5]:
            print(f"  - {f}")


def finish(args, done: dict[str, dict], skipped: pd.DataFrame) -> int:
    if not done:
        print("沒有任何標註結果。", file=sys.stderr)
        return 1

    rows = list(done.values())
    out = pd.DataFrame(rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(args.out, index=False)

    films = pd.read_parquet(args.films)
    coverage = len(out) / len(films)

    print(f"\n=== motifs ===")
    print(f"已標註:        {len(out):,}/{len(films):,} ({coverage:.1%})")
    print(f"劇情過短跳過:  {len(skipped)}")
    print(f"平均母題數:    {out['motif_tags'].map(len).mean():.1f}")
    print(f"tone_axis:     中位數 {out['tone_axis'].median():+.2f}")

    histogram(rows, "motif_tags", MOTIFS, "母題分布")
    histogram(rows, "character_archetypes", ARCHETYPES, "角色原型分布")
    tone_distribution(rows)

    print(f"\nDoD 覆蓋率 > 95%: "
          f"{'PASS' if coverage > 0.95 else 'FAIL'} ({coverage:.1%})")
    print("接著人工抽查 20 部："
          "  ./scripts/run_etl.sh etl/04_motif_enrichment.py --sample 20")
    return 0


def report_only(args, done: dict[str, dict]) -> int:
    """Inspect an existing checkpoint without spending a single call."""
    if not done:
        sys.exit(f"ERROR: {args.checkpoint} is empty. Run the labelling first.")
    rows = list(done.values())

    if args.histogram:
        histogram(rows, "motif_tags", MOTIFS, "母題分布")
        histogram(rows, "character_archetypes", ARCHETYPES, "角色原型分布")
        return 0

    # The manual check needs the title and the plot beside the tags, so the
    # reviewer can judge them. Plot text is printed to the terminal only.
    films = pd.read_parquet(args.films).set_index("film_id")
    picked = random.Random(0).sample(rows, min(args.sample, len(rows)))

    print(f"=== 人工抽查 {len(picked)} 部 ===")
    print("看的是：母題是否真的是這部片的戲劇處境，而不是題材的同義複述\n")
    for row in picked:
        film = films.loc[row["film_id"]]
        plot = clean_markup(film["plot"])
        print(f"{film['title']} ({film['release_year']})")
        print(f"  母題    {', '.join(row['motif_tags'])}")
        print(f"  原型    {', '.join(row['character_archetypes'])}")
        print(f"  結構    {row['act_structure']}   "
              f"衝突 {row['conflict_scale']}   "
              f"調性 {row['tone_axis']:+.2f}")
        print(f"  劇情    {plot[:400]}…\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
