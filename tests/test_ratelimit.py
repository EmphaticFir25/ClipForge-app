"""Sign-in limits live in the database, so they hold across restarts and across processes."""
from pathlib import Path

from webapp import service as svc
from webapp.service import Service
from webapp.store import Store


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, body):
        self.sent.append(to)


def test_limit_holds_across_two_service_instances_sharing_one_database(tmp_path):
    db = tmp_path / "shared.sqlite3"
    mailer = Recorder()
    a = Service(Store(db), tmp_path, mailer=mailer)
    b = Service(Store(db), tmp_path, mailer=mailer)  # a second worker, same database
    for i in range(svc.LOGIN_REQUESTS_PER_HOUR):
        (a if i % 2 == 0 else b).request_sign_in("loop@example.com", "https://x.test")
    assert len(mailer.sent) == svc.LOGIN_REQUESTS_PER_HOUR
    b.request_sign_in("loop@example.com", "https://x.test")
    a.request_sign_in("loop@example.com", "https://x.test")
    assert len(mailer.sent) == svc.LOGIN_REQUESTS_PER_HOUR  # refused by both, same visible reply


def test_limit_survives_a_restart(tmp_path):
    db = tmp_path / "restart.sqlite3"
    mailer = Recorder()
    first = Service(Store(db), tmp_path, mailer=mailer)
    for _ in range(svc.LOGIN_REQUESTS_PER_HOUR):
        first.request_sign_in("again@example.com", "https://x.test")
    del first
    second = Service(Store(db), tmp_path, mailer=mailer)
    second.request_sign_in("again@example.com", "https://x.test")
    assert len(mailer.sent) == svc.LOGIN_REQUESTS_PER_HOUR


def test_limit_is_per_address(tmp_path):
    mailer = Recorder()
    service = Service(Store(tmp_path / "p.sqlite3"), tmp_path, mailer=mailer)
    for _ in range(svc.LOGIN_REQUESTS_PER_HOUR + 3):
        service.request_sign_in("one@example.com", "https://x.test")
    service.request_sign_in("two@example.com", "https://x.test")
    assert mailer.sent.count("two@example.com") == 1
