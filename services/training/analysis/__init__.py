"""Training session-analysis library (distilled from the athlete's coaching chat, validated).

    from analysis import Athlete, analyse_safe
    res = analyse_safe(hr_per_second, duration_min, Athlete(), sport_label)

Per-second HR only (analyse_safe rejects pre-binned input). numpy required; scipy optional (fits).
"""
from .engine import Athlete, analyse, CHART_SPEC, five_min_max
from .revisions import analyse_safe, flag_easy_session, trend_chart_spec
__all__ = ["Athlete", "analyse", "analyse_safe", "CHART_SPEC", "five_min_max",
           "flag_easy_session", "trend_chart_spec"]
