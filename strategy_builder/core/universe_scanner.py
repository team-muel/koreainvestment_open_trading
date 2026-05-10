"""KOSPI/KOSDAQ 유니버스 스캐너 — ICT Liquidity Reclaim 전략 특화 점수 기반 종목 선정.

선정 원칙:
  ICT 전략에 적합한 종목을 단순 정량 필터가 아닌
  100점 만점 다요소 점수로 평가한다.
  목표: "강한 종목"이 아니라 "장중 ICT 진입 구조가 만들어질 수 있는 위치의 종목"

점수 구성 (100점 + bonus - penalties):
  A. Sell-side Liquidity 근접도  30점  — 핵심 (전일/근일 저점 스윕 후 리클레임)
  B. 거래량 구조                 20점  — 관심도 및 지속성
  C. 거래대금 / 유동성           15점  — 주문 체결 가능성
  D. 가격 구조                   15점  — OR/VWAP 적합성 (저점↔고점 샌드위치)
  E. 전일 등락률 위치            10점  — 스윕 후 리클레임 가능성
  F. 캐시 준비도                  5점  — 분봉 신호 신뢰도
  G. 추세 일관성                  5점  — 단기 방향성

Hard filter (즉시 제외):
  - 주가 2,000원 미만
  - 당일/평균 거래대금 30억 미만
  - ETF/ETN/ELW/스팩/우선주/리츠
  - 전일 등락률 -2% 미만 또는 +8% 초과
  - 장전 예상체결 갭 -5% 미만
  - VI 위험 (갭 +8% 이상 / 상한가 근접)

등급 기준:
  80점+: A+ (Prime Setup)
  70~79: A  (High Priority)
  60~69: B  (Watch)
  50~59: C  (Low Priority)
  40~49: D  (Monitor Only)
  40 미만: Excluded
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ict_core import ICTSetup, IntradayLiquidityReclaimBuilder

from . import data_fetcher
from .ict_cache import MinuteBarCache, build_minute_bar_cache
from .symbol_master import SymbolInfo, SymbolMaster

logger = logging.getLogger(__name__)

EXCLUDED_NAME_KEYWORDS = (
    "ETF", "ETN", "ELW", "스팩", "SPAC",
    "리츠", "REIT", "인버스", "레버리지", "선물", "옵션",
)

GRADE_THRESHOLDS = [
    (80, "A+", "Prime Setup"),
    (70, "A",  "High Priority"),
    (60, "B",  "Watch"),
    (50, "C",  "Low Priority"),
    (40, "D",  "Monitor Only"),
    (0,  "X",  "Excluded"),
]


def score_to_grade(score: float) -> tuple[str, str]:
    for threshold, grade, label in GRADE_THRESHOLDS:
        if score >= threshold:
            return grade, label
    return "X", "Excluded"


@dataclass(frozen=True)
class UniverseFilterConfig:
    # Hard filter
    min_price: int = 2_000
    min_avg_trading_value: int = 3_000_000_000      # 평균 30억
    min_last_trading_value: int = 5_000_000_000     # 당일 50억
    min_prev_change_pct: float = -0.02              # 전일 -2% 이상 (조정 후 반등 포함)
    max_prev_change_pct: float = 0.08               # 전일 +8% 미만 (VI 위험 제외)
    min_relative_volume: float = 1.5
    daily_lookback_days: int = 25
    max_scan_symbols: int = 200
    # 저장 계층
    premarket_save_limit: int = 30                  # premarket_scan 저장: 상위 30개
    watchlist_limit: int = 10                       # watchlists 저장: 상위 10개
    engine_symbols_limit: int = 5                   # 엔진 투입: ICT 70점+ 상위 5개
    engine_min_score: float = 60.0                  # 엔진 투입 최소 점수
    request_delay: float = 0.8
    require_ready_cache: bool = False
    use_volume_rank: bool = True
    # 추가 필터
    exclude_vi_risk: bool = True
    vi_gap_pct: float = 0.08                        # 갭 8% 이상 = VI 위험
    exclude_expected_fill_risk: bool = True
    expected_fill_gap_min: float = -0.05            # 갭 -5% 미만 제외
    exclude_sector_momentum: bool = True
    min_ict_score: float = 40.0                     # watchlist 최소 점수


@dataclass
class ICTScore:
    """ICT 전략 적합도 점수 세부 내역."""
    sell_side_liquidity: float = 0.0   # A: 0~30
    volume_structure: float = 0.0      # B: 0~20
    trading_value: float = 0.0         # C: 0~15
    price_structure: float = 0.0       # D: 0~15
    prev_change_position: float = 0.0  # E: 0~10
    cache_readiness: float = 0.0       # F: 0~5
    trend_consistency: float = 0.0     # G: 0~5
    bonus: float = 0.0
    penalties: float = 0.0
    total: float = 0.0
    grade: str = "X"
    grade_label: str = "Excluded"
    reasons_ko: list[str] = field(default_factory=list)    # 선정 근거
    warnings_ko: list[str] = field(default_factory=list)   # 위험 경고
    details: dict[str, Any] = field(default_factory=dict)  # 상세 수치

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": round(self.total, 1),
            "grade": self.grade,
            "grade_label": self.grade_label,
            "sell_side_liquidity": self.sell_side_liquidity,
            "volume_structure": self.volume_structure,
            "trading_value": self.trading_value,
            "price_structure": self.price_structure,
            "prev_change_position": self.prev_change_position,
            "cache_readiness": self.cache_readiness,
            "trend_consistency": self.trend_consistency,
            "bonus": self.bonus,
            "penalties": self.penalties,
            "selection_reason_ko": " | ".join(self.reasons_ko),
            "warnings_ko": self.warnings_ko,
            "details": self.details,
        }


@dataclass(frozen=True)
class UniverseCandidate:
    symbol: SymbolInfo
    last_close: float
    last_volume: int
    last_trading_value: float
    avg_trading_value: float
    prev_change_pct: float
    prev_high_distance_pct: float | None
    prev_low_distance_pct: float | None
    relative_volume: float
    liquidity_level: str
    ict_setup: str
    scan_reason: str
    priority: int
    invalidation_level: float | None
    ready_coverage_days: int
    ict_score: ICTScore | None = None
    setup: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        base = {
            "code": self.symbol.code,
            "name": self.symbol.name,
            "exchange": self.symbol.exchange,
            "exchange_name": self.symbol.exchange_name,
            "last_close": self.last_close,
            "last_volume": self.last_volume,
            "last_trading_value": self.last_trading_value,
            "avg_trading_value": self.avg_trading_value,
            "prev_change_pct": self.prev_change_pct,
            "prev_high_distance_pct": self.prev_high_distance_pct,
            "prev_low_distance_pct": self.prev_low_distance_pct,
            "relative_volume": self.relative_volume,
            "liquidity_level": self.liquidity_level,
            "ict_setup": self.ict_setup,
            "scan_reason": self.scan_reason,
            "priority": self.priority,
            "invalidation_level": self.invalidation_level,
            "plan": "09:15-10:30 OR Low/previous low sweep + VWAP reclaim only",
            "status": "WATCHLIST",
            "ready_coverage_days": self.ready_coverage_days,
            "setup": self.setup,
        }
        if self.ict_score:
            base["ict_score"] = self.ict_score.to_dict()
        return base


class KRXUniverseScanner:
    def __init__(
        self,
        master: SymbolMaster | None = None,
        cache: MinuteBarCache | None = None,
        builder: IntradayLiquidityReclaimBuilder | None = None,
    ):
        self.master = master or SymbolMaster()
        self.cache = cache or build_minute_bar_cache()
        self.builder = builder or IntradayLiquidityReclaimBuilder()

    def collect_master(self) -> dict:
        return self.master.collect(["kospi", "kosdaq"])

    def scan(self, config: UniverseFilterConfig | None = None, env_dv: str = "vps") -> dict:
        config = config or UniverseFilterConfig()
        symbols = self.master.load_all()
        if not symbols:
            self.collect_master()
            symbols = self.master.load_all()

        filtered_symbols = [s for s in symbols if self._basic_symbol_ok(s)]
        filtered_symbols = self._liquidity_first_order(filtered_symbols, config, env_dv)

        candidates: list[UniverseCandidate] = []
        scanned = skipped = 0

        for symbol in filtered_symbols[:config.max_scan_symbols]:
            scanned += 1
            candidate = self._evaluate_candidate(symbol, config, env_dv)
            if candidate is None:
                skipped += 1
            else:
                candidates.append(candidate)
            time.sleep(config.request_delay)

        # 점수 기준 내림차순 정렬
        scored = [c for c in candidates if c.ict_score and c.ict_score.total >= config.min_ict_score]
        ranked = sorted(scored, key=lambda c: c.ict_score.total if c.ict_score else 0, reverse=True)

        if config.exclude_sector_momentum:
            ranked = self._apply_sector_limit(ranked, max_per_sector=2)

        # premarket_scan 저장용 (상위 30개)
        premarket_list = []
        for rank, candidate in enumerate(ranked[:config.premarket_save_limit], start=1):
            item = candidate.to_dict()
            item["rank"] = rank
            premarket_list.append(item)

        # watchlists 저장용 (상위 10개)
        watchlist = premarket_list[:config.watchlist_limit]

        # 엔진 투입 종목: 70점+ 우선, 없으면 60점+, 상위 5개
        engine_candidates = [c for c in ranked if c.ict_score and c.ict_score.total >= 70]
        if len(engine_candidates) < config.engine_symbols_limit:
            engine_candidates = [c for c in ranked if c.ict_score and c.ict_score.total >= config.engine_min_score]
        engine_symbols = [c.symbol.code for c in engine_candidates[:config.engine_symbols_limit]]

        logger.info(
            "유니버스 스캔: 전체 %d → 기초필터 %d → 점수합격 %d → watchlist %d → 엔진투입 %s",
            len(symbols), len(candidates), len(scored), len(watchlist), engine_symbols,
        )
        return {
            "universe_count": len(symbols),
            "eligible_name_count": len(filtered_symbols),
            "scanned_count": scanned,
            "candidate_count": len(candidates),
            "scored_count": len(scored),
            "watchlist_count": len(watchlist),
            "watchlist": watchlist,
            "premarket_list": premarket_list,
            "symbols_for_ict_start": engine_symbols,
            "engine_symbols": engine_symbols,
            "filters": {
                "min_price": config.min_price,
                "min_avg_trading_value": config.min_avg_trading_value,
                "min_ict_score": config.min_ict_score,
                "engine_min_score": config.engine_min_score,
            },
        }

    def scan_cached_ict_setups(self, symbols: list[str], limit: int = 30) -> dict:
        setups: list[dict] = []
        for symbol in symbols[:limit]:
            setup = self._build_setup_from_cache(symbol)
            if setup is not None:
                setups.append(setup.to_dict())
        actionable = [s for s in setups if s.get("trade_plan") and s.get("state") != "WARMING_UP"]
        return {
            "scanned_count": min(len(symbols), limit),
            "setup_count": len(setups),
            "actionable_count": len(actionable),
            "setups": setups,
            "actionable_symbols": [s["symbol"] for s in actionable],
        }

    # ── 핵심: ICT 점수 평가 ───────────────────────────────────

    def _evaluate_candidate(
        self,
        symbol: SymbolInfo,
        config: UniverseFilterConfig,
        env_dv: str,
    ) -> UniverseCandidate | None:
        df = data_fetcher.get_daily_prices(symbol.code, days=config.daily_lookback_days, env_dv=env_dv)
        if df.empty or len(df) < 5:
            return None

        prev_day = df.iloc[-1]
        day_before = df.iloc[-2] if len(df) >= 2 else prev_day

        last_close = float(prev_day["close"])
        last_volume = int(prev_day["volume"])
        last_trading_value = last_close * last_volume
        avg_trading_value = float((df["close"] * df["volume"]).tail(20).mean())
        avg_volume = float(df["volume"].tail(20).mean())
        relative_volume = last_volume / avg_volume if avg_volume > 0 else 0.0

        prev_close = float(prev_day["close"])
        prev_high = float(prev_day["high"])
        prev_low = float(prev_day["low"])
        day_before_close = float(day_before["close"])
        prev_change_pct = (
            (prev_close - day_before_close) / day_before_close
            if day_before_close > 0
            else 0.0
        )

        recent_highs = df["high"].tail(5).tolist() if "high" in df.columns else []
        recent_lows = df["low"].tail(5).tolist() if "low" in df.columns else []
        recent_high_5 = max(recent_highs) if recent_highs else None
        recent_low_5 = min(recent_lows) if recent_lows else None

        # 3일 거래량/거래대금 추세
        vol_3d = df["volume"].tail(3).tolist() if len(df) >= 3 else []
        tv_3d = (df["close"] * df["volume"]).tail(3).tolist() if len(df) >= 3 else []

        ready_days = self.cache.ready_coverage_days(symbol.code)

        # ── Hard filter ────────────────────────────────────────
        if last_close < config.min_price:
            return None
        if last_trading_value < config.min_last_trading_value:
            return None
        if avg_trading_value < config.min_avg_trading_value:
            return None
        if relative_volume < config.min_relative_volume:
            return None
        # 전일 등락률 범위: -2% ~ +8% (Score E와 일관성 유지)
        if not (config.min_prev_change_pct <= prev_change_pct <= config.max_prev_change_pct):
            return None

        # 장전 예상체결가 갭 확인
        current_price = 0.0
        try:
            price_data = data_fetcher.get_current_price(symbol.code, env_dv)
            current_price = float(price_data.get("price", 0) or 0)
            if current_price > 0 and prev_close > 0:
                gap = (current_price - prev_close) / prev_close
                # 갭 -5% 미만 제외
                if gap < config.expected_fill_gap_min:
                    return None
                # VI 위험: 갭 +8% 이상
                if config.exclude_vi_risk and gap >= config.vi_gap_pct:
                    return None
        except Exception:
            pass

        # ── ICT 점수 계산 ──────────────────────────────────────
        score = self._calculate_ict_score(
            last_close=last_close,
            prev_low=prev_low,
            prev_high=prev_high,
            prev_close=prev_close,
            prev_change_pct=prev_change_pct,
            recent_low_5=recent_low_5,
            recent_high_5=recent_high_5,
            relative_volume=relative_volume,
            avg_trading_value=avg_trading_value,
            last_trading_value=last_trading_value,
            vol_3d=vol_3d,
            tv_3d=tv_3d,
            ready_days=ready_days,
        )

        prev_high_dist = (prev_high - last_close) / last_close if last_close > 0 else None
        prev_low_dist = (last_close - prev_low) / last_close if last_close > 0 else None
        liquidity_level = self._liquidity_level(prev_high_dist, prev_low_dist)
        ict_setup = self._build_ict_setup_text(last_close, prev_low, prev_high, score)

        return UniverseCandidate(
            symbol=symbol,
            last_close=last_close,
            last_volume=last_volume,
            last_trading_value=last_trading_value,
            avg_trading_value=avg_trading_value,
            prev_change_pct=prev_change_pct,
            prev_high_distance_pct=prev_high_dist,
            prev_low_distance_pct=prev_low_dist,
            relative_volume=relative_volume,
            liquidity_level=liquidity_level,
            ict_setup=ict_setup,
            scan_reason=" | ".join(score.reasons_ko),
            priority=self._grade_to_priority(score.grade),
            invalidation_level=prev_low,
            ready_coverage_days=ready_days,
            ict_score=score,
        )

    def _calculate_ict_score(
        self,
        last_close: float,
        prev_low: float,
        prev_high: float,
        prev_close: float,
        prev_change_pct: float,
        recent_low_5: float | None,
        recent_high_5: float | None,
        relative_volume: float,
        avg_trading_value: float,
        last_trading_value: float,
        vol_3d: list[float],
        tv_3d: list[float],
        ready_days: int,
    ) -> ICTScore:
        score = ICTScore()
        reasons: list[str] = []
        warnings: list[str] = []

        # ── A. Sell-side Liquidity 근접도 (0~30점) ─────────────
        # 거리 기준: (현재가 - 전일 저점) / 전일 저점 (prev_low 기준)
        a_score = 0.0
        if prev_low > 0 and last_close > 0:
            dist = (last_close - prev_low) / prev_low

            if dist < 0:
                # 현재가가 전일 저점 아래 → 이미 스윕 완료 또는 하락 중
                a_score = 0.0
                warnings.append(f"현재가가 전일 저점({prev_low:,.0f}) 아래 — 이미 스윕 or 하락 진행")
            elif dist < 0.005:
                # 0~0.5%: 너무 근접, 장 시작 직후 바로 깨질 수 있음
                a_score = 15.0
                warnings.append(f"전일 저점 {dist:.1%} 이내 — 스윕 후보이나 추격 위험")
                score.details["risk_flag"] = "too_close_to_prev_low"
            elif dist <= 0.02:
                # 0.5~2%: 최적 구간
                a_score = 30.0
                reasons.append(f"전일 저점 {dist:.1%} 상단 (스윕 후 리클레임 최적)")
            elif dist <= 0.04:
                # 2~4%: 좋음
                a_score = 22.0
                reasons.append(f"전일 저점 {dist:.1%} 상단 (근접)")
            elif dist <= 0.07:
                # 4~7%: 보통
                a_score = 12.0
                reasons.append(f"전일 저점 {dist:.1%} 상단 (보통)")
            else:
                # 7% 초과: 점수 없음
                a_score = 0.0
                reasons.append(f"전일 저점 {dist:.1%} 상단 (원거리)")

        # 5일 저점도 보완
        if recent_low_5 and recent_low_5 != prev_low and recent_low_5 > 0 and last_close > 0:
            dist_5d = (last_close - recent_low_5) / recent_low_5
            if 0.005 <= dist_5d <= 0.03 and a_score < 22:
                a_score = max(a_score, 18.0)
                reasons.append(f"5일 저점 {dist_5d:.1%} 상단 (보완)")

        score.sell_side_liquidity = a_score

        # ── B. 거래량 구조 (0~20점) ───────────────────────────
        b_score = 0.0
        if relative_volume >= 5.0:
            b_score = 20.0
            reasons.append(f"상대거래량 {relative_volume:.1f}x (급증)")
        elif relative_volume >= 3.0:
            b_score = 16.0
            reasons.append(f"상대거래량 {relative_volume:.1f}x (강세)")
        elif relative_volume >= 2.0:
            b_score = 12.0
            reasons.append(f"상대거래량 {relative_volume:.1f}x (보통)")
        elif relative_volume >= 1.5:
            b_score = 6.0
            reasons.append(f"상대거래량 {relative_volume:.1f}x (미약)")

        # 3일 연속 거래량 증가 + 마지막 날 거래대금 300억 이상
        if (
            len(vol_3d) == 3
            and vol_3d[0] < vol_3d[1] < vol_3d[2]
            and last_trading_value >= 30_000_000_000
        ):
            b_score = min(b_score + 5.0, 20.0)
            reasons.append("3일 연속 거래량 증가 (거래대금 300억+ 확인)")

        score.volume_structure = b_score

        # ── C. 거래대금 / 유동성 (0~15점) ────────────────────
        c_score = 0.0
        if avg_trading_value >= 100_000_000_000:   # 1000억
            c_score = 15.0
        elif avg_trading_value >= 50_000_000_000:  # 500억
            c_score = 12.0
        elif avg_trading_value >= 30_000_000_000:  # 300억
            c_score = 8.0
        elif avg_trading_value >= 10_000_000_000:  # 100억
            c_score = 4.0
        # 100억 미만은 0점 (hard filter 30억 통과한 경우도 점수 없음)

        if c_score > 0:
            reasons.append(f"평균거래대금 {avg_trading_value/1e8:.0f}억")
        # 상대거래량 높지만 거래대금 100억 미만 → penalty (아래에서 처리)

        score.trading_value = c_score

        # ── D. 가격 구조 (0~15점) ─────────────────────────────
        d_score = 0.0

        if prev_high > 0 and last_close > 0:
            high_dist = (prev_high - last_close) / last_close
            if 0.01 <= high_dist <= 0.05:
                d_score += 10.0
                reasons.append(f"전일 고점 {high_dist:.1%} 하단 (Buy-side liquidity)")
            elif 0.05 < high_dist <= 0.10:
                d_score += 5.0

        if prev_high > prev_low and prev_low > 0:
            close_loc = (prev_close - prev_low) / (prev_high - prev_low)
            if close_loc >= 0.8:
                d_score = min(d_score + 5.0, 15.0)
                reasons.append(f"전일 고가 {close_loc:.0%} 위치 마감 (강한 수급)")
            elif close_loc <= 0.2:
                warnings.append(f"전일 저가 근접 마감 ({close_loc:.0%}) — 매도 압력 주의")

        score.price_structure = d_score

        # ── E. 전일 등락률 (0~10점) ───────────────────────────
        e_score = 0.0
        if 0.005 <= prev_change_pct <= 0.02:
            e_score = 10.0
            reasons.append(f"전일 {prev_change_pct:+.1%} (이상적)")
        elif -0.02 <= prev_change_pct < 0.005:
            e_score = 8.0
            reasons.append(f"전일 {prev_change_pct:+.1%} (조정 후 반등)")
        elif 0.0 <= prev_change_pct < 0.005:
            e_score = 6.0
        elif 0.02 < prev_change_pct <= 0.05:
            e_score = 5.0
        elif 0.05 < prev_change_pct <= 0.08:
            e_score = 2.0
            warnings.append(f"전일 {prev_change_pct:+.1%} (과열 주의)")
        score.prev_change_position = e_score

        # ── F. 캐시 준비도 (0~5점) ────────────────────────────
        f_score = 5.0 if ready_days >= 20 else 3.0 if ready_days >= 10 else 1.0 if ready_days >= 5 else 0.0
        score.cache_readiness = f_score

        # ── G. 추세 일관성 (0~5점) ────────────────────────────
        g_score = 0.0
        if recent_low_5 and prev_low > 0 and recent_low_5 > 0 and prev_low > recent_low_5 * 0.99:
            g_score += 3.0
            reasons.append("5일 저점 상승 추세")
        if recent_high_5 and prev_high > 0 and prev_high >= recent_high_5 * 0.98:
            g_score = min(g_score + 2.0, 5.0)
        score.trend_consistency = g_score

        # ── Penalty ───────────────────────────────────────────
        penalties = 0.0
        if 0.05 < prev_change_pct <= 0.08:
            penalties += 5.0
            warnings.append(f"전일 {prev_change_pct:+.1%} 과열 페널티 -5점")
        if prev_high > prev_low and prev_low > 0:
            range_pct = (prev_high - prev_low) / prev_low
            if range_pct > 0.10 and a_score > 0:
                penalties += 10.0
                warnings.append(f"전일 변동폭 {range_pct:.1%} 과도 -10점")
        if score.details.get("risk_flag") == "too_close_to_prev_low":
            penalties += 5.0
        if relative_volume >= 3.0 and avg_trading_value < 10_000_000_000:
            penalties += 5.0
            warnings.append("거래량 급증이나 거래대금 100억 미만 -5점")
        score.penalties = penalties

        # ── 최종 점수 ─────────────────────────────────────────
        raw = (a_score + score.volume_structure + score.trading_value + score.price_structure
               + score.prev_change_position + score.cache_readiness + score.trend_consistency
               + score.bonus - score.penalties)
        score.total = max(0.0, min(100.0, raw))
        score.grade, score.grade_label = score_to_grade(score.total)
        score.reasons_ko = reasons
        score.warnings_ko = warnings
        score.details.update({
            "prev_low": prev_low, "prev_high": prev_high,
            "prev_low_dist_pct": round((last_close - prev_low) / prev_low, 4) if prev_low > 0 else None,
            "prev_high_dist_pct": round((prev_high - last_close) / last_close, 4) if last_close > 0 else None,
            "relative_volume": round(relative_volume, 2),
            "avg_trading_value_bn": round(avg_trading_value / 1e8, 1),
            "ready_days": ready_days,
        })
        return score

    def _build_ict_setup_text(self, last_close: float, prev_low: float, prev_high: float, score: ICTScore) -> str:
        parts = []
        if score.sell_side_liquidity >= 25:
            dist = (last_close - prev_low) / prev_low if prev_low > 0 else 0
            parts.append(f"전일 저점({prev_low:,.0f}) {dist:.1%} 상단 — Sell-side sweep 대기")
        elif score.sell_side_liquidity >= 15:
            parts.append(f"전일 저점({prev_low:,.0f}) 근처")
        else:
            parts.append(f"전일 저점 원거리")
        if score.price_structure >= 8:
            hdist = (prev_high - last_close) / last_close if last_close > 0 else 0
            parts.append(f"전일 고점({prev_high:,.0f}) {hdist:.1%} 하단 Buy-side target")
        parts.append(f"점수 {score.total:.0f}점 [{score.grade}: {score.grade_label}]")
        if score.warnings_ko:
            parts.append(f"⚠ {score.warnings_ko[0]}")
        return " | ".join(parts)

    @staticmethod
    def _grade_to_priority(grade: str) -> int:
        return {"A+": 5, "A": 4, "B": 3, "C": 2, "D": 1}.get(grade, 1)

    def _basic_symbol_ok(self, symbol: SymbolInfo) -> bool:
        name = symbol.name.upper()
        if symbol.exchange not in ("kospi", "kosdaq"):
            return False
        if any(k.upper() in name for k in EXCLUDED_NAME_KEYWORDS):
            return False
        if symbol.name.endswith(("우", "우B", "우C")):
            return False
        return True

    def _liquidity_first_order(self, symbols: list[SymbolInfo], config: UniverseFilterConfig, env_dv: str) -> list[SymbolInfo]:
        if not config.use_volume_rank:
            return symbols
        try:
            rank = data_fetcher.get_volume_rank(env_dv=env_dv, min_price=config.min_price, min_volume=100_000)
            if rank.empty or "code" not in rank.columns:
                return symbols
            by_code = {s.code: s for s in symbols}
            ranked: list[SymbolInfo] = []
            seen: set[str] = set()
            for code in rank["code"].astype(str):
                sym = by_code.get(code.zfill(6))
                if sym and sym.code not in seen:
                    ranked.append(sym)
                    seen.add(sym.code)
            ranked.extend(s for s in symbols if s.code not in seen)
            return ranked
        except Exception:
            return symbols

    @staticmethod
    def _apply_sector_limit(candidates: list[UniverseCandidate], max_per_sector: int = 2) -> list[UniverseCandidate]:
        seen: dict[str, int] = {}
        result: list[UniverseCandidate] = []
        for c in candidates:
            key = c.liquidity_level
            if seen.get(key, 0) < max_per_sector:
                result.append(c)
                seen[key] = seen.get(key, 0) + 1
        return result

    def _build_setup_from_cache(self, symbol: str) -> ICTSetup | None:
        bars_1m = self.cache.get_1m_bars(symbol)
        if len(bars_1m) < 300:
            return None
        bars_1d = self.cache.resample_krx_session_daily(bars_1m)
        return self.builder.build_long_setup(symbol, bars_1m, bars_1d)

    @staticmethod
    def _liquidity_level(prev_high_distance_pct: float | None, prev_low_distance_pct: float | None) -> str:
        distances = [abs(v) for v in (prev_high_distance_pct, prev_low_distance_pct) if v is not None]
        if not distances:
            return "unknown"
        nearest = min(distances)
        if nearest < 0.005:
            return "at_liquidity"
        if nearest <= 0.03:
            return "near_previous_liquidity"
        return "far_from_previous_liquidity"
