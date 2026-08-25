from urllib.parse import urlencode


def build_autofill_link(
    admin_base_url: str,
    draft_id: str,
    publication_token: str | None = None,
) -> str:
    params = {"af_draft_id": draft_id}
    if publication_token:
        params["af_publish_token"] = publication_token
    return f"{admin_base_url}?{urlencode(params)}"
