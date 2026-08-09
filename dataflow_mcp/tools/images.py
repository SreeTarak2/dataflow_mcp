"""Contest image pipeline MCP tools (missing/broken banners, cover prompts)."""

import urllib.request
from typing import Any, Dict
from urllib.error import HTTPError, URLError

from dataflow_mcp.core import (
    mcp,
    logger,
    check_rate_limit,
    update_metrics,
    DEFAULT_COLLECTION,
    _json_safe,
    _build_broken_image_card,
    _build_formatted_broken_image_card,
    _build_cover_image_prompt,
)
from tools.data_manager import DataManager


@mcp.tool()
def get_contests_missing_images(
    batch_size: int = 10,
    skip: int = 0,
) -> Dict[str, Any]:
    """
    Fetch contests where the primary image URL is missing or empty.

    Use this to identify documents that need AI-generated replacement banners.
    """
    client_id = "get_contests_missing_images"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        filter_dict = {
            "$or": [
                {"image": {"$exists": False}},
                {"image": None},
                {"image.primary": {"$exists": False}},
                {"image.primary": None},
                {"image.primary.url": {"$exists": False}},
                {"image.primary.url": None},
                {"image.primary.url": ""},
            ]
        }

        result = DataManager.read_data(
            collection_name=DEFAULT_COLLECTION,
            filter_query=filter_dict,
            limit=min(max(int(batch_size), 1), 100),
            skip=max(int(skip), 0),
        )

        if not result.get("success"):
            update_metrics(False)
            return result

        data = result.get("data", [])
        update_metrics(True)
        return {
            "success": True,
            "count": len(data),
            "total": result.get("total", 0),
            "skip": result.get("skip", skip),
            "limit": result.get("limit", batch_size),
            "contests": _json_safe(data),
        }

    except Exception as e:
        logger.error(f"Error in get_contests_missing_images: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def get_contests_with_broken_images(
    batch_size: int = 10,
    skip: int = 0,
) -> Dict[str, Any]:
    """
    Fetch contests whose image.primary.status is marked as 'broken'.

    Returns the contests so the chatbot can display or re-generate banners.
    """
    client_id = "get_contests_with_broken_images"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        filter_dict = {"image.primary.status": "broken"}

        result = DataManager.read_data(
            collection_name=DEFAULT_COLLECTION,
            filter_query=filter_dict,
            limit=min(max(int(batch_size), 1), 500),
            skip=max(int(skip), 0),
        )

        if not result.get("success"):
            update_metrics(False)
            return result

        data = result.get("data", [])
        cards = [_build_broken_image_card(contest) for contest in data]
        formatted_cards = [
            _build_formatted_broken_image_card(card, contest) for card, contest in zip(cards, data)
        ]
        update_metrics(True)
        return {
            "success": True,
            "count": len(data),
            "total": result.get("total", 0),
            "skip": result.get("skip", skip),
            "limit": result.get("limit", batch_size),
            "contest_cards": cards,
            "formatted_cards": formatted_cards,
            "contests": _json_safe(data),
        }

    except Exception as e:
        logger.error(f"Error in get_contests_with_broken_images: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def generate_cover_prompt_for_contest(contest_id: str) -> Dict[str, Any]:
    """
    Generate a premium image-generation prompt for one contest by ID.

    This returns a single prompt string that can be fed into any image model
    when the original contest banner is missing.
    """
    client_id = "generate_cover_prompt_for_contest"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        doc_result = DataManager.get_document(DEFAULT_COLLECTION, contest_id)
        if not doc_result.get("success"):
            update_metrics(False)
            return doc_result

        contest = doc_result.get("data", {})
        prompt = _build_cover_image_prompt(contest)

        update_metrics(True)
        return {
            "success": True,
            "contest_id": contest_id,
            "title": contest.get("title"),
            "image_prompt": prompt,
        }

    except Exception as e:
        logger.error(f"Error in generate_cover_prompt_for_contest: {e}")
        update_metrics(False)
        return {"success": False, "error": "An error occurred"}


@mcp.tool()
def verify_image_urls(
    batch_size: int = 50,
    skip: int = 0,
    user_agent: str = "DataFlow-MCP/1.0",
) -> Dict[str, Any]:
    """
    Verify image URLs for contests and mark broken images in the database.

    This tool scans contests that have an `image.primary.url`, performs a
    lightweight HTTP HEAD/GET to verify reachability, and updates
    `image.primary.status` to 'active' or 'broken'. It returns a report.
    """
    client_id = "verify_image_urls"

    if not check_rate_limit(client_id):
        return {"success": False, "error": "Rate limit exceeded"}

    try:
        update_metrics(False)

        # Fetch contests that include an image URL
        filter_query = {"image.primary.url": {"$exists": True, "$ne": None, "$ne": ""}}
        result = DataManager.read_data(
            collection_name=DEFAULT_COLLECTION,
            filter_query=filter_query,
            limit=min(max(int(batch_size), 1), 500),
            skip=max(int(skip), 0),
        )

        if not result.get("success"):
            update_metrics(False)
            return result

        contests = result.get("data", [])
        report = {"checked": 0, "active": 0, "broken": 0, "details": []}

        for contest in contests:
            report["checked"] += 1
            contest_id = contest.get("_id")
            url = None
            try:
                image = contest.get("image") or {}
                primary = image.get("primary") if isinstance(image, dict) else None
                url = primary.get("url") if isinstance(primary, dict) else None
            except Exception:
                url = None

            if not url:
                report["details"].append({"contest_id": contest_id, "status": "no-url"})
                continue

            # Attempt a HEAD request first; fall back to GET
            req = urllib.request.Request(url, headers={"User-Agent": user_agent}, method="HEAD")
            status_ok = False
            try:
                with urllib.request.urlopen(req, timeout=6) as resp:
                    if resp.status == 200:
                        status_ok = True
            except HTTPError:
                status_ok = False
            except URLError:
                status_ok = False
            except Exception:
                status_ok = False

            # If HEAD failed, try GET as some servers don't support HEAD
            if not status_ok:
                try:
                    req2 = urllib.request.Request(
                        url, headers={"User-Agent": user_agent}, method="GET"
                    )
                    with urllib.request.urlopen(req2, timeout=6) as resp2:
                        if resp2.status == 200:
                            status_ok = True
                except Exception:
                    status_ok = False

            # Update document with status
            update_data = {
                "image": {"primary": {"url": url, "status": ("active" if status_ok else "broken")}}
            }
            try:
                # Use DataManager.update_document to safely validate and update
                update_result = DataManager.update_document(
                    DEFAULT_COLLECTION, contest_id, update_data
                )
                if update_result.get("success"):
                    if status_ok:
                        report["active"] += 1
                    else:
                        report["broken"] += 1
                    report["details"].append(
                        {
                            "contest_id": contest_id,
                            "url": url,
                            "status": ("active" if status_ok else "broken"),
                        }
                    )
                else:
                    report["details"].append(
                        {
                            "contest_id": contest_id,
                            "url": url,
                            "status": "update-failed",
                            "error": update_result.get("error"),
                        }
                    )
            except Exception as e:
                report["details"].append(
                    {
                        "contest_id": contest_id,
                        "url": url,
                        "status": "update-exception",
                        "error": str(e),
                    }
                )

        update_metrics(True)
        return {"success": True, "report": report}

    except Exception as e:
        logger.error(f"Error in verify_image_urls: {e}")
        update_metrics(False)
        return {"success": False, "error": str(e)}
