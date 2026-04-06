"""
nara_query.py — 나라장터 DB 조회 CLI
nara_bid.db (nara_bid_v3.py가 생성한 SQLite DB)를 읽기 전용으로 열어
필터링 + 사정율 계산 + 통계 출력.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd


# ──────────────────────────────────────────────
# 한글 폰트 탐색 (matplotlib용)
# ──────────────────────────────────────────────
def _find_korean_font() -> str | None:
    try:
        from matplotlib import font_manager
        candidates = ["NanumGothic", "맑은 고딕", "AppleGothic", "Malgun Gothic",
                      "NanumBarunGothic", "Dotum", "Gulim"]
        available = {f.name for f in font_manager.fontManager.ttflist}
        for name in candidates:
            if name in available:
                return name
        # 폰트 이름에 한/영 모두 탐색
        for f in font_manager.fontManager.ttflist:
            low = f.name.lower()
            if "nanum" in low or "malgun" in low or "gothic" in low or "dotum" in low:
                return f.name
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────
# DB 로드
# ──────────────────────────────────────────────
SQL = """
SELECT
    l.bidNtceNo,
    MAX(l.bidNtceNm)           AS bidNtceNm,
    MAX(l.dminsttNm)           AS dminsttNm,
    MAX(l.ntceInsttNm)         AS ntceInsttNm,
    MAX(l.opengDt)             AS opengDt,
    MAX(l.prtcptCnum)          AS prtcptCnum,
    MAX(l.progrsDivCdNm)       AS progrsDivCdNm,
    p.bssamt,
    p.plnprc
