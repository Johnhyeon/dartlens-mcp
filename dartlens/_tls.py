"""TLS 신뢰 기준을 OS 인증서 저장소에 맞춘다 — 프로세스당 한 번.

파이썬 HTTP 클라이언트는 기본적으로 `certifi` 번들만 믿고 OS 인증서 저장소를 보지
않는다. 그래서 백신이나 회사망 장비가 TLS를 가로채 자기 루트로 재서명하는 PC에서는
브라우저는 멀쩡한데 우리만 이렇게 죽는다:

    ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer
    certificate

2026-08-13 문의에서 실제로 이 원인으로 조회가 100% 실패했다.

`_http.py` 의 클라이언트는 `verify=` 로 직접 지정하지만, 그 클라이언트를 안 타는
호출이 남아 있다 — 자가진단(`diagnostics.py`), KRX 상장목록(`_market.py`),
폐기 목록·업데이트 확인(`licensing.py`, `_update_check.py`). 여기서 `ssl` 모듈을
통째로 바꿔두면 그 전부가 같은 기준을 쓴다.

**자가진단이 특히 중요하다.** 진단은 통과하는데 서버는 실패하면, 사용자도 우리도
원인을 못 찾는다. 실제로 그렇게 반나절을 썼다.

StockLens 에는 `curl_cffi`(yfinance)를 위한 처리가 하나 더 있다. DartLens 는
libcurl 계열을 쓰지 않으므로 여기서는 필요 없다.
"""

from __future__ import annotations

_applied = False


def apply() -> None:
    """프로세스당 한 번 적용. 어떤 경우에도 예외를 올리지 않는다 — 신뢰 범위를
    넓히려다 서버가 안 뜨면 본말전도다. 실패하면 조용히 기존 동작(certifi)으로 남는다.
    """
    global _applied
    if _applied:
        return
    _applied = True
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass
