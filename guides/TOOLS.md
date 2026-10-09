# DartLens 도구 안내

DartLens 도구와 응답에 붙는 결과 메타 규약입니다. 제품 소개는 [README](../README.md)를 보세요.

## 도구

| 도구 | 목적 |
|---|---|
| `search_company` | 종목명/종목코드 → corp_code + 기업개황. 다른 도구의 `corp_code` 에는 회사명·6자리 종목코드를 그대로 입력해도 된다(여러 회사면 후보 표시) |
| `list_disclosures` | 기간·유형별 공시 목록 (rcept_no 반환) |
| `get_disclosure_detail` | 짧은 공시는 본문 발췌, 긴 보고서는 인덱스 + viewer URL. `find="키워드"`로 본문 검색 |
| `get_major_accounts` | 정기보고서 핵심 재무 (매출/영업이익/순이익/자산/부채/자본). 분기·반기 손익은 3개월/누적 컬럼 분리 |
| `get_full_financial` | 전체 재무제표. sj_div(BS/IS/CIS/CF/SCE) 필수 |
| `get_order_backlog` | 사업/분기/반기보고서 표에서 수주잔고·계약잔액 추이를 구조화 |
| `get_major_holders` | 5%룰 대량보유 변동 — 외인/펀드/행동주의 진입 추적 |
| `get_insider_trades` | 임원·주요주주 특정증권 소유 — 내부자 매매 시그널 |
| `get_dividend_history` | 사업보고서 배당 사항 연도별 이어 붙이기 + 현금ㆍ현물배당결정 공시(분기·결산·감액배당) |
| `scan_earnings_season` | 어닝 시즌 전체/시장별 실적 스캔 — 채팅용 Top N Markdown |
| `export_earnings_scan` | 실적 스캔 결과를 `.xlsx`/`.csv` 파일로 저장. 한국 Excel은 `.xlsx` 권장 |

### 권장 워크플로우

```
# 공시 흐름
search_company("삼성전자") → corp_code "00126380"
list_disclosures(corp_code="00126380", days=30) → rcept_no 목록
get_disclosure_detail(rcept_no="...") → 짧은 공시는 본문, 긴 보고서는 인덱스
get_disclosure_detail(rcept_no="...", find="신사업") → 긴 보고서에서 키워드 매치 ±300자

# 재무 흐름
search_company("삼성전자") → corp_code
get_major_accounts(corp_code, bsns_year=2024, reprt_code="annual") → 핵심 수치
get_full_financial(corp_code, bsns_year=2024, reprt_code="annual",
                   fs_div="CFS", sj_div="IS") → 손익 전체
get_order_backlog(corp_code, years=3) → 수주잔고/계약잔액 추이

# 어닝 시즌 전체 스캔
scan_earnings_season(period="2026Q1", universe="kospi", top_n=30) → 채팅용 요약 표
export_earnings_scan(period="2026Q1", universe="all",
                     output_format="xlsx", max_rows=1000) → 엑셀 파일 + 행/열 검증
export_earnings_scan(period="2026Q1", universe="all",
                     output_format="both", amount_unit="eok") → XLSX + CSV 동시 생성

# 지분 흐름 (시세에 안 나오는 자본 움직임)
search_company("삼성전자") → corp_code
get_major_holders(corp_code, limit=10) → 5%룰 보고서 (외인/펀드/행동주의)
get_insider_trades(corp_code, limit=10) → 임원·주요주주 자사주 매매
```

---

## 결과 메타 (`RESULT_META_JSON`) - 규약 v3

각 도구 응답 끝에 `RESULT_META_JSON_START…END` 블록이 붙습니다. `meta_v`가 규약
버전이고 현재 **3**입니다. StockLens·TelegramLens와 같은 파일(`_result_meta.py`)을
씁니다.

**v3에서 늘어난 필드는 전부 선택적입니다.** 해당 개념이 없는 도구에는 키 자체가
생기지 않고, v2만 아는 소비자는 무시해도 됩니다. 기존 키의 의미는 바뀌지 않았습니다.

### `data_completeness` 는 요청한 범위 기준

`complete`는 "요청한 범위를 다 채웠다"는 뜻입니다. 2,894건 중 20건이 왔으면 그 20건이
온전해도 `partial`입니다. `coverage.coverage_complete=false` 인데
`data_completeness=complete` 인 조합은 값 검증에서 막힙니다.

### `coverage` - 목록이 잘렸는가 (`list_disclosures`)

```json
"coverage": {
  "requested": {
    "bgn_de": "20250826",
    "end_de": "20260826",
    "kind": "all",
    "limit": 20
  },
  "returned_count": 20,
  "total_count": 2894,
  "truncated": true,
  "coverage_complete": false,
  "reason": "pagination"
}
```

`requested` 에는 조회 조건이 통째로 들어갑니다. `limit` 만 남기면 메타만 받은
소비자가 어느 기간의 어떤 유형을 본 것인지 복원할 수 없고, 공시 유형은 본문에도
코드가 아니라 라벨로만 나옵니다.

전체가 다 왔으면 `truncated=false`·`coverage_complete=true`·`complete`입니다.
원천이 `total_count`를 주지 않으면 다 봤는지 알 수 없으므로 `total_count=null`,
`reason=unknown`, `partial`로 둡니다. 0건은 원천이 "없다"고 말해준 것이라
`coverage_complete=true` + `none`입니다.

### `match_coverage` - 본문 검색이 몇 건 중 몇 건인가 (`get_disclosure_detail`)

```json
"match_coverage": {
  "keyword": "배당",
  "total_matches": 165,
  "displayed_matches": 5,
  "displayed_excerpts": 3,
  "truncated": true,
  "coverage_complete": false
}
```

발췌는 최대 5개입니다. 가까이 붙은 매치(앞뒤 300자 범위가 겹치거나 맞닿는 매치)는 한 발췌로
묶고, 발췌마다 매치 수를 함께 표시합니다. `displayed_matches`는 표시된 발췌에 들어 있는 매치 수,
`displayed_excerpts`는 발췌 개수입니다. 전체 건수는 따로 집계해 싣습니다. 0건이면
`absence_confirmed=false`가 함께 붙습니다 - 표 안 텍스트나 다른 표기(현금배당 등)면
실제로 있어도 0건이 나오므로 부정 결론을 내리면 안 됩니다.

### `financial_scope` - 연결과 별도가 섞였는가

```json
"financial_scope": {
  "scopes_present": ["CFS", "OFS"],
  "preferred_scope": "CFS",
  "scope_mixed_in_response": true,
  "currency": "KRW"
}
```

`get_major_accounts`가 쓰는 `fnlttSinglAcnt.json`은 연결과 별도를 **한 응답에 같이**
줍니다. 두 범위를 합산하면 존재하지 않는 회사의 숫자가 됩니다. 혼재 시 본문에도
경고가 붙습니다. `get_full_financial`은 `fs_div` 하나만 가져오므로 섞이지 않습니다.

### `filing_state` - 이 숫자가 정정 반영본인가

```json
"filing_state": {
  "business_year": "2025",
  "report_code": "11011",
  "filing_date": "2026-03-10",
  "correction_checked": true,
  "correction_applied": false,
  "latest_correction_date": null
}
```

정정 조회가 **실패한 것**과 정정이 **없는 것**은 다릅니다. 실패하면
`correction_checked=false`와 경고를 남기고, 수치와 표는 그대로 돌려줍니다.
