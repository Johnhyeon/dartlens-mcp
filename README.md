<div align="center">

# dartlens

**전자공시(DART)를 Claude가 진짜 데이터로 읽습니다**

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

</div>

---

## 배포 상태

DartLens의 공개 설치 안내는 2026-06-01 기준으로 종료했습니다.

현재 신규 설치는 구매자 안내문을 통해 제공되는 설치 명령어와 가이드를 기준으로 진행합니다. 이미 설치한 기존 사용자는 보유한 공개 버전을 계속 사용할 수 있지만, 신규 배포·설치 지원·활용 템플릿은 구매자 패키지 기준으로 정리합니다.

## 왜 필요한가

AI에게 공시 PDF를 보여주면 **숫자를 추측해서 틀린 답**을 합니다 — 매출, 영업이익, 지분율, 보고일자가 죄다 환각.

**dartlens**는 Claude에 금융감독원 [DART OpenAPI](https://opendart.fss.or.kr)를 직접 연결해서, AI가 추측이 아닌 **공시 원문·정형 재무제표**를 읽고 분석하도록 만듭니다.

```
❌ "삼성전자 작년 영업이익 35조쯤이었던 것 같아요" (추측, 틀림)
✅ "삼성전자 2024 사업보고서 영업이익 32.7조원, 전년 6.6조 대비 +395%" (DART 원본)
```

> "삼성전자 최근 분기보고서 요약해줘" · "카카오 최근 1개월 공시 목록" · "LG에너지솔루션 영업이익 추이" · "삼성전자 5% 이상 보유한 주주 변동" · "현대차 임원들 자사주 매매 보여줘"

자매 프로젝트 [stocklens-mcp](https://github.com/Johnhyeon/stocklens-mcp)(네이버 증권 기반 시세·차트·수급)와 **독립**입니다. Claude가 두 MCP를 조합해 종목 분석을 수행합니다.

## 주요 기능

- 📑 **7개 도구** — 기업 검색, 공시 목록·본문, 재무제표(요약·전체), 5%룰, 임원·주요주주 매매
- 🔐 **API 키는 OS 키체인** (Windows DPAPI / macOS Keychain / Linux Secret Service) — config 평문 저장 X
- 💸 **DART OpenAPI** — 분당 1,000건 / 일 20,000건
- 🧠 **토큰 다이어트** — 단위 압축 + 보고서 인덱스 + 키워드 매치 (`find=...`)로 긴 사업보고서도 가벼움
- 🩺 **`dartlens-doctor`** — 막혔을 때 원인·해결 명령까지 자동 진단

## 설치 안내

구매자에게 제공되는 안내문에는 다음 과정이 포함됩니다.

1. `uv` 확인 및 설치
2. DartLens MCP 설치
3. DART API 키 입력 및 검증
4. Claude Desktop 또는 Claude Code MCP 설정 자동 등록
5. 설치 진단과 첫 실행 확인

공개 README에는 더 이상 직접 설치 명령어를 게시하지 않습니다.

## 동작 확인

Claude에서:
```
삼성전자 최근 공시 보여줘
```

기업명, 공시 목록, 보고일자가 나오면 설치 완료입니다.

## 설치 문제 진단

```bash
dartlens-doctor
```

uv·패키지·명령·config·API 키 5단계 자동 점검. 문제 원인과 고치는 명령어까지 표시. 친구분이 막혔을 때 이 한 줄만 보내주세요.

---

## 키 보관 정책

`dartlens-setup`은 **DART API 키를 `claude_desktop_config.json`에 평문으로 박지 않습니다.**

- 기본: 키를 OS 키체인에 저장
  - Windows → Credential Manager (DPAPI · 사용자 계정 단위 자동 암호화)
  - macOS → Keychain
  - Linux → Secret Service (GNOME Keyring / KDE Wallet)
- config 파일에는 `mcpServers.dartlens.command` 만 들어가고 키는 들어가지 않음
- 서버는 부팅 시 `DART_API_KEY` 환경변수를 먼저 보고, 없으면 키체인에서 자동 조회

JSON config에 평문 `DART_API_KEY`가 박혀있는 환경에서 `dartlens-setup`을 다시 실행하면 자동으로 키체인으로 이전되고 JSON에서 제거됩니다.

### 평문 모드 (헤드리스 환경 fallback)

OS 키체인을 쓸 수 없는 환경(서버, 일부 WSL/Docker)에서는 `--plaintext`로 명시적 옵트아웃:

```bash
dartlens-setup --plaintext <KEY>
```

이 경우 기존처럼 `env.DART_API_KEY`가 JSON에 평문 저장됩니다.

---

## 도구

| 도구 | 목적 |
|---|---|
| `search_company` | 종목명/종목코드 → corp_code + 기업개황 |
| `list_disclosures` | 기간·유형별 공시 목록 (rcept_no 반환) |
| `get_disclosure_detail` | 짧은 공시는 본문 발췌, 긴 보고서는 인덱스 + viewer URL. `find="키워드"`로 본문 검색 |
| `get_major_accounts` | 정기보고서 핵심 재무 (매출/영업이익/순이익/자산/부채/자본). 분기·반기 손익은 3개월/누적 컬럼 분리 |
| `get_full_financial` | 전체 재무제표. sj_div(BS/IS/CIS/CF/SCE) 필수 |
| `get_order_backlog` | 사업/분기/반기보고서 표에서 수주잔고·계약잔액 추이를 구조화 |
| `get_major_holders` | 5%룰 대량보유 변동 — 외인/펀드/행동주의 진입 추적 |
| `get_insider_trades` | 임원·주요주주 특정증권 소유 — 내부자 매매 시그널 |
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

# 터미널에서 바로 엑셀 파일 생성
uvx --from dartlens-mcp dartlens-export-earnings --period 2026Q1 --universe all --sort-by op_yoy --max-rows 3000 --format xlsx
# PowerShell에서 corp_code 콤마 리스트를 직접 넘길 때는 따옴표 사용
uvx --from dartlens-mcp dartlens-export-earnings --period 2026Q1 --universe "00126380,00631518" --format both

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
  "truncated": true,
  "coverage_complete": false
}
```

매치는 최대 5건까지 발췌합니다. 전체 건수는 따로 세어 싣습니다. 0건이면
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

## 지원 환경

| 환경 | 지원 |
|------|------|
| Claude Desktop (앱, Win/macOS) | ✅ |
| Claude Code (CLI, Win/macOS/Linux/RasPi) | ✅ |
| Claude.ai (웹) | ❌ 로컬 MCP 미지원 |

`dartlens-setup --target {claude-desktop, claude-code, both}` 로 명시적으로 선택 가능. 기본 `auto`는 환경 감지.

## 원칙

- **DART OpenAPI만** 사용합니다. 네이버·다음 등 스크래핑 일절 없음.
- 시세·차트·수급은 stocklens가, **공시·재무제표 정형 데이터**는 dartlens가 담당.
- 두 서버는 서로 호출하지 않습니다. **Claude가 조정자**입니다.
- 투자 추천·매매 시그널을 만들지 않습니다. 데이터 제공만.

## 운영 원칙

DartLens는 투자 추천·매수/매도 신호·자동매매 기능을 제공하지 않습니다. DART 공시와 정형 재무 데이터를 Claude가 읽을 수 있는 형태로 연결하는 데이터 도구입니다.

## 라이선스

MIT
