"""S-018: the admission work queue: a worker pool, fair across submitters, bounded, and restart-safe.

- Workers: `workers` threads process submissions in parallel; each case is one sandbox run, so this is the
  number of containers admission can have running at once.
- Fairness: submissions are taken round-robin across submitters, so one agent submitting a hundred tools does
  not delay another agent's one tool behind all of them (REG-ADM-14).
- Bounds: at most `max_queued` submissions wait in all, and at most `max_open_per_submitter` are open (queued or
  being processed) for one submitter. Past either bound, `reserve` raises Busy and nothing is stored, so the
  caller is told to retry later instead of the queue growing without limit (REG-ADM-15).
- Restart: `resume` queues every submission the store holds in an open state (REG-ADM-9).

Settings are deployment settings (03_DESIGN §7). The dev and test setting is 20 concurrent submitters; the
design goal is 100, to be load-tested separately.
"""

from __future__ import annotations

import collections
import logging
import threading
import time

from .admission import OPEN_STATES, AdmissionError

LOG = logging.getLogger("capability.admission_queue")


class AdmissionQueue:
    def __init__(self, admission, *, workers: int = 4, max_queued: int = 200, max_open_per_submitter: int = 20,
                 retry_delays: tuple = (10.0, 30.0, 90.0)):
        """`retry_delays`: when processing fails for a reason that is not the submission's (Docker is down, a
        container could not be removed in time), the submission is tried again after each delay in turn. After
        the last, it stays open in the store and the next start resumes it."""
        if workers < 1 or max_queued < 1 or max_open_per_submitter < 1:
            raise ValueError("workers, max_queued and max_open_per_submitter must be at least 1")
        self._admission = admission
        self.workers, self.max_queued, self.max_open = workers, max_queued, max_open_per_submitter
        self._lock = threading.Condition()
        self._waiting: dict[str, collections.deque] = {}   # submitter -> submission ids, in arrival order
        self._turns: collections.deque = collections.deque()  # submitters with waiting work, round-robin
        self._open: collections.Counter = collections.Counter()  # submitter -> queued + in progress + reserved
        self._known: set[str] = set()  # ids queued or in progress, so nothing is processed twice
        self._queued = 0
        self._busy = 0
        self._retry_delays = tuple(retry_delays)
        self._attempts: collections.Counter = collections.Counter()  # sid -> failed attempts so far
        self._retrying = 0  # submissions waiting out a retry delay
        self._stopping = False
        self._threads: list[threading.Thread] = []

    # -- lifecycle --
    def start(self):
        for number in range(self.workers):
            thread = threading.Thread(target=self._work, name=f"admission-worker-{number}", daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self, timeout: float = 30.0):
        """Stop taking work. Submissions not yet started stay open in the store and are picked up by `resume`."""
        with self._lock:
            self._stopping = True
            self._lock.notify_all()
        deadline = time.monotonic() + timeout  # one deadline for all workers, not one per worker
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def resume(self) -> int:
        """Queue every open submission in the store (after a restart). Bounds do not apply: it is existing work."""
        count = 0
        for state in OPEN_STATES:
            for record in self._admission.store.submissions(state):
                with self._lock:
                    if record["id"] in self._known:
                        continue
                    self._open[record["submitted_by"]] += 1
                    self._push(record["submitted_by"], record["id"])
                count += 1
        return count

    # -- intake --
    def reserve(self, submitter: str):
        """Claim room for one submission, or raise Busy. Call `release` if the submission is then not queued."""
        with self._lock:
            if self._stopping:
                raise AdmissionError("Busy", "the registry is shutting down")
            if self._queued >= self.max_queued:
                raise AdmissionError("Busy", "the admission queue is full")
            if self._open[submitter] >= self.max_open:
                raise AdmissionError("Busy", f"at most {self.max_open} open submissions per submitter")
            self._open[submitter] += 1

    def release(self, submitter: str):
        with self._lock:
            self._drop(submitter)

    def enqueue(self, submitter: str, sid: str):
        """Queue a submission whose room was reserved."""
        with self._lock:
            if sid in self._known:
                self._drop(submitter)  # already queued (a retry of the same submission): give the room back
                return
            self._push(submitter, sid)

    def _push(self, submitter, sid):
        if submitter not in self._waiting or not self._waiting[submitter]:
            self._waiting.setdefault(submitter, collections.deque())
            self._turns.append(submitter)
        self._waiting[submitter].append(sid)
        self._known.add(sid)
        self._queued += 1
        self._lock.notify_all()

    def _drop(self, submitter):
        self._open[submitter] -= 1
        if self._open[submitter] <= 0:
            del self._open[submitter]

    # -- work --
    def _next(self):
        """The next submission, round-robin across submitters. Called with the lock held."""
        submitter = self._turns.popleft()
        sid = self._waiting[submitter].popleft()
        if self._waiting[submitter]:
            self._turns.append(submitter)  # back of the line: everyone else goes first
        else:
            del self._waiting[submitter]
        self._queued -= 1
        return submitter, sid

    def _work(self):
        while True:
            with self._lock:
                while not self._turns and not self._stopping:
                    self._lock.wait()
                if self._stopping:
                    return
                submitter, sid = self._next()
                self._busy += 1
            retry_in = None
            try:
                self._admission.process(sid)
            except Exception:  # one bad submission never stops a worker
                with self._lock:
                    self._attempts[sid] += 1
                    attempt = self._attempts[sid]
                    if attempt <= len(self._retry_delays) and not self._stopping:
                        retry_in = self._retry_delays[attempt - 1]
                LOG.exception("admission worker: submission %s could not be processed (attempt %d); %s", sid,
                              attempt, f"retrying in {retry_in:g}s" if retry_in is not None else
                              "it stays open until the next start")
            finally:
                with self._lock:
                    self._busy -= 1
                    if retry_in is None:
                        self._attempts.pop(sid, None)
                        self._known.discard(sid)
                        self._drop(submitter)
                    else:  # keep its room and its place in _known, so nothing else can queue it meanwhile
                        self._retrying += 1
                        timer = threading.Timer(retry_in, self._retry, (submitter, sid))
                        timer.daemon = True
                        timer.start()
                    self._lock.notify_all()

    def _retry(self, submitter, sid):
        with self._lock:
            self._retrying -= 1
            if self._stopping:  # it stays open in the store; resume picks it up at the next start
                self._known.discard(sid)
                self._drop(submitter)
                self._lock.notify_all()
                return
            self._known.discard(sid)
            self._push(submitter, sid)

    def drain(self, timeout: float | None = None) -> bool:
        """Wait until nothing is queued or in progress. Returns False on timeout."""
        with self._lock:
            return self._lock.wait_for(lambda: self._queued == 0 and self._busy == 0 and self._retrying == 0,
                                       timeout=timeout)

    def stats(self) -> dict:
        with self._lock:
            return {"queued": self._queued, "in_progress": self._busy, "retrying": self._retrying, "submitters_waiting": len(self._turns),
                    "workers": self.workers}
