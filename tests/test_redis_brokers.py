from datetime import datetime, timedelta, timezone
import time
from unittest.mock import patch, Mock

import pytest

from spinach.brokers.redis import (
    RedisBroker, RUNNING_JOBS_KEY, PERIODIC_TASKS_HASH_KEY
)
from spinach.job import Job, JobStatus
from spinach.task import Task


@pytest.fixture
def broker():
    broker = RedisBroker()
    broker.namespace = 'tests'
    broker.must_stop_periodicity = 0.01
    broker.flush()
    broker.start()
    yield broker
    broker.stop()
    broker.flush()


# Fixture to get a second broker
broker_2 = broker


def test_redis_flush(broker):
    broker._r.set('tests/foo', b'1')
    broker._r.set('tests2/foo', b'2')
    broker.flush()
    assert broker._r.get('tests/foo') is None
    assert broker._r.get('tests2/foo') == b'2'
    broker._r.delete('tests2/foo')


def test_running_job(broker):
    running_jobs_key = broker._to_namespaced(
        RUNNING_JOBS_KEY.format(broker._id)
    )

    # Non-idempotent job
    job = Job('foo_task', 'foo_queue', datetime.now(timezone.utc), 0)
    broker.enqueue_jobs([job])
    assert broker._r.hget(running_jobs_key, str(job.id)) is None
    broker.get_jobs_from_queue('foo_queue', 1)
    assert broker._r.hget(running_jobs_key, str(job.id)) is None
    # Try to remove it, even if it doesn't exist in running
    broker.remove_job_from_running(job)

    # Idempotent job - get from queue
    job = Job('foo_task', 'foo_queue', datetime.now(timezone.utc), 10)
    broker.enqueue_jobs([job])
    assert broker._r.hget(running_jobs_key, str(job.id)) is None
    broker.get_jobs_from_queue('foo_queue', 1)
    job.status = JobStatus.RUNNING
    assert (
        Job.deserialize(broker._r.hget(running_jobs_key, str(job.id)).decode())
        == job
    )

    # Idempotent job - re-enqueue after job ran with error
    job.retries += 1
    broker.enqueue_jobs([job])
    assert broker._r.hget(running_jobs_key, str(job.id)) is None
    broker.get_jobs_from_queue('foo_queue', 1)
    job.status = JobStatus.RUNNING
    assert (
        Job.deserialize(broker._r.hget(running_jobs_key, str(job.id)).decode())
        == job
    )

    # Idempotent job - job succeeded
    broker.remove_job_from_running(job)
    assert broker._r.hget(running_jobs_key, str(job.id)) is None
    assert broker.get_jobs_from_queue('foo_queue', 1) == []


def test_enqueue_jobs_from_dead_broker(broker, broker_2):
    # Enqueue one idempotent job and one non-idempotent job
    job_1 = Job('foo_task', 'foo_queue', datetime.now(timezone.utc), 0)
    job_2 = Job('foo_task', 'foo_queue', datetime.now(timezone.utc), 10)
    broker.enqueue_jobs([job_1, job_2])

    # Simulate broker starting the jobs
    broker.get_jobs_from_queue('foo_queue', 100)

    # Mark broker as dead, should re-enqueue only the idempotent job
    assert broker_2.enqueue_jobs_from_dead_broker(broker._id) == 1

    # Simulate broker 2 getting jobs from the queue
    job_2.status = JobStatus.RUNNING
    job_2.retries = 1
    assert broker_2.get_jobs_from_queue('foo_queue', 100) == [job_2]

    # Check that a broker can be marked as dead multiple times
    # without duplicating jobs
    assert broker_2.enqueue_jobs_from_dead_broker(broker._id) == 0


def test_detect_dead_broker(broker, broker_2):
    broker_2.enqueue_jobs_from_dead_broker = Mock(return_value=10)

    # Register the first broker
    broker.move_future_jobs()

    # Set the 2nd broker to detect dead brokers after 2 seconds of inactivity
    broker_2.broker_dead_threshold_seconds = 2
    time.sleep(2.1)

    # Detect dead brokers
    broker_2.move_future_jobs()
    broker_2.enqueue_jobs_from_dead_broker.assert_called_once_with(
        broker._id
    )


def test_not_detect_deregistered_broker_as_dead(broker, broker_2):
    broker_2.enqueue_jobs_from_dead_broker = Mock(return_value=10)

    # Register and de-register the first broker
    broker.move_future_jobs()
    broker.stop()

    # Set the 2nd broker to detect dead brokers after 2 seconds of inactivity
    broker_2.broker_dead_threshold_seconds = 2
    time.sleep(2.1)

    # Detect dead brokers
    broker_2.move_future_jobs()
    broker_2.enqueue_jobs_from_dead_broker.assert_not_called()

    # Just so that the fixture can terminate properly
    broker.stop = Mock()


def test_old_periodic_tasks(broker):
    periodic_tasks_hash_key = broker._to_namespaced(PERIODIC_TASKS_HASH_KEY)
    tasks = [
        Task(print, 'foo', 'q1', 0, timedelta(seconds=5)),
        Task(print, 'bar', 'q1', 0, timedelta(seconds=10))
    ]

    broker.register_periodic_tasks(tasks)
    assert broker._number_periodic_tasks == 2
    assert broker._r.hgetall(periodic_tasks_hash_key) == {
        b'foo': b'{"max_retries": 0, "name": "foo", '
                b'"periodicity": 5, "queue": "q1"}',
        b'bar': b'{"max_retries": 0, "name": "bar", '
                b'"periodicity": 10, "queue": "q1"}'
    }

    broker.register_periodic_tasks([tasks[1]])
    assert broker._number_periodic_tasks == 1
    assert broker._r.hgetall(periodic_tasks_hash_key) == {
        b'bar': b'{"max_retries": 0, "name": "bar", '
                b'"periodicity": 10, "queue": "q1"}'
    }


@patch('spinach.brokers.redis.generate_idempotency_token', return_value='42')
def test_idempotency_token(_, broker):
    job_1 = Job('foo_task', 'foo_queue', datetime.now(timezone.utc), 0)
    job_2 = Job('foo_task', 'foo_queue', datetime.now(timezone.utc), 0)
    broker.enqueue_jobs([job_1])
    broker.enqueue_jobs([job_2])

    jobs = broker.get_jobs_from_queue('foo_queue', max_jobs=10)
    job_1.status = JobStatus.RUNNING
    assert jobs == [job_1]
