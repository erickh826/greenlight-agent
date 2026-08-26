#!/usr/bin/env python3
"""QA checks for the M1 weekend output.

Each check prints PASS or FAIL with the number behind it, so a failure says
what is wrong rather than only that something is. Exit code is non-zero if any
check fails.

Usage:
    ./scripts/run_etl.sh scripts/qa_m1_data.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SPINE = ROOT / "data" / "films_spine.parquet"
JOINED = ROOT / "data" / "films_with_plots.parquet"
ENRICHED = ROOT / "data" / "films_enriched.parquet"
CMU = ROOT / "data" / "MovieSummaries"

sys.path.insert(0, str(ROOT / "etl"))
from vocab import ARCHETYPES, MOTIFS, RELEASE_BUCKETS, release_bucket  # noqa: E402

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str) -> None:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}\n         {detail}")


def main() -> int:
    print("=== M1 資料 QA ===\n")

    for p in (SPINE, JOINED):
        if not p.exists():
            print(f"缺少 {p.relative_to(ROOT)} — 先跑 ETL", file=sys.stderr)
            return 1

    spine = pd.read_parquet(SPINE)
    df = pd.read_parquet(JOINED)

    print("A. 唯一性（最容易靜默出錯的地方）")
    dup_s = spine["film_id"].duplicated().sum()
    check(dup_s == 0, "spine 沒有重複 film_id",
          f"{len(spine)} 列 / {spine['film_id'].nunique()} 部唯一，重複 {dup_s}")
    dup_j = df["film_id"].duplicated().sum()
    check(dup_j == 0, "join 結果沒有重複 film_id",
          f"{len(df)} 列 / {df['film_id'].nunique()} 部唯一，重複 {dup_j}")

    print("\nB. 幣別與金額")
    check(bool((df["budget_usd"] > 0).all()), "所有預算為正數",
          f"最小 ${df['budget_usd'].min():,.0f}")
    check(bool((df["revenue_usd"] > 0).all()), "所有票房為正數",
          f"最小 ${df['revenue_usd'].min():,.0f}")
    roi = df["roi"]
    check(bool(roi.notna().all()), "每部片都算得出 ROI",
          f"中位數 {roi.median():.2f}，ROI>1 佔 {(roi > 1).mean():.0%}")

    # A seven-figure budget against a three-figure gross used to mean a
    # territory subtotal had been kept instead of the worldwide figure -- the
    # bug that gave Shrek an ROI of 0.00008. With that fixed, the survivors are
    # mostly real: The Room genuinely took $1,800 in its first run. Only The
    # Eye ($57 against a $12M budget) is wrong, and it is wrong in Wikidata
    # itself, which holds no other P2142 value for it. Reported, not failed --
    # the MVs aggregate with quantileState(0.5), which these cannot move.
    absurd = df[(df["budget_usd"] > 1e6) & (df["revenue_usd"] < 1e5)]
    check(len(absurd) <= 10, "極低票房樣本在可接受範圍（多為真實慘敗）",
          f"{len(absurd)} 部；最低 "
          + ", ".join(f"{r.title!r} ${r.revenue_usd:,.0f}"
                      for r in absurd.nsmallest(3, "revenue_usd").itertuples()))

    print("\nC. 欄位語意（用錯會靜默失敗）")
    diff = (df["title"] != df["enwiki_title"]).mean()
    check(diff > 0.3, "title 與 enwiki_title 確實不同",
          f"{diff:.1%} 不同 — join 用 title，pageviews 用 enwiki_title")
    has_suffix = df["enwiki_title"].str.contains(r"\(.*\)$", regex=True)
    example = df.loc[has_suffix, "enwiki_title"].iloc[0] if has_suffix.any() else "-"
    check(has_suffix.mean() > 0.3, "enwiki_title 帶消歧義後綴",
          f"{has_suffix.mean():.1%} 有括號後綴，例：{example!r}")

    print("\nD. 劇情文本")
    plot = df["plot"]
    check(bool(plot.notna().all()), "每部片都有劇情",
          f"中位數 {int(plot.str.len().median())} 字元")
    short = (plot.str.len() < 500).sum()
    check(short < len(df) * 0.05, "過短劇情比例可接受",
          f"{short} 部 < 500 字元（{short / len(df):.1%}）— 母題抽取前要設下限")
    markup = plot.str.contains(r"\{\{|\[\[|<ref", regex=True).sum()
    check(True, "wiki markup 殘留量（僅告知，M1 後段要清）",
          f"{markup} 部含 markup（{markup / len(df):.1%}）")

    print("\nE. 年份範圍與 CMU 涵蓋上限")
    check(bool(df["release_year"].max() <= 2014), "沒有 CMU 涵蓋不到的年份混入",
          f"年份 {df['release_year'].min()}–{df['release_year'].max()}")
    post12 = (df["release_year"] >= 2013).sum()
    check(True, "2013 年後的樣本數（CMU 幾乎沒有）",
          f"{post12} 部 — CMU 2013 只有 70 部、2014 只有 4 部")

    print("\nF. 1500 部門檻（MILESTONES 8/24 DoD）")
    n = df["film_id"].nunique()
    check(n >= 1500, "唯一影片數 ≥ 1500",
          f"實際 {n} 部 — 未達標時見 docs/M1_DATA_FINDINGS.md §2")

    print("\nG. 受控詞彙表單一來源")
    check(len(set(MOTIFS)) == len(MOTIFS) == 30, "母題 30 個且無重複",
          f"{len(MOTIFS)} 個，unique {len(set(MOTIFS))}")
    check(len(set(ARCHETYPES)) == len(ARCHETYPES) == 25, "角色原型 25 個且無重複",
          f"{len(ARCHETYPES)} 個，unique {len(set(ARCHETYPES))}")
    buckets = {release_bucket(y) for y in df["release_year"].unique()}
    check(buckets <= set(RELEASE_BUCKETS), "所有影片都落在已定義的 cohort 內",
          f"用到 {len(buckets)}/{len(RELEASE_BUCKETS)} 個 bucket: "
          f"{', '.join(sorted(buckets))}")

    if ENRICHED.exists():
        print("\nH. 關注度欄位與 cohort 正規化")
        en = pd.read_parquet(ENRICHED)
        have = en["interest_median_daily"].notna()
        check(have.mean() > 0.9, "多數影片取得 pageviews",
              f"{have.sum()}/{len(en)} ({have.mean():.0%})")

        e = en[have]
        # Measured, not assumed. The design brief said raw counts would track
        # how long ago a film came out, so the percentile had to remove that.
        # At full scale it does not: r = -0.009 raw. The +0.272 that motivated
        # the column came from a 25-film head() slice, which is not a random
        # sample. Both numbers are printed so the claim stays checkable.
        raw_r = np.corrcoef(e["years_to_measurement"],
                            e["interest_median_daily"])[0, 1]
        pct_r = np.corrcoef(e["years_to_measurement"],
                            e["interest_cohort_pct"])[0, 1]
        check(abs(pct_r) < 0.10, "關注度與量測延遲無關（正規化後）",
              f"原始 r={raw_r:+.3f}，cohort 百分位 r={pct_r:+.3f}"
              + ("（原始已無相關 — 見 docs/M1_DATA_FINDINGS.md §1）"
                 if abs(raw_r) < 0.10 else ""))

        # A real check, unlike the median-near-0.5 one it replaces: rank(pct=True)
        # forces that median to 0.50 by construction, so it could never fail.
        # This asks whether the percentile still *ranks* correctly -- a broken
        # groupby or a misaligned merge (--repair rewrites this column) would
        # scramble the mapping while leaving the distribution perfectly uniform.
        # Spearman by hand: rank both, then Pearson. pandas delegates
        # method="spearman" to scipy, and scipy is not worth a dependency here.
        worst = min(
            g["interest_cohort_pct"].rank().corr(
                g["interest_median_daily"].rank())
            for _, g in e.groupby("release_bucket") if len(g) >= 10
        )
        check(worst > 0.999, "百分位在每個 cohort 內與原始值同序",
              f"最差 cohort 的 Spearman = {worst:.4f}（應為 1.0000）")
    else:
        print(f"\nH. 關注度欄位 — 跳過（尚無 {ENRICHED.name}，"
              f"跑 etl/03_pageviews.py 後才有）")

    failed = [r for r in results if not r[0]]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} 通過 ===")
    if failed:
        print("未通過：")
        for _, name, detail in failed:
            print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
