from app.link_builder import build_autofill_link
from app.state import ProcessingState


def test_short_link_contains_only_draft_id() -> None:
    assert build_autofill_link(
        "https://dev.nedra.kz/admin/news", "abc123"
    ) == "https://dev.nedra.kz/admin/news?af_draft_id=abc123"


def test_auto_publish_link_contains_confirmation_token() -> None:
    assert build_autofill_link(
        "https://dev.nedra.kz/admin/news", "abc123", "secure-token"
    ) == (
        "https://dev.nedra.kz/admin/news?af_draft_id=abc123"
        "&af_publish_token=secure-token"
    )


def test_state_never_moves_backwards(tmp_path) -> None:
    state = ProcessingState(tmp_path / "state.json")
    assert state.load_last_processed_id() == 0
    state.save_last_processed_id(20)
    state.save_last_processed_id(10)
    assert state.load_last_processed_id() == 20
