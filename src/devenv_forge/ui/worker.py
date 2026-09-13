"""Background worker.

Every check and every remedy runs here. Some remedies take minutes and raise a
UAC prompt, so nothing in this module may touch a widget: it communicates only
through signals.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from ..core.models import CheckResult, ProgressSink, RemedyOutcome, Status
from ..platforms.base import Platform


class SignalSink(ProgressSink):
    """Adapts :class:`ProgressSink` onto Qt signals.

    Qt queues cross-thread signal emissions, so a remedy can report progress
    from the worker thread without any locking on the UI side.
    """

    def __init__(self, worker: "PreflightWorker") -> None:
        self._worker = worker

    def log(self, line: str) -> None:
        self._worker.log.emit(line)

    def step(self, message: str) -> None:
        self._worker.step.emit(message)

    @property
    def cancelled(self) -> bool:
        return self._worker.cancel_requested


class PreflightWorker(QObject):
    """Runs the preflight sequence and applies remedies on demand."""

    check_started = pyqtSignal(str, str)
    check_finished = pyqtSignal(object)
    run_finished = pyqtSignal(object)
    remedy_started = pyqtSignal(str)
    #: key, outcome, in_batch. ``in_batch`` tells the page not to re-run the
    #: checks yet, because more fixes are still queued behind this one.
    remedy_finished = pyqtSignal(str, object, bool)
    batch_finished = pyqtSignal()
    log = pyqtSignal(str)
    step = pyqtSignal(str)
    busy_changed = pyqtSignal(bool)

    def __init__(self, platform: Platform) -> None:
        super().__init__()
        self.platform = platform
        self.cancel_requested = False
        self._sink = SignalSink(self)

    # -- checks ------------------------------------------------------------

    @pyqtSlot()
    def run_checks(self) -> None:
        self.cancel_requested = False
        self.busy_changed.emit(True)
        self.log.emit(f"Running preflight on {self.platform.family.value}")
        results: list[CheckResult] = []
        try:
            for key, title, probe in self.platform.steps():
                if self.cancel_requested:
                    self.log.emit("Cancelled")
                    break
                self.check_started.emit(key, title)
                try:
                    result = probe()
                except Exception as exc:  # one bad probe must not stop the run
                    result = CheckResult(
                        key=key,
                        title=title,
                        status=Status.FAILED,
                        summary="Check raised an error",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                self.platform.results[result.key] = result
                results.append(result)
                self.log.emit(f"{result.title}: {result.status.value} - {result.summary}")
                self.check_finished.emit(result)
        finally:
            self.run_finished.emit(results)
            self.busy_changed.emit(False)

    # -- remedies ----------------------------------------------------------

    @pyqtSlot(str)
    def apply_remedy(self, key: str) -> None:
        result = self.platform.results.get(key)
        if result is None or result.remedy is None:
            self.remedy_finished.emit(
                key, RemedyOutcome(False, "Nothing to apply for this check."), False
            )
            return

        self.busy_changed.emit(True)
        outcome = self._run_one(key, result)
        # Clear busy *before* announcing the outcome: the page reacts by
        # re-running the checks, and that call is refused while busy.
        self.busy_changed.emit(False)
        self.remedy_finished.emit(key, outcome, False)

    def _run_one(self, key: str, result) -> RemedyOutcome:
        self.remedy_started.emit(key)
        self.log.emit(f"--- {result.remedy.label}: {result.title} ---")
        try:
            outcome = result.remedy.action(self._sink)
        except Exception as exc:
            outcome = RemedyOutcome(False, f"{type(exc).__name__}: {exc}")
        self.log.emit(f"Result: {'ok' if outcome.ok else 'failed'} - {outcome.message}")
        return outcome

    @pyqtSlot(list)
    def apply_all(self, keys: list) -> None:
        """Apply several remedies in the order the checks are listed.

        Stops at the first failure: later fixes usually depend on earlier ones,
        so continuing would just produce a cascade of confusing errors.
        """
        self.cancel_requested = False
        self.busy_changed.emit(True)
        try:
            for key in keys:
                if self.cancel_requested:
                    self.log.emit("Cancelled")
                    break
                result = self.platform.results.get(key)
                if result is None or result.remedy is None:
                    continue
                outcome = self._run_one(key, result)
                self.remedy_finished.emit(key, outcome, True)
                if not outcome.ok:
                    self.log.emit("Stopping: later steps depend on this one.")
                    break
        finally:
            self.busy_changed.emit(False)
            # One re-check for the whole batch, not one per fix.
            self.batch_finished.emit()

    @pyqtSlot()
    def cancel(self) -> None:
        self.cancel_requested = True
