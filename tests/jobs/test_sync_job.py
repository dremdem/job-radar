"""Tests for SyncJob: concurrent starts run the sync only once (docs/queue.md#sync, #72)."""

import threading

import job_radar.jobs.sync_job as jobs_sync_job


def test_concurrent_starts_run_once():
    job = jobs_sync_job.SyncJob()
    release, calls, gate = threading.Event(), [], threading.Barrier(8)
    results = []

    def runner(echo, on_steps):
        calls.append(1)
        release.wait(5)

    def click():
        gate.wait()
        results.append(job.start(runner))

    threads = [threading.Thread(target=click) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1 and job.state == "running"
    release.set()
    for _ in range(100):
        if job.state != "running":
            break
        threading.Event().wait(0.05)
    assert calls == [1] and job.state == "done"


def test_new_sync_starts_after_the_last_one_is_over():
    job = jobs_sync_job.SyncJob()
    done = threading.Event()

    def runner(echo, on_steps):
        done.set()

    assert job.start(runner)
    done.wait(5)
    for _ in range(100):
        if job.state != "running":
            break
        threading.Event().wait(0.05)
    assert job.start(lambda echo, on_steps: None)
