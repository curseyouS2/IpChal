# CLAUDE.md — 나라장터 용역 사정율 예측 AI 프로젝트

## 프로젝트 개요

나라장터(조달청 공공데이터) API로 **용역** 분야 개찰결과를 수집하고, **사정율 예측 AI**를 만들기 위한 데이터 파이프라인 프로젝트.

- 목표: "건축 설계 및 감리(용역)" 분야의 사정율(예정가격/기초금액×100)을 예측하는 모델 학습 데이터 구축
- 현재 단계: 데이터 수집 스크립트 (`nara_service_bid_collector.py`) 완성, 정상 동작 확인

---

## 핵심 도메인 용어

| 용어 | 의미 |
|---|---|
| **기초금액** (`bssamt`) | 설계가격을 검토·조정한 가격. 복수예비가격 산출의 기준 |
| **예정가격** (`plnprc`) | 복수예비가 중 추첨된 것들의 산술평균. 낙찰자 선정 기준 상한가 |
| **사정율(%)** | `(예정가격 / 기초금액) × 100`. 보통 98~102% 범위에 분포 |
| **복수예비가격** | 기초금액의 ±2~3% 범위에서 15개 생성, 그 중 4개를 추첨하여 평균 → 예정가격 |
| **개찰** | 입찰서를 열어보는 행위. 개찰 후 낙찰자 선정 |
| **rsrvtnPrceFileExistnceYn** | "Y"면 복수예비가격 데이터 존재, "N"이면 협상계약 등으로 없음 |

---

## 조달청 API 명세

### 서비스 기본 정보

- **서비스 ID**: `ScsbidInfoService` (나라장터 낙찰정보서비스)
- **Base URL**: `http://apis.data.go.kr/1230000/as/ScsbidInfoService`
- **인증**: ServiceKey (공공데이터포털 발급)
- **프로토콜**: REST (GET)
- **응답 형식**: XML (기본) / JSON (`type=json` 파라미터 추가 시)

### 사용하는 오퍼레이션 2개

#### 1. 개찰결과 용역 목록 조회

```
GET /getOpengResultListInfoServc
```

**요청 파라미터:**

| 파라미터 | 필수 | 설명 |
|---|---|---|
| `ServiceKey` | 필수 | 공공데이터포털 인증키 |
| `type` | 선택 | `json` 지정 시 JSON 응답 |
| `inqryDiv` | 필수 | 조회구분: `1`=입력일시, `2`=공고일시, `3`=개찰일시, `4`=입찰공고번호 |
| `inqryBgnDt` | 조건부 | 조회시작 `YYYYMMDDHHMM` (inqryDiv 1,2,3일 때 필수) |
| `inqryEndDt` | 조건부 | 조회종료 `YYYYMMDDHHMM` (inqryDiv 1,2,3일 때 필수) |
| `bidNtceNo` | 조건부 | 입찰공고번호 (inqryDiv 4일 때 필수) |
| `numOfRows` | 선택 | 한 페이지 결과 수 (기본 10, 최대 999) |
| `pageNo` | 선택 | 페이지 번호 |

**응답 주요 필드:**

| 필드명 | 설명 |
|---|---|
| `bidNtceNo` | 입찰공고번호 (PK) |
| `bidNtceOrd` | 입찰공고차수 |
| `bidClsfcNo` | 입찰분류번호 |
| `rbidNo` | 재입찰번호 |
| `bidNtceNm` | 입찰공고명 |
| `opengDt` | 개찰일시 `YYYY-MM-DD HH:MM:SS` |
| `prtcptCnum` | 참가업체수 |
| `opengCorpInfo` | 개찰업체정보 (^구분자: 업체명^사업자번호^대표자^투찰금액^투찰율) |
| `progrsDivCdNm` | 진행구분: 유찰 / 개찰완료 / 재입찰 |
| `rsrvtnPrceFileExistnceYn` | 예비가격파일존재여부 `Y`/`N` |
| `ntceInsttNm` | 공고기관명 |
| `dminsttNm` | 수요기관명 |
| `dminsttCd` | 수요기관코드 |

#### 2. 개찰결과 용역 예비가격상세 목록 조회

```
GET /getOpengResultListInfoServcPreparPcDetail
```

**요청 파라미터:**

