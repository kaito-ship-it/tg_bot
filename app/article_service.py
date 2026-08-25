from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from xml.etree import ElementTree

import requests
from lxml import html as lxml_html
from PIL import Image, ImageOps
from pypdf import PdfReader
from trafilatura import extract, extract_metadata, html2txt

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\[\]()]+", re.IGNORECASE)
MAX_HTML_BYTES = 6 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_DOCUMENT_TEXT_CHARS = 120_000
MAX_DOCX_XML_BYTES = 20 * 1024 * 1024
GOV_DOCUMENT_PATH_RE = re.compile(
    r"^/memleket/entities/[^/]+/documents/details/(?P<id>\d+)/?$",
    re.IGNORECASE,
)
DOCUMENT_CONTENT_TYPES = (
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/136 Safari/537.36 tg2site/1.1"
)


class ArticleExtractionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExtractedArticle:
    url: str
    title: str
    text: str
    image_url: str | None = None


def extract_first_url(text: str) -> str | None:
    match = URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip('.,;:!?)]}»"')


def is_link_only_post(text: str, url: str) -> bool:
    remainder = text.replace(url, "", 1)
    remainder = re.sub(r"[\s\-—–:;,.!?«»()\[\]{}]+", "", remainder)
    return len(remainder) <= 20


def _validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ArticleExtractionError("Only public HTTP(S) links are supported")
    hostname = parsed.hostname.casefold()
    if hostname == "localhost" or hostname.endswith(".local"):
        raise ArticleExtractionError("Local addresses are not allowed")
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or 443)
        }
    except socket.gaierror as exc:
        raise ArticleExtractionError(
            f"Cannot resolve article host: {hostname}"
        ) from exc
    if not addresses:
        raise ArticleExtractionError(f"Cannot resolve article host: {hostname}")
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise ArticleExtractionError(
                "Private or reserved addresses are not allowed"
            )


