"""Unit tests — atomic structured logging.

Parallel review nodes share one ``Log``; a lost write or an interleaved line
would corrupt both the diagnostic stream and the structured LLM-call records, so
each line must be written as a single atomic write.
"""

import io
import threading

from veritas.utils.logging import Log


def test_parallel_lines_never_interleave():
    """8 threads writing 200 lines each: every line survives intact and exactly
    the expected lines appear — no line ever carries two messages."""
    stream = io.StringIO()
    log = Log(stream=stream)
    threads = 8
    lines_per_thread = 200

    def worker(thread_id: int) -> None:
        for i in range(lines_per_thread):
            log.info(f"thread-{thread_id} line-{i:03d}")

    workers = [
        threading.Thread(target=worker, args=(thread_id,))
        for thread_id in range(threads)
    ]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join()

    lines = stream.getvalue().splitlines()
    assert len(lines) == threads * lines_per_thread
    expected = {
        f"[info] thread-{thread_id} line-{i:03d}"
        for thread_id in range(threads)
        for i in range(lines_per_thread)
    }
    assert set(lines) == expected


def test_verbose_structured_line_is_single_line_too():
    """Verbose structured records still land as one atomic line per message."""
    stream = io.StringIO()
    log = Log(stream=stream, verbose=True)
    log.llm_call("m", 12.34, prompt_tokens=5, total_tokens=9)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("[info] LLM call m (12 ms)")
    assert '"prompt_tokens": 5' in lines[0]