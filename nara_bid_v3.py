"""
나라장터 용역 사정율 수집기 (v3 — SQLite + 증분 수집)
====================================================
실행할 때마다 DB의 마지막 수집 시점 이후 ~ 현재까지 신규 데이터만 자동 수집.
기존 CSV 데이터 → DB 마이그레이션도 지원.

사용법:
  python nara_bid_v3.py                  # 일반 실행 (증분 수집)
  python nara_bid_v3.py --migrate CSV    # 기존 CSV → DB 마이그레이션
  python nara_bid_v3.py --full           # DB 무시하고 1년치 전체 재수집
  python nara_bid_v3.py --export         # DB → CSV 내보내기
  python nara_bid_v3.py --stats          # DB 통계만 출력
"""

import requests
import pandas as pd
import sqlite3
import time
import math
import os
import sys
import argparse
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

# ──────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────
API_KEY  = "ff0c8399785817c6ac0c9640ade14525a05233ce7bd3e51e54982741106d0314"
BASE_URL = "http://apis.data.go.kr/1230000/as/ScsbidInfoService"

DB_FILE       = "nara_bid.db"
ROWS_PER_PAGE = 999
REQUEST_DELAY = 0.5
MAX_RETRIES   = 3
RETRY_BASE    = 2.0

# 기관 필터 — 빈 리스트 [] 면 필터 OFF
AGENCY_KEYWORDS = [
    "국방부", "육군", "해군", "공군",
    "국방시설", "방위사업청", "국군",
]

# 최초 수집 시 기본 기간 (1년)
DEFAULT_LOOKBACK_DAYS = 365


# ══════════════════════════════════════════════
# DB 레이어
# ══════════════════════════════════════════════
def init_db(db_path: str = DB_FILE) -> sqlite3.Connection:
    """DB 초기화. 테이블이 없으면 생성."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")  # 동시 읽기 성능 향상

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS bid_list (
        bidNtceNo               TEXT NOT NULL,
        bidNtceOrd              TEXT,
        bidClsfcNo              TEXT,
        rbidNo                  TEXT,
        bidNtceNm               TEXT,
        opengDt                 TEXT,
        prtcptCnum              TEXT,
        opengCorpInfo           TEXT,
        progrsDivCdNm           TEXT,
        rsrvtnPrceFileExistnceYn TEXT,
        ntceInsttCd             TEXT,
        ntceInsttNm             TEXT,
        dminsttCd               TEXT,
        dminsttNm               TEXT,
        inptDt                  TEXT,
        opengRsltNtcCntnts      TEXT,
        collected_at            TEXT DEFAULT (datetime('now','localtime')),
        PRIMARY KEY (bidNtceNo, bidNtceOrd, bidClsfcNo, rbidNo)
    );

    CREATE TABLE IF NOT EXISTS bid_price (
        bidNtceNo   TEXT PRIMARY KEY,
        bssamt      TEXT,
        plnprc      TEXT,
        collected_at TEXT DEFAULT (datetime('now','localtime'))
    );

    CREATE TABLE IF NOT EXISTS sync_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        sync_type   TEXT,
        sync_start  TEXT,
        sync_end    TEXT,
        new_list    INTEGER DEFAULT 0,
        new_price   INTEGER DEFAULT 0,
        started_at  TEXT DEFAULT (datetime('now','localtime')),
        finished_at TEXT
    );
    """)
    conn.commit()
    return conn


def get_last_sync_date(conn: sqlite3.Connection) -> datetime | None:
    """DB에 저장된 가장 최근 개찰일시를 반환. 없으면 None."""
    row = conn.execute(
        "SELECT MAX(opengDt) FROM bid_list"
    ).fetchone()
    if row and row[0]:
        try:
            return datetime.strptime(row[0][:16], "%Y-%m-%d %H:%M")
        except ValueError:
            return None
    return None