| 파라미터 | 필수 | 설명 |
|---|---|---|
| `ServiceKey` | 필수 | 인증키 |
| `type` | 선택 | `json` |
| `inqryDiv` | 필수 | `1`=입력일시, `2`=입찰공고번호 |
| `inqryBgnDt` | 조건부 | inqryDiv 1일 때 필수 |
| `inqryEndDt` | 조건부 | inqryDiv 1일 때 필수 |
| `bidNtceNo` | 조건부 | inqryDiv 2일 때 필수 |
| `numOfRows` | 선택 | 기본 10 |
| `pageNo` | 선택 | 페이지 번호 |

**응답 주요 필드:**

| 필드명 | 설명 |
|---|---|
| `bidNtceNo` | 입찰공고번호 |
| `plnprc` | **예정가격** (원) |
| `bssamt` | **기초금액** (원) |
| `totRsrvtnPrceNum` | 총예가건수 (보통 15) |
| `compnoRsrvtnPrceSno` | 복수예가순번 (1~15) |
| `bsisPlnprc` | 기초예정가격 (개별 복수예비가격) |
| `drwtYn` | 추첨여부 `Y`/`N` |
| `drwtNum` | 추첨횟수 |
| `bidwinrSlctnAplBssCntnts` | 낙찰자선정적용기준 (조달청/행자부) |
| `bssamtBssUpNum` | 기초금액기준상위건수 |

> **참고**: 1개 공고에 복수예가 15행이 반환됨. `plnprc`(예정가격)과 `bssamt`(기초금액)은 모든 행에서 동일값 → 1행만 읽으면 됨.

---

## API 호출 시 반드시 지켜야 할 제약사항

### 조회 기간 제한 (가장 중요)
- **1회 API 호출의 조회 기간은 최대 1개월** 이내로 제한할 것
- 1년치를 한 번에 요청하면 서버가 HTTP 500 또는 타임아웃 반환
- 현재 구현: `generate_monthly_ranges()`로 1개월 단위 청킹

### 호출 빈도 제한
- 초당 최대 30 TPS (문서 기준)
- 현재 구현: `REQUEST_DELAY = 0.5`초 (안전 마진 포함)

### 예비가격이 없는 공고
- `rsrvtnPrceFileExistnceYn == "N"` 인 공고는 예비가격상세 API를 호출해도 데이터 없음
- 원인: 협상에 의한 계약, 자체전자조달시스템, 유찰/재입찰 미확정 건
- 현재 구현: `"Y"` 인 공고만 2단계 API 호출, 나머지 스킵

### 기관 필터링 (API가 아닌 후처리)
- API 요청 파라미터에 기관 필터가 **없음** — `inqryDiv`, `inqryBgnDt`, `inqryEndDt`, `bidNtceNo`만 지원
- 따라서 1단계에서 전체 용역 데이터를 수집한 뒤, **1.5단계에서 파이썬으로 후처리 필터링**
- `dminsttNm`(수요기관명) 또는 `ntceInsttNm`(공고기관명) 중 하나라도 키워드 포함 시 매칭
- 현재 키워드: `국방부`, `육군`, `해군`, `공군`, `국방시설`, `방위사업청`, `국군`
- `AGENCY_KEYWORDS = []` 으로 비우면 필터 OFF (전체 수집)

### 응답 JSON 파싱 주의점
- `items`가 `dict`(정상), 빈 문자열 `""`, `null`, `list` 등 다양한 형태로 올 수 있음
- 단건 응답 시 `item`이 리스트가 아니라 단일 `dict`로 옴 → 리스트로 감싸기 필요
- `extract_items()` 함수가 이 모든 케이스를 처리

---

## 프로젝트 파일 구조

```
project/
├── CLAUDE.md                          ← 이 파일
├── nara_service_bid_collector.py      ← 메인 데이터 수집 스크립트
├── 나라장터_용역_사정율_데이터.csv       ← 출력: 전체 (NaN 포함)
├── 나라장터_용역_사정율_데이터_유효만.csv  ← 출력: 사정율 유효만 (AI 학습용)
├── _checkpoint_list.csv               ← 중간저장 (수집 완료 시 자동 삭제)
└── _checkpoint_detail.csv             ← 중간저장 (수집 완료 시 자동 삭제)
```

---

## 메인 스크립트 아키텍처 (`nara_service_bid_collector.py`)

### 실행 파이프라인

