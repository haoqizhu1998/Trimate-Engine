"""Operating-system scheduler adapters."""

from athlete_growth.adapters.scheduler.macos import LABEL, MacOSLaunchdScheduler
from athlete_growth.adapters.scheduler.windows import TASK_NAME, WindowsTaskScheduler

__all__ = ["LABEL", "MacOSLaunchdScheduler", "TASK_NAME", "WindowsTaskScheduler"]
