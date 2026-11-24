"""Thread-safe counters for LLM usage."""
import threading


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._calls_total = 0
        self._errors_total = 0
        self._in_flight = 0
        self._max_in_flight = 0

    def call_started(self):
        with self._lock:
            self._calls_total += 1
            self._in_flight += 1
            if self._in_flight > self._max_in_flight:
                self._max_in_flight = self._in_flight

    def call_finished(self):
        with self._lock:
            self._in_flight -= 1

    def record_error(self):
        with self._lock:
            self._errors_total += 1

    def snapshot(self):
        with self._lock:
            return {
                "llm_calls_total": self._calls_total,
                "llm_errors_total": self._errors_total,
                "max_concurrent_llm_calls": self._max_in_flight,
            }