```
STEP 1: fetch_service_bid_list()
  ├─ 월별 청크 생성 (generate_monthly_ranges)
  ├─ 각 월마다 getOpengResultListInfoServc 호출
  ├─ 페이지네이션 자동 처리 (numOfRows=999)
  ├─ safe_get()으로 재시도 (지수 백오프)
  └─ 매 월 _checkpoint_list.csv 중간 저장

STEP 1.5: filter_by_agency()
  ├─ AGENCY_KEYWORDS가 빈 리스트면 스킵 (전체 수집)
  ├─ dminsttNm OR ntceInsttNm에 키워드 포함 여부 체크
  ├─ 매칭된 공고만 남기고 나머지 제거
  └─ 매칭 기관명 예시 출력 (검증용)

STEP 2: enrich_with_price_details()
  ├─ rsrvtnPrceFileExistnceYn == "Y" 필터링
  ├─ Y인 공고만 getOpengResultListInfoServcPreparPcDetail 호출
  ├─ N인 공고는 스킵 (빈 레코드로 추가)
  └─ 100건마다 _checkpoint_detail.csv 중간 저장

STEP 3: build_final_dataframe()
  ├─ 목록 + 예비가격 merge (bidNtceNo 기준)
  ├─ 사정율(%) = (예정가격 / 기초금액) × 100
  ├─ 기초금액 0인 경우 NaN 처리 (ZeroDivision 방지)
  └─ 유효/무효 통계 출력

OUTPUT: CSV 2종 저장 + 사정율 통계 출력
```

### 핵심 함수 요약

| 함수 | 역할 |
|---|---|
| `generate_monthly_ranges(start, end)` | 기간을 1개월 단위 `(start, end)` 튜플 리스트로 분할 |
| `fmt_dt(dt)` | datetime → `YYYYMMDDHHMM` 문자열 (API 파라미터용) |
| `safe_get(url, params)` | requests.get + 최대 3회 재시도 (지수 백오프 2/4/8초) |
| `extract_items(data)` | 공공데이터 JSON 응답 → `(item_list, total_count)` 추출 |
| `fetch_service_bid_list()` | STEP 1 — 용역 개찰결과 목록 수집 (월별 청킹) |
| `filter_by_agency(df)` | STEP 1.5 — AGENCY_KEYWORDS로 수요/공고기관 OR 필터링 |
| `fetch_price_detail(bid_ntce_no)` | 단일 공고의 기초금액·예정가격 조회 |
| `enrich_with_price_details(df_list)` | STEP 2 — 예비가격 일괄 조회 + Y/N 필터링 |
| `build_final_dataframe(df_list, df_detail)` | STEP 3 — 병합 + 사정율 계산 |

---

## 설정값 (스크립트 상단)

```python
API_KEY       = "ff0c8399785817c6ac0c9640ade14525a05233ce7bd3e51e54982741106d0314"
BASE_URL      = "http://apis.data.go.kr/1230000/as/ScsbidInfoService"
ROWS_PER_PAGE = 999      # 한 페이지 최대 (API 허용 최대)
REQUEST_DELAY = 0.5      # 호출 간 딜레이(초)
MAX_RETRIES   = 3        # 실패 시 재시도 횟수
RETRY_BASE    = 2.0      # 지수 백오프 기본 대기(초)

# 기관 필터 — 빈 리스트 []면 필터 OFF
AGENCY_KEYWORDS = ["국방부", "육군", "해군", "공군", "국방시설", "방위사업청", "국군"]
```

---

## 최종 CSV 컬럼 스키마

| 컬럼명 | 타입 | 설명 |
|---|---|---|
| `공고번호` | str | 입찰공고번호 (`R25BK00882586` 형식) |
| `공고명` | str | 입찰공고명 |
| `수요기관명` | str | 실제 수요기관명 |
| `기초금액` | float | 기초금액 (원) |
| `예정가격` | float | 예정가격 (원) |
| `참가업체수` | float | 참가업체 수 |
| `사정율(%)` | float | (예정가격/기초금액)×100, 보통 98~102 |
| `개찰일시` | str | `YYYY-MM-DD HH:MM:SS` |
| `진행구분` | str | 유찰 / 개찰완료 / 재입찰 |

---

## 의존성

```
pip install requests pandas python-dateutil
```

---

