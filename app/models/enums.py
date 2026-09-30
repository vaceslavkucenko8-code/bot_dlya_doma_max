import enum


class UserRole(str, enum.Enum):
    RESIDENT = "resident"
    MANAGEMENT = "management"


class IncidentStatus(str, enum.Enum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
