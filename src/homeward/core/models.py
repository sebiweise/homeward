"""Core data types shared by planner, executor and providers."""

from enum import StrEnum


class SyncMode(StrEnum):
    """Direction of a Drive sync pair."""

    UPLOAD = "upload"
    DOWNLOAD = "download"
    BIDIRECTIONAL = "bidirectional"
