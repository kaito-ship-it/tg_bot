import asyncio
from pathlib import Path

from app import draft_builder
from app.article_service import ExtractedArticle
from app.draft_builder import (
    build_draft,
    clean_text_for_editor,
    extract_album_text,
    extract_title,
    guess_category_id,
    match_category_id,
)


class Message:
    def __init__(
        self, raw_text: str, message_id: int = 1, photo=False, media=None
    ) -> None:
        self.raw_text = raw_text
        self.id = message_id
        self.photo = photo
        self.media = media


class Client:
    async def download_media(self, message, file: str):
        del message
        await asyncio.to_thread(Path(file).write_bytes, b"jpeg")
        return file


def test_extract_title_uses_first_non_empty_line() -> None:
    assert extract_title("\n Заголовок \nТекст") == "Заголовок"


def test_extract_title_is_limited() -> None:
    title = extract_title("слово " * 60)
    assert len(title) <= 255
    assert title.endswith("…")
    assert not title.endswith("сл…")


def test_extract_title_uses_complete_first_sentence() -> None:
    text = (
        "В Китае мини-сериал собрал более четверти миллиарда просмотров. "
        "Второе предложение не относится к заголовку."
    )
    assert extract_title(text) == (
        "В Китае мини-сериал собрал более четверти миллиарда просмотров."
    )


def test_category_is_selected_only_for_one_match() -> None:
    assert guess_category_id("Получена лицензия на добычу") == 35
    assert guess_category_id("Новая цифровая технология") == 9
    assert guess_category_id("Геология и экология проекта") == 35
    assert guess_category_id("Обычная новость") == 35
    assert match_category_id("Обычная новость") is None


def test_ai_and_digital_article_is_technology() -> None:
    text = (
        "В Китае набирает популярность цифровая актриса. "
        "Героиня сгенерирована алгоритмами искусственного интеллекта."
    )
    assert guess_category_id(text) == 9
    assert guess_category_id("ИИ-актриса стала популярной") == 9
    assert guess_category_id("Новое направление геологии опубликовано") == 13


def test_album_text_is_unique_and_keeps_order() -> None:
    messages = [Message("Подпись"), Message(""), Message("Подпись"), Message("Текст")]
    assert extract_album_text(messages) == "Подпись\n\nТекст"


def test_clean_text_keeps_text_until_rules_are_known() -> None:
    assert clean_text_for_editor("  Текст\n") == "Текст"


def test_clean_text_removes_duplicate_title_and_trailing_promotions() -> None:
    text = """
    Новое месторождение открыли в Казахстане

    Геологи завершили исследование участка и подтвердили запасы сырья.

    Читайте также: другие новости
    Подписывайтесь на наш Telegram-канал
    https://t.me/example
    """
    assert (
        clean_text_for_editor(text, "Новое месторождение открыли в Казахстане")
        == "Геологи завершили исследование участка и подтвердили запасы сырья."
    )


def test_clean_text_preserves_embedded_links_and_nonduplicate_lead() -> None:
    text = (
        "Подробности опубликованы на https://example.com/report.\n"
        "Это второй полезный абзац."
    )
    assert clean_text_for_editor(text, "Другой заголовок") == text


def test_build_draft_uses_first_album_photo(tmp_path) -> None:
    draft = asyncio.run(
        build_draft(
            [
                Message("Лицензия на добычу", 10),
                Message("", 11, photo=True),
                Message("", 12, photo=True),
            ],
            Client(),
            tmp_path,
            "a" * 32,
        )
    )
    assert draft.category_id == 35
    assert draft.source_message_id == 12
    assert draft.photo_filename == f"{'a' * 32}.jpg"
    assert (tmp_path / draft.photo_filename).read_bytes() == b"jpeg"


