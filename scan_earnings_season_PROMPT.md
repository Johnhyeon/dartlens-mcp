# 작업 — dartlens MCP에 `scan_earnings_season` 도구 추가

## 한 줄 목적
"이번 분기 실적 다 뒤져서 두드러진 회사 추려줘"를 **한 번의 MCP 호출 + ~60초 + 결과 토큰 ≤ 8K**로 처리할 수 있게 한다. 기존 `get_disclosure_detail` × N 단건 fan-out 패턴을 대체하는 서버사이드 스캔 도구.

---

## 작업 위치 / 기존 컨벤션

- 저장소 루트: `D:\project\stocklens\mcp-dart\`
- 메인 서버: `dartlens/server.py` — 여기에 새 `@mcp.tool()` 추가
- HTTP 헬퍼: `dartlens/_http.py` — fnlttMultiAcnt 호출 함수 추가
- 캐시: `dartlens/_cache.py` — 기존 메커니즘 확장. 없으면 같은 파일에 SQLite 백엔드 추가
- corp_code 캐시: `dartlens/_corp_code.py` — universe 해석에 활용
- 테스트: `tests/test_scan_earnings_season.py` 신규
- 데코레이터 셋트: `@mcp.tool() + @safe_tool + @track_metrics("name")` (기존 도구 전부 동일 패턴)
- 반환 타입: 마크다운 `str` (기존과 동일)

**작업 시작 전 반드시 읽을 것:** `dartlens/server.py` 의 `list_disclosures`, `get_major_accounts`, `get_full_financial` 구현부와 `_http.py`, `_cache.py`, `_corp_code.py` 전체. 컨벤션·스타일·헬퍼 함수 재사용 가능 여부 파악 후 작업.

---

## 도구 시그니처

```python
@mcp.tool()
@safe_tool
@track_metrics("scan_earnings_season")
async def scan_earnings_season(
    period: str,                          # "2026Q1" | "2025Q3" | "2025H1" | "2024"
    universe: str = "kospi",              # "all" | "kospi" | "kosdaq" | "<corp_code>,<corp_code>..."
    sort_by: str = "op_yoy",              # rev_yoy | op_yoy | ni_yoy | op_margin | rev | op | ni
    direction: str = "desc",              # desc | asc
    top_n: int = 30,                      # 1~100
    fs_div: str = "CFS",                  # CFS=연결 | OFS=개별
) -> str:
    """분기/연간 실적 스캐닝. fnlttMultiAcnt 다중회사 API로 유니버스 전체의 당기·전년동기
    핵심계정을 일괄 조회한 뒤 YoY 계산, 정렬·필터해 top_n만 마크다운 테이블로 반환.

    Args:
        period: "YYYYQ1/Q2/Q3", "YYYYH1", "YYYY".
                Q1→reprt_code=11013, H1→11012, Q3→11014, 연간→11011.
                Q4는 별도(잠정실적은 v2 예정). Q4 요청 시 ValueError.
        universe: "all"(상장사 전체), "kospi", "kosdaq", 또는 corp_code 콤마 리스트.
        sort_by: *_yoy는 전년동기 대비 %, op_margin은 영업이익률, rev/op/ni는 절댓값.
        direction: desc/asc. 흑전·적전 케이스는 비고에 별도 표시.
        top_n: 1~100.
        fs_div: 연결재무제표(CFS) 기본. v1은 OFS 폴백 없음.

    Returns:
        마크다운 테이블 — 순위 | 회사(corp_code) | 매출 | 매출 YoY | 영업이익 | OP YoY | 순이익 | NI YoY | OP 마진 | 비고
    """
```

---

## 외부 API: DART `fnlttMultiAcnt.json`

```
GET https://opendart.fss.or.kr/api/fnlttMultiAcnt.json
  ?crtfc_key=...
  &corp_code=00126380,00164742,...   # 최대 100개, 콤마 구분
  &bsns_year=2026                    # YYYY
  &reprt_code=11013                  # 11011(사업)/11012(반기)/11013(1Q)/11014(3Q)
