"""Unit tests — atomic structured logging.

Parallel review nodes share one ``Log``; a lost write or an interleaved line
would corrupt both the diagnostic stream and the structured LLM-call records, so
each line must be written as a single atomic write.
"""

import io
import json
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


# --- every LLM call carries its structured fields in every run (T135, FR-019) ---


def _fields(line: str) -> dict:
    """The JSON object that ends a structured log line."""
    return json.loads(line[line.rindex(" {") + 1 :])


def test_llm_call_line_is_structured_without_verbose():
    stream = io.StringIO()
    Log(stream=stream).llm_call(
        "gpt-4o-mini", 1234.4, prompt_tokens=120, completion_tokens=30, total_tokens=150
    )
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert lines[0] == (
        '[info] LLM call gpt-4o-mini (1234 ms) {"completion_tokens": 30, "latency_ms": 1234.4, '
        '"model": "gpt-4o-mini", "prompt_tokens": 120, "total_tokens": 150}'
    )


def test_llm_call_line_without_usage_carries_null_token_fields():
    stream = io.StringIO()
    Log(stream=stream).llm_call("gpt-4o-mini", 7.0)
    assert _fields(stream.getvalue()) == {
        "completion_tokens": None,
        "latency_ms": 7.0,
        "model": "gpt-4o-mini",
        "prompt_tokens": None,
        "total_tokens": None,
    }


def test_llm_call_line_is_the_same_with_and_without_verbose():
    plain, verbose = io.StringIO(), io.StringIO()
    for stream, flag in ((plain, False), (verbose, True)):
        Log(stream=stream, verbose=flag).llm_call("m", 5.0, prompt_tokens=1, total_tokens=2)
    assert plain.getvalue() == verbose.getvalue()


def test_failed_llm_call_line_is_structured_without_verbose():
    stream = io.StringIO()
    Log(stream=stream).llm_call("m", 12.0, error="openai 401: bad")
    line = stream.getvalue()
    assert line.startswith("[error] LLM call m (12 ms) failed: openai 401: bad {")
    fields = _fields(line)
    assert fields["total_tokens"] is None
    assert "error" not in fields


def test_other_lines_stay_plain_without_verbose():
    """Guard: only LLM-call lines always carry their fields."""
    stream = io.StringIO()
    Log(stream=stream).info("Report written to r.md", report_path="r.md")
    assert stream.getvalue() == "[info] Report written to r.md\n"