## v3 — SQLite + 증분 수집 (`nara_bid_v3.py`)

### 왜 v3인가
v2는 매번 전체 1년치를 수집하고 CSV로 저장하는 구조. v3는 SQLite DB에 데이터를 누적하고, 실행 시마다 마지막 수집 시점 이후의 신규 데이터만 수집하는 증분(incremental) 방식.

### DB 스키마 (`nara_bid.db`)

| 테이블 | 역할 |
|---|---|
| `bid_list` | 1단계 개찰결과 목록. PK: (bidNtceNo, bidNtceOrd, bidClsfcNo, rbidNo) |
| `bid_price` | 2단계 예비가격. PK: bidNtceNo. bssamt(기초금액), plnprc(예정가격) |
| `sync_log` | 수집 이력. sync_type, 기간, 신규 건수, 시작/완료 시각 |

### 증분 수집 동작 원리

1. `bid_list`에서 `MAX(opengDt)` 조회 → 마지막 개찰일시 확인
2. (마지막 개찰일시 - 1일) ~ 현재까지만 API 호출 (겹침 허용, INSERT OR IGNORE)
3. `bid_list`에는 있지만 `bid_price`에 없는 공고만 예비가격 API 호출
4. DB가 비어있으면 자동으로 최근 365일 전체 수집

### CLI 사용법

```bash
# 증분 수집 (기본)
python nara_bid_v3.py

# 기존 CSV → DB 마이그레이션
python nara_bid_v3.py --migrate 나라장터_용역_사정율_데이터.csv

# DB 무시하고 1년치 전체 재수집
python nara_bid_v3.py --full

# DB → CSV 내보내기
python nara_bid_v3.py --export

# DB 통계만 출력
python nara_bid_v3.py --stats
```

### 권장 초기 세팅 순서

```bash
# 1. 기존 CSV가 있으면 먼저 마이그레이션
python nara_bid_v3.py --migrate 나라장터_용역_사정율_데이터_유효만.csv

# 2. 이후부터는 실행할 때마다 자동 증분
python nara_bid_v3.py

# 3. AI 학습용 CSV가 필요하면
python nara_bid_v3.py --export
```

---

## 파일 구조 (v3 기준)

```
project/
├── CLAUDE.md                          ← 이 파일
├── nara_bid_v3.py                     ← 메인 스크립트 (SQLite + 증분)
├── nara_bid.db                        ← SQLite DB (자동 생성)
├── nara_service_bid_collector.py      ← v2 스크립트 (CSV 기반, 레거시)
├── 나라장터_용역_사정율_데이터.csv       ← --export 시 생성
└── 나라장터_용역_사정율_데이터_유효만.csv  ← --export 시 생성
```

---

## 코딩 컨벤션

- 언어: Python 3.10+
- 타입 힌트 사용 (`dict | None` 유니온 문법)
- 한국어 변수명은 최종 DataFrame 컬럼에만 사용, 함수/변수명은 영문
- API 필드명은 조달청 원본 그대로 유지 (camelCase: `bidNtceNo`, `plnprc` 등)
- print 로그에 이모지 사용 (✅ ⚠️ ❌ ⏳ 📄 📊)

---

## 알려진 이슈 및 향후 과제

### 현재 이슈
1. **가격 미확보 공고 비율**: 전체 공고 중 30~50%는 예비가격 데이터가 없음 (협상계약 등)
2. **API 간헐적 500 에러**: 조달청 서버 부하 시 발생 → safe_get 재시도로 대응 중

### 향후 확장 계획
- [x] ~~SQLite DB 도입~~ (v3 완료)
- [x] ~~증분 수집 (실행 시 자동 체크)~~ (v3 완료)
- [x] ~~기존 CSV → DB 마이그레이션~~ (v3 완료)
- [ ] 수집 데이터 기반 사정율 예측 모델 학습 (XGBoost, LightGBM 등)
- [ ] '건축 설계 및 감리' 세부 분류 필터링 로직 추가 (현재는 용역 전체)
- [ ] 복수예비가격 15개 개별값(`bsisPlnprc`) 피처로 활용
- [ ] 추첨여부(`drwtYn`), 추첨횟수(`drwtNum`) 피처 추가
- [ ] 수요기관 유형별 사정율 분포 분석
- [ ] cron/systemd timer 연동 (일별 자동 수집)
