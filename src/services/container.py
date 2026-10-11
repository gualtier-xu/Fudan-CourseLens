"""Explicit service container assembled by :mod:`src.app`."""

from __future__ import annotations

from dataclasses import dataclass
from .domains import (
    AuthCatalogService,
    Operation,
    AutomationService,
    LearningService,
    LifecycleService,
    LiveRoomDomainService,
    MediaSessionService,
    RemoteComputeService,
    SettingsService,
    TaskService,
    TimetableService,
    ClientUpdateService,
)


@dataclass(frozen=True, slots=True)
class CourseLensServices:
    lifecycle: LifecycleService
    auth_catalog: AuthCatalogService
    media_session: MediaSessionService
    live_room: LiveRoomDomainService
    learning: LearningService
    tasks: TaskService
    remote_compute: RemoteComputeService
    automation: AutomationService
    timetable: TimetableService
    settings: SettingsService
    # U⑩：settings GET 的两个读数直挂容器顶层（路由契约按顶层读取）。
    max_deepseek_tokens_limit: Operation
    ai_usage_month: Operation
    set_max_deepseek_tokens: Operation
    # AS6（第四十七案）消耗透镜：本机累计读数挂 settings GET；余额读数由
    # 设置页显式拉取（GET /api/v3/deepseek-balance），同走顶层读数契约。
    task_usage_month: Operation
    deepseek_balance_snapshot: Operation
    client_update: ClientUpdateService

    def close(self) -> None:
        self.lifecycle.close()


__all__ = ["CourseLensServices"]
