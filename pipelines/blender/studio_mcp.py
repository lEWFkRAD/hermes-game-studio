"""Project-local MCP; start on the host owning STUDIO_ART_ROOT and Blender.

Explicit setup required. Does not alter Codex/global MCP registration or services.
"""
import os
from pathlib import Path
import sys

from mcp.server.fastmcp import FastMCP, Image
from studio_jobs import StudioJobs

mcp = FastMCP('configured Blender review studio')
jobs = StudioJobs(os.environ['STUDIO_ART_ROOT'], os.environ['STUDIO_BLENDER'])


@mcp.tool()
def review_blend(source_path: str, samples: int = 32, size: int = 1024, audit_only: bool = False, wait: bool = True) -> dict:
    """Review a named .blend under the art root; source is never saved.

    Opens a separate background Blender. Does not control any existing Blender window.
    By default keep this MCP call open until completion. wait=false returns a job id,
    but closing its MCP session can terminate child processes on Windows. Status
    detects that interruption; only an explicit resume starts a replacement job.
    """
    return jobs.submit(source_path, samples, size, audit_only, wait)


@mcp.tool()
def resume_blender_review(job_id: str, wait: bool = True) -> dict:
    """Explicitly retry an interrupted/failed review of the same unchanged source.

    Creates a new job linked to the previous receipt; preserves the original logs.
    Rejects a still-running review or a source that changed since submission.
    """
    return jobs.resume(job_id, wait)


@mcp.tool()
def review_job_status(job_id: str) -> dict:
    """Read completion, source identity/hash, UV/material/texture audit, and artifact paths."""
    return jobs.status(job_id)


@mcp.tool()
def review_preview(job_id: str) -> Image:
    """Return the actual rendered preview from a completed review, not a concept image."""
    status = jobs.status(job_id)
    if status['status'] != 'complete' or 'preview.png' not in status['artifacts']:
        raise ValueError('This job has no completed preview. Read review_job_status first.')
    return Image(path=status['artifacts']['preview.png'])


if __name__ == '__main__':
    mcp.run(transport='stdio')
