import asyncio

from app import category_service
from app.category_service import OpenAICategoryClassifier


def test_openai_category_classifier_parses_allowed_id(monkeypatch) -> None:
    captured = {}

    class Response:
        ok = True
        headers = {}

        def json(self):
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "13"}],
                    }
                ]
            }

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return Response()

    monkeypatch.setattr(category_service.requests, "post", fake_post)
    classifier = OpenAICategoryClassifier(
        api_key="test-key",
        model="gpt-5.4-nano",
    )

    category_id = asyncio.run(
        classifier.classify(
            title="Исследование структуры земной коры",
            text="Учёные представили результаты исследования.",
        )
    )

    assert category_id == 13
    assert captured["url"] == category_service.OPENAI_RESPONSES_ENDPOINT
    assert captured["json"]["model"] == "gpt-5.4-nano"
    assert captured["json"]["reasoning"] == {"effort": "none"}
    assert captured["timeout"] == (10, 60)


def test_openai_category_classifier_retries_rate_limit(monkeypatch) -> None:
    attempts = 0
    delays = []

    class Response:
        headers = {}
        reason = "rate limited"

        def __init__(self, ok: bool, status_code: int) -> None:
            self.ok = ok
            self.status_code = status_code

        def json(self):
            if self.ok:
                return {"output_text": "9"}
            return {"error": {"message": "slow down"}}

    def fake_post(*args, **kwargs):
        nonlocal attempts
        del args, kwargs
        attempts += 1
        return Response(attempts > 1, 200 if attempts > 1 else 429)

    monkeypatch.setattr(category_service.requests, "post", fake_post)
    monkeypatch.setattr(category_service.time, "sleep", delays.append)
    classifier = OpenAICategoryClassifier(
        api_key="test-key",
        model="gpt-5.4-nano",
    )

    category_id = asyncio.run(
        classifier.classify(title="Новая технология", text="Текст новости")
    )

    assert category_id == 9
    assert attempts == 2
    assert delays == [1]