def get_db_stats(conn: sqlite3.Connection) -> dict:
    """DB 현황 통계."""
    stats = {}
    stats["total_list"] = conn.execute("SELECT COUNT(*) FROM bid_list").fetchone()[0]
    stats["unique_bids"] = conn.execute(
        "SELECT COUNT(DISTINCT bidNtceNo) FROM bid_list"
    ).fetchone()[0]
    stats["total_price"] = conn.execute("SELECT COUNT(*) FROM bid_price").fetchone()[0]
    stats["price_valid"] = conn.execute(
        "SELECT COUNT(*) FROM bid_price WHERE plnprc IS NOT NULL AND plnprc != ''"
    ).fetchone()[0]
    stats["last_opengDt"] = conn.execute(
        "SELECT MAX(opengDt) FROM bid_list"
    ).fetchone()[0]
    stats["last_sync"] = conn.execute(
        "SELECT MAX(finished_at) FROM sync_log WHERE finished_at IS NOT NULL"
    ).fetchone()[0]
    return stats


def upsert_list(conn: sqlite3.Connection, items: list[dict]) -> int:
    """bid_list에 UPSERT. 신규 삽입된 건수 반환."""
    if not items:
        return 0
    before = conn.execute("SELECT COUNT(*) FROM bid_list").fetchone()[0]
    conn.executemany("""
        INSERT OR IGNORE INTO bid_list
            (bidNtceNo, bidNtceOrd, bidClsfcNo, rbidNo,
             bidNtceNm, opengDt, prtcptCnum, opengCorpInfo,
             progrsDivCdNm, rsrvtnPrceFileExistnceYn,
             ntceInsttCd, ntceInsttNm, dminsttCd, dminsttNm,
             inptDt, opengRsltNtcCntnts)
        VALUES
            (:bidNtceNo, :bidNtceOrd, :bidClsfcNo, :rbidNo,
             :bidNtceNm, :opengDt, :prtcptCnum, :opengCorpInfo,
             :progrsDivCdNm, :rsrvtnPrceFileExistnceYn,
             :ntceInsttCd, :ntceInsttNm, :dminsttCd, :dminsttNm,
             :inptDt, :opengRsltNtcCntnts)
    """, items)
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM bid_list").fetchone()[0]
    return after - before


def upsert_price(conn: sqlite3.Connection, bid_no: str,
                 bssamt: str | None, plnprc: str | None) -> bool:
    """bid_price에 UPSERT. 신규면 True."""
    cur = conn.execute(
        "INSERT OR REPLACE INTO bid_price (bidNtceNo, bssamt, plnprc) VALUES (?,?,?)",
        (bid_no, bssamt, plnprc)
    )
    conn.commit()
    return cur.rowcount > 0


def get_bids_without_price(conn: sqlite3.Connection) -> list[dict]:
    """bid_list에는 있지만 bid_price에 없고, 예비가격 파일이 Y인 공고 목록."""
    rows = conn.execute("""
        SELECT DISTINCT l.bidNtceNo
        FROM bid_list l
        LEFT JOIN bid_price p ON l.bidNtceNo = p.bidNtceNo
        WHERE p.bidNtceNo IS NULL
          AND UPPER(l.rsrvtnPrceFileExistnceYn) = 'Y'
    """).fetchall()
    return [{"bidNtceNo": r[0]} for r in rows]


def export_to_dataframe(conn: sqlite3.Connection,
                       agency_keywords: list = None) -> pd.DataFrame:
    """
    DB에서 최종 분석용 DataFrame 생성.
    agency_keywords가 주어지면 수요기관명/공고기관명으로 필터링.
    """
    df = pd.read_sql_query("""
        SELECT
            l.bidNtceNo   AS 공고번호,
            l.bidNtceNm   AS 공고명,
            l.dminsttNm   AS 수요기관명,
            l.ntceInsttNm AS 공고기관명,
            p.bssamt      AS 기초금액,
            p.plnprc      AS 예정가격,
            l.prtcptCnum  AS 참가업체수,
            l.opengDt     AS 개찰일시,
            l.progrsDivCdNm AS 진행구분
        FROM bid_list l
        LEFT JOIN bid_price p ON l.bidNtceNo = p.bidNtceNo
        GROUP BY l.bidNtceNo
    """, conn)

    # 기관 필터 (내보내기/분석 시점에 적용)
    if agency_keywords:
        mask = pd.Series(False, index=df.index)
        for col in ["수요기관명", "공고기관명"]:
            if col in df.columns:
                col_str = df[col].fillna("").astype(str)
                for kw in agency_keywords:
                    mask = mask | col_str.str.contains(kw, na=False)
        before = len(df)
        df = df[mask].reset_index(drop=True)
        print(f"  🔍 기관 필터 적용: {before}건 → {len(df)}건")

    df["기초금액"]   = pd.to_numeric(df["기초금액"], errors="coerce")
    df["예정가격"]   = pd.to_numeric(df["예정가격"], errors="coerce")
    df["참가업체수"] = pd.to_numeric(df["참가업체수"], errors="coerce")

    df.loc[df["기초금액"] == 0, "기초금액"] = pd.NA
    df["사정율(%)"] = ((df["예정가격"] / df["기초금액"]) * 100).round(4)

    return df