def test_build_draft_extracts_link_only_article(monkeypatch, tmp_path) -> None:
    async def fake_fetch_article(url: str) -> ExtractedArticle:
        return ExtractedArticle(
            url=url,
            title="Полный заголовок статьи",
            text="Технологии помогают автоматизировать публикацию новостей.",
            image_url="https://example.com/cover.webp",
        )

    async def fake_download_article_image(image_url: str, target) -> str:
        assert image_url == "https://example.com/cover.webp"
        target.write_bytes(b"jpeg")
        return target.name

    monkeypatch.setattr(draft_builder, "fetch_article", fake_fetch_article)
    monkeypatch.setattr(
        draft_builder, "download_article_image", fake_download_article_image
    )
    draft = asyncio.run(
        build_draft(
            [Message("https://example.com/news/42", 20)],
            Client(),
            tmp_path,
            "d" * 32,
        )
    )

    assert draft.title == "Полный заголовок статьи"
    assert draft.text.startswith("Технологии помогают")
    assert draft.category_id == 9
    assert draft.source_url == "https://example.com/news/42"
    assert draft.photo_filename == f"{'d' * 32}.jpg"


def test_build_draft_falls_back_to_telegram_link_preview(monkeypatch, tmp_path) -> None:
    class WebPage:
        title = "Заголовок из Telegram-превью"
        description = "Описание статьи о геологии из Telegram-превью."
        photo = object()

    class Media:
        webpage = WebPage()

    async def failed_fetch(url: str):
        del url
        raise draft_builder.ArticleExtractionError("403 Forbidden")

    monkeypatch.setattr(draft_builder, "fetch_article", failed_fetch)
    draft = asyncio.run(
        build_draft(
            [Message("https://blocked.example/news", 30, media=Media())],
            Client(),
            tmp_path,
            "e" * 32,
        )
    )

    assert draft.title == "Заголовок из Telegram-превью"
    assert draft.text.startswith("Описание статьи")
    assert draft.category_id == 13
    assert draft.source_url == "https://blocked.example/news"
    assert draft.photo_filename == f"{'e' * 32}.jpg"


def test_build_draft_generates_cover_only_when_no_photo(tmp_path) -> None:
    class FakeImageService:
        calls = 0

        async def generate_cover(self, *, title: str, news_text: str, target):
            self.calls += 1
            assert title == "Новая цифровая технология"
            assert news_text == "Новая цифровая технология"
            target.write_bytes(b"generated-jpeg")
            return target.name

    image_service = FakeImageService()
    draft = asyncio.run(
        build_draft(
            [Message("Новая цифровая технология", 40)],
            Client(),
            tmp_path,
            "f" * 32,
            image_service,
        )
    )

    assert image_service.calls == 1
    assert draft.photo_filename == f"{'f' * 32}.jpg"
    assert draft.photo_is_generated is True


def test_build_draft_does_not_generate_when_telegram_photo_exists(tmp_path) -> None:
    class FakeImageService:
        calls = 0

        async def generate_cover(self, **kwargs):
            del kwargs
            self.calls += 1
            raise AssertionError("generation should not be called")

    image_service = FakeImageService()
    draft = asyncio.run(
        build_draft(
            [Message("Лицензия на добычу", 50, photo=True)],
            Client(),
            tmp_path,
            "1" * 32,
            image_service,
        )
    )

    assert image_service.calls == 0
    assert draft.photo_is_generated is False


def test_build_draft_uses_ai_classifier_for_uncertain_category(tmp_path) -> None:
    class FakeClassifier:
        calls = 0

        async def classify(self, *, title: str, text: str) -> int:
            self.calls += 1
            assert title == "Исследование структуры земной коры"
            assert text == title
            return 13

    classifier = FakeClassifier()
    draft = asyncio.run(
        build_draft(
            [Message("Исследование структуры земной коры", 60)],
            Client(),
            tmp_path,
            "2" * 32,
            None,
            classifier,
        )
    )

    assert classifier.calls == 1
    assert draft.category_id == 13
