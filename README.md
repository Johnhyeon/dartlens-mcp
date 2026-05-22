<div align="center">

# dartlens

**전자공시(DART)를 Claude가 진짜 데이터로 읽습니다**

[![PyPI](https://img.shields.io/pypi/v/dartlens-mcp.svg)](https://pypi.org/project/dartlens-mcp/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

</div>

---

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
- 💸 **무료 DART OpenAPI** — 분당 1,000건 / 일 20,000건
- 🧠 **토큰 다이어트** — 단위 압축 + 보고서 인덱스 + 키워드 매치 (`find=...`)로 긴 사업보고서도 가벼움
- 🩺 **`dartlens-doctor`** — 막혔을 때 원인·해결 명령까지 자동 진단

## 빠른 시작 (Python 사전 설치 불필요)

[`uv`](https://docs.astral.sh/uv/)가 Python 런타임까지 자동으로 설치합니다. 터미널에 한 줄 복붙.

### Windows (PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/Johnhyeon/dartlens-mcp/main/install.ps1 | iex"
```

### macOS / Linux (터미널)

```bash
curl -LsSf https://raw.githubusercontent.com/Johnhyeon/dartlens-mcp/main/install.sh | sh
```

스크립트가 ① uv 설치 → ② `uv tool install dartlens-mcp` → ③ DART API 키 입력·검증 → ④ MCP 클라이언트 자동 등록까지 처리합니다. **타겟 자동 감지**: `claude` CLI가 PATH에 있으면 Claude Code, Claude Desktop config 디렉토리가 있으면 Desktop, 둘 다면 둘 다 등록. 끝나면 Claude Desktop은 완전히 종료(트레이→Quit) 후 재시작, Claude Code는 새 세션에서 자동 적용.

API 키가 없다면 먼저 [DART OpenAPI 발급](https://opendart.fss.or.kr/uss/umt/EgovMberInsertView.do) (무료, 분당 1,000건 / 일 20,000건).

> 💡 **키를 미리 넣고 무대화 설치**
> - PowerShell: `$env:DART_API_KEY="..."; powershell -c "irm https://raw.githubusercontent.com/Johnhyeon/dartlens-mcp/main/install.ps1 | iex"`
> - bash: `curl -LsSf https://raw.githubusercontent.com/Johnhyeon/dartlens-mcp/main/install.sh | DART_API_KEY=... sh`
>
> 💡 **타겟 직접 지정** (자동감지 무시): `DARTLENS_TARGET=claude-code` (또는 `claude-desktop` / `both`) 를 추가로 export.

### 업데이트

```bash
uv tool upgrade dartlens-mcp
```

또는 위 install 명령을 다시 실행하면 됩니다.

---

### 수동 설치 (pip)

uv 없이 기존 환경에 설치하려면:

```bash
pip install dartlens-mcp

dartlens-setup <DART_API_KEY>          # 인자로 직접
dartlens-setup                         # 또는 대화형
DART_API_KEY=... dartlens-setup        # 또는 env로
```

`dartlens-setup`은:

1. DART API 키를 받아 **유효성 검증** (삼성전자 기업개황 1회 호출)
2. 키를 **OS 키체인**에 저장 (Windows DPAPI / macOS Keychain / Linux Secret Service)
3. Claude Desktop의 `claude_desktop_config.json`에 `mcpServers.dartlens` 엔트리 등록 (키는 JSON에 박지 않음)

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
| `get_major_accounts` | 정기보고서 핵심 재무 (매출/영업이익/순이익/자산/부채/자본 — 당기·전기·전전기 비교) |
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

## 기여

이슈, PR 모두 환영합니다. 버그 제보나 기능 요청은 [Issues](https://github.com/Johnhyeon/dartlens-mcp/issues)에 남겨주세요.

## 라이선스

MIT
