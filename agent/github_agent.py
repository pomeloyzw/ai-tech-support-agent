"""
GitHub agent
============
Provides tools to search GitHub code and recent commits.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from github import Github, GithubException

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class CodeSearchResult:
    filename: str
    file_path: str
    repository: str
    matched_lines: list[str]
    html_url: str


@dataclass
class CommitResult:
    sha: str
    message: str
    author: str
    committed_at: str
    files_changed: list[str]
    html_url: str


@dataclass
class GitHubFindings:
    code_results: list[CodeSearchResult]
    commit_results: list[CommitResult]
    search_queries_used: list[str]
    total_duration_ms: int


def _search_code_blocking(query: str, file_extension: str) -> list[CodeSearchResult]:
    """Blocking implementation of GitHub code search."""
    if not settings.github_token:
        logger.warning("GitHub token not set. Skipping search.")
        return []

    gh = Github(settings.github_token)
    try:
        repo = gh.get_repo(settings.github_repo)
        full_query = f"{query} extension:{file_extension} repo:{settings.github_repo}"
        
        # We only take the first N results and then stop fetching.
        paginated_results = gh.search_code(full_query)
        results = []
        for i, item in enumerate(paginated_results):
            if i >= settings.github_max_code_results:
                break
                
            # Skip large files (e.g., > 50KB)
            if item.size > 50 * 1024:
                logger.warning("File %s too large (%d bytes), skipping", item.path, item.size)
                continue
                
            try:
                # Fetch file content
                file_content_obj = repo.get_contents(item.path)
                if isinstance(file_content_obj, list):
                    continue
                    
                content_lines = file_content_obj.decoded_content.decode("utf-8").splitlines()
                
                # Naive search for matching lines
                matched_lines_with_context = []
                for idx, line in enumerate(content_lines):
                    if query.lower() in line.lower():
                        start = max(0, idx - settings.github_code_context_lines)
                        end = min(len(content_lines), idx + settings.github_code_context_lines + 1)
                        snippet = "\n".join(f"{j+1}: {content_lines[j]}" for j in range(start, end))
                        matched_lines_with_context.append(snippet)
                        
                results.append(
                    CodeSearchResult(
                        filename=item.name,
                        file_path=item.path,
                        repository=repo.full_name,
                        matched_lines=matched_lines_with_context,
                        html_url=item.html_url,
                    )
                )
            except Exception as e:
                logger.warning("Error fetching content for %s: %s", item.path, e)

        return results
    except GithubException as e:
        logger.warning("GitHub API exception during search_code: %s", e)
        return []


async def search_code(query: str, file_extension: str = "py") -> list[CodeSearchResult]:
    """
    Search the codebase for source code relevant to the issue.
    Runs in asyncio executor to avoid blocking the event loop.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _search_code_blocking, query, file_extension)


def _get_recent_commits_blocking(path_filter: str) -> list[CommitResult]:
    """Blocking implementation of GitHub commit history search."""
    if not settings.github_token:
        logger.warning("GitHub token not set. Skipping commit search.")
        return []

    gh = Github(settings.github_token)
    try:
        repo = gh.get_repo(settings.github_repo)
        since_date = datetime.now() - timedelta(days=settings.github_commit_lookback_days)
        
        # Path must be None if empty string
        kwargs = {"since": since_date}
        if path_filter:
            kwargs["path"] = path_filter
            
        paginated_commits = repo.get_commits(**kwargs)
        results = []
        for i, commit in enumerate(paginated_commits):
            if i >= settings.github_max_commit_results:
                break
                
            try:
                # Truncate file list
                files_changed = [f.filename for f in commit.files][:settings.github_max_files_per_commit]
                
                results.append(
                    CommitResult(
                        sha=commit.sha[:7],
                        message=commit.commit.message.split("\n")[0] if commit.commit.message else "",
                        author=commit.commit.author.name if commit.commit.author else "Unknown",
                        committed_at=commit.commit.author.date.isoformat() if commit.commit.author else "",
                        files_changed=files_changed,
                        html_url=commit.html_url,
                    )
                )
            except Exception as e:
                logger.warning("Error processing commit %s: %s", commit.sha, e)

        return results
    except GithubException as e:
        logger.warning("GitHub API exception during get_recent_commits: %s", e)
        return []


async def get_recent_commits(path_filter: str = "") -> list[CommitResult]:
    """
    Fetch recent commits from the last N days to check if a code change introduced the issue.
    Runs in asyncio executor to avoid blocking the event loop.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _get_recent_commits_blocking, path_filter)


async def execute_tool(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Execute a GitHub agent tool and return the serialisable result dict."""
    if tool_name == "search_code":
        results = await search_code(
            query=tool_input.get("query", ""),
            file_extension=tool_input.get("file_extension", "py"),
        )
        return {"code_results": [vars(r) for r in results]}
        
    elif tool_name == "get_recent_commits":
        results = await get_recent_commits(
            path_filter=tool_input.get("path_filter", ""),
        )
        return {"commit_results": [vars(r) for r in results]}
        
    elif tool_name == "write_draft_reply":
        # draft is handled entirely in the orchestrator/draft_writer
        return {"status": "draft_recorded"}
        
    else:
        return {"error": f"Unknown tool: {tool_name}"}