# ══════════════════════════════════════════════
# API 유틸 (v2와 동일)
# ══════════════════════════════════════════════
def generate_monthly_ranges(start: datetime, end: datetime) -> list:
    ranges = []
    cursor = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor <= end:
        chunk_start = max(cursor, start)
        next_month = cursor + relativedelta(months=1)
        chunk_end = min(next_month - timedelta(seconds=1), end)
        ranges.append((chunk_start, chunk_end))
        cursor = next_month
    return ranges


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M")


def safe_get(url: str, params: dict, timeout: int = 30) -> dict | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            wait = RETRY_BASE ** attempt
            print(f"      ⏳ 타임아웃 (시도 {attempt}/{MAX_RETRIES}), {wait:.0f}초 후 재시도…")
            time.sleep(wait)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status in (500, 502, 503, 504):
                wait = RETRY_BASE ** attempt
                print(f"      ⚠️  HTTP {status} (시도 {attempt}/{MAX_RETRIES}), {wait:.0f}초 후 재시도…")
                time.sleep(wait)
            else:
                print(f"      ❌ HTTP {status}: {e}")
                return None
        except requests.exceptions.RequestException as e:
            wait = RETRY_BASE ** attempt
            print(f"      ⚠️  요청 오류 (시도 {attempt}/{MAX_RETRIES}): {e}")
            time.sleep(wait)
        except ValueError:
            print(f"      ❌ JSON 파싱 실패")
            time.sleep(RETRY_BASE)
    return None


def extract_items(data: dict) -> tuple:
    header = data.get("response", {}).get("header", {})
    if str(header.get("resultCode")) != "00":
        return [], 0
    body = data.get("response", {}).get("body", {})
    total_count = int(body.get("totalCount", 0))
    items = body.get("items", {})
    if isinstance(items, dict):
        item_list = items.get("item", [])
    elif isinstance(items, list):
        item_list = items
    elif items == "" or items is None:
        return [], total_count
    else:
        item_list = []
    if isinstance(item_list, dict):
        item_list = [item_list]
    return item_list, total_count


# ══════════════════════════════════════════════
# 기관 필터
# ══════════════════════════════════════════════
def matches_agency(item: dict) -> bool:
    """AGENCY_KEYWORDS 중 하나라도 수요기관/공고기관에 포함되면 True."""
    if not AGENCY_KEYWORDS:
        return True
    for col in ("dminsttNm", "ntceInsttNm"):
        val = (item.get(col) or "")
        for kw in AGENCY_KEYWORDS:
            if kw in val:
                return True
    return False


# ══════════════════════════════════════════════
# STEP 1: 개찰결과 목록 수집 → DB 저장
# ══════════════════════════════════════════════
def sync_bid_list(conn: sqlite3.Connection,
                  bgn: datetime, end: datetime) -> int:
    """
    bgn ~ end 구간의 용역 개찰결과를 월 단위로 수집 → DB 저장.
    필터 없이 전체 저장. 분석/내보내기 시 기관 필터 적용.
    신규 삽입 건수 반환.
    """
    url = f"{BASE_URL}/getOpengResultListInfoServc"
    monthly_ranges = generate_monthly_ranges(bgn, end)
    total_new = 0

    print(f"[STEP 1] 용역 개찰결과 목록 수집 (전체)")
    print(f"  기간: {bgn:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}")
    print(f"  청크: {len(monthly_ranges)}개\n")

    for ci, (c_start, c_end) in enumerate(monthly_ranges, 1):
        label = f"{c_start:%Y-%m-%d} ~ {c_end:%Y-%m-%d}"
        print(f"  [{ci}/{len(monthly_ranges)}] {label}", end="", flush=True)

        page = 1
        chunk_items = []

        while True:
            params = {
                "ServiceKey": API_KEY,
                "type":       "json",
                "inqryDiv":   "3",
                "inqryBgnDt": fmt_dt(c_start),
                "inqryEndDt": fmt_dt(c_end),
                "numOfRows":  ROWS_PER_PAGE,
                "pageNo":     page,
            }

            data = safe_get(url, params)
            if data is None:
                print(f" → 요청 실패, 건너뜀")
                break

            item_list, total_count = extract_items(data)
            if total_count == 0:
                print(f" → 0건")
                break

            chunk_items.extend(item_list)

            total_pages = math.ceil(total_count / ROWS_PER_PAGE)
            if page >= total_pages:
                break
            page += 1
            time.sleep(REQUEST_DELAY)

        if chunk_items:
            new = upsert_list(conn, chunk_items)
            total_new += new
            print(f" → {len(chunk_items)}건 수집, {new}건 신규")

        time.sleep(REQUEST_DELAY)

    print(f"\n  ✅ 1단계 완료: 신규 {total_new}건 DB 저장\n")
    return total_new


