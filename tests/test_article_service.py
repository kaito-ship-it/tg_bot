from io import BytesIO
import zipfile

import pytest

from app.article_service import (
    ArticleExtractionError,
    _extract_docx_text,
    _gov_document_api_details,
    extract_first_url,
    is_link_only_post,
    parse_article_html,
    parse_gov_document_payload,
    _validate_public_url,
)


def test_detects_link_only_post() -> None:
    text = "https://example.com/news/42"
    url = extract_first_url(text)
    assert url == text
    assert is_link_only_post(text, url)
    assert not is_link_only_post(f"Большой самостоятельный текст новости {text}", url)


def test_extracts_article_metadata_text_and_image() -> None:
    html = """
    <html><head>
      <title>Запасной заголовок</title>
      <meta property="og:title" content="Полный заголовок новости">
      <meta property="og:image" content="/images/cover.jpg">
      <meta name="description" content="Описание новости">
    </head><body><main><article>
      <h1>Полный заголовок новости</h1>
      <p>Первый достаточно длинный абзац основной части тестовой новости,
      содержащий полезные сведения для читателя и редактора публикации.</p>
      <p>Второй достаточно длинный абзац подтверждает, что извлекается именно
      содержимое статьи, а не меню, реклама или служебный текст страницы.</p>
    </article></main></body></html>
    """
    article = parse_article_html(html, "https://example.com/news/42")
    assert article.title == "Полный заголовок новости"
    assert "Первый достаточно длинный абзац" in article.text
    assert article.image_url == "https://example.com/images/cover.jpg"


def test_semantic_article_title_wins_over_generic_h1() -> None:
    html = """
    <html><head><title>Название статьи — Название сайта</title></head><body>
      <h1>Большая международная конференция — 2026</h1>
      <h2 class="page-title">Новое исследование месторождений Казахстана</h2>
      <article><p>Исследователи представили подробные результаты изучения
      месторождений и рассказали о дальнейших работах.</p></article>
    </body></html>
    """
    article = parse_article_html(html, "https://example.com/article")
    assert article.title == "Новое исследование месторождений Казахстана"


def test_private_url_is_rejected() -> None:
    with pytest.raises(ArticleExtractionError):
        _validate_public_url("http://127.0.0.1/admin")


def test_gov_document_url_maps_to_public_api() -> None:
    url = (
        "https://www.gov.kz/memleket/entities/mps/documents/details/"
        "1053929?lang=ru"
    )
    assert _gov_document_api_details(url) == (
        "https://www.gov.kz/api/v1/public/content-manager/documents/1053929",
        "ru",
    )
    assert _gov_document_api_details("https://example.com/documents/details/1") is None


def test_parses_gov_document_payload_with_attachment_text() -> None:
    article = parse_gov_document_payload(
        {
            "title": "Приказ о программе управления фондом недр",
            "content": "<p>Краткое описание приказа</p>",
        },
        "https://www.gov.kz/memleket/entities/mps/documents/details/1053929",
        "Полный текст документа о выдаче лицензии на добычу.",
    )
    assert article.title == "Приказ о программе управления фондом недр"
    assert article.text == "Полный текст документа о выдаче лицензии на добычу."
    assert article.image_url is None


def test_extracts_text_from_docx_bytes() -> None:
    document_xml = '''<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:p><w:r><w:t>Первый абзац</w:t></w:r></w:p>
        <w:p><w:r><w:t>Второй абзац о недропользовании</w:t></w:r></w:p>
      </w:body>
    </w:document>'''.encode("utf-8")
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", document_xml)

    assert _extract_docx_text(output.getvalue()) == (
        "Первый абзац\nВторой абзац о недропользовании"
    )
