from __future__ import annotations

import argparse

from app.config import load_settings
from app.draft_store import DraftStore
from app.publication_queue import PublicationQueue


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Return one failed admin publication to the queue"
    )
    parser.add_argument("draft_id")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="retry without the interactive duplicate warning",
    )
    args = parser.parse_args()

    settings = load_settings()
    store = DraftStore(
        settings.drafts_dir,
        settings.photos_dir,
        settings.draft_ttl_hours,
    )
    draft = store.get_without_ttl(args.draft_id)
    queue = PublicationQueue(settings.publication_queue_dir)
    job = queue.get(args.draft_id)
    if draft is None or job is None:
        raise SystemExit("Draft or publication job was not found")
    if job.status != "failed":
        raise SystemExit(f"Publication status is {job.status}, not failed")

    print(f"Draft: {draft.title}")
    print("Check the site first. Retrying an existing publication creates a duplicate.")
    if not args.yes:
        confirmation = input("Type RETRY to return this draft to the queue: ").strip()
        if confirmation != "RETRY":
            raise SystemExit("Cancelled")

    if queue.retry_failed(args.draft_id) is None:
        raise SystemExit("Could not retry the publication")
    print("Publication returned to the pending queue")


if __name__ == "__main__":
    main()