# ══════════════════════════════════════════════
# STEP 2: 예비가격상세 수집 → DB 저장
# ══════════════════════════════════════════════
def sync_bid_prices(conn: sqlite3.Connection) -> int:
    """
    bid_list에는 있지만 bid_price에 없는 공고의 예비가격을 수집.
    이미 가격이 있는 건은 자동으로 건너뜀 (증분).
    """
    missing = get_bids_without_price(conn)
    total = len(missing)

    if total == 0:
        print("[STEP 2] 예비가격 수집할 신규 공고 없음 — 스킵\n")
        return 0

    print(f"[STEP 2] 예비가격상세 조회 ({total}건)")

    url = f"{BASE_URL}/getOpengResultListInfoServcPreparPcDetail"
    ok_count = 0

    for idx, item in enumerate(missing):
        bid_no = item["bidNtceNo"]
        params = {
            "ServiceKey": API_KEY,
            "type":       "json",
            "inqryDiv":   "2",
            "bidNtceNo":  bid_no,
            "numOfRows":  "1",
            "pageNo":     "1",
        }

        data = safe_get(url, params)
        bssamt, plnprc = None, None

        if data:
            item_list, _ = extract_items(data)
            if item_list:
                bssamt = item_list[0].get("bssamt")
                plnprc = item_list[0].get("plnprc")

        upsert_price(conn, bid_no, bssamt, plnprc)
        if plnprc:
            ok_count += 1

        done = idx + 1
        if done % 100 == 0 or done == total:
            pct = done / total * 100
            print(f"  {done}/{total} ({pct:.1f}%) — 가격 확보 {ok_count}건")

        time.sleep(REQUEST_DELAY)

    print(f"\n  ✅ 2단계 완료: {total}건 호출, 가격 확보 {ok_count}건\n")
    return ok_count