def _download(
    url: str,
    *,
    expected_content_prefix: str | tuple[str, ...],
    max_bytes: int,
    request_headers: dict[str, str] | None = None,
) -> tuple[bytes, str, str]:
    session = requests.Session()
    current_url = url
    try:
        for _ in range(6):
            _validate_public_url(current_url)
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,image/*;q=0.8,*/*;q=0.5",
            }
            if request_headers:
                headers.update(request_headers)
            response = session.get(
                current_url,
                headers=headers,
                timeout=(8, 25),
                stream=True,
                allow_redirects=False,
            )
            try:
                if response.is_redirect or response.is_permanent_redirect:
                    location = response.headers.get("Location")
                    if not location:
                        raise ArticleExtractionError("Redirect without Location header")
                    current_url = urljoin(current_url, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                content_type = content_type.strip().casefold()
                if not content_type.startswith(expected_content_prefix):
                    raise ArticleExtractionError(
                        f"Unexpected content type: {content_type or 'unknown'}"
                    )
                declared_size = int(response.headers.get("Content-Length", "0") or 0)
                if declared_size > max_bytes:
                    raise ArticleExtractionError("Remote content is too large")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ArticleExtractionError("Remote content is too large")
                    chunks.append(chunk)
                return b"".join(chunks), content_type, current_url
            finally:
                response.close()
        raise ArticleExtractionError("Too many redirects")
    except requests.RequestException as exc:
        raise ArticleExtractionError(
            f"Could not download {current_url}: {exc}"
        ) from exc
    finally:
        session.close()


def parse_article_html(html: str | bytes, url: str) -> ExtractedArticle:
    metadata = extract_metadata(html, default_url=url)
    title = _extract_semantic_html_title(html)
    if not title:
        title = (getattr(metadata, "title", None) or "").strip()
    image = (getattr(metadata, "image", None) or "").strip() or None
    text = (
        extract(
            html,
            url=url,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
            output_format="txt",
        )
        or ""
    ).strip()
    description = (getattr(metadata, "description", None) or "").strip()
    if not text:
        text = description
    if not title and text:
        title = text.splitlines()[0].strip()
    if not title or not text:
        raise ArticleExtractionError(
            "The page does not contain an extractable title and text"
        )
    return ExtractedArticle(
        url=url,
        title=title,
        text=text,
        image_url=urljoin(url, image) if image else None,
    )


def _extract_semantic_html_title(html: str | bytes) -> str:
    try:
        document = lxml_html.fromstring(html)
    except (TypeError, ValueError, lxml_html.ParserError):
        return ""

    meta_candidates = document.xpath(
        '//meta[@property="og:title"]/@content | //meta[@name="twitter:title"]/@content'
    )
    class_tokens = (
        "article-title",
        "news-title",
        "entry-title",
        "post-title",
        "page-title",
    )
    class_condition = " or ".join(
        f'contains(concat(" ", normalize-space(@class), " "), " {token} ")'
        for token in class_tokens
    )
    semantic_nodes = document.xpath(
        f"//h1[{class_condition}] | //h2[{class_condition}]"
    )
    semantic_candidates = [" ".join(node.itertext()) for node in semantic_nodes]

    for candidate in [*meta_candidates, *semantic_candidates]:
        candidate = " ".join(str(candidate).split()).strip()
        if 8 <= len(candidate) <= 500:
            return candidate
    return ""


def _normalize_document_text(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)[:MAX_DOCUMENT_TEXT_CHARS].strip()


def _extract_docx_text(document_bytes: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(document_bytes)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise ArticleExtractionError("DOCX document XML is too large")
            xml_bytes = archive.read(info)
        root = ElementTree.fromstring(xml_bytes)
    except (
        KeyError,
        OSError,
        ValueError,
        zipfile.BadZipFile,
        ElementTree.ParseError,
    ) as exc:
        raise ArticleExtractionError("The DOCX file could not be decoded") from exc

    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(namespace + "p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == namespace + "t":
                parts.append(node.text or "")
            elif node.tag == namespace + "tab":
                parts.append(" ")
            elif node.tag == namespace + "br":
                parts.append("\n")
        line = "".join(parts).strip()
        if line:
            paragraphs.append(line)
    text = _normalize_document_text("\n".join(paragraphs))
    if not text:
        raise ArticleExtractionError("The DOCX file does not contain extractable text")
    return text


def _extract_pdf_text(document_bytes: bytes) -> tuple[str, str | None]:
    try:
        reader = PdfReader(BytesIO(document_bytes), strict=False)
        paragraphs: list[str] = []
        total = 0
        for page in reader.pages[:250]:
            page_text = (page.extract_text() or "").strip()
            if not page_text:
                continue
            paragraphs.append(page_text)
            total += len(page_text)
            if total >= MAX_DOCUMENT_TEXT_CHARS:
                break
        text = _normalize_document_text("\n\n".join(paragraphs))
        metadata_title = (getattr(reader.metadata, "title", None) or "").strip()
    except Exception as exc:
        raise ArticleExtractionError("The PDF file could not be decoded") from exc
    if not text:
        raise ArticleExtractionError(
            "The PDF does not contain extractable text; it may require OCR"
        )
    return text, metadata_title or None


def _document_title_from_url(url: str) -> str:
    stem = Path(unquote(urlparse(url).path)).stem
    return re.sub(r"[_-]+", " ", stem).strip() or "Документ"


def _parse_direct_document(
    document_bytes: bytes, content_type: str, url: str
) -> ExtractedArticle:
    if content_type.startswith("application/pdf"):
        text, metadata_title = _extract_pdf_text(document_bytes)
        title = metadata_title or _document_title_from_url(url)
    else:
        text = _extract_docx_text(document_bytes)
        title = _document_title_from_url(url)
    return ExtractedArticle(url=url, title=title, text=text)


def parse_gov_document_payload(
    payload: dict[str, object], url: str, document_text: str = ""
) -> ExtractedArticle:
    title = str(payload.get("title") or "").strip()
    content_html = str(payload.get("content") or "").strip()
    content_text = (
        _normalize_document_text(html2txt(content_html)) if content_html else ""
    )
    text = _normalize_document_text(document_text) or content_text or title
    if not title or not text:
        raise ArticleExtractionError("The gov.kz document has no title or text")
    return ExtractedArticle(url=url, title=title, text=text)


def _gov_document_api_details(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if (parsed.hostname or "").casefold() not in {"gov.kz", "www.gov.kz"}:
        return None
    match = GOV_DOCUMENT_PATH_RE.match(parsed.path.rstrip("/") + "/")
    if not match:
        return None
    lang = parse_qs(parsed.query).get("lang", ["ru"])[0].casefold()
    if lang not in {"ru", "kk", "en"}:
        lang = "ru"
    api_url = urljoin(url, f"/api/v1/public/content-manager/documents/{match['id']}")
    return api_url, lang


def _fetch_gov_document_sync(url: str, api_url: str, lang: str) -> ExtractedArticle:
    payload_bytes, _, _ = _download(
        api_url,
        expected_content_prefix="application/json",
        max_bytes=MAX_HTML_BYTES,
        request_headers={"Accept": "application/json", "Accept-Language": lang},
    )
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArticleExtractionError("gov.kz returned invalid document data") from exc
    if not isinstance(payload, dict):
        raise ArticleExtractionError("gov.kz returned invalid document data")

    document_text = ""
    full_text = payload.get("full_text")
    if isinstance(full_text, list):
        for file_info in full_text:
            if not isinstance(file_info, dict):
                continue
            file_path = str(file_info.get("document") or "").strip()
            if not file_path.lower().endswith((".docx", ".pdf")):
                continue
            file_url = urljoin(url, file_path)
            try:
                file_bytes, content_type, final_file_url = _download(
                    file_url,
                    expected_content_prefix=DOCUMENT_CONTENT_TYPES,
                    max_bytes=MAX_DOCUMENT_BYTES,
                )
                document_text = _parse_direct_document(
                    file_bytes, content_type, final_file_url
                ).text
                break
            except ArticleExtractionError as exc:
                logger.warning(
                    "Could not extract gov.kz attachment %s: %s", file_url, exc
                )

    return parse_gov_document_payload(payload, url, document_text)


def _fetch_article_sync(url: str) -> ExtractedArticle:
    gov_details = _gov_document_api_details(url)
    if gov_details:
        return _fetch_gov_document_sync(url, *gov_details)

    content_bytes, content_type, final_url = _download(
        url,
        expected_content_prefix=("text/html", *DOCUMENT_CONTENT_TYPES),
        max_bytes=MAX_DOCUMENT_BYTES,
    )
    if content_type.startswith("text/html"):
        return parse_article_html(content_bytes, final_url)
    return _parse_direct_document(content_bytes, content_type, final_url)


async def fetch_article(url: str) -> ExtractedArticle:
    return await asyncio.to_thread(_fetch_article_sync, url)


def _download_image_sync(image_url: str, target: Path) -> str:
    image_bytes, _, _ = _download(
        image_url, expected_content_prefix="image/", max_bytes=MAX_IMAGE_BYTES
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(BytesIO(image_bytes)) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((2400, 2400))
            if image.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            image.save(target, format="JPEG", quality=90, optimize=True)
    except (OSError, ValueError) as exc:
        raise ArticleExtractionError("The article image could not be decoded") from exc
    return target.name


async def download_article_image(image_url: str, target: Path) -> str:
    return await asyncio.to_thread(_download_image_sync, image_url, target)
