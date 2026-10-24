"""Lifecycle diagnostics with mocked encoders: no output, sockets or credentials."""
from io import BytesIO
import json
from unittest.mock import Mock, patch

from gnb_player import broadcast


TARGET = "rtmps://example.invalid/live/private-ingest-key"
SECRET = "private-ingest-key"


def session_with_events():
    session = broadcast.BroadcastSession()
    session._config = broadcast.BroadcastConfig(mode="stream", target=TARGET, secrets=(SECRET,))
    session._running = True
    events = []
    session.diagnostic.connect(events.append)
    return session, events


def test_each_failed_attempt_and_retry_keeps_redacted_reason():
    session, events = session_with_events()
    problems = [f"attempt {i} failed at {TARGET}; key={SECRET}" for i in range(1, 5)]
    errors = []
    session.error.connect(errors.append)
    with patch.object(session, "_run_once", side_effect=problems) as run_once, \
            patch.object(session._stop, "wait", return_value=False) as wait:
        session._run()
    assert run_once.call_count == 4
    failures = [event for event in events if event["event"] == "attempt_failed"]
    retries = [event for event in events if event["event"] == "retry_scheduled"]
    assert [event["attempt"] for event in failures] == [1, 2, 3, 4]
    assert [event["delay_seconds"] for event in retries] == [1.0, 2.0, 4.0]
    assert [call.args for call in wait.call_args_list] == [(1.0,), (2.0,), (4.0,)]
    assert all(f"attempt {i}" in event["reason"] for i, event in enumerate(failures, 1))
    assert len(errors) == 1 and "attempt 4" in errors[0]
    assert events[-1]["event"] == "finished"
    assert events[-1]["user_stop"] is False
    assert "attempt 4" in events[-1]["reason"]
    payload = json.dumps(events)
    assert TARGET not in payload and SECRET not in payload and "example.invalid" not in payload


def test_start_requested_has_only_safe_configuration_and_no_encoder_execution():
    session = broadcast.BroadcastSession()
    events = []
    session.diagnostic.connect(events.append)
    config = broadcast.BroadcastConfig(mode="stream", target=TARGET, secrets=(SECRET,),
                                       width=640, height=360, fps=15, bitrate_kbps=1200)
    with patch.object(broadcast.BroadcastConfig, "validate"), patch.object(broadcast, "Thread") as thread:
        session.start(config)
    assert thread.return_value.start.call_count == 1
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "start_requested"
    assert {name: event[name] for name in ("mode", "width", "height", "fps", "bitrate_kbps")} == {
        "mode": "stream", "width": 640, "height": 360, "fps": 15, "bitrate_kbps": 1200}
    assert "target" not in event and "ffmpeg_path" not in event
    assert TARGET not in json.dumps(event) and SECRET not in json.dumps(event)


def test_stop_requested_and_finished_identify_user_stop_without_retry():
    session, events = session_with_events()
    session.stop()
    with patch.object(session, "_run_once") as run_once:
        session._run()
    run_once.assert_not_called()
    assert [event["event"] for event in events] == ["stop_requested", "finished"]
    assert all(event["user_stop"] is True for event in events)
    assert events[-1]["counters"]["running"] is False


def test_clean_unrequested_encoder_exit_and_spawn_failure_are_preserved():
    for result in (None, RuntimeError(f"could not open {TARGET} with {SECRET}")):
        session, events = session_with_events()
        session._config = broadcast.BroadcastConfig(mode="stream", target=TARGET,
                                                    secrets=(SECRET,), reconnect_attempts=0)
        options = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
        with patch.object(session, "_run_once", **options):
            session._run()
        failure = next(event for event in events if event["event"] == "attempt_failed")
        assert failure["attempt"] == 1
        assert "encoder" in failure["reason"]
        assert events[-1]["reason"] == failure["reason"]
        assert TARGET not in json.dumps(events) and SECRET not in json.dumps(events)


class FakeListener:
    def getsockname(self):
        return "127.0.0.1", 12345

    def close(self):
        pass


class FakeThread:
    """Run only the in-memory stderr reader, never socket feeder functions."""
    def __init__(self, target, name, **kwargs):
        self.target = target
        self.name = name
        self.args = kwargs.get("args", ())

    def start(self):
        if self.name == "broadcast-encoder-log":
            self.target()

    def join(self, timeout=None):
        pass


def test_mock_encoder_pid_exit_and_bounded_redacted_stderr():
    session, events = session_with_events()
    session._attempt_number = 2
    process = Mock(pid=9876)
    process.poll.return_value = 1
    process.wait.return_value = 1
    process.stderr = BytesIO((("x" * 3000) + "\n" + f"Failure at {TARGET} key={SECRET}\n").encode())
    with patch.object(session, "_listener", side_effect=[FakeListener(), FakeListener()]), \
            patch.object(broadcast.subprocess, "Popen", return_value=process), \
            patch.object(broadcast, "Thread", FakeThread):
        problem = session._run_once(session._config)
    spawned, exited = events
    assert spawned["event"] == "encoder_spawned" and spawned["pid"] == 9876
    assert exited["event"] == "encoder_exit" and exited["returncode"] == 1
    assert exited["attempt"] == 2
    assert len(exited["stderr_tail"]) <= 1800
    assert exited["local_feed_errors"] == []
    assert problem.startswith("Encoder stopped with an error.")
    assert TARGET not in json.dumps(events) and SECRET not in json.dumps(events)
    process.wait.assert_called_once_with(timeout=8)


def test_diagnostic_recursively_redacts_values_and_bounds_collections():
    session, events = session_with_events()
    session._diagnostic("test", reason=f"{TARGET} {SECRET}",
                        details={"stderr": [f"{SECRET}: " + "z" * 4000] * 30})
    event = events[0]
    assert len(event["details"]["stderr"]) == 12
    assert all(len(line) <= 1800 for line in event["details"]["stderr"])
    assert SECRET not in json.dumps(event) and TARGET not in json.dumps(event)


def test_local_video_send_timeout_is_labelled_without_changing_two_second_timeout():
    session, events = session_with_events()
    client = Mock()
    client.sendall.side_effect = TimeoutError(f"timed out {TARGET} {SECRET}")
    video = FakeListener()
    video.accept = lambda: (client, ("127.0.0.1", 23456))

    class FeedThread(FakeThread):
        def start(self):
            if self.name == "broadcast-video":
                self.target(*self.args)
            else:
                super().start()

    process = Mock(pid=9876)
    process.poll.return_value = 0
    process.wait.return_value = 0
    process.stderr = BytesIO()
    with patch.object(session, "_listener", side_effect=[video, FakeListener()]), \
            patch.object(broadcast.subprocess, "Popen", return_value=process), \
            patch.object(broadcast, "Thread", FeedThread):
        problem = session._run_once(session._config)
    client.settimeout.assert_called_once_with(2.0)
    assert problem.startswith("video feed: timed out")
    exited = next(event for event in events if event["event"] == "encoder_exit")
    assert len(exited["local_feed_errors"]) == 1
    assert exited["local_feed_errors"][0].startswith("video feed: timed out")
    assert SECRET not in json.dumps(events) and TARGET not in json.dumps(events)