# ══════════════════════════════════════════════
# 메인 커맨드
# ══════════════════════════════════════════════
def cmd_sync(args):
    """기본 실행 — 증분 수집."""
    conn = init_db()
    now = datetime.now()

    stats = get_db_stats(conn)
    print("=" * 60)
    print("  나라장터 용역 사정율 수집기  (v3 — SQLite)")
    print("=" * 60)

    if AGENCY_KEYWORDS:
        print(f"  분석 필터: {', '.join(AGENCY_KEYWORDS)} (수집은 전체)")

    if stats["total_list"] == 0:
        # DB가 비어있으면 1년치 전체 수집
        bgn = now - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        print(f"  DB 비어있음 → 최근 {DEFAULT_LOOKBACK_DAYS}일 전체 수집")
        mode = "full_initial"
    else:
        last_dt = get_last_sync_date(conn)
        if last_dt:
            # 마지막 개찰일시 - 1일 부터 (겹침 허용, UPSERT 처리)
            bgn = last_dt - timedelta(days=1)
            gap_days = (now - last_dt).days
            print(f"  DB 현황: {stats['unique_bids']}건 (마지막 개찰: {stats['last_opengDt']})")
            print(f"  증분 수집: {bgn:%Y-%m-%d} ~ {now:%Y-%m-%d} ({gap_days}일 분)")
            mode = "incremental"
        else:
            bgn = now - timedelta(days=DEFAULT_LOOKBACK_DAYS)
            mode = "full_initial"

    print()

    # sync_log 기록 시작
    conn.execute(
        "INSERT INTO sync_log (sync_type, sync_start, sync_end) VALUES (?,?,?)",
        (mode, bgn.isoformat(), now.isoformat())
    )
    conn.commit()
    log_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # STEP 1
    new_list = sync_bid_list(conn, bgn, now)

    # STEP 2
    new_price = sync_bid_prices(conn)

    # sync_log 기록 완료
    conn.execute(
        "UPDATE sync_log SET new_list=?, new_price=?, finished_at=datetime('now','localtime') WHERE id=?",
        (new_list, new_price, log_id)
    )
    conn.commit()

    # 결과 요약
    stats = get_db_stats(conn)
    print("=" * 60)
    print(f"  ✅ 수집 완료")
    print(f"     DB 전체: {stats['unique_bids']}건")
    print(f"     가격 확보: {stats['price_valid']}건")
    print(f"     이번 신규 목록: +{new_list}건")
    print(f"     이번 신규 가격: +{new_price}건")
    print(f"     마지막 개찰일: {stats['last_opengDt']}")
    print("=" * 60)

    # 사정율 통계
    df = export_to_dataframe(conn)
    valid = df["사정율(%)"].dropna()
    if not valid.empty:
        print(f"\n  📊 사정율 통계 (전체 DB):")
        print(f"     평균     : {valid.mean():.4f}%")
        print(f"     중앙값   : {valid.median():.4f}%")
        print(f"     최솟값   : {valid.min():.4f}%")
        print(f"     최댓값   : {valid.max():.4f}%")
        print(f"     유효 건수: {len(valid)} / {len(df)}")

    conn.close()


def cmd_full(args):
    """전체 재수집."""
    conn = init_db()
    now = datetime.now()
    bgn = now - timedelta(days=DEFAULT_LOOKBACK_DAYS)

    print(f"  ⚡ 전체 재수집 모드: {bgn:%Y-%m-%d} ~ {now:%Y-%m-%d}\n")

    conn.execute(
        "INSERT INTO sync_log (sync_type, sync_start, sync_end) VALUES (?,?,?)",
        ("full_resync", bgn.isoformat(), now.isoformat())
    )
    conn.commit()
    log_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    new_list = sync_bid_list(conn, bgn, now)
    new_price = sync_bid_prices(conn)

    conn.execute(
        "UPDATE sync_log SET new_list=?, new_price=?, finished_at=datetime('now','localtime') WHERE id=?",
        (new_list, new_price, log_id)
    )
    conn.commit()

    stats = get_db_stats(conn)
    print(f"  ✅ 전체 재수집 완료 — DB {stats['unique_bids']}건")
    conn.close()


def cmd_migrate(args):
    """기존 CSV → DB 마이그레이션."""
    csv_path = args.migrate
    if not os.path.exists(csv_path):
        print(f"  ❌ 파일 없음: {csv_path}")
        return

    conn = init_db()
    df = pd.read_csv(csv_path, dtype=str)
    print(f"  📥 CSV 로드: {csv_path} ({len(df)}건)")

    # 컬럼명 매핑 (한글 → API 필드명)
    col_map_reverse = {
        "공고번호": "bidNtceNo",
        "공고명": "bidNtceNm",
        "수요기관명": "dminsttNm",
        "기초금액": "bssamt",
        "예정가격": "plnprc",
        "참가업체수": "prtcptCnum",
        "개찰일시": "opengDt",
        "진행구분": "progrsDivCdNm",
    }
    df_mapped = df.rename(columns=col_map_reverse)

    # bid_list에 삽입 가능한 행 구성
    list_inserted = 0
    price_inserted = 0

    for _, row in df_mapped.iterrows():
        bid_no = row.get("bidNtceNo")
        if not bid_no or pd.isna(bid_no):
            continue

        # bid_list에 최소한의 정보로 삽입
        item = {
            "bidNtceNo": bid_no,
            "bidNtceOrd": row.get("bidNtceOrd", "000"),
            "bidClsfcNo": row.get("bidClsfcNo", "0"),
            "rbidNo": row.get("rbidNo", "000"),
            "bidNtceNm": row.get("bidNtceNm"),
            "opengDt": row.get("opengDt"),
            "prtcptCnum": row.get("prtcptCnum"),
            "opengCorpInfo": None,
            "progrsDivCdNm": row.get("progrsDivCdNm"),
            "rsrvtnPrceFileExistnceYn": "Y" if row.get("plnprc") else "N",
            "ntceInsttCd": None,
            "ntceInsttNm": row.get("ntceInsttNm"),
            "dminsttCd": None,
            "dminsttNm": row.get("dminsttNm"),
            "inptDt": None,
            "opengRsltNtcCntnts": None,
        }
        new = upsert_list(conn, [item])
        list_inserted += new

        # bid_price 삽입
        bssamt = row.get("bssamt")
        plnprc = row.get("plnprc")
        if bssamt or plnprc:
            upsert_price(conn, bid_no, bssamt, plnprc)
            price_inserted += 1

    print(f"  ✅ 마이그레이션 완료")
    print(f"     bid_list 신규: {list_inserted}건")
    print(f"     bid_price 저장: {price_inserted}건")

    stats = get_db_stats(conn)
    print(f"     DB 전체: {stats['unique_bids']}건")
    conn.close()


