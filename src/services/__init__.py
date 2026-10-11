"""Explicit service boundaries used by the local application."""

from .container import CourseLensServices
from .domains import (
    AuthCatalogService, MediaSessionService, LearningService, LifecycleService,
    TaskService, RemoteComputeService, AutomationService, TimetableService,
    SettingsService, ClientUpdateService, LiveRoomDomainService,
)

__all__ = [
    "CourseLensServices", "AuthCatalogService", "MediaSessionService",
    "LearningService", "LifecycleService", "TaskService", "RemoteComputeService",
    "AutomationService", "TimetableService", "SettingsService",
    "ClientUpdateService", "LiveRoomDomainService",
]
