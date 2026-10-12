"""Explicit, offline test doubles. These never open a socket or send mail."""
import copy
import json
from collections import deque
from .models import FetchSnapshot, StageFailure
from .classifier import ClassifierReply
from .notifier import DeliveryResult


class FakeFetcher:
    def __init__(self, tweets=(), *, complete=True, outcomes=()):
        self.tweets = list(tweets)
        self.complete = complete
        self.outcomes = deque(outcomes)
        self.calls = []

    def fetch_latest(self, *, limit):
        self.calls.append(limit)
        value = self.outcomes.popleft() if self.outcomes else FetchSnapshot(self.tweets, self.complete)
        if isinstance(value, BaseException):
            raise value
        return copy.deepcopy(value)


class FakeClassifier:
    def __init__(self, classifications=None, *, outcomes=()):
        self.classifications = classifications or {}
        self.outcomes = deque(outcomes)
        self.calls = []

    def classify(self, tweet, *, now, timezone):
        self.calls.append(copy.deepcopy(tweet))
        value = self.outcomes.popleft() if self.outcomes else self.classifications.get(tweet["id"])
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise StageFailure(retryable=False)
        if isinstance(value, ClassifierReply):
            return value
        if isinstance(value, str):
            return ClassifierReply(value)
        return ClassifierReply(json.dumps(value, ensure_ascii=False, allow_nan=False))


class FakeNotifier:
    def __init__(self, *, outcomes=()):
        self.outcomes = deque(outcomes)
        self.calls = []

    def send(self, notification):
        self.calls.append(copy.deepcopy(notification))
        value = self.outcomes.popleft() if self.outcomes else DeliveryResult("SENT")
        if isinstance(value, BaseException):
            raise value
        return value
