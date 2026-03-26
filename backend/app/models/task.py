"""
Task state management.
Used for tracking long-running jobs such as graph building.

Persistence model
-----------------
- Every task is written to disk at creation time as  uploads/tasks/<task_id>.json
- Progress updates stay in memory (too frequent for disk I/O)
- Terminal states (COMPLETED / FAILED) are flushed to disk immediately so they
  survive a server restart
- get_task() falls back to disk when a task_id is not in the in-memory cache,
  which recovers from a restart that happens mid-job
"""

import os
import json
import uuid
import threading
from datetime import datetime
from enum import Enum
from typing import Dict, Any, Optional
from dataclasses import dataclass, field


class TaskStatus(str, Enum):
    """Task status enum."""
    PENDING = "pending"          # Waiting.
    PROCESSING = "processing"    # In progress.
    COMPLETED = "completed"      # Completed.
    FAILED = "failed"            # Failed.


@dataclass
class Task:
    """Task data class."""
    task_id: str
    task_type: str
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    progress: int = 0              # Overall progress percentage from 0 to 100.
    message: str = ""              # Status message.
    result: Optional[Dict] = None  # Task result.
    error: Optional[str] = None    # Error information.
    metadata: Dict = field(default_factory=dict)  # Additional metadata.
    progress_detail: Dict = field(default_factory=dict)  # Detailed progress information.
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-safe dictionary."""
        return {
            'task_id': self.task_id,
            'task_type': self.task_type,
            'status': self.status.value if isinstance(self.status, TaskStatus) else self.status,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'progress': self.progress,
            'message': self.message,
            'result': self.result,
            'error': self.error,
            'metadata': self.metadata,
            'progress_detail': self.progress_detail,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Task':
        """Reconstruct a Task from a persisted dictionary."""
        status = data.get('status', 'pending')
        if isinstance(status, str):
            try:
                status = TaskStatus(status)
            except ValueError:
                status = TaskStatus.FAILED

        def _parse_dt(val):
            if isinstance(val, datetime):
                return val
            try:
                return datetime.fromisoformat(val)
            except Exception:
                return datetime.now()

        return cls(
            task_id=data['task_id'],
            task_type=data.get('task_type', 'unknown'),
            status=status,
            created_at=_parse_dt(data.get('created_at')),
            updated_at=_parse_dt(data.get('updated_at')),
            progress=data.get('progress', 0),
            message=data.get('message', ''),
            result=data.get('result'),
            error=data.get('error'),
            metadata=data.get('metadata', {}),
            progress_detail=data.get('progress_detail', {}),
        )


class TaskManager:
    """
    Thread-safe task manager with disk persistence.

    Persistence model:
    - Tasks are written to  uploads/tasks/<task_id>.json  at creation time.
    - Progress updates (frequent) stay in memory only.
    - Terminal states (COMPLETED / FAILED) are flushed to disk immediately.
    - get_task() falls back to disk when a task isn't in memory, recovering
      from server restarts that happen while a build is running.
    """

    _instance = None
    _lock = threading.Lock()

    # Storage path is resolved lazily so Config is available.
    _tasks_dir: Optional[str] = None

    def __new__(cls):
        """Singleton implementation."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._tasks: Dict[str, Task] = {}
                    cls._instance._task_lock = threading.Lock()
        return cls._instance

    # ------------------------------------------------------------------
    # Disk helpers
    # ------------------------------------------------------------------

    @classmethod
    def _get_tasks_dir(cls) -> str:
        """Return (and create) the tasks storage directory."""
        if cls._tasks_dir is None:
            from ..config import Config
            cls._tasks_dir = os.path.join(Config.UPLOAD_FOLDER, 'tasks')
        os.makedirs(cls._tasks_dir, exist_ok=True)
        return cls._tasks_dir

    def _task_path(self, task_id: str) -> str:
        return os.path.join(self._get_tasks_dir(), f"{task_id}.json")

    def _save_task(self, task: Task) -> None:
        """Persist a task to disk; logs a warning on failure but never raises."""
        import logging
        logger = logging.getLogger('mirofish.tasks')
        try:
            path = self._task_path(task.task_id)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(task.to_dict(), f, ensure_ascii=False, indent=2)
            logger.debug("Persisted task %s (%s) to %s", task.task_id, task.status, path)
        except Exception as exc:
            logger.warning("Could not persist task %s: %s", task.task_id, exc)

    def _load_task_from_disk(self, task_id: str) -> Optional[Task]:
        """Load a task from disk; returns None on cache-miss or corrupt file."""
        import logging
        logger = logging.getLogger('mirofish.tasks')
        path = self._task_path(task_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            task = Task.from_dict(data)
            logger.info("Recovered task %s (%s) from disk", task_id, task.status)
            return task
        except Exception as exc:
            logger.warning("Could not read task file %s: %s", path, exc)
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_task(self, task_type: str, metadata: Optional[Dict] = None) -> str:
        """Create a new task, persist to disk, and return the task ID."""
        task_id = str(uuid.uuid4())
        now = datetime.now()
        task = Task(
            task_id=task_id,
            task_type=task_type,
            status=TaskStatus.PENDING,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )
        with self._task_lock:
            self._tasks[task_id] = task
        self._save_task(task)
        return task_id

    def get_task(self, task_id: str) -> Optional[Task]:
        """Fetch a task — checks memory first, then falls back to disk."""
        with self._task_lock:
            task = self._tasks.get(task_id)
            if task is not None:
                return task
        # Cache miss (e.g. after a server restart): try disk
        task = self._load_task_from_disk(task_id)
        if task is not None:
            with self._task_lock:
                self._tasks[task_id] = task
        return task

    def update_task(
        self,
        task_id: str,
        status: Optional[TaskStatus] = None,
        progress: Optional[int] = None,
        message: Optional[str] = None,
        result: Optional[Dict] = None,
        error: Optional[str] = None,
        progress_detail: Optional[Dict] = None,
    ):
        """Update task state; flushes to disk when the task reaches a terminal state."""
        flush = False
        with self._task_lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task.updated_at = datetime.now()
            if status is not None:
                task.status = status
                if status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    flush = True
            if progress is not None:
                task.progress = progress
            if message is not None:
                task.message = message
            if result is not None:
                task.result = result
                flush = True
            if error is not None:
                task.error = error
            if progress_detail is not None:
                task.progress_detail = progress_detail
        if flush:
            self._save_task(task)

    def complete_task(self, task_id: str, result: Dict):
        """Mark a task as completed and persist the result."""
        self.update_task(
            task_id,
            status=TaskStatus.COMPLETED,
            progress=100,
            message="Task completed",
            result=result,
        )

    def fail_task(self, task_id: str, error: str):
        """Mark a task as failed and persist the error."""
        self.update_task(
            task_id,
            status=TaskStatus.FAILED,
            message="Task failed",
            error=error,
        )

    def list_tasks(self, task_type: Optional[str] = None) -> list:
        """List all in-memory tasks, newest first."""
        with self._task_lock:
            tasks = list(self._tasks.values())
        if task_type:
            tasks = [t for t in tasks if t.task_type == task_type]
        return [t.to_dict() for t in sorted(tasks, key=lambda x: x.created_at, reverse=True)]

    def cleanup_old_tasks(self, max_age_hours: int = 24):
        """Remove old completed/failed tasks from memory and disk."""
        from datetime import timedelta
        import logging
        logger = logging.getLogger('mirofish.tasks')
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        with self._task_lock:
            old_ids = [
                tid for tid, t in self._tasks.items()
                if t.created_at < cutoff and t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
            ]
            for tid in old_ids:
                del self._tasks[tid]
        for tid in old_ids:
            path = self._task_path(tid)
            try:
                if os.path.exists(path):
                    os.remove(path)
                    logger.debug("Removed stale task file %s", path)
            except OSError as exc:
                logger.warning("Could not delete task file %s: %s", path, exc)

