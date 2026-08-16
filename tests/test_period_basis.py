"""분기/반기 손익의 3개월 vs 누적 구분 회귀 테스트.

배경 (고객 문의, 2026-08-16):
디오(039840) 2026 반기보고서에서 get_major_accounts가 매출 449억을
"제 39 기 반기" 라벨로 반환했다. 449억은 **2분기 3개월** 금액이고 상반기
누적은 862.7억(반기보고서 원문 매출실적표 86,272백만원)이다. DART는
thstrm_amount에 해당 3개월, thstrm_add_amount에 당해 누적을 함께 주는데
누적을 버리고 보고서 종류명을 라벨로 붙여 3개월 값이 누적치로 읽혔다.

여기 숫자는 전부 실제 DART 응답에서 가져온 것이라 회귀 시 원문과 대조 가능.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from dartlens._earnings import (
    _has_current_schema,
    basis_note,
    compute_row,
    extract_accounts,
)
from dartlens.server import (
    _dedup_account_rows,
    _format_full_financial,
    _format_major_accounts,
    _period_columns,
)

# --- 디오 2026 반기보고서 연결, fnlttSinglAcnt 실제 응답 발췌 --------------

DIO_H1_BS = [
    {
        "fs_div": "CFS", "sj_div": "BS", "sj_nm": "재무상태표", "ord": "3",
        "account_nm": "자산총계",
        "thstrm_nm": "제 39 기반기말", "thstrm_dt": "2026.06.30 현재",
        "thstrm_amount": "327,432,470,288",
        "frmtrm_nm": "제 38 기말", "frmtrm_dt": "2025.12.31 현재",
        "frmtrm_amount": "328,467,730,338",
        "currency": "KRW",
    },
]

DIO_H1_IS = [
    {
        "fs_div": "CFS", "sj_div": "IS", "sj_nm": "손익계산서", "ord": "23",
        "account_nm": "매출액",
        "thstrm_nm": "제 39 기반기", "thstrm_dt": "2026.01.01 ~ 2026.06.30",
        "thstrm_amount": "44,949,617,767",
        "thstrm_add_amount": "86,272,175,136",
        "frmtrm_nm": "제 38 기반기", "frmtrm_dt": "2025.01.01 ~ 2025.06.30",
        "frmtrm_amount": "40,099,205,051",
        "frmtrm_add_amount": "75,904,529,147",
        "currency": "KRW",
    },
    {
        "fs_div": "CFS", "sj_div": "IS", "sj_nm": "손익계산서", "ord": "25",
        "account_nm": "영업이익",
        "thstrm_nm": "제 39 기반기",
        "thstrm_amount": "4,407,797,260",
        "thstrm_add_amount": "8,543,994,337",
        "frmtrm_nm": "제 38 기반기",
        "frmtrm_amount": "3,020,388,364",
        "frmtrm_add_amount": "4,538,485,157",
        "currency": "KRW",
    },
]


def _major(items, reprt_code):
    return _format_major_accounts(
        {"list": items}, corp_code="00115931", bsns_year="2026", reprt_code=reprt_code
    )


# ---------------------------------------------------------------------------
# 1. 반기 손익 — 3개월/누적 분리
# ---------------------------------------------------------------------------


class HalfYearIncomeColumnsTests(unittest.TestCase):
    def test_cumulative_column_present_and_correct(self):
        out = _major(DIO_H1_IS, "11012")
        self.assertIn("| 2분기(3개월) | 상반기 누적 | 전년 2분기 | 전년 상반기 |", out)
        # 449억(2분기) / 862억(상반기 누적) — 원문 매출실적표 86,272백만원과 일치
        self.assertIn("| 매출액 | 449억 | 862억 | 400억 | 759억 |", out)
        self.assertIn("| 영업이익 | 44억 | 85억 | 30억 | 45억 |", out)

    def test_report_name_never_labels_three_month_value(self):
        """'제 39 기반기'가 3개월 금액의 컬럼 헤더로 쓰이면 안 된다 (문의 원인)."""
        out = _major(DIO_H1_IS, "11012")
        income_table = out.split("### 손익계산서")[1]
        self.assertNotIn("제 39 기반기", income_table)

    def test_no_ambiguity_warning_when_cumulative_exists(self):
        self.assertNotIn("3개월/누적 구분이 불가", _major(DIO_H1_IS, "11012"))


# ---------------------------------------------------------------------------
# 2. 재무상태표는 자기 기간 라벨을 쓴다 (손익 라벨에 오염 금지)
# ---------------------------------------------------------------------------


class BalanceSheetLabelTests(unittest.TestCase):
    def test_bs_keeps_own_prev_label(self):
        """반기보고서 BS의 전기는 '제38기말'(전년 12/31)이지 '제38기반기'가 아니다.

        예전에는 라벨을 보고서 전체에서 하나로 뽑아(마지막 행 기준) BS 표에도
        손익의 '제 38 기반기'가 붙었다. 3,284억은 2025-12-31 값인데 2025년
        반기말로 읽히던 버그.
        """
        out = _major(DIO_H1_BS + DIO_H1_IS, "11012")
        bs_table = out.split("### 재무상태표")[1].split("###")[0]
        self.assertIn("| 계정 | 제 39 기반기말 | 제 38 기말 |", bs_table)
        self.assertNotIn("제 38 기반기", bs_table)

    def test_bs_and_is_headers_differ(self):
        out = _major(DIO_H1_BS + DIO_H1_IS, "11012")
        self.assertIn("| 계정 | 제 39 기반기말 | 제 38 기말 |", out)
        self.assertIn("| 계정 | 2분기(3개월) | 상반기 누적 | 전년 2분기 | 전년 상반기 |", out)


# ---------------------------------------------------------------------------
# 3. 1분기 / 사업보고서 — 기존 동작 유지
# ---------------------------------------------------------------------------


class OtherReportTypeTests(unittest.TestCase):
    def test_q1_collapses_because_three_month_equals_cumulative(self):
        """1분기는 3개월 = 누적. 같은 숫자를 두 컬럼에 낼 이유가 없다."""
        rows = [
            {
                "fs_div": "CFS", "sj_div": "IS", "sj_nm": "손익계산서", "ord": "23",
                "account_nm": "매출액",
                "thstrm_nm": "제 39 기1분기",
                "thstrm_amount": "41,322,557,369",
                "thstrm_add_amount": "41,322,557,369",
                "frmtrm_nm": "제 38 기1분기",
                "frmtrm_amount": "35,805,324,096",
                "frmtrm_add_amount": "35,805,324,096",
            },
        ]
        out = _major(rows, "11013")
        self.assertIn("| 계정 | 제 39 기1분기 | 제 38 기1분기 |", out)
        self.assertIn("| 매출액 | 413억 | 358억 |", out)
        self.assertNotIn("누적", out)

    def test_annual_keeps_three_year_comparison(self):
        rows = [
            {
                "fs_div": "CFS", "sj_div": "IS", "sj_nm": "손익계산서", "ord": "23",
                "account_nm": "매출액",
                "thstrm_nm": "제 38 기", "thstrm_amount": "164,077,830,292",
                "frmtrm_nm": "제 37 기", "frmtrm_amount": "119,649,873,058",
                "bfefrmtrm_nm": "제 36 기", "bfefrmtrm_amount": "155,817,892,643",
            },
        ]
        out = _format_major_accounts(
            {"list": rows}, corp_code="00115931", bsns_year="2025", reprt_code="11011"
        )
        self.assertIn("| 계정 | 제 38 기 | 제 37 기 | 제 36 기 |", out)
        self.assertIn("| 매출액 | 1,640억 | 1,196억 | 1,558억 |", out)


# ---------------------------------------------------------------------------
# 4. 누적 컬럼 자체가 없는 반기 → 단정 금지 경고
# ---------------------------------------------------------------------------


class AmbiguousBasisTests(unittest.TestCase):
    def test_half_report_without_cumulative_warns(self):
        rows = [
            {
                "fs_div": "CFS", "sj_div": "IS", "sj_nm": "손익계산서", "ord": "23",
                "account_nm": "매출액",
                "thstrm_nm": "제 39 기반기", "thstrm_amount": "44,949,617,767",
                "frmtrm_nm": "제 38 기반기", "frmtrm_amount": "40,099,205,051",
            },
        ]
        self.assertIn("3개월/누적 구분이 불가", _major(rows, "11012"))

    def test_balance_sheet_never_warns(self):
        """BS는 시점값이라 누적 개념이 없다. 경고가 붙으면 노이즈."""
        self.assertNotIn("3개월/누적 구분이 불가", _major(DIO_H1_BS, "11012"))

    def test_annual_never_warns(self):
        rows = [
            {
                "fs_div": "CFS", "sj_div": "IS", "sj_nm": "손익계산서", "ord": "23",
                "account_nm": "매출액",
                "thstrm_nm": "제 38 기", "thstrm_amount": "164,077,830,292",
                "frmtrm_nm": "제 37 기", "frmtrm_amount": "119,649,873,058",
            },
        ]
        out = _format_major_accounts(
            {"list": rows}, corp_code="00115931", bsns_year="2025", reprt_code="11011"
        )
        self.assertNotIn("3개월/누적 구분이 불가", out)


# ---------------------------------------------------------------------------
# 5. get_full_financial — frmtrm_q_amount 폴백
# ---------------------------------------------------------------------------


class FullFinancialPrevColumnTests(unittest.TestCase):
    # fnlttSinglAcntAll은 분기/반기 손익에 frmtrm_amount를 안 준다.
    # frmtrm_q_amount(전년 3개월) + frmtrm_add_amount(전년 누적)로 내려온다.
    ALL_CIS = [
        {
            "sj_div": "CIS", "sj_nm": "포괄손익계산서", "ord": "6",
            "account_nm": "영업이익",
            "thstrm_nm": "제 39 기 반기",
            "thstrm_amount": "4407797260",
            "thstrm_add_amount": "8543994337",
            "frmtrm_q_nm": "제 38 기 반기",
            "frmtrm_q_amount": "3020388364",
            "frmtrm_add_amount": "4538485157",
            "currency": "KRW",
        },
    ]

    def _fmt(self, items, reprt_code, sj_div):
        return _format_full_financial(
            {"list": items},
            corp_code="00115931",
            bsns_year="2026",
            reprt_code=reprt_code,
            fs_div="CFS",
            sj_div=sj_div,
        )

    def test_prev_column_no_longer_empty(self):
        """예전엔 frmtrm_amount만 읽어서 전기 컬럼이 통째로 '-'였다."""
        out = self._fmt(self.ALL_CIS, "11012", "CIS")
        self.assertIn("| 영업이익 | 44억 | 85억 | 30억 | 45억 |", out)

    def test_prev_label_falls_back_to_q_name(self):
        rows = [dict(self.ALL_CIS[0])]
        del rows[0]["thstrm_add_amount"]
        del rows[0]["frmtrm_add_amount"]
        out = self._fmt(rows, "11012", "CIS")
        self.assertIn("| 계정 | 제 39 기 반기 | 제 38 기 반기 |", out)
        self.assertIn("| 영업이익 | 44억 | 30억 |", out)

    def test_balance_sheet_unaffected(self):
        rows = [
            {
                "sj_div": "BS", "sj_nm": "재무상태표", "ord": "7",
                "account_nm": "자산총계",
                "thstrm_nm": "제 39 기 반기말", "thstrm_amount": "327432470288",
                "frmtrm_nm": "제 38 기말", "frmtrm_amount": "328467730338",
                "currency": "KRW",
            },
        ]
        out = self._fmt(rows, "11012", "BS")
        self.assertIn("| 계정 | 제 39 기 반기말 | 제 38 기말 |", out)
        self.assertIn("| 자산총계 | 3,274억 | 3,284억 |", out)


# ---------------------------------------------------------------------------
# 6. _period_columns 단위
# ---------------------------------------------------------------------------


class PeriodColumnsUnitTests(unittest.TestCase):
    def test_missing_values_render_as_dash_not_crash(self):
        rows = [
            {
                "fs_div": "CFS", "sj_div": "IS", "sj_nm": "손익계산서", "ord": "1",
                "account_nm": "매출액",
                "thstrm_nm": "제 39 기반기",
                "thstrm_amount": "44,949,617,767",
                "thstrm_add_amount": "86,272,175,136",
                "frmtrm_amount": "-",
                "frmtrm_add_amount": "",
            },
        ]
        self.assertIn("| 매출액 | 449억 | 862억 | - | - |", _major(rows, "11012"))

    def test_q3_labels(self):
        rows = [
            {
                "fs_div": "CFS", "sj_div": "IS", "sj_nm": "손익계산서", "ord": "1",
                "account_nm": "매출액",
                "thstrm_nm": "제 38 기3분기",
                "thstrm_amount": "41,448,782,601",
                "thstrm_add_amount": "117,353,311,748",
                "frmtrm_amount": "31,345,869,246",
                "frmtrm_add_amount": "81,848,929,518",
            },
        ]
        out = _format_major_accounts(
            {"list": rows}, corp_code="00115931", bsns_year="2025", reprt_code="11014"
        )
        self.assertIn("| 계정 | 3분기(3개월) | 3분기 누적 | 전년 3분기 | 전년 3분기 누적 |", out)
        self.assertIn("| 매출액 | 414억 | 1,173억 | 313억 | 818억 |", out)

    def test_columns_are_per_table_not_per_report(self):
        cols_bs, _ = _period_columns(DIO_H1_BS, "11012")
        cols_is, _ = _period_columns(DIO_H1_IS, "11012")
        self.assertEqual([h for h, _ in cols_bs], ["제 39 기반기말", "제 38 기말"])
        self.assertEqual(
            [h for h, _ in cols_is],
            ["2분기(3개월)", "상반기 누적", "전년 2분기", "전년 상반기"],
        )


# ---------------------------------------------------------------------------
# 7. dedup — 누적 금액이 다르면 별개 행
# ---------------------------------------------------------------------------


class DedupTests(unittest.TestCase):
    def test_identical_rows_deduped(self):
        rows = [dict(DIO_H1_IS[0], ord="29"), dict(DIO_H1_IS[0], ord="61")]
        self.assertEqual(len(_dedup_account_rows(rows)), 1)

    def test_differing_cumulative_preserved(self):
        rows = [
            dict(DIO_H1_IS[0], ord="29"),
            dict(DIO_H1_IS[0], ord="61", thstrm_add_amount="86,272,175,999"),
        ]
        self.assertEqual(len(_dedup_account_rows(rows)), 2)


# ---------------------------------------------------------------------------
# 8. scan_earnings_season — 누적 기준으로 스캔
# ---------------------------------------------------------------------------


def _scan_row(account, ths, frm, add=None, frm_add=None, corp="00115931"):
    row = {
        "corp_code": corp,
        "corp_name": "디오",
        "rcept_no": "20260813001067",
        "account_nm": account,
        "fs_div": "CFS",
        "sj_div": "IS",
        "thstrm_amount": ths,
        "frmtrm_amount": frm,
    }
    if add is not None:
        row["thstrm_add_amount"] = add
    if frm_add is not None:
        row["frmtrm_add_amount"] = frm_add
    return row


class ExtractAccountsBasisTests(unittest.TestCase):
    def test_half_report_uses_cumulative(self):
        rows = [
            _scan_row("매출액", "44,949,617,767", "40,099,205,051",
                      "86,272,175,136", "75,904,529,147"),
        ]
        acc = extract_accounts(rows, "00115931", "CFS", "11012")
        self.assertEqual(acc["basis"], "cum")
        self.assertEqual(acc["rev_cur"], 86_272_175_136.0)   # 상반기 누적
        self.assertEqual(acc["rev_prev"], 75_904_529_147.0)
        self.assertEqual(acc["rev_q_cur"], 44_949_617_767.0)  # 2분기 3개월 보존
        self.assertEqual(acc["rev_q_prev"], 40_099_205_051.0)

    def test_annual_has_no_cumulative_concept(self):
        rows = [_scan_row("매출액", "164,077,830,292", "119,649,873,058")]
        acc = extract_accounts(rows, "00115931", "CFS", "11011")
        self.assertEqual(acc["basis"], "annual")
        self.assertEqual(acc["rev_cur"], 164_077_830_292.0)

    def test_half_report_without_cumulative_marked_3m(self):
        rows = [_scan_row("매출액", "44,949,617,767", "40,099,205,051")]
        acc = extract_accounts(rows, "00115931", "CFS", "11012")
        self.assertEqual(acc["basis"], "3m")
        self.assertEqual(acc["rev_cur"], 44_949_617_767.0)

    def test_3m_company_flagged_in_note(self):
        rows = [_scan_row("매출액", "44,949,617,767", "40,099,205,051")]
        row = compute_row("00115931", extract_accounts(rows, "00115931", "CFS", "11012"))
        self.assertIn("⚠3개월값", row.note)

    def test_cum_company_not_flagged(self):
        rows = [
            _scan_row("매출액", "44,949,617,767", "40,099,205,051",
                      "86,272,175,136", "75,904,529,147"),
        ]
        row = compute_row("00115931", extract_accounts(rows, "00115931", "CFS", "11012"))
        self.assertNotIn("⚠3개월값", row.note)
        self.assertEqual(row.rev, 86_272_175_136.0)

    def test_prev_falls_back_to_quarter_when_cumulative_missing(self):
        rows = [
            _scan_row("매출액", "44,949,617,767", "40,099,205,051", "86,272,175,136"),
        ]
        acc = extract_accounts(rows, "00115931", "CFS", "11012")
        self.assertEqual(acc["rev_prev"], 40_099_205_051.0)


class BasisNoteTests(unittest.TestCase):
    def test_half_says_cumulative_not_the_old_reverse_claim(self):
        """예전 각주는 'Q2 이후는 누적 기준'이라 써놓고 3개월 값을 쓰고 있었다."""
        self.assertEqual(basis_note("11012", []), "상반기 누적 기준")

    def test_q1_and_q3_and_annual(self):
        self.assertEqual(basis_note("11013", []), "1분기 기준(3개월=누적)")
        self.assertEqual(basis_note("11014", []), "3분기 누적(1~9월) 기준")
        self.assertEqual(basis_note("11011", []), "연간 기준")

    def test_three_month_rows_counted(self):
        rows = [
            compute_row("00115931", extract_accounts(
                [_scan_row("매출액", "100", "90")], "00115931", "CFS", "11012")),
            compute_row("00126380", extract_accounts(
                [_scan_row("매출액", "100", "90", "200", "180", corp="00126380")],
                "00126380", "CFS", "11012")),
        ]
        note = basis_note("11012", rows)
        self.assertIn("상반기 누적 기준", note)
        self.assertIn("⚠3개월값 1건", note)


class CacheSchemaGuardTests(unittest.TestCase):
    def test_pre_basis_payload_is_stale(self):
        """basis 없는 옛 캐시는 _cur가 3개월 값이라 반드시 재조회해야 한다."""
        old = {"rcept_no": "2026...", "filing_date": "2026-08-13", "rev_cur": 1.0}
        self.assertFalse(_has_current_schema(old))

    def test_current_payload_accepted(self):
        rows = [_scan_row("매출액", "100", "90", "200", "180")]
        self.assertTrue(_has_current_schema(extract_accounts(rows, "00115931", "CFS", "11012")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
