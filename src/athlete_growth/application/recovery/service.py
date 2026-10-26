"""Gradual, non-diagnostic return-to-training workflow."""

from __future__ import annotations

from athlete_growth.domain.recovery import (
    InjuryStatus,
    ReturnStage,
    ReturnToTrainingPlan,
)
from athlete_growth.domain.safety import DISCLAIMER


class ReturnToTrainingService:
    def assess(
        self,
        *,
        injury: InjuryStatus,
        pain: int,
        swelling: bool,
        normal_daily_function: bool,
        stable_days: int,
    ) -> ReturnToTrainingPlan:
        if not 0 <= pain <= 10:
            raise ValueError("pain must be between 0 and 10")
        red_flags: list[str] = []
        if pain >= 7:
            red_flags.append("high pain")
        if swelling:
            red_flags.append("swelling")
        if not normal_daily_function:
            red_flags.append("daily function is limited")
        if red_flags:
            return ReturnToTrainingPlan(
                ReturnStage.PROTECT,
                0,
                (
                    "Pause training progression.",
                    "Seek appropriate healthcare professional review before resuming load.",
                ),
                tuple(red_flags),
                True,
                DISCLAIMER,
            )
        if injury.status == "cleared" and injury.professional_clearance:
            stage = ReturnStage.RETURN
        elif stable_days >= 7 and pain <= 2:
            stage = ReturnStage.RELOAD
        else:
            stage = ReturnStage.RESTORE
        load = {
            ReturnStage.PROTECT: 0,
            ReturnStage.RESTORE: 20,
            ReturnStage.RELOAD: 50,
            ReturnStage.RETURN: 75,
            ReturnStage.UNRESTRICTED: 100,
        }[stage]
        return ReturnToTrainingPlan(
            stage,
            load,
            ("Progress only if symptoms remain stable during and after activity.",),
            (),
            False,
            DISCLAIMER,
        )

    def progress(
        self,
        *,
        current_stage: ReturnStage,
        pain: int,
        swelling: bool,
        normal_daily_function: bool,
        stable_days: int,
    ) -> ReturnStage:
        if pain >= 4 or swelling or not normal_daily_function:
            return ReturnStage(max(ReturnStage.PROTECT, current_stage - 1))
        if pain <= 2 and stable_days >= 7:
            return ReturnStage(min(ReturnStage.UNRESTRICTED, current_stage + 1))
        return current_stage