FROM bid_list l
LEFT JOIN bid_price p ON l.bidNtceNo = p.bidNtceNo
GROUP BY l.bidNtceNo
"""


def load_db(db_path: str) -> pd.DataFrame:
    uri = Path(db_path).resolve().as_posix()
    con = sqlite3.connect(f"file:{uri}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(SQL, con)
    finally:
        con.close()

    # 숫자 변환
    for col in ("bssamt", "plnprc", "prtcptCnum"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 사정율 계산 (기초금액 0 또는 NaN → NaN)
    df["사정율(%)"] = df.apply(
        lambda r: (r["plnprc"] / r["bssamt"] * 100)
        if (pd.notna(r["bssamt"]) and r["bssamt"] != 0 and pd.notna(r["plnprc"]))
        else float("nan"),
        axis=1,
    )
    return df


# ──────────────────────────────────────────────
# 필터링
# ──────────────────────────────────────────────
def apply_filters(df: pd.DataFrame, names: list[str], agencies: list[str]) -> pd.DataFrame:
    if names:
        mask = pd.Series(False, index=df.index)
        for kw in names:
            mask |= df["bidNtceNm"].str.contains(kw, na=False, regex=False)
        df = df[mask]

    if agencies:
        mask = pd.Series(False, index=df.index)
        for kw in agencies:
            mask |= df["dminsttNm"].str.contains(kw, na=False, regex=False)
            mask |= df["ntceInsttNm"].str.contains(kw, na=False, regex=False)
        df = df[mask]

    return df


# ──────────────────────────────────────────────
# 정렬
# ──────────────────────────────────────────────
SORT_MAP = {
    "날짜": ("opengDt", False),
    "사정율": ("사정율(%)", False),
    "금액": ("bssamt", False),
}


def apply_sort(df: pd.DataFrame, sort_key: str) -> pd.DataFrame:
    col, asc = SORT_MAP[sort_key]
    return df.sort_values(col, ascending=asc, na_position="last")


# ──────────────────────────────────────────────
# 출력
# ──────────────────────────────────────────────
def _trunc(s: str | None, n: int = 30) -> str:
    if not isinstance(s, str):
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_money(x) -> str:
    if pd.isna(x):
        return ""
    return f"{x:,.0f}"


def _fmt_rate(x) -> str:
    if pd.isna(x):
        return ""
    return f"{x:.4f}"


def build_filter_label(names: list[str], agencies: list[str]) -> str:
    parts = []
    if names:
        parts.append(f'공고명{names}')
    if agencies:
        parts.append(f'기관{agencies}')
    return " / ".join(parts) if parts else "전체"


def print_results(df: pd.DataFrame, names: list[str], agencies: list[str]) -> None:
    total = len(df)
    valid = df["사정율(%)"].notna().sum()
    label = build_filter_label(names, agencies)

    print()
    print("══════════════════════════════════════════════════════════════")
    print(f"  📊 조회 결과: {total:,}건  (사정율 유효: {valid:,}건)")
    print(f"  필터: {label}")
    print("══════════════════════════════════════════════════════════════")

    if valid > 0:
        rates = df["사정율(%)"].dropna()
        print(
            f"  평균 {rates.mean():.4f}%  |  중앙 {rates.median():.4f}%  |"
            f"  범위 {rates.min():.2f}~{rates.max():.2f}%  |  표준편차 {rates.std():.4f}%"
        )
    else:
        print("  사정율 유효 데이터 없음")

    print("──────────────────────────────────────────────────────────────")

    if total == 0:
        print("  (결과 없음)")
        print()
        return

    # 표시용 DataFrame 구성
    disp = pd.DataFrame()
    disp["공고번호"] = df["bidNtceNo"]
    disp["공고명"] = df["bidNtceNm"].apply(lambda s: _trunc(s, 30))
    disp["수요기관명"] = df["dminsttNm"].fillna("")
    disp["기초금액"] = df["bssamt"].apply(_fmt_money)
    disp["예정가격"] = df["plnprc"].apply(_fmt_money)
    disp["사정율(%)"] = df["사정율(%)"].apply(_fmt_rate)
    disp["개찰일시"] = df["opengDt"].fillna("")

    pd.set_option("display.max_rows", None)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 0)
    pd.set_option("display.max_colwidth", None)

    print(disp.to_string(index=False))
    print()


# ──────────────────────────────────────────────
# CSV 저장
# ──────────────────────────────────────────────
def save_csv(df: pd.DataFrame, names: list[str], agencies: list[str]) -> str:
    keywords = names + agencies
    suffix = "_".join(keywords) if keywords else "전체"
    fname = f"조회결과_{suffix}.csv"

    out = df[["bidNtceNo", "bidNtceNm", "dminsttNm", "bssamt", "plnprc", "사정율(%)", "opengDt"]].copy()
    out.columns = ["공고번호", "공고명", "수요기관명", "기초금액", "예정가격", "사정율(%)", "개찰일시"]
    out.to_csv(fname, index=False, encoding="utf-8-sig")
    return fname


# ──────────────────────────────────────────────
# Plot
# ──────────────────────────────────────────────
def save_plot(df: pd.DataFrame, names: list[str], agencies: list[str]) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    font_name = _find_korean_font()
    if font_name:
        plt.rcParams["font.family"] = font_name
    plt.rcParams["axes.unicode_minus"] = False

    rates = df["사정율(%)"].dropna()
    if len(rates) == 0:
        print("⚠️  사정율 유효 데이터가 없어 분포도를 생성할 수 없습니다.")
        return ""

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.hist(rates, bins=30, color="steelblue", edgecolor="white", alpha=0.85)

    mean_val = rates.mean()
    med_val = rates.median()
    ax.axvline(mean_val, color="red", linestyle="--", linewidth=1.5,
               label=f"평균 {mean_val:.4f}%")
    ax.axvline(med_val, color="royalblue", linestyle="--", linewidth=1.5,
               label=f"중앙값 {med_val:.4f}%")

    label = build_filter_label(names, agencies)
    ax.set_title(f"사정율 분포 — {label}", fontsize=13)
    ax.set_xlabel("사정율 (%)")
    ax.set_ylabel("건수")
    ax.legend()
    fig.tight_layout()

    keywords = names + agencies
    suffix = "_".join(keywords) if keywords else "전체"
    fname = f"분포도_{suffix}.png"
    fig.savefig(fname, dpi=150)
    plt.close(fig)
    return fname


def open_file(path: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        print(f"⚠️  파일 자동 열기 실패: {e}")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="나라장터 DB 조회 CLI — 사정율 필터링·통계·분포도"
    )
    p.add_argument("-n", "--name", action="append", default=[],
                   metavar="키워드", help="공고명 키워드 (여러 번 사용 시 OR)")
    p.add_argument("-a", "--agency", action="append", default=[],
                   metavar="키워드", help="기관 키워드 (수요/공고기관 OR, 여러 번 사용 시 OR)")
    p.add_argument("--db", default="nara_bid.db", metavar="파일",
                   help="DB 파일 경로 (기본: nara_bid.db)")
    p.add_argument("--limit", type=int, default=None, metavar="N",
                   help="출력 최대 건수")
    p.add_argument("--sort", choices=["사정율", "날짜", "금액"], default="날짜",
                   help="정렬 기준 (기본: 날짜 내림차순)")
    p.add_argument("--csv", action="store_true", help="결과를 CSV 파일로도 저장")
    p.add_argument("--plot", action="store_true", help="사정율 분포 히스토그램 PNG 저장 + 자동 열기 (현재 비활성화)")
    return p.parse_args()


def main() -> None:
    # Windows cp949 터미널에서 이모지/한글 깨짐 방지
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = parse_args()

    if not Path(args.db).exists():
        print(f"❌  DB 파일을 찾을 수 없습니다: {args.db}")
        sys.exit(1)

    print(f"⏳  DB 로딩 중... ({args.db})")
    df = load_db(args.db)

    df = apply_filters(df, args.name, args.agency)
    df = apply_sort(df, args.sort)

    if args.limit is not None:
        df = df.head(args.limit)

    print_results(df, args.name, args.agency)

    if args.csv:
        fname = save_csv(df, args.name, args.agency)
        print(f"📄  CSV 저장: {fname}")

    if args.plot:
        print("⚠️  --plot 기능은 현재 비활성화되어 있습니다.")


if __name__ == "__main__":
    main()