def cmd_export(args):
    """DB → CSV 내보내기. --filter 시 기관 필터 적용."""
    conn = init_db()
    kw = AGENCY_KEYWORDS if args.filter else None
    df = export_to_dataframe(conn, agency_keywords=kw)

    suffix = "_국방부" if args.filter else ""
    out_all   = f"나라장터_용역_사정율_데이터{suffix}.csv"
    out_valid = f"나라장터_용역_사정율_데이터{suffix}_유효만.csv"

    df.to_csv(out_all, index=False, encoding="utf-8-sig")
    df_valid = df.dropna(subset=["사정율(%)"])
    df_valid.to_csv(out_valid, index=False, encoding="utf-8-sig")

    print(f"  📄 전체: {out_all} ({len(df)}건)")
    print(f"  📄 유효: {out_valid} ({len(df_valid)}건)")
    conn.close()


def cmd_stats(args):
    """DB 통계 출력."""
    conn = init_db()
    stats = get_db_stats(conn)

    print("=" * 50)
    print("  📊 DB 현황")
    print("=" * 50)
    print(f"  전체 공고:     {stats['unique_bids']}건")
    print(f"  가격 수집:     {stats['total_price']}건")
    print(f"  가격 유효:     {stats['price_valid']}건")
    print(f"  마지막 개찰일: {stats['last_opengDt'] or '없음'}")
    print(f"  마지막 동기화: {stats['last_sync'] or '없음'}")

    # 최근 sync_log 5건
    rows = conn.execute(
        "SELECT sync_type, sync_start, sync_end, new_list, new_price, finished_at "
        "FROM sync_log ORDER BY id DESC LIMIT 5"
    ).fetchall()
    if rows:
        print(f"\n  최근 수집 이력:")
        for r in rows:
            print(f"    {r[5] or '진행중'} | {r[0]} | +{r[3]}목록 +{r[4]}가격")

    # 사정율 통계
    df = export_to_dataframe(conn)
    valid = df["사정율(%)"].dropna()
    if not valid.empty:
        print(f"\n  사정율 통계:")
        print(f"    평균 {valid.mean():.4f}% | 중앙 {valid.median():.4f}% | "
              f"범위 {valid.min():.4f}~{valid.max():.4f}%")

    conn.close()


# ══════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="나라장터 용역 사정율 수집기 v3"
    )
    parser.add_argument(
        "--migrate", metavar="CSV",
        help="기존 CSV 파일 → DB 마이그레이션"
    )
    parser.add_argument(
        "--full", action="store_true",
        help="DB 무시하고 1년치 전체 재수집"
    )
    parser.add_argument(
        "--export", action="store_true",
        help="DB → CSV 내보내기"
    )
    parser.add_argument(
        "--filter", action="store_true",
        help="--export 시 AGENCY_KEYWORDS로 기관 필터링 적용"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="DB 통계만 출력"
    )

    args = parser.parse_args()

    if args.migrate:
        cmd_migrate(args)
    elif args.full:
        cmd_full(args)
    elif args.export:
        cmd_export(args)
    elif args.stats:
        cmd_stats(args)
    else:
        cmd_sync(args)


if __name__ == "__main__":
    main()