```

응답 핵심 필드 (회사 × 계정 행):
- `corp_code`, `corp_name`
- `account_nm`: "매출액" | "영업이익" | "당기순이익" | "자산총계" | ...
- `fs_div`: "CFS"(연결) | "OFS"(개별)
- `sj_div`: "BS"(재무상태표) | "IS"(손익) | "CIS"(포괄손익) | "CF"(현금흐름)
- `thstrm_amount`: 당기 금액 — **분기/반기 IS는 해당 3개월치** (문자열, 콤마 포함 가능)
- `thstrm_add_amount`: 당기 누적 (분기/반기 IS에만 존재)
- `frmtrm_amount`: 전기 동기(직전 동일 보고서) 금액
- `frmtrm_add_amount`: 전기 동기 누적
- `bfefrmtrm_amount`: 전전기

**3개월 vs 누적 (2026-08-16 수정):** `thstrm_amount`는 분기/반기 IS에서 **해당 3개월**이고 `thstrm_nm`은 보고서 종류명("제39기 반기")만 준다. 라벨을 그대로 믿으면 3개월치가 누적으로 읽힌다 — 디오 2026 반기 매출 449억(2분기)을 상반기로 오독한 고객 문의가 실제로 나왔다. Q1은 3개월=누적이라 두 값이 같다.

스캔은 `period`가 약속한 기간(2026H1=상반기)에 맞춰 **`thstrm_add_amount`(누적)를 우선 사용**한다. 3개월 값은 `_q_cur`/`_q_prev`에 보존하고, 어느 쪽을 썼는지는 `basis`("annual"/"cum"/"3m")로 표시. 누적 미제출 회사는 비고에 `⚠3개월값`.

**YoY 계산:** `frmtrm_amount`로 1차 YoY 가능. 단 일부 회사는 frmtrm 결측 — 그 경우 전년도 같은 `(bsns_year-1, reprt_code)`를 별도 chunk로 호출해 보완.

**에러:** `status` 필드 — "000"=정상, "013"=조회 결과 없음, "020"=키 불일치, "100"=필수값 누락 등. 기존 `_http.py`/`_safe.py` 패턴 따라 처리.

**스펙 확인:** 응답 필드명·값 케이스가 의심되면 https://opendart.fss.or.kr/guide/main.do 의 "다중회사 주요계정" 가이드 페이지에서 재확인.

---

## 내부 동작 (4단계)

1. **period 파싱** → `(bsns_year, reprt_code)`.
   - 정규식 `^(\d{4})(Q[1-3]|H1)?$` 매칭.
   - Q4 또는 H2 요청 시 ValueError("Q4/H2는 v2에서 잠정실적공시와 함께 지원").

2. **universe 해석** → `list[corp_code]`.
   - `_corp_code.py`의 corpCode.xml 캐시 활용.
   - "all" → `stock_code`가 비어있지 않은 회사 전체(상장사).
   - "kospi" / "kosdaq" → 시장 구분 메타데이터 필요. v1에서 corpCode.xml에 시장 구분이 없으면 두 옵션 모두 "all 상장사"로 폴백하고 결과 footer에 한 줄 경고. (v2에서 KRX 매핑 추가 예정)
   - corp_code 콤마 문자열 → 그대로 파싱.

3. **다중회사 조회** — 핵심 병렬 로직.
   - 100개씩 chunk → `fnlttMultiAcnt` 호출.
   - `asyncio.Semaphore(5)` + 분당 호출 ≤ 300 슬로틀 (DART rate limit 보호).
   - 당기 `(bsns_year, reprt_code)` + 전년동기 `(bsns_year-1, reprt_code)` 두 세트.
   - 한 chunk 실패해도 나머지 진행, 실패 corp 수는 결과 footer에 누적 표시.
   - 캐시 hit한 corp는 API 호출 스킵.

4. **계산·정렬·반환**
   - 각 corp에서 (매출액, 영업이익, 당기순이익) 추출. `fs_div=CFS` 우선.
   - YoY = (당기 - 전년) / |전년| × 100. 전년 0 또는 결측 → "N/A", 정렬 시 맨 뒤.
   - 흑전(전년≤0, 당기>0) / 적전(전년>0, 당기≤0) → 비고에 한글로.
   - sort_by · direction 적용 후 top_n.
   - 마크다운 테이블 반환.

---

## 캐시 설계

- 위치: `dartlens/.cache/earnings.sqlite` (기존 `_cache.py`가 다른 경로 쓰면 거기 맞춤)
- 키: `(corp_code, bsns_year, reprt_code, fs_div)`
- 값: 핵심 계정 dict + `fetched_at` ISO timestamp
- **정책:** 정기보고서 확정 데이터는 **영구 캐시** — 분기 마감 후엔 변경 없음. 같은 period 재호출 시 캐시 hit corp는 API 호출 스킵. 어닝 시즌에 매일 호출해도 신규 접수분만 증분 처리.
- `force_refresh` 인자는 v1 미포함. 디버깅 시 파일 삭제로 처리.

---

## 출력 포맷 예시

```markdown
# 분기 실적 스캐닝 — 2026Q1 (KOSPI, 연결 기준)

조회 회사: 821 / 데이터 보유: 780 / 정렬: op_yoy desc / Top 30

