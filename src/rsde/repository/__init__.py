"""Repository state: workspace files, configuration and persisted evidence."""

from rsde.repository.config import (
    CONFIG_FILE,
    DEFAULT_ROOT_SPEC,
    Config,
    ConfigError,
    Workspace,
    WorkspaceError,
    load_config,
    open_workspace,
)
from rsde.repository.files import (
    Changes,
    FileHasher,
    GlobSet,
    diff_snapshots,
    list_workspace_files,
    matching_files,
    take_snapshot,
)
from rsde.repository.state import CheckResult, Evidence, RunRecord, StateStore

__all__ = [
    "CONFIG_FILE",
    "DEFAULT_ROOT_SPEC",
    "Changes",
    "CheckResult",
    "Config",
    "ConfigError",
    "Evidence",
    "FileHasher",
    "GlobSet",
    "RunRecord",
    "StateStore",
    "Workspace",
    "WorkspaceError",
    "diff_snapshots",
    "list_workspace_files",
    "load_config",
    "matching_files",
    "open_workspace",
    "take_snapshot",
]