| 순위 | 회사 (corp_code) | 매출 | 매출 YoY | 영업이익 | OP YoY | 순이익 | NI YoY | OP 마진 | 비고 |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 삼성전자 (00126380) | 71.2조 | +18.4% | 6.6조 | +932% | 7.1조 | +812% | 9.3% | - |
| 2 | SK하이닉스 (00164742) | ... | ... | ... | ... | ... | ... | ... | - |
| 3 | XX바이오 (...) | 142억 | +52% | 8억 | N/A | 3억 | N/A | 5.6% | 흑전 |

_금액 단위: 조/억 자동 절사 (`_fmt_won` 재사용). YoY는 전년동기(2025Q1) 대비._
_데이터 결측 41건 · API 실패 0건 · 캐시 hit 740 / API fetch 81._
```

- 회사명 옆 corp_code 명시 → 사용자가 후속 도구 호출(예: `get_full_financial`)하기 쉽게.
- 헤더에 모집단/유효수/정렬 기준 / footer에 결측·실패·캐시 통계.

---

## 에러·한계 처리

- API 키 누락 → 기존 `_validate.py` 패턴 따라 친절 에러
- HTTP 429 또는 status="020" → exponential backoff 3회 재시도 후 fail
- chunk 부분 실패 → 나머지 진행, footer에 실패 건수
- corp_code 100개 초과 입력 → 자동 chunk
- universe 결과 > 2000개 → 경고 후 진행

---

## v1 스코프 (이 PR에 **포함하지 않을 것**)

- 잠정실적공시(주요사항보고서) 머지 → v2
- KOSPI200 · 섹터별 universe → v2 (v1은 all/kospi/kosdaq/corp_code 리스트만)
- 컨센서스 대비 서프라이즈 → stocklens 영역, 별도 통합 작업
- ~~Q2/Q3 누적이 아닌 분기별 환산 → v2~~ (2026-08-16 해결: `thstrm_add_amount`가 누적, `thstrm_amount`가 3개월. 스캔은 누적, 3개월은 `_q_cur`에 보존)
- Q4 / H2 → v2 (잠정실적과 함께)
- 시총 필터(`min_market_cap`) → 시총 데이터 source 별도, v2
- OFS 폴백 → v2

---

## 테스트 (`tests/test_scan_earnings_season.py`)

pytest + pytest-asyncio. 기존 `tests/test_validate.py`, `tests/test_order_backlog.py` 스타일 참고.

1. period 파싱: "2026Q1"→(2026, 11013) / "2025H1"→(2025, 11012) / "2024"→(2024, 11011) / "2026Q4"→ValueError
2. universe 파싱: "all", "kospi", "00126380,00164742" 각각 corp_code 리스트
3. fnlttMultiAcnt 응답 mock → YoY 계산 정확도 (흑전·적전·결측·전년 0 케이스 포함)
4. 캐시 hit/miss: 1차 호출 → 2차 호출 시 API 호출 0건 검증
5. 정렬·top_n: sort_by 각 모드별 결과 순서 검증
6. 마크다운 출력 스냅샷 (smoke 수준)

---

## DoD

- [ ] `scan_earnings_season` 도구가 `@mcp.tool()`로 등록되고 MCP Inspector에서 호출 가능
- [ ] KOSPI 전체(~830개) 1차 스캔이 ≤ 60초, API 호출 ≤ 20회 (당기+전년 합쳐)
- [ ] 같은 period 2차 호출 ≤ 5초, API 호출 ≤ 2회 (신규 접수분만)
- [ ] 결과 토큰 ≤ 8K (top_n=30 기준)
- [ ] 테스트 6케이스 전부 통과
- [ ] `dartlens/__init__.py` export 필요 시 추가
- [ ] PyPI 배포는 별도 작업 — 이 PR은 코드 + 테스트만

---

## 재사용 가능한 기존 코드

- `_http.py`의 DART API 호출 헬퍼 — fnlttMultiAcnt용 함수 추가
- `_corp_code.py`의 corpCode.xml 캐시 — universe 해석
- `_safe.py`의 `@safe_tool`
- `_metrics.py`의 `@track_metrics`
- `_validate.py`의 `normalize_yyyymmdd`, `normalize_corp_code`
- `server.py`의 `_fmt_won`, `_fmt_amount`, `_fmt_pct`

---

## 작업 흐름 권장

1. 기존 코드 파악 (server.py / _http.py / _cache.py / _corp_code.py)
2. fnlttMultiAcnt 호출 함수를 `_http.py`에 추가 + 단위 테스트
3. 캐시 레이어 추가 (`_cache.py` 확장)
4. `scan_earnings_season` 본체 구현
5. 출력 포매팅 + 테스트 작성
6. 실제 API 키로 KOSPI 한 번 풀스캔 → 타이밍·결과 검증
7. 캐시 작동 확인 (2차 호출 비교)